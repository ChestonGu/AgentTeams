#!/usr/bin/env bash
# Deploy the reference Synapse homeserver for AgentTeams v1.2.8
# (matrix.provider=synapse, matrix.mode=existing) into the agentos namespace.
#
# Prerequisites (see README.md):
#   - PostgreSQL role/database for Synapse already created on 10.254.254.105:
#       role "agi-synapse" LOGIN PASSWORD '<pg password>'
#       database "synapse" OWNER "agi-synapse" ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'
#   - Helm release agentteams-128 installed in namespace agentos (the
#     controller is still on Tuwunel at this point; switching it is a separate
#     step, see README.md §4).
#
# Environment overrides:
#   PG_PASSWORD (required) NS RELEASE SERVER_NAME PUBLIC_BASEURL CONTROLLER_URL
#   PG_HOST PG_PORT PG_DB PG_USER
#
# Phases:
#   1. homeserver config without AppService + StatefulSet/Service + admin
#      bootstrap (register_new_matrix_user -a).
#   2. AppService registration Secret + homeserver config with
#      app_service_config_files + rollout restart (Synapse reloads it).
#   3. Verification (delegates to verify-synapse.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RENDER="$ROOT/render"
mkdir -p "$RENDER"

NS="${NS:-agentos}"
RELEASE="${RELEASE:-agentteams-128}"
SYNAPSE_NAME="${RELEASE}-synapse"
SERVER_NAME="${SERVER_NAME:-${SYNAPSE_NAME}.${NS}.svc.cluster.local}"
PUBLIC_BASEURL="${PUBLIC_BASEURL:-http://10.254.254.105:32210}"
CONTROLLER_URL="${CONTROLLER_URL:-http://${RELEASE}-controller.${NS}.svc.cluster.local:8090}"
PG_HOST="${PG_HOST:-10.254.254.105}"
PG_PORT="${PG_PORT:-5432}"
PG_DB="${PG_DB:-synapse}"
PG_USER="${PG_USER:-agi-synapse}"
PG_PASSWORD="${PG_PASSWORD:-}"

if [[ -z "$PG_PASSWORD" ]]; then
  echo "PG_PASSWORD is required (postgres password of role ${PG_USER})" >&2
  exit 1
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need kubectl
need python3
need base64

echo "==> namespace=$NS release=$RELEASE server_name=$SERVER_NAME"

# ---------------------------------------------------------------------------
# 1. Persistent Synapse secrets (generated once, never rotate automatically).
# ---------------------------------------------------------------------------
if ! kubectl -n "$NS" get secret agentteams-synapse-secrets >/dev/null 2>&1; then
  echo "==> creating agentteams-synapse-secrets (registration/macaroon/form secrets)"
  kubectl -n "$NS" create secret generic agentteams-synapse-secrets \
    --from-literal=registration_shared_secret="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
    --from-literal=macaroon_secret_key="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
    --from-literal=form_secret="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
    >/dev/null
else
  echo "==> agentteams-synapse-secrets already exists (reusing)"
fi
secret_of() {
  kubectl -n "$NS" get secret agentteams-synapse-secrets -o jsonpath="{.data.$1}" | base64 -d
}
REGISTRATION_SHARED_SECRET="$(secret_of registration_shared_secret)"
MACAROON_SECRET="$(secret_of macaroon_secret_key)"
FORM_SECRET="$(secret_of form_secret)"

# ---------------------------------------------------------------------------
# 2. Controller-owned values: AppService tokens (existing Secret).
# ---------------------------------------------------------------------------
RT_SECRET="${RELEASE}-runtime-env"
rt_data() {
  kubectl -n "$NS" get secret "$RT_SECRET" -o jsonpath="{.data.$1}" | base64 -d
}
AS_TOKEN="$(rt_data AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN)"
HS_TOKEN="$(rt_data AGENTTEAMS_MATRIX_APPSERVICE_HS_TOKEN)"
ADMIN_USER="$(rt_data AGENTTEAMS_ADMIN_USER)"
if [[ -z "$AS_TOKEN" || -z "$HS_TOKEN" ]]; then
  echo "AppService tokens are empty in secret ${RT_SECRET}; run helm install/upgrade once first" >&2
  exit 1
fi
echo "==> controller admin user: ${ADMIN_USER}; appservice tokens read from ${RT_SECRET}"

# ---------------------------------------------------------------------------
# Rendering helpers (Python so placeholders/paths are handled literally).
# ---------------------------------------------------------------------------
render_with_env() {
  # render_with_env <template> <output>
  python3 - "$1" "$2" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f:
    text = f.read()
for key, val in os.environ.items():
    if key.startswith("TPL_"):
        text = text.replace("__%s__" % key[4:], val)
with open(dst, "w", encoding="utf-8") as f:
    f.write(text)
PY
}

render_homeserver() {
  # render_homeserver <with_appservice 0|1>
  local with_as="$1"
  local as_block=""
  if [[ "$with_as" == "1" ]]; then
    as_block=$'app_service_config_files:\n  - /as-registrations/appservice.yaml'
  fi
  export TPL_SERVER_NAME="$SERVER_NAME" \
         TPL_PUBLIC_BASEURL="$PUBLIC_BASEURL" \
         TPL_REGISTRATION_SHARED_SECRET="$REGISTRATION_SHARED_SECRET" \
         TPL_MACAROON_SECRET="$MACAROON_SECRET" \
         TPL_FORM_SECRET="$FORM_SECRET" \
         TPL_DB_USER="$PG_USER" \
         TPL_DB_PASSWORD="$PG_PASSWORD" \
         TPL_DB_NAME="$PG_DB" \
         TPL_DB_HOST="$PG_HOST" \
         TPL_DB_PORT="$PG_PORT" \
         TPL_APP_SERVICE_BLOCK="$as_block"
  render_with_env "$ROOT/templates/homeserver.yaml.tmpl" "$RENDER/homeserver.yaml"
  cp "$ROOT/templates/log_config.yaml" "$RENDER/log_config.yaml"
  kubectl -n "$NS" create secret generic agentteams-synapse-config \
    --from-file=homeserver.yaml="$RENDER/homeserver.yaml" \
    --from-file=log_config.yaml="$RENDER/log_config.yaml" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  unset TPL_SERVER_NAME TPL_PUBLIC_BASEURL TPL_REGISTRATION_SHARED_SECRET \
        TPL_MACAROON_SECRET TPL_FORM_SECRET TPL_DB_USER TPL_DB_PASSWORD \
        TPL_DB_NAME TPL_DB_HOST TPL_DB_PORT TPL_APP_SERVICE_BLOCK
}

render_appservice() {
  export TPL_SERVER_NAME="$SERVER_NAME" \
         TPL_CONTROLLER_URL="$CONTROLLER_URL" \
         TPL_AS_TOKEN="$AS_TOKEN" \
         TPL_HS_TOKEN="$HS_TOKEN"
  render_with_env "$ROOT/templates/appservice.yaml.tmpl" "$RENDER/appservice.yaml"
  kubectl -n "$NS" create secret generic agentteams-synapse-appservice \
    --from-file=appservice.yaml="$RENDER/appservice.yaml" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  unset TPL_SERVER_NAME TPL_CONTROLLER_URL TPL_AS_TOKEN TPL_HS_TOKEN
}

wait_synapse() {
  kubectl -n "$NS" rollout status statefulset/"$SYNAPSE_NAME" --timeout=420s
}

# ---------------------------------------------------------------------------
# Phase 1: homeserver without AppService registration, then admin bootstrap.
# The admin account must exist before the controller starts (its EnsureUser
# path uses the Synapse admin API, which requires a server admin identity).
# ---------------------------------------------------------------------------
echo "==> phase 1: render config (no appservice) and apply workloads"
render_homeserver 0
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-service.yaml" >/dev/null
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-statefulset.yaml" >/dev/null
# A re-run must load the freshly rendered config even when the StatefulSet pod
# spec is unchanged (a mounted Secret update does not restart the pod).
kubectl -n "$NS" rollout restart statefulset/"$SYNAPSE_NAME" >/dev/null
wait_synapse

echo "==> phase 1: bootstrap admin user ${ADMIN_USER} (register_new_matrix_user -a)"
kubectl -n "$NS" delete job agentteams-synapse-bootstrap --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-bootstrap-job.yaml" >/dev/null
if ! kubectl -n "$NS" wait --for=condition=complete job/agentteams-synapse-bootstrap --timeout=420s; then
  echo "bootstrap job failed; logs:" >&2
  kubectl -n "$NS" logs job/agentteams-synapse-bootstrap --tail=100 >&2 || true
  exit 1
fi
kubectl -n "$NS" logs job/agentteams-synapse-bootstrap --tail=20 || true

# ---------------------------------------------------------------------------
# Phase 2: declarative AppService registration (token rotation is impossible
# on Synapse; the registration file is re-mounted and the server restarted).
# ---------------------------------------------------------------------------
echo "==> phase 2: render appservice registration and restart Synapse"
render_appservice
render_homeserver 1
kubectl -n "$NS" rollout restart statefulset/"$SYNAPSE_NAME" >/dev/null
wait_synapse

echo "==> phase 3: verification"
"$HERE/verify-synapse.sh"

echo
echo "Synapse is ready at http://${SYNAPSE_NAME}.${NS}.svc.cluster.local:8008 (server_name=${SERVER_NAME})."
echo "Next: switch the controller to the Synapse provider (README.md section 4)."
