"""The issuefleet CLI: doctor / run / once / status / attach / stop / logs."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from issuefleet import config as config_mod
from issuefleet import creds
from issuefleet.config import Config, ConfigError
from issuefleet.doctor import run_doctor
from issuefleet.github import GithubForge, parse_repo_slug
from issuefleet import gitops as gitops_mod
from issuefleet.gitops import Gitops
from issuefleet.linear import LinearClient, LinearTracker, client_from_config
from issuefleet.mailbox import Mailbox
from issuefleet.model import WorkerRecord
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry
from issuefleet.runner import TmuxRunner

DEFAULT_CONFIG = "~/.config/issuefleet/config.toml"

log = logging.getLogger("issuefleet")


def build_stack(cfg: Config) -> Reconciler:
    tracker = LinearTracker(client_from_config(cfg))
    git = Gitops()
    if creds.github_auth_mode(cfg) == "app":
        from issuefleet.githubapp import AppTokenProvider

        provider = AppTokenProvider(
            cfg.github_app_id,
            cfg.github_app_key_file,
            installation_id=cfg.github_app_installation_id,
        )

        def token_source(owner: str):
            return lambda: provider.token_for_owner(owner)

    else:
        github_token, _ = creds.resolve_github_token(cfg)

        def token_source(owner: str):
            return github_token

    forges = {}
    for project in cfg.projects:
        # Slug from whichever source exists — the forge (and its scoped
        # token) must exist BEFORE the clone, which uses it over HTTPS so
        # no SSH key is ever needed.
        if git.is_repo(project.repo):
            slug = parse_repo_slug(git.remote_url(project.repo))
        elif project.git_url:
            slug = parse_repo_slug(project.git_url)
        else:
            raise SystemExit(
                f"[{project.name}] repo {project.repo} does not exist and the project "
                "has no git_url to clone from"
            )
        forge = GithubForge(token_source(slug.split("/")[0]), slug)
        try:
            clone_url, clone_auth = forge.push_spec()
            action = gitops_mod.ensure_checkout(
                git, project, clone_url=clone_url, auth_header=clone_auth
            )
            if action:
                log.info("[%s] %s -> %s", project.name, project.repo, action)
        except gitops_mod.GitError as e:
            raise SystemExit(f"[{project.name}] {e}")
        forges[project.name] = forge
    registry = Registry(cfg.state_dir)
    runner = TmuxRunner(log_dir=cfg.state_dir / "logs")
    return Reconciler(cfg, registry, tracker, forges, git, runner)


def build_fleet_manager(cfg: Config, reconciler: Reconciler):
    """The fleet manager, or None when disabled. Shares the reconciler's
    tracker and registry so both see the same fleet; credentials (sigbot key,
    advisor key) resolve host-side, same as everything else."""
    fm = cfg.fleet_manager
    if not fm.enabled:
        return None
    from issuefleet.advisor import build_advisor
    from issuefleet.fleet_manager import FleetManager
    from issuefleet.sigbot import SigbotClient

    api_key, _ = creds.resolve_sigbot_key(cfg)  # raises CredentialError if absent
    signal = SigbotClient(fm.base_url, api_key)
    anthropic_key = creds.resolve_anthropic_key(cfg)
    advisor = build_advisor(fm.advisor, anthropic_key)
    # The same key makes the inbound Signal path agentic; without it the manager
    # falls back to its deterministic dispatch (see FleetManager._handle_inbound).
    return FleetManager(
        cfg, reconciler.tracker, signal, advisor, reconciler.registry, agent_key=anthropic_key
    )


class DaemonLock:
    """One reconciling process per state dir — two daemons would double-relay.
    (`status`/`logs`/`attach` don't take it; they only read.)"""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "daemon.lock"
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                f"another issuefleet daemon holds {self.path}; "
                "stop it first (its agents keep running either way)"
            )
        self.fd.write(str(os.getpid()))
        self.fd.flush()
        return self

    def __exit__(self, *exc):
        self.fd.close()


def _find_worker(registry: Registry, key: str) -> WorkerRecord:
    for rec in registry.all():
        if rec.issue_key.lower() == key.lower():
            return rec
    raise SystemExit(
        f"no worker for {key!r}; known: {', '.join(w.issue_key for w in registry.all()) or 'none'}"
    )


def cmd_once(cfg: Config, dry_run: bool) -> int:
    if dry_run:
        rec = build_stack(cfg)
        print("Dry run — the next tick would:")
        for line in rec.plan():
            print(f"  {line}")
        return 0
    with DaemonLock(cfg.state_dir):
        build_stack(cfg).tick()
    return 0


def _webhook_bind(wcfg) -> str:
    # The containerized stack must bind beyond loopback: docker's port
    # publish forwards to the container's eth0 IP, and the sidecar-shared
    # netns exposes 0.0.0.0 only to the docker bridge + tailnet (endpoints
    # stay HMAC-verified). Env override keeps the laptop default loopback.
    return os.environ.get("ISSUEFLEET_WEBHOOK_BIND", wcfg.bind)


def _start_webhooks(cfg: Config, reconciler: Reconciler, wake: threading.Event):
    from issuefleet import webhooks as webhooks_mod

    wcfg = cfg.webhooks
    github_secret = creds.resolve_optional(wcfg.github_secret_env, wcfg.github_secret_file)
    linear_secret = creds.resolve_optional(wcfg.linear_secret_env, wcfg.linear_secret_file)
    if not github_secret and not linear_secret:
        log.warning("[webhooks] enabled but no signing secrets resolve; not starting listener")
        return None

    def on_session(evt):
        reconciler.enqueue_session(evt)
        if evt.action == "created":
            # Linear marks the session unresponsive without an activity
            # within 10s; ack from a thread so the webhook 200 isn't held.
            threading.Thread(
                target=reconciler._emit_activity_quietly,
                args=(evt.session_id,
                      {"type": "thought",
                       "body": "On it — spinning up an isolated worker (worktree + "
                               "container). Progress will stream here."}),
                daemon=True,
            ).start()

    server = webhooks_mod.WebhookServer(
        bind=_webhook_bind(wcfg),
        port=wcfg.port,
        wake=wake.set,
        on_session=on_session,
        github_secret=github_secret,
        linear_secret=linear_secret,
    ).start()
    log.info(
        "webhook listener on %s:%d (/webhook/github%s, /webhook/linear%s) — put a tunnel in front",
        _webhook_bind(wcfg), server.port,
        "" if github_secret else " [no secret: disabled]",
        "" if linear_secret else " [no secret: disabled]",
    )
    return server


def _dashboard_bind(dcfg) -> str:
    # Same story as the webhook listener: the containerized stack must bind
    # beyond loopback (docker publishes to the container IP), so an env
    # override lets the compose file open it up while a laptop stays on
    # loopback. Never expose it publicly — Stop is a real control.
    return os.environ.get("ISSUEFLEET_DASHBOARD_BIND", dcfg.bind)


def _start_dashboard(cfg: Config, reconciler: Reconciler, wake: threading.Event):
    from issuefleet.dashboard import DashboardServer, FleetView

    dcfg = cfg.dashboard

    def stop_cb(issue_key: str) -> None:
        reconciler.enqueue_stop(issue_key)
        wake.set()

    view = FleetView(cfg.state_dir, stop_cb=stop_cb)
    bind = _dashboard_bind(dcfg)
    server = DashboardServer(bind=bind, port=dcfg.port, view=view).start()
    log.info("dashboard on http://%s:%d (introspection web UI; put a private tunnel in front)",
             bind, server.port)
    return server


def cmd_run(cfg: Config) -> int:
    stop = {"flag": False}
    wake = threading.Event()

    def _sig(signum, frame):
        log.info("signal %d: finishing this tick, then exiting (agents keep running)", signum)
        stop["flag"] = True
        wake.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    with DaemonLock(cfg.state_dir):
        reconciler = build_stack(cfg)
        server = _start_webhooks(cfg, reconciler, wake) if cfg.webhooks.enabled else None
        dashboard = _start_dashboard(cfg, reconciler, wake) if cfg.dashboard.enabled else None
        fleet = None
        if cfg.fleet_manager.enabled:
            try:
                fleet = build_fleet_manager(cfg, reconciler)
                log.info("fleet manager up: sigbot %s, board %r, advisor=%s",
                         cfg.fleet_manager.base_url, cfg.fleet_manager.board_project,
                         cfg.fleet_manager.advisor)
            except Exception:
                # Never let a fleet-manager startup problem (missing sigbot key,
                # unreadable state file, …) take down the reconcile loop.
                log.exception("fleet manager enabled but not startable; running without it")
        # The loop wakes often enough to poll Signal at the fleet manager's
        # cadence when it's the tighter interval.
        loop_interval = cfg.poll_interval_s
        if fleet is not None:
            loop_interval = min(loop_interval, cfg.fleet_manager.poll_interval_s)
        log.info("daemon up: %d project(s), poll every %ds%s%s%s",
                 len(cfg.projects), loop_interval,
                 " + webhook wake-ups" if server else "",
                 " + dashboard" if dashboard else "",
                 " + fleet manager" if fleet else "")
        from issuefleet.model import PHASE_CRASHED

        crashed = [w.issue_key for w in reconciler.registry.all() if w.phase == PHASE_CRASHED]
        if crashed:
            log.warning(
                "holding %d CRASHED worker(s), not auto-restarting: %s — worktrees kept "
                "for inspection; release with 'issuefleet stop <KEY>' and re-delegate",
                len(crashed), ", ".join(crashed),
            )
        try:
            while not stop["flag"]:
                wake.clear()
                try:
                    reconciler.tick()
                except Exception:
                    log.exception("tick failed; retrying next interval")
                if fleet is not None:
                    try:
                        fleet.tick()
                    except Exception:
                        log.exception("fleet manager tick failed; retrying next interval")
                # Sleep until the poll interval elapses OR a webhook wakes us.
                if wake.wait(timeout=loop_interval):
                    log.debug("woken early by webhook")
        finally:
            if server:
                server.stop()
            if dashboard:
                dashboard.stop()
    return 0


def cmd_linear_oauth(cfg: Config) -> int:
    from issuefleet import oauth

    if not cfg.linear_oauth_client_id:
        raise SystemExit(
            "set [credentials] linear_oauth_client_id first (create the OAuth app at "
            "https://linear.app/settings/api/applications/new, enable webhooks with "
            "'Agent session events', and point its webhook URL at your tunnel)"
        )
    client_secret = creds.resolve_optional(
        cfg.linear_oauth_client_secret_env, cfg.linear_oauth_client_secret_file
    )
    if not client_secret:
        raise SystemExit(
            f"no OAuth client secret: set ${cfg.linear_oauth_client_secret_env} or write it "
            f"to {cfg.linear_oauth_client_secret_file} (chmod 600)"
        )
    redirect_uri = f"http://localhost:{cfg.linear_oauth_redirect_port}/callback"
    url = oauth.build_authorize_url(cfg.linear_oauth_client_id, redirect_uri)
    print("Open this URL as a workspace ADMIN (actor=app install):\n\n  " + url + "\n")
    print(f"Waiting for the redirect on {redirect_uri} …")
    code = oauth.wait_for_code(cfg.linear_oauth_redirect_port)
    token = oauth.exchange_code(
        cfg.linear_oauth_client_id, client_secret, code, redirect_uri
    )
    cfg.linear_api_key_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.linear_api_key_file.write_text(token)
    cfg.linear_api_key_file.chmod(0o600)
    tracker = LinearTracker(LinearClient(token))
    viewer = tracker.viewer()
    print(f"\nInstalled. Agent app user: {viewer.get('name')} (id {viewer.get('id')})")
    print(f"Token written to {cfg.linear_api_key_file} (chmod 600).")
    print("The daemon will now authenticate as the agent; @-mention or delegate "
          "issues to it in Linear to claim them.")
    return 0


def cmd_status(cfg: Config) -> int:
    from issuefleet.dashboard import worker_snapshot

    registry = Registry(cfg.state_dir)
    runner = TmuxRunner(log_dir=cfg.state_dir / "logs")
    workers = registry.all()
    if not workers:
        print("fleet empty")
        return 0
    for rec in workers:
        s = worker_snapshot(rec, runner)
        agent_dir = Path(rec.worktree) / ".agent"
        turn_info = (
            f"{s['turn_phase']}, turn {s['turns_taken']}, "
            f"auto {s['auto_turns']}/{s['max_auto_turns'] or '?'}"
            if s["turn_phase"] else "no state"
        )
        alive = "alive" if s["alive"] else "DEAD"
        pr = f"PR #{s['pr_number']}" if s["pr_number"] else "no PR"
        # Turn-log mtime distinguishes "mid-turn, streaming" from "wedged".
        activity = (
            f"last activity {s['last_activity_s']}s ago"
            if s["last_activity_s"] is not None else "no turns yet"
        )
        print(f"{s['issue_key']} [{s['project']}] {s['phase']}/{alive} — {s['issue_title']}")
        print(f"    agent: {turn_info}; {pr}; restarts {s['restarts']}; {activity}")
        print(
            f"    branch {s['branch']}; outbox pending {s['outbox_pending']}, "
            f"inbox pending {s['inbox_pending']}"
        )
        print(f"    watch: tmux attach -t {s['tmux_session']}")
        print(f"    turn logs: {agent_dir / 'logs'}   pane log: {runner.log_path(rec)}")
    return 0


def cmd_attach(cfg: Config, key: str) -> int:
    rec = _find_worker(Registry(cfg.state_dir), key)
    os.execvp("tmux", ["tmux", "attach", "-t", f"={rec.tmux_session}"])


def cmd_stop(cfg: Config, key: str) -> int:
    reconciler = build_stack(cfg)
    rec = _find_worker(reconciler.registry, key)
    project = cfg.project(rec.project)
    mailbox = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
    reconciler._wind_down(rec, project, mailbox, reason="stopped by operator", done=False)
    print(f"{rec.issue_key}: wound down (branch {rec.branch} kept; transcript archived)")
    return 0


def cmd_logs(cfg: Config, key: str, follow: bool) -> int:
    registry = Registry(cfg.state_dir)
    runner = TmuxRunner(log_dir=cfg.state_dir / "logs")
    rec = _find_worker(registry, key)
    path = runner.log_path(rec)
    if not path.exists():
        raise SystemExit(f"no log yet at {path}")
    if follow:
        os.execvp("tail", ["tail", "-n", "100", "-f", str(path)])
    subprocess.run(["tail", "-n", "100", str(path)])
    return 0


def cmd_fleet(cfg: Config) -> int:
    import json

    fm = cfg.fleet_manager
    if not fm.enabled:
        print("fleet manager: disabled ([fleet_manager] enabled = false)")
        return 0
    print(f"fleet manager: enabled — sigbot {fm.base_url}, board {fm.board_project!r} "
          f"(team {fm.board_team!r}), advisor={fm.advisor}")
    try:
        state = json.loads((cfg.state_dir / "fleet_manager.json").read_text())
    except FileNotFoundError:
        print("  no state yet (daemon hasn't run the fleet manager)")
        return 0
    print(f"  signal cursor: {state.get('signal_cursor')}")
    pending = state.get("pending", [])
    if not pending:
        print("  awaiting human input: none")
    else:
        print(f"  awaiting human input ({len(pending)}):")
        for p in pending:
            print(f"    {p['issue_key']}: {p['question'][:100]}")
    return 0


def cmd_github_app_setup(cfg: Config, args) -> int:
    from issuefleet import githubapp

    port = args.port
    redirect_url = f"http://localhost:{port}/callback"
    webhook_url = args.webhook_url
    if not webhook_url:
        print("NOTE: no --webhook-url given; the app is created without a webhook. "
              "Add one later in the app's settings (point it at your tunnel's "
              "/webhook/github) to get push wake-ups.")
    manifest = githubapp.build_manifest(args.name, redirect_url, webhook_url)
    target = (
        f"https://github.com/organizations/{args.org}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )
    html = githubapp.manifest_form_html(manifest, target)
    print(f"Open  http://localhost:{port}/  in your browser and click "
          "'Create GitHub App' (one click; no token needed).")
    code = githubapp.run_manifest_flow(port, html)
    app = githubapp.convert_manifest_code(code)

    cfg.github_app_key_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.github_app_key_file.write_text(app["pem"])
    cfg.github_app_key_file.chmod(0o600)
    print(f"Private key written to {cfg.github_app_key_file} (chmod 600).")
    if app.get("webhook_secret"):
        wsf = cfg.webhooks.github_secret_file
        wsf.parent.mkdir(parents=True, exist_ok=True)
        wsf.write_text(app["webhook_secret"])
        wsf.chmod(0o600)
        print(f"Webhook secret written to {wsf} (chmod 600).")

    slug = app.get("slug", "?")
    print(f"\nApp created: {app.get('html_url')} — PRs will open as {slug}[bot].")
    print("\nAdd to your config [credentials]:\n"
          f"  github_app_id = \"{app['id']}\"\n"
          "\nThen INSTALL it on the target repos (required):\n"
          f"  https://github.com/apps/{slug}/installations/new\n"
          "\nFinally: bin/issuefleet doctor")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Global flags live in a parent attached to the main parser AND every
    # subparser, so `issuefleet run -v` and `issuefleet -v run` both work —
    # a trailing -v after the subcommand once bootlooped the container
    # (usage error -> exit -> restart: unless-stopped). SUPPRESS keeps a
    # subparser's defaults from clobbering values parsed before the
    # subcommand; main() reads them via getattr with defaults.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help=f"config path (default {DEFAULT_CONFIG})")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="issuefleet", description=__doc__, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", parents=[common],
                   help="verify tooling, credentials, config; show what would be claimed")
    sub.add_parser("linear-oauth", parents=[common],
                   help="one-time Linear agent (actor=app) install; writes the token")
    p = sub.add_parser("github-app-setup", parents=[common],
                       help="create the GitHub App via the manifest flow; writes key + secret")
    p.add_argument("--name", default="issuefleet", help="app name (default: issuefleet)")
    p.add_argument("--org", help="create under this org instead of your user account")
    p.add_argument("--webhook-url", help="public URL for /webhook/github (your tunnel)")
    p.add_argument("--port", type=int, default=9780, help="localhost port for the flow")
    p = sub.add_parser("once", parents=[common], help="a single reconcile tick (cron-friendly)")
    p.add_argument("--dry-run", action="store_true", help="log would-be actions; mutate nothing")
    sub.add_parser("run", parents=[common], help="the daemon")
    sub.add_parser("status", parents=[common], help="fleet state")
    sub.add_parser("fleet", parents=[common],
                   help="fleet-manager state (Signal cursor, pending escalations)")
    for name, help_ in (
        ("attach", "attach to a worker's tmux session"),
        ("stop", "wind one worker down"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.add_argument("issue", help="issue key, e.g. FUG-12")
    p = sub.add_parser("logs", parents=[common], help="show a worker's output")
    p.add_argument("issue")
    p.add_argument("-f", "--follow", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(getattr(args, "config", DEFAULT_CONFIG)).expanduser()
    if args.cmd == "doctor":
        return run_doctor(config_path)
    try:
        cfg = config_mod.load(config_path)
    except ConfigError as e:
        raise SystemExit(f"config error: {e} (run `issuefleet doctor`)")

    try:
        if args.cmd == "linear-oauth":
            return cmd_linear_oauth(cfg)
        if args.cmd == "github-app-setup":
            return cmd_github_app_setup(cfg, args)
        if args.cmd == "once":
            return cmd_once(cfg, args.dry_run)
        if args.cmd == "run":
            return cmd_run(cfg)
        if args.cmd == "status":
            return cmd_status(cfg)
        if args.cmd == "fleet":
            return cmd_fleet(cfg)
        if args.cmd == "attach":
            return cmd_attach(cfg, args.issue)
        if args.cmd == "stop":
            return cmd_stop(cfg, args.issue)
        if args.cmd == "logs":
            return cmd_logs(cfg, args.issue, args.follow)
    except creds.CredentialError as e:
        raise SystemExit(f"credential error: {e} (run `issuefleet doctor`)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
