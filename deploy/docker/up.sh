#!/usr/bin/env bash
# Bring up the homelab stack (daemon + tailscale funnel), detached.
# Needs: docker on PATH, .env with TS_AUTHKEY next to the compose file, and
# the $HOME/.issuefleet host prep from deploy/docker/README.md.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:up}/deploy/docker"
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
issuefleet_preflight
[ -f .env ] || echo "warning: no .env here — tailscale needs TS_AUTHKEY=... in it" >&2
exec docker compose up -d --build "$@"
