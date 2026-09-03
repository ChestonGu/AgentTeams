#!/usr/bin/env bash
# T10 local smoke for the opencode worker migration (design doc §6.4).
# No controller involved: build the MinIO tree by hand, drive taskflow /
# agentteams-sync exactly as the worker would, and byte-check what the
# copaw Leader will read back.
#
# Usage:
#   ./smoke.sh [--alias agentteams] [--bucket agentteams-storage] \
#              [--team <storage-team>] [--worker test-worker] [--keep]
#
# Requires: mc (alias already configured), python3, jq.
set -u

ALIAS="${ALIAS:-agentteams}"
BUCKET="${BUCKET:-agentteams-storage}"
TEAM="${TEAM:-}"
WORKER="${WORKER:-test-worker}"
DOMAIN="${DOMAIN:-example.org}"
KEEP=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../template/opencode-worker-agent"
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

while [ $# -gt 0 ]; do
  case "$1" in
    --alias) ALIAS="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --team) TEAM="$2"; shift 2 ;;
    --worker) WORKER="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

TS="$(date +%s)"
TASK_ID="t-smoke-$TS"
FAILS=0
WORK="$(mktemp -d)"
FS_ROOT="$WORK/fs"
AGENT_PREFIX="$ALIAS/$BUCKET/agents/$WORKER"
if [ -n "$TEAM" ]; then
  SHARED_PREFIX="$ALIAS/$BUCKET/teams/$TEAM/shared"
else
  SHARED_PREFIX="$ALIAS/$BUCKET/shared"
fi

fail() { echo "  FAIL: $*"; FAILS=$((FAILS + 1)); }
ok() { echo "  ok: $*"; }
step() { echo; echo "== $* =="; }

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo "(keep) workdir=$WORK task=$TASK_ID prefix=$SHARED_PREFIX"
    return
  fi
  mc rm --recursive --force "$SHARED_PREFIX/tasks/$TASK_ID" >/dev/null 2>&1
  mc rm --recursive --force "$AGENT_PREFIX" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------- step 1
step "1. build agents/$WORKER tree on MinIO from the template"
mkdir -p "$FS_ROOT"
cp -r "$TEMPLATE" "$FS_ROOT/workspace-tpl" 2>/dev/null || true
echo "# SOUL (smoke)" > "$FS_ROOT/SOUL.md"
mc mirror --overwrite "$FS_ROOT/workspace-tpl/skills" "$AGENT_PREFIX/skills" >/dev/null || fail "mirror skills"
mc cp "$FS_ROOT/workspace-tpl/AGENTS.md" "$AGENT_PREFIX/AGENTS.md" >/dev/null || fail "cp AGENTS.md"
mc ls "$AGENT_PREFIX/skills/task-management/scripts/taskflow.py" >/dev/null 2>&1 \
  && ok "template skills + AGENTS.md on MinIO" || fail "template upload"

# ---------------------------------------------------------------- step 2
step "2. leader writes shared/tasks/$TASK_ID/{meta.json,spec.md}"
mkdir -p "$WORK/leader/shared/tasks/$TASK_ID"
cat > "$WORK/leader/shared/tasks/$TASK_ID/meta.json" <<EOF
{
  "task_id": "$TASK_ID",
  "project_id": "p-smoke",
  "task_title": "Smoke task",
  "assigned_to": "@$WORKER:$DOMAIN",
  "room_id": "!smoke-room:$DOMAIN",
  "status": "assigned",
  "depends_on": [],
  "assigned_at": "2026-09-01T00:00:00Z"
}
EOF
printf '# Spec\n\nDeliver workspace/smoke.out containing the word ready.\n' \
  > "$WORK/leader/shared/tasks/$TASK_ID/spec.md"
mc mirror --overwrite "$WORK/leader/shared/tasks/$TASK_ID" \
  "$SHARED_PREFIX/tasks/$TASK_ID" >/dev/null || fail "leader task upload"
ok "task $TASK_ID assigned to @$WORKER:$DOMAIN"

# worker env per contract §1
export AGENTTEAMS_WORKER_NAME="$WORKER"
export AGENTTEAMS_MATRIX_USER_ID="@$WORKER:$DOMAIN"
export AGENTTEAMS_FS_ROOT="$FS_ROOT"
export AGENTTEAMS_TEAM="$TEAM"
export AGENTTEAMS_STORAGE_ALIAS="$ALIAS"
# static trio: fill these if your mc alias is NOT backed by MC_HOST_*
: "${AGENTTEAMS_FS_ENDPOINT:=}"
: "${AGENTTEAMS_FS_ACCESS_KEY:=}"
: "${AGENTTEAMS_FS_SECRET_KEY:=}"
export AGENTTEAMS_FS_ENDPOINT AGENTTEAMS_FS_ACCESS_KEY AGENTTEAMS_FS_SECRET_KEY
export AGENTTEAMS_FS_BUCKET="$BUCKET"

TASKFLOW="$PY $FS_ROOT/workspace-tpl/skills/task-management/scripts/taskflow.py"
SYNC="$PY $FS_ROOT/workspace-tpl/skills/file-sharing/scripts/agentteams_sync.py"

# ---------------------------------------------------------------- step 3
step "3. reject paths first (identity spoof / submit-before-ack)"
$TASKFLOW ack "$TASK_ID" --actor "@mallory:$DOMAIN" >/dev/null 2>&1 \
  && fail "spoofed ack accepted" || ok "spoofed ack rejected"
$TASKFLOW submit "$TASK_ID" --status SUCCESS --summary premature >/dev/null 2>&1 \
  && fail "submit before ack accepted" || ok "submit before ack rejected"

step "3b. happy path: check -> ack (spec in output) -> deliverable -> submit"
$TASKFLOW check "$TASK_ID" | grep -q "status: assigned" && ok "check shows assigned" || fail "check"
ACK_OUT="$($TASKFLOW ack "$TASK_ID")" || fail "ack exit code"
echo "$ACK_OUT" | grep -q "Deliver workspace/smoke.out" \
  && ok "ack output contains spec content" || fail "spec missing from ack output"
echo "$ACK_OUT" | grep -q "assigned -> in_progress" && ok "state transition" || fail "transition"

mkdir -p "$FS_ROOT/shared/tasks/$TASK_ID/workspace"
echo "ready" > "$FS_ROOT/shared/tasks/$TASK_ID/workspace/smoke.out"
$TASKFLOW submit "$TASK_ID" --status SUCCESS \
  --summary "Smoke deliverable produced." \
  --deliverables workspace/smoke.out >/dev/null || fail "submit exit code"
ok "submit completed"

# ---------------------------------------------------------------- step 5
step "5. byte/field check what the Leader reads back from MinIO"
mc cat "$SHARED_PREFIX/tasks/$TASK_ID/result.md" > "$WORK/remote-result.md" 2>/dev/null \
  || fail "remote result.md missing"
printf 'STATUS: SUCCESS\nSUMMARY: Smoke deliverable produced.\n\nDELIVERABLES:\n- shared/tasks/%s/workspace/smoke.out\n' \
  "$TASK_ID" > "$WORK/expected-result.md"
if diff -u "$WORK/expected-result.md" "$WORK/remote-result.md" >/dev/null; then
  ok "result.md byte-identical to copaw protocol"
else
  fail "result.md mismatch:"; diff -u "$WORK/expected-result.md" "$WORK/remote-result.md" | head -10
fi
mc cat "$SHARED_PREFIX/tasks/$TASK_ID/meta.json" > "$WORK/remote-meta.json" 2>/dev/null \
  || fail "remote meta.json missing"
[ "$(jq -r .status "$WORK/remote-meta.json")" = "submitted" ] \
  && ok "remote meta status=submitted" || fail "remote meta status"
[ "$(jq -r .assigned_to "$WORK/remote-meta.json")" = "@$WORKER:$DOMAIN" ] \
  && ok "remote assigned_to intact" || fail "remote assigned_to"
jq -e .submitted_at "$WORK/remote-meta.json" >/dev/null && ok "submitted_at set" || fail "submitted_at"
mc stat "$SHARED_PREFIX/tasks/$TASK_ID/spec.md" >/dev/null 2>&1 \
  && ok "spec.md survived worker pushes (leader-owned)" || fail "spec.md lost"

# ---------------------------------------------------------------- step 6
step "6. agentteams-sync push/stat/list cycle"
mkdir -p "$FS_ROOT/shared/tasks/$TASK_ID/progress"
echo "mid-task note" > "$FS_ROOT/shared/tasks/$TASK_ID/progress/2026-09-01.md"
$SYNC push "shared/tasks/$TASK_ID/progress/" >/dev/null && ok "push progress/" || fail "push"
$SYNC stat "shared/tasks/$TASK_ID/progress/2026-09-01.md" | grep -q '"ok": *true' \
  && ok "stat exists=true" || fail "stat"
$SYNC list "shared/tasks/$TASK_ID/progress" | grep -q "2026-09-01.md" \
  && ok "list shows file" || fail "list"
echo "drifted" | mc pipe "$SHARED_PREFIX/tasks/$TASK_ID/progress/2026-09-01.md" >/dev/null
$SYNC pull "shared/tasks/$TASK_ID/progress/" >/dev/null
grep -q "drifted" "$FS_ROOT/shared/tasks/$TASK_ID/progress/2026-09-01.md" \
  && ok "pull picks up remote drift" || fail "pull drift"

# ---------------------------------------------------------------- summary
echo
if [ "$FAILS" = "0" ]; then
  echo "SMOKE PASSED (task=$TASK_ID)"
  exit 0
fi
echo "SMOKE FAILED: $FAILS check(s) above"
exit 1
