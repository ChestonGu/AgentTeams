# MatrixOps 业务能力清单与双实现对照（现行参考）

> **状态**：现行参考（与 `agentteams-controller/internal/matrix` 当前代码同步）
> **作者**：Sisyphus + 用户协作
> **更新**：2026-08-07
> **目的**：按业务能力列出调协层（controller reconcilers → service 门面）当前实际使用到的 Matrix 业务能力，并给出每个能力在 **Tuwunel** 与 **Synapse** 两种 homeserver 下的底层实现对照（端点级）。本文档是**代码事实的快照**，改动 `internal/matrix` 接口或实现时请同步更新。
> **权威契约**：方法的业务语义以 [`openspec/changes/synapse-support/specs/matrix-ops/spec.md`](../openspec/changes/synapse-support/specs/matrix-ops/spec.md) 为准；面向运维的说明见 [`docs/synapse.md`](../docs/synapse.md)；Synapse 1.127 逐方法行为核对的原始证据见 [`design/synapse-interface-contracts.md`](synapse-interface-contracts.md)。
> **取代**：本文档取代 [`design/matrix-call-sites-inventory.md`](matrix-call-sites-inventory.md)（基于旧 `matrix.Client` 协议层的调用点反查，为抽象层设计的历史依据；本文档记录抽象层落地后的**当前**调用点与双实现）。

---

## 1. 调用链总览

调协层（controller）不直接接触任何 homeserver 协议细节，全部通过三层门面：

```
controller reconcilers                     internal/controller/*.go
   │  （只依赖 service 门面方法，不出现 MatrixOps/Client 类型）
   ▼
service 门面 Provisioner                   internal/service/provisioner.go
   │  p.matrixOps (matrix.MatrixOps, :166)
   ▼
MatrixOps 接口 (33 方法)                    internal/matrix/ops.go:33
   │
   ├─ NewOps("" | "tuwunel")  → TuwunelMatrixOps   internal/matrix/tuwunel_ops.go
   │       内嵌 *TuwunelClient（admin bot "!admin ..." + CS API）
   └─ NewOps("synapse")       → SynapseMatrixOps   internal/matrix/synapse_ops.go
           内嵌 *matrixClient（CS API）+ *SynapseClient（Synapse admin REST）
```

- **Provider 选择**：`NewOps`（`provider.go:22`）按 `AGENTTEAMS_MATRIX_PROVIDER` 选择实现，大小写不敏感；空值或 `tuwunel` → `TuwunelMatrixOps`，`synapse` → `SynapseMatrixOps`，其它值启动即报错。同一 `Config` 校验在 config 包加载时已执行。
- **MatrixOps 接口**：33 个方法，按业务能力分为 6 组（见 §2）。`ops.go:232-233` 有编译期断言，两个实现漂移会直接构建失败。
- **共享核心**：`reconcileMembersImpl`、`joinRoomForMember`、`leaveRoomForMember`、`forceLeaveAllRoomsForMember`、`listRoomMembersForMember`、`isUserInRoomForMember`、`isManagerJoinedDMForMember`、`roomSpecToRequest` 等被两个实现复用——两侧 CS API 表面一致，共享逻辑天然 provider 无关。
- **Synapse 永不触碰 Tuwunel 概念**：`SynapseMatrixOps` 不调用 `AdminCommand`/`SendMessageAsAdmin`/`SetPasswordAsAdmin`/registration_token 流程/运行时 AS 注册注销。`SynapseClient.AdminCommand` 特意定义为**返回错误的回归护栏**（`synapse_client.go:46`），防止 `!admin` 消息被意外送进 Synapse 房间。

## 2. 接口能力分组（33 方法 / 6 组）

| 能力组 | 方法数 | 方法 |
|---|---|---|
| 房间生命周期 | 5 | `CreateRoom` `DissolveRoom` `ReleaseRoomAlias` `ResolveRoomAlias` `ArchiveRoom` |
| 房间成员管理 | 7 | `AddMember` `InviteMember` `RemoveMember` `ReconcileMembers` `JoinRoom` `LeaveRoom` `ForceLeaveAllRooms` |
| 房间元数据与消息 | 3 | `SetRoomMetadata` `RenameRoom` `SendSystemMessage` |
| 查询与运维 | 5 | `ListRoomMembers` `ListJoinedRooms` `IsUserInRoom` `IsManagerJoinedDM` `HealthCheck` |
| 用户身份与凭据 | 10 | `ProvisionUser` `ProvisionUserViaAppService` `LoginUser` `LoginUserViaAppService` `ResetUserPassword` `DeactivateUser` `SetUserDisplayName` `VerifyUserAccessToken` `UserIDFor` `BackfillLegacyPassword` |
| AppService 治理 | 3 | `RegisterAppService` `UnregisterAppService` `SmokeTestAppService` |

（合计 5+7+3+5+10+3 = 33。`docs/synapse.md` 的 "33 methods" 与此一致。）

## 3. 业务层实际调用点（28 / 33）

以下为 **service / initializer / server / controller** 四层对 `MatrixOps` 的当前调用点（grep `\.<Method>(` 大小写敏感，排除 matrix 包自身与测试）。**未出现的方法**：`ResolveRoomAlias`、`ListJoinedRooms`、`IsUserInRoom`、`VerifyUserAccessToken`、`UnregisterAppService` —— 属接口保留但业务层暂未直接使用（`UnregisterAppService` 由 `appservice.go` 内部 unregister-before-register 逻辑经客户端调用，非业务层直调）。

| 方法 | 调用点（文件:行） |
|---|---|
| `UserIDFor` | `provisioner.go:223, 276, 324-326, 397, 419, 766-770, 791, 988, 1007, 1021, 1285-1286, 1666`（17 处，纯格式化） |
| `CreateRoom` | `provisioner.go:430, 441, 810, 883, 1358`（worker / team / leader DM / manager DM 四类房型） |
| `JoinRoom` | `provisioner.go:490, 844, 919, 947, 960`、`provisioner_human.go:177` |
| `ReleaseRoomAlias` | `provisioner.go:438, 1207, 1212, 1244, 1255` |
| `SetRoomMetadata` | `provisioner.go:469, 866, 938, 1373` |
| `ProvisionUser` | `provisioner.go:357, 1320`、`provisioner_human.go:53`、`initializer.go:192` |
| `ProvisionUserViaAppService` | `provisioner.go:351, 1314`、`provisioner_human.go:36` |
| `LoginUser` | `provisioner.go:596, 624`、`provisioner_human.go:85` |
| `LoginUserViaAppService` | `provisioner.go:594, 622`、`provisioner_human.go:78` |
| `AddMember` | `provisioner.go:1084`、`provisioner_human.go:168` |
| `RemoveMember` | `provisioner.go:1090`、`provisioner_human.go:182, 193` |
| `ListRoomMembers` | `provisioner.go:1145, 1153` |
| `ArchiveRoom` | `provisioner.go:1225, 1231` |
| `IsManagerJoinedDM` | `provisioner.go:1542`（`matrixOps` 直调）；`manager_reconcile_welcome.go:132`（controller 经 `Provisioner` 门面方法转发到 :1542） |
| `InviteMember` | `provisioner.go:957` |
| `ReconcileMembers` | `provisioner.go:1124` |
| `SendSystemMessage` | `provisioner.go:1563` |
| `BackfillLegacyPassword` | `provisioner.go:1667` |
| `DissolveRoom` | `provisioner.go:285` |
| `ForceLeaveAllRooms` | `provisioner.go:275` |
| `LeaveRoom` | `provisioner.go:854` |
| `RenameRoom` | `provisioner.go:833` |
| `SetUserDisplayName` | `provisioner_human.go:162` |
| `ResetUserPassword` | `provisioner_human.go:71` |
| `DeactivateUser` | `provisioner_human.go:202` |
| `HealthCheck` | `initializer.go:187` |
| `RegisterAppService` | `initializer.go:213`、`appservice_mgmt_handler.go:79` |
| `SmokeTestAppService` | `initializer.go:216`、`appservice_mgmt_handler.go:85` |

业务语义对应的房型/用户流：worker 生命周期（`CreateRoom`/`SetRoomMetadata`/`JoinRoom`/`AddMember`/`ReconcileMembers`/`DissolveRoom`）、team 房间（`CreateRoom` + `RenameRoom` + `InviteMember` + actor-token 成员收敛）、leader/manager DM（`ArchiveRoom`/`IsManagerJoinedDM`）、Human 用户（`ProvisionUser*`/`ResetUserPassword`/`DeactivateUser`/`SetUserDisplayName`）、启动引导（`HealthCheck`/`RegisterAppService`/`SmokeTestAppService`）、存量密码迁移（`BackfillLegacyPassword`）。

## 4. 逐方法双实现对照（端点级）

> 约定：CS API = `/_matrix/client/v3/*`（两 provider 一致，复用共享 client）；"admin bot" = Tuwunel `!admin ...` 聊天命令；"admin REST" = Synapse `/_synapse/admin/*`。

| # | 方法 | Tuwunel 实现 | Synapse 实现 |
|---|---|---|---|
| 1 | `CreateRoom` | CS `POST /createRoom`（`roomSpecToRequest`，creator=admin token；alias 幂等：已存在则返回既有房） | 同 CS 端点 + **creator PL=100 注入护栏**（`power_level_content_override.users` 缺 creator 时 Synapse 报 400 `M_BAD_JSON`，`room.py:878-888`） |
| 2 | `DissolveRoom` | admin bot `!admin rooms delete-room <roomID>`（fire-and-forget） | admin REST `DELETE /_synapse/admin/v2/rooms/{roomID}`（异步 purge，返回 delete_id） |
| 3 | `AddMember` | CS `POST /rooms/{id}/invite`（admin token，幂等） | 同 CS invite；失败按 §5 分类恢复（force-join / make_room_admin + 重试） |
| 4 | `InviteMember` | `ActorToken` 非空 → CS invite as actor；否则走 `AddMember` | 同左 |
| 5 | `RemoveMember` | CS `POST /rooms/{id}/kick`（admin）；失败且 `shouldForceLeaveAfterKickError` → admin bot `!admin users force-leave-room <userID> <roomID>` | CS kick（admin）；失败按 §5 恢复；**Synapse 1.127 无 admin kick 端点**（`/rooms/{id}/kick` 404），kick 恢复后仍走 CS |
| 6 | `ReconcileMembers` | 共享 `reconcileMembersImpl`；admin 路径用本实现 `RemoveMember` 升级，actor 路径用 CS WithToken | 同左（共享同一核心） |
| 7 | `JoinRoom` | CS `POST /join/{roomIdOrAlias}`（member/ActorToken，空→admin） | 同左（共享 `joinRoomForMember`） |
| 8 | `LeaveRoom` | CS `POST /rooms/{id}/leave` | 同左（共享） |
| 9 | `ForceLeaveAllRooms` | `ListJoinedRooms` + 逐个 leave（best-effort，失败仅记日志） | 同左（共享） |
| 10 | `ReleaseRoomAlias` | CS `DELETE /directory/room/{alias}`（幂等：不存在→nil） | 同左 |
| 11 | `ResolveRoomAlias` | CS `GET /directory/room/{alias}` | 同左（业务层当前未调用） |
| 12 | `ArchiveRoom` | CS `PUT /rooms/{id}/state/m.room.name`（member/ActorToken，空→admin） | 同左 |
| 13 | `SetRoomMetadata` | CS `PUT /rooms/{id}/state/room.meta`（actor/admin token；admin 在房即有权，**无回退**） | 同 CS 写 room.meta；失败按 §5 恢复（actor 不在房/PL < state_default=50） |
| 14 | `RenameRoom` | CS `PUT .../m.room.name`（actor/admin） | 同左 + §5 恢复 |
| 15 | `SendSystemMessage` | `SendMessageAsAdmin`（admin token + CS `PUT /rooms/{id}/send/m.room.message`） | 私有 `sendMessageAsAdmin`（`ensureAdminToken` + CS SendMessage，**不复用 Tuwunel 的 SendMessageAsAdmin**）+ §5 恢复 |
| 16 | `ListRoomMembers` | CS `GET /rooms/{id}/members`（actor/admin token；admin 绕过在房检查） | 同左（共享 `listRoomMembersForMember`） |
| 17 | `ListJoinedRooms` | CS `GET /joined_rooms` | 同左（业务层当前未调用） |
| 18 | `IsUserInRoom` | CS membership 读取（admin） | 同左（业务层当前未调用） |
| 19 | `IsManagerJoinedDM` | CS membership 读取（admin；可在每次 reconcile 轮询） | 同左（共享） |
| 20 | `HealthCheck` | CS `POST /login`（故意无效凭据）：HTTP 级响应（401/403）视为在线，传输层错误才返回 | 同左 |
| 21 | `ProvisionUser` | `EnsureUser`：CS `POST /register` + `m.login.registration_token`（单步注册）+ login 兜底 + 孤儿恢复 | `EnsureUser`：admin REST `PUT /_synapse/admin/v2/users/{id}`（带 password 创建/更新）+ CS login。**`Created` 恒为 true**（admin API 无注册/登录之分） |
| 22 | `ProvisionUserViaAppService` | CS `POST /register` + `m.login.application_service`（as_token 认证） | 同左（需声明式注册已生效） |
| 23 | `LoginUser` | CS `POST /login`（password） | 同左 |
| 24 | `LoginUserViaAppService` | CS `POST /login`（`m.login.application_service`） | 同左 |
| 25 | `ResetUserPassword` | admin bot `!admin users reset-password <userID>`（`SetPasswordAsAdmin`） | admin REST `POST /_synapse/admin/v1/reset_password/{userID}`（body `new_password`） |
| 26 | `DeactivateUser` | admin bot `!admin users deactivate <userID>`（fire-and-forget，不删数据） | admin REST `POST /_synapse/admin/v1/deactivate/{userID}`（`erase=false`，保数据，与 Tuwunel 语义一致） |
| 27 | `SetUserDisplayName` | accessToken 非空 → CS `PUT /profile/{userId}/displayname`；空 → admin token 走同端点 | accessToken 非空 → 同 CS 端点；空 → admin REST `PUT /_synapse/admin/v2/users/{userID}`（仅 `displayname` 字段，不动密码） |
| 28 | `VerifyUserAccessToken` | CS `GET /account/whoami` | 同左（业务层当前未调用） |
| 29 | `UserIDFor` | 纯格式化 `@<localpart>:<domain>` | 同左 |
| 30 | `BackfillLegacyPassword` | `SetPasswordAsAdmin`（同 #25，bulk 迁移语义） | `POST /_synapse/admin/v1/reset_password/{userID}`（同 #25） |
| 31 | `RegisterAppService` | admin bot `!admin appservices register`（smoke-test 幂等 + unregister-before-register 兜底） | **仅 smoke test**：声明式（Helm `app_service_config_files`），未生效时报错并指向 Helm 配置 |
| 32 | `UnregisterAppService` | admin bot `!admin appservices unregister <id>` | 返回静态错误指向 Helm（无运行时注销端点） |
| 33 | `SmokeTestAppService` | AS login 为 `sender_localpart` 用户 | 同左 |

## 5. Synapse sender 恢复机制（§4 恢复路径详解）

`SynapseMatrixOps` 对 in-room CS 操作（invite/kick/setState/send）失败的恢复由 `recoverSynapseSenderOp`（`synapse_ops.go:209`）统一驱动：

1. **分类** `classifySynapseSenderError`（`:169`）：仅 `M_FORBIDDEN` 进入，按错误串分为——
   - `synapseRecoveryJoin`：含 `"not in room"` → sender 不在房（`event_auth.py:687/:731`）
   - `synapseRecoveryPower`：含 `"permission to invite"` / `"permission to post"` / `"cannot kick user"` / `"cannot unban user"` → PL 不足（`event_auth.py:703/:717/:768`）
   - 其它 → 原样返回（已加入/已邀请等幂等情形在 CS client 内已返回 nil，不会到此处）
2. **Join 恢复**：`POST /_synapse/admin/v1/join/{roomID}`（原生 membership 恢复：自动 invite + join）→ 重试原 CS 操作；重试仍报 PL 不足则落入 Power 恢复。
3. **Power 恢复**：`POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin`（把 sender 提到房间最高 admin PL）→ 重试原 CS 操作。

适用方法：`AddMember`、`RemoveMember`、`SetRoomMetadata`、`RenameRoom`、`SendSystemMessage`。

**Tuwunel 侧对应**：无 make_room_admin 概念（admin bot 本就跨房特权），kick 失败由 `shouldForceLeaveAfterKickError`（`tuwunel_ops.go:219`）升级到 `!admin users force-leave-room`；该函数同时匹配 Tuwunel（`"not have enough power"`/`"power"`）与 Synapse 1.127（`"cannot kick user"`/`"cannot unban user"`/`"not in room"`）错误串，使升级判定 provider 无关。

## 6. 关键差异速查

| 维度 | Tuwunel | Synapse |
|---|---|---|
| 管理通道 | admin bot（`!admin` 聊天命令，fire-and-forget） | admin REST（`/_synapse/admin/v1|v2/*`，同步 + 2xx 判定） |
| 房间销毁 | `!admin rooms delete-room` | `DELETE /_synapse/admin/v2/rooms/{id}`（异步） |
| 踢人升级 | `!admin users force-leave-room` | **无 admin kick 端点**；make_room_admin + CS kick 重试 |
| 用户预置 | CS register（registration_token，单步） | admin REST `PUT /_synapse/admin/v2/users/{id}`（`Created` 恒 true） |
| 停用 | `!admin users deactivate`（不删数据） | `POST /_synapse/admin/v1/deactivate/{id}`（`erase=false`） |
| 显示名（无用户 token） | admin token 走 CS profile | admin REST users 端点（仅 displayname） |
| AppService 治理 | 运行时注册/注销（admin bot） | 声明式（Helm），仅 smoke test；RotateToken 端点返回 501 |
| AS 注册/登录流 | `m.login.application_service`（as_token） | 同左（两 provider 一致） |

## 7. 相关文档

- [`docs/synapse.md`](../docs/synapse.md) — 面向运维：provider 切换、声明式 AppService、namespace 安全、token 轮换、恢复行为。
- [`openspec/changes/synapse-support/specs/matrix-ops/spec.md`](../openspec/changes/synapse-support/specs/matrix-ops/spec.md) — 权威契约：33 方法的 Requirement/Scenario。
- [`design/synapse-interface-contracts.md`](synapse-interface-contracts.md) — Synapse 1.127 逐方法行为核对（原始证据，历史依据）。
- [`design/synapse-support.md`](synapse-support.md) — 抽象层设计权衡过程（历史依据）。
- [`design/matrix-call-sites-inventory.md`](matrix-call-sites-inventory.md) — 旧 `matrix.Client` 调用点反查（历史依据，被本文档取代）。
- `agentteams-controller/internal/matrix/ops_exhaustive_test.go` — 双实现等价性测试（每个方法钉死行为一致性）。
