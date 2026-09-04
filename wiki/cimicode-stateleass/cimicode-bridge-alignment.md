# cimicode-bridge 对齐文档（bridge 现状 / controller 依赖 / gateway-mock）

> 日期：2026-09-04
> 用途：给 controller 团队、cimicode-gateway 团队对齐用。**全部内容以 2026-09-03 在 105 服务器实测验证过的行为为准**，未实现的设计目标明确标注。
> 当前运行版本：bridge `v0.0.8` @ 105 agentos ns，gateway-mock `v0.0.1` + GLM-5.3-flash，已实测打通：**admin 在 Element Web @cimicode-agent → bridge → mock → GLM 流式回复出现在群里（含 markdown 渲染、三层 mention、typing 指示）**。

---

## 一、bridge 代码现状与部署（我们最关注的部分）

### 1.1 已实现并实测的能力

| 能力 | 状态 | 说明 |
|---|---|---|
| Matrix 接入 | ✅ | matrix-nio AsyncClient，token + whoami + 手动 sync 循环 |
| since 持久化 | ✅ 基础 | StateStore 读写（memory/file/redis 三后端，105 当前用 memory） |
| 断线重连 | ✅ | 指数退避 1s→30s |
| 401 token 刷新 | ✅ | 检测 `M_UNKNOWN_TOKEN`/`401`，调 controller 刷新接口，重试 3 次×5s（对齐 CoPaw） |
| mention 过滤 | ✅ | 三级检测：`m.mentions.user_ids` → `matrix.to` 链接 → 文本正则 |
| 角色白名单 | ✅ | leader/admin/human 放行，worker 间互相阻断，unknown 拒绝 |
| CoPaw 三段式 history | ✅ | per-room 内存 buffer，默认 50 条 FIFO，event_id 去重 |
| agentMd 组装 | ✅ | 协作上下文(COORDINATION_* env) + AGENTS.md + SOUL.md，每轮全量重拼 |
| Gateway chat 调用 | ✅ | 只调 `/v1/gateway/session/chat`，SSE 消费（手写行解析） |
| SSE part 聚合 | ✅ | `message.part.delta` 追加 / `message.part.updated` 替换 / `done` 聚合 |
| Markdown 渲染 | ✅ | markdown-it-py（对齐 CoPaw 配置），formatted_body + m.mentions 三层出站 |
| typing 指示 | ✅ | turn 期间 25s 续期，回复/出错/NO_REPLY 后停止 |
| NO_REPLY | ✅ | trim 后精确匹配，不发送 |
| 探针 | ✅ | /healthz /readyz /status |
| S3 配置拉取 | ✅ | MinIO SDK，openclaw.json 重试 6×5s |

### 1.2 关键设计决策（已定）

1. **bridge 只调 gateway 的 chat 接口**——不调 create/destroy。Session 由调谐/平台预创建。
2. **sessionId/sandboxId 从 S3 openclaw.json 读**（`bridge.runtime` 段），不从 env 传。
3. **Matrix token 从 S3 `openclaw.json.channels.matrix.accessToken` 读**（CoPaw 同款路径），env 只作本地开发 fallback。
4. **无本地业务状态**：对话记忆在 gateway 侧，bridge 可随时销毁重建（重启只丢群聊 buffer，安全降级）。

### 1.3 部署（当前 105 的真实布局）

```text
ns: agentos
deployment: cimicode-bridge（单副本，nodeSelector 固定 105，imagePullPolicy IfNotPresent）
镜像: agentteams/cimicode-bridge:v0.0.8（docker build → save → k3s ctr import，不走 registry）
```

一键部署脚本（已验证可用）：

```bash
# 105 上，一条命令完成: 拉代码→构建→导入containerd→改tag→apply→验证
bash /home/extvdiadmin/agentteams-deploy/deploy-bridge-105.sh v0.0.9
```

环境变量全集（deployment yaml 里已配好的，controller 接管时按此对照）：

| env | 值（105 实测） | 用途 |
|---|---|---|
| `AGENTTEAMS_FS_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` | minio-sim.s3-sim:9000 / minio-sim-root / … / agentteams-storage | S3 拉配置 |
| `AGENTTEAMS_STORAGE_PREFIX` | agentteams/agentteams-storage | 对象 key 前缀 |
| `AGENTTEAMS_WORKER_NAME` | cimicode-agent | 定位 agents/<name>/ 目录 |
| `AGENTTEAMS_MATRIX_URL` | http://agentteams-synapse.agentos:8008 | homeserver 兜底（主来源 S3） |
| `AGENTTEAMS_MATRIX_DOMAIN` | agentteams-synapse.agentos.svc.cluster.local | mention 域 |
| `AGENTTEAMS_WORKER_MATRIX_USER_ID` | @cimicode-agent:… | 启动前的自身 ID 预判 |
| `COORDINATION_LEADER/ADMIN/WORKERS/ROLE/TEAM/ROOM` | 各 MXID | 角色过滤 + agentMd 协作上下文 |
| `AGENTTEAMS_CONTROLLER_URL` | http://agentteams-controller.agentos:8090 | 401 时刷新 token |
| `BRIDGE_REDIS_URL` | 空（当前 memory） | StateStore（生产建议配） |

### 1.4 已知未完成（不影响当前联调，按优先级）

1. history 不持久化——重启后群聊 buffer 从空开始（安全降级，不阻塞）
2. Gateway chat 无重试/超时分类（当前失败→日志+静默，用户重新 @ 即可）
3. 媒体消息（图/文件）、加密事件未处理（忽略，不 crash）
4. report-ready 未执行、readyz 未包含 gateway health 检查
5. imagePullPolicy 依赖手工 import 镜像（registry 化后可去掉 nodeSelector）

---

## 二、需要 controller（调谐）给我们什么

> 现状：105 上这些是 **setup-105.sh 手工模拟**的。controller 接管 = 把下面的事自动化，bridge 代码零改动。

### 2.1 S3 写入（最核心）

controller 需在 Worker 创建时，往 `${STORAGE_PREFIX}/agents/<worker-name>/` 写三个文件：

**① openclaw.json**（行为配置主载体，格式已冻结）：

```json
{
  "channels": {
    "matrix": {
      "homeserver": "http://agentteams-synapse.agentos.svc.cluster.local:8008",
      "accessToken": "<fresh matrix token>"
    }
  },
  "bridge": {
    "runtime": {
      "baseUrl": "http://<cimicode-gateway>:<port>",
      "templateId": "<模板标识>",
      "sessionId": "<gateway 预创建的 session>",
      "sandboxId": "<gateway 预创建的 sandbox>"
    }
  }
}
```

要点：
- `accessToken` 必须是**有效 fresh token**（bridge 唯一凭证来源；失效靠 401 刷新链兜底）
- `sessionId/sandboxId` 由 controller 调 gateway create 拿到后写入（bridge 自己不 create）
- 字段名兼容 camelCase/snake_case 两种写法

**② AGENTS.md**：行为协议（@mention 规则、NO_REPLY 说明）——bridge 拉取后拼进 agentMd。
**③ SOUL.md**：人格——同上。

### 2.2 Worker CR / 部署面

1. **runtime 枚举加 `cimicode-bridge`**：CRD 校验 + 按此选镜像
2. **env 注入**：§1.3 表格全集（特别是 `COORDINATION_*` 7 个 + `AGENTTEAMS_WORKER_NAME`）
3. **Matrix bot 生命周期**：注册 bot 用户 + 邀请进 team room（setup-105.sh 第 1-2 步的自动化）
4. **删除语义**：删 Worker CR 时，bridge pod 删除 + 通知 gateway 销毁 session（bridge 不自己调 destroy）
5. **COORDINATION_* 变更**：需重启 pod 生效（env 语义）；AGENTS.md 变更走 S3 重拉即可

### 2.3 controller 现有接口（bridge 已在用）

```text
POST {AGENTTEAMS_CONTROLLER_URL}/api/v1/credentials/matrix-token
Authorization: Bearer {AGENTTEAMS_AUTH_TOKEN 或 AUTH_TOKEN_FILE 内容}
→ {"access_token": "..."}     # 401 刷新用，已实测
```

---

## 三、gateway-mock：契约与实现（给 gateway 团队）

### 3.1 bridge 实际怎么调 mock（= gateway 必须实现的契约）

bridge **只调一个接口**：

```http
POST {baseUrl}/v1/gateway/session/chat
Content-Type: application/json
（不鉴权——当前约定 gateway 关鉴权）
```

请求体（bridge 每轮全量传）：

```json
{
  "sessionId": "sess-cimicode-agent-001",
  "sandboxId": "sbx-cimicode-agent-001",
  "turnId": "$<Matrix event_id>",
  "agentMd": "<协作上下文+AGENTS.md+SOUL.md 拼好的全文>",
  "history": [],
  "userMessage": "<三段式群聊视野文本>"
}
```

字段语义（gateway 纯透传，不解析不存储）：

| 字段 | 谁生成 | gateway 义务 |
|---|---|---|
| `sessionId/sandboxId` | controller 预创建时 | 路由到对应 Sandbox |
| `turnId` | bridge（= Matrix event_id） | **幂等键**：同 turnId 重复提交不能重复执行 |
| `agentMd` | bridge 每轮重拼 | 作为 system 指令传给运行时 |
| `history` | bridge（当前恒空） | 透传 |
| `userMessage` | bridge | 作为本轮 user 输入 |

响应 = **SSE 流**（`Content-Type: text/event-stream`）：

```
data: {"event": "message", "delta": "增量文本"}     ← 0..N 条
data: {"event": "done", "content": "完整聚合文本"}   ← 恰好 1 条收尾
data: {"event": "error", "code": "...", "retryable": false, "message": "..."}  ← 失败时代替 done
```

**bridge 侧已实测的解析行为**（gateway 必须满足）：
- 每帧 `data: <单行JSON>\n\n`（空行分帧）
- `event` 名**内嵌在 JSON 的 `event` 字段**（不是 SSE 的 `event:` 行）
- 收到 `done` 才算成功；流断在 done 之前 → bridge 判 interrupted，不重试
- `delta` 是纯增量追加；`done.content` 必须是完整全文（bridge 直接用它做出站消息）

### 3.2 mock 的实现（D:\workbach\cimicode-gateway-mock，105 上 /home/extvdiadmin/cimicode-gateway-mock）

本质：**SSE 协议适配器 + 真实 LLM**——把 gateway 契约字段映射成 LLM 调用：

```text
bridge chat 请求
  agentMd    → LLM system 消息
  history    → LLM 历史消息（当前空）
  userMessage → LLM user 消息
  sessionId/sandboxId/turnId → 仅日志（mock 不校验）

LLM（GLM-5.3-flash，Anthropic 协议 /v1/messages 流式）
  content_block_delta 增量 → 逐段包成 {"event":"message","delta":...}
  流结束                  → {"event":"done","content":聚合全文}
  异常                    → {"event":"error","code":"LLM_ERROR","retryable":false,...}
```

支持两种 LLM 协议（env `LLM_API_STYLE` 切换）：
- `anthropic`（当前用）：`/v1/messages`，system 顶层字段，`x-api-key` 鉴权
- `openai`：`/chat/completions`，Bearer 鉴权
- 未配 key 时自动降级 **echo 模式**（回显请求，不调外部，用于纯链路调试）

LLM 配置经 K8s Secret 注入（不落 yaml）：

```bash
kubectl -n agentos create secret generic gateway-mock-llm \
  --from-literal=LLM_BASE_URL=https://open.bigmodel.cn/api/anthropic \
  --from-literal=LLM_API_KEY=<key> \
  --from-literal=LLM_MODEL=glm-5.3-flash \
  --from-literal=LLM_API_STYLE=anthropic
```

另实现 create/destroy 的**桩**（返回 SUCCESS），仅供探活，bridge 不调。

### 3.3 真 gateway 就绪时的切换路径

bridge **零代码改动**，两步：

```bash
# 1. MinIO 里 openclaw.json 的 bridge.runtime.baseUrl 改成真 gateway 地址（sessionId/sandboxId 同步换真的）
# 2. kubectl -n agentos rollout restart deployment/cimicode-bridge
```

mock 下线：`kubectl -n agentos delete -f k8s.yaml` + 删掉 mock 工程目录（一次性脚手架）。

### 3.4 对 gateway 团队的三个问题（本次对齐要确认的）

1. **turnId 幂等**：同 turnId 重复提交的行为是重放、挂接还是 DUPLICATED？（bridge 提交层会重试，依赖此防重复执行）
2. **SSE 保活**：长 turn（分钟级）期间是否发注释行/心跳防中间层掐空闲连接？
3. **错误码表**：`SESSION_EXPIRED/SANDBOX_BUSY/TURN_TIMEOUT` 之外还有哪些？`retryable` 的判定语义 bridge 要照做。

---

## 附：相关资源索引

| 资源 | 位置 |
|---|---|
| bridge 源码 | `cimicode-bridge/`（分支 feature/stateless_cimicode_docking，HEAD 0ba4312b） |
| bridge 详细 spec | `wiki/cimicode-bridge-spec.md`（按代码实况重写版） |
| 105 部署手册 | `wiki/cimicode-bridge-deploy-105.md` |
| gateway 会话契约文档 | `wiki/gateway-session.md`（v0.2 现行版，本 mock 实现依据） |
| mock 工程 | `D:\workbach\cimicode-gateway-mock\`（105: /home/extvdiadmin/cimicode-gateway-mock） |
| 一键部署脚本 | `cimicode-bridge/deploy/deploy-bridge-105.sh` |
| 模拟调谐脚本 | `cimicode-bridge/deploy/setup-105.sh`（controller 接管后废弃） |
| mock 验证脚本 | `cimicode-bridge/deploy/verify-gateway-mock.sh` |
