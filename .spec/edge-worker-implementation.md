# Edge Worker 实现参考

## 分支信息

- **分支**: `feat/edge-handler-and-team-fixes`
- **核心提交**: `e29a75b` — feat(controller): add edge worker handler and team M_FORBIDDEN fix
- **变更范围**: 9 files, +620 / -45 lines

---

## 代码架构

```
                          ┌──────────────────────────────┐
                          │         外部 Pod               │
                          │   (edge worker, 非 HiClaw 管理) │
                          └──────┬──────────────┬────────┘
                  POST /edge-workers  │              │ POST /members/{name}/ready
                          ┌───────────▼──────────────▼────────────┐
                          │          Controller REST API          │
                          │                                      │
                          │  edge_handler.go     lifecycle_handler│
                          │  Register()          TeamMemberReady()│
                          │  RemoveEdgeWorker()                   │
                          └───────┬──────────────┬───────────────┘
                                  │              │
                    ┌─────────────▼──┐   ┌──────▼──────────────┐
                    │ Provisioner    │   │ Team CR Status       │
                    │ Deployer       │   │ .members[].Ready     │
                    │ (Matrix/GW/OSS)│   │ .members[].LastReadyAt│
                    └───────┬────────┘   └──────┬──────────────┘
                            │                   │
                            ▼                   ▼
              ┌─────────────────────────────────────────┐
              │          Team Reconciler                 │
              │  summarizeBackendReadiness()             │
              │  ├─ managed:   backend.Status()          │
              │  └─ edge:      isTeamMemberHeartbeatFresh│
              │  Phase 计算                               │
              │  Step 3b: 过期清理                        │
              └─────────────────────────────────────────┘
```

---

## 核心数据结构

### TeamWorkerSpec.ContainerManaged（`types.go`）

```go
// TeamWorkerSpec 新增字段
ContainerManaged *bool `json:"containerManaged,omitempty"`

// 辅助方法：默认 true，保持向后兼容
func (s TeamWorkerSpec) DesiredContainerMan() bool {
    if s.ContainerManaged != nil {
        return *s.ContainerManaged
    }
    return true
}
```

**设计要点**:
- 使用 `*bool` 而非 `bool`，nil → 默认为 true，现有 Team CR 零侵入
- `DesiredContainerMan()` 方法封装默认值逻辑，全项目统一使用该方法判断

### TeamMemberStatus.LastReadyAt（`types.go`）

```go
// TeamMemberStatus 新增字段
LastReadyAt string `json:"lastReadyAt,omitempty"`
```

**设计要点**:
- `omitempty` — 现有成员反序列化时为空字符串，不触发 TTL 逻辑
- RFC3339 格式，由 lifecycle_handler 写 UTC 时间
- 不单独定义 `EdgeWorkerStatus`（跳过了之前的设计），直接复用 `TeamMemberStatus`

---

## 三层 ready 判断体系

```
Layer 1: CR Status 写入
  └─ POST /api/v1/teams/{team}/members/{name}/ready
     → lifecycle_handler.TeamMemberReady()
     → 直接写 Team.Status via k8s.Status().Update()

Layer 2: Reconciler 读取
  └─ summarizeBackendReadiness()
     ├─ containerManaged=true → backend.Status() (pod 状态)
     └─ containerManaged=false → isTeamMemberHeartbeatFresh(LastReadyAt)

Layer 3: Reconciler Phase 计算
  └─ hasEdgeWorker?
     ├─ false → readyWorkers == TotalWorkers → Active
     └─ true  → phaseReadyWorkers == managedTotal → Active
               (edge worker NotReady 不影响 Team Phase)
```

### 心跳函数

```go
// team_controller.go 包级变量（init-time 从环境变量读取一次）
var edgeWorkerReadyTTL  = parseOr("HICLAW_EDGE_READY_TTL", 120*time.Second)
var edgeWorkerCleanupTTL = parseOr("HICLAW_EDGE_CLEANUP_TTL", 10*time.Minute)

func isTeamMemberHeartbeatFresh(lastReadyAt string) bool {
    // 空字符串或无法 parse → false
    // time.Since(ts) < edgeWorkerReadyTTL → true
}

func isTeamMemberHeartbeatExpired(lastReadyAt string) bool {
    // 空字符串 → true (立即清理)
    // time.Since(ts) > edgeWorkerCleanupTTL → true
}
```

**注意**: `isTeamMemberHeartbeatExpired` 对空 `LastReadyAt` 返回 `true`，这意味着从未心跳过的 edge worker 会被 step 3b 立即清理。这要求 edge worker 必须在注册后尽快开始心跳。

---

## Reconciler 变更详解

### summarizeBackendReadiness（team_controller.go ~line 530）

新增 edge worker 分支，在遍历 `members` 时先检查 `DesiredContainerMan()`:

```go
for _, m := range members {
    if !m.Spec.DesiredContainerMan() {
        // edge 分支：读 LastReadyAt，不走 backend.Status()
        ms := t.Status.MemberByName(m.Name)
        ready := ms != nil && isTeamMemberHeartbeatFresh(ms.LastReadyAt)
        if ms != nil {
            ms.Ready = ready
        }
        // 累积 leaderReady / readyWorkers
        continue
    }
    // 原有 managed 分支保持不变
    result, err := wb.Status(ctx, m.Name)
    ...
}
```

**关键**: edge 分支中的 `ms.Ready = ready` 是就地写入的，后续 Phase 计算和 status update 会一起持久化。

### Step 3b: 过期清理（team_controller.go ~line 273）

在 `pruneMembers` 之后、`step 3.5`（leader coordination）之前执行:

```go
if edgeWorkerCleanupTTL > 0 {
    for i := len(t.Spec.Workers) - 1; i >= 0; i-- {
        w := t.Spec.Workers[i]
        if w.DesiredContainerMan() { continue }
        ms := t.Status.MemberByName(w.Name)
        if ms == nil || !ms.Observed { continue }
        if !isTeamMemberHeartbeatExpired(ms.LastReadyAt) { continue }

        // 调用 ReconcileMemberDelete 做完整清理
        ReconcileMemberDelete(ctx, deps, expiredCtx)
        r.removeLegacyMember(ctx, runtimeName)
        t.Spec.Workers = append(t.Spec.Workers[:i], t.Spec.Workers[i+1:]...)
    }
}
```

**设计决策**:
- 放在 step 3.5 之前 — 清理完再重建 leader context，避免构造包含已过期 worker 的 team snapshot
- 倒序遍历 — 安全地从 slice 中删除元素
- `ms.Observed` guard — 仅清理已进入 status 的 member
- 容错: `ReconcileMemberDelete` 失败仅 log，不中断 reconcile
- `HICLAW_EDGE_CLEANUP_TTL=0` 完全禁用自动清理

### Phase 计算分离

```go
hasEdgeWorker := false
for _, w := range t.Spec.Workers {
    if !w.DesiredContainerMan() { hasEdgeWorker = true; break }
}

phaseReadyWorkers := readyWorkers
if hasEdgeWorker {
    // edge worker 即使 Ready 也不计入 phaseReadyWorkers
    for _, m := range desiredMembers {
        if m.Role == RoleTeamWorker && !m.Spec.DesiredContainerMan() {
            ms := t.Status.MemberByName(m.Name)
            if ms != nil && ms.Ready {
                phaseReadyWorkers--
            }
        }
    }
}

switch {
case len(perMemberErrors) > 0:
    t.Status.Phase = "Degraded"
case hasEdgeWorker:
    managedTotal := t.Status.TotalWorkers
    for _, w := range t.Spec.Workers {
        if !w.DesiredContainerMan() { managedTotal-- }
    }
    if leaderReady && phaseReadyWorkers == managedTotal {
        t.Status.Phase = "Active"
    } else {
        t.Status.Phase = "Pending"
    }
default:
    // 零 edge worker 时走原逻辑，字符级兼容
    if leaderReady && readyWorkers == t.Status.TotalWorkers {
        t.Status.Phase = "Active"
    } else {
        t.Status.Phase = "Pending"
    }
}
```

**设计理由**: 将 edge worker 从 Phase 计算中排除，避免未心跳的 edge worker 把整个 Team 拖入 Pending/Degraded。`hasEdgeWorker = false` 时走完全原 switch 逻辑，零行为变化。

---

## API 层

### EdgeWorkerHandler（新增 `edge_handler.go`，276 行）

**依赖注入**（`ServerDeps` 新增 `Provisioner` / `Deployer`）:

```go
type ServerDeps struct {
    // ... 原有字段 ...
    Provisioner service.WorkerProvisioner  // nil 时 Register 返回 501
    Deployer    service.WorkerDeployer     // nil 时 Register 返回 501
}
```

**Register（POST /edge-workers）**:
1. GET Team CR → 检查重名 → 追加 `{ContainerManaged: &false}` 到 spec → Update
2. `Provisioner.ProvisionWorker()` — 同步创建 Matrix 账号 + Gateway consumer + room
3. `Deployer.DeployWorkerConfig()` — 生成 openclaw.json，push 到 MinIO
4. channelPolicy 手动构建: `GroupAllowExtra = [leader, admin, humanMembers]`
5. 返回 Matrix token / password / gateway key / roomID 等凭证

**容错**: step 2 失败时 team spec 已写入，reconciler 后续会重试 provision（但当前 spec 中 provision 是在 handler 中同步做的，reconciler 不会自动重试 provision — 需要关注）。

**RemoveEdgeWorker（DELETE /edge-workers/{member}）**:
1. 验证是 edge worker（`DesiredContainerMan() == false`），防止误删 managed worker
2. LeaveAllWorkerRooms → DeleteWorkerRoom → DeprovisionWorker → CleanupOSSData → DeleteServiceAccount → DeleteWorkerRoomAlias
3. 从 spec.workers 移除 → Update

### LifecycleHandler.TeamMemberReady（lifecycle_handler.go 新增）

```go
func (h *LifecycleHandler) TeamMemberReady(w http.ResponseWriter, r *http.Request) {
    // 1. GET Team CR
    // 2. team.Status.MemberByName(memberName)
    // 3. ms.Ready = true; ms.LastReadyAt = time.Now().UTC().Format(time.RFC3339)
    // 4. h.k8s.Status().Update(ctx, &team)  ← 写 status subresource
    // 5. 204 No Content
}
```

**关键**: 使用 `Status().Update()` 而非 `Update()` — 只写 status，不触发 spec 变更，不产生额外的 reconcile 事件风暴。

### 路由注册（http.go）

```go
// Edge workers
mux.Handle("POST /api/v1/teams/{team}/edge-workers", authz(ActionCreate, "worker")(ewh.Register))
mux.Handle("DELETE /api/v1/teams/{team}/edge-workers/{member}", authz(ActionDelete, "worker")(ewh.RemoveEdgeWorker))

// Team member heartbeat
mux.Handle("POST /api/v1/teams/{team}/members/{member}/ready", authz(ActionReady, "worker")(lh.TeamMemberReady))
```

---

## 数据流总结

```
外部 Pod 注册:
  POST /edge-workers → edge_handler.Register()
    → k8s.Update(&team) [spec.workers += edge-w]
    → Provisioner.ProvisionWorker() [Matrix + GW + Room]
    → Deployer.DeployWorkerConfig() [MinIO openclaw.json]
    → 返回凭证

外部 Pod 心跳:
  POST /members/{name}/ready → lifecycle_handler.TeamMemberReady()
    → k8s.Status().Update(&team) [LastReadyAt = now]

Reconciler (每 N 分钟):
  summarizeBackendReadiness()
    ├─ managed: backend.Status()
    └─ edge: LastReadyAt < 120s → Ready
  Phase = managed workers 全 Ready → Active
  Step 3b: LastReadyAt > 10min → ReconcileMemberDelete + 移除

外部 Pod / Manager 主动移除:
  DELETE /edge-workers/{name} → edge_handler.RemoveEdgeWorker()
    → LeaveAllRooms → DeleteRoom → Deprovision → CleanupOSS
    → k8s.Update(&team) [spec.workers -= edge-w]
```

---

## 测试覆盖

`team_controller_test.go` 新增 2 个测试:

### TestTeamWorkerSpecToWorkerSpec_EdgeWorkerContainerManaged
- managed worker 的 `ContainerManaged` 为 nil（默认 managed）
- edge worker 的 `ContainerManaged` 为 `&false`（透传）

### TestBuildDesiredMembers_EdgeWorkerIncluded
- edge worker 出现在 desired members 中
- leader 的 `GroupAllowExtra` 包含 edge worker name（双向提及）
- edge worker 的 `GroupAllowExtra` 包含 leader name

---

## 配置项

| 环境变量 | 默认值 | 作用 | 关闭方式 |
|----------|--------|------|----------|
| `HICLAW_EDGE_READY_TTL` | `120s` | 心跳超时，超过后标记 NotReady | 调大 |
| `HICLAW_EDGE_CLEANUP_TTL` | `10m` | 过期自动移除 | 设为 `0` |

---

## 边界情况 & 注意事项

1. **注册后 provision 失败** — spec 已写入但 provision 失败，reconciler 目前不会自动重试（provider 调用在 handler 中同步完成）。需确认后续 reconcile 是否会补 provision。
2. **从未心跳的 edge worker** — `isTeamMemberHeartbeatExpired("")` 返回 `true`，会被 step 3b 立即清理。edge worker 必须在注册后立即开始心跳。
3. **`ResourceHandler.CreateTeam/UpdateTeam` 透传 `ContainerManaged`** — 通过 API 创建/更新 Team 时，`containerManaged` 字段会被保留到 spec 中（`resource_handler.go`）。
4. **Leader 的 `GroupAllowExtra` 自动包含 edge worker** — 由 `leaderWorkerSpec()` 遍历 `team.Spec.Workers` 实现，无需额外代码。
5. **Phase 字符级兼容** — 无 edge worker 时，Phase 计算走完全相同的 switch 分支，golang 编译结果应与变更前一致。
