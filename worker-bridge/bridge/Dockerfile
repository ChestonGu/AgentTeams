# Build context MUST be the repo root (see Makefile build-cimicode-bridge):
# the image bundles the v2.4 agent.md generator + its source template from
# worker-bridge/ (contract §2 bridge-side ownership — they never enter the worker
# sandbox image).
FROM python:3.12-slim

WORKDIR /opt/bridge
COPY worker-bridge/bridge/pyproject.toml worker-bridge/bridge/README.md ./
COPY worker-bridge/bridge/src ./src
COPY worker-bridge/bridge/config ./config
RUN pip install --no-cache-dir .

# v2.4 generator toolchain (contract §6): generator + unified log module +
# source template. The template lands at /opt/template — the generator's
# default --template resolves to <script_dir>/../template/..., so this
# relative pairing must be preserved for flagless invocation.
COPY worker-bridge/bridge/generate_agent_md.py /opt/agenttools/generate_agent_md.py
COPY worker-bridge/bridge/agentteams_log.py /opt/agenttools/agentteams_log.py
COPY worker-bridge/template/worker-bridge-agent/AGENTS.md /opt/template/worker-bridge-agent/AGENTS.md
ENV BRIDGE_GENERATOR_PATH=/opt/agenttools/generate_agent_md.py

# sed strips any CR characters: a CRLF shebang (#!/bin/sh\r) fails exec with
# a misleading "no such file or directory" — files edited on Windows must not
# break the entrypoint.
COPY worker-bridge/bridge/scripts/bridge-entrypoint.sh /usr/local/bin/bridge-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/bridge-entrypoint.sh \
    && chmod +x /usr/local/bin/bridge-entrypoint.sh
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/bridge-entrypoint.sh"]
CMD ["run", "--host", "0.0.0.0", "--port", "8081"]
