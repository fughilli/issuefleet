# Both runtime adapters use the existing launcher's workspace, uid mapping,
# linked-worktree mounts, and project overlays. Install Codex in the shared
# base, so choosing a runtime never requires mutating the project checkout.
# docker-bake.hcl builds this named context from the pinned launcher source.
FROM claude_base

USER root
ARG CODEX_VERSION=0.154.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && codex --version
