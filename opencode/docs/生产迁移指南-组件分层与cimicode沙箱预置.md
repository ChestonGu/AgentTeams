# 生产迁移指南：组件分层与 cimicode 沙箱预置

> 目标读者：把本分支成果迁到真实环境的开发。
> 配套阅读：[opencode运行时替换与协作流转详解.md](opencode运行时替换与协作流转详解.md)（机制全景）。
> 结论先行：**目标架构下（bridge → 无状态 cimicode，cimicode 自建沙箱），opencode/sandbox 双 pod、opencode-stack-operator、钉 105 全部是测试专用物，不需要迁移**；要带走的是 bridge 本体、controller 的少量通用修复、协作工具/skill 预置层（已抽成 `opencode/cimicode-sandbox/Dockerfile`）。

---

## 一、分层清单：哪些直接用、哪些不能带

### A. 直接可用（生产迁移核心资产）

| 资产 | 说明 |
|---|---|
| **cimicode-bridge 本体**（v0.1.9） | 与具体运行时解耦：matrix 防护、turn 编排、每 turn 现拉+agent.md 生成、事件回流。对接新运行时只需一个 adapter（§四） |
| **agent.md 生成链**（`opencode/bridge/generate_agent_md.py` + worker 模板） | 输入=runtime.yaml+soul/identity，输出即 system prompt——对任何"接收 system prompt 的运行时"通用 |
| **协作工具与 skills 预置层** | `opencode/cimicode-sandbox/Dockerfile`（本仓库新增）：taskflow/agentteams-sync CLI + 5 worker skills + mc/jq/git/python3，零 opencode 依赖 |
| **controller 通用修复**（v1.2.2.5/6） | pod-template env hybrid（`ApplyPodTemplate.mergeEnvVars`）、建 pod 前就绪屏障（`RuntimeConfigReadyForBootstrap`：runtime.yaml 含 matrixUserId 才放行）——与运行时无关的健壮性修复 |
| **协议约定** | 结构化 mention（m.mentions+matrix.to）、taskflow 状态机、MinIO 目录布局、rc_invites 放宽、`.gitattributes *.sh eol=lf`、资源三层限额思路 |

### B. 测试专用（真实环境**不需要**）

| 资产 | 为什么不需要 |
|---|---|
| **opencode-runtime 镜像及双 pod 栈** | 被 cimicode 替换 |
| **opencode-sandbox 镜像 + sandbox_helper**（:4097 /exec /agents-md） | 那是"opencode 在 pod A、执行在 pod B"的远程执行通道；cimicode 自建自管沙箱，无跨 pod 执行问题（也就没有 /tmp 边界坑） |
| **opencode-stack-operator**（opencode/operator/） | 它存在的唯一目的=自动供给上述双 pod 栈。cimicode 自己建沙箱，team 侧零管控 |
| **钉 105（nodeSelector/hostPath）** | hostPath 共享卷是双 pod 方案的伴生物；103 故障是测试集群特例。真实环境存储按平台标准（PVC/对象存储）走 |
| **bash.ts 自定义工具、ripgrep、skill 等待 entrypoint** | opencode 专属（skill 工具依赖、启动扫描竞态） |
| **higress 二实例/AI 网关**（opencode 直连 LLM） | 测试为隔离搭的第二网关；生产网关按需 |
| **dashboard fork 的 OpenCode 表单选项** | 只是测试 UX |
| r1-r5 verify 脚本（dispatch/room_tail/purge_rooms 等） | 测试工房工具，可留作参考不入生产路径 |

### C. 需要适配（机制保留、实现重写）

| 资产 | 适配点 |
|---|---|
| bridge 的 **runtime adapter 层** | 现有 opencode/cimicode(SSE) 两实现；为无状态 cimicode 写第三个（§四对接契约） |
| **agent.md 注入方式** | opencode 靠"共享卷 AGENTS.md 文件"生效；cimicode 若接口直收 system prompt 则跳过文件落盘，`chat(agent_md=...)` 直传 |
| **worker pod 形态** | 现在 bridge 以 worker 伪装跑成 pod（controller 建）；若 cimicode 是平台侧服务，bridge 部署形态随之调整（deployment 常驻或 sidecar），controller 的 masquerade 镜像 env（`AGENTTEAMS_OPENCODE_BRIDGE_IMAGE`）换成 cimicode 对接约定 |
| MinIO/OSS 端点与凭据来源 | 测试用 chart 内 MinIO+root 凭据；生产按真实对象存储+per-worker 凭据（契约不变：`AGENTTEAMS_FS_*` 四件套+TEAM+MATRIX_USER_ID） |

---

## 二、controller 为 bridge 做了什么（细节，迁移时对照）

1. **worker 伪装建 pod**：Worker CR 的 runtime 留空 → `AGENTTEAMS_DEFAULT_WORKER_RUNTIME` env 兜底解析为 opencode → controller 建 worker pod 时用 `AGENTTEAMS_OPENCODE_BRIDGE_IMAGE` 指的 bridge 镜像（`kubernetes.go OpenCodeBridgeImage`）。bridge pod 因此**免费获得**全部 worker 基础设施：CR finalizer 生命周期、SA projected token、env 注入、Matrix 账号供给、个人房间。
2. **runtime.yaml 写入**（agent.md 唯一事实源）：TeamReconciler 的 managed 分支为每个成员写 `agents/<w>/runtime/runtime.yaml`（team/member/desired.inlineConfig/storage/credentials 五段）。**首写在 matrix 注册前（缺 matrixUserId），注册完重写全量**——这就是就绪屏障存在的原因。
3. **就绪屏障**（v1.2.2.6）：`member_reconcile` 在 `wb.Create` 前，managed runtime 调 `Deployer.RuntimeConfigReadyForBootstrap`（读 runtime.yaml，`member.matrixUserId` 非空才放行，否则 5s requeue）。**保证 bridge pod 诞生时刻 bootstrap 必然一次成型**。此修复与运行时无关，生产保留。
4. **env hybrid**（v1.2.2.5）：pod-template ConfigMap 的 worker 容器 env 先落、controller 同名覆盖——运维一处配置全员生效（测试里用它注入 `COPAW_TOOL_GUARD_ENABLED=false`）。注意**线上 ConfigMap 有 env 段而仓库副本没有**，迁移时从线上抄回。
5. controller **不感知 opencode 细节**：它只做"managed runtime 的通用供给"。替换成 cimicode 后 1/4/5 原样适用；2 的 runtime.yaml 机制原样适用（cimicode 的 agent.md 同样从它渲染）。

## 三、opencode+sandbox 是怎么"自动"起来的（operator 细节，仅供理解现有测试链）

`opencode-stack-operator`（单文件 Python，level-triggered 轮询 ~10s，幂等收敛）对每个 opencode 型 Worker 供给：

- `opencode-<w>` Deployment+svc:4096（会话 pod，LLM 直连）
- `opencode-<w>-sandbox` Deployment+svc:4097（执行 pod：skills+CLI+helper）
- `opencode-sandbox-fs-<w>` Secret——**凭据搬运**：sandbox 非 controller 管、不会自动获得 filesync env（契约 §4.1），operator 从 controller 建的 bridge pod env 里读明文四件套抄进 Secret
- patch Worker CR `spec.env` 补 `BRIDGE_RUNTIME_ADAPTER/_BASE_URL/_HELPER_URL`
- `AGENTTEAMS_TEAM` 每次 reconcile 从 Team CR workerMembers 重新解析（worker 后进团队自动跟上）
- 删除 worker / runtime 变更 → 按 managed-by label GC 全栈

共享卷：**测试用同节点 hostPath `/workspace`**（这也是钉 105 的原因之一）；生产若保留双 pod 形态应换 RWX PVC，但目标架构（cimicode 单体自管沙箱）没有这个问题。

## 四、bridge 对接无状态 cimicode（目标架构的接缝）

bridge 的运行时抽象就一个契约（`runtime/base.py`）：

```python
class RuntimeAdapter:
    async def chat(self, *, session_id, sandbox_id, turn_id, agent_md, ...) -> list[RuntimeEvent]
    # RuntimeEvent: TEXT_DELTA / TURN_COMPLETED(可带 data.progress_texts) /
    #               RUNTIME_ERROR / TURN_INTERRUPTED
```

对接无状态 cimicode = 实现一个 adapter：

1. **system prompt**：`chat(agent_md=...)` 每 turn 传入现生成的 agent.md——cimicode 接口若直收 system prompt 直接用；若 cimicode 像 opencode 一样只认文件，则由 cimicode 自己落盘（bridge 不再需要 helper URL）。
2. **会话状态**：bridge 侧 HistoryStore 拼上下文（`[Chat messages since your last reply - for context]\n<历史>@sender: body`），adapter 可每 turn 无状态调用——cimicode 的"无状态"正好匹配，无需 session 续期。
3. **长 turn**：POST 阻塞或轮询均可，超时用 `BRIDGE_RUNTIME_TURN_TIMEOUT`（默认 3600s）；**务必实现"超时≠失败"的语义**（opencode adapter 的文案范式："task still running server-side; ask again to re-attach"）。
4. **中间进度**：turn 结束把多条 assistant 文本拆 progress_texts，bridge 会逐条发房间（协作可见性的关键）。
5. 现成参照：`runtime/opencode_adapter.py`（REST+轮询收尾）与 `runtime/adapters.py` 的 cimicode SSE 实现。

### 沙箱侧（cimicode 自建，team 零管控）

只需两件事：
1. **基础镜像** = `opencode/cimicode-sandbox/Dockerfile`（预置：5 worker skills 全文 + taskflow/agentteams-sync 全局命令 + mc/jq/git/python3；无任何 opencode/helper/entrypoint 残留）。leader 场景追加 leader 模板的 project-management/team-coordination 两件 skill。
2. **建沙箱时注入 env 契约**（镜像零凭据）：

```
AGENTTEAMS_FS_ENDPOINT / _BUCKET / _ACCESS_KEY / _SECRET_KEY   # 对象存储与凭据
AGENTTEAMS_TEAM          # 团队共享根（teams/<t>/shared/）
AGENTTEAMS_MATRIX_USER_ID# taskflow --actor 与所有权判定
AGENTTEAMS_WORKER_NAME   # agents/<w>/ 前缀
```

**skill 生效的本质**（理解后可平移到任何运行时）：① SKILL.md 在沙箱可读 ② scripts CLI 在 PATH ③ 协议文本引导"用前先读"。opencode 的原生 skill 工具只是这三要素的自动化封装；cimicode 的 agent 用 read/cat 读 SKILL.md + 直接敲 CLI 即完全等价（r5 实测两路径并存，均闭环）。

## 五、迁移核对单（建议顺序）

1. bridge 对接：写 cimicode adapter（§四）+ 单测（FakeRuntime 框架现成）
2. cimicode 沙箱：用新 Dockerfile 出镜像，cimicode 建沙箱流程注入 env 契约，验证 `taskflow ack/submit` 走通 MinIO
3. controller：带上 v1.2.2.5/6 修复；`AGENTTEAMS_OPENCODE_BRIDGE_IMAGE` 换 cimicode 对接镜像；ConfigMap env 段从线上抄回
4. 剔除 B 类：operator/双 pod/网关二实例/钉节点全部不迁
5. 端到端冒烟：建 worker→team→派单→TASK_COMPLETED→验收→完结（对照 r5 零干预基线）
