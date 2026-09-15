# AgentTeams Controller 调和层全量总结（internal/controller）

> 源码目录：`agentteams-controller/internal/controller`
> 关联文档：
> - `docs/zh-cn/controller-reconcile-summary.md` — 三大调和器（Worker/Team/共享成员阶段）的高层总结
> - `docs/zh-cn/controller-reconcile-flow.drawio` — 5 页流程图（worker / team / member-phases / channel-policy / allowlist-write-path）
> - `docs/zh-cn/channel-policy-guide.md` — Matrix 通道策略（允许列表）专题科普
> - `docs/design/agentteams-controller-refactor.md` — 旧版控制器重构设计

本文是**全量**总结：覆盖该目录下所有调和器、共享阶段、工具函数与通用模式，对每个细节按源码行号展开。

---

## 0. 文件清单与职责总览

### 调和器（4 个 CRD 调和器 + 1 个后台控制器）

| 文件 | 类型 | 管理对象 | 一句话职责 |
|---|---|---|---|
| `worker_controller.go` | `WorkerReconciler` | Worker CR | 独立 Worker 全生命周期（供给/配置/容器/授权） |
| `team_controller.go` | `TeamReconciler` | Team CR | 团队编排：房间、协作上下文、心跳、通道策略、状态聚合 |
| `manager_controller.go` | `ManagerReconciler` | Manager CR | 管理器容器全生命周期 |
| `human_controller.go` | `HumanReconciler` | Human CR | 人类 Matrix 身份 + 房间成员关系 |
| `auto_sleep_controller.go` | `AutoSleepController` | 无 CR（后台） | 定时扫描 Worker 闲置自动睡眠 |

### 分阶段实现文件（Manager / Human）

| 文件 | 所属调和器 | 内容 |
|---|---|---|
| `manager_reconcile_infra.go` | Manager | 基础设施（Matrix/网关/MinIO/房间/凭据） |
| `manager_reconcile_config.go` | Manager | OSS 配置部署（package / manager config / skills） |
| `manager_reconcile_container.go` | Manager | 容器收敛（含 embedded 配置、spec hash 重建判定） |
| `manager_reconcile_welcome.go` | Manager | 首启欢迎消息（容器 Running 后才发送） |
| `manager_reconcile_delete.go` | Manager | 删除清理 + finalizer 移除 |
| `human_reconcile_infra.go` | Human | Matrix 账号注册/稳态零登录 |
| `human_reconcile_rooms.go` | Human | 房间成员关系收敛（invite/join/kick） |
| `human_reconcile_delete.go` | Human | force-leave 全部房间 + 身份 deactivate |

### 共享成员层（Worker 与 Team 复用）

| 文件 | 内容 |
|---|---|
| `member_reconcile.go` | `MemberContext`/`MemberDeps`/`MemberState` + 全部分阶段函数 + sandbox/docker worker-deps 与 token 投影 |
| `member_reconcile_service.go` | K8s ClusterIP Service 阶段（`ReconcileMemberService`） |

### 工具 / scope / 阶段计算

| 文件 | 内容 |
|---|---|
| `labels.go` | `mergeLabels`：Pod 标签四层优先级合并 |
| `user_env.go` | `mergeUserEnv`：系统 env 与用户 env 合并 |
| `manager_scope.go` | `managerScope` + `computeManagerPhase` |
| `human_scope.go` | `humanScope` + `computeHumanPhase` + `buildDesiredHumanRooms` |

测试文件（`*_test.go`，worker/team/human 控制器测试、谓词测试、member 阶段测试、user_env 测试、labels 测试、metrics 测试、manager 容器测试、auto_sleep 测试）不展开。

---

## 1. 通用模式

所有调和器共享以下设计决策：

### 1.1 finalizer 双态（删除前先清理）
统一 finalizer 名 `agentteams.io/cleanup`（`worker_controller.go:34`）。Reconcile 入口先查 `DeletionTimestamp`：
- 有删除标记且含 finalizer → 走 `*Delete` 清理路径（清外部状态后移除 finalizer）
- 无删除标记且无 finalizer → 先添加 finalizer 再走正常路径

四个 CRD（Worker/Team/Manager/Human）全部如此，保证「外部状态清理」永远先于「CR 真正消失」。

### 1.2 scope + defer 统一 status patch
每个调和器用一个 scope 结构（`managerScope`/`humanScope`/`MemberState`）透传跨阶段状态，结尾用 `defer` 统一 `Status().Patch`。
- 用 `client.MergeFrom(x.DeepCopy())` 兜底
- `ObservedGeneration` **只在调和成功时**写入，避免「失败状态写回 → 触发重调和 → Generation != ObservedGeneration」的无限循环 bug（`worker_controller.go:109-136` 注释）
- 删除路径跳过 defer（finalizer 路径自己 `Update`，且 CR 可能已不存在）

### 1.3 metrics 统一埋点
每个 `Reconcile` 开头 `start := time.Now()`，`defer metrics.Observe(kind, start, reterr)`（worker/team/manager/human 各注册自己的 kind）。

### 1.4 分阶段幂等收敛
正常路径是一串**串行阶段**，任何阶段失败即提前返回（错误上抛给 controller-runtime 带退避重入队）；阶段内部幂等，OSS 写入直接覆盖，容器创建用 spec hash 判断是否需要重建。

### 1.5 节奏常量（`worker_controller.go:33-43`）
| 常量 | 值 | 用途 |
|---|---|---|
| `reconcileInterval` | 5m | 正常周期性重入队（Worker/Team/Manager/Human 通用） |
| `reconcileRetryDelay` | 30s | Team 成员缺失等可恢复错误的重试 |
| `edgeReconcileInterval` | 1m | Edge Worker 的周期 |
| `edgeHeartbeatTimeout` | 2m | Edge 心跳超时判定（超时 Phase→Pending） |
| `appServiceNotReadyRequeue` | 5s | Matrix AppService 未就绪（M_UNKNOWN_TOKEN 启动竞态）的短退避 |
| `welcomeRequeueInterval` | 5s | Manager 欢迎消息等待容器 Running 的短重入队（`manager_reconcile_welcome.go:23`） |
| `defaultAutoSleepInterval` | 1m | AutoSleep ticker 间隔（`auto_sleep_controller.go:14`） |

### 1.6 标签合并（`labels.go`）
`mergeLabels(layers...)` 是唯一出口，保证四层优先级恒定：
```
pod-template（最低）< CR metadata.labels < CR spec.labels < controller 系统标签（最高）
```
后层覆盖前层，空层跳过，不修改输入。

### 1.7 env 合并（`user_env.go`）
`mergeUserEnv(sysEnv, userEnv, logger, subject)`：系统 env 打底，用户 env 覆盖同名键。

### 1.8 多实例隔离与事件订阅
- Pod/Sandbox 事件都带 `agentteams.io/controller` 标签谓词过滤，多 controller 实例共享 namespace 不互相 watch
- `PodLifecyclePredicates` / `SandboxLifecyclePredicates`（`worker_controller.go:940/1021`）用 Pod ready 条件 + 容器状态信号过滤冗余事件

---

## 2. WorkerReconciler（`worker_controller.go`）

### 2.1 Reconcile 入口（`:92`）
1. `Get` Worker CR（NotFound 直接忽略）
2. `patchBase := MergeFrom(deepcopy)`，`state := &MemberState{}`
3. `defer`：计算 Phase + 写 `ObservedGeneration`/`Message` + `Status().Patch`（`isEdgeWorker` 且成功时按心跳新鲜度置 Phase）
4. 删除分支 → `reconcileDelete(:358)`；finalizer 分支 → 先加 finalizer
5. 正常 → `reconcileNormal(:173)`

### 2.2 Edge 判定（`:156-169`）
`isEdgeWorker`：`spec.deployMode == edge`。`edgeHeartbeatStale`：`lastHeartbeat` 为空或超过 `edgeHeartbeatTimeout`(2m) 判为陈旧。

### 2.3 reconcileNormal（`:173`）
1. 组装 `MemberDeps`（Provisioner/Deployer/Backend/EnvBuilder/GatewayClient/DynamicClient 等）
2. `effectiveWorkerSpec(:478)` 取生效 spec + 资源 + updateStrategy；`validateWorkerDeploymentTargetImmutable(:483)` 校验部署目标不可变
3. `workerMemberContextWithSpec(:579)` 建 `MemberContext`；`workerTeamName(:335)` 查团队归属
4. `teamRoleForWorker(:454)` 反查角色（standalone/team_leader/worker）；`configOwnedByTeam = inTeam && runtime==qwenpaw`
5. `ResolveModelProvider`（若 `spec.modelProvider` 非空）→ 授权 consumer 到模型 HttpApi
6. 若 `inTeam && role==team_leader`，`configContext.Role = team_leader`（Leader 用 Leader 语义写配置）

**Edge 轻量分支**（`spec.deployMode==edge`，`:223-271`）：
- Edge UUID 轮换：`LabelWorkerEdgeUUID` 变化时删 SA（使旧长令牌失效）→ 换新 UUID 绑定
- 只走 `ReconcileMemberInfra` → `EnsureModelProviderAuth` → `ReconcileMemberConfig`（受 `configOwnedByTeam` 控制）；**不管 Pod/Service/Expose/容器**，SA 生命周期交给 EdgeHandler.ExchangeToken 按需驱动
- Requeue 1m

**常规分支**（`:273-333`）：
1. `ValidateMemberDeployment(:274)`：reject `remote`（开源控制器不支持），edge 必须走 Edge 分支
2. `ReconcileMemberInfra` → `EnsureModelProviderAuth` → `EnsureMemberServiceAccount` → `ReconcileMemberConfig`（`configOwnedByTeam` 时跳过并记日志）
3. `ReconcileMemberContainer` → `applyDeploymentTargetStatus` → `ReconcileMemberService`（K8s Service）
4. Service 名标签 `agentteams.io/service` 写回 Worker CR（注意：**先在 mutation 前快照 base**，否则 MergeFrom 差异为空，标签永远写不上，`:308-317`）
5. `ReconcileMemberExpose`（非致命）→ `applyMemberStateToWorker` → `Status.SpecHash = mctx.AppliedSpecHash`
6. `reconcileManagerAccess(:414)` 授权逻辑
7. Requeue = `min(reconcileInterval, state.RequeueAfter)`

### 2.4 workerTeamName（`:335`）
优先读注解 `agentteams.io/worker-team-name`；否则全量 List Team 按名字排序，找 `spec.workerMembers` 中引用本 Worker 的团队，返回 `EffectiveTeamName`。

### 2.5 reconcileDelete（`:358`）
- **删除保护**：`teamRoleForWorker` 发现仍被 Team 引用 → 记日志并 Requeue，**拒绝删除**（团队成员的删除权归 Team）
- `ReconcileMemberDelete(:393)` 全量清理（忽略错误）
- 撤销 Manager `groupAllowFrom`（`UpdateManagerGroupAllowFrom(false)`）
- 移除 finalizer

### 2.6 reconcileManagerAccess（`:414`）
独立 Worker 与团队成员的 Manager 群聊权限收敛：
- 团队成员：非 Leader → 把 Manager 从 Worker 个人房间 force-leave + 撤销 Manager `groupAllowFrom`；Leader → 不动（TeamReconciler 管）
- 独立 Worker：授予 Manager `groupAllowFrom`（publish 权限进 Manager 群 DM）

### 2.7 teamRoleForWorker（`:454`）
List Team，查 `spec.workerMembers` 引用，返回角色与是否在团队。

### 2.8 部署目标与 spec hash（`:478-916`）
- `effectiveWorkerSpec`：合并 deploymentTarget；`workerSpecWithAppliedDeploymentTarget`：把 status 里的已应用部署目标合回 spec 再比对
- `hashAppliedWorkerSpec(:777)` / `hashAppliedWorkerSpecForRuntime(:814)` / `hashAppliedWorkerSpecForRuntimeAndResources(:824)`：决定容器是否重建；QwenPaw 有专用 `hashQwenPawPodSpec(:867)`；worker-deps 布局版本 `workerDepsLayoutHashVersion(:909)` 用于挂载布局升级
- `computeWorkerPhase(:691)` → `computeMemberPhase`（见 §7.10）

### 2.9 SetupWithManager（`:695`）
Watch Worker 本体 + Pod（`WorkerPodMapFunc(:735)` 按 label `agentteams.io/worker` 映射）+ Sandbox/SandboxClaim，谓词过滤生命周期事件，按 `ControllerName` 隔离。

---

## 3. TeamReconciler（`team_controller.go`）

### 3.1 Reconcile 入口（`:68`）
Get Team CR → 删除分支 `handleDeleteTeam(:1001)`；正常分支先加 finalizer → `reconcileTeam(:343)`。

### 3.2 deriveTeamWithResolvedIdentities（`:172`）
以 Human CR 的 `Status.MatrixUserID` 为权威，回填 Team Admin 与 HumanMembers 的真实 MatrixID，替代旧的 `localpart==name` 推导。

### 3.3 reconcileTeam 七步（`:343`）

**Step1 `validateWorkerMembers(:347)`**：恰好一个 `team_leader`、无重复；失败 → `failTeam`（Phase=Failed，Requeue 30s）。

**Step2 `resolveTeamMembers(:353)`**：从 Worker CR 加载引用快照；有成员缺失 → Phase=Degraded + Requeue 30s。

**Step3 `resolveTeamAdminActor(:364)`**：解析团队管理员 Human 身份（含 token）；失败 → failTeam。

**Step4 团队基础设施（`:370-425`）**：
- `ProvisionTeamRooms`：Team Room + Leader DM Room；写 `Status.TeamRoomID` / `Status.LeaderDMRoomID`；`DisplayNameSyncedGeneration` 追踪
- `syncTeamRoomHumanStatuses`：Human 房间状态同步
- `EnsureTeamStorage`（共享存储，非致命）
- 每个成员 `setWorkerTeamAnnotation`（记录归属）+ `RefreshWorkerCredentials`（按团队名刷新存储访问凭据）

**Step5 协作上下文 + 心跳（`:427-492`）**：
- Leader（非 QwenPaw）：`SyncTeamLeaderAssets`（恢复 team-leader 内置 prompt/skill）→ `InjectCoordinationContext`（团队名/成员/协调者/heartbeat/LeaderSoul）→ `InjectHeartbeatConfig`（`heartbeatEvery` 非空时启用）
- Worker（非 QwenPaw）：`InjectWorkerCoordination`（团队名/Leader/协调者）
- `deployTeamRuntimeConfigs`：QwenPaw/CoPaw/Edge 走 `runtime/runtime.yaml`（失败 → failTeam）

**Step6 通道授权（`:494-534`）**（详见 `channel-policy-guide.md`）：
- Manager 侧：`UpdateManagerGroupAllowFrom(leader, true)`；对每个非 Leader Worker force-leave Manager 出个人房间 + 撤销 `groupAllowFrom(false)`
- Worker 侧：每个成员（非 QwenPaw）`teamChannelPolicy(:863)` 计算 → `Deployer.InjectChannelPolicy` 原地写回 `openclaw.json`

**Step7 状态聚合（`:536-552`）**：`cleanupStaleTeamMembers` + `aggregateTeamStatus`（leaderReady / readyWorkers / totalWorkers）→ `Status().Patch` → Requeue 5m。

### 3.4 detachTeamMember（`:792`）
成员脱离团队的回退（Team 删除或成员移除）：
1. `DropTeamContext`：撤销 Worker 的团队协作上下文
2. Manager 重新邀请回 Worker 个人房间
3. 撤销 Manager `groupAllowFrom`（非 QwenPaw）
4. `InjectChannelPolicy` 重注独立语义允许列表 `[manager, 系统管理员, 团队管理员]`（`uniqueTeamStrings` 去重）

### 3.5 teamChannelPolicy（`:863`）
计算核心，见 `channel-policy-guide.md` §4-5：
- Leader：groupAllow = `[manager, systemAdmin, coordinators..., 全体成员]`；dmAllow = `[manager, systemAdmin, coordinators...]`
- Worker：groupAllow = `[leader, systemAdmin, coordinators..., peers?]`（`spec.peerMentions` 默认开启才含 peers）；dmAllow = `[leader, systemAdmin, coordinators...]`
- 系统管理员恒在；`coordinatorIDs = teamCoordinatorIDs(:1298)`（Team admin + HumanMembers 中 role 为空或 `coordinator`）
- `mergeChannelPolicy(:1401)`：团队级优先、个人级追加；`applyChannelAllowPolicy(:940)`：base + allowExtra − denyExtra；`uniqueTeamStrings(:1321)`：去空去重

### 3.6 handleDeleteTeam（`:1001`）
逐成员 `detachTeamMember` → 移除 Leader heartbeat（`InjectHeartbeatConfig Enabled=false`）→ 撤销 Leader 的 Manager `groupAllowFrom` → `archiveTeamRooms` + `DeleteTeamRoomAliases`（保证同名团队可重建）→ 移除 finalizer。**不销毁 Worker CR/容器**（Team 是引用式，不拥有 Worker 生命周期）。

### 3.7 SetupWithManager（`:1337`）
Watch Team + Worker status 变化（字段索引 `spec.workerMembers.name`）：任一 Worker 变化即触发对应 Team 重调和。

---

## 4. ManagerReconciler（`manager_controller.go` + 5 个分阶段文件）

### 4.1 Reconcile 入口（`:77`）
`ManagerEmbeddedConfig`（embedded 模式专用：workspace 挂载 / host-share / extra env / console 端口）。`ManagerReconciler` 字段含 `DefaultRuntime`（来源 `AGENTTEAMS_MANAGER_RUNTIME`，与 Worker 的 `DefaultRuntime` 区分——Backend.Create 无法区分哪个 env 适用）。

`reconcileManagerNormal(:136)` 串行：modelProvider 解析 → infra → consumer 授权 → ServiceAccount → config → container → welcome → Requeue 5m。

### 4.2 reconcileManagerInfrastructure（`manager_reconcile_infra.go:15`）
- 已存在（`Status.MatrixUserID != ""`）：`RefreshManagerCredentials` + `EnsureManagerGatewayAuth`——**网关授权错误必须上抛**（曾因吞错导致数据面 `allowedConsumers` 空、Higress PUT 非 200 被掩盖）
- 首次：`ProvisionManager` 写回 `MatrixUserID`/`RoomID`/`provResult`

### 4.3 reconcileManagerConfig（`manager_reconcile_config.go:15`）
`DeployPackage` → `DeployManagerConfig`（matrixToken/gatewayKey/McpServers/AIGatewayURL）→ `PushOnDemandSkills`（失败仅记日志）。

### 4.4 reconcileManagerContainer（`manager_reconcile_container.go`）
- `ensureManagerContainerPresent(:43)` / `ensureManagerContainerAbsent(:119)`：按 `spec.state` 收敛
- `managerSpecChanged(:260)` + `hashAppliedManagerSpec(:291)`：spec 哈希变化才重建容器
- `applyEmbeddedConfig(:223)`：embedded 模式注入 workspace/host-share/env/console 端口
- `managerBackend(:306)`：按 backend registry 选 Pod/Sandbox 后端

### 4.5 reconcileManagerWelcome（`manager_reconcile_welcome.go:103`）
**必须在容器起来之后**（Manager 的 Matrix 用户首次 `/sync` 加入 Admin DM 后才发）——提前发送会落成历史消息被跳过。容器未 Running 时短路并 5s 重入队。

### 4.6 reconcileManagerDelete（`manager_reconcile_delete.go:13`）
LeaveAllManagerRooms → DeleteManagerRoom → DeprovisionManager → 删容器 → CleanupOSSData → DeleteCredentials → DeleteManagerServiceAccount → 释放房间别名（保留房间，只释放别名，同名 Manager 可重建）→ 移除 finalizer。全部非致命，不让外部故障卡死 finalizer。

### 4.7 managerScope + computeManagerPhase（`manager_scope.go`）
`computeManagerPhase`：失败且未供给 → Failed；失败且无阶段 → Pending；成功 → `spec.DesiredState()`。

### 4.8 SetupWithManager（`:184`）
Watch Manager + Pod/Sandbox/SandboxClaim（按 `LabelManager` 映射 + 谓词隔离）。

---

## 5. HumanReconciler（`human_controller.go` + 3 个分阶段文件）

Human 无容器、无网关 consumer、无 MinIO 账号，只有「Matrix 账号 + 房间成员关系」。

### 5.1 Reconcile 入口（`:35`）
`humanScope{human, username, patchBase, identity, userToken}`。删除分支 `reconcileHumanDelete`；正常分支 `reconcileHumanNormal(:109)`：
1. `resolveHumanScope` → 失败置 Degraded + Requeue 5m
2. `reconcileHumanInfra` → Matrix AppService 未就绪则 5s 重入队；错误 Requeue 5m
3. `reconcileHumanRooms`（**不返回错误**，房间部分故障不阻塞）

### 5.2 resolveHumanScope（`:128`）
`humanidentity.ResolveHuman`（支持 SSO / legacy-password 身份源）。**身份固化**：一旦 `Status.MatrixUserID` 已存在且与解析结果不同 → 报错 `identitySource changed; recreate CR to switch identity`（防止 Status.Rooms 指向旧账号）。不管理初始密码的身份源会把 `InitialPassword` 清空。

### 5.3 reconcileHumanInfra（`human_reconcile_infra.go:35`）
- 首次供给（`MatrixUserID == ""` 或需重建）：身份源 `EnsurePrecreated` 注册账号，写 `MatrixUserID`/`InitialPassword`，seed `userToken`
- **稳态零登录**：`MatrixUserID != ""` 后**什么都不做**，`userToken` 故意留空——房间阶段仅在真正有新房间要 `/join` 时才 `ensureUserToken`（Tuwunel 对不带 device_id 的 login 每次创建新设备会话，若每 5 分钟轮询都 login，一天产生 ~288 台孤儿设备）
- 刻意不回退 `EnsureHumanUser`：其孤儿恢复分支会 `reset-password`，覆盖用户在 Element 里改过的密码
- displayName 同步：首次或 spec generation 变化时 `SetDisplayName`，成功后记 `DisplayNameSyncedGeneration`

### 5.4 reconcileHumanRooms（`human_reconcile_rooms.go:27`）
- `buildDesiredHumanRooms(:68)`：`Spec.AccessibleWorkers`/`AccessibleTeams` → 各自主的 RoomID（未就绪的跳过，下次再取）
- 新增：admin `InviteToRoom` → 惰性 `ensureUserToken` 拿 token → `JoinRoomAs`（token 拿不到只邀请不 join，记为 pending 不进 `Status.Rooms`）
- 移除：admin `KickFromRoom`（失败保留在 Status.Rooms 供下次重试）
- 单房间失败只记日志，函数跑完所有房间

### 5.5 ensureUserToken（`:102`）
`LoginWithPassword` 拿 token 并缓存到 scope；失败返回 `""`（陈旧密码是预期状态而非调和失败），调用方降级为纯邀请。

### 5.6 reconcileHumanDelete（`human_reconcile_delete.go:22`）
无法以 human 身份 `/leave`（密码可能已改），改用 Tuwunel admin bot `ForceLeaveRoom` 逐房间 force-leave → 身份源 `EnsureDeactivated`（失败 Requeue 5m 重试）→ 移除 finalizer。`delete_rooms_after_leave`/`forget_forced_upon_leave` 兜底。

### 5.7 computeHumanPhase（`human_scope.go:37`）
Degraded 恒定为 Degraded；失败时 MatrixUserID 为空 → Failed，否则保持原阶段；成功时 MatrixUserID 为空 → Pending，否则 Active。

---

## 6. AutoSleepController（`auto_sleep_controller.go`）

非 CR 调和器，无 informer，纯后台定时：
- `Start(:23)`：先跑一轮，再 `time.NewTicker(Interval)`（默认 1m）循环
- `reconcile(:45)`：List 全部 Worker → `shouldSleep(now, DesiredState, IdleTimeout, LastActiveAt)` 命中则 `setWorkerState(worker, "Sleeping")`
- `shouldSleep(:66)`：`Running` + `idleTimeout` 可解析 + `now-lastActiveAt > timeout`
- `setWorkerState(:81)`：`retry.RetryOnConflict` 写 `spec.state=Sleeping`（先判同态避免无谓写）
- Team 成员也是 Worker CR，同一循环天然覆盖团队内 Worker

---

## 7. 共享成员层（`member_reconcile.go`，2065 行）

### 7.1 角色与结构（`:33-241`）
- `MemberRole`：`RoleStandalone` / `RoleTeamLeader` / `RoleTeamWorker`（`:35-39`）
- `MemberContext`（`:81`）：`Name`（CR/Pod/SA key）/ `RuntimeName`（Matrix/OSS/房间别名 key）/ `TeamName` / `Role` / `Spec` / `SpecChanged` / `ExistingMatrixUserID` / `ExistingRoomID` / `DeployMode` / `ModelProviderInfo` / `DisplayName` 等。**Generation 仅用于日志，不用于 spec 变更检测**——调用方必须显式设 `SpecChanged`
- `MemberDeps`（`:241`）：服务层依赖（Provisioner/Deployer/Backend/EnvBuilder/GatewayClient/DynamicClient/DefaultRuntime 等）
- `MemberState`（`:179`）：调和输出（MatrixUserID/RoomID/ProvResult/ContainerState/BackendRuntime/ExposedPorts/RequeueAfter 等）

### 7.2 ValidateMemberDeployment（`:288`）
`local` 或空 → 通过；`remote` → 报错（开源控制器不支持，改用 local 或 Edge）；`edge` → 必须走 Edge 分支。

### 7.3 ReconcileMemberInfra（`:304`）
- 稳态（`ExistingMatrixUserID != ""`）：`RefreshWorkerCredentials(name, runtimeName, teamName)` 刷新，`syncMemberDisplayName(:352)` 同步显示名
- 首次：`ProvisionWorker{Name: RuntimeName, CredentialName: Name, Role}`；Matrix AppService 未就绪 → 5s 重入队
- 写 `state.MatrixUserID/RoomID/ProvResult`

### 7.4 EnsureModelProviderAuth（`:367`）
`modelProviderInfo` 与 `gatewayKey` 就绪时 `AuthorizeAIRoutes(consumer="worker-"+runtimeName, httpApiID)`；否则 no-op。

### 7.5 EnsureMemberServiceAccount（`:384`）
与 Infra 分离是因为 SA 创建可能跟 namespace 就绪竞态，独立重试更好。

### 7.6 ReconcileMemberConfig（`:393`）
按生效 runtime 分叉：
- **QwenPaw / Edge**：`DeployMemberRuntimeConfig` 写 `runtime/runtime.yaml`（Edge 用 `runtimeRemoteManagedLocal`，注入 matrixAccessToken/gatewayKey）
- **OpenClaw（默认）**：`DeployPackage` → `WriteInlineConfigs` → `DeployWorkerConfig`（→ `GenerateOpenClawConfig` → PutObject `agents/<runtime>/openclaw.json`）→ `PushOnDemandSkills`（失败仅日志）
- `runtimeSkillRegistryConfig(:460)`：从 `EnvBuilder.Build` 取 `SKILLS_API_URL`/`NACOS_AUTH_TYPE`

### 7.7 ReconcileMemberContainer（`:470`）
`!DesiredContainerMan` 时跳过（用户自管进程，如 systemd）。按期望状态分派：
- `Stopped` → `ensureMemberContainerAbsent(remove=true)`
- `Sleeping` → `ensureMemberContainerAbsent(remove=false)`
- 默认（Running）→ `ensureMemberContainerPresent(:495)`

`ensureMemberContainerPresent` 关键逻辑：
- **后端切换**：`BackendRuntime` 变化时先删旧后端资源再建新后端（`:518-526`）
- spec hash 对比决定重建（`memberRuntimeStale(:734)`）
- `createMemberContainer(:741)`：先 `waitForScopedWorkerConfig(:840)` 等配置就绪，sandbox 场景 `refreshSandboxSetWorkerDeps(:888)` 刷新 worker-deps，`prepareMemberWorkerDeps(:919)` 组装挂载对象

### 7.8 worker-deps 与 token 投影（`:986-1454`）
- sandbox 挂载对象族：StorageClass / PV / PVC / CredentialProvider / AgentIdentity / AgentRole / AgentRoleBinding / Secret（`buildWorkerDeps*` 系列，动态 GVR 创建/更新）
- 认证模式：`RRSA` / `AccessKey`（`workerDepsAuthTypeRRSA` / `workerDepsAuthTypeAccessKey`），挂载路径预留校验（`isWorkerDepsReservedMount`）
- token 投影：`sandboxSetTokenProjections` / `dockerTokenProjections`（`sync.Map`），`projectSandboxSetWorkerToken(:1257)` / `projectInitialDockerWorkerToken(:1344)` / `refreshDockerWorkerToken(:1353)`，带 jitter 的刷新计划（`tokenRefreshJitter(:1338)`）

### 7.9 状态/暴露/删除
- `computeMemberPhase(:1911)`（见下）
- `extraHostsForBackend(:1946)`：docker 后端加 `host.docker.internal:host-gateway`
- `ReconcileMemberExpose(:1956)`：网关端口暴露，非致命（`gateway.ErrUnsupportedOp` 视为跳过）
- `ReconcileMemberDelete(:1978)`：清 token 投影 → `LeaveAllWorkerRooms` → `DeleteWorkerRoom` → `DeprovisionWorker` → 删容器/SA/OSS（见 §7.11）

### 7.10 computeMemberPhase（`:1911`）
- 错误：MatrixUserID 空 → Failed；无当前阶段 → Pending；否则保持
- Sleeping：容器 `stopping` → Stopping，否则 Sleeping
- Stopped：容器 `stopping` → Stopping，否则 Stopped
- Running：容器 running/ready → Running；starting → Starting；failed → Failed；否则 Pending

### 7.11 ReconcileMemberDelete 细节（`:1978-2065`）
1. 清理 `sandboxSetTokenProjections` / `dockerTokenProjections`（`:1981-1982`）
2. `LeaveAllWorkerRooms` → `DeleteWorkerRoom`（`ExistingRoomID` 非空时）
3. `DeprovisionWorker{Name, IsTeamWorker:false, ExposedPorts, ExposeSpec}`（`:1994`）
4. 删除容器（`:2015-2038`）：按 `StatusBackendRuntime`（兜底 `BackendRuntime`/pod）选后端 `wb.Delete`，ErrNotFound 容忍；**双后端安全网**——若 spec 与 status 后端不一致（上次调和留下的半完成切换），再尝试从 spec 侧后端删一次，防资源泄漏
5. `CleanupOSSData` → `DeleteWorkerCredentials` → `DeleteServiceAccount`（均非致命）
6. `DeleteWorkerRoomAlias`（`:2053`）：释放每个 Worker 私有 comm 房间别名，**保留房间本身**以留存历史，同名 Worker/Team 可重建
7. `ensureServiceDeleted`（`:2058-2062`）：按标签选择清理该成员的 ClusterIP Service
8. finalizer 移除归调用方（WorkerReconciler 负责）

---

## 8. K8s Service 阶段（`member_reconcile_service.go`）

`ReconcileMemberService(:23)`：
- `!ServiceEnabled` → `ensureServiceDeleted`
- 启用但 `spec.expose` 无端口 → 跳过（无端口 Service 无意义）
- 启用且有端口 → `ensureServiceExists(:33)`：按 `serviceName(:144)` / `serviceSelector(:150)` / `buildServicePorts(:157)`（intstr 端口处理）创建/更新 ClusterIP Service
- **必须在 `ReconcileMemberContainer` 之后运行**，保证 Pod selector 标签已存在

---

## 9. 数据流全景（一句话串起来）

```
事件(CR/Pod/Sandbox/定时) ──► 各 Reconcile 入口
  ├─ finalizer 分支 ─► *Delete 清理 ─► 移除 finalizer
  └─ 正常分支 ─► 分阶段收敛（scope 透传）─► defer 统一 Status().Patch ─► Requeue
       Worker/Team ─► member_reconcile.go 共享阶段 ─► openclaw.json / runtime.yaml / 容器
       Manager    ─► manager_reconcile_* 五阶段
       Human      ─► human_reconcile_* 三阶段
后台：AutoSleep 定时把闲置 Worker 置 Sleeping ─► 触发 WorkerReconciler 容器收敛
```

---

## 10. 设计要点回看

1. **Team 不拥有 Worker 生命周期**：Worker 删除被 Team 引用时阻塞；Team 删除只回退、不销毁 Worker。
2. **所有外部清理都是 best-effort**：外部故障不卡 finalizer，靠 Requeue 重试。
3. **稳态零副作用**：Human 懒登录防设备膨胀、Worker/Manager 凭据刷新路径避免重复 Login。
4. **spec hash 驱动重建**：容器重建与否由 spec 哈希（含 runtime/资源/worker-deps 布局版本）决定，幂等且最少打扰。
5. **统一 status patch + ObservedGeneration 防抖**：消除失败状态写回导致的无限调和。
