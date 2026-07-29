"""The issuefleet CLI: doctor / run / once / status / attach / stop / logs."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from issuefleet import config as config_mod
from issuefleet import creds
from issuefleet.config import Config, ConfigError
from issuefleet.doctor import run_doctor
from issuefleet.github import GithubForge, parse_repo_slug
from issuefleet.gitops import Gitops
from issuefleet.linear import LinearClient, LinearTracker
from issuefleet.mailbox import Mailbox
from issuefleet.model import WorkerRecord
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry
from issuefleet.runner import TmuxRunner

DEFAULT_CONFIG = "~/.config/issuefleet/config.toml"

log = logging.getLogger("issuefleet")


def build_stack(cfg: Config) -> Reconciler:
    linear_key, _ = creds.resolve_linear_key(cfg)
    tracker = LinearTracker(LinearClient(linear_key))
    github_token, _ = creds.resolve_github_token(cfg)
    git = Gitops()
    forges = {}
    for project in cfg.projects:
        slug = parse_repo_slug(git.remote_url(project.repo))
        forges[project.name] = GithubForge(github_token, slug)
    registry = Registry(cfg.state_dir)
    runner = TmuxRunner(log_dir=cfg.state_dir / "logs")
    return Reconciler(cfg, registry, tracker, forges, git, runner)


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


def cmd_run(cfg: Config) -> int:
    stop = {"flag": False}

    def _sig(signum, frame):
        log.info("signal %d: finishing this tick, then exiting (agents keep running)", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    with DaemonLock(cfg.state_dir):
        reconciler = build_stack(cfg)
        log.info("daemon up: %d project(s), poll every %ds", len(cfg.projects), cfg.poll_interval_s)
        while not stop["flag"]:
            started = time.monotonic()
            try:
                reconciler.tick()
            except Exception:
                log.exception("tick failed; retrying next interval")
            remaining = cfg.poll_interval_s - (time.monotonic() - started)
            while remaining > 0 and not stop["flag"]:
                time.sleep(min(1, remaining))
                remaining -= 1
    return 0


def cmd_status(cfg: Config) -> int:
    registry = Registry(cfg.state_dir)
    runner = TmuxRunner(log_dir=cfg.state_dir / "logs")
    workers = registry.all()
    if not workers:
        print("fleet empty")
        return 0
    for rec in workers:
        agent_dir = Path(rec.worktree) / ".agent"
        turn_info = "no state"
        try:
            state = json.loads((agent_dir / "state.json").read_text())
            turn_info = (
                f"{state.get('phase')}, turn {state.get('turns_taken', 0)}, "
                f"auto {state.get('auto_turns', 0)}/{state.get('max_auto_turns', '?')}"
            )
        except (OSError, json.JSONDecodeError):
            pass
        mb = Mailbox(agent_dir / "mailbox")
        alive = "alive" if runner.alive(rec) else "DEAD"
        pr = f"PR #{rec.pr_number}" if rec.pr_number else "no PR"
        # Turn-log mtime distinguishes "mid-turn, streaming" from "wedged".
        newest = max(
            (f.stat().st_mtime for f in (agent_dir / "logs").glob("turn-*")), default=None
        )
        activity = f"last activity {int(time.time() - newest)}s ago" if newest else "no turns yet"
        print(f"{rec.issue_key} [{rec.project}] {rec.phase}/{alive} — {rec.issue_title}")
        print(f"    agent: {turn_info}; {pr}; restarts {rec.restarts}; {activity}")
        print(
            f"    branch {rec.branch}; outbox pending {len(mb.pending_outbox())}, "
            f"inbox pending {len(mb.pending_inbox())}"
        )
        print(f"    watch: tmux attach -t {rec.tmux_session}   log: {runner.log_path(rec)}")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="issuefleet", description=__doc__)
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"config path (default {DEFAULT_CONFIG})")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="verify tooling, credentials, config; show what would be claimed")
    p = sub.add_parser("once", help="a single reconcile tick (cron-friendly)")
    p.add_argument("--dry-run", action="store_true", help="log would-be actions; mutate nothing")
    sub.add_parser("run", help="the daemon")
    sub.add_parser("status", help="fleet state")
    for name, help_ in (
        ("attach", "attach to a worker's tmux session"),
        ("stop", "wind one worker down"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("issue", help="issue key, e.g. FUG-12")
    p = sub.add_parser("logs", help="show a worker's output")
    p.add_argument("issue")
    p.add_argument("-f", "--follow", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config).expanduser()
    if args.cmd == "doctor":
        return run_doctor(config_path)
    try:
        cfg = config_mod.load(config_path)
    except ConfigError as e:
        raise SystemExit(f"config error: {e} (run `issuefleet doctor`)")

    try:
        if args.cmd == "once":
            return cmd_once(cfg, args.dry_run)
        if args.cmd == "run":
            return cmd_run(cfg)
        if args.cmd == "status":
            return cmd_status(cfg)
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
