## Context

Controller 现有架构：业务层（Provisioner / Initializer / HTTP handlers）**直接依赖** `matrix.Client` 协议层接口（`internal/matrix/client.go` 的 `Client` interface，~30 方法）。协议层有两个实现：`TuwunelClient`（默认）和 `SynapseClient`（嵌入 TuwunelClient，覆盖 `AdminCommand` + `EnsureUser`）。

问题不在协议层本身——它正确抽象了 Matrix CS API。问题在于**业务层需要做协议层不该知道的决策**：

- "这次 invite 用 admin token 还是 team admin token？"（业务上下文）
- "kick 失败后调 `!admin force-leave-room` 还是 `make_room_admin` + 重试？"（provider 特有 fallback）
- "CreateRoom 时 creator 必须在 PowerLevels 里"（Synapse 1.127 强制约束，`room.py:878-888`）
- "AS 注册用 admin bot 还是声明式 YAML？"（架构性差异）

这些决策散落在 Provisioner 的 50+ 调用点里，导致 Tuwunel→Synapse 切换需要改业务代码。详细反查见 `design/matrix-call-sites-inventory.md`（34 调用点，6 业务域），Synapse 1.127 接口契约见 `design/synapse-interface-contracts.md`。

## Goals / Non-Goals

**Goals:**

- 引入 `matrix.MatrixOps` 业务抽象，按 6 大业务能力组织（不是协议动词）
- 业务层（Provisioner / Initializer / HTTP handlers）只依赖 `MatrixOps`，不再直接调 `matrix.Client` 或写 `!admin` 字符串
- Tuwunel 和 Synapse 各一个实现，封装所有 provider 特有逻辑
- Provider 通过 `AGENTTEAMS_MATRIX_PROVIDER` env 切换，业务编排代码一行不动
- TuwunelMatrixOps **行为字节等价**（现有测试零修改通过）

**Non-Goals:**

- 不删除 `matrix.Client` 协议层——它作为 MatrixOps 实现的 HTTP 客户端保留
- 不改 Provisioner 向 controller 暴露的接口（`WorkerProvisioner` / `ManagerProvisioner` / `HumanProvisioner`）
- 不引入 6 个小接口（`UserOps` / `RoomOps` / ...）——按用户决定用一个大的 `MatrixOps`
- 不把 `internal/matrix` 包拆出去——MatrixOps 与现有 matrix.Client 同包
- 不做 Tuwunel→Synapse 历史数据自动迁移
- 不为 `DELETE /v2/rooms` 添加状态轮询（保持 fire-and-forget）

## Decisions

### Decision 1: 一个大接口 vs 多个小接口 → 选一个大接口

**选择**：单一 `MatrixOps` 接口，~30 方法，按 6 业务能力分组用注释分隔。

**Rationale**：
- Provisioner / Initializer 只需持有一个依赖
- Go 风格倾向大接口（`io.ReadWriteCloser`、`http.Handler`）
- 方法虽多但调用模式一致（ctx + spec → ref/error），认知负担可控
- 6 个小接口会让 Provisioner 持有 6 个依赖，构造和 mock 都更繁琐

**Alternatives**：
- 6 个小接口（已否决）：构造器爆炸，mock 实现要 6 套
- 嵌入式接口（`MatrixOps` 嵌入 `UserOps` + `RoomOps` + ...）：表面优雅但本质还是大接口，徒增类型

### Decision 2: MatrixOps 放 `internal/matrix` 包内 vs 独立 `internal/matrixops` 包 → 选 `internal/matrix`

**选择**：接口和实现都放 `internal/matrix` 包，新增文件 `ops.go` / `tuwunel_ops.go` / `synapse_ops.go` / `synapse_admin.go`。

**Rationale**：
- MatrixOps 实现需要访问 `matrix.Config` / `matrix.StateEvent` / `matrix.AppServiceRegistration` 等类型——同包免 import
- `matrix.Client` 协议层作为实现内部 HTTP 客户端，同包访问其未导出字段（如 `doJSON`）更自然
- 现有 `matrix.Client` 接口可以**逐步私有化**（改名 `httpClient` 或保持）而不跨包

**Alternatives**：
- `internal/matrixops` 独立包（已否决）：要 import `internal/matrix` 的所有类型，且无法访问协议层未导出方法
- 把 `matrix.Client` 也移到新包（已否决）：破坏性太大，无收益

### Decision 3: 业务类型设计——Spec/Ref 模式

**选择**：方法签名用业务类型 `RoomSpec` / `RoomRef` / `UserSpec` / `MemberSpec` / `RoomMetadata`，**不复用** `matrix.CreateRoomRequest` / `matrix.UserCredentials` 等协议层类型。

**Rationale**：
- 协议层类型泄漏实现细节（`CreateRoomRequest.CreatorToken`、`UserCredentials.Password`）
- 业务类型只表达业务意图
- 转换在实现内部做（`tuwunel_ops.go` 把 `RoomSpec` 转 `matrix.CreateRoomRequest`）

**类型对照**：

| 业务类型（新） | 替代的协议类型 | 关键差异 |
|---|---|---|
| `RoomSpec` | `matrix.CreateRoomRequest` | `CreatorToken` 由 `ActorUserID`+`ActorToken` 表达（见下）；有 `Metadata` 业务字段 |
| `RoomRef` | `matrix.RoomInfo` | 同形（RoomID + Created bool） |
| `UserSpec` | `matrix.EnsureUserRequest` | 只有 Username + Password |
| `UserRef` | `matrix.UserCredentials` | 无 Password（敏感信息不进业务层）|
| `MemberSpec` | （无对应） | UserID + 可选 Actor hint |
| `RoomMetadata` | `map[string]interface{}` | 强类型字段（Kind / TeamName / Members ...）|

**Actor hint（Phase 1 实现时确定，修订 Decision 3 原文 "RoomSpec 没有 CreatorToken"）**：
`RoomSpec` 增加 `ActorUserID string` + `ActorToken string`（均可为空）。业务层用它们表达"以谁的身份创建房间"：
- 团队房间 / Leader DM：`ActorUserID = team admin`、`ActorToken = req.TeamAdminActorToken`——与改造前 `CreateRoomRequest.CreatorToken = req.TeamAdminActorToken` 字节等价（团队 admin 故意不列入 Invite，靠 creator 身份自动入房；测试断言 `createRooms[i].CreatorToken == "team-admin-token"`）。
- Worker / Manager DM 房间：两者为空 → 实现用全局 admin。
- `ActorUserID` 供 Synapse 的 PL 注入定位 creator（修复 4），`ActorToken` 供 Tuwunel 认证 createRoom 调用——ops 层无状态，必须由业务层把 token 传进来，无法从 CredentialStore 反查（见 Open Question 2 结论）。

**注意**：`UserRef` 不含 Password——但 Provisioner 当前需要 Password 写入 creds。**折中**：`ProvisionUser` 返回 `UserRef` + 凭证通过 out-parameter 或单独方法（`UserCredentials()`）暴露。**待 tasks 阶段 finalize**。

### Decision 4: Synapse 的 make_room_admin fallback 策略

**选择**：`SynapseMatrixOps` 的 `AddMember` / `RemoveMember` / `SetRoomMetadata` / `RenameRoom` / `SendSystemMessage` 在收到 sender-not-in-room 或 PL-insufficient 错误时，调 `POST /_synapse/admin/v1/rooms/{id}/make_room_admin {"user_id":"<admin>"}` 让 admin 接管房间（Synapse admin API 会：grant PL 100 + invite admin + admin 自动 join），然后重试 CS API 操作。

**Rationale**（基于源码核对）：
- Synapse 1.127 admin API 没有 kick 端点（`synapse/rest/admin/` 无 servlet）
- `make_room_admin`（`rooms.py:568-`）是 Synapse 官方提供的"admin 接管房间"机制，副作用是改 power_levels——但在 controller-managed 房间里 admin 本应 PL=100，所以夺权不会引入非预期变化
- 夺权后 admin 永久在房——后续操作无需再 fallback

**Alternatives**：
- OperatorResolver（已废弃）：解决的是 admin 偶尔不在房的问题，但 admin 默认在房（CreateRoom 时 invite），实际触发少
- 用 `POST /v1/join/{room}` admin 强制 join：这个端点 join 的是任意用户，不是 admin 自己，且需要 admin 已被 invite——循环依赖
- 不做 fallback，让业务层处理：违反抽象层目标

### Decision 5: ForceLeaveRoom 不进 MatrixOps 接口

**选择**：MatrixOps 不暴露 `ForceLeaveRoom`——它作为 `RemoveMember` 实现内部的 fallback 存在。业务层（Provisioner）通过 `RemoveMember` 触发，不直接调强踢。

**Rationale**：
- 现有 `Provisioner.ForceLeaveRoom` 接口向 controller 暴露（`interfaces.go:62`）——保留这个接口方法，但内部实现改为调 `matrixOps.RemoveMember`（Synapse 下走 make_room_admin；Tuwunel 下走 admin bot force-leave-room）
- 业务语义上 ForceLeaveRoom = "确保用户不在房"，与 RemoveMember 相同
- 减少接口方法数

**注意**：`Provisioner.ForceLeaveRoom` 向 controller 的接口签名**保留不变**（`ForceLeaveRoom(ctx, userID, roomID) error`），controller 调用零改动。内部转发到 `matrixOps.RemoveMember`。

### Decision 6: Provider 选择 + 注入

**选择**：
- 启动时读 `AGENTTEAMS_MATRIX_PROVIDER` env
- 构造对应 MatrixOps 实例（单例）
- 通过 `ProvisionerConfig.MatrixOps` / `Initializer.MatrixOps` / `NewAppServiceHandler(matrixCfg, matrixOps)` 注入
- HTTP handler 不再自己构造 client

**Rationale**：简单、显式、可测。`appservice_mgmt_handler.go` 的硬编码 `NewTuwunelClient` 自然消除。

### Decision 7: 渐进迁移（方案 B），5 Phase

**选择**：按 §4 Phase 1→5 逐步迁移，每 Phase 独立可验证、可回滚。

**Rationale**：
- 大重构（方案 A）PR 太大，review 困难，风险高
- Phase 1（4 核心操作）能快速验证抽象边界是否正确
- 每个 Phase 的 TuwunelMatrixOps 实现都是"现有逻辑搬迁"，零行为变化

**Phase 划分**（与 tasks.md 对应）：
- Phase 1：CreateRoom / DissolveRoom / AddMember / RemoveMember
- Phase 2：ReconcileMembers / JoinRoom / LeaveRoom / ForceLeaveAllRooms / ReleaseRoomAlias / ArchiveRoom
- Phase 3：SetRoomMetadata / RenameRoom / SendSystemMessage / ListRoomMembers / ListJoinedRooms
- Phase 4：ProvisionUser / Login* / ResetUserPassword / DeactivateUser / SetUserDisplayName / RegisterAppService / UnregisterAppService / SmokeTestAppService + 修复 appservice_mgmt_handler 硬编码
- Phase 5：清理 matrix.Client 引用 + 文档

## Risks / Trade-offs

**[Risk] 大接口方法多，迁移期可能漏迁移某些调用点** → 迁移期 `matrix.Client` 和 `MatrixOps` 并存
- **Mitigation**：Phase 5 用 grep 确认 `internal/service/`、`internal/initializer/`、`internal/server/` 里不再有 `matrix.Client` / `matrix.TuwunelClient` / `!admin` 引用；CI 加 lint 规则

**[Risk] make_room_admin 改 power_levels 引入副作用** → 在 controller-managed 房间里 admin 本应 PL=100
- **Mitigation**：fallback 仅在 admin 不在房/PL 不足时触发；正常路径（admin 默认在房）不触发；fallback 后 admin 永久在房，后续操作稳定

**[Risk] 业务类型转换层增加代码量** → RoomSpec ↔ CreateRoomRequest 等
- **Acceptable because**：转换集中在 `*_ops.go` 实现里，业务层变薄；类型安全提升

**[Risk] TuwunelMatrixOps "行为字节等价"难验证** → 现有测试可能依赖实现细节
- **Mitigation**：每 Phase 迁移后跑全量现有测试；Phase 1 特别关注 `provisioner_team_test.go` 的 admin-bot 命令字符串断言

**[Trade-off] UserRef 不含 Password，但 Provisioner 需要写 creds** → 凭证获取方式变化
- **Mitigation**：Decision 3 的折中——ProvisionUser 返回 UserRef，凭证通过单独方法或在实现内部直接更新 creds store；tasks 阶段 finalize

**[Trade-off] Synapse 路径比 Tuwunel 复杂（make_room_admin fallback）** → SynapseMatrixOps 代码量更多
- **Acceptable because**：复杂度封装在一个文件里，业务层不感知

## Migration Plan

### 从未启用 Synapse（全新部署）

1. `helm install` with `matrix.provider=synapse` + `matrix.appservice.enabled=true` + token
2. Controller 启动 → 构造 SynapseMatrixOps → 注入 → smoke test 通过
3. 业务编排代码（ProvisionWorker 等）零感知，正常工作

### 从 Tuwunel 切换到 Synapse（同一集群）

1. 部署 Synapse 实例（独立或替换 Tuwunel）
2. `helm upgrade` 设置 `matrix.provider=synapse` + AS 配置
3. Controller 重启 → 切到 SynapseMatrixOps
4. **注意**：Tuwunel 上的 Matrix 账号和房间不自动迁移——Worker/Manager CR 会重新 provision

### Rollback（Synapse → Tuwunel）

1. `helm upgrade` 设置 `matrix.provider=tuwunel`
2. Synapse 数据保留
3. Controller 重启 → 切回 TuwunelMatrixOps

### 渐进迁移期（Phase 1-5）

每个 Phase：
- 新增 MatrixOps 方法 + TuwunelMatrixOps 实现（搬迁现有逻辑）+ SynapseMatrixOps 实现
- Provisioner / Initializer / handler 对应调用点切到 MatrixOps
- 跑全量现有测试（Tuwunel 零回归）
- 新增 Synapse 风格测试用例
- 中间状态：`matrix.Client` 和 `MatrixOps` 并存，未迁移方法继续走 `matrix.Client`

## Open Questions

1. **`UserRef` 与凭证**：Provisioner 需要把 MatrixPassword / AccessToken 写入 `WorkerCredentials` 持久化。`ProvisionUser` 返回 `UserRef`（不含敏感字段）后，凭证如何回传？两个候选：
   - (a) `ProvisionUser` 同时返回 `UserRef` 和 `UserCredentials`（含 token/password）——后者标记为"实现细节，不进业务逻辑"
   - (b) `MatrixOps` 持有 `CredentialStore` 引用，`ProvisionUser` 内部直接写 store
   - **倾向 (a)**——保持 MatrixOps 无状态，业务层决定如何处理凭证。tasks 阶段 finalize。

2. **`MemberSpec` 的 Actor hint**：`ReconcileMembers` 现在支持 `actorToken`（team admin token）。业务层如何通过 `MemberSpec` 表达"用 team admin 身份操作"？
   - **已定（Phase 1）**：`MemberSpec` 增加 `ActorUserID string` 字段（业务层指定"以这个用户身份"）+ `ActorToken string`（该用户的 access token，空 = admin）。`RoomSpec` 采用同一模式（见 Decision 3 Actor hint 修订）。实现层**不**从 CredentialStore 反查 token——ops 层无状态，token 由业务层传入；反查留待 Phase 2 `ReconcileMembers` 迁移时若需要再做。

这两个问题不影响 spec 和整体设计，可在实现 Phase 1 时确定。
