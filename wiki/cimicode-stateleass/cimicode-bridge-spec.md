# cimicode-bridge 当前实现规格

> 版本：2026-09-09（rev2，多 runtime 化后的重述）
>
> 本文以仓库当前 `cimicode-bridge/` 源码为准，描述已经实现并经过测试的行为。尚未实现的设计目标统一放在“后续工作”，不作为当前运行契约。

## 1. 当前定位

`cimicode-bridge` 是一个 Python 3.12+ FastAPI 常驻服务，用于把 AgentTeams 的 Matrix Worker 接入 AgentTeams 的运行时 Gateway。当前支持两种 runtime，按 `runtime.adapter` 分派：

- **cimicode**：无状态 Gateway，通道为 `POST /v1/gateway/session/chat` + SSE。
- **opencode**：opencode headless server，通道为 REST + 轮询，bridge 自管会话生命周期。

cimicode 链路：

```text
Synapse
  → matrix-nio AsyncClient /sync
  → MentionFilter 硬过滤
  → CoPaw 风格三段式群聊视野
  → Gateway POST /v1/gateway/session/chat
  → SSE RuntimeEvent
  → Matrix 消息格式
  → Synapse room_send
  → Element UI 显示
```

opencode 链路（`adapter=opencode` 时）：

```text
Synapse
  → mention 过滤
  → 三段式群聊视野
  → 每轮从 S3 重拉 runtime.yaml → v2.4 generator 渲染 agent.md
  → POST sandbox helper /agents-md（推 agent.md）
  → POST opencode /session/{id}/message（阻塞到整轮结束）
  → GET /session/{id}/message 轮询
  → 回发 Matrix
```

**Session 归属因 adapter 而异**：cimicode 依赖调谐或平台预创建 Session/Sandbox，bridge 只读取复用（`sessionId`、`sandboxId` 来自 S3 `openclaw.json`）；opencode 分支自管会话（首轮创建、后续复用、sandbox 重启后重建）。

当前不涉及新建前端。Synapse 和 Element UI 使用现有部署；bridge 只发送标准 Matrix `m.room.message` 内容。

## 2. 代码结构

```text
cimicode-bridge/
  Dockerfile
  pyproject.toml
  config/bridge.example.yaml
  deploy/deployment.yaml
  scripts/bridge-entrypoint.sh
  src/cimicode_bridge/
    app.py                 # FastAPI 工厂、生命周期、消息编排、晚到接线自愈
    bootstrap.py           # S3/MinIO 配置读取（openclaw.json + runtime.yaml 双载体）
    config.py              # 本地 YAML 配置模型
    events.py              # RuntimeEvent 和消息模型
    render.py              # agentMd 拼装 + Markdown→Matrix HTML 消息构造
    prompt.py              # v2.4 agent.md generator 子进程封装（opencode 分支）
    session.py             # CoPaw 风格 room history（HistoryStore 单 room buffer + HistoryManager per-room 注册表）
    api/routes.py          # HTTP 端点（探针 + 本地调试入口）
    api/probes.py          # 探针状态模型
    controller/client.py   # controller 交互（401 token 刷新 + 运行时接线查询）
    matrix/filter.py       # mention 和角色过滤
    matrix/gateway.py      # Matrix AsyncClient、sync、收发、typing、401 刷新
    runtime/base.py        # RuntimeAdapter / AuthProvider SPI 契约
    runtime/registry.py    # adapter 工厂（按 runtime.adapter 分派 cimicode / opencode）
    runtime/client.py      # Gateway HTTP + 手写 SSE 客户端（cimicode）
    runtime/adapters.py    # cimicode SSE 事件到 RuntimeEvent 的翻译
    runtime/turn.py        # Gateway 单轮调用编排（agentMd 组装 + chat + SSE 事件聚合）
    runtime/opencode_adapter.py  # opencode REST + 轮询适配器
    store/                 # memory/file/redis StateStore
```

依赖方向：Matrix 协议放在 `matrix/gateway.py`，mention 规则放在 `matrix/filter.py`，Gateway 单轮执行放在 `runtime/`（cimicode SSE / opencode 轮询，由 `runtime/registry.py` 工厂分派），三段式视野放在 `session.py`，HTTP 端点在 `api/routes.py`，controller 交互在 `controller/client.py`，FastAPI 只负责组装这些组件。

## 3. 配置来源

### 3.1 本地默认配置

`config.py` 从本地 YAML 读取 `BridgeConfig`。不存在配置文件时使用模型默认值。

示例文件：`cimicode-bridge/config/bridge.example.yaml`。

### 3.2 S3/MinIO 配置

当以下环境变量齐全时，bridge 创建 MinIO 客户端：

```text
AGENTTEAMS_FS_ENDPOINT
AGENTTEAMS_FS_ACCESS_KEY
AGENTTEAMS_FS_SECRET_KEY
AGENTTEAMS_FS_BUCKET
AGENTTEAMS_STORAGE_PREFIX       # 可选
AGENTTEAMS_WORKER_NAME          # 用于定位 agent 目录
AGENTTEAMS_FS_SECURE            # 可选，true/1/yes 表示 HTTPS
```

对象路径为：

```text
{STORAGE_PREFIX}/agents/{AGENTTEAMS_WORKER_NAME}/{object_name}
```

启动读取**两个载体**（各自独立重试，默认 6 次 × 5 秒）：

- **openclaw.json**：legacy cimicode 路径的载体（bridge.runtime session/sandbox 绑定）
- **runtime/runtime.yaml**：managed（opencode）运行时轨道的载体，`managed_runtime_type()` 从 `member.runtime` 读取运行时类型，据此自裁决 opencode adapter

读取对象包括：

```text
openclaw.json
runtime/runtime.yaml
AGENTS.md
SOUL.md
PROFILE.md
```

`openclaw.json` 的当前消费字段：

```json
{
  "channels": {
    "matrix": {
      "homeserver": "https://synapse.example",
      "accessToken": "matrix-access-token"
    }
  },
  "bridge": {
    "runtime": {
      "baseUrl": "http://cimicode-gateway",
      "templateId": "worker-template",
      "sessionId": "sess-123",
      "sandboxId": "sandbox-456",
      "helperUrl": "http://opencode-<worker>-sandbox-svc:4097"
    }
  }
}
```

兼容 snake_case 写法：`access_token`、`base_url`、`template_id`、`session_id`、`sandbox_id`、`helper_url`。

Matrix token 的来源优先级：

1. S3 `openclaw.json.channels.matrix.accessToken`
2. 本地开发 fallback：`AGENTTEAMS_WORKER_MATRIX_TOKEN`

runtime.yaml-only 引导：无 openclaw.json 且仅 runtime.yaml 时，persona（soul / identity-as-profile）从 `desired.inlineConfig` 提取；matrix token 走 `AGENTTEAMS_WORKER_MATRIX_TOKEN` env（managed 分支不写 openclaw.json）。

token 只放在进程内的 Matrix client，不写入 StateStore。当前 bridge 不读取或使用 S3 的 `credentials/matrix/password`，也不执行密码登录。

## 4. Matrix 接入

### 4.1 建立连接

bridge 使用：

```python
AsyncClient(
    homeserver,
    user="",
    config=AsyncClientConfig(store_sync_tokens=False),
)
```

启动时把 access token 写入 client，然后调用 `whoami()`。成功后保存：

- `user_id`
- `device_id`（如果服务端返回）

`whoami()` 失败时本次 Matrix 启动失败，当前进程不会建立可用 Matrix 连接。

### 4.2 同步

认证成功后注册 `RoomMessageText` 回调，并运行手动 `/sync` 循环：

- 长轮询超时：`matrix.sync_timeout_seconds * 1000` 毫秒
- `full_state=True` 当当前没有 since
- since 从 StateStore 读取
- 每次响应的 `next_batch` 保存回内存和 StateStore
- 普通异常按 `1s → 2s → 4s ... → 30s` 退避
- 取消任务时退出循环
- 响应中带 `invite` 的房间会被自动 `join()`（controller 把 worker 邀进 team 房间，没有谁替我们 join 就收不到那些房间的消息）

当前 `since` key 为：

```text
matrix:since:{AGENTTEAMS_WORKER_NAME 或 worker}
```

当前已实现 StateStore 读写接入，但还没有完整的 catch-up 抑制回调语义；首次同步仍由 matrix-nio 的正常响应处理。

### 4.3 token 刷新

sync 异常字符串包含 `401` 或 `M_UNKNOWN_TOKEN` 时，bridge 调用：

```text
POST {AGENTTEAMS_CONTROLLER_URL}/api/v1/credentials/matrix-token
Authorization: Bearer {AGENTTEAMS_AUTH_TOKEN 或 AUTH_TOKEN_FILE 内容}
```

成功返回 `access_token` 后更新 Matrix client，并重新执行 `whoami()`。

刷新链参数：`MAX_TOKEN_REFRESH_RETRIES = 3`、`TOKEN_REFRESH_BACKOFF_S = 5`（`matrix/gateway.py`）。`401/M_UNKNOWN_TOKEN` 触发刷新，刷新耗尽后 sync 会继续重试。尚未引入独立的 Matrix 错误专用类型判断。

### 4.4 入站文本事件

当前只注册 `RoomMessageText`。收到事件后提取：

```text
room.room_id
event.sender
event.event_id
event.source.content.body
```

自身发送的消息直接忽略。图片、文件、音频、视频、加密事件和成员 display name 事件当前未注册处理。

## 5. Mention 和角色过滤

`MentionFilter` 支持三种入站 mention 来源：

1. `content["m.mentions"]["user_ids"]`
2. `formatted_body` 中的 `https://matrix.to/#/...` 链接
3. body 文本中的 `@user` 或 `@user:domain`

当前默认 alias：

```text
leader
manager
team
```

过滤配置：

```text
require_mention: true
allow_unknown: false
group_allow_from_worker: [leader, admin, human]
```

角色来自以下环境变量：

```text
COORDINATION_LEADER
COORDINATION_ADMIN
COORDINATION_WORKERS    # 当前按逗号分隔
AGENTTEAMS_WORKER_MATRIX_USER_ID  # 启动前的可选自身 ID
```

过滤规则：

- 自身消息拒绝
- `require_mention=true` 时没有 @ 当前 agent 拒绝
- unknown sender 默认拒绝
- 群聊只允许 `leader`、`admin`、`human`
- worker 默认不能直接触发另一个 worker
- 没有角色映射的本地开发模式将 sender 视为 `human`，以兼容本地 HTTP 测试

过滤返回 `FilterDecision`：

```text
accepted
reason
role
mentions
```

## 6. CoPaw 三段式群聊视野

bridge 复用 CoPaw 的核心 history 语义，不复制整个 CoPaw runtime。

三段式组装入口为 `session.HistoryManager`（per-room 注册表，懒创建单 room 的 `HistoryStore`）；app.py 通过 `record_ambient / build_context / clear` 三个方法操作 buffer，不直接触碰 dict。

每个 room 有一个内存 `HistoryStore`。容量由配置 `history.max_entries` 决定（**默认 50**，`HistoryStore` 类默认值 200 会被此配置覆盖），超出后 FIFO 淘汰，并按 `event_id` 去重。

允许但未 mention 当前 agent 的消息进入 room history。mention 触发时组装：

```text
[Chat messages since your last reply - for context]
role: 历史消息 1
role: 历史消息 2

[Current message - respond to this]
当前发送者: 当前消息
```

当前实现将三段式文本放进 Gateway 的 `userMessage`，Gateway 的 `history` 字段传空数组，避免 Gateway 再次拼接造成重复上下文。

Gateway 调用成功并完成 Matrix 发送后，清空对应 room history。Gateway 出错时保留 history。

当前 history 还没有持久化到 Redis/FileStore，也没有从 Matrix timeline 重建。

## 7. agentMd

agentMd 组装**因 adapter 分两个路径**。

**cimicode 路径**——每次 Gateway chat 前调用 `build_agent_md()`，组装：

```text
## Coordination
- Role
- Leader
- Team
- Room
- Admin
- Workers
- 仅响应最新一条明确 @ 你的消息

## AGENTS.md
...

## SOUL.md
...
```

内容来源：协作环境变量 `COORDINATION_*`、S3 `AGENTS.md`、S3 `SOUL.md`。

**opencode 路径**——每轮调用 v2.4 generator（`prompt.py` `build_agent_md_via_generator()`，镜像内置 `/opt/agenttools/generate_agent_md.py`），从 `runtime/runtime.yaml` + SOUL/PROFILE 渲染 agent.md：

- 每轮**从 S3 重拉** runtime.yaml（controller 首写后会继续 enrich `member.matrixUserId` 等，启动缓存不能遮蔽真相），仅拉取失败时回退启动缓存
- 渲染失败 fail-loud（`GenerateAgentMdError`），拒不开启本轮，绝不把半配置的 system prompt 发给 sandbox

无论哪个路径，agent.md 生成后做 best-effort **回写 S3**（`agents/<worker>/agent-md/latest.md`，观测通道，失败仅告警）。当前尚未实现 `config.refresh_interval` 的每轮重新拉取（cimicode 路径仍在用启动缓存，opencode 路径是每轮重拉）。

## 8. Gateway chat

### 8.1 调用范围

**cimicode 路径**只调用：

```http
POST /v1/gateway/session/chat
```

不调用 `/v1/gateway/session/create` 和 `/v1/gateway/session/destroy`。Session 和 Sandbox 由调谐或平台预创建，并通过 S3 `openclaw.json.bridge.runtime` 下发。

**opencode 路径**调用另一组端点（由 `runtime/opencode_adapter.py` 封装）：

```text
POST {base_url}/session                       # 创建（自管会话；显式 session_id 失效时重建）
GET  {base_url}/session/{id}                  # 校验会话存在
POST {base_url}/session/{id}/message          # 提交消息（服务端阻塞到整轮结束）
GET  {base_url}/session/{id}/message          # 轮询结果
POST {helper_url}/agents-md                   # 每轮把 agent.md 推给 sandbox helper
```

opencode 分支由 bridge 自管会话生命周期：`sessionId` 为空时首轮创建、后续复用、sandbox 重启后 404 则重建。

### 8.2 请求体

**cimicode**：

```json
{
  "sessionId": "sess-123",
  "sandboxId": "sandbox-456",
  "turnId": "$matrix-event-id",
  "agentMd": "...",
  "history": [],
  "userMessage": "CoPaw 三段式群聊视野"
}
```

**opencode**（消息体）：`{"parts": [{"type": "text", "text": "<三段式 userMessage>"}]}`——history 已由调用方折叠进 userMessage，`turn_id` 仅用于日志（该路径无幂等保护）。

`turnId` 当前使用 Matrix `event_id`。

当前 Gateway 不鉴权。`runtime.auth_type` 默认是 `none`，代码保留可选 auth provider 参数，但当前没有注入 Gateway token。

### 8.3 SSE

cimicode 路径用 `httpx` 读取 SSE，但 **SSE 行解析为手写实现**（`runtime/client.py` `stream_sse()`，按空行分帧、`data:` 行拼接为 JSON），**不依赖 `httpx-sse`**（其 `aconnect_sse` 在容器内两种用法均异常，对应修复 commit `3f2bf596`）。`pyproject.toml` 仍保留 `httpx-sse` 依赖，目前为死依赖。

```text
data: {"event":"message","delta":"你好"}

data: {"event":"done","content":"你好"}
```

事件会转换为统一 `RuntimeEvent`：

```text
text_delta
turn_completed
runtime_error
turn_interrupted
```

支持的 Gateway 事件名称：

```text
message
message.part.delta
message.part.updated
message.updated
done
error
session.error
```

Cimicode dialect 会按 `part_id`：

- delta 追加内容
- updated 替换 part 内容
- done 按首次出现的 part 顺序拼接

SSE 结束时没有 `turn_completed`，追加 `turn_interrupted`。

当前未实现提交阶段重试、HTTP 状态专用错误映射和完整的“首事件前可重试、首事件后不可重试”策略。

## 9. Matrix 出站消息

Gateway 完成后，bridge 使用 `room_send()` 发标准 Matrix 文本消息。

基础消息：

```json
{
  "msgtype": "m.text",
  "body": "任务已完成"
}
```

body 中出现完整 Matrix MXID 时，追加三层 mention：

```json
{
  "msgtype": "m.text",
  "body": "@leader:matrix.local 任务已完成",
  "format": "org.matrix.custom.html",
  "formatted_body": "<a href=\"https://matrix.to/#/%40leader%3Amatrix.local\">@leader:matrix.local</a> 任务已完成",
  "m.mentions": {
    "user_ids": ["@leader:matrix.local"]
  }
}
```

Element UI 通过 Synapse 收到该标准 Matrix 内容后显示，不需要 bridge 修改前端。

当前已实现：Markdown → HTML 渲染（`markdown-it-py`，html=False 防 XSS、linkify、breaks、strikethrough、table）、HTML escape、换行转 `<br>`、基本 Matrix pill，以及**三层 mention**。三层 mention 的两个组成部分（`render.py` + `matrix/gateway.py`）：

1. 正文出现完整 MXID 时，写 `m.mentions` 结构化字段，并把首次出现的 MXID 换成 `matrix.to/#/...` 锚点（Element pill）
2. 正文里的**裸 localpart（如 `@oct-lead`）**经房间成员索引（`joined_members`，TTL 缓存 600s）解析为完整 MXID，再进 `m.mentions`

`turn failed` 时 app 会向房间发 `**turn failed**: ...`（失败可见化，避免房间永远等下去）。尚未实现 HTML 白名单 sanitizer、长消息分片和可配置流式策略。

`NO_REPLY` 当前由 app 做 trim 后精确过滤：命中后不发送 Matrix 消息，但清空当前 room history。

## 10. FastAPI 生命周期与接口

入口为 `cimicode_bridge.main`，FastAPI 使用 lifespan：

```text
创建 app
  → BridgeApp.start()
  → lifespan start_background()
  → 创建 Matrix task（配置齐全时）
  → 退出时 stop + shutdown
```

start() 之后有两种后台路径（`app.py`）：

- **接线齐全**：起 Matrix sync 循环
- **接线不全**（如 opencode 运行时接线在 pod 创建后才被 controller 写进 Worker spec.env）：`start_background` 不起 sync，改起**自愈轮询**（`_recover_late_runtime_wiring`），每 15s 轮询 `GET /api/v1/workers/{self}` 或重拉 S3 bootstrap；接线到位后在进程内重建 runtime adapter 与 Matrix 网关，无需重启 pod

HTTP 接口：

```http
GET /healthz
GET /readyz
GET /status
POST /api/v1/bridge/handle-message
```

`/healthz`：进程存活。

`/readyz`：Matrix 配置不完整时返回 false；Matrix task 建立连接且有 session 时返回 true（session 门禁 adapter 化：只有 cimicode 要求预创建 sessionId）。当前还没有 Gateway health 检查和 report-ready。

`/status` 当前返回（`phase` 为运行时实际值，`runtime` 为 adapter 名）：

```json
{
  "worker": "cimicode-bridge",
  "phase": "listening",
  "runtime": "cimicode",
  "matrix_connected": true,
  "runtime_healthy": true,
  "ready": true
}
```

`/api/v1/bridge/handle-message` 是本地调试入口。它复用 mention 和三段式组装逻辑，但接受的 HTTP 请求不会自动调用真实 Gateway chat；真实 Gateway 调用由 Matrix 回调路径执行。

## 11. StateStore

当前提供统一异步接口：

```python
get(key)
set(key, value, ttl_seconds=None)
delete(key)
```

后端：

```text
MemoryStore
FileStore
RedisStore
```

配置：

```text
store.backend: memory | file | redis
BRIDGE_REDIS_URL
```

当前真正接入的是 Matrix since 保存；history、Turn 和 Session 状态还没有统一接入。

## 12. 部署

当前交付文件：

```text
Dockerfile
scripts/bridge-entrypoint.sh
config/bridge.example.yaml
deploy/deployment.yaml
```

Docker 镜像使用 `python:3.12-slim`，安装当前 bridge 包后启动 FastAPI CLI。镜像内**打包 v2.4 agent.md generator 工具链**（`opencode/bridge/generate_agent_md.py` + `opencode/bridge/agentteams_log.py` + `opencode/template/opencode-worker-agent/AGENTS.md` 源模板），供 opencode 分支的每个 turn 渲染 agent.md；因此 Docker **build context 必须是仓库根**（`Makefile build-cimicode-bridge` 这样约定）。

Kubernetes manifest 提供：

- 单副本 Deployment
- Service 端口 8081
- `/healthz` liveness probe
- `/readyz` readiness probe
- 30 秒 termination grace period
- 100m/128Mi requests
- 1 CPU/512Mi limits

实际 Secret 名称、镜像仓库、完整环境变量注入方式需要和 controller/operator 联调确认。

## 13. 测试现状

当前单元测试覆盖：

- mention 解析和角色过滤
- CoPaw 三段式 history
- event_id 去重
- S3 openclaw 配置字段解析
- Cimicode part 聚合
- Matrix → Gateway → Matrix 的 fake 闭环
- FastAPI health/readiness/status
- opencode adapter（会话自管、错误、轮询完成）
- bootstrap 与 env 覆盖
- 晚到接线自愈（late runtime wiring recovery）
- typing 指示器（uvicorn/gateway mentions）

最近一次验证：

```text
18 passed, 1 warning
```

测试主要是单元和 fake transport 测试，尚未覆盖真实 Synapse、真实 MinIO、真实 Gateway、Redis 和 Kubernetes。

## 14. 后续工作

以下不是当前规格，而是后续实现任务：

1. 统一 HTTP 调试入口和 Matrix 回调入口，HTTP 也调用同一个异步 chat 流程。
2. 完善 Matrix 401 专用错误判断、sync 恢复和 since catch-up 语义；成员 display name 处理。
3. 将 history 接入 StateStore，并支持 Matrix timeline 重建。
4. 完善 Gateway chat 的提交重试、断流和 timeout 语义（`submit_max_retries`、`queue_max_pending` 配置已在 config.py 但未接线）。
5. 增加 Gateway health 检查和 `agt worker report-ready`（`runtime_healthy` 目前仍由 Matrix 连接驱动）。
6. 完善 HTML 白名单 sanitizer、NO_REPLY 可配置化、长消息分片和可配置流式策略。
7. 增加图片/文件/音频/视频和加密事件处理。
8. 补充真实 Synapse + MinIO + Gateway mock + Redis 集成测试。
9. 修正 message 并发语义：bridge 当前无输入队列、全进程串行（单 turn 阻塞所有房间 sync），多 @ 无合并；对比 copaw 的 per-room 队列 + drain/merge + turn 期间 pending 折叠行为存在差距，需要参照执行。
