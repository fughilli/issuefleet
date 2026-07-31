# The single declaration of the homelab stack's root directory, sourced by
# every bazel wrapper. $HOME/.issuefleet works on all platforms: on macOS it
# lives inside /Users, which Docker Desktop shares by default; on Linux it
# needs no sudo. Override by exporting ISSUEFLEET_ROOT before `bazel run`.
export ISSUEFLEET_ROOT="${ISSUEFLEET_ROOT:-$HOME/.issuefleet}"

# The tree is created on demand — no manual mkdir step. (Also prevents
# docker from creating root-owned dirs at mount time on Linux.)
mkdir -p "$ISSUEFLEET_ROOT"/{worktrees,repos,claude-config,state,config,ssh,bin}
chmod 700 "$ISSUEFLEET_ROOT/ssh" 2>/dev/null || true

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
  [ -f "$ISSUEFLEET_ROOT/config/config.toml" ] || missing="$missing
  - config: $ISSUEFLEET_ROOT/config/config.toml (see deploy/docker/README.md)"
  [ -n "$(ls -A "$ISSUEFLEET_ROOT/claude-config" 2>/dev/null)" ] || missing="$missing
  - worker claude credentials: cp -r ~/.config/claude-container/config/* $ISSUEFLEET_ROOT/claude-config/"
  [ -e "$ISSUEFLEET_ROOT/ssh/id_ed25519" ] || missing="$missing
  - push key: $ISSUEFLEET_ROOT/ssh/{id_ed25519,known_hosts}"
  if [ -n "$missing" ]; then
    echo "warning: still needed before workers can run:$missing" >&2
  fi
}
