#!/usr/bin/env bash
# Build the issuefleet daemon image. Extra args pass through to docker build:
#   bazel run //deploy/docker:image -- --platform linux/arm64
# Override the tag with IMAGE_TAG=... ; CI pushes the canonical tags.
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:image}"
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
TAG="${IMAGE_TAG:-ghcr.io/fughilli/issuefleet:dev}"
exec docker build -f deploy/docker/Dockerfile -t "$TAG" "$@" .
