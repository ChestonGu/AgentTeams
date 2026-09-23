#!/bin/sh
# cimicode pod entrypoint（单容器，基于内部 cimicode 换皮 opencode）：
#   1. 种子 cimicode 全局配置到 $HOME/.cimi/cimicode/（存在即不覆盖——排障
#      时可挂 ConfigMap 手改）；skills 经种子配置的 skills.paths 直读镜像目
#      录 /opt/agentteams/skills，零拷贝、不往工作目录发布任何东西；
#   2. 预建空 node_modules——cimicode 对每个配置目录做 node_modules 存在性
#      检查（core/npm.ts），缺则后台 npm install @opencode-ai/plugin（内网
#      不可达，注定失败）——空目录让它直接跳过（custom tool 链路已退役，
#      本无 tools/*.ts 需要加载）；
#   3. 前台 exec cimicode serve（:4096，bridge 的 turn 入口）。
# agent.md 不经本脚本：bridge 把每 turn 重拼的 agent.md 放在消息体的
# system 字段（cimicode 原生通道）随 POST /session/{id}/message 下发，当
# turn 即生效——无 pod 内 helper、无 AGENTS.md 文件落盘。
# 工作目录无 emptyDir：/workspace/work 即容器可写层（cwd 与 shared/ 同根，
# 见 WORKDIR 推导行）；pod 重建丢会话由 bridge 的
# 404 自愈 + taskflow mc pull 兜住。
# 模型配置也不经本脚本：operator 以 OPENCODE_CONFIG_CONTENT env
# （secretKeyRef）注入，cimicode 原生消费（合并优先级最高）。
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/workspace/work}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

export AGENTTEAMS_FS_ROOT="$WORKDIR"

# 种子全局配置（cimicode 全局配置目录 = $HOME/.cimi/cimicode，配置文件名
# cimicode.json——见 agi-opencode packages/core/src/global.ts）
CFG_DIR="$HOME/.cimi/cimicode"
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/cimicode.json" ]; then
    cp /opt/agentteams/cimicode.json "$CFG_DIR/cimicode.json"
    echo "[cimicode] seeded global config to $CFG_DIR/cimicode.json"
fi
# 空 node_modules：跳过后台 npm install 尝试（见文件头注释）
mkdir -p "$CFG_DIR/node_modules"

echo "[cimicode] workdir=$WORKDIR port=$OPENCODE_PORT"
exec cimicode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
