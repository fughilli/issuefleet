# Both runtime adapters use the existing launcher's workspace, uid mapping,
# linked-worktree mounts, and project overlays. Install Codex in the shared
# base, so choosing a runtime never requires mutating the project checkout.
ARG CLAUDE_CONTAINER_VERSION=1.7.0
FROM nezhar/claude-container:${CLAUDE_CONTAINER_VERSION}

USER root
ARG CODEX_VERSION=0.154.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && codex --version
