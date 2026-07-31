#!/usr/bin/env bash
# Attach to a worker's live tmux session, wherever it runs. Detach: Ctrl-b d.
#   bazel run //deploy/docker:attach -- FUG-14
# Workers of the containerized stack live inside the daemon container's
# tmux server, so we exec through compose (which allocates the tty); if the
# stack isn't running, fall back to the host tmux (laptop mode).
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:attach -- <ISSUE-KEY>}"
[ $# -ge 1 ] || { echo "usage: bazel run //deploy/docker:attach -- <ISSUE-KEY>" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
cd deploy/docker
source ./env.sh
if docker compose ps --status running issuefleet 2>/dev/null | grep -q issuefleet; then
  exec docker compose exec issuefleet /entrypoint.sh bin/issuefleet attach "$1"
fi
echo "note: containerized stack not running; attaching via the host tmux" >&2
cd "$BUILD_WORKSPACE_DIRECTORY"
exec bin/issuefleet attach "$1"
