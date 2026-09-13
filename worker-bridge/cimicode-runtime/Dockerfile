# =============================================================================
# cimicode-runtime — cimicode pod 合并镜像（opencode + 任务协作工具 + skills）
# =============================================================================
# 内部 cimicode 是换皮的 opencode：cimicode-pod 模式的运行时 pod 单容器承载
# 对话与执行（无独立 sandbox pod）：
#   1. opencode serve（:4096，bridge 的 turn 入口，REST 契约见
#      worker-bridge/contract/adapter-contract.md）
#   2. sandbox helper（:4097，/agents-md 落 AGENTS.md + /exec 承接被
#      tools/bash.ts 转发的命令执行）
#   3. 全套 worker skills + taskflow / agentteams-sync / mc / jq / git
#   4. LLM：烘焙智谱 Coding Plan provider（glm-5.3-flash，真实回答）
#
# 构建上下文 = 仓库根（见 Makefile build-cimicode-runtime）：
#   docker build -f worker-bridge/cimicode-runtime/Dockerfile \
#     --build-arg OPENCODE_VERSION=1.18.27 \
#     --build-arg ZHIPU_API_KEY=<key> \
#     -t agentteams/cimicode-runtime:<tag> .
#
# 镜像内不含凭据与节点名；apiKey 经 ZHIPU_API_KEY build-arg 烘焙（key 只
# 出现在构建命令行，仓库内是 __ZHIPU_API_KEY__ 占位符）。
#
# 运行时 env 契约（operator 供给 Deployment 时注入；FS_* 走 Secret）：
#   AGENTTEAMS_FS_ROOT           工作目录（默认 /workspace，emptyDir 挂载点）
#   AGENTTEAMS_WORKER_NAME       worker 名（agents/<w>/ 前缀解析）
#   AGENTTEAMS_FS_ENDPOINT / AGENTTEAMS_FS_BUCKET        MinIO 端点/桶（明文）
#   AGENTTEAMS_FS_ACCESS_KEY / AGENTTEAMS_FS_SECRET_KEY  存储凭据（secretKeyRef）
#   AGENTTEAMS_TEAM              Team CR 名（有才设）
#   AGENTTEAMS_MATRIX_USER_ID    worker 的 Matrix MXID（taskflow --actor）
#   注意：不得设置 AGENTTEAMS_RUNTIME（mc 同步会误走 k8s 模式，local 三元组
#   才是本 pod 的契约——见 worker-bridge/cimicode-sandbox 同款说明）
# =============================================================================
FROM node:22-slim

ARG OPENCODE_VERSION=1.18.27
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG ZHIPU_API_KEY

RUN apt-get update \
    # ripgrep: opencode 的 skill 工具 shell 出 rg 扫描 skill 内容——缺则每次
    # 调用挂 130s 后报 "ripgrep execution failed"（node-slim 不带 rg）；
    # python3 承载 helper（纯 stdlib，无需 pip）；其余为协作工具运行依赖。
    && apt-get install -y --no-install-recommends \
        ca-certificates tini ripgrep python3 curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/mc \
        https://dl.min.io/client/mc/release/linux-amd64/mc \
    && chmod +x /usr/local/bin/mc \
    && npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# ── 1. 运行时组件（helper + opencode 配置 + bash 工具 + entrypoint）──────
COPY worker-bridge/cimicode-runtime/sandbox_helper.py /opt/agentteams/sandbox_helper.py
COPY worker-bridge/cimicode-runtime/opencode.json /opt/agentteams/opencode.json
COPY worker-bridge/cimicode-runtime/tools/bash.ts /opt/agentteams/tools/bash.ts
# 烘焙 apiKey：build-arg 必填，缺则构建失败；占位符必须被完全替换。
RUN if [ -z "${ZHIPU_API_KEY}" ]; then echo "ZHIPU_API_KEY build-arg is required" >&2; exit 1; fi \
    && sed -i "s|__ZHIPU_API_KEY__|${ZHIPU_API_KEY}|" /opt/agentteams/opencode.json \
    && ! grep -q "__ZHIPU_API_KEY__" /opt/agentteams/opencode.json

COPY worker-bridge/cimicode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
# sed 剥 entrypoint 的 CR：CRLF shebang（#!/bin/sh\r）会让 exec 误报
# no such file or directory——Windows 编辑过的文件不得打断启动。
RUN sed -i "s|\r$||" /opt/agentteams/entrypoint.sh \
    && chmod +x /opt/agentteams/entrypoint.sh /opt/agentteams/sandbox_helper.py

# ── 2. skills 全套 + 协作协议 CLI（全局命令包装到 PATH）──────────────────
COPY worker-bridge/template/worker-bridge-agent/skills /opt/agentteams/skills
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

# ── 3. 约定 ──────────────────────────────────────────────────────────────
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    BRIDGE_SANDBOX_HELPER_PORT=4097
EXPOSE 4096 4097
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
