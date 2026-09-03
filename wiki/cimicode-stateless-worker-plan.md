# OpenClaw → 无状态 cimicode 替换技术方案

> 基于 agi-agentteams / agi-agentteams-controller / agi-cimicode / agi-context 项目深度代码分析，
> 及新版 cimicode 无状态方案（统一会话管理 + cimicode 无状态服务编排 + CimiCode 池化 + Sandbox Gateway + Skill Catalog）。
>
> **cimicode 无状态服务由其他团队提供，teams 是消费方。本文聚焦 teams 侧技术方案，
> 同时明确 cimicode 无状态服务必须提供哪些能力，避免能力缺失导致方案失效。**

---

## 一、背景与目标

### 1.1 问题

当前 Agent Teams 系统中，每个 agent 启动一个 OpenClaw 常驻 pod（Node.js LLM gateway + Ubuntu + 本地 fs + Matrix 长连接），不管 agent 是否在干活都占用资源。一个 team 有 N 个 agent（含 Team Leader），就创建 N 个常驻 pod。

### 1.2 目标

用其他团队提供的无状态 cimicode 服务替换 OpenClaw，降低资源占用。cimicode 无状态服务负责 LLM 推理、会话管理、沙箱执行、文件管理、Skills 加载等。teams 侧只需要一个轻量 bridge pod 做 Matrix 对接。

### 1.3 职责边界

**cimicode 无状态服务（其他团队提供，teams 不实现）：**

- 统一会话管理 PG（canonical 对话真相源）
- CimiCode Replica Pool（无状态 LLM 推理 + 工具调度）
- Sandbox Gateway（按需创建/TTL/drain/LRU/generation 重建）
- 文件管理 + S3（附件 + 产物 + publish_artifact）
- Skill Catalog + S3 Package Store（按需加载）
- Nacos 配置（opencode.json 不可变 Revision）
- cimicode 无状态服务编排（Session 创建/Turn 调度/Sandbox 生命周期/Turn 提交）

**teams 侧（本文聚焦）：**

- Bridge pod（Matrix 监听 + 调 cimicode 无状态服务 API + 事件流转发 Matrix）
- HiClaw operator（创建 bridge pod + Matrix bot provision）
- agi-agentteams-controller（team CRUD + 调 cimicode 无状态服务创建 agent Session）

### 1.4 根本挑战

OpenClaw 和 cimicode 是**两个不同的运行时**：

| 维度     | OpenClaw（现状）                                             | cimicode 无状态服务                                          |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 状态     | 有状态，本地 SQLite/.jsonl + MinIO                           | 无状态，统一会话管理 PG                                      |
| 生命周期 | 长驻 pod，永不退出                                           | 池化 Replica，Turn 完即释放                                  |
| Matrix   | 自带 Matrix plugin，主动连 Matrix                            | 不连 Matrix                                                  |
| 文件操作 | worker pod 本地 fs + mc sync MinIO                           | Sandbox PVC 工作现场 + publish_artifact                      |
| 定时任务 | 内部 cron（heartbeat/dreaming）                              | 无定时机制                                                   |
| 指令加载 | SOUL.md/AGENTS.md/HEARTBEAT.md/openclaw.json/skills（runtime 内置） | AGENTS.md（instruction.ts）；配置走 Nacos；Skills 走 Catalog |

**cimicode 无状态服务是为首页单用户 chat 场景设计的。** team 场景是多 agent 群聊——每个 agent 是 Matrix 群里的成员，需要主动连 Matrix 监听 @mention、维护会话状态、能 @mention 别人协作。

**核心难点：teams 侧需要一个 Bridge 组件补上 cimicode 缺失的"Matrix 群聊成员"角色，且 team 侧的协作机制（@mention 协议/群聊视野/文件协作/heartbeat）需要和 cimicode 无状态服务 API 对接。**

### 1.5 方案选择

采用**"每 agent 一个轻量 bridge pod + 调 cimicode 无状态服务 API"**方案：

- **不改变分布式架构**——每 agent 一个独立 bridge pod，保持无单点/无瓶颈/爆炸半径小
- **bridge 极轻**——只需 Matrix 监听 + 调 cimicode 无状态服务 API + 事件流转发，不需要 PG/S3/沙箱管理（需 Redis 存 session_id 映射/Matrix token）
- **LLM 推理集中**——cimicode 无状态服务池化，按需扩缩
- **cimicode 无状态服务接管编排**——上下文重建/Session 管理/Sandbox 管理/文件管理/Skills/配置都由 cimicode 无状态服务自己处理

---

## 二、现状分析

### 2.1 项目全景

```
agi-agentteams/              # HiClaw 核心（Go K8s operator + Docker 镜像）
  ├── hiclaw-controller/     # Go operator：Team/Worker/Human CRD 编排
  ├── worker/                # OpenClaw Worker 镜像
  ├── manager/               # Manager 镜像 + agent 模板 + skills
  └── openclaw-base/         # 基础镜像

agi-agentteams-controller/   # 桥接层（Java Spring Boot + Dubbo REST）
  └── 对前端 /v1/teams/*，对下用 HiClawClient 调 hiclaw operator

agi-cimicode/                # 无状态 cimicode（TypeScript/Bun，opencode fork）
  └── packages/opencode/    # 核心：context_prompt SSE + InMemorySessionStore + sandboxID 穿透

agi-context/                 # 编排层（Java Spring Boot）
  └── 已实现单用户 chat：建 OpenSandbox + 调 cimicode SSE + PG 持久化历史
```

### 2.2 当前 OpenClaw 架构

```
前端 → controller（Dubbo REST）→ HiClawClient → HiClaw operator
  → Team CR reconciler → 为 spec.workers 每个 agent 创建 1 个 openclaw worker pod
  → 创建 Matrix bot 账号 + provision room + 生成配置文件推 OSS

每个 openclaw worker pod：
  · worker-entrypoint.sh 启动
  · 从 MinIO 拉配置（openclaw.json, SOUL.md, AGENTS.md, skills/）
  · 起 file sync（本地 fs ↔ MinIO 双向同步）
  · 连 Matrix + exec openclaw gateway run（永不退出）
  · 监听 @mention → 响应 → 可 @mention 别人协作
```

### 2.3 OpenClaw agent 群聊上下文机制

**LLM 输入格式（两段式）：**

```
[Chat messages since your last reply - for context]
... 别人在这段时间说的话（per-room buffer, ≤50 条 FIFO）...

[Current message - respond to this]
... 这次 @我 的消息 ...
```

**三层记忆：**

| 层次                 | 存储                       | 生命周期        | 用途             |
| -------------------- | -------------------------- | --------------- | ---------------- |
| session（.jsonl）    | pod 本地 + MinIO           | 每天 04:00 重置 | LLM 对话上下文   |
| agent 手写 memory/   | Markdown + MinIO           | 永久            | 日记本和长期笔记 |
| memory-core dreaming | OpenClaw 内部 memory store | cron 每 6h 蒸馏 | 自动记忆浓缩     |
| memorySearch         | embedding 向量检索         | 持久            | RAG 检索长期记忆 |

**agent 间协作：** 默认 worker 间不能互 @mention（`groupAllowFrom` 只含 Manager/Admin），协作靠 Team Leader 中转 + MinIO 共享文件（spec.md/result.md/plan.md）。

**指令体系（OpenClaw runtime 内置加载）：**

| 文件                  | 机制                                  | 作用                                                         |
| --------------------- | ------------------------------------- | ------------------------------------------------------------ |
| SOUL.md               | runtime 启动时自动加载                | agent 人格（seed-only，agent 可自我修改）                    |
| AGENTS.md             | runtime 自动加载                      | 行为指令（@mention 协议/NO_REPLY/任务执行/文件操作）         |
| HEARTBEAT.md          | runtime cron 定时读                   | Leader heartbeat 检查清单（seed-only）                       |
| openclaw.json         | runtime 启动时读                      | 运行配置（models/gateway/channels/matrix/plugins/session/dreaming） |
| skills/SKILL.md       | runtime 自动加载                      | 技能说明（skills/scripts/ 在 pod 本地执行）                  |
| mcporter-servers.json | agent 通过 mcporter CLI 调 MCP Server | MCP 工具配置                                                 |

**Operator 生成的初始文件**（`deployer.go:164-351`）：openclaw.json（GenerateOpenClawConfig ~450 行）/SOUL.md（seed-only）/AGENTS.md（三层合并 + InjectCoordinationContext 注入协作上下文）/HEARTBEAT.md（seed-only）/mcporter-servers.json/skills/credentials。

**AGENTS.md 三层合并**（`prepareAndPushAgentsMD:827`）：

1. builtin 模板（MergeBuiltinSection 标记包裹，保留用户内容）
2. package/OSS 内容
3. InjectCoordinationContext（per-instance 协作上下文：coordinator/room/workers/@mention 协议）

### 2.4 新版 cimicode 无状态方案

**核心架构（`cimicode无状态方案-new.md`）：**

| 组件                                 | 职责                                                         |
| ------------------------------------ | ------------------------------------------------------------ |
| **专家模式cimicode 无状态服务**      | Turn 唯一写入者；创建 Session + SessionSandboxBinding；调度 CimiCode Replica；编排 Sandbox 生命周期；汇聚 CimiCode 事件提交 Turn |
| **统一会话管理 PG**                  | canonical 真相源（Session/Turn/用户输入/结构化输出）；只读 API（按 session_id + through_message_seq） |
| **CimiCode Replica Pool**            | 无状态，无 PVC，从 PG 读取 canonical 重建上下文；输出结构化运行事件（带 event_seq）；通过 Sandbox Gateway 执行；publish_artifact 显式发布产物 |
| **Sandbox Gateway**                  | 受控入口；ensure+provision+restore+exec；TTL/drain/LRU/generation 重建 |
| **文件管理 + S3**                    | 附件 + 产物元数据；Artifact Manifest（逻辑产物 + 不可变版本）；S3 签名 URL 下载 |
| **Skill Catalog + S3 Package Store** | Git 发布 → 不可变包 → Catalog 登记元数据 → CimiCode 读 SKILL.md → Sandbox 物化 scripts/templates/assets |
| **Nacos**                            | opencode.json 不可变 Revision；CimiCode Pod 启动加载固定 Revision |

**Turn 执行流程**（`§6`）：

```
用户提交消息 → cimicode 无状态服务激活 Turn 写入统一会话管理 → 调度队列分配 CimiCode Replica
  → CimiCode 读取 canonical 历史 → 通过 Sandbox Gateway 执行 → 输出结构化运行事件
  → cimicode 无状态服务汇聚事件提交 Turn → 返回事件流给用户
```

**关键契约**（`§16`）：

```
create_expert_session(owner_id)
submit_turn(session_id, input, file_ids, required_skill_ids)
read_session(session_id, through_message_seq, capability)
complete_turn(turn_id, attempt_id, status, structured_output)
ensure_and_exec(session_id, turn_id, command, capability)
publish_artifact(path, display_name, artifact_id?, capability)
list_session_artifacts(session_id)
download_artifact(artifact_id, version_id?)
```

**Sandbox 生命周期**（`§7`）：按需创建（第一次工具调用触发）→ TTL 内多 Turn 复用 → TTL 到期 drain → 新 Turn 创建新 generation → 恢复最新产物。

**失败恢复**（`§12`）：CimiCode/cimicode 无状态服务失联 → Turn 标记 interrupted → 持久化已确认部分 → 用户显式重试（不自动重试，不回滚副作用）。

---

## 三、整体架构

### 3.1 架构图

```
┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 teams 侧（本文聚焦）

┌─────────────────────────────────────────────────────────────────────┐
│ 前端（不变）                                                           │
└──────────────┬──────────────────────────────────────────────────────┘
               │ Dubbo REST
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ agi-agentteams-controller                                            │
│  · createTeamCR 传 runtime=cimicode-bridge                           │
│  · team 创建时调 cimicode 无状态服务创建各 agent 的 Session            │
└──────┬──────────────────────────────────────┬───────────────────────┘
       │ HiClawClient REST                    │ 调 cimicode 无状态服务 API
       ▼                                      │
┌──────────────────────────────┐              │
│ HiClaw operator              │              │
│  · 为每个 member 创建 bridge │              │
│    pod                       │              │
│  · Matrix bot provision      │              │
│  · 协作上下文写入 bridge env │              │
└──────────┬───────────────────┘              │
           │                                  │
           ▼                                  │
┌──────────────────────────────┐              │
│ 轻量 Bridge pod（每 agent 1）│              │
│  · Matrix /sync + @mention   │              │
│  · 调 cimicode 无状态服务     │──────────────┘
│    submit_turn              │
│  · 消费事件流                │
│  · 事件流→Matrix 消息        │
│  · NO_REPLY 检测             │
│  · @mention 解析+触发         │
│  · heartbeat 定时器（Leader）│
│  · 两段式群聊视野             │
└──────────────────────────────┘
┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
 cimicode 无状态服务（其他团队提供，teams 不实现）

┌─────────────────────────────────────────────────────────────────────┐
│ cimicode 无状态服务                                                  │
│  · create_expert_session（per agent）                                │
│  · submit_turn（bridge 调用）                                       │
│  · 调度 CimiCode Replica                                            │
│  · 编排 Sandbox Gateway                                             │
│  · 汇聚 CimiCode 事件 → 提交 Turn → 返回事件流给 bridge             │
│  · Session FIFO（同 Session 串行）                                   │
│  · Turn interrupted 管理                                            │
└──────────┬──────────────────────────────────────────────────────────┘
           │
     ┌─────┴─────┬──────────────┬──────────────┬─────────────┐
     ▼           ▼              ▼              ▼             ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
│统一会话  │ │CimiCode  │ │Sandbox   │ │文件管理  │ │Skill Catalog│
│管理 PG   │ │Replica   │ │Gateway   │ │+ S3      │ │+ Package    │
│          │ │Pool      │ │          │ │          │ │Store        │
│canonical │ │          │ │ensure+   │ │publish_  │ │             │
│真相源    │ │从 PG 读  │ │exec      │ │artifact  │ │按需加载     │
│          │ │canonical │ │TTL/drain │ │+ 不可变  │ │             │
│          │ │重建上下文│ │LRU/gen   │ │版本      │ │             │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────────┘
                                         ┌──────────┐
                                         │ Nacos    │
                                         │opencode  │
                                         │.json Rev │
                                         └──────────┘
┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
```

**分界线说明：** 虚线以上是 teams 侧（本文聚焦），虚线以下是 cimicode 无状态服务（其他团队提供）。bridge 通过 cimicode 无状态服务的 API（submit_turn 等）对接。

### 3.2 cimicode 无状态服务必须提供的能力清单

**以下能力是 teams 方案成立的前提。如果 cimicode 无状态服务不提供某项能力，对应 teams 侧功能会断裂。**

| #    | 必须提供的能力                            | 用途                                                         | 不提供的后果                                         |
| ---- | ----------------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------- |
| 1    | **程序化创建 Session API**                | teams 为每个 agent 创建独立 Session（不是 UI 触发）          | bridge 无法获取 session_id → **方案不成立**          |
| 2    | **程序化提交 Turn API**（submit_turn）    | bridge 收到 @mention 后提交 Turn                             | bridge 无法触发 agent 执行 → **方案不成立**          |
| 3    | **事件流返回 API**                        | bridge 消费 Turn 执行事件流，转发 Matrix                     | bridge 无法获取 agent 响应 → **方案不成立**          |
| 4    | **per-Turn system 指令注入**              | bridge 传入协作上下文（coordinator/room/workers）+ HEARTBEAT.md 内容 | agent 不知道 coordinator/room/workers → **协作断裂** |
| 5    | **Session 关闭/删除 API**                 | team 解散时关闭各 agent 的 Session + drain Sandbox           | Session/Sandbox 残留 → **资源泄漏**                  |
| 6    | **多 Session 同时活跃支持**               | 一个 team N 个 agent = N 个 Session 同时活跃                 | 并发不兼容 → **多 agent team 不工作**                |
| 7    | **程序化调用鉴权**                        | bridge/controller 用什么身份调 cimicode 无状态服务 API       | 无法安全对接 → **方案不成立**                        |
| 8    | **Turn interrupted 通知机制**             | CimiCode/cimicode 无状态服务失联时通知 bridge Turn 中断      | bridge 不知道 Turn 失败 → **agent 卡死**             |
| 9    | **跨 Session 文件共享机制**               | team 侧 agent 间靠文件协作（spec.md/result.md）              | agent 间无法文件协作 → **team 协作模式断裂**         |
| 10   | **AGENTS.md 加载渠道**                    | cimicode 的 instruction.ts 从哪里读 AGENTS.md（无 PVC 无共享卷） | agent 无行为指令 → **agent 不知道怎么协作**          |
| 11   | **per-Session 配置隔离**                  | 不同 agent 类型（worker/leader）有不同的 AGENTS.md/skills    | 所有 agent 用同一份指令 → **角色不区分**             |
| 12   | **Sandbox TTL + LRU 回收**                | agent 空闲时 Sandbox 回收，省资源                            | Sandbox 常驻 → **资源没省**                          |
| 13   | **generation 重建 + 产物恢复**            | Sandbox TTL 回收后重建，恢复最新产物                         | 产物丢失 → **agent 工作成果丢失**                    |
| 14   | **Session 日重置 / canonical 按时间过滤** | OpenClaw 每天 04:00 重置 session，canonical 不无限增长       | canonical 过长 → **上下文重建慢/超 token**           |
| 15   | **memory/ 持久化**                        | agent 手写 memory/YYYY-MM-DD.md 和 MEMORY.md，Sandbox TTL 后不丢 | memory 丢失 → **agent 日记和长期笔记丢失**           |

**⚠️ 能力 4（per-Turn system 指令注入）、9（跨 Session 文件共享）、10（AGENTS.md 加载渠道）、14（日重置）、15（memory 持久化）是当前新版方案没有明确的——需和 cimicode 无状态服务团队确认。**

### 3.3 设计决策

**决策 1：每 agent 1 个 bridge pod**

依据：OpenClaw 是分布式的（每 agent 1 pod），保持分布式架构避免单点/瓶颈/爆炸半径。

代价：pod 数量没减（N×T），但 bridge 极轻（只需 Matrix + HTTP client）。

**决策 2：bridge 不直接调 CimiCode，调 cimicode 无状态服务 API（submit_turn）**

依据：cimicode 无状态服务的cimicode 无状态服务是 Turn 调度者和唯一写入者。bridge 必须通过cimicode 无状态服务 API 提交 Turn，不能直接调 CimiCode。

代价：bridge 依赖 cimicode 无状态服务（cimicode 无状态服务挂了 bridge 无法工作）。

**决策 3：每个 agent 一个 cimicode 无状态服务 Session**

依据：cimicode 无状态服务的 create_expert_session 创建独立 Session。team 场景下每个 agent 一个 Session。cimicode 无状态服务不需要知道 team/Leader/Worker 概念——team 的协作逻辑在 bridge 层处理。

代价：一个 team N 个 agent = N 个cimicode 无状态服务 Session + N 个 Sandbox。

**决策 4：硬过滤在 bridge 层，软指令在 AGENTS.md 引导**

依据：OpenClaw 的 @mention 过滤/NO_REPLY/peer-mentions 是 runtime 内置强制的。cimicode 没有这些内置行为。涉及"不调cimicode 无状态服务"或"不发 Matrix"的过滤必须在 bridge 层硬实现。

代价：bridge 要复刻 OpenClaw runtime 的过滤逻辑。剩余风险在 LLM 输出端。

**决策 5：协作上下文通过 submit_turn 的 system 字段注入**

依据：bridge 从环境变量读协作上下文（coordinator/room/workers），拼进 submit_turn 的 input.system 字段传入。cimicode 无状态服务传递给 CimiCode，拼进 system prompt。

代价：协作上下文占 LLM context（几百 token）。**前提：cimicode 无状态服务的 submit_turn 必须支持 per-Turn system 指令（能力清单 #4）。**

---

## 四、分模块技术方案

### 4.1 Bridge Pod

**角色：** 替换 OpenClaw worker pod。补上 cimicode 缺失的"Matrix 群聊成员"角色——每 agent 一个独立 bridge pod，只做 Matrix 监听 + 调cimicode 无状态服务 + 事件流转发。

**爆炸半径：** 1 个 bridge pod 挂只影响 1 个 agent。K8s 自动重启。恢复只需连 Matrix + 连cimicode 无状态服务（不需恢复沙箱/PG/Redis，比 V1 快）。

**技术栈：** 只需 Matrix SDK + HTTP client + Redis 客户端。不需要 PG/S3/沙箱管理。资源前景比 OpenClaw pod 轻很多。

#### 4.1.1 Matrix 连接

**设计：** bridge 用该 agent bot 账号建立 /sync 长轮询连接。login + token 缓存 Redis + 401 重登录 + backoff 重连。E2EE 用 Matrix SDK 的 olm/megolm 支持。since token 持久化 Redis（TTL 7 天）。

**风险：**

1. **Matrix SDK 成熟度**：Go 的 mautrix-go 生态小；Bun 的 matrix-js-sdk 官方但 Bun 兼容性需验证；Python 的 matrix-nio 最成熟但最重。**E2EE 是 Matrix 最复杂的部分**——device key 管理、key 分发/信任、session 管理。SDK 对 E2EE 支持不完善则 E2EE room 的 agent 无法正常工作。

2. **since token 丢失**：Redis 挂了或 token 过期 → 全量 /sync → 大量历史消息涌入。需限制全量同步处理量。

3. **多 bridge 同时 login 触发 Synapse 限流（429）**：team 创建时 N 个 agent 同时启动，N 个 login 请求。需 login 重试 + 退避。

4. **Matrix 消息延迟**：agent 间协作经 Matrix Server 中转，/sync 有 1-5s 延迟。多跳协作（A → Leader → B）延迟叠加。OpenClaw 是同进程内 Matrix plugin 直接收消息（延迟极小）。

5. **E2EE crypto state 每次 pod 重启丢失**：和 OpenClaw worker-entrypoint.sh:314 一样 `rm -rf` crypto storage 重新协商。需重新 login 创建新 device_id + 重新协商 E2EE keys。

#### 4.1.2 @mention 检测与硬过滤

**设计：**

| 过滤规则          | bridge 实现                                                  | 对应 OpenClaw                     |
| ----------------- | ------------------------------------------------------------ | --------------------------------- |
| requireMention    | 不 @自己的消息不调cimicode 无状态服务（存入 history buffer） | `groups.*.requireMention: true`   |
| groupAllowFrom    | 只处理允许列表（Leader/Admin/Human）的 @mention              | `groupAllowFrom: [leader, admin]` |
| peer-mentions off | worker 响应里 @其他 worker 不触发                            | `groupAllowFrom` 不含其他 worker  |
| 自 @过滤          | agent 不能 @自己触发自己                                     | runtime 自动过滤                  |

**@mention 解析容错：** 正则 `@[\w.-]+:[\w.-]+` + 容错（无 domain → 查映射表；尾部空格 → trim；@后空格 → 兼容）+ 匹配失败不触发。

**风险：** LLM 生成的 @mention 格式可能不标准。OpenClaw 的 Matrix plugin 是成熟代码处理了各种边界情况，bridge 是新写的容易解析错。解析错 → 协作断裂。

#### 4.1.3 调cimicode 无状态服务 submit_turn

**设计：** bridge 检测到 @自己 → 构造 input（两段式消息 + 协作上下文）→ 调cimicode 无状态服务 `submit_turn(session_id, input, ...)`。cimicode 无状态服务调度 CimiCode Replica 执行，返回事件流。

**input 构造：**

- `input.text`：两段式消息（history buffer + current message，如果仍需 bridge 维护）
- `input.system`：协作上下文（coordinator/room/workers，从 env 读）+ HEARTBEAT.md 内容（如果是 heartbeat 触发）
- `file_ids`：如果需要传入共享文件引用（见 §4.7 跨 Session 文件协作）
- `required_skill_ids`：如果需要指定 Skill

**cimicode 无状态服务返回事件流：** 结构化运行事件（带 event_seq），bridge 消费后转发 Matrix。

**风险：**

1. **cimicode 无状态服务 API 是否支持 input 里的 system 指令**：新版方案的 `submit_turn(session_id, input, file_ids, required_skill_ids)` 没有 system 字段。**需和cimicode 无状态服务团队确认是否支持 per-Turn system 指令。** 如果不支持，协作上下文怎么传到 CimiCode 是未解决问题。

2. **cimicode 无状态服务是否支持程序化调用（非 UI 触发）**：新版方案是为首页 UI 设计的。bridge 是程序化调用cimicode 无状态服务 API。cimicode 无状态服务是否接受程序化 Session 创建和 Turn 提交？鉴权怎么做（bridge 用什么身份调cimicode 无状态服务）？

3. **cimicode 无状态服务调度队列公平性**：team 的多个 agent 同时 submit_turn，cimicode 无状态服务全局调度队列怎么公平分配？team 的 agent 间有协作依赖（A 完成后 B 才开始），cimicode 无状态服务是否理解这种依赖？cimicode 无状态服务不理解——cimicode 无状态服务只管 Session FIFO（同一 Session 串行），跨 Session 的依赖在 bridge 层通过 @mention 协作处理。

4. **cimicode 无状态服务 Turn 超时/interrupted 后通知 bridge**：CimiCode/cimicode 无状态服务失联 → Turn interrupted。bridge 怎么知道？cimicode 无状态服务是否主动通知 bridge？还是 bridge 轮询 Turn 状态？**事件流应该是cimicode 无状态服务主动推给 bridge 的，但如果cimicode 无状态服务自己挂了谁来通知 bridge？** bridge 需要检测cimicode 无状态服务事件流断开 → 发 Matrix "处理中断，请重新 @我"。

5. **bridge 消费事件流的方式**：cimicode 无状态服务返回的是结构化运行事件（带 event_seq），不是 cimicode 的 SSE（message.part.delta 等）。bridge 要把这些事件转成 Matrix 消息——是等完整响应再发一条，还是增量发多条？**需确认cimicode 无状态服务事件流格式和消费方式。**

6. **cimicode返回格式与elementweb（Matrix）格式转换：重点！**

#### 4.1.4 事件流转发 Matrix

**设计：** bridge 消费cimicode 无状态服务事件流 → 累积响应 → 以 agent bot 身份发回 Matrix room。

- **NO_REPLY 检测**：响应文本是 NO_REPLY → 不发 Matrix（静默）
- **@mention 解析**：响应文本里 @了别的 agent → 按 peer-mentions 规则触发
- **流式发送**：cimicode 无状态服务事件流是流式的，bridge 是否实时转发 Matrix？Matrix 不支持消息流式编辑——要么等完整响应再发一条，要么发多条增量（消息碎片化）。**需确认cimicode 无状态服务事件流是否支持 bridge 实时转发，还是 bridge 等完整响应。**

**风险：**

1. **消息碎片化**：等完整响应再发，用户等待时间长（几十秒到几分钟无反馈）。OpenClaw 支持 `streaming: partial`（`worker-openclaw.json.tmpl:34`）。bridge 要复刻流式行为，但 Matrix 协议不支持消息流式编辑。

2. **cimicode 无状态服务事件流格式未明确**：新版方案只说"结构化运行事件（带 event_seq）"，具体格式（是 SSE？WebSocket？HTTP chunked？）未定义。bridge 消费方式取决于格式。

3. **NO_REPLY 检测的准确性**：文本匹配（NO_REPLY 精确匹配 or trim）。如果 LLM 输出了 "NO_REPLY\n" 或 " NO_REPLY "，bridge 要正确识别。

#### 4.1.5 两段式群聊视野

**OpenClaw 的 Matrix 插件不只是"广播接收+过滤@自己+发给 LLM"，它还做了上下文管理。** 完整消息处理链路：

```
Matrix /sync 收到 room 里的消息
  │
  ├─ 消息不 @自己？
  │    └─ 不触发 LLM，但存入 per-room history buffer（≤50 条 FIFO）
  │       （buffer 格式：{sender, body, messageId, media_parts}）
  │
  └─ 消息 @自己？
       │
       ├─ 过滤：requireMention？groupAllowFrom 白名单？
       │    └─ 不通过 → 丢弃（连 buffer 都不进）
       │
       └─ 通过 → 取该 room 的 history buffer
            │
            ├─ 格式化为两段式文本：
            │   [Chat messages since your last reply - for context]
            │   senderA: 消息内容 [id:xxx]
            │   senderB: 消息内容 [id:yyy]
            │
            │   [Current message - respond to this]
            │   @agentA 帮我写个函数
            │
            ├─ 把两段式文本作为 LLM 的 user 消息输入
            │
            └─ LLM 回复后 → 清空该 room 的 history buffer
```

**为什么需要两段式：** agent 在群里不是一直在"听"的——它只在被 @mention 时才被唤醒。但唤醒时它需要知道"我沉默期间群里发生了什么"，否则会问"你们在讨论什么？"。history 段让 agent 有群聊视野，但**只作为只读上下文**——agent 不能基于 history 段去 @mention 别人（防死循环，`worker-agent/AGENTS.md:20`）。

**代码参考：** `copaw/src/matrix/channel.py:1054-1129`（`_record_history`/`_build_history_prefix`/`_apply_history_to_parts`/`_clear_history`），CoPaw 是 OpenClaw 的 Python 替代 runtime，行为对齐。

**bridge 方案：** 精确复刻这个机制：

1. 收到不 @自己的 Matrix 消息 → 存入 per-room history buffer（{sender, body, messageId}，≤50 条 FIFO）
2. 收到 @自己的消息 → 取该 room 的 buffer → 格式化为两段式文本 → 拼进 `submit_turn` 的 `input.text`
3. cimicode 无状态服务返回响应后 → 清空该 room 的 buffer

**视角处理：** input.text 里的两段式文本是纯文本，CimiCode 把它当 user 消息处理。canonical 里保存的是这个纯文本（两段式格式），不是 per-speaker 的结构化消息。这和 OpenClaw 的处理方式一致（OpenClaw 的 history buffer 也是纯文本格式 `sender: body`）。

**风险：**

1. **history buffer 重建不完整**：bridge pod 挂了 buffer 丢失，重启后从 Matrix room timeline 拉最近消息重建，但 Matrix 默认只保留有限历史，且重建是事后拉取不是实时维护。agent 可能失去部分群聊视野。

2. **buffer 精确复刻**：OpenClaw 的 history buffer 包含 buffer 管理、FIFO 淘汰、回复后清空、vision 图片处理等。bridge 要精确复刻这些行为——任何偏差都可能导致 agent 的群聊视野不准确，影响协作质量。**需参考 CoPaw channel.py 精确实现。**

#### 4.1.6 heartbeat（Leader）

**heartbeat 是 Leader 的"主动巡查"机制——让 Leader 不需要被 @就能定期行动。**

正常情况下 agent 是被动的——只在被 @mention 时才被唤醒干活。但 Team Leader 作为协调者，需要**主动定期检查团队状态**：

- 检查各 Worker 的任务进度（谁卡住了？谁该被催？）
- 整理任务状态（更新 DAG、标记完成的任务）
- 评估团队容量（是否有空闲 Worker？是否需要创建新 Worker？）
- 更新 memory（记录今天发生了什么）
- 向上报告（如果 requester 在等待）

**OpenClaw 怎么做的：**

- OpenClaw runtime 内部 cron 定时器（每 15m，`worker-openclaw.json.tmpl:103-106` 配置 `heartbeat.every`）
- 定时触发时，runtime 自动读取 workspace 里的 `HEARTBEAT.md`，把内容作为 LLM 输入
- LLM 根据 HEARTBEAT.md 的检查清单执行巡查（检查 Worker 状态、催进度、整理任务）
- `HEARTBEAT.md` 是 seed-only——Leader 可以自己编辑检查清单

**HEARTBEAT.md 内容示例**（`team-leader-agent/HEARTBEAT.md`）：

```markdown
# Heartbeat Checklist
- [ ] 检查所有 Worker 的任务状态
- [ ] 对超时未完成的任务 @mention 催进度
- [ ] 更新 state.json 中的任务状态
- [ ] 如果有阻塞任务，向 requester 报告
- [ ] 更新 memory/YYYY-MM-DD.md
```

**bridge 方案：**

- Leader 的 bridge pod 内定时器（每 15m）
- 从 S3/对象存储读 HEARTBEAT.md 最新版本
- 调 cimicode 无状态服务 `submit_turn`，input 含 HEARTBEAT.md 内容
- cimicode 无状态服务返回响应后 → bridge 发回 Matrix room
- 如果 Leader 在沙箱里修改了 HEARTBEAT.md → publish_artifact 发布新版本 → bridge 下次读最新版本

**风险：**

1. **heartbeat 与正常 @的并发**：heartbeat 正在跑时有人 @了 Leader。cimicode 无状态服务有 Session FIFO（同一 Session 串行）——heartbeat 和正常 @是同一 Session（Leader 的 Session），所以自动串行。**这是新版方案的优势**——Session FIFO 天然解决了并发控制问题。

2. **heartbeat 触发的 @worker**：heartbeat 的响应里如果 @了 worker，bridge 解析并触发对应 worker 的 submit_turn。和正常 @mention 处理一样，但触发源是定时器而非外部 @。

3. **HEARTBEAT.md 怎么注入**：如果 cimicode 无状态服务 submit_turn 支持 system 指令 → bridge 拼 HEARTBEAT.md 内容进 system。如果不支持 → 需要其他注入渠道。**需确认 cimicode 无状态服务 API（能力清单 #4）。**

4. **heartbeat 基础负载**：每 15m 每个 Leader 一次 cimicode 无状态服务调用。T 个 team = 每 15m T 次调用。cimicode 无状态服务集群要承受这个基础负载。

#### 4.1.7 Session 管理与映射

**核心关系：一个 session_id 对应一个 worker（agent）。** bridge 为每个 agent 调 cimicode 无状态服务 `create_expert_session` 创建独立 Session，之后每次 `submit_turn` 都传这个 session_id。

**worker 存活期间用同一个 session。** Session 生命周期：

| 阶段     | 谁做                                      | 时机                      | 说明                                                         |
| -------- | ----------------------------------------- | ------------------------- | ------------------------------------------------------------ |
| 创建     | bridge 调 `create_expert_session`         | team 创建时               | 为每个 agent 创建一个 Session，获取 session_id               |
| 使用     | bridge 每次 `submit_turn` 传 session_id   | 每次 @mention / heartbeat | worker 存活期间用同一个 session_id                           |
| 日重置？ | ⚠️ 需确认                                  | 每天 04:00？              | OpenClaw 有 04:00 重置，新版 cimicode 方案没有明确日重置机制 |
| 关闭     | bridge 调 cimicode 无状态服务关闭 Session | team 解散时               | 释放 Session + drain Sandbox                                 |

**日重置问题：** OpenClaw 的 session 每天 04:00 重置（`worker-openclaw.json.tmpl:113-118`）——每天新的对话上下文，旧对话不带入。新版 cimicode 方案的 canonical 在 PG 持久化，Session 不会自动过期。如果不重置，canonical 越来越长，CimiCode 从 PG 读取重建上下文会慢/超 token 限制。

| 方式        | 做法                                | 优点             | 缺点                                        |
| ----------- | ----------------------------------- | ---------------- | ------------------------------------------- |
| A. 不日重置 | worker 存活期间一直用同一个 session | 简单             | canonical 越来越长，依赖 compaction         |
| B. 日重置   | bridge 每天 04:00 创建新 session    | 和 OpenClaw 一致 | 多天历史断了；bridge 要管理 session_id 切换 |

**⚠️ 需和 cimicode 无状态服务团队确认：** Session 是否支持日重置？canonical 过长时 compaction 是否有效？是否支持按时间过滤 canonical？

**session_id ↔ agent 映射存储：** bridge 持久化 `agent_id → session_id` 映射（Redis，key: `session:{agentId}`，TTL = team 生命周期），pod 重启后恢复。

**bridge 需要维护的状态：**

| 映射                           | 存哪        | 用途                           |
| ------------------------------ | ----------- | ------------------------------ |
| agent_id → session_id          | Redis       | 每次 submit_turn 传 session_id |
| agent_id → Matrix since token  | Redis       | Matrix /sync 增量同步位置      |
| agent_id → Matrix access token | Redis       | Matrix login token 缓存        |
| per-room history buffer        | bridge 内存 | 两段式群聊视野（§4.1.5）       |

**⚠️ 风险：**

1. **Redis 挂了映射丢失**——bridge 重启后不知道 session_id。需 Redis HA 或恢复机制。

2. **日重置后 session_id 切换**——方案 B 下 bridge 每天 04:00 创建新 session 更新 Redis。如果创建失败，agent 用旧 session_id 继续（canonical 继续增长）或等待重试。

3. **Session 过期**——如果 cimicode 无状态服务 Session 有 TTL，bridge 需检测并重新创建。

#### 4.1.8 启动生命周期

**设计：** 分阶段等待：Phase 1 等 Matrix → Phase 2 连cimicode 无状态服务 → Phase 3 report-ready → Phase 4 Leader 启 heartbeat → Phase 5 监听。

**风险：**

1. **ready 判定语义变更**：ready = "bridge 连 Matrix + 连cimicode 无状态服务"。controller 的 `waitForTeamMembersAndSetDisplayNames`（`TeamService.java:911`）等 ready 后设 displayName。ready 标准变了，controller 联动要改。

2. **displayName 设置时机**：bridge 自己设还是 controller 设？什么时机？如果 bridge 设了 controller 又设可能冲突。

3. **cimicode 无状态服务未就绪**：bridge 启动时cimicode 无状态服务还没准备好 → 重试连接cimicode 无状态服务。cimicode 无状态服务就绪检测怎么实现（HTTP health check？）。

#### 4.1.9 协作上下文注入

**为什么需要 teams 侧注入：** cimicode 无状态服务不知道 team/Leader/Worker 概念——它只管 Session+Turn。但 agent 需要知道自己在 team 里的角色和协作关系。

OpenClaw 里这些协作上下文是 operator 在创建 agent 时通过 `InjectCoordinationContext`（`coordination.go:37-121`）注入到 AGENTS.md 的：

**Leader 注入的协作上下文**（`coordination.go:58-92`）：

```markdown
## Coordination
- Upstream coordinator: @manager:{domain}（Manager）——你从 Manager 接收任务
- Team Admin: {adminMatrixId}——可以在 team 内分配任务和做决策
- Team: {teamName}
- Team Room: {teamRoomId}——在这里 @mention workers 分配任务
- Leader DM: {leaderDmRoomId}——Team Admin 在这里和你沟通
- Heartbeat interval: {heartbeatEvery}
- Team Workers:
  - @workerA:{domain}——Room: {roomId}
  - @workerB:{domain}——Room: {roomId}
- 你分解任务、分配给 workers、@mention workers in Team Room
- 使用 team-state.json 作为任务活动真相源
- 你决定 wake/sleep team workers
- 报告结果给 Manager 或 Team Admin
```

**Worker 注入的协作上下文**（`coordination.go:94-111`）：

```markdown
## Coordination
- Coordinator: @{teamLeaderName}:{domain}（Team Leader of {teamName}）
- Team Admin: {adminMatrixId}（在 team 内有 admin 权限）
- 报告任务完成/阻塞/问题给 coordinator
- 响应 coordinator/Team Admin/Admin 的 @mention
- 不要直接 @mention Manager——所有通信通过 Team Leader
```

**为什么不能 cimicode 侧做：**

| 原因                         | 说明                                                         |
| ---------------------------- | ------------------------------------------------------------ |
| cimicode 不知道 team 拓扑    | cimicode 无状态服务只管 Session+Turn，不知道谁是 Leader、谁是 Worker、coordinator 是谁 |
| 协作上下文是 per-instance 的 | 每个 team 的每个 agent 有不同的 coordinator/room/workers。cimicode 的 Nacos 是全局的，Skill Catalog 也是全局的 |
| 协作上下文含 Matrix 身份信息 | `@leader:domain`、`!room-id:domain` 等——cimicode 不连 Matrix |
| 协作上下文含团队编排逻辑     | "你分解任务分配给 workers"、"不要直接 @mention Manager"——这是 team 的协作规则 |

**bridge 方案：** 从环境变量读协作上下文（operator 创建 bridge pod 时写入），拼进 `submit_turn` 的 `input.system` 字段传入。

**协作上下文来源：** operator 创建 bridge pod 时写进环境变量：

```
COORDINATION_ROLE=worker（或 team_leader）
COORDINATION_LEADER=@leader:domain
COORDINATION_TEAM=team-name
COORDINATION_ROOM=!room-id:domain
COORDINATION_ADMIN=@admin:domain
COORDINATION_WORKERS=@workerA:domain,@workerB:domain
COORDINATION_HEARTBEAT_EVERY=15m
```

**CimiCode 最终看到**：Nacos opencode.json 的 agent.prompt + instruction.ts 读的 AGENTS.md + bridge 传的 system（协作上下文）。等效于 OpenClaw 的 AGENTS.md 三层合并。

**风险：**

1. **cimicode 无状态服务 API 是否支持 per-Turn system 指令**：`submit_turn` 的 input 是否支持 system 部分？**这是最大的未确认问题——如果不支持，协作上下文没有注入渠道。**（能力清单 #4）

2. **协作上下文变更**：team 成员加/删 → 协作上下文变了 → 环境变量要重启 pod 更新。OpenClaw 通过 operator reconcile + file-sync 动态更新 AGENTS.md。bridge 模型下要么重启 pod，要么 bridge 从 API 动态查。

3. **AGENTS.md 加载渠道**：新版 CimiCode 无 PVC 无共享卷。instruction.ts 的 directory 指向哪里？**需和 cimicode 团队确认 instruction.ts 在新版方案里怎么工作。**（能力清单 #10）

### 4.2 cimicode 无状态服务 team 场景适配

**问题：** cimicode 无状态服务是为首页单用户 chat 设计的。team 场景是多 agent 群聊——N 个 agent 各自一个 Session，agent 间通过 @mention 协作。

**teams 侧不实现 cimicode 无状态服务，但需要确认以下适配点（对应 §3.2 能力清单）：**

1. **程序化 Session 创建**：cimicode 无状态服务的 `create_expert_session(owner_id)` 是否接受程序化调用（不是 UI 触发）？owner_id 用什么（agent 的 Matrix ID？team-agent 组合 ID？）？鉴权怎么做？**teams 侧 controller 或 bridge 需要程序化创建 Session。**

2. **多 Session 并发**：一个 team 有 N 个 agent → N 个 cimicode 无状态服务 Session 同时活跃。调度队列和执行槽位怎么处理多 Session 并发？team 的 N 个 agent 同时 submit_turn，全局调度队列公平分配——但 team 的 agent 间有协作依赖（A 完成后 B 才开始），这种依赖在 bridge 层通过 @mention 处理，cimicode 无状态服务不理解也不需要理解。

3. **事件流消费方式**：cimicode 无状态服务返回结构化运行事件给谁？在首页场景是给 UI。team 场景是给 bridge。bridge 怎么消费事件流（SSE？WebSocket？HTTP chunked？轮询？）？**需确认事件流 API 格式。**

4. **per-Turn system 指令**：cimicode 无状态服务 submit_turn 的 input 是否支持 system 指令字段？如果不支持，协作上下文没有注入渠道。**这是关键适配点（能力清单 #4）。**

5. **Turn interrupted 通知**：CimiCode/cimicode 无状态服务失联 → Turn interrupted。cimicode 无状态服务怎么通知 bridge？如果cimicode 无状态服务自己挂了谁来通知？bridge 需要检测事件流断开 → 发 Matrix 中断通知。**需确认通知机制（能力清单 #8）。**

6. **跨 Session 文件共享**：team 侧 agent 间靠文件协作（spec.md/result.md），cimicode 无状态服务的文件管理是 per-Session 的。**需确认是否提供跨 Session 文件共享机制（能力清单 #9）。** 见 §4.7 详细分析。

7. **配额适配**：cimicode 无状态服务有用户配额（每用户最大活跃 Sandbox 数等）。team 场景下每个 agent 是一个"用户"（一个 Session + 一个 Sandbox）。一个 team N 个 agent 就占 N 个 Sandbox。配额怎么设？

### 4.3 CimiCode Replica Pool（cimicode 无状态服务提供）

**角色：** 无状态 LLM 推理引擎，从统一会话管理 PG 读取 canonical 重建上下文。

**team 场景适配：** 每个 agent 一个 Session → CimiCode 从该 Session 的 canonical 读取历史重建。CimiCode 不需要知道 team/Leader/Worker 概念——它只管 Session + Turn。

**Skills：** 从 Skill Catalog 按需加载。team 侧的 builtin skills（project-participation/task-progress）需发布到 Catalog。

**配置：** 从 Nacos opencode.json 加载。team 侧的 AGENTS.md/SOUL.md 怎么加载？**需确认 instruction.ts 在新版方案里怎么工作（能力清单 #10）。**

**MCP：** opencode.json 的 mcp 配置走 Nacos。team 侧不用管 MCP 配置。

### 4.4 Sandbox Gateway（cimicode 无状态服务提供）

**角色：** 受控入口，按需创建/TTL/drain/LRU/generation 重建。

**team 场景适配：** 每个 agent 一个 Session → 一个 Sandbox。Sandbox 工作现场是该 agent 的私有工作区。agent 在 Sandbox 里干活（读 spec.md、写 result.md、写 outputs/）。

**文件恢复：** generation 重建时恢复最新产物（/artifacts/）。但 team 侧的 shared/ 文件（spec.md/result.md）不是该 agent 的产物——它们是跨 agent 共享的。**跨 Session 文件协作见 §4.7。**

### 4.5 统一会话管理 PG（cimicode 无状态服务提供）

**角色：** canonical 真相源，CimiCode 从 PG 读取重建上下文。

**team 场景适配：** 每个 agent 一个 Session，该 Session 的 canonical 是该 agent 的对话历史。bridge 提交 Turn → cimicode 无状态服务写入 canonical → CimiCode 读取 canonical 重建。

**风险：**

1. **canonical 是 per-Session 的，不是 per-room 的**：两段式群聊视野（"群里别人说的话"）不在 canonical 里——它在 bridge 的 history buffer 里。bridge 维护 buffer，格式化为两段式注入 input.text。cimicode 无状态服务把 input.text 写入 canonical（作为该 agent 的 user 消息）。CimiCode 读取 canonical 时看到的是两段式文本（不是 per-speaker 结构化）。这和 OpenClaw 的处理方式一致。

2. **canonical 不保存逐 token delta**：只保存完整 Turn（用户输入 + 结构化输出）。流式输出在事件流里，不在 canonical 里。bridge 消费事件流做流式转发，canonical 只存最终结果。

### 4.6 文件管理与 S3

**角色：** 附件 + 产物元数据，Artifact Manifest（逻辑产物 + 不可变版本），S3 签名 URL 下载。

**team 场景适配：** agent 在 Sandbox 里工作，产物通过 `publish_artifact` 显式发布到 S3。bridge 不参与文件管理（不像 V1 要做文件 sync）。

**但 team 侧的文件协作模式不同：**

- 首页场景：用户上传附件 → CimiCode 读取 → CimiCode 发布产物 → 用户下载
- team 场景：Leader 写 spec.md → Worker 读 spec.md → Worker 写 result.md → Leader 读 result.md

**这是跨 agent 跨 Session 的文件协作**，不是 per-Session 的。见 §4.7。

### 4.7 跨 Session 文件协作（team 特有问题）

**问题：** OpenClaw 的文件协作是核心模式——Leader 写 spec.md 到 shared/，Worker 拉来读，干完写 result.md 推回。新版 cimicode 的文件管理是 per-Session 的（每个 agent 一个 Sandbox + publish_artifact 发布到 S3）。

**OpenClaw 的文件协作流：**

```
Leader 写 spec.md → mc mirror 推 MinIO shared/tasks/{task-id}/
  → Leader @mention Worker
  → Worker hiclaw-sync 拉 spec.md 到本地
  → Worker 干活 → 写 result.md
  → Worker mc mirror 推 MinIO shared/tasks/{task-id}/
  → Worker @mention Leader(TASK_COMPLETED)
  → Leader hiclaw-sync 拉 result.md
```

**新版 cimicode 的文件管理：**

- 每个 agent 一个 Session → 一个 Sandbox（per-Session 的工作现场）
- 产物通过 `publish_artifact` 显式发布到 S3（per-Session 的 artifact）
- Sandbox 重建时恢复最新产物（per-Session 的）

**问题：Leader 的 spec.md 在 Leader 的 Sandbox/S3 里，Worker 怎么读到？**

**推荐方案：利用 publish_artifact + bridge 协调 + cimicode 无状态服务跨 Session API**

```
Leader 的 CimiCode 写 spec.md 到 Leader 的 Sandbox
  → publish_artifact 发布到 S3（artifact_id=spec-task-xxx）
  → Leader bridge 发 Matrix @mention Worker
    （消息里包含 artifact 引用："spec.md 已发布，artifact_id=spec-task-xxx"）
  → Worker bridge 收到 @mention
  → Worker bridge 调 cimicode 无状态服务 API 把 artifact 拉到 Worker 的 Sandbox
    （如果提供跨 Session artifact 读取 API：download_artifact(artifact_id)）
    （如果不提供：bridge 从 S3 直接拉，需要 S3 访问权限）
  → Worker 的 CimiCode 在 Worker 的 Sandbox 里读 spec.md
  → Worker 干完写 result.md → publish_artifact 发布
  → Worker bridge 发 Matrix @mention Leader(TASK_COMPLETED)
  → Leader bridge 收到 → 调 API 拉 Worker 的 result.md
```

**这个方案的前提条件：**

| #    | 前提                                                         | 如果不满足                                                   |
| ---- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| 1    | cimicode 无状态服务提供跨 Session artifact 读取 API（`download_artifact(artifact_id)` 或 `list_session_artifacts(session_id)` 不限于自己的 session） | bridge 要自己从 S3 拉（需 S3 访问权限），增加 bridge 复杂度  |
| 2    | cimicode 无状态服务提供把文件 staging 到指定 Sandbox 的 API（把 S3 文件拉到 Worker 的 Sandbox 里） | Worker 的 CimiCode 怎么从 S3 拉文件到 Worker 的 Sandbox？通过 Sandbox Gateway 的 ensure_and_exec？还是需要专门的 staging API？ |
| 3    | artifact_id 怎么传递                                         | 通过 @mention 消息内容（Leader 说"spec 已发布，artifact_id=xxx"）？还是通过协作上下文？ |
| 4    | cimicode 无状态服务的 `submit_turn` 的 `file_ids` 参数能否引用别的 Session 的 artifact | 如果能引用 → bridge 传 file_ids。如果不能 → 要先 staging 到 Worker 的 Sandbox |

**其他备选方案：**

| 方案                                           | 做法                                                         | 优点                        | 缺点                               |
| ---------------------------------------------- | ------------------------------------------------------------ | --------------------------- | ---------------------------------- |
| B. bridge 中转文件                             | Leader bridge 从 Leader Sandbox 拉产物 → 推到对象存储 shared/ 区域；Worker bridge 从 shared/ 拉 → 推到 Worker Sandbox | 和 OpenClaw 模式一致        | bridge 又要做文件 sync，增加复杂度 |
| C. cimicode 无状态服务提供 team 级共享文件区域 | cimicode 无状态服务设计 team 级共享文件区域，各 agent 的 Session 都能读写 | cimicode 无状态服务统一管理 | 需 cimicode 无状态服务改造         |

**⚠️ 这是最需要和 cimicode 无状态服务团队一起设计的对接点。** 如果 cimicode 无状态服务不提供跨 Session artifact 读取 + staging 能力，bridge 又要回到自己做文件 sync 的模式（从 S3 拉到 Worker Sandbox），增加 bridge 复杂度。**对应能力清单 #9。**

### 4.8 Skills / Catalog（cimicode 无状态服务提供，teams 侧不参与运行时）

**Skills 的加载/管理/执行完全由 cimicode 无状态服务负责：**

- Skill Catalog 元数据 + S3 Package Store 不可变包 + Resolver 按需加载/物化
- CimiCode 读 SKILL.md 指令，Sandbox 物化 scripts/templates/assets
- Skill 选择（UI 显式选择 or CimiCode 自动选择）
- 这一切都是 cimicode 无状态服务内部完成，teams 侧 bridge 不参与

**teams 侧唯一需要做的一次性工作：** 把 OpenClaw 的 team 侧 skills 改写后发布到 Skill Catalog。

| OpenClaw skill                                               | 处理             | 原因                                                         |
| ------------------------------------------------------------ | ---------------- | ------------------------------------------------------------ |
| file-sync（`hiclaw-sync`）                                   | **废弃，不发布** | cimicode 无状态服务自己管文件，不需要 hiclaw-sync            |
| find-skills                                                  | **废弃，不发布** | CimiCode 有自己的 Skill 加载机制                             |
| mcporter                                                     | **废弃，不发布** | CimiCode 有原生 MCP（Nacos opencode.json）                   |
| project-participation                                        | **改写后发布**   | 纯协议文本（Phase handoff/DAG），但去掉 `hiclaw-sync`/`mc` 引用 |
| task-progress                                                | **直接发布**     | 纯格式说明（写 progress/YYYY-MM-DD.md），不依赖任何 CLI      |
| Leader 的 skills（team-coordination/organization/project-management/task-management/file-sharing/communication） | **改写后发布**   | 需逐一检查是否依赖 `mc`/`hiclaw-sync`/`hiclaw` CLI，依赖的去掉 |

**这是一次性的 skill 发布工作，不是 bridge 运行时职责。** bridge 运行时不管 Skills。

### 4.9 HiClaw Operator

**调谐模型不变**（每 member 1 pod），但调谐步骤要改：

| 调谐步骤                  | 改什么                                                       |
| ------------------------- | ------------------------------------------------------------ |
| ReconcileMemberInfra      | Matrix bot provision 保留；gateway consumer 不需要（Higress 鉴权关了） |
| ReconcileMemberConfig     | 不生成 openclaw.json → 生成 bridge 配置（cimicode 无状态服务 URL/协作上下文 env）；不生成 mcporter → 不需要；AGENTS.md/SOUL.md 不推 OSS → 发布到 Catalog/Nacos？或 bridge 通过 system 注入？ |
| ReconcileMemberContainer  | env 换 bridge 专用（cimicode 无状态服务 URL/协作上下文/Matrix 凭证）；image 换 bridge 镜像 |
| summarizeBackendReadiness | ready = bridge 连 Matrix + 连cimicode 无状态服务（语义变更） |
| handleDelete              | 删 pod + 清 Matrix bot/room + 调cimicode 无状态服务关闭 Session |
| EnvBuilder                | 重写（bridge 专用 env）                                      |
| agentconfig.Generator     | 新写 GenerateBridgeConfig                                    |
| 启动脚本                  | 新写 bridge 启动脚本                                         |
| 双 runtime 分流           | 每步 if/else + 兼容验证                                      |

### 4.10 原有脚本与工具迁移

OpenClaw worker pod 内有一套完整的脚本和工具链，替换后这些都不存在了，需要明确哪些废弃、哪些由谁替代。

#### 4.10.1 worker-entrypoint.sh（启动脚本）

OpenClaw 的启动脚本（`worker-entrypoint.sh`，407 行）做了以下事情：

| 步骤                               | OpenClaw 做什么                                              | 替换后谁做                                                   | 处理                                                         |
| ---------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------ |
| Step 1: 拉配置                     | 从 MinIO 拉配置（openclaw.json/SOUL.md/AGENTS.md/skills/）   | cimicode 无状态服务（Nacos/Skill Catalog）；bridge（协作上下文 env） | **operator 新写 bridge 启动脚本**，只做：等 Matrix 就绪 + 连 cimicode 无状态服务 + report-ready |
| Step 2: 写本地配置                 | merge-openclaw-config.sh 合并 openclaw.json                  | 不需要——cimicode 无状态服务从 Nacos 读配置                   | 废弃                                                         |
| Step 3: 起 file sync               | 本地 fs ↔ MinIO 双向 sync（Local→Remote 变更触发推送 + Remote→Local 按需拉取 + 5 分钟兜底） | cimicode 无状态服务自己管文件（Sandbox PVC + publish_artifact + S3） | 废弃，bridge 不做文件 sync                                   |
| Step 4: 配置 mcporter              | mcporter-servers.json 配置 MCP                               | cimicode 无状态服务（Nacos opencode.json mcp 配置）          | 废弃                                                         |
| Step 5a: 清理 session 锁           | 清理 .jsonl.lock 孤儿锁                                      | 不需要——cimicode 无状态服务无本地 session                    | 废弃                                                         |
| Step 5b: 清 Matrix crypto          | rm crypto storage 重新协商 E2EE                              | bridge 自己做（Matrix SDK 初始化时处理）                     | bridge 启动脚本处理                                          |
| Step 5b: 重新 login Matrix         | 用密码重新 login 获取新 token + device_id                    | bridge 自己做                                                | bridge 启动脚本处理                                          |
| Step 5c: readiness reporter        | 等 gateway health → hiclaw worker report-ready               | bridge 自己 report-ready                                     | bridge 启动脚本处理                                          |
| Step 5d: exec openclaw gateway run | exec openclaw gateway run（永不退出）                        | 不存在——bridge 是自己的进程                                  | 废弃                                                         |

**总结：** worker-entrypoint.sh 的 8 个步骤，废弃 5 个（拉配置/写配置/file sync/mcporter/session 锁），bridge 启动脚本自己做 3 个（清 Matrix crypto/重新 login/report-ready）。**operator 需新写 bridge 启动脚本。**

#### 4.10.2 mc（MinIO Client）

OpenClaw worker pod 里有 `mc` CLI（`worker/Dockerfile:28`），用于文件 sync。

| 用途                                    | 替换后                                 | 谁负责              |
| --------------------------------------- | -------------------------------------- | ------------------- |
| `mc mirror` 推 MinIO                    | cimicode 无状态服务的 publish_artifact | cimicode 无状态服务 |
| `mc cp` 拉 MinIO                        | cimicode 无状态服务的文件 staging/恢复 | cimicode 无状态服务 |
| `mc cat` 读 MinIO 文件                  | cimicode 无状态服务从 S3 读            | cimicode 无状态服务 |
| AGENTS.md 里的 `mc mirror`/`mc cp` 指令 | 改写成"产物通过 publish_artifact 发布" | 指令翻译（§4.11）   |

**总结：** `mc` CLI 完全废弃。文件操作由 cimicode 无状态服务负责。AGENTS.md 里的 `mc` 指令要改写。

#### 4.10.3 hiclaw CLI

OpenClaw worker pod 里有 `hiclaw` CLI（`worker/Dockerfile:31`，从 controller 镜像 COPY）。

| 用途                         | OpenClaw 怎么用                     | 替换后谁做                                              | 处理                |
| ---------------------------- | ----------------------------------- | ------------------------------------------------------- | ------------------- |
| `hiclaw worker report-ready` | worker-entrypoint.sh:398 报告 ready | bridge 自己调 cimicode 无状态服务或 HiClaw operator API | bridge 启动脚本     |
| `hiclaw get workers`         | Leader 查 Worker 状态               | **需替代方案**                                          | 见 §4.10.5          |
| `hiclaw create worker`       | Leader 创建 Worker                  | controller API（不变）                                  | controller          |
| `hiclaw delete worker`       | Leader 删除 Worker                  | controller API（不变）                                  | controller          |
| `hiclaw get teams`           | 查 Team 状态                        | controller API（不变）                                  | controller          |
| `push-worker-skills.sh`      | 推 skills 到 OSS                    | cimicode 无状态服务 Skill Catalog                       | 一次性发布，见 §4.8 |

**总结：** 大部分 `hiclab` CLI 功能由 controller API 或 bridge 替代。**但 Leader 在 cimicode 模型下没有 `hiclaw` CLI 可用**——Leader 的 CimiCode 在 Sandbox 里执行，Sandbox 里没有 `hiclaw` CLI。Leader 需要查 Worker 状态/读 Worker 产物时怎么办？见 §4.10.5。

#### 4.10.4 mcporter（MCP 工具 CLI）

OpenClaw 用 `mcporter` CLI 调 MCP Server（`mcporter-servers.json` 配置，gateway key-auth）。

| 用途                       | 替换后                                                      | 谁负责              |
| -------------------------- | ----------------------------------------------------------- | ------------------- |
| 调 MCP Server 工具         | cimicode 无状态服务原生 MCP（Nacos opencode.json mcp 配置） | cimicode 无状态服务 |
| mcporter-servers.json 配置 | Nacos opencode.json 的 mcp 字段                             | cimicode 无状态服务 |
| GenerateMcporterConfig     | 不需要——cimicode 无状态服务从 Nacos 读 mcp 配置             | 废弃                |

**总结：** mcporter 完全废弃。MCP 由 cimicode 无状态服务原生支持。AGENTS.md 里的 mcporter 指令要改写。

#### 4.10.5 Leader hiclaw CLI 替代方案

**这是最重要的替代方案设计。** Leader 协调时要查 Worker 状态、读 Worker 产物，原来用 `hiclaw get workers` + `hiclaw-sync`，cimicode 模型下这些都没有。

**Leader 在 cimicode 模型下需要的能力：**

| 能力                | OpenClaw 怎么做                        | cimicode 模型下怎么替代                                      |
| ------------------- | -------------------------------------- | ------------------------------------------------------------ |
| 查 Worker 列表/状态 | `hiclaw get workers -o json`           | bridge 从协作上下文 env 读 Worker 列表（operator 创建时写入）；Worker 状态通过 heartbeat 时检查（Leader 的 CimiCode 在 heartbeat 时 @mention 各 Worker 催进度） |
| 读 Worker 产物      | `hiclaw-sync` 拉 Worker 的 result.md   | 跨 Session 文件协作（§4.7）——Worker publish_artifact 发布 result.md → Leader bridge 调 cimicode 无状态服务 API 拉 |
| 创建/删除 Worker    | `hiclaw create worker`/`delete worker` | Leader 的 CimiCode 通过 MCP 工具调 controller API？或 bridge 提供 HTTP API 给 Leader 的 CimiCode 调？ |
| 读 team-state.json  | Leader 直接读本地文件                  | team-state.json 在 Leader 的 Sandbox 里？还是 bridge 维护？  |

**推荐方案：bridge 提供 HTTP API + heartbeat 注入 Worker 状态**

1. **Worker 状态查询**：bridge 维护该 team 的 Worker 状态（各 Worker 的 bridge 可定期 report 状态，或 Leader bridge 从 Matrix room 检测各 Worker 的最新消息时间），heartbeat 时 bridge 把 Worker 状态注入协作上下文（"workerA: 上次活动 5 分钟前；workerB: 上次活动 1 小时前"）

2. **Worker 产物读取**：跨 Session 文件协作（§4.7）

3. **创建/删除 Worker**：Leader 的 CimiCode 如果需要创建/删除 Worker，通过 MCP 工具调 controller API——但这需要在 Nacos opencode.json 里配置指向 controller API 的 MCP Server。**或者 bridge 提供 HTTP API，Leader 的 CimiCode 在 Sandbox 里通过 HTTP 调 bridge。**

**⚠️ 这是需要设计的替代方案，不是简单替换。Leader 的 hiclaw CLI 依赖是 team 协调的核心能力，替代不了会影响 Leader 功能。**

#### 4.10.6 原有工具/命令替换映射总结

| 原有命令/工具                   | 用途                  | 替换后                               | 谁负责              |
| ------------------------------- | --------------------- | ------------------------------------ | ------------------- |
| `worker-entrypoint.sh`          | 启动脚本              | 新写 bridge 启动脚本                 | operator            |
| `merge-openclaw-config.sh`      | openclaw.json 合并    | 不需要                               | 废弃                |
| `hiclaw-sync`（hiclaw-sync.sh） | 拉 MinIO 文件         | cimicode 无状态服务文件 staging/恢复 | cimicode 无状态服务 |
| `mc mirror`/`mc cp`/`mc cat`    | 推/拉/读 MinIO        | publish_artifact + S3                | cimicode 无状态服务 |
| `mcporter`（CLI）               | 调 MCP Server         | cimicode 无状态服务原生 MCP          | cimicode 无状态服务 |
| `mcporter-servers.json`         | MCP 配置              | Nacos opencode.json mcp              | cimicode 无状态服务 |
| `hiclaw worker report-ready`    | 报告 ready            | bridge 自己 report                   | bridge              |
| `hiclaw get workers`            | Leader 查 Worker 状态 | bridge 维护 + heartbeat 注入         | bridge              |
| `hiclaw create/delete worker`   | 创建/删除 Worker      | controller API 或 bridge HTTP API    | controller/bridge   |
| `openclaw gateway run`          | 启动 agent runtime    | 不存在——bridge 是自己的进程          | bridge              |
| `push-worker-skills.sh`         | 推 skills 到 OSS      | 一次性发布到 Skill Catalog           | 一次性工作          |
| `notify-platform.sh`            | outputs/ 回调         | cimicode 无状态服务 publish_artifact | cimicode 无状态服务 |

### 4.11 agi-agentteams-controller

- createTeamCR 传 runtime=cimicode-bridge
- team 创建时调cimicode 无状态服务 create_expert_session（或 bridge 启动时自调）
- package 解压分流 → Skills 发布到 Catalog（一次性）；AGENTS.md/SOUL.md 走哪里需确认

### 4.12 指令体系翻译

**问题：** OpenClaw 的 AGENTS.md 含大量 OpenClaw 专属指令（`hiclaw-sync`/`mc mirror`/`mcporter`/`HICLAW_MATRIX_DOMAIN`/两段式格式/NO_REPLY），cimicode agent 执行不了这些命令。

**方案：** 为 cimicode-agent 新写 AGENTS.md 模板：

| OpenClaw 指令                | cimicode 改成                                |
| ---------------------------- | -------------------------------------------- |
| `hiclaw-sync`                | "文件已自动同步"                             |
| `mc mirror`/`mc cp`          | "产物通过 publish_artifact 发布"             |
| `mcporter`                   | "MCP 工具已配置"                             |
| `HICLAW_MATRIX_DOMAIN`       | bridge 注入协作上下文                        |
| `hiclaw get workers`         | "通过 bridge 查询 Worker 状态"（见 §4.10.5） |
| 两段式格式/NO_REPLY/@mention | 保留说明（引导 LLM），bridge 硬过滤兜底      |

**AGENTS.md 加载渠道问题：** 新版 CimiCode 无 PVC 无共享卷，instruction.ts 的 directory 指向哪里？可能渠道：

- AGENTS.md 作为 Nacos opencode.json 的 instructions 配置项？
- AGENTS.md 作为 Skill Catalog 的一项？
- AGENTS.md 通过cimicode 无状态服务 submit_turn 的 input.system 传入？
- **需和 cimicode 团队确认。**

**SOUL.md 处理：** cimicode 不认 SOUL.md。合并进 AGENTS.md 的 Identity 章节？或通过cimicode 无状态服务 input.system 传入？

**HEARTBEAT.md 处理：** bridge 从对象存储/S3 读 HEARTBEAT.md 内容，作为 input.system 传入cimicode 无状态服务 submit_turn。Leader 修改 HEARTBEAT.md → publish_artifact 发布新版本 → bridge 读取最新版本。

**agent 手写 memory/ 处理：** OpenClaw agent 在 workspace 里写 `memory/YYYY-MM-DD.md`（日记）和 `MEMORY.md`（长期笔记），通过 mc sync 到 MinIO 持久化。cimicode 模型下：

- agent 在 Sandbox 里写 memory/ → 文件在 Sandbox PVC 里（TTL 内存在）
- **持久化：** agent 通过 `publish_artifact` 发布 memory 文件到 S3？或者 Sandbox 重建时恢复 memory/？
- 新版方案说"generation 重建时恢复最新产物"——memory/ 不是产物（不是 publish_artifact 发布的），是工作文件。Sandbox TTL 到期后 memory/ 会丢。
- **⚠️ 需确认：** cimicode 无状态服务的 Sandbox 重建是否恢复 memory/？如果不恢复，agent 的日记和长期笔记会丢失。或者 bridge 额外做 memory/ 的备份恢复（但又增加 bridge 复杂度）。

**team-state.json 处理：** OpenClaw Leader 维护 `team-state.json`（任务活动真相源，记录各 Worker 的任务状态）。cimicode 模型下：

- team-state.json 在 Leader 的 Sandbox 里 → Leader 的 CimiCode 读写
- 但 Sandbox TTL 到期后 team-state.json 会丢
- **或者 bridge 维护 team-state.json？** bridge 知道各 agent 的状态（从 Matrix 消息推断），可以维护一个 team 状态文件
- **⚠️ 需确认：** team-state.json 怎么持久化？在 Sandbox 里（TTL 到期丢）还是 bridge 维护（增加 bridge 职责）？

**"软指令"替换"硬机制"：** OpenClaw 的 @mention 过滤/NO_REPLY/两段式格式/peer-mentions 是 runtime 内置强制的。cimicode 没有这些内置行为。**方案：bridge 层硬过滤 + AGENTS.md 软引导双层保障。** 涉及"不调cimicode 无状态服务"或"不发 Matrix"的硬过滤在 bridge 强制实现。剩余风险在 LLM 输出端（@mention 格式不对、该 NO_REPLY 时回了内容），bridge 无法完全控制但硬过滤保证不触发错误协作。

---

## 五、场景流程

### 5.1 Team 创建

```
前端 POST /v1/teams → controller.createTeam()
  → createTeamCR(runtime=cimicode-bridge)
→ HiClaw operator
  → 为每个 member 创建 Matrix bot 账号 + 凭证
  → 为每个 member 创建 1 个 bridge pod（挂凭证 + cimicode 无状态服务 URL + 协作上下文 env）
→ controller 调cimicode 无状态服务为每个 agent 创建 expert session
  （或 bridge 启动时自己调cimicode 无状态服务 create_expert_session）
→ 每个 bridge pod 启动
  → 等 Matrix 就绪 → 连cimicode 无状态服务 → report-ready → 监听
  → Leader 启 heartbeat 定时器
→ controller 异步 monitor → 等 ready → 设 displayName → Team ACTIVE
```

**坑点：**

1. controller 调cimicode 无状态服务创建 Session 还 是 bridge 启动时自调？如果 controller 调，controller 怎么和cimicode 无状态服务对接（API 鉴权）？如果 bridge 自调，bridge 启动时cimicode 无状态服务没准备好怎么办（重试）？
2. cimicode 无状态服务 create_expert_session 的 owner_id 用什么？agent 的 Matrix ID？team-agent 组合 ID？
3. bridge pod 启动时cimicode 无状态服务可能还没就绪 → 重试连接cimicode 无状态服务
4. 多个 bridge 同时启动 → 同时调cimicode 无状态服务 create_expert_session → cimicode 无状态服务并发创建多个 Session

### 5.2 Agent 被 @mention 响应

```
Human @agentA → agentA 的 bridge 收到
  → 硬过滤（@自己？允许列表？）→ 通过
  → 构造 input：两段式消息（history buffer + current msg）+ 协作上下文（system）
  → 调cimicode 无状态服务 submit_turn（session_id=agentA 的 session, input）
  → cimicode 无状态服务调度 CimiCode Replica
    → CimiCode 从统一会话管理 PG 读取 canonical 历史
    → CimiCode 通过 Sandbox Gateway 执行（按需建沙箱 + 工具调用）
    → CimiCode 输出结构化运行事件
  → cimicode 无状态服务汇聚事件 → 提交 Turn → 返回事件流给 bridge
  → bridge 消费事件流 → 以 agentA bot 身份发回 Matrix
  → NO_REPLY 检测 + 响应里 @mention 解析 → 按规则触发
  → 清 history buffer
```

**坑点：**

1. input 构造——两段式消息从 bridge 的 history buffer 来，协作上下文从 env 来。但cimicode 无状态服务 submit_turn 的 input 格式是否支持 system 指令？**未确认。**
2. cimicode 无状态服务事件流格式——bridge 怎么消费？SSE？WebSocket？**未确认。**
3. 事件流转发 Matrix——等完整响应还是增量发？**未确认。**
4. Turn interrupted——cimicode 无状态服务/CimiCode 失联，bridge 怎么知道？事件流断开检测？**需设计。**
5. history buffer 清空时机——done 后清？还是 Turn completed 后清？
6. cimicode 无状态服务 Session FIFO——同一 agent 被连续 @，cimicode 无状态服务自动串行（heartbeat 和正常 @是同一 Session）。**这是优势。**

### 5.3 Agent 间协作（Leader 中转）

```
agentA 完成 → @Leader(TASK_COMPLETED)
  → Leader bridge 检测 @Leader → 调cimicode 无状态服务 submit_turn（Leader 的 session）
  → cimicode 无状态服务调度 CimiCode → Leader 推理 → @agentB
  → Leader bridge 发回 Matrix → 解析 @agentB
  → agentB bridge 检测 @agentB → 调cimicode 无状态服务 submit_turn（agentB 的 session）
  → cimicode 无状态服务调度 CimiCode → agentB 推理 + 在沙箱干活
  → done → 事件流回 Matrix
```

**坑点：**

1. 协作延迟——agentA 的 bridge 发 Matrix → Leader 的 bridge /sync 收到（1-5s 延迟）→ 调cimicode 无状态服务 → CimiCode 推理 → Leader bridge 发 Matrix → agentB 的 bridge /sync 收到（1-5s 延迟）→ 调cimicode 无状态服务 → CimiCode 推理。多跳延迟叠加。
2. Leader 读 Worker 产物——Leader 的 CimiCode 要读 agentA 的 result.md。但 agentA 的 result 在 agentA 的 Session 的 Sandbox 里。Leader 的 CimiCode 怎么读到？**跨 Session 文件协作（见 §4.7）。**
3. Leader 的 CimiCode 在 Leader 的 Sandbox 里干活——它怎么知道 agentA 的产物？通过 @mention 消息内容（agentA 说"result 已发布，artifact_id=xxx"）？通过协作上下文？

### 5.4 Leader heartbeat

```
Leader bridge 定时器（每 15m）
  → 从 S3/对象存储读 HEARTBEAT.md（最新版本）
  → 调cimicode 无状态服务 submit_turn（input="heartbeat" + system=HEARTBEAT.md 内容）
  → cimicode 无状态服务调度 CimiCode → 执行 heartbeat 检查
  → 事件流回 Matrix
  → Leader 可能 @worker 催进度 → bridge 解析触发
```

**坑点：**

1. HEARTBEAT.md 怎么注入——如果cimicode 无状态服务不支持 system 指令怎么办？
2. heartbeat 和正常 @并发——cimicode 无状态服务 Session FIFO 自动串行（同一 Session）。**优势。**

### 5.5 Team 解散

```
controller.dissolveTeam → HiClawClient.deleteTeam → operator finalizer
  → 删除所有 bridge pod
  → 调cimicode 无状态服务关闭各 agent 的 expert session + drain 沙箱
  → 清理 Matrix bot/room
```

**坑点：**

1. 调cimicode 无状态服务关闭 Session——cimicode 无状态服务 API 是否支持关闭/删除 Session？`create_expert_session` 有，关闭 Session 的 API 呢？
2. Sandbox drain——cimicode 无状态服务自动 drain 还是要显式触发？

---

## 六、风险分析

### 6.1 技术风险

| 风险                                      | 等级 | 说明                                                         | 缓解                                                        |
| ----------------------------------------- | ---- | ------------------------------------------------------------ | ----------------------------------------------------------- |
| **cimicode 无状态服务 API team 场景适配** | 🔴 高 | cimicode 无状态服务为首页单用户 chat 设计，team 多 agent 场景需适配（程序化调用/多 Session/事件流消费/per-Turn system/跨 Session 文件） | 和cimicode 无状态服务团队确认 API 契约                      |
| **协作上下文注入渠道**                    | 🔴 高 | per-instance 协作上下文没有明确注入渠道（Nacos 全局/Catalog 全局） | 需cimicode 无状态服务 submit_turn 支持 per-Turn system 指令 |
| **AGENTS.md/SOUL.md 加载渠道**            | 🔴 高 | 新版 CimiCode 无 PVC 无共享卷，instruction.ts directory 指向哪里？ | 和 cimicode 团队确认                                        |
| **跨 Session 文件协作**                   | 🟡 中 | team 侧 agent 间靠文件协作，新版文件管理是 per-Session 的    | 设计跨 Session 文件共享机制                                 |
| **cimicode 无状态服务事件流格式**         | 🟡 中 | 结构化运行事件格式/消费方式未明确                            | 和cimicode 无状态服务团队确认                               |
| **Matrix client 从零写**                  | 🟡 中 | Matrix SDK + E2EE + @mention 检测是新逻辑                    | spike 验证                                                  |
| **bridge pod 资源**                       | 🟢 低 | 只需 Matrix + HTTP，资源前景乐观                             | POC 实测                                                    |
| **agent 间协作延迟**                      | 🟡 中 | 经 Matrix 中转，多跳延迟叠加                                 | 架构特性，接受                                              |
| **cimicode 无状态服务单点**               | 🟡 中 | cimicode 无状态服务挂了所有 agent Turn 中断                  | cimicode 无状态服务 HA                                      |
| **指令翻译行为一致性**                    | 🟡 中 | 软指令替换硬机制，LLM 可能不严格遵守                         | bridge 硬过滤兜底                                           |

### 6.2 功能退化风险

| 风险                   | 等级 | 说明                                     | 缓解                  |
| ---------------------- | ---- | ---------------------------------------- | --------------------- |
| dreaming 放弃          | 🟡 中 | 长对话不自动蒸馏，跨日经验积累退化       | 确认可接受            |
| memorySearch 放弃      | 🟡 中 | 不能语义检索长期记忆                     | 确认可接受            |
| 群聊视野复刻不精确     | 🟡 中 | history buffer 偏差影响协作              | 参考 CoPaw channel.py |
| agent 自我修改 SOUL.md | 🟡 低 | 新版 publish_artifact + 不可变版本可支持 | 确认是否必须          |

### 6.3 资源风险

| 风险                    | 等级 | 说明                                                         | 缓解                         |
| ----------------------- | ---- | ------------------------------------------------------------ | ---------------------------- |
| **资源没省**            | 🟡 中 | bridge 轻但 N×T 个 + cimicode 无状态服务 + CimiCode 池 + N Sandbox | POC 实测                     |
| cimicode 无状态服务资源 | 🟡 中 | cimicode 无状态服务是 Java 服务，资源占用待测                | POC 实测                     |
| Sandbox 资源            | 🟡 中 | N 个 Sandbox，TTL 内占资源                                   | cimicode 无状态服务 LRU 回收 |

### 6.4 失败模式

| 失败                  | 爆炸半径              | 恢复                                                     | 说明                                  |
| --------------------- | --------------------- | -------------------------------------------------------- | ------------------------------------- |
| bridge pod 挂         | 1 agent               | K8s 重启 + 连 Matrix + 连cimicode 无状态服务（比 V1 快） | 只影响 1 agent                        |
| CimiCode pod 挂       | Turn interrupted      | cimicode 无状态服务标记 interrupted + 用户重试           | cimicode 无状态服务管理 Turn 生命周期 |
| cimicode 无状态服务挂 | 所有 agent Turn 中断  | 恢复cimicode 无状态服务                                  | 新单点                                |
| Sandbox TTL 到期      | 1 agent（沙箱销毁）   | generation 重建                                          | cimicode 无状态服务管理               |
| 统一会话管理 PG 挂    | 所有 agent 无法读历史 | 恢复 PG                                                  | 新依赖                                |
| Matrix/Synapse 挂     | 所有 agent 无法收发   | 恢复 Synapse                                             | 和 OpenClaw 一样                      |

### 6.5 补充坑点

| 坑点                                           | 说明                                                    |
| ---------------------------------------------- | ------------------------------------------------------- |
| cimicode 无状态服务是否支持程序化调用          | cimicode 无状态服务为 UI 设计，程序化调用需确认鉴权方式 |
| cimicode 无状态服务是否支持多 Session 同时活跃 | team 的 N 个 agent 各自一个 Session                     |
| cimicode 无状态服务事件流消费方式              | SSE？WebSocket？轮询？                                  |
| Turn interrupted 通知 bridge                   | cimicode 无状态服务主动通知还是 bridge 检测断开？       |
| bridge 消费事件流转 Matrix 的流式问题          | Matrix 不支持消息流式编辑                               |
| since token 丢失全量重同步                     | Redis 挂了或 token 过期                                 |
| 多 bridge 同时 login Synapse 限流              | team 创建时 N 个 login 请求                             |
| E2EE crypto state 重启丢失                     | 每次重启重新协商                                        |
| @mention 解析鲁棒性                            | LLM 生成格式不标准                                      |
| displayName 设置时机                           | ready 语义变了，controller 联动要改                     |
| 协作上下文变更                                 | team 成员加/删要更新协作上下文                          |
| Skills scripts 依赖                            | `mc`/`hiclaw-sync`/`hiclaw` CLI 在 Sandbox 不存在       |
| Skill 发布流水线                               | builtin skills 要通过 Git→评审→打包→S3→Catalog          |
| operator 和cimicode 无状态服务对接             | operator/bridge 怎么调cimicode 无状态服务创建 Session   |

---

## 七、可信性分析

### 7.1 资源条件评估

| 条件                    | 评估           | 说明                                                         |
| ----------------------- | -------------- | ------------------------------------------------------------ |
| 新版 cimicode 方案      | ⚠️ 设计收敛中   | 统一会话管理/cimicode 无状态服务/CimiCode 池化/Sandbox Gateway/Skill Catalog/Nacos 设计完成，实现进行中 |
| cimicode 无状态服务 API | ⚠️ 需 team 适配 | 为首页单用户 chat 设计，team 场景需确认                      |
| CimiCode instruction.ts | ⚠️ 需确认       | 新版无 PVC，AGENTS.md 加载渠道不明确                         |
| HiClaw operator         | ⚠️ 调谐步骤要改 | 调谐模型不变但配置生成/env/启动脚本/ready 都要改             |
| Matrix/Synapse          | ✅ 就绪         | 现有可复用                                                   |
| 对象存储/S3             | ✅ 就绪         | 新版方案复用                                                 |

### 7.2 功能支持情况

| 功能                      | OpenClaw               | 新版 cimicode + bridge                            | 状态                      |
| ------------------------- | ---------------------- | ------------------------------------------------- | ------------------------- |
| 群聊 @mention 协作        | ✅ runtime              | ✅ bridge 硬过滤                                   | 需 bridge 实现            |
| Leader 协调 + heartbeat   | ✅ runtime cron         | ✅ bridge 定时器 + cimicode 无状态服务 submit_turn | 需 bridge 实现            |
| agent 间文件协作          | ✅ MinIO + mc           | ⚠️ 需设计跨 Session 机制                           | 未解决                    |
| LLM 推理                  | ✅ 每 pod 独立          | ✅ CimiCode 池化                                   | cimicode 无状态服务实现中 |
| MCP / Skills              | ✅ mcporter + workspace | ✅ Nacos + Catalog                                 | 需发布到 Catalog          |
| 会话上下文                | ✅ 本地 .jsonl          | ✅ 统一会话管理 PG                                 | cimicode 无状态服务实现中 |
| 群聊视野                  | ✅ history buffer       | ⚠️ bridge 仍需维护                                 | 需 bridge 实现            |
| compaction                | ✅ contextWindow        | ✅ CimiCode 自带                                   | 已就绪                    |
| dreaming / memorySearch   | ✅                      | ❌ 放弃                                            | 退化                      |
| agent 手写 memory         | ✅ MinIO                | ✅ Sandbox + publish_artifact                      | 支持                      |
| 指令（AGENTS.md/SOUL.md） | ✅ runtime 加载         | ⚠️ 加载渠道不明确                                  | 需确认                    |
| 失败恢复                  | ✅ session 在本地       | ✅ Turn interrupted + 持久化                       | cimicode 无状态服务管理   |

### 7.3 资源前景

| 维度         | OpenClaw（N pod）                                       | 替换后                                  | 谁省           |
| ------------ | ------------------------------------------------------- | --------------------------------------- | -------------- |
| agent pod    | N 个 openclaw（重，Node.js+Ubuntu+LLM gateway+本地 fs） | N 个 bridge（轻，只 Matrix+HTTP）       | bridge 省      |
| LLM 推理     | N 个 Node.js gateway 常驻                               | CimiCode 池化（按需扩缩）               | CimiCode 省    |
| 执行环境     | N 个本地 fs 常驻                                        | N 个 Sandbox（TTL+LRU 回收）            | 取决于空闲比例 |
| 编排         | 0                                                       | cimicode 无状态服务 + 统一会话管理      | 新增           |
| 额外基础设施 | 0                                                       | Sandbox Gateway + Skill Catalog + Nacos | 新增           |

**前景比 V1 乐观**：bridge 更轻（只需 Matrix+HTTP）+ CimiCode 池化（按需扩缩）+ Sandbox TTL/LRU 回收。但仍需 POC 实测。

### 7.4 不同规模分析

| 规模       | 分析                                                       |
| ---------- | ---------------------------------------------------------- |
| 小规模     | bridge 轻 + cimicode 无状态服务基础设施成本，可能不划算    |
| 中等规模   | bridge 轻 + CimiCode 池化 + Sandbox 回收，有条件可行       |
| 大规模     | 最能省——轻 bridge 平摊 + CimiCode 池化 + 空闲 Sandbox 回收 |
| agent 常忙 | Sandbox 常驻，省的是 bridge 更轻 + LLM 集中                |

### 7.5 前置验证

| #    | 验证项                            | 验证什么                                                     | 不通过的后果                                              |
| ---- | --------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------- |
| 1    | cimicode 无状态服务 API team 适配 | 程序化调用 + 多 Session + 事件流消费 + per-Turn system       | cimicode 无状态服务不兼容 → **需cimicode 无状态服务改造** |
| 2    | 协作上下文注入                    | submit_turn 是否支持 per-Turn system 指令？                  | 无注入渠道 → **agent 不知道 coordinator**                 |
| 3    | AGENTS.md/SOUL.md 加载            | instruction.ts directory 指向哪里？                          | 无加载渠道 → **agent 无行为指令**                         |
| 4    | 跨 Session 文件协作               | agent 间文件怎么跨 Session 共享？                            | 无法协作 → **team 文件协作断裂**                          |
| 5    | cimicode 无状态服务事件流格式     | 事件流 API 格式？bridge 怎么消费？                           | 格式不明确 → **bridge 实现不了**                          |
| 6    | Bridge spike                      | Matrix client + 调cimicode 无状态服务 + 事件流转发 Matrix 端到端 | Matrix 不可行 → **bridge 不成立**                         |
| 7    | POC 实测资源                      | bridge + cimicode 无状态服务 + CimiCode 池 + Sandbox 总资源  | 资源没省 → **核心目标失败**                               |
| 8    | 功能退化可接受                    | dreaming/RAG 放弃是否可接受                                  | 不可接受 → **需实现 dreaming**                            |
| 9    | Session 日重置                    | canonical 是否支持日重置/按时间过滤？                        | 不支持 → **canonical 过长，上下文重建慢**                 |
| 10   | memory/ 持久化                    | Sandbox TTL 后 memory/ 是否恢复？                            | 不恢复 → **agent 日记和长期笔记丢失**                     |

### 7.6 综合结论

**方案架构方向合理**：bridge 极轻（Matrix↔cimicode 无状态服务转发），cimicode 无状态服务 + CimiCode 体系接管大部分编排（上下文/Session/沙箱/文件/Skills/配置/失败恢复），比 V1（bridge 承担 12 类职责）显著简化。

**但 team 场景有 7 个未解决问题**（cimicode 无状态服务 API 适配/协作上下文注入/AGENTS.md 加载/跨 Session 文件协作/事件流格式/Session 日重置/memory 持久化），这些是新版方案为首页单用户 chat 设计导致的 gap。

**建议先和cimicode 无状态服务团队 + cimicode 团队确认前置验证 1-10，再做 bridge spike + POC。**

---

## 八、建议下一步

### 8.1 和cimicode 无状态服务团队确认 API 契约（最高优先）

确认：程序化调用 + 多 Session + 事件流格式 + per-Turn system + 跨 Session 文件 + Session 关闭 API。

### 8.2 和 cimicode 团队确认指令加载

确认：instruction.ts directory 指向哪里？AGENTS.md/SOUL.md 怎么加载？

### 8.3 设计跨 Session 文件协作

team 侧 agent 间 shared/ 文件怎么跨 Session 共享？

### 8.4 Bridge spike

Matrix client + 调cimicode 无状态服务 + 事件流转发 Matrix 端到端。

### 8.5 POC 实测资源

bridge + cimicode 无状态服务 + CimiCode 池 + Sandbox 总资源对比 openclaw。

### 8.6 确认功能退化可接受

dreaming/RAG 放弃。

### 8.7 如果验证通过

HiClaw operator 加 cimicode-bridge runtime + bridge pod 实现 + Skills 发布到 Catalog + 渐进灰度。

---

## 九、任务拆分与排期

> 排期单位：人周（PW）。假设投入 3 名后端 + 1 名 SRE + 1 名 prompt/指令工程。
> 前置验证（§八 8.1-8.6）完成后才进入开发阶段。

### 9.1 阶段 0：前置验证（2 PW）

| 任务                                    | 负责人 | 工时   | 说明                                                         |
| --------------------------------------- | ------ | ------ | ------------------------------------------------------------ |
| 和 cimicode 无状态服务团队确认 API 契约 | 后端 A | 0.3 PW | 程序化调用/多 Session/事件流/per-Turn system/跨 Session 文件/Session 关闭/日重置/memory 持久化 |
| 和 cimicode 团队确认指令加载            | 后端 A | 0.2 PW | instruction.ts directory/AGENTS.md/SOUL.md 加载渠道          |
| 设计跨 Session 文件协作方案             | 后端 B | 0.3 PW | 基于 API 契约确认结果设计                                    |
| 设计 Leader hiclaw CLI 替代方案         | 后端 B | 0.2 PW | Worker 状态查询/产物读取/创建删除 Worker                     |
| Bridge spike                            | 后端 C | 1 PW   | 选定技术栈 + Matrix client + 调 cimicode 无状态服务 + 事件流转发 Matrix 端到端 |

**阶段 0 产出：** API 契约确认文档 + 跨 Session 文件协作方案 + Leader CLI 替代方案 + Bridge spike 验证报告

### 9.2 阶段 1：指令体系翻译与 Skills 发布（1.5 PW）

| 任务                                                         | 负责人      | 工时   | 说明                                                        |
| ------------------------------------------------------------ | ----------- | ------ | ----------------------------------------------------------- |
| 重写 worker-agent AGENTS.md 模板                             | prompt 工程 | 0.3 PW | 去掉 hiclaw-sync/mc mirror/mcporter，改成 cimicode 模型指令 |
| 重写 team-leader-agent AGENTS.md 模板                        | prompt 工程 | 0.3 PW | 同上 + Leader 协调指令适配                                  |
| SOUL.md 处理方案确定与实现                                   | prompt 工程 | 0.2 PW | 合并进 AGENTS.md 或通过 system 注入（取决于 §9.1 确认结果） |
| 改写 project-participation skill                             | prompt 工程 | 0.1 PW | 去掉 hiclaw-sync/mc 引用                                    |
| 改写 Leader skills（team-coordination/organization/project-management/task-management/file-sharing/communication） | prompt 工程 | 0.4 PW | 逐一检查依赖，去掉 mc/hiclaw-sync/hiclaw CLI                |
| Skills 发布到 Catalog                                        | 后端 C      | 0.2 PW | 通过 Git→打包→S3→Catalog 发布流程                           |

**阶段 1 产出：** cimicode-agent AGENTS.md 模板（worker + leader）+ 改写后的 Skills 发布到 Catalog

### 9.3 阶段 2：Bridge Pod 开发（5 PW）

这是 teams 侧最大工作量模块。按子模块拆分：

| 子模块                                                   | 负责人 | 工时   | 依赖                      | 说明                                                         |
| -------------------------------------------------------- | ------ | ------ | ------------------------- | ------------------------------------------------------------ |
| Bridge 骨架 + 启动生命周期                               | 后端 A | 0.5 PW | 阶段 0                    | 技术栈选定 + 启动框架 + 分阶段等待（Matrix/cimicode 无状态服务）+ report-ready + K8s 部署 |
| Matrix 连接（login/sync/E2EE/backoff/since token Redis） | 后端 C | 1 PW   | 阶段 0 spike              | 基于 spike 验证结果，实现完整 Matrix 连接 + E2EE + 重连 + since token 持久化 |
| @mention 检测与硬过滤                                    | 后端 C | 0.5 PW | Matrix 连接               | requireMention/groupAllowFrom/peer-mentions + @mention 解析容错 |
| 两段式群聊视野（history buffer）                         | 后端 C | 0.5 PW | @mention 检测             | per-room buffer ≤50 FIFO + 格式化两段式 + 回复后清空 + 重建  |
| Session 管理与映射                                       | 后端 A | 0.3 PW | cimicode 无状态服务 API   | create_expert_session + session_id↔agent Redis 映射 + 日重置（如果需要） |
| 协作上下文注入                                           | 后端 A | 0.2 PW | Session 管理              | 从 env 读协作上下文 + 拼进 submit_turn input.system          |
| 调 cimicode 无状态服务 submit_turn                       | 后端 A | 0.5 PW | Session 管理 + 协作上下文 | 构造 input（两段式 + 协作上下文）+ 调 submit_turn + 事件流消费 |
| 事件流转发 Matrix                                        | 后端 B | 0.5 PW | submit_turn               | 消费事件流 + 累积响应 + 以 bot 身份发 Matrix + NO_REPLY 检测 + @mention 解析 |
| heartbeat 定时器                                         | 后端 B | 0.3 PW | submit_turn               | Leader 每 15m 调 submit_turn + HEARTBEAT.md 读取 + @worker 触发 |
| 跨 Session 文件协作                                      | 后端 B | 0.7 PW | 阶段 0 方案               | publish_artifact 协调 + 跨 Session artifact 读取/staging     |

**阶段 2 产出：** 可运行的 bridge pod（单 agent 端到端：@mention → submit_turn → 事件流 → Matrix 响应）

### 9.4 阶段 3：HiClaw Operator 改造（3 PW）

| 任务                          | 负责人       | 工时   | 依赖               | 说明                                                         |
| ----------------------------- | ------------ | ------ | ------------------ | ------------------------------------------------------------ |
| EnvBuilder 重写               | 后端 A       | 0.3 PW | 阶段 2             | bridge 专用 env（cimicode 无状态服务 URL/协作上下文/Matrix 凭证） |
| GenerateBridgeConfig 新写     | 后端 A       | 0.5 PW | 阶段 1             | 替代 GenerateOpenClawConfig，生成 bridge 配置                |
| Deployer 改造                 | 后端 A       | 0.3 PW | 阶段 1             | 不推 OSS → 推共享卷/Nacos（取决于确认结果）+ package 解压分流 |
| ReconcileMemberContainer 改造 | 后端 B       | 0.3 PW | EnvBuilder         | image 换 bridge 镜像 + volumeMounts 改                       |
| 启动脚本新写                  | 后端 B       | 0.2 PW | 阶段 2             | bridge 启动脚本（等 Matrix + 连 cimicode 无状态服务 + report-ready） |
| summarizeBackendReadiness 改  | 后端 B       | 0.2 PW |                    | ready = bridge 连 Matrix + 连 cimicode 无状态服务            |
| handleDelete 改               | 后端 B       | 0.2 PW |                    | 删 pod + 清 Matrix + 调 cimicode 无状态服务关闭 Session      |
| 双 runtime 分流               | 后端 B       | 0.5 PW | 所有 operator 改动 | 每步 if/else（openclaw / cimicode-bridge）                   |
| 双 runtime 兼容验证           | 后端 B + SRE | 0.5 PW | 双 runtime 分流    | 同一 operator 同时处理两种 runtime + 切换迁移 + finalizer    |

**阶段 3 产出：** operator 支持 cimicode-bridge runtime + 双 runtime 灰度

### 9.5 阶段 4：Controller 改造（1 PW）

| 任务                                         | 负责人 | 工时   | 依赖            | 说明                                                         |
| -------------------------------------------- | ------ | ------ | --------------- | ------------------------------------------------------------ |
| createTeamCR 传 runtime=cimicode-bridge      | 后端 A | 0.1 PW | 阶段 3          |                                                              |
| 调 cimicode 无状态服务 create_expert_session | 后端 A | 0.3 PW | 阶段 0 API 确认 | controller 程序化调 cimicode 无状态服务创建 agent Session + 鉴权 |
| package 解压分流                             | 后端 A | 0.2 PW | 阶段 1          | 指令→Catalog/Nacos；工作区→对象存储                          |
| displayName 联动改                           | 后端 A | 0.2 PW | 阶段 3 ready 改 | waitForTeamMembersAndSetDisplayNames 适配新 ready 语义       |
| Team 解散调 cimicode 无状态服务关闭 Session  | 后端 A | 0.2 PW |                 | dissolveTeam 时调 cimicode 无状态服务关闭各 agent Session    |

**阶段 4 产出：** controller 支持 cimicode-bridge runtime 的 team 创建/解散

### 9.6 阶段 5：集成测试与灰度（2 PW）

| 任务                | 负责人          | 工时   | 依赖     | 说明                                                         |
| ------------------- | --------------- | ------ | -------- | ------------------------------------------------------------ |
| 单 agent 端到端测试 | 后端 A + 后端 C | 0.3 PW | 阶段 2-4 | @mention → bridge → cimicode 无状态服务 → 事件流 → Matrix 响应 |
| 多 agent 协作测试   | 后端 A + 后端 C | 0.5 PW | 单 agent | Leader 中转 + Worker 协作 + 跨 Session 文件 + heartbeat      |
| POC 实测资源        | SRE             | 0.3 PW | 多 agent | bridge + cimicode 无状态服务 + CimiCode 池 + Sandbox 总资源对比 openclaw |
| 灰度方案实施        | SRE             | 0.5 PW | POC 通过 | 新 team 默认 cimicode-bridge + 有问题回退 openclaw           |
| 异常场景测试        | 后端 B + 后端 C | 0.4 PW | 灰度     | bridge 挂重启 / cimicode 无状态服务挂 / Turn interrupted / Sandbox TTL |

**阶段 5 产出：** 端到端验证通过 + 资源实测报告 + 灰度方案

### 9.7 总排期

| 阶段   | 内容                    | 工时   | 累计    |
| ------ | ----------------------- | ------ | ------- |
| 阶段 0 | 前置验证 + Bridge spike | 2 PW   | 2 PW    |
| 阶段 1 | 指令翻译 + Skills 发布  | 1.5 PW | 3.5 PW  |
| 阶段 2 | Bridge Pod 开发         | 5 PW   | 8.5 PW  |
| 阶段 3 | HiClaw Operator 改造    | 3 PW   | 11.5 PW |
| 阶段 4 | Controller 改造         | 1 PW   | 12.5 PW |
| 阶段 5 | 集成测试与灰度          | 2 PW   | 14.5 PW |

**总计约 14.5 PW（3 名后端 + 1 名 SRE + 1 名 prompt 工程并行约 5-6 周）。**

**关键路径：** 阶段 0（前置验证）→ 阶段 2（Bridge 开发，最长）→ 阶段 3（Operator）→ 阶段 5（集成测试）。阶段 1（指令翻译）可与阶段 2 并行。阶段 4（Controller）可与阶段 3 后半段并行。

### 9.8 人力分配

| 人员                     | 阶段 0                    | 阶段 1                             | 阶段 2                               | 阶段 3                                     | 阶段 4          | 阶段 5        |
| ------------------------ | ------------------------- | ---------------------------------- | ------------------------------------ | ------------------------------------------ | --------------- | ------------- |
| 后端 A（Bridge 主力）    | API 确认                  | —                                  | 骨架/Session/协作上下文/submit_turn  | EnvBuilder/Generator/Deployer              | controller 改造 | 端到端测试    |
| 后端 B（Operator 主力）  | CLI 替代设计/文件协作设计 | —                                  | 事件流转发/heartbeat/跨 Session 文件 | Container/启动脚本/ready/delete/双 runtime | —               | 异常测试      |
| 后端 C（cimicode/infra） | Bridge spike              | Skills 发布                        | Matrix 连接/@mention/history buffer  | —                                          | —               | 多 agent 测试 |
| SRE                      | —                         | —                                  | —                                    | 双 runtime 兼容验证                        | —               | POC 资源/灰度 |
| prompt 工程              | —                         | AGENTS.md 重写/SOUL.md/skills 改写 | —                                    | —                                          | —               | —             |

### 9.9 关键风险与里程碑

| 里程碑                | 时间点    | 前提条件                                       |
| --------------------- | --------- | ---------------------------------------------- |
| API 契约确认完成      | 第 1 周末 | cimicode 无状态服务团队配合                    |
| Bridge spike 验证通过 | 第 2 周末 | Matrix SDK 可用 + cimicode 无状态服务 API 可调 |
| 单 agent 端到端跑通   | 第 5 周末 | 阶段 2 完成 + 阶段 3/4 部分完成                |
| 多 agent 协作跑通     | 第 6 周末 | 跨 Session 文件协作 + heartbeat + Leader 中转  |
| 灰度上线              | 第 7 周末 | POC 资源通过 + 异常测试通过                    |

**⚠️ 最大风险：** 阶段 0 的 API 契约确认——如果 cimicode 无状态服务不支持 per-Turn system 指令/跨 Session 文件/程序化调用/日重置/memory 持久化，阶段 2 的设计方案要调整，排期可能延长。