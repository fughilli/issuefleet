"""`issuefleet doctor` — tell the operator precisely what is missing.

Safe and side-effect-free: every check is a read (filesystem stats, tool
lookups, API queries). Dependencies are injectable so the checks are
testable offline; when not injected, real clients are built from config.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from issuefleet import config as config_mod
from issuefleet import creds
from issuefleet.config import Config, ConfigError
from issuefleet.github import GithubForge, parse_repo_slug
from issuefleet.gitops import Gitops
from issuefleet.linear import LinearClient, LinearTracker, client_from_config
from issuefleet.reconcile import Reconciler
from issuefleet.registry import Registry

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✓", WARN: "⚠", FAIL: "✗"}


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""

    def render(self) -> str:
        return f" {_ICON[self.status]} {self.label}" + (f" — {self.detail}" if self.detail else "")


def _writable_ancestor(path: Path) -> bool:
    p = Path(path)
    while not p.exists():
        if p == p.parent:
            return False
        p = p.parent
    return os.access(p, os.W_OK)


def _check_tools(cfg: Config) -> list[Check]:
    out = []
    v = sys.version_info
    out.append(
        Check(OK if v >= (3, 11) else FAIL, f"python {v.major}.{v.minor}.{v.micro}",
              "" if v >= (3, 11) else "3.11+ required (tomllib)")
    )
    for tool, why, severity in (
        ("tmux", "workers run in detached tmux sessions", FAIL),
        ("docker", "claude-container needs it", FAIL),
        (cfg.claude_container, "the agent runner", FAIL),
        ("git", "worktrees and pushes", FAIL),
        ("claude", "only needed inside containers; harmless if absent on the host", WARN),
    ):
        path = shutil.which(tool)
        out.append(Check(OK if path else severity, tool, path or f"not on PATH — {why}"))
    return out


def _check_launcher_flags(cfg: Config) -> list[Check]:
    """Every configured launcher flag must be one the installed launcher
    understands — an unknown flag aborts every worker launch. Probed via
    `--help`, which is side-effect-free."""
    if not cfg.launcher_args:
        return []
    launcher = shutil.which(cfg.claude_container)
    if launcher is None:
        return []  # the tools check already failed this
    import subprocess

    try:
        proc = subprocess.run(
            [cfg.claude_container, "--help"], capture_output=True, text=True, timeout=15
        )
        help_text = proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return [Check(WARN, "launcher flags", f"could not run {cfg.claude_container} --help: {e}")]
    out = []
    for flag in cfg.launcher_args:
        name = flag.split("=")[0]
        if name in help_text:
            out.append(Check(OK, f"launcher flag {name}"))
        else:
            out.append(
                Check(
                    FAIL,
                    f"launcher flag {name}",
                    f"not in {cfg.claude_container} --help — launcher too old? "
                    "Upgrade it or remove the flag from [agent] launcher_args",
                )
            )
    return out


def _check_worker_env(cfg: Config) -> list[Check]:
    """Each [agent.env] entry must actually resolve. A missing one is a WARN,
    not a FAIL: workers still launch, they just start without the variable —
    which for TS_AUTHKEY means no tailnet, and every HITL reservation failing
    much further downstream, where the cause is far from obvious."""
    out = []
    for name, src in sorted(cfg.worker_env.items()):
        if src.resolve() is None:
            out.append(
                Check(
                    WARN,
                    f"worker env {name}",
                    f"no value at {src.describe()} — workers start without {name}",
                )
            )
        else:
            out.append(Check(OK, f"worker env {name}", f"from {src.describe()}"))
    return out


def _check_worker_runtime(cfg: Config) -> list[Check]:
    """The two conditions that silently break every worker launch when the
    daemon itself runs containerized (observed live on the first stack)."""
    out = []
    if hasattr(os, "geteuid"):
        if os.geteuid() == 0:
            out.append(Check(FAIL, "running as root",
                             "workers inherit this uid via the launcher, and claude "
                             "refuses bypassPermissions as root — every turn fails "
                             "instantly. Run the compose stack via the bazel targets "
                             "(env.sh exports your uid) or set `user:` yourself"))
        else:
            out.append(Check(OK, f"running as uid {os.geteuid()}", "workers inherit a non-root uid"))
    sock = Path("/var/run/docker.sock")
    if sock.exists():
        if os.access(sock, os.W_OK):
            out.append(Check(OK, "docker socket", "writable"))
        else:
            out.append(Check(FAIL, "docker socket", "/var/run/docker.sock exists but is not "
                             "writable by this uid — worker launches will fail; add the "
                             "socket's group (env.sh exports ISSUEFLEET_DOCKER_GID)"))
    return out


def _check_container_settings(cfg: Config) -> list[Check]:
    config_dir = cfg.container_config_dir or Path("~/.config/claude-container/config").expanduser()
    settings = config_dir / "settings.json"
    label = f"container settings {settings}"
    try:
        mode = json.loads(settings.read_text()).get("permissions", {}).get("defaultMode")
    except FileNotFoundError:
        return [Check(WARN, label, "not found — the launcher normally writes it on first run; "
                      "without defaultMode=bypassPermissions headless turns hang forever")]
    except (OSError, json.JSONDecodeError) as e:
        return [Check(FAIL, label, f"unreadable: {e}")]
    if mode == "bypassPermissions":
        return [Check(OK, label, "permissions.defaultMode=bypassPermissions")]
    return [Check(FAIL, label,
                  f"permissions.defaultMode={mode!r} — headless turns will hang on the first "
                  "permission prompt; expected 'bypassPermissions'")]


def _check_dirs(cfg: Config) -> list[Check]:
    out = []
    for name, path in (("state_dir", cfg.state_dir), ("worktree_root", cfg.worktree_root)):
        ok = _writable_ancestor(path)
        out.append(Check(OK if ok else FAIL, f"{name} {path}",
                         "writable" if ok else "no writable ancestor"))
    return out


def _check_webhooks(cfg: Config) -> list[Check]:
    w = cfg.webhooks
    if not w.enabled:
        return [Check(OK, "webhooks", "disabled — poll-only (fine, just slower)")]
    out = []
    have_any = False
    for provider, env, path in (
        ("github", w.github_secret_env, w.github_secret_file),
        ("linear", w.linear_secret_env, w.linear_secret_file),
    ):
        secret = creds.resolve_optional(env, path)
        if secret:
            have_any = True
            out.append(Check(OK, f"webhook secret ({provider})", f"resolves (${env} or {path})"))
            if not creds.file_permissions_ok(Path(path)):
                out.append(Check(WARN, f"{path}", "readable by group/other — chmod 600 it"))
        else:
            out.append(Check(WARN, f"webhook secret ({provider})",
                             f"missing — {provider} endpoint will be disabled "
                             f"(set ${env} or write {path})"))
    if not have_any:
        out.append(Check(FAIL, "webhooks", "enabled but no signing secrets resolve; the "
                         "listener will not start"))
    else:
        out.append(Check(OK, "webhook listener", f"will bind {w.bind}:{w.port} — put a "
                         "tunnel in front; never expose the port directly"))
    return out


def _check_dashboard(cfg: Config) -> list[Check]:
    d = cfg.dashboard
    if not d.enabled:
        return [Check(OK, "dashboard", "disabled")]
    return [Check(OK, "dashboard", f"will bind {d.bind}:{d.port} — introspection web UI; "
                  "loopback + a private tunnel (it can stop workers)")]


def _check_fleet_manager(cfg: Config) -> list[Check]:
    fm = cfg.fleet_manager
    if not fm.enabled:
        return [Check(OK, "fleet manager", "disabled")]
    out = [Check(OK, "fleet manager", f"enabled — board {fm.board_project!r} "
                 f"(team {fm.board_team!r}), advisor={fm.advisor}")]
    try:
        key, src = creds.resolve_sigbot_key(cfg)
        out.append(Check(OK, "sigbot key", f"resolves ({src})"))
    except creds.CredentialError as e:
        out.append(Check(FAIL, "sigbot key", str(e)))
    # The sigbot integration and the Anthropic advisor call are live-only; the
    # daemon speaks to them at runtime. Flag what's needed, don't dial out here.
    try:
        import sigbot_client  # noqa: F401
        out.append(Check(OK, "sigbot-client", "installed"))
    except ImportError:
        out.append(Check(FAIL, "sigbot-client", "not importable — run via `bazel run "
                         "//:issuefleet` (the lock provides it), or `pip install "
                         "sigbot-client` into this interpreter"))
    if fm.advisor == "claude":
        if creds.resolve_anthropic_key(cfg):
            out.append(Check(OK, "advisor key", "Anthropic key resolves"))
        else:
            out.append(Check(WARN, "advisor key", "advisor=claude but no ANTHROPIC_API_KEY / "
                             "~/.config/issuefleet/anthropic.key — will escalate everything"))
    return out


def _check_security(cfg: Config) -> list[Check]:
    sec = cfg.security
    if sec.mode == "off":
        return [Check(WARN, "security gate", "off — the `ready` diff is NOT scanned "
                      "for leaked credentials before it's pushed")]
    label = "blocks a leaky `ready`" if sec.mode == "block" else "warns but still pushes"
    out = [Check(OK, "security gate", f"mode={sec.mode} ({label}); "
                 "deterministic secret scan on the pushed diff")]
    if sec.deep_scan == "claude":
        if creds.resolve_anthropic_key(cfg):
            out.append(Check(OK, "security deep-scan", "Anthropic key resolves"))
        else:
            out.append(Check(WARN, "security deep-scan", "deep_scan=claude but no "
                             "ANTHROPIC_API_KEY / ~/.config/issuefleet/anthropic.key — "
                             "will fall back to the deterministic scanner"))
    return out


def _check_linear(cfg: Config, tracker) -> list[Check]:
    out = []
    if creds.linear_uses_app_token(cfg):
        try:
            client_id, _ = creds.resolve_linear_oauth_client(cfg)
        except creds.CredentialError as e:
            return [Check(FAIL, "Linear app credentials", str(e))]
        out.append(Check(OK, "Linear app credentials",
                         f"client_credentials grant (client id {client_id}); "
                         "30-day app token, auto-refetched"))
        if not creds.file_permissions_ok(cfg.linear_oauth_client_secret_file):
            out.append(Check(WARN, f"{cfg.linear_oauth_client_secret_file}",
                             "readable by group/other — chmod 600 it"))
    else:
        try:
            key, source = creds.resolve_linear_key(cfg)
        except creds.CredentialError as e:
            return [Check(FAIL, "Linear API key", str(e))]
        mode = LinearClient(key, auth=cfg.linear_auth).auth
        out.append(Check(OK, "Linear API key",
                         f"from {source} ({'agent/OAuth token, Bearer' if mode == 'oauth' else 'personal key, raw header'})"))
        if not creds.file_permissions_ok(cfg.linear_api_key_file):
            out.append(Check(WARN, f"{cfg.linear_api_key_file}",
                             "readable by group/other — chmod 600 it"))
    if tracker is None:
        tracker = LinearTracker(client_from_config(cfg))
    try:
        viewer = tracker.viewer()
        out.append(Check(OK, "Linear API", f"authenticated as {viewer.get('name')} "
                         f"({viewer.get('email', 'no email')})"))
    except Exception as e:
        out.append(Check(FAIL, "Linear API", str(e)))
        return out

    for project in cfg.projects:
        try:
            issues = tracker.open_issues(project)
            if project.claim.strategy == "agent":
                me = tracker.get_viewer_id()
                eligible = [i for i in issues if me in (i.assignee_id, i.delegate_id)]
                claim_desc = (f"{len(eligible)} delegated to the agent "
                              "(also claims via @-mention webhooks)")
            else:
                eligible = [i for i in issues if project.claim.matches(i)]
                claim_desc = (f"{len(eligible)} eligible "
                              f"({project.claim.strategy}={project.claim.value!r})")
            out.append(Check(OK, f"[{project.name}] Linear project {project.linear_project!r}",
                             f"{len(issues)} open issue(s), {claim_desc}"))
            if issues:
                states = tracker._states_for_issue(issues[0].id)
                for state in (project.state_in_progress, project.state_done):
                    if state.lower() not in states:
                        out.append(Check(FAIL, f"[{project.name}] workflow state {state!r}",
                                         f"not found on the team; available: {sorted(states)}"))
                    else:
                        out.append(Check(OK, f"[{project.name}] workflow state {state!r}"))
            else:
                out.append(Check(WARN, f"[{project.name}] workflow states",
                                 "no open issues, cannot verify state names yet"))
        except Exception as e:
            out.append(Check(FAIL, f"[{project.name}] Linear project", str(e)))
    return out


def _check_github(cfg: Config, git: Gitops, forges: dict | None) -> list[Check]:
    out = []
    mode = creds.github_auth_mode(cfg)
    token_source = None
    if mode == "app":
        if shutil.which("openssl") is None:
            return [Check(FAIL, "GitHub App auth", "openssl not on PATH — required to sign "
                          "the app JWT (RS256 is beyond the Python stdlib)")]
        if not cfg.github_app_id:
            return [Check(FAIL, "GitHub App auth", "github_auth=app but github_app_id is unset")]
        if not cfg.github_app_key_file.is_file():
            return [Check(FAIL, "GitHub App auth",
                          f"private key not found at {cfg.github_app_key_file} — generate one "
                          "on the app's settings page and save it there (chmod 600)")]
        if not creds.file_permissions_ok(cfg.github_app_key_file):
            out.append(Check(WARN, f"{cfg.github_app_key_file}",
                             "readable by group/other — chmod 600 it"))
        if forges is None:  # live probe only when not injected with fakes
            from issuefleet.githubapp import AppTokenProvider, GithubAppError

            try:
                provider = AppTokenProvider(
                    cfg.github_app_id, cfg.github_app_key_file,
                    installation_id=cfg.github_app_installation_id,
                )
                slug_name = provider.app_slug()
                installs = "pinned installation" if cfg.github_app_installation_id else \
                    "installed on: " + ", ".join(sorted(provider.installations()))
                out.append(Check(OK, "GitHub App", f"{slug_name}[bot] (app id "
                                 f"{cfg.github_app_id}); {installs}"))
                token_source = lambda owner: (lambda: provider.token_for_owner(owner))
            except (GithubAppError, Exception) as e:
                out.append(Check(FAIL, "GitHub App", str(e)))
                return out
        else:
            out.append(Check(OK, "GitHub App", f"app id {cfg.github_app_id} (fake probe)"))
    else:
        try:
            token, source = creds.resolve_github_token(cfg)
        except creds.CredentialError as e:
            return [Check(FAIL, "GitHub token", str(e))]
        out.append(Check(OK, "GitHub token", f"from {source}"))
        if not creds.file_permissions_ok(cfg.github_token_file):
            out.append(Check(WARN, f"{cfg.github_token_file}",
                             "readable by group/other — chmod 600 it"))
        token_source = lambda owner: token

    for project in cfg.projects:
        name = project.name
        have_clone = git.is_repo(project.repo)
        if not have_clone:
            if project.git_url:
                out.append(Check(WARN, f"[{name}] repo {project.repo}",
                                 f"missing — will be cloned from {project.git_url} on first run"))
            else:
                out.append(Check(FAIL, f"[{name}] repo {project.repo}",
                                 "does not exist — add git_url so the daemon can clone it"))
                continue
        try:
            remote = git.remote_url(project.repo) if have_clone else project.git_url
            slug = parse_repo_slug(remote)
            out.append(Check(OK, f"[{name}] origin", f"{remote} -> {slug}"))
        except Exception as e:
            out.append(Check(FAIL, f"[{name}] origin remote", str(e)))
            continue
        forge = (forges or {}).get(name) or GithubForge(token_source(slug.split("/")[0]), slug)
        try:
            forge.repo_accessible()
            out.append(Check(OK, f"[{name}] GitHub API", f"can read {slug}"))
        except Exception as e:
            out.append(Check(FAIL, f"[{name}] GitHub API", str(e)))
        if have_clone:
            try:
                git.has_commits_ahead(project.repo, project.base_ref)  # resolves the base ref
                out.append(Check(OK, f"[{name}] base ref {project.base_ref!r}"))
            except Exception as e:
                out.append(Check(FAIL, f"[{name}] base ref {project.base_ref!r}", str(e)))
    return out


def run_doctor(
    config_path: Path,
    tracker=None,
    forges: dict | None = None,
    git: Gitops | None = None,
    runner=None,
    stream=sys.stdout,
) -> int:
    """Returns the process exit code (0 = healthy or warnings only)."""
    print(f"issuefleet doctor — config {config_path}", file=stream)
    try:
        cfg = config_mod.load(config_path)
        checks = [Check(OK, "config", f"{len(cfg.projects)} project(s)")]
    except ConfigError as e:
        checks = [Check(FAIL, "config", str(e))]
        for c in checks:
            print(c.render(), file=stream)
        print("\n1 problem must be fixed before anything else can be checked.", file=stream)
        return 1

    git = git or Gitops()
    checks += _check_tools(cfg)
    checks += _check_worker_runtime(cfg)
    checks += _check_launcher_flags(cfg)
    checks += _check_worker_env(cfg)
    checks += _check_container_settings(cfg)
    checks += _check_dirs(cfg)
    checks += _check_webhooks(cfg)
    checks += _check_dashboard(cfg)
    checks += _check_fleet_manager(cfg)
    checks += _check_security(cfg)
    linear_checks = _check_linear(cfg, tracker)
    checks += linear_checks
    checks += _check_github(cfg, git, forges)

    for c in checks:
        print(c.render(), file=stream)

    # The would-claim report: exactly what `run` would pick up, claim-order.
    if tracker is not None or not any(c.status == FAIL for c in linear_checks):
        try:
            registry = Registry(cfg.state_dir)
            if tracker is None:
                tracker = LinearTracker(client_from_config(cfg))
            rec = Reconciler(cfg, registry, tracker, forges or {}, git, runner or _NullRunner())
            eligible = {p.name: tracker.eligible_issues(p) for p in cfg.projects}
            claim_now, waiting = rec.claim_queue(eligible)
            print("\nWould claim now:", file=stream)
            if not claim_now:
                print("  (nothing)", file=stream)
            for issue, project in claim_now:
                print(f"  {issue.key} [{project.name}] p{issue.priority} — {issue.title}", file=stream)
            for issue, project in waiting:
                print(f"  (waiting, fleet full) {issue.key} — {issue.title}", file=stream)
            if registry.all():
                print(f"\nRegistered workers: {len(registry.all())} "
                      f"(see `issuefleet status`)", file=stream)
        except Exception as e:
            print(f"\nwould-claim report unavailable: {e}", file=stream)

    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    print(f"\n{len(fails)} problem(s), {len(warns)} warning(s).", file=stream)
    return 1 if fails else 0


class _NullRunner:
    def alive(self, rec) -> bool:
        return False
