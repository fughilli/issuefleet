#!/usr/bin/env bash
# Follow the daemon's reconcile-loop log (ticks, claims, relays, webhook
# events). Extra args pass to `docker compose logs`.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:logs}/deploy/docker"
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
exec docker compose logs -f --tail 100 issuefleet "$@"
