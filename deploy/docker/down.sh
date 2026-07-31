#!/usr/bin/env bash
# Stop the homelab stack. Worker containers launched by the daemon are
# siblings, not children — they survive this and keep running.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:down}/deploy/docker"
source ./env.sh
exec docker compose down "$@"
