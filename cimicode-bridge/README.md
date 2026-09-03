# cimicode-bridge

`cimicode-bridge` 是 AgentTeams 的轻量 Python Bridge，用于把现有 Matrix/Synapse 房间接入 cimicode 无状态 Gateway。

它不包含新的前端。Synapse 继续负责 Matrix 服务，Element UI 继续负责消息展示；bridge 负责接收 Matrix 消息、调用 Gateway，并把结果发送回 Matrix。

## 当前消息链路

```text
Synapse
  -> matrix-nio AsyncClient /sync
  -> CoPaw 风格 mention 过滤
  -> CoPaw 三段式群聊视野
  -> Gateway POST /v1/gateway/session/chat
  -> SSE RuntimeEvent
  -> Matrix m.room.message
  -> Synapse
  -> Element UI
```

当前 Gateway Session 不由 bridge 创建或销毁。调谐负责创建 Session，并把 `sessionId`、`sandboxId` 写入 S3 的 `openclaw.json`；bridge 启动时读取并复用。

## 已实现能力

- FastAPI + uvicorn 常驻服务
- `matrix-nio` Matrix 客户端
- Matrix `whoami()` 身份校验
- Matrix 手动 sync 循环
- Matrix since 的 StateStore 基础读写
- sync 异常指数退避
- Matrix 文本事件接收
- `m.mentions`、`matrix.to`、文本 mention 检测
- leader/admin/human 白名单和 worker 间触发阻断
- CoPaw 三段式 room history
- S3/MinIO 读取 worker 配置
- Gateway chat SSE 调用
- `message`、`done`、`error` 事件转换
- Cimicode part delta/updated 基础聚合
- `agentMd` 组装
- Matrix `room_send`
- `body`、`formatted_body`、`m.mentions.user_ids` 基础三层 mention
- memory、file、redis StateStore
- `/healthz`、`/readyz`、`/status`
- Dockerfile、entrypoint、Kubernetes 基础 manifest

## 目录结构

```text
cimicode-bridge/
├── Dockerfile
├── README.md
├── pyproject.toml
├── config/
│   └── bridge.example.yaml
├── deploy/
│   └── deployment.yaml
├── scripts/
│   └── bridge-entrypoint.sh
├── src/cimicode_bridge/
│   ├── app.py                 # FastAPI 工厂和消息编排
│   ├── bootstrap.py           # S3/MinIO 配置读取
│   ├── config.py              # YAML 配置模型
│   ├── events.py              # RuntimeEvent 和消息模型
│   ├── matrix_client.py       # mention 和角色过滤
│   ├── render.py              # agentMd 和 Matrix 消息格式
│   ├── session.py             # CoPaw 三段式 history
│   ├── matrix/gateway.py      # Matrix AsyncClient、sync、发送
│   ├── runtime/client.py      # Gateway HTTP + SSE
│   ├── runtime/adapters.py    # SSE 到 RuntimeEvent
│   └── store/                 # memory/file/redis
└── tests/unit/
```

## S3/MinIO 配置

当以下变量存在时，bridge 使用 MinIO SDK 读取调谐写入的配置：

```text
AGENTTEAMS_FS_ENDPOINT
AGENTTEAMS_FS_ACCESS_KEY
AGENTTEAMS_FS_SECRET_KEY
AGENTTEAMS_FS_BUCKET
AGENTTEAMS_STORAGE_PREFIX       # 可选
AGENTTEAMS_WORKER_NAME
AGENTTEAMS_FS_SECURE            # 可选
```

读取路径：

```text
{STORAGE_PREFIX}/agents/{AGENTTEAMS_WORKER_NAME}/openclaw.json
{STORAGE_PREFIX}/agents/{AGENTTEAMS_WORKER_NAME}/AGENTS.md
{STORAGE_PREFIX}/agents/{AGENTTEAMS_WORKER_NAME}/SOUL.md
```

`openclaw.json` 示例：

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

Matrix token 来源顺序：

1. `openclaw.json.channels.matrix.accessToken`
2. `AGENTTEAMS_WORKER_MATRIX_TOKEN`，仅作为本地开发 fallback

当前不使用 `credentials/matrix/password`，不执行密码登录。Gateway 当前不鉴权。

## Matrix 接入

bridge 使用 `matrix-nio.AsyncClient`：

1. 从 S3 配置读取 homeserver 和 access token。
2. 调用 `whoami()` 获取自身 Matrix 用户 ID。
3. 注册 `RoomMessageText` 回调。
4. 运行长轮询 `/sync`。
5. 保存 `next_batch` 作为 since。
6. 收到文本事件后交给 bridge 消息处理链。

当前支持从 StateStore 读取和保存 since，sync 异常使用 `1s → 2s → 4s ... → 30s` 退避。

## Mention 和群聊视野

入站 mention 支持：

- `m.mentions.user_ids`
- `formatted_body` 中的 `matrix.to` 链接
- body 中的 `@user` 或 `@user:domain`

默认过滤规则：

```text
require_mention: true
allow_unknown: false
group_allow_from_worker: [leader, admin, human]
```

允许但没有 mention 当前 worker 的消息会进入当前 room 的 history。触发消息会组装成 CoPaw 风格：

```text
[Chat messages since your last reply - for context]
alice: 之前的群聊消息

[Current message - respond to this]
bob: @worker 请继续处理
```

Gateway 的 `history` 当前传空数组，三段式内容放入 `userMessage`，避免重复拼接。

## Gateway chat

bridge 只调用：

```http
POST /v1/gateway/session/chat
```

请求体：

```json
{
  "sessionId": "sess-123",
  "sandboxId": "sandbox-456",
  "turnId": "$matrix-event-id",
  "agentMd": "协作上下文 + AGENTS.md + SOUL.md",
  "history": [],
  "userMessage": "CoPaw 三段式群聊视野"
}
```

bridge 不调用：

```text
/v1/gateway/session/create
/v1/gateway/session/destroy
```

这两个生命周期操作由调谐或平台负责。

Gateway chat 返回 SSE，当前支持：

```text
data: {"event":"message","delta":"你好"}

data: {"event":"done","content":"你好"}
```

bridge 将事件转换为 `RuntimeEvent`，并在没有完成事件时生成 `turn_interrupted`。

## Matrix 出站消息

普通回复：

```json
{
  "msgtype": "m.text",
  "body": "任务已完成"
}
```

包含完整 Matrix 用户 ID 时，会生成基础三层 mention：

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

bridge 将该事件发送给 Synapse，Element UI 使用现有 Matrix 客户端自动显示。

当前 `NO_REPLY` 会被过滤，不发送 Matrix 消息；基础实现支持 HTML escape 和 Matrix pill，完整 Markdown sanitizer、长消息分片和流式发送策略仍在后续计划中。

## 运行

### Windows PowerShell

```powershell
cd D:\workbach\fork\AgentTeams\cimicode-bridge
D:\workbach\fork\AgentTeams\.venv\Scripts\python.exe -m pip install -e ".[dev]"
D:\workbach\fork\AgentTeams\.venv\Scripts\python.exe -m cimicode_bridge.main --host 127.0.0.1 --port 8081
```

### Linux/macOS

```bash
cd cimicode-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m cimicode_bridge.main --host 0.0.0.0 --port 8081
```

健康检查：

```text
GET /healthz
GET /readyz
GET /status
```

本地调试接口：

```text
POST /api/v1/bridge/handle-message
```

该接口用于模拟 Matrix 消息。真实部署应使用 Matrix `/sync` 回调路径。

## StateStore

当前提供：

```python
get(key)
set(key, value, ttl_seconds=None)
delete(key)
```

后端：

```text
store.backend: memory | file | redis
BRIDGE_REDIS_URL
```

当前主要用于 Matrix since；history、Turn 状态和完整恢复流程还没有全部接入。

## 测试和静态检查

运行测试：

```powershell
D:\workbach\fork\AgentTeams\.venv\Scripts\python.exe -m pytest -q
```

编译检查：

```powershell
D:\workbach\fork\AgentTeams\.venv\Scripts\python.exe -m compileall -q src
```

Git 格式检查：

```powershell
git diff --check
```

当前测试基线：

```text
18 passed, 1 warning
```

## 当前未完成项

以下能力仍需要生产化完善：

- Matrix 401 专用错误判断和 token 刷新重试
- 完整 since catch-up 语义
- Matrix invite、成员 display name、媒体和加密事件处理
- Gateway chat 提交重试和错误分类
- SSE 断流、timeout 和首事件前后重试语义
- 完整 `agentMd` 动态刷新
- Markdown 安全 HTML sanitizer
- 长消息分片和完整流式策略
- history 持久化和 timeline 重建
- `report-ready`
- 真实 Synapse、MinIO、Gateway、Redis 集成测试
- Docker CLI 入口与 `pyproject.toml` console script 的最终联调
