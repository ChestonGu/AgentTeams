# AgentTeams opencode Worker 接口契约

**版本** v2.4（2026-09-03，§6 生成契约：源模板 + runtime.yaml 渲染，canonical AGENTS.md 退役为非输入） · 对应实现：`opencode-worker-migration` T1-T11 交付物
**上游基线**：copaw dev-v1.2.2（`AgentTeams/copaw/src/copaw_worker/`、`manager/agent/copaw-worker-agent/`）

两方消费本契约：

| 方 | 消费内容 |
|---|---|
| **沙箱镜像/部署侧** | §1 env、§2 镜像布局、§3 同步、§5 命令 |
| **bridge（Matrix 桥接层）** | §1 env 注入、§4 触发消息、§6 agent.md 生成 |

> Leader 侧（copaw）当前不迁移、不改动；本契约主体约束 opencode worker 侧
> 接口，与 copaw 机制相接的部分（任务协议字节、MinIO 布局、消息格式）以
> copaw 实现为权威。**§5.4 为未来 leader 迁移预置的 projectflow CLI**
> （已实现已测试，未部署；模板 `template/opencode-leader-agent/`）。

## §0 架构决策（v2，2026-09-01 与需求方确认）

1. **无独立编排层组件**。原"编排层"职责并入两个已有角色：**bridge**（env
   注入、§4 消息构造与转发、§6 prompt 动态组装）与**沙箱镜像/部署侧**
   （skills/CLI/运行时预装、pod 生命周期）。
2. **沙箱无状态**。沙箱内无 SOUL.md 文件、无 memory/、无 per-agent
   文件树、无 heartbeat。**拉推 = `shared/` 唯一**（任务、产物、协作数据）。
   验收项 V5（跨会话记忆）随之取消。persona（SOUL/PROFILE 内容）改为
   经 §6 生成器**合并进 system prompt**，不作为沙箱状态落盘。
3. **AGENTS.md 动态传**。内容可能含动态信息（团队成员表等），由 bridge
   每次会话调生成工具（§6，v2.4：源模板 + runtime.yaml 渲染）产出 agent.md
   作 system prompt，
   不落沙箱文件。
4. **skills / 脚本 / CLI 预装**。对所有 opencode worker 逐字节相同，构建
   镜像时统一预装（或启动时从共享 `skills/` 前缀拉取一次）。不做 per-agent
   分发，不依赖 MinIO 热更新。
5. **find-skills 已删除**（无外网；且运行时安装与无状态模型冲突）。
6. controller 侧**零代码改动**（见 controller-handover.md v2）。

## §1 环境变量契约（bridge → 沙箱）

| 变量 | 必须 | 缺省 | 消费者 | 说明 |
|---|---|---|---|---|
| `AGENTTEAMS_WORKER_NAME` | ✅ | — | `mc_sync.filesync_from_env` | worker 名；缺失时 mc 同步直接报错 |
| `AGENTTEAMS_MATRIX_USER_ID` | ✅ | — | `taskflow --actor` | 身份护栏；取 localpart 与 `meta.json.assigned_to` 规范化比对 |
| `AGENTTEAMS_FS_ROOT` | ✅ | `/root/agentteams-fs` | `taskflow --root` / `agentteams_sync --root` | workspace 根（含 `shared/`） |
| `AGENTTEAMS_TEAM` | ✅ | 空 = 无团队 → global shared | `mc_sync._get_team_id` | **storage team name**（bridge 从 worker CR `.team` 剥 bucket 前缀后注入）。沙箱无 agt（D8），团队共享区 `teams/{team}/shared/` 的解析只认此变量 |
| `AGENTTEAMS_FS_ENDPOINT` | local 模式 ✅ | — | `filesync_from_env` | MinIO endpoint（`http(s)://` 前缀可选） |
| `AGENTTEAMS_FS_ACCESS_KEY` | local 模式 ✅ | — | 同上 | 静态 AK |
| `AGENTTEAMS_FS_SECRET_KEY` | local 模式 ✅ | — | 同上 | 静态 SK |
| `AGENTTEAMS_FS_BUCKET` | 否 | `agentteams-storage` | 同上 | |
| `AGENTTEAMS_RUNTIME` | 否 | local | `mc_sync` alias 三模式 | `k8s`（凭据走 MC_HOST）/ `aliyun`（STS 刷新）/ 缺省 local（`mc alias set` 一次） |
| `AGENTTEAMS_WORKER_CR_NAME` | 否 | = WORKER_NAME | 同上 | 仅 copaw 兼容（agt 查询 fallback 路径），opencode 沙箱用不到 |
| `AGENTTEAMS_STORAGE_ALIAS` / `AGENTTEAMS_STORAGE_PREFIX` | 否 | `agentteams` | mc 别名 | prefix 取首段作别名 |
| `MC_HOST_agentteams` | k8s/cloud ✅ | — | mc 二进制 | 凭据串（k8s 由 mc-wrapper 维护，cloud 由 agentteams-env.sh 刷新） |
| `AGENTTEAMS_LOG_FILE` | 否 | 见 §5.5 | 全部 CLI（§5.5） | 统一日志文件路径；`none` 显式关闭文件 sink |
| `AGENTTEAMS_LOG_DIR` | 否 | — | 同上 | 目录形式（文件名固定 `agentteams.log`），优先级低于上一条 |
| `AGENTTEAMS_LOG_LEVEL` | 否 | `INFO` | 同上 | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `AGENTTEAMS_LOG_STDERR` | 否 | `1` | 同上 | `0` 关闭 stderr 镜像（文件 sink 不受影响） |

> env 三元组（ENDPOINT/AK/SK）与 MC_HOST 二选一：local 模式注入三元组，
> k8s/cloud 模式注入 MC_HOST。同时存在时三元组优先用于 `mc alias set`。

## §2 沙箱布局契约（v2：镜像预装 + 运行时仅 shared/）

**镜像预装**（部署侧构建时固化，所有 opencode worker 相同）：

```
/usr/local/bin/taskflow            ← PATH wrapper → python3 <skills>/task-management/scripts/taskflow.py
/usr/local/bin/agentteams-sync     ← PATH wrapper → python3 <skills>/file-sharing/scripts/agentteams_sync.py
<skills-root>/                     ← 预装 skill 集（镜像内或启动时从共享 skills/ 前缀拉一次）
├── task-management/{SKILL.md, scripts/{taskflow.py, taskflow_core.py, mc_sync.py, agentteams_log.py}}
├── file-sharing/{SKILL.md, scripts/{agentteams_sync.py, mc_sync.py, agentteams_log.py}}
├── communication/SKILL.md
├── organization/SKILL.md
└── mcporter/SKILL.md
+ mc / python3 / jq / PyYAML（generate_agent_md.py 结构化解析 runtime.yaml）
```

skill 文档已写明无 wrapper 时的 `python3 <全路径>` 兜底，两条路都必须可用。
`agentteams_log.py` 为统一日志模块（§5.5），随各 scripts 目录部署（逐字节
相同的副本；`verify/simulator.py` 启动时校验全部部署副本与 `cli/` 源一致）。

**运行时**（$AGENTTEAMS_FS_ROOT，唯一同步区）：

```
/root/agentteams-fs/
├── shared/                              ← 协作区（MinIO 权威态的本地投影）
│   ├── tasks/{task-id}/{meta.json, spec.md, base/, workspace/, progress/, result.md}
│   └── projects/{project-id}/
└── logs/agentteams.log                  ← §5.5 统一日志（与 shared/ 平级，永不同步）
```

- 无 `agents/<name>/` 目录、无 SOUL.md、无 memory/、无 HEARTBEAT。
- `shared/tasks/{id}/spec.md`、`base/`、`meta.json.status/result.md` 为协议所有：
  worker 推送恒排除 `spec.md`+`base/`（taskflow 内置），`meta.json`/`result.md`
  只能由 taskflow 命令写。

## §3 同步契约（v2：shared/ 唯一）

**启动拉取**：`shared/`（团队 worker 解析到 `teams/{team}/shared/`）。仅此一项。

**运行中**：所有同步发生在命令内部——taskflow ack/submit 自带 pull/push/verify，
agentteams-sync 显式拉推。无后台轮询（copaw 同构）。

**收尾**：无推送。worker 在 `shared/` 写入的内容已由命令实时推送，
沙箱可直接销毁。

## §4 触发消息契约（D6）

与 copaw matrix 通道**逐字同构**（`copaw_worker/matrix_channel.py:154-158`，
OpenClaw 兼容约定），bridge 构造下发消息时使用完全相同的两个 marker：

```
[Chat messages since your last reply - for context]
{sender}: {body} [id:{message_id}]        ← 每行一条，旧→新，可省略 [id:...]
{sender}: {body}

[Current message - respond to this]
{当前消息正文}
```

- history 为空时**只发** `[Current message - respond to this]` 段。
- history 上限 50 条（`DEFAULT_HISTORY_LIMIT`），超出丢最旧。
- worker 每次回复后，该房间 history 缓冲清空。
- worker 上下文规则（prompt [2] 段 + AGENTS.md）：history 仅作上下文，
  只响应 current 段；不设"启动先 check 恢复状态"硬规则。

**回复转发**：worker 当轮最终文本 = 发给团队的消息。bridge 原样转发到消息
来源房间（不加工、不代答）。`NO_REPLY` 表示无意发送——bridge 收到后不转发。

## §5 命令契约（worker 侧 bash 命令）

### 5.1 taskflow（= copaw taskflow 工具 check_task/ack_task/submit_task）

```
taskflow [--root FS_ROOT] [--actor MATRIX_ID] [--sync none|mc] \
         check  <task-id>
         ack    <task-id>
         submit <task-id> --status <STATUS> --summary "<文本>" \
                [--deliverables <path>...] [--notes "<文本>"...]
```

- `--sync mc`（**默认**；copaw taskflow 工具永远同步）内部完成 pull/push/verify；
  `--sync none` 仅供测试。
- ack 流程 = copaw：pull → 身份/房间校验 → `assigned→in_progress` → push（exclude
  `spec.md`/`base/`）；**输出包含 spec 全文**。`in_progress` 重 ack 幂等成功。
- submit 流程 = copaw：**不 pull** → 本地写协议 `result.md` → `in_progress→submitted`
  → push（同 exclude）→ 远端 `mc stat` verify。
- 裸 deliverable 路径自动补 `shared/tasks/{id}/` 前缀。
- `result.md` 协议字节与 copaw 逐字一致（UTF-8、LF）。
- **增强项（相对 copaw）**：命令层 CAS（ack 要求 assigned、submit 要求
  in_progress）；push/verify 失败回滚本地 `meta.json`。
- 退出码：0 成功 / 1 任务协议错误 / 2 用法错误。

### 5.2 agentteams-sync（= copaw filesync 工具四 action）

```
agentteams-sync [--root FS_ROOT] pull  <path> [--dry-run]
                push  <path> [--exclude <glob>]... [--dry-run]
                stat  <path> [--dry-run]
                list  <path> [--dry-run]
```

- stdout 恒为一行 JSON，与 copaw filesync 工具响应同形。
- `{shared|global-shared}/{projects|tasks}/{id}` 三段路径自动补 `/`（目录语义），
  仅 pull/push/list；stat 逐字匹配。
- `global-shared/` 只读（push 拒绝）。

### 5.3 与 copaw 的有意差异（全量清单）

1. CLI 层前置 CAS（copaw 无状态检查）
2. 显式 UTF-8 + LF 写入（Linux 行为不变，Windows 开发机防 GBK/CRLF）
3. push/verify 失败回滚（copaw 无回滚）
4. `AGENTTEAMS_TEAM` env 替代 agt 运行时查询（沙箱无 controller 访问，D8）

### 5.4 projectflow（leader 侧 CLI，预置未部署）

为未来 leader 运行时迁移到 opencode 预置。实现在 `cli/projectflow/`
（三件套 projectflow.py + projectflow_core.py + mc_sync.py，后者与前两处
逐字节一致），vendor 自 copaw task.py **全量**（worker+leader 函数一体，
CLI 层只暴露 leader 动词）。映射关系：

```
projectflow create-project|show-project|pause-project|resume-project|complete-project <pid>
projectflow add-tasks|plan-dag <pid> --tasks-json '<JSON数组|@file|->'
projectflow plan-loop <pid> --goal <g> --stop-condition <c> --iteration-template <t|@file|-> --max-iterations <n> [--tasks-json ...]
projectflow record-iteration <pid> --iteration <n> --decision continue|replan|ask_user|stop_success|stop_blocked --summary <s> [--next-action <a>]
projectflow show-plan <pid> / ready <pid>          # ready 自动按 plan 类型分 dag/loop
projectflow delegate <pid> <tid> --spec <text|@file|-> [--room-id <!room:domain>]
projectflow delegate-commit <pid> <tid> [--event-id <matrix event>]
projectflow check <tid>                             # leader 读回：meta + result.md 全文 + effective_for_acceptance
```

与 worker `taskflow` 的差异（全部有意）：

1. **推送无排除**：leader 是 `spec.md`/`base/`/`plan.md`/任务 `meta.json`
   的协议所有方，push 恒带空 exclude（worker 侧恒排除前两者）。
2. 每条命令 pull → mutate → push；push 失败**快照回滚**本地文件
   （与 §5.3.3 同一增强类，leader 形态）。
3. 委派序列固定：`ready` → `delegate`（写 meta+spec、节点标记 delegated）
   → 通知 worker → `delegate-commit`（记 event_id 防重发，幂等）。
4. `--tasks-json`/`--spec`/`--iteration-template` 统一支持字面量 / `@file` / `-`(stdin)。
5. 角色分离在工具面：worker `taskflow` CLI 无 leader 动词，`projectflow`
   无 worker 动词（ack/submit 专属 worker）。
6. env 同 §1（`AGENTTEAMS_WORKER_NAME` 注入 leader 名，mc 同步契约要求）。

### 5.5 统一日志契约（全部工具 → 一个 JSONL 文件）

**要求**（2026-09-02 与需求方确认）：所有工具的日志统一记录在**同一个
日志文件**里，方便排查和追溯。

覆盖工具：`taskflow`、`agentteams-sync`、`projectflow`（部署后）、
`generate_agent_md`（bridge 侧运行，日志文件由调用方指定）。实现模块
`agentteams_log.py`，随每个 scripts 目录部署。

**文件与路径**（优先级从高到低）：

1. `AGENTTEAMS_LOG_FILE` —— 显式路径；`none` = 显式关闭文件 sink；
2. `AGENTTEAMS_LOG_DIR` —— 目录，文件名固定 `agentteams.log`；
3. 缺省 `$AGENTTEAMS_FS_ROOT/logs/agentteams.log`（FS_ROOT 也缺省时
   `/root/agentteams-fs/logs/agentteams.log`）。

`logs/` 与 `shared/` 平级 → **永远不会被同步/推送**（同步区只有 shared/）。

**格式**：JSONL，每行一个 JSON 对象：

```json
{"ts": "2026-09-02T07:41:03.512Z", "level": "INFO", "tool": "taskflow", "worker": "bob", "run_id": "9f3a1c2d", "logger": "taskflow", "event": "status_change", "task": "t-1", "from": "assigned", "to": "in_progress"}
```

| 字段 | 说明 |
|---|---|
| `ts` / `level` | UTC ISO-8601（毫秒）/ `DEBUG`..`ERROR` |
| `tool` / `worker` | 命令名（`taskflow` 等）/ `AGENTTEAMS_WORKER_NAME`（无则 `-`） |
| `run_id` | 每进程一次（8 hex），同一次命令的所有事件可归并 |
| `event` | `cmd_start` → 业务事件（`pull`/`push`/`verify`/`rollback`/`status_change`/`action`/`error`/`generated`…）→ `cmd_end` |
| `cmd_end` 附加 | `exit`、`duration_ms`、`command`、`task`/`project` |

**行为保证**：

- 每条命令恒写 `cmd_start`（含截断后的 argv）与 `cmd_end`；关键状态变迁
  （`assigned→in_progress`、`in_progress→submitted`、`pending→prepared→assigned`）
  有 `status_change` 事件；push 失败回滚有 `rollback`（WARNING）。
- stderr 镜像默认开（实时可见），`AGENTTEAMS_LOG_STDERR=0` 关闭；镜像
  不算命令输出，stdout 契约不变。
- 长值截断到 200 字符（`...(+N chars)`）；字段名 `from`/`to`。
- **日志永不打断命令**：路径不可写 → stderr 告警一次，降级为 stderr-only；
  模块缺失（bridge 独立部署场景）→ no-op shim。
- 同文件多进程追加（leader + worker 同 pod 场景天然共用一个文件）。

## §6 agent.md 生成契约（bridge 每次会话调用生成工具，v2.4）

v2.4 起 opencode worker **不消费 controller 合成的 canonical AGENTS.md**。
生成工具 `bridge/generate_agent_md.py` 用仓库内**源模板** +
**MemberRuntimeConfig（runtime.yaml）**直接渲染出 agent.md，**整体作为
system prompt 传给 opencode**——"调谐合成 AGENTS.md"这件事（静态骨架 +
动态团队事实的拼装）在本仓库内重做，上游 markdown 文档不再是被解析对象
（v2.1-v2.3 对 canonical AGENTS.md 的段落手术/fence 处理整类取消）。

数据源：

- **runtime.yaml**（MinIO `agents/<name>/runtime/runtime.yaml`，controller
  调谐写入的 MemberRuntimeConfig）：**身份 + 团队事实的唯一来源**——
  member 段（name/matrixUserId/role）、team 段（name、admin、members 名册
  含 leader/coordinator 角色）、storage 段（sharedPrefix）。
- **SOUL.md / PROFILE.md**（MinIO `agents/<name>/` 下存在时）：persona
  逐字合并。
- **源模板**：镜像内 `template/opencode-worker-agent/AGENTS.md`（与生成器
  同仓部署；`--template` 可覆盖）。

### 6.1 生成规则（模板固化骨架 + 两个占位符）

源模板固化全部静态内容与 controller 标记骨架（输出形态与 copaw prompt
一致），仅有 `{{COORDINATION}}` / `{{ENVIRONMENT}}` 两个占位符：

```
<!-- agentteams-builtin-start -->   ← 模板固化（文件开头）
> ⚠️ DO NOT EDIT ...                ← 模板固化
# opencode Worker Agent Workspace   ← 模板固化（前导 + §2-§8 全静态正文）
<!-- agentteams-builtin-end -->     ← 模板固化
<!-- agentteams-team-context-start -->
{{COORDINATION}}                    ← 渲染自 runtime.yaml team 段
<!-- agentteams-team-context-end -->
{{ENVIRONMENT}}                     ← 渲染自 runtime.yaml member/storage 段
## Persona（接缝）+ SOUL/PROFILE    ← persona 文件存在时追加
```

| 渲染槽 | 数据来源 | 规则 |
|---|---|---|
| `{{COORDINATION}}` worker 形态 | team 段 | Coordinator = `members[role=team_leader]`；Team Admin = `team.admin.matrixUserId`（缺省整行省略）；Coordinator Members = `members[role=coordinator]`（排除 admin、去重保序）；Respond 行按 admin/coordinators 有无共四种变体。**文案与 controller `agentconfig/coordination.go` 逐行对齐**（等价信息换结构化源重渲染，消费语义不变） |
| `{{COORDINATION}}` standalone 形态 | member.matrixUserId 域名 | Coordinator = `@manager:{domain}` + 两条规则（无 team 段时；role 缺省按 team 有无回落 worker/standalone） |
| `{{ENVIRONMENT}}` | member/storage 段 | worker 名（member.name 缺省回落 runtimeName）/ Matrix ID / team.name（缺省 standalone）/ storage.sharedPrefix（缺省 agentteams）/ UTC 日期 |
| Persona 追加 | SOUL/PROFILE 文件 | 归一（CRLF→LF、去首尾空行）后逐字追加，顺序 SOUL → PROFILE；前置 `## Persona` 接缝段（显式归因 "define who you are" + "never override your Worker role" 优先级声明）；两者皆缺则接缝不出现；空白文件 no-op |

行为细节（**fail-loud：以下全部硬错误，退出码 1，bridge 拒开该会话，
绝不回退裸 prompt**）：

- runtime.yaml 用 **PyYAML 结构化解析**（`yaml.safe_load`，dict/list 直取；
  镜像预装 pyyaml，无逐行文本匹配——不合法 YAML 当场拒绝）。
- 缺 member 身份（name/runtimeName/matrixUserId）、matrixUserId 无域名、
  worker 形态无 team_leader 成员、`member.role=team_leader`（leader 模板
  未建，拒绝降级渲染 worker 形态）。
- 模板占位符数量不等于恰一个、模板含未知 `{{...}}`、渲染后残留 `{{`/`}}`
  （含占位符 payload 自身被污染的情况）。
- 输出恒 UTF-8/LF（CLI 内部强制，不受宿主 locale 影响）。

### 6.2 调用方式

```
python3 generate_agent_md.py --runtime-config <runtime.yaml> \
    [--soul-file SOUL.md] [--profile-file PROFILE.md] \
    [--template <AGENTS.md>] [--date YYYY-MM-DD] [--output agent.md]
```

`--runtime-config` 必填（指向 bridge 从 MinIO 拉下的本地文件）；
`--output` 缺省 stdout；退出码 0/1/2（与其他 CLI 一致）。
`--soul-file`/`--profile-file` 文件存在才传（与 copaw
`system_prompt_files` 的 if-exists 语义一致）。
golden 单测锁定：渲染输出 == `bridge/tests/fixtures/golden-team.md` /
`golden-standalone.md`（审定快照 = 真件脱敏 fixture 的渲染产物）——模板、
渲染代码、fixture 任一漂移即红。
生成器自身也走 §5.5 统一日志（tool=`generate_agent_md`，`generated`
事件含 worker_name/role/team/storage_prefix/coordinators/admin/
soul_merged/profile_merged）。

### 6.3 数据义务

- runtime.yaml：bridge 从 MinIO `agents/<name>/runtime/runtime.yaml` 拉取。
  **前提：opencode worker 的 Member 必须落在 controller 写 runtime.yaml
  的调谐分支（`runtime=qwenpaw` 或 `deployMode=edge`，见
  member_reconcile.go ReconcileMemberConfig）**——生产 worker 真件已确认
  同型部署；团队变化经 team 调谐 `MergeMemberRuntimeTeamContext` 更新
  team 段，下次会话自动生效。
- persona：bridge 拉取 MinIO `agents/<name>/SOUL.md`，存在再拉
  `PROFILE.md`（存在才传对应 flag）。SOUL 定位是个性/风格，**不含角色
  指令**（角色由模板 + Coordination 定义；Persona 接缝已声明不覆盖
  Worker 角色）。
- ~~canonical AGENTS.md~~：**不再是 opencode worker 的输入**（copaw
  leader/worker 照常消费，互不影响）；`spec.agents` 用户自定义内容不向
  opencode 传播（个性化走 PROFILE.md）。
- 模板：镜像构建时从仓库 `template/opencode-worker-agent/` 取，与生成器
  的相对位置即缺省 `--template` 解析路径。

## §7 职责矩阵（v2：两方）

| 职责 | 沙箱镜像/部署侧 | bridge | controller |
|---|---|---|---|
| 镜像内 mc/python3/jq、skills 预装、PATH wrapper | ✅ | | |
| runtime=opencode 的 pod 镜像选择 | ✅（部署配置） | | |
| §1 env 注入 | | ✅ | |
| §4 消息构造（history 累积 + marker + 转发 + NO_REPLY 吞掉） | | ✅ | |
| §6 agent.md 生成（generate_agent_md.py：源模板 + runtime.yaml 渲染，Coordination/Environment + persona 合并） | 模板随镜像 | ✅（调用方） | |
| `agents/<name>/` 两件套（runtime/runtime.yaml、SOUL.md；canonical AGENTS.md 与 opencode 无关） | | 拉取 | ✅ 调谐写入维护（runtime.yaml 仅 qwenpaw/edge 分支） |
| Coordination 等价信息（原 controller 注入 AGENTS.md 的块） | | 从 runtime.yaml team 段渲染 | ✅ team 段调谐维护 |
| §5.5 统一日志（文件位置由 env/缺省决定；bridge 侧生成器日志文件自定） | ✅（pod 内缺省路径） | ✅（生成器） | |
| §3 启动拉取 shared/ | ✅（沙箱 init） | | |
| 任务协议状态机（meta/result 字节） | taskflow 命令内（T1-T2 实现） | | copaw Leader 为对手方 |

## §8 变更记录

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-09-01 | T8 定稿：七节；新增 `AGENTTEAMS_TEAM`（解 D8） |
| v1.0.1 | 2026-09-01 | T10 冒烟修正：`--sync` 默认 `none`→`mc` |
| v2.0 | 2026-09-01 | 架构 v2 定稿：新增 §0 六项决策（无独立编排层→bridge+镜像；沙箱无状态、shared/ 唯一同步、删 SOUL/memory/heartbeat/V5；AGENTS 动态传；skills 预装；find-skills 删除；controller 零改动）；§2 改镜像布局；§3 收窄为 shared/；§6 改 3 段动态渲染；§7 职责两方化 |
| v2.1 | 2026-09-01 | §6 重写：弃 prompt 模板概念，改为 **bridge 调 `bridge/convert_agent_md.py` 直接产出 agent.md 作 system prompt**（段落级转换：runtime 段替换、其余透传、追加团队表+环境段；golden 测试锁定与模板一致）；新增 §5.4 projectflow leader CLI（预置未部署） |
| v2.2 | 2026-09-02 | §6：`## Coordination`（controller 注入块）显式静默透传（fence 归属修正，替换段不再吞 fence）；新增 `--soul-file`/`--profile-file` persona 逐字合并（copaw 顺序，if-exists）；§6.3 persona 数据义务。新增 **§5.5 统一日志契约**（所有工具 → 一个 JSONL 文件：路径解析、字段、行为保证；§1 补 4 个 `AGENTTEAMS_LOG_*` env；§2 布局补 `agentteams_log.py` 副本与 `logs/` 目录）。模拟器新增部署副本漂移检查 |
| v2.3 | 2026-09-02 | §6 数据源重构（需求方定夺）：**身份/团队信息不再经 CLI 参数**——删 `--worker-name/--matrix-id/--team/--storage-prefix/--members` 及 §9 团队表渲染（团队事实唯一来源 = 透传的 Coordination 块）；新增 `--runtime-config`（MinIO `agents/<name>/runtime/runtime.yaml`，MemberRuntimeConfig，copaw 同款逐行解析）渲染 Environment 段；**标记骨架保持**（copaw 整文件进 prompt，转换输出保留 builtin-start/DO NOT EDIT/builtin-end/team-context 完整围栏——修复了前导标记丢失与段尾空行吞 fence 两个 bug）；fixture 换成真实生产形态（desensitized）；§6.3 数据义务改为三件套全 MinIO 拉取，bridge 不再向 controller 查询名册 |
| v2.4 | 2026-09-03 | §6 重写为**生成契约**（需求方定夺）：弃"转换 canonical AGENTS.md"，改 `bridge/generate_agent_md.py` = **源模板 + runtime.yaml 渲染**——模板占位符化（`{{COORDINATION}}`/`{{ENVIRONMENT}}`，标记骨架固化在模板）；Coordination 块改从 runtime.yaml team 段渲染（文案与 controller `coordination.go` 逐行对齐，等价信息换结构化源）；runtime.yaml 改 **PyYAML 结构化解析**（当场抓出 fixture 粘贴污染）；persona 追加前置 **`## Persona` 接缝段**（归因 + 不覆盖 Worker 角色声明）；**fail-loud 全硬错误**（缺身份/无 leader/leader 角色/占位符漂移/残留括号/坏 YAML 均退出码 1，bridge 拒开会话）；canonical AGENTS.md 退役为非输入（`spec.agents` 用户内容不传播，个性化走 PROFILE.md）；数据义务改两件套（runtime.yaml + persona）+ 镜像内模板，**前提 = Member 落 qwenpaw/edge 调谐分支**（生产同型部署已确认）；golden 改为审定渲染快照双份（team/standalone） |
