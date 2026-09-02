# cimicode 无状态 worker — Bridge 设计文档

> 依据《cimicode无状态worker的方案.md》(下称"方案文档")。命名沿用方案文档:Bridge / cimicode 无状态服务 / Session / Turn / submit_turn / create_expert_session / ~~history buffer / 两段式~~(修订:flat history,见 §3.4) / requireMention / groupAllowFrom / peer-mentions / NO_REPLY / 协作上下文 / report-ready;runtime 名 `cimicode-bridge`。(~~heartbeat~~ 已删:B12 取消,见拓扑前提)
>
> **设计总原则:尽可能对齐现有 worker 的协作方式。** 配置由调谐写入 S3(含 cimicode gateway baseurl + templateId)、启动时从 S3 拉取、就绪走 report-ready——这些机制沿用,只把"runtime 内核"从常驻 OpenClaw 换成对 cimicode 无状态服务的按 Turn 调用。**Matrix 凭证不走密码重登录(评审修正 2026-08-28:bridge 不做任何登录,凭证 = env token 注入,见 §3.1)。**
>
> **拓扑前提(评审修正,2026-09)**:**Leader 保持 OpenClaw 现状,只有 Worker 换成 bridge**。即:Manager(OpenClaw/copaw)→ Leader(OpenClaw,常驻、Matrix plugin、心跳全保留)→ Workers ×N(bridge,无状态)。后果:
> - bridge 只承担 **worker + standalone** 两种角色——**team_leader 分支、heartbeat 定时器全部删除**(B12 取消,§五.4 启动时序 Phase 4 取消,§七.3 协作上下文 leader 模板取消);
> - 跨 runtime 边界在 **team 内部**:OpenClaw Leader ↔ bridge Worker 是主协作通道——bridge 发侧必须三层 mention 全写(§三.6),OpenClaw Leader 收侧才 wake up;
> - bridge worker **无心跳**,OpenClaw Leader 需要存活探针——本地只读 `/status` 接口(扩展点 #10)**升级为必做**(§七.5)。
>
> **评审修正(2026-08-28,S3/凭证/AGENTS.md 三项)**:① **bridge 不做任何登录**——Matrix 密码 login 链整体裁剪,凭证唯一来源 = env `AGENTTEAMS_WORKER_MATRIX_TOKEN`,401 恢复走 controller matrix-token 刷新接口(§3.1);② **cimicode gateway 不鉴权**(Higress 鉴权关),bridge 只需 **baseurl + templateId**,二者由调谐写入 S3 openclaw.json bridge 扩展段(§5.1/T3,不再新增 env);③ **AGENTS.md 启动拉一次缓存,每个 Turn 都随 agentMd 携带**(§7.3;`config.refresh_interval` 可选开启每 Turn 重拉)。
>
> **flat history 修订(2026-08-31,依 gateway 契约 v0.2)**:原"内存 history buffer + 两段式文本"机制**整体废除**,改为 **flat history 数组**直传 gateway chat 接口的 `history` 字段(§3.4 重写)。gateway 对三字段(agentMd / history / userMessage)**纯透传**,对话记忆由 bridge 自维护。调用面命名对齐 gateway 契约 v0.2(`wiki/gateway-session.md（v0.2 现行版）`):**create / chat / destroy + turnId 幂等**。两段式是 copaw"轮次型 session 存储"的历史包袱;flat history 消除嵌套滚雪球、每条群聊消息只出现一次,agent 群聊视野更完整(跨轮而非仅上次回复之后)。代价:①"背景 vs 指令"信号从 marker 降级为 agentMd 协作规则软约束("仅响应最后一条 @ 你的消息");② history 增长更快,bridge 必须做滑窗裁剪。

---

## 一、总览

### 1.1 现有 worker 协作基线(本设计的参照事实)

现有 worker(调谐 → 启动 → 运行)的实际链路,已核实代码:

| 环节 | 现有 worker 的做法 | bridge 的处理 |
|---|---|---|
| 配置生成 | 调谐(deployer)把 openclaw.json / SOUL.md(seed-only,不覆盖) / AGENTS.md(三层合并+协作上下文注入) / HEARTBEAT.md(Leader,OpenClaw 仍需要) / config/mcporter.json / `credentials/matrix/password` 写入 S3 `${STORAGE_PREFIX}/agents/<name>/` | **沿用**。bridge 复用 openclaw.json(+bridge 扩展段,**含 cimicode gateway baseurl + templateId**,§五.1);不需要 mcporter.json/skills/HEARTBEAT.md |
| 配置拉取 | entrypoint 启动时从 S3 mirror 到 workspace,**重试 6 次×5s 等 deployer 写入**(调谐与 pod 启动存在竞态,靠重试收敛);验证必需文件存在 | **沿用**(进程内 S3 SDK 拉取,同一重试语义) |
| Matrix 凭证 | 密码在 S3 `credentials/matrix/password`(调谐写入);启动时拉密码 → **重新 login 换 fresh access_token + device_id**;另有 env 注入 token 一路 | **裁剪登录(评审修正 2026-08-28)**:不做密码 login,凭证唯一来源 = env `AGENTTEAMS_WORKER_MATRIX_TOKEN`(controller 注入),启动 whoami 校验;S3 密码文件仍拉取但不消费(§3.1) |
| Token 失效 | 平台提供 `POST /api/v1/credentials/matrix-token`(controller)供 401 恢复 | **沿用**(登录链裁剪后成为 401 **唯一**恢复路径,§3.1) |
| 文件同步 | 本地 fs ↔ S3 双向常驻 sync | **裁剪**。bridge 无工作区文件,仅启动拉配置(见 §五) |
| 就绪上报 | 后台等 runtime 健康 → `agt worker report-ready --name ${AGENTTEAMS_WORKER_CR_NAME:-${WORKER_NAME}}` | **沿用**(命令不变,就绪判定换为 bridge 自己的语义,见 §七.5) |
| runtime | 常驻 OpenClaw(本地 LLM gateway + session + cron) | **替换**:submit_turn 调 cimicode 无状态服务,Session/Turn 状态在服务侧 |
| 心跳 | OpenClaw worker 有 heartbeat/dreaming(常驻 runtime 内部) | **删除**:bridge worker 无心跳(拓扑前提:Leader 保持 OpenClaw 负责心跳与 worker 存活管理,经 §七.5 `/status` 探活) |

### 1.2 职责与范围

**bridge 全部职责**(方案 §4.1):Matrix 监听与 @mention 硬过滤、flat history 组装(§3.4)、调 gateway(create / chat / destroy,契约 v0.2)、事件流消费转发 Matrix、协作上下文注入、启动生命周期与 report-ready、**本地只读 /status 存活接口(§七.5,供 OpenClaw Leader 探活)**。

**bridge 不承担的职责**:~~heartbeat 定时器~~(Leader 保持 OpenClaw,worker 无心跳)、跨 Session 文件协作的实现(方案 §4.7 未定稿,只留通道)。

**范围外**(他人负责,只预留接口):cimicode 无状态服务及其配套、operator 改造(EnvBuilder/在 openclaw.json 加 bridge 扩展段/注入 env)、controller 改造、跨 Session 文件协作的实现(方案 §4.7 未定稿,只留通道)。

### 1.3 技术前提

- Python 3.12+,asyncio 单进程;**首版不支持 E2EE**(不引入 libolm;team room 不启用加密为平台约束,加密事件降级处理,见 §三.5)。
- 依赖:matrix-nio(无 e2e extra)、httpx + httpx-sse、pydantic v2、pyyaml、jsonpath-ng、minio(S3 拉配置)、redis(可选 extra)。
- **Matrix 协议层复用 agentteams-matrix-channel 插件(评审修正 v2)**:**`plugins/agentteams-matrix-channel/agentteams_matrix/channel.py`(4605 行,AgentTeamsMatrixChannel)是当前项目最成熟的 Matrix 实现**——它是 `copaw/src/matrix/channel.py` 的 **QwenPaw 2 继任者**(commit 124f06d1 将 Manager runtime 从 copaw 迁到 QwenPaw 2.0 时引入,当前 Manager 正在使用)。已实现并验证:**login 三级链(密码/token/whoami/401 恢复)、sync 循环 + since 持久化、429 退避、catch-up sync、E2EE(olm + SAS 验证,完整)、两段式格式、NO_REPLY、发侧三层 mention(body + matrix.to 锚点 + m.mentions.user_ids)、流式 thread 编辑、长消息分包(64KB 协议限制)、readiness 探针回复、TeamHarness 触发**。bridge 的 matrix/gateway.py、matrix/mention.py **优先从该插件移植而非重写**(sync/mention/渲染方法直接抽取;**登录链不移植**——评审修正 2026-08-28:bridge 不做 Matrix 登录)——OpenClaw Leader(收侧)与 QwenPaw Manager(发侧)是 bridge 唯二的跨 runtime 对端,复用该插件使三端协议语义天然一致,混跑兼容性由共享代码保证。
  > **移植注意**:插件是 `qwenpaw.app.channels.base.BaseChannel` 子类,依赖 QwenPaw 框架(process/on_reply_sent/WORKING_DIR/enqueue);bridge 是裸 asyncio 单进程,**需剥离 BaseChannel 依赖,保留 Matrix 协议逻辑**(登录/sync/mention/渲染方法可近乎照抄,发送/事件回调路径改为 bridge 自己的 asyncio 队列)。TeamHarness 专属逻辑(`_teamharness_self_trigger`/attachment 关系)bridge 场景可裁剪。E2EE store 路径插件用 WORKING_DIR 持久化,bridge 无工作区文件,启用 E2EE 时改走 StateStore。

---

## 二、核心流程(端到端)

### 2.1 角色与拓扑

```
                        AgentTeams 平台(controller 调谐 + S3 + Matrix + cimicode 无状态服务)
┌───────────────────────────────────────────────────────────────────────────────┐
│  Manager (OpenClaw/copaw,现状)                                                 │
│      │ 派活                                                                     │
│      ▼                                                                          │
│  Leader (OpenClaw,现状:常驻 + Matrix plugin + 心跳 + 协作上下文注入)              │
│      │ @mention 派活 / /status 探活(§七.5)                                       │
│      ▼                                                                          │
│  Worker 1 (bridge)   Worker 2 (bridge)   ... Worker N (bridge)                  │
│    │ Matrix /sync 监听 + 硬过滤 + flat history → chat(turnId) → 事件流 → 回复 Matrix   │
│    └─ 每个 worker = 1 个 bridge pod + 1 个 cimicode Session + 1 个 Sandbox        │
└───────────────────────────────────────────────────────────────────────────────┘
```

- **每 agent 1 个 bridge pod**(方案决策 1):保持分布式,爆炸半径 = 1 个 agent。
- **bridge 不直接调 CimiCode,调 cimicode 无状态服务 API**(方案决策 2):submit_turn 是唯一入口。
- **每 agent 1 个 Session**(方案决策 3):cimicode 无状态服务不需要知道 team 概念,协作逻辑全在 bridge。
- **硬过滤在 bridge,软指令在 AGENTS.md**(方案决策 4)。

### 2.2 主流程:agent 被 @mention 响应(方案 §5.2)

```
Human/Leader 在 room 发消息 @agentA
  → bridge /sync 收到 m.room.message
  → §三.3 硬过滤链:requireMention → groupAllowFrom → 自@/unknown
  → 命中 → HistoryStore.snapshot() + agentMd 拼装(§3.4 flat history)
  → §四 chat(sessionId, turnId, agentMd, history, userMessage)(契约 v0.2)
  → cimicode 无状态服务调度 CimiCode Replica 执行
  → SSE 事件流 → §四.3 RuntimeEvent 聚合
  → §三.6 渲染管线:Markdown → formatted_body 白名单消毒 + 三层 mention
  → POST /rooms/{roomId}/send/m.room.message 回复
```

### 2.3 协作与中断

| 场景 | 路径 | 设计落点 |
|---|---|---|
| Leader 派活 | OpenClaw Leader → @worker(文本)→ bridge 收 | §三.3/§三.4 |
| Worker 汇报 | bridge 三层 mention → OpenClaw Leader 收 | §三.6/§九 E2E-6 |
| Worker 间协作 | 必须经 Leader 中转(peer-mentions 阻断) | §三.3 |
| 断流/超时 | bridge 发中断通知,不自动重试,重新 @ 再触发 | §七.1 |
| 重启恢复 | Session 复用(idempotencyKey)+ history 重建 + since 续传 | §七.1/§七.2/§七.4 |

---

## 三、核心设计一:Matrix 接入

### 3.1 身份与凭证(env token 直用;评审修正 2026-08-28:登录链整体裁剪)

```
启动:
  ① env token(唯一路径,controller 注入)
     AGENTTEAMS_WORKER_MATRIX_TOKEN + whoami() 校验 → user_id / device_id(仅存内存)
     token 缺失/校验失败 → Phase 1 失败重试(退避),不 crash
运行中(sync 返回 M_UNKNOWN_TOKEN / 401):
  ② 调 controller POST /api/v1/credentials/matrix-token 换新 token
     (AGENTTEAMS_CONTROLLER_URL + AGENTTEAMS_AUTH_TOKEN(_FILE))
     有限次重试 + 退避;期间 sync 循环不退出,失败则继续循环重试 + 告警
```

- **裁剪项(评审修正 2026-08-28)**:S3 密码 login / 运行中密码重 login / login 429 风暴防护(无 login 即无风暴)/ device_id 每次启动新建——全部随登录链删除;`matrix.credentials.mode`/`password_env` 配置组删除。
- **access_token / device_id 只存内存、不持久化**(任务 B2 语义保留);StateStore 不存任何 Matrix 凭证。
- **token 有效期由 controller 保障**(注入即 fresh);刷新持续失败 → sync 停滞 + 告警,`/status` Matrix 连接态=false(§7.5),不退出进程。
- S3 `credentials/matrix/password` 仍拉取但 bridge 不消费(兼容现有 S3 布局,为将来恢复登录链预留)。

### 3.2 连接与同步循环

客户端与循环参数:

- `AsyncClient(homeserver)`,`AsyncClientConfig(store_sync_tokens=False)` —— since token 由 bridge 自己管(内存,可选 StateStore 持久化),不交给 SDK 内部状态。
- HTTP request_timeout = max(sync 长轮询秒数 + 30, 60),防止 HTTP 层提前掐断合法长轮询。

sync 循环(状态机):

```
load since = 内存(可选 StateStore 持久化,persist=true 时)
if since 缺失:
    catch-up 同步:临时摘除事件回调 → sync(timeout=0, full_state=True) 只取 next_batch
    → 恢复回调。效果:不回放历史、不长轮询吞新消息(since 丢失的"限流全量"语义)
else:
    sync(timeout, since, full_state=True) —— 加载 room 成员 displayname(@mention
    解析需要),离线窗口内的消息正常处理
标记 Matrix 就绪
loop:
    resp = sync(timeout, since, full_state=False)
    next_batch → 内存 since(每轮覆盖;persist=true 时同步写 StateStore)
    auto-join 被 invite 的 room
    401/M_UNKNOWN_TOKEN → §3.1 ② 刷新链;其他错误 → sleep 重试;CancelledError → 干净退出
```

- **since token 持久化(评审修正,任务 B3)**:默认 `matrix.since.persist=true`(Redis 为生产标配),重启续传不重放;`false` 时仅内存 → 重启走 catch-up 全量同步(timeout=0 + full_state 只取 next_batch,不回放历史、不吞新消息)。
- **事件解析(任务 B3)**:`m.room.message` → body/sender/room_id/event_id 进处理链;`m.room.member` → 维护 displayname 映射(@mention 解析用);其余类型忽略,媒体消息(m.image/m.file 等)按 §3.3 规则记 `[media: 文件名]` 占位进 buffer。
- **重连退避(任务 B3)**:1s→2s→4s→…封顶 30s;401 走刷新链(§3.1 ②)不走退避。
- homeserver 来源:`AGENTTEAMS_MATRIX_URL`(env,平台已注入);domain(`AGENTTEAMS_MATRIX_DOMAIN`)用于 @mention 无 domain 补全。

### 3.3 消息处理链(硬过滤)

收到 room 消息后按固定顺序过滤(全部规则可配):

```
1. 自己发的消息 / 非文本类型 → 忽略(媒体消息记占位 [media: 文件名] 进 buffer)
2. requireMention(默认 true):不 @自己 → 不触发,进 history buffer
3. RoleResolver:sender 归类 leader / admin / worker / human / self / unknown
   (依据 COORDINATION_LEADER/ADMIN/WORKERS + 自身 user_id)
4. groupAllowFrom 白名单(按角色,评审修正——bridge 只有 worker 角色):
   worker 默认 [leader, admin, human](Leader 是 OpenClaw,peer-mentions 语义:
   worker 的响应 @另一 worker,对端因 sender 不在白名单而不触发,
   协作只能经 OpenClaw Leader 中转,与现有 worker 完全一致)
5. unknown sender → 默认拒绝(allow_unknown 可放开)
```

@mention 解析(容错):`m.mentions` 字段 → `matrix.to` 事件指针 → 文本正则 `@[\w.-]+:[\w.-]+` 三级检测;无 domain 时查 COORDINATION 映射补全;尾部空格 trim、`@` 后空格容错;displayname 别名匹配可配开启(依赖 §3.2 full_state 加载的成员名)。**匹配失败一律不触发——宁漏勿误。**

### 3.4 flat history 组装(2026-08-31 修订,替代原"两段式群聊视野")

> **机制变更**:copaw 的"内存 history buffer + 两段式文本打包"是轮次型 session 存储(.jsonl 按 turn 追加)的历史包袱——沉默期消息没地方按条写,只能拼进本轮 user content,导致上下文嵌套滚雪球(第 N 轮两段式包含第 N-1 轮 buffer,而后者已在 session 历史里)。gateway chat 接口(v0.2 契约)原生支持 `history: [{role, content}]` 数组,因此 bridge 改为 **flat history**:每条群聊消息独立一条记录,只出现一次。

**数据结构**:`HistoryStore`,per-room 消息列表(内存 + StateStore 持久化可选):

```
# 每条记录 = {"role": "user"|"assistant", "content": str, "event_id": str?}
# user content 带 sender 前缀: "王五: @frontend-agent 按李四说的做"
# assistant content = turn 完成时的聚合文本(done 事件全文)
```

**写入规则**:

- 不 @ 自己且通过白名单的群聊消息 → `append_user(sender_display_name, body, event_id)`;**白名单拒绝的消息不进 history**(与原 buffer 规则一致)。
- DM 消息不进 history(DM 触发的 Turn 由 turn 编排直接组装)。
- agent 回复完成后 → `append_assistant(聚合文本)`。
- **event_id 去重**:Matrix 事件重放/重启重建时按 event_id 跳过已存在的记录。
- **滑窗裁剪**:`history.max_entries` 默认 200(比原 buffer 的 50 大,因为现在承载跨轮全量群聊);超出 FIFO 淘汰。按 token 预算裁剪预留(首版按条数)。
- 媒体消息记占位 `[sent an image: 文件名]` 文本(vision 通道 B10 再议)。

**触发 Turn 时的请求组装**(对齐 gateway 契约 v0.2 §5.2):

```json
{
  "turnId": "<Matrix event_id>",
  "agentMd": "<协作上下文 + AGENTS.md + SOUL.md,每轮全量重拼>",
  "history": "<HistoryStore.snapshot(): 单层平铺数组,不含本轮消息>",
  "userMessage": "<当前触发消息(带 sender 前缀,含 @mention 原文)>"
}
```

- **userMessage 仅为当前那条被 @ 的消息**(含 sender 前缀),不再打包任何历史块。
- gateway 按 messages 语义组装 `[system(agentMd), ...history, user(userMessage)]`,**不解析、不拼接、不持久化**(契约 §5.1)。
- **"背景 vs 指令"信号降级为软约束**:原两段式 marker 明确告诉 LLM"history 块只是背景";flat 模式下由 agentMd 协作规则承载——SystemPromptBuilder 在协作上下文末尾注入一条:"history 中的群聊消息仅作上下文;只响应 userMessage(最后一条 @ 你的消息)"。硬过滤(§3.3)仍兜底防误触发。

**turn 中断语义变更**:

- 原"turn_interrupted 保留 buffer"失效——flat history 无 buffer 概念;中断时**本轮 userMessage 是否写入 history 由配置决定**(`history.record_interrupted`,默认 false:中断消息不落 history,重新 @ 时它作为新的 userMessage 重现,不重复)。
- turn 提交语义:本轮 userMessage **在提交 Turn 前写入 history**(与 copaw "enqueue 即清"等价——"提交即记录"),assistant 回复在 turn 完成时写入。

**重启重建(任务 B6,语义调整)**:从主 room timeline(`AGENTTEAMS_WORKER_ROOM_ID`)回拉最近 N 条(默认 50)重建 history(user 消息 + 按 timeline 里的 agent 消息恢复 assistant 记录)。若 StateStore 持久化开启(`history.persist`,默认 false),优先从 StateStore 恢复,timeline 回拉作 fallback。**受 Synapse 历史保留/可见性限制时拉到多少算多少**,不足 N 条不补造、不告警、不阻塞。

### 3.5 加密事件处理

首版不做密钥协商。收到加密事件(MegolmEvent)→ 警告日志 + 不触发 Turn、不进 buffer、不 crash;room 级加密则该 room 等效失联(持续告警)。`matrix.e2ee` 配置位保留(首版仅 off),未来启用时只动本节与登录处,其余模块不变。

### 3.6 发送、格式转换与流式(任务 B10 / B11)

**渲染管线**(结构化运行事件 → Element Web 可读消息):

```
RuntimeEvent 流 ──dialect part 聚合──▶ Markdown 全文
              ──格式转换──▶ m.room.message
                 body           = 纯文本(去 Markdown 语法)
                 formatted_body = Markdown → 安全 HTML(org.matrix.custom.html)
```

- **文本渲染(任务 B10)**:`formatted_body` 走 Markdown→HTML 转换 + **白名单消毒**:仅保留 Element 支持标签(p/br/pre/code/ul/ol/li/blockquote/h1-h6/strong/em/del/a[href]/table/thead/tbody/tr/th/td/hr),剥离 script/style/事件属性(on*)/危险协议链接(javascript:)。`emitter.format.markdown_html: false` 时只发纯文本 body(Element 退化为原文渲染)——**转换层故障不阻塞发送,永远有退化路径**。
- **结构化内容渲染策略(任务 B10)**:cimicode 事件中的 thinking / tool_call / tool_result / component 标记按类别可配:
  - `thinking` → 默认 `hide`(不发);可选 `quote`(引用块 `> …` 折叠)/ `show`(全文);
  - `tool_call` / `tool_result` → 默认 `compact` 单行(`🔧 web_search(q=…) → 3 条结果`);可选 `hide` / `show`(多行详情);
  - `component` / `artifact` → 可读占位(`[组件: {type}]`)+ 末尾 `artifact: {artifact_id}` 引用行(跨 Session 文件协作的传递通道,方案 §4.7)。
- **NO_REPLY 检测(任务 B11)**:trim 后精确等于 `NO_REPLY`(mode: exact|trim|regex 可配,容忍 `" NO_REPLY\n"`);命中 → 不发 Matrix,仍清 buffer。
- **流式策略(任务 B10 的"流式 vs 完整"决策)**:Matrix 不支持消息编辑,三选一可配:`complete`(等整条,默认,格式转换一次成型)/ `chunked`(按完整文本段分多条)/ `throttled`(增量节流聚合,默认 3s/40 字起批,最接近现有 runtime 的 partial 流式体验;每个节流窗口的聚合文本**独立走一次格式转换**)。
- **@mention 处理(任务 B11,评审修正)**:bridge 发出的响应文本里,@其他 agent(worker 汇报 Leader / 任务转发)必须 **三层 mention 全写**:
  1. `body` 明文 `@leader:domain`(纯文本可读);
  2. `formatted_body` 里 `matrix.to` 锚点(Element pill);
  3. `m.mentions.user_ids` 结构化字段(MSC3952)。
  原因:**OpenClaw Leader 收侧只认结构化 mention 触发 wake up**(agentteams_matrix/channel.py 注释明确"OpenClaw 需要结构化+可见双条件"),bridge 若只发纯文本 @,Leader 会静默漏收——这是全 bridge worker 拓扑下的主协作通道,必须与插件发侧已验证逻辑一致(见 §1.3 复用决策)。**收侧**仍按 §3.3 三级检测判定,发送侧三层同写保证对端(OpenClaw Leader / QwenPaw Manager)都能触发。

---

## 四、核心设计二:cimicode 无状态服务对接(SDK 设计)

> **这一章用大白话讲什么**:bridge 要跟外部服务(cimicode 无状态服务)通信,而这个服务是**他组做的、接口还没完全定死、以后还可能换**。这一章设计一套"**换服务不用重写 bridge**"的机制——把"跟外部服务通信"做成一个**可插拔、可翻译、可降级**的系统。
>
> 打个比方:
> - **Adapter(充电头)**:每个服务一个,知道"这个服务的网址、API 长什么样"。
> - **Dialect(翻译官)**:每种事件格式一个,知道"这个服务吐的 `message.part.delta` 是什么意思"。
> - **引擎(USB-C 口)**:所有服务共用,只管"发 HTTP 请求、收 SSE 流、处理超时/断流"这些通用技术活。
> - **SPI(插头标准)**:规定"充电头长什么样才能插进来"。
>
> **换一个服务 = 换一个充电头 + 换一个翻译官,USB-C 口(引擎)和 bridge 其他代码一行不用改。**

### 4.1 连接形态:bridge 只调 3 个 API(2026-08-31 修订:命名对齐 gateway 契约 v0.2)

cimicode gateway = **HTTP 服务,SSE 接口**。bridge 的调用面(命名按 gateway 契约 v0.2,`wiki/gateway-session.md（v0.2 现行版）`;旧文档的 create_expert_session/submit_turn 为方案文档原始命名,映射关系见下):

| gateway 契约 v0.2 | 语义 | 消费方式 | 旧命名(方案 §2.4) |
|---|---|---|---|
| `POST /v1/gateway/session/create` `{templateId, skills, idempotencyKey}` | 初始化 Sandbox + 安装 Skill,取 sessionId/sandboxId | 普通请求-响应;**idempotencyKey 幂等**(bridge 重启找回 Session);template_id 来自 S3 bridge 扩展段(§5.1) | create_expert_session(owner_id, template_id) |
| `POST /v1/gateway/session/chat` `{sessionId, sandboxId, turnId, agentMd, history, userMessage}` | 提交 Turn(核心) | **SSE 流式响应**,事件流消费;**turnId 幂等**(= Matrix event_id,防提交重试重复执行) | submit_turn(session_id, input, file_ids, required_skill_ids) |
| `POST /v1/gateway/session/destroy` `{sessionId}` | team 解散/SIGTERM 清理,幂等 | 普通请求-响应,失败仅告警 | 关闭 Session(端点待服务方确认) |

chat 请求三字段(gateway 纯透传,不解析/不拼接/不持久化,契约 §5.1):

- `agentMd`:系统指令层 = 协作上下文 + AGENTS.md + SOUL.md,**每轮全量重拼**(变更下轮生效);
- `history`:flat history 数组(§3.4),bridge 自维护,全量重传;
- `userMessage`:当前触发消息(带 sender 前缀)。
- 旧 `input.system`/能力 #4 的降级路径(text 首部拼接)**随契约冻结而删除**——agentMd 字段即 system 通道。

> **chat 是核心,也是本章要解决的最大问题**:它**不是一次性返回结果,而是流式地吐一堆事件**(message/done/error,契约 §5.3)。bridge 必须把这些碎片拼成完整文本,再交给 §三.6 渲染发给 Element Web。**"拼接碎片"就是 Dialect 层的主要工作。**

### 4.2 SDK 分层(四层):换服务不换核心

```
┌────────────────────────────────────────────────────┐
│ Adapter 层:cimicode / opencode / generic-sse / 插件 │  ← 对外可插拔(充电头)
│   (每个 adapter ≈ 一组端点默认值 + 一个 dialect 引用) │
├────────────────────────────────────────────────────┤
│ Dialect 层:SSE 事件 → 内部 RuntimeEvent 翻译        │  ← 内容格式可插拔(翻译官)
├────────────────────────────────────────────────────┤
│ 引擎层 HttpSseRuntime:端点模板渲染 + SSE 解析 +      │  ← 全 adapter 共用(USB-C 口)
│   鉴权注入 + 超时/重试/错误映射/取消                  │
├────────────────────────────────────────────────────┤
│ SPI(base.py):RuntimeAdapter / EventDialect /      │  ← 契约(插头标准)
│   AuthProvider / TurnInput / RuntimeEvent / 能力声明  │
└────────────────────────────────────────────────────┘
```

**Transport(引擎)与 Dialect(事件格式)分离是本设计的核心**:换一个 runtime = 换一个 adapter(端点+方言),引擎与 bridge 其余模块零改动。

### 4.3 内部统一事件模型(RuntimeEvent):所有服务的"普通话"

**为什么非要抽象这一层?** 因为 bridge 的核心业务逻辑(渲染、发送、flat history)**只认普通话,不认方言**:

```
cimicode 说 message.part.delta  ──┐
opencode 说 output.delta        ──┼─→ 都翻译成统一的 text_delta ──▶ bridge 渲染层统一处理
将来的服务说 xxx                 ──┘
```

```python
class RuntimeEvent:
    seq: int | None        # event_seq(服务有则透传)
    kind: turn_started          # 开始干活了
        | text_delta            # 吐了一段文本(增量)
        | text_done             # 一段文本说完了
        | tool_started          # 开始调用工具
        | tool_finished         # 工具调用完了
        | artifact_published    # 发布了产物(文件等)
        | turn_completed        # 整个任务完成(带完整文本)
        | turn_interrupted      # 任务中断(断流/超时)
        | runtime_error         # 出错了
    text: str              # delta / 完成文本
    data: dict             # 方言原始事件(诊断与扩展透传)
```

规则:**未识别的方言事件不丢弃**,包进 `data` 透传(debug 日志),漏翻译不丢信息。

**cimicode 方言事件映射(内置,任务 B9 的事件面)**:

| cimicode SSE 事件 | → RuntimeEvent | 语义 |
|---|---|---|
| `message.part.delta` | text_delta | 增量追加到对应 part |
| `message.part.updated` | text_delta(标记 full) | 该 part 内容**全量替换** |
| `message.updated` | text_delta(消息级全量) | 消息文本整体更新 |
| `done` | turn_completed(取聚合文本) | Turn 正常结束 |
| `session.error` | runtime_error(错误码透传) | 服务侧错误 |

### 4.4 解析过程演示(重点:part 缓冲聚合是怎么做的)

> **这是 Dialect 层最核心、也最容易绕晕的逻辑。** 用真实数据走一遍:

**cimicode 返回的 SSE 流(原始)**:

```
event: message.part.delta     ← 第 1 条:part p1 增量 "你好"
data: {"part_id": "p1", "delta": "你好"}

event: message.part.delta     ← 第 2 条:part p1 再追加 ",我是"
data: {"part_id": "p1", "delta": ",我是"}

event: message.part.updated   ← 第 3 条:part p2 全量替换为 "我可以帮你"
data: {"part_id": "p2", "text": "我可以帮你"}

event: message.part.delta     ← 第 4 条:part p2 再追加 "写代码"
data: {"part_id": "p2", "delta": "写代码"}

event: done                   ← 结束
data: {}
```

**dialect 的 part 缓冲(逐步)**:

```
收到第 1 条 → buffer = { "p1": "你好" }
收到第 2 条 → buffer = { "p1": "你好,我是" }        ← delta = 追加
收到第 3 条 → buffer = { "p1": "你好,我是", "p2": "我可以帮你" }   ← updated = 全量替换
收到第 4 条 → buffer = { "p1": "你好,我是", "p2": "我可以帮你写代码" }  ← delta = 追加
收到 done  → 按 part_id 顺序拼接:p1 + "\n" + p2
             = "你好,我是\n我可以帮你写代码"   ← 这是完整响应文本,交给渲染层
```

**对应的 RuntimeEvent 输出流**:

```
RuntimeEvent(kind=text_delta,       text="你好")
RuntimeEvent(kind=text_delta,       text="你好,我是")
RuntimeEvent(kind=text_delta,       text="我可以帮你")          ← updated 也是 text_delta
RuntimeEvent(kind=text_delta,       text="我可以帮你写代码")
RuntimeEvent(kind=turn_completed,   text="你好,我是\n我可以帮你写代码")  ← done
```

**关键规则**:
- `delta`(增量)→ **追加**到该 part 已有文本末尾;
- `updated`(全量)→ **替换**该 part 的整段文本(不是追加);
- `done` → 按 part 顺序拼接出完整文本,发 `turn_completed`;
- 这就是 **part 缓冲聚合**——也是 cimicode 方言必须写成**内置 Python 而非声明式映射**的原因(generic-sse 只做单事件一对一映射,§4.5 通道②,做不了这种跨事件的"先攒后拼"逻辑)。
- **event_seq 透传**:服务带 event_seq 时透传进 RuntimeEvent.seq,并做单调性检查(乱序仅告警、不丢弃)。
- 映射本身表驱动,事件格式改版时改映射表、不动引擎。

### 4.5 可插拔通道(满足"对接任意 HTTP runtime")

| 通道 | 适用 | 接入成本 |
|---|---|---|
| ① 内置 adapter(`cimicode` / `opencode`) | 两个已知目标 | 零配置(默认值) |
| ② `generic-sse` 声明式映射 | 任意新 HTTP+SSE 服务 | **零代码**,纯配置:端点模板(`{session_id}`/`{owner_id}` 占位)、请求体模板、session_id 提取(JSONPath)、事件映射(match 字段相等 + 取值 JSONPath) |
| ③ Python 插件 | 复杂语义(跨事件聚合/状态机,如 cimicode 的 part 缓冲) | 配置 `dialect: import:包.模块:类`,或打包时 entry_points 注册 |

> 注意:通道② generic-sse 适合"一对一"的简单事件;cimicode 这种需要跨事件攒缓冲的,必须用通道①内置 Python 或通道③插件。

generic 声明式映射示例(对接一个假设形状的服务):

```yaml
runtime:
  adapter: generic-sse
  base_url: http://any-runtime:8080
  auth: { type: header, header: X-API-Key, token_env: RUNTIME_AUTH_TOKEN }
  session:
    create: { method: POST, path: /v1/sessions, body: { owner: "{owner_id}" } }
    id_pointer: $.session_id
  turn:
    submit: { method: POST, path: /v1/sessions/{session_id}/turns,
              body: { input: { text: "{text}", system: "{system}" } } }
  events:
    mapping:
      text_delta:     { match: { type: "message.part.updated" }, text_pointer: $.part.delta }
      turn_completed: { match: { type: "turn.completed" }, text_pointer: $.output.text }
```

### 4.6 引擎行为规格(HttpSseRuntime,所有 adapter 共享)

> 引擎不管服务是谁,只管"**怎么把请求发出去、怎么把流接住**"。这是通信的技术底线:

| 关注点 | 规格 |
|---|---|
| HTTP 客户端 | 单个 `httpx.AsyncClient` 复用;超时矩阵:connect 10s / write 30s / **read = turn.timeout**(SSE 长流读超时即 Turn 超时) |
| SSE 解析 | `httpx-sse` EventSourceParser(data/event/注释行/跨 chunk 缓冲);`event` 字段与 `data` JSON 一并交 dialect |
| 翻译失败 | dialect 异常 → 包成 `runtime_error` 事件,**流不断** |
| 断流判定 | EOF / 传输错误 / read 超时,且未收到 `turn_completed` → 迭代器末尾 yield 一条 `turn_interrupted(reason)` 后正常结束 |
| 取消 | 消费方取消(队列关闭/SIGTERM)→ 关闭 HTTP 流静默结束,在途 Turn 由上层标记 interrupted |
| 重试矩阵(任务 B8) | `create_expert_session` / health / close:幂等,429/5xx 指数退避重试;**`submit_turn` 分两段**——提交请求本身失败(连接错误/429/5xx,**尚未收到任何 SSE 事件**)→ 有限重试(默认 3 次,指数退避);**已开始消费事件流后断开 → 一律不自动重试**(方案 §12:不重试、不回滚副作用,重新 @ 触发新 Turn) |
| 错误映射 | 401/403 → AuthError(告警 fail fast);404 session → SessionExpired(触发 Session 重建后单次重试);429 → 退避(仅幂等操作);5xx/网络 → interrupted 语义 |
| 鉴权 | cimicode 场景**固定 none**(评审修正 2026-08-28:gateway/Higress 不鉴权);AuthProvider(bearer/basic/header)扩展点保留给其他 runtime,token 从 env 读,不落配置明文 |
| 可测性 | 引擎只吃构造参数(不吃 bridge 配置对象);bridge 配置层负责把 YAML/env 渲染成参数 → SDK 可脱离配置系统单测(ASGI transport 直连 mock) |

> **最重要的规则**:submit_turn 一旦开始收到事件流,断流就**绝不自动重试**——因为重试会导致**重复执行副作用**(重复扣费、重复建文件)。断了就发"处理中断,请重新 @我"。

### 4.7 能力声明与降级:服务能力不够也能跑

每个 adapter 声明能力,bridge 按能力自动降级(不被服务契约阻塞):

| 服务能力(方案 §3.2) | 不支持时 |
|---|---|
| #4 system 通道(能力 #4) | **已随契约 v0.2 冻结**:agentMd 字段即 system 通道,无降级路径 |
| #5 Session 关闭 API | close_session no-op,SIGTERM 只取消在途 Turn |
| #8 interrupted 主动通知 | 引擎断流 + read 超时检测(§4.6)兜底判定 |
| #14 Session 日重置 | 默认方案 A 不重置,开关见 §7.2 |

> **大白话**:服务说"我不会 X",bridge 就自动换一条路做 X,而不是崩溃。比如服务不支持 `system` 字段,就把协作上下文拼到消息开头——效果一样,只是格式朴素点。

### 4.8 SSE 库与 SDK 形态选型结论

- **SSE 库**:httpx + httpx-sse(与 Matrix 侧同生态单 HTTP 栈;ASGI 可测;断流=迭代器自然结束语义透明)。否决 aiohttp 系(内建自动重连对 Turn 是危险的——静默重连可能重复消费事件)与裸手写行解析(多行 data/注释行/跨 chunk 缓冲均已解决)。
- **SDK 形态**:桥内分层模块(`cimicode_bridge/runtime/` 包),不做独立 SDK 包(唯一消费方是 bridge,YAGNI;SPI 边界即未来抽包边界);不用 OpenAPI 生成客户端作主体(与"任意 runtime 可插拔"冲突,SSE 建模弱)——若服务方日后提供 OpenAPI,可在 cimicode adapter 内部用作请求层实现细节。

### 4.9 扩展点总表

统一扩展机制:内置 / 配置 `import:包.模块:类` / entry_points 组注册(`cimicode_bridge.{adapters,dialects,stores,auth,emitter_strategies}`)。**依赖方向恒定:core 只 import SPI,新扩展零改动 core。**

| # | 扩展点 | 内置实现 | 扩展场景 |
|---|---|---|---|
| 1 | RuntimeAdapter | cimicode / opencode / generic-sse | 新 runtime |
| 2 | EventDialect | cimicode / opencode / generic 映射 | 事件格式改版 |
| 3 | AuthProvider | none / bearer / basic / header | 自定义签名/STS |
| 4 | StateStore | redis / memory / file | etcd 等 |
| 5 | SystemPrompt parts | coordination / instructions | 新上下文源 |
| 6 | Emitter 流式策略 | complete / chunked / throttled | 渲染分块 |
| 7 | (预留)指令源 | s3 / file / inline | AGENTS.md/SOUL.md 拉取渠道(§五.1) |
| 8 | (预留)E2EE | 无 | 仅动 §三 |
| 9 | (预留)artifact 通道 | 引用行输出 | 跨 Session 文件协作定稿 |
| 10 | **本地只读 HTTP(评审修正:必做,非预留)** | /status | **OpenClaw Leader 查 bridge worker 存活**(§七.5,worker 无心跳) |

---

## 五、核心设计三:配置分发与启动流程

> 回答"配置文件怎么来":**沿用现有 worker 模式——调谐写 S3,启动拉 S3**。env 只承载平台级与敏感信息。

### 5.1 配置分发(调谐 → S3 → bridge)

**S3 路径**(与现有 worker 完全一致):`${AGENTTEAMS_STORAGE_PREFIX}/agents/${AGENTTEAMS_WORKER_NAME}/`

| 文件 | 谁写 | bridge 用途 | 对应现有 worker |
|---|---|---|---|
| `openclaw.json`(**复用,评审修正**) | 调谐写入(已存在) | **bridge 行为配置的主载体**:读 `channels.matrix`(homeserver/accessToken/dm/groupPolicy/groupAllowFrom/historyLimit)+ 新增 `bridge` 扩展段(adapter/端点/dialect/流式/超时/mention 三层开关 + **runtime.base_url=cimicode gateway 地址 + runtime.template_id**,评审修正 2026-08-28:二者由调谐写入,不走 env)。**不新建 bridge.yaml**——避免双份配置漂移,调谐侧只需在现有生成器加一段而非新增生成器 | 完全沿用(原 bridge.yaml 方案取消) |
| `credentials/matrix/password` | 调谐写入(已存在) | 仍拉取但**不消费**(登录链已裁剪,§3.1;为恢复预留) | 沿用 |
| `AGENTS.md` | 调谐三层合并+协作上下文注入(已存在) | 软指令,**启动拉取缓存,每个 Turn 都随 agentMd 携带**(§七.3;refresh_interval 可选开启每 Turn 重拉) | 完全沿用 |
| `SOUL.md` | 调谐 seed-only(已存在) | 人格,并入 agentMd(可配关) | 完全沿用 |
| ~~HEARTBEAT.md~~ | — | **不需要**(Leader 是 OpenClaw,worker 无心跳) | 裁剪 |
| ~~config/mcporter.json~~ | — | 不需要(MCP 走服务侧 Nacos) | 裁剪 |
| ~~skills/~~ | — | 不需要(Skill Catalog) | 裁剪 |

**拉取实现**:bridge 进程内用 S3 SDK(minio)按前缀列出+下载上述文件到本地工作目录(`/root/agentteams-fs/agents/<name>/`,沿用现有 HOME/workspace 布局),凭证用 env `AGENTTEAMS_FS_*`(静态 AK/SK 优先,与现有 worker Step 1 一致)。**凭证获取抽象对齐 oss-credentials.sh 三模式**:静态 env / MC_HOST 解析 / STS(controller 下发)——明确区分,避免"provider=oss 但静态凭证"时误走 STS(现有 worker 已踩坑,af5ce7be 修复,桥内 minio SDK 应直接吃 endpoint/AK/SK,不经过 mc CLI)。

**竞态与重试语义**(对齐现有 worker Step 2):pod 启动时调谐可能尚未写完配置 → 拉取失败重试 6 次×5s;`openclaw.json` 缺失走默认值+env(与现有 worker 对 openclaw.json 的强依赖相比更宽松;base_url/template_id 缺失时可用 env `BRIDGE_RUNTIME_URL` 覆盖兜底);`AGENTTEAMS_WORKER_MATRIX_TOKEN` 缺失为致命项(凭证唯一来源,Phase 1 失败重试,§3.1)。

**配置分层**(低→高):镜像内置默认 → S3 `openclaw.json`(含 bridge 扩展段)→ env 覆盖(平台级与敏感项)。`${ENV}` 占位可在 openclaw.json 内引用 env。

### 5.2 env 契约(bridge 消费,平台注入)

| env | 用途 | 现状 |
|---|---|---|
| `AGENTTEAMS_WORKER_NAME` / `AGENTTEAMS_WORKER_CR_NAME` | agent 名 / report-ready | 已有 |
| `AGENTTEAMS_FS_ACCESS_KEY/SECRET_KEY/ENDPOINT/BUCKET` + `AGENTTEAMS_STORAGE_PREFIX` | 拉 S3 配置 | 已有 |
| `AGENTTEAMS_MATRIX_URL` / `AGENTTEAMS_MATRIX_DOMAIN` | homeserver / domain 补全 | 已有 |
| `AGENTTEAMS_WORKER_MATRIX_TOKEN` | **Matrix 凭证唯一来源**(评审修正 2026-08-28:无登录链) | 已有 |
| `AGENTTEAMS_WORKER_ROOM_ID` | 主 room(history 重建) | 已有 |
| `AGENTTEAMS_CONTROLLER_URL` + `AGENTTEAMS_AUTH_TOKEN(_FILE)` | token 刷新 + report-ready | 已有 |
| `COORDINATION_ROLE/LEADER/TEAM/ROOM/ADMIN/WORKERS` | 协作上下文(方案 §4.1.9;**bridge 只有 worker/standalone 角色,无 HEARTBEAT_EVERY**) | 方案文档定义,operator 待实现 |
| ~~`BRIDGE_RUNTIME_AUTH_TOKEN`~~ | ~~cimicode 鉴权~~ | **取消**(2026-08-28:gateway 不鉴权);baseurl+templateId 改由 S3 bridge 扩展段下发(T3),`BRIDGE_RUNTIME_URL` 降级为可选 env 覆盖 |
| `BRIDGE_REDIS_URL` | StateStore redis 后端 | **新增** |

### 5.3 启动时序

```
Phase 0  拉配置:S3 → 本地(重试等调谐;§5.1)
Phase 1  Matrix 凭证(§3.1:env token whoami 校验,失败退避重试)+ catch-up/full_state sync 就绪
Phase 2  连 runtime:health(无限退避重试,不 crash)→ ensure_session
         (StateStore 有 session_id 则复用,miss 则 create_expert_session)
Phase 3  report-ready(命令可配,默认 agt worker report-ready --name ...)
Phase 4  /sync 监听主循环(§3.2)
```

- 每 Phase 就绪翻转本地探针:`/healthz`(进程活)/`/readyz`(Phase 4 达成),端口可配(默认 8081),供 K8s probe;**`/status`(只读 JSON:worker 名/角色/Phase/最近 Turn 时间/Matrix 连接态)供 OpenClaw Leader 经 §七.5 探活**。

### 5.4 配置变更(原"心跳与配置变更"节,心跳已删)

- 协作上下文变更(team 成员增删):现状靠调谐重写 AGENTS.md + file-sync 下发;bridge 模式下 COORDINATION_* env 变更需重启 pod(operator 侧语义,预留接口),AGENTS.md 软指令可经 S3 重新拉取生效——**评审已定(2026-08-28):默认启动拉取缓存、每个 Turn 附带缓存内容;`config.refresh_interval` >0 时每 Turn 前重拉 S3**(§7.3)。

---

## 六、平台侧调谐需求(给调谐团队的验收清单)

> **本章是给"调谐/controller 团队"的需求清单**(方案文档 §4.9/§4.11 的整合 + 本 Bridge-Spec 对平台的全部要求)。调谐是**别人做**,bridge 侧只消费其结果(env + S3 + 镜像)。按此清单验收,逐项打勾。
>
> 基准代码:dev-v1.2.2(`agentteams-controller/`)。所有"沿用"项已存在,只列**必须新做/必改**的。

#### 6.1 配置生成(ReconcileMemberConfig 改造)

| # | 需求 | 现状(dev-v1.2.2) | 调谐要做的 |
|---|---|---|---|
| T1 | **openclaw.json 增加 `bridge` 扩展段** | `deployer.go` 现生成 openclaw.json(整段 openclaw 结构) | 在现有生成器里**加一段** `bridge`(adapter/端点/dialect/流式/超时/mention 三层开关 + **runtime.base_url + runtime.template_id**,T3 并入此处),**不新建独立文件**(评审定,避免双份配置漂移) |
| T2 | **注入 COORDINATION_* env**(协作上下文) | `worker_env.go::applyClusterDefaults` 现注入 AGENTTEAMS_FS_*/MATRIX_*/CONTROLLER_URL 等 | 新增注入:`COORDINATION_ROLE/LEADER/TEAM/ROOM/ADMIN/WORKERS`(值来自 Team CR 调谐上下文;bridge 只有 worker/standalone 角色,**无 HEARTBEAT_EVERY**) |
| T3 | **bridge 扩展段写入 cimicode gateway baseurl + templateId**(评审修正 2026-08-28:并入 T1,不再新增 env) | 无 | openclaw.json `bridge.runtime.base_url`(cimicode gateway 地址)+ `bridge.runtime.template_id`(worker 运行模板标识,值由平台/服务方定义);gateway 不鉴权,无 token 项 |
| T4 | **注入 BRIDGE_REDIS_URL** | 无 | 新增注入:StateStore redis 后端(可选,无则 bridge 用 memory 兜底) |
| T5 | **AGENTS.md/SOUL.md 继续推 S3** | `deployer.go` 已做(三层合并+seed-only) | **沿用**(bridge 从 S3 拉,并入 agentMd,§7.3) |
| T6 | **credentials/matrix/password 继续推 S3** | `deployer.go:450` 已做 | **沿用**(bridge 仍拉取但不消费——登录链已裁剪 §3.1);另需保障注入的 `AGENTTEAMS_WORKER_MATRIX_TOKEN` 为有效 fresh token(bridge 唯一凭证来源) |
| T7 | ~~mcporter/skills~~ | `deployer.go` 现推 mcporter+skills | **bridge 不需要**(MCP 走服务侧 Nacos),可沿用(无害)或按 runtime 跳过 |

#### 6.2 容器与镜像(ReconcileMemberContainer 改造)

| # | 需求 | 现状 | 调谐要做的 |
|---|---|---|---|
| T8 | **新 runtime 枚举 `cimicode-bridge`** | CRD `workers.agentteams.io.yaml` runtime 枚举无此项(dev-v1.2.2 无 cimicode 接线) | CRD 枚举 + `RuntimeCimicodeBridge` 常量 + 镜像名 `agentteams-cimicode-bridge` |
| T9 | **镜像 env 注入** | `worker_env.go` / k8s backend 按 runtime 分支注入 | 按 runtime=cimicode-bridge 注入:§5.2 env 契约全集 + COORDINATION_*(T2)+ BRIDGE_*(T3/T4) |
| T10 | **镜像选择** | 按 runtime 选 image | runtime=cimicode-bridge → 用 `agentteams-cimicode-bridge:${VERSION}` |
| T11 | **存活探针** | 现 worker 用 liveness/readiness | liveness `/healthz`、readiness `/readyz`(Phase 4 达成)、**`/status` 供 Leader 探活**(§7.5) |
| T12 | **Termination grace** | 默认 | `terminationGracePeriodSeconds: 30`(> bridge §7.1 grace 20s),SIGTERM 清理 Session |

#### 6.3 就绪与生命周期(summarizeBackendReadiness / handleDelete 改造)

| # | 需求 | 现状 | 调谐要做的 |
|---|---|---|---|
| T13 | **ready 语义变更** | 现按"runtime 内部健康"判 ready | bridge ready = **Matrix 连接 + runtime health + ensure_session 成功**(§7.5);调谐的 ready 判定要兼容 bridge 的 report-ready 时机 |
| T14 | **report-ready 命令沿用** | `agt worker report-ready --name …` | **沿用**(命令不变,bridge 在 Phase 3 调用) |
| T15 | **删除时关闭 Session** | handleDelete 删 pod + 清 Matrix bot/room | 新增:调 cimicode 无状态服务 **close_session**(能力 #5;不支持则 bridge SIGTERM 自清理) |
| T16 | **COORDINATION_* 变更语义** | 现状靠重写 AGENTS.md + file-sync 下发 | bridge 模式:**env 变更需重启 pod**(operator 语义);AGENTS.md 变更经 S3 重拉(§5.4) |

#### 6.4 双写防护与协作上下文(重要约束)

| # | 需求 | 现状 | 调谐要做的 |
|---|---|---|---|
| T17 | **停止向 AGENTS.md 注入 Coordination 块** | `deployer.go:478` InjectCoordinationContext 现把 team 上下文注入 AGENTS.md | 注入 COORDINATION_* env 后,**调谐应停止 InjectCoordinationContext**(否则 bridge 的 §7.3 env 渲染 + AGENTS.md 注入块双写重复);迁移期未停前,bridge 侧 `system_prompt.parts` 去掉 coordination 段(§十一 #13) |
| T18 | **AGENTS.md 模板 cimicode 化** | 现有 AGENTS.md 模板含 OpenClaw 专属指令(hiclaw-sync/mcporter/mc mirror) | 为 cimicode-bridge runtime 生成 **cimicode 化 AGENTS.md**(方案 §4.12 映射:产物→publish_artifact、文件→自动同步、@mention/NO_REPLY 保留说明) |

#### 6.5 调谐团队验收标准(给他们的 DoD)

- [ ] T1:Worker CR 指定 runtime=cimicode-bridge 后,`openclaw.json`(含 bridge 扩展段 + base_url/template_id)写入 S3 `agents/<name>/`
- [ ] T2/T4:bridge pod env 含 COORDINATION_* / BRIDGE_REDIS_URL / **AGENTTEAMS_WORKER_MATRIX_TOKEN(必填)**(可 `kubectl exec env` 验证);T3:bridge 扩展段含 base_url + template_id(无 BRIDGE_RUNTIME_AUTH_TOKEN)
- [ ] T8:CRD 接受 runtime=cimicode-bridge,不报 unknown field
- [ ] T9-T10:bridge pod 用 `agentteams-cimicode-bridge` 镜像启动,env 与 §5.2 契约一致
- [ ] T13-T14:bridge 调 `agt worker report-ready` 后 Worker CR `status.phase=Running`(ready 语义对齐)
- [ ] T15:删 Worker CR 后,cimicode 无状态服务侧 Session 被关闭(或 bridge SIGTERM 自清理)
- [ ] T17:注入 COORDINATION_* 后,AGENTS.md 里**不再出现** Coordination 注入块(单通道)
- [ ] T18:cimicode-bridge worker 的 AGENTS.md 无 OpenClaw 专属命令(hiclaw-sync/mcporter/mc mirror)

> **调谐团队与 bridge 开发的依赖**:T1+T3(openclaw.json 扩展段,含 base_url/template_id)与 T2(env)是 bridge **联调的前置**——bridge 本地开发可先用 mock env 绕过,但真环境联调必须等调谐完成;T5/T6(沿用的 S3 推送)已在 dev-v1.2.2 存在,不阻塞。

---

## 七、运行期行为

### 7.1 Turn 编排与生命周期

- **per-Session 串行提交队列**:同 Session 后续 @mention 排队(max_pending 默认 8,溢出 buffer/reject 可配);服务侧 Session FIFO 是优势,但 bridge 不依赖对方保证。
- **Turn interrupted**:断流/超时/服务通知 → 发 Matrix 中断通知(默认"处理中断,请重新 @我",可配)→ **不自动重试**(方案 §12)→ 重新 @ 可再触发。
- **SIGTERM**:取消在途 Turn → close_session(能力支持且配置开启)→ grace(默认 20s)内退出;StateStore 无脏残留。pod 重启恢复 = Session 复用(StateStore/idempotencyKey)+ history 重建 + since(persist 开启时续传,默认 catch-up 全量)。

### 7.2 Session 管理

- 启动 ensure_session 幂等(`session:{agentId}` 映射,默认 TTL 30d);submit_turn 404 → 重建映射单次重试;日重置默认关闭(方案 A),`daily_reset/reset_at` 开关预留(方案 B)。
- owner_id 取值可配(matrix_id 默认 / agent_name / team_agent),方案 §5.1 坑点 2 待与平台确认。

### 7.3 协作上下文与指令注入(agentMd 的组装;2026-08-31 更名)

> **流向**:本节的 SystemPromptBuilder 负责**组装**;组装结果作为 chat 请求的 `agentMd` 字段(契约 v0.2 §5.2)传给 gateway,gateway 透传给 Sandbox 运行时作为 system 指令 CimiCode Replica 的 per-Turn 上下文(方案能力 #4)。**必须每次 Turn 都传**——CimiCode Replica 无状态、不存 agent 身份,协作信息只能随请求带过去。

SystemPromptBuilder 按序拼装(全部可配、可关):

1. **协作上下文**(必选):COORDINATION_* env 渲染为方案 §4.1.9 的 Markdown 块(coordinator/room/workers/协议)。**两分支模板(worker / standalone)= 现有 agentconfig/coordination.go worker(95-112)+ standalone(114-118)两模板的等价迁移**(worker 版含"不许直接 @Manager,所有沟通经 Team Leader";leader 模板已删——Leader 是 OpenClaw,不是 bridge);与 AGENTS.md 注入块的双写防护见 §十一 #13;
2. **AGENTS.md**(默认开):S3 启动拉取、内存缓存的软指令(行为协议/NO_REPLY 说明/"仅响应最后一条 @ 你的消息"软约束——LLM 引导层,硬过滤在 §3.3 兜底)——**每个 Turn 都随 agentMd 携带**(评审修正 2026-08-28;refresh_interval 开启时每 Turn 前重拉);
3. **SOUL.md**(默认开):人格。

这保持了现有 worker "AGENTS.md 三层合并"的指令语义——生成仍归调谐,bridge 只负责搬运进 system。降级路径已随契约 v0.2 冻结删除(agentMd 即 system 通道)。

### 7.4 状态存储(StateStore)

SPI:get/set/delete + TTL;后端 redis(生产,BRIDGE_REDIS_URL)/ memory(兜底)/ file(调试)。keys:`session:{agentId}`(TTL 30d,**唯一必须持久化的状态**——丢了重启会新建 Session)+ `matrix:since:{agentId}`(评审修正:默认 `matrix.since.persist=true` 写入,Redis 为生产标配,重启续传不重放;false=仅内存)。**Matrix 凭证不进 StateStore**(任务 B2:token/device 仅内存)。Redis 不可用 → memory 降级 + 告警(重启 = 新 Session + catch-up,行为安全)。

### 7.5 就绪上报与存活探针

- report-ready 时机 = Phase 3(= Matrix 连接 + runtime health + ensure_session 全部成功);命令默认平台 CLI `agt worker report-ready --name ${AGENTTEAMS_WORKER_CR_NAME:-${AGENTTEAMS_WORKER_NAME}}`,完全对齐现有 worker。
- ready 语义变更(Matrix+服务连通,而非 runtime 内部健康)对 controller 的联动是平台侧改造点,已在 §十一假设表登记。
- **`/status` 本地只读接口(评审修正:扩展点 #10 从"预留"升级为必做)**:bridge worker 无心跳(拓扑前提),OpenClaw Leader 靠 `/status` 区分"worker 空闲"与"worker 死亡"——Leader 的 worker 存活管理(wake/sleep)依赖它。响应 JSON 含:worker 名 / COORDINATION_ROLE / 当前 Phase / 最近 Turn 完成时间 / Matrix 连接态 / runtime health。K8s 内通过 `kubectl exec` 或 localhost 直连;接口只读、无鉴权(仅集群内可达,§八.1 无外部暴露)。

### 7.6 可观测性

- 结构化 JSON 日志(logging),关键事件必打:Phase 迁移、登录/token 刷新、sync 恢复、过滤判定(丢弃原因)、Turn 提交/完成/中断(带 turn_id/session_id)、Session 创建/重建、SIGTERM 清理、/status 探活请求。
- 运行指标先走日志(不做 metrics 端点,预留扩展点);`AGENTTEAMS_MATRIX_DEBUG=1` 开 Matrix 细粒度 trace(对齐现有 worker 的调试开关习惯)。

---

## 八、部署与代码结构

### 8.1 镜像与构建(概要)

- 多阶段:`agentteams-controller` 阶段取 `agt` CLI + final `python:3.12-slim`(同 registry 模式);**单 venv** `/opt/venv/bridge`;分层缓存(pyproject 重层 / src overlay 轻层)。
- **不进镜像**:Node/npm、mc、libolm、build 工具链、第二 venv——bridge 是纯 Python 常驻 HTTP 客户端,预估镜像 ~150-200MB(现有重 runtime 1.5-2GB+)。
- Makefile:`build-cimicode-bridge` / `push-cimicode-bridge`,镜像名 `agentteams-cimicode-bridge`,照现有 runtime target 模式。
- entrypoint 极简:`exec cimicode-bridge run`(配置拉取与生命周期全在进程内;exec 保证 PID1=python,SIGTERM 直达)。
- **K8s Deployment 参考 manifest(随仓库交付,任务 B1)**:单副本 Deployment(bridge 无状态,不需要 StatefulSet);env = §5.2 契约全集;probes:liveness `/healthz`、readiness `/readyz`(§5.3,initialDelay 10s)、**`/status`(§七.5 存活探针,供 OpenClaw Leader 查询——经 K8s Service DNS 或 kubectl exec,集群内可达)**;`terminationGracePeriodSeconds: 30`(> §7.1 grace 20s);resources 建议 requests 100m/128Mi、limits 1 CPU/512Mi;**无持久卷、无挂载**(全部状态在 S3/StateStore/服务侧)。operator 接管前手工部署用这份 YAML;接管后由 operator 生成等价 spec——YAML 同时是给 operator 侧的接口样例。

### 8.2 模块与目录

```
cimicode-bridge/
  Dockerfile / pyproject.toml / Makefile target
  scripts/bridge-entrypoint.sh     # exec cimicode-bridge run(B1)
  config/bridge.example.yaml       # 全量配置样例 = 配置总表(§十)的可运行版;含 openclaw.json bridge 扩展段示例(B1)
  deploy/deployment.yaml           # K8s 参考 manifest(B1,§8.1)
  src/cimicode_bridge/
    main.py           # CLI:run / close-session / print-config(B1)
    app.py            # Phase 0-4 生命周期编排 + 结构化日志装配(B13)
    bootstrap.py      # S3 配置拉取,重试等调谐(§5.1)(B1/B13)
    config.py         # 分层配置加载:内置默认 < S3 openclaw.json(bridge 扩展段)< env(B1)
    events.py         # TurnInput / RuntimeEvent 统一事件模型(§4.3)(B9)
    matrix/gateway.py # env token + whoami + 401 刷新链 + 凭证内存态 + sync 循环(§3.1-3.2)(B2/B3)
                      # ★ 移植 agentteams_matrix/channel.py(4605 行,当前 Manager 在用),不重写(§1.3)
    matrix/mention.py # @mention 三级解析 + 容错 + 发侧三层 mention(§3.3/§3.6)(B4/B11)
                      # ★ 移植 agentteams_matrix/channel.py 的 mention 逻辑(§1.3)
    core/filter.py    # 过滤链 + RoleResolver(COORDINATION_* 消费)(B4/B7)
    core/history.py   # flat history HistoryStore + 去重/滑窗 + 重启重建(§3.4)(B5/B6)
    core/turn.py      # Turn 编排 + 串行队列 + 两段重试(§4.6/§7.1)(B8)
    core/session.py   # ensure_session 幂等 / 404 重建 / close(§7.2)(B8)
    core/render.py    # 渲染管线:part 聚合后 Markdown→formatted_body 白名单消毒
                      # + thinking/tool/component 策略(§3.6)(B10)
    core/emitter.py   # 发送 / NO_REPLY / 三层 mention / 流式三策略(§3.6)(B10/B11)
    core/system_prompt.py  # 协作上下文 + AGENTS/SOUL 拼装(§7.3)(B7)
    probes.py         # /healthz /readyz + /status 存活探针(§5.3/§7.5)(B13)
    runtime/base.py   # SPI:Adapter / Dialect / AuthProvider / 能力声明(§4.2)
    runtime/client.py # HttpSseRuntime 引擎(§4.6)(B8)
    runtime/adapters/{cimicode,opencode,generic}.py   # 端点默认值 + dialect 引用(B8)
    runtime/dialects/{cimicode,opencode,generic}.py   # 事件翻译;cimicode 含
                                                      # part 缓冲聚合(§4.3)(B9)
    store/{base,redis,memory,file}.py
  tests/
    unit/             # F/M/H/E/X/L 组用例(B14)
    contract/         # A 组 + T/S 组,mock 断言请求体(B14)
    mock_runtime/     # ASGI mock runtime:SSE 剧本回放 + 故障注入 + 请求记录(§9.1)
    scenarios/        # 剧本 YAML:done / interrupted / error / timeout / part 流(B9/B14)
    integration/      # compose:Synapse + mock runtime + redis;E2E-1~6 剧本(B14)
```

> 删除:`core/heartbeat.py`(B12 取消,Leader 是 OpenClaw 非 bridge)。<br>
> 复用:`matrix/gateway.py`、`matrix/mention.py` 从 `plugins/agentteams-matrix-channel/agentteams_matrix/channel.py` 移植(剥 BaseChannel 依赖,保留协议逻辑),不重写。

依赖方向两条红线:
1. core/matrix/store 只 import SPI(runtime/base.py),不 import 具体 adapter——"换 runtime 不动核心"在结构上成立;
2. **render/emitter 只依赖 RuntimeEvent 模型(events.py),不接触任何方言原始事件**——B10 的格式转换对 runtime 无感,换 runtime 不重写渲染。

---

## 九、测试验证方案

### 9.1 四层策略

| 层 | 被测 | 环境 | 跑点 |
|---|---|---|---|
| 单元 | 过滤链/@mention 容错/flat history 组装/NO_REPLY/dialect 翻译/配置分层 | 无网络 | 每 PR |
| 契约 | runtime adapter 全链路(端点/鉴权/SSE/降级) | 进程内 mock runtime(ASGI,SSE 剧本回放+故障注入+请求记录断言) | 每 PR |
| 集成 | Matrix + 全模块 + 多 bridge 协作 | compose:真实 Synapse + mock runtime + redis | 每晚/发版 |
| 通用性验收 | generic-sse 零代码接入"第三形状"服务;资源冒烟 | 同上 | 里程碑 |

### 9.2 用例矩阵(43 条,分组;B 组 heartbeat 已随 B12 取消)

- **F 组 过滤**:不@不触发进 buffer / 白名单通过触发 / worker 拒 worker(F8 端到端:peer-mentions 阻断,Leader=OpenClaw 收侧)/ **worker 汇报 OpenClaw Leader 触发**(跨 runtime,三层 mention 验证)/ 自@忽略 / unknown 拒绝 / requireMention=false 开关
- **M 组 mention 解析**:标准 / 无 domain 补全 / 尾空格 / @后空格 / 畸形不触发
- **H 组 flat history**(2026-08-31 修订):FIFO≤200 滑窗 / **每条消息只出现一次(无嵌套重复)** / chat 请求断言 history 为单层平铺且不含本轮 userMessage / 拒绝不进 history / event_id 去重(重放/重建不重复) / 中断不落 history(record_interrupted=false)/ 重启重建≤N / 媒体占位
- **T 组 Turn**:chat 四元组 turnId/agentMd(协作上下文)/history snapshot/userMessage(mock 断言请求体)/ 同 Session 串行排队 / 溢出策略 / 断流→interrupted 通知+不自动重试 / 超时同 / 服务 interrupted 事件同
- **S 组 Session**:首建幂等复用 / 404 重建 / 日重置开关 / close 无能力 no-op / SIGTERM 清理
- **E 组 发送与格式转换(B10/B11)**:NO_REPLY trim 容忍 / 静默仍清 buffer / complete|chunked|throttled 三策略 / **发侧三层 mention(body + matrix.to + m.mentions.user_ids 同写,mock 断言 m.mentions 字段)**+artifact 引用行 / **Markdown→formatted_body 白名单消毒(script/on*/javascript: 全滤)** / thinking hide|quote、tools compact 单行 / `markdown_html=false` 退化纯文本仍可发 / throttled 每窗口独立转换
- **X 组 Matrix**:断线重连 since 续传不重复触发 / since 丢失 catch-up 不回放 / env token whoami 校验(凭证唯一来源) / 401→controller 刷新链 / 刷新失败重试+告警不退出 / 加密事件降级
- **L 组 生命周期**:S3 配置未就绪重试 6×5s / openclaw.json 缺失可跑 / runtime 未就绪无限退避不 report-ready / 探针翻转(/healthz /readyz **/status**) / SIGTERM grace / 配置非法报错指明键名
- **A 组 adapter**:cimicode dialect 契约样例 / opencode dialect / generic 映射提取 / 未识别事件透传 / system 降级 text_prefix(mock 断言)/ **A6 通用性:纯配置接第三形状服务零 Python**

### 9.3 端到端剧本(对应方案 §5)

E2E-1 单 agent 响应(方案 §5.2)/ E2E-2 双 agent OpenClaw Leader 中转+反例阻断(方案 §5.3)/ E2E-4 中断恢复 / E2E-5 重启恢复(Session 复用+history 重建+since 续传)/ **E2E-6(新增,评审修正):OpenClaw Leader ↔ bridge Worker 跨 runtime 协作——Leader(OpenClaw,结构化 m.mentions 收)发任务 → bridge(文本正则收)执行;bridge 汇报 → OpenClaw Leader 三层 mention 收;Leader 经 /status 探活 worker。这是生产主拓扑,必测**。

> E2E-3(原 heartbeat 剧本)随 B12 取消。

### 9.4 验收清单(DoD = 方案 §9.3 阶段 2 产出 + 方案 §7.5 前置验证 6)

全矩阵绿(CI 记录)/ A6 演示记录 / E2E-1、2、4、5、6 通过 / golden 对照通过 / 资源冒烟(idle RSS < 150MB 目标、镜像 < 200MB、单 Turn 峰值记录)。

---

## 十、配置总表(收敛版)

| 节.键 | 默认 | 说明 |
|---|---|---|
| `matrix.credentials.token_env` | `AGENTTEAMS_WORKER_MATRIX_TOKEN` | **凭证唯一来源**(评审修正 2026-08-28:登录链裁剪,mode/password_env 配置删除) |
| `matrix.token_refresh.max_retries` / `interval` | `10` / `5s` | 401 → controller matrix-token 刷新重试(§3.1 ②) |
| `matrix.since.persist` | `true` | since 持久化 StateStore 续传(评审修正:Redis 为生产标配,默认开启避免每次重启全量 catch-up);false=仅内存 |
| `matrix.homeserver_url` | `${AGENTTEAMS_MATRIX_URL}` | |
| `matrix.domain` | `${AGENTTEAMS_MATRIX_DOMAIN}` | |
| `matrix.sync.timeout` / `reconnect.max_backoff` | `30s` / `30s` | 重连退避 1s→2s→4s→…封顶 30s(B3) |
| `matrix.e2ee` | `off` | 首版仅 off(§3.5) |
| `matrix.set_display_name` | `false` | displayName 归 controller(§十一) |
| `filter.require_mention` | `true` | |
| `filter.group_allow_from.worker` | `[leader,admin,human]` | peer-mentions 语义(bridge 只有 worker 角色,Leader 是 OpenClaw;原 `.leader` 配置删除) |
| `filter.allow_unknown` | `false` | |
| `filter.mention.match_displayname` | `false` | |
| `history.max_entries` / `record_interrupted` / `persist` / `rebuild_limit` | `200` / `false` / `false` / `50` | flat history 滑窗 / 中断轮不落 history / StateStore 持久化开关 / 重启 timeline 回拉条数(§3.4) |
| `runtime.adapter` | `cimicode` | cimicode / opencode / generic-sse / 插件 |
| `runtime.base_url` | S3 bridge 扩展段(调谐写入) | cimicode gateway 地址;env `BRIDGE_RUNTIME_URL` 仅作覆盖兜底 |
| `runtime.template_id` | S3 bridge 扩展段(调谐写入) | worker 运行模板标识,create_expert_session 携带(§4.1;端点契约待 D1,假设 #16) |
| `runtime.auth.type` / `token_env` | `none` / — | cimicode gateway 不鉴权(评审修正 2026-08-28);bearer/basic/header 留给其他 runtime |
| ~~`runtime.system_channel`~~ | ~~auto~~ | **已随契约 v0.2 删除**(agentMd 即 system 通道,无降级) |
| `runtime.turn.timeout` / `queue.max_pending` / `queue.overflow` | `10m` / `8` / `buffer` | |
| `runtime.turn.submit_max_retries` | `3` | 仅提交阶段重试(未收到首事件前,B8) |
| `runtime.session.owner_id` / `daily_reset` / `reset_at` | `matrix_id` / `false` / `04:00` | |
| `events.dialect` | 随 adapter | cimicode / opencode / generic / import:… |
| `emitter.no_reply.mode/values` | `trim` / `["NO_REPLY"]` | |
| `emitter.streaming.mode` / `throttle` / `min_chunk_chars` | `complete` / `3s` / `40` | complete/chunked/throttled |
| `emitter.format.markdown_html` | `true` | Markdown→formatted_body 白名单消毒(B10);false=纯文本退化 |
| `emitter.format.thinking` / `.tools` / `.components` | `hide` / `compact` / `placeholder` | B10:thinking=hide/quote/show;tools=hide/compact/show |
| `emitter.mention.three_layer` | `true` | 发侧三层 mention(body + matrix.to + m.mentions.user_ids),对齐 agentteams_matrix/channel.py 已验证逻辑;false=单文本(仅 bridge↔bridge 调试用) |
| `emitter.interrupted_notice` | `处理中断,请重新 @我` | |
| `system_prompt.parts` | `[coordination, agents_md, soul_md]` | 无 heartbeat_checklist(B12 取消) |
| `config.refresh_interval` | `0`(关) | AGENTS.md 默认启动缓存、每 Turn 附带;>0 时每 Turn 前重拉 S3(§5.4,评审已定默认关) |
| `store.backend` / `redis.url_env` | `memory` / `BRIDGE_REDIS_URL` | 生产注入 redis |
| `bootstrap.s3.retry` | `6×5s` | 等调谐写入 |
| `lifecycle.report_ready.command` | `agt worker report-ready --name …` | |
| `lifecycle.probes.port` | `8081` | /healthz /readyz **/status(§七.5 存活探针)** |
| `shutdown.cancel_turn/close_session/grace` | `true`/`true`/`20s` | |
| `media.mode` | `placeholder` | placeholder / ignore |

---

## 十一、接口假设与降级(评审拍板项)

| # | 假设(方案条目) | bridge 降级(已内建) |
|---|---|---|
| 1 | ~~submit_turn 支持 input.system~~(能力 #4) | **已冻结**:契约 v0.2 agentMd 字段,无降级 |
| 2 | SSE 事件样例可拿到(前置 5) | generic 零代码适配;拿不到前 cimicode dialect 按契约形状占位 |
| 3 | ~~程序化鉴权确定(能力 #7)~~ **已定(2026-08-28):cimicode gateway 不鉴权**(Higress 关) | auth.type 固定 none;四种 AuthProvider 扩展点保留给其他 runtime |
| 4 | Session 关闭 API 存在(能力 #5) | close no-op |
| 5 | interrupted 主动通知(能力 #8) | 断流+超时检测 |
| 6 | 日重置(能力 #14) | 默认方案 A |
| 7 | 跨 Session 文件协作形态(能力 #9) | file_ids/artifact 通道占位 |
| 8 | ~~HEARTBEAT.md 分发渠道~~ **已取消** | ~~S3 每轮拉取~~:Leader 是 OpenClaw,worker 无心跳,不需要 |
| 9 | Redis 平台标配 | memory/file 兜底 |
| 10 | report-ready 命令与 displayName 归属(方案 §4.1.8) | 命令可配;bridge 不设 displayName |
| 11 | team room 不启用加密(首版无 E2EE) | 加密事件告警跳过(§3.5) |
| 12 | **调谐在 openclaw.json 加 bridge 扩展段 + COORDINATION_* env 注入**(operator 改造项,评审修正:原"生成 bridge.yaml"取消) | openclaw.json 缺省可跑(默认+env);缺 COORDINATION_* 时 RoleResolver 全unknown、协作上下文为空(告警) |
| 13 | operator 注入 COORDINATION_* 后,**调谐应停止向 AGENTS.md 注入 Coordination 块**(现 InjectCoordinationContext 逻辑),避免与 bridge 的 env 渲染双写重复 | 迁移期未停止前,`system_prompt.parts` 去掉 `coordination` 段改用 AGENTS.md 里的注入块(单通道,不双写) |
| 14 | **OpenClaw Leader 需探活 bridge worker(评审新增)**:worker 无心跳,Leader 依赖 `/status` 区分空闲与死亡 | `/status` 本地只读接口必做(§七.5);Leader 侧(OpenClaw)调用方式待平台确认(kubectl exec / service DNS) |
| 15 | **跨 runtime 协作(评审新增)**:OpenClaw Leader ↔ bridge Worker 是主协作通道 | bridge 发侧三层 mention(§3.6)+ 复用 agentteams-matrix-channel 插件(§1.3);E2E-6 验证 |
| 16 | **cimicode gateway 契约(评审新增 2026-08-28)**:baseurl + templateId 由 S3 bridge 扩展段下发;create session 携带 template_id 的端点形态、submit_turn 是否也需携带——待服务方 D1 定稿 | adapter 端点模板可配;定稿前按 owner_id+template_id 占位实现,mock 契约先行 |

---

## 十二、实施计划(任务拆解 B1-B14,共 26 天)

> 桥侧工时表原文。依赖 D1/D2(cimicode 无状态服务侧,他组任务)只约束 B8 起的**真服务联调**——B1-B7、B14 的单元/契约层用 mock runtime 先行,不等 D 线。依赖链核对:B2←B1、B3←B2、B4←B3、B5←B4、B6←B5、B7←B1、B8←(D1,D2,B7)、B9←B8、B10←B9、B11←B10、B13←(B2,B3)、B14←全部(B12 已取消)。

### 12.1 任务拆解

| 任务 ID | 任务名称 | 工时 | 依赖 | 具体内容 |
|---------|---------|------|------|---------|
| B1 | Bridge 骨架 + 技术栈选型 + 项目初始化 | 2 天 | 无 | 确定技术栈(Go/Bun/Python);项目结构搭建;环境变量解析(cimicode 无状态服务 URL/协作上下文/Matrix 凭证/Matrix homeserver URL);日志框架;K8s Deployment YAML;Docker 镜像 |
| B2 | Matrix token 管理(评审修正 2026-08-28:登录链裁剪) | 2 天 | B1 | env token(`AGENTTEAMS_WORKER_MATRIX_TOKEN`)加载 + whoami 校验(凭证唯一来源,无密码 login);401 → controller matrix-token 刷新 + 重试退避;token 仅内存不持久化 |
| B3 | Matrix /sync 长轮询 + 事件解析 | 2 天 | B2 | /sync 长轮询循环(timeout=30s);since token 持久化(**评审修正:默认 persist=true** 走 StateStore,重启续传;false 时仅内存→重启 catch-up 全量);next_batch 提取;事件解析(m.room.message → body+sender+room_id);backoff 重连(1s→2s→4s→...→30s) |
| B4 | @mention 检测与硬过滤 | 2 天 | B3 | requireMention(不@自己不调 gateway chat,消息进 flat history);groupAllowFrom(只处理 Leader/Admin/Human——**Leader 是 OpenClaw,worker 之间 peer-mentions 默认阻断**);自@过滤;@mention 正则解析 + 容错(无 domain→查映射表;尾部空格→trim;@后空格→兼容) |
| B5 | flat history 组装(2026-08-31 修订,原"两段式 history buffer") | 1 天 | B4 | per-room HistoryStore(`[{role, content, event_id}]`,≤200 条 FIFO);未 @ 消息 append_user(sender 前缀);agent 回复 append_assistant;event_id 去重;滑窗裁剪;snapshot() 供 chat 请求组装——**无两段式拼接、无 buffer 清空逻辑**(§3.4) |
| B6 | history 重建 | 1 天 | B5 | bridge pod 重启后 history 丢失:StateStore 持久化开启时优先恢复;否则从 Matrix room timeline 拉最近 N 条重建(user + assistant 消息);限制全量同步处理量;处理 Matrix 历史保留限制 |
| B7 | 协作上下文注入 | 2 天 | B1 | 从环境变量读协作上下文(COORDINATION_ROLE/LEADER/TEAM/ROOM/ADMIN/WORKERS);构建 Coordination 文本(对应 agentconfig/coordination.go:95-123 worker + standalone 两模板,leader 模板已删);**bridge 只有 worker/standalone 角色** |
| B8 | 调 cimicode gateway chat | 2 天 | D1, D2, B7 | 组装 chat 请求(turnId=event_id + agentMd + history snapshot + userMessage,契约 v0.2 §5.2);create(带 idempotencyKey)找Session;鉴权 none(gateway 不鉴权);提交阶段失败有限重试(同 turnId 幂等) |
| B9 | cimicode 无状态服务事件流消费 | 2 天 | B8 | 消费结构化运行事件(带 event_seq);事件类型分类(message.part.delta/message.part.updated/message.updated/done/session.error);累积响应文本;Turn interrupted 检测(事件流断开发 Matrix 中断通知) |
| **B10** | **cimicode 响应格式 → Matrix(Element Web)格式转换** | **2 天** | B9 | **重点任务**:cimicode 无状态服务返回的结构化运行事件 → 转换成 Matrix m.room.message 格式发送;处理消息体格式(纯文本/Markdown/HTML formatted_body);处理工具调用结果展示(cimicode 的 thinking/tool_call/component 标记 → Matrix 可读格式);处理流式输出 vs 完整响应策略(Matrix 不支持消息流式编辑,确定等完整还是分段发) |
| B11 | 事件流转发 Matrix | 2 天 | B10 | 以 agent bot 身份发 Matrix 消息(POST /rooms/{roomId}/send/m.room.message);NO_REPLY 检测(不发 Matrix);**发侧三层 mention(body 明文 + matrix.to 锚点 + m.mentions.user_ids)——OpenClaw Leader 只认结构化 mention,单文本会漏** |
| B12 | ~~heartbeat 定时器~~ **已取消** | — | — | **Leader 保持 OpenClaw,worker 无心跳。原 B12 任务删除,不实现。** 相应 §5.4 启动时序 Phase 4、§七.3 leader 模板、§九 B 组测试、§十 heartbeat.* 配置全部移除 |
| B13 | 启动生命周期管理 | 2 天 | B2, B3 | 分阶段等待:Phase 1 等 Matrix(env token whoami 校验,退避重试)→ Phase 2 连 cimicode 无状态服务(health check + 重试)→ Phase 3 report-ready → Phase 4 监听;K8s liveness/readiness probe;**本地只读 /status 接口供 OpenClaw Leader 查 worker 存活(§七.5,必做)**;优雅停机 |
| B14 | Bridge 单元测试 + 集成调试 | 2 天 | B1-B13 | 各子模块单元测试;单 agent 端到端调试(@mention → submit_turn → 事件流 → 格式转换 → Matrix 响应);修复联调问题 |

### 12.2 设计覆盖表(任务 → 本文档落点,逐 bullet 核对)

| 任务 | 设计落点 |
|---|---|
| B1 | §1.3 定栈 / §5.2 env 解析 / §7.6 日志 / §8.1 镜像 + **Deployment YAML** / §8.2 目录 |
| B2 | §3.1:env token whoami、**token 仅内存**、401 → controller 刷新链 + 重试退避(登录链已裁剪,差异见 §3.1) |
| B3 | §3.2:timeout=30s、**since 持久化(评审修正:默认 true)+ 重启 catch-up**、next_batch、m.room.message 解析、退避 1s→…→30s |
| B4 | §3.3:requireMention / groupAllowFrom / peer-mentions off / 自@ / 三级解析+容错 |
| B5 | §3.4(flat history 修订):HistoryStore ≤200 FIFO、event_id 去重、滑窗、snapshot 组装;**无两段式 golden 对照**(机制已废) |
| B6 | §3.4:StateStore 恢复优先、timeline 回拉 N 条 fallback、Synapse 历史保留限制处理 |
| B7 | §七.3 + §5.2:7 个 COORDINATION_* env、worker/standalone 两分支(**leader 模板已删**);**前置依赖 = 调谐 T2(§六.1 注入 COORDINATION_* env)** |
| B8 | §4.1(契约 v0.2):turnId/agentMd/history/userMessage 四元组、create 带 idempotencyKey、鉴权 none(gateway 不鉴权)、**两段重试** |
| B9 | §4.3:五事件映射表、event_seq、part 缓冲聚合、断流→interrupted |
| **B10(重点)** | **§3.6**:渲染管线、formatted_body 白名单消毒、thinking/tool/component 策略、流式三策略 |
| B11 | §3.6:bot 发送、NO_REPLY、**发侧三层 mention(body+matrix.to+m.mentions)** |
| ~~B12~~ | ~~heartbeat~~ **已取消**:Leader 保持 OpenClaw,worker 无心跳,原任务删除 |
| B13 | §5.3 Phase 0-4(login 10×5s、health 无限退避、report-ready)+ §7.1 优雅停机 + 探针 + **/status 存活接口(§七.5)** |
| B14 | §九:四层 43 用例 + E2E-1~6(单 agent 端到端即 E2E-1)**;E2E-6 = OpenClaw Leader ↔ bridge Worker 跨 runtime 协作** |

### 12.3 实施顺序建议

```
阶段 0(2 天):B1 骨架 + mock runtime 就位(§9.1 契约层,不等 D 线)
阶段 1(6 天):B2-B4-B5-B6 Matrix 收侧 + B3 sync(B2←B1、B4←B3、B5←B4、B6←B5)
阶段 2(2 天):B7 协作上下文(与 B2/B3 并行,只依赖 B1)
阶段 3(6 天):B8-B9-B10-B11 runtime 对接 + 渲染 + 发送(B8 依赖 D1/D2,期间契约 mock)
阶段 4(2 天):B13 生命周期 + /status(依赖 B2,B3)
阶段 5(2 天):B14 测试 + 端到端(E2E-1/2/4/5/6)
合计:B1-B14(除 B12)共 26 天
```
