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

# Wait for the sandbox pod to publish the skill tree into the shared
# workdir (opencode scans project skills at startup only — a runtime pod
# that comes up first used to start with an empty skill tool). Degrade
# after SKILL_WAIT_SECONDS so a permanently missing sandbox never wedges
# the service; the protocol text in AGENTS.md still carries the worker.
SKILL_WAIT_SECONDS="${SKILL_WAIT_SECONDS:-180}"
waited=0
until ls "$WORKDIR"/.opencode/skills/*/SKILL.md >/dev/null 2>&1; do
    if [ "$waited" -ge "$SKILL_WAIT_SECONDS" ]; then
        echo "[opencode] WARNING: no skills published after ${SKILL_WAIT_SECONDS}s; starting without the skill tool"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done
echo "[opencode] skills present after ${waited}s: $(ls "$WORKDIR/.opencode/skills" 2>/dev/null | wc -l) entries"

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
