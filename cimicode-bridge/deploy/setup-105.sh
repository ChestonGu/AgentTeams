#!/bin/bash
# =====================================================================
# cimicode-bridge @105 部署准备脚本（模拟调谐动作，调谐上线后可整体废弃）
#  1) 在 Synapse 注册 bot(cimicode-agent) 并登录拿 access_token
#  2) 邀请 bot 进 team room 并加房
#  3) 在 MinIO 写入 agents/cimicode-agent/{openclaw.json,AGENTS.md,SOUL.md}
#  4) 将 docker 镜像导入 k3s containerd
# 运行位置: 105 本机  bash /home/extvdiadmin/agentteams-deploy/setup-105.sh
# =====================================================================
set -uo pipefail

NS=agentos
WORKER=cimicode-agent
BOT_PASSWORD='Cimicode#2026'
DOMAIN=agentteams-synapse.agentos.svc.cluster.local
MATRIX_URL="http://${DOMAIN}:8008"
ROOM='!oCOZLhucNczxLoAoUU:agentteams-synapse.agentos.svc.cluster.local'
INVITER_TOKEN='syt_ZnJvbnRlbmQtYWdlbnQ_jCCgeOdRNsmNqwNZQWpj_4A00M1'
SUDO_PW='Cxmt@20250730'
IMAGE='agentteams/cimicode-bridge:v0.0.1'
DEPLOY_DIR=/home/extvdiadmin/agentteams-deploy

# ---------- Matrix HTTP 助手（在 synapse pod 内执行，避免主机无法访问 ClusterIP） ----------
cat > /tmp/mx.py <<'PYEOF'
import json, sys, urllib.request, urllib.error, urllib.parse
base = "http://agentteams-synapse.agentos.svc.cluster.local:8008"

def req(method, path, data=None, token=None):
    r = urllib.request.Request(base + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(r, body) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()[:300])
        except Exception:
            return e.code, {}

cmd = sys.argv[1]
if cmd == "register":
    s, b = req("POST", "/_matrix/client/v3/register",
               {"username": sys.argv[2], "password": sys.argv[3], "auth": {"type": "m.login.dummy"}})
    print(s, json.dumps(b)[:200])
elif cmd == "login":
    s, b = req("POST", "/_matrix/client/v3/login",
               {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": sys.argv[2]}, "password": sys.argv[3]})
    if s == 200 and "access_token" in b:
        print(b["access_token"])
    else:
        print("", end=""); sys.exit(3)
elif cmd == "invite":
    room = urllib.parse.quote(sys.argv[2], safe="")
    s, b = req("POST", "/_matrix/client/v3/rooms/%s/invite" % room, {"user_id": sys.argv[3]}, sys.argv[4])
    print(s, json.dumps(b)[:200])
elif cmd == "join":
    room = urllib.parse.quote(sys.argv[2], safe="")
    s, b = req("POST", "/_matrix/client/v3/rooms/%s/join" % room, {}, sys.argv[3])
    print(s, json.dumps(b)[:200])
PYEOF

echo "== 1. Synapse 注册/登录 bot: ${WORKER} =="
kubectl -n "$NS" exec -i agentteams-synapse-0 -c synapse -- python3 - register "$WORKER" "$BOT_PASSWORD" < /tmp/mx.py || true

TOKEN=$(kubectl -n "$NS" exec -i agentteams-synapse-0 -c synapse -- python3 - login "$WORKER" "$BOT_PASSWORD" < /tmp/mx.py)
if [ -z "$TOKEN" ]; then
  echo "---- dummy 注册/登录失败，尝试 register_new_matrix_user（共享密钥）----"
  CFG=$(kubectl -n "$NS" exec agentteams-synapse-0 -c synapse -- bash -c 'find / -maxdepth 5 -name "homeserver.yaml" -not -path "/proc/*" 2>/dev/null | head -1')
  echo "homeserver.yaml: ${CFG:-not-found}"
  if [ -n "$CFG" ]; then
    kubectl -n "$NS" exec agentteams-synapse-0 -c synapse -- bash -c \
      "printf '%s\n%s\n%s\nno\n' '$WORKER' '$BOT_PASSWORD' '$BOT_PASSWORD' | register_new_matrix_user -c '$CFG' http://localhost:8008" || true
    TOKEN=$(kubectl -n "$NS" exec -i agentteams-synapse-0 -c synapse -- python3 - login "$WORKER" "$BOT_PASSWORD" < /tmp/mx.py)
  fi
fi
[ -n "$TOKEN" ] || { echo "FATAL: 无法获取 bot token"; exit 3; }
echo "bot access_token: ${TOKEN:0:24}..."

echo "== 2. 邀请 bot 进房并 join =="
kubectl -n "$NS" exec -i agentteams-synapse-0 -c synapse -- python3 - invite "$ROOM" "@${WORKER}:${DOMAIN}" "$INVITER_TOKEN" < /tmp/mx.py || true
kubectl -n "$NS" exec -i agentteams-synapse-0 -c synapse -- python3 - join "$ROOM" "$TOKEN" < /tmp/mx.py || true

echo "== 3. MinIO 写入 agents/${WORKER}/ =="
kubectl -n s3-sim exec deployment/minio-sim -- sh -c "mc alias set local http://127.0.0.1:9000 minio-sim-root 'MinioSim#2026' >/dev/null 2>&1" || true
S3_BASE="local/agentteams-storage/agentteams/agentteams-storage/agents/${WORKER}"
kubectl -n s3-sim exec -i deployment/minio-sim -- sh -c "mc pipe ${S3_BASE}/openclaw.json" <<EOF
{
  "channels": {
    "matrix": {
      "homeserver": "${MATRIX_URL}",
      "accessToken": "${TOKEN}"
    }
  },
  "bridge": {
    "runtime": {
      "baseUrl": "http://cimicode-gateway.agentos.svc.cluster.local:8080",
      "templateId": "cimicode-default",
      "sessionId": "sess-${WORKER}-001",
      "sandboxId": "sbx-${WORKER}-001"
    }
  }
}
EOF
kubectl -n s3-sim exec -i deployment/minio-sim -- sh -c "mc pipe ${S3_BASE}/AGENTS.md" <<'EOF'
# AGENTS.md（bridge 模拟配置，调谐上线后由平台生成）

## 协作规则
- 只响应最后一条明确 @ 你的消息。
- 无需回复时，输出 NO_REPLY。
- 完成任务后向 Team Leader 汇报结果，不要直接 @ Manager。
EOF
kubectl -n s3-sim exec -i deployment/minio-sim -- sh -c "mc pipe ${S3_BASE}/SOUL.md" <<'EOF'
# SOUL.md（bridge 模拟配置）

你是 cimicode-agent，一名严谨务实的前端开发工程师，回答简洁、结果导向。
EOF
echo "-- 校验 S3 内容 --"
kubectl -n s3-sim exec deployment/minio-sim -- sh -c "mc ls --recursive ${S3_BASE}/ && echo && mc cat ${S3_BASE}/openclaw.json"

echo "== 4. 导入镜像到 k3s containerd =="
echo "$SUDO_PW" | sudo -S -v 2>/dev/null || true
if ! sudo -n k3s ctr images ls 2>/dev/null | grep -q "cimicode-bridge:v0.0.1"; then
  if docker images | grep -q "cimicode-bridge.*v0.0.1"; then
    docker save "$IMAGE" -o /tmp/cimicode-bridge-v0.0.1.tar
    sudo -n k3s ctr images import /tmp/cimicode-bridge-v0.0.1.tar
  else
    echo "WARN: docker 中未找到 $IMAGE，跳过导入"
  fi
fi
sudo -n k3s ctr images ls 2>/dev/null | grep cimicode || true

echo
echo "== setup 完成，接下来执行: kubectl apply -f ${DEPLOY_DIR}/deployment-cimicode-bridge.yaml =="
