# Synapse 1.127 支持方案（含架构决策记录）

> **状态**：设计讨论中（未定稿）
> **作者**：Sisyphus + 用户协作
> **更新**：2025-08-05
> **目标**：让 AgentTeams 控制器在 Synapse 1.127 下支持完整的房间/用户/AppService 业务，与现有 Tuwunel 实现并存。

> **历史标注（2026-08）**：本文档的设计决策已转化为 openspec proposal
> [`synapse-support`](../openspec/changes/synapse-support/) 并落地实现。本文档作为
> **历史依据**保留，记录架构权衡过程；后续实施以 openspec proposal 与代码为准。
> 面向运维的 Synapse 支持说明见 [`docs/synapse.md`](../docs/synapse.md)。

---

## 0. 文档目的

本文档记录 Synapse 支持的设计决策过程，**特别是经过用户质疑后修正的架构方向**。后续讨论与实施以本文档为依据。

---

## 1. 现状分析（已完成，基于代码反查）

### 1.1 当前 Synapse 支持程度

`SynapseClient` 嵌入 `*TuwunelClient`，覆盖 2 个方法：
- `AdminCommand` — 把 3 种 `!admin ...` 命令翻译成 Synapse REST API
- `EnsureUser` — 用 `PUT /_synapse/admin/v2/users/{id}` 替代 registration_token

其余 17 个 CS API 方法从 `TuwunelClient` 继承，行为一致。

**3 个 admin 命令翻译**（`synapse_client.go:43-50`）：
```
!admin users reset-password    → POST   /_synapse/admin/v1/reset_password/{userID}
!admin users force-leave-room  → POST   /_synapse/admin/v1/rooms/{roomID}/kick
!admin rooms  delete-room      → DELETE /_synapse/admin/v2/rooms/{roomID}
```

### 1.2 Synapse 1.127 的核心约束

1. **房间操作的授权规则**（用户指出的关键点）：CS API 的 invite/kick/setState/sendMessage 要求**操作者 token 持有者必须在房间里**且有足够 PL。Synapse 严格遵守，没有 Tuwunel admin bot 那种"超级管理员"特权。
2. **唯一例外**：`POST /_synapse/admin/v1/rooms/{id}/kick` 不要求操作者在房间内（admin API 特权）。
3. **AppService 只能声明式**：没有运行时注册/注销 API，必须通过 `homeserver.yaml` 的 `app_service_config_files:` 列表加载。
4. **DELETE v2 异步**：`DELETE /_synapse/admin/v2/rooms/{id}` 立即返回 `delete_id`，后台执行。需要轮询 `GET /_synapse/admin/v2/rooms/{id}/delete_status` 才能确认真正完成。

### 1.3 当前代码的授权模型假设（错误前提）

Tuwunel 的 admin bot 是 homeserver 内嵌"超级管理员"，可操作任何房间的任何用户，**无需在该房间内**。当前代码大量依赖这个特权：

- `ReconcileRoomMembership`（`provisioner.go:1097`）在 `actorToken==""` 时用 admin token 调 invite/kick，假设 admin 永远有特权
- `provisioner.go:859` 显式让 admin 离开 team 房间（"global admin leave team room"）—— 之后任何对 team 房间的 admin 操作在 Synapse 下都会失败
- `team_controller_test.go:510` 测试断言"team room should not include global admin power level"

**结论**：现有架构**隐式假设 admin 永远有跨房特权**，这个假设在 Synapse 下不成立。

---

## 2. Provisioner 职责总结（已与用户确认）

### 2.1 Provisioner 是什么

`Provisioner`（`internal/service/provisioner.go:164`）是**面向业务实体（Worker / Manager / Human）的基础设施生命周期编排器**，不是单纯的 Matrix 客户端。

### 2.2 持有的依赖

```go
type Provisioner struct {
    matrix         matrix.Client          // Matrix 用户/房间
    matrixConfig   matrix.Config
    gateway        gateway.Client         // Higress AI 网关
    ossAdmin       oss.StorageAdminClient // MinIO
    creds          CredentialStore        // 凭证持久化
    k8sClient      kubernetes.Interface   // K8s SA
    kubeMode / namespace / authAudience / matrixDomain / adminUser string
    resourcePrefix / controllerName / remoteCache
    managerPassword / managerGatewayKey / managerEnabled / aiGatewayURL / managerModel
}
```

### 2.3 Provisioner 做的两类事

| 类别 | 职责 | 评估 |
|---|---|---|
| ① **跨基础设施编排**（应保留） | ProvisionWorker 串起：凭证 → Matrix 账号 → MinIO 用户 → 网关消费者 → K8s SA → 端口暴露；ProvisionManager / ProvisionTeamRooms / Deprovision* | 这是 Provisioner 的核心价值，**保留** |
| ② **Matrix 房间编排细节**（散落其中） | ProvisionWorker 内 200 行 Matrix 房间创建+成员+meta 逻辑；ProvisionTeamRooms 整个函数；ReconcileRoomMembership；ArchiveTeamRooms | 与基础设施编排混在一起，**但调用的是 matrix.Client 协议层** |

---

## 3. 业务操作清单（基于反向调用查询验证）

通过 grep 所有 `matrix.Client.*` / `Provisioner.matrix.*` / `Provisioner.{Matrix方法}` 调用，按**业务意图**重新归类。

### 3.1 业务域 1：用户身份生命周期（User Identity）

> **核心约束（Synapse 1.127）**：用户账号操作（创建/重设密码/封禁）走 admin API（不要求 in-room），登录走 CS API。这一域**不受房间授权模型影响**。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| U1 | ProvisionUser（password） | `provisioner.go:370, 1392`、`provisioner_human.go::RegisterLegacyUser` | 创建 Matrix 账号 | ✅ 可行：SynapseClient 已覆盖 `EnsureUser` → `PUT /_synapse/admin/v2/users/{id}` |
| U2 | ProvisionUser（AppService） | `provisioner.go:364`、`provisioner_human.go:35` | 同上，无密码 | ⚠️ 有条件可行：依赖 AppService 声明式注册（见 A1）+ AS login；AS registration 不能运行时注册 |
| U3 | Login（已知密码） | `ensureMatrixToken`（多处） | 刷新 access token | ✅ 可行：CS API `/login`，Synapse 标准实现 |
| U4 | Login（AppService） | `provisioner_human.go:78`、`provisioner.go:604,632` | 同上 | ✅ 可行：CS API `m.login.application_service`，Synapse 支持 |
| U5 | ResetPassword | `provisioner_human.go:71` | Human 重设密码 | ✅ 可行：SynapseClient 已覆盖 → `POST /_synapse/admin/v1/reset_password/{id}` |
| U6 | SetDisplayName | `provisioner_human.go:162` | Human 显示名 | ✅ 可行：CS API `PUT /profile/{userId}/displayname`，不要求 in-room |
| U7 | DeactivateUser | `provisioner_human.go:199` | Human 删除 | ⚠️ 实际是 leave-all-rooms：见 M7。Synapse admin API 有 `POST /_synapse/admin/v1/deactivate/{userId}` 但 AgentTeams 当前未用 |

### 3.2 业务域 2：房型化房间创建（4 种独立房型）

> **核心约束（Synapse 1.127）**：
> - CreateRoom 走 CS API `POST /createRoom`，**创建者自动 joined**（`room.py:1154-1164`），不要求预先 in-room
> - **`power_level_content_override.users` 必须包含 creator user_id**（`room.py:878-888`），否则返回 `400 M_BAD_JSON`
> - invite 列表在 creator join 之后处理（`room.py:972-988`），invitee 收到真实 invite（不自动 join）
> - 房间内的默认 PL：`state_default=50, events_default=0, kick=50, invite=50, ban=50`

| # | 业务操作 | 调用点 | 房间构成 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| R1 | CreateWorkerRoom | `ProvisionWorker:435-462` | worker + admin + authority(leader/manager/admin)，PL: worker=0, others=100 | ✅ 可行：creator=admin（空 CreatorToken），admin 在 PowerLevels 里 + 自动 joined |
| R2 | CreateTeamRoom | `ProvisionTeamRooms:820` | team-admin + leader + coordinators + members + workers，PL: 100 | ✅ 可行：creator=team admin（TeamAdminActorToken）或 admin，creator 都在 PowerLevels 里 |
| R3 | CreateLeaderDMRoom | `ProvisionTeamRooms:888` | leader + (team-admin or admin)，PL: 100，IsDirect=true | ✅ 可行：同上 |
| R4 | CreateManagerDMRoom | `ProvisionManager:1437` | manager + admin，PL: 100，IsDirect=true | ✅ 可行：creator=admin，admin 在 PowerLevels 里 |

**潜在 BUG（防御性，当前 4 调用点巧合不触发）**：`power_level_content_override.users` 必须含 creator。当前调用点都巧合满足，但建议在 `client.go::CreateRoom` 自动注入 creator 防御（见 contracts 文档 §4 修复 4）。

**4 种房型有完全不同的业务规则**：Power level 分配、metadata 结构、alias 格式、创建后流程（worker 房要 worker join、team 房要让 admin 离开、manager 房要等 LLM ready）。

### 3.3 业务域 3：房间成员管理

> **🔴 最关键约束（Synapse 1.127）——"操作者必须 in-room"**
>
> 1. **CS API 的 invite/kick/ban/unban** 全部要求 **sender（token 持有者）必须 joined**（`event_auth.py:687` `if not caller_in_room: raise NOT_JOINED`）
> 2. **Synapse admin API 不提供任何 single-user kick/ban/leave/unban 端点**——只有 `POST /v1/join/{room}`（强制 join）和 `DELETE /v2/rooms/{id}`（解散整房）
> 3. **kick 还要求 sender PL ≥ kick_level（默认 50）**（`event_auth.py:717`），且 sender PL > target PL
> 4. **invite 要求 sender PL ≥ invite_level（默认 50）**（`event_auth.py:703`）
> 5. **leave（self-leave）不要求 in-room**——受邀/已 leave 的用户都能 self-leave（`event_auth.py:683-685`）
>
> **解法**：AgentTeams 在所有 controller-managed 房间把 admin 写进 invite 列表（4 个 CreateRoom 调用点）→ admin 自动 joined + PL=100 ≥ 所有必需 level。所有成员管理操作走 admin token 默认成功。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| M1 | InviteMember | `team_controller.go:838`、`human_reconcile_rooms.go:51`、`provisioner.go:1142` | 把用户加入指定房 | ✅ 可行：admin 默认 in-room + PL=100 ≥ invite_level。幂等匹配精确（见 contracts §2，无 BUG） |
| M2 | RemoveMember（普通踢） | `provisioner.go:1174`、`human_reconcile_rooms.go:80` | 踢出房间 | ⚠️ 有 BUG：`KickFromRoomWithToken` 幂等匹配过宽（`"not in"` 误匹配 sender 不在房，`"cannot kick"` 误匹配 PL 不足）。见 contracts §1 修复 1 |
| M3 | ForceRemoveMember（强踢） | `provisioner.go:1179`、`worker_controller.go:429`、`team_controller.go:504`、`human_reconcile_delete.go:32` | 普通踢失败后的兜底 | 🔴 **关键缺口**：`shouldForceLeaveAfterKickError` 在 Synapse 下永不触发（消息不匹配），且 **Synapse admin API 没有 `/rooms/{id}/kick` 端点**（contracts §3）→ 强踢链彻底失效。修复后仍是 best-effort |
| M4 | ReconcileMembers | `ProvisionWorker:479`、`ProvisionTeamRooms:852,926` | 把房间成员驱动到期望集合 | ⚠️ 组合 M1+M2+M3，受上述 BUG 影响。team 房间用 actor token 规避（team admin 是 in-room operator） |
| M5 | LeaveSelf（用户主动离开） | `provisioner.go:281, 859, 920` | 用户自己离开某房 | ✅ 可行：self-leave 不要求 in-room（event_auth.py:683）。`LeaveRoom` 方法本身不幂等，但调用点有前置检查规避（contracts §6） |
| M6 | JoinSelf（用户主动加入） | `provisioner.go:504, 849, 920` | 用户接受邀请 | ✅ 可行：CS API `/join`，被 invite 后即可 join（contracts §5，无 BUG） |
| M7 | ForceLeaveAllRooms（批量清退） | `LeaveAllWorkerRooms/LeaveAllManagerRooms` | deprovision 清理 | ✅ 可行：循环调 LeaveRoom，用用户自己的 token。前提是 token 仍有效（creds 未被清） |

**M3 的真实业务影响**：当 admin 被 leave 出 team 房间（`provisioner.go:859`）后，对该 team 房间的 kick 走 team admin token（`actorToken`，provisioner.go:1172）。team admin 在房 + PL=100 → CS kick 直接成功。**M3 的 ForceLeaveRoom fallback 主要用于 worker room（admin 一直在房，PL 100 ≥ kick_level 50），实际极少触发**。修复 `shouldForceLeaveAfterKickError` 是为了让 Tuwunel 路径继续工作；Synapse 下承认强踢不可行，依赖 CS kick + admin 始终在房。

#### M5 vs M7 合并分析（用户要求读代码判断）

读代码后：
- **M5 LeaveSelf** 单点离开，用用户自己的 token：`provisioner.go:281` `p.matrix.LeaveRoom(ctx, roomID, token)`
- **M7 ForceLeaveAllRooms** 是 `leaveAllRooms` 函数（`provisioner.go:258-287`）：循环 `ListJoinedRooms` 后调用同一个 `LeaveRoom(ctx, roomID, token)`

**底层完全相同**——都是 CS API `POST /_matrix/client/v3/rooms/{id}/leave`。区别只在业务语义（单点 vs 批量）。

**结论**：不合并业务动词（语义不同），但都映射到 matrix.Client 的 `LeaveRoom`。在 matrix.Client 层不需要新增方法。

### 3.4 业务域 4：房间元数据与改名

> **核心约束（Synapse 1.127）**：所有非 membership 状态事件（`m.room.name`、自定义 `room.meta` 等）走 `PUT /rooms/{id}/state/{type}/{stateKey}`，sender 必须 joined（`event_auth.py:_check_event_sender_in_room`）+ PL ≥ `state_default`（默认 50）。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| D1 | SetRoomMetadata | `ProvisionWorker:483`、`ProvisionTeamRooms:871,936`、`ProvisionManager:1452` | 写 room.meta 自定义状态事件 | ✅ 可行：admin 默认 in-room + PL=100 ≥ state_default(50)。team 房间用 team admin token |
| D2 | RenameRoom | `ProvisionTeamRooms:838`、`ArchiveTeamRooms:1304,1310` | 改 m.room.name | ✅ 可行：同上。m.room.name send_level=50 |
| D3 | ArchiveRoom | `ArchiveTeamRooms:1300` | 改名加 `[deleted]` 后缀（不删房） | ✅ 可行：本质是 SetRoomName，Synapse 无 Archive 概念但 CS API 改名完全支持。**注意**：ArchiveTeamRooms 在 `ActorToken=""` 时 fallback 到 admin token，若 admin 已 leave team 房会失败——但是 best-effort，无业务影响 |

### 3.5 业务域 5：系统消息

> **核心约束（Synapse 1.127）**：发消息（`m.room.message`）走 `PUT /rooms/{id}/send/m.room.message/{txnId}`，sender 必须 joined（`_check_event_sender_in_room`）+ PL ≥ `events_default`（默认 0）。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| S1 | SendSystemMessage | `SendManagerWelcomeMessage:1652` | admin 身份发欢迎/提示消息 | ✅ 可行：admin 在 manager DM（创建时被 invite + 自动 joined）+ PL=100 ≥ events_default(0)。仅用于 manager DM，无 in-room 风险 |

### 3.6 业务域 6：房间解散与别名

> **核心约束（Synapse 1.127）**：
> - 别名操作（resolve/delete）走 CS API `/directory/room/{alias}`，**不要求 in-room**
> - 真正解散房间走 admin API `DELETE /v2/rooms/{id}`，**异步**返回 `delete_id`

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| X1 | ReleaseRoomAlias | `DeleteTeamRoomAliases:1286,1291`、`DeleteWorkerRoomAlias:1323`、`DeleteManagerRoomAlias:1334` | 删 Matrix alias（房保留） | ✅ 可行：CS API `DELETE /directory/room/{alias}` + admin API `DELETE /directory/room/{alias}`，都不要求 in-room |
| X2 | DeleteRoom（真解散） | `Provisioner.DeleteWorkerRoom:311`、`Provisioner.DeleteManagerRoom:323` → `deleteRoom:293` → `AdminCommand("!admin rooms delete-room")` | 真正解散房间 | ⚠️ 部分可行：SynapseClient 已翻译为 `DELETE /v2/rooms/{id}`。但 (a) **v2 异步**——返回 `delete_id` 后台执行，`synDeleteRoom` 把 2xx 当成功立即返回不轮询；(b) `delete_id` 被丢弃无法查状态。当前行为是 fire-and-forget，与 Tuwunel admin bot 异步语义一致（已确认 C 方案，保持现状） |

**Synapse 如何处理 ArchiveRoom**：Synapse 没有 Archive 概念。当前 `ArchiveTeamRooms` 实现是 CS API 改 `m.room.name` 加 `[deleted]` 后缀（`provisioner.go:1300-1315`），Synapse 完全支持，无需特殊处理。

#### X2 不是死代码（修正之前的误判）

我之前判断 X2 是死代码——**错了**。反查后确认：

- `manager_reconcile_delete.go:24` 调 `r.Provisioner.DeleteManagerRoom(ctx, m.Status.RoomID)`
- `member_reconcile.go:1988` 调 `d.Provisioner.DeleteWorkerRoom(ctx, m.ExistingRoomID)`
- 两者都走 `Provisioner.deleteRoom`（`provisioner.go:293`）→ `AdminCommand("!admin rooms delete-room <id>")`
- Synapse 下 `AdminCommand` switch 已有 `delete-room` 分支 → `synDeleteRoom` → `DELETE /_synapse/admin/v2/rooms/{id}`

**所以 X2 在 Synapse 下能调到，但有 2 个问题**：

1. **DELETE v2 异步**：`synDeleteRoom` 把 2xx 当成功立即返回，但房间实际还在。需要轮询 `GET /_synapse/admin/v2/rooms/{id}/delete_status/{delete_id}` 才能确认完成。
2. **返回的 `delete_id` 被丢弃**：`synAdminCall`（`synapse_client.go:72`）签名是 `error`，不返回 body，所以拿不到 `delete_id`。

**Synapse 如何处理 ArchiveRoom**：Synapse 没有 Archive 概念。当前 `ArchiveTeamRooms` 实现是 CS API 改 `m.room.name` 加 `[deleted]` 后缀（`provisioner.go:1300-1315`），Synapse 完全支持，无需特殊处理。

### 3.7 业务域 7：AppService 治理

> **🔴 核心约束（Synapse 1.127）**：Synapse **没有运行时 AS 注册 API**。Application Service 只能通过 `homeserver.yaml` 的 `app_service_config_files:` 列表**声明式**加载（启动时读取）。`GET /_synapse/admin/v1/appservices` 只读列表。
>
> Tuwunel 的 admin bot `!admin appservices register/unregister` 在 Synapse 下**完全无对应**——`SynapseClient.AdminCommand` switch 里没有 `appservices` 分支，会返回 `unsupported !admin command`。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| A1 | RegisterAppService | `initializer.go:213` | controller 启动注册自己 | 🔴 **不可运行时实现**：必须改用声明式——Helm 渲染 AS registration YAML 挂载到 Synapse pod + `homeserver.yaml` 加 `app_service_config_files:`。`SynapseClient.RegisterAppService` 覆盖为 smoke-test-only |
| A2 | SmokeTestAppService | `initializer.go:216` | 验证 AS 工作 | ✅ 可行：`AppServiceSmokeTest` 走 CS API `m.login.application_service`（`LoginAppServiceUser`），与声明式注册兼容 |
| A3 | RotateAppServiceToken | `appservice_mgmt_handler.go:60` | 运行时轮转 token | 🔴 **不可运行时实现**：声明式架构下 token 轮转需 `helm upgrade`（更新 AS Secret + restart Synapse pod）。`RotateToken` handler 在 Synapse 下返回 501 |

### 3.8 业务域 8：可观察性/查询

> **核心约束（Synapse 1.127）**：所有只读查询（成员列表、已加入房间、alias 解析）走 CS API，admin 用户有读取任意房间状态的特权（`auth/base.py:206` admin bypass `check_user_in_room`）。**不要求 in-room**。

| # | 业务操作 | 调用点 | 目的 | Synapse 1.127 约束与可行性 |
|---|---|---|---|---|
| O1 | ListRoomMembers | `ReconcileRoomMembership:1113`、`observedRoomMembership:1223` | 读房间成员做 diff | ✅ 可行：CS API `GET /rooms/{id}/members`，admin bypass in-room 检查 |
| O2 | ListJoinedRooms | `leaveAllRooms:275` | 读用户参与的房 | ✅ 可行：CS API `GET /joined_rooms`，用用户自己的 token |
| O3 | IsManagerJoinedDM | `manager_reconcile_welcome.go:132` | 探测 manager 就绪 | ✅ 可行：本质是 ListRoomMembers 封装，admin bypass |
| O4 | ResolveRoomAlias | `client.go::CreateRoom` 内部 | alias→roomID（幂等） | ✅ 可行：CS API `GET /directory/room/{alias}`，不要求 in-room |

#### O1-O4 是否算 RoomOps 职责（用户要求读源码判断）

读代码后：
- 这些都是**只读查询**，无副作用，调用方用它们做业务决策（diff、就绪探针、幂等解析）
- 它们已经存在于 matrix.Client 接口里，是**协议层能力**
- Synapse 与 Tuwunel 在这些查询上行为完全一致（CS API）

**结论**：O1-O4 是 matrix.Client 协议层方法，保留不动，**不需要单独抽象**。业务层（Provisioner）直接调用即可。

### 3.9 业务可行性总览（基于源码核对）

按已核对的接口契约（`synapse-interface-contracts.md` §1-§8），30 个业务操作的可行性分布：

| 可行性 | 数量 | 业务操作 |
|---|---|---|
| ✅ 直接可行（无需改动） | 20 | U1, U3-U6, R1-R4, M1, M5-M7, D1-D3, S1, X1, A2, O1-O4 |
| ⚠️ 有 BUG 但可修复 | 3 | M2（kick 幂等匹配）、M4（受 M2 影响）、X2（v2 异步不轮询，已确认保持现状） |
| 🔴 架构性不可行（需声明式方案） | 3 | M3（admin API 无 kick 端点，依赖 CS kick + admin 始终在房）、A1（AS 运行时注册）、A3（AS token 运行时轮转） |
| ⚠️ 有条件可行 | 2 | U2（依赖 A1 声明式 AS）、U7（实际是 M7，可行） |
| 🚧 待核对 | 2 | 其余 admin API（ResetPassword 已核对可行、EnsureUser 待核对） |

**关键洞察**：

1. **"操作者必须 in-room"约束被 admin 默认在房 + PL=100 规避**——AgentTeams 在所有 CreateRoom 调用里都 invite admin（4 个调用点），admin 自动 joined，所有后续 CS API 操作（invite/kick/state/message）默认成功。
2. **唯一真实的失败场景**是 `provisioner.go:859` 显式让 admin 离开 team 房间——但业务此时切换到 `teamAdminActorToken`（team admin 是 in-room operator），不走 admin 路径。**这是现有代码的正确设计**，不需要 OperatorResolver 之类的新抽象。
3. **M3 强踢能力在 Synapse 下确实丧失**——但业务影响小：worker room 里 admin 始终在房 + PL=100，CS kick 直接成功；team 房间用 team admin token kick。ForceLeaveRoom 兜底主要用于 Tuwunel。
4. **AppService（A1/A3）是唯一真正需要架构改动的部分**——必须改用 Helm 声明式渲染 + Synapse pod 重启加载。

**因此真正的工作量**（按优先级）：
- **P0（必修）**：M2 kick 幂等匹配 BUG（contracts §1 修复 1+2）
- **P1（声明式 AS）**：A1/A3 Helm 渲染 + SynapseClient 覆盖（独立工作流）
- **P2（防御性，低）**：CreateRoom 自动注入 creator（contracts §4 修复 4）、LeaveRoom 幂等化（contracts §6 修复 5）
- **P3（可选）**：X2 delete-room 状态轮询（已确认保持现状，不做）

---

## 4. 架构决策（核心，经过用户质疑后修正）

### 4.1 用户的质疑（关键）

> "我理解 Provisioner 下调用的是 Matrix 通用能力，为什么抽象出 RoomOps/UserOps/AppServerOps？"

这个质疑**完全正确**。我之前提议的 RoomOps/UserOps/AppServiceOps 三层抽象是**过度设计**。理由：

- `matrix.Client` 已经是 Matrix 协议层的抽象（CS API + admin 扩展）
- TuwunelClient 和 SynapseClient 都实现了 matrix.Client
- CS API 在两个 homeserver 上行为一致（只有 AdminCommand 的实现不同）
- Provisioner 调用 `matrix.Client` 是合理的——它确实在用"Matrix 通用能力"

### 4.2 真正的问题在哪

不是缺一层业务抽象，而是 **`matrix.Client` 的某些方法在 Synapse 下需要 fallback 逻辑**，而这些 fallback 需要**业务上下文**（"找一个房间内的 sponsor token"）。

具体矛盾：
- 协议层（matrix.Client）不应该依赖业务层概念（CredentialStore、admin user）
- 但 Synapse 的"必须 operator 在场"要求知道业务上下文（谁能当 sponsor）
- 把 fallback 写进 Provisioner 会让业务层感知 homeserver 差异（错误的方向）

### 4.3 最终方案：OperatorResolver 依赖注入

**保持 matrix.Client 协议层不变**，让 SynapseClient 通过依赖注入获得"找 operator"的能力。

```go
// 新接口：知道如何找一个"在房间内的、有权限的 operator token"
// 由 Provisioner 实现并注入到 SynapseClient
type OperatorResolver interface {
    // ResolveInRoomOperator 返回一个在 roomID 内、有足够 PL 执行 admin 操作
    // 的用户 ID + access token。优先级：team admin > manager > 任何 controller-managed 用户。
    // 失败时返回 ("", "", err)。
    ResolveInRoomOperator(ctx context.Context, roomID string) (userID, token string, err error)

    // EnsureAdminInRoom 让 admin 用户进入房间（通过 in-room operator 邀请）。
    // 用于 admin 长期不在房间、需要重新进入的场景。
    EnsureAdminInRoom(ctx context.Context, roomID string) error
}
```

**SynapseClient 内部 fallback 链**（业务层零感知）：

```go
func (s *SynapseClient) InviteToRoom(ctx, roomID, userID string) error {
    // 1. 先用 admin token 试（admin 通常在房间里）
    adminTok, _ := s.ensureAdminToken(ctx)
    err := s.InviteToRoomWithToken(ctx, roomID, userID, adminTok)
    if err == nil || !isNotInRoomErr(err) {
        return err
    }
    // 2. admin 不在房间 → 用 OperatorResolver 找一个 in-room operator
    opUserID, opToken, opErr := s.operatorResolver.ResolveInRoomOperator(ctx, roomID)
    if opErr != nil {
        return fmt.Errorf("admin not in room and no operator available: %w (opErr: %v)", err, opErr)
    }
    // 3. 用 operator token 重试
    return s.InviteToRoomWithToken(ctx, roomID, userID, opToken)
}
```

同样的 fallback 模式应用到所有"operator 必须在场"的方法：`InviteToRoom` / `KickFromRoom`（CS） / `SetRoomName` / `SetRoomState` / `SendMessage` / `SendMessageAsAdmin`。

**注意**：`KickFromRoom` 已有 `ForceLeaveRoom` 兜底（走 admin API，不要求 in-room）。SynapseClient.KickFromRoom 的 fallback 链是：admin token → operator token → 调用方走 ForceLeaveRoom。

### 4.4 方案对比

| 维度 | 之前方案（RoomOps 三层抽象） | 最终方案（OperatorResolver） |
|---|---|---|
| 新抽象数量 | 3 个（RoomOps/UserOps/AppServiceOps） | 1 个（OperatorResolver） |
| Provisioner 改动 | 大幅重写（搬出所有 Matrix 逻辑） | 加 2 个方法（实现 OperatorResolver） |
| TuwunelClient 改动 | 零 | 零 |
| SynapseClient 改动 | 实现 RoomOps（重写） | 加 fallback 链（小改） |
| 业务层感知 homeserver | 否 | 否 |
| 新代码量 | 500+ 行 | ~100 行 |
| 测试影响 | 大量重写 | 加几个 SynapseClient 测试 |

### 4.5 AppService 的处理

AppService 治理**确实需要不同的抽象**，因为 Synapse 是声明式（Helm 渲染），Tuwunel 是运行时注册。但这**不需要新接口**——直接在 SynapseClient 上覆盖方法即可：

```go
// SynapseClient 覆盖（继承自 TuwunelClient）
func (s *SynapseClient) RegisterAppService(ctx, reg) error {
    // 声明式：只做 smoke test，不写
    if err := s.AppServiceSmokeTest(ctx); err != nil {
        return fmt.Errorf("synapse AS not active (declared via Helm): %w", err)
    }
    return nil
}

func (s *SynapseClient) UnregisterAppService(ctx, id) error {
    return fmt.Errorf("synapse: AS is declarative; set matrix.appservice.enabled=false in Helm")
}
```

业务层（initializer / mgmt_handler）零改动——它们调 `matrix.Client.RegisterAppService`，Synapse 自动用声明式语义。

### 4.6 Provisioner 的最终定位（确认）

```
controller (K8s reconcile)
    │
    └─ Provisioner（保留，几乎不变）
          职责：跨基础设施编排（凭证 → Matrix → MinIO → Gateway → K8s SA）
          │
          ├─ matrix.Client（保留，协议层）
          │     ├─ TuwunelClient（零改动）
          │     └─ SynapseClient（加 OperatorResolver fallback）
          │
          ├─ gateway.Client（不变）
          ├─ ossAdmin（不变）
          ├─ CredentialStore（不变）
          └─ 新增：实现 OperatorResolver 接口（2 个方法）
                  注入到 SynapseClient 构造时
```

---

## 5. OperatorResolver 实现细节

### 5.1 Provisioner 实现 OperatorResolver

```go
// internal/service/provisioner.go 新增

// ResolveInRoomOperator 在 roomID 内找一个 controller-managed 的、有 PL 的 operator。
// 优先级：team admin > manager > 任何 controller-managed worker。
func (p *Provisioner) ResolveInRoomOperator(ctx context.Context, roomID string) (string, string, error) {
    members, err := p.matrix.ListRoomMembers(ctx, roomID)
    if err != nil {
        return "", "", fmt.Errorf("list members of %s: %w", roomID, err)
    }

    // 收集所有 join 状态的 controller-managed 用户
    // controller-managed = 在 p.creds 里能找到的（worker/manager）
    type candidate struct {
        userID   string
        localpart string
        priority int // 越小越优先：manager=0, team_admin=1, worker=2
    }
    var candidates []candidate

    names, _ := p.creds.List(ctx)
    nameSet := make(map[string]bool, len(names))
    for _, n := range names {
        nameSet[n] = true
    }

    for _, m := range members {
        if m.Membership != "join" {
            continue
        }
        localpart := parseLocalpart(m.UserID, p.matrixDomain)
        if !nameSet[localpart] {
            continue // 不是 controller-managed
        }
        priority := 2 // worker
        if localpart == "manager" {
            priority = 0
        }
        // team admin 的判断需要业务知识，简化：任何以 "admin-" 或团队 admin 命名的用户优先级 1
        // （实际实现可能需要读 Team CR 反查）
        candidates = append(candidates, candidate{m.UserID, localpart, priority})
    }

    if len(candidates) == 0 {
        return "", "", fmt.Errorf("no controller-managed operator in room %s", roomID)
    }

    // 按 priority 排序，取第一个
    sort.Slice(candidates, func(i, j int) bool {
        return candidates[i].priority < candidates[j].priority
    })
    chosen := candidates[0]

    creds, err := p.creds.Load(ctx, chosen.localpart)
    if err != nil || creds == nil {
        return "", "", fmt.Errorf("load creds for %s: %w", chosen.localpart, err)
    }
    token, err := p.ensureMatrixToken(ctx, chosen.localpart, creds)
    if err != nil {
        return "", "", fmt.Errorf("get token for %s: %w", chosen.localpart, err)
    }
    return chosen.userID, token, nil
}

// EnsureAdminInRoom 通过 in-room operator 重新邀请 admin 进房间。
func (p *Provisioner) EnsureAdminInRoom(ctx context.Context, roomID string) error {
    opUserID, opToken, err := p.ResolveInRoomOperator(ctx, roomID)
    if err != nil {
        return fmt.Errorf("no operator to re-invite admin: %w", err)
    }
    adminMatrixID := p.matrix.UserID(p.adminUser)
    if err := p.matrix.InviteToRoomWithToken(ctx, roomID, adminMatrixID, opToken); err != nil {
        return fmt.Errorf("operator %s invite admin: %w", opUserID, err)
    }
    // admin 自己 join（admin token 内部获取）
    adminToken, err := p.matrix.(interface {
        AdminToken(context.Context) (string, error)
    }).AdminToken(ctx)
    // ↑ 这里需要 matrix.Client 暴露 admin token 获取，或新增 JoinAsAdmin
    _ = adminToken
    return p.matrix.JoinRoom(ctx, roomID, /* admin token */)
}
```

### 5.2 matrix.Client 需要新增的方法

为了让 OperatorResolver 能让 admin 重新 join，matrix.Client 需要新增：

```go
type Client interface {
    // ... 现有方法 ...

    // JoinRoomAsAdmin 让 homeserver admin 用户 join 指定房间。
    // 内部用缓存的 admin token。
    JoinRoomAsAdmin(ctx context.Context, roomID string) error
}

func (c *TuwunelClient) JoinRoomAsAdmin(ctx context.Context, roomID string) error {
    token, err := c.ensureAdminToken(ctx)
    if err != nil {
        return fmt.Errorf("join as admin: %w", err)
    }
    return c.JoinRoom(ctx, roomID, token)
}
```

`SynapseClient` 继承自 `TuwunelClient`，自动获得。

### 5.3 SynapseClient 构造签名变化

```go
func NewSynapseClient(cfg Config, httpClient *http.Client, resolver OperatorResolver) *SynapseClient {
    return &SynapseClient{
        TuwunelClient:     NewTuwunelClient(cfg, httpClient),
        operatorResolver:  resolver,
    }
}
```

**关键**：Tuwunel 部署下不构造 SynapseClient，所以 Tuwunel 路径完全不受影响。Provisioner 在初始化 SynapseClient 时把自己作为 OperatorResolver 注入（`NewSynapseClient(cfg, http, p)`）。

---

## 6. Synapse 房间操作的授权矩阵（修正版）

| 业务操作 | Tuwunel 路径 | Synapse 路径 | 备注 |
|---|---|---|---|
| CreateWorkerRoom | admin token（admin 在 invite 列表，自动在房间） | 同左 | 两个 HS 都工作 |
| CreateTeamRoom | TeamAdminActorToken | 同左 | team admin 是创建者+操作者 |
| CreateLeaderDMRoom | TeamAdminActorToken | 同左 | 同上 |
| CreateManagerDMRoom | admin token（admin 在 invite） | 同左 | admin 是创建者+操作者 |
| InviteMember（admin 路径） | admin bot 特权 | admin token 失败 → OperatorResolver fallback | 关键差异点 |
| InviteMember（actor token 路径） | actor token | 同左 | actor 在房间，工作 |
| RemoveMember（普通踢） | admin bot 特权 | admin token 失败 → operator token → 失败 → ForceLeaveRoom | ForceLeaveRoom 走 admin API |
| ForceRemoveMember | admin bot `!admin force-leave-room` | `POST /_synapse/admin/v1/rooms/{id}/kick` | 已覆盖 |
| ReconcileMembers | 上述组合 | 上述组合 + fallback | |
| SetRoomMetadata | admin bot 特权 | admin token 失败 → OperatorResolver fallback | |
| RenameRoom | admin bot 特权 | 同上 | |
| ArchiveRoom | 改 m.room.name（CS API） | 同左 | 两 HS 都工作 |
| SendSystemMessage | admin bot 特权 | admin token 失败 → OperatorResolver fallback | |
| ReleaseAlias | CS API | 同左 | 两 HS 都工作 |
| DeleteRoom | `!admin rooms delete-room` | `DELETE /_synapse/admin/v2/rooms/{id}` | **异步问题待解决** |
| ForceLeaveAllRooms | 用户自己 LeaveRoom | 同左 | 用户用自己的 token，工作 |

---

## 7. 待解决的边缘问题

### 7.1 DeleteRoom 的异步问题（X2）

`DELETE /_synapse/admin/v2/rooms/{id}` 在 Synapse 1.127 是异步的。当前 `synDeleteRoom` 把 2xx 当成功，不轮询。

**解决方案选项**：
- **A. 同步等待**：`synDeleteRoom` 改为返回 `delete_id`，然后轮询 `GET /_synapse/admin/v2/rooms/{id}/delete_status/{delete_id}` 直到完成（带超时）
- **B. 文档说明**：声明 Synapse 下 delete 是 best-effort，依赖 Synapse 后台最终一致
- **C. 不动**：保持现状（fire-and-forget），和 Tuwunel 行为一致（Tuwunel 也是异步通过 admin bot）

**推荐**：C（保持现状）。理由：Tuwunel 的 delete-room 通过 admin bot 也是异步的，业务层已经按 best-effort 处理。Synapse 下行为一致，无需特殊同步。

### 7.2 历史房间迁移（不适用）

~~原方案考虑了从 Tuwunel 迁移到 Synapse 的场景。~~

**实际情况**：用户当前直接使用 Synapse 部署，不存在 Tuwunel 历史数据迁移问题。本节作废。

### 7.3 ArchiveTeamRooms 在 Synapse + ActorToken="" 下的边缘失败

`team_controller_test.go:1384` 显示存在 `ActorToken=""` 的归档路径。Synapse 下如果 admin 不在房间里，`SetRoomName` 会失败。

**解决方案**：SetRoomName 在 SynapseClient 内部也加 OperatorResolver fallback。归档失败仍是 best-effort（已是 non-fatal）。

---

## 8. 实施计划

### Phase 优先级（按代码位置 + 解决的问题）

| Phase | 改哪个目录 | 具体做什么 | 解决什么问题 |
|---|---|---|---|
| **1** | `internal/matrix/` | 给 SynapseClient 加 fallback 链（admin 不在房间时自动找替代操作者）；新增 OperatorResolver 接口、JoinRoomAsAdmin 方法、isNotInRoomErr 辅助 | "admin is not in room" 类报错 |
| **2** | `internal/service/provisioner.go` | Provisioner 实现 OperatorResolver（ResolveInRoomOperator / EnsureAdminInRoom），构造 SynapseClient 时注入 self | Phase 1 接口落地——没有这步，Phase 1 找不到 token |
| **3** | `internal/matrix/` + `helm/` | AppService 声明式：SynapseClient 覆盖 RegisterAppService 为 smoke-test-only；Helm 渲染声明式 AS YAML + homeserver.yaml 加 app_service_config_files | AS 注册/注销类报错（如果用 AS 模式） |
| **4** | `internal/config/` + `internal/app/` + `docs/` | env 变量 MatrixProvider、按 provider 选 client、文档 | 收尾 |

**Phase 1 和 2 绑定**：Phase 1 定义接口，Phase 2 实现它，一起才能工作。可合并为一个"核心修复"PR。

**实际优先级取决于当前报错**（待用户提供日志）：
- 报 "admin is not in room" 类 → Phase 1+2 最紧急
- 报 AS 注册失败 → Phase 3 最紧急
- 报 EnsureUser 失败 → 单独排查

### Phase 1：协议层增强（核心修复）

**目标**：让 SynapseClient 在内部处理"operator 必须在场"差异。

- [ ] 新增 `OperatorResolver` 接口（`internal/matrix/operator_resolver.go`）
- [ ] `matrix.Client` 接口加 `JoinRoomAsAdmin` 方法
- [ ] `TuwunelClient` 实现 `JoinRoomAsAdmin`
- [ ] `SynapseClient` 加 `operatorResolver` 字段，构造函数接受 OperatorResolver
- [ ] `SynapseClient` 覆盖 `InviteToRoom` / `KickFromRoom` / `SetRoomName` / `SetRoomState` / `SendMessage` / `SendMessageAsAdmin`，加 fallback 链
- [ ] 新增 `isNotInRoomErr` 辅助函数

**测试**：
- `SynapseClient` 各方法的 fallback 测试（mock OperatorResolver）
- `JoinRoomAsAdmin` 测试
- `isNotInRoomErr` 测试

### Phase 2：Provisioner 实现 OperatorResolver

**目标**：让 Provisioner 能当 SynapseClient 的 OperatorResolver。

- [ ] Provisioner 实现 `ResolveInRoomOperator` / `EnsureAdminInRoom`
- [ ] Provisioner 初始化时按 provider 选择 client 构造（注入 self 作为 resolver）
- [ ] 新增 `parseLocalpart` 辅助函数

**测试**：
- `ResolveInRoomOperator` 各种场景测试（房间内有 manager / 只有 worker / 空）

### Phase 3：AppService 声明式

**目标**：让 Synapse 下 AppService 通过 Helm 声明，controller 仅校验。

Go 侧：
- [ ] `SynapseClient.RegisterAppService` 覆盖为 smoke-test-only
- [ ] `SynapseClient.UnregisterAppService` 覆盖为返回错误
- [ ] `appservice_mgmt_handler.RotateToken` 在 Synapse 下返回 501

Helm 侧：
- [ ] 新增 `templates/matrix/synapse-appservice-secret.yaml`（声明式 AS YAML）
- [ ] `templates/matrix/synapse-configmap.yaml` 加 `app_service_config_files:`
- [ ] `templates/matrix/synapse-statefulset.yaml` 挂载 AS Secret
- [ ] `templates/secrets/runtime-env.yaml` 注入 AS env
- [ ] `templates/_helpers.tpl` 加 `appservice.pushURL` 辅助
- [ ] `values.yaml` 加 `matrix.appservice.*`
- [ ] `templates/00-validate.yaml` 加 AS 校验

### Phase 4：配置与文档

- [ ] `internal/config/config.go` 加 `MatrixProvider` env
- [ ] `internal/app/app.go` 按 provider 选 client
- [ ] `docs/synapse.md` 新增（声明式 AS 工作原理、Helm values、token 轮转流程）
- [ ] `changelog/current.md` 记录变更

---

## 9. 用户确认结果（2025-08-05）

1. ✅ **OperatorResolver 方案**：接受
2. ✅ **Phase 顺序**：理解后确认（详见第 8 节白话版）
3. ✅ **DeleteRoom 异步问题**：选 C（保持现状 fire-and-forget）
4. ✅ **历史房间迁移**：不适用（用户当前直接使用 Synapse，无 Tuwunel 历史数据；7.2 节已作废）
5. ✅ **OperatorResolver sponsor 优先级**：暂时不动（实现时找到第一个 controller-managed join 成员即可，不细化优先级排序）

**当前状态**：用户已在 Synapse 部署下遇到 reconcile 报错。具体报错类型待用户提供日志后确定，据此调整 Phase 优先级。

---

## 附录 A：业务操作 → matrix.Client 方法映射

| 业务操作 | 底层 matrix.Client 方法 | Synapse 是否需要 fallback |
|---|---|---|
| ProvisionUser | EnsureUser / EnsureAppServiceUser | 否（Synapse 已覆盖 EnsureUser） |
| Login | Login / LoginAppServiceUser | 否 |
| ResetPassword | SetPasswordAsAdmin → AdminCommand | 否（AdminCommand 已翻译） |
| CreateWorkerRoom | CreateRoom | 否（创建者自动在房间） |
| CreateTeamRoom | CreateRoom | 否 |
| CreateLeaderDMRoom | CreateRoom | 否 |
| CreateManagerDMRoom | CreateRoom | 否 |
| InviteMember | InviteToRoom / InviteToRoomWithToken | **是**（admin 不在房间时） |
| RemoveMember | KickFromRoom / KickFromRoomWithToken | **是**（admin 不在房间时；ForceLeaveRoom 已覆盖） |
| ReconcileMembers | 上述组合 | **是** |
| SetRoomMetadata | SetRoomState | **是** |
| RenameRoom | SetRoomName | **是** |
| ArchiveRoom | SetRoomName | **是** |
| SendSystemMessage | SendMessageAsAdmin → SendMessage | **是** |
| ReleaseAlias | DeleteRoomAlias | 否（CS API，不需 in-room） |
| DeleteRoom | AdminCommand("!admin rooms delete-room") | 否（AdminCommand 已翻译；异步问题见 7.1） |
| ForceLeaveAllRooms | ListJoinedRooms + LeaveRoom（用户 token） | 否（用户自己的 token） |
| ListRoomMembers | ListRoomMembers / ListRoomMembersWithToken | 否 |
| ListJoinedRooms | ListJoinedRooms | 否 |
| IsManagerJoinedDM | ListRoomMembers 封装 | 否 |
| RegisterAppService | RegisterAppService | 否（SynapseClient 覆盖为声明式） |

**需要 OperatorResolver fallback 的方法**：`InviteToRoom` / `KickFromRoom` / `SetRoomName` / `SetRoomState` / `SendMessage` / `SendMessageAsAdmin`（共 6 个）。
