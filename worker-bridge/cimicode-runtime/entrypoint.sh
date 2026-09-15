#!/bin/sh
# cimicode pod entrypoint（单容器合并形态）：
#   1. 把镜像内 skill 树发布到工作目录的 .opencode/skills/（opencode 以
#      cwd 原生发现 project skills，skill 工具才可用）；
#   2. 种子 opencode 全局配置（智谱 provider + 转发 bash 工具）到 $HOME；
#   3. 后台起 sandbox helper（:4097——bridge 推 AGENTS.md / bash 工具
#      转发 /exec 都走它）；
#   4. 前台 exec opencode serve（:4096，bridge 的 turn 入口）。
# 旧双 pod 形态的 180s skill-wait 轮询已删：单 pod 文件本地就绪才继续。
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/workspace}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
HELPER_PORT="${BRIDGE_SANDBOX_HELPER_PORT:-4097}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

export AGENTTEAMS_FS_ROOT="$WORKDIR"
export OPENCODE_WORKDIR="$WORKDIR"

# 发布 skill 树为 opencode project skills（镜像内副本权威，每次启动重同步）
if [ -d /opt/agentteams/skills ]; then
    mkdir -p "$WORKDIR/.opencode/skills"
    cp -rf /opt/agentteams/skills/. "$WORKDIR/.opencode/skills/"
    echo "[cimicode] skills published to $WORKDIR/.opencode/skills ($(ls "$WORKDIR/.opencode/skills" | wc -l) entries)"
fi

# 种子 opencode 全局配置（存在即不覆盖——排障时可挂 ConfigMap 手改）
CFG_DIR="$HOME/.config/opencode"
mkdir -p "$CFG_DIR/tools"
if [ ! -f "$CFG_DIR/opencode.json" ]; then
    cp /opt/agentteams/opencode.json "$CFG_DIR/opencode.json"
fi
cp /opt/agentteams/tools/bash.ts "$CFG_DIR/tools/bash.ts"

# bash 工具转发目标：默认同容器 helper 回环地址
export SANDBOX_EXEC_URL="${SANDBOX_EXEC_URL:-http://127.0.0.1:${HELPER_PORT}}"

echo "[cimicode] workdir=$WORKDIR port=$OPENCODE_PORT helper_port=$HELPER_PORT exec_url=$SANDBOX_EXEC_URL"
python3 /opt/agentteams/sandbox_helper.py &
exec opencode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
