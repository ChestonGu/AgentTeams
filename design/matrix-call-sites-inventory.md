# Matrix 调用点完整反查（抽象层设计依据）

> **目的**：穷举 AgentTeams controller 里所有调用 Matrix/Tuwunel 能力的位置，作为 `matrix` 抽象层（`internal/matrix`）的接口设计依据。
>
> **方法**：grep 所有 `p.matrix.*` / `.matrix.*(` / `matrix.Client` / `matrix.Config` / `!admin` 命令字符串 / 直接调 matrix.Client 的非 Provisioner 代码。
>
> **结论先行**：共发现 **34 个业务调用点**，分布在 5 个文件，按业务能力可归为 **8 大类**。`matrix.Client` 的 30+ 个协议层方法被业务层直接调用，全部需要进抽象层。

> **历史标注（2026-08）**：本文档的调用点反查结论已转化为 openspec proposal
> [`synapse-support`](../openspec/changes/synapse-support/) 并落地实现
>（34 个调用点全部迁移到 `MatrixOps` 抽象层，见 proposal tasks.md Phase 1–4）。
> 本文档作为**历史依据**保留，记录抽象层设计的原始依据。

---

## 1. 调用点分布概览

| 文件 | 调用点数 | 角色 |
|---|---|---|
| `internal/service/provisioner.go` | 50+ | 业务编排核心（Worker/Manager/Team provision） |
| `internal/service/provisioner_human.go` | 11 | Human 用户 provision |
| `internal/initializer/initializer.go` | 4 | controller 启动初始化（admin 用户、AS 注册、健康检查） |
| `internal/server/appservice_mgmt_handler.go` | 3 | HTTP endpoint（AS token 轮转） |
| `internal/matrix/room_meta.go` | 1 | 构造 state event（间接） |

**业务调用方**（不直接调 matrix.Client，通过 Provisioner 接口）：controller 包下的 7 个 reconcile 文件。

---

## 2. matrix.Client 接口方法使用清单（30 个方法，全量）

### 2.1 用户生命周期（7 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `EnsureUser(ctx, EnsureUserRequest)` | `provisioner.go:370, 1399`、`provisioner_human.go:54`、`initializer.go:194` | 创建/恢复 Matrix 账号（password 模式） |
| `EnsureAppServiceUser(ctx, username)` | `provisioner.go:364, 1393`、`provisioner_human.go:36` | 创建账号（AS 模式，无密码） |
| `Login(ctx, username, password)` | `provisioner.go:606, 634`、`provisioner_human.go:86`、`initializer.go:184`（健康检查） | 密码登录获取 token |
| `LoginAppServiceUser(ctx, username)` | `provisioner.go:604, 632`、`provisioner_human.go:79` | AS 登录获取 token |
| `SetPasswordAsAdmin(ctx, userID, password)` | `provisioner_human.go:72`、`provisioner.go:1756`（BackfillLegacyPasswords） | 重设密码（admin bot） |
| `SetDisplayName(ctx, userID, token, name)` | `provisioner_human.go:163` | 设显示名 |
| `UserID(localpart)` | `provisioner.go:223, 337-339, 410, 432, 776-780, 815, 986, 1005, 1019, 1160, 1364-1365, 1621, 1755`（15 处） | 构造 `@localpart:domain` |
| `VerifyAccessToken(ctx, token)` | （接口声明，未在业务层直接调用——AS smoke test 间接用） | 验证 token |

### 2.2 房间创建（1 个方法，4 种房型）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `CreateRoom(ctx, CreateRoomRequest)` | `provisioner.go:444, 455, 820, 888, 1437`（5 处，含 1 处重试） | 4 种房型：worker / team / leader DM / manager DM |

### 2.3 房间成员管理（8 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `InviteToRoom(ctx, roomID, userID)` | `provisioner.go:1082, 1142` | admin token 邀请 |
| `InviteToRoomWithToken(ctx, roomID, userID, token)` | `provisioner.go:955, 1140` | 指定 inviter token 邀请（leader/team admin） |
| `KickFromRoom(ctx, roomID, userID, reason)` | `provisioner.go:1088, 1174` | admin token 踢人 |
| `KickFromRoomWithToken(ctx, roomID, userID, reason, token)` | `provisioner.go:1172` | 指定 kicker token 踢人 |
| `JoinRoom(ctx, roomID, userToken)` | `provisioner.go:504, 849, 920, 945, 958`（5 处） | 用户自己加入 |
| `LeaveRoom(ctx, roomID, userToken)` | `provisioner.go:281, 859` | 用户/admin 自己离开 |
| `ListRoomMembers(ctx, roomID)` | `provisioner.go:1113, 1224, 1622` | 读房间成员（admin） |
| `ListRoomMembersWithToken(ctx, roomID, token)` | `provisioner.go:1111, 1232` | 读房间成员（指定 token） |

### 2.4 房间元数据（3 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `SetRoomName(ctx, roomID, name, token)` | `provisioner.go:838, 1304, 1310` | 改 m.room.name |
| `SetRoomState(ctx, roomID, type, key, content, token)` | `provisioner.go:483, 871, 936, 1452` | 写自定义 state event（room.meta） |
| `SendMessageAsAdmin(ctx, roomID, body)` | `provisioner.go:1652` | admin 发系统消息（manager welcome） |

### 2.5 别名管理（2 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `DeleteRoomAlias(ctx, alias)` | `provisioner.go:452, 1286, 1291, 1323, 1334` | 删 alias（5 处） |
| `ResolveRoomAlias(ctx, alias)` | `matrix/client.go::CreateRoom` 内部调用 | alias→roomID（幂等） |

### 2.6 查询（1 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `ListJoinedRooms(ctx, userToken)` | `provisioner.go:275` | 读用户参与的所有房（deprovision 清理用） |

### 2.7 Admin 命令（admin bot，1 个方法 + 5 种命令字符串）

| 方法 | 调用点 | 命令字符串 | 业务语义 |
|---|---|---|---|
| `AdminCommand(ctx, cmd)` | `provisioner.go:298` | `!admin rooms delete-room <roomID>` | 解散整房 |
| `AdminCommand(ctx, cmd)` | `provisioner_human.go:193` | `!admin users force-leave-room <userID> <roomID>` | 强踢兜底 |
| `AdminCommand(ctx, cmd)` | `provisioner_human.go:202` | `!admin users deactivate <userID>` | 封禁用户 |
| `AdminCommand(ctx, cmd)` | `matrix/client.go:278, 445`（内部） | `!admin users reset-password <userID> <password>` | 重设密码（被 `SetPasswordAsAdmin` 包装） |
| `AdminCommand(ctx, cmd)` | `matrix/appservice.go:85`（内部） | `!admin appservices register\n```yaml\n...``` | AS 注册 |
| `AdminCommand(ctx, cmd)` | `matrix/appservice.go:103`（内部） | `!admin appservices unregister <id>` | AS 注销 |

### 2.8 AppService 治理（3 个方法）

| 方法 | 调用点 | 业务语义 |
|---|---|---|
| `RegisterAppService(ctx, reg)` | `initializer.go:213`、`appservice_mgmt_handler.go:60` | 注册 AS |
| `UnregisterAppService(ctx, id)` | `matrix/appservice.go:101`（内部，由 RegisterAppService fallback 调） | 注销 AS |
| `AppServiceSmokeTest(ctx)` | `initializer.go:216`、`appservice_mgmt_handler.go:66`、`matrix/appservice.go:68`（内部） | AS smoke test |

---

## 3. 按业务能力分组（抽象层接口设计输入）

### 3.1 用户身份与凭证（User Identity & Credentials）

**业务能力**：创建/登录/重设密码/封禁 Matrix 用户账号。

**涉及方法**：`EnsureUser` / `EnsureAppServiceUser` / `Login` / `LoginAppServiceUser` / `SetPasswordAsAdmin` / `SetDisplayName` / `UserID` / `VerifyAccessToken` + admin 命令 `deactivate`

**业务调用点**：
- `ProvisionWorker` step 2：注册 worker Matrix 账号
- `ProvisionManager` step 2：注册 manager Matrix 账号
- `EnsureHumanUser` / `RegisterLegacyUser` / `RegisterAppServiceUser`：Human 用户 provision
- `RefreshWorkerCredentials` / `ensureMatrixToken`：刷新 token
- `BackfillLegacyPasswords`：批量回填密码
- `Initializer.registerAdmin`：启动时创建 admin 账号
- `Initializer.waitForMatrix`：健康检查（Login 探活）
- Human SSO 路径（`externalsso/source.go`、`legacypassword/source.go`）

### 3.2 房间生命周期（Room Lifecycle）

**业务能力**：创建/解散/归档房间，管理别名。

**涉及方法**：`CreateRoom` / `DeleteRoomAlias` / `ResolveRoomAlias` + admin 命令 `rooms delete-room`

**业务调用点**：
- 4 种房型创建：worker / team / leader DM / manager DM
- `DeleteTeamRoomAliases` / `DeleteWorkerRoomAlias` / `DeleteManagerRoomAlias`：deprovision 时释放 alias
- `deleteRoom`：deprovision 时解散整房（worker/manager）
- `ArchiveTeamRooms`：team 归档（改名加 `[deleted]`）

### 3.3 房间成员管理（Room Membership）

**业务能力**：邀请/踢人/自己加入/自己离开/批量清退/reconcile。

**涉及方法**：`InviteToRoom` / `InviteToRoomWithToken` / `KickFromRoom` / `KickFromRoomWithToken` / `JoinRoom` / `LeaveRoom` / `ListRoomMembers` / `ListRoomMembersWithToken` + admin 命令 `force-leave-room`

**业务调用点**：
- `ReconcileRoomMembership`：核心 diff 收敛（worker/team/leader DM）
- `EnsureRoomMember` / `EnsureRoomNonMember`：单点操作
- `ForceLeaveRoom`：强踢兜底
- `leaveAllRooms`：批量清退
- `ProvisionWorker` / `ProvisionTeamRooms` 内的 join/leave 调用
- Human 房间 reconcile（`human_reconcile_rooms.go` 通过 Provisioner 接口）

### 3.4 房间元数据与消息（Room Metadata & Messaging）

**业务能力**：写自定义 state event、改房间名、发系统消息。

**涉及方法**：`SetRoomName` / `SetRoomState` / `SendMessageAsAdmin`

**业务调用点**：
- 写 room.meta（4 个 CreateRoom 后都调用）
- Rename team 房（display name 同步）
- Archive team 房（改名加后缀）
- Send manager welcome message

### 3.5 AppService 治理（AppService Governance）

**业务能力**：注册/注销/校验/轮转 AS。

**涉及方法**：`RegisterAppService` / `UnregisterAppService` / `AppServiceSmokeTest` + admin 命令 `appservices register/unregister`

**业务调用点**：
- `Initializer.registerAppService`：启动时注册
- `RotateToken` HTTP endpoint：运行时轮转
- `Initializer.registerAppService` 的 smoke test

### 3.6 查询（Read Queries）

**业务能力**：只读查询，供业务决策。

**涉及方法**：`ListRoomMembers` / `ListRoomMembersWithToken` / `ListJoinedRooms`

**业务调用点**：
- `ReconcileRoomMembership`：读当前成员做 diff
- `observedRoomMembership`：检查指定用户是否在房
- `IsManagerJoinedDM`：manager 就绪探针
- `leaveAllRooms`：读用户所有房间

---

## 4. 非 Provisioner 调用方（需同样进抽象）

### 4.1 `initializer.go`

```go
i.Matrix.Login(ctx, "__healthcheck__", "invalid")  // 健康检查
i.Matrix.EnsureUser(ctx, matrix.EnsureUserRequest{...})  // 创建 admin
i.Matrix.RegisterAppService(ctx, reg)  // 注册 AS
i.Matrix.AppServiceSmokeTest(ctx)  // AS smoke test
```

**需迁移到**：`matrix.MatrixOps` 接口的对应方法。

### 4.2 `appservice_mgmt_handler.go`

```go
client := matrix.NewTuwunelClient(newCfg, nil)  // ⚠️ 直接构造 TuwunelClient
client.RegisterAppService(ctx, reg)
client.AppServiceSmokeTest(ctx)
```

**问题**：直接 `NewTuwunelClient`，硬编码 Tuwunel——Synapse 下完全失效。

**需迁移到**：通过抽象层接口调用，由 provider 选择实现。

### 4.3 controller 包（通过 Provisioner 接口间接调用）

这些调用方**已经通过 Provisioner 接口抽象**，不直接调 matrix.Client。但 Provisioner 的这些方法只是转发 `p.matrix.*`，迁移时 Provisioner 内部实现要改。

涉及 Provisioner 接口方法：`InviteToRoom` / `KickFromRoom` / `ForceLeaveRoom` / `JoinRoomAs` / `LeaveAllWorkerRooms` / `DeleteWorkerRoom` / `DeleteManagerRoom` / `DeleteWorkerRoomAlias` / `DeleteManagerRoomAlias` / `MatrixUserID` / `LoginAppServiceUser` / `SetDisplayName` 等。

---

## 5. 抽象层接口设计输入（按业务能力大方向）

基于上述反查，`matrix` 抽象层应按 **6 大业务能力** 组织接口（不是按 Matrix 协议动词）：

| 业务能力 | 方法数（估算） | 底层差异点 |
|---|---|---|
| **用户身份与凭证** | 8-10 | EnsureUser（Synapse 用 admin API PUT）、SetPassword（admin bot vs admin API）、Deactivate（admin bot 命令 vs admin API） |
| **房间生命周期** | 5-6 | CreateRoom（防御性注入 creator）、DissolveRoom（admin bot delete-room vs DELETE v2）、ReleaseAlias（CS API 一致） |
| **房间成员管理** | 8-10 | AddMember/RemoveMember（Synapse 需 make_room_admin fallback）、ForceLeave（admin bot vs 不存在）、ReconcileMembers（组合） |
| **房间元数据与消息** | 3-4 | SetRoomState/SetRoomName/SendMessage（Synapse 需 in-room fallback） |
| **AppService 治理** | 3-4 | RegisterAppService（运行时 vs 声明式）、UnregisterAppService（运行时 vs 不支持）、RotateToken（运行时 vs helm upgrade） |
| **查询** | 3-4 | 行为一致，但接口要统一 |

**总计约 30-38 个业务方法**（与 matrix.Client 协议层方法数相当，但按业务语义重组 + 加 fallback 逻辑封装）。

---

## 6. 抽象层落地策略（方案 B 渐进）

### Phase 1：核心 4 操作（验证抽象边界）

先抽 **CreateRoom / DissolveRoom / AddMember / RemoveMember** 到 `matrix.MatrixOps` 接口，Tuwunel 和 Synapse 各一个实现。Provisioner 对应调用点切换。

**验证点**：抽象边界是否正确、Synapse 的 make_room_admin fallback 是否工作。

### Phase 2：扩展到全部成员管理 + 房间生命周期

迁移 ReconcileMembers / JoinRoom / LeaveRoom / ForceLeaveAllRooms / ReleaseAlias / ArchiveRoom。

### Phase 3：扩展到元数据 + 消息 + 查询

迁移 SetRoomMetadata / RenameRoom / SendSystemMessage / ListRoomMembers / ListJoinedRooms。

### Phase 4：扩展到用户身份 + AppService

迁移 ProvisionUser / Login / ResetPassword / RegisterAppService 等。**这一步同时解决** `appservice_mgmt_handler.go` 硬编码 TuwunelClient 的问题。

### Phase 5：清理

移除业务层对 `matrix.Client` 的所有直接引用。`matrix.Client` 退化为 `internal/matrix` 实现的 HTTP 客户端层。

---

## 7. 待确认问题

1. **接口位置**：`internal/matrix/matrixops.go`（接口） + `internal/matrix/tuwunel.go` / `synapse.go`（实现）？还是单独 `internal/matrixops` 包？
   - 我倾向 `internal/matrix` 包内——与现有 `matrix.Client` 同包，便于访问 Config / types。

2. **接口粒度**：一个大的 `MatrixOps` 接口（30+ 方法），还是按业务能力拆 6 个小接口（`UserOps` / `RoomOps` / `MembershipOps` / `MetadataOps` / `AppServiceOps` / `QueryOps`）？
   - 我倾向**一个大的接口**——Provisioner 只持有一个依赖，简单。Go 风格也倾向大接口（io.ReadWriteCloser）。

3. **Provisioner 接口暴露**：Provisioner 现在通过 `WorkerProvisioner` / `ManagerProvisioner` / `HumanProvisioner` 接口向 controller 暴露 Matrix 相关方法（如 `InviteToRoom`）。这些是否保留？
   - 我倾向保留——controller 不直接调 MatrixOps，仍通过 Provisioner 接口。Provisioner 内部从 `p.matrix.*` 改为 `p.matrixOps.*`。

---

## 附录 A：完整调用点清单（按文件）

### A.1 `internal/service/provisioner.go`（50+ 处）

（详见 §2 各表格，此处省略重复）

### A.2 `internal/service/provisioner_human.go`（11 处）

```
36: p.matrix.EnsureAppServiceUser(ctx, username)
54: p.matrix.EnsureUser(ctx, matrix.EnsureUserRequest{Username: username})
72: p.matrix.SetPasswordAsAdmin(ctx, userID, password)
79: p.matrix.LoginAppServiceUser(ctx, username)
86: p.matrix.Login(ctx, username, password)
163: p.matrix.SetDisplayName(ctx, userID, accessToken, displayName)
169: p.matrix.InviteToRoom(ctx, roomID, userID)
178: p.matrix.JoinRoom(ctx, roomID, userToken)
183: p.matrix.KickFromRoom(ctx, roomID, userID, reason)
193: p.matrix.AdminCommand(ctx, "!admin users force-leave-room ...")
202: p.matrix.AdminCommand(ctx, "!admin users deactivate ...")
```

### A.3 `internal/initializer/initializer.go`（4 处）

```
184: i.Matrix.Login(ctx, "__healthcheck__", "invalid")  // 健康检查
194: i.Matrix.EnsureUser(ctx, matrix.EnsureUserRequest{...})  // admin
213: i.Matrix.RegisterAppService(ctx, reg)
216: i.Matrix.AppServiceSmokeTest(ctx)
```

### A.4 `internal/server/appservice_mgmt_handler.go`（3 处）

```
54: client := matrix.NewTuwunelClient(newCfg, nil)  // ⚠️ 硬编码 Tuwunel
59: reg := matrix.RenderAppServiceRegistration(newCfg)
60: client.RegisterAppService(ctx, reg)
66: client.AppServiceSmokeTest(ctx)
```

### A.5 `internal/matrix/room_meta.go`（1 处，间接）

```
10: func roomMetaState(content) []matrix.StateEvent {...}  // 构造 state event 类型，非方法调用
```
