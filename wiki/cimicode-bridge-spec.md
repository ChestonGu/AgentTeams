# cimicode-bridge 当前实现规格

> 版本：2026-09-03
>
> 本文以仓库当前 `cimicode-bridge/` 源码为准，描述已经实现并经过测试的行为。尚未实现的设计目标统一放在“后续工作”，不作为当前运行契约。

## 1. 当前定位

`cimicode-bridge` 是一个 Python 3.12+ FastAPI 常驻服务，用于把 AgentTeams 的 Matrix Worker 接入 cimicode 无状态 Gateway。

当前运行链路：

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

当前 bridge 不负责 Gateway Session 的创建和销毁。`sessionId`、`sandboxId` 由调谐或平台写入 S3 的 `openclaw.json`，bridge 启动时读取并复用。

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
    app.py                 # FastAPI 工厂、生命周期、消息编排
    bootstrap.py           # S3/MinIO 配置读取
    config.py              # 本地 YAML 配置模型
    events.py              # RuntimeEvent 和消息模型
    matrix_client.py       # mention 和角色过滤
    render.py              # agentMd 和 Matrix 消息内容
    session.py             # CoPaw 风格 room history
    matrix/gateway.py      # Matrix AsyncClient、sync、发送
    runtime/client.py      # Gateway HTTP + SSE 客户端
    runtime/adapters.py    # Gateway 事件到 RuntimeEvent 的转换
    store/                 # memory/file/redis StateStore
```

依赖方向：Matrix 协议放在 `matrix/gateway.py`，mention 规则放在 `matrix_client.py`，Gateway 协议放在 `runtime/`，FastAPI 只负责组装这些组件。

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

启动最多读取 `openclaw.json` 6 次，每次失败间隔 5 秒。读取对象包括：

```text
openclaw.json
AGENTS.md
SOUL.md
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
      "sandboxId": "sandbox-456"
    }
  }
}
```

兼容 snake_case 写法：`access_token`、`base_url`、`template_id`、`session_id`、`sandbox_id`。

Matrix token 的来源优先级：

1. S3 `openclaw.json.channels.matrix.accessToken`
2. 本地开发 fallback：`AGENTTEAMS_WORKER_MATRIX_TOKEN`

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

当前 `since` key 为：

```text
matrix:since:{AGENTTEAMS_WORKER_NAME 或 worker}
```

当前已实现 StateStore 读写接入，但还没有完整的 catch-up 抑制回调语义；首次同步仍由 matrix-nio 的正常响应处理。

### 4.3 token 刷新

sync 异常字符串包含 `401` 时，bridge 可调用：

```text
POST {AGENTTEAMS_CONTROLLER_URL}/api/v1/credentials/matrix-token
Authorization: Bearer {AGENTTEAMS_AUTH_TOKEN 或 AUTH_TOKEN_FILE 内容}
```

成功返回 `access_token` 后更新 Matrix client，并重新执行 `whoami()`。

当前实现是基础刷新路径，尚未实现独立的刷新最大重试配置和专用 Matrix 错误类型判断。

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

每个 room 有一个内存 `HistoryStore`，默认容量 200 条，超出后 FIFO 淘汰，并按 `event_id` 去重。

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

每次 Gateway chat 前调用 `build_agent_md()`，组装：

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

内容来源：

- 协作环境变量：`COORDINATION_*`
- S3 `AGENTS.md`
- S3 `SOUL.md`

当前每轮使用启动时缓存的 AGENTS.md/SOUL.md，尚未实现 `config.refresh_interval` 的每轮重新拉取。

## 8. Gateway chat

### 8.1 调用范围

当前 bridge 只调用：

```http
POST /v1/gateway/session/chat
```

bridge 不调用：

```text
/v1/gateway/session/create
/v1/gateway/session/destroy
```

Session 和 Sandbox 由调谐或平台预创建，并通过 S3 `openclaw.json.bridge.runtime` 下发。

### 8.2 请求体

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

`turnId` 当前使用 Matrix `event_id`。

当前 Gateway 不鉴权。`runtime.auth_type` 默认是 `none`，代码保留可选 auth provider 参数，但当前没有注入 Gateway token。

### 8.3 SSE

使用 `httpx` 和 `httpx-sse` 读取标准 SSE：

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

当前已实现 HTML escape、换行转 `<br>` 和基本 Matrix pill；尚未实现 Markdown 转换、HTML 白名单 sanitizer、长消息分片和可配置流式策略。

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

HTTP 接口：

```http
GET /healthz
GET /readyz
GET /status
POST /api/v1/bridge/handle-message
```

`/healthz`：进程存活。

`/readyz`：当前实现中，Matrix 配置不完整时返回 false；Matrix task 建立连接并且有 session ID 时返回 true。当前还没有 Gateway health 检查和 report-ready。

`/status` 当前返回：

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

Docker 镜像使用 `python:3.12-slim`，安装当前 bridge 包后启动 FastAPI CLI。

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

最近一次验证：

```text
18 passed, 1 warning
```

测试主要是单元和 fake transport 测试，尚未覆盖真实 Synapse、真实 MinIO、真实 Gateway、Redis 和 Kubernetes。

## 14. 后续工作

以下不是当前规格，而是后续实现任务：

1. 统一 HTTP 调试入口和 Matrix 回调入口，HTTP 也调用同一个异步 chat 流程。
2. 完善 Matrix 401 专用错误判断、token 刷新重试和 sync 恢复。
3. 完善 since catch-up 语义、自动 join 和成员 display name。
4. 将 history 接入 StateStore，并支持 Matrix timeline 重建。
5. 完善 Gateway chat 的提交重试、断流和 timeout 语义。
6. 增加 Gateway health 检查和 `agt worker report-ready`。
7. 完善 Markdown/HTML 安全处理、NO_REPLY 配置、长消息分片和三层 mention alias 解析。
8. 增加图片/文件/音频/视频和加密事件处理。
9. 补充真实 Synapse + MinIO + Gateway mock + Redis 集成测试。
10. 修正 Docker CLI 入口与 `pyproject.toml` console script，使镜像 entrypoint 可直接启动。
