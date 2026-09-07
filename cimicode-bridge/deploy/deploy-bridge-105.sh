#!/bin/bash
# =====================================================================
# cimicode-bridge @105 一键部署：拉代码 → 构建 → 导入 k3s → apply → 验证
# 用法: bash deploy-bridge-105.sh [VERSION]
#   默认 VERSION=v0.1.5
# 前置: 105 上有 fork 仓库(含 Makefile)，kubectl 可用，sudo 密码为 SUDO_PW
# 修复记录（2026-09-07）:
#   ① 导入后强制校验镜像存在（按完整镜像名匹配，防无关镜像同 tag 误判）
#   ② sed 替代 python heredoc 改 tag（plink/非交互 shell 下引号会被剥离）
# =====================================================================
set -euo pipefail

VERSION="${1:-v0.1.5}"
REPO_DIR="${REPO_DIR:-/home/extvdiadmin/workbach/AgentTeams}"
SUDO_PW="${SUDO_PW:-Cxmt@20250730}"
BRANCH=feature/stateless_cimicode_docking
DEPLOY_DIR=/home/extvdiadmin/agentteams-deploy
IMAGE="agentteams/cimicode-bridge:$VERSION"
YAML="$DEPLOY_DIR/deployment-cimicode-bridge-105.yaml"

echo "==> [1/6] 拉取最新代码 ($REPO_DIR, $BRANCH)"
cd "$REPO_DIR"
git pull origin "$BRANCH" 2>&1 | tail -2
git log --oneline -1

echo "==> [2/6] 构建镜像 $IMAGE"
# 必须看到 naming to 才算构建成功；COPY src 非缓存说明新代码已打入
make build-cimicode-bridge VERSION="$VERSION" 2>&1 | grep -E "naming to|COPY src|ERROR" | head -3 || true
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "FATAL: 构建失败，镜像 $IMAGE 不存在"; exit 1
fi

echo "==> [3/6] 导入 k3s containerd（含强制校验）"
docker save "$IMAGE" -o /tmp/cim-bridge.tar
echo "$SUDO_PW" | sudo -S k3s ctr images import /tmp/cim-bridge.tar >/dev/null 2>&1
# 强制校验：按完整引用名匹配（containerd 里带 docker.io/ 前缀），失败即中止
if ! sudo k3s ctr images ls 2>/dev/null | grep -q "agentteams/cimicode-bridge:$VERSION"; then
  echo "FATAL: 镜像导入失败（containerd 中未找到 $IMAGE），请手工执行："
  echo "  echo '<sudo密码>' | sudo -S k3s ctr images import /tmp/cim-bridge.tar"
  exit 1
fi
echo "    已确认 containerd 中存在: $IMAGE"

echo "==> [4/6] 更新 deployment 镜像 tag → $VERSION（sed 方式）"
cp cimicode-bridge/deploy/deployment-cimicode-bridge-105.yaml "$YAML"
sed -i "s/\(cimicode-bridge:\)v[0-9][0-9.]*\([-a-z0-9.]*\)\{0,1\}/\1$VERSION/" "$YAML"
# 校验替换结果
if ! grep -q "cimicode-bridge:$VERSION" "$YAML"; then
  echo "FATAL: yaml tag 替换失败，请手工编辑 $YAML"; exit 1
fi
echo "    yaml image -> $VERSION"

echo "==> [5/6] 应用 deployment"
kubectl apply -f "$YAML"
sleep 30
kubectl -n agentos get pods -l app=cimicode-bridge

echo "==> [6/6] 健康验证"
POD=$(kubectl -n agentos get pod -l app=cimicode-bridge --field-selector=status.phase=Running -o jsonpath="{.items[0].metadata.name}" 2>/dev/null || true)
if [ -n "${POD:-}" ]; then
  kubectl -n agentos exec "$POD" -- python -c 'import urllib.request;print("status:",urllib.request.urlopen("http://127.0.0.1:8081/status").read().decode())' 2>/dev/null \
    || echo "WARN: /status 查询失败（pod 可能仍在启动），稍后手动检查"
else
  echo "WARN: 无 Running 状态的 pod，检查: kubectl -n agentos get pods -l app=cimicode-bridge"
fi

echo
echo "==> 部署完成。在 Element Web 群里 @cimicode-agent 测试，然后看日志："
echo "    kubectl -n agentos logs -f deployment/cimicode-bridge"
