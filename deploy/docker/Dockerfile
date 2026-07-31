# issuefleet daemon image.
#
# The daemon launches agent containers via the HOST's docker daemon (socket
# mount), so workers are siblings, not children. Everything the launcher
# bind-mounts into workers (worktrees, repos, claude config dir) MUST live
# at the same absolute path in this container and on the host — see
# docker-compose.yml and deploy/docker/README.md.
#
# claude-container itself is bind-mounted in (it's the operator's launcher
# script and updates independently); this image provides its dependencies:
# bash, git, tmux, docker CLI, openssl (GitHub App JWTs), ssh (pushes).

FROM python:3.12-slim

ARG DOCKER_CLI_VERSION=27.5.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git openssh-client openssl tmux util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(uname -m)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

# The daemon runs as the OPERATOR'S uid — claude refuses bypassPermissions
# as root, which broke every worker turn when the launcher propagated uid
# 0. entrypoint.sh (root) makes the docker socket usable, then setprivs
# down to ISSUEFLEET_UID/GID. Any-uid writable home for that user:
RUN mkdir -p /home/fleet && chmod 1777 /home/fleet
ENV HOME=/home/fleet
COPY deploy/docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

WORKDIR /opt/issuefleet
COPY src/ src/
COPY bin/ bin/

# Stdlib-only: no pip install. Config is mounted at
# /home/fleet/.config/issuefleet (the default --config location under HOME).
ENV PYTHONUNBUFFERED=1
CMD ["bin/issuefleet", "-v", "run"]
