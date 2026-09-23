# =============================================================================
# cimicode-runtime — cimicode pod 合并镜像（cimicode + 任务协作工具 + skills）
# =============================================================================
# 基于内部 coder-cimicode 基础镜像（cimicode = 换皮 opencode，bun 编译单二进
# 制），cimicode-pod 模式的运行时 pod 单容器承载对话与执行（无独立 sandbox
# pod，也无 pod 内 helper）：
#   1. cimicode serve（:4096，bridge 的 turn 入口，REST 契约见
#      worker-bridge/contract/adapter-contract.md）
#   2. 全套 worker skills + taskflow / agentteams-sync / mc / jq / git
#   3. LLM：operator 供给期从 Worker CR spec.model + bridge pod env
#      （AGENTTEAMS_AI_GATEWAY_URL / AGENTTEAMS_WORKER_GATEWAY_KEY）渲染
#      OPENCODE_CONFIG_CONTENT 注入——镜像不烘焙任何模型与凭据
#
# agent.md 通道：消息体 system 字段（cimicode 原生）——bridge 每 turn 重拼
# 的 agent.md 随 POST /session/{id}/message 下发，当前 turn 即生效（历史消
# 息转换不含该字段，无重复注入）。旧链路（pod 内 helper POST /agents-md 落
# cwd AGENTS.md）已整体退役，本镜像只开 :4096 一个端口。
#
# 姊妹目录 worker-bridge/opencode-runtime/ 是**同契约**的外网 opencode（npm）
# 形态——两 runtime 差异全部封装在各自镜像目录内（基础镜像/配置路径/命令
# 名），bridge 与 operator 对两者完全一致：
#   - bash.ts 转发工具与 pod 内 helper 退役：单容器形态下内置 bash 本就在
#     本容器、同 cwd 执行；custom tool 需要运行时往配置目录 npm install
#     @opencode-ai/plugin（内网不可达），一并去掉
#   - 权限走 ENV OPENCODE_PERMISSION={"*":"allow"}：cimicode 在全局配置加载
#     后 merge 此 env，免疫首启默认权限 patcher（headless 无人应答权限弹窗，
#     ask = 无超时死等——未匹配权限的默认 action 也是 ask）
#   - 路径以 cimicode 为准：全局配置 $HOME/.cimi/cimicode/cimicode.json，
#     skills 经种子配置 skills.paths 直读 /opt/agentteams/skills（零拷贝，
#     不往工作目录发布任何东西）
#
# 构建上下文 = 仓库根（见 Makefile build-cimicode-runtime）：
#   docker build -f worker-bridge/cimicode-runtime/Dockerfile \
#     --build-arg CIMICODE_BASE_IMAGE=<internal-registry>/library/coder-cimicode:0.5.0 \
#     -t agentteams/cimicode-runtime:<tag> .
#
# CIMICODE_BASE_IMAGE 无默认值（本仓库不携带内网 registry 地址）：Makefile
# 目标带空值守卫 fail-loud，内网构建命令行显式提供。
#
# 镜像内不含凭据、模型与节点名。
#
# 运行时 env 契约（operator 供给 Deployment 时注入；FS_* 与模型配置走
# Secret）：
#   AGENTTEAMS_FS_ROOT           工作目录（默认 /workspace/work，容器可写层）
#   AGENTTEAMS_WORKER_NAME       worker 名（agents/<w>/ 前缀解析）
#   AGENTTEAMS_FS_ENDPOINT / AGENTTEAMS_FS_BUCKET        MinIO 端点/桶（明文）
#   AGENTTEAMS_FS_ACCESS_KEY / AGENTTEAMS_FS_SECRET_KEY  存储凭据（secretKeyRef）
#   AGENTTEAMS_TEAM              Team CR 名（有才设）
#   AGENTTEAMS_MATRIX_USER_ID    worker 的 Matrix MXID（taskflow --actor）
#   OPENCODE_CONFIG_CONTENT      模型/provider 配置（secretKeyRef，operator 渲染）
#   注意：不得设置 AGENTTEAMS_RUNTIME（mc 同步会误走 k8s 模式，local 三元组
#   才是本 pod 的契约——见 worker-bridge/cimicode-sandbox 同款说明）
#   配置文件回退：工具链同时认 /opt/teams/config/runtime.json（平铺 JSON，
#   键=env 名，仅 AGENTTEAMS_ 前缀生效；env 恒优先——interface-contract.md
#   §1.1）。pod 模式 env 注入齐全，不写该文件（缺席是正常态）；通道留给
#   env 缺键的场合补缺。
# =============================================================================
ARG CIMICODE_BASE_IMAGE
FROM ${CIMICODE_BASE_IMAGE}

# base 镜像缺的协作/运行依赖（其 APT 源已指向内网镜像源）：
#   git / jq / procps / curl  协作工具链与排障依赖
#   tini                      进程收尸与信号转发
#   python3（taskflow 等 CLI）、ripgrep（skill 工具 shell 出 rg）、Node.js、
#   busybox  base 已带，不再重装。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps tini \
    && rm -rf /var/lib/apt/lists/*

# mc（MinIO 客户端，taskflow / agentteams-sync 的同步通道）：vendored 二进制
# 直供，不走构建期下载——dl.min.io 的 latest 式路径已下线（410），且真实
# 内网构建无外网。版本 RELEASE.2025-08-13T08-35-41Z（linux-amd64）。
# 与 opencode-runtime 共享 worker-bridge/bin/（两镜像构建上下文均为仓库根）。
COPY worker-bridge/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# ── 1. 种子配置 + entrypoint ─────────────────────────────────────────────
COPY worker-bridge/cimicode-runtime/cimicode.json /opt/agentteams/cimicode.json
COPY worker-bridge/cimicode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
# sed 剥 entrypoint 的 CR：CRLF shebang（#!/bin/sh\r）会让 exec 误报
# no such file or directory——Windows 编辑过的文件不得打断启动。
RUN sed -i "s|\r$||" /opt/agentteams/entrypoint.sh \
    && chmod +x /opt/agentteams/entrypoint.sh

# ── 2. skills 全套 + 协作协议 CLI（全局命令包装到 PATH）──────────────────
COPY worker-bridge/template/worker-bridge-agent/skills /opt/agentteams/skills
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

# ── 3. 约定 ──────────────────────────────────────────────────────────────
# OPENCODE_PERMISSION：headless 全放行。未匹配权限的默认 action 是 ask
# （permission/evaluate.ts），ask 在无客户端应答时是无超时死等；bridge 是
# 唯一客户端且不应答权限弹窗。此 env 在全局配置加载后 merge（含首启默认
# 权限 patcher 之后），是权限面的唯一权威——种子配置里不写 permission。
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    OPENCODE_PERMISSION={"*":"allow"}
EXPOSE 4096
# WORKDIR 仅为镜像级兜底：entrypoint 会按 AGENTTEAMS_FS_ROOT（operator 注入
# /workspace/work）mkdir + cd，实际 cwd 恒跟随 FS_ROOT——两者焊成单旋钮。
WORKDIR /workspace/work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
