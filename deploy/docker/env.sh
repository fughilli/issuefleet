# The single declaration of the homelab stack's root directory, sourced by
# every bazel wrapper. $HOME/.issuefleet works on all platforms: on macOS it
# lives inside /Users, which Docker Desktop shares by default; on Linux it
# needs no sudo. Override by exporting ISSUEFLEET_ROOT before `bazel run`.
export ISSUEFLEET_ROOT="${ISSUEFLEET_ROOT:-$HOME/.issuefleet}"
# Config + secrets: same location as a laptop setup.
export ISSUEFLEET_CONFIG="${ISSUEFLEET_CONFIG:-$HOME/.config/issuefleet}"
# The daemon container runs as YOUR uid (root would break every worker:
# claude refuses bypassPermissions as root). Docker-socket access is
# handled in-container by entrypoint.sh, which stats the REAL mounted
# socket — host-side stats are wrong on macOS.
export ISSUEFLEET_UID="${ISSUEFLEET_UID:-$(id -u)}"
export ISSUEFLEET_GID="${ISSUEFLEET_GID:-$(id -g)}"
# Checkouts the daemon does NOT own — where a `repo` that points at your own
# working tree lives. Same-path mounted like the root, because the launcher
# resolves a linked worktree's .git pointer in THIS container and then hands
# the resulting path to the host docker daemon; both sides must agree.
export ISSUEFLEET_PROJECTS="${ISSUEFLEET_PROJECTS:-$HOME/Projects}"

# The tree is created on demand — no manual mkdir step. (Also prevents
# docker from creating root-owned dirs at mount time on Linux.)
mkdir -p "$ISSUEFLEET_ROOT"/{worktrees,repos,claude-config,state,bin}
mkdir -p "$ISSUEFLEET_PROJECTS"

# Called by up/doctor (not down): seed what can be seeded safely and name
# what the operator still has to provide. The launcher is a plain script,
# so copying it from PATH is safe; credentials are never copied silently.
issuefleet_preflight() {
  local launcher="$ISSUEFLEET_ROOT/bin/claude-container"
  if [ ! -s "$launcher" ] && command -v claude-container >/dev/null 2>&1; then
    cp "$(command -v claude-container)" "$launcher"
    echo "note: seeded launcher into $launcher from PATH" >&2
  fi
  local missing=""
  # An absent launcher FILE would silently become a directory at bind-mount
  # time — warn loudly instead.
  [ -s "$launcher" ] || missing="$missing
  - launcher: cp \$(command -v claude-container) $launcher"
  [ -f "$HOME/.config/issuefleet/config.toml" ] || missing="$missing
  - config: ~/.config/issuefleet/config.toml (same file as a laptop setup)"
  [ -n "$(ls -A "$ISSUEFLEET_ROOT/claude-config" 2>/dev/null)" ] || missing="$missing
  - worker claude credentials: cp -r ~/.config/claude-container/config/* $ISSUEFLEET_ROOT/claude-config/"
  if [ -n "$missing" ]; then
    echo "warning: still needed before workers can run:$missing" >&2
  fi
}
