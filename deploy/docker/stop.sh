#!/usr/bin/env bash
# Wind one worker down by hand: bazel run //deploy/docker:stop -- FUG-14
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:stop -- <ISSUE-KEY>}/deploy/docker"
[ $# -ge 1 ] || { echo "usage: bazel run //deploy/docker:stop -- <ISSUE-KEY>" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
if docker compose ps --status running issuefleet 2>/dev/null | grep -q issuefleet; then
  exec docker compose exec issuefleet /entrypoint.sh bin/issuefleet stop "$1"
fi
echo "note: issuefleet service not running; using a one-shot container" >&2
exec docker compose run --rm issuefleet bin/issuefleet stop "$1"
