#!/bin/sh
# Runs as root for exactly this much: make the docker socket usable, then
# drop to the operator's uid (workers inherit it via the launcher, and
# claude refuses bypassPermissions as root). Needed because group_add
# can't help on Docker Desktop, which mounts the socket root:root 0755 —
# no group has write until we chmod it.
set -eu
# A real passwd/group entry for the target uid: without one, whoami/id -un/
# $USER-dependent tools (shell scripts included) fail in confusing ways.
if ! getent passwd "${ISSUEFLEET_UID:?set by env.sh via compose}" >/dev/null 2>&1; then
  echo "fleet:x:${ISSUEFLEET_UID}:${ISSUEFLEET_GID:?set by env.sh via compose}::/home/fleet:/bin/bash" >> /etc/passwd
fi
if ! getent group "${ISSUEFLEET_GID}" >/dev/null 2>&1; then
  echo "fleet:x:${ISSUEFLEET_GID}:" >> /etc/group
fi
# HOME and .config must be writable by the operator uid: the config
# bind-mount creates .config as root, but the launcher writes its own
# state to HOME/.config/claude-container. chown the dirs (not the
# read-only mount inside .config) to the target uid.
chown "${ISSUEFLEET_UID}:${ISSUEFLEET_GID}" /home/fleet /home/fleet/.config 2>/dev/null || true
SOCK=/var/run/docker.sock
sock_gid=0
if [ -S "$SOCK" ]; then
  sock_gid=$(stat -c %g "$SOCK")
  chmod g+rw "$SOCK" 2>/dev/null || true
fi
exec setpriv \
  --reuid "${ISSUEFLEET_UID}" \
  --regid "${ISSUEFLEET_GID}" \
  --groups "$sock_gid" \
  env USER=fleet LOGNAME=fleet "$@"
