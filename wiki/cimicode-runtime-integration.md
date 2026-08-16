# cimicode 运行时接入分析（基于 opencode 魔改的无状态 code agent）

> 日期: 2026-08-16（v2，按 cimicode 无状态架构文档重写）
> 基线: 分支 `dev-v1.2.2`；目标：新增 Worker 运行时 `cimicode`--基于 opencode 魔改的 **cimicode 无状态版本**（大脑无状态 HTTP 服务 + OpenSandbox 远程沙箱执行），作为 Worker 加入平台
> 环境前提: K8s Helm 部署 + 外接 S3 endpoint（静态 AK）+ Synapse homeserver
> 关联文档: [migration-v1.1.2-to-dev-v1.2.2.md](migration-v1.1.2-to-dev-v1.2.2.md)（先完成迁移再接入新运行时）
> 架构依据: cimicode 侧设计文档《无状态多 Pod + 远程沙箱演进》（`POST /session/context_prompt` + SSE + in-memory session store + sandbox 外部生命周期）

---

## 0. 结论（v2 修订）

- **架构假设变更**：cimicode 不是"本地有代码工作区的 opencode"，而是**大脑（无状态 HTTP 服务）+ 手脚（OpenSandbox 远程沙箱）**。所有文件/命令操作在沙箱内执行，cimicode 自身零状态。
- 集成形态：AgentTeams Worker 容器 = **Matrix 桥（Node）兼任编排层**，调 cimicode Deployment 的 `/session/context_prompt`（SSE）；沙箱生命周期与 sessionID↔sandboxID 映射由桥管理、持久化到 agent S3 workspace。
- 改动面可控：controller 侧约 10 个文件（第 1 节清单不变）；wrapper 从"驱动 opencode 进程 + 文件同步"**简化为纯 Matrix↔HTTP 桥 + 映射管理**。
- 风险重心转移：原"S3 同步风暴"风险**大幅降级**（代码在沙箱不在本地）；新风险集中在 **SSE 单连接事务模型**（断连即丢事务）、**同 session 并发**、**TTL 回收丢数据**、**编排层角色归属**（第 5 节 R1-R10）。

---

## 1. 改动清单（文件级检查表，AgentTeams 仓库侧）

### 1.1 Controller / Go（必改，核心链路）

| 文件 | 位置 | 改动 |
|------|------|------|
| `agentteams-controller/internal/backend/interface.go` | L34-38, L60-64 | 新增 `RuntimeCimicode = "cimicode"` 常量；`ValidRuntime()` 加分支（唯一的 runtime 合法性校验入口） |
| `internal/backend/kubernetes.go` | ~L259-266（镜像 switch）、~L275-295（WorkingDir switch）、~L743-750（runtime 归一化） | 三处 switch 加 `case RuntimeCimicode`；镜像用 `config.CimicodeWorkerImage`；工作目录走 default 分支（`HOME == /root/agentteams-fs/agents/<name>`，免改） |
| `internal/backend/docker.go` / `sandbox.go` | docker ~L114-122；sandbox ~L149-156 | 同样的镜像 switch 加分支，**三个 backend 必须同步** |
| `internal/config/config.go` | L568-571、L611-614、L628-631（三处 WorkerEnv/镜像配置结构） | 加 `CimicodeWorkerImage`，env `AGENTTEAMS_CIMICODE_WORKER_IMAGE`（默认 `agentteams/agentteams-cimicode-worker:latest`） |
| `helm/agentteams/crds/workers.agentteams.io.yaml` | L25 | **`spec.runtime` 是硬枚举，必须加 `cimicode`，否则 CR 直接被 API server 拒绝**（注意 openhuman 都不在枚举里） |
| `api/v1beta1/types.go` | L181、L699 | 注释更新（cosmetic） |
| `internal/service/deployer.go` | L395（`inlineOwnsSoul`）、L1487-1501（`builtinAgentDir`） | `builtinAgentDir` 加 `case "cimicode": return filepath.Join(baseDir, "cimicode-worker-agent")`；是否像 copaw/hermes 合并 identity 进 SOUL.md 按需 |
| `internal/executor/package.go` | L658-667（`mergeIdentityIntoSoul`） | 若走 SOUL 合并路线则加 runtime 判断 |
| `cmd/agt/create.go` / `update.go` / `worker_cmd.go` | runtime flag 校验/帮助文本 | 跟随 `ValidRuntime()`，主要是帮助文本 |

**明确不用改的**：

- `internal/service/worker_env.go`：env 注入与 runtime 无关（`AGENTTEAMS_WORKER_NAME/MATRIX_TOKEN/FS 凭证/HOME` 对所有 runtime 通用）。
- `internal/agentconfig/generator.go`：openclaw.json 对非 openclaw runtime 已有兼容分支（L186-189）。
- `internal/service/runtime_config.go`：仅当走 qwenpaw 式 `runtime.yaml` 期望态下发才需要；cimicode 桥直接消费 openclaw.json，不用改。
- `team_controller.go` / `member_reconcile.go` / `worker_controller.go` 的 `RuntimeQwenPaw` 分支：全是 qwenpaw(leader)/edge 特例，cimicode 作为普通 worker 不涉及；仅确认它落入"非 qwenpaw 非 copaw"分支的行为正确（team_controller ~L830）。
- `provisioner.go` L523"不自动加入被邀房间"的补偿逻辑对 cimicode 桥同样适用（controller 会帮 join）。

### 1.2 Helm（必改）

| 文件 | 位置 | 改动 |
|------|------|------|
| `helm/agentteams/values.yaml` | ~L349-360 | `worker.defaultImage.cimicode.repository/tag`；另需评估是否加 `cimicode:` 段（cimicode 核心 Deployment 与 OpenSandbox 的部署 values，见第 4 节） |
| `templates/_helpers.tpl` | ~L196-213 | 新增 `{{- define "agentteams.worker.cimicodeImage" }}` |
| `templates/controller/deployment.yaml` | ~L80-87 | 新增 env `AGENTTEAMS_CIMICODE_WORKER_IMAGE` |

### 1.3 Manager 侧模板（必改）

| 位置 | 改动 |
|------|------|
| `manager/agent/cimicode-worker-agent/`（新建） | 至少 `AGENTS.md`（协作上下文：说明该 worker 是 code agent、任务委派格式、沙箱/会话语义），可选 `skills/` |
| `manager/scripts/init/start-manager-agent.sh` ~L1070-1074 | 加两行 `bash "$RENDER" .../cimicode-worker-agent`（workspace + image 两份） |
| `manager/scripts/init/upgrade-builtins.sh` | worker builtins 从 `worker-agent` 发布，cimicode 一般不用改，确认即可 |

### 1.4 Worker 镜像（新建顶层 `cimicode/` 包）

结构远简于 hermes（无运行时进程包装、无代码工作区同步）：

```
cimicode/
├── Dockerfile              # Node 基础镜像 + mc + agt CLI + shared/ 脚本
└── scripts/
    ├── cimicode-worker-entrypoint.sh   # env 校验 -> Matrix 重登录 -> 同步(仅小状态) -> readiness -> exec 桥
    └── bridge/                         # Node 桥：matrix-js-sdk <-> /session/context_prompt
```

同步检查根 `Makefile` 的镜像构建目标。

### 1.5 测试 / 脚手架（跟随改）

`test/testutil/fixtures/worker.go`、`cmd/agt/*_test.go`、`api/v1beta1/types_test.go`、`internal/backend/*_test.go`、`internal/service/deployer_test.go` 等含 runtime 枚举的用例；`install/agentteams-apply.sh` / `import.sh` / `verify.sh` 中 runtime 相关引用逐一确认。

---

## 2. Hermes 参照模式（仅取其壳，内核已不同）

cimicode 桥仍需照抄 hermes 的**容器接入壳**，但**不再需要**其核心的运行时包装：

| 组件 | hermes | cimicode 桥 |
|------|--------|-------------|
| CLI 入口/entrypoint | typer + Python | Node + bash entrypoint（保留） |
| Matrix 重登录（`credentials/matrix/password` -> login -> token 写回） | ✅ 需要 | ✅ 照抄（必须，否则重启 E2EE 异常掉线） |
| readiness 上报（`agt worker report-ready`） | ✅ 需要 | ✅ 照抄（信号改为"桥已连上 Matrix 且 cimicode Service 可达"） |
| MinIO/S3 全量工作区 mirror + push/pull 循环 | ✅ 核心 | ❌ 不需要（代码在沙箱）；仅同步**小状态**（映射 state.json、记忆、AGENTS/SOUL 模板），用 `shared/lib/worker-file-sync.sh` 即可 |
| openclaw.json -> 运行时配置桥接 | ✅ 核心 | ⚠️ 简化：只取 LLM provider（网关 baseUrl+consumer key）与 Matrix 凭证注入桥进程 env |
| 包装外部 agent 进程 | ✅ 核心 | ❌ 不包装任何 agent 进程，纯 HTTP 客户端 |

可直接复用的 `shared/lib` 基建：`agentteams-env.sh`、`oss-credentials.sh`、`mc-wrapper.sh`、`worker-file-sync.sh`（小状态）、`merge-openclaw-config.sh`。

---

## 3. cimicode 无状态架构解读（v2 核心）

### 3.1 状态去向清单

原单机版四类状态被拆到四个归属地，cimicode 自身零状态：

| 状态 | 原去向（单机版） | 新去向（无状态版） |
|------|----------------|------------------|
| 会话历史 | Pod 本地 SQLite | **每次请求由外部全量传入**（`context` 字段，export 格式） |
| 会话运行时态 | SQLite 事务 | **Pod 内存，SSE 生命周期内**（in-memory store，30min TTL + 5min 扫描） |
| 文件/命令执行环境 | Pod 本地文件系统 | **OpenSandbox 远程沙箱**（TTL 默认 3600s 兜底回收，工具调用成功后续期） |
| sessionID↔sandboxID 映射 | 插件内部 | **外部系统维护**（AgentTeams 集成中 = 桥，见第 4 节） |

### 3.2 核心运行模型："单连接事务"

```
SSE 连接建立 -> 注册内存 session -> 导入历史 -> [先订阅 Bus] -> LLM loop（工具调沙箱）
            -> idle/error/断连 -> 发 done -> 关 SSE -> 删内存 session ->（沙箱不动）
```

- 事务边界 = SSE 连接边界；**连接断 = 事务亡**（含客户端断连路径）。
- 多 Pod 一致性是被"事务化"消除的，不是被"共享"消除的：没有 Redis/共享存储/分布式锁。**同一 sessionID 并发请求时一致性保证失效**（风险 R2）。
- SSE 事件类型：`message.part.delta` / `message.part.updated` / `message.updated` / `message.removed` / `session.status` / `session.error` / `server.heartbeat`(10s) / `done`(终态)。

### 3.3 接口契约要点

`POST /session/context_prompt`（SSE 模式需 `Accept: text/event-stream`）：

- 必填：`context`（export 格式历史）、`parts`（新消息）；可选：`sessionID`、`sandboxID`、`model`、`agent`、`directory`、`system`、`variant`。
- 向后兼容：两 ID 均可选，核心不报错；**sandboxID 未传时插件层报错要求传入**。
- **JSON 模式（无 Accept 头）走 `GET /event` 轮询 + `DELETE /session/...`，多 Pod 下仍有 404 问题**--集成时一律用 SSE 模式，JSON 模式仅限调试（风险 R10）。
- 实现侧约束：SSE 客户端必须 fetch + ReadableStream（EventSource 仅支持 GET）；**先挂 SSE 消费者再触发请求**（否则丢初始 `session.status: busy`）。

### 3.4 cimicode fork 源码改动汇总（六需求）

| 模块 | 文件/层 | 改动性质 | BREAKING |
|------|--------|---------|----------|
| HTTP API | 新增 `/session/context_prompt` 端点（①add-context-prompt-api；独立端点而非扩展 `prompt_async`，复用 fork 的 `Session.importContext()`，ExportData schema 与 CLI export 对齐） | 新增 | 否（上游合并冲突高发区） |
| SSE | Accept 头协商、终态自动清理三路径（idle/error/断连）（②context-prompt-sse） | 新增 | 否 |
| Session | 顶层 `sessionID` 字段 -> `Session.create()` 透传 `createNext(id)`；前端复刻 ID 算法 `ses_<12位hex降序时间戳><14位随机base62>` 过 Zod（③external-session-id） | 修改 | 否（算法重复实现有漂移风险） |
| Session | `InMemorySessionStore`：service 层 7 写 + 2 读加条件分支、`Set<SessionID>` 预注册、直接 Bus 全量发布绕过 SyncEvent/convertEvent、30min TTL + 5min 扫描、5 Map 扁平结构（④） | 新增+修改 | 事件格式可能与持久 session 有差异 |
| 穿透 | `ContextPromptPayload -> … -> LoopInput -> resolveTools -> Tool.Context` 全链透传 sandboxID（瞬态，不持久化）；`Tool.Context` 加 `sandboxID?: string`，靠 `registry.ts` 的 `...toolCtx` spread 自动转发（⑤） | 修改 | 否（spread 是隐式契约：新工具必须经 registry spread 取 ctx） |
| 插件 | `getSandbox` 签名五参->一参 `(sandboxId)`；保留 验证+resume（Paused->resume、Pausing->等、终态->报错）+执行+renewTTL；**注释掉** 创建/删除/暂停/持久化/缓存/事件/信号处理（⑤瘦身，BREAKING，无 fallback 创建） | 修改 | **是** |
| 插件 | 7 工具覆盖（bash/read/write/edit/ls/glob/grep）改用 `ctx.sandboxID`；自研 OpenSandbox HTTP 客户端（SSE 双格式 NDJSON+`data:`）；Execd 经 Server Proxy；路径规范化校验于 `/tmp/workspace/` 内防逃逸；工具参数全兼容（read offset/limit、edit replaceAll、bash background）；TTL 每次工具调用成功后续期、失败仅告警（⑥） | 新增+修改 | 是（配合上条） |

> ⚠️ 待确认的文档占位符（疑为笔误）：`DELETE /session/glm-5.3_common` 与沙箱名 `glm-5.3_common`（出现于 Execd Proxy 示例 URL）。与 cimicode 侧确认实际值后再锁定桥的实现。

### 3.5 设计细节的已知注意点

- **纯 LLM 推理段不续期 TTL**：长 chain-of-thought 期间无工具调用 -> TTL 到期 -> 沙箱被回收 -> 下一次工具调用终态报错。建议给插件加"每轮 prompt 开始时也续期"。
- **background bash 是审计盲区**：done 之后后台进程仍在沙箱内跑，TTL 到期才回收。"任务完成"≠"沙箱内无活动进程"。
- **内存 session 事件格式**：直接 Bus 全量发布 vs 持久 session 的 convertEvent 展开，两者可能有细微不一致，编排层不能假设逐字节一致。
- **断连检测机制未写明**：清理三路径中"断连"靠什么探测（TCP FIN vs heartbeat 超时）需看实现；若靠 10s heartbeat，孤儿 session 最长多活一个心跳周期（再加 30min TTL 上限兜底）。
- **内存峰值**：`同时活跃 SSE 数 × 平均 session 大小`，Deployment 内存与 HPA 按此估算（建议按 SSE 并发数而非 CPU 扩缩容）。

---

## 4. AgentTeams 集成架构（v2 重写）

### 4.1 编排层归属：桥兼任

cimicode 文档假设的"外部业务系统"（沙箱生命周期 owner + 映射维护者）在 AgentTeams 中由 **Worker 容器内的 Node 桥**兼任--它天然按房间串行处理消息，且有 agent S3 workspace 可持久化状态。

### 4.2 映射持久化

`state.json` 存 agent S3 workspace（体量小，走 `worker-file-sync.sh` 同步）：

```json
{
  "sessions": {
    "<matrixRoomId>": {
      "sessionID": "ses_...",
      "sandboxID": "sbx_...",
      "historyKey": "sessions/<matrixRoomId>/export-<n>.json",
      "updatedAt": "..."
    }
  }
}
```

- 桥用自己的会话键（Matrix room ID + 任务切分派生），**不依赖 cimicode 的 sessionID 算法**（规避 ③ 的前端复刻漂移风险）；sessionID 只作透传。
- 每轮 done 后把 export（会话历史）存 S3，下轮请求从 S3 组装 `context`（全量重放，见风险 R5）。

### 4.3 沙箱生命周期规则（Phase 0 决策项）

| 事件 | 动作 |
|------|------|
| 新任务首条消息 | `createSandbox` -> 得 sandboxID -> 建映射 |
| done 后 N 分钟空闲 | `pauseSandbox`（节流；resume 由插件自动处理） |
| 任务关闭 / Team 删除 / Worker 删除 | `deleteSandbox`（finalizer 链路需评估是否纳入 controller 清理） |
| 每轮 done / 里程碑 | **沙箱内 git commit 推远程仓库或快照**（对冲 R4：TTL 回收 = 代码全灭，沙箱无内置快照） |

### 4.4 消息处理主流程

```
Matrix 消息到达 -> 房间互斥（R2 串行化）
  -> 查映射: 有 sessionID+sandboxID?
     ├─ 有 -> 沙箱健康检查（终态则按规则重建）-> 组 context（S3 历史 export + 新消息 parts）
     └─ 无 -> createSandbox -> 生成桥侧会话键
  -> POST /session/context_prompt (Accept: text/event-stream, fetch+ReadableStream)
  -> 消费 SSE: message.part.delta 可选节流转发房间；done -> 存 export 到 S3 -> 映射回写
  -> 异常路径: SSE 断连/沙箱终态/TTL 到期 -> 按重试语义处理（R1/R4）
```

### 4.5 部署形态

```
┌─ AgentTeams controller（照第 1 节清单改）
├─ Worker Pod（cimicode runtime）: Matrix 桥 + 映射管理 + 小状态同步 + agt report-ready
├─ cimicode Deployment（无状态，2+ 副本，无 PVC）:
│    内存 = 活跃 SSE 数 × session 大小 × 系数；terminationGracePeriodSeconds >= 最长任务；
│    preStop 等活跃 SSE 结束；LLM provider 环境变量注入网关 consumer key（R8）
└─ OpenSandbox（独立 namespace + 每沙箱资源配额 + TTL 策略 + 仅集群内暴露）
```

---

## 5. 风险坑位汇总（v2 合并）

### 🔴 架构级（上线前必须有答案）

| # | 风险 | 机理 | 缓解 |
|---|------|------|------|
| R1 | **SSE 断连 = 事务丢失** | 滚动更新/Pod 驱逐/probe 误杀/网关 idle timeout/网络抖动都会断流；重试 = 全量 context 重放 + LLM 重新推理（费用双倍，输出可能不同） | 桥做断点续传语义；Deployment 优雅终止覆盖最长 SSE；probe 不杀长连接 |
| R2 | **同 session 并发请求** | 轮询命中不同 Pod -> 两个内存 session 并行消费同一历史+同一沙箱 -> 双倍计费、沙箱并发写冲突 | 桥按房间/会话互斥串行化；建议 cimicode 侧加防御（同 sessionID 活跃 SSE 时拒绝第二个） |
| R3 | **编排层角色归属** | 沙箱生命周期 + 映射的 owner 必须明确，Pod 重启映射不能丢 | 本文方案：桥兼任，映射持久化 agent S3（4.2） |
| R4 | **TTL 回收 = 代码数据丢失** | 沙箱无快照机制，3600s 兜底回收后沙箱内代码全灭 | 生命周期规则（4.3）：done 后 pause 而非放任 TTL 到期；里程碑 git commit/快照 |
| R5 | **context 全量重放成本曲线** | 历史在请求体，随会话线性增长；长会话请求体 MB 级、LLM 输入 token 全额计费 | 桥做会话切分（每任务一个 session）+ 历史摘要压缩；监控请求体大小 |
| R6 | **纯推理段 TTL 不续期** | 长无工具调用的推理 + TTL 临近到期 -> 推理完首次工具调用遇终态 | 建议插件加"每轮 prompt 开始续期"；桥在收到 session.error 终态时走重建+重放 |

### 🟡 集成级（AgentTeams 视角）

| # | 风险 | 说明 |
|---|------|------|
| R7 | **Higress SSE 透传** | 网关 buffer / idle timeout < 10s heartbeat 周期会掐断流式。验证 Higress 对 `text/event-stream` 的 flush 与超时；不理想则 cimicode 走集群内 Service 直连不过网关 |
| R8 | **`/session/context_prompt` 无认证 + LLM provider 归属** | 契约无 auth 字段，谁能 POST 谁就能烧 LLM 额度。过网关必须加 consumer key-auth；cimicode 侧 LLM 的 baseURL/apiKey 注入网关 consumer key，请求级 `model` 与启动级配置的优先级需实测 |
| R9 | **插件 BREAKING 回归** | v2 只用不建：沙箱不存在/终态时的报错路径要清晰映射为可恢复动作（重建+重放），不能直接向用户报错；注释掉的代码长期失修（建议加 `TODO(v3-fallback)` 标记或移独立分支） |
| R10 | **JSON 轮询模式仍有多 Pod 问题** | 非 SSE 模式走 `GET /event` 轮询，多 Pod 下 404 回归。集成一律 SSE 模式 |
| R11 | **Execd 经 Server Proxy 的吞吐放大** | 所有命令 IO 过 Server 一跳，大输出（构建日志）延迟/吞吐放大，bench worst case |
| R12 | **路径逃逸与 symlink** | 插件规范化后校验于 `/tmp/workspace/` 内；沙箱内 symlink 指向外部可否绕过取决于沙箱 fs 隔离强度，需验证 |

### 🟢 平台接入侧（AgentTeams 仓库改动，与 v1 相同仍适用）

| # | 风险 | 说明 |
|---|------|------|
| P1 | **CRD 枚举第一坑** | `workers.agentteams.io.yaml` L25 硬枚举，不加 `cimicode` 则 `kubectl apply` 直接被拒 |
| P2 | **三 backend + config.go 镜像 switch 必须同步** | 漏一处则该 backend 下静默回退 openclaw 镜像 |
| P3 | **readiness 协议** | 镜像 COPY `agt`，信号 = 桥已连 Matrix 且 cimicode Service 可达，再 `agt worker report-ready`，否则 Team 编排卡等 ready |
| P4 | **Matrix 重登录** | 照抄 hermes `_matrix_relogin`（密码在 `credentials/matrix/password`），否则重启 E2EE 异常掉线 |
| P5 | **消息 catch-up** | 桥启动先完成 initial sync 再处理新消息（参见 `manager_reconcile_welcome.go` L64 说明） |
| P6 | **桥接产物排除** | 桥进程写入 workspace 的派生文件（如本地缓存、映射临时文件）列入推送排除，避免与 Manager 下发文件打架 |

### ✅ v1 风险的降级/作废记录

- **S3 同步风暴（.git/node_modules/构建产物）**：✅ 大幅降级--代码在沙箱，agent S3 workspace 只有小状态与记忆。
- **XDG 目录持久化（`XDG_DATA_HOME` 等）**：❌ 作废--cimicode 无状态，不再需要。
- **每消息冷启动 / opencode serve 常驻选型**：❌ 作废--架构已定为独立 Deployment + HTTP API。

---

## 6. 建议实施顺序（v2）

```
Phase 0  定契约（先于编码）
  - 编排层归属确认（本文方案：桥兼任）
  - 映射/历史持久化格式定稿（4.2）
  - 沙箱生命周期规则定稿（4.3）
  - 与 cimicode 侧确认：SSE 断连检测机制、纯推理段续期、认证方案、占位符笔误
Phase 1  cimicode 核心部署（无状态 Deployment：内存/优雅终止/HPA 按 SSE 并发/provider env）
Phase 2  OpenSandbox 部署（namespace/配额/TTL 策略/网络暴露范围/与 Worker 的隔离授权）
Phase 3  桥实现（Matrix <-> context_prompt；房间互斥；映射与历史 S3 持久化；异常重试语义）
Phase 4  AgentTeams runtime 接入（第 1 节清单 + P1-P6）
Phase 5  故障注入验证（上线前必做）
  □ SSE 中途杀 cimicode Pod -> 桥重放恢复
  □ 同房间连发 5 条消息 -> 严格串行
  □ 沙箱 TTL 到期（调 60s 测）-> 终态报错 -> 重建 -> context 重放
  □ 沙箱 pause -> resume 路径
  □ 桥 Pod 重启 -> S3 恢复映射，沙箱还在
  □ Higress 过流式 vs 集群内直连对比（延迟/事件完整性）
  □ background bash 在 done 后的行为与回收
  □ 大 context（>1MB 请求体）的网关/超时行为
```
