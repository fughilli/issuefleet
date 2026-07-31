#!/usr/bin/env bash
# Fleet status inside the running daemon container.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:status}/deploy/docker"
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
exec docker compose exec issuefleet bin/issuefleet status
