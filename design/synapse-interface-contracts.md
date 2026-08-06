# Synapse 1.127 接口契约（AgentTeams 调用核对）

> **工作原则**：**凡 AgentTeams 调用 Synapse 接口（CS API 或 Admin API），必须先查 Synapse 1.127 源码确认实际行为、错误码、错误消息原文**，再判断 AgentTeams 代码是否满足业务需要。
>
> 不基于 Matrix spec 推断行为，只基于 `element-hq/synapse` 仓库 `v1.127.0` 标签的实际源码。
>
> 本文档按 AgentTeams 业务操作顺序，逐个核对 Synapse 实现，记录发现与 bug。

> **历史标注（2026-08）**：本文档的接口契约已转化为 openspec proposal
> [`synapse-support`](../openspec/changes/synapse-support/) 并落地实现。本文档作为
> **历史依据**保留，记录 Synapse 1.127 源码核对的原始证据；后续契约以 openspec
> proposal 与代码为准。面向运维的说明见 [`docs/synapse.md`](../docs/synapse.md)。

---

## 🔑 总览（关键修正）

经逐条核对 Synapse 1.127 源码，**AgentTeams 当前架构中所谓的"Synapse 授权问题"被严重高估**。原因：

1. **kick** 虽要求 sender joined（event_auth.py:687），但所有 controller-managed 房间创建时 admin 都在 invite 列表（provisioner.go:424, 795, 883, 1440）→ admin 自动 joined → 默认成功
2. **invite** 同样要求 sender joined，但 admin 默认 joined → 邀请链不中断
3. **SetRoomName/SetRoomState/SendMessage** 同样要求 sender joined，admin 默认在房 → 直接成功
4. **真正失败的场景**只有 `provisioner.go:859` 显式让 admin 离开 team 房间后对该 team 房间的后续操作——但业务此时用 `teamAdminActorToken`，不走 admin 路径

**真正需要做的是**：
- 修复 §1 的 2 个真实 bug（kick 错误匹配）
- 修复 §1 的 `shouldForceLeaveAfterKickError`（让它能识别 Synapse 错误字符串）
- 把 §2 的 idempotency 匹配改精确（区分"sender 不在房"vs"目标不在房"）
- 声明式 AppService（独立工作流）
- **不需要** OperatorResolver fallback（admin 默认在房，业务用 actor token 覆盖 team 房间场景）

---

## §1 KickFromRoom

### 1.1 AgentTeams 调用路径

```
provisioner.go::ReconcileRoomMembership
   ├─ KickFromRoom(ctx, roomID, userID, reason)        // admin token 路径
   │   └─ KickFromRoomWithToken(ctx, ..., adminToken)   // 继承自 TuwunelClient
   └─ KickFromRoomWithToken(ctx, ..., actorToken)       // actor token 路径
```

底层 HTTP：`POST /_matrix/client/v3/rooms/{roomId}/kick`，body `{"user_id":"...", "reason":"..."}`

**Fallback**：CS kick 失败且 `shouldForceLeaveAfterKickError(err)==true` → `ForceLeaveRoom` → `AdminCommand("!admin users force-leave-room ...")` → Synapse `POST /_synapse/admin/v1/rooms/{roomId}/kick`

### 1.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/handlers/room_member.py:1022, 1039` — 目标不在房间的检查（前置）
- `synapse/event_auth.py:_is_membership_change_allowed` (line 529-720) — kick 的 PL 与 sender 检查（事件授权层）
- `synapse/api/errors.py:93-95, 416` — errcode 与 UnstableSpecAuthError 定义

**关键源码片段**：

```python
# synapse/event_auth.py:566-571 — caller_in_room 判定
key = (EventTypes.Member, event.user_id)
caller = auth_events.get(key)
caller_in_room = caller and caller.membership == Membership.JOIN

# synapse/event_auth.py:680-690 — 非 join/knock 的 membership 变更前置检查
if Membership.JOIN != membership and Membership.KNOCK != membership:
    if (caller_invited or caller_knocked) and Membership.LEAVE == membership and target_user_id == event.user_id:
        return
    if not caller_in_room:  # ← sender 不 joined
        raise UnstableSpecAuthError(403, "%s not in room %s." % (event.user_id, event.room_id), errcode=Codes.NOT_JOINED)

# synapse/event_auth.py:709-720 — kick 的 PL 检查（membership=leave, target != sender）
elif Membership.LEAVE == membership:
    if target_banned and user_level < ban_level:
        raise UnstableSpecAuthError(403, "You cannot unban user %s." % target_user_id, errcode=INSUFFICIENT_POWER)
    elif target_user_id != event.user_id:
        kick_level = get_named_level(auth_events, "kick", 50)  # 默认 50
        if user_level < kick_level or user_level <= target_level:
            raise UnstableSpecAuthError(403, "You cannot kick user %s." % target_user_id, errcode=INSUFFICIENT_POWER)
```

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 | 源码 |
|---|---|---|---|---|
| K1 目标已 leave/ban | 403 | `M_FORBIDDEN` | `"The target user is not in the room"` | room_member.py:1022 |
| K2 目标无成员事件 | 403 | `M_FORBIDDEN` | `"The target user is not in the room"` | room_member.py:1039 |
| K3 sender 不 joined | 403 | `M_FORBIDDEN`+unstable:`NOT_JOINED` | `"@sender:domain not in room !room:domain."` | event_auth.py:687 |
| K4 sender joined 但 PL < kick_level(50) | 403 | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You cannot kick user @target:domain."` | event_auth.py:717 |
| K5 sender joined 但 PL ≤ target_level | 403 | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You cannot kick user @target:domain."` | event_auth.py:717 |
| K6 sender joined，PL ≥ kick_level，PL > target_level | 200 | — | — | 通过 |
| K7 目标 = sender 自己 | 200 | — | —（走 self-leave） | 通过 |
| K8 目标被 ban，sender PL < ban_level(50) | 403 | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You cannot unban user @target:domain."` | event_auth.py:711 |

**关键发现**：
1. **kick 实际上要求 sender joined**（event_auth.py:687）——修正之前"kick 不看 joined"的误判
2. **errcode 永远是 `M_FORBIDDEN`**——Synapse 用 `UnstableSpecAuthError` 时 stable errcode 仍是 `M_FORBIDDEN`，新码 `ORG.MATRIX.MSC3848.*` 放在 unstable 字段
3. **K3 错误消息 `"@x:y not in room !r:d."`** 包含 `"not in"`——会匹配 AgentTeams 的幂等条件！**这是 BUG**
4. **AgentTeams admin 在所有房间 PL=100**（provisioner.go:419, 805, 967, 1433），远高于 kick_level=50，所以 K4/K5 不会触发——但 idempotent 匹配仍要精确

### 1.3 AgentTeams 现有匹配逻辑核对

**`client.go:961-999` KickFromRoomWithToken**：

| 匹配条件 | Synapse 场景 | 结果 |
|---|---|---|
| `statusCode == 200/201` → nil | K6/K7 | ✅ 正确 |
| `statusCode == 404` → nil | 永不发生 | ⚠️ dead branch |
| `M_FORBIDDEN` + 含 `"not in"` → nil | K1, K2, **K3** | 🔴 **BUG**：K3（sender 不 joined）被误判幂等成功 |
| `M_FORBIDDEN` + 含 `"not a member"` → nil | 不匹配 Synapse | ⚠️ dead branch |
| `M_FORBIDDEN` + 含 `"cannot kick"` → nil | K4, K5 | 🔴 **BUG**：PL 不足被误判幂等成功 |
| 其他 → 错误 | — | ✅ |

**`provisioner.go:1259-1266` shouldForceLeaveAfterKickError**：

| 匹配条件 | Synapse 场景 | 结果 |
|---|---|---|
| `m_forbidden` + `not have enough power` | 无 | ⚠️ Tuwunel 风格 |
| `m_forbidden` + `power` | 无 | ⚠️ 同上 |
| Synapse K4/K5 `"You cannot kick user"` | — | 🔴 **BUG**：不触发 fallback |
| Synapse K3 `"@x:y not in room"` | — | 🔴 **BUG**：不触发 fallback |

**现有测试 `provisioner_team_test.go:896`**：mock 用 Tuwunel 风格 `"sender does not have enough power"`，**与 Synapse 实际完全不符**，无法覆盖。

### 1.4 修复方案

**修复 1：`KickFromRoomWithToken`（`client.go:990-996`）**

精确匹配幂等条件——**只匹配目标不在房间**，不匹配 sender 不在房间、不匹配 PL 不足。

```go
// 修改前
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    lower := strings.ToLower(resp.Error)
    if strings.Contains(lower, "not in") || strings.Contains(lower, "not a member") ||
        strings.Contains(lower, "cannot kick") {
        return nil
    }
}

// 修改后：精确匹配 Synapse 1.127 的 idempotent 消息（room_member.py:1022, 1039）
// 原消息是 "The target user is not in the room" — 不含 "sender not in room"
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    lower := strings.ToLower(resp.Error)
    // 仅匹配目标确实不在房间的 idempotent 情况
    // Synapse: "The target user is not in the room"（room_member.py:1022, 1039）
    // 不匹配 "@x:y not in room !r:d."（event_auth.py:687，sender 不 joined）
    if strings.Contains(lower, "target user is not in") ||
       strings.Contains(lower, "not a member") {
        return nil
    }
}
```

**修复 2：`shouldForceLeaveAfterKickError`（`provisioner.go:1259-1266`）**

扩展匹配 Synapse 实际字符串，确保 fallback 到 ForceLeaveRoom。

```go
// 修改前
func shouldForceLeaveAfterKickError(err error) bool {
    if err == nil { return false }
    msg := strings.ToLower(err.Error())
    return strings.Contains(msg, "m_forbidden") &&
        (strings.Contains(msg, "not have enough power") || strings.Contains(msg, "power"))
}

// 修改后
func shouldForceLeaveAfterKickError(err error) bool {
    if err == nil { return false }
    msg := strings.ToLower(err.Error())
    if !strings.Contains(msg, "m_forbidden") { return false }
    // Tuwunel 风格
    if strings.Contains(msg, "not have enough power") || strings.Contains(msg, "power") {
        return true
    }
    // Synapse 1.127 风格
    // event_auth.py:717 — PL 不足或 PL ≤ target
    if strings.Contains(msg, "cannot kick user") || strings.Contains(msg, "cannot unban user") {
        return true
    }
    // event_auth.py:687 — sender 不 joined（K3 场景，admin 不在房间里）
    if strings.Contains(msg, "not in room") {
        return true
    }
    return false
}
```

**修复 3：测试数据**

`provisioner_team_test.go:896` 等处加 Synapse 风格 mock：

```go
// 现有（Tuwunel）
matrixClient.kickErr = errors.New("HTTP 403 M_FORBIDDEN: sender does not have enough power to kick target user")

// 新增（Synapse 1.127 PL 不足）
synapsePLErr := errors.New("kick @x:y from !r:d: HTTP 403 M_FORBIDDEN You cannot kick user @x:y.: ...")
// 新增（Synapse 1.127 sender 不 joined）
synapseNotInRoomErr := errors.New("kick @x:y from !r:d: HTTP 403 M_FORBIDDEN @admin:d not in room !r:d.: ...")
```

### 1.5 是否需要 OperatorResolver fallback？

**实际不需要**。理由：
- admin 在所有 controller-managed 房间 PL=100 ≥ kick_level(50) → K4/K5 不触发
- admin 默认 joined（所有 CreateRoom 都 invite admin）→ K3 不触发
- 唯一触发 K3 的场景：`provisioner.go:859` 显式 leave admin 出 team 房间后对该 team 房间做 kick——但此时业务用 `teamAdminActorToken`（`provisioner.go:1172`），不走 admin 路径

保险起见修复 `shouldForceLeaveAfterKickError` 后，万一触发 K3 也有 ForceLeaveRoom 兜底。

---

## §2 InviteToRoom

### 2.1 AgentTeams 调用路径

```
provisioner.go::ReconcileRoomMembership
   ├─ InviteToRoom(ctx, roomID, userID)             // admin token 路径
   │   └─ InviteToRoomWithToken(ctx, ..., adminToken)
   └─ InviteToRoomWithToken(ctx, ..., actorToken)    // actor token 路径

team_controller.go:838       — manager 邀请进 worker room
human_reconcile_rooms.go:51  — human 邀请进可访问 room
provisioner.go:1140-1142     — reconcile 内部 invite
```

底层 HTTP：`POST /_matrix/client/v3/rooms/{roomId}/invite` with body `{"user_id":"..."}`

### 2.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/event_auth.py:_is_membership_change_allowed` (line 660-705) — invite 完整授权路径

**关键源码**：

```python
# synapse/event_auth.py:680-690 — 非 join/knock 的前置检查
if Membership.JOIN != membership and Membership.KNOCK != membership:
    if (caller_invited or caller_knocked) and Membership.LEAVE == membership and target_user_id == event.user_id:
        return
    if not caller_in_room:  # ← I1 触发点
        raise UnstableSpecAuthError(403, "%s not in room %s." % (event.user_id, event.room_id), errcode=Codes.NOT_JOINED)

# synapse/event_auth.py:692-705 — invite 业务检查
if Membership.INVITE == membership:
    if target_banned:
        raise AuthError(403, "%s is banned from the room" % (target_user_id,))  # I4
    elif target_in_room:  # ← I3 触发点
        raise UnstableSpecAuthError(403, "%s is already in the room." % target_user_id, errcode=Codes.ALREADY_JOINED)
    else:
        if user_level < invite_level:  # ← I2 触发点
            raise UnstableSpecAuthError(403, "You don't have permission to invite users", errcode=Codes.INSUFFICIENT_POWER)
```

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 | 源码 |
|---|---|---|---|---|
| I1 sender 不 joined | 403 | `M_FORBIDDEN`+unstable:`NOT_JOINED` | `"@sender:domain not in room !room:domain."` | event_auth.py:687 |
| I2 sender joined，PL < invite_level | 403 | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You don't have permission to invite users"` | event_auth.py:703 |
| I3 目标已在房间 | 403 | `M_FORBIDDEN`+unstable:`ALREADY_JOINED` | `"@target:domain is already in the room."` | event_auth.py:697 |
| I4 目标被 ban | 403 | `M_FORBIDDEN` | `"@target:domain is banned from the room"` | event_auth.py:694 |
| I5 成功 invite | 200 | — | — | 通过 |

**关键发现**：
1. **invite 在 event_auth 层不区分 server_admin 与普通用户**——所有 sender 一视同仁，都必须 caller_in_room。`is_requester_admin` 旁路只发生在 `room_member.py:899`（block_non_admin_invites 检查），不影响 event_auth 层。
2. **I3 的幂等匹配**：AgentTeams 必须把 `"is already in the room"` 当成 nil 处理，否则 reconcile 会因为目标已 joined 而报错。
3. **invite_level 默认 50**（不是 0）——见 `synapse/handlers/room.py:1210` create_room 默认 power_levels。但 AgentTeams 在所有 controller-managed 房间把 admin 设为 PL=100 ≥ 50，所以仍然满足。

### 2.3 AgentTeams 现有代码核对（`client.go:922-951`）

```go
if statusCode == http.StatusOK || http.StatusCreated { return nil }
// Idempotent: user already in the room.
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    lower := strings.ToLower(resp.Error)
    if strings.Contains(lower, "already in") || strings.Contains(lower, "already a member") {
        return nil
    }
}
return fmt.Errorf(...)
```

**匹配对照**：

| 匹配条件 | Synapse 场景 | 结果 |
|---|---|---|
| `200/201` → nil | I5 | ✅ 正确 |
| `M_FORBIDDEN` + `"already in"` → nil | **I3** `"@target is already in the room."` | ✅ 正确（精确匹配，下文不含歧义） |
| `M_FORBIDDEN` + `"already a member"` → nil | Synapse 不返回此消息 | ⚠️ dead branch（兼容性保留） |
| 其他 → 错误 | I1/I2/I4 | ✅ 正确 |

**关键观察**：invite 的匹配比 kick **干净**——它只匹配 `"already in"`/`"already a member"`，不会误匹配 `"not in room"`（I1 sender 不在房）。所以 I1（sender 不 joined）会被正确地当错误返回。

### 2.4 结论：InviteToRoom 无 BUG

AgentTeams 现有的 invite 幂等匹配**在 Synapse 1.127 下工作正确**：
- I3（目标已 joined）→ 匹配 `"already in"` → 幂等 nil ✅
- I1（sender 不 joined）→ 不匹配任何幂等条件 → 返回错误 ✅，调用方可以走 fallback
- I2（PL 不足）→ 返回错误 ✅
- I4（目标被 ban）→ 返回错误 ✅

**无需修改**。

### 2.5 是否需要 OperatorResolver fallback？

**不需要**。
- admin 在所有 controller-managed 房间默认 joined → I1 不触发
- admin PL=100 ≥ invite_level(0) → I2 不触发
- 唯一 I1 触发场景：admin 已 leave team 房间后对该 team 房间做 invite——但此时业务用 `teamAdminActorToken`（`provisioner.go:1140`），不走 admin 路径

如果未来需要给 admin 退出 team 房间后还能 invite 的能力，加 OperatorResolver 才有意义。当前业务流程已用 actor token 覆盖此场景。

---

## §3 ForceLeaveRoom（admin API kick）

### 3.1 AgentTeams 调用路径

```
provisioner.go::ForceLeaveRoom (provisioner_human.go:190)
   └─ matrix.AdminCommand(ctx, "!admin users force-leave-room <userID> <roomID>")
       └─ SynapseClient.AdminCommand → synKick
           └─ POST /_synapse/admin/v1/rooms/{roomId}/kick  ← 端点
```

### 3.2 Synapse 1.127 源码核对

**🔴 重大发现**：**该端点不存在**。

我读完 `synapse/rest/admin/` 全部源码，**没有任何 kick servlet**：
- `synapse/rest/admin/rooms.py` 只有：`RoomRestV2Servlet`(DELETE room)、`DeleteRoomStatus*`、`ListRoomRestServlet`、`RoomMembersRestServlet`(GET members)、`RoomStateRestServlet`、`JoinRoomAliasServlet`(POST join)、`MakeRoomAdminRestServlet`、`ForwardExtremitiesRestServlet`、`RoomContextRestServlet`、`BlockRoomRestServlet`、`RoomMessagesRestServlet`、`TimestampToEventRestServlet`
- `synapse/rest/admin/users.py` 的 `UserMembershipRestServlet` 只支持 `GET /users/{id}/joined_rooms`（只读）
- 所有 admin_patterns 列表里没有任何 `/rooms/.*/(kick|leave|invite|ban|unban)` 路由

**实际行为**：AgentTeams 的 `synKick` 调用 `POST /_synapse/admin/v1/rooms/{roomId}/kick` → Synapse 返回 **HTTP 404**。

**这意味着 §1 修复 2 里的 ForceLeaveRoom 兜底在 Synapse 下完全不工作**——`shouldForceLeaveAfterKickError` 触发后调用 ForceLeaveRoom，但 ForceLeaveRoom 自己又失败（404）。

### 3.3 Synapse 1.127 真正可用的 admin membership 端点

经过完整核对，与房间成员相关的 admin API 只有 3 个：

| 端点 | 用途 | 能否实现 kick? |
|---|---|---|
| `POST /_synapse/admin/v1/join/{room_identifier}` | admin 强制把一个本地用户 join 进房间（自动 invite + join）| ❌ 反向操作 |
| `POST /_synapse/admin/v1/rooms/{room_id}/make_room_admin` | admin 夺权（把自己加成 PL=100）| ❌ 夺权不踢人 |
| `DELETE /_synapse/admin/v2/rooms/{room_id}` | shutdown + purge 整个房间（所有用户都被踢）| ⚠️ 杀鸡用牛刀 |

**结论**：Synapse 1.127 的 admin API **不能单独踢一个人**。要踢人只能走 CS API（要求操作者 in-room + PL ≥ kick_level）。

### 3.4 修复方案

**修改 `synapse_client.go::synKick`（line 92-95）**：

让 `ForceLeaveRoom` 在 Synapse 下走一条**真正存在**的路径。两个选项：

**选项 A（推荐）：直接报错，让上层用 CS kick**

承认 Synapse 没有 admin kick 能力，让 `synKick` 返回明确错误，调用方 `ForceLeaveRoom` 应该返回错误，业务层（`provisioner.go:1178`）已经容忍 kick 失败（best-effort）。

```go
// 修改后
func (s *SynapseClient) synKick(ctx context.Context, roomID, userID string) error {
    return fmt.Errorf("synapse admin: POST /rooms/%s/kick endpoint does not exist in Synapse 1.127; "+
        "use CS API kick (requires operator in-room)", url.PathEscape(roomID))
}
```

**选项 B：用 make_room_admin 夺权 + CS kick**

```
1. POST /_synapse/admin/v1/rooms/{id}/make_room_admin {"user_id": "<admin>"}
   → admin 现在 PL=100 + 在房间里（admin API 强制 join）
2. POST /_matrix/client/v3/rooms/{id}/kick (with admin token)
   → 成功
```

副作用大（改了 power levels），不推荐用于正常 kick 路径。**仅在 kick 失败、且业务确实需要强踢时作为深度 fallback**。

**推荐选项 A**——简单、明确、不引入副作用。让 `shouldForceLeaveAfterKickError` 触发后调用 ForceLeaveRoom，ForceLeaveRoom 立即失败，业务层继续走（已是 best-effort）。

### 3.5 对 §1 修复 2 的影响

§1 修复 2（扩展 `shouldForceLeaveAfterKickError`）**仍然有意义**：
- 在 Tuwunel 下：触发 admin bot force-leave，能工作
- 在 Synapse 下：触发 ForceLeaveRoom，ForceLeaveRoom 失败，业务继续（best-effort）

**不要因 Synapse 没 admin kick 就跳过修复 2**——Tuwunel 仍需要它。

---

## §4 CreateRoom

### 4.1 AgentTeams 调用路径

```
provisioner.go:444 (worker room), 820 (team room), 888 (leader DM), 1437 (manager DM)
   └─ matrix.CreateRoom(ctx, req)
       └─ POST /_matrix/client/v3/createRoom
```

body 包含：name / topic / invite / preset=trusted_private_chat / is_direct / room_alias_name / power_level_content_override / initial_state

### 4.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/rest/client/room.py:RoomCreateRestServlet` (line 150-186)
- `synapse/handlers/room.py:create_room` (line 713-1022)
- `synapse/handlers/room.py:_send_events_for_new_room` (line 1024-1300)

**关键源码**：

```python
# synapse/handlers/room.py:878-888 — power_level_content_override 校验
power_level_content_override = config.get("power_level_content_override")
if (
    power_level_content_override
    and "users" in power_level_content_override
    and user_id not in power_level_content_override["users"]  # ← creator 必须在 users 里
):
    raise SynapseError(400, "Not a valid power_level_content_override: 'users' did not contain %s" % (user_id,))

# synapse/handlers/room.py:1154-1164 — creator 自动 join
member_event_id, _ = await self.room_member_handler.update_membership(
    creator, creator.user, room_id, "join", ...  # ← creator 必然 joined
)

# synapse/handlers/room.py:972-988 — invite 列表处理（在 creator join 之后）
for invitee in invite_list:
    await self.room_member_handler.update_membership_locked(
        requester, UserID.from_string(invitee), room_id, "invite", ...
    )

# synapse/handlers/room.py:1192-1212 — 默认 power_levels（无 override 时）
power_level_content = {
    "users": {creator_id: 100},
    "events": {EventTypes.Name: 50, EventTypes.PowerLevels: 100, ...},
    "events_default": 0,
    "state_default": 50,
    "ban": 50, "kick": 50, "redact": 50, "invite": 50,  # ← 默认 invite=50
    ...
}
```

**实际行为**：

| 场景 | HTTP | errcode | 源码 |
|---|---|---|---|
| C1 创建成功 | 200 | `{"room_id": "..."}` | room.py:1022 |
| C2 room_alias_name 已被占用 | 400 | `M_ROOM_IN_USE` `"Room alias already taken"` | room.py:846 |
| C3 power_level_content_override.users 不含 creator | 400 | `M_BAD_JSON` `"Not a valid power_level_content_override: 'users' did not contain @creator:domain"` | room.py:884 |
| C4 invite 列表里有不存在的本地用户 | 400 | `M_BAD_JSON` `"Invalid user_id: ..."` 或 403 | room.py:853 |

**关键发现**：
1. **creator 自动 joined**（line 1154-1164）——AgentTeams 传 `CreatorToken` 的 user 自动在房间里
2. **invite 列表在 creator join 之后处理**——invitee 收到的是真实 invite（不会自动 join）
3. **power_level_content_override.users 必须包含 creator**——AgentTeams 当前代码（`client.go:506-509`）只在 `req.PowerLevels` 非空时设置，但 **creator user_id 不一定在里面**——这是潜在 BUG！
4. **默认 invite_level=50**——不是 0

### 4.3 AgentTeams 现有代码核对

**`client.go:482-568` CreateRoom**：

```go
if len(req.PowerLevels) > 0 {
    body["power_level_content_override"] = map[string]interface{}{
        "users": req.PowerLevels,
    }
}
```

**对照业务调用**：

| 调用点 | CreatorToken | PowerLevels 是否含 creator? | 风险 |
|---|---|---|---|
| worker room (provisioner.go:435) | 空 → admin token | `powerLevels` 含 `managerMatrixID:100, adminMatrixID:100, authorityID:100, workerMatrixID:0` — admin 在里面 ✅ | 无 |
| team room (provisioner.go:820) | `req.TeamAdminActorToken` 或空→admin | team_room_power_levels 含 `managerMatrixID:100, leaderMatrixID:100, (teamAdminID 或 adminMatrixID):100` — **creator 是 team admin 或 admin，含在 PowerLevels 里 ✅** | 无 |
| leader DM (provisioner.go:888) | `req.TeamAdminActorToken` 或空→admin | `leaderDMPowerLevels` 含 manager+leader+(teamAdmin 或 admin) — creator 含 ✅ | 无 |
| manager DM (provisioner.go:1437) | 空 → admin token | `powerLevels` 含 `adminMatrixID:100, managerMatrixID:100` — admin 是 creator ✅ | 无 |

**结论**：**当前 4 个 CreateRoom 调用点都没问题**——creator user_id 恰好都在 PowerLevels 里。

但这是**巧合**——如果未来加新的 CreateRoom 调用点忘记把 creator 加进 PowerLevels，就会触发 C3 错误。**应该在 `client.go::CreateRoom` 里自动注入 creator**。

### 4.4 修复方案

**修复 4（防御性）：CreateRoom 自动注入 creator 到 power_level_content_override**

```go
// client.go::CreateRoom，在构造 body 之后、doJSON 之前
if len(req.PowerLevels) > 0 {
    // 获取 creator user_id
    var creatorUserID string
    if req.CreatorToken != "" {
        // 用 token 反查 user_id（通过 whoami）
        if uid, err := c.accessTokenUserID(ctx, req.CreatorToken); err == nil {
            creatorUserID = uid
        }
    } else {
        creatorUserID = c.UserID(c.config.AdminUser)  // admin 是 creator
    }
    
    users := make(map[string]int, len(req.PowerLevels))
    for k, v := range req.PowerLevels {
        users[k] = v
    }
    // 确保 creator 在 users 里（Synapse 1.127 强制要求，否则 400）
    if creatorUserID != "" {
        if _, ok := users[creatorUserID]; !ok {
            users[creatorUserID] = 100  // creator 默认 PL=100
        }
    }
    body["power_level_content_override"] = map[string]interface{}{"users": users}
}
```

**注意**：当前 4 个调用点不触发此 bug，所以**优先级低**。但建议加防御。

### 4.5 其他 CreateRoom 相关结论

- **preset=trusted_private_chat**：Synapse 处理 preset 的逻辑在 `_room_preset_config`，影响 power_levels 默认值。AgentTeams 同时传了 `power_level_content_override`，会覆盖 preset 的 power_levels。
- **is_direct**：AgentTeams 用于 leader DM 和 manager DM，Synapse 会把它传播到 invite event 的 content 里，触发客户端的 DM 标记。行为正常。
- **initial_state**：AgentTeams 用它传 `room.meta` 和 `m.room.encryption`。Synapse 在 `_send_events_for_new_room` 里按顺序处理，行为正常。

---

## §5 JoinRoom

### 5.1 AgentTeams 调用路径

```
provisioner.go:504 (worker join own room), 849 (team admin join team room), 
                 920 (leader join leader DM), 945/958 (team admin join leader DM)
   └─ matrix.JoinRoom(ctx, roomID, userToken)
       └─ POST /_matrix/client/v3/rooms/{roomId}/join
```

### 5.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/rest/client/room.py:JoinRoomAliasServlet._do` (line 496-545)
- `synapse/event_auth.py:_is_membership_change_allowed` JOIN 分支 (line 629-687)
- `synapse/handlers/room_member.py:update_membership_locked` JOIN 前置检查 (line 1006-1044)

**关键源码**：

```python
# synapse/event_auth.py:629-687 — JOIN 授权
elif Membership.JOIN == membership:
    if event.user_id != target_user_id:
        raise AuthError(403, "Cannot force another user to join.")  # 不能代别人 join
    elif target_banned:
        raise AuthError(403, "You are banned from this room")
    elif join_rule == JoinRules.PUBLIC:
        pass  # 公共房间，任何人都能 join
    elif join_rule == JoinRules.INVITE or JoinRules.KNOCK...:
        if not (caller_in_room or caller_invited):
            raise AuthError(403, "You are not invited to this room.")  # 必须 invited 或已 joined
```

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 | 源码 |
|---|---|---|---|---|
| J1 成功 join | 200 | `{"room_id": "..."}` | — | 通过 |
| J2 已 joined（幂等） | 200 | —（NOOP，spec 允许）| — | event_auth.py:633 注释 "already joined (it's a NOOP)" |
| J3 未被 invite | 403 | `M_FORBIDDEN` | `"You are not invited to this room."` | event_auth.py:683 |
| J4 被 ban | 403 | `M_FORBIDDEN` | `"You are banned from this room"` | event_auth.py:639 |
| J5 force join 别人 | 403 | `M_FORBIDDEN` | `"Cannot force another user to join."` | event_auth.py:637 |

### 5.3 AgentTeams 现有代码核对

**`client.go:733-758` JoinRoom**：

```go
statusCode, respBody, err := c.doJSON(ctx, http.MethodPost,
    fmt.Sprintf("/_matrix/client/v3/rooms/%s/join", encodedRoom),
    userToken, map[string]interface{}{}, &resp)
if statusCode == http.StatusOK || http.StatusCreated { return nil }
// Idempotent: user already in the room.
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    if strings.Contains(strings.ToLower(resp.Error), "already") {
        return nil
    }
}
```

| 匹配条件 | Synapse 场景 | 结果 |
|---|---|---|
| `200/201` → nil | J1/J2 | ✅ 正确（J2 已 joined 是 NOOP，直接返回 200） |
| `M_FORBIDDEN` + `"already"` → nil | 不匹配 Synapse JOIN 错误 | ⚠️ dead branch（保留兼容） |
| J3 `"You are not invited"` | 错误 | ✅ 正确返回 |
| J4 `"You are banned"` | 错误 | ✅ 正确返回 |

**结论**：JoinRoom **无 BUG**。

### 5.4 业务层观察

AgentTeams 调用 JoinRoom 都用**用户自己的 token**（不是 admin token）：
- `provisioner.go:504`：worker 用 `userCreds.AccessToken` join 自己的 worker room（之前已被 invite）
- `provisioner.go:849`：team admin 用 `req.TeamAdminActorToken` join team room（已被 invite）
- `provisioner.go:920`：leader 用 leader token join leader DM（已被 invite）

所有场景都先 invite 再 join，所以 J3（未 invite）不会触发。**安全**。

---

## §6 LeaveRoom

### 6.1 AgentTeams 调用路径

```
provisioner.go:281 (leaveAllRooms — deprovision), 
               859 (admin 离开 team room), 
provisioner_human.go:JoinRoomAs 等
   └─ matrix.LeaveRoom(ctx, roomID, userToken)
       └─ POST /_matrix/client/v3/rooms/{roomId}/leave
```

### 6.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/rest/client/room.py:RoomMembershipRestServlet` (line 1063-1145) — `/leave` 走这里
- `synapse/event_auth.py:_is_membership_change_allowed` LEAVE 分支 (line 688-704)

**关键源码**：

```python
# synapse/event_auth.py:680-690 — 非 join/knock 的前置检查（leave 也走这里）
if Membership.JOIN != membership and Membership.KNOCK != membership:
    if (caller_invited or caller_knocked) and Membership.LEAVE == membership and target_user_id == event.user_id:
        return  # ← 受邀/敲门用户可以自己 leave，不要求 caller_in_room
    if not caller_in_room:
        raise UnstableSpecAuthError(403, "%s not in room %s." % (event.user_id, event.room_id), errcode=Codes.NOT_JOINED)

# synapse/event_auth.py:688-704 — LEAVE PL 检查
elif Membership.LEAVE == membership:
    if target_banned and user_level < ban_level:
        raise UnstableSpecAuthError(403, "You cannot unban user %s." % target_user_id, errcode=INSUFFICIENT_POWER)
    elif target_user_id != event.user_id:
        # 这是 kick 路径，不是 self-leave
        kick_level = get_named_level(auth_events, "kick", 50)
        if user_level < kick_level or user_level <= target_level:
            raise UnstableSpecAuthError(403, "You cannot kick user %s." % target_user_id, errcode=INSUFFICIENT_POWER)
    # self-leave (target == sender) 没有任何 PL 检查
```

**RoomMembershipRestServlet 路由**（room.py:1071-1077）：

```python
PATTERNS = "/rooms/(?P<room_id>[^/]*)/(?P<membership_action>join|invite|leave|ban|unban|kick)"
```

`/leave` 走 `update_membership(requester, target=requester.user, action="leave")`——**target 永远是 sender 自己**。

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 | 源码 |
|---|---|---|---|---|
| L1 成功 leave | 200 | — | — | 通过 |
| L2 sender 不在房间（未 join/已 leave） | 403 | `M_FORBIDDEN`+unstable:`NOT_JOINED` | `"@sender:domain not in room !room:domain."` | event_auth.py:687 |
| L3 已 leave 的用户再次 leave（同 L2） | 403 | `M_FORBIDDEN` | 同上 | event_auth.py:687 |
| L4 受邀但未 join 的用户 leave（拒绝邀请）| 200 | — | — | event_auth.py:683-685 早期 return |

### 6.3 AgentTeams 现有代码核对

**`client.go:760-780` LeaveRoom**：

```go
func (c *TuwunelClient) LeaveRoom(ctx context.Context, roomID, userToken string) error {
    token := userToken
    if token == "" {
        token, _ = c.ensureAdminToken(ctx)  // ← 空 token 时 fallback 到 admin
    }
    statusCode, respBody, err := c.doJSON(ctx, http.MethodPost,
        fmt.Sprintf("/_matrix/client/v3/rooms/%s/leave", encodedRoom),
        token, map[string]interface{}{}, nil)
    if statusCode != http.StatusOK && statusCode != http.StatusCreated {
        return fmt.Errorf(...)  // ← 没有幂等匹配！
    }
    return nil
}
```

| 匹配条件 | Synapse 场景 | 结果 |
|---|---|---|
| `200/201` → nil | L1/L4 | ✅ 正确 |
| 任何非 2xx → 错误 | **L2/L3** | 🔴 **问题**：用户已 leave 后再 leave 会返回 403 错误，但业务层期望幂等 |

### 6.4 业务影响分析

**调用点**：
- `provisioner.go:281`（leaveAllRooms）：循环 `ListJoinedRooms` 后 leave——**只对已 join 的房间调 leave**，所以 L2/L3 不触发
- `provisioner.go:859`（admin 离开 team room）：先 check 是否 in room（`observedRoomMembershipWithToken`），再 leave——**已防guard**
- `provisioner.go:281` 实际有 logger.Error 但不返回错误——best-effort

**结论**：**实际业务无 BUG**——所有调用点都有前置检查避免重复 leave。但 `LeaveRoom` 本身**不幂等**，未来新增调用点可能踩坑。

### 6.5 修复方案（防御性，低优先级）

**修复 5：让 LeaveRoom 处理"已不在房间"为幂等 nil**

```go
// 修改后
func (c *TuwunelClient) LeaveRoom(ctx context.Context, roomID, userToken string) error {
    // ... 现有逻辑 ...
    if statusCode == http.StatusOK || statusCode == http.StatusCreated {
        return nil
    }
    // Idempotent: user not in room (already left / never joined).
    // Synapse: "@sender not in room !room" with errcode=M_FORBIDDEN (event_auth.py:687)
    var resp struct {
        ErrCode string `json:"errcode"`
        Error   string `json:"error"`
    }
    _ = json.Unmarshal(respBody, &resp)
    if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
        lower := strings.ToLower(resp.Error)
        if strings.Contains(lower, "not in room") {
            return nil
        }
    }
    return fmt.Errorf(...)
}
```

注意：当前 `LeaveRoom` 没解析 resp body，需要加。**优先级低**——业务无 BUG。

---

## §7 SetRoomName / SetRoomState

### 7.1 调用路径

```
SetRoomName: provisioner.go:838, 1304, 1310
SetRoomState: provisioner.go:483, 871, 936, 1452
```

底层 HTTP：`PUT /_matrix/client/v3/rooms/{roomId}/state/{eventType}/{stateKey}`

### 7.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/rest/client/room.py:RoomStateEventRestServlet.on_PUT` (line 286-330)
- `synapse/event_auth.py:check_state_dependent_auth_rules` (line ~450) → `_check_event_sender_in_room`
- `synapse/event_auth.py:_can_send_event` (line 740-790) — PL 检查

**关键源码**：

```python
# synapse/event_auth.py:check_state_dependent_auth_rules
# 5. If type is m.room.membership: 走 _is_membership_change_allowed
# 否则（包括 m.room.name 和自定义 state event）:
_check_event_sender_in_room(event, auth_dict)  # ← sender 必须 joined

# synapse/event_auth.py:722-735
def _check_event_sender_in_room(event, auth_events):
    key = (EventTypes.Member, event.user_id)
    member_event = auth_events.get(key)
    _check_joined_room(member_event, event.user_id, event.room_id)

def _check_joined_room(member, user_id, room_id):
    if not member or member.membership != Membership.JOIN:
        raise AuthError(403, "User %s not in room %s (%s)" % (user_id, room_id, repr(member)))

# synapse/event_auth.py:_can_send_event (line 760-770)
send_level = get_send_level(event.type, state_key, power_levels_event)  # m.room.name → state_default=50
user_level = get_user_power_level(event.user_id, auth_events)
if user_level < send_level:
    raise UnstableSpecAuthError(403, "You don't have permission to post that to the room. user_level (%d) < send_level (%d)" % (user_level, send_level), errcode=Codes.INSUFFICIENT_POWER)
```

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 | 源码 |
|---|---|---|---|---|
| S1 sender 不 joined | 403 | `M_FORBIDDEN` | `"User @sender:domain not in room !room:domain (None)"` | event_auth.py:731 |
| S2 sender joined，PL < state_default(50) | 403 | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You don't have permission to post that to the room. user_level (X) < send_level (50)"` | event_auth.py:768 |
| S3 sender joined，PL ≥ state_default | 200 | — | — | 通过 |

**注意**：
- `m.room.name` 的 send_level = `state_default`（默认 50）
- 自定义 state event（如 `room.meta`）同样 send_level = `state_default`
- AgentTeams 的 admin PL=100 ≥ 50，所以只要 admin joined，SetRoomName/SetRoomState 都成功

### 7.3 是否需要 OperatorResolver fallback？

**不需要**。
- worker room / manager DM：admin 一直在房，直接成功
- team 房间：业务用 `teamAdminActorToken`（`provisioner.go:871, 936`），team admin 是 joined + PL=100
- ArchiveTeamRooms（`provisioner.go:1304, 1310`）：用 `req.ActorToken`，team admin 在房

唯一边缘场景：`ArchiveTeamRooms` 在 `ActorToken=""` 时 fallback 到 admin token——但此时 admin 可能已 leave。这个场景下 SetRoomName 失败，业务容忍（ArchiveTeamRooms 是 best-effort）。

---

## §8 SendMessage / SendMessageAsAdmin

### 8.1 调用路径

```
SendMessageAsAdmin: provisioner.go:1652 (SendManagerWelcomeMessage)
```

底层 HTTP：`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`

### 8.2 Synapse 1.127 源码核对

**相关源码**：
- `synapse/rest/client/room.py:RoomSendEventRestServlet._do` (line 386-437)
- `synapse/event_auth.py:_check_event_sender_in_room → _can_send_event`

**关键发现**：
- m.room.message 不是 state event，send_level 默认是 `events_default=0`
- 但 sender 仍然必须 joined（`_check_event_sender_in_room` 在所有非 membership 事件前都执行）

**实际返回值**：

| 场景 | HTTP | errcode | error 原文 |
|---|---|---|---|
| M1 sender 不 joined | 403 | `M_FORBIDDEN` | `"User @sender not in room !room (None)"` |
| M2 sender joined，PL < events_default | 403 | `M_FORBIDDEN`+unstable | `"You don't have permission to post that to the room..."` |
| M3 sender joined | 200 | — | — |

### 8.3 业务场景分析

`SendMessageAsAdmin` 仅用于 `SendManagerWelcomeMessage`，发到 manager DM 房间。manager DM 创建时 admin 在 invite 列表（`provisioner.go:1440`）→ admin joined → 直接成功。

**不需要 OperatorResolver fallback**。

---

## §9-15 其他业务操作

🚧 按需补充

---

## 附录 A：Synapse 错误消息原文速查表（v1.127.0）

### A.1 Membership 相关（event_auth.py + room_member.py）

| 场景 | errcode（stable） | errcode（unstable） | 消息原文 | 源码 |
|---|---|---|---|---|
| Sender not joined | `M_FORBIDDEN` | `ORG.MATRIX.MSC3848.NOT_JOINED` | `"@sender:domain not in room !room:domain."` | event_auth.py:687 |
| Target already joined (invite 时) | `M_FORBIDDEN` | `ORG.MATRIX.MSC3848.ALREADY_JOINED` | `"@target:domain is already in the room."` | event_auth.py:697 |
| PL < invite_level | `M_FORBIDDEN` | `ORG.MATRIX.MSC3848.INSUFFICIENT_POWER` | `"You don't have permission to invite users"` | event_auth.py:703 |
| PL < kick_level or PL ≤ target_level | `M_FORBIDDEN` | `INSUFFICIENT_POWER` | `"You cannot kick user @target:domain."` | event_auth.py:717 |
| PL < ban_level (unban) | `M_FORBIDDEN` | `INSUFFICIENT_POWER` | `"You cannot unban user @target:domain."` | event_auth.py:711 |
| PL < ban_level (ban) | `M_FORBIDDEN` | `INSUFFICIENT_POWER` | `"You don't have permission to ban"` | event_auth.py:727 |
| 目标不在房间（kick 前置检查） | `M_FORBIDDEN` | — | `"The target user is not in the room"` | room_member.py:1022, 1039 |
| 目标被 ban（invite 时） | `M_FORBIDDEN` | — | `"@target:domain is banned from the room"` | event_auth.py:694 |

### A.2 事件发送相关（event_auth.py:_can_send_event + _check_event_sender_in_room）

| 场景 | errcode | 消息原文 | 源码 |
|---|---|---|---|
| Sender not joined | `M_FORBIDDEN` | `"User @sender:domain not in room !room:domain (None)"` | event_auth.py:731 |
| PL < send_level | `M_FORBIDDEN`+unstable:`INSUFFICIENT_POWER` | `"You don't have permission to post that to the room. user_level (X) < send_level (Y)"` | event_auth.py:768 |

### A.3 errcode 定义（api/errors.py:93-95）

```python
ALREADY_JOINED = "ORG.MATRIX.MSC3848.ALREADY_JOINED"
NOT_JOINED = "ORG.MATRIX.MSC3848.NOT_JOINED"
INSUFFICIENT_POWER = "ORG.MATRIX.MSC3848.INSUFFICIENT_POWER"
```

**这些是 unstable 字段**，stable `errcode` 始终是 `M_FORBIDDEN`。AgentTeams 当前只读 `errcode`，所以看到的全是 `M_FORBIDDEN`。

---

## 附录 B：核对进度

| § | 业务操作 | 状态 | 发现 |
|---|---|---|---|
| 1 | KickFromRoom | ✅ 已核对 | 2 个 BUG（idempotent 误匹配 K3/K4，fallback 不触发 Synapse 字符串） |
| 2 | InviteToRoom | ✅ 已核对 | **无 BUG**——幂等匹配精确（只匹配 "already in"） |
| 3 | ForceLeaveRoom (admin API kick) | ✅ 已核对 | 🔴 **重大**：Synapse 1.127 没有此端点！synKick 调用必然 404 |
| 4 | CreateRoom | ✅ 已核对 | 无 BUG（当前 4 个调用点巧合正确），但建议加防御性修复（自动注入 creator） |
| 5 | JoinRoom | ✅ 已核对 | 无 BUG |
| 6 | LeaveRoom | ✅ 已核对 | 无业务 BUG（调用点有前置检查），但方法本身不幂等，建议防御性修复 |
| 7 | SetRoomName / SetRoomState | ✅ 已核对 | sender 必须 joined，admin 默认在房，预计无 BUG |
| 8 | SendMessage | ✅ 已核对 | 同上 |
| 9 | DeleteRoom (admin API) | 🚧 待核对 | |
| 10 | EnsureUser (admin API) | 🚧 待核对 | |
| 11 | Login (CS API) | 🚧 待核对 | |
| 12 | ResetPassword (admin API) | 🚧 待核对 | |
| 13 | ResolveRoomAlias / DeleteRoomAlias | 🚧 待核对 | |
| 14 | RegisterAppService | 🚧 待核对 | |
| 15 | ListRoomMembers / ListJoinedRooms | 🚧 待核对 | |

---

## 附录 C：对总设计文档（synapse-support.md）的修正

经源码核对后，对原设计的修正：

### C.1 OperatorResolver fallback 链大幅缩减

**原计划 6 个方法需要 fallback**：InviteToRoom / KickFromRoom / SetRoomName / SetRoomState / SendMessage / SendMessageAsAdmin

**修正后**：
- **都不需要 OperatorResolver fallback**——admin 在所有 controller-managed 房间默认 joined + PL=100，所有操作直接成功
- 唯一需要的是：修复 `shouldForceLeaveAfterKickError` 让 ForceLeaveRoom 兜底能在 Synapse 下触发（§1 修复 2）

### C.2 ForceLeaveRoom（admin kick）在 Synapse 下完全失效

**新发现（§3）**：Synapse 1.127 admin API **没有** `POST /_synapse/admin/v1/rooms/{roomId}/kick` 端点。AgentTeams 的 `synKick` 调用必然 404。

**这意味着**：
- §1 修复 2 让 `shouldForceLeaveAfterKickError` 触发后调 ForceLeaveRoom，ForceLeaveRoom 在 Synapse 下又失败
- 业务层（`provisioner.go:1178`）已是 best-effort，所以不会卡死，但**踢人能力确实丧失了**（当 admin 不在房间或 PL 不足时）

**真正的修复方向**：让 admin 始终在 controller-managed 房间里（PL=100，已自动满足）——这样 CS API kick 就能直接工作，根本不需要 admin API kick。

### C.3 真正的工作量（修正后）

1. **§1 修复 1+2**：精确化 kick 的 idempotent 匹配 + 扩展 fallback 字符串（让 Tuwunel 下的 ForceLeaveRoom 继续工作）
2. **§3 修复**：`synKick` 改为明确报错（承认 Synapse 没 admin kick），避免静默 404
3. **§4 修复 4（防御性，低优先级）**：CreateRoom 自动注入 creator 到 PowerLevels
4. **§6 修复 5（防御性，低优先级）**：LeaveRoom 幂等化
5. **声明式 AppService**（独立工作流，与 CS API 修复无关）
6. **可选**：移除原方案里 OperatorResolver 相关代码（如果之前已写）

OperatorResolver 方案**整体放弃**——基于源码核对，它解决的是不存在的问题。

### C.4 §2 修正

之前写"invite_level 默认 0"是错的。`synapse/handlers/room.py:1210` 显示默认 `invite=50`。但 AgentTeams 在所有 controller-managed 房间把 admin 设为 PL=100 ≥ 50，所以仍然满足，无业务影响。
