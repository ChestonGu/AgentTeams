#!/usr/bin/env bash
# Enable the Synapse HA topology in namespace agentos (one-shot, idempotent):
#
#   1. persist the HA secrets (copy of the main signing key, Redis password,
#      worker replication shared secret);
#   2. deploy Redis (replication bus for workers);
#   3. add the replication listener + redis/instance_map/worker_replication_secret
#      to the main homeserver.yaml and restart the main process once;
#   4. deploy the client worker Deployment (2 replicas) and its Service/PDB;
#   5. configure Higress routes so /_matrix/client/v3/sync etc. go to workers.
#
# Safe to re-run: secrets are preserved, config transformation is idempotent,
# Redis/workers are applied declaratively. The only interruption is one main
# process restart (about 10-30 seconds; AppService push/media pause, /sync
# served by workers once they are up).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RENDER="$ROOT/render"
mkdir -p "$RENDER"

NS="${NS:-agentos}"
RELEASE="${RELEASE:-agentteams-128}"
SYNAPSE_NAME="${RELEASE}-synapse"
REDIS_NAME="${RELEASE}-synapse-redis"
MAIN_SVC="${RELEASE}-synapse-main"
CLIENT_SVC="${RELEASE}-synapse-client"
MAIN_HOST="${MAIN_SVC}.${NS}.svc.cluster.local"
REDIS_HOST="${REDIS_NAME}.${NS}.svc.cluster.local"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need kubectl
need python3
need base64

echo "==> namespace=$NS release=$RELEASE"

# ---------------------------------------------------------------------------
# 1. Ensure HA secrets. All values are generated once and then preserved.
# ---------------------------------------------------------------------------
POD="$(kubectl -n "$NS" get pod -l "app.kubernetes.io/name=${SYNAPSE_NAME}" -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "main Synapse pod not found; deploy Synapse first" >&2
  exit 1
fi

echo "==> reading signing key from main pod ${POD} (PVC /data)"
kubectl -n "$NS" exec "$POD" -- cat /data/agentteams.signing.key > "$RENDER/signing.key"
if [[ ! -s "$RENDER/signing.key" ]]; then
  echo "signing key is empty; refusing to continue (would invalidate existing sessions)" >&2
  exit 1
fi

kubectl -n "$NS" get secret agentteams-synapse-secrets -o json > "$RENDER/synapse-secrets.json"
python3 - "$RENDER/synapse-secrets.json" "$RENDER/signing.key" <<'PY'
import base64, json, secrets, sys
path, keyfile = sys.argv[1], sys.argv[2]
with open(path) as f:
    sec = json.load(f)
data = sec.setdefault("data", {})
def put(name, value):
    data[name] = base64.b64encode(value.encode()).decode()
def has(name):
    return bool(data.get(name))
if not has("signing_key"):
    put("signing_key", open(keyfile).read().strip())
if not has("redis_password"):
    put("redis_password", secrets.token_hex(24))
if not has("worker_replication_secret"):
    put("worker_replication_secret", secrets.token_hex(32))
with open(path, "w") as f:
    json.dump(sec, f)
PY
kubectl apply -f "$RENDER/synapse-secrets.json" >/dev/null || kubectl replace -f "$RENDER/synapse-secrets.json" >/dev/null
echo "==> HA secrets ensured (signing_key/redis_password/worker_replication_secret)"

secret_of() {
  kubectl -n "$NS" get secret agentteams-synapse-secrets -o jsonpath="{.data.$1}" | base64 -d
}
REDIS_PASSWORD="$(secret_of redis_password)"
WORKER_REPLICATION_SECRET="$(secret_of worker_replication_secret)"

# ---------------------------------------------------------------------------
# 2. Redis (Secret with redis.conf + password, then StatefulSet/Service).
# ---------------------------------------------------------------------------
echo "==> rendering Redis config secret"
cat > "$RENDER/redis.conf" <<EOF
bind 0.0.0.0
protected-mode yes
port 6379
requirepass ${REDIS_PASSWORD}
appendonly yes
appendfsync everysec
dir /data
save 900 1
maxmemory 256mb
maxmemory-policy allkeys-lru
EOF
kubectl -n "$NS" create secret generic "$REDIS_NAME" \
  --from-file=redis.conf="$RENDER/redis.conf" \
  --from-literal=redis-password="$REDIS_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-redis.yaml" >/dev/null
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-main-service.yaml" >/dev/null
kubectl -n "$NS" rollout status statefulset/"$REDIS_NAME" --timeout=180s

# ---------------------------------------------------------------------------
# 3. Transform the existing homeserver.yaml: replication listener + HA block.
# ---------------------------------------------------------------------------
echo "==> rendering HA homeserver.yaml from the live config secret"
kubectl -n "$NS" get secret agentteams-synapse-config -o yaml > "$RENDER/backup-config-secret-$(date +%Y%m%d-%H%M%S).yaml"
kubectl -n "$NS" get secret agentteams-synapse-config -o jsonpath='{.data.homeserver\.yaml}' | base64 -d > "$RENDER/homeserver-live.yaml"
kubectl -n "$NS" get secret agentteams-synapse-config -o jsonpath='{.data.log_config\.yaml}' | base64 -d > "$RENDER/log_config.yaml"

python3 - "$RENDER/homeserver-live.yaml" "$RENDER/homeserver.yaml" "$REDIS_HOST" "$MAIN_HOST" "$REDIS_PASSWORD" "$WORKER_REPLICATION_SECRET" <<'PY'
import sys
src, dst, redis_host, main_host, redis_pw, worker_secret = sys.argv[1:7]
with open(src) as f:
    text = f.read()

if "port: 9093" not in text:
    needle = "        compress: false\n"
    repl = (
        "        compress: false\n"
        "  - port: 9093\n"
        "    tls: false\n"
        "    type: http\n"
        "    bind_addresses:\n"
        "      - 0.0.0.0\n"
        "    resources:\n"
        "      - names: [replication]\n"
    )
    if needle not in text:
        sys.exit("cannot find client listener block to extend")
    text = text.replace(needle, repl, 1)

if "\nredis:\n  enabled: true" not in text:
    ha = f"""
# --- HA (client workers): Redis replication + HTTP replication endpoint ---
redis:
  enabled: true
  host: {redis_host}
  port: 6379
  password: "{redis_pw}"

instance_map:
  main:
    host: {main_host}
    port: 9093

worker_replication_secret: "{worker_secret}"
"""
    text = text.rstrip("\n") + "\n" + ha

with open(dst, "w") as f:
    f.write(text)
PY

NEW_HASH="$(sha256sum "$RENDER/homeserver.yaml" | awk '{print $1}')"
CUR_HASH="$(kubectl -n "$NS" get secret agentteams-synapse-config -o jsonpath='{.data.homeserver\.yaml}' | base64 -d | sha256sum | awk '{print $1}')"
if [ "$NEW_HASH" = "$CUR_HASH" ]; then
  echo "==> homeserver config unchanged; skipping main process restart"
else
  kubectl -n "$NS" create secret generic agentteams-synapse-config \
    --from-file=homeserver.yaml="$RENDER/homeserver.yaml" \
    --from-file=log_config.yaml="$RENDER/log_config.yaml" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "==> restarting the Synapse main process (brief interruption expected)"
  kubectl -n "$NS" rollout restart statefulset/"$SYNAPSE_NAME" >/dev/null
  kubectl -n "$NS" rollout status statefulset/"$SYNAPSE_NAME" --timeout=420s
fi

# ---------------------------------------------------------------------------
# 4. Client workers (Deployment, stateless) + Service/PDB.
# ---------------------------------------------------------------------------
echo "==> deploying client workers"
kubectl -n "$NS" apply -f "$ROOT/manifests/synapse-worker.yaml" >/dev/null
if ! kubectl -n "$NS" rollout status deploy/"${RELEASE}-synapse-client" --timeout=300s; then
  echo "worker rollout failed; recent events/logs:" >&2
  kubectl -n "$NS" get pods -l "app.kubernetes.io/name=${RELEASE}-synapse-client" -o wide >&2 || true
  kubectl -n "$NS" logs deploy/"${RELEASE}-synapse-client" --tail=80 >&2 || true
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. Gateway routes: sync endpoints -> workers, everything else -> main.
# ---------------------------------------------------------------------------
echo "==> configuring Higress routes"
"$HERE/enable-sync-workers.sh"

echo
echo "==> HA enabled. Quick state:"
kubectl -n "$NS" get pods -l 'app.kubernetes.io/component=matrix' -o wide
echo
echo "Next: run scripts/verify-synapse.sh and scripts/verify-ha.sh, then watch"
echo "the worker/main access logs to confirm /sync lands on the workers."
