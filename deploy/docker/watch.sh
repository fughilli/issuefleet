#!/usr/bin/env bash
# The outer deploy loop: keep the homelab stack running AND current.
#
# This supervises the docker-compose stack (issuefleet daemon + tailscale
# funnel) and, on an interval, promotes the daemon to the newest build of
# `main` — without a human ever running `up` again. Two ways to get "newest":
#
#   IMAGE mode (default, ISSUEFLEET_WATCH_MODE=image): the optimal path.
#     CI publishes ghcr.io/fughilli/issuefleet:latest on every push to main,
#     so we just `docker compose pull issuefleet` and, if the local :latest
#     resolves to a new image id, recreate the daemon to run it. No source
#     tree, no build — the box consumes exactly what the action built.
#
#   SOURCE mode (ISSUEFLEET_WATCH_MODE=source): the fallback, for a box that
#     can't pull the image (private registry, air-gapped, or you want the
#     box to build). We `git fetch`, and if origin/<branch> moved, fast-
#     forward the checkout and `up -d --build issuefleet`.
#
# Either way only the `issuefleet` service is recreated: the tailscale
# sidecar keeps the tailnet/funnel up, and worker containers are siblings of
# the daemon (launched on the host docker daemon) so they survive a daemon
# swap untouched — an in-flight worker is not interrupted by a deploy.
#
# Run it in the foreground (a terminal, tmux), or install the systemd unit
# deploy/issuefleet-watch.service to autostart it on boot. Stop with
# SIGTERM/SIGINT: the loop exits cleanly and leaves the stack running.
#
#   bazel run //deploy/docker:watch                 # image mode, 5-min poll
#   ISSUEFLEET_WATCH_INTERVAL=60 bazel run //deploy/docker:watch
#   ISSUEFLEET_WATCH_MODE=source bazel run //deploy/docker:watch
set -euo pipefail

cd "${BUILD_WORKSPACE_DIRECTORY:?run via: bazel run //deploy/docker:watch}"
REPO_ROOT="$PWD"
cd deploy/docker
command -v docker >/dev/null 2>&1 || { echo "error: docker not on PATH" >&2; exit 1; }
source ./env.sh
issuefleet_preflight

MODE="${ISSUEFLEET_WATCH_MODE:-image}"
INTERVAL="${ISSUEFLEET_WATCH_INTERVAL:-300}"
# In source mode, the ref whose movement triggers a rebuild.
BRANCH="${ISSUEFLEET_WATCH_BRANCH:-main}"
SERVICE=issuefleet

log() { printf '%s watch: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Exit cleanly on signals: the stack keeps running (we only stop watching).
running=1
trap 'running=0' TERM INT

# Image id the running SERVICE container is actually executing (empty if the
# service isn't up). This is the source of truth for "what is deployed", not
# the :latest tag, which we may have just pulled ahead of a recreate.
deployed_image_id() {
  local cid
  cid="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"
  [ -n "$cid" ] || return 0
  docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || true
}

# Local image id the :latest tag currently points at (empty if not pulled).
local_latest_id() {
  docker image inspect --format '{{.Id}}' ghcr.io/fughilli/issuefleet:latest 2>/dev/null || true
}

# Bring the stack up if it isn't already. `up -d` is idempotent: it no-ops
# for services whose config+image are unchanged, and starts what's missing.
ensure_up() {
  log "ensuring stack is up (docker compose up -d)"
  if [ "$MODE" = source ]; then
    docker compose up -d --build
  else
    docker compose up -d
  fi
}

# IMAGE mode: pull :latest, and if it moved off what's deployed, recreate the
# daemon onto it. Returns without touching the stack when already current.
tick_image() {
  # `pull` only downloads layers when the remote digest changed, so this is
  # cheap on a steady state (a manifest HEAD) and self-limiting on churn.
  if ! docker compose pull "$SERVICE" 2>&1 | sed "s/^/$(date -u +%H:%M:%SZ) pull: /"; then
    log "pull failed (registry unreachable?); will retry next tick"
    return 0
  fi
  local latest deployed
  latest="$(local_latest_id)"
  deployed="$(deployed_image_id)"
  if [ -z "$latest" ]; then
    log "no local :latest image after pull; skipping"
    return 0
  fi
  if [ "$latest" = "$deployed" ]; then
    return 0
  fi
  log "new image: ${deployed:-<none>} -> ${latest}; recreating $SERVICE"
  # Recreate only the daemon; tailscale + sibling workers are untouched.
  docker compose up -d "$SERVICE"
  log "deploy complete: $SERVICE now on ${latest}"
}

# SOURCE mode: fast-forward the checkout to origin/<branch>, and if it moved,
# rebuild the daemon image from the tree and recreate onto it.
tick_source() {
  if ! git -C "$REPO_ROOT" fetch --quiet origin "$BRANCH" 2>/dev/null; then
    log "git fetch failed (network?); will retry next tick"
    return 0
  fi
  local local_sha remote_sha
  local_sha="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)"
  remote_sha="$(git -C "$REPO_ROOT" rev-parse "origin/$BRANCH" 2>/dev/null || true)"
  if [ -z "$remote_sha" ] || [ "$local_sha" = "$remote_sha" ]; then
    return 0
  fi
  log "origin/$BRANCH moved: ${local_sha:0:12} -> ${remote_sha:0:12}"
  # Fast-forward only: refuse to clobber local commits (a dirty homelab
  # checkout is an operator mistake worth surfacing, not silently resetting).
  if ! git -C "$REPO_ROOT" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1; then
    log "cannot fast-forward $BRANCH (local diverged/dirty?); skipping build"
    return 0
  fi
  log "rebuilding + recreating $SERVICE from source"
  docker compose up -d --build "$SERVICE"
  log "deploy complete: $SERVICE rebuilt at ${remote_sha:0:12}"
}

log "starting: mode=$MODE interval=${INTERVAL}s branch=$BRANCH"
ensure_up
while [ "$running" = 1 ]; do
  if [ "$MODE" = source ]; then
    tick_source || log "tick errored; continuing"
  else
    tick_image || log "tick errored; continuing"
  fi
  # Sleep in short slices so a signal is honored within ~1s, not a full
  # interval. `wait` on a backgrounded sleep would also work but this keeps
  # the trap simple and portable.
  waited=0
  while [ "$running" = 1 ] && [ "$waited" -lt "$INTERVAL" ]; do
    sleep 1
    waited=$((waited + 1))
  done
done
log "stopped watching; stack left running"
