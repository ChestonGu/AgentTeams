#!/bin/bash
# qwenpaw-worker-entrypoint.sh - QwenPaw Worker Agent container startup.
# Reads controller-injected environment variables and launches qwenpaw-worker.
#
# Environment variables:
#   AGENTTEAMS_WORKER_NAME   - Worker name (required)
#   AGENTTEAMS_FS_ENDPOINT   - MinIO/OSS endpoint (required in local mode)
#   AGENTTEAMS_FS_ACCESS_KEY - MinIO/OSS access key (required in local mode)
#   AGENTTEAMS_FS_SECRET_KEY - MinIO/OSS secret key (required in local mode)
#   AGENTTEAMS_RUNTIME       - "k8s" for controller-managed mc-wrapper storage access
#   TZ                   - Timezone (optional)

set -e

source /opt/agentteams/scripts/lib/agentteams-env.sh

WORKER_NAME="${AGENTTEAMS_WORKER_NAME:?AGENTTEAMS_WORKER_NAME is required}"
WORKER_CR_NAME="${AGENTTEAMS_WORKER_CR_NAME:-${WORKER_NAME}}"
# Align with CoPaw/openclaw/hermes: HOME == workspace == MinIO mirror root.
# install_dir is its parent so install_dir/<name> == HOME.
INSTALL_DIR="/root/agentteams-fs/agents"
WORKER_HOME="${AGENTTEAMS_WORKER_HOME:-${INSTALL_DIR}/${WORKER_NAME}}"
CONSOLE_PORT="${AGENTTEAMS_CONSOLE_PORT:-8088}"

log() {
    echo "[agentteams-qwenpaw-worker $(date '+%Y-%m-%d %H:%M:%S')] $1"
}

if [ -n "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
    log "Timezone set to ${TZ}"
fi

if [ "${AGENTTEAMS_RUNTIME:-}" = "k8s" ]; then
    log "Kubernetes mode: mc-wrapper handles storage credentials"
    # CLI args are required by qwenpaw-worker but unused by FileSync in k8s mode.
    FS_ENDPOINT="${AGENTTEAMS_FS_ENDPOINT:-k8s-placeholder}"
    FS_ACCESS_KEY="${AGENTTEAMS_FS_ACCESS_KEY:-k8s}"
    FS_SECRET_KEY="${AGENTTEAMS_FS_SECRET_KEY:-k8s}"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-storage}"
    # 静态 mc 别名契约（对齐 copaw-worker-entrypoint.sh）：oss-credentials.sh 的
    # ensure_mc_credentials 在非 oss 模式是 no-op（注释明言"静态别名由容器
    # entrypoint 配置"），FileSync 的 k8s 分支也不配别名直接宣称 ready——所以
    # 别名必须在这里配好，否则每条 mc 命令（mirror/cp）在未配置的 alias 上
    # 立刻报错，worker fail-fast 退出。
    if [ -n "${AGENTTEAMS_FS_ENDPOINT:-}" ]; then
        STORAGE_ALIAS="${AGENTTEAMS_STORAGE_ALIAS:-agentteams}"
        mc alias set "${STORAGE_ALIAS}" "${FS_ENDPOINT}" "${FS_ACCESS_KEY}" "${FS_SECRET_KEY}"
        # MC_HOST_<alias>：mc.bin 原生读取的环境形态，也覆盖不读 mc 配置文件的调用方。
        export "MC_HOST_${STORAGE_ALIAS}=${FS_ENDPOINT%%://*}://${FS_ACCESS_KEY}:${FS_SECRET_KEY}@${FS_ENDPOINT#*://}"
        log "  mc static alias configured: ${STORAGE_ALIAS}"
    fi
else
    FS_ENDPOINT="${AGENTTEAMS_FS_ENDPOINT:?AGENTTEAMS_FS_ENDPOINT is required}"
    FS_ACCESS_KEY="${AGENTTEAMS_FS_ACCESS_KEY:?AGENTTEAMS_FS_ACCESS_KEY is required}"
    FS_SECRET_KEY="${AGENTTEAMS_FS_SECRET_KEY:?AGENTTEAMS_FS_SECRET_KEY is required}"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-storage}"
fi
log "  FS bucket: ${FS_BUCKET}"

mkdir -p "${WORKER_HOME}"
export HOME="${WORKER_HOME}"
export AGENTTEAMS_WORKER_HOME="${WORKER_HOME}"
export AGENTTEAMS_AGENT_NAME="${WORKER_NAME}"
export AGENTTEAMS_AGENT_ROLE="worker"
export AGENTTEAMS_AGENT_HOME="${WORKER_HOME}"

# Set QWENPAW_WORKING_DIR before starting; QwenPaw reads it during config/plugin setup.
export QWENPAW_WORKING_DIR="${WORKER_HOME}/.qwenpaw"
export AGENT_WORKSPACE="${QWENPAW_WORKING_DIR}/workspaces/default"
export QWENPAW_SECRET_DIR="${QWENPAW_SECRET_DIR:-${QWENPAW_WORKING_DIR}.secret}"
export QWENPAW_RUNNING_IN_CONTAINER=true
export QWENPAW_LOG_LEVEL="${QWENPAW_LOG_LEVEL:-info}"

# Configure LoongSuite observability plugin if tracing is enabled.
CMS_TRACES_ENABLED="$(echo "${AGENTTEAMS_CMS_TRACES_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
if [ "${CMS_TRACES_ENABLED}" = "true" ]; then
    log "Configuring CoPaw CMS plugin..."
    LOONGSUITE_DIR="${HOME}/.loongsuite"
    mkdir -p "${LOONGSUITE_DIR}"

    cat > "${LOONGSUITE_DIR}/bootstrap-config.json" <<EOF
{
  "OTEL_EXPORTER_OTLP_ENDPOINT": "${AGENTTEAMS_CMS_ENDPOINT}",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
  "OTEL_EXPORTER_OTLP_HEADERS": "x-arms-license-key=${AGENTTEAMS_CMS_LICENSE_KEY},x-arms-project=${AGENTTEAMS_CMS_PROJECT},x-cms-workspace=${AGENTTEAMS_CMS_WORKSPACE}",
  "OTEL_SERVICE_NAME": "${AGENTTEAMS_CMS_SERVICE_NAME:-agentteams-worker-${WORKER_NAME}}",
  "OTEL_SEMCONV_STABILITY_OPT_IN": "http,gen_ai_latest_experimental",
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_AND_EVENT",
  "LOONGSUITE_PYTHON_SITE_BOOTSTRAP": "true",
  "LOONGSUITE_PYTHON_SITE_BOOTSTRAP_LOG_SUCCESS": "false"
}
EOF
    log "CoPaw CMS plugin configured at ${LOONGSUITE_DIR}/bootstrap-config.json"
    export LOONGSUITE_PYTHON_SITE_BOOTSTRAP=true
    export LOONGSUITE_PYTHON_SITE_BOOTSTRAP_LOG_SUCCESS=false
fi

VENV="/opt/venv/qwenpaw"

CMD_ARGS=(
    --name "${WORKER_NAME}"
    --cr-name "${WORKER_CR_NAME}"
    --fs "${FS_ENDPOINT}"
    --fs-key "${FS_ACCESS_KEY}"
    --fs-secret "${FS_SECRET_KEY}"
    --fs-bucket "${FS_BUCKET}"
    --install-dir "${INSTALL_DIR}"
    --console-port "${CONSOLE_PORT}"
)

log "Starting qwenpaw-worker: ${WORKER_NAME}"
log "  Worker CR name: ${WORKER_CR_NAME}"
log "  FS endpoint: ${FS_ENDPOINT}"
log "  Install dir: ${INSTALL_DIR}"
log "  Worker home: ${WORKER_HOME}"
log "  QwenPaw working dir: ${QWENPAW_WORKING_DIR}"
log "  QwenPaw venv: ${VENV}"
log "  Console port: ${CONSOLE_PORT}"

exec "${VENV}/bin/qwenpaw-worker" "${CMD_ARGS[@]}"
