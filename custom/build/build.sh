#!/usr/bin/env bash
#
# build.sh — 一键构建 hiclaw worker / manager 镜像（基于自定义 openclaw fork）
#
# 必须显式指定要构建哪个: --worker 和/或 --manager (至少一个)。
# 构建顺序: [controller 可选] → [openclaw-base 可选] → { worker | manager | 两者 }
#   - controller:     默认跳过（复用已 build 过的）；--build-controller 重建。
#                      worker 和 manager 都需要它（COPY hiclaw CLI）。
#   - openclaw base:  默认构建；--skip-openclaw-base 复用已有（没改 openclaw 源码时）。
#                      worker 和 manager 都 FROM 它。
#   - worker/manager: 按 --worker / --manager 选择，可只建一个或两个都建。
#
# 所有镜像名 / 版本 tag / 构建开关 均可通过命令行参数或环境变量配置。
#
# ── 常用场景速查 ──────────────────────────────────────────────
#   ./build.sh --worker                           # 只建 worker (改了 worker 脚本 / openclaw)
#   ./build.sh --manager                          # 只建 manager
#   ./build.sh --worker --manager                 # 两个都建 (改了 openclaw 源码后最常用)
#   ./build.sh --worker --skip-openclaw-base       # 只改了 worker 脚本, 复用 base, 秒出
#   ./build.sh --help                             # 全部选项 + 完整示例
#
#   # 改了 openclaw 源码并 push 到 fork 后, 重建 base + worker + manager (全量):
#   ./build.sh --worker --manager --no-cache \
#       --openclaw-base-tag v1.0 --worker-tag v1.0 --manager-tag v1.0
# ──────────────────────────────────────────────────────────────
#
# ── ⚠ 重要提示：关于 --no-cache 与 openclaw 源码改动 ──────────
#   1. openclaw-base 的 Dockerfile 是 `git clone` fork 的 `hiclaw-2026.4.14-custom` 分支,
#      **不读本地 hiclaw-openclaw 目录**。改了 openclaw 源码必须先 push 到该分支。
#   2. 普通构建（不带 --no-cache）时, clone 层命中 Docker 缓存——即便 fork 分支有新提交
#      也不会重新拉。要拿 push 后的最新代码，必须加 --no-cache，或用
#      --docker-build-args "--build-arg OPENCLAW_COMMIT=<sha>" 钉到具体 commit。
#   3. --no-cache 会让 openclaw-base 重跑完整 pnpm build 链（apt+node → clone → install
#      → build → ui:build，约 5~10 分钟），这是"全新构建"的预期代价，不是卡住。
#   4. 跑前先确认 custom 分支存在:
#      git ls-remote --heads https://github.com/shepherd-aaa/hiclaw-openclaw.git | grep hiclaw-2026.4.14-custom
#   5. pnpm install 只装 node_modules 依赖，**不会覆盖 src/extensions 源码**;
#      pnpm build 把源码编译进 dist/ 才是改动生效的环节。
# ──────────────────────────────────────────────────────────────
set -euo pipefail

# ─── 自定义镜像配置 ───────────────────────────────────────────
# 默认带 -patch 后缀, 以区别于官方镜像。环境变量可覆盖; 命令行参数优先级最高。

# 选择构建目标 (必须至少传一个)
BUILD_WORKER="${BUILD_WORKER:-0}"        # --worker → 1
BUILD_MANAGER="${BUILD_MANAGER:-0}"      # --manager → 1

# controller 镜像 (worker 和 manager 都依赖; 默认跳过构建, 复用已有)
CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-hiclaw/hiclaw-controller-patch}"
CONTROLLER_TAG="${CONTROLLER_TAG:-latest}"
BUILD_CONTROLLER="${BUILD_CONTROLLER:-0}"   # 1=构建, 0=跳过复用

# openclaw base 镜像 (worker 和 manager 都 FROM 它; 默认构建)
OPENCLAW_BASE_IMAGE="${OPENCLAW_BASE_IMAGE:-hiclaw/openclaw-hiclaw-patch}"
OPENCLAW_BASE_TAG="${OPENCLAW_BASE_TAG:-latest}"
BUILD_OPENCLAW_BASE="${BUILD_OPENCLAW_BASE:-1}"   # 1=构建, 0=跳过复用

# worker 镜像
WORKER_IMAGE="${WORKER_IMAGE:-hiclaw/worker-agent-patch}"
WORKER_TAG="${WORKER_TAG:-latest}"

# manager 镜像
MANAGER_IMAGE="${MANAGER_IMAGE:-hiclaw/hiclaw-manager-patch}"
MANAGER_TAG="${MANAGER_TAG:-latest}"

# 透传给 docker build 的额外参数 (代理 / npm 镜像 / OPENCLAW_COMMIT 等)
EXTRA_BUILD_ARGS="${EXTRA_BUILD_ARGS:-}"

# --no-cache：对所有"实际构建"的镜像加 --no-cache (跳过构建的走复用, 不受影响)
NO_CACHE="${NO_CACHE:-0}"

# ─── 帮助 ─────────────────────────────────────────────────────
usage() {
  cat <<EOF
用法: $0 --worker|--manager [选项]

一键构建 hiclaw worker / manager 镜像。必须显式指定至少一个构建目标。

构建目标 (二选一或都选):
  --worker                      构建 worker 镜像
  --manager                     构建 manager 镜像

镜像名 / tag (命令行或环境变量, 默认带 -patch 后缀):
  --worker-image NAME           worker 镜像名     (默认: $WORKER_IMAGE)
  --worker-tag TAG              worker tag        (默认: $WORKER_TAG)
  --manager-image NAME          manager 镜像名    (默认: $MANAGER_IMAGE)
  --manager-tag TAG             manager tag       (默认: $MANAGER_TAG)
  --controller-image NAME       controller 镜像名 (默认: $CONTROLLER_IMAGE)
  --controller-tag TAG          controller tag    (默认: $CONTROLLER_TAG)
  --build-controller            构建 controller    (默认跳过, 复用已有; worker/manager 都需要它)
  --openclaw-base-image NAME    openclaw base 镜像名 (默认: $OPENCLAW_BASE_IMAGE)
  --openclaw-base-tag TAG       openclaw base tag    (默认: $OPENCLAW_BASE_TAG)
  --skip-openclaw-base          跳过 base 构建       (默认构建; base 没改时用, 复用已有)
  --docker-build-args ARGS      透传给 docker build 的额外参数 (代理 / 镜像 / OPENCLAW_COMMIT 等)
  --no-cache                    对所有"实际构建"的镜像加 --no-cache (想拉 fork 最新代码时用)
  -h, --help                    显示此帮助

⚠ 关于 openclaw 源码改动 (完整说明见脚本头注释):
  · openclaw-base 从 GitHub fork 的 hiclaw-2026.4.14-custom 分支 clone, 不读本地目录;
    改了源码必须先 push, 再加 --no-cache (或 --docker-build-args "--build-arg OPENCLAW_COMMIT=<sha>") 重建。
  · pnpm install 不覆盖源码; pnpm build 把源码编进 dist/ 使改动生效。

环境变量 (同名大写, 命令行参数优先):
  BUILD_WORKER / BUILD_MANAGER
  CONTROLLER_IMAGE / CONTROLLER_TAG / BUILD_CONTROLLER
  OPENCLAW_BASE_IMAGE / OPENCLAW_BASE_TAG / BUILD_OPENCLAW_BASE
  WORKER_IMAGE / WORKER_TAG / MANAGER_IMAGE / MANAGER_TAG / EXTRA_BUILD_ARGS

═══ 场景一: 只构建 worker ═══
  # 改了 worker 脚本 (entrypoint / notify-platform.sh), 不动 openclaw 源码:
  $0 --worker --skip-openclaw-base
  # 改了 openclaw 源码并已 push (worker 也基于 base, 需重建 base):
  $0 --worker
  # 钉到具体 commit, 避免 clone 缓存:
  $0 --worker --docker-build-args "--build-arg OPENCLAW_COMMIT=\$(git -C ../hiclaw-openclaw rev-parse HEAD)"

═══ 场景二: 只构建 manager ═══
  # manager 也是 openclaw, 复用已 build 的 base + controller:
  $0 --manager --skip-openclaw-base
  # openclaw 源码改了 (manager 也要新 base):
  $0 --manager

═══ 场景三: worker + manager 都构建 (改了 openclaw 源码后最常用) ═══
  # 重建 base + worker + manager, controller 复用:
  $0 --worker --manager
  # 全量全新构建 (四镜像都不走缓存, 拉 fork 最新; 含 controller 重建);
  # 显式写出所有镜像名 + tag (最全形式):
  $0 --no-cache --worker --manager --build-controller \\
     --controller-image hiclaw/hiclaw-controller-patch --controller-tag v1.0 \\
     --openclaw-base-image hiclaw/openclaw-hiclaw-patch --openclaw-base-tag v1.0 \\
     --worker-image hiclaw/hiclaw-worker-patch --worker-tag v1.0 \\
     --manager-image hiclaw/hiclaw-manager-patch --manager-tag v1.0
  # 用默认 -patch 名时, 上面 4 个 --*-image 可省, 只留 --*-tag v1.0 即可。

═══ 其他 ═══
  # 只改 tag (版本号):
  $0 --worker --worker-tag v1.0
  $0 --worker --manager --worker-tag v1.0 --manager-tag v1.0
  # 改镜像名 (自定义仓库):
  $0 --worker --worker-image myorg/my-worker
  # 网络 (国内服务器 clone github / npm 慢):
  $0 --worker --manager --docker-build-args "--build-arg HTTP_PROXY=http://host:1087 --build-arg HTTPS_PROXY=http://host:1087"
EOF
}

# ─── 命令行参数解析 ───────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker)                BUILD_WORKER=1; shift;;
    --manager)               BUILD_MANAGER=1; shift;;
    --build-controller)      BUILD_CONTROLLER=1; shift;;
    --skip-openclaw-base)    BUILD_OPENCLAW_BASE=0; shift;;
    --controller-image)      CONTROLLER_IMAGE="${2:?--controller-image 需要值}"; shift 2;;
    --controller-tag)        CONTROLLER_TAG="${2:?--controller-tag 需要值}"; shift 2;;
    --openclaw-base-image)   OPENCLAW_BASE_IMAGE="${2:?--openclaw-base-image 需要值}"; shift 2;;
    --openclaw-base-tag)     OPENCLAW_BASE_TAG="${2:?--openclaw-base-tag 需要值}"; shift 2;;
    --worker-image)          WORKER_IMAGE="${2:?--worker-image 需要值}"; shift 2;;
    --worker-tag)            WORKER_TAG="${2:?--worker-tag 需要值}"; shift 2;;
    --manager-image)         MANAGER_IMAGE="${2:?--manager-image 需要值}"; shift 2;;
    --manager-tag)           MANAGER_TAG="${2:?--manager-tag 需要值}"; shift 2;;
    --docker-build-args)     EXTRA_BUILD_ARGS="${2:?--docker-build-args 需要值}"; shift 2;;
    --no-cache)              NO_CACHE=1; shift;;
    -h|--help)               usage; exit 0;;
    *) echo "✗ 未知参数: $1" >&2; usage; exit 1;;
  esac
done

# 必须至少指定一个构建目标
if [[ "$BUILD_WORKER" != 1 && "$BUILD_MANAGER" != 1 ]]; then
  echo "✗ 必须指定至少一个构建目标: --worker 和/或 --manager" >&2
  echo >&2
  usage >&2
  exit 1
fi

# --no-cache 折进 EXTRA_BUILD_ARGS：自动作用于所有"实际构建"的镜像
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
[[ -f manager/Dockerfile ]]       || die "未在 $REPO_ROOT 找到 manager/Dockerfile"

TARGETS=""
[[ "$BUILD_WORKER" = 1 ]]  && TARGETS="${TARGETS}worker "
[[ "$BUILD_MANAGER" = 1 ]] && TARGETS="${TARGETS}manager "

log "AgentTeams 根目录: $REPO_ROOT"
log "构建目标: ${TARGETS}"
log "controller:    ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}  (构建: $([ "$BUILD_CONTROLLER" = 1 ] && echo '是' || echo '否·复用已有'))"
log "openclaw base: ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}  (构建: $([ "$BUILD_OPENCLAW_BASE" = 1 ] && echo '是' || echo '否·复用已有'))"
[[ "$BUILD_WORKER" = 1 ]]  && log "worker:        ${WORKER_IMAGE}:${WORKER_TAG}"
[[ "$BUILD_MANAGER" = 1 ]] && log "manager:       ${MANAGER_IMAGE}:${MANAGER_TAG}"
[[ -n "$EXTRA_BUILD_ARGS" ]] && log "额外 build 参数: $EXTRA_BUILD_ARGS"
echo

# ─── [1] controller（worker 和 manager 都依赖；可选）─────────
if [[ "$BUILD_CONTROLLER" = 1 ]]; then
  log "[1] 构建 controller → ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
  make build-hiclaw-controller \
    LOCAL_CONTROLLER="${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" \
    DOCKER_BUILD_ARGS="$EXTRA_BUILD_ARGS"
  ok "controller 构建完成"
else
  log "[1] 跳过 controller 构建（如需重建: --build-controller）"
  docker image inspect "${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" >/dev/null 2>&1 \
    || die "本地不存在 ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}！worker/manager 都需要它。请加 --build-controller 先构建，或用 --controller-image/--controller-tag 指向已存在的镜像"
  ok "复用已有 controller: ${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
fi

# ─── [2] openclaw-base（worker 和 manager 都 FROM 它；可选）───
if [[ "$BUILD_OPENCLAW_BASE" = 1 ]]; then
  log "[2] 构建 openclaw-base → ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}  (从 fork clone openclaw 源码, 最耗时)"
  make build-openclaw-base \
    LOCAL_OPENCLAW_BASE="${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}" \
    DOCKER_BUILD_ARGS="$EXTRA_BUILD_ARGS"
  ok "openclaw base 构建完成"
else
  log "[2] 跳过 openclaw base 构建（如需重建: 去掉 --skip-openclaw-base）"
  docker image inspect "${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}" >/dev/null 2>&1 \
    || die "本地不存在 ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}！worker/manager 都 FROM 它。去掉 --skip-openclaw-base 先构建，或用 --openclaw-base-image/--openclaw-base-tag 指向已存在的镜像"
  ok "复用已有 openclaw base: ${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}"
fi

# ─── [3] worker（可选）───────────────────────────────────────
if [[ "$BUILD_WORKER" = 1 ]]; then
  log "[3] 构建 worker → ${WORKER_IMAGE}:${WORKER_TAG}"
  WORKER_BUILD_ARGS="--build-arg HICLAW_CONTROLLER_IMAGE=${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
  [[ -n "$EXTRA_BUILD_ARGS" ]] && WORKER_BUILD_ARGS="$WORKER_BUILD_ARGS $EXTRA_BUILD_ARGS"
  make build-worker \
    LOCAL_WORKER="${WORKER_IMAGE}:${WORKER_TAG}" \
    OPENCLAW_BASE_IMAGE="$OPENCLAW_BASE_IMAGE" \
    OPENCLAW_BASE_VERSION="$OPENCLAW_BASE_TAG" \
    DOCKER_BUILD_ARGS="$WORKER_BUILD_ARGS"
  ok "worker 构建完成"
fi

# ─── [4] manager（可选）──────────────────────────────────────
# 直接 docker build (绕过 make build-manager, 后者会强制重建 controller)。
# manager/Dockerfile 用普通 COPY (shared/lib, manager/agent 等) 从 build context ".",
# 不需要 --build-context shared=; 只需 OPENCLAW_BASE_IMAGE + HICLAW_CONTROLLER_IMAGE。
if [[ "$BUILD_MANAGER" = 1 ]]; then
  log "[4] 构建 manager → ${MANAGER_IMAGE}:${MANAGER_TAG}  (FROM openclaw-base, 复用 controller)"
  MANAGER_BUILD_ARGS="--build-arg HICLAW_CONTROLLER_IMAGE=${CONTROLLER_IMAGE}:${CONTROLLER_TAG}"
  MANAGER_BUILD_ARGS="$MANAGER_BUILD_ARGS --build-arg OPENCLAW_BASE_IMAGE=${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}"
  [[ -n "$EXTRA_BUILD_ARGS" ]] && MANAGER_BUILD_ARGS="$MANAGER_BUILD_ARGS $EXTRA_BUILD_ARGS"
  docker build -f manager/Dockerfile $MANAGER_BUILD_ARGS -t "${MANAGER_IMAGE}:${MANAGER_TAG}" .
  ok "manager 构建完成"
fi

# ─── 验证 ─────────────────────────────────────────────────────
echo
log "构建结果:"
BUILT_REFS=("${CONTROLLER_IMAGE}:${CONTROLLER_TAG}" "${OPENCLAW_BASE_IMAGE}:${OPENCLAW_BASE_TAG}")
[[ "$BUILD_WORKER" = 1 ]]  && BUILT_REFS+=("${WORKER_IMAGE}:${WORKER_TAG}")
[[ "$BUILD_MANAGER" = 1 ]] && BUILT_REFS+=("${MANAGER_IMAGE}:${MANAGER_TAG}")
for ref in "${BUILT_REFS[@]}"; do
  line="$(docker images "$ref" --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' 2>/dev/null)"
  if [[ -n "$line" ]]; then
    printf '  %s\n' "$line"
  else
    printf '  %s  ⚠ 未找到\n' "$ref"
  fi
done
echo
ok "完成 ✅"
[[ "$BUILD_WORKER" = 1 ]]  && echo "   worker:  HICLAW_WORKER_IMAGE=${WORKER_IMAGE}:${WORKER_TAG}"
[[ "$BUILD_MANAGER" = 1 ]] && echo "   manager: HICLAW_MANAGER_IMAGE=${MANAGER_IMAGE}:${MANAGER_TAG}"
