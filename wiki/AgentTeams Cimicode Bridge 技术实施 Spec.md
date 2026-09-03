# AgentTeams Cimicode Bridge 技术实施 Spec

**版本：v1.0-draft**

**技术栈：Python 3.12+ / asyncio**

**目标：可以直接进入开发**

---

# 1. 项目目标

## 1.1 背景

AgentTeams 当前采用：

```text
Manager
   ↓
Leader
   ↓
Worker × N
```

的多 Agent 协作模型。

现有 Worker 使用常驻 Runtime，同时负责：

- Matrix 协作
- Session 管理
- Agent 执行
- 生命周期
- 心跳等能力

本次将 Worker Runtime 替换成：

```text
AgentTeams
    │
    ▼
cimicode-bridge
    │
    │ HTTP / SSE
    ▼
Cimicode Stateless Gateway
    │
    ▼
Cimicode
    │
    ▼
OpenCode
```

其中：

> Cimicode 是基于 OpenCode 扩展形成的 Agent Runtime。

Bridge 不应该关心 Cimicode/OpenCode 内部如何执行，而只关心统一的：

```text
Session
Turn
RuntimeEvent
```

因此 Bridge 的本质定位是：

```text
AgentTeams Collaboration Protocol
                ↓
          cimicode-bridge
                ↓
      Runtime Abstraction SPI
                ↓
Cimicode / OpenCode / Generic Runtime
```

---

# 2. 设计目标

## 2.1 Python 单进程

基础环境：

```text
Python 3.12+
asyncio
```

主要依赖：

```text
matrix-nio
httpx
httpx-sse
pydantic v2
PyYAML
minio
redis
```

当前需求明确采用 Python 3.12+、asyncio 单进程，并使用 Matrix、HTTP/SSE、S3、Redis 等能力。

---

# 3. Core 设计原则

## 3.1 Core 不得依赖具体 Runtime

禁止：

```python
from cimicode import ...
```

禁止在 Core 大量出现：

```python
if runtime == "cimicode":
    ...
```

正确方式：

```text
Core
 ↓
Runtime SPI
 ↓
Adapter
 ↓
Cimicode
```

---

## 3.2 Core 不依赖基础设施 SDK

例如：

```text
Core
  ✕ matrix-nio
  ✕ redis
  ✕ minio
  ✕ httpx
```

而采用：

```text
Core
 ↓
Port / Protocol
 ↓
Infrastructure Adapter
```

这样才能进行：

```text
Unit Test
Mock
Replace
```

---

# 4. 总体架构

```text
                         AgentTeams
                              │
              ┌───────────────┴───────────────┐
              │                               │
           Manager                          Leader
        当前 Runtime                     当前 Runtime
              │                               │
              └──────────── Team ─────────────┘
                                              │
                                      Matrix @mention
                                              │
             ┌────────────────────────────────┼──────────────┐
             │                                │              │
             ▼                                ▼              ▼
       Bridge Worker A                  Bridge Worker B   Worker N
             │                                │
             ├── Matrix                      ├── Matrix
             ├── Filter                      ├── Filter
             ├── History                     ├── History
             ├── Session                     ├── Session
             ├── Turn                        ├── Turn
             ├── Render                      ├── Render
             └── Runtime SPI                 └── Runtime SPI
                      │
                      ▼
                Runtime Adapter
                      │
             ┌────────┼──────────────┐
             ▼        ▼              ▼
         Cimicode   OpenCode    Generic SSE
             │
             ▼
       Stateless Gateway
             │
             ▼
        Sandbox / Agent
```

---

# 5. Bridge 内部六层

```text
┌─────────────────────────────────────────┐
│              Application                │
│       Lifecycle / Orchestration         │
├─────────────────────────────────────────┤
│                 Core                    │
│ Filter / History / Turn / Session       │
│ Render / Emit / Prompt                  │
├─────────────────────────────────────────┤
│               Protocols                 │
│ Matrix / HTTP / SSE / S3                │
├─────────────────────────────────────────┤
│             Runtime SPI                 │
│ Adapter / Dialect / Auth                │
├─────────────────────────────────────────┤
│               Storage                   │
│ Memory / Redis / File                   │
├─────────────────────────────────────────┤
│              Extensions                 │
│ Cimicode / OpenCode / Generic           │
└─────────────────────────────────────────┘
```

---

# 6. Python 工程结构

```text
cimicode-bridge/
│
├── pyproject.toml
├── Dockerfile
├── Makefile
├── README.md
│
├── scripts/
│   └── bridge-entrypoint.sh
│
├── config/
│   ├── bridge.example.yaml
│   └── scenarios/
│       ├── done.yaml
│       ├── interrupted.yaml
│       ├── timeout.yaml
│       └── error.yaml
│
├── deploy/
│   └── deployment.yaml
│
├── src/
│   └── cimicode_bridge/
│
│       ├── __init__.py
│       ├── main.py
│       ├── app.py
│       ├── bootstrap.py
│       ├── config.py
│       ├── events.py
│       ├── errors.py
│       ├── logging.py
│       └── probes.py
│
│       ├── matrix/
│       │   ├── __init__.py
│       │   ├── client.py
│       │   ├── gateway.py
│       │   ├── events.py
│       │   ├── mention.py
│       │   ├── sender.py
│       │   └── models.py
│
│       ├── core/
│       │   ├── filter.py
│       │   ├── roles.py
│       │   ├── history.py
│       │   ├── turn.py
│       │   ├── session.py
│       │   ├── render.py
│       │   ├── emitter.py
│       │   ├── system_prompt.py
│       │   └── lifecycle.py
│
│       ├── runtime/
│       │   ├── base.py
│       │   ├── client.py
│       │   ├── registry.py
│       │   ├── models.py
│       │   ├── auth.py
│       │
│       │   ├── adapters/
│       │   │   ├── cimicode.py
│       │   │   ├── opencode.py
│       │   │   └── generic.py
│       │
│       │   └── dialects/
│       │       ├── cimicode.py
│       │       ├── opencode.py
│       │       └── generic.py
│
│       ├── store/
│       │   ├── base.py
│       │   ├── memory.py
│       │   ├── redis.py
│       │   └── file.py
│
│       └── config_sources/
│           ├── base.py
│           └── s3.py
│
└── tests/
    ├── unit/
    ├── contract/
    ├── mock_runtime/
    ├── scenarios/
    └── integration/
```

原设计已经给出了类似的目录划分；这里进一步明确 Infrastructure / SPI / Core 的依赖边界。

---

# 7. 核心领域模型

## 7.1 MatrixMessage

```python
class MatrixMessage(BaseModel):
    event_id: str
    room_id: str
    sender: str
    sender_display_name: str | None = None
    body: str
    timestamp: int | None = None
    mentions: list[str] = Field(
        default_factory=list
    )
```

---

# 8. Runtime SPI

这是最核心的扩展层。

```python
class RuntimeAdapter(Protocol):

    name: str

    async def health(self) -> HealthStatus:
        ...

    async def create_session(
        self,
        request: CreateSessionRequest,
    ) -> SessionInfo:
        ...

    async def chat(
        self,
        request: ChatRequest,
    ) -> AsyncIterator[RuntimeEvent]:
        ...

    async def destroy_session(
        self,
        session_id: str,
    ) -> None:
        ...

    def capabilities(
        self,
    ) -> RuntimeCapabilities:
        ...
```

Core 永远只看到：

```text
RuntimeAdapter
```

---

# 9. Runtime Models

## 9.1 CreateSessionRequest

```python
class CreateSessionRequest(BaseModel):
    owner_id: str
    template_id: str | None = None
    skills: list[str] = Field(
        default_factory=list
    )
    idempotency_key: str
```

---

## 9.2 ChatRequest

```python
class ChatRequest(BaseModel):
    session_id: str
    sandbox_id: str | None
    turn_id: str

    agent_md: str
    history: list[HistoryMessage]
    user_message: str
```

当前 Gateway v0.2 已经采用这一类请求结构，并规定 `agentMd / history / userMessage` 由 Gateway 透传。

---

# 10. RuntimeEvent

Bridge 内部只认 RuntimeEvent。

```python
class RuntimeEvent(BaseModel):
    seq: int | None = None
    kind: RuntimeEventKind
    text: str = ""
    data: dict[str, Any] = Field(
        default_factory=dict
    )
```

枚举：

```python
class RuntimeEventKind(str, Enum):

    TURN_STARTED = "turn_started"

    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"

    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"

    ARTIFACT_PUBLISHED = "artifact_published"

    TURN_COMPLETED = "turn_completed"

    TURN_INTERRUPTED = "turn_interrupted"

    RUNTIME_ERROR = "runtime_error"
```

---

# 11. EventDialect

```python
class EventDialect(Protocol):

    name: str

    async def translate(
        self,
        raw_event: RawRuntimeEvent,
        context: DialectContext,
    ) -> list[RuntimeEvent]:
        ...

    def finalize(
        self,
        context: DialectContext,
    ) -> RuntimeEvent:
        ...
```

---

# 12. Cimicode Dialect

当前版本支持：

```text
message.part.delta
message.part.updated
message.updated
done
session.error
```

映射：

```text
message.part.delta
    ↓
text_delta

message.part.updated
    ↓
text_delta

message.updated
    ↓
text_delta

done
    ↓
turn_completed

session.error
    ↓
runtime_error
```

这一事件面已经在当前设计中明确。

---

# 13. PartBuffer

Cimicode 不能使用简单的一对一 Event Mapping，因此需要独立的 PartBuffer。

```python
class PartBuffer:

    parts: dict[str, str]
    order: list[str]

    def append_delta(
        self,
        part_id: str,
        delta: str,
    ) -> None:
        ...

    def replace(
        self,
        part_id: str,
        text: str,
    ) -> None:
        ...

    def render(self) -> str:
        ...
```

规则：

```text
delta
    → append

updated
    → replace

done
    → 按 part order 合并
```

当前方案已经明确这一机制。

---

# 14. Generic Runtime

必须支持：

```yaml
runtime:
  adapter: generic-sse

  base_url: http://runtime:8080

  auth:
    type: header
    header: X-API-Key
    token_env: RUNTIME_AUTH_TOKEN

  session:
    create:
      method: POST
      path: /v1/sessions

      body:
        owner: "{owner_id}"

    id_pointer: $.session_id

  turn:
    submit:
      method: POST
      path: /v1/sessions/{session_id}/turns

      body:
        input:
          text: "{text}"

  events:
    mapping:
      text_delta:
        match:
          type: message.delta

        text_pointer: $.text

      turn_completed:
        match:
          type: turn.completed
```

要求：

> 接入简单的第三方 HTTP+SSE Runtime，只改配置，不改 Python。

这是整个 Runtime 扩展能力的关键验收项。

---

# 15. Python Plugin Adapter

Generic 不能处理的 Runtime：

```text
复杂状态机
跨事件聚合
特殊 SSE
自定义签名
复杂鉴权
```

允许：

```yaml
runtime:
  adapter: import:cimicode_bridge.plugins.foo:FooAdapter
```

支持：

```text
import path
entry_points
```

---

# 16. HttpSseRuntime

统一 HTTP/SSE 引擎：

```python
class HttpSseRuntime:

    async def request(...):
        ...

    async def stream_sse(...):
        ...
```

负责：

```text
HTTP
SSE
Timeout
Error Mapping
Auth Injection
Submit Retry
Cancel
```

不负责：

```text
Matrix
History
Prompt
Markdown
Mention
```

---

# 17. HTTP Timeout

默认：

```text
connect = 10s
write   = 30s
read    = turn.timeout
```

---

# 18. Retry Model

## 18.1 提交阶段

还没有收到任何 SSE Event：

```text
connection error
429
5xx
```

允许：

```text
最多 3 次
指数退避
```

---

## 18.2 流式阶段

一旦：

```text
收到第一条 SSE Event
```

之后：

```text
禁止自动 retry
```

断流：

```text
INTERRUPTED
    ↓
Matrix notice
    ↓
等待重新 @
```

原因是避免重复执行 Runtime 副作用。现有方案将该规则列为最重要的可靠性约束之一。

---

# 19. SessionManager

```python
class SessionManager:

    async def ensure_session(
        self,
        agent_id: str,
    ) -> SessionInfo:
        ...

    async def get_session(...):
        ...

    async def invalidate(...):
        ...

    async def close(...):
        ...
```

---

# 20. Session State

StateStore：

```text
session:{agent_id}
```

TTL：

```text
30d
```

Session 404：

```text
invalidate
    ↓
create
    ↓
当前 Turn 单次重试
```

---

# 21. Turn Manager

Session 内必须串行：

```text
Session A

Turn 1
   ↓
Turn 2
   ↓
Turn 3
```

默认：

```yaml
queue:
  max_pending: 8
  overflow: buffer
```

Bridge 不依赖服务端 FIFO。

当前需求也明确要求 per-Session 串行队列。

---

# 22. Turn State Machine

```text
PENDING
   │
   ▼
SUBMITTING
   │
   ▼
STREAMING
   │
   ├──────────────┐
   │              │
   ▼              ▼
COMPLETED      INTERRUPTED
   │
   ▼
RECORDED
```

异常：

```text
SUBMITTING
    ↓
FAILED
```

---

# 23. History Store

```python
class HistoryStore(Protocol):

    async def append_user(...):
        ...

    async def append_assistant(...):
        ...

    async def snapshot(...):
        ...

    async def rebuild(...):
        ...
```

数据：

```python
class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    event_id: str | None = None
```

默认：

```text
max_entries = 200
```

规则：

```text
FIFO
event_id 去重
拒绝消息不写
assistant 完成时写入
interrupted 是否写入由配置决定
```

当前方案已经完成从“两段式 buffer”到 flat history 的设计修订。

---

# 24. Chat Request 组装

最终：

```python
ChatRequest(
    session_id=session.session_id,
    sandbox_id=session.sandbox_id,
    turn_id=event.event_id,

    agent_md=system_prompt,

    history=history_snapshot,

    user_message=(
        f"{sender_name}: {event.body}"
    ),
)
```

语义：

```text
agentMd
    System / Instructions

history
    Context

userMessage
    Current Command
```

---

# 25. Matrix Adapter

Bridge 需要一个独立 Matrix Port：

```python
class MatrixPort(Protocol):

    async def sync(...):
        ...

    async def whoami(...):
        ...

    async def send_message(...):
        ...

    async def get_history(...):
        ...
```

Matrix SDK 只存在 Infrastructure 层。

---

# 26. Matrix Token

Bridge：

```text
不执行密码登录
```

唯一来源：

```text
AGENTTEAMS_WORKER_MATRIX_TOKEN
```

启动：

```text
Token
 ↓
whoami
 ↓
success
 ↓
ready
```

401：

```text
Controller
 ↓
POST /api/v1/credentials/matrix-token
 ↓
新 token
 ↓
whoami
 ↓
恢复 sync
```

Token/device_id：

```text
只存内存
```

不进入 StateStore。

这是当前评审后的确定规则。

---

# 27. Matrix Sync

```text
START
 ↓
LOAD_SINCE
 ↓
CATCH_UP
 ↓
READY
 ↓
SYNC LOOP
```

State：

```text
matrix:since:{agent_id}
```

默认：

```yaml
matrix:
  since:
    persist: true
```

---

# 28. Message Filter

顺序固定：

```text
1. 自己发送 → ignore
2. 非文本 → media policy
3. requireMention
4. RoleResolver
5. groupAllowFrom
6. unknown
7. mention parse
8. trigger
```

默认：

```yaml
filter:
  require_mention: true

  group_allow_from:
    worker:
      - leader
      - admin
      - human

  allow_unknown: false
```

---

# 29. Peer Mention

默认：

```text
Worker A → Worker B
```

不能直接触发。

必须：

```text
Worker A
    ↓
Leader
    ↓
Worker B
```

---

# 30. Mention Parser

三级检测：

```text
1. m.mentions
2. matrix.to
3. text regex
```

容错：

```text
@agent:domain
@agent
@ agent
尾部空格
displayname
```

原则：

```text
宁漏勿误
```

---

# 31. SystemPrompt

最终：

```text
agentMd
 ├── coordination
 ├── AGENTS.md
 └── SOUL.md
```

默认：

```yaml
system_prompt:
  parts:
    - coordination
    - agents_md
    - soul_md
```

---

# 32. Coordination Context

环境变量：

```text
COORDINATION_ROLE
COORDINATION_LEADER
COORDINATION_TEAM
COORDINATION_ROOM
COORDINATION_ADMIN
COORDINATION_WORKERS
```

Builder：

```python
class CoordinationContextBuilder:

    def build(
        self,
        context: CoordinationContext,
    ) -> str:
        ...
```

---

# 33. AGENTS.md

启动：

```text
S3
 ↓
本地缓存
```

每次 Turn：

```text
agentMd 携带
```

可选动态刷新：

```yaml
config:
  refresh_interval: 0
```

当：

```text
refresh_interval > 0
```

每次 Turn 前重新获取。

---

# 34. S3 ConfigSource

```python
class ConfigSource(Protocol):

    async def get(
        self,
        path: str,
    ) -> bytes:
        ...
```

默认：

```text
S3ConfigSource
```

未来：

```text
FileConfigSource
HttpConfigSource
K8sConfigSource
```

Core 不认识 Minio。

---

# 35. StateStore

```python
class StateStore(Protocol):

    async def get(
        self,
        key: str,
    ) -> bytes | None:
        ...

    async def set(
        self,
        key: str,
        value: bytes,
        ttl: int | None = None,
    ) -> None:
        ...

    async def delete(
        self,
        key: str,
    ) -> None:
        ...
```

实现：

```text
Memory
Redis
File
```

生产：

```text
Redis
```

本地：

```text
Memory
```

调试：

```text
File
```

---

# 36. State Key

第一版：

```text
session:{agent_id}
matrix:since:{agent_id}
```

不持久化：

```text
Matrix token
```

---

# 37. Runtime Adapter Registry

```python
class AdapterRegistry:

    def register(
        self,
        name: str,
        factory: AdapterFactory,
    ) -> None:
        ...

    def resolve(
        self,
        name: str,
    ) -> RuntimeAdapter:
        ...
```

内置：

```text
cimicode
opencode
generic-sse
```

插件：

```text
entry_points
```

---

# 38. Render Pipeline

```text
RuntimeEvent
      ↓
Aggregate
      ↓
Markdown
      ↓
HTML
      ↓
Sanitize
      ↓
Matrix Event
```

---

# 39. HTML Sanitizer

允许：

```text
p
br
pre
code
ul
ol
li
blockquote
h1-h6
strong
em
del
a[href]
table
thead
tbody
tr
th
td
hr
```

禁止：

```text
script
style
on*
javascript:
```

转换异常：

```text
formatted_body
    ↓
discard

body
    ↓
send plain text
```

---

# 40. Thinking / Tool / Artifact

默认：

```yaml
emitter:
  format:
    thinking: hide
    tools: compact
    components: placeholder
```

Thinking：

```text
hide
quote
show
```

Tool：

```text
hide
compact
show
```

Artifact：

```text
[组件: xxx]

artifact: id
```

---

# 41. NO_REPLY

默认：

```text
NO_REPLY
```

判断：

```python
text.strip() == "NO_REPLY"
```

命中：

```text
不发送 Matrix
Turn 正常结束
状态正常更新
```

---

# 42. Matrix 三层 Mention

Bridge 发给 Leader：

### 第一层

```text
body

@leader:example.com
```

### 第二层

```text
formatted_body

<matrix.to href="@leader:example.com">
...
</matrix.to>
```

### 第三层

```json
{
  "m.mentions": {
    "user_ids": [
      "@leader:example.com"
    ]
  }
}
```

当前 Leader 收侧依赖结构化 mention，因此三层必须一起写。

---

# 43. Streaming

支持：

```text
complete
chunked
throttled
```

默认：

```yaml
emitter:
  streaming:
    mode: complete
    throttle: 3s
    min_chunk_chars: 40
```

---

# 44. Lifecycle

```text
Phase 0
S3 Config
    ↓
Phase 1
Matrix Token
Whoami
Sync
    ↓
Phase 2
Runtime Health
Ensure Session
    ↓
Phase 3
report-ready
    ↓
Phase 4
Main Sync Loop
```

这是当前方案确定的启动顺序。

---

# 45. HTTP Probes

必须：

```text
GET /healthz
GET /readyz
GET /status
```

### healthz

判断：

```text
进程存活
```

### readyz

判断：

```text
Matrix
Runtime
Session
```

### status

返回：

```json
{
  "worker": "agent-a",
  "role": "worker",
  "phase": "running",
  "matrix_connected": true,
  "runtime_healthy": true,
  "last_turn_completed_at": "...",
  "current_turn_id": null
}
```

---

# 46. Shutdown

```text
SIGTERM
 ↓
Stop accepting new Turn
 ↓
Cancel active Turn
 ↓
Destroy Session
 ↓
Flush State
 ↓
Exit
```

Bridge：

```text
grace = 20s
```

K8s：

```text
terminationGracePeriodSeconds = 30
```

---

# 47. Unified Error Model

不要让 Core 感知底层异常。

统一：

```python
class BridgeError(Exception):
    ...


class RuntimeAuthError(BridgeError):
    ...


class RuntimeRateLimited(BridgeError):
    ...


class RuntimeSessionExpired(BridgeError):
    ...


class RuntimeTransportError(BridgeError):
    ...


class RuntimeInterrupted(BridgeError):
    ...


class RuntimeProtocolError(BridgeError):
    ...
```

---

# 48. Error Mapping

```text
401 / 403
    → RuntimeAuthError

404
    → RuntimeSessionExpired

429
    → RuntimeRateLimited

5xx
    → RuntimeTransportError

EOF
    → RuntimeInterrupted

timeout
    → RuntimeInterrupted
```

---

# 49. Unknown Runtime Event

未知事件：

```text
不能静默丢弃
```

记录：

```python
RuntimeEvent(
    kind=RuntimeEventKind.RUNTIME_ERROR,
    data={
        "raw_event": raw_event
    }
)
```

并记录结构化日志：

```text
runtime_unknown_event
```

---

# 50. Adapter Capability

Runtime Adapter 必须声明：

```python
class RuntimeCapabilities(BaseModel):

    supports_session_destroy: bool = False
    supports_interrupt_event: bool = False
    supports_artifact: bool = False
    supports_streaming: bool = True
```

未来可以继续扩展：

```text
supports_tools
supports_system_prompt
supports_history
supports_cancel
supports_daily_reset
```

Core 根据 capability 进行降级，而不是判断：

```text
if cimicode
```

---

# 51. Configuration Schema

采用 Pydantic：

```python
class BridgeConfig(BaseModel):

    matrix: MatrixConfig
    filter: FilterConfig
    history: HistoryConfig

    runtime: RuntimeConfig

    events: EventConfig
    emitter: EmitterConfig

    system_prompt: SystemPromptConfig

    config: ConfigRefreshConfig
    store: StoreConfig

    bootstrap: BootstrapConfig
    lifecycle: LifecycleConfig
    shutdown: ShutdownConfig
```

---

# 52. 配置优先级

严格：

```text
Built-in Default
       ↓
S3 openclaw.json
       ↓
Environment
```

即：

```text
default < config < env
```

---

# 53. openclaw.json

Bridge 主配置：

```json
{
  "bridge": {

    "runtime": {
      "adapter": "cimicode",
      "base_url": "http://cimicode-gateway",
      "template_id": "coding-worker"
    },

    "events": {
      "dialect": "cimicode"
    },

    "queue": {
      "max_pending": 8,
      "overflow": "buffer"
    },

    "turn": {
      "timeout": "10m",
      "submit_max_retries": 3
    }
  }
}
```

当前评审已经确定，不新增独立 `bridge.yaml` 作为生产主配置，而是在 `openclaw.json` 增加 `bridge` 扩展段。

---

# 54. 推荐默认配置

```yaml
matrix:
  credentials:
    token_env: AGENTTEAMS_WORKER_MATRIX_TOKEN

  since:
    persist: true

  sync:
    timeout: 30s

  e2ee: off

filter:
  require_mention: true

  group_allow_from:
    worker:
      - leader
      - admin
      - human

  allow_unknown: false

history:
  max_entries: 200
  record_interrupted: false
  persist: false
  rebuild_limit: 50

runtime:
  adapter: cimicode

  auth:
    type: none

  turn:
    timeout: 10m
    submit_max_retries: 3

  queue:
    max_pending: 8
    overflow: buffer

emitter:
  no_reply:
    mode: trim

  streaming:
    mode: complete

    throttle: 3s
    min_chunk_chars: 40

  format:
    markdown_html: true
    thinking: hide
    tools: compact
    components: placeholder

  mention:
    three_layer: true

system_prompt:
  parts:
    - coordination
    - agents_md
    - soul_md

config:
  refresh_interval: 0

store:
  backend: memory
```

---

# 55. Mock Runtime

Mock Runtime 是第一阶段就必须完成的。

接口：

```text
GET  /health
POST /v1/gateway/session/create
POST /v1/gateway/session/chat
POST /v1/gateway/session/destroy
```

Chat 返回：

```text
SSE
```

---

# 56. Scenario YAML

例如：

```yaml
name: normal_done

events:

  - event: message.part.delta

    data:
      part_id: p1
      delta: "hello"

  - event: message.part.delta

    data:
      part_id: p1
      delta: " world"

  - event: done

    data: {}
```

---

# 57. Mock Failure Injection

必须支持：

```text
401
403
404
429
500
SSE EOF
network error
timeout
malformed JSON
unknown event
event sequence disorder
slow response
```

这样真实 Cimicode 尚未 ready 时，Bridge 也可以继续开发。

---

# 58. Contract Test

每个 Adapter 至少需要：

```text
Request Contract
Response Contract
SSE Contract
Error Contract
Idempotency Contract
```

例如验证：

```text
sessionId
sandboxId
turnId
agentMd
history
userMessage
```

全部必须断言。

---

# 59. Generic Runtime 验收

创建一个与 Cimicode/OpenCode 都不同的第三服务：

```text
POST /sessions
POST /sessions/{id}/turns
SSE:
  delta
  completed
```

要求：

```text
只改 YAML
不增加 Python Adapter
```

必须能够：

```text
Generic Runtime
     ↓
Bridge
     ↓
Matrix
```

正常运行。

---

# 60. Unit Test

## Filter

```text
不 @
正确 @
自发消息
Leader
Admin
Human
Worker
Unknown
```

## Mention

```text
标准
无 domain
@ 后空格
尾部空格
displayname
非法格式
```

## History

```text
FIFO
200 entries
event dedup
拒绝消息不记录
assistant record
interrupted
rebuild
snapshot
```

## Runtime

```text
create
reuse
chat
404 recreate
retry
timeout
EOF
destroy
```

## Render

```text
Markdown
HTML sanitize
NO_REPLY
thinking
tool
artifact
mention
```

---

# 61. Integration Test

环境：

```text
Synapse
Redis
Mock Runtime
Bridge
```

---

# 62. E2E

必须：

```text
E2E-1
单 Agent 响应

E2E-2
Leader → Worker

E2E-4
Interrupted

E2E-5
Restart Recovery

E2E-6
Leader Runtime ↔ Bridge Worker
```

其中 E2E-6 是当前生产主拓扑的关键测试。

---

# 63. 日志

采用：

```text
JSON Structured Logging
```

字段：

```json
{
  "timestamp": "...",
  "level": "INFO",
  "component": "turn",
  "worker": "agent-a",
  "room_id": "...",
  "session_id": "...",
  "turn_id": "...",
  "event": "turn_completed"
}
```

---

# 64. 必须记录的事件

```text
bootstrap_started
bootstrap_completed

matrix_whoami
matrix_token_refresh
matrix_sync_started
matrix_sync_reconnected

message_received
message_filtered
message_triggered

session_create
session_reuse
session_expired
session_recreated

turn_queued
turn_submitted
turn_started
turn_completed
turn_interrupted
turn_failed

runtime_event_received
runtime_unknown_event

matrix_send
matrix_send_failed

shutdown_started
shutdown_completed
```

---

# 65. 禁止日志内容

禁止：

```text
Matrix Token
Controller Token
S3 Secret Key
Runtime Token
Authorization Header
```

也不建议打印：

```text
完整 agentMd
完整 history
完整 userMessage
```

应该只记录：

```text
长度
hash
ID
```

---

# 66. Metrics

第一阶段只做日志。

架构预留：

```text
/metrics
```

未来增加：

```text
turn_total
turn_success_total
turn_interrupted_total
turn_latency
runtime_http_errors
matrix_messages_received
matrix_messages_filtered
queue_depth
session_create_total
session_recreate_total
```

---

# 67. Docker

基础：

```text
python:3.12-slim
```

虚拟环境：

```text
/opt/venv/bridge
```

不包含：

```text
Node
npm
mc
libolm
```

目标：

```text
150–200MB
```

---

# 68. Kubernetes

默认：

```yaml
replicas: 1
```

原因：

```text
一个 Agent
一个 Bridge
一个 Session
```

不允许多个 Pod 共享同一个 Agent Identity。

---

# 69. Resource Recommendation

```yaml
resources:

  requests:
    cpu: 100m
    memory: 128Mi

  limits:
    cpu: 1
    memory: 512Mi
```

---

# 70. Operator 接入

Operator 增加：

```text
runtime = cimicode-bridge
```

映射：

```text
agentteams-cimicode-bridge:${VERSION}
```

---

# 71. Operator Env

```text
AGENTTEAMS_WORKER_NAME
AGENTTEAMS_WORKER_CR_NAME

AGENTTEAMS_FS_ACCESS_KEY
AGENTTEAMS_FS_SECRET_KEY
AGENTTEAMS_FS_ENDPOINT
AGENTTEAMS_FS_BUCKET
AGENTTEAMS_STORAGE_PREFIX

AGENTTEAMS_MATRIX_URL
AGENTTEAMS_MATRIX_DOMAIN

AGENTTEAMS_WORKER_MATRIX_TOKEN
AGENTTEAMS_WORKER_ROOM_ID

AGENTTEAMS_CONTROLLER_URL
AGENTTEAMS_AUTH_TOKEN

COORDINATION_ROLE
COORDINATION_LEADER
COORDINATION_TEAM
COORDINATION_ROOM
COORDINATION_ADMIN
COORDINATION_WORKERS

BRIDGE_REDIS_URL
```

这些对应当前平台侧 T1–T18 的主要要求。

---

# 72. Startup Tolerance

S3：

```text
6 × 5s
```

Runtime：

```text
无限 exponential backoff
```

Matrix：

```text
401 → refresh
```

---

# 73. Recoverable / Fatal

Recoverable：

```text
S3 暂时不可用
Runtime 503
Matrix 401
Redis unavailable
SSE network error
```

Fatal：

```text
配置语法不可解析
配置 schema 不合法
核心依赖初始化完全失败
```

---

# 74. Runtime API 变更隔离

假设：

```text
Cimicode v0.2
```

升级：

```text
Cimicode v0.3
```

不允许污染 Core。

应该：

```text
cimicode_v03.py
        ↓
RuntimeEvent
        ↓
Core
```

即：

```text
External Contract
       ↓
Adapter / Dialect
       ↓
Internal Contract
       ↓
Core
```

---

# 75. Open Questions / TODO

所有外部不确定项统一进入此章节。

## TODO-01 Gateway API

确认：

```text
create
chat
destroy
```

最终 URL。

---

## TODO-02 Create Request

确认：

```text
templateId
skills
idempotencyKey
ownerId
metadata
```

最终字段。

---

## TODO-03 templateId

确认：

```text
来源
格式
生命周期
是否固定
```

---

## TODO-04 ownerId

候选：

```text
matrix_id
agent_name
team_agent
```

当前默认：

```text
matrix_id
```

---

## TODO-05 sandboxId

确认：

```text
create 是否返回
chat 是否必需
destroy 是否需要
生命周期
```

---

## TODO-06 destroy API

确认：

```text
是否存在
是否幂等
同步还是异步
```

不存在则：

```text
no-op
```

---

## TODO-07 SSE Schema

确认：

```text
event
data
event_seq
part_id
message_id
```

---

## TODO-08 EOF 语义

确认：

```text
EOF = success
```

还是：

```text
EOF = interrupted
```

Bridge 默认：

```text
未收到 completed → interrupted
```

---

## TODO-09 Cimicode Error Codes

确认：

```text
quota
sandbox unavailable
model unavailable
invalid request
session expired
```

---

## TODO-10 Auth

当前：

```text
none
```

未来改成：

```text
AuthProvider
```

无需改 Core。

---

## TODO-11 History Token Limit

当前：

```text
200 entries
```

未来确认：

```text
是否采用 token budget
```

---

## TODO-12 History Persistence

确认：

```text
生产环境是否需要 Redis 持久化 history
```

---

## TODO-13 E2EE

第一版：

```text
off
```

---

## TODO-14 Artifact

确认：

```text
artifact_id
file_id
下载
跨 Session
```

---

## TODO-15 Leader Probe

确认 `/status` 最终访问：

```text
Service DNS
localhost
kubectl exec
sidecar
```

---

## TODO-16 Coordination Double Write

完成迁移后：

```text
AGENTS.md
```

不再注入 Coordination Block。

统一：

```text
COORDINATION_*
```

---

## TODO-17 Cimicode AGENTS.md

需要最终提供：

```text
Cimicode Worker Template
```

去掉：

```text
OpenClaw 专属指令
mcporter
mc mirror
hiclaw-sync
```

---

## TODO-18 Operator

最终确认：

```text
CRD
image
env
resource
probe
termination
```

---

# 76. TODO 的重要原则

TODO 不能阻塞：

```text
Core
Matrix
History
Render
Generic Runtime
Mock Runtime
Unit Test
Contract Test
```

只能阻塞：

```text
真实 Cimicode 联调
```

即：

```text
                 TODO
                  │
        ┌─────────┴──────────┐
        │                    │
    不阻塞开发             阻塞联调
        │                    │
  Mock / Generic         Real Cimicode
  Unit / Contract        E2E
```

---

# 77. 实施顺序

## Phase 0：Skeleton

完成：

```text
B1

pyproject
config
logging
errors
SPI
docker
probes
```

---

## Phase 1：Mock Runtime

先完成：

```text
Mock Runtime
Generic SSE
RuntimeEvent
Contract Test
```

此时 Bridge 已经拥有：

```text
Runtime Abstraction
```

---

## Phase 2：Matrix

完成：

```text
B2
B3
B4
```

包括：

```text
Token
Whoami
Sync
Mention
Filter
Send
```

---

## Phase 3：History

完成：

```text
B5
B6
```

---

## Phase 4：Turn

完成：

```text
B8
B9
```

形成：

```text
Matrix
 ↓
Filter
 ↓
History
 ↓
Turn
 ↓
Mock Runtime
 ↓
RuntimeEvent
```

---

## Phase 5：Render

完成：

```text
B10
B11
```

---

## Phase 6：Cimicode

最后才接：

```text
cimicode adapter
cimicode dialect
```

此时 Cimicode 只是：

```text
Adapter
+
Dialect
```

而不是整个 Bridge 的架构基础。

---

# 78. 第一版只应该重点实现哪些代码

```text
src/cimicode_bridge/

application/
    app.py
    bootstrap.py

core/
    filter.py
    history.py
    session.py
    turn.py
    render.py
    emitter.py
    system_prompt.py

matrix/
    gateway.py
    mention.py
    sender.py

runtime/
    base.py
    client.py
    registry.py

    adapters/
        cimicode.py
        generic.py

    dialects/
        cimicode.py
        generic.py

store/
    base.py
    memory.py
    redis.py

config_sources/
    base.py
    s3.py

events.py
errors.py
config.py
probes.py
```

---

# 79. 第一阶段核心调用链

```text
Matrix /sync
      │
      ▼
MatrixMessage
      │
      ▼
MessageFilter
      │
      ▼
MentionResolver
      │
      ▼
HistoryStore
      │
      ▼
SystemPromptBuilder
      │
      ▼
TurnManager
      │
      ▼
SessionManager
      │
      ▼
RuntimeAdapter
      │
      ▼
HttpSseRuntime
      │
      ▼
SSE
      │
      ▼
EventDialect
      │
      ▼
RuntimeEvent
      │
      ▼
Renderer
      │
      ▼
Emitter
      │
      ▼
Matrix
```

---

# 80. 关键依赖关系

必须保持：

```text
core
 ↓
SPI
 ↓
adapter
```

不能：

```text
core
 ↓
cimicode
```

同时：

```text
render
 ↓
RuntimeEvent
```

不能：

```text
render
 ↓
Cimicode Raw Event
```

---

# 81. 最终验收

最终必须完成：

```text
Leader
   │
   │ @frontend-agent
   ▼
Matrix
   │
   ▼
Bridge
   │
   ├── Mention
   ├── Filter
   ├── History
   ├── System Prompt
   │
   ▼
RuntimeAdapter
   │
   ▼
Cimicode
   │
   ▼
SSE
   │
   ▼
RuntimeEvent
   │
   ├── Aggregate
   ├── Render
   ├── NO_REPLY
   ├── Mention
   │
   ▼
Matrix
   │
   ▼
Leader
```

同时：

```text
Cimicode 断流
    ↓
Interrupted
    ↓
不自动重试
```

Bridge 重启：

```text
Restart
 ↓
Session Recover
 ↓
Since Recover
 ↓
History Recover
```

---

# 82. 最终架构定位

整个系统最终应该形成：

```text
                    AgentTeams
                        │
                        │
                 Collaboration
                        │
                        ▼
               cimicode-bridge
                        │
             ┌──────────┼──────────┐
             │          │          │
          Matrix       Core    Runtime SPI
                        │          │
                        │     ┌────┼─────┐
                        │     ▼    ▼     ▼
                        │   Cimi Open Generic
                        │    │   Code   SSE
                        │
                        ▼
                  RuntimeEvent
```

最终职责边界：

```text
AgentTeams
    = 多 Agent 编排与协作平台

cimicode-bridge
    = AgentTeams 与 Runtime 的解耦层

Cimicode
    = Agent Runtime

OpenCode
    = Cimicode 的基础能力来源
```

最终目标不是：

> “做一个 Cimicode Client。”

而是：

> **做一个可插拔的 AgentTeams Runtime Bridge。**

Cimicode 是第一实现，OpenCode 是第二实现，Generic SSE 和 Python Plugin 则保证未来可以接入任意满足 Runtime SPI 的服务。