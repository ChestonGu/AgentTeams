# =============================================================================
# opencode service image (conversation loop + LLM, no local execution)
# =============================================================================
# Standalone opencode pod: `opencode serve` headless with a baked Zhipu GLM
# (anthropic-compatible) provider. It owns no tools — the built-in bash tool
# is replaced (same-name custom tool) by a thin client that forwards commands
# to the agent sandbox pod via SANDBOX_EXEC_URL. The workdir is a shared
# volume with the sandbox, so AGENTS.md (written by the bridge through the
# sandbox helper) and project files are visible from both sides.
#
# Build (context = repo root, see Makefile build-opencode-runtime):
#   docker build -f opencode/runtime-image/Dockerfile \
#     --build-arg OPENCODE_VERSION=<pin> \
#     -t agentteams/opencode-runtime:<tag> .
# =============================================================================
FROM node:22-slim

ARG OPENCODE_VERSION=latest
ARG NPM_REGISTRY=https://registry.npmjs.org

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

COPY opencode/runtime-image/opencode.json /opt/agenttools/opencode.json
COPY opencode/runtime-image/tools/bash.ts /opt/agenttools/tools/bash.ts
COPY opencode/runtime-image/entrypoint.sh /opt/agenttools/entrypoint.sh
RUN chmod +x /opt/agenttools/entrypoint.sh

# LLM credentials live in opencode.json (options.baseURL + options.apiKey —
# the v1.18.x effect config removed the top-level api_key/api_key_env fields;
# key must target the Coding Plan endpoint, not the generic /api/paas/v4).
ENV OPENCODE_PORT=4096

EXPOSE 4096
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agenttools/entrypoint.sh"]
