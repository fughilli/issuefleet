#!/usr/bin/env bash
# Doctor inside the homelab stack: exec into the running daemon container,
# or — if the daemon is down/crash-looping (exactly when you want doctor) —
# a one-shot container with the same mounts. Either way the tailscale
# service must be up: the daemon container shares its network namespace.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:doctor}/deploy/docker"
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
if docker compose ps --status running issuefleet 2>/dev/null | grep -q issuefleet; then
  exec docker compose exec issuefleet bin/issuefleet --config /etc/issuefleet/config.toml doctor
fi
echo "note: issuefleet service not running; using a one-shot container" >&2
exec docker compose run --rm issuefleet bin/issuefleet --config /etc/issuefleet/config.toml doctor
