# Gateway 会话（Session）接口对接文档

> 本文档为 **需求方** 提供给 **Gateway 实现团队** 的会话管理接口需求文档。
> 覆盖会话生命周期三大接口：**创建 session / 对话 session / 销毁 session**。
> 环境 / 协议约定见下文，如有冲突以 Gateway 团队实际方案为准，并请回评修订。

**修订历史**（v0.1 初稿与 v0.2 回评修订已合并为本文档，正文即 v0.2 现行版）：

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v0.1 | 2026-08-31 | 需求方初稿：三接口框架 + `agentMd`/`history`/`userMessage` 最小字段集 |
| v0.2 | 2026-08-31 | 需求方回评修订（现行版），核心变更：① chat 请求新增 **`turnId` 幂等键**（必须，防重复执行副作用）；② 明确 **`agentMd`/`history`/`userMessage` 为调用方全量托管**——Gateway 不拼接、不持久化、每轮以请求值为准（Gateway 实现更简单：无会话状态存储）；③ **SSE 错误事件结构化**（区分可重试/不可重试）；④ 明确销毁不存在 Session 的行为为幂等成功 |

> 修订原则：**Gateway 保持无状态转发 + Sandbox 生命周期管理，对话上下文全部由调用方（AgentTeams bridge）组装**。

---

## 1. 概述

Gateway 作为外部系统，负责为配置型专家（Agent）初始化 **Sandbox 运行环境**，并提供会话级别（Session）的对话通道。

会话（Session）模型：

- 一个 **Session** 对应运行在 **Sandbox** 中的一个专家实例运行时。
- 创建时，Gateway 将专家自身 Skill 与其插件 Skill 一并安装进该 Sandbox。
- 对话通过 SSE 流式返回。
- Team 解散时销毁 Session（回收 Sandbox）。

**上下文模型（v0.2 明确，对 Gateway 透明）：**

调用方（AgentTeams bridge）每个 Turn 传入的上下文分三层，对应字段如下：

| 层 | 字段 | 语义 | Gateway 职责 |
| --- | --- | --- | --- |
| 系统指令层 | `agentMd` | Agent 身份/协作规则/人格（AGENTS.md + SOUL.md + 协作上下文） | **透传**，每轮以请求值为准 |
| 历史层 | `history` | 跨轮对话历史（bridge 自维护） | **透传**，不持久化、不拼接 |
| 当前消息层 | `userMessage` | 本轮触发消息（含群聊视野的完整文本） | **透传** |

> **关键约定：Gateway 对三层内容均为无状态透传**。Gateway 不存储对话内容、不维护会话记忆、不对 `history` 做追加或改写。同一 Turn 的重试由调用方通过 `turnId` 幂等控制。这样 Gateway 的 Session 仅承载 Sandbox 绑定关系（sessionId → sandboxId），实现最简。

### 1.1 涉及的外部依赖

| 依赖          | 作用                                                         |
| ------------- | ------------------------------------------------------------ |
| **AgentHub**  | 提供配置型专家详情（`templateId`），含 `packageUri`、Skill 列表及每个 Skill 的 `packageUri` |
| **Skill Hub** | 提供 Skill 的 `packageUri`                                   |
| **Sandbox**   | Gateway 创建并管理，用于安装 Skill 并运行专家                |

---

## 2. 通用约定

### 2.1 协议

- 传输协议：**HTTP/HTTPS**
- 数据格式：`application/json`（对话接口响应为 `text/event-stream`，SSE）
- 字符集：UTF-8

### 2.2 认证

- **当前 AgentTeams bridge 集成阶段不启用认证**：create/chat/destroy 接口不要求认证 Header。
- bridge 侧配置固定使用 `runtime.auth.type: none`，不注入、不持久化任何 Gateway token。
- 后续如果 Gateway 开启认证，需要先冻结认证协议，再由 bridge 增加对应的 AuthProvider 和 token 配置。

> 这不影响未来扩展认证；在认证协议冻结前，禁止 bridge 假设 `X-Access-Token`、SSO Token 或 eid 的具体格式。

### 2.3 响应包裹结构（非 SSE 接口）

所有非流式接口统一返回如下结构：

```json
{
  "code": "SUCCESS",
  "message": "操作成功",
  "data": { }
}
```

| 字段      | 类型   | 说明                                              |
| --------- | ------ | ------------------------------------------------- |
| `code`    | String | `SUCCESS` 成功；`FAILED` 失败；其他业务码另行约定 |
| `message` | String | 提示信息，失败时说明原因                          |
| `data`    | Object | 业务数据，成功时非空                              |

> HTTP 状态码：成功 `200`；参数校验失败 `400`；认证失败 `401`；服务端异常 `500`。

### 2.4 幂等总则（v0.2 新增）

- **create**：以 `idempotencyKey`（调用方传入）幂等——同 key 重复调用返回已存在的 Session，不重复创建 Sandbox。
- **chat**：以 `sessionId + turnId` 幂等——重复提交同一 `turnId` 时的行为见 §5.6。
- **destroy**：天然幂等（销毁已销毁/不存在的 Session 均返回成功，§6.4）。

---

## 3. 接口清单

> **AgentTeams bridge 当前只调用 chat**。Session 的 create/destroy 由调谐或平台生命周期负责，并将已创建 Session 的 `sessionId`、`sandboxId` 写入 S3 的 `openclaw.json.bridge.runtime`；bridge 启动时读取后复用。

| 接口         | 方法 | 路径                          | 说明                        |
| ------------ | ---- | ----------------------------- | --------------------------- |
| 创建 session | POST | `/v1/gateway/session/create`  | 平台/调谐调用，初始化 Sandbox 并安装 Skill |
| 对话 session | POST | `/v1/gateway/session/chat`    | **bridge 调用**，流式对话，SSE 返回       |
| 销毁 session | POST | `/v1/gateway/session/destroy` | 平台/调谐调用，释放 Sandbox                |

> 路径前缀 `/v1/gateway` 为建议值，最终以 Gateway 实际路由为准。

---

## 4. 创建 Session

### 4.1 说明

Gateway 根据 `templateId` 从 **AgentHub** 获取配置型专家详情，包括：

- 专家的 `packageUri`
- 专家当前配置的 Skill 列表，以及每个 Skill 的 `packageUri`

同时根据 `skills` 参数（Skill Hub 中的 id 列表），从 **Skill Hub** 获取对应的 `packageUri`。

将上述专家 Skill 与插件 Skill **汇总后，统一安装到 Sandbox** 中。

**幂等语义（v0.2）**：携带相同 `idempotencyKey` 的重复 create 调用，返回**已存在的** sessionId/sandboxId（`data.duplicated=true`），不新建 Sandbox。调用方（bridge）重启恢复时靠此语义找回 Session。

### 4.2 请求

```
POST /v1/gateway/session/create
Content-Type: application/json
```

**Body：**

```json
{
  "templateId": "tpl_abc123",
  "skills": ["skill_001", "skill_002"],
  "idempotencyKey": "agentteams:worker:frontend-agent"
}
```

| 字段              | 类型           | 必填 | 说明                                                         |
| ----------------- | -------------- | ---- | ------------------------------------------------------------ |
| `templateId`      | String         | 是   | 配置型专家 ID（AgentHub 中的 id），用于获取专家详情、专家 packageUri 及专家自带 Skill |
| `skills`          | List\<String\> | 否   | 需安装的插件 Skill 的 id 列表（Skill Hub 中的 id），可为空   |
| `idempotencyKey`  | String         | 否   | 幂等键（v0.2 新增）。同一 key 重复调用返回已有 Session；缺省时 Gateway 每次新建 |

### 4.3 响应

```json
{
  "code": "SUCCESS",
  "message": "创建成功",
  "data": {
    "sessionId": "sess_a1b2c3d4",
    "sandboxId": "sbx_9f8e7d6c",
    "duplicated": false
  }
}
```

| 字段                    | 类型    | 说明                                              |
| ----------------------- | ------- | ------------------------------------------------- |
| `data.sessionId`        | String  | 会话唯一标识，创建成功后生成，供对话 / 销毁时使用 |
| `data.sandboxId`        | String  | Sandbox 唯一标识，创建成功后生成                  |
| `data.duplicated`       | Boolean | v0.2 新增：`true` 表示命中幂等键返回的已有 Session |

### 4.4 错误场景

| 场景                       | HTTP | 说明                         |
| -------------------------- | ---- | ---------------------------- |
| `templateId` 为空 / 不存在 | 400  | AgentHub 中未找到专家        |
| `skills` 中某 id 不存在    | 400  | Skill Hub 中未找到对应 Skill |
| Skill 安装到 Sandbox 失败  | 500  | 返回失败信息与失败 Skill     |

---

## 5. 对话 Session

### 5.1 说明

每次对话调用需携带当前完整上下文与运行时信息，Gateway 处理后通过 **SSE** 流式返回专家回复。

**上下文组装责任（v0.2 明确）**：三字段全部由调用方组装并全量传入：

- `agentMd`：系统指令层（Agent 身份 + 协作规则 + 人格）。**每轮以本次请求值为准**——Gateway 不持久化、不缓存上次的值。调用方保证每轮传最新内容（团队成员变更下轮立即生效）。
- `history`：历史层，`[{role, content}]` 数组。**Gateway 原样透传给运行时，不做追加、不做拼接、不持久化**。下一轮的 history 由调用方自行维护（调用方语义：见 §5.5 调用方说明）。
- `userMessage`：当前消息层，本轮触发消息全文（调用方格式自定义，可以是任意结构化文本，Gateway 不解析）。

> 对 Gateway 而言：chat = 收到四元组（sessionId / turnId / 上下文 / 用户消息）→ 转发给 Sandbox 内运行时 → SSE 流回传。**无会话级存储义务**。

### 5.2 请求

```
POST /v1/gateway/session/chat
Content-Type: application/json
```

**Body：**

```json
{
  "sessionId": "sess_a1b2c3d4",
  "sandboxId": "sbx_9f8e7d6c",
  "turnId": "evt_oCOZLhucNczxLoAoUU",
  "agentMd": "## Agent 身份\n...(AGENTS.md 全文)\n\n## 人格\n...(SOUL.md 全文)\n\n## 协作上下文\n- Coordinator: @web-team-lead:...",
  "history": [
    {"role": "user", "content": "王五: @frontend-agent 按李四说的做"},
    {"role": "assistant", "content": "收到，我先看下登录页的现有实现..."}
  ],
  "userMessage": "[Chat messages since your last reply - for context]\n张三: 讨论下登录页方案 [id:$abc]\n李四: 我觉得蓝主题更好 [id:$def]\n\n[Current message - respond to this]\n王五: @frontend-agent 按李四说的做"
}
```

| 字段          | 类型         | 必填 | 说明                                                |
| ------------- | ------------ | ---- | --------------------------------------------------- |
| `sessionId`   | String       | 是   | 会话唯一标识（创建时返回）                          |
| `sandboxId`   | String       | 是   | Sandbox 唯一标识（创建时返回）                      |
| `turnId`      | String       | **是** | v0.2 新增：Turn 幂等键。调用方保证同一 Session 内唯一（建议用触发事件的 event_id）。幂等行为见 §5.6 |
| `agentMd`     | String       | 否   | 系统指令层（每轮以请求值为准，Gateway 不持久化）     |
| `history`     | List\<Object\> | 否   | 历史层 `[{role, content}]`，全量透传，Gateway 不改写 |
| `userMessage` | String       | 否   | 本次用户输入（当前 turn 全文，调用方自定义格式）     |

### 5.3 响应（SSE）

```
Content-Type: text/event-stream
```

**事件格式（标准 SSE，`data:` + JSON，`\n\n` 结束）：**

```
data: {"event":"message","content":"你好，我是……","delta":"你好"}
```

**SSE 事件类型：**

| 事件      | 说明                                   |
| --------- | -------------------------------------- |
| `message` | 专家回复增量内容（`delta` 为增量片段） |
| `done`    | 对话结束标记，携带完整聚合文本（v0.2）  |
| `error`   | 对话过程异常，结构化错误（v0.2，见下）  |

**error 事件结构（v0.2 细化）：**

```
data: {"event":"error","code":"TURN_TIMEOUT","retryable":false,"message":"turn execution exceeded timeout"}
```

| 字段        | 类型    | 说明                                                                 |
| ----------- | ------- | -------------------------------------------------------------------- |
| `code`      | String  | 机器可读错误码（见下表）                                             |
| `retryable` | Boolean | 调用方据此决定是否可安全重试（true=提交层可重试；false=重试无意义）  |
| `message`   | String  | 人可读描述                                                           |

建议错误码（可由 Gateway 团队增补）：

| code              | retryable | 说明                       |
| ----------------- | --------- | -------------------------- |
| `SESSION_EXPIRED` | true      | Session/Sandbox 已失效，调用方需重建后重试 |
| `SANDBOX_BUSY`    | true      | 同一 Sandbox 上一 Turn 仍在执行（调用方可稍后重试同 turnId） |
| `TURN_TIMEOUT`    | false     | 本 Turn 执行超时（副作用已发生与否未知，调用方不自动重试） |
| `INTERNAL_ERROR`  | false     | 运行时内部错误             |

> `retryable=true` 的场景，调用方重试时**携带同一 turnId**——Gateway 依据幂等（§5.6）避免重复执行。

**示例流：**

```
data: {"event":"message","delta":"你好"}
data: {"event":"message","delta":"，我是专家助手"}
data: {"event":"message","delta":"，有什么可以帮你？"}
data: {"event":"done","content":"你好，我是专家助手，有什么可以帮你？"}
```

> SSE 事件名、字段为建议格式，最终以 Gateway 团队的流式协议为准，双方对齐后冻结。

### 5.4 请求-流语义（v0.2 明确）

- **提交成功定义**：Gateway 返回 HTTP 200 且开始输出 SSE 流（首事件到达）。在此之前（连接失败/4xx/5xx），Turn 未被受理，调用方可重试（同 turnId）。
- **流中断**：SSE 连接中断且未收到 `done`/`error` 事件——调用方视为 Turn 结果未知，**不自动重试**（防副作用重复），按业务中断处理。
- **同 Session 串行**：同一 Sandbox 同一时间只执行一个 Turn（FIFO）。上一 Turn 未完成时新 Turn 的受理行为由 Gateway 定义（建议：排队或返回 `SANDBOX_BUSY`，二选一需冻结）。

### 5.5 调用方上下文维护策略（v0.2 新增，供 Gateway 理解调用行为）

调用方（AgentTeams bridge）按以下规则组装每轮请求，**不需要 Gateway 任何配合**：

1. `history` 的 `content` **只存每轮的"当前消息"部分**（不含该轮两段式文本里的群聊视野块）——历史是单层平铺的，不会滚雪球；
2. `userMessage` 是完整两段式文本（群聊视野 + 当前消息），**只在本轮出现一次**，下一轮它已收敛为 history 里的一条 user 记录；
3. `agentMd` 每轮重拼（协作上下文可能因 team 成员变更而更新），因为 Gateway 不持久化，天然实现"变更下轮生效"；
4. agent 自身的回复在下轮作为 `{"role":"assistant","content":...}` 进入 history（content 为最终聚合文本，即 `done` 事件的完整内容）。

> 对 Gateway 的唯一含义：**这些字段就是普通字符串/数组，透传即可**。

### 5.6 幂等行为（v0.2 新增）

以 `sessionId + turnId` 为幂等键：

| 场景 | 行为 |
| --- | --- |
| turnId 首次提交 | 正常执行，SSE 流回传 |
| turnId 重复提交，原 Turn **已完成** | 重新回放结果：优先重放聚合文本（单个 `message` 事件 + `done`）；无法重放时返回 `error{code:"DUPLICATED",retryable:false,message:"turn already completed, replay unavailable"}` |
| turnId 重复提交，原 Turn **仍在执行** | 挂接到正在执行的事件流（后续事件继续推送）；无法挂接时返回 `error{code:"SANDBOX_BUSY",retryable:true}` |
| 幂等窗口 | 建议：Turn 完成后保留幂等记录 ≥ 24h（调用方重试都发生在秒级~分钟级，24h 裕量充足） |

> 若 Gateway 实现重放/挂接成本高，可降级为：重复提交一律返回 `DUPLICATED`（不带重放）——调用方可接受，会在桥侧把该 Turn 标记为"结果未知，走中断通知"。**此项由 Gateway 团队按实现成本决定，需冻结选择。**

### 5.7 错误场景（非流式，HTTP 层）

| 场景 | HTTP | 说明 |
| --- | --- | --- |
| sessionId/sandboxId 不匹配或不存在 | 404 | SESSION_NOT_FOUND（调用方重建 Session） |
| 缺 turnId | 400 | 参数校验失败 |
| 认证失败 | 401 | |

---

## 6. 销毁 Session

### 6.1 说明

Team **解散时调用本接口销毁 Sandbox**。
本接口需保证**幂等**：对已销毁的 Session 重复调用时，仍返回成功，并附带提示信息。

### 6.2 请求

```
POST /v1/gateway/session/destroy
Content-Type: application/json
```

**Body：**

```json
{
  "sessionId": "sess_a1b2c3d4"
}
```

| 字段        | 类型   | 必填 | 说明                       |
| ----------- | ------ | ---- | -------------------------- |
| `sessionId` | String | 是   | 会话唯一标识（创建时返回） |

### 6.3 响应

**首次销毁：**

```json
{
  "code": "SUCCESS",
  "message": "Sandbox 已回收",
  "data": {
    "success": true
  }
}
```

**重复销毁（幂等，已销毁）：**

```json
{
  "code": "SUCCESS",
  "message": "已回收sandbox,不需要重复执行",
  "data": {
    "success": true
  }
}
```

| 字段           | 类型    | 说明                                                         |
| -------------- | ------- | ------------------------------------------------------------ |
| `data.success` | Boolean | 销毁处理结果，始终为 `true`                                  |
| `message`      | String  | 首次销毁为「Sandbox 已回收」；重复销毁为「已回收sandbox,不需要重复执行」 |

### 6.4 幂等约定

- `sessionId` 已被销毁 -> 仍返回 `code=SUCCESS`, `data.success=true`，`message="已回收sandbox,不需要重复执行"`。
- `sessionId` 不存在 -> **v0.2 冻结：返回成功 + 幂等提示**（`message="sandbox不存在或已回收"`）——调用方语义是"确保资源不残留"，不存在即目标已达成。**不返回 404**（避免调用方误判为异常去重建）。

---

## 7. 流程衔接（与 Team 生命周期）

| 阶段                     | 动作                                                |
| ------------------------ | --------------------------------------------------- |
| **创建 Team / 专家就绪** | 调用「创建 Session」（带 idempotencyKey）-> 获得 `sessionId`、`sandboxId` |
| **对话**                 | 反复调用「对话 Session」（SSE，带 turnId 幂等）       |
| **Team 解散**            | 「销毁 Session」释放 Sandbox，重复调用幂等处理      |

> 调用方异常恢复路径（供参考）：bridge 进程重启 -> 重新从 S3 拉取调谐下发的 `sessionId`/`sandboxId` -> 从本地/存储恢复 history 继续对话。bridge 不重调 create。

---

## 8. 待确认项（Open Questions）

| #    | 问题                                       | 说明                                                         |
| ---- | ------------------------------------------ | ------------------------------------------------------------ |
| 1    | ~~认证头字段~~                             | **当前 bridge 集成阶段不鉴权**；未来若启用认证，需另行冻结协议后再改 bridge |
| 2    | SSE 协议字段                               | `message/done/error` 事件名与字段（含 v0.2 的 error 结构化字段），需与 Gateway 冻结 |
| 3    | 路径前缀                                   | `/v1/gateway` 是否为最终路由前缀                             |
| 4    | ~~sessionId 不存在时的销毁行为~~           | **v0.2 已冻结：幂等返回成功**（§6.4），不再是 open question  |
| 5    | ~~agentMd / history / userMessage 结构~~   | **v0.2 已冻结：三层透传语义**（§5.1/§5.5），不再是 open question |
| 6    | **重复 turnId 的降级选项**（v0.2 新增）     | 重放/挂接 vs 一律 DUPLICATED（§5.6），由 Gateway 按实现成本定，需冻结 |
| 7    | **同 Sandbox 并发 Turn 的受理行为**（v0.2 新增） | 排队 vs 拒绝（SANDBOX_BUSY），二选一需冻结（§5.4）     |
| 8    | **SSE 心跳/保活**（v0.2 新增）              | 长执行 Turn（数分钟级）期间是否有 comment/keepalive 行防止中间层断开空闲连接，需确认 |

---

## 附录 A：字段语义速查（v0.2 新增）

| 字段 | 谁生成 | 生命周期 | Gateway 是否存储 |
| --- | --- | --- | --- |
| `sessionId` | Gateway（create 时） | Session 全程 | 是（→ sandboxId 绑定） |
| `sandboxId` | Gateway（create 时） | Session 全程 | 是 |
| `idempotencyKey` | 调用方 | create 幂等 | 是（幂等记录，建议 TTL ≥ 7d） |
| `turnId` | 调用方（建议 = 触发事件 event_id） | 单 Turn | 是（幂等记录，建议 TTL ≥ 24h） |
| `agentMd` | 调用方 | 单轮，每轮覆盖 | **否** |
| `history` | 调用方 | 单轮，全量重传 | **否** |
| `userMessage` | 调用方 | 单轮 | **否** |
