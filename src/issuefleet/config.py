"""TOML config loading and validation.

Secrets never live in the config file: credentials are resolved from the
environment or a chmod-600 file whose *path* is configured here. Putting a
literal key in the config is rejected outright.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# "agent" = no poll-based claiming; issues are claimed only by delegating
# (assigning) them to the Linear agent or @-mentioning it (agent sessions).
CLAIM_STRATEGIES = ("label", "assignee", "state", "agent")

_FORBIDDEN_SECRET_KEYS = (
    "linear_api_key",
    "github_token",
    "gh_token",
    "token",
    "api_key",
)


class ConfigError(Exception):
    pass


@dataclass
class EnvSource:
    """Where a worker environment variable's value comes from.

    Exactly one field is set. ``file`` and ``env`` name a location and keep the
    secret out of the config file (and out of git); ``value`` is a literal, for
    non-secrets only — ``parse`` rejects a literal that looks like a key.
    """

    file: Path | None = None
    env: str | None = None
    value: str | None = None

    def resolve(self) -> str | None:
        """The value, or None when the source isn't there. Never raises: a
        missing key must degrade the worker (the overlay it feeds decides how),
        not take the daemon down."""
        if self.value is not None:
            return self.value
        if self.env is not None:
            return os.environ.get(self.env)
        if self.file is not None:
            try:
                return self.file.read_text().strip()
            except OSError:
                return None
        return None

    def describe(self) -> str:
        if self.value is not None:
            return "literal"
        if self.env is not None:
            return f"${self.env}"
        return str(self.file)


@dataclass
class ClaimRule:
    strategy: str  # label | assignee | state
    value: str  # label name | Linear user id/email | workflow state name

    def matches(self, issue) -> bool:
        if self.strategy == "label":
            return self.value in issue.labels
        if self.strategy == "assignee":
            return issue.assignee_id == self.value
        if self.strategy == "state":
            return issue.state_name == self.value
        if self.strategy == "agent":
            return False  # poll path claims nothing; sessions claim directly
        raise ConfigError(f"unknown claim strategy {self.strategy!r}")


@dataclass
class ProjectConfig:
    name: str  # short handle, used in paths and logs
    linear_project: str  # Linear project name or UUID
    repo: Path  # local main checkout (push remote = origin)
    claim: ClaimRule
    # Remote to clone from when `repo` doesn't exist yet — the daemon
    # bootstraps the checkout itself, and `repo` is always a real clone it
    # owns. Without it, a missing repo is an error. (Only owner/name is
    # parsed from this; the clone itself goes over HTTPS with the GitHub
    # App's scoped token.)
    git_url: str | None = None
    base_ref: str = "main"
    branch_template: str = "agent/{key}-{slug}"
    state_in_progress: str = "In Progress"
    state_done: str = "Done"
    delete_remote_branch: bool = True
    max_workers: int | None = None  # per-project cap; None = only global cap


@dataclass
class WebhookConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"  # put a tunnel in front; never expose directly
    port: int = 8787
    # Secrets resolved env-then-file, same rules as API credentials.
    github_secret_env: str = "ISSUEFLEET_GITHUB_WEBHOOK_SECRET"
    github_secret_file: Path = Path("~/.config/issuefleet/github_webhook.secret").expanduser()
    linear_secret_env: str = "ISSUEFLEET_LINEAR_WEBHOOK_SECRET"
    linear_secret_file: Path = Path("~/.config/issuefleet/linear_webhook.secret").expanduser()


@dataclass
class DashboardConfig:
    """The introspection + light-control web UI. It can wind a worker down and
    (unless ``allow_add_project`` is off) add a project to the fleet, so treat
    it as a control plane: bind it to the tailnet, not to a public interface.
    The default bind is ``0.0.0.0`` so a plain host run is reachable from other
    machines on the tailnet; keep it behind Tailscale (a private serve, or just
    the tailnet itself) and never point a public Funnel at it."""

    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8788
    # Whether the dashboard may add a project to the fleet (clone the repo and
    # persist a new [[projects]] entry to the config file). On by default —
    # anyone who can reach the dashboard can already stop workers — but can be
    # turned off for a look-but-don't-touch deployment.
    allow_add_project: bool = True


@dataclass
class FleetManagerConfig:
    """The fleet manager: a host-side singleton that bridges a Signal group
    (fronted by a sigbot service) to the fleet. It records user goals as issues
    on a dedicated top-level board — and, with an Anthropic key, manages issues
    across every project board it knows (filing onto and updating any of them,
    not just the goals board) — watches the workers, unblocks agents whose
    questions the ticket/board context answers, and routes the rest to the
    human over Signal. Disabled by default — the daemon runs the reconcile loop
    with or without it."""

    enabled: bool = False
    # sigbot service (one Signal group, one persona). base_url is not a secret;
    # the API key follows the usual env-then-file rule (never the config file).
    base_url: str = ""
    api_key_env: str = "ISSUEFLEET_SIGBOT_API_KEY"
    api_key_file: Path = Path("~/.config/issuefleet/sigbot.key").expanduser()
    # The dedicated top-level board where user goals are recorded as issues.
    # A team is required (a brand-new goal has no context issue to inherit one
    # from); the project is where the goals land and is polled for progress.
    board_project: str = ""  # Linear project name or UUID
    board_team: str = ""  # Linear team name, key, or UUID
    poll_interval_s: int = 60  # how often to check Signal + the fleet
    report_interval_s: int = 3600  # progress-report cadence to Signal; 0 = never
    # Assign filed goals to the fleet's agent identity so they auto-claim under
    # the 'agent' claim strategy. Off if you'd rather triage goals by hand.
    assign_goals: bool = True
    # Triage backend for blocked-worker questions: 'conservative' always
    # escalates to the human (deterministic, no LLM); 'claude' asks the model
    # whether ticket/board context answers it first.
    advisor: str = "conservative"


@dataclass
class Config:
    projects: list[ProjectConfig]
    poll_interval_s: int = 60
    max_workers: int = 4
    state_dir: Path = Path("~/.local/state/issuefleet").expanduser()
    worktree_root: Path = Path("~/worktrees").expanduser()
    # agent behavior
    max_auto_turns: int = 50
    max_restarts: int = 3
    claude_args: list[str] = field(default_factory=list)
    # Workspace-local state copied from the parent checkout into each fresh
    # worktree (copy-if-missing), e.g. .claude/settings.local.json, which is
    # untracked and would otherwise be absent there. Git-excluded in the
    # worktree. (Skill approval is NOT here — claude-container keys it on
    # the main working tree in its user config dir, shared by worktrees.)
    copy_from_repo: list[str] = field(
        default_factory=lambda: [".claude", ".claude-container-overlay"]
    )
    # Host-side flags passed to the launcher before the in-container command.
    # --skills-ignore-new (claude-container > 1.6.12) starts the container
    # with only already-accepted skills instead of prompting for undecided
    # ones — a headless worker must never sit at a prompt. Set to [] for
    # older launchers (doctor checks the installed launcher knows each flag).
    launcher_args: list[str] = field(default_factory=lambda: ["--skills-ignore-new"])
    container_config_dir: Path | None = None  # None = launcher's shared default
    claude_container: str = "claude-container"
    # Environment handed to the launcher process for each worker, name -> where
    # to read the value ([agent.env] in the config file; values are file paths
    # or env var names, never secrets). A worker's container sees a variable
    # only if its workspace overlay also declares it in overlay.json "env" —
    # the launcher forwards by name, so nothing leaks into a container that
    # didn't ask. The motivating case is TS_AUTHKEY: led_mapper's overlay joins
    # the tailnet at container start so workers can reach the HITL rigs.
    worker_env: dict[str, EnvSource] = field(default_factory=dict)
    # credential lookup (values are env var names / file paths, never secrets)
    linear_api_key_env: str = "LINEAR_API_KEY"
    linear_api_key_file: Path = Path("~/.config/issuefleet/linear.key").expanduser()
    github_token_env: list[str] = field(default_factory=lambda: ["GITHUB_TOKEN", "GH_TOKEN"])
    github_token_file: Path = Path("~/.config/issuefleet/github.key").expanduser()
    # GitHub auth mode: "token" (PAT/machine user), "app" (GitHub App — PRs
    # open as <app>[bot]), or "auto" (app when app_id + key file exist).
    github_auth: str = "auto"
    github_app_id: str = ""  # numeric App ID (not secret)
    github_app_key_file: Path = Path("~/.config/issuefleet/github_app.pem").expanduser()
    github_app_installation_id: int | None = None  # None = discover per repo owner
    # Linear auth mode:
    #   "auto"  — infer from a static token's prefix (lin_api_ = raw personal
    #             key, lin_oauth_ = Bearer OAuth/agent token)
    #   "api_key" / "oauth" — force one of the above for a static token
    #   "client_credentials" — the daemon mints its OWN app-actor token from
    #             linear_oauth_client_id + the client secret (30-day, no
    #             browser, auto-refetched on expiry/401). Preferred for the
    #             long-running daemon: no daily re-auth. Needs no linear.key.
    linear_auth: str = "auto"
    # Linear OAuth app (for the agents platform). Client id is not a secret;
    # the client secret follows the usual env-then-file rules.
    linear_oauth_client_id: str = ""
    linear_oauth_client_secret_env: str = "LINEAR_OAUTH_CLIENT_SECRET"
    linear_oauth_client_secret_file: Path = Path(
        "~/.config/issuefleet/linear_oauth_client.secret"
    ).expanduser()
    linear_oauth_redirect_port: int = 9779
    webhooks: WebhookConfig = field(default_factory=WebhookConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    fleet_manager: FleetManagerConfig = field(default_factory=FleetManagerConfig)
    # The file this config was loaded from, when it came from disk. The daemon
    # writes back here when a project is added in-band (see append_project);
    # None for a config built straight from a dict (tests, `parse`).
    source_path: Path | None = None

    def project(self, name: str) -> ProjectConfig:
        for p in self.projects:
            if p.name == name:
                return p
        raise ConfigError(f"no [[projects]] entry named {name!r}")


def _reject_secrets(table: dict, where: str) -> None:
    for k in table:
        if k.lower() in _FORBIDDEN_SECRET_KEYS:
            raise ConfigError(
                f"{where}: refusing to read {k!r} from the config file — "
                "secrets go in the environment or a chmod-600 file "
                "(see linear_api_key_file / github_token_file)"
            )


# Path variables the daemon fills in itself when the environment doesn't.
# The homelab stack exports both (deploy/docker/env.sh) and same-path mounts
# them, so one config.toml is the same text on a laptop and in the container.
# ISSUEFLEET_ROOT is the data root; ISSUEFLEET_PROJECTS is where checkouts
# the daemon does NOT own live (a `repo` pointing at your own working tree —
# `~` can't be used there, since it is /root inside the container).
_PATH_VARS = {
    "ISSUEFLEET_ROOT": "~/.issuefleet",
    "ISSUEFLEET_PROJECTS": "~/Projects",
    # Worker Claude creds: the launcher's live shared config dir. Shared,
    # never copied — a snapshot's OAuth token is revoked when the host
    # rotates its own.
    "ISSUEFLEET_CLAUDE_CONFIG": "~/.config/claude-container/config",
}


def _parse_worker_env(table: object, source: str) -> dict[str, EnvSource]:
    """Parse [agent.env]: NAME = { file | env | value = ... }.

    Requiring the table form (rather than a bare string) keeps "where does this
    come from" explicit at the callsite — a bare path and a bare env var name
    are indistinguishable, and guessing wrong would silently hand a worker the
    literal string "/path/to/key"."""
    if not isinstance(table, dict):
        raise ConfigError(f"{source}: [agent.env] must be a table of NAME = {{ ... }}")
    out: dict[str, EnvSource] = {}
    for name, spec in table.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ConfigError(f"{source}: [agent.env] '{name}' is not a valid variable name")
        if not isinstance(spec, dict) or len(spec) != 1:
            raise ConfigError(
                f"{source}: [agent.env] {name} must set exactly one of "
                "file, env, or value (e.g. {name} = {{ file = \"~/.config/issuefleet/ts.key\" }})"
            )
        kind, raw = next(iter(spec.items()))
        if kind == "file":
            out[name] = EnvSource(file=_path(str(raw)))
        elif kind == "env":
            out[name] = EnvSource(env=str(raw))
        elif kind == "value":
            # A literal is fine for a hostname or a pool list, but a config
            # file is the one place a credential must never be.
            if _looks_secret(str(raw)):
                raise ConfigError(
                    f"{source}: [agent.env] {name} looks like a secret — "
                    "use file = or env = so it stays out of the config"
                )
            out[name] = EnvSource(value=str(raw))
        else:
            raise ConfigError(
                f"{source}: [agent.env] {name} has unknown source '{kind}' "
                "(expected file, env, or value)"
            )
    return out


_ADVISOR_KINDS = ("conservative", "claude")


def _parse_fleet_manager(table: dict, source: str) -> FleetManagerConfig:
    fm = FleetManagerConfig(
        enabled=bool(table.get("enabled", False)),
        base_url=str(table.get("base_url", "")),
        board_project=str(table.get("board_project", "")),
        board_team=str(table.get("board_team", "")),
        poll_interval_s=int(table.get("poll_interval_s", 60)),
        report_interval_s=int(table.get("report_interval_s", 3600)),
        assign_goals=bool(table.get("assign_goals", True)),
        advisor=str(table.get("advisor", "conservative")),
    )
    if "api_key_env" in table:
        fm.api_key_env = str(table["api_key_env"])
    if "api_key_file" in table:
        fm.api_key_file = _path(str(table["api_key_file"]))
    if fm.advisor not in _ADVISOR_KINDS:
        raise ConfigError(
            f"{source} [fleet_manager]: advisor must be one of {_ADVISOR_KINDS}, "
            f"got {fm.advisor!r}"
        )
    # Only validate the rest when it's actually turned on — a disabled section
    # (or none at all) must never block the daemon from starting.
    if fm.enabled:
        for req, key in (
            (fm.base_url, "base_url"),
            (fm.board_project, "board_project"),
            (fm.board_team, "board_team"),
        ):
            if not req:
                raise ConfigError(
                    f"{source} [fleet_manager]: {key} is required when enabled"
                )
        if fm.poll_interval_s < 5:
            raise ConfigError(f"{source} [fleet_manager]: poll_interval_s must be >= 5")
        if fm.report_interval_s < 0:
            raise ConfigError(f"{source} [fleet_manager]: report_interval_s must be >= 0")
    return fm


def _looks_secret(v: str) -> bool:
    """Catch the obvious paste-a-key-into-the-config mistake. Deliberately
    narrow: known credential prefixes, or a long opaque token-ish string."""
    prefixes = ("tskey-", "lin_api_", "lin_oauth_", "ghp_", "ghs_", "github_pat_", "sk-")
    if v.startswith(prefixes):
        return True
    return len(v) >= 40 and not any(c in v for c in " /\\")


def _path(v: str) -> Path:
    # Substituting the default ourselves (rather than leaning on expandvars)
    # keeps a shared config from leaving a literal "${ISSUEFLEET_ROOT}"
    # directory behind on plain laptop runs.
    for var, default in _PATH_VARS.items():
        if var not in os.environ:
            fallback = str(Path(default).expanduser())
            v = v.replace("${%s}" % var, fallback).replace("$%s" % var, fallback)
    return Path(os.path.expandvars(v)).expanduser()


def parse_project(p: dict, where: str) -> ProjectConfig:
    """Validate and build one ``[[projects]]`` entry. Shared by ``parse`` (the
    file loader) and the dashboard's add-project path, so a project typed into
    the web form is checked exactly the way one written into the config is."""
    if not isinstance(p, dict):
        raise ConfigError(f"{where}: a project must be a table")
    _reject_secrets(p, where)
    for req in ("name", "linear_project", "repo"):
        if not p.get(req):
            raise ConfigError(f"{where}: missing required key {req!r}")
    claim_raw = p.get("claim", {"strategy": "label", "value": "agent"})
    strategy = claim_raw.get("strategy", "label")
    if strategy not in CLAIM_STRATEGIES:
        raise ConfigError(
            f"{where}: claim.strategy must be one of {CLAIM_STRATEGIES}, got {strategy!r}"
        )
    if strategy != "agent" and not claim_raw.get("value"):
        raise ConfigError(f"{where}: claim.value is required (e.g. the label name)")
    if "local_checkout" in p:
        # Removed: `repo` is always a clone the daemon owns. Silently
        # ignoring the key would swap a symlink for a clone with no
        # warning, so say so instead.
        raise ConfigError(
            f"{where}: local_checkout is no longer supported — the daemon always "
            "clones into `repo` now. Drop the key (and set git_url if unset)."
        )
    max_workers = p.get("max_workers")
    if max_workers is not None:
        try:
            max_workers = int(max_workers)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: max_workers must be an integer")
        if max_workers < 1:
            raise ConfigError(f"{where}: max_workers must be >= 1")
    return ProjectConfig(
        name=p["name"],
        linear_project=p["linear_project"],
        repo=_path(p["repo"]),
        claim=ClaimRule(strategy=strategy, value=claim_raw.get("value", "")),
        git_url=p.get("git_url") or None,
        base_ref=p.get("base_ref") or "main",
        branch_template=p.get("branch_template") or "agent/{key}-{slug}",
        state_in_progress=p.get("state_in_progress") or "In Progress",
        state_done=p.get("state_done") or "Done",
        delete_remote_branch=bool(p.get("delete_remote_branch", True)),
        max_workers=max_workers,
    )


def _toml_str(v: str) -> str:
    """A TOML basic string. Escapes what the spec requires; enough for the
    project fields we serialize (names, paths, URLs, labels)."""
    out = v.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def project_to_toml(p: ProjectConfig) -> str:
    """Render a project back to a ``[[projects]]`` block. Used to append a
    dashboard-added project to the config file; kept minimal and canonical
    (only the fields the project carries), not a faithful echo of hand-written
    formatting."""
    lines = [
        "[[projects]]",
        f"name = {_toml_str(p.name)}",
        f"linear_project = {_toml_str(p.linear_project)}",
        f"repo = {_toml_str(str(p.repo))}",
    ]
    if p.git_url:
        lines.append(f"git_url = {_toml_str(p.git_url)}")
    lines.append(f"base_ref = {_toml_str(p.base_ref)}")
    if p.claim.strategy == "agent":
        lines.append("claim = { strategy = \"agent\" }")
    else:
        lines.append(
            f"claim = {{ strategy = {_toml_str(p.claim.strategy)}, "
            f"value = {_toml_str(p.claim.value)} }}"
        )
    if p.branch_template != "agent/{key}-{slug}":
        lines.append(f"branch_template = {_toml_str(p.branch_template)}")
    lines.append(f"state_in_progress = {_toml_str(p.state_in_progress)}")
    lines.append(f"state_done = {_toml_str(p.state_done)}")
    lines.append(f"delete_remote_branch = {str(p.delete_remote_branch).lower()}")
    if p.max_workers is not None:
        lines.append(f"max_workers = {p.max_workers}")
    return "\n".join(lines) + "\n"


def append_project(config_path: str | Path, p: ProjectConfig) -> None:
    """Persist a newly added project by appending its ``[[projects]]`` block to
    the config file. Appending (rather than rewriting) keeps every existing
    comment and hand-formatting intact — the file stays the operator's, we only
    add to the end. The caller appends only after the clone succeeds, so the
    file never grows an entry the daemon can't bring up on the next start."""
    path = Path(config_path).expanduser()
    existing = path.read_text() if path.exists() else ""
    sep = "" if existing.endswith("\n\n") or not existing else ("\n" if existing.endswith("\n") else "\n\n")
    with open(path, "a") as f:
        f.write(sep + project_to_toml(p))


def load(path: str | Path) -> Config:
    path = Path(path).expanduser()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}")
    cfg = parse(data, source=str(path))
    cfg.source_path = path
    return cfg


def parse(data: dict, source: str = "<config>") -> Config:
    daemon = data.get("daemon", {})
    creds = data.get("credentials", {})
    agent = data.get("agent", {})
    hooks = data.get("webhooks", {})
    dash = data.get("dashboard", {})
    fleet = data.get("fleet_manager", {})
    for name, table in (
        ("daemon", daemon),
        ("credentials", creds),
        ("agent", agent),
        ("webhooks", hooks),
        ("dashboard", dash),
        ("fleet_manager", fleet),
    ):
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{name}] must be a table")
        _reject_secrets(table, f"{source} [{name}]")

    raw_projects = data.get("projects", [])
    if not raw_projects:
        raise ConfigError(f"{source}: at least one [[projects]] entry is required")

    projects = [
        parse_project(p, f"{source} [[projects]] #{i + 1}")
        for i, p in enumerate(raw_projects)
    ]
    names = [p.name for p in projects]
    if len(set(names)) != len(names):
        raise ConfigError(f"{source}: duplicate [[projects]] name")

    cfg = Config(
        projects=projects,
        poll_interval_s=int(daemon.get("poll_interval_s", 60)),
        max_workers=int(daemon.get("max_workers", 4)),
        max_auto_turns=int(agent.get("max_auto_turns", 50)),
        max_restarts=int(agent.get("max_restarts", 3)),
        claude_args=list(agent.get("claude_args", [])),
        copy_from_repo=list(
            agent.get("copy_from_repo", [".claude", ".claude-container-overlay"])
        ),
        launcher_args=list(agent.get("launcher_args", ["--skills-ignore-new"])),
        claude_container=agent.get("claude_container", "claude-container"),
    )
    if "state_dir" in daemon:
        cfg.state_dir = _path(daemon["state_dir"])
    if "worktree_root" in daemon:
        cfg.worktree_root = _path(daemon["worktree_root"])
    if "container_config_dir" in agent:
        cfg.container_config_dir = _path(agent["container_config_dir"])
    cfg.worker_env = _parse_worker_env(agent.get("env", {}), source)
    if "linear_api_key_env" in creds:
        cfg.linear_api_key_env = creds["linear_api_key_env"]
    if "linear_api_key_file" in creds:
        cfg.linear_api_key_file = _path(creds["linear_api_key_file"])
    if "github_token_env" in creds:
        v = creds["github_token_env"]
        cfg.github_token_env = [v] if isinstance(v, str) else list(v)
    if "github_token_file" in creds:
        cfg.github_token_file = _path(creds["github_token_file"])
    if "github_auth" in creds:
        if creds["github_auth"] not in ("auto", "token", "app"):
            raise ConfigError(f"{source}: github_auth must be auto, token, or app")
        cfg.github_auth = creds["github_auth"]
    cfg.github_app_id = str(creds.get("github_app_id", "") or "")
    if "github_app_key_file" in creds:
        cfg.github_app_key_file = _path(creds["github_app_key_file"])
    if "github_app_installation_id" in creds:
        cfg.github_app_installation_id = int(creds["github_app_installation_id"])
    if "linear_auth" in creds:
        if creds["linear_auth"] not in ("auto", "api_key", "oauth", "client_credentials"):
            raise ConfigError(
                f"{source}: linear_auth must be auto, api_key, oauth, or client_credentials"
            )
        cfg.linear_auth = creds["linear_auth"]
    cfg.linear_oauth_client_id = creds.get("linear_oauth_client_id", "")
    if "linear_oauth_client_secret_env" in creds:
        cfg.linear_oauth_client_secret_env = creds["linear_oauth_client_secret_env"]
    if "linear_oauth_client_secret_file" in creds:
        cfg.linear_oauth_client_secret_file = _path(creds["linear_oauth_client_secret_file"])
    if "linear_oauth_redirect_port" in creds:
        cfg.linear_oauth_redirect_port = int(creds["linear_oauth_redirect_port"])

    cfg.webhooks = WebhookConfig(
        enabled=bool(hooks.get("enabled", False)),
        bind=hooks.get("bind", "127.0.0.1"),
        port=int(hooks.get("port", 8787)),
    )
    if "github_secret_env" in hooks:
        cfg.webhooks.github_secret_env = hooks["github_secret_env"]
    if "github_secret_file" in hooks:
        cfg.webhooks.github_secret_file = _path(hooks["github_secret_file"])
    if "linear_secret_env" in hooks:
        cfg.webhooks.linear_secret_env = hooks["linear_secret_env"]
    if "linear_secret_file" in hooks:
        cfg.webhooks.linear_secret_file = _path(hooks["linear_secret_file"])

    cfg.dashboard = DashboardConfig(
        enabled=bool(dash.get("enabled", True)),
        bind=dash.get("bind", "0.0.0.0"),
        port=int(dash.get("port", 8788)),
        allow_add_project=bool(dash.get("allow_add_project", True)),
    )

    cfg.fleet_manager = _parse_fleet_manager(fleet, source)

    if cfg.poll_interval_s < 5:
        raise ConfigError(f"{source}: poll_interval_s must be >= 5")
    if cfg.max_workers < 1:
        raise ConfigError(f"{source}: max_workers must be >= 1")
    return cfg
