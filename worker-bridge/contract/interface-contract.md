# AgentTeams opencode Worker 接口契约

> **统一命名注记（2026-09-13）**：runtime 统一为 worker-bridge（原 opencode），旧 opencode 链路退役；adapter 拆分 cimicode-stateless / cimicode-pod。文中 opencode 表述按此映射阅读。
**版本** v2.8（2026-10-16，见 §8 版本沿革） · 对应实现：`opencode-worker-migration` T1-T11 + `leader-worker-bridge` P0 交付物
**上游基线**：copaw dev-v1.2.2（`AgentTeams/copaw/src/copaw_worker/`、`manager/agent/copaw-worker-agent/`）

两方消费本契约：

| 方 | 消费内容 |
|---|---|
| **沙箱镜像/部署侧** | §1 env、§2 镜像布局、§3 同步、§5 命令 |
| **bridge（Matrix 桥接层）** | §1 env 注入、§4 触发消息、§4.1 出站端点、§6 agent.md 生成 |

> Leader 侧（copaw）可继续沿用、不因本契约改动；本契约主体约束
> worker-bridge worker 侧接口，与 copaw 机制相接的部分（任务协议字节、
> MinIO 布局、消息格式）以 copaw 实现为权威。**v2.7 起 leader 形态已
> 接线**：生成器接受 `member.role=team_leader` 并按 role 自动分派 leader
> 模板（§6），`projectflow` CLI 随 leader 沙箱镜像部署、delegate-commit
> 默认自动通知（§5.4，经 §4.1 出站端点）。

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
| `AGENTTEAMS_FS_ROOT` | ✅ | `/root/agentteams-fs` | `taskflow --root` / `agentteams_sync --root` | workspace 根（含 `shared/`）。**沙箱约定值 `/workspace/work`**（pod 模式 operator 注入；entrypoint 按它 mkdir+cd，cwd 与 shared/ 同根） |
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
| `BRIDGE_SEND_URL` | leader 沙箱 ✅ | — | `projectflow delegate-commit`（notify.py） | bridge send 端点（§4.1）基址，如 `http://agentteams-worker-<w>-bridge-svc:8081`（`<w>`=leader worker 名，暴露形态由部署侧定）。**仅 leader 沙箱需要**（delegate-commit 默认自动通知）；worker 恒不需要。缺失（未设置/空白）时 delegate-commit 走回复送达（记录指派、exit 0、打印引导，见 §5.4）；不可达时仍报错退出、任务保持 prepared。**§1.1 runtime.json 回退不覆盖此键**（无 `AGENTTEAMS_` 前缀）——stateless leader 由平台侧以真实 env 注入 |

> env 三元组（ENDPOINT/AK/SK）与 MC_HOST 二选一：local 模式注入三元组，
> k8s/cloud 模式注入 MC_HOST。同时存在时三元组优先用于 `mc alias set`。

### 1.1 配置文件回退（stateless 沙箱，v2.5）

上表 env 键中 **`AGENTTEAMS_` 前缀者**可通过配置文件补齐：`/opt/teams/config/runtime.json`（即下文的前缀过滤）；`BRIDGE_SEND_URL`（§4.1）无此前缀，不走文件通道——pod 形态由 operator 从 `spec.runtimeParameter.bridgeSendUrl` 注入（见 §4.1）。

- **格式**：平铺一层 JSON 对象，键 = 环境变量名原样（`{"AGENTTEAMS_WORKER_NAME": "dev-01", ...}`）；仅 `AGENTTEAMS_` 前缀的字符串值生效，其余条目跳过并 stderr 警告。
- **优先级**：显式 env 恒优先（`setdefault` 语义）——文件只补缺失键。两通道可并存：pod 模式（env 注入）不受影响，stateless 靠文件。
- **谁写**：stateless 沙箱由平台侧服务注入（值拼装同 operator 组 runtime env 的三源：bridge pod env 五键 + Worker CR `status.matrixUserID` + Team CR 反查 `AGENTTEAMS_TEAM`）；pod 模式不写该文件，**文件缺席是正常态，静默**。
- **装载时点**：工具链 CLI 进程 import `mc_sync` 时执行（`mc_sync.load_runtime_config_file()`，早于 argparse default 求值与 `_MC_ALIAS` 模块级读取）。文件存在但坏（非 JSON/非对象）= stderr 警告后跳过，退化为普通缺 env 报错，不崩 CLI。
- **生效粒度**：每次命令执行都是新进程——平台追加/更新文件后，下一条命令即读到新值，无需重启沙箱。

## §2 沙箱布局契约（v2：镜像预装 + 运行时仅 shared/）

**worker 沙箱镜像预装**（部署侧构建时固化，所有 opencode worker 相同）：

```
/usr/local/bin/taskflow            ← PATH wrapper → python3 <skills>/task-management/scripts/taskflow.py
/usr/local/bin/agentteams-sync     ← PATH wrapper → python3 <skills>/file-sharing/scripts/agentteams_sync.py
<skills-root>/                     ← 预装 skill 集（镜像内或启动时从共享 skills/ 前缀拉一次）
├── task-management/{SKILL.md, scripts/{taskflow.py, taskflow_core.py, mc_sync.py, agentteams_log.py}}
├── file-sharing/{SKILL.md, scripts/{agentteams_sync.py, mc_sync.py, agentteams_log.py}}
├── communication/SKILL.md
├── organization/SKILL.md
└── mcporter/SKILL.md
+ mc / python3 / jq
```

**bridge 侧预装**（bridge daemon 容器；生成器在 bridge 进程运行，§5.5，
**不进 worker 沙箱镜像**）：

```
bridge/generate_agent_md.py        ← agent.md 生成工具（§6）
bridge/agentteams_log.py           ← 与 skills 副本逐字节同源（§5.5）
template/worker-bridge-agent/AGENTS.md ← worker/standalone 源模板（§6）
template/worker-bridge-leader-agent/AGENTS.md ← leader 源模板（§6，v2.7）
                                     ↑ 两模板均随 bridge 镜像打包；生成器默认
                                     按 member.role 自动分派，路径是相对自身的
                                     ../template/<对应模板>/AGENTS.md——两处须
                                     保持仓库相对布局成对部署，布局不同则显式
                                     传 --template
+ python3 / PyYAML（safe_load 结构化解析 runtime.yaml）
```

skill 文档已写明无 wrapper 时的 `python3 <全路径>` 兜底，两条路都必须可用。
`agentteams_log.py` 为统一日志模块（§5.5），随各 scripts 目录 + bridge/
部署（逐字节相同的镜像内多份副本；`verify/simulator.py` 启动时校验全部部署
副本与 `cli/` 源一致）。

**运行时**（$AGENTTEAMS_FS_ROOT，唯一同步区）：

```
/workspace/work/                          ← $AGENTTEAMS_FS_ROOT（沙箱约定值；示例）
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

**路由纪律（v2.7）**：会话回复只送达触发本轮 turn 的房间——bridge 不提供
"回复到其他房间"的会话通道；跨房发送（典型：leader 在 Leader DM 被触发，
委派通知必须进 Team Room）必须经工具走 §4.1 出站端点——每次发送是独立
Matrix 事件、txn 可幂等、可追溯。

### 4.1 出站端点契约（bridge send，v2.7）

沙箱侧工具往任意房间**主动发送**消息的唯一通道（会话回复之外的出站
路径；当前消费者 = leader `projectflow delegate-commit` 自动通知，§5.4）。
bridge 的 HTTP 服务（uvicorn，缺省 `0.0.0.0:8081`，与探针同一进程）承载
本端点；**端点无鉴权，以网络隔离为前提**——只应在集群命名空间内对沙箱
可达，部署侧必须保证该端口不向集群外暴露。

```
POST /api/v1/bridge/send            ← Content-Type: application/json
{"room_id": "<!room:domain>",       ← 必填，目标房间全 ID
 "text":    "<消息正文>",            ← 必填；markdown 渲染 + m.mentions 自动解析
 "txn_id":  "<事务 ID>"}            ← 可选；事务 ID（幂等语义见下）

→ {"ok": true,  "event_id": "$..."}   ← 发送成功（event_id 可空：网关未回执时 ok 仍 true 而 event_id 缺席）
→ {"ok": false, "error": "<原因>"}    ← 参数拒绝 / 网关未就绪 / 发送失败
```

- **拒绝项**：`room_id`/`text` 缺失或空白（显式 `null` 按缺失处理，不落
  字面量 `"None"`）；`text` 去空白后恰为 `NO_REPLY`（"无意发送"的语义
  不允许经工具发往任何房间）；Matrix 网关未就绪；发送异常。
- **`txn_id` 幂等**：可选；`null`/空串 = homeserver 缺省事务 ID。非空时
  透传 homeserver 作事务 ID——同 ID 重放由 homeserver 去重，重复请求
  不会重复入房。重试敏感的调用方应固定事务 ID（§5.4 委派通知固定
  `delegate-{task_id}`）。
- 端点基址经 `BRIDGE_SEND_URL` env 注入沙箱（§1）；worker 不消费此端点。
- pod 形态供给链：该 env 由 worker-bridge-operator 从 Worker CR
  `spec.runtimeParameter.bridgeSendUrl` 注入（缺省不注入，与历史一致）；
  该 worker 的沙箱镜像可经 `runtimeParameter.sandboxImage` 按 worker
  覆盖（leader 变体镜像走此通道，缺省 = 全局 `CIMICODE_IMAGE`）。

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

### 5.4 projectflow（leader 侧 CLI，v2.7 起随 leader 沙箱部署）

leader 形态的命令面（v2.7 前为预置未部署）。实现在 `cli/projectflow/`
（projectflow.py + projectflow_core.py + mc_sync.py + notify.py 四件，
mc_sync 与其他 scripts 副本逐字节一致），vendor 自 copaw task.py
**全量**（worker+leader 函数一体，CLI 层只暴露 leader 动词）；随
leader 模板 skills 树进 leader 沙箱镜像（§2）。映射关系：

```
projectflow create-project|show-project|pause-project|resume-project|complete-project <pid>
projectflow add-tasks|plan-dag <pid> --tasks-json '<JSON数组|@file|->'
projectflow plan-loop <pid> --goal <g> --stop-condition <c> --iteration-template <t|@file|-> --max-iterations <n> [--tasks-json ...]
projectflow record-iteration <pid> --iteration <n> --decision continue|replan|ask_user|stop_success|stop_blocked --summary <s> [--next-action <a>]
projectflow show-plan <pid> / ready <pid>          # ready 自动按 plan 类型分 dag/loop
projectflow delegate <pid> <tid> --spec <text|@file|-> [--room-id <!room:domain>]
projectflow delegate-commit <pid> <tid> --assignee-mxid <full-mxid> | --no-notify [--event-id <matrix event>]
projectflow check <tid>                             # leader 读回：meta + result.md 全文 + effective_for_acceptance
```

与 worker `taskflow` 的差异（全部有意）：

1. **推送无排除**：leader 是 `spec.md`/`base/`/`plan.md`/任务 `meta.json`
   的协议所有方，push 恒带空 exclude（worker 侧恒排除前两者）。
2. 每条命令 pull → mutate → push；push 失败**快照回滚**本地文件
   （与 §5.3.3 同一增强类，leader 形态）。
3. **委派序列固定**：`ready` → `delegate`（写 meta+spec、节点标记
   delegated；`--room-id` 记入 meta——委派通知的目的地与 worker ack 的
   房间校验共用它，建议恒传）→ `delegate-commit`。
4. `--tasks-json`/`--spec`/`--iteration-template` 统一支持字面量 / `@file` / `-`(stdin)。
5. 角色分离在工具面：worker `taskflow` CLI 无 leader 动词，`projectflow`
   无 worker 动词（ack/submit 专属 worker）。
6. env 同 §1（`AGENTTEAMS_WORKER_NAME` 注入 leader 名，mc 同步契约要求；
   delegate-commit 自动通知另需 `BRIDGE_SEND_URL`）。

**delegate-commit 通知语义（v2.7，默认自动通知；v2.8 补无端点分支）**：

- **自动路径（默认）**：`delegate-commit <pid> <tid> --assignee-mxid <full-mxid>`
  经 `BRIDGE_SEND_URL`（§1）调 bridge send 端点（§4.1），把委派通知发到
  `meta.room_id`（Team Room）——文本 `@{assignee} You are assigned task
  **{task_id}**: {title}` + spec 预览（500 字符截断），固定事务 ID
  `delegate-{task_id}`（homeserver 幂等，重试不重复入房）。**先发后
  commit**（copaw delegate_task 原子序）。`delegate` 收尾提示语直接引导
  跑此命令并声明"勿自发委派消息"——重复通知可能把 worker 触发两次。
- **无端点分支（v2.8，回复送达）**：`BRIDGE_SEND_URL` 缺失（未设置/空白）
  = 部署形态而非故障——跳过 notify（零网络调用），照常 commit（event_id
  空、exit 0、任务转 assigned），stdout 打三行 `[notify]` 引导块：未发送
  任何内容 / 本回合以回复正文送达（task id + 一行目标 + spec 位置）/
  回复正文必须含 assignee 带 `@` 前缀的完整 Matrix ID——bridge 把回复
  正文 `@` token 转结构化 mention 唤醒对方（与 worker TASK_COMPLETED
  @leader 同机制）。与 `--no-notify` 的区别：仍走 `--assignee-mxid` 与
  room_id 前置校验、必输出引导块；leader 模板双模式协议见 leader
  AGENTS.md "Delegation delivery modes"。
- **前置**：`meta.room_id`（delegate 时记入，缺则报错要求带 `--room-id`
  重新 delegate）与 `--assignee-mxid`（协作块 Team Workers 名单里的
  full Matrix ID）。
- **失败保持 prepared**：端点不可达 / 发送被拒——命令退出 1，
  meta 仍 prepared、未 commit；修复后重跑同一命令（固定 txn 幂等短路，
  已入房的通知不会重发）。（env 缺失自 v2.8 走上面的无端点分支，
  不再算失败）
- **幂等短路**：任务已 assigned 且带 event_id 时直接跳过通知、幂等成功。
- **手动通道（`--no-notify [--event-id <id>]`）**：零通知网络调用（文件同步 mc pull/push 照常）——v2.7 前的
  唯一形态，通知由外部自发、拿到 event id 再记账；该路径下 notify
  模块根本不加载。

### 5.5 统一日志契约（全部工具 → 一个 JSONL 文件）

**要求**（2026-09-02 与需求方确认）：所有工具的日志统一记录在**同一个
日志文件**里，方便排查和追溯。

覆盖工具：`taskflow`、`agentteams-sync`、`projectflow`（leader 沙箱）、
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
- **源模板**：bridge 容器内 `template/worker-bridge-agent/AGENTS.md`
  （worker/standalone）或 `template/worker-bridge-leader-agent/AGENTS.md`
  （leader）——**生成器按 member.role 自动分派**（不传 `--template` 时；
  显式传参覆盖），两模板均随 bridge 镜像打包（§2 bridge 段）。

### 6.1 生成规则（模板固化骨架 + 两个占位符）

源模板固化全部静态内容与 controller 标记骨架（输出形态与 copaw prompt
一致），仅有 `{{COORDINATION}}` / `{{ENVIRONMENT}}` 两个占位符：

```
<!-- agentteams-builtin-start -->   ← 模板固化（文件开头）
> ⚠️ DO NOT EDIT ...                ← 模板固化
# opencode Worker Agent Workspace   ← 模板固化（前导 + §2-§8 全静态正文；
                                     　 leader 模板同构，标题/正文为 leader 版）
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
| `{{COORDINATION}}` leader 形态（v2.7） | team 段 | `member.role=team_leader`（须有 team 段）渲染 leader 视角：Team / Team Room（`team.teamRoomId`）/ Leader DM Room（`team.leaderDmRoomId`）/ Team Admin / **Team Workers**（`members[role=worker]` 全体，委派对象=delegate-commit `--assignee-mxid` 取值处）/ Coordinator Members / Team Roster（自排除）+ Team Room 协作面与 requester 报告双锚规则 |
| `{{ENVIRONMENT}}` | member/storage 段 | worker 名（member.name 缺省回落 runtimeName）/ Matrix ID / team.name（缺省 standalone）/ storage.sharedPrefix（缺省 agentteams）/ UTC 日期；leader 形态首行标签 `Leader name:`（worker/standalone 保持 `Worker name:` 字节不变） |
| Persona 追加 | SOUL/PROFILE 文件 | 归一（CRLF→LF、去首尾空行）后逐字追加，顺序 SOUL → PROFILE；前置 `## Persona` 接缝段（显式归因 "define who you are" + "never override your Worker role" 优先级声明）；两者皆缺则接缝不出现；空白文件 no-op |

行为细节（**fail-loud：以下全部硬错误，退出码 1，bridge 拒开该会话，
绝不回退裸 prompt**）：

- runtime.yaml 用 **PyYAML 结构化解析**（`yaml.safe_load`，dict/list 直取；
  镜像预装 pyyaml，无逐行文本匹配——不合法 YAML 当场拒绝）。
- 缺 member 身份（name/runtimeName/matrixUserId）、matrixUserId 无域名、
  worker 形态无 team_leader 成员、`member.role` 不在白名单
  （worker/standalone/team_leader——coordinator 等真实名册角色无模板
  形态，拒绝静默降级）、`team_leader` 无 team 段（leader 只存在于团队内）。
  v2.6 前 `team_leader` 被整类拒绝（leader 模板未建）；v2.7 起放行并自动
  分派 leader 模板，worker/standalone 两形态渲染逐字节不变（golden 锁定）。
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
`--template` 缺省按 member.role 在 worker/leader 两模板间**自动分派**
（§6.1；显式传参覆盖）。
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
- 模板：镜像构建时从仓库 `template/worker-bridge-agent/` 与
  `template/worker-bridge-leader-agent/` 取两份，与生成器的相对位置即
  缺省分派的解析路径（`<gen>/../template/<对应模板>/AGENTS.md`）。

## §7 职责矩阵（v2：两方）

| 职责 | 沙箱镜像/部署侧 | bridge | controller |
|---|---|---|---|
| 镜像内 mc/python3/jq、skills 预装、PATH wrapper | ✅ | | |
| runtime=opencode 的 pod 镜像选择 | ✅（部署配置） | | |
| §1 env 注入 | | ✅ | |
| §4 消息构造（history 累积 + marker + 转发 + NO_REPLY 吞掉）+ §4.1 出站端点 | | ✅ | |
| §6 agent.md 生成（generate_agent_md.py：源模板按 role 分派 worker/leader + runtime.yaml 渲染，Coordination/Environment + persona 合并） | 模板随镜像 | ✅（调用方） | |
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
| v2.5 | 2026-09-22 | 新增 **§1.1 配置文件回退**：`/opt/teams/config/runtime.json`（平铺 JSON，键=env 名，仅 `AGENTTEAMS_` 前缀生效）作为 §1 全部 env 键的 fallback 通道——stateless 沙箱平台无法建时注入 env，改由平台侧服务按 operator 同款三源拼值写文件；env 显式恒优先，pod 模式不受影响（文件缺席=正常态）。装载实现在 `mc_sync.load_runtime_config_file()`，import 时执行（早于 `_MC_ALIAS` 模块级求值），CLI 三件（taskflow/agentteams-sync/projectflow）零入口改动全覆盖 |
| v2.6 | 2026-09-23 | §1 `AGENTTEAMS_FS_ROOT` 行补**沙箱约定值 `/workspace/work`**（pod 模式 operator 注入；runtime entrypoint 按 env `mkdir -p` + `cd`，cwd 与 `shared/` 同根——单旋钮）。工具链缺席缺省 `/root/agentteams-fs` 不变（沙箱内恒被 env/runtime.json 覆盖）；cimicode-runtime / opencode-runtime / cimicode-sandbox 三镜像的 WORKDIR 与 entrypoint 兜底同步迁移；stateless 平台侧改 runtime.json 供值即可，沙箱 cwd 仍归平台管（cimicode-sandbox 无 entrypoint） |
| v2.7 | 2026-10-15 | Leader 形态接线（dev-v1.2.3-leader 首批）：§4 补路由纪律（会话回复只达触发房、跨房必经工具）+ 新增 **§4.1 出站端点** `POST /api/v1/bridge/send`（请求/响应体、`txn_id` 幂等透传、NO_REPLY 拒发、无鉴权以网络隔离为前提）；§1 新增 `BRIDGE_SEND_URL`（仅 leader 沙箱；§1.1 回退不覆盖，stateless 走平台真实 env）；§5.4 projectflow 转正为随 leader 沙箱部署——delegate-commit **默认自动通知**（`--assignee-mxid`、先发后 commit、txn `delegate-{task_id}` 幂等短路、失败保持 prepared、`--no-notify` 手动通道），"notify 由 agent 自发"旧描述删除；§6 生成器接受 `member.role=team_leader` 并按 role 自动分派 worker/leader 两模板（fail-loud 白名单改写；worker/standalone 渲染逐字节不变，golden 锁定），leader Coordination 渲染 Team/Team Room/Leader DM Room/Team Admin/Team Workers/Coordinator Members/Team Roster，Environment leader 标签 `Leader name:`；§2 bridge 预装补 leader 模板（随镜像打包） |
| v2.8 | 2026-10-16 | 无出站形态回复送达（105 无出站对照实验 t-nb 结论：无 `BRIDGE_SEND_URL` 时 delegate-commit 报 NotifyError 卡死，人工教一次"回复正文 @ 完整 ID"后零干预通过）：§1 `BRIDGE_SEND_URL` 缺失语义拆分——缺失（未设置/空白）时 delegate-commit 走回复送达（记录指派、exit 0、打印 `[notify]` 引导），不可达时仍报错退出、任务保持 prepared；§5.4 自动路径补**无端点分支**（跳过 notify 零网络调用、event_id 空、仍要求 `--assignee-mxid`、与 `--no-notify` 区别=必输出引导块），"失败保持 prepared"收窄为端点不可达/发送被拒。已配置端点的自动路径与 `--no-notify` 手动通道**字节级不变**；CLI/notify.py 与 leader 模板两份副本同步（notify 缺失文案改防御性引导） |
