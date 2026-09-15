#!/bin/sh
# notify-platform.sh — invoked by the worker sync loop (worker-entrypoint.sh)
# whenever new output files appear under ~/outputs/. Deploy a customized copy at
# ${WORKSPACE}/scripts/notify-platform.sh (synced via MinIO) to override this
# bundled default WITHOUT rebuilding the image.
#
# Inputs (env, set by the sync loop):
#   HICLAW_NOTIFY_WORKER       worker name
#   HICLAW_NOTIFY_MINIO_PREFIX MinIO path prefix for this worker's workspace
#                              (artifacts are at ${PREFIX}/outputs/<rel>)
#   HICLAW_NOTIFY_ROOM         best-effort Matrix home room / userId (may be empty)
#   HICLAW_NOTIFY_OUTPUTS      newline-separated, workspace-relative output paths
#                              (e.g. "outputs/report.md")
# You set:
#   HICLAW_OUTPUT_NOTIFY_URL   your control platform endpoint (unset = log only)
#
# Design: fire-and-forget from the sync loop (10s timeout). Keep it fast; never
# block. team is intentionally blank for now (controller does not propagate team
# name to workers — platform maps worker -> team on its side).
set -u

WORKER="${HICLAW_NOTIFY_WORKER:-}"
MINIO_PREFIX="${HICLAW_NOTIFY_MINIO_PREFIX:-}"
ROOM="${HICLAW_NOTIFY_ROOM:-}"
OUTPUTS="${HICLAW_NOTIFY_OUTPUTS:-}"
URL="${HICLAW_OUTPUT_NOTIFY_URL:-}"
TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# JSON-safe list of output paths (drop empty lines).
OUTPUTS_JSON="$(printf '%s' "${OUTPUTS}" | jq -R -s -c 'split("\n") | map(select(length > 0))' 2>/dev/null)"
[ -n "${OUTPUTS_JSON}" ] || OUTPUTS_JSON="[]"

PAYLOAD="$(jq -nc \
  --arg worker "${WORKER}" \
  --arg team "" \
  --arg room "${ROOM}" \
  --arg time "${TIME}" \
  --argjson outputs "${OUTPUTS_JSON}" \
  --arg minio_prefix "${MINIO_PREFIX}" \
  '{worker:$worker, team:$team, room:$room, time:$time, outputs:$outputs, minio_prefix:$minio_prefix}')"

echo "[notify-platform $(date -u +%H:%M:%S)] payload=${PAYLOAD}"

if [ -z "${URL}" ]; then
  echo "[notify-platform] HICLAW_OUTPUT_NOTIFY_URL not set; logged payload only (no POST)"
  exit 0
fi

# POST to the control platform. Failures are non-fatal (sync loop logs them).
HTTP_CODE="$(curl -s -o /tmp/notify-platform.resp -w '%{http_code}' \
  -X POST "${URL}" \
  -H 'Content-Type: application/json' \
  --max-time 8 \
  -d "${PAYLOAD}" 2>/dev/null)" || HTTP_CODE="000"
echo "[notify-platform] POST ${URL} -> HTTP ${HTTP_CODE}"
[ "${HTTP_CODE}" = "000" ] && exit 1
exit 0
