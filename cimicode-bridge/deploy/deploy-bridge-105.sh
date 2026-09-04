#!/bin/bash
# =====================================================================
# cimicode-bridge @105 一键部署：拉代码 → 构建 → 导入 k3s → apply → 验证
# 用法: bash deploy-bridge-105.sh [VERSION]
#   默认 VERSION=v0.0.6
# 前置: 105 上有 fork 仓库(含 Makefile)，kubectl 可用，sudo 密码为 SUDO_PW
# =====================================================================
set -euo pipefail

VERSION="${1:-v0.0.7}"
REPO_DIR="${REPO_DIR:-/home/extvdiadmin/workbach/AgentTeams}"
SUDO_PW="${SUDO_PW:-Cxmt@20250730}"
BRANCH=feature/stateless_cimicode_docking
DEPLOY_DIR=/home/extvdiadmin/agentteams-deploy

echo "==> [1/6] 拉取最新代码 ($REPO_DIR, $BRANCH)"
cd "$REPO_DIR"
git pull origin "$BRANCH" 2>&1 | tail -2
git log --oneline -1

echo "==> [2/6] 构建镜像 agentteams/cimicode-bridge:$VERSION"
make build-cimicode-bridge VERSION="$VERSION" 2>&1 | grep -E "naming to|COPY src|ERROR" | head -3 || true

echo "==> [3/6] 导入 k3s containerd"
docker save "agentteams/cimicode-bridge:$VERSION" -o /tmp/cim-bridge.tar
echo "$SUDO_PW" | sudo -S k3s ctr images import /tmp/cim-bridge.tar 2>&1 | grep -E "unpacking|done" | tail -1
sudo k3s ctr images ls 2>/dev/null | grep "cimicode-bridge:$VERSION" | head -1

echo "==> [4/6] 更新 deployment 镜像 tag → $VERSION"
python3 - cimicode-bridge/deploy/deployment-cimicode-bridge-105.yaml "$VERSION" <<'PYEOF'
import re, sys
src, tag = sys.argv[1], sys.argv[2]
dst = "/home/extvdiadmin/agentteams-deploy/deployment-cimicode-bridge-105.yaml"
s = open(src).read().replace("\r\n", "\n")
s = re.sub(r"cimicode-bridge:v\d+\.\d+\.\d+([-\w.]*)", f"cimicode-bridge:{tag}\\1", s)
open(dst, "w").write(s)
print("yaml image ->", tag)
PYEOF

echo "==> [5/6] 应用 deployment"
kubectl apply -f "$DEPLOY_DIR/deployment-cimicode-bridge-105.yaml"
sleep 30
kubectl -n agentos get pods -l app=cimicode-bridge

echo "==> [6/6] 健康验证"
POD=$(kubectl -n agentos get pod -l app=cimicode-bridge -o jsonpath="{.items[0].metadata.name}")
kubectl -n agentos exec "$POD" -- python -c 'import urllib.request;print("status:",urllib.request.urlopen("http://127.0.0.1:8081/status").read().decode())' 2>/dev/null \
  || echo "WARN: /status 查询失败，请手动检查"

echo
echo "==> 部署完成。在 Element Web 群里 @cimicode-agent 测试，然后看日志："
echo "    kubectl -n agentos logs -f deployment/cimicode-bridge"
