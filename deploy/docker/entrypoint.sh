#!/bin/sh
# Runs as root for exactly this much: make the docker socket usable, then
# drop to the operator's uid (workers inherit it via the launcher, and
# claude refuses bypassPermissions as root). Needed because group_add
# can't help on Docker Desktop, which mounts the socket root:root 0755 —
# no group has write until we chmod it.
set -eu
SOCK=/var/run/docker.sock
sock_gid=0
if [ -S "$SOCK" ]; then
  sock_gid=$(stat -c %g "$SOCK")
  chmod g+rw "$SOCK" 2>/dev/null || true
fi
exec setpriv \
  --reuid "${ISSUEFLEET_UID:?set by env.sh via compose}" \
  --regid "${ISSUEFLEET_GID:?set by env.sh via compose}" \
  --groups "$sock_gid" \
  "$@"
