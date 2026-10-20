#!/bin/sh
# opencode pod entrypoint（单容器，外网 opencode npm 形态——与
# cimicode-runtime 同契约，仅配置路径/命令名不同）：
#   1. 种子 opencode 全局配置到 $HOME/.config/opencode/（存在即不覆盖——排障
#      时可挂 ConfigMap 手改）；skills 经种子配置的 skills.paths 直读镜像目
#      录 /opt/agentteams/skills，零拷贝、不往工作目录发布任何东西；
#   2. 预建空 node_modules——opencode 对配置目录做 node_modules 存在性检查，
#      缺则后台 npm install @opencode-ai/plugin（离线环境必败）——空目录让
#      它直接跳过（custom tool 链路已退役，本无 tools/*.ts 需要加载）；
#   3. 前台 exec opencode serve（:4096，bridge 的 turn 入口）。
# agent.md 不经本脚本：bridge 把每 turn 重拼的 agent.md 放在消息体的
# system 字段（opencode 原生通道，1.18.27 session/prompt.ts
# PromptInput.system）随 POST /session/{id}/message 下发，当 turn 即生效
# ——无 pod 内 helper、无 AGENTS.md 文件落盘。
# 工作目录无 emptyDir：/workspace/work 即容器可写层（cwd 与 shared/ 同根，
# 见 WORKDIR 推导行）；pod 重建丢会话由 bridge 的
# 404 自愈 + taskflow mc pull 兜住。
# 模型配置也不经本脚本：operator 以 OPENCODE_CONFIG_CONTENT env
# （secretKeyRef，1.18.27 config/config.ts 直读、合并优先级最高）注入。
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/workspace/work}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

export AGENTTEAMS_FS_ROOT="$WORKDIR"

# 种子全局配置（opencode 全局配置目录 = $HOME/.config/opencode，配置文件名
# opencode.json——见 opencode config/config.ts）
CFG_DIR="$HOME/.config/opencode"
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/opencode.json" ]; then
    cp /opt/agentteams/opencode.json "$CFG_DIR/opencode.json"
    echo "[opencode] seeded global config to $CFG_DIR/opencode.json"
fi
# 空 node_modules：跳过后台 npm install 尝试（见文件头注释）
mkdir -p "$CFG_DIR/node_modules"

echo "[opencode] workdir=$WORKDIR port=$OPENCODE_PORT"
exec opencode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
