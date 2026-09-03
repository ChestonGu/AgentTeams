# opencode Worker 运行时迁移方案（任务协作 / 产物协作 / 身份认知）

> 2026-08-31 定稿。基于仓库 `d:\workspace\agent-teams-830\AgentTeams`（分支 dev-v1.2.2）。
> 范围：将普通 team worker 的运行时从 copaw 换为 opencode（**leader 保持 copaw 不迁移**），覆盖 **opencode 实例所需的全部内容与机制层**。
> 术语：**沙箱初始化**（stage-in）= 任务开始前把 MinIO workspace 拉进沙箱；**收尾推送**（stage-out）= 任务结束后把变更推回 MinIO。
> 参考：《cimicode迁移技术方案-1.2.2.md》（仅参考，本方案独立成立）。

---

## §0 范围与决策记录

### 0.1 边界（与他人分工）

| 职责 | 归属 |
|---|---|
| Matrix 收发、触发/唤醒 worker | 他人 |
| 沙箱/池基础设施、opencode 运行、模型接入 | 他人 |
| 沙箱初始化/收尾推送的**动作实现** | 他人（拉什么推什么=本方案 §4.3 契约） |
| system prompt **内容模板** | **本方案**（传入机制归编排层） |
| **沙箱内容包**（skills + 协作脚本 + AGENTS.md 模板） | **本方案** |
| **接口契约**（env / 目录 / 协议互通） | **本方案** |
| controller 代码改动 | controller 负责人（本方案交付改动说明，见 T9） |

### 0.2 决策记录

| # | 决策 | 结论 |
|---|---|---|
| D0 | 总体基线 | **一切以 copaw worker 机制为准**：文件协议、状态机、行为规范（AGENTS.md 与 skills 语义）、汇报词法全部照抄 `manager/agent/copaw-worker-agent/`。**唯一改变是工具载体**：copaw 的 tool call（LLM function call）→ opencode 的 bash CLI + skill 指引。leader 是 copaw 且不迁移，协作必须零协议差异 |
| D1 | 协作工具形式 | **taskflow 脚本化 CLI + skill 文档**（方案 B）。不用 MCP / opencode plugin。状态机 vendor 自 `copaw/src/copaw_worker/task.py`（纯 stdlib），护栏校验全在脚本内 |
| D2 | 任务/产物协议 | **原样复用** Teams 文件协议：`shared/tasks/{id}/{meta.json,spec.md,result.md,progress/}`，MinIO 权威。leader（**copaw**，原生 taskflow 工具）零改动，opencode worker 与 copaw worker 产出同构 |
| D3 | 行为规范载体 | AGENTS.md + SOUL.md 中性化转换后随 **system_prompt 传入**（4 段，§3） |
| D4 | filesync 工具 | 落成 `agentteams-sync` 三子命令 `pull / push / status`，**一一对应 copaw `filesync` 工具的 `pull/push/stat` 三 action**（含 push 的 path+exclude 参数语义）；现有脚本仅 pull，补 push/status |
| D5 | projectflow 工具 | **一期不做**——全部操作是 leader/manager 规划职责，普通 worker 不调用；worker 只读项目上下文（project-participation skill 原样）。二期 leader 迁移时同构 CLI 化（1-2 天） |
| D6 | 会话上下文 | **与 copaw 原生一致，不写"先 check 恢复状态"硬规则**。原生机制：copaw worker 长驻会话 + 每条消息自带 history（"history 仅作上下文，只响应当前消息"），无任何恢复性 check；opencode 靠**会话延续**（自管 session）等价实现。加硬规则的害处：① 与原生行为不一致（copaw 无此规则）；② 会话延续下是冗余调用；③ 假恢复——session 真丢失时 check 只能拿回 meta 状态，恢复不了执行上下文。保留 copaw 的合法用法：新任务第一步=ack（响应含 spec，D7）；长间隔/存疑/上下文不足时 `check` 核对（meta.json 是唯一权威），check 本来就是 copaw task-management skill 的"reading task state"用途 |
| D7 | 读写机制 | **与 copaw worker 完全一致**：`ack` 一步完成"拉任务目录+读 spec+确认 in_progress+推送"，**响应中包含 spec 内容**（沿袭 copaw ack_task 原版设计）；`submit` 一步完成"写 result.md+标记 submitted+推送+远端 verify"。任务文件的同步全部由 taskflow 命令内部处理，worker **不单独调 filesync 处理任务收发**（照抄 copaw file-sharing skill 约束）；非任务共享文件才用 `agentteams-sync push/pull` |
| D8 | 成员认知 | **只用 AGENTS.md 静态名单**（Coordination 段 Team Workers + matrix localpart 对照表）。沙箱不放 agt、不需要 controller 访问 |
| D9 | 记忆连续性 | `memory/` 为唯一跨任务记忆（MinIO 权威、初始化拉/收尾推，机制归沙箱团队）。`progress/*.md` 按 copaw 语义为 **optional**（仅 spec 要求时写）；`task-history.json` 是 openclaw 专属机制，不迁移 |
| D10 | outputs/ 平台回调 | copaw 基线无 outputs/ 概念，随基线移除（openclaw 专属；notify-platform.sh env 命名断裂本就是坏的） |

### 0.3 四工具改造总览（copaw tool call → opencode 载体）

| copaw 工具 | worker 用不用 | opencode 载体 | 改造点 |
|---|---|---|---|
| `taskflow`（check_task / ack_task / submit_task） | ✅ 核心 | `taskflow` bash CLI（T1/T2） | 状态机 vendor 自 `task.py`；JSON action 调用 → 命令行参数；一步到位语义逐条对齐（ack 响应含 spec、submit 写 result+推送+verify，D7） |
| `filesync`（pull / push / stat） | ✅ 非任务文件与中途推送 | `agentteams-sync` bash CLI（T3） | 三 action → 三子命令（含 path+exclude 参数语义）；"任务收发绝不调 filesync"约束照抄 |
| `message` | ❌ **零改造** | 无——回复即产出 | copaw communication skill 本就禁止 worker 用 message 跨房间（"Always reply directly in the current room"）；汇报=当前会话回复→编排层转发（Matrix 侧他人负责）。任务主链路 ack/submit 不发消息 |
| `projectflow` | ❌ **零改造（一期）** | 无 | copaw 里是 leader/manager 专属工具，worker 从不调用（D5）；worker 读项目上下文走 `agentteams-sync pull shared/projects/{id}/`。二期 leader 迁移时再 CLI 化（§8） |

---

## §1 协作流程全景（一条任务的完整生命周期）

### 1.1 时序

```
Leader(copaw, 原生 taskflow 工具，不动)
  │ delegate_task：写 shared/tasks/{id}/meta.json(status=assigned)+spec.md
  │ → 推 MinIO → Matrix @mention 通知 worker            ← (Matrix 侧他人负责)
  ▼
触发层唤醒 opencode worker                              ← (他人负责)
  ▼
沙箱初始化：MinIO agents/<name>/ + shared/ → 沙箱        ← (他人负责，内容=§4.3)
  ▼
opencode 启动，system prompt 4 段传入（§3）              ← (编排层传入)
  ▼ LLM 推理（会话延续，opencode 自管上下文）
  ├ Receive：判断消息是新任务/延续/提问/上下文（history 仅作上下文，只响应当前消息）
  ├ Start：先在当前会话直接说"收到"（copaw 规则：不刷屏、不重复确认）
  ├ 【接活】taskflow ack {id}（= copaw ack_task，一步完成：
  │     拉任务目录 → 读 spec/meta → 校验身份 → assigned→in_progress
  │     → 推 MinIO；响应中包含 spec 内容，不再单独读文件）
  ├ 【执行】按 spec 干活；交付物只写 shared/tasks/{id}/ 内
  │     （workspace/ 子目录 + 任务目录下 deliverables）
  │     长任务中途推送：agentteams-sync push <path>（= copaw filesync.push）
  │     进度消息格式 "Progress: completed X; next Y"（copaw 反模式清单）
  │     progress/日期.md 仅 spec 要求时写（optional）
  ├ 【交付】taskflow submit {id} --status SUCCESS --summary "..." --deliverables ...
  │     （= copaw submit_task，一步完成：校验身份+状态 → 写协议格式
  │      result.md → submitted → 推 MinIO → 远端 stat verify）
  └ 【Notify】@coordinator TASK_COMPLETED/BLOCKED/QUESTION 词法汇报
        （copaw communication 规则：当前房间回复即产出，NO_REPLY 抑制低信息回复）
        → 编排层转发团队                              ← (转发机制他人负责)
  ▼
Leader check_task（自动 pull shared/tasks/{id}/）
  读 result.md → 有效结果(SUCCESS/SUCCESS_WITH_NOTES)？
  ├ 是 → plan.md 节点标 [x] → projectflow ready_nodes → 派下一任务
  └ 否(REVISION_NEEDED/BLOCKED) → 修订循环 / 上报 Manager
```

### 1.2 状态机（与 copaw 版逐字节兼容）

```
pending ──delegate(leader)──▶ assigned ──ack(worker)──▶ in_progress ──submit(worker)──▶ submitted
DAG 视角：plan.md 标记 [ ] → [~](delegated) → [x](completed，leader 侧判定)
```

### 1.3 四道护栏（全在脚本内，LLM 绕不过）

| 护栏 | 挡住什么 |
|---|---|
| `ack`/`submit` 身份比对：env `AGENTTEAMS_MATRIX_USER_ID` 的 localpart == `meta.assigned_to` | 冒充他人接活/交付 |
| CAS：`ack` 要求 status==assigned，`submit` 要求 in_progress | 跳步/重复操作 |
| `result.md` 只由 submit 脚本按协议格式生成 | 手写格式漂移 |
| submit 后远端 verify（mc stat result.md） | 推送失败但状态已标 submitted |

---

## §2 内容包清单（每个文件：是什么 / 干什么 / 来源）

### 2.1 身份与指令文件（MinIO `agents/<name>/`）

| 文件 | 干什么 | 处置 |
|---|---|---|
| `SOUL.md` | worker 人格/自我认知 | 内容零改，进 system[1] |
| `AGENTS.md` | 行为规范：六阶段任务工作流、NO_REPLY、汇报词法、语言跟随、凭据禁读、反模式清单、Team Workers 名单（成员认知唯一来源，D8） | controller 管线生成，模板基线=copaw-worker-agent，载体适配改写（§3.2） |
| `memory/` + `MEMORY.md` | 跨任务长期记忆 | 初始化拉/收尾推；读写规则进 AGENTS.md |

### 2.2 skills（MinIO `agents/<name>/skills/` → 沙箱 `~/skills/`）

> 清单以 **copaw-worker-agent 的 6 个 skills 为基线**（D0）；openclaw 专属的 task-progress / project-participation 不迁移（见 §8）。

| skill / 脚本 | 干什么 | 来源与处置 |
|---|---|---|
| `task-management/SKILL.md` | **任务协作主 skill**：触发词（task ID/spec.md/ack/submit/BLOCKED…）、任务目录所有权（taskflow 拥有 result.md/meta.json）、六阶段流程、命令样例 | 改写自 copaw 同名 skill：仅把 `taskflow(action="ack_task")` JSON 样例换成 `taskflow ack` 命令行，其余原样 |
| `task-management/scripts/taskflow.py` | **状态机 CLI**：check/ack/submit = copaw taskflow 工具的 check_task/ack_task/submit_task 三个 action；四道护栏；内置推送 | **新写**：vendor `copaw/src/copaw_worker/task.py`（纯 stdlib，剥 agentscope 类型） |
| `file-sharing/SKILL.md` | 非任务共享文件读写、中途推送、缺文件排查；"任务收发绝不调 filesync"约束 | 改写自 copaw 同名 skill：filesync pull/push/stat JSON 样例 → `agentteams-sync` 命令行 |
| `file-sharing/scripts/agentteams-sync.sh` | 同步 CLI：`pull <path>` / `push <path> [--exclude …]` / `status <path>`，对齐 copaw filesync 三 action；无参=pull 兼容 | 扩展现有 pull-only 脚本；watermark 增量与排除表复用 `worker-file-sync.sh:worker_sync_push_once`（:23-34） |
| `communication/SKILL.md` | 汇报词法（@coordinator TASK_COMPLETED/BLOCKED/QUESTION）、@mention 规则、NO_REPLY、loop safeguard、语言跟随 | 改写自 copaw 同名 skill：近乎原样（"当前房间回复"本来就是 copaw 机制）；仅把 room 表述映射为"当前会话回复" |
| `organization/SKILL.md` | 团队拓扑/身份查询（copaw 版走 agt CLI） | 按 D8 改写：删 agt 依赖，指向 AGENTS.md Coordination 静态名单；信息缺失时问 coordinator |
| `find-skills/` | 技能发现 | 原样（两版相同） |
| `mcporter/` | MCP 工具经 bash 跑 mcporter CLI（runtime 无关） | 原样（两版相同） |

### 2.3 沙箱基础依赖（沙箱团队镜像，契约 §4）

mc（MC_HOST 凭据）、jq、python3、`/opt/agentteams/scripts/lib/`（agentteams-env.sh / worker-file-sync.sh / oss-credentials.sh / mc-wrapper.sh / base.sh）、`/usr/local/bin/agentteams-sync` wrapper。**无 agt、无 controller 访问**（D8）。

---

## §3 system prompt 设计（4 段，内容本方案定义 / 传入归编排层）

```
system =
  [1] 身份段    SOUL.md 全文（原样）
  [2] 指令段    AGENTS.md 三层合并结果（controller 生成，含 Coordination 段：
               Team Workers 名单 + matrix localpart 对照表）
  [3] 运行时段  opencode 专属新写：
               - 工具形态：协作工具是 bash 命令（taskflow / agentteams-sync），
                 经 shell 调用，样例见 task-management skill
               - 目录：~/ = workspace；/root/agentteams-fs/shared/ = 协作区
               - 任务文件（meta/spec/result/progress）的同步由 taskflow 命令
                 内部完成（ack/submit 自带拉取+推送，ack 响应含 spec 内容，见 D7）
                 非任务共享文件（plan/参考资料）才 agentteams-sync pull/push
               - 上下文规则：SOUL/AGENTS 已在上下文中勿重读；
                 新任务先回"收到"再 taskflow ack（响应含 spec 内容，D7）；同一任务延续对话时
                 状态在上下文中，但 meta.json 是唯一权威，长间隔/存疑时 check 核对
               - 回复即产出：最终文本会被转发给团队，汇报词法规则重申
  [4] 环境段    ${AGENTTEAMS_*} 实际值渲染：
               worker 名 / matrix localpart / team / 存储前缀 / 当前日期
```

### 3.2 AGENTS.md 模板改写（基线 = copaw-worker-agent/AGENTS.md）

模板基线换成 copaw 版后，**绝大多数内容原样保留**（六阶段工作流、NO_REPLY、语言跟随、凭据禁读安全段、反模式清单、循环防护、history-as-context 规则），仅以下载体适配点改动：

| # | copaw 原文 | 改写后 |
|---|---|---|
| 1 | "You are a **QwenPaw Worker** — a Python-based agent" | "You are an opencode Worker"（去 runtime 名） |
| 2 | 工具调用样例 `taskflow(action="ack_task")`（JSON） | `taskflow ack {task-id}` 等命令行样例 |
| 3 | "Do not call `filesync pull/push/stat` for task acceptance…" | 同样约束，命令名换 `agentteams-sync` |
| 4 | "In the current room, directly say…"（Start/Notify 的 room 语义） | "在当前会话直接回复…"（回复即产出，转发归编排层） |
| 5 | §4 skills 清单中 organization 走 agt CLI | organization 条目指向 AGENTS.md 名单（D8），删 agt 表述 |
| 6 | （新增段） | opencode 运行时说明：目录布局、上下文规则（并入 §3 [3] 段） |

---

## §4 接口契约（交付沙箱团队 + 编排层）

### 4.1 环境变量（编排层注入）

| env | 用途 | 必须 |
|---|---|---|
| `AGENTTEAMS_WORKER_NAME` | workspace 定位，所有脚本依赖 | ✅ |
| `AGENTTEAMS_MATRIX_USER_ID` | taskflow 身份校验（取 localpart） | ✅ |
| `AGENTTEAMS_STORAGE_PREFIX` | MinIO 前缀 | ✅ |
| `MC_HOST_agentteams`（或 FS endpoint/AK/SK trio） | MinIO 凭据 | ✅ |

> 另一条契约（D6）：**触发消息需携带必要 history/任务上下文**（与 copaw 消息机制同构）——由编排层构造消息时注入。

### 4.2 目录布局（Teams 原布局，runtime 无关，模板路径零改动）

```
/root/agentteams-fs/
├── agents/<name>/     ← HOME/workspace（SOUL.md、AGENTS.md、skills/、memory/…）
└── shared/            ← 协作区（tasks/{id}/、projects/{id}/）
```

### 4.3 沙箱初始化内容清单（拉什么、排除什么）

拉：`agents/<name>/`（**排除** `credentials/**`、`.openclaw/**`）+ `shared/`；`skills/**/*.sh` chmod +x 重放（MinIO 不保留权限位）。
收尾推送：workspace 变更 + `shared/` 中 worker 写入的部分（排除表同上反向）。

---

## §5 协作动作 ↔ 工具串联映射

| 协作动作 | 谁做 | 用什么 | 读写 | 对方怎么看到 |
|---|---|---|---|---|
| 派任务 | Leader | copaw 原生 taskflow 工具（不动） | 写 meta.json+spec.md → MinIO | 触发唤醒 worker |
| 查任务（可选） | worker | `taskflow check` | 读 meta.json（状态摘要） | — |
| 接活 | worker | `taskflow ack` | 写 in_progress → 推 | Leader check_task / 状态聚合 |
| 执行+进度 | worker | communication 词法 + progress/*.md（optional） | 交付物写 shared/tasks/{id}/ 内 | push 后 Leader 可读 |
| 中途保存 | worker | `agentteams-sync push <path>` | 按 copaw filesync.push 语义推指定路径 | MinIO 即权威态 |
| 拉协作区 | worker | `agentteams-sync pull` | 拉 shared/+配置 | — |
| 交付 | worker | `taskflow submit` | 脚本生成 result.md → submitted → 推+verify | Leader check_task 自动 pull |
| 汇报 | worker | communication skill 词法（@coordinator TASK_COMPLETED/BLOCKED/QUESTION） | — | 回复即产出，编排层转发 |
| 查团队/队友 | worker | AGENTS.md 名单（静态） | system[2] 已含 | — |
| DAG 推进 | Leader | copaw 原生 projectflow（不动） | plan.md 标记 | ready_nodes 派下一任务 |

---

## §6 任务拆分（4 大模块 / 13 小任务 × 2 天 = 26 人日）

### 6.1 模块总览

| 模块 | 目标 | 小任务 | 工期 |
|---|---|---|---|
| **M1 协作命令层** | copaw 工具语义 → bash CLI（taskflow / agentteams-sync） | T1-T3 | 6 人日 |
| **M2 认知与规范层** | copaw 内容包 → opencode 模板（AGENTS.md / skills / system prompt） | T4-T7 | 8 人日 |
| **M3 契约与移交层** | 接口契约定稿 + controller 移交 | T8-T9 | 4 人日 |
| **M4 验证与联调层** | 本地冒烟 → 保底模拟 → 真环境验收 | T10-T13 | 8 人日 |

### 6.2 任务明细

| # | 模块 | 任务 | 核心内容 | 交付物 | 验证方式 | 依赖 |
|---|---|---|---|---|---|---|
| T1 | M1 | taskflow CLI：状态机与命令 | vendor `task.py` 状态机（剥 agentscope 依赖）；`check/ack/submit` 三命令；ack 响应包含 spec 内容（D7）；check 输出 meta 状态摘要 | `taskflow.py` 命令骨架 | 命令可跑：check 摘要 / ack 状态转移 / submit 生成 result.md | — |
| T2 | M1 | taskflow CLI：护栏与推送 | 四道护栏（身份比对/CAS/result 仅由 submit 生成/远端 verify）；ack/submit 内置推送 + 推送失败回滚 | `taskflow.py` 完整版 + 单测 | 单测：状态机全转移 + 全部拒绝路径（身份/状态/推送失败回滚） | T1 |
| T3 | M1 | filesync CLI 对齐 | `agentteams-sync` 对齐 copaw filesync 三 action：`pull <path>` / `push <path> [--exclude]`（path 精确推送 + 无 path 时 watermark 全量增量，排除表照抄 worker-file-sync.sh:23-34）/ `status <path>`（远端核对）；无参=pull 兼容 | 更新版 `agentteams-sync.sh` + 单测 | 单测：path 推送、增量正确性、排除表命中、一致性判定 | — |
| T4 | M2 | AGENTS.md 模板改写 | §3.2 六点载体适配（runtime 名 / 命令样式 / filesync 约束 / room→会话 / 删 agt / 新增运行时段）；新建 `opencode-worker-agent/` 目录骨架 | AGENTS.md 模板 + 目录骨架 | 与 copaw-worker-agent 版逐条 diff（唯一差异=载体） | T1（命令签名） |
| T5 | M2 | 核心对 skills 改写 | `task-management`（JSON 样例→CLI+六阶段流程）+ `file-sharing`（filesync→agentteams-sync，"任务收发不调"约束照抄） | 2 份 SKILL.md | 逐条 diff copaw 版；命令样例与 T1/T3 实现一致 | T1, T3, T4 |
| T6 | M2 | 辅助 skills 改写 | `communication`（room→当前会话表述）+ `organization`（删 agt→AGENTS.md 名单，D8）+ `find-skills`/`mcporter` 核对 | 4 份 SKILL.md | 逐条 diff；organization 确认无 agt 残留 | T4 |
| T7 | M2 | system prompt 模板 | 4 段结构定稿；[3] 运行时段文案（含 D6 上下文规则：不写 check 硬规则）；[4] 环境段占位符与渲染规则 | prompt 模板 + 渲染规则文档 | 与 copaw worker 指令基线对账方法（供 V4 用） | T5, T6 |
| T8 | M3 | 接口契约定稿 | §4 定稿为正式契约（env / 目录 / 初始化清单 / 触发消息带 history）；与沙箱/编排/Matrix 三方对齐 | 契约文档 | 三方对齐确认 | —（独立并行） |
| T9 | M3 | controller 移交包 | 模板目录 + deployer.go `builtinAgentDir()` 加 opencode case 的改动说明 + 冒烟脚本 | 移交说明 | controller 负责人确认可接 | T5-T7 |
| T10 | M4 | 本地冒烟 | 不动 controller 手动放模板，按 §6.4 六步跑全链路 | 冒烟脚本 + 冒烟记录 | §6.4 冒烟 6 步全过 | T9 |
| T11 | M4 | 保底：本地模拟编排层 | ~百行模拟器：假触发（消息带 history，对齐 D6 契约）+ 假沙箱初始化 + 假消息转发，本地跑通全链路 | 模拟器脚本 + 联调记录 | delegate→ack→执行→submit→leader 读 result 整链本地跑通 | T10 |
| T12 | M4 | 真环境联调一 | 沙箱团队就绪后接真环境：V1 任务闭环 / V2 护栏 / V3 身份认知 | 联调报告 + 修复 | V1-V3 验收通过（§7） | T11 + 沙箱就绪 |
| T13 | M4 | 真环境联调二 + 收尾 | V4 指令保真 / V5 记忆跨任务 / V6 混编（copaw+opencode worker 并存）；二期清单收尾 | 验收报告 + 最终文档 | V4-V6 验收通过（§7） | T12 |

### 6.3 依赖与排期

```
模块内串行、模块间按依赖衔接：
  M1:  T1 → T2 ；  T3（与 T1/T2 并行）
  M2:  T4（等 T1 命令签名）→ T5（等 T3）→ T6 → T7
  M3:  T8（独立并行） ；  T9（等 M2 完成）
  M4:  T10 → T11 → T12（另需沙箱团队就绪）→ T13

单人串行：T1→T2→T3→T4→T5→T6→T7→T8→T9→T10→T11→T12→T13   26 人日 ≈ 5 周
两人并行：A 线（关键路径）：T1→T2→T4→T5→T7→T9→T10→T11→T12→T13
          B 线：T3→T6→T8→（支援 M4）                        ≈ 3 周
关键路径：T1 → T4 → T5 → T7 → T9 → T10 → T11 → T12 → T13
```

### 6.4 T9 移交物与 T10 冒烟步骤

**T9 移交 controller 负责人（仅 1 处代码改动）**：

| 项 | 内容 |
|---|---|
| 模板目录 | `opencode-worker-agent/`（T4-T6 产物，含 AGENTS.md 模板 + 全部 skills） |
| 代码改动 | `internal/service/deployer.go` 的 `builtinAgentDir()`（约 :1488）加 `runtime==opencode` case 返回新模板目录（参考现有 qwenpaw case 写法） |
| 不改的理由 | openclaw.json 照常生成无害（模板已不引用）；其余 DeployWorkerConfig 管线全部不动 |

**本地冒烟步骤（不动 controller，手动放模板）**：

| 步 | 操作 | 预期 |
|---|---|---|
| 1 | 本地 MinIO（或 mc alias 指测试桶），按 §4.2/4.3 手工构建 `agents/test-worker/` 目录树（SOUL/AGENTS/skills/memory）并推上 | MinIO 前缀下文件齐全 |
| 2 | 模拟 leader：手写 `shared/tasks/t-1/meta.json`（status=assigned，assigned_to=test-worker 的 localpart）+ spec.md，推 MinIO | 任务就位 |
| 3 | 导出 §4.1 四个 env，依次跑 `taskflow check t-1` → `ack t-1` → 在 `shared/tasks/t-1/` 下构造交付物 → `submit t-1 --status SUCCESS --summary ... --deliverables ...` | ack 响应包含 spec 内容；ack 后 in_progress 且 MinIO 同步；submit 后 result.md 存在且 submitted |
| 4 | 拒绝路径：改 assigned_to 冒充 → ack；assigned 状态直接 submit | 两者均报错拒绝 |
| 5 | 逐字段核对 MinIO 上 meta.json / result.md | 与 copaw worker 产物格式逐字段一致 |
| 6 | `agentteams-sync push/status` 循环：改本地文件 → push → status | push 后 status 报一致；人为改远端后 status 报差异 |

---

## §7 验收清单

| # | 验收项 | 通过标准 |
|---|---|---|
| V1 | 任务闭环 | leader delegate → check/ack/submit → leader check_task 读到；meta.json/result.md 与 copaw worker 产物**逐字段一致** |
| V2 | 护栏 | 非 assigned 状态 ack 被拒；身份不符 ack/submit 被拒；result.md 仅由 submit 生成 |
| V3 | 身份认知 | system 快照 4 段齐全；"你是谁/团队/队友"回答正确（对照 Coordination 段） |
| V4 | 指令保真 | system prompt 与 copaw worker 指令基线对账：内容一致（仅 §3.2 六点载体差异） |
| V5 | 记忆连续性 | 任务 A 写 memory → 任务 B（新沙箱）读到 |
| V6 | 混编协作 | 同队 copaw worker + opencode worker 并存，copaw leader 无感区分 |

---

## §8 二期遗留清单

1. projectflow CLI 化（leader 迁 opencode 时同构 vendor，1-2 天）
2. delegate 侧脚本化（若 leader 换 runtime）
3. openclaw 增强 skills 评估：task-progress（断点恢复/task-history.json）、project-participation——copaw 基线没有，确有需要再补
4. mcporter → opencode 原生 MCP 映射（现走 bash 够用）
