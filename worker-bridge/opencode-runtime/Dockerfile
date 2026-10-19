# =============================================================================
# opencode-runtime — opencode pod 合并镜像（opencode + 任务协作工具 + skills）
# =============================================================================
# 外网 opencode（npm 包 opencode-ai，钉 1.18.27）形态的运行时 pod 单容器镜像，
# 与 worker-bridge/cimicode-runtime/（内部 coder-cimicode 基础镜像）是同契约
# 姊妹目录。对外行为完全一致：单端口 :4096 REST、agent.md 走消息体 system
# 字段、权限 OPENCODE_PERMISSION、skills.paths 直读、模型经
# OPENCODE_CONFIG_CONTENT 由 operator 供给期注入、/workspace/work = 容器可写层无卷。
# 两 runtime 差异全部封装在各自镜像目录内（基础镜像来源/配置路径/命令名），
# bridge 与 operator 对两者零差异——内外网切换只换运行时镜像（CIMICODE_IMAGE
# 指向谁就供谁）。契约见 worker-bridge/contract/adapter-contract.md。
#
# 与 cimicode-runtime 的具体差异（全部收敛在本目录内）：
#   - 基础镜像 node:22-slim + npm install -g opencode-ai@${OPENCODE_VERSION}。
#     钉 1.18.27：消息体 system 字段（session/prompt.ts PromptInput.system）、
#     OPENCODE_CONFIG_CONTENT、OPENCODE_PERMISSION、skills.paths 四通道均按
#     该版本源码核实（cdn.jsdelivr.net/gh/sst/opencode@v1.18.27）
#   - node-slim 缺 ripgrep 与 python3，apt 必须带（cimicode 基础镜像已含，
#     其 Dockerfile 不装）
#   - 配置路径是 opencode 惯例：~/.config/opencode/opencode.json（cimicode 是
#     ~/.cimi/cimicode/cimicode.json）；命令 opencode serve（cimicode serve）
#   - OPENCODE_PERMISSION 在 1.18.27 权限生态较弱（bash 工具无权限声明），
#     此 env 加上无害——统一契约保留，cimicode 侧是必需（ask 死锁防护）
#
# 构建上下文 = 仓库根（见 Makefile build-opencode-runtime）：
#   docker build -f worker-bridge/opencode-runtime/Dockerfile \
#     --build-arg OPENCODE_VERSION=1.18.27 \
#     --build-arg NPM_REGISTRY=<mirror> \
#     -t agentteams/opencode-runtime:<tag> .
#
# 多 stage（docker 不带 --target 时构建最后 stage，worker 故意放末位——
# 默认构建产出与单 stage 时代字节一致；stage 划分同
# cimicode-sandbox/Dockerfile）：
#   --target leader   leader 变体：skills 整树替换为 leader 版 + projectflow
#                     包装器替代 taskflow（105 pod 形态 leader 沙箱经
#                     operator runtimeParameter.sandboxImage 选此变体镜像
#                     agentteams/opencode-runtime-leader，见 Makefile
#                     build-opencode-runtime-leader）
#   --target worker / 默认   worker 变体（现状不变）
#
# 镜像内不含凭据、模型与节点名。运行时 env 契约与 cimicode-runtime 完全一致
# （见其 Dockerfile 头注释与 adapter-contract.md）：
#   AGENTTEAMS_FS_ROOT / _WORKER_NAME / _FS_ENDPOINT / _FS_BUCKET / _TEAM /
#   _MATRIX_USER_ID（明文）+ _FS_ACCESS_KEY / _FS_SECRET_KEY（secretKeyRef）
#   + OPENCODE_CONFIG_CONTENT（secretKeyRef，operator 渲染注入）
#   不得设置 AGENTTEAMS_RUNTIME（mc 同步会误走 k8s 模式，local 三元组才是本
#   pod 的契约）。
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# leader stage —— leader 变体，与 worker stage 两处功能不同（同
# cimicode-sandbox 的 leader stage 划法）：
#   ① skills COPY 源 = leader 树（整树替换 worker 版）
#   ② CLI 包装 RUN：创建 projectflow 包装器而非 taskflow（leader 树载
#     projectflow，leader 文档从不调用 taskflow），agentteams-sync 同 worker
# （RUN 注释里的 CLI 名随所在 stage 写 projectflow / taskflow，其余逐行同）
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim AS leader

ARG OPENCODE_VERSION=1.18.27
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG APT_MIRROR=

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    # ripgrep: opencode 的 skill 工具 shell 出 rg 扫描 skill 内容——缺则每次
    # 调用挂 130s 后报 "ripgrep execution failed"（node-slim 不带 rg）；
    # python3 承载 projectflow 等 CLI（纯 stdlib，无需 pip）；其余为协作工具
    # 运行依赖。
    && apt-get install -y --no-install-recommends \
        ca-certificates tini ripgrep python3 curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# mc（MinIO 客户端，projectflow / agentteams-sync 的同步通道）：vendored 二进制
# 直供，不走构建期下载——dl.min.io 的 latest 式路径已下线（410）。版本
# RELEASE.2025-08-13T08-35-41Z（linux-amd64）。与 cimicode-runtime 共享
# worker-bridge/bin/（两镜像构建上下文均为仓库根）。
COPY worker-bridge/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# ── 1. 种子配置 + entrypoint ─────────────────────────────────────────────
COPY worker-bridge/opencode-runtime/opencode.json /opt/agentteams/opencode.json
COPY worker-bridge/opencode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
# sed 剥 entrypoint 的 CR：CRLF shebang（#!/bin/sh\r）会让 exec 误报
# no such file or directory——Windows 编辑过的文件不得打断启动。
RUN sed -i "s|\r$||" /opt/agentteams/entrypoint.sh \
    && chmod +x /opt/agentteams/entrypoint.sh

# ── 2. skills 全套（leader 3 件）+ 协作协议 CLI（全局命令包装到 PATH）─────
# communication / task-management / file-sharing（task-management scripts 载
# projectflow(_core) 委派状态机 / notify / mc_sync / agentteams_log；
# file-sharing scripts 与 worker 树逐字节相同）
# 底座 skills 树只含共享 skills + task-management/scripts（SKILL.md 按出站
# 方式拆两变体，单一来源 scripts 不随变体复制）；变体 SKILL.md 入镜像
# /opt/agentteams/skill-variants/{reply,outbound}/，entrypoint 启动时按
# BRIDGE_SEND_URL 有无选一落位正式路径（同一镜像双形态，与 delegate-commit
# 的运行期分岔同源——env 由 operator 从 Worker CR 同一字段注入）
COPY worker-bridge/template/worker-bridge-leader-agent/skills /opt/agentteams/skills
COPY worker-bridge/template/worker-bridge-leader-reply-agent/skills/task-management/SKILL.md /opt/agentteams/skill-variants/reply/task-management/SKILL.md
COPY worker-bridge/template/worker-bridge-leader-outbound-agent/skills/task-management/SKILL.md /opt/agentteams/skill-variants/outbound/task-management/SKILL.md
# projectflow     任务/项目状态机：create / plan-dag / plan-loop / delegate /
#                 delegate-commit（含自动通知）/ check（shared/projects|tasks/）
# agentteams-sync 共享文件拉推（mc 后端；push 有协议文件排除护栏）
# （不创建 taskflow 包装器：worker 形态的 taskflow.py 不在 leader 树中。
#   AGENTS.md 指令通篇直呼裸 projectflow 命令，必须 PATH 可达——终审 F1
#   裁定；SKILL.md 的 python3 绝对路径回退是补充而非主通道）
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/projectflow.py "$@"\n' > /usr/local/bin/projectflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/projectflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

# ── 3. 约定 ──────────────────────────────────────────────────────────────
# OPENCODE_PERMISSION：与 cimicode-runtime 同一契约（headless 全放行；cimicode
# 侧是 ask 死锁防护的必需项，1.18.27 加上无害）——种子配置里不写 permission。
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    OPENCODE_PERMISSION='{"*":"allow"}'
EXPOSE 4096
# WORKDIR 仅为镜像级兜底：entrypoint 会按 AGENTTEAMS_FS_ROOT（operator 注入
# /workspace/work）mkdir + cd，实际 cwd 恒跟随 FS_ROOT——两者焊成单旋钮。
WORKDIR /workspace/work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]

# ─────────────────────────────────────────────────────────────────────────────
# worker stage —— 默认 target（末位 stage：不带 --target 的构建产出与改前
# 字节一致，指令序与单 stage 时代相同，仅 FROM 增加 stage 名）
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim AS worker

ARG OPENCODE_VERSION=1.18.27
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG APT_MIRROR=

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    # ripgrep: opencode 的 skill 工具 shell 出 rg 扫描 skill 内容——缺则每次
    # 调用挂 130s 后报 "ripgrep execution failed"（node-slim 不带 rg）；
    # python3 承载 taskflow 等 CLI（纯 stdlib，无需 pip）；其余为协作工具
    # 运行依赖。
    && apt-get install -y --no-install-recommends \
        ca-certificates tini ripgrep python3 curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# mc（MinIO 客户端，taskflow / agentteams-sync 的同步通道）：vendored 二进制
# 直供，不走构建期下载——dl.min.io 的 latest 式路径已下线（410）。版本
# RELEASE.2025-08-13T08-35-41Z（linux-amd64）。与 cimicode-runtime 共享
# worker-bridge/bin/（两镜像构建上下文均为仓库根）。
COPY worker-bridge/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# ── 1. 种子配置 + entrypoint ─────────────────────────────────────────────
COPY worker-bridge/opencode-runtime/opencode.json /opt/agentteams/opencode.json
COPY worker-bridge/opencode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
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
# OPENCODE_PERMISSION：与 cimicode-runtime 同一契约（headless 全放行；cimicode
# 侧是 ask 死锁防护的必需项，1.18.27 加上无害）——种子配置里不写 permission。
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    OPENCODE_PERMISSION='{"*":"allow"}'
EXPOSE 4096
# WORKDIR 仅为镜像级兜底：entrypoint 会按 AGENTTEAMS_FS_ROOT（operator 注入
# /workspace/work）mkdir + cd，实际 cwd 恒跟随 FS_ROOT——两者焊成单旋钮。
WORKDIR /workspace/work
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
