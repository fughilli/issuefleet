"""Pick and build the right Forge for a project.

A fleet can mix GitHub and GitLab projects, so forge construction is a factory
keyed on the project's remote. The kind is taken from the project's explicit
``forge`` setting when it has one, else inferred from the remote host — the
common gitlab.com / ``gitlab.*`` hosts are recognized, and everything else
defaults to GitHub (so existing single-forge configs are untouched). A
self-hosted GitLab on an unrecognizable host is named explicitly with
``forge = "gitlab"`` in the project config.

The factory closes over both forges' credentials (built once at startup) and is
handed the project's remote URL at call time, so it needs no network of its own.
"""

from __future__ import annotations

from issuefleet.config import ProjectConfig
from issuefleet.giturl import parse_remote
from issuefleet.github import GithubForge
from issuefleet.gitlab import GitlabForge

FORGE_KINDS = ("github", "gitlab")


def infer_kind(host: str) -> str:
    """Guess the forge kind from a remote host. Only the unambiguous GitLab
    hosts are recognized; anything else is GitHub, which keeps existing configs
    (and GitHub Enterprise on a custom host) working without a `forge` setting."""
    h = host.lower()
    if h == "gitlab.com" or h.startswith("gitlab.") or ".gitlab." in h:
        return "gitlab"
    return "github"


def forge_kind(project: ProjectConfig, host: str) -> str:
    """The forge kind for a project: its explicit ``forge`` when set, else
    inferred from the remote host."""
    return project.forge or infer_kind(host)


def build_forge(project: ProjectConfig, remote: str, gh_token_source, gl_token_source):
    """Construct the project's Forge from its remote URL.

    ``gh_token_source`` is ``owner -> (callable | token)`` (GitHub App tokens
    are per-owner and expire hourly); ``gl_token_source`` is ``() -> token``
    for GitLab (a single access token, host-wide). Either may be None when no
    project of that kind is configured; asking for the missing one raises, which
    the caller surfaces as a config/credential error."""
    host, slug = parse_remote(remote)
    kind = forge_kind(project, host)
    if kind == "gitlab":
        if gl_token_source is None:
            raise ValueError(
                f"project {project.name!r} is a GitLab project but no GitLab token is "
                "configured (set gitlab_token_env / gitlab_token_file in [credentials])"
            )
        return GitlabForge(gl_token_source(), slug, host=host)
    if gh_token_source is None:
        raise ValueError(f"project {project.name!r}: no GitHub credential is configured")
    return GithubForge(gh_token_source(slug.split("/")[0]), slug)
