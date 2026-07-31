"""TOML config loading and validation.

Secrets never live in the config file: credentials are resolved from the
environment or a chmod-600 file whose *path* is configured here. Putting a
literal key in the config is rejected outright.
"""

from __future__ import annotations

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
class Config:
    projects: list[ProjectConfig]
    poll_interval_s: int = 60
    max_workers: int = 4
    state_dir: Path = Path("~/.local/state/issuefleet").expanduser()
    worktree_root: Path = Path("~/worktrees").expanduser()
    # agent behavior
    max_auto_turns: int = 40
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
    # Linear auth mode: "auto" infers from the token prefix (lin_api_ = raw
    # personal key, lin_oauth_ = Bearer OAuth/agent token); or force one.
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


def _path(v: str) -> Path:
    return Path(v).expanduser()


def load(path: str | Path) -> Config:
    path = Path(path).expanduser()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}")
    return parse(data, source=str(path))


def parse(data: dict, source: str = "<config>") -> Config:
    daemon = data.get("daemon", {})
    creds = data.get("credentials", {})
    agent = data.get("agent", {})
    hooks = data.get("webhooks", {})
    for name, table in (
        ("daemon", daemon),
        ("credentials", creds),
        ("agent", agent),
        ("webhooks", hooks),
    ):
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [{name}] must be a table")
        _reject_secrets(table, f"{source} [{name}]")

    raw_projects = data.get("projects", [])
    if not raw_projects:
        raise ConfigError(f"{source}: at least one [[projects]] entry is required")

    projects = []
    for i, p in enumerate(raw_projects):
        where = f"{source} [[projects]] #{i + 1}"
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
        projects.append(
            ProjectConfig(
                name=p["name"],
                linear_project=p["linear_project"],
                repo=_path(p["repo"]),
                claim=ClaimRule(strategy=strategy, value=claim_raw.get("value", "")),
                base_ref=p.get("base_ref", "main"),
                branch_template=p.get("branch_template", "agent/{key}-{slug}"),
                state_in_progress=p.get("state_in_progress", "In Progress"),
                state_done=p.get("state_done", "Done"),
                delete_remote_branch=p.get("delete_remote_branch", True),
                max_workers=p.get("max_workers"),
            )
        )
    names = [p.name for p in projects]
    if len(set(names)) != len(names):
        raise ConfigError(f"{source}: duplicate [[projects]] name")

    cfg = Config(
        projects=projects,
        poll_interval_s=int(daemon.get("poll_interval_s", 60)),
        max_workers=int(daemon.get("max_workers", 4)),
        max_auto_turns=int(agent.get("max_auto_turns", 40)),
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
        if creds["linear_auth"] not in ("auto", "api_key", "oauth"):
            raise ConfigError(f"{source}: linear_auth must be auto, api_key, or oauth")
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

    if cfg.poll_interval_s < 5:
        raise ConfigError(f"{source}: poll_interval_s must be >= 5")
    if cfg.max_workers < 1:
        raise ConfigError(f"{source}: max_workers must be >= 1")
    return cfg
