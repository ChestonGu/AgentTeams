# =============================================================================
# cimicode-sandbox — 预置协作工具与 skills 的执行环境镜像
# =============================================================================
# 用途：无状态 cimicode 运行时自建沙箱的基础镜像。cimicode 使用过程中自行
# 创建沙箱（team 侧不做任何管控），沙箱内必须预置：
#   1. 全套 worker skills（agent 可读的 SKILL.md 文档 + scripts CLI）
#   2. taskflow / agentteams-sync 全局命令（协作协议的命令行入口）
#   3. 运行依赖：python3 / mc(MinIO 客户端) / jq / git / procps
#
# 不包含（对比 opencode 测试沙箱的裁剪项）：
#   - sandbox_helper（:4097 /exec /agents-md——那是给 opencode/bridge 远程
#     调用用的；cimicode 自己管理沙箱，不需要 HTTP 入口）
#   - opencode / bash.ts / ripgrep（opencode 专属）
#   - 任何 entrypoint 守护进程（启动命令由 cimicode 决定）
#
# skill 生效的本质三要素（与 opencode 原生 skill 工具等价）：
#   ① SKILL.md 文件在沙箱内可读（agent 用 read/cat 读取）
#   ② scripts CLI 在 PATH 上可直接执行
#   ③ agent.md/AGENTS.md 协议文本引导"用工具前先读对应 skill"
#
# 构建上下文 = 仓库根：
#   docker build -f opencode/cimicode-sandbox/Dockerfile -t <registry>/cimicode-sandbox:<tag> .
#
# 运行时 env 契约（cimicode 建沙箱时注入，镜像内不含任何凭据）：
#   AGENTTEAMS_FS_ENDPOINT      MinIO/OSS 端点（如 http://minio.ns.svc:9000）
#   AGENTTEAMS_FS_BUCKET        协作存储桶（agentteams-storage）
#   AGENTTEAMS_FS_ACCESS_KEY / AGENTTEAMS_FS_SECRET_KEY   该 worker 的存储凭据
#   AGENTTEAMS_TEAM             Team CR 名（团队共享根解析；无 agt CLI 时必需）
#   AGENTTEAMS_MATRIX_USER_ID   worker 的 Matrix MXID（taskflow --actor/所有权）
#   AGENTTEAMS_WORKER_NAME      worker 名（agents/<w>/ 前缀解析）
#   （可选）AGENTTEAMS_STORAGE_PREFIX   alias/bucket 前缀（默认 agentteams/agentteams-storage）
# =============================================================================
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /usr/local/bin/mc \
        https://dl.min.io/client/mc/release/linux-amd64/mc \
    && chmod +x /usr/local/bin/mc

# ── 1. skills 全套（worker 5 件）─────────────────────────────────────────
# communication / file-sharing / mcporter / organization / task-management
# 若该沙箱还要承载 leader 角色，追加：
#   opencode/template/opencode-leader-agent/skills 下的
#   project-management / team-coordination（leader 专属 2 件）
COPY opencode/template/opencode-worker-agent/skills /opt/agentteams/skills

# ── 2. 协作协议 CLI（全局命令包装到 PATH）───────────────────────────────
# taskflow        任务状态机：ack / submit / check（teams/<t>/shared/tasks/）
# agentteams-sync 共享文件拉推（mc 后端；push 有协议文件排除护栏）
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

# ── 3. 约定 ──────────────────────────────────────────────────────────────
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills
# 协作工作区约定路径（cimicode 挂载任务工作区到这里； Skills 与任务文件同树可读）
WORKDIR /workspace

# 无 ENTRYPOINT：cimicode 自行决定沙箱启动命令（通常 sleep infinity / 长驻 shell）
