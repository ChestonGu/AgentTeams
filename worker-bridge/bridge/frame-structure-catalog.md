# SSE 帧结构说明清单（Gateway 消费侧）

> 状态：v1（2026-09-25）
> 定位：Gateway 对接文档。逐帧给出 `POST /v1/turn` SSE 流上每一种帧的 envelope 属性、`data` 字段结构、发出时机、落库与 fold（历史还原）处理。与 [`history-restore-from-durable-frames.md`](./history-restore-from-durable-frames.md)（还原流程）配套使用。
> 权威依据：契约 06 §6.1/§6.2/§7/§7.1；字段定义以 `packages/schema/src/session-event.ts`、`packages/schema/src/llm.ts`、`packages/schema/src/prompt.ts` 与 `packages/runtime-contract/src/turn/frame.ts` 为准。本文不引入新语义；冲突时以契约与 schema 为准。
> 适用：`context.format = "opencode.session-message@1"`（V2）。v0.1 兼容集合（§7 的 `message.*` / `tool.*` / `context.compacted` / `input.admitted`）只在 v0.1 format 发出，本表不展开。

---

## 1. 帧公共结构（envelope）

SSE wire 形态（契约 06 §6.1）：

```text
id: <seq>                 ← 仅 Runtime 帧；invocation 内从 0 连续；turn.accepted/superseded 无 id
event: <type>             ← 帧类型
data: <一行 JSON>          ← 下表 envelope；JSON 不含裸换行
retry: 1000               ← 流开头一次
: hb                      ← heartbeat 注释行（默认 5000ms）
```

envelope 字段（契约 06 §6.2）：

| 字段 | 取值 | live | durable | 说明 |
| --- | --- | --- | --- | --- |
| `kind` | `"live" \| "durable"` | ✓ | ✓ | `durable` 即 DB 行，Gateway **必须原样落库**（不挑选、不转换）；`live` 永不落库 |
| `sid` | string | ✓ | ✓ | Session ID |
| `inv` | string | ✓ | ✓ | `invocation.id` |
| `epoch` | 十进制整数 | ✓ | ✓ | 请求里的 `invocation.epoch`；实时消费时与本 attempt 不符即丢弃 |
| `seq` | 十进制整数 | 可选 | ✓（MUST） | invocation 内从 0 连续；`turn.accepted`/`superseded` 无 seq |
| `type` | string | ✓ | ✓ | 事件类型（§2 目录） |
| `data` | JSON | ✓ | ✓ | 事件负载（本文 §3/§4 逐帧说明） |
| `rev` | 十进制整数 | — | ✓（MUST） | durable revision，从 `session.revision + 1` 起连续 |
| `eventID` | string | — | ✓（MUST） | `evt_<ULID>`，幂等键 |
| `ts` | 十进制整数 | 可选 | 可选 | Runtime 写入时刻（Unix ms），仅诊断 |
| `ext` | JSON | 可选 | 可选 | 扩展袋（如 `opencode.turnID`、`opencode.cause`） |

硬规则（契约 06 §6.2 / §7）：

1. 一个 invocation 的 Runtime 帧**必须**以 `invocation.started`（seq 0，durable）开始，以且仅以一个终态帧（`invocation.idle` / `yielded` / `failed`，durable）结束；终态之后不再有任何帧。
2. durable `rev` 在 seq 顺序上单调；同 `eventID` 重试重复写出时 `kind/type/data/rev` **必须**逐字节相同。
3. **终态帧的 envelope `rev` 与 `data.revision` 必须相等**，即本 invocation 最后一个 durable `rev`；下一 turn 的 `session.revision` 即该值。
4. durable 帧**不得**被截断（帧字节上限由 Config-A `channel.maxFrameBytes` 承担）。
5. 消费者对未知 `type`：live 忽略，durable 原样落库（未来按 `@N` 扩展）。

## 2. 类型总目录

| `type` | kind | 落库 | 进 fold | 何时 |
| --- | --- | --- | --- | --- |
| `turn.accepted` | live | — | — | 校验通过后的首事件（无 seq） |
| `superseded` | live | — | — | 新订阅者到达时写给旧连接（无 seq） |
| `invocation.started` | durable | ✓ | — | 进入执行（seq 0；本 invocation DB 行的"表头"） |
| `status.changed` | live | — | — | phase 变化 |
| `diagnostic` | live | — | — | 非致命问题 |
| `session.updated` | durable | ✓ | — | 标题等 Session 元数据变化（只含变化的键） |
| `invocation.idle` | durable | ✓ | — | 正常终态 |
| `invocation.yielded` | durable | ✓ | — | handoff 让渡终态 |
| `invocation.failed` | durable | ✓ | — | 取消/超时/不可恢复错误终态 |
| `session.next.prompt.admitted@1` | durable | ✓ | — | `ctl append` 录取（仅追加输入；初始输入不发） |
| `session.next.prompted@1` | durable | ✓ | ✓ | 用户输入 promote 为用户消息（初始/steer/queue 三种时机） |
| `session.next.step.started@1` | durable | ✓ | ✓ | 每次 Provider 调用开始；`ext["opencode.turnID"]` 携 turnID |
| `session.next.step.ended@2` | durable | ✓ | ✓ | Provider 调用正常结束 |
| `session.next.step.failed@2` | durable | ✓ | ✓ | Provider 调用失败 |
| `session.next.text.started@1` | durable | ✓ | ✓ | 文本 part 开始 |
| `session.next.text.delta` | live | — | — | 文本增量 |
| `session.next.text.ended@1` | durable | ✓ | ✓ | 文本 part 收口（携全文） |
| `session.next.reasoning.started@1` | durable | ✓ | ✓ | 推理 part 开始 |
| `session.next.reasoning.delta` | live | — | — | 推理增量 |
| `session.next.reasoning.ended@1` | durable | ✓ | ✓ | 推理 part 收口（携全文） |
| `session.next.tool.input.started@1` | durable | ✓ | ✓ | 工具输入流开始 |
| `session.next.tool.input.delta` | live | — | — | 工具输入增量 |
| `session.next.tool.input.ended@1` | durable | ✓ | ✓ | 工具输入收口（携原始串） |
| `session.next.tool.called@1` | durable | ✓ | ✓ | 输入完整、即将调用沙箱之前；`ext["opencode.turnID"]` 携 turnID |
| `session.next.tool.progress@1` | durable | ✓ | ✓ | 工具运行中有界进度更新 |
| `session.next.tool.success@1` | durable | ✓ | ✓ | 工具结算：成功 |
| `session.next.tool.failed@1` | durable | ✓ | ✓ | 工具结算：失败/取消/超时 |
| `session.next.compaction.started@1` | durable | ✓ | — | 上下文压缩开始 |
| `session.next.compaction.delta` | live | — | — | 压缩摘要增量 |
| `session.next.compaction.ended@1` | durable | ✓ | ✓ | 压缩收口（携 summary + recent） |

> `epoch.fenced` 为 v0.2 Redis 遗留，**v0.3 起不再发出**；历史消费者读到则忽略。

## 3. turn 域帧详解（两种 format 通用）

### 3.1 `turn.accepted`（live，无 seq）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `invocationID` | string | MUST | 本 invocation ID |
| `epoch` | number | MUST | 本 attempt epoch |
| `runtime.id` / `runtime.version` | string | MUST | Runtime 身份 |
| `runtime.bid` | string | MUST | 进程 bootID；Gateway 写入 `runtime_boot_id` 并与 `X-OC-Runtime-Boot` 核对 |
| `links.events` / `links.ctl` / `links.status` | string | MUST | 三个控制端点 URL |
| `acceptedAtMs` | number | MAY | 接受时刻 |

### 3.2 `superseded`（live，无 seq）

`data` 为空对象。新订阅者到达时写给旧连接后关闭旧连接。

### 3.3 `invocation.started`（durable，seq 0）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `inputID` | string | MUST | 本次初始输入的消息 ID（`msg_` 前缀） |
| `model.providerID` / `model.modelID` | string | MUST | 激活模型 |
| `model.variant` | string | MAY | 模型变体 |
| `sandbox.kind` | `"remote" \| "none"` | MUST | remote 时含 `workspaceID`、`workspaceRoot` |
| `agent` | string | MUST | 选定 agent 名 |
| `tools` | string[] | MUST | 有效工具集（白/黑名单与 deny 生效后） |
| `limits` | object | MUST | 生效上限：`maxProviderTurns` / `maxToolCalls` / `maxOutputBytes` / `maxToolOutputBytes`（Config-A 与请求 `limits` 逐字段取 min 后） |
| `runtime.id` / `runtime.version` | string | MUST | |
| `mcpTools` | `{name, server, tool}[]` | MAY（v0.2） | §14 派生名 → 来源映射；无 MCP 为空数组 |
| `promptSections` | string[] | MAY（v0.2） | §4.4 系统提示词段落名 |
| `schemaFingerprint` | string | MAY（v0.2） | 已登记 `session.next.*@N` 定义指纹的 canonical sha256（RISS-084） |
| `configRef` | object | MAY | 请求 `config.ext["opencode.configRef"]` 透传（审计引用） |

Gateway 落库后按 `inv` 把本行关联为同 invocation 全部行的"表头"（workspace 身份、选模、工具集、Config 审计引用都在这里且仅在这里）。

### 3.4 `status.changed`（live）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `phase` | `starting \| model \| tool \| finishing \| idle \| yielded \| failed` | MUST | 与 `TurnStatus.phase` 同步 |
| `detail` | string | MAY | |

### 3.5 `diagnostic`（live）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `level` | `info \| warn \| error` | MUST | |
| `code` | string | MUST | 如 `UnknownTool`、`DuplicateInput`、`Unsupported`、`Truncated` |
| `message` | string | MUST | |
| `details` | object | MAY | |

### 3.6 `session.updated`（durable）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `title` | string | MAY | 只含变化的键；新增键即扩展 |

进 DB 但**不进 fold**（V2 无 `session.next.updated`；标题留在 turn/Session 元数据域）。

### 3.7 `invocation.idle`（durable，终态）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `revision` | number | MUST | **必须等于 envelope `rev`**；下一 turn 的 `session.revision` |
| `stopReason` | `"complete" \| "limit"` | MUST | `limit` = 触顶 `maxProviderTurns`/`maxToolCalls` |
| `turns` | number | MUST | 实际 Provider 轮数 |
| `toolCalls` | number | MUST | 实际工具调用数 |

### 3.8 `invocation.yielded`（durable，终态）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `revision` | number | MUST | 同上 |
| `reason` | `"handoff"` | MUST | |

### 3.9 `invocation.failed`（durable，终态）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `revision` | number | MUST | 同上 |
| `error.code` | string | MUST | `Invalid \| Conflict \| DivergentRetry \| Unsupported \| Unavailable \| Cancelled \| Timeout \| OutputLimit \| PolicyDenied \| Indeterminate \| Internal` |
| `error.message` | string | MUST | |
| `error.retryable` | boolean | MUST | |
| `error.details` | object | MAY | |

Runtime 自发取消（非 ctl）时 `error.code = "Cancelled"`，且帧 `ext["opencode.cause"]` 为 `"gateway-lost"` 或 `"shutdown"`。

## 4. V2 Session 域帧详解（`opencode.session-message@1`）

**公共 `data` 字段**（下表不再重复）：

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `timestamp` | number | MUST | epoch millis（V2 `DateTime` 编码） |
| `sessionID` | string | MUST | **必须等于 envelope `sid`**，否则帧非法（`InvalidFrame`） |

「fold」列指 `foldSessionMessages` 的处理；详见还原文档 §6。所有 live delta 帧 fold 一律忽略。

### 4.1 `session.next.prompt.admitted@1`（durable）

`ctl append` 被录取的当下写出（**仅追加输入**；初始输入不发此帧，直接 `prompted`）。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `messageID` | string | MUST | `msg_` 前缀；= ctl `input.id` |
| `prompt.text` | string | MUST | 正文 |
| `prompt.files` | `FileAttachment[]` | MAY | 见 §5.2 |
| `prompt.agents` | `AgentAttachment[]` | MAY | turn/1 恒为空 |
| `delivery` | `"steer" \| "queue"` | MUST | 词表只有这两个值（schema 限制），无 `initial` |

- fold：**忽略**。
- Gateway：落库；这是「append 已被接受」的确认（契约 06 §6.4 规则 2）。未得到此帧的 append 在终态后经 `session_pending_input` 重提。

### 4.2 `session.next.prompted@1`（durable）

用户输入 promote 为用户消息时写出：初始输入在 `invocation.started` 后；steer 在 provider 流结束的安全边界；queue 在即将 idle 时。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `messageID` | string | MUST | 用户消息 ID |
| `prompt.text` / `prompt.files` / `prompt.agents` | | | 同 4.1 |
| `delivery` | `"steer" \| "queue"` | MUST | **初始输入也为 `"steer"`**（词表限制）；不得用 `delivery` 区分初始/steer，fold 也不使用它 |

- fold：追加 `User` 消息（`id=messageID`、`text/files/agents`、`time.created=timestamp`）。

### 4.3 `session.next.step.started@1`（durable）

每次 Provider 调用开始。envelope `ext["opencode.turnID"]` 携本轮 turnID。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` | string | MUST | `msg_` 前缀 |
| `agent` | string | MUST | 本轮 agent |
| `model.providerID` / `model.id` | string | MUST | 本轮模型（`Model.Ref`；另有可选 `variant`） |
| `snapshot` | string | MAY | 快照起点（agent 快照机制） |

- fold：把上一个未闭合的 assistant 补 `time.completed`；追加 `Assistant` 外壳（`content=[]`、`time.created`、`snapshot.start`）。

### 4.4 `session.next.step.ended@2`（durable）

Provider 调用正常结束。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` | string | MUST | |
| `finish` | string | MUST | provider 收尾原因（如 `"stop"`） |
| `cost` | number | MUST | 本轮成本 |
| `tokens.input` / `output` / `reasoning` | number | MUST | token 计数 |
| `tokens.cache.read` / `cache.write` | number | MUST | 缓存 token |
| `snapshot` | string | MAY | 快照终点 |
| `files` | string[] | MAY | 相对路径列表 |

- fold：给对应 assistant 补 `time.completed`、`finish`、`cost`、`tokens`、`snapshot.end/files`。
- **审计注意**：`@2` 是本类型的现行版本（v1 已被 v2 定义取代），消费者必须按 `@N` 选对应 schema 校验。

### 4.5 `session.next.step.failed@2`（durable）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` | string | MUST | |
| `error.type` | `"unknown"` | MUST | `SessionMessage.UnknownError` |
| `error.message` | string | MUST | |

- fold：补 `time.completed`、`finish:"error"`、`error`。

### 4.6 `session.next.text.started@1` / `text.delta`（live） / `text.ended@1`

| 帧 | kind | 字段 |
| --- | --- | --- |
| `text.started@1` | durable | `assistantMessageID`、`textID`（part 内唯一 ID） |
| `text.delta` | live | `assistantMessageID`、`textID`、`delta`（增量串） |
| `text.ended@1` | durable | `assistantMessageID`、`textID`、`text`（**全文**，delta 序列的收口，RISS-059） |

- fold：`started` 追加空 `AssistantText{id:textID, text:""}`；`ended` 把全文**整体替换**进该 part。`content` 内 part 顺序 = `*.started` 到达顺序。

### 4.7 `session.next.reasoning.started@1` / `reasoning.delta`（live） / `reasoning.ended@1`

| 帧 | kind | 字段 |
| --- | --- | --- |
| `reasoning.started@1` | durable | `assistantMessageID`、`reasoningID`、`providerMetadata?`（§5.4） |
| `reasoning.delta` | live | `assistantMessageID`、`reasoningID`、`delta` |
| `reasoning.ended@1` | durable | `assistantMessageID`、`reasoningID`、`text`（全文）、`providerMetadata?` |

- fold：`started` 追加 `AssistantReasoning`（含 `providerMetadata`、`time.created`）；`ended` 替换全文、补 `time.completed` 与 `providerMetadata`。
- **语义**：`providerMetadata` 在下一 turn 同模型时被原样透传给 provider（reasoning 复用），是语义必需字段。

### 4.8 `session.next.tool.input.started@1` / `tool.input.delta`（live） / `tool.input.ended@1`

| 帧 | kind | 字段 |
| --- | --- | --- |
| `tool.input.started@1` | durable | `assistantMessageID`、`callID`、`name`（模型侧工具名） |
| `tool.input.delta` | live | `assistantMessageID`、`callID`、`delta` |
| `tool.input.ended@1` | durable | `assistantMessageID`、`callID`、`text`（**原始输入串**，不保证合法 JSON） |

- fold：`started` 追加 `AssistantTool{id:callID, name, state:{status:"pending", input:""}}`；`ended` 把 `state.input` 置为原始串。
- 崩溃在 `ended` 之前：历史剩一个带工具名的 pending tool part——这是 `started` 帧 durable 化的主要价值。

### 4.9 `session.next.tool.called@1`（durable）

输入完整、**即将调用沙箱/工具之前**写出（v0 保留的唯一"副作用前先记录"门闩；不等 DB ack）。envelope `ext["opencode.turnID"]` 携 turnID。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` | string | MUST | |
| `callID` | string | MUST | 与 input 三帧同一 `callID` |
| `tool` | string | MUST | 工具名 |
| `input` | object | MUST | 解析后的工具输入（JSON 对象） |
| `provider.executed` | boolean | MUST | 是否由 provider 侧执行（MCP 工具为 false） |
| `provider.metadata` | `ProviderMetadata` | MAY | |

- fold：`state` 升为 `running`（`input` 换成解析后对象、`structured={}`、`content=[]`），补 `time.ran`、`provider`。
- **版本注意**：`@1` 是 v1 定义（`tool` 为字符串、`input` 为对象）。schema 中同名 `@2`（`session.next.tool.called.2`，`PortableToolProfileRef` 形状）是 execute/1 遗留，turn/1 **不发出**；校验 `@1` 时不得使用 v2 定义。

### 4.10 `session.next.tool.progress@1`（durable）

工具运行中有界进度更新（语义转换或有界节奏，不是每个输出块）。

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` / `callID` | string | MUST | |
| `structured` | object | MUST | 结构化进度 |
| `content` | `ToolContent[]` | MUST | 见 §5.5 |

- fold：覆盖 running 态的 `structured`/`content`。

### 4.11 `session.next.tool.success@1`（durable）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` / `callID` | string | MUST | |
| `structured` | object | MUST | 结构化输出 |
| `content` | `ToolContent[]` | MUST | |
| `outputPaths` | string[] | MAY | |
| `result` | any | MAY | provider-executed 工具的原始结果 |
| `provider.executed` | boolean | MUST | |
| `provider.metadata` | `ProviderMetadata` | MAY | 结算侧 metadata（fold 映为 `resultMetadata`） |

- fold：`state` → `completed`（`input` 沿用 running 态对象）。
- 约束：每个 `tool.called@1` 终态前**必须**恰有一个同 `callID` 的结算帧（`success` 或 `failed`）；崩溃例外由 route-lost 流程处理。

### 4.12 `session.next.tool.failed@1`（durable）

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `assistantMessageID` / `callID` | string | MUST | |
| `error.type` | `"unknown"` | MUST | |
| `error.message` | string | MUST | 取消/超时路径为取消原因 |
| `result` | any | MAY | |
| `provider.executed` | boolean | MUST | |
| `provider.metadata` | `ProviderMetadata` | MAY | |

- fold：`state` → `error`（可从 `pending` 或 `running` 进入；`structured`/`content` 沿用已有值，pending 的字符串 `input` 归一为 `{}`）。

### 4.13 `session.next.compaction.started@1` / `compaction.delta`（live） / `compaction.ended@1`

| 帧 | kind | 字段 |
| --- | --- | --- |
| `compaction.started@1` | durable | `messageID`（Compaction 消息 ID）、`reason("auto"|"manual")` |
| `compaction.delta` | live | `messageID`、`delta` |
| `compaction.ended@1` | durable | `messageID`、`reason`、`text`（=summary 全文）、`recent`（保留的近期上下文序列化） |

- fold：只有 `ended` 落成 `Compaction` 消息（`summary=text`）；`started` 不产生消息。
- 语义：**没有 `compaction.ended@1` 的压缩在历史上不可见**，下一 turn Gateway 仍送完整历史——Runtime 不得做不发此帧的压缩。

## 5. 共享子结构定义

### 5.1 `Delivery`

`"steer" | "queue"`（`packages/schema/src/session-delivery.ts`）。词表没有 `initial`；初始输入的 `prompted@1` 也写 `"steer"`。

### 5.2 `FileAttachment`（`packages/schema/src/prompt.ts`）

| 字段 | 类型 | 必需 |
| --- | --- | --- |
| `uri` | string | MUST |
| `mime` | string | MUST |
| `name` | string | MAY |
| `description` | string | MAY |

### 5.3 `AgentAttachment`

`{ name: string, source?: {start, end, text} }`。turn/1 请求映射规定 `agents` 恒为空。

### 5.4 `ProviderMetadata`（`packages/schema/src/llm.ts`）

`Record<string, Record<string, unknown>>`——按 provider 名分组的透传 metadata（如 Anthropic reasoning 签名）。语义必需：下一 turn 同模型时复用。

### 5.5 `ToolContent`

| 类型 | 字段 |
| --- | --- |
| `{ type: "text", text: string }` | |
| `{ type: "file", uri: string, mime: string, name?: string }` | |

### 5.6 `Model.Ref`

`{ providerID: string, id: string, variant?: string }`。

### 5.7 `UnknownError`

`{ type: "unknown", message: string }`（`SessionMessage.UnknownError`）。

## 6. 版本与校验规则

1. **按 `@N` 选定义**：`data` 必须用 `type` 中 `@N` 对应版本的 `SessionEvent` 定义校验；同名不同版本不得互相替代（如 `tool.called@1` ≠ schema 的 v2 `Called`；`step.ended@2` ≠ v1）。新版本只能作为新 `@N+1` 类型登记契约 06 §10.6 后加入。
2. **`data.sessionID === envelope sid`**，否则拒收（`diagnostic{code:"InvalidFrame"}`）。
3. **指纹核对**：`invocation.started.data.schemaFingerprint` 是已登记 `session.next.*@N` 定义指纹的 canonical sha256；Gateway 可在 `GET /v1/runtime` 协商阶段或首 turn 时缓存并与后续 invocation 比对，漂移即告警。
4. **未知类型**：live 忽略、durable 原样落库（§1 规则 5）。
5. 落库幂等：`unique(session_id, revision)` + `eventID`；同 `eventID` 重试要求逐字节相同。

## 7. 典型帧序列

### 7.1 正常一轮（初始输入 → 一段文本 + 一次工具调用 → 收尾）

```text
seq 0   durable  invocation.started          ← 表头
seq 1   durable  session.next.prompted@1     ← 初始输入 promote（delivery:"steer"）
seq 2   live     status.changed{phase:"model"}
seq 3   durable  session.next.step.started@1      ext["opencode.turnID"]
seq 4   durable  session.next.text.started@1
seq 5..n live     session.next.text.delta × N      ← 永不落库
seq n+1 durable  session.next.text.ended@1         ← 全文收口
seq n+2 durable  session.next.tool.input.started@1
seq n+3 live     session.next.tool.input.delta × N
seq n+4 durable  session.next.tool.input.ended@1
seq n+5 durable  session.next.tool.called@1         ← 先落库再打沙箱
seq n+6 durable  session.next.tool.success@1        ← 结算
seq n+7 durable  session.next.step.ended@2          ← tokens/cost
（如模型继续：回到 step.started@1，受 maxProviderTurns 约束）
seq m    live     status.changed{phase:"finishing"}
seq m+1  durable  invocation.idle            ← 终态；envelope rev == data.revision
```

随后 Runtime 关闭 SSE。Gateway：终态落库 → `DELETE /v1/turn/{inv}` → 以 `session.revision = data.revision` 开下一 turn，`context.messages[]` 由 DB 行 fold 得到。

### 7.2 steer 追加

```text
（执行中）POST /v1/turn/{inv}/ctl { kind:"append", delivery:"steer", input:{id, parts} }
seq k   durable  session.next.prompt.admitted@1   ← 录取确认
（当前 provider 流结束、工具全部结算后的安全边界）
seq k+1 durable  session.next.prompted@1           ← promote；随后进入下一 provider 轮
```

### 7.3 崩溃窗口（Runtime 在收口前消失）

DB 里最后几行可能是：`… step.started@1 → text.started@1 → tool.input.started@1`（无 `ended`/结算/终态）。还原结果：未闭合 assistant（无 `finish`）+ 空文本 part + pending tool part。**Gateway 不得修补**；route-lost 流程（契约 06 §6.5）开新 epoch，新 turn 的 `session.revision = MAX(revision)`，历史按原样送出（契约 06 §4.2：pending/running 是历史事实，Runtime 不得重新执行）。

## 8. 与还原流程的关系

- 进 fold 的帧集合、每帧贡献的 `SessionMessage` 字段、字段必要性分级、崩溃窗口语义：见 [`history-restore-from-durable-frames.md`](./history-restore-from-durable-frames.md) §6–§8。
- 一句话分工：**本文回答「每帧长什么样、什么时候来、落不落库」；还原文档回答「落库之后怎么变成下一 turn 的 `context.messages[]`」**。
