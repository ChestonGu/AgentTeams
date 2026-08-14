#!/bin/bash
# push-worker-skills.sh - Reconcile Worker skills through Worker CR specs.

set -euo pipefail

# Defensive log() fallback: agentteams-env.sh provides log() via base.sh or its
# own fallback, but this script must keep working even when agentteams-env.sh
# fails to load 閳?otherwise an mc failure below turns into a misleading
# "log: command not found".
if ! declare -F log >/dev/null 2>&1; then
    log() { echo "[agentteams $(date '+%H:%M:%S')] $*"; }
fi

# ============================================================
# Parse arguments
# ============================================================
WORKER_NAME=""
SKILL_NAME=""
ADD_SKILL=""
REMOVE_SKILL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --worker) WORKER_NAME="$2"; shift 2 ;;
        --skill) SKILL_NAME="$2"; shift 2 ;;
        --add-skill) ADD_SKILL="$2"; shift 2 ;;
        --remove-skill) REMOVE_SKILL="$2"; shift 2 ;;
        --no-notify) shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "${WORKER_NAME}" ] && [ -z "${SKILL_NAME}" ]; then
    echo "Usage: $0 --worker NAME [--add-skill SKILL|--remove-skill SKILL] | --skill SKILL" >&2
    exit 2
fi
[ -z "${ADD_SKILL}" ] || [ -z "${REMOVE_SKILL}" ] || { echo "--add-skill and --remove-skill are mutually exclusive" >&2; exit 2; }

workers_json=$(agt get workers -o json)
if [ -n "${WORKER_NAME}" ]; then
    targets="${WORKER_NAME}"
else
    targets=$(echo "${workers_json}" | jq -r --arg skill "${SKILL_NAME}" '.workers[] | select((.skills // []) | index($skill)) | .name')
fi

for worker in ${targets}; do
    current=$(agt get workers "${worker}" -o json | jq '.skills // []')
    if [ -n "${ADD_SKILL}" ]; then
        desired=$(echo "${current}" | jq --arg skill "${ADD_SKILL}" 'if index($skill) then . else . + [$skill] end')
    elif [ -n "${REMOVE_SKILL}" ]; then
        desired=$(echo "${current}" | jq --arg skill "${REMOVE_SKILL}" 'map(select(. != $skill))')
    else
        desired="${current}"
    fi
    csv=$(echo "${desired}" | jq -r 'join(",")')
    agt update worker --name "${worker}" --skills "${csv}"
done
