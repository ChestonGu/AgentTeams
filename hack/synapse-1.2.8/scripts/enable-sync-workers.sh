#!/usr/bin/env bash
# Route the /sync endpoints to the Synapse client workers through Higress.
#
# Idempotent: creates/updates the `synapse-client` DNS service source and the
# sync routes, leaving the existing `matrix-homeserver` fallback (/_matrix ->
# main) untouched. Higress/Envoy matches the most specific prefix, so the sync
# prefix wins over the /_matrix fallback.
set -euo pipefail

NS="${NS:-agentos}"
RELEASE="${RELEASE:-agentteams-128}"
CLIENT_HOST="${RELEASE}-synapse-client.${NS}.svc.cluster.local"

POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=controller -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "controller pod not found (needed to reach the Higress console API)" >&2
  exit 1
fi

kubectl -n "$NS" exec -i "$POD" -- env CLIENT_HOST="$CLIENT_HOST" sh -s <<'EOS'
set -eu
BASE=http://higress-console.agentos.svc.cluster.local:8080
CJ=/tmp/higress-cj
rm -f "$CJ"
curl -s -c "$CJ" -X POST "$BASE/session/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$AGENTTEAMS_ADMIN_USER\",\"password\":\"$AGENTTEAMS_ADMIN_PASSWORD\"}" >/dev/null

ensure_service_source() {
  name="$1"; domain="$2"; port="$3"
  code=$(curl -s -b "$CJ" -o /tmp/ss.json -w '%{http_code}' "$BASE/v1/service-sources/$name")
  if [ "$code" = "200" ]; then
    jq --arg n "$name" --arg d "$domain" --argjson p "$port" \
      '.data | .name=$n | .type="dns" | .domain=$d | .port=$p | .protocol="http"' \
      /tmp/ss.json > /tmp/ss-body.json
    code=$(curl -s -b "$CJ" -o /tmp/ss.out -w '%{http_code}' -X PUT \
      "$BASE/v1/service-sources/$name" -H 'Content-Type: application/json' --data @/tmp/ss-body.json)
    echo "service-source $name: updated (HTTP $code)"
  else
    jq -n --arg n "$name" --arg d "$domain" --argjson p "$port" \
      '{name:$n,type:"dns",domain:$d,port:$p,protocol:"http",properties:{},authN:{enabled:false}}' \
      > /tmp/ss-body.json
    code=$(curl -s -b "$CJ" -o /tmp/ss.out -w '%{http_code}' -X POST \
      "$BASE/v1/service-sources" -H 'Content-Type: application/json' --data @/tmp/ss-body.json)
    echo "service-source $name: created (HTTP $code)"
  fi
  if [ "$code" != "200" ] && [ "$code" != "201" ]; then
    echo "failed to ensure service source $name:" >&2
    cat /tmp/ss.out >&2 || true
    exit 1
  fi
}

ensure_route() {
  name="$1"; path="$2"; svc="$3"; port="$4"
  code=$(curl -s -b "$CJ" -o /tmp/rt.json -w '%{http_code}' "$BASE/v1/routes/$name")
  if [ "$code" = "200" ]; then
    jq --arg n "$name" --arg p "$path" --arg s "$svc" --argjson port "$port" \
      '.data | .name=$n | .domains=[] | .path={matchType:"PRE",matchValue:$p,caseSensitive:false} | .services=[{name:$s,port:$port,weight:100}]' \
      /tmp/rt.json > /tmp/rt-body.json
    code=$(curl -s -b "$CJ" -o /tmp/rt.out -w '%{http_code}' -X PUT \
      "$BASE/v1/routes/$name" -H 'Content-Type: application/json' --data @/tmp/rt-body.json)
    echo "route $name ($path -> $svc:$port): updated (HTTP $code)"
  else
    jq -n --arg n "$name" --arg p "$path" --arg s "$svc" --argjson port "$port" \
      '{name:$n,domains:[],path:{matchType:"PRE",matchValue:$p,caseSensitive:false},services:[{name:$s,port:$port,weight:100}]}' \
      > /tmp/rt-body.json
    code=$(curl -s -b "$CJ" -o /tmp/rt.out -w '%{http_code}' -X POST \
      "$BASE/v1/routes" -H 'Content-Type: application/json' --data @/tmp/rt-body.json)
    echo "route $name ($path -> $svc:$port): created (HTTP $code)"
  fi
  if [ "$code" != "200" ] && [ "$code" != "201" ]; then
    echo "failed to ensure route $name:" >&2
    cat /tmp/rt.out >&2 || true
    exit 1
  fi
}

ensure_service_source synapse-client "$CLIENT_HOST" 8008
# Sync endpoints served by workers (client generic_worker); both v3 and legacy r0.
ensure_route matrix-homeserver-sync /_matrix/client/v3/sync synapse-client.dns 8008
ensure_route matrix-homeserver-sync-r0 /_matrix/client/r0/sync synapse-client.dns 8008
ensure_route matrix-homeserver-events /_matrix/client/v3/events synapse-client.dns 8008

echo "--- current route order (prefix) ---"
curl -s -b "$CJ" "$BASE/v1/routes?pageSize=100" | jq -r '.data[] | .name + "  " + .path.matchValue'
EOS

echo "==> gateway route check (through NodePort 32210):"
curl -s -o /dev/null -w '  /_matrix/client/versions  -> HTTP %{http_code}\n' -m 10 http://10.254.254.105:32210/_matrix/client/versions || true
curl -s -o /dev/null -w '  /_matrix/client/v3/sync    -> HTTP %{http_code} (401 = reachable, token required)\n' -m 10 http://10.254.254.105:32210/_matrix/client/v3/sync || true
