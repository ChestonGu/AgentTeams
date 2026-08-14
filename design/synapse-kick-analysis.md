# KickFromRoom 在 Synapse 1.127 下的行为分析

> **结论**：AgentTeams 现有 `KickFromRoomWithToken` **部分正确，但 `shouldForceLeaveAfterKickError` 有真实 bug**。
> 此文件作为 `design/synapse-support.md` 的证据附件。

> **历史标注（2026-08）**：本文档分析的 bug 已在 openspec proposal
> [`synapse-support`](../openspec/changes/synapse-support/) Phase 1 中修复（精确匹配
> `"target user is not in"` 幂等；`shouldForceLeaveAfterKickError` 识别 Tuwunel 与
> Synapse 1.127 两套错误字符串）。本文档作为**历史依据**保留。

---

## 1. Synapse 1.127 实际行为（源码逐条核对）

### kick 路径
`POST /_matrix/client/v3/rooms/{roomId}/kick` → `RoomKickRestServlet` → `update_membership(action="kick", target=user_id, requester=sender)` → `update_membership_locked` → `_local_membership_update` → `create_event` + `handle_new_client_event` → `event_auth.check`。

### 各失败模式的实际返回值

| # | 场景 | HTTP | errcode | error 消息原文 | 源码位置 |
|---|---|---|---|---|---|
| K1 | 目标不在房间（之前 leave/ban） | 403 | `M_FORBIDDEN` | `"The target user is not in the room"` | `room_member.py:1022` `if old_membership in ["ban","leave"] and action=="kick": raise AuthError(403, "The target user is not in the room")` |
| K2 | 目标不在房间（无成员事件） | 403 | `M_FORBIDDEN` | `"The target user is not in the room"` | `room_member.py:1039` `else: if action=="kick": raise AuthError(403, "The target user is not in the room")` |
| K3 | 目标在房间，sender PL < ban_level（默认 50） | 403 | `M_FORBIDDEN`（稳定码）+ `ORG.MATRIX.MSC3848.INSUFFICIENT_POWER`（unstable） | `"You cannot kick user @target:domain."` | `event_auth.py:784` `raise UnstableSpecAuthError(403, "You cannot kick user %s." % target_user_id, errcode=Codes.INSUFFICIENT_POWER)` |
| K4 | 目标在房间，sender PL ≥ ban_level | 200 | — | — | 通过 |
| K5 | 目标 = sender 自己 | 200 | — | —（走 self-leave 路径，event_auth 的 `not target_banned and target_user_id != user_id` 条件不满足 → 不报错） | 通过 |
| K6 | 目标被 ban，sender PL < ban_level | 403 | `M_FORBIDDEN`+`INSUFFICIENT_POWER` | `"You cannot unban user @target:domain."` | `event_auth.py:772` |

### 关键发现

1. **K1/K2（目标不在房间）的 errcode 是 `M_FORBIDDEN`**，不是 `M_USER_NOT_FOUND`，也不是 404。AgentTeams 当前的 `if statusCode == http.StatusNotFound { return nil }` 在 Synapse 下永远命中不到——但 `M_FORBIDDEN` 分支的字符串匹配 `"not in"` 能命中 `"The target user is not in the room"`，所以最终返回 nil（幂等成功）**正确**。

2. **K3（sender PL 不足）的 errcode 稳定字段仍是 `M_FORBIDDEN`**（`UnstableSpecAuthError` 保留 stable code 作为 `errcode`，新码 `ORG.MATRIX.MSC3848.INSUFFICIENT_POWER` 只放在 `org.matrix.msc3848.unstable.errcode` 字段）。但消息是 **`"You cannot kick user @x:y."`**，**不含 "power" 字眼**。

3. **`UnstableSpecAuthError`**（`synapse/api/errors.py:416`）= AuthError 的子类，HTTP 403 + 保留 stable errcode + 在 response body 加 unstable 字段。AgentTeams 只读 `errcode` 字段，所以看到的是 `M_FORBIDDEN`。

---

## 2. AgentTeams 现有代码对照

### `KickFromRoomWithToken`（`client.go:961-999`）

```go
if statusCode == http.StatusOK || statusCode == http.StatusCreated { return nil }
// Idempotent: user not in the room (or already left).
if statusCode == http.StatusNotFound { return nil }  // ← Synapse 下永不命中
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    lower := strings.ToLower(resp.Error)
    if strings.Contains(lower, "not in") || strings.Contains(lower, "not a member") ||
        strings.Contains(lower, "cannot kick") {
        return nil
    }
}
```

| 场景 | Synapse 实际 | 匹配结果 |
|---|---|---|
| K1/K2 目标不在房间 | errcode=`M_FORBIDDEN`, msg=`"The target user is not in the room"` | ✅ `strings.Contains(lower, "not in")` 命中 → nil（幂等成功） |
| K3 sender PL 不足 | errcode=`M_FORBIDDEN`, msg=`"You cannot kick user @x:y."` | ⚠️ **匹配上了 `"cannot kick"`** → 返回 nil → **这是 BUG** |

**`"cannot kick"` 这个匹配项错了**——它原本是为了匹配某些 HS 的"sender 不能 kick"错误（写测试时假设的），但 Synapse 把它用于 "sender PL 不足" 的 K3 场景。结果 PL 不足被当作幂等成功**直接吞掉了**。

### `shouldForceLeaveAfterKickError`（`provisioner.go:1259-1266`）

```go
func shouldForceLeaveAfterKickError(err error) bool {
    if err == nil { return false }
    msg := strings.ToLower(err.Error())
    return strings.Contains(msg, "m_forbidden") &&
        (strings.Contains(msg, "not have enough power") || strings.Contains(msg, "power"))
}
```

| 场景 | Synapse 实际消息 | 匹配结果 |
|---|---|---|
| K3 sender PL 不足 | `"HTTP 403 M_FORBIDDEN You cannot kick user @x:y."` | ❌ **不命中** —— 消息里既没有 "not have enough power" 也没有独立的 "power" 字眼（除了错误码里的 "FORBIDDEN" 本身，但 `m_forbidden` 匹配的是 errcode） |

**这是真实 bug**：现有测试 `provisioner_team_test.go:896` 用的 mock 消息是 `"HTTP 403 M_FORBIDDEN: sender does not have enough power to kick target user"`（Tuwunel 风格），但 Synapse 实际返回的是 `"You cannot kick user @x:y."`，匹配器在 Synapse 下**永远不触发 ForceLeaveRoom fallback**。

### 顺带发现：测试数据与 Synapse 实际不符

`provisioner_team_test.go:896`：
```go
matrixClient.kickErr = errors.New("HTTP 403 M_FORBIDDEN: sender does not have enough power to kick target user")
```

这个 mock 假设的错误格式是 **Tuwunel 风格的**（含 "not have enough power"）。Synapse 1.127 实际消息是 **`"You cannot kick user @x:y."`**——完全不同的字符串。所以现有测试无法覆盖 Synapse 行为。

---

## 3. 真实 bug 清单（按严重度）

### BUG-1（严重）：`shouldForceLeaveAfterKickError` 在 Synapse 下永远 false

**影响**：当 worker/leader PL 高于 admin（极少，但可能发生，比如被人手动改了 power_levels）时，普通 kick 失败后**不会 fallback 到 ForceLeaveRoom**，被踢的人永远踢不掉。

**修复**：扩展匹配字符串：
```go
func shouldForceLeaveAfterKickError(err error) bool {
    if err == nil { return false }
    msg := strings.ToLower(err.Error())
    if !strings.Contains(msg, "m_forbidden") { return false }
    // Tuwunel 风格
    if strings.Contains(msg, "not have enough power") || strings.Contains(msg, "power") {
        return true
    }
    // Synapse 1.127 风格（event_auth.py:784）
    if strings.Contains(msg, "cannot kick user") { return true }
    // Synapse 1.127 unban PL 不足（event_auth.py:772）
    if strings.Contains(msg, "cannot unban user") { return true }
    return false
}
```

### BUG-2（中等）：`KickFromRoomWithToken` 把 K3（PL 不足）当幂等成功

**影响**：admin 在某房间被降级到 < 50 PL 后，kick 任何人都被静默吞掉，业务层以为成功。

**修复**：从 `"cannot kick"` 匹配项里**移除** K3 场景。但这个字符串原本匹配的 K1/K2 场景已经被 `"not in"` 覆盖，所以 `"cannot kick"` 这个匹配项**整个删掉**更安全：

```go
if statusCode == http.StatusForbidden && resp.ErrCode == "M_FORBIDDEN" {
    lower := strings.ToLower(resp.Error)
    // 仅匹配目标确实不在房间的 idempotent 情况
    if strings.Contains(lower, "not in") || strings.Contains(lower, "not a member") {
        return nil
    }
    // 不再匹配 "cannot kick" —— Synapse 把它用于 PL 不足，不是幂等
}
```

### BUG-3（轻微）：404 分支在 Synapse 下永不命中

**影响**：dead branch，无实际影响。

**修复**：保留不动（兼容可能返回 404 的其他 HS）。

---

## 4. 关于"sender 必须在房间"的关键发现

**重新核对 event_auth.py 的 kick PL 检查**（line 768-784）：

```python
elif Membership.LEAVE == membership:
    if target_banned and user_level < ban_level:
        raise UnstableSpecAuthError(403, "You cannot unban user X.", errcode=INSUFFICIENT_POWER)
    elif (not target_banned and target_user_id != user_id):
        if user_level < ban_level:
            raise UnstableSpecAuthError(403, "You cannot kick user X.", errcode=INSUFFICIENT_POWER)
```

**这里只检查 `user_level`（PL），不检查 sender 是否 join**。也就是说：

> **Synapse 1.127 的 kick 不要求 sender 在房间里——只要求 sender 在 m.power_levels.users 里有 ≥ ban_level（默认 50）的 PL。**

由于 AgentTeams 在所有 CreateRoom 调用里都把 `adminMatrixID: 100` 写进 `power_level_content_override`（`provisioner.go:419, 805, 967, 1433`），admin 在所有 controller-managed 房间里都有 PL 100 ≥ 50 → **kick 路径在 Synapse 下不会因"admin 不在房间"失败**。

**这修正了 `design/synapse-support.md` 第 6 节授权矩阵里关于 RemoveMember 的判断**：原判断"admin 不在房间时需要 OperatorResolver fallback"对 kick **不成立**——admin PL 100 足够，不在房间里也能 kick。

### 但 invite 不一样

invite 走的是 `Membership.INVITE` 分支（`event_auth.py:428`）：
```python
if user_level < invite_level:
    raise UnstableSpecAuthError(403, "You don't have permission to invite users", errcode=INSUFFICIENT_POWER)
```

但 invite 还有一个**前置检查**——sender 必须在房间里。这是 Matrix spec 规定的（"An invite event can only be sent by a user who is joined to the room"），Synapse 在更早的地方强制执行（`event_auth.py` 的 member 检查 + `room_member.py` 的 update_membership 流程）。

**结论**：
- `KickFromRoom` / `KickFromRoomWithToken` 在 Synapse 下**不需要 OperatorResolver fallback**——admin PL 100 够了
- `InviteToRoom` / `InviteToRoomWithToken` **需要** OperatorResolver fallback——sender 必须 joined
- `SetRoomName` / `SetRoomState` / `SendMessage` **需要** fallback——发送事件要求 sender joined

### 修订后的 OperatorResolver 适用方法清单

原方案文档第 9 节附录 A 说需要 fallback 的 6 个方法（InviteToRoom / KickFromRoom / SetRoomName / SetRoomState / SendMessage / SendMessageAsAdmin），**修正为 5 个**：去掉 `KickFromRoom`。

`KickFromRoom` 只需修复 BUG-1 和 BUG-2 即可——不需要 fallback 链。

---

## 5. 验证依据（源码引用）

| 文件 | 行 | 内容 |
|---|---|---|
| `synapse/handlers/room_member.py` | 1022 | `if old_membership in ["ban","leave"] and action=="kick": raise AuthError(403, "The target user is not in the room")` |
| `synapse/handlers/room_member.py` | 1039 | `else: if action=="kick": raise AuthError(403, "The target user is not in the room")` |
| `synapse/event_auth.py` | 768 | `elif Membership.LEAVE == membership:` |
| `synapse/event_auth.py` | 772 | `raise UnstableSpecAuthError(403, "You cannot unban user %s." % target_user_id, errcode=Codes.INSUFFICIENT_POWER)` |
| `synapse/event_auth.py` | 784 | `raise UnstableSpecAuthError(403, "You cannot kick user %s." % target_user_id, errcode=Codes.INSUFFICIENT_POWER)` |
| `synapse/event_auth.py` | 428 | `if user_level < invite_level: raise UnstableSpecAuthError(403, "You don't have permission to invite users", errcode=Codes.INSUFFICIENT_POWER)` |
| `synapse/api/errors.py` | 94 | `INSUFFICIENT_POWER = "ORG.MATRIX.MSC3848.INSUFFICIENT_POWER"` |
| `synapse/api/errors.py` | 416 | `class UnstableSpecAuthError(AuthError)` —— 保留 stable errcode `M_FORBIDDEN`，新码放 unstable 字段 |

所有引用来自 `element-hq/synapse` repo 的 `develop` 分支（v1.127.0 标签相同位置）。
