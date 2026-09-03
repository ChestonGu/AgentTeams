# =============================================================================
# agent sandbox image (opencode worker execution environment)
# =============================================================================
# The sandbox is a standalone pod that the opencode service pod calls into:
# every tool, skill and CLI the worker needs is preinstalled here and the
# opencode side routes its bash tool to this pod's helper (:4097).
# opencode itself does NOT run in this image (separation: opencode service
# pod = conversation loop + LLM; sandbox pod = execution).
#
# Helper endpoints (sandbox_helper.py):
#   POST /exec       run a command in the workdir (shared volume)
#   POST /agents-md  write the per-turn AGENTS.md onto the shared workdir
#
# Build (context = repo root, see Makefile build-opencode-sandbox):
#   docker build -f opencode/sandbox-image/Dockerfile \
#     -t agentteams/opencode-sandbox:<tag> .
# =============================================================================
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/mc https://dl.min.io/client/mc/release/linux-amd64/mc \
    && chmod +x /usr/local/bin/mc

# collaboration toolchain: skill tree (taskflow / file-sharing scripts are the
# byte-identical deploy copies kept in sync by verify/simulator.py drift checks)
COPY opencode/template/opencode-worker-agent/skills /opt/agentteams/skills
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

COPY opencode/sandbox-image/sandbox_helper.py /opt/agentteams/sandbox_helper.py
COPY opencode/sandbox-image/entrypoint.sh /opt/agentteams/entrypoint.sh
RUN chmod +x /opt/agentteams/entrypoint.sh

ENV BRIDGE_SANDBOX_HELPER_PORT=4097
EXPOSE 4097

ENTRYPOINT ["/opt/agentteams/entrypoint.sh"]
