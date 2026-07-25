#!/usr/bin/env bash
#
# build.sh — 一键构建 hiclaw worker 镜像（基于自定义 openclaw fork）
#
# 构建顺序: [controller 可选] → [openclaw-base 可选] → worker
#   - controller:     默认跳过（已 build 过），--build-controller 重建
#   - openclaw base:  默认构建，--skip-openclaw-base 跳过（没改 openclaw 源码时复用已有）
#   - worker:         始终构建（FROM openclaw base + COPY controller 的 hiclaw CLI）
#
# 所有镜像名 / 版本 tag / 构建开关 均可通过命令行参数或环境变量配置。
#
# ── 常用场景速查 ──────────────────────────────────────────────
#   ./build.sh                          # 日常: 跳 controller, build openclaw base + worker
#   ./build.sh --skip-openclaw-base     # 只改了 worker, 复用 base, 秒出 worker (最快)
#   ./build.sh --build-controller       # controller 也要重建
#   ./build.sh --help                   # 看全部选项 + 完整分类示例
#
#   # 全量全新构建 (三镜像都不走缓存, 拉 custom 分支最新代码 — 最全):
#   ./build.sh --no-cache --build-controller \
#       --controller-image hiclaw/hiclaw-controller-patch --controller-tag v1.0 \
#       --openclaw-base-image  hiclaw/hiclaw-openclaw-patch  --openclaw-base-tag v1.0 \
#       --worker-image         hiclaw/hiclaw-worker-patch     --worker-tag v1.0
# ──────────────────────────────────────────────────────────────
#
# ── ⚠ 重要提示：关于 --no-cache ───────────────────────────────
#   1. --no-cache 只对"实际构建"的镜像生效；跳过构建的（默认 controller、或 --skip-openclaw-base）
#      走 docker image inspect 复用，不触发 build，自然不受影响。
#   2. 普通构建（不带 --no-cache）时，openclaw-base 的 `git clone` 层会命中 Docker 缓存——
#      即便 fork 的 custom 分支有新提交也不会重新拉。要拿 fork 最新代码，必须加 --no-cache，
#      或用 --docker-build-args "--build-arg OPENCLAW_COMMIT=<sha>" 钉到具体 commit。
#   3. --no-cache 会让 openclaw-base 重跑完整 pnpm build 链（apt+node → clone → install → build
#      → ui:build，约 5~10 分钟），这是"全新构建"的预期代价，不是卡住。
#   4. 跑全量前先确认 custom 分支存在，否则 git clone -b 会直接失败：
#      git ls-remote --heads https://github.com/shepherd-aaa/hiclaw-openclaw.git | grep hiclaw-2026.4.14-custom
# ──────────────────────────────────────────────────────────────
set -euo pipefail

# ─── 自定义镜像配置 ───────────────────────────────────────────
# 说明: 下面都是【你要构建的自定义镜像】，默认带 -patch 后缀，以区别于官方镜像。
#       环境变量可覆盖；命令行参数优先级最高。每个镜像由「镜像名 + tag」组成。

# controller 镜像（默认跳过构建，复用已 build 的）
CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-hiclaw/hiclaw-controller-patch}"   # 镜像名 (repository)
CONTROLLER_TAG="${CONTROLLER_TAG:-latest}"                              # 版本 tag
BUILD_CONTROLLER="${BUILD_CONTROLLER:-0}"                               # 1=构建, 0=跳过复用

# openclaw base 镜像（openclaw 源码编译产物，worker 的基础；默认构建）
OPENCLAW_BASE_IMAGE="${OPENCLAW_BASE_IMAGE:-hiclaw/openclaw-hiclaw-patch}"  # 镜像名
OPENCLAW_BASE_TAG="${OPENCLAW_BASE_TAG:-latest}"                           # 版本 tag
BUILD_OPENCLAW_BASE="${BUILD_OPENCLAW_BASE:-1}"                            # 1=构建, 0=跳过复用

# worker 镜像（最终产物，始终构建）
WORKER_IMAGE="${WORKER_IMAGE:-hiclaw/worker-agent-patch}"               # 镜像名
WORKER_TAG="${WORKER_TAG:-latest}"                                      # 版本 tag

# 透传给 docker build 的额外参数（代理 / npm 镜像等）
EXTRA_BUILD_ARGS="${EXTRA_BUILD_ARGS:-}"

# --no-cache：对所有"实际构建"的镜像加 --no-cache（跳过构建的镜像走复用，不受影响）
NO_CACHE="${NO_CACHE:-0}"

# ─── 帮助 ─────────────────────────────────────────────────────
usage() {
  cat <<EOF
用法: $0 [选项]

一键构建 hiclaw worker 镜像。默认: 跳过 controller、构建 openclaw base + worker。

镜像名 / tag / 构建开关（命令行或环境变量均可，默认带 -patch 后缀）:
  --controller-image NAME       controller 镜像名     (默认: $CONTROLLER_IMAGE)
  --controller-tag TAG          controller tag        (默认: $CONTROLLER_TAG)
  --build-controller            构建 controller         (默认跳过，复用已有)
  --openclaw-base-image NAME    openclaw base 镜像名   (默认: $OPENCLAW_BASE_IMAGE)
  --openclaw-base-tag TAG       openclaw base tag      (默认: $OPENCLAW_BASE_TAG)
  --skip-openclaw-base          跳过 openclaw base 构建  (默认构建；base 没改时用，复用已有)
  --worker-image NAME           worker 镜像名          (默认: $WORKER_IMAGE)
  --worker-tag TAG              worker tag             (默认: $WORKER_TAG)
  --docker-build-args ARGS      透传给 docker build 的额外参数 (代理 / 镜像等)
  --no-cache                    对所有"实际构建"的镜像加 --no-cache (跳过构建的不受影响；想拉 fork 最新代码时用)
  -h, --help                    显示此帮助

⚠ 关于 --no-cache (重要, 完整说明见脚本头注释):
  · 不带 --no-cache 时, openclaw-base 的 git clone 层会命中 Docker 缓存, fork 有新提交也不重拉。
  · 要拿 fork 最新代码必须加 --no-cache (或 --docker-build-args "--build-arg OPENCLAW_COMMIT=<sha>" 钉 commit)。
  · --no-cache 会让 base 重跑完整 pnpm build 链 (~5-10 分钟)。

环境变量（同名大写，命令行参数优先）:
  CONTROLLER_IMAGE / CONTROLLER_TAG / BUILD_CONTROLLER
  OPENCLAW_BASE_IMAGE / OPENCLAW_BASE_TAG / BUILD_OPENCLAW_BASE
  WORKER_IMAGE / WORKER_TAG / EXTRA_BUILD_ARGS

示例:

  # —— 基础 ——
  $0                                                 # 默认: 跳过 controller, build openclaw base + worker
  $0 --build-controller                              # 全量: controller + base + worker 都 build
  $0 --skip-openclaw-base                            # 只 build worker (复用 base + controller, 最快)

  # —— 全量全新构建 (最全: 三镜像都不走缓存, 拉 fork 最新代码) ——
  $0 --no-cache --build-controller \
     --controller-image hiclaw/hiclaw-controller-patch --controller-tag v1.0 \
     --openclaw-base-image  hiclaw/hiclaw-openclaw-patch  --openclaw-base-tag v1.0 \
     --worker-image         hiclaw/hiclaw-worker-patch     --worker-tag v1.0

  # —— 改 tag (版本号) ——
  $0 --worker-tag v1.0                               # 只给 worker 打 v1.0
  $0 --openclaw-base-tag v1.0 --worker-tag v1.0      # base + worker 同 tag
  $0 --controller-tag v1.0 --openclaw-base-tag v1.0 --worker-tag v1.0   # 三镜像同 tag (发版)

  # —— 改镜像名 (自定义仓库 / 名字) ——
  $0 --worker-image myorg/my-worker                  # worker 用自定义名
  $0 --openclaw-base-image myorg/oc --worker-image myorg/wrk            # base + worker 自定义
  $0 --controller-image myorg/ctrl --openclaw-base-image myorg/oc --worker-image myorg/wrk  # 全自定义

  # —— 用环境变量配置 (等价于命令行参数) ——
  WORKER_TAG=v1.0 $0
  OPENCLAW_BASE_TAG=dev1 WORKER_TAG=dev1 BUILD_CONTROLLER=1 $0

  # —— 复用已存在的镜像 (不重建) ——
  $0 --skip-openclaw-base                            # 复用本地已 build 的 base
  $0 --openclaw-base-image registry.example.com/oc:v1 --skip-openclaw-base  # 指向 registry 既有 base

  # —— 网络 (国内服务器, clone github / npm 慢时加代理或镜像) ——
  $0 --docker-build-args "--build-arg HTTP_PROXY=http://host:1087 --build-arg HTTPS_PROXY=http://host:1087"
  $0 --docker-build-args "--build-arg NPM_REGISTRY=https://registry.npmmirror.com/ --build-arg APT_MIRROR=mirrors.aliyun.com"

  # —— 实战 ——
  # 改了 openclaw 源码并 push 到 fork 后, 重建 base + worker:
  $0
  # 只改了 worker/ 下脚本 (entrypoint 等), 不动 openclaw 源码, 只 build worker:
  $0 --skip-openclaw-base
  # controller 也改了, 全量重建三个镜像:
  $0 --build-controller
EOF
}

# ─── 命令行参数解析 ───────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-controller)      BUILD_CONTROLLER=1; shift;;
    --skip-openclaw-base)    BUILD_OPENCLAW_BASE=0; shift;;
    --controller-image)      CONTROLLER_IMAGE="${2:?--controller-image 需要值}"; shift 2;;
    --controller-tag)        CONTROLLER_TAG="${2:?--controller-tag 需要值}"; shift 2;;
    --openclaw-base-image)   OPENCLAW_BASE_IMAGE="${2:?--openclaw-base-image 需要值}"; shift 2;;
    --openclaw-base-tag)     OPENCLAW_BASE_TAG="${2:?--openclaw-base-tag 需要值}"; shift 2;;
    --worker-image)          WORKER_IMAGE="${2:?--worker-image 需要值}"; shift 2;;
    --worker-tag)            WORKER_TAG="${2:?--worker-tag 需要值}"; shift 2;;
    --docker-build-args)     EXTRA_BUILD_ARGS="${2:?--docker-build-args 需要值}"; shift 2;;
    --no-cache)              NO_CACHE=1; shift;;
    -h|--help)               usage; exit 0;;
    *) echo "✗ 未知参数: $1" >&2; usage; exit 1;;
  esac
done

# --no-cache 折进 EXTRA_BUILD_ARGS：自动作用于所有"实际构建"的镜像
# （跳过的 controller/base 走 docker image inspect 复用，不触发 build，自然不受影响）
if [[ "$NO_CACHE" = 1 ]]; then
  EXTRA_BUILD_ARGS="${EXTRA_BUILD_ARGS:+$EXTRA_BUILD_ARGS }--no-cache"
fi

# ─── 辅助函数 ─────────────────────────────────────────────────
log()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ─── 定位 AgentTeams 根目录（脚本在 custom/build/，回溯两级）──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# ─── 前置检查 ─────────────────────────────────────────────────
command -v make   >/dev/null 2>&1 || die "未找到 make，请先安装"
command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装"
[[ -f openclaw-base/Dockerfile ]] || die "未在 $REPO_ROOT 找到 openclaw-base/Dockerfile（脚本位置或仓库结构异常）"

log "AgentTeams 根目录: $REPO_ROOT"
log "controller:    ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}  (构建: $([ "$BUILD_CONTROLLER" = 1 ] && echo '是' || echo '否·复用已有'))"
log "openclaw base: ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}  (构建: $([ "$BUILD_OPENCLAW_BASE" = 1 ] && echo '是' || echo '否·复用已有'))"
log "worker:        ${WORKER_IMAGE}:${WORKER_TAG}  (构建: 是)"
[[ -n "$EXTRA_BUILD_ARGS" ]] && log "额外 build 参数: $EXTRA_BUILD_ARGS"
echo

# ─── [1/3] controller（可选）──────────────────────────────────
if [[ "$BUILD_CONTROLLER" = 1 ]]; then
  log "[1/3] 构建 controller → ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
  make build-hiclaw-controller \
    LOCAL_CONTROLLER="${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" \
    DOCKER_BUILD_ARGS="$EXTRA_BUILD_ARGS"
  ok "controller 构建完成"
else
  log "[1/3] 跳过 controller 构建（如需重建: --build-controller）"
  docker image inspect "${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" >/dev/null 2>&1 \
    || die "本地不存在 ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}！请加 --build-controller 先构建，或用 --controller-image/--controller-tag 指向已存在的镜像"
  ok "复用已有 controller: ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
fi

# ─── [2/3] openclaw-base（可选）───────────────────────────────
if [[ "$BUILD_OPENCLAW_BASE" = 1 ]]; then
  log "[2/3] 构建 openclaw-base → ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}  (从 fork clone openclaw 源码，最耗时)"
  make build-openclaw-base \
    LOCAL_OPENCLAW_BASE="${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}" \
    DOCKER_BUILD_ARGS="$EXTRA_BUILD_ARGS"
  ok "openclaw base 构建完成"
else
  log "[2/3] 跳过 openclaw base 构建（如需重建: 去掉 --skip-openclaw-base）"
  docker image inspect "${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}" >/dev/null 2>&1 \
    || die "本地不存在 ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}！base 无法跳过——去掉 --skip-openclaw-base 先构建，或用 --openclaw-base-image/--openclaw-base-tag 指向已存在的镜像"
  ok "复用已有 openclaw base: ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}"
fi

# ─── [3/3] worker（始终构建）──────────────────────────────────
log "[3/3] 构建 worker → ${WORKER_IMAGE}:${WORKER_TAG}"
WORKER_BUILD_ARGS="--build-arg HICLAW_CONTROLLER_IMAGE=${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
[[ -n "$EXTRA_BUILD_ARGS" ]] && WORKER_BUILD_ARGS="$WORKER_BUILD_ARGS $EXTRA_BUILD_ARGS"
make build-worker \
  LOCAL_WORKER="${WORKER_IMAGE}:${WORKER_TAG}" \
  OPENCLAW_BASE_IMAGE="$OPENCLAW_BASE_IMAGE" \
  OPENCLAW_BASE_VERSION="$OPENCLAW_BASE_TAG" \
  DOCKER_BUILD_ARGS="$WORKER_BUILD_ARGS"
ok "worker 构建完成"

# ─── 验证 ─────────────────────────────────────────────────────
echo
log "构建结果:"
for ref in "${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" "${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}" "${WORKER_IMAGE}:${WORKER_TAG}"; do
  line="$(docker images "$ref" --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' 2>/dev/null)"
  if [[ -n "$line" ]]; then
    printf '  %s\n' "$line"
  else
    printf '  %s  ⚠ 未找到\n' "$ref"
  fi
done
echo
ok "完成 ✅  worker 镜像: ${WORKER_IMAGE}:${WORKER_TAG}"
echo "   部署时设: HICLAW_WORKER_IMAGE=${WORKER_IMAGE}:${WORKER_TAG}"
