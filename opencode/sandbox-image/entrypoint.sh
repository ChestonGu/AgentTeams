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

echo "[sandbox] workdir=$WORKDIR helper_port=$HELPER_PORT"
exec python3 /opt/agentteams/sandbox_helper.py
