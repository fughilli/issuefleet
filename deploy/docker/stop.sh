#!/usr/bin/env bash
# Wind one worker down by hand: bazel run //deploy/docker:stop -- FUG-14
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:stop -- <ISSUE-KEY>}/deploy/docker"
[ $# -ge 1 ] || { echo "usage: bazel run //deploy/docker:stop -- <ISSUE-KEY>" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
exec docker compose exec issuefleet /entrypoint.sh bin/issuefleet stop "$1"
