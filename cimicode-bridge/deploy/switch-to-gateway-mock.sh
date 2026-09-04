#!/bin/bash
# 把 MinIO 中 openclaw.json 的 gateway baseUrl 切到 gateway-mock，并重启 bridge
set -euo pipefail
S3BASE="local/agentteams-storage/agentteams/agentteams-storage/agents/cimicode-agent"
MOCK_URL="http://cimicode-gateway-mock.agentos.svc.cluster.local:8080"

kubectl -n s3-sim exec deployment/minio-sim -- sh -c \
  "mc alias set local http://127.0.0.1:9000 minio-sim-root 'MinioSim#2026' >/dev/null 2>&1"

kubectl -n s3-sim exec deployment/minio-sim -- sh -c \
  "mc cat ${S3BASE}/openclaw.json" > /tmp/openclaw.json

python3 - "$MOCK_URL" <<'PYEOF'
import json, sys
path = "/tmp/openclaw.json"
cfg = json.load(open(path))
cfg.setdefault("bridge", {}).setdefault("runtime", {})["baseUrl"] = sys.argv[1]
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
print(json.dumps(cfg["bridge"], ensure_ascii=False))
PYEOF

kubectl -n s3-sim exec -i deployment/minio-sim -- sh -c "mc pipe ${S3BASE}/openclaw.json" < /tmp/openclaw.json
echo "-- S3 已更新，重启 bridge --"
kubectl -n agentos rollout restart deployment/cimicode-bridge
sleep 30
kubectl -n agentos get pods -l app=cimicode-bridge
