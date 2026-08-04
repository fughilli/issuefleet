"""Credential resolution. Secrets come from the environment or a chmod-600
file — never the config file (config.py enforces the latter)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from issuefleet.config import Config


class CredentialError(Exception):
    pass


def _read_key_file(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None
    return text or None


def file_permissions_ok(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return True
    return not (mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH))


def resolve_optional(env_name: str, file_path: Path) -> str | None:
    """Env-then-file secret lookup that returns None instead of raising —
    for optional secrets like webhook signing keys."""
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    return _read_key_file(Path(file_path))


def resolve_linear_key(cfg: Config) -> tuple[str, str]:
    """Returns (key, source-description). Raises CredentialError if absent."""
    v = os.environ.get(cfg.linear_api_key_env)
    if v:
        return v.strip(), f"env ${cfg.linear_api_key_env}"
    v = _read_key_file(cfg.linear_api_key_file)
    if v:
        return v, str(cfg.linear_api_key_file)
    raise CredentialError(
        f"no Linear API key: set ${cfg.linear_api_key_env} or write the key to "
        f"{cfg.linear_api_key_file} (chmod 600). Create one at "
        "https://linear.app/settings/api"
    )


def linear_uses_app_token(cfg: Config) -> bool:
    """True when the daemon should mint its own Linear token via the
    client_credentials grant (30-day app-actor token, auto-refetched) rather
    than reading a static personal/OAuth key from env-or-file."""
    return cfg.linear_auth == "client_credentials"


def resolve_linear_oauth_client(cfg: Config) -> tuple[str, str]:
    """(client_id, client_secret) for the client_credentials grant. The id is
    not a secret (lives in config); the secret follows the env-then-file rule.
    Raises CredentialError if either is missing."""
    if not cfg.linear_oauth_client_id:
        raise CredentialError(
            "linear_auth = 'client_credentials' needs [credentials] "
            "linear_oauth_client_id (the OAuth app's client id)"
        )
    secret = resolve_optional(
        cfg.linear_oauth_client_secret_env, cfg.linear_oauth_client_secret_file
    )
    if not secret:
        raise CredentialError(
            f"no Linear OAuth client secret: set ${cfg.linear_oauth_client_secret_env} "
            f"or write it to {cfg.linear_oauth_client_secret_file} (chmod 600)"
        )
    return cfg.linear_oauth_client_id, secret


def resolve_sigbot_key(cfg: Config) -> tuple[str, str]:
    """(key, source-description) for the fleet manager's sigbot API key.
    Env-then-file, same rule as every other secret. Raises CredentialError if
    absent — the fleet manager can't reach its Signal group without it."""
    fm = cfg.fleet_manager
    v = os.environ.get(fm.api_key_env)
    if v:
        return v.strip(), f"env ${fm.api_key_env}"
    v = _read_key_file(fm.api_key_file)
    if v:
        return v, str(fm.api_key_file)
    raise CredentialError(
        f"no sigbot API key: set ${fm.api_key_env} or write it to "
        f"{fm.api_key_file} (chmod 600). Mint one in the sigbot dashboard."
    )


def resolve_anthropic_key(cfg: Config) -> str | None:
    """Optional Anthropic API key for the fleet manager's 'claude' advisor.
    Env-then-file; returns None when absent (the advisor falls back to
    escalate-everything). Not resolved from the config file (a secret)."""
    return resolve_optional(
        "ANTHROPIC_API_KEY", Path("~/.config/issuefleet/anthropic.key").expanduser()
    )


def github_auth_mode(cfg: Config) -> str:
    """'app' or 'token'. auto = app when the App ID is configured and its
    private key file exists, else fall back to a PAT."""
    if cfg.github_auth != "auto":
        return cfg.github_auth
    if cfg.github_app_id and cfg.github_app_key_file.is_file():
        return "app"
    return "token"


def resolve_github_token(cfg: Config) -> tuple[str, str]:
    """Env vars in configured order, then the key file, then `gh auth token`
    if gh happens to exist (brief §5.3)."""
    for env in cfg.github_token_env:
        v = os.environ.get(env)
        if v:
            return v.strip(), f"env ${env}"
    v = _read_key_file(cfg.github_token_file)
    if v:
        return v, str(cfg.github_token_file)
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "gh auth token"
        except OSError:
            pass
    envs = " or ".join(f"${e}" for e in cfg.github_token_env)
    raise CredentialError(
        f"no GitHub token: set {envs} or write a fine-grained PAT "
        f"(Contents: RW, Pull requests: RW) to {cfg.github_token_file} (chmod 600)"
    )
