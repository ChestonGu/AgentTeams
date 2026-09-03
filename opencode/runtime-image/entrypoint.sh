#!/bin/sh
# opencode service entrypoint: seed the opencode global config (Zhipu
# provider + custom bash tool that routes to the sandbox pod) under $HOME,
# then run the headless opencode server with the shared workdir as cwd —
# AGENTS.md lives on the shared volume, written per turn by the bridge via
# the sandbox helper.
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/root/agentteams-fs/agents/${AGENTTEAMS_WORKER_NAME:-worker}}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

CFG_DIR="$HOME/.config/opencode"
mkdir -p "$CFG_DIR/tools"
if [ ! -f "$CFG_DIR/opencode.json" ]; then
    cp /opt/agenttools/opencode.json "$CFG_DIR/opencode.json"
fi
cp /opt/agenttools/tools/bash.ts "$CFG_DIR/tools/bash.ts"

export AGENTTEAMS_FS_ROOT="$WORKDIR"
export OPENCODE_WORKDIR="$WORKDIR"

echo "[opencode] workdir=$WORKDIR port=$OPENCODE_PORT sandbox_exec_url=${SANDBOX_EXEC_URL:-UNSET}"
exec opencode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
