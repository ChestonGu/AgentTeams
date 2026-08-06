## Why

AgentTeams 控制器目前**直接依赖 Matrix 协议层**（`matrix.Client` 接口）来编排业务逻辑：`Provisioner`、`Initializer`、`appservice_mgmt_handler` 等业务组件都直接持有 `matrix.Client` 并直接调用 CS API 方法（`CreateRoom` / `InviteToRoom` / `KickFromRoom` / ...）和 admin bot 命令字符串（`!admin users force-leave-room` 等）。

这个架构在只有 Tuwunel 一种 homeserver 时工作良好，但现在**阻止了 Synapse 平滑接入**：

1. **协议层细节泄漏到业务层** — Provisioner 必须知道"用 admin token 还是 actor token"、"kick 失败要 fallback 到 `!admin force-leave-room`"、"admin 必须 in-room"等 Matrix 协议细节。这些是底层实现策略，不是业务意图。
2. **Tuwunel 特有概念（admin bot 命令）散落在业务代码里** — 6 处 `!admin ...` 命令字符串硬编码在 `provisioner.go` / `provisioner_human.go`，Synapse 完全无对应。
3. **业务代码硬编码底层 client 构造** — `appservice_mgmt_handler.go:54` 直接 `matrix.NewTuwunelClient(...)`，Synapse 下完全失效。
4. **Synapse 1.127 的真实约束（已逐条源码核对，见 `design/synapse-interface-contracts.md`）无法在现有架构里封装**：
   - CS API invite/kick/state/send 都要求操作者 in-room + 充分 PL，失败时需要 `make_room_admin` admin API 夺权 + 自邀请 + 重试（Synapse 特有 fallback）
   - admin API 没有 kick 端点（源码核对：`synapse/rest/admin/` 无此 servlet）
   - AppService 只能声明式加载（无运行时注册 API）

**目标**：引入按业务能力组织的 `MatrixOps` 抽象层（包 `internal/matrix`），把"如何完成业务操作"下沉到 `TuwunelMatrixOps` / `SynapseMatrixOps` 实现里，让业务层（Provisioner / Initializer / HTTP handlers）零感知底层 homeserver。这样 Tuwunel↔Synapse 切换只改一个 env 变量，业务编排逻辑一行不动。

## What Changes

### 新增 `matrix.MatrixOps` 业务抽象接口（核心）

新增**一个**大接口 `matrix.MatrixOps`，按 6 大业务能力组织（不是按 Matrix 协议动词），覆盖现有 `matrix.Client` 的全部 30+ 方法的业务用途：

- **用户身份与凭证**（~10 方法）：`ProvisionUser` / `ProvisionUserViaAppService` / `LoginUser` / `LoginUserViaAppService` / `ResetUserPassword` / `DeactivateUser` / `SetUserDisplayName` / `VerifyUserAccessToken` / `UserIDFor` / `BackfillLegacyPassword`
- **房间生命周期**（~5 方法）：`CreateRoom` / `DissolveRoom` / `ReleaseRoomAlias` / `ResolveRoomAlias` / `ArchiveRoom`
- **房间成员管理**（~8 方法）：`AddMember` / `RemoveMember` / `ReconcileMembers` / `JoinRoom` / `LeaveRoom` / `ForceLeaveAllRooms` / `ListRoomMembers` / `IsUserInRoom`
- **房间元数据与消息**（~3 方法）：`SetRoomMetadata` / `RenameRoom` / `SendSystemMessage`
- **AppService 治理**（~3 方法）：`RegisterAppService` / `UnregisterAppService` / `SmokeTestAppService`
- **查询与运维**（~3 方法）：`ListJoinedRooms` / `IsManagerJoinedDM` / `HealthCheck`

详见 spec 的 `matrix-ops` capability。

### 两个实现

- **`TuwunelMatrixOps`**：现有逻辑搬迁——直接转发 CS API；kick 失败 fallback 到 `!admin users force-leave-room`；AS 走 `!admin appservices register`；DissolveRoom 走 `!admin rooms delete-room`。**零行为变化**。
- **`SynapseMatrixOps`**：基于源码核对实现 Synapse 1.127 真实约束——
  - `AddMember`/`RemoveMember`/`SetRoomMetadata`/`RenameRoom`/`SendSystemMessage` 失败时，若错误是 sender 不在房/PL 不足（`event_auth.py:687,717`），调 `POST /_synapse/admin/v1/rooms/{id}/make_room_admin` 让 admin 接管房间（夺权 + 自动 invite + join），再重试 CS API
  - `RemoveMember` 的幂等匹配精确到 Synapse 实际字符串（`"The target user is not in the room"` vs `"@x not in room"` vs `"You cannot kick user"`）
  - `DissolveRoom` 走 `DELETE /_synapse/admin/v2/rooms/{id}`（异步 fire-and-forget）
  - `RegisterAppService` 仅做 smoke test（声明式，由 Helm 渲染 YAML 加载）
  - `UnregisterAppService` 返回明确错误（声明式不能运行时注销）
  - `DeactivateUser` 走 `POST /_synapse/admin/v1/deactivate/{userId}`

### Provider 选择

新增 `AGENTTEAMS_MATRIX_PROVIDER` env（`tuwunel` 默认 / `synapse`），启动时构造对应 `MatrixOps` 实现注入到 `Provisioner` / `Initializer` / HTTP handlers。

### 声明式 AppService 支持（Helm）

- 新增 `templates/matrix/synapse-appservice-secret.yaml`：渲染 AS registration YAML 到 Secret
- 修改 Synapse `homeserver.yaml`：AS 启用时追加 `app_service_config_files:`
- 修改 Synapse StatefulSet：挂载 AS Secret 到 `/as-registrations/`
- 修改 controller runtime-env Secret：注入 AS env + provider env
- 修改 `RotateToken` HTTP endpoint：Synapse 下返回 501 + Helm 指引

### 业务层迁移（方案 B 渐进，5 phase）

**Phase 1**：抽 4 个核心操作（CreateRoom / DissolveRoom / AddMember / RemoveMember）到 MatrixOps，两个实现，对应调用点切换。验证抽象边界。

**Phase 2**：扩展到全部成员管理 + 房间生命周期（ReconcileMembers / JoinRoom / LeaveRoom / ForceLeaveAllRooms / ReleaseAlias / ArchiveRoom）。

**Phase 3**：扩展到元数据 + 消息 + 查询（SetRoomMetadata / RenameRoom / SendSystemMessage / ListRoomMembers / ListJoinedRooms）。

**Phase 4**：扩展到用户身份 + AppService 治理（ProvisionUser / Login / ResetPassword / RegisterAppService）。**这一步同时修复** `appservice_mgmt_handler.go` 硬编码 TuwunelClient 的问题。

**Phase 5**：清理——移除业务层对 `matrix.Client` 的所有直接引用。`matrix.Client` 退化为 `internal/matrix` 实现内部使用的 HTTP 客户端层。

### 顺带修复的 bug（在 Synapse 实现里自然修复）

- `KickFromRoomWithToken` 幂等匹配过宽（`"not in"` 误匹配 sender 不在房，`"cannot kick"` 误匹配 PL 不足）
- `shouldForceLeaveAfterKickError` 不识别 Synapse 字符串
- `synKick` 调用不存在的端点（改为 `make_room_admin` + CS kick 重试）
- `CreateRoom` 不保证 creator 在 `power_level_content_override.users` 里（防御性自动注入）
- `LeaveRoom` 不幂等（"user not in room" 应为 nil）

这些 bug 在 `SynapseMatrixOps` 实现里自然修复，**TuwunelMatrixOps 路径零行为变化**。

### Provisioner 向 controller 的接口

**保留**现有 `WorkerProvisioner` / `ManagerProvisioner` / `HumanProvisioner` 接口——controller 不直接调 `MatrixOps`，仍通过 Provisioner 接口。Provisioner 内部从 `p.matrix.*`（matrix.Client）切到 `p.matrixOps.*`（MatrixOps）。

## Capabilities

### New Capabilities

- `matrix-ops`: 业务层对 Matrix 能力的完整抽象——按 6 大业务能力（用户身份/房间生命周期/成员管理/元数据消息/AppService 治理/查询）组织的单一 `MatrixOps` 接口，封装 token 选择、API 路径、provider 特有 fallback 策略，让业务层零感知底层 homeserver
- `matrix-provider-selection`: 按 `AGENTTEAMS_MATRIX_PROVIDER` 选择 MatrixOps 实现（TuwunelMatrixOps / SynapseMatrixOps）的能力
- `synapse-appservice`: Synapse 下通过 Helm 声明式渲染 + 加载 Application Service 注册的能力（替代 Tuwunel 的运行时 admin bot 注册）

## Impact

### 受影响代码

**新增**：
- `agentteams-controller/internal/matrix/ops.go`（新）— `MatrixOps` 接口 + 业务类型定义（RoomSpec / RoomRef / UserSpec / MemberSpec / RoomMetadata / AppServiceRegistration 等）
- `agentteams-controller/internal/matrix/tuwunel_ops.go`（新）— `TuwunelMatrixOps` 实现（现有逻辑搬迁）
- `agentteams-controller/internal/matrix/synapse_ops.go`（新）— `SynapseMatrixOps` 实现（含 make_room_admin fallback）
- `agentteams-controller/internal/matrix/synapse_admin.go`（新）— Synapse admin API 客户端（make_room_admin / delete_room / deactivate / reset_password 等）

**修改**（按 Phase 渐进）：
- `agentteams-controller/internal/service/provisioner.go` — `matrix matrix.Client` 字段改为 `matrixOps matrix.MatrixOps`；50+ 调用点迁移
- `agentteams-controller/internal/service/provisioner_human.go` — 11 处调用点迁移
- `agentteams-controller/internal/initializer/initializer.go` — `Matrix matrix.Client` 改为 `MatrixOps`；4 处调用点迁移
- `agentteams-controller/internal/server/appservice_mgmt_handler.go` — **修复硬编码**：通过 `MatrixOps` 接口调用，不再 `NewTuwunelClient`
- `agentteams-controller/internal/config/config.go` — 新增 `MatrixProvider` env
- `agentteams-controller/internal/app/app.go` — 按 provider 选 MatrixOps 实现
- `agentteams-controller/internal/matrix/types.go` — `Config` 加 `Provider` 字段
- `agentteams-controller/internal/matrix/client.go` — 保留作为 HTTP 客户端层，`matrixOps` 实现内部使用
- `agentteams-controller/internal/matrix/synapse_client.go` — `AdminCommand` 翻译逻辑上移到 `SynapseMatrixOps`，本文件可逐步退化

**保留不变**：
- `agentteams-controller/internal/controller/**` — 通过 Provisioner 接口调用，零改动
- `agentteams-controller/internal/service/interfaces.go` — Provisioner 接口签名不变

### 受影响 Helm chart

- `helm/agentteams/templates/matrix/synapse-configmap.yaml` — 加 `app_service_config_files:`
- `helm/agentteams/templates/matrix/synapse-statefulset.yaml` — 挂载 AS Secret
- `helm/agentteams/templates/matrix/synapse-appservice-secret.yaml`（新）
- `helm/agentteams/templates/secrets/runtime-env.yaml` — 注入 AS env + provider env
- `helm/agentteams/templates/_helpers.tpl` — 新增 `appservice.pushURL` helper
- `helm/agentteams/templates/00-validate.yaml` — AS 启用校验
- `helm/agentteams/values.yaml` — 新增 `matrix.provider` + `matrix.appservice.*`

### 受影响 API

- `POST /api/v1/appservice/rotate-token` — Synapse provider 下返回 501

### 关键约束

- **Tuwunel 路径零行为变化** — 同一 binary + chart，切 env 即切实现
- **Provisioner 向 controller 的接口不变** — controller 包零改动
- **`matrix.Client` 协议层保留** — 作为 MatrixOps 实现的 HTTP 客户端，不删除
- **所有 Synapse 行为有源码依据** — 见 `design/synapse-interface-contracts.md`

### 文档

- `design/matrix-call-sites-inventory.md`（已存在）— 完整调用点反查，本 proposal 的依据
- `design/synapse-interface-contracts.md`（已存在）— Synapse 1.127 接口契约核对
- `design/synapse-support.md`（已存在）— 业务需求与可行性分析
- `docs/synapse.md`（新）— 面向用户的部署/配置指南
