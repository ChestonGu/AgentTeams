# Build context MUST be the repo root (see Makefile build-cimicode-bridge):
# the image bundles the v2.4 agent.md generator + its source template from
# opencode/ (contract §2 bridge-side ownership — they never enter the worker
# sandbox image).
FROM python:3.12-slim

WORKDIR /opt/bridge
COPY cimicode-bridge/pyproject.toml cimicode-bridge/README.md ./
COPY cimicode-bridge/src ./src
COPY cimicode-bridge/config ./config
RUN pip install --no-cache-dir .

# v2.4 generator toolchain (contract §6): generator + unified log module +
# source template. The template keeps its relative layout
# (template/opencode-worker-agent/AGENTS.md) so the generator's default
# --template resolution works without flags.
COPY opencode/bridge/generate_agent_md.py /opt/agenttools/generate_agent_md.py
COPY opencode/bridge/agentteams_log.py /opt/agenttools/agentteams_log.py
COPY opencode/template/opencode-worker-agent/AGENTS.md /opt/agenttools/template/opencode-worker-agent/AGENTS.md
ENV BRIDGE_GENERATOR_PATH=/opt/agenttools/generate_agent_md.py

COPY cimicode-bridge/scripts/bridge-entrypoint.sh /usr/local/bin/bridge-entrypoint.sh
RUN chmod +x /usr/local/bin/bridge-entrypoint.sh
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/bridge-entrypoint.sh"]
CMD ["run", "--host", "0.0.0.0", "--port", "8081"]
