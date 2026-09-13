# Runtime Adapter 传输契约（cimicode-stateless / cimicode-pod）

**版本** v1.0（2026-09-13）· 对应实现：`worker-bridge/bridge/src/cimicode_bridge/runtime/`

两种 adapter 形态共享 Runtime SPI（`chat(*, session_id, sandbox_id, turn_id,
agent_md, history, user_message) -> list[RuntimeEvent]`，base.py），传输层已分化：

| | cimicode-stateless | cimicode-pod |
|---|---|---|
| 对端 | 外部 cimicode 平台 gateway | operator 供给的 cimicode pod（换皮 opencode） |
| 传输 | HTTP + SSE（`POST /v1/gateway/session/chat`） | opencode REST + 轮询 |
| agent.md | 请求字段（agentMd） | 每 turn 经 helper 文件推送（`POST :4097/agents-md`） |
| 会话 | 预建绑定（runtime.yaml bridge 段 sessionId/sandboxId） | adapter 自持（404 自愈重建） |
| 流式 | SSE 事件流 | 否（turn 结束出全文 + progress_texts） |

## cimicode-pod REST 契约（按 opencode 1.18.27 标定）

端口（镜像契约，`worker-bridge/cimicode-runtime`）：

| 端口 | 服务 | 端点 |
|---|---|---|
| 4096 | `opencode serve` | `POST /session`（创建，返回顶层 `id`）；`GET /session`（列表/健康）；`GET /session/{id}`；`POST /session/{id}/message`（**服务端阻塞整 turn**）；`GET /session/{id}/message`（消息列表） |
| 4097 | sandbox helper | `POST /agents-md`（body=markdown 原文，原子写工作目录 AGENTS.md）；`POST /exec`（bash 工具转发执行）；`GET /healthz` |

turn 生命周期（bridge `CimicodePodAdapter.chat`）：

1. **推 agent.md**：`POST {helper_url}/agents-md`（helper_url 缺失 → RUNTIME_ERROR，
   fail-loud——不静默用旧系统指令跑 turn）；
2. **会话自愈**：入参 session_id（pod 形态恒空）或自持 id `GET` 探活，404 →
   `POST /session` 重建（pod 重建后会话目录丢失是预期内场景）；
3. **baseline**：`GET .../message` 取最后一条 assistant 消息 id；
4. **提交**：`POST .../message` body `{"parts":[{"type":"text","text":...}]}`
   （独立长超时 = turn 超时 + 30s）；
5. **轮询**：`GET .../message` 直到 baseline 之后出现带 `info.time.completed`
   的 assistant 消息；`info.error`（`error.data.message`）→ RUNTIME_ERROR；
   轮询窗口耗尽 → TURN_INTERRUPTED；
6. **产出**：`[TEXT_DELTA(全文), TURN_COMPLETED]`（与 stateless 事件形态同构）；
   turn 中途的 assistant 插话随 `TURN_COMPLETED.data.progress_texts` 透出。

SSE 在近期 opencode 版本不可靠——轮询是标定结论，勿回退。

## 接线（Worker CR spec.env → bridge 自愈轮询）

operator patch 三键（bridge 经 controller runtimeEnv 读取，或 pod env 直接注入）：

```
BRIDGE_RUNTIME_ADAPTER=cimicode-pod
BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096
BRIDGE_RUNTIME_HELPER_URL=http://<w>-cimicode-svc.<ns>.svc:4097
```

会话门禁（app.py 三处 `== "cimicode-stateless"`）语义是"仅 stateless 要求预建
绑定"——pod 形态免预建，勿放宽为通用检查。

## 已知限制（生产化备忘）

- **会话数据目录随 pod 重建丢失**：emptyDir 保容器重启；pod 重建由 404 自愈 +
  每 turn 重推 AGENTS.md + taskflow `mc pull` 恢复。PVC 化是生产化步骤。
- **AGENTS.md 变更在新建会话时生效**（opencode 从 cwd 读取的时机）；团队名单
  变化未生效时兜底 = 每 turn 新建 session（未启用，需要时一行切换）。
- **ripgrep 必装**（镜像已含）：opencode skill 工具 shell 出 rg，缺则每次挂 130s。
