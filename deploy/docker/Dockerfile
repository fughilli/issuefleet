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
        bash ca-certificates curl git openssh-client openssl tmux \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(uname -m)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
        | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /opt/issuefleet
COPY src/ src/
COPY bin/ bin/

# Stdlib-only: no pip install. Config is mounted at /etc/issuefleet.
ENV PYTHONUNBUFFERED=1
CMD ["bin/issuefleet", "--config", "/etc/issuefleet/config.toml", "run", "-v"]
