# OpenClaw → 无状态 cimicode 替换技术方案

> 基于对 agi-agentteams / agi-agentteams-controller / agi-cimicode / agi-context 四个项目的深度代码分析及多轮决策推演。
> 本文不涉及代码改动，盘清整体方案、组件角色、衔接关系、场景流程、技术解决方案、风险与可信性。

---

## 一、背景与目标

### 1.1 问题

当前 Agent Teams 系统中，每个 agent 启动一个 OpenClaw 的常驻 pod（Node.js LLM gateway + Ubuntu + 本地 fs + Matrix 长连接），不管 agent 是否在干活都占用资源。一个 team 有 N 个 agent（含 Team Leader），就创建 N 个常驻 pod。

### 1.2 目标

用基于 opencode 改造的无状态 cimicode 服务替换 OpenClaw，降低资源占用。cimicode 无状态方案的核心卖点是"多 Pod 无状态 + 远程沙箱"——少量 cimicode pod 共享，按 sessionID 路由，文件操作全在远程 OpenSandbox 执行。

### 1.3 根本挑战

OpenClaw 和 cimicode 是**两个不同的运行时**，运行模型根本不同：

| 维度 | OpenClaw（现状） | cimicode（目标） |
|------|------------------|-------------------|
| 状态 | 有状态，本地 SQLite/.jsonl 存会话 | 无状态，不存历史，靠外部传 context |
| 生命周期 | 长驻 pod，永不退出 | 被动请求-响应，调用完即结束 |
| Matrix | 自带 Matrix plugin，主动连 Matrix 监听 @mention | 不连 Matrix、不监听、不主动 |
| 文件操作 | worker pod 本地 fs + mc sync MinIO | 无本地 fs，全在远程 OpenSandbox |
| 定时任务 | 内部 cron（heartbeat/dreaming） | 无定时机制 |
| 指令加载 | SOUL.md/AGENTS.md/HEARTBEAT.md/openclaw.json/skills（OpenClaw runtime 内置加载） | AGENTS.md/cimicode.json/skills（opencode instruction.ts/config.ts 加载）；**不认 SOUL.md/HEARTBEAT.md/openclaw.json/mcporter** |

**cimicode 无状态方案是为单用户 chat 场景设计的**（一个用户、一个 session、一个 sandbox、请求-响应），不连 Matrix、不存历史、不主动。teams 场景是**多 agent 群聊**——每个 agent 是 Matrix 群里的成员，需要主动连 Matrix 监听 @mention、维护会话状态、能 @mention 别人协作。

**这个错配是方案的核心难点：需要一个 Bridge 组件补上 cimicode 缺失的"Matrix 群聊成员"角色，且 OpenClaw 的指令体系要翻译成 cimicode 的指令体系。**

### 1.4 方案选择

经分析（详见附录 A 方案演进），采用**"每 agent 一个轻量 bridge pod + 共享无状态 cimicode"**方案：

- **不改变分布式架构**——每 agent 一个独立 pod（和 OpenClaw 同构），不集中化，保持无单点/无瓶颈/爆炸半径小
- **pod 更轻**——bridge pod 只做 Matrix 监听 + SSE 转发，去掉 Node.js LLM gateway + Ubuntu
- **LLM 推理集中**——共享 2-3 个无状态 cimicode pod 服务所有 agent
- **执行环境可暂停**——OpenSandbox 沙箱 agent 空闲时可 pause/回收

**省资源的三个来源：** ① bridge pod 比 openclaw pod 轻；② LLM 推理集中到共享 cimicode；③ 沙箱可 pause。**不靠减 pod 数量**（还是 N×T 个），靠每个 pod 更轻 + LLM 集中 + 沙箱可暂停。

**⚠️ 三个来源每个都有不确定性，资源是否真省完全取决于 POC 实测（详见 §七可信性分析）。**

---

## 二、现状分析

### 2.1 项目全景

```
agi-agentteams/              # HiClaw 核心（Go K8s operator + Docker 镜像）
  ├── hiclaw-controller/     # Go operator：Team/Worker/Human CRD 编排
  ├── worker/                # OpenClaw Worker 镜像
  ├── manager/               # Manager 镜像 + agent 模板 + skills
  └── openclaw-base/         # 基础镜像：Ubuntu + Node.js + OpenClaw

agi-agentteams-controller/   # 桥接层（Java Spring Boot + Dubbo REST）
  └── 对前端 /v1/teams/*，对下用 HiClawClient 调 hiclaw operator

agi-cimicode/                # 无状态 cimicode（TypeScript/Bun，opencode fork）
  └── packages/opencode/     # 核心：context_prompt SSE + InMemorySessionStore + sandboxID 穿透

agi-context/                 # 编排层（Java Spring Boot）
  └── 已实现单用户 chat：建 OpenSandbox + 调 cimicode SSE + PG 持久化历史
```

### 2.2 当前 OpenClaw 架构

```
前端 → controller（Dubbo REST）→ HiClawClient → HiClaw operator（Go）
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

经代码深挖（`worker-agent/AGENTS.md`、`copaw/src/matrix/channel.py`、`worker-openclaw.json.tmpl`），OpenClaw agent 的上下文管理是精细的分层模型：

**LLM 输入格式（两段式）：**
```
[Chat messages since your last reply - for context]
... 别人在这段时间说的话（per-room buffer, ≤50 条 FIFO）...

[Current message - respond to this]
... 这次 @我 的消息 ...
```

**三层记忆：**

| 层次 | 存储 | 生命周期 | 用途 |
|------|------|----------|------|
| session（.jsonl） | pod 本地 + MinIO 同步 | 每天 04:00 重置 | LLM 对话上下文 |
| agent 手写 memory/ | Markdown + MinIO | 永久 | 日记本和长期笔记 |
| memory-core dreaming | OpenClaw 内部 memory store | cron 每 6h 蒸馏 | 自动记忆浓缩 |
| memorySearch | embedding 向量检索 | 持久 | RAG 检索长期记忆 |

**agent 间协作：** 默认 worker 间不能互 @mention（`groupAllowFrom` 只含 Manager/Admin），协作靠 Team Leader 中转 + MinIO 共享文件（spec.md/result.md/plan.md）。

**指令体系（OpenClaw runtime 内置加载）：**

| 文件 | 机制 | 作用 |
|------|------|------|
| SOUL.md | runtime 启动时自动加载 | agent 人格和身份（seed-only，agent 可自我修改） |
| AGENTS.md | runtime 自动加载 | 行为指令（@mention 协议/NO_REPLY/任务执行/文件操作） |
| HEARTBEAT.md | runtime cron 定时读 | Leader heartbeat 检查清单（seed-only） |
| openclaw.json | runtime 启动时读 | 运行配置（models/gateway/channels/matrix/plugins/session/dreaming） |
| skills/SKILL.md | runtime 自动加载 | 技能说明（skills/scripts/ 在 pod 本地执行） |
| mcporter-servers.json | agent 通过 mcporter CLI 调 MCP Server | MCP 工具配置 |

### 2.4 cimicode 无状态方案现状

cimicode 无状态方案已**基本实现**，核心接口 `POST /session/context_prompt`（SSE）完整可用：

| 能力 | 实现位置 | 状态 |
|------|----------|------|
| `POST /session/context_prompt`（SSE） | `session.ts:984-1158` | ✅ |
| InMemorySessionStore（30min TTL） | `in-memory-session-store.ts` | ✅ |
| external sessionID / importContext | `session.ts` | ✅ |
| sandboxID 穿透到 Tool.Context | `prompt.ts:379` | ✅ |
| requestHeaders 穿透 | `llm.ts:386` | ✅ |
| 单次会话 compaction | `compaction.ts` | ✅ |
| OpenSandbox 插件 v2 | `cimi-plugin/opensandbox/` | ✅ |
| directory 级配置加载 | `config/paths.ts:18-34` | ✅ |
| MCP 客户端 | `mcp/index.ts` | ✅ |
| serve 模式 / 单 binary Docker | `cli/cmd/serve.ts` | ✅ |

**cimicode 指令加载机制（opencode 运行时）：**

| 文件 | 机制 | 作用 |
|------|------|------|
| AGENTS.md | instruction.ts 从 directory 向上查找，读内容拼进 system prompt | 指令 |
| cimicode.json | config.ts 从 directory 向上查找 | 配置（provider/agent/mcp/permission） |
| skills/SKILL.md | skill/index.ts 从 directory 查找，注入 system prompt skills 列表 | 技能说明 |
| context_prompt 的 system 字段 | 请求传入，拼进 system prompt | 额外指令 |
| **SOUL.md** | **不识别** | cimicode 无加载机制 |
| **HEARTBEAT.md** | **不识别** | cimicode 无定时机制 |
| **openclaw.json** | **不识别** | cimicode 用 cimicode.json，格式完全不同 |
| **mcporter-servers.json** | **不识别** | cimicode 用 cimicode.json 的 mcp 配置 |

**cimicode system prompt 组装顺序**（`llm.ts:100-108`）：
```
system[0] = agent.prompt（cimicode.json 里 agent 配置的 prompt，或 provider 默认）
system[1] = instruction.system()（从 directory 读 AGENTS.md + skills 列表）
system[2] = input.system（context_prompt 请求传的 system 字段）
```

### 2.5 agi-context 现状

已实现单用户 chat 编排：建 OpenSandbox + 调 cimicode SSE + PG 持久化历史。**没有 Matrix 集成。**

---

## 三、替换后整体架构

### 3.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│ 前端（不变）                                                           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ Dubbo REST（不变）
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ agi-agentteams-controller                                            │
│  · createTeamCR 传 runtime=cimicode-bridge                           │
│  · package 解压分流（指令→共享卷，工作区→对象存储）                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ HiClawClient REST（不变）
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ HiClaw operator                                                      │
│  · Team CR reconciler：为每个 member 创建 1 个轻量 bridge pod         │
│    （和 openclaw 同构：每 member 1 pod，但 pod 内容/配置/env/启动脚本全换）│
│  · Matrix bot provision 仍由 operator（凭证挂给 bridge pod）           │
│  · 保留 openclaw runtime（双 runtime 灰度）                            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ 每 member 创建 1 个 bridge pod
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 轻量 Bridge pod（每 agent 1 个，分布式）                                 │
│                                                                       │
│  · Matrix 监听：该 agent bot 的 /sync（1 个连接）                       │
│  · @mention 检测 → 调共享 cimicode /session/context_prompt（SSE）       │
│  · SSE 响应以该 agent bot 身份发回 Matrix room                          │
│  · per-agent session/history buffer（per-pod 内存，挂了只丢 1 个）       │
│  · heartbeat 定时器（如果是 Leader agent）                               │
│  · 沙箱管理（建/pause/sync，只管自己这 1 个 agent）                       │
│  · 旁路落 PG（会话历史持久化）                                           │
│  · 协作上下文注入（从环境变量读，拼进 cimicode system 字段）              │
│  · 文件 sync（对象存储 ↔ 沙箱，增量同步）                                │
│                                                                       │
│  技术栈：轻量（Go / Bun / Python，待定）                                 │
└──────────────┬───────────────────────────────────┬───────────────────┘
               │ HTTP(SSE)                          │ 挂载(只读)
               ▼                                    ▼
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│ 共享 cimicode 集群（2-3 pod）  │  │ 共享配置卷（按 agent 类型组织）      │
│  · 无状态，按 sessionID 路由    │  │  /{agentType}/                    │
│  · /session/context_prompt     │  │    AGENTS.md（cimicode-agent 模板）│
│  · MCP 客户端（直连 MCP Server）│  │    skills/<name>/SKILL.md         │
│  · 自己连 LLM                  │  │    cimicode.json（per-type MCP）   │
└──────────────┬───────────────┘  └──────────────────────────────────┘
               │ sandboxID
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│ OpenSandbox（每 agent 1 个，可 pause/回收）                              │
│  · agent 工作区（代码/outputs/memory/skill scripts）                    │
│  · bash/read/write/edit/ls/glob/grep 7 工具                             │
└─────────────────────────────────────────────────────────────────────┘
               ▲
               │ 共享文件交换
┌──────────────┴────────────────────────────────────────────────────┐
│ 对象存储（MinIO/OSS）：shared/ + memory/ + outputs/ + overrides/     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 核心设计理念

**理念一：不改变分布式架构**——每 agent 一个独立 pod，每 pod 只管 1 个 agent（1 个 Matrix 连接），保持 OpenClaw 分布式优势（无单点/无瓶颈/爆炸半径小）。

**理念二：pod 更轻 + LLM 推理集中**——bridge pod 去掉 Node.js LLM gateway + Ubuntu，只做 Matrix 监听 + SSE 转发；LLM 推理集中到 2-3 个共享 cimicode pod。

**理念三：cimicode 尽量不改**——无状态方案现成。**但"不改代码"是前提假设，需验证多 session/directory 并发是否支持（§七 前置验证 3），如果不支持则需改 cimicode 代码。**

**理念四：Agent 类型层复用 + 实例层隔离**——共享卷按 agent 类型组织指令/skills/MCP（多 team 共享）；每 agent 独立 bridge pod + 沙箱 + 会话（隔离）；per-instance 协作上下文通过 system 字段动态注入。

**⚠️ 核心不确定性：** 省资源完全依赖"每个 pod 更轻 + LLM 集中 + 沙箱 pause"三个因素都成立。如果任何一个不成立，资源可能没省反增。**必须 POC 实测，不能盲目乐观。**

---

## 四、分模块技术方案

### 4.1 Bridge Pod 设计

#### 4.1.1 设计约束与决策依据

Bridge pod 的设计不是凭空选择，而是由以下客观条件约束决定的：

**约束 1：cimicode context_prompt 接口的契约**（`cimicode无状态方案.md` §五）

cimicode 的接口已经固定：`POST /session/context_prompt`，请求体含 `context`（ExportData 格式：`{info, messages:[{info, parts}]}`）、`parts`（新消息）、`sessionID`、`sandboxID`、`system`、`directory`、`model`。SSE 响应含 `message.part.delta`/`message.part.updated`/`message.updated`/`session.status`/`done`/`session.error` 等事件。session 在 cimicode 内存存活 30min TTL，done 后删除。

**这意味着 bridge 必须按此契约组装请求**——context 从 PG 查历史构建、parts 注入两段式消息、sessionID 稳定生成、sandboxID 从 Redis 查映射、system 注入协作上下文。这些不是"bridge 选择怎么做"，而是"接口契约要求必须这么做"。

**约束 2：OpenClaw 的行为契约**（`worker-agent/AGENTS.md` + `copaw/src/matrix/channel.py`）

OpenClaw agent 被 @后的行为链是固定的：Matrix plugin 收消息 → @mention 过滤（requireMention + groupAllowFrom）→ 非 @自己的消息进 history buffer → @自己的消息触发 LLM → 两段式格式注入（`[Chat messages since your last reply] + [Current message]`）→ LLM 推理 → 响应发回 Matrix → 回复后清 history buffer。

**bridge 要等价复刻这个行为链**，否则 agent 行为偏差。这不是"bridge 可以选择怎么处理消息"，而是"必须和 OpenClaw 行为一致"。

**约束 3：agi-context 已验证的编排范式**（`docs/cimicode-chat-sse-api.md`）

agi-context 已实现并验证了"建沙箱 → 调 cimicode SSE → 旁路落 PG"的完整编排。bridge 的 cimicode 调用 + SSE 消费 + PG 持久化逻辑可以参考 agi-context 的设计，但 agi-context 是 Java/OkHttp 栈，bridge 用不同技术栈需重写。

**约束 4：controller 的 Matrix 集成范式**（`MatrixClientImpl.java` + `MatrixSyncService.java`）

controller 已实现 Matrix CS API + Admin API 封装（login/sync/sendMessage/joinRoom/inviteUser/kickUser/createRoom/getRoomMembers 等）+ per-user /sync 长轮询（backoff + 重登录 + since token Redis 持久化）。但这些是 Java/WebClient 栈，bridge 需参考逻辑重写。**注意：controller 的 MatrixSyncManager 从未被任何消费者调用，backoff/重登录路径未经验证。**

**约束 5：OpenSandbox 的生命周期模型**（`cimicode无状态方案.md` §四）

cimicode 的 OpenSandbox 插件 v2 只"使用"沙箱（getSandbox 验证状态 + resume + 执行命令 + renewTTL），不管理生命周期。**沙箱的创建/删除/pause/恢复是外部职责**——在 bridge 模型下，这个外部职责由 bridge 承担。

#### 4.1.2 架构设计

基于上述约束，bridge pod 的架构设计如下：

```
┌─ Bridge pod（每 agent 1 个）─────────────────────────────────────────┐
│                                                                       │
│  ┌─ Matrix 连接层 ──────────────────────────────────────────────┐    │
│  │ · /sync 长轮询（1 连接，since token 持久化 Redis）             │    │
│  │ · login + token 缓存 + 401 重登录 + backoff 重连              │    │
│  │ · E2EE（olm/megolm，每次重启重新协商）                         │    │
│  │ · 事件解析（m.room.message → body + sender）                   │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                              │                                        │
│  ┌─ @mention 过滤层 ─────────┴──────────────────────────────────┐    │
│  │ · requireMention（不 @自己 → 进 history buffer，不调 cimicode）│    │
│  │ · groupAllowFrom（非允许列表 → 丢弃）                          │    │
│  │ · peer-mentions off（worker @其他 worker → 不触发）            │    │
│  │ · 自 @过滤                                                     │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                              │ @自己                                  │
│  ┌─ 上下文构建层 ────────────┴──────────────────────────────────┐    │
│  │ · PG 查历史（按 sessionID，token 预算截断）                    │    │
│  │ · 视角转换（自己=assistant，别人=user）                        │    │
│  │ · 两段式格式注入（history buffer + current message）           │    │
│  │ · 协作上下文注入（从 env 读，拼进 system 字段）                │    │
│  │ · sessionID 生成（含 team/room/agent/date 维度）               │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                              │                                        │
│  ┌─ 沙箱 + 文件 sync 层 ─────┴──────────────────────────────────┐    │
│  │ · 调 cimicode 前：对象存储 → 沙箱（shared/ + 私有文件，增量）  │    │
│  │ · 调 cimicode 后：沙箱 → 对象存储（产物 + overrides，增量）    │    │
│  │ · 沙箱生命周期：懒建 → pause → resume → 回收备份               │    │
│  │ · outputs/ 检测 → 调 controller 回调                           │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                              │                                        │
│  ┌─ cimicode 调用层 ────────┴──────────────────────────────────┐    │
│  │ · POST /session/context_prompt（SSE）                         │    │
│  │ · SSE 消费超时（5min）+ 断开不重试                             │    │
│  │ · 并发控制（排队 + 取最新）                                    │    │
│  │ · 旁路落 PG（message.updated + message.part.updated）         │    │
│  └──────────────────────────┬──────────────────────────────────┘    │
│                              │ SSE 响应                                │
│  ┌─ Matrix 响应层 ──────────┴──────────────────────────────────┐    │
│  │ · NO_REPLY 检测（不发 Matrix）                                 │    │
│  │ · 响应以 agent bot 身份发回 room                              │    │
│  │ · 响应里 @mention 解析 → 按 peer-mentions 规则触发            │    │
│  │ · 清 history buffer                                           │    │
│  │ · pause 沙箱                                                  │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─ heartbeat 定时器（Leader only）─────────────────────────────┐    │
│  │ · 每 15m 调 cimicode（system=HEARTBEAT.md）                   │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─ 启动生命周期 ───────────────────────────────────────────────┐    │
│  │ · 分阶段等待 Matrix/Redis/PG → report-ready → 监听            │    │
│  └──────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
```

**设计决策与依据：**

**决策 1：每 agent 1 个 bridge pod（而非每 team 1 个集中 bridge）**

依据：OpenClaw 是分布式的（每 agent 1 pod），集中 bridge 会引入单点/瓶颈/爆炸半径（详见附录 A）。每 agent 1 个 bridge pod 保持分布式架构，爆炸半径和 OpenClaw 相当（1 个挂只影响 1 agent）。

代价：pod 数量没减（还是 N×T），省资源靠"每个 pod 更轻"而非"减少 pod 数量"。

**决策 2：bridge 用轻量技术栈（而非 Java/agi-context）**

依据：agi-context 是 Java Spring Boot（重），如果每 agent 一个 Java pod，资源比 openclaw 还重。bridge 只做 Matrix 监听 + SSE 转发 + 沙箱管理 + 文件 sync，不需要 Spring Boot 的重量级框架。用 Go/Bun/Python 轻量栈。

代价：agi-context/controller 的 Java 逻辑只能参考不能直接复用，需重写。

**决策 3：硬过滤在 bridge 层实现，软指令在 AGENTS.md 引导**

依据：OpenClaw 的 @mention 过滤/NO_REPLY 处理/peer-mentions 控制是 runtime 内置强制的。cimicode 没有这些内置行为。如果完全靠 LLM 遵守 AGENTS.md 指令文本，行为不可靠（LLM 可能不遵守）。因此涉及"不调 cimicode"或"不发 Matrix"的过滤必须在 bridge 层硬实现。LLM 指令文本只做引导（让 LLM 知道行为预期），bridge 硬过滤兜底。

代价：bridge 要复刻 OpenClaw runtime 的过滤逻辑，这些是全新代码。剩余风险在 LLM 输出端（@mention 格式不对、该 NO_REPLY 时回了内容），bridge 无法完全控制。

**决策 4：协作上下文通过 system 字段注入（而非合并进 AGENTS.md）**

依据：共享卷按 agent 类型组织（per-type），但协作上下文是 per-instance 的。如果合并进 AGENTS.md，要么失去类型复用（per-instance AGENTS.md），要么 bridge 要复刻 operator 的三层合并逻辑。通过 cimicode context_prompt 的 `system` 字段注入更简单——bridge 从环境变量读协作上下文，拼进 system 字段，cimicode 自动拼进 system prompt（`llm.ts:106`）。

代价：协作上下文占 system prompt 的 LLM context（几百 token）。协作上下文变更要重启 pod 更新 env（OpenClaw 通过 file-sync 动态更新）。

**决策 5：文件 sync 由 bridge 驱动（而非 agent 自己驱动）**

依据：OpenClaw agent 自己用 `mc` CLI 驱动 MinIO sync（`hiclaw-sync` 拉、`mc mirror` 推）。cimicode agent 在沙箱里没有 `mc` CLI 也没有 MinIO 凭证。且 cimicode 无状态，agent 不知道对象存储的存在。因此 sync 必须由 bridge 驱动——调 cimicode 前把文件准备好推进沙箱，done 后把产物拉回来。

代价：agent 丧失了自主 sync 能力（OpenClaw agent 可以自己决定何时 sync）。bridge 是每轮 @前后都 sync，不如 OpenClaw 灵活。AGENTS.md 里所有 `hiclaw-sync`/`mc mirror` 指令要改写。

#### 4.1.3 Matrix 连接设计

**问题分析：**

OpenClaw 的 Matrix 连接由 runtime 内置的 Matrix plugin 管理（基于 matrix-js-sdk），处理了 login、/sync 长轮询、E2EE、重连、重登录、since token 持久化等。这是经过实战验证的成熟代码。

bridge 要用不同技术栈从零实现等价能力。核心问题是 Matrix SDK 的成熟度和 E2EE 支持。

**方案设计：**

**login 策略：** bridge 启动时用 bot 账号密码 login（`POST /_matrix/client/v3/login`，`m.login.password`）。和 OpenClaw worker-entrypoint.sh:320-356 一致——每次 pod 启动重新 login 获取新 access token + device_id（因为 E2EE crypto state 每次清空重新协商）。

**/sync 长轮询：** `GET /_matrix/client/v3/sync?timeout=30000&since={token}`。30s long-polling，收到响应后解析 `rooms.join.{roomId}.timeline.events`，处理 `m.room.message` 事件。since token（`next_batch`）存 Redis（key `matrix:sync:since:{agentId}`，TTL 7 天），pod 重启后增量同步。

**重连与 backoff：** /sync 失败时指数退避（1s → 2s → 4s → ... → 30s 上限）。401 时重新 login 获取新 token。参考 controller MatrixSyncService 的 backoff 逻辑（`MatrixSyncService.java:244-251`），但注意该逻辑**从未被消费者调用，未经验证**。

**E2EE：** 如果 team room 启用 E2EE，bridge 需要 olm/megolm 客户端。每次 pod 重启 crypto state 丢失（和 OpenClaw worker-entrypoint.sh:314 `rm -rf` crypto storage 一样），重新 login 创建新 device_id + 重新协商 E2EE keys。

**技术栈选择对 Matrix 连接的影响：**

| 技术栈 | Matrix SDK | E2EE 支持 | 风险 |
|--------|-----------|----------|------|
| Go | mautrix-go | ✅ 支持 | SDK 生态小，边界情况可能未覆盖 |
| Bun | matrix-js-sdk | ✅ 支持 | 官方 SDK 但和 Bun 兼容性需验证 |
| Python | matrix-nio | ✅ 支持 | SDK 最成熟，但资源最重 |

**风险分析：**

1. **E2EE 是 Matrix 最复杂的部分**——device key 管理、key 分发/信任、session 管理。OpenClaw 的 Matrix plugin 基于 matrix-js-sdk 处理了这些。如果 bridge 技术栈的 Matrix SDK 对 E2EE 支持不完善，E2EE room 里的 agent 可能无法正常工作。**这是 Matrix 集成最大的技术风险。**

2. **controller 的 MatrixSyncService backoff/重登录路径未经验证**——bridge 参考的范式本身没有实战验证。backoff 策略、重登录时机、since token 损坏处理都可能有问题。

3. **since token 丢失导致全量重同步**——Redis 挂了或 token 过期，bridge 全量 /sync 收到大量历史消息，可能内存暴涨。需限制全量同步处理量。

4. **多个 bridge pod 同时 login 触发 Synapse 限流**——team 创建时 N 个 agent 同时启动，N 个 login 请求可能触发 429。需 login 重试 + 退避。

5. **Matrix 消息延迟**——OpenClaw 是同进程内 Matrix plugin 直接收消息（延迟极小）。bridge 模型下 agent 间协作经 Matrix Server 中转，/sync 有 1-5s 延迟。多跳协作（A → Leader → B）延迟叠加。

#### 4.1.4 上下文构建设计

**问题分析：**

cimicode 无状态不存历史，每次请求要传完整 context。OpenClaw 的上下文管理是 runtime 内置的（session .jsonl + history buffer + 两段式格式）。bridge 要从 PG 历史 + 实时消息组装 cimicode context_prompt 请求。

这是 bridge 最复杂的逻辑——不是简单的"查历史拼 JSON"，而是要精确复刻 OpenClaw 的上下文行为。

**方案设计：**

**sessionID 生成规则：**

sessionID 必须满足：同 agent 同 room 同天稳定（不能每次 @变，否则 PG 历史断了）；04:00 换新（对应 OpenClaw 的 daily reset）。

设计：`ses_{teamId}_{agentId}_{roomId}_{YYYYMMDD}`（简化格式，实际要符合 cimicode 的 SessionID schema：`ses_<12位hex降序时间戳><14位随机base62>`）。

bridge 每次 @时检查当前日期是否和 sessionID 里的日期一致，不一致则用新 sessionID。

**⚠️ 风险：** 生成规则错了会导致历史断裂（sessionID 变了 PG 查不到）或串会话（不同 agent/room 用了同样的 sessionID）。需确保 teamId/agentId/roomId/date 维度唯一。

**PG 历史查询与构建：**

bridge 按 sessionID 查 PG（和 agi-context 的 ContextBuilder 逻辑一致），取该 agent 今日所有对话轮次（messages + parts），构建 cimicode ExportData 格式的 `context`：
```json
{
  "info": { "id": "sessionID", "title": "..." },
  "messages": [
    { "info": { "id": "msg_...", "role": "user", ... }, "parts": [...] },
    { "info": { "id": "msg_...", "role": "assistant", ... }, "parts": [...] }
  ]
}
```

**视角转换：** PG 历史里的消息角色已经是 user/assistant（bridge 落 PG 时就转换好了），所以查出来直接用。bridge 落 PG 时做视角转换：该 agent 自己的响应 → assistant，别的 agent/human 的消息 → user。

**context 长度控制：** 取最近 N 轮（如 20 轮）。如果超 token 预算，截断最旧的。cimicode 有单次会话 compaction（`compaction.ts`），但跨 session 的历史在 PG——**cimicode 的 compaction 是否对 importContext 导入的历史生效需验证**。如果不生效，bridge 自己控制 context 长度。

**两段式群聊视野注入：**

这是复刻 OpenClaw 行为的核心。bridge 维护 per-room history buffer：

| 事件 | bridge 操作 |
|------|------------|
| 收到不 @自己的消息 | 存入该 room 的 history buffer（`{sender, body, messageId}`），≤50 条 FIFO |
| 收到 @自己的消息 | 取该 room 的 history buffer → 格式化为两段式 → 注入 parts → 调 cimicode |
| cimicode done | 清空该 room 的 history buffer |

两段式格式（复刻 `copaw/src/matrix/channel.py:1077-1125` 的 `_apply_history_to_parts`）：
```
[Chat messages since your last reply - for context]
senderA: 消息内容 [id:xxx]
senderB: 消息内容 [id:yyy]

[Current message - respond to this]
@agentA 帮我写个函数
```

注入到 cimicode context_prompt 的 `parts[0].text`。

**⚠️ 风险：** history buffer 重建不完整——bridge pod 挂了 buffer 丢失，重启后从 Matrix room timeline 拉最近消息重建，但 Matrix 默认只保留有限历史，且重建是事后拉取不是实时维护。agent 可能失去部分群聊视野。

**协作上下文注入：**

bridge 从环境变量读协作上下文（`COORDINATION_ROLE`/`COORDINATION_LEADER`/`COORDINATION_TEAM`/`COORDINATION_ROOM`/`COORDINATION_WORKERS` 等），构建 Coordination 文本，拼进 cimicode context_prompt 的 `system` 字段。

cimicode 的 system prompt 组装（`llm.ts:100-108`）：
```
system[0] = agent.prompt（cimicode.json 配置）
system[1] = instruction.system()（共享卷 AGENTS.md + skills 列表）
system[2] = input.system（bridge 传的协作上下文）
```

**⚠️ 风险：** 协作上下文变更（team 成员加/删）要重启 pod 更新 env。OpenClaw 通过 operator reconcile + file-sync 动态更新 AGENTS.md，bridge 模型下要么重启 pod，要么 bridge 从 API 动态查（增加复杂度）。

#### 4.1.5 cimicode 调用与 SSE 消费设计

**问题分析：**

cimicode context_prompt 是 SSE 长连接，一个请求可能持续几十秒到几分钟。bridge 消费 SSE 时阻塞。且 cimicode 无状态——pod 挂了 session 不在，重试 = 从头跑。

**方案设计：**

**SSE 消费：** bridge 调 cimicode `POST /session/context_prompt`（Accept: text/event-stream），消费 SSE 事件流：
- `message.part.delta`：增量片段 → 累积（不发 Matrix，等完整响应）
- `message.part.updated`：完整 part → 旁路落 PG
- `message.updated`：消息元信息 → 旁路落 PG
- `done`：流结束 → flush PG + 清 history buffer + 完整响应发 Matrix + pause 沙箱
- `session.error`：错误 → 发 Matrix 错误消息

**SSE 超时：** bridge 设 SSE 消费超时（5 分钟可配置）。超时取消 SSE 连接 + 发 Matrix "处理超时"。**必须设超时**——cimicode 的 SSE readTimeout=0（永不超时），不设超时 cimicode 卡住时 bridge 永久阻塞。

**SSE 断开处理：** cimicode pod 挂了 SSE 断开。**不自动重试**——cimicode 无状态，重试时 K8s service 负载均衡可能命中不同 pod，session 不在（InMemorySessionStore 30min TTL），重试 = 从头跑（重新 importContext + prompt），之前已生成的部分响应丢失。方案：发 Matrix "处理中断，请重新 @我"。

**并发控制：** cimicode 正在跑时新 @排队（最多 3 条），done 后取最新的 1 条处理（旧的丢弃，可能已过时）。

**设计依据：** 不并发的原因——cimicode 的 session 是同一个 sessionID，两个请求同时 importContext + prompt 可能冲突（InMemorySessionStore 并发安全未验证）。OpenClaw 有 `maxConcurrent: 4` 允许并发，但 OpenClaw 的 session 是有状态的（本地 .jsonl），并发访问有文件锁。cimicode 无状态 session 模型不支持安全并发。

**响应转发 Matrix：**

bridge 消费完 SSE 后，把完整响应以该 agent bot 身份发到 Matrix room（`POST /_matrix/client/v3/rooms/{roomId}/send/m.room.message`）。

**NO_REPLY 检测：** 如果 cimicode 响应文本是 `NO_REPLY`（精确匹配或 trim 后匹配），bridge 不发 Matrix（静默）。对应 OpenClaw 的 NO_REPLY 协议（`worker-agent/AGENTS.md:107-114`）。

**响应里 @mention 解析：** bridge 从响应文本里解析 @mention（正则 `@[\w.-]+:[\w.-]+`），按 peer-mentions 规则处理——默认 worker @其他 worker 不触发（peer-mentions off），Leader @worker 触发。

**⚠️ 风险：**

1. **SSE 长连接阻塞**——cimicode 卡住时 bridge 永久阻塞（即使设了超时，5 分钟内该 agent 瘫痪）。OpenClaw 的 LLM 调用也有超时，但 OpenClaw 的 runtime 有更细粒度的控制（如工具调用超时、单步超时）。

2. **不自动重试的代价**——cimicode pod 挂了，用户要重新 @，之前的工作白费。OpenClaw pod 挂了 K8s 重启后 session 还在（本地 .jsonl + MinIO 同步），可以继续。cimicode 无状态 session done 后就删了，无法续。

3. **Matrix 消息碎片化**——bridge 等完整响应再发一条 Matrix 消息，用户等待时间长（几十秒到几分钟无反馈）。OpenClaw 支持 `streaming: partial`（`worker-openclaw.json.tmpl:34`）——partial 流式输出。bridge 要复刻流式行为，但 Matrix 协议不支持消息流式编辑。**需调研 OpenClaw 的流式输出机制并复刻，或接受完整响应再发。**

4. **并发控制用户体验差**——排队 + 取最新意味着中间 @被丢弃。用户 @了但没响应。OpenClaw 的 maxConcurrent: 4 至少能并发响应。

#### 4.1.6 沙箱管理与文件 sync 设计

**问题分析：**

OpenClaw agent 在 pod 本地 fs 干活，自己用 `mc` CLI 驱动 MinIO sync（3 套同步路径：变更触发推送、按需拉取、openclaw.json 合并）。cimicode agent 在远程沙箱干活，没有 `mc` CLI 也没有 MinIO 凭证。且 cimicode 无状态——sandboxID 从请求传入，cimicode 只"使用"沙箱不管理生命周期。

**bridge 要承担两个新职责：沙箱生命周期管理 + 文件 sync。** 这两个在 OpenClaw 里分别是"pod 本地 fs 自动有"和"agent 自己用 mc 驱动"的。

**沙箱生命周期管理设计：**

| 状态 | 触发 | bridge 操作 | 风险 |
|------|------|------------|------|
| 不存在 | 首次 @ | 调 OpenSandbox Server 建沙箱 + 轮询等 Running（60s 超时）+ 从对象存储恢复工作区 | 冷启动 7-10s 用户等待 |
| Running | @到时已 Running | 直接用 | — |
| Paused | cimicode done 后 | 调 Server pause | pause 可靠性未验证 |
| Paused | 再次 @ | 调 Server resume + 轮询等 Running | resume 可能失败 |
| 长期空闲 | TTL 到期 | Server 自动回收 | bridge 要在回收前备份 |
| 回收后 | 再次 @ | 重建沙箱 + 从对象存储恢复 | 恢复可能不完整 |

**文件 sync 设计：**

**调 cimicode 前（对象存储 → 沙箱）：**
1. 从对象存储拉 `shared/`（tasks/projects/knowledge）→ 推进沙箱 `/workspace/shared/`
2. 从对象存储拉 agent 私有文件（`memory/`、`outputs/`、`overrides/`）→ 推进沙箱
3. 增量同步：bridge 记录文件 etag，下次只拉变更的（S3 ListObjects + etag 比较）

**cimicode done 后（沙箱 → 对象存储）：**
1. 从沙箱拉变更文件（`result.md`/`outputs/`/`memory/`/`overrides/`）→ 推回对象存储
2. 检测 `outputs/` 新文件 → 调 controller 回调（对应 OpenClaw notify-platform.sh）
3. pause 沙箱

**沙箱回收备份：**
- 沙箱 TTL 回收前，bridge 把沙箱里未推回的变更推到对象存储
- 再次 @到时重建沙箱，从对象存储恢复 `memory/`/`outputs/`（`shared/` 每次拉最新）

**设计依据：**

sync 由 bridge 驱动而非 agent 驱动——因为 cimicode agent 在沙箱里没有 `mc` CLI 也没有对象存储凭证，且 cimicode 无状态不知道对象存储存在。bridge 是唯一能访问对象存储和沙箱的组件。

增量同步而非全量——每轮 @前后都全量同步太慢（shared/ 可能有很多文件）。按 etag 增量只同步变更的文件。

**⚠️ 风险：**

1. **sync 时机性能**——每轮 @前后都 sync，即使增量也有 ListObjects API 调用 + etag 比较开销。文件多时延迟几秒到几十秒。OpenClaw 是 agent 自己按需 sync（`hiclaw-sync` 只在需要时执行一次），cimicode bridge 是每轮都 sync。

2. **沙箱 pause/resume 可靠性未验证**——OpenSandbox 技术实现是黑盒。pause 后文件是否真保留？resume 后状态是否正确？如果 resume 失败 bridge 要重建沙箱 + 从对象存储恢复——但对象存储可能没有最新的沙箱内文件（如果上次 done 后 sync 失败了）。

3. **沙箱回收后文件丢失风险**——如果 bridge 没来得及备份（如 bridge 也挂了），沙箱里的未推回变更丢失。**bridge 挂了谁来备份是个边缘场景风险。**

4. **权限模型变更**——OpenClaw 靠 MinIO 用户权限控制（Worker 只能写 `agents/{name}/` 和 `shared/`，其他 403）。cimicode bridge 统一操作对象存储，**权限控制从"per-agent MinIO 用户"变成"bridge 内部逻辑"**——无技术强制只有逻辑保证。bridge 有 bug 写错路径可能覆盖其他 agent 文件。

5. **对象存储 API 调用量**——每次 @前后 ListObjects + GetObject/PutObject，大量 @时 API 调用量高。可能性能瓶颈或费用问题。

6. **沙箱文件系统与对象存储一致性**——bridge 推文件到沙箱后 agent 才能读。如果推的过程中有延迟或失败，agent 可能读到不完整的文件。需确保 sync 完成后才调 cimicode。

#### 4.1.7 heartbeat 设计

**问题分析：**

OpenClaw 的 heartbeat 是 runtime 内置 cron（每 15m），自动读 HEARTBEAT.md 执行检查。cimicode 无定时机制。heartbeat 由 bridge 定时器触发。

**方案设计：**

Leader 的 bridge pod 内定时器（每 15m）：
1. 从对象存储读 `overrides/HEARTBEAT.md`（Leader 修改后的版本，没修改则用基线）
2. 构造 cimicode 请求：`system=HEARTBEAT.md 内容`，`parts=[{type:"text", text:"heartbeat"}]`，`sessionID=leader 的 sessionID`，`sandboxID=leader 的沙箱`
3. 调 cimicode → LLM 执行 heartbeat 检查（检查 Worker 状态、催进度）
4. SSE 响应以 Leader bot 身份发回 Matrix room
5. 如果 Leader 修改了 HEARTBEAT.md（在沙箱里改）→ done 后同步回 `overrides/`

**heartbeat 期间 Leader 可能 @worker**——heartbeat 的 cimicode 响应里如果 @了 worker，bridge 解析并触发对应 worker 的 cimicode 调用。和正常 @mention 处理一样，但触发源是定时器。

**⚠️ 风险：**

1. **heartbeat 与正常 @的并发**——heartbeat 正在跑时有人 @了 Leader，排队 + 取最新可能丢弃 heartbeat。需决策优先级。

2. **heartbeat 基础负载**——每 15m 每个 Leader 一次 cimicode 调用。T 个 team = 每 15m T 次调用。cimicode 集群要承受这个基础负载。

#### 4.1.8 启动生命周期设计

**问题分析：**

bridge 启动时要连 Matrix + Redis + PG，依赖服务没准备好会 CrashLoopBackOff。OpenClaw 的 worker-entrypoint.sh 有详细的等待重试逻辑（等配置文件出现、等 Matrix 连接）。

**方案设计：**

分阶段等待 + 重试：
```
Phase 1: 等 Matrix（重试 login 10 次，间隔 5s）→ /sync 初始化 → 存 since token
Phase 2: 等 Redis（重试 10 次，间隔 5s）→ 读 sessionID↔sandboxID 映射
Phase 3: 等 PG（重试 10 次，间隔 5s）
Phase 4: 沙箱懒建（第一次 @时才建）
Phase 5: report-ready → HiClaw operator
Phase 6: Leader 启 heartbeat 定时器
Phase 7: 进入 /sync 监听
```

**⚠️ 风险：**

1. **ready 判定语义变更影响 controller 联动**——OpenClaw ready = "容器连上 Matrix"。bridge ready = "bridge 连 Matrix + Redis/PG 就绪"。controller 的 `waitForTeamMembersAndSetDisplayNames`（`TeamService.java:911`）等 ready 后设 displayName——ready 标准变了，controller 联动要改。

2. **displayName 设置时机**——OpenClaw 连 Matrix 时自动设 displayName，controller 等 ready 后设最终值（因为 OpenClaw 第一次连会覆盖）。bridge 自己设还是 controller 设？什么时机？需协调。

#### 4.1.9 可复用分析

**可参考（Java 栈，需用 bridge 技术栈重写）：**

| 来源 | 逻辑 | 重写原因 |
|------|------|----------|
| agi-context CimicodeClient | OkHttp SSE 消费 | Java→bridge 栈 |
| agi-context ContextBuilder | PG→context 构建 | Java→bridge 栈 |
| agi-context ChatHistoryRecorder | SSE 旁路落 PG | Java→bridge 栈 |
| agi-context SandboxClient | OpenSandbox 建沙箱+轮询 | Java→bridge 栈 |
| controller MatrixClient | Matrix CS+Admin API | Java→bridge 栈 |
| controller MatrixSyncService | /sync 长轮询+backoff+重登录 | Java→bridge 栈，且未经验证 |
| CoPaw channel.py | history buffer+两段式格式 | Python→bridge 栈，有参考实现 |
| OpenClaw worker-entrypoint.sh | 启动等待重试 | Shell→bridge 栈 |

**必须新写（全仓库不存在）：**

- @mention 检测 + 路由（grep "mention" 无命中）
- 协作上下文构建 + system 字段注入
- 并发控制（排队 + 取最新）
- heartbeat 定时器
- SSE 响应转发 Matrix（多身份 + NO_REPLY 检测）
- 文件 sync（对象存储 ↔ 沙箱增量同步）
- 沙箱 pause/resume/回收/备份/恢复
- sessionID 生成（含日期维度）
- overrides/ 检测与恢复

**⚠️ 核心风险评估：**

bridge 的职责范围是 12 类（Matrix 连接/@mention 过滤/上下文构建/SSE 消费/沙箱管理/文件 sync/heartbeat/PG 持久化/并发控制/启动依赖/协作上下文注入/overrides 管理）。这相当于一个完整 agent runtime 的功能集（只是不跑 LLM）。

OpenClaw 这些能力分散在 runtime 内置（Matrix plugin + gateway + cron + file-sync skill + mcporter），经过实战验证。bridge 要用全新代码从零实现等价功能，且用不同技术栈重写 Java 逻辑。**用未经验证的新代码替换经过实战验证的成熟代码，本身就是风险。任何一个边界情况处理不当（E2EE key 协商失败、since token 损坏、@mention 解析错误、history buffer 重建不完整、沙箱 resume 失败、文件 sync 不一致），agent 协作就会出问题。**

### 4.2 共享 cimicode 集群（大脑）

**角色：** 无状态 LLM 推理引擎，所有 team 所有 agent 共享，按 sessionID 路由。

**部署：** K8s Deployment，2-3 副本，serve 模式，挂共享配置卷。

**尽量不改代码**——无状态方案现成。**但"不改代码"是前提假设**，需验证：
- 多 session 并发：InMemorySessionStore（30min TTL）多 session 同时存在是否 OOM？
- 多 directory 隔离：N 个 agent 类型 = N 个 directory = N 个 InstanceState 同时存在是否正确工作？
- context_prompt 并发安全：多并发请求 InMemorySessionStore 是否线程安全？
- directory 级配置隔离：多 directory 配置是否互相串？

**如果任何一项验证不通过，就要改 cimicode 代码——前提可能不成立。**

**⚠️ 并发瓶颈：** 2-3 pod 服务所有 agent。高并发时可能不够，要扩缩。cimicode pod 资源占用取决于并发量和模型大小。

### 4.3 OpenSandbox（手脚）

**角色：** 远程代码执行沙箱，替代 openclaw pod 本地 fs。每 agent 1 个。

**⚠️ 资源关键点：** N 个 agent = N 个沙箱，数量没减。沙箱 Running 时资源可能和 openclaw pod 本地 fs 相当。只有 pause/回收才省。

**⚠️ 技术黑盒：** OpenSandbox 的技术实现（容器？microVM？WASM？）、资源模型、pause/resume 可靠性、TTL 回收后文件是否真丢——**目前是黑盒，必须调研清楚。**

### 4.4 共享配置卷

按 agent 类型组织 `/{agentType}/`（AGENTS.md cimicode-agent 模板 + skills + cimicode.json per-type MCP）。cimicode 的 directory 指向它，原生读取。

**⚠️ 共享卷并发写入风险：** 两个 team 同时创建且用了同一 agent 类型时，并发写同一个 `/{agentType}/` 路径。需锁或 CAS 机制。RWX 卷并发写入一致性需验证。

### 4.5 对象存储

agent 间共享文件 + 产物持久化 + per-instance overrides/（SOUL.md/HEARTBEAT.md 自我修改）。复用现有 MinIO/S3。

### 4.6 HiClaw Operator

**调谐模型不变**（每 member 1 pod），但**几乎每步调谐都要改**：

| 调谐步骤 | 当前（openclaw） | cimicode-bridge 改什么 |
|----------|------------------|------------------------|
| ReconcileMemberInfra | Matrix bot provision + gateway consumer | Matrix bot provision 保留；gateway consumer 不需要 |
| ReconcileMemberConfig | GenerateOpenClawConfig + GenerateMcporterConfig + 推 OSS | **大改/新写**：GenerateBridgeConfig（cimicode endpoint/sandbox/协作上下文 env）；cimicode.json MCP 配置；AGENTS.md/skills 推共享卷；package 解压分流 |
| ReconcileMemberContainer | EnvBuilder openclaw env + backend Create | env 完全不同；image 换 bridge；volumeMounts 不同（共享卷） |
| summarizeBackendReadiness | ready = openclaw 连 Matrix | ready = bridge 连 Matrix + 沙箱/会话已建（语义变更） |
| handleDelete | 删 pod + 清 Matrix/room/OSS | 加清沙箱 + 清会话 |
| EnvBuilder | openclaw 专用 env | **重写** |
| agentconfig.Generator | GenerateOpenClawConfig（~450 行） | **新写** GenerateBridgeConfig |
| Deployer | 推 OSS | 推共享卷 + 对象存储 |
| 启动脚本 | worker-entrypoint.sh（openclaw 专用） | **新写** bridge 启动脚本 |
| 双 runtime 分流 | 无 | 每步 if/else 分支 + 兼容验证 |

### 4.7 agi-agentteams-controller

createTeamCR 传 runtime=cimicode-bridge；package 解压分流（指令→共享卷，工作区→对象存储）。

### 4.8 Agent 模型

类型层（共享卷 `/{agentType}/`，多 team 共享）+ 实例层（每 agent 独立 bridge pod + 沙箱 + 会话，隔离）。

### 4.9 指令体系翻译方案

**这是方案的核心难点之一。** OpenClaw 和 cimicode 是两个不同运行时，指令体系不兼容。文件不能直接同步复用，要翻译/重写。

#### 4.9.1 SOUL.md → AGENTS.md Identity 章节

cimicode 不认 SOUL.md。方案：cimicode-agent 的 AGENTS.md 模板第一章节是 `# Identity`，内容来自 SOUL.md。cimicode 读 AGENTS.md → LLM 在 system prompt 看到 Identity → 等效于 OpenClaw 读 SOUL.md。

#### 4.9.2 HEARTBEAT.md → bridge 定时注入 system 字段

cimicode 不认 HEARTBEAT.md。方案：HEARTBEAT.md 存对象存储（per-instance），bridge heartbeat 定时器读内容作为 system 字段传 cimicode。

#### 4.9.3 openclaw.json → cimicode.json（只保留需要的）

大部分配置不需要（cimicode 自己连 LLM、不连 Matrix、无 dreaming）。只保留 MCP 配置 + model 配置，转成 cimicode.json 格式。operator 的 `GenerateMcporterConfig` 改成 `GenerateCimicodeMCPConfig`。

#### 4.9.4 AGENTS.md 重写

为 cimicode-agent 新写一套 AGENTS.md 模板（worker 和 leader 各一套），去掉 OpenClaw 专属指令：

| OpenClaw 指令 | cimicode 改成 |
|--------------|--------------|
| `hiclaw-sync` | "文件已自动同步，直接读写即可" |
| `mc mirror` / `mc cp` | "完成工作后文件自动同步到共享存储" |
| `mcporter` | "MCP 工具已配置好，直接使用" |
| `HICLAW_MATRIX_DOMAIN` | bridge 注入协作上下文时写进 Coordination |
| 两段式格式/NO_REPLY/@mention | 保留说明（引导 LLM），bridge 硬过滤兜底 |
| `openclaw.json` / `HICLAW_STORAGE_PREFIX` | 删除 |

**cimicode-agent AGENTS.md 模板结构：**
```
# Identity（← 原 SOUL.md）
## Role / Core Rules

# Behavior（← 原 AGENTS.md 重写）
## Task Execution（去掉 hiclaw-sync/mc mirror）
## Communication（@mention 协议保留，说明 bridge 处理）
## Memory（沙箱里操作，bridge 自动 sync）
## Safety

# Coordination（← bridge 通过 system 字段注入，per-instance）

# Skills（cimicode 自动从 skills/ 加载注入）
```

#### 4.9.5 skills 处理

| skill | 处理 |
|-------|------|
| file-sync | 废弃（bridge 自动 sync）；沙箱放 `hiclaw-sync` stub |
| find-skills | 废弃（cimicode 有自己的 skill 机制） |
| mcporter | 废弃（cimicode 原生 MCP） |
| project-participation | 保留（纯协议文本，不依赖脚本） |
| task-progress | 保留（纯格式说明） |

#### 4.9.6 seed-only 语义

OpenClaw 的 SOUL.md/HEARTBEAT.md 是 seed-only（agent 可自我修改）。cimicode 模型下共享卷只读。

**方案：** agent 自我修改存对象存储 `agents/{teamId}-{agentName}/overrides/`。bridge 调 cimicode 前从 overrides/ 读（覆盖基线）；done 后检测沙箱修改同步回 overrides/。

**如果 agent 不需要自我进化人格**（SOUL.md 固定不变），可简化。**需确认现有 agent 是否依赖此能力。**

#### 4.9.7 "软指令"替换"硬机制"

OpenClaw 很多行为是 runtime 内置强制的，cimicode 没有这些内置行为。

**方案：bridge 层硬过滤 + AGENTS.md 软引导双层保障。**

| OpenClaw 硬机制 | bridge 硬实现 | AGENTS.md 软引导 |
|----------------|-------------|-----------------|
| @mention 过滤 | bridge 只处理 @自己的消息 | "你只在被 @mention 时被唤醒" |
| groupAllowFrom | bridge 只处理允许列表 | "只响应 coordinator/admin 的 @" |
| NO_REPLY | bridge 检测 NO_REPLY 不发 Matrix | "无事可说时回复 NO_REPLY" |
| 两段式格式 | bridge 注入 | "消息可能含两段" |
| history buffer ≤50 | bridge 维护 | "history 是群聊上下文" |
| session 日重置 | bridge 实现 sessionID 日期维度 | 无需 LLM 知道 |
| peer-mentions off | bridge 不触发 | "不要 @其他 worker" |

**核心原则：涉及"不调 cimicode"或"不发 Matrix"的硬过滤，都在 bridge 强制实现，不依赖 LLM 遵守指令文本。**

**剩余风险：** LLM 输出端可能不严格遵守协议（@mention 格式不对、该 NO_REPLY 时回了内容）。bridge 无法完全控制 LLM 输出，但硬过滤保证不触发错误协作。**这些剩余风险和 OpenClaw 也有类似之处**——OpenClaw 的 NO_REPLY 检测也是文本匹配，不是 LLM 内置的。

### 4.10 文件产物同步方案

**核心变化：** 从"agent 自己用 mc 驱动 MinIO sync"变成"bridge 驱动对象存储 ↔ 沙箱 sync"。

#### 4.10.1 当前 OpenClaw 的文件同步机制（3 套路径）

**路径一：Local → Remote（变更触发推送，`worker-entrypoint.sh:178-250`）**
- 后台循环每 5s 扫描 workspace，`find -newer .last-pull` 检测变更
- 有变更 → `mc mirror workspace/ → MinIO agents/{workerName}/`（排除 openclaw.json/matrix/canvas/credentials/caches）
- SOUL.md/AGENTS.md/HEARTBEAT.md 单独按 mtime 判断（只在 agent 自己修改后推送）
- outputs/ 有新文件 → 触发 notify-platform.sh 回调平台

**路径二：Remote → Local（按需拉取 + 5 分钟兜底，`hiclaw-sync.sh`）**
- 按需：agent 被 @告知"文件更新了" → 执行 `hiclaw-sync` → `mc mirror MinIO → workspace`
- 兜底：每 5 分钟自动拉 Manager-managed 文件（openclaw.json merge、mcporter.json、skills/、shared/）
- 拉完后 `touch .last-pull` 更新标记，防止循环推回

**路径三：openclaw.json 特殊合并（local-first merge，`merge-openclaw-config.sh`）**
- 本地 openclaw.json 为 base，MinIO overlay 只覆盖 models/gateway/channels/plugins
- 防止覆盖 agent 本地自定义的 plugin 配置（如 dreaming schedule）

**共享空间 `/root/hiclaw-fs/shared/`：**
- tasks/{task-id}/spec.md（Leader 写，Worker 读）
- tasks/{task-id}/result.md（Worker 写，Leader 读）
- tasks/{task-id}/base/（Leader 写的参考文件，只读）
- projects/{project-id}/plan.md（Leader 写，所有 Worker 读）
- knowledge/、uploads/（团队共享）

**MinIO 写权限控制：** Worker 只能写 `agents/{workerName}/` 和 `shared/`，其他路径 403。

#### 4.10.2 替换后的同步方案

```
@agentA → bridge pod
  ① 调 cimicode 前：从对象存储拉 shared/ + agent 私有文件 → 推进沙箱（增量同步）
  ② 调 cimicode → agent 在沙箱里干活（读 spec.md、写 result.md）
  ③ done 后：从沙箱拉变更 → 推回对象存储 + 检测 outputs/ 新文件 → 调 controller 回调
  ④ pause 沙箱
```

**增量同步实现：** bridge 记录每次 sync 时文件 mtime，下次只拉/推变更的。对象存储用 S3 API（ListObjects + etag 比较）。

**沙箱回收恢复：** 回收前 bridge 把未推回变更推到对象存储。重建时恢复 memory/outputs/（shared/ 每次拉最新不需恢复）。

**难点：**
1. **sync 时机性能**——每轮 @前后都 sync，文件多/大时有延迟。需增量同步。
2. **agent 不知道 sync**——AGENTS.md 里 `hiclaw-sync`/`mc mirror` 指令要改掉（沙箱里没有这些命令）。或沙箱放 `hiclaw-sync` stub（no-op）。
3. **沙箱回收恢复**——回收前备份未推回变更；重建时恢复 memory/outputs/。
4. **并发写 shared/**——靠"不同 agent 写不同子目录"避免（Worker 写自己 task 目录，Leader 写 spec.md）。
5. **权限模型变更**——从"per-agent MinIO 用户权限"变成"bridge 内部逻辑"——bridge 要确保只写正确路径。安全模型变更。

### 4.11 协作机制与功能保持

**核心问题：怎么保持原有 team 形态的 Leader-Worker 协作功能不变。**

#### 4.11.1 当前 OpenClaw 的 Leader-Worker 协作机制

**Team Leader 职责**（`team-leader-agent/AGENTS.md`）：把外部请求变成 Projects 和 DAG 工作；给 Workers 分配任务；收集 Worker 结果、检查任务结果、推进或重新规划 DAG；向 requester 报告进度/阻塞/最终结果；heartbeat 定期巡查。

**Worker 职责**（`worker-agent/AGENTS.md`）：被 @到后先 `hiclaw-sync` 拉文件 → 读 task spec（`shared/tasks/{task-id}/spec.md`）→ 在 task 目录干活（写 plan.md/result.md/progress/）→ `mc mirror` 推 MinIO + @Leader(TASK_COMPLETED) → 被阻塞时 @Leader(BLOCKED/QUESTION)。

**协作流：**
```
Requester → @Leader → Leader 创建 Project + DAG + 分配 task
  → Leader 写 spec.md 到 shared/tasks/{task-id}/ → 推 MinIO
  → Leader @Worker
  → Worker hiclaw-sync 拉 spec.md → 干活 → 写 result.md → mc mirror 推 MinIO
  → Worker @Leader(TASK_COMPLETED)
  → Leader hiclaw-sync 拉 result.md → 读结果 → 更新 DAG → 分配下一个 task
```

#### 4.11.2 替换后的协作保持方案

| 协作元素 | OpenClaw | cimicode bridge | 方案 |
|----------|----------|-----------------|------|
| @mention 协议（TASK_COMPLETED/BLOCKED/QUESTION/PHASE{N}_DONE） | runtime 过滤 | bridge 硬过滤 | §4.1.1 |
| NO_REPLY 协议 | runtime 处理 | bridge 检测不发 Matrix | §4.9.7 |
| 两段式消息格式（history + current） | runtime 注入 | bridge 注入 | §4.1.2 |
| file-sync（@到后先 sync） | agent `hiclaw-sync` | bridge 自动 sync | §4.10 |
| shared/ 文件交换（spec.md/result.md/plan.md） | MinIO + mc | 对象存储 + bridge sync | §4.10 |
| heartbeat（Leader 定期巡查） | runtime cron | bridge 定时器 | §4.1.6 |
| peer-mentions 默认 off | 配置级 | bridge 配置级 | §4.1.1 |
| outputs/ 回调 | notify-platform.sh | bridge 检测 + 调 controller | §4.10 |
| Leader 查 Worker 状态 | `hiclaw get workers` | **需替代方案** | §4.11.3 |

#### 4.11.3 Leader hiclaw CLI 替代方案

Leader 协调时要查 Worker 状态（`hiclaw get workers`）、读 Worker 产物（`hiclaw-sync`），cimicode 模型下这些都没有。

**方案：** bridge 提供 HTTP API（/team/workers/status）给 Leader 的 cimicode 查（通过 MCP 工具或自定义工具）。或 heartbeat 时 bridge 把 Worker 状态注入 context。

**⚠️ 这是个待设计的替代方案，需验证可行性。**

#### 4.11.4 功能保持的难点

1. **AGENTS.md/SOUL.md 里的指令要改**——原文里有 `hiclaw-sync`、`mc mirror`、`mc cp` 等命令，cimicode agent 在沙箱里没有这些命令。需重写指令（详见 §4.9）。
2. **5 个 worker-agent builtin skills 要逐个处理**——file-sync（废弃/stub）、find-skills（废弃）、mcporter（废弃）、project-participation（保留）、task-progress（保留）。skill 里的 scripts/ 可能依赖 `mc`/`hiclaw-sync`/`hiclaw` CLI（详见 §4.9.5）。
3. **Leader 的 skills**（team-coordination/organization/project-management/task-management/file-sharing）——可能不依赖 `mc`/`hiclaw-sync`（协调指令不是文件操作），但需逐一验证。
4. **HEARTBEAT.md**——heartbeat 时 Leader 可能要检查 Worker 状态（读 Worker 的 result.md），需 Leader 的 bridge 从对象存储拉 Worker 产物进 Leader 的沙箱。**Leader 也要有文件 sync 能力。**
5. **project-participation skill** 里的多 Worker 项目协作协议（Phase handoff、DAG 推进）——靠 shared/ 文件 + @mention 实现，底层变了但协议不变。**需验证协议在新文件 sync 模型下还能正常工作。**

### 4.12 原有工具/命令替换映射

| 原有命令 | 用途 | cimicode 模型 | 谁负责 |
|---------|------|--------------|--------|
| `hiclaw-sync` | 拉 MinIO | bridge 自动 sync | bridge |
| `mc mirror`/`mc cp` | 推/拉 MinIO | bridge done 后推回 | bridge |
| `mcporter` | 调 MCP | cimicode 原生 MCP | cimicode |
| `hiclaw worker report-ready` | 报告 ready | bridge 自己 report | bridge |
| `hiclaw get workers` | Leader 查状态 | 待设计替代方案 | bridge/待定 |
| `openclaw gateway run` | 启动 runtime | bridge 调 cimicode | bridge |
| `mcporter-servers.json` | MCP 配置 | cimicode.json mcp | operator |
| `openclaw.json` | 运行配置 | bridge 配置 | operator |

### 4.13 初始化文件生成与协作上下文注入

**Team 创建时，operator 的 Deployer 不仅创建 pod，还生成一系列初始文件推到 OSS，定义 agent 身份、协作关系、工具使用方式。如果这些文件生成不正确，agent 协作功能直接断裂。**

#### 4.13.1 operator 生成的文件

Deployer.DeployWorkerConfig（`deployer.go:164-351`）按顺序生成并推送：

| 文件 | 生成方式 | 作用 | seed 语义 |
|------|----------|------|-----------|
| openclaw.json | GenerateOpenClawConfig（~450 行 Go） | 运行配置（models/gateway/channels/matrix/plugins/session/dreaming） | 每次调谐重新生成 |
| SOUL.md（Worker） | 模板 or spec.soul or 默认 | agent 人格 | **seed-only**（首次写不覆盖） |
| SOUL.md（Leader） | SOUL.md.tmpl 模板渲染（`${TEAM_LEADER_NAME}`/`${TEAM_NAME}`/`${TEAM_WORKERS}`） | Leader 人格含团队上下文 | **seed-only** |
| mcporter-servers.json | GenerateMcporterConfig | MCP 配置 | 每次重新生成 |
| credentials/matrix/password | provision 结果 | Matrix 密码（E2EE 重新登录） | 写入 |
| HEARTBEAT.md（Leader） | 模板 | heartbeat 检查清单 | **seed-only** |
| AGENTS.md | 三层 merge + 协作上下文注入 | 行为指令 | 每次 reconcile 重新 merge |
| skills/（builtin） | 从镜像模板复制 | SKILL.md + scripts/ | 变更时才推 |

#### 4.13.2 AGENTS.md 三层合并机制

AGENTS.md 不是简单复制，是**三层合并**（`prepareAndPushAgentsMD:827`）：

```
第一层：builtin AGENTS.md 模板
  ↓ MergeBuiltinSection（<!-- hiclaw-builtin-start/end --> 标记包裹，保留标记外用户内容）
第二层：package/OSS 提供的 AGENTS.md（或 inline spec.agents）
  ↓ InjectCoordinationContext（<!-- hiclaw-team-context-start/end --> 标记注入协作上下文）
第三层：最终 AGENTS.md（推 OSS）
```

**InjectCoordinationContext**（`coordination.go:37`）根据角色注入不同协作上下文：

**Leader 注入：**
```
## Coordination
- Upstream coordinator: @manager:{domain}
- Team: {teamName}
- Team Room: {teamRoomId}——@mention workers here
- Heartbeat interval: {heartbeatEvery}
- Team Workers: @workerA:{domain}——Room: {roomId}, @workerB:{domain}——Room: {roomId}
- 你分解任务、分配给 workers、决定 wake/sleep
```

**Worker 注入：**
```
## Coordination
- Coordinator: @{teamLeaderName}:{domain}（Team Leader of {teamName}）
- 报告完成/阻塞/问题给 coordinator
- 不要直接 @mention Manager——通过 Team Leader
```

**⚠️ 这些协作上下文是 agent 能正确协作的关键。** 没有这些注入，agent 不知道 coordinator 是谁、Team Room 在哪、该 @mention 谁。

#### 4.13.3 cimicode 模型下的文件生成与注入方案

**根本矛盾：** 共享卷按 agent 类型组织（per-type），但协作上下文是 per-instance 的（每个 team 每个 agent 不同）。

**方案（§4.1.4 已详述）：** 共享卷放 per-type 基线 AGENTS.md（不含 Coordination）；bridge 调 cimicode 时通过 `system` 字段注入 per-instance 协作上下文（从环境变量读）。

**operator 改动：**
- 不再生成 openclaw.json → 改生成 bridge 配置（cimicode endpoint/协作上下文 env/共享卷路径）
- 不再生成 mcporter-servers.json → 改生成 cimicode.json 的 mcp 配置
- AGENTS.md/skills 推共享卷而不是 OSS
- 协作上下文写进 bridge pod 环境变量（不再注入 AGENTS.md）
- SOUL.md/HEARTBEAT.md 内容存对象存储 overrides/（per-instance，seed-only）

**⚠️ 这块的工作量之前被严重低估**——不只是"文件换个地方放"，而是 per-type vs per-instance 矛盾 + seed-only 语义 + AGENTS.md 三层合并要改成 system 字段注入 + 所有指令文件里的 mc/hiclaw-sync/hiclaw CLI 命令要改写。

---

## 五、整体场景流程

### 5.1 Team 创建

```
前端 POST /v1/teams → controller.createTeam()
  → 从 agi-chat 获取 config expert 资源
  → 解压 package：指令→共享卷 /{agentType}/，工作区→对象存储
  → createTeamCR(runtime=cimicode-bridge)
→ HiClaw operator
  → 为每个 member 创建 Matrix bot 账号 + 凭证
  → 为每个 member 创建 1 个 bridge pod（挂凭证 + 共享卷 + 协作上下文 env）
→ 每个 bridge pod 启动
  → 分阶段等待 Matrix/Redis/PG 就绪
  → 连 Matrix /sync
  → report-ready
  → Leader 启 heartbeat 定时器
→ controller 异步 monitor → 等 ready → 设 displayName → Team ACTIVE
```

### 5.2 Agent 被 @mention 响应

```
Human @agentA → agentA 的 bridge 收到
  → 硬过滤（@自己？允许列表？）→ 通过
  → 并发控制（正在跑？排队）→ 可处理
  → 沙箱管理：不存在→建+恢复 / Paused→resume / Running→直接用
  → 文件 sync：对象存储 shared/ + 私有文件 → 沙箱（增量）
  → 构建 context：PG 历史 + 两段式群聊视野 + 视角转换
  → 构建协作上下文：从 env 读 → 拼进 system 字段
  → 调 cimicode context_prompt（SSE）
    → cimicode：读共享卷 AGENTS.md + skills + cimicode.json → LLM loop（工具在沙箱执行）
    → SSE 增量事件流
  → bridge 消费 SSE → 以 agentA bot 身份发回 Matrix
  → 旁路落 PG
  → done → 清 history buffer + pause 沙箱 + 产物同步回对象存储
  → 检测响应里 @别的 agent → 按 peer-mentions 规则处理
```

### 5.3 Agent 间协作（Leader 中转）

```
agentA 完成 → @Leader(TASK_COMPLETED)
  → Leader bridge 检测 @Leader → 调 Leader cimicode
    → Leader 推理 → @agentB
  → agentB bridge 检测 @agentB → 调 agentB cimicode
    → sync shared/ 进 agentB 沙箱（含 agentA 的 result.md）
    → agentB 推理 + 在沙箱干活
    → done → 产物同步回对象存储
```

### 5.4 Leader heartbeat

```
Leader bridge 定时器（每 15m）
  → 从对象存储读 HEARTBEAT.md（overrides/）
  → 构造 cimicode 请求：system=HEARTBEAT.md 内容
  → 调 cimicode → SSE 回 Matrix
  → Leader 修改了 HEARTBEAT.md → done 后同步回 overrides/
```

### 5.5 Team 解散

```
controller.dissolveTeam → HiClawClient.deleteTeam → operator finalizer
  → 删除所有 bridge pod + 清理沙箱 + 清理 Matrix bot/room
  → 对象存储清理 + 本地 DB 更新
```

---

## 六、风险分析

### 6.1 技术风险

| 风险 | 等级 | 说明 | 缓解 |
|------|------|------|------|
| **bridge pod 资源不够轻** | 🔴 高 | bridge 含 Matrix/SSE/沙箱/sync/PG，实际资源可能不显著低于 openclaw | POC 实测 |
| **三个省资源因素都不成立** | 🔴 高 | bridge 不够轻 + cimicode 集中后资源也高 + agent 常忙沙箱 pause 不了 | POC 实测三因素 |
| **cimicode "不改代码"前提可能不成立** | 🔴 高 | 多 session/directory 并发、InMemorySessionStore 并发安全未验证 | 前置验证 3 |
| **Matrix client 从零写** | 🟡 中 | 不管哪个技术栈，Matrix /sync + @mention 都是新逻辑 | spike 验证 |
| **OpenSandbox 技术黑盒** | 🟡 中 | 实现未知，pause/resume 可靠性/TTL 文件丢失未验证 | 技术调研 |
| **HiClaw operator 每步调谐都要改** | 🟡 中 | 配置生成/env/文件部署/启动脚本/backend/ready/双 runtime 分流 | 充分测试 |
| **agi-context/controller 逻辑只能参考** | 🟡 中 | Java 栈，bridge 用别的栈需重写 | 接受重写成本 |
| **cimicode 并发瓶颈** | 🟡 中 | 2-3 pod 服务所有 agent，高并发可能不够 | 扩缩 + POC |
| **上下文视角转换** | 🟡 中 | 群聊多 speaker → user/assistant | 先单 agent 验证 |
| **沙箱冷启动** | 🟡 中 | 首次建沙箱 7-10s | 预热高频 agent |
| **cimicode SSE readTimeout=0** | 🟡 中 | 卡住的流永久阻塞 | bridge 加 SSE 超时 |

### 6.2 功能退化风险

| 风险 | 等级 | 说明 | 缓解 |
|------|------|------|------|
| dreaming 放弃 | 🟡 中 | 长对话不自动蒸馏，跨日经验积累退化 | 阶段2 bridge 离线实现 |
| memorySearch 放弃 | 🟡 中 | 不能语义检索长期记忆 | 远期加 |
| 群聊视野复刻不精确 | 🟡 中 | 两段式 history buffer 偏差影响协作 | 参考 CoPaw channel.py |
| seed-only 自我修改丢失 | 🟡 中 | SOUL.md/HEARTBEAT.md 共享卷只读 | overrides/ 方案，需确认是否必须 |
| 指令翻译后行为偏差 | 🟡 中 | 软指令替换硬机制，LLM 可能不严格遵守 | bridge 硬过滤兜底 |

### 6.3 资源风险

| 风险 | 等级 | 说明 | 缓解 |
|------|------|------|------|
| **资源没省反增** | 🔴 高 | N 沙箱没减 + bridge 新增 + OpenSandbox Server 新增 | POC 实测 |
| OpenSandbox Running 资源 | 🟡 中 | 沙箱 Running 时和 openclaw 本地 fs 相当 | pause 策略 |
| cimicode pod 扩缩成本 | 🟡 中 | 并发高时要扩 | 按需扩缩 |
| 共享卷并发写入 | 🟡 中 | 同类型 agent 并发写 | 锁/CAS |

### 6.4 失败模式与爆炸半径

| 失败 | 爆炸半径 | 恢复时间 | 状态丢失 |
|------|----------|----------|----------|
| bridge pod 挂 | 1 个 agent | 30-60s（K8s 重启+连 Matrix+重建 buffer+恢复沙箱） | history buffer + SSE |
| cimicode pod 挂 | 1 个 agent 响应（其他副本接管） | 快（无状态） | 无 |
| cimicode 集群全挂 | 所有 agent 无法推理 | K8s 重启 | 无 |
| OpenSandbox Server 挂 | 所有沙箱操作失败 | 恢复 Server | 无（沙箱可能还在运行） |
| 某 agent 沙箱挂 | 1 个 agent | 重建 7-10s | 未推回的变更（如有备份则可恢复） |
| Redis 挂 | 映射/since token 丢失 | 恢复 Redis | sessionID↔sandboxID 映射 |
| PG 挂 | 会话历史无法读写 | 恢复 PG | 无（持久化） |
| 对象存储挂 | 共享文件交换中断 | 恢复 | 无（持久化） |
| Synapse 挂 | 所有 agent 无法收发 | 恢复 Synapse | 无 |

**和 OpenClaw 基本同构的失败模式**——1 个 pod 挂只影响 1 agent，K8s 自动重启。没有集中式单点。**但 bridge 是全新组件，稳定性不如经过实战验证的 OpenClaw。**

### 6.5 补充坑点（经整体复查发现的遗漏）

#### 6.5.1 cimicode 多 session 并发内存风险

cimicode 的 InMemorySessionStore 是为单用户 chat 设计的（一个用户一个 session，30min TTL）。teams 场景下 N×T 个 agent 共享 2-3 个 cimicode pod，每个 agent 被 @时创建一个内存 session。如果多个 agent 同时被 @，一个 cimicode pod 同时维护大量内存 session——**可能 OOM**。

- 每个 session 含完整对话历史（messages + parts），context 大时内存占用高
- 30min TTL 清理，但高并发下 30min 内可能积累大量 session
- cimicode 的 InstanceState 是 per-directory 的（`src/effect/instance-state.ts`），N 个 agent 类型 = N 个 directory = N 个 InstanceState 同时存在于一个 pod——**内存/文件描述符压力未验证**
- **必须 POC 实测：一个 cimicode pod 能同时维护多少 session？多少 directory？内存占用多少？**

#### 6.5.2 cimicode SSE 长连接阻塞 bridge 线程

cimicode context_prompt 是 SSE 长连接，一个请求可能持续几十秒到几分钟（LLM 推理 + 工具调用循环）。bridge 消费 SSE 时阻塞一个线程/协程。

- 如果 cimicode 卡住（LLM 不响应/工具调用 hang），SSE readTimeout=0（永不超时），bridge 消费线程永久阻塞
- 每 agent 1 个 bridge pod，如果 bridge 是单线程消费 SSE，cimicode 卡住 = 该 agent 永久阻塞（直到 K8s liveness probe 重启 pod）
- **必须：** bridge 加 SSE 消费超时 + cimicode 加 stream-level 超时 + bridge 有并发控制

#### 6.5.3 bridge pod 同时只处理 1 个 @mention 的并发问题

每 agent 1 个 bridge pod——如果该 agent 被 @了，cimicode 正在跑（SSE 还没 done），此时又有人 @该 agent，bridge 怎么处理？

- 排队：等上一轮 done 再处理（但上一轮可能跑几分钟，用户等不了）
- 拒绝：返回"忙"消息（用户体验差）
- 并发：同时跑两轮（但 cimicode 的 session 是同一个 sessionID，两个请求同时 importContext + prompt 可能冲突）
- OpenClaw 有 `maxConcurrent: 4`（`worker-openclaw.json.tmpl:91`），bridge 也要设计并发控制策略
- **方案：排队 + 取最新（§4.1.3）**

#### 6.5.4 sessionID 稳定性与生成规则

sessionID 需要含 team/room/agent/date 维度，保证同一 agent 在同一 room 同一天 sessionID 稳定（不能每次 @都生成新的，否则历史断了）。

- sessionID 由谁生成？bridge pod？如果是 bridge pod 生成，生成规则要跨 bridge pod 一致（同一个 agent 的 bridge pod 重启后生成同样的 sessionID）
- 日期维度：04:00 切换新 sessionID——但 bridge pod 怎么知道 04:00 该换了？定时器？还是每次 @时检查日期？
- **生成规则的正确性是个坑**——错了会导致历史断裂或串会话

#### 6.5.5 OpenSandbox 技术实现未知

- OpenSandbox 是什么实现？容器？microVM？WASM？——不同实现资源占用和 pause/resume 特性差异巨大
- pause 后文件在不在？resume 后状态对不对？TTL 回收后文件真的丢吗？
- pause/resume 的可靠性？会不会 resume 失败？
- OpenSandbox Server 本身的 HA/可用性？挂了所有沙箱操作都失败
- **OpenSandbox 的技术实现和可靠性是方案的关键依赖，但目前是黑盒，必须调研清楚**

#### 6.5.6 Matrix 消息延迟导致协作延迟

agent 间协作靠 Matrix 消息传递：agentA 的 bridge pod 把响应发到 Matrix room → Leader 的 bridge pod 通过 /sync 收到 → 检测 @Leader → 调 cimicode。

- OpenClaw 是同一个进程里 Matrix plugin 直接收到消息，延迟极小
- bridge 模型下协作要经过 Matrix Server 中转——/sync 的 long-polling 有延迟（默认 30s 超时，但消息到达后才返回，实际延迟可能 1-5s）
- 多跳协作（A → Leader → B）的延迟叠加
- **协作延迟可能影响 agent teams 的响应速度**

#### 6.5.7 cimicode pod 挂后的 SSE 重试语义不明确

cimicode pod 挂了，bridge 的 SSE 连接断开。bridge 怎么处理？

- 重试？重新调一次 cimicode context_prompt
- 但 cimicode 是无状态的，InMemorySessionStore 30min TTL——重试时 K8s service 负载均衡可能命中不同 pod，session 不在
- **重试会重新 importContext + prompt，等于从头跑一遍，之前已生成的部分响应丢失**
- bridge 需要设计重试策略（重试 vs 告知用户失败）

#### 6.5.8 共享卷并发写入风险

team 创建时 controller 解压 package 到共享卷 `/{agentType}/`。如果两个 team 同时创建且用了同一个 agent 类型：

- 两个 controller 实例同时写同一个 `/{agentType}/` 路径——并发写入冲突
- 共享卷的写入需要锁或 CAS 机制
- **RWX 卷的并发写入一致性需要验证**

#### 6.5.9 bridge pod 启动依赖顺序

bridge pod 启动时要：连 Matrix → 建/恢复沙箱 → 生成 sessionID → Redis 存映射 → report-ready。

- 如果 Matrix 还没准备好？如果 Redis/PG 还没准备好？
- 启动顺序和重试逻辑是个坑——OpenClaw 的 worker-entrypoint.sh 有详细的等待重试逻辑，bridge 也要有
- **启动依赖处理不当会导致 bridge pod 反复 CrashLoopBackOff**

#### 6.5.10 displayName 设置时机与 controller 联动

OpenClaw 连 Matrix 时自动设 displayName，controller 的 `waitForTeamMembersAndSetDisplayNames`（`TeamService.java:911`）等 ready 后设最终 displayName。

- bridge 模型下 ready 语义变了（从"openclaw 连 Matrix"变成"bridge 连 Matrix + 沙箱/会话已建"）
- controller 的 waitForTeamMembersAndSetDisplayNames 逻辑要改——它怎么知道 bridge ready 了？
- bridge 自己设 displayName 还是 controller 设？什么时机？
- **displayName 设置的时机和 controller 联动是个容易被忽略的细节坑**

#### 6.5.11 agent 响应文本里 @mention 解析

agent 间协作时，agentA 的 cimicode 响应文本里如果 @了 Leader（`@leader:domain`），bridge 要从 cimicode 的 SSE 响应文本里解析 @mention。

- Matrix 消息有结构化的 mention 格式（`m.mentions`），但 LLM 生成的文本只有 `@botname:domain` 字符串
- bridge 要从纯文本里正则匹配 @mention——LLM 生成的格式可能不标准（多了空格、少了 :domain、用了别名等）
- **@mention 解析的鲁棒性是个坑**

#### 6.5.12 cimicode "不改代码"前提下的潜在问题

- cimicode serve 模式是否支持多 session 并发？——需验证
- cimicode 的 InstanceState per-directory——N 个 directory 同时在一个 pod 上是否正确工作？——需验证
- cimicode 的 context_prompt SSE——多个并发请求时 InMemorySessionStore 的并发安全？——需验证
- cimicode 的 directory 级配置加载——多个 directory 的配置是否互相隔离？会不会串？——需验证
- **"不改代码"是方案的核心前提，但如果以上任何一项验证不通过，就要改 cimicode 代码——前提可能不成立**

---

## 七、可信性分析

### 7.1 现有资源条件评估

| 条件 | 评估 | 说明 |
|------|------|------|
| cimicode 无状态方案 | ✅ 就绪（待验证多并发） | 12 项核心能力已实现，多 session/directory 并发待验证 |
| agi-context cimicode 编排 | ⚠️ 逻辑可参考 | Java 栈，bridge 需重写 |
| controller Matrix 集成 | ⚠️ 逻辑可参考 | Java 栈，bridge 需重写 |
| HiClaw operator | ⚠️ 框架就绪但每步调谐都要改 | 详见 §4.6 |
| OpenSandbox | ⚠️ 待部署+待调研 | 技术实现黑盒 |
| 对象存储 / Synapse | ✅ 就绪 | 现有可复用 |
| 共享配置卷 | ⚠️ 待部署 | RWX PVC 或 JuiceFS/s3fs |

### 7.2 功能支持情况

| 功能 | OpenClaw | 替换后 | 状态 |
|------|----------|--------|------|
| 群聊 @mention 协作 | ✅ | ✅ bridge 硬过滤 | 需 bridge 实现 |
| Leader 协调 + heartbeat | ✅ | ✅ bridge 定时器 | 需 bridge 实现 |
| agent 间文件协作 | ✅ | ✅ OpenSandbox + bridge sync | 需 bridge sync |
| LLM 推理 | ✅ 每 pod 独立 | ✅ 共享 cimicode | 已就绪（待验证并发） |
| MCP / skills / 指令 | ✅ | ✅ cimicode 原生 + 共享卷 | 需指令翻译 |
| 会话上下文 + 群聊视野 | ✅ | ✅ PG + bridge buffer | 需 bridge 实现 |
| 单次 compaction | ✅ | ✅ cimicode 自带 | 已就绪 |
| dreaming / memorySearch | ✅ | ❌ 放弃 | 退化 |
| agent 手写 memory | ✅ | ✅ 沙箱 + 对象存储 | 支持 |
| slash commands | ✅ | ❌ 不迁移 | 非核心 |
| seed-only 自我修改 | ✅ | ⚠️ overrides/ 方案 | 需确认是否必须 |

### 7.3 资源消耗客观分析

**省资源依赖三个因素，每个都有不确定性：**

**因素一：bridge pod 比 openclaw pod 轻**

| 组件 | openclaw pod | bridge pod（Go 保守） | bridge pod（Python） |
|------|-------------|---------------------|---------------------|
| 运行时 | Node.js 22 + npm | Go 单 binary | Python |
| LLM gateway | 常驻 | 无 | 无 |
| 本地 fs | 工作区 | 无（在沙箱） | 无 |
| 估计内存 | ~200-500MB+ | ~40-80MB | ~100-180MB |

**不确定性：** bridge 不只是 Matrix client，还有 SSE/沙箱/sync/PG。实际资源必须 POC 实测。

**因素二：LLM 推理集中省**

- OpenClaw：N 个 Node.js gateway 常驻
- 替换后：2-3 个 cimicode 共享（按需忙）
- **不确定性：** cimicode pod 并发时 CPU/内存可能很高。InMemorySessionStore 多 session 内存未验证。

**因素三：沙箱可 pause 省**

- OpenClaw：N 个本地 fs 常驻
- 替换后：N 个沙箱（可 pause/回收）
- **不确定性：** 沙箱数量没减（N 个）。只有 agent 空闲 pause 才省。沙箱 Running 资源可能和 openclaw 本地 fs 相当。OpenSandbox 技术黑盒。

**净分析：**

| 维度 | OpenClaw（N pod） | 替换后 | 谁省 |
|------|------------------|--------|------|
| agent pod | N 个 openclaw（重） | N 个 bridge（轻？） | 取决于 bridge 多轻 |
| LLM gateway | N 个常驻 | 2-3 个共享 | cimicode 省（如果集中成立） |
| 执行环境 | N 个本地 fs 常驻 | N 个沙箱（可 pause？） | 取决于 agent 空闲比例 |
| 额外基础设施 | 0 | OpenSandbox Server | OpenClaw 省 |

**结论：三个因素都有不确定性，资源是否省完全取决于 POC 实测。不能盲目乐观。**

### 7.4 不同规模分析

| 规模 | 资源分析 | 结论 |
|------|----------|------|
| 小规模（几个 team） | OpenSandbox Server 基础设施成本不划算 | 不确定，收益有限 |
| 中等规模（十几个 team） | 三因素可能都成立，但需 POC | 有条件可行 |
| 大规模（几十~上百 team，agent 空闲多） | 最能省——轻 bridge + LLM 集中平摊 + 空闲沙箱 pause | 理论上最值得 |
| agent 常忙 | 沙箱没省，省 bridge 更轻 + LLM 集中 | 部分可行 |

### 7.5 综合结论

**方案在架构方向上合理（保持分布式不集中化），比集中 bridge 方案显著更优。** 但**"比有问题的方案好"不代表"本身没问题"**。可行性面临三个根本性挑战：

#### 挑战一：资源目标大概率不成立或收益有限

每个 agent 还是要配一个沙箱（N 个沙箱 = N 个 openclaw pod 数量），沙箱 Running 时资源和 openclaw 本地 fs 相当。省的只有"bridge 更轻" + "LLM 集中" + "空闲沙箱 pause"，但新增 OpenSandbox Server。**三个因素中任何一个不成立，净省就有限。必须 POC 实测。**

#### 挑战二：Bridge pod 是全新组件，大量逻辑从零写

Matrix client + @mention + history buffer + SSE 转发 + 沙箱管理 + 文件 sync + heartbeat + 协作上下文注入 + 并发控制 + 启动依赖——全是新逻辑。且 Java 栈逻辑需重写。**用未经验证的新代码替换经过实战验证的 OpenClaw 成熟代码，本身就是风险。**

#### 挑战三：原有功能保持有大量隐藏工作量

1. **指令体系不兼容**——SOUL.md/HEARTBEAT.md/openclaw.json/mcporter 在 cimicode 不被识别，AGENTS.md 含 OpenClaw 专属指令要重写
2. **初始化文件生成**——operator 生成 SOUL.md/AGENTS.md/HEARTBEAT.md/skills/mcporter，含三层合并 + per-instance 协作上下文注入
3. **per-type vs per-instance 矛盾**——共享卷 per-type，协作上下文 per-instance，通过 system 字段注入解决但增加复杂度
4. **seed-only 语义**——共享卷只读，agent 自我修改需 overrides/ 方案
5. **软指令替换硬机制**——bridge 硬过滤兜底，但 LLM 输出端仍有风险
6. **HiClaw operator 每步调谐都要改**——配置生成/env/文件部署/启动脚本/backend/ready/双 runtime
7. **cimicode "不改代码"前提可能不成立**——多 session/directory 并发未验证
8. **draming/memorySearch 退化**——agent 跨日经验积累退化

### 7.6 前置验证（必须先做，决定方案是否值得推进）

| # | 验证项 | 验证什么 | 不通过的后果 |
|---|--------|----------|-------------|
| 1 | POC 实测资源 | bridge pod 资源 + cimicode 并发资源 + 沙箱 pause 资源差 + 总资源对比 | 资源没省 → **核心目标失败** |
| 2 | Bridge spike | Matrix client + @mention + cimicode SSE 端到端 + 长连接 24h + E2EE | Matrix 不可行 → **bridge 不成立** |
| 3 | cimicode 多 session/directory | 多 session + 多 directory 并发 + 内存 + 并发安全 | "不改代码"不成立 → **要改 cimicode** |
| 4 | OpenSandbox 技术调研 | 实现方式 + 资源模型 + pause/resume 可靠性 + TTL 文件 | 沙箱不可靠 → **文件持久化不成立** |
| 5 | 功能退化可接受 | dreaming/RAG 放弃是否可接受 | 不可接受 → **需实现 dreaming** |
| 6 | AGENTS.md 改写评估 | 指令文件里 OpenClaw 专属命令有多少？改写工作量？ | 工作量过大 → **成本被低估** |
| 7 | Leader hiclaw CLI 替代 | Leader 查 Worker 状态/读产物替代方案可行？ | 无替代 → **Leader 协调退化** |
| 8 | 协作上下文注入 | per-type vs per-instance 矛盾解决方案可行？bridge 怎么获取？ | 无方案 → **协作断裂** |
| 9 | seed-only 语义 | SOUL.md/HEARTBEAT.md 自我修改在共享卷只读下怎么保持？必须？ | 无法保持且必须 → **功能退化** |
| 10 | 指令体系兼容性 | OpenClaw 指令能否翻译成 cimicode 指令？行为能否不变？软指令替换硬机制风险？ | 偏差大 → **协作退化或断裂** |

**总体判断：** 方案架构方向合理，比集中 bridge 方案显著更优，但实现层面有大量未验证假设和隐藏坑点，资源是否真省取决于三个实测变量，原有功能保持有大量容易被低估的工作量。**建议先做 10 项前置验证，全部通过后再决定是否推进。如果资源没省或 bridge spike 不通过，应重新评估其他路径（OpenClaw pod 自身优化 / agent 按需启停等）。**

---

## 八、建议下一步

### 8.1 POC 实测资源（最高优先）

**目标：** 确认三个省资源因素是否成立。

**测量：** bridge pod 资源（Go/Bun/Python 完整 bridge）+ cimicode pod 并发资源（5/10/20 并发 SSE + 多 session/directory）+ OpenSandbox Running/Paused/Terminated 资源差 + 总资源对比 openclaw。

**成功标准：** 净省 ≥ 30%。

### 8.2 Bridge Spike

选定技术栈 + Matrix client + @mention + cimicode SSE 端到端 + 长连接 24h + 重连 + E2EE。

### 8.3 cimicode 多 session/directory 验证

验证"不改代码"前提。多 session + 多 directory 并发 + 内存 + 并发安全。

### 8.4 OpenSandbox 技术调研

实现方式 + 资源模型 + pause/resume 可靠性 + TTL 文件。

### 8.5 确认功能退化可接受

dreaming/RAG 放弃是否可接受。

### 8.6 AGENTS.md 改写评估

盘清所有 OpenClaw 专属命令，评估改写工作量。

### 8.7 Leader hiclaw CLI 替代方案设计

Leader 查 Worker 状态/读产物替代方案。

### 8.8 协作上下文注入方案验证

per-type vs per-instance 矛盾解决方案 + bridge 获取协作上下文方式。

### 8.9 seed-only 语义验证

SOUL.md/HEARTBEAT.md 自我修改在共享卷只读下怎么保持 + 是否必须。

### 8.10 指令体系兼容性验证

OpenClaw 指令翻译成 cimicode 指令 + 行为一致性 + 软指令替换硬机制风险。

### 8.11 如果 10 项验证全部通过

HiClaw operator 加 cimicode-bridge runtime + bridge pod 实现 + 铺基础设施 + 渐进灰度。

### 8.12 如果资源没省或 bridge spike 不通过

重新评估：OpenClaw pod 自身优化 / agent 按需启停（HiClaw 已有 sleep/wake）/ agent 共享 pod / 其他方案。

---

## 附录 A：方案演进

### A.1 集中 bridge 方案的问题

初版采用"每 team 1 个集中 bridge pod"，经分析发现根本问题：单点（挂了全 team 瘫痪）、规模瓶颈（N 连接集中 1 进程）、爆炸半径（全 team）、HiClaw 大改（调谐结构性变更）、资源没省（N 沙箱没减 + bridge 新增）。

### A.2 修正：每 agent 一个轻量 bridge pod

拆成"每 agent 1 个轻量 bridge pod"：消灭单点/瓶颈/爆炸半径、HiClaw 同构（每 member 1 pod）、复刻 OpenClaw 成熟模式、cimicode 保持无状态共享。

### A.3 也考虑过的路径：cimicode 升级成有状态 agent runtime

| 维度 | 每 agent 轻量 bridge（本方案） | cimicode 有状态 runtime |
|------|-------------------------------|------------------------|
| cimicode 改动 | 尽量不改 | 大改（加 Matrix + 长驻 + 有状态） |
| LLM 推理 | 集中（2-3 共享） | N 个常驻（没省 LLM gateway） |
| 网络跳 | bridge→cimicode 多一跳 | 无跳（同进程） |

**选择轻量 bridge 的理由：** cimicode 尽量不改 + LLM 推理集中省。代价是 bridge→cimicode 多一跳 + bridge 是新组件。
