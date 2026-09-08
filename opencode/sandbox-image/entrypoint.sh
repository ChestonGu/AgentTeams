#!/bin/sh
# agent sandbox entrypoint: the sandbox is the execution environment the
# opencode service pod calls into (helper :4097 — POST /exec runs commands,
# POST /agents-md writes the per-turn AGENTS.md onto the shared workdir).
# All tools/skills are preinstalled in this image; opencode itself does NOT
# run here.
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/root/agentteams-fs/agents/${AGENTTEAMS_WORKER_NAME:-worker}}"
HELPER_PORT="${BRIDGE_SANDBOX_HELPER_PORT:-4097}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

export AGENTTEAMS_FS_ROOT="$WORKDIR"

# Publish the skill tree to the shared workdir as opencode *project* skills:
# the opencode service pod (cwd=$WORKDIR) natively discovers
# .opencode/skills/<name>/SKILL.md and exposes them via its skill tool, so
# the agent actually reads the skill docs instead of falling back to the
# AGENTS.md protocol text. The image copy is authoritative — re-synced on
# every sandbox start.
if [ -d /opt/agentteams/skills ]; then
    mkdir -p "$WORKDIR/.opencode/skills"
    cp -rf /opt/agentteams/skills/. "$WORKDIR/.opencode/skills/"
    echo "[sandbox] skills published to $WORKDIR/.opencode/skills ($(ls "$WORKDIR/.opencode/skills" | wc -l) entries)"
fi

echo "[sandbox] workdir=$WORKDIR helper_port=$HELPER_PORT"
exec python3 /opt/agentteams/sandbox_helper.py
