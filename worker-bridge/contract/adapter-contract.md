# Runtime Adapter 传输契约（cimicode-stateless / cimicode-pod 统一形态）

**版本** v1.1（2026-09-16）· 对应实现：`worker-bridge/bridge/src/cimicode_bridge/runtime/`
（adapter 层）、`worker-bridge/cimicode-runtime/` + `worker-bridge/opencode-runtime/`
（双 runtime 镜像）、`worker-bridge/operator/`（供给与模型注入）

**v1.1 变更**（自 v1.0，2026-09-13）：helper 通道（:4097 `/agents-md` `/exec`）
整体退役——agent.md 改走消息体 `system` 字段原生通道；端口收敛为单 4096；
emptyDir 移除（/workspace=容器可写层）；模型改经 operator 注入
`OPENCODE_CONFIG_CONTENT`（镜像零凭据）；Worker env patch 收敛为两键。

## 两种 adapter 形态（共享 Runtime SPI）

两种 adapter 形态共享 Runtime SPI（`chat(*, session_id, sandbox_id, turn_id,
agent_md, history, user_message) -> list[RuntimeEvent]`，base.py），传输层已分化：

| | cimicode-stateless | cimicode-pod |
|---|---|---|
| 对端 | 外部 cimicode 平台 gateway | operator 供给的 runtime pod（cimicode 或 opencode） |
| 传输 | HTTP + SSE（`POST /v1/gateway/session/chat`） | opencode REST + 轮询（单端口） |
| agent.md | 请求字段（agentMd） | 消息体 `system` 字段（每 turn 原生注入） |
| 会话 | 预建绑定（runtime.yaml bridge 段 sessionId/sandboxId） | adapter 自持（404 自愈重建） |
| 流式 | SSE 事件流 | 否（turn 结束出全文 + progress_texts） |

## 双 runtime 镜像（差异全部封装在镜像目录内）

bridge 与 operator 对两 runtime **零差异**——内外网切换只换运行时镜像
（Makefile `CIMICODE_IMAGE` / `OPENCODE_RUNTIME_IMAGE` 指谁供谁）：

| | cimicode-runtime/ | opencode-runtime/ |
|---|---|---|
| 基础镜像 | 内部 coder-cimicode（`CIMICODE_BASE_IMAGE` 构建必填，无默认值——内网 registry 地址不进仓库） | node:22-slim |
| runtime 二进制 | `cimicode`（bun 编译，基础镜像自带） | `opencode`（npm `opencode-ai@1.18.27`，`OPENCODE_VERSION` 可钉） |
| 全局配置路径 | `~/.cimi/cimicode/cimicode.json` | `~/.config/opencode/opencode.json` |
| ripgrep / python3 | 基础镜像自带 | 镜像 apt 安装（node-slim 缺） |
| serve 命令 | `cimicode serve` | `opencode serve` |

两镜像同契约：单端口 4096、entrypoint 种配置（存在不覆盖）、
`OPENCODE_PERMISSION={"*":"allow"}` 镜像 ENV 固化、
`skills.paths=/opt/agentteams/skills` 零拷贝生效、共享
`worker-bridge/bin/mc`、taskflow/agentteams-sync 包装器同构。

## cimicode-pod REST 契约（双标定：opencode 1.18.27 与 cimicode 同型）

端口（镜像契约，两目录同构）：

| 端口 | 服务 | 端点 |
|---|---|---|
| 4096 | `cimicode serve` / `opencode serve` | `POST /session`（创建，返回顶层 `id`）；`GET /session`（列表/健康）；`GET /session/{id}`；`POST /session/{id}/message`（**服务端阻塞整 turn**，body 含 `system`）；`GET /session/{id}/message`（消息列表） |

**agent.md 生效机制**（消息体 `system` 字段，原生通道）：

1. opencode 1.18.27 `PromptInput.system`
   （packages/opencode/src/session/prompt.ts:1499-1521）接受消息级 system
   prompt，随消息持久化（:668）——cimicode 换皮同型；
2. bridge 每 turn 组装 body `{"system": <agent_md>, "parts": [...]}`——turn 级
   生效，团队名单变化下一 turn 即见（v1.0 的 helper 文件推送 + cwd 读取时机
   依赖退役）；
3. 冒烟注记 **S1**：1.18.27 历史消息转换是否对旧消息重复注入 system——
   同会话多 turn 冒烟验证（未见 system 内容复读即确认）。

turn 生命周期（bridge `CimicodePodAdapter.chat`）：

1. **会话自愈**：入参 session_id（pod 形态恒空）或自持 id `GET` 探活，404 →
   `POST /session` 重建（pod 重建后会话目录丢失是预期内场景）；
2. **baseline**：`GET .../message` 取最后一条 assistant 消息 id；
3. **提交**：`POST .../message` body `{"system": <agent_md>,
   "parts":[{"type":"text","text":...}]}`（独立长超时 = turn 超时 + 30s）；
4. **轮询**：`GET .../message` 直到 baseline 之后出现带 `info.time.completed`
   的 assistant 消息；`info.error`（`error.data.message`）→ RUNTIME_ERROR；
   轮询窗口耗尽 → TURN_INTERRUPTED；
5. **产出**：`[TEXT_DELTA(全文), TURN_COMPLETED]`（与 stateless 事件形态同构）；
   turn 中途的 assistant 插话随 `TURN_COMPLETED.data.progress_texts` 透出。

SSE 在近期 opencode 版本不可靠——轮询是标定结论，勿回退。

## 模型注入（operator，runtime 无关）

**镜像零凭据**：不烘焙任何 provider/apiKey（v1.0 的 ZHIPU_API_KEY build-arg
退役）。模型配置由 `worker-bridge/operator/worker_bridge_operator.py` 在
reconcile 时渲染：

- **来源**：Worker CR `spec.model` + bridge pod env（`AGENTTEAMS_AI_GATEWAY_URL`
  / `AGENTTEAMS_WORKER_GATEWAY_KEY`，controller 无条件注入）；
- **产物**：`render_model_config`——`agentteams-gateway` provider（npm
  `@ai-sdk/openai-compatible`、`baseURL <gw>/v1`、apiKey=网关 key）+ 顶层
  `model` 主键；URL 尾斜杠归一化只在此处（调用侧仅 `.strip()`）；
- **注入**：Secret `<w>-cimicode-fs` key `model-config` → pod env
  `OPENCODE_CONFIG_CONTENT`（secretKeyRef）；
- **哈希滚动**：`CIMICODE_MODEL_CONFIG_HASH`（sha256[:16] 明文 env）变更经
  deployment drift（整 spec replace）触发滚动重启——Secret 值变更本身不重启
  pod，哈希 env 是重启触发器；
- **native-config 哨兵**：`spec.model=native-config`（strip().lower() 对齐
  controller `isNativeConfigModel`）跳过注入，runtime 自管配置；
- **缺失推迟**：spec.model 空或网关 env 缺（一次性报齐）→ 本轮零副作用推迟
  （旧栈原样保留），补齐后收敛。

配置通道标定（opencode 1.18.27，cimicode 同型）：
`OPENCODE_CONFIG_CONTENT`（config/config.ts:482-489，process.env 直读，
source="local"，优先级最高）——不落盘、不进镜像；
`OPENCODE_PERMISSION`（config.ts:559-563，JSON parse 后 mergeDeep）由镜像
ENV 固化；`skills.paths`（skill/index.ts:211）直读生效。

## 接线（Worker CR spec.env → bridge 自愈轮询）

operator patch 两键（bridge 经 controller runtimeEnv 读取，或 pod env 直接注入）：

```
BRIDGE_RUNTIME_ADAPTER=cimicode-pod
BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096
```

v1.0 的 `BRIDGE_RUNTIME_HELPER_URL` 退役：新 bridge 不读；存量 Worker CR
残留键由 operator 每轮显式清除（`ENV_KEY_HELPER_URL_RETIRED`）。

会话门禁（app.py 三处 `== "cimicode-stateless"`）语义是"仅 stateless 要求预建
绑定"——pod 形态免预建，勿放宽为通用检查。

## 已知限制（生产化备忘）

- **无卷**：/workspace=容器可写层，pod 重建即丢（会话目录 + 任务状态）。
  会话由 404 自愈重建兜底；任务状态经 taskflow `mc pull` 恢复。PVC 化是
  生产化步骤。
- **ripgrep 必装**：opencode skill 工具 shell 出 rg，缺则每次挂 130s
  （cimicode 基础镜像自带；opencode-runtime 镜像 apt 已装）。
- **S1（1.18.27 system 历史语义）**：历史消息转换是否重复注入 system——
  契约冒烟清单项（见上），同会话多 turn 验证。
- **S2（cimicode 0.5.0 对齐）**：内部 coder-cimicode 0.5.0 与上游 opencode
  的 system / CONFIG_CONTENT 通道对齐度——内网冒烟验证，外网只留本记录。
