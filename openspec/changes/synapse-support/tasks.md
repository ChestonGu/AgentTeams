## 0. 基础设施搭建（Phase 1 之前）

- [x] 0.1 在 `agentteams-controller/internal/matrix/types.go` 新增业务类型：`RoomSpec`、`RoomRef`、`UserSpec`、`UserRef`、`MemberSpec`、`RoomMetadata`、`AppServiceRegistration`（如复用现有则标注）；同时在 `Config` 加 `Provider string` 字段
- [x] 0.2 新增 `agentteams-controller/internal/matrix/ops.go`：定义 `MatrixOps` 接口骨架（按 6 业务能力分组注释，先列 Phase 1 的 4 个方法，其余方法随 Phase 推进追加）；加编译时断言 `var _ MatrixOps = (*TuwunelMatrixOps)(nil)` 占位
- [x] 0.3 在 `agentteams-controller/internal/config/config.go` 新增 `MatrixProvider` 字段，从 `AGENTTEAMS_MATRIX_PROVIDER` env 读取（默认 `tuwunel`），未知值 panic；在 `MatrixConfig()` 把 Provider 传给 `matrix.Config`
- [x] 0.4 在 `agentteams-controller/internal/app/app.go`（或 controller 构造 matrix client 的位置）按 `cfg.MatrixProvider` 选择 `NewTuwunelMatrixOps` / `NewSynapseMatrixOps`；未知 provider fail-fast
  - **注**：现有 app.go 已用 `cfg.UsesSynapse()` 选择 `matrix.NewSynapseClient` / `matrix.NewTuwunelClient`（旧的 matrix.Client 协议层）。Phase 0 仅修复编译（添加 UsesSynapse 方法到 config.go）。Phase 1 完成后，此选择点会改为构造对应的 `*MatrixOps` 实现，并注入到 Provisioner。
- [x] 0.5 新增 `agentteams-controller/internal/matrix/provider.go`（`NewOps` 工厂）+ `provider_test.go`：覆盖 tuwunel/unset → TuwunelMatrixOps；synapse → SynapseMatrixOps；dendrite → error；`app.go::initInfraClients` 改用该工厂

## 1. Phase 1 — 4 核心操作（验证抽象边界）

目标：抽 `CreateRoom` / `DissolveRoom` / `AddMember` / `RemoveMember` 到 MatrixOps；两个实现；Provisioner 对应调用点切换；验证 Tuwunel 零回归 + Synapse make_room_admin fallback 工作。

### TuwunelMatrixOps 实现（搬迁现有逻辑）

- [x] 1.1 新增 `agentteams-controller/internal/matrix/tuwunel_ops.go`：实现 `CreateRoom(ctx, RoomSpec) (RoomRef, error)`——把 `RoomSpec` 转 `matrix.CreateRoomRequest`（CreatorToken 默认空→admin token），调用嵌入的 `*TuwunelClient.CreateRoom`；保留 alias 幂等（Created=false 分支）
- [x] 1.2 `TuwunelMatrixOps.DissolveRoom(ctx, roomID)`：发送 `"!admin rooms delete-room <roomID>"`（搬迁 `provisioner.go:293-298` 的 `deleteRoom`）
- [x] 1.3 `TuwunelMatrixOps.AddMember(ctx, roomID, userID)`：调用 `InviteToRoom(ctx, roomID, userID)`（admin token 路径），保留 `"already in"` 幂等
- [x] 1.4 `TuwunelMatrixOps.RemoveMember(ctx, roomID, userID, reason)`：调用 `KickFromRoom`；**修复现有 BUG**（见 contracts §1 修复 1）：精确匹配 `"target user is not in"` 为幂等 nil，移除 `"cannot kick"` 误匹配；`shouldForceLeaveAfterKickError` 触发后 fallback 到 `"!admin users force-leave-room <userID> <roomID>"`
- [x] 1.5 `TuwunelMatrixOps` 持有 `*TuwunelClient`（嵌入或字段）+ `Config`；构造函数 `NewTuwunelMatrixOps(cfg Config, http *http.Client) *TuwunelMatrixOps`

### SynapseMatrixOps 实现（基于源码核对）

- [x] 1.6 新增 `agentteams-controller/internal/matrix/synapse_admin.go`：实现 Synapse admin API 客户端方法 `MakeRoomAdmin(ctx, roomID, userID)` → `POST /_synapse/admin/v1/rooms/{id}/make_room_admin`；`DeleteRoom(ctx, roomID)` → `DELETE /_synapse/admin/v2/rooms/{id}`（fire-and-forget）；基于 `synAdminCall` 模式
- [x] 1.7 新增 `agentteams-controller/internal/matrix/synapse_ops.go`：实现 `SynapseMatrixOps` 结构体 + 构造函数；持有 `*TuwunelClient`（复用 CS API 方法）+ `synapseAdmin` + `Config`
- [x] 1.8 `SynapseMatrixOps.CreateRoom`：复用 `TuwunelClient.CreateRoom`，但**自动注入 creator**（contracts §4 修复 4）：解析 creator user_id（admin），若不在 `RoomSpec.PowerLevels` 中则注入 PL=100
- [x] 1.9 `SynapseMatrixOps.DissolveRoom`：调用 `synapseAdmin.DeleteRoom`（fire-and-forget）
- [x] 1.10 `SynapseMatrixOps.AddMember`：先 `InviteToRoom`（admin token）；若错误是 sender 不在房（`"@x not in room"`，contracts §2 I1）或 PL 不足（`"You don't have permission to invite"`），调 `MakeRoomAdmin(roomID, adminUserID)` → 重试 `InviteToRoom`；保留 `"is already in the room"` 幂等
- [x] 1.11 `SynapseMatrixOps.RemoveMember`：先 `KickFromRoom`；精确幂等匹配（`"target user is not in"` → nil）；若错误是 sender 不在房或 PL 不足（`"cannot kick user"`/`"@x not in room"`），调 `MakeRoomAdmin` → 重试 `KickFromRoom`；**不**调用不存在的 `/_synapse/admin/v1/rooms/.../kick`（contracts §3）

### 业务层迁移（Phase 1 调用点）

- [x] 1.12 修改 `agentteams-controller/internal/service/provisioner.go::Provisioner`：新增 `matrixOps matrix.MatrixOps` 字段；保留旧 `matrix matrix.Client` 字段（过渡期）；`ProvisionerConfig` 加 `MatrixOps` 字段
  - 注：`NewProvisioner` 在 `MatrixOps` 未配置时用 `matrix.NewLegacyClientOps(Matrix, MatrixConfig)` 包装，保证仅提供 `Matrix` 的现有调用方/测试零改动（1.27 转换层）
- [x] 1.13 迁移 `ProvisionWorker` 的 CreateRoom 调用点（`provisioner.go:444, 455`）：从 `p.matrix.CreateRoom(ctx, matrix.CreateRoomRequest{...})` 改为 `p.matrixOps.CreateRoom(ctx, roomSpec)`；在调用点构造 `RoomSpec`（PowerLevels/Invite/Metadata 等）
- [x] 1.14 迁移 `ProvisionTeamRooms` 的 CreateRoom 调用点（`provisioner.go:820, 888`）—— 2 个房型（team room / leader DM 带 `ActorUserID: teamAdminID` + `ActorToken: req.TeamAdminActorToken`）
- [x] 1.15 迁移 `ProvisionManager` 的 CreateRoom 调用点（`provisioner.go:1437`）—— manager DM
- [x] 1.16 迁移 `Provisioner.deleteRoom`（`provisioner.go:293-298`）→ `p.matrixOps.DissolveRoom(ctx, roomID)`；删除原 `deleteRoom` 私有方法
- [x] 1.17 迁移 `EnsureRoomMember`（`provisioner.go:1081-1083`）→ `p.matrixOps.AddMember(ctx, roomID, userID)`
- [x] 1.18 迁移 `EnsureRoomNonMember`（`provisioner.go:1087-1089`）→ `p.matrixOps.RemoveMember(ctx, roomID, userID, reason)`
- [x] 1.19 迁移 `ReconcileRoomMembership` 内部的 `InviteToRoom` / `KickFromRoom` 分支（`provisioner.go:1140-1142, 1172-1174`）→ 对应 MatrixOps 方法（非 actor 分支走 `AddMember`/`RemoveMember`；actor-token 分支 `InviteToRoomWithToken`/`KickFromRoomWithToken` 暂留 `p.matrix`，Phase 2 随 `ReconcileMembers` 整体迁移；外层 force-leave fallback 保留服务于 actor 分支）
- [x] 1.20 修改 `ForceLeaveRoom`（`provisioner_human.go:190-203`）→ 内部改为调 `p.matrixOps.RemoveMember`（去掉对 `!admin users force-leave-room` 字符串的直接构造）

### Phase 1 测试

- [x] 1.21 新增 `agentteams-controller/internal/matrix/tuwunel_ops_test.go::TestTuwunelOps_CreateRoom` / `_DissolveRoom` / `_AddMember` / `_RemoveMember`：用 httptest.Server mock Tuwunel，断言 CS API 调用 + admin bot 命令字符串与现有 provisioner_test 期望一致
- [x] 1.22 新增 `synapse_ops_test.go::TestSynapseOps_CreateRoom_AutoInjectsCreator`：RoomSpec.PowerLevels 不含 admin → 请求体 users 含 admin=100
- [x] 1.23 新增 `TestSynapseOps_AddMember_FallbackViaMakeRoomAdmin`：mock 首次 invite 返回 403 `"@admin not in room"`，断言调用 make_room_admin 后重试成功
- [x] 1.24 新增 `TestSynapseOps_RemoveMember_Idempotent_TargetNotInRoom`：返回 `"The target user is not in the room"` → nil
- [x] 1.25 新增 `TestSynapseOps_RemoveMember_FallbackViaMakeRoomAdmin`：返回 `"You cannot kick user"` → 调 make_room_admin → 重试
- [x] 1.26 新增 `TestSynapseOps_DissolveRoom_UsesV2Delete`：断言 DELETE `/v2/rooms/{id}` 被调用
- [x] 1.27 跑全量现有测试（`provisioner_team_test.go` / `client_test.go` / `appservice_test.go`）确认 Tuwunel 零回归；如有 break，修复转换层

## 2. Phase 2 — 全部成员管理 + 房间生命周期

- [x] 2.1 MatrixOps 接口追加：`ReconcileMembers` / `JoinRoom` / `LeaveRoom` / `ForceLeaveAllRooms` / `ReleaseRoomAlias` / `ResolveRoomAlias` / `ArchiveRoom`
- [x] 2.2 `TuwunelMatrixOps` 实现这 7 个方法（搬迁 `provisioner.go::ReconcileRoomMembership` / `leaveAllRooms` / `DeleteTeamRoomAliases` / `DeleteWorkerRoomAlias` / `DeleteManagerRoomAlias` / `ArchiveTeamRooms` 的底层调用）
- [x] 2.3 `SynapseMatrixOps` 实现这 7 个方法（JoinRoom/LeaveRoom/ForceLeaveAllRooms 直接转发 CS API；ReleaseAlias 转发；ArchiveRoom 用 RenameRoom；ReconcileMembers 组合 AddMember/RemoveMember）
- [x] 2.4 迁移 `Provisioner.ReconcileRoomMembership`（`provisioner.go:1097-1204`）整个函数 → 调 `p.matrixOps.ReconcileMembers`；保留 actorToken 透传（通过 MemberSpec）
- [x] 2.5 迁移 `leaveAllRooms`（`provisioner.go:258-287`）→ 调 `p.matrixOps.ForceLeaveAllRooms`
- [x] 2.6 迁移 `DeleteTeamRoomAliases` / `DeleteWorkerRoomAlias` / `DeleteManagerRoomAlias`（`provisioner.go:1283, 1320, 1331`）→ 调 `p.matrixOps.ReleaseRoomAlias`
- [x] 2.7 迁移 `ArchiveTeamRooms`（`provisioner.go:1300-1315`）→ 调 `p.matrixOps.ArchiveRoom`
- [x] 2.8 跑全量现有测试 + 新增 Synapse 风格用例

## 3. Phase 3 — 元数据 + 消息 + 查询

- [x] 3.1 MatrixOps 接口追加：`SetRoomMetadata` / `RenameRoom` / `SendSystemMessage` / `ListRoomMembers` / `ListJoinedRooms` / `IsUserInRoom` / `IsManagerJoinedDM` / `HealthCheck`
- [x] 3.2 `TuwunelMatrixOps` 实现这 8 个方法（搬迁现有逻辑，包括 `SetRoomState` / `SetRoomName` / `SendMessageAsAdmin` / `ListRoomMembers*` / `ListJoinedRooms`）
- [x] 3.3 `SynapseMatrixOps` 实现这 8 个方法；`SetRoomMetadata`/`RenameRoom`/`SendSystemMessage` 加 make_room_admin fallback（与 AddMember 同款）；查询方法直接转发（admin bypass in-room 检查）
- [x] 3.4 迁移 `Provisioner` 内的 `SetRoomState` 调用点（`provisioner.go:483, 871, 936, 1452`）→ `SetRoomMetadata`
- [x] 3.5 迁移 `SetRoomName` 调用点（`provisioner.go:838, 1304, 1310`）→ `RenameRoom`
- [x] 3.6 迁移 `SendManagerWelcomeMessage`（`provisioner.go:1652`）→ `SendSystemMessage`
- [x] 3.7 迁移 `ListRoomMembers*` 调用点（`provisioner.go:1113, 1224, 1232, 1622`）→ `ListRoomMembers`
- [x] 3.8 迁移 `ListJoinedRooms` 调用点（`provisioner.go:275`）→ `ListJoinedRooms`
- [x] 3.9 迁移 `IsManagerJoinedDM`（`provisioner.go:1617-1637`）→ `IsManagerJoinedDM`
- [x] 3.10 迁移 `Initializer.waitForMatrix`（`initializer.go:182-191`）→ `MatrixOps.HealthCheck`
- [x] 3.11 跑全量测试 + 新增 Synapse 元数据 fallback 用例

## 4. Phase 4 — 用户身份 + AppService 治理 + 修复硬编码

- [x] 4.1 MatrixOps 接口追加：`ProvisionUser` / `ProvisionUserViaAppService` / `LoginUser` / `LoginUserViaAppService` / `ResetUserPassword` / `DeactivateUser` / `SetUserDisplayName` / `VerifyUserAccessToken` / `UserIDFor` / `BackfillLegacyPassword` / `RegisterAppService` / `UnregisterAppService` / `SmokeTestAppService`
- [x] 4.2 `TuwunelMatrixOps` 实现这 13 个方法（搬迁现有 `EnsureUser` / `EnsureAppServiceUser` / `Login` / `LoginAppServiceUser` / `SetPasswordAsAdmin` / `SetDisplayName` / `VerifyAccessToken` / `UserID` / `RegisterAppService` / `UnregisterAppService` / `AppServiceSmokeTest`）
- [x] 4.3 `SynapseMatrixOps` 实现这 13 个方法：`ProvisionUser` 用 `PUT /_synapse/admin/v2/users/{id}`；`ResetUserPassword` 用 `POST /_synapse/admin/v1/reset_password/{id}`；`DeactivateUser` 用 `POST /_synapse/admin/v1/deactivate/{id}`；`RegisterAppService` 仅做 `SmokeTestAppService`（声明式）；`UnregisterAppService` 返回错误指引 Helm
- [x] 4.4 迁移 `Provisioner` 内的 `EnsureUser` / `EnsureAppServiceUser` 调用点（`provisioner.go:364, 370, 1393, 1399`）→ `ProvisionUser` / `ProvisionUserViaAppService`
- [x] 4.5 迁移 `ensureMatrixToken` / `RefreshWorkerCredentials` 的 Login 调用点（`provisioner.go:604, 606, 632, 634`）→ `LoginUser` / `LoginUserViaAppService`
- [x] 4.6 迁移 `SetPasswordAsAdmin` / `SetDisplayName` 调用点（`provisioner.go:1756`、`provisioner_human.go:72, 163`）→ `ResetUserPassword` / `SetUserDisplayName`
- [x] 4.7 迁移 `provisioner_human.go::DeactivateHumanUser`（line 199-203，调 `!admin users deactivate`）→ `DeactivateUser`
- [x] 4.8 迁移 `Initializer.registerAdmin`（`initializer.go:193-199`）→ `MatrixOps.EnsureUser`
- [x] 4.9 迁移 `Initializer.registerAppService`（`initializer.go:203-220`）→ `MatrixOps.RegisterAppService` + `SmokeTestAppService`
- [x] 4.10 **修复硬编码**：重写 `appservice_mgmt_handler.go::RotateToken`——通过注入的 `matrixCfg.Provider` 判断；synapse → 返回 501 + Helm 指引；tuwunel → 通过 MatrixOps 走 RegisterAppService 流程（不再 `NewTuwunelClient`）
- [x] 4.11 修改 `NewAppServiceHandler` 签名接收 `MatrixOps`（或保留 `matrix.Config` 仅用 Provider 字段，rotate 通过单独方法）；更新 `http.go` 的构造调用
- [x] 4.12 跑全量测试 + 新增 Synapse AS 用例（RegisterAppService smoke-test-only / UnregisterAppService 报错 / RotateToken 501）

## 5. Phase 5 — 清理 + 文档

- [x] 5.1 grep 确认 `internal/service/` / `internal/initializer/` / `internal/server/` 不再引用 `matrix.Client` / `matrix.TuwunelClient` / `matrix.SynapseClient` / `"!admin`
- [x] 5.2 把 `Provisioner.matrix` 字段（`matrix.Client`）移除；`ProvisionerConfig.Matrix` 字段移除；只保留 `MatrixOps`
- [x] 5.3 `Initializer.Matrix` 字段类型改为 `MatrixOps`
- [x] 5.4 把 `matrix.Client` 接口转为内部使用（可改名 `httpClient` 或保留，但不再被业务层 import）；`SynapseClient.AdminCommand` 翻译逻辑上移到 `SynapseMatrixOps`，`synapse_client.go` 可逐步退化或合并进 `synapse_ops.go`
- [x] 5.5 新增 `agentteams-controller/internal/matrix/ops_exhaustive_test.go`：跨实现的行为等价性测试矩阵（每个方法 × Tuwunel/Synapse × 成功/失败场景）
- [x] 5.6 新增 `docs/synapse.md`：声明式 AS 工作原理、Helm values 配置示例、token 轮转流程、命名空间安全模型、provider 切换
- [x] 5.7 更新 `changelog/current.md`：feat(matrix): introduce MatrixOps business abstraction + Synapse 1.127 support；fix: kick idempotency & ForceLeaveRoom fallback；fix: appservice_mgmt_handler hardcoded TuwunelClient
- [x] 5.8 在 `design/matrix-call-sites-inventory.md` / `design/synapse-support.md` / `design/synapse-interface-contracts.md` 顶部标注"已转化为 openspec proposal `synapse-support`，本文档作为历史依据"

## 6. Helm 声明式 AS（与 Phase 并行，独立可验证）

- [x] 6.1 新增 `helm/agentteams/templates/matrix/synapse-appservice-secret.yaml`：条件 `matrix.provider=synapse AND matrix.mode=managed AND matrix.appservice.enabled`；渲染 Secret `<synapse-fullname>-appservice`，`stringData.agentteams-controller.yaml` 含 id/as_token/hs_token/sender_localpart/namespaces/url/rate_limited
- [x] 6.2 修改 `helm/agentteams/templates/matrix/synapse-configmap.yaml`：AS 启用时追加 `app_service_config_files: [/as-registrations/agentteams-controller.yaml]`
- [x] 6.3 修改 `helm/agentteams/templates/matrix/synapse-statefulset.yaml`：AS 启用时追加 volume（secret）+ volumeMount（`/as-registrations` readOnly）
- [x] 6.4 修改 `helm/agentteams/templates/secrets/runtime-env.yaml`：注入 `AGENTTEAMS_MATRIX_PROVIDER` + AS 启用时注入 `_APPSERVICE_*` env
- [x] 6.5 修改 `helm/agentteams/templates/_helpers.tpl`：新增 `agentteams.appservice.pushURL` helper
- [x] 6.6 修改 `helm/agentteams/values.yaml`：新增 `matrix.provider`（默认改为 synapse）+ `matrix.appservice.*`（enabled 默认 true / id / senderLocalpart / asToken / hsToken / userNamespaceRegex / pushURL）
- [x] 6.7 修改 `helm/agentteams/templates/00-validate.yaml`：AS 启用时 required 校验 `asToken` / `hsToken`；`matrix.mode != managed` 且 `userNamespaceRegex` 为空时 fail
- [x] 6.8 `helm template` 验证 4 组合：(a) `synapse + appservice.enabled=true`；(b) `tuwunel`；(c) `synapse + appservice.enabled=false`；(d) `asToken=""` fail

## 7. 集成验证

- [ ] 7.1 Tuwunel dev 实例：跑全量 e2e，确认零回归（业务编排代码一行不动）
- [ ] 7.2 Synapse 1.127 dev 实例 + `provider=synapse + appservice.enabled=true`：controller 启动 smoke test 通过
- [ ] 7.3 Synapse：验证 4 房型创建（worker / team / leader DM / manager DM）
- [ ] 7.4 Synapse：验证 `ReconcileRoomMembership` 不再因 kick 幂等误匹配吞错
- [ ] 7.5 Synapse：验证 AddMember/RemoveMember 的 make_room_admin fallback 在 admin 已离开 team 房间后能恢复
- [ ] 7.6 Synapse：验证 `RotateToken` 返回 501
- [ ] 7.7 Synapse：验证声明式 AS Secret + homeserver.yaml + StatefulSet 挂载协同工作
- [ ] 7.8 切换 provider（`helm upgrade` tuwunel↔synapse）验证业务层零感知
