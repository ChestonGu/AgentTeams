# AgentTeams Controller 调和逻辑总结

> 对应源码：`agentteams-controller/internal/controller/`
> 流程图见：`controller-reconcile-flow.drawio`（5 个页面）
> 关于 Matrix 通道策略（允许列表）的单独科普文见：`channel-policy-guide.md`

AgentTeams 的 Kubernetes operator 有三块核心调和逻辑，全部采用 **Reconciler + 共享阶段函数** 的组织方式：

| 调和器 | 对象 | 职责 | 源码 |
|---|---|---|---|
| `WorkerReconciler` | Worker CR | 独立 Worker 的全生命周期（供给、配置、容器、授权） | `worker_controller.go` |
| `TeamReconciler` | Team CR | 团队编排：房间、协作上下文、心跳、**通道策略**、状态聚合 | `team_controller.go` |
| 共享成员阶段 | — | 与 CR 无关的通用成员处理函数，两个调和器共用 | `member_reconcile.go` |

三者的关系：`TeamReconciler` **不重建 Worker 容器、不重写基础 openclaw.json**（那是 Worker 自己的事），它只做"叠加层"。共享阶段用统一的 `MemberContext` / `MemberDeps` / `MemberState` 三种结构把"CR 无关的输入 / 服务层依赖 / 调和输出"解耦。

---

## 一、WorkerReconciler：独立 Worker 的主流程

入口 `Reconcile`（`worker_controller.go:92`）→ 正常路径 `reconcileNormal`（`worker_controller.go:173`）：

```
Reconcile(Worker) 事件
  │ 获取 Worker CR
  ├─ DeletionTimestamp? ──是──► reconcileDelete（清理基础设施/房间/OSS → 移除 finalizer）
  ├─ 含 finalizer? ──否──► 添加 finalizer（保证删除前可清理）
  ▼
reconcileNormal：
  1. 构建 MemberDeps + effectiveSpec + MemberContext
  2. teamRoleForWorker 查 Team 归属（独立 Worker 时为 standalone）
  3. isEdgeWorker? ──是──► 轻量分支（只做 Infra → ModelAuth → Config）
  4. ValidateMemberDeployment
  5. ReconcileMemberInfra        供给 Matrix / 网关 / MinIO / 个人房间
  6. EnsureModelProviderAuth     授权网关 consumer 到模型提供方路由
  7. EnsureMemberServiceAccount
  8. ReconcileMemberConfig       写 OSS 配置（openclaw.json / runtime.yaml / SOUL.md / AGENTS.md / skills）
  9. ReconcileMemberContainer    容器收敛（Pod / Docker / Sandbox）
  10. ReconcileMemberService + ReconcileMemberExpose
  11. reconcileManagerAccess     独立 Worker 被授予 Manager 群聊权限
  12. 写回 Worker.Status + RequeueAfter
```

关键点：
- **Team 感知**：`teamRoleForWorker`（`worker_controller.go:454`）反向查 Team，若该 Worker 已被 `spec.workerMembers` 引用为 leader，则 `configContext.Role` 改为 `team_leader`；`reconcileManagerAccess`（`worker_controller.go:414`）对 Team 成员**不再**授予 Manager 群聊权限（普通 worker 甚至会被踢出 Manager 房间）。
- **configOwnedByTeam**：仅当 `inTeam && runtime == qwenpaw` 时，Worker 侧跳过配置部署（`ReconcileMemberConfig`），改由 TeamReconciler 的 `deployTeamRuntimeConfigs` 负责。
- **事件订阅**：`SetupWithManager`（`worker_controller.go:695`）Watch Worker 本体 + Pod / Sandbox / SandboxClaim，用 `agentteams.io/controller` 标签隔离多实例。

---

## 二、TeamReconciler：Team 编排（引用式）

入口 `Reconcile`（`team_controller.go:68`）→ 正常路径 `reconcileTeam`（`team_controller.go:343`）：

```
Reconcile(Team) 事件
  │ 获取 Team CR
  ├─ DeletionTimestamp? ──是──► handleDeleteTeam（回退所有成员 → 删别名 → 移除 finalizer）
  ├─ 含 finalizer? ──否──► 添加 finalizer
  ▼
reconcileTeam（7 步）：
  Step1  validateWorkerMembers        恰好一个 team_leader、无重复（失败→Failed）
  Step2  resolveTeamMembers           加载引用的 Worker CR（缺失→Degraded+Requeue）
  Step3  resolveTeamAdminActor        解析团队管理员 Human 身份 + 回填成员真实 MatrixID
  Step4  团队基础设施                 ProvisionTeamRooms / EnsureTeamStorage /
                                       setWorkerTeamAnnotation / RefreshWorkerCredentials
  Step5  协作上下文+心跳              Leader: SyncTeamLeaderAssets + InjectCoordinationContext
                                       + InjectHeartbeatConfig；Worker: InjectWorkerCoordination；
                                       QwenPaw/CoPaw/Edge: deployTeamRuntimeConfigs(runtime.yaml)
  Step6  通道授权 ★                  Manager groupAllowFrom 双向调整 +
                                       每个成员的 teamChannelPolicy → InjectChannelPolicy
  Step7  状态聚合                     cleanupStaleTeamMembers + aggregateTeamStatus(Active)
  │ 写回 Team.Status + RequeueAfter(5min)
```

关键点：
- **Team 不拥有 Worker 生命周期**：`detachTeamMember`（`team_controller.go:792`）只回退协作上下文、凭据、通道策略，把 Worker 恢复成独立语义；`handleDeleteTeam`（`team_controller.go:1001`）同样不销毁 Worker。
- **身份权威化**：`deriveTeamWithResolvedIdentities`（`team_controller.go:172`）以 Human CR 的 `Status.MatrixUserID` 为准回填管理员与成员身份，替代旧的 `localpart==name` 推导。
- **事件订阅**：`SetupWithManager`（`team_controller.go:1337`）Watch Team + Worker status 变化（字段索引 `spec.workerMembers.name`），Worker 一变化就触发对应 Team 重调和。
- **Step6 是矩阵允许列表的核心**，详见 `channel-policy-guide.md`。

---

## 三、共享成员阶段：member_reconcile.go

三个核心结构（`member_reconcile.go`）：

| 结构 | 含义 | 典型字段 |
|---|---|---|
| `MemberContext` | CR 无关的成员输入 | `Name`(CR 名) / `RuntimeName`(运行时名) / `Role` / `Spec` / `TeamName` / `SpecChanged` / `Owner` |
| `MemberDeps` | 服务层依赖 | `Provisioner` / `Deployer` / `Backend` / `EnvBuilder` / `DefaultRuntime` |
| `MemberState` | 调和输出 | `MatrixUserID` / `RoomID` / `ContainerState` / `ProvResult` |

阶段函数（谁需要谁调用）：

| 阶段 | 作用 | 写入点 |
|---|---|---|
| `ReconcileMemberInfra`（member_reconcile.go:304） | Matrix 账号 / 网关 consumer / MinIO 用户 / 个人房间 | 凭据仓库 |
| `EnsureModelProviderAuth`（:367） | 授权网关 consumer 到模型提供方 HttpApi | 网关 |
| `EnsureMemberServiceAccount`（:384） | 保证 Pod 用 SA 存在 | K8s |
| `ReconcileMemberConfig`（:393） | 写全部 OSS 配置 | **`agents/<worker>/openclaw.json`**（openclaw）；`agents/<worker>/runtime/runtime.yaml`（qwenpaw/copaw/edge） |
| `ReconcileMemberContainer`（:470） | 容器收敛（Running/Sleeping/Stopped） | 后端 |
| `ReconcileMemberExpose`（:1956） | 网关端口暴露 | 网关 |
| `ReconcileMemberDelete`（:1978） | 全量清理 | — |

**openclaw.json 写入点**（`ReconcileMemberConfig` → `Deployer.DeployWorkerConfig`，`service/deployer.go:281`）：

```
GenerateOpenClawConfig (agentconfig/generator.go:26)
   └─ buildMatrixChannelConfig : 生成 channels.matrix.*（含默认允许列表）
mergeUserPluginConfig (deployer.go:1378)
   └─ preserveChannelMatrixAllowFrom : 从旧文件拷回 groupAllowFrom/dm.allowFrom
PutObject(agents/<worker>/openclaw.json)
```

使用方差异：
- **WorkerReconciler**：走全部阶段（独立语义）。
- **TeamReconciler**：团队路径复用 `RefreshWorkerCredentials`（团队存储访问），但**不调用** `ReconcileMemberContainer`（容器归 Worker）；仅对 QwenPaw/CoPaw/Edge 调 `DeployMemberRuntimeConfig`。

---

## 四、三者如何配合（一张图概括）

```
                 ┌─────────────────────────────────────────────┐
                 │              TeamReconciler                 │
                 │ 房间/上下文/心跳/通道策略/状态聚合(叠加层)     │
                 └──────┬──────────────────────────┬───────────┘
                        │ spec.workerMembers 引用   │ teamChannelPolicy→InjectChannelPolicy
                        ▼                          ▼
   Worker CR ◄────────  WorkerReconciler ──►  ReconcileMemberConfig ──► agents/<w>/openclaw.json
   (引用但独立)   生成基础配置/容器              (共享阶段)                (允许列表被叠加+保护)
```

两条写入路径 + 保护机制详见 `channel-policy-guide.md` 与 drawio 的 `openclaw-allowlist-write-path` 页面。
