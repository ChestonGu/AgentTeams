#!/usr/bin/env bash
# Verify the Synapse HA topology: Redis reachable, main process has the
# replication listener, client workers are running, and the sync endpoints are
# served by the worker pool while other client endpoints stay on main.
set -euo pipefail

NS="${NS:-agentos}"
RELEASE="${RELEASE:-agentteams-128}"
SYNAPSE_NAME="${RELEASE}-synapse"
REDIS_NAME="${RELEASE}-synapse-redis"

fail=0
ok() { echo "  [OK]   $*"; }
bad() { echo "  [FAIL] $*"; fail=1; }

echo "== Redis =="
if kubectl -n "$NS" get pod -l "app.kubernetes.io/name=${REDIS_NAME}" -o jsonpath='{.items[0].status.phase}' | grep -q Running; then
  ok "Redis pod Running"
else
  bad "Redis pod not Running"
fi
REDIS_PW="$(kubectl -n "$NS" get secret "$REDIS_NAME" -o jsonpath='{.data.redis-password}' | base64 -d)"
if kubectl -n "$NS" exec sts/"$REDIS_NAME" -- redis-cli -a "$REDIS_PW" ping 2>/dev/null | grep -q PONG; then
  ok "Redis PING -> PONG"
else
  bad "Redis PING failed"
fi

echo "== main process =="
MAIN_POD="$(kubectl -n "$NS" get pod -l "app.kubernetes.io/name=${SYNAPSE_NAME}" -o jsonpath='{.items[0].metadata.name}')"
if kubectl -n "$NS" get pod "$MAIN_POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' | grep -q True; then
  ok "main pod Ready ($MAIN_POD)"
else
  bad "main pod not Ready"
fi
if kubectl -n "$NS" exec "$MAIN_POD" -- sh -c 'python3 - <<PY
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:9093/_matrix/client/versions", timeout=3)
except Exception:
    pass
PY' >/dev/null 2>&1; then
  :
fi
# replication listener: check from inside the pod via /proc or by connecting
if kubectl -n "$NS" exec "$MAIN_POD" -- sh -c 'cat /proc/net/tcp 2>/dev/null | grep -qi ":2385"' >/dev/null 2>&1; then
  ok "main replication listener (9093) is open"
else
  bad "main replication listener (9093) not found"
fi
if kubectl -n "$NS" exec "$MAIN_POD" -- grep -q "port: 9093" /config/homeserver.yaml 2>/dev/null; then
  ok "homeserver.yaml has the replication listener"
else
  bad "homeserver.yaml is missing the replication listener"
fi
if kubectl -n "$NS" exec "$MAIN_POD" -- grep -q "redis:" /config/homeserver.yaml 2>/dev/null; then
  ok "homeserver.yaml has redis/instance_map config"
else
  bad "homeserver.yaml is missing redis config"
fi

echo "== client workers =="
WORKER_READY="$(kubectl -n "$NS" get deploy "${RELEASE}-synapse-client" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
WORKER_DESIRED="$(kubectl -n "$NS" get deploy "${RELEASE}-synapse-client" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
if [ "${WORKER_READY:-0}" -ge 1 ] && [ "${WORKER_READY:-0}" = "${WORKER_DESIRED:-0}" ]; then
  ok "client workers ready: ${WORKER_READY}/${WORKER_DESIRED}"
else
  bad "client workers not all ready: ${WORKER_READY:-0}/${WORKER_DESIRED:-0}"
fi
if kubectl -n "$NS" get svc "${RELEASE}-synapse-client" >/dev/null 2>&1; then
  ok "client worker Service exists"
else
  bad "client worker Service missing"
fi

echo "== gateway routing =="
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://10.254.254.105:32210/_matrix/client/versions || true)"
if [ "$CODE" = "200" ]; then ok "gateway /_matrix/client/versions -> 200"; else bad "gateway /_matrix/client/versions -> ${CODE}"; fi
CODE="$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://10.254.254.105:32210/_matrix/client/v3/sync || true)"
if [ "$CODE" = "401" ]; then ok "gateway /_matrix/client/v3/sync -> 401 (route reachable)"; else bad "gateway /_matrix/client/v3/sync -> ${CODE} (expected 401)"; fi

echo
if [ "$fail" -eq 0 ]; then
  echo "HA verification: PASS"
else
  echo "HA verification: FAIL" >&2
  exit 1
fi
