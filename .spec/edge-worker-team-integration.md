# Edge Worker 加入 Team 设计规格

## 概述

允许 HiClaw 体系外的 Pod（edge worker）作为 Team Worker 加入 Team，其容器生命周期不受 controller 管理。Edge worker 加入时自动完成双向 `groupAllowFrom` 提及关系，通过心跳机制检测存活性，长时间无心跳自动清理。

## 核心设计原则

1. **零侵入**：spec 中无 edge worker 的 Team 走完全原逻辑，行为零变化
2. **可回退**：所有新增行为通过环境变量可配置、可禁用
3. **CR 驱动**：状态存储在 CR status，可观测（kubectl）、可持久化
4. **复用现有代码**：双向 `groupAllowFrom` 由现有 `teamWorkerSpecToWorkerSpec`/`leaderWorkerSpec` 自动覆盖

---

## 业务流程

### 流程一：注册加入 Team

```
外部 Pod                              Controller
   │                                      │
   │ POST /api/v1/teams/alpha/            │
   │   edge-workers                       │
   │ {name:"edge-w1", model:"gpt-4"}     │
   │ ────────────────────────────────────►│
   │                                      │
   │                    ┌─ edge_handler.go: Register() ──────────────────────┐
   │                    │                                                  │
   │                    │ ① GET Team CR "alpha"                            │
   │                    │ ② 检查 edge-w1 不在 spec.workers 中（防重复）     │
   │                    │ ③ 追加到 spec.workers:                           │
   │                    │    {name:"edge-w1", containerManaged: false}     │
   │                    │ ④ k8s.Update(&team)                             │
   │                    │ ⑤ Provisioner.ProvisionWorker():                 │
   │                    │    - 创建 Matrix 账号 @edge-w1:domain            │
   │                    │    - 创建 Gateway consumer                       │
   │                    │    - 创建 worker 通信 room                       │
   │                    │ ⑥ Deployer.DeployWorkerConfig():                │
   │                    │    - 生成 openclaw.json                          │
   │                    │      groupAllowFrom = [leader, admin, coords]   │
   │                    │    - push 到 MinIO agents/edge-w1/              │
   │                    └──────────────────────────────────────────────────┘
   │                                      │
   │◄──────────────────────────────────── │
   │ {matrixToken, gatewayKey,            │
   │  matrixPassword, roomID, teamRoomID} │
```

**API**：

```
POST /api/v1/teams/{teamName}/edge-workers

Request:
{
  "name": "edge-w1",         // required, Team 内唯一标识
  "workerName": "edge-w1",   // optional, Matrix localpart, 默认等于 name
  "model": "gpt-4",          // required
  "runtime": "copaw",        // optional, 默认由 backend.ResolveRuntime 决定
  "skills": ["github"]       // optional
}

Response (201):
{
  "name": "edge-w1",
  "matrixUserID": "@edge-w1:matrix.hiclaw.io",
  "matrixToken": "syt_xxx...",
  "matrixPassword": "xxx",
  "gatewayKey": "xxx",
  "roomID": "!abc:matrix.hiclaw.io",
  "teamRoomID": "!def:matrix.hiclaw.io",
  "message": "registered as edge worker in team alpha"
}
```

### 流程二：双向 groupAllowFrom（零新增代码）

Edge worker 加入 `spec.workers` 后，下次 reconcile 自动生效：

```
leaderWorkerSpec(team):
  → 遍历 team.Spec.Workers 所有 worker name
  → leader.GroupAllowExtra += ["edge-w1", ...]
  → leader 的 openclaw.json: groupAllowFrom 包含 edge-w1  ✓

teamWorkerSpecToWorkerSpec(team, edge-w1):
  → policy.GroupAllowExtra += [leaderName]
  → policy.GroupAllowExtra += [adminMatrixID, coordinatorIDs]
  → if PeerMentions: += [其他 peer worker names]
  → edge-w1 的 openclaw.json: groupAllowFrom 包含 leader + human ✓
```

**由现有代码自动处理，零改动。**

### 流程三：心跳上报

```
外部 Pod                              Controller
   │                                      │
   │ (每 60s)                             │
   │ POST /api/v1/teams/alpha/            │
   │   members/edge-w1/ready              │
   │ ────────────────────────────────────►│
   │                                      │
   │                    ┌─ lifecycle_handler.go ────────────────────────────┐
   │                    │                                                 │
   │                    │ ① GET Team CR "alpha"                           │
   │                    │ ② MemberByName("edge-w1")                      │
   │                    │ ③ ms.Ready = true                              │
   │                    │    ms.LastReadyAt = "2026-07-29T10:00:00Z"     │
   │                    │ ④ k8s.Status().Update(&team)                   │
   │                    │    → 写入 CR status，kubectl 可见               │
   │                    └─────────────────────────────────────────────────┘
   │                                      │
   │◄──────────────────────────────────── │
   │ 204 No Content                       │
```

**API**：

```
POST /api/v1/teams/{teamName}/members/{memberName}/ready

无 request body。
204 No Content 表示成功。
```

**kubectl 可观测**：

```bash
kubectl get team alpha -o jsonpath='{.status.members[*].lastReadyAt}'
# 2026-07-29T10:00:00Z 2026-07-29T10:00:00Z ...
```

### 流程四：Reconcile Readiness 检测

```
TeamReconciler (每 5min)

summarizeBackendReadiness():
  对每个 member:
  ┌──────────────────────────────────────┐
  │ containerManaged = true (默认)       │
  │   → 查 backend (pod 状态)            │  ← 原有逻辑，零改动
  │                                      │
  │ containerManaged = false (edge)      │
  │   → 读 ms.LastReadyAt               │
  │   → isTeamMemberHeartbeatFresh()     │
  │     < HICLAW_EDGE_READY_TTL → Ready  │
  │     ≥ HICLAW_EDGE_READY_TTL → !Ready │
  └──────────────────────────────────────┘

Phase 计算:
  ┌──────────────────────────────────────┐
  │ spec 中无 edge worker:               │
  │   → 原逻辑: readyWorkers ==          │
  │     TotalWorkers                      │  ← 完全不变
  │                                      │
  │ spec 中有 edge worker:               │
  │   → 只看 managed worker 数量         │
  │   → edge worker NotReady             │
  │     不影响 Team Phase                │
  └──────────────────────────────────────┘
```

### 流程五：过期自动清理

```
TeamReconciler (每 5min)

Step 3b: 过期清理
  (HICLAW_EDGE_CLEANUP_TTL=0 可禁用)

  for 每个 spec.worker (倒序):
  ┌──────────────────────────────────────┐
  │ DesiredContainerMan() = true?        │
  │   → 跳过（不是 edge worker）          │
  │                                      │
  │ ms.LastReadyAt > HICLAW_EDGE_CLEANUP │
  │   → ReconcileMemberDelete():         │
  │     - LeaveAllWorkerRooms()          │
  │     - DeleteWorkerRoom()             │
  │     - DeprovisionWorker()            │  ← 完全清理
  │     - CleanupOSSData()               │
  │     - DeleteServiceAccount()         │
  │     - DeleteWorkerRoomAlias()        │
  │   → 从 spec.workers 移除             │
  │   → 下次 reconcile leader config     │
  │     自动不再包含该 worker             │
  └──────────────────────────────────────┘
```

### 流程六：主动移除

```
外部 Pod / Manager                     Controller
   │                                      │
   │ DELETE /api/v1/teams/alpha/          │
   │   edge-workers/edge-w1               │
   │ ────────────────────────────────────►│
   │                                      │
   │                    ┌─ edge_handler.go: RemoveEdgeWorker() ────────────┐
   │                    │                                                 │
   │                    │ ① GET Team CR, 验证是 edge worker              │
   │                    │ ② Provisioner.LeaveAllWorkerRooms()             │
   │                    │ ③ Provisioner.DeleteWorkerRoom()                │
   │                    │ ④ Provisioner.DeprovisionWorker()              │
   │                    │ ⑤ Deployer.CleanupOSSData()                    │
   │                    │ ⑥ 从 spec.workers 移除                         │
   │                    │ ⑦ k8s.Update(&team)                            │
   │                    │   → 下次 reconcile leader config 自动更新      │
   │                    └─────────────────────────────────────────────────┘
   │                                      │
   │◄──────────────────────────────────── │
   │ {message: "edge worker removed"}     │
```

**API**：

```
DELETE /api/v1/teams/{teamName}/edge-workers/{memberName}

Response (200):
{
  "name": "edge-w1",
  "message": "edge worker removed from team alpha"
}
```

---

## API 端点汇总

| 方法 | 路径 | 用途 | 来源 |
|------|------|------|------|
| `POST` | `/api/v1/teams/{team}/edge-workers` | 注册 edge worker | **新增** |
| `DELETE` | `/api/v1/teams/{team}/edge-workers/{member}` | 移除 edge worker | **新增** |
| `POST` | `/api/v1/teams/{team}/members/{member}/ready` | 心跳上报 | **新增** |
| `PUT` | `/api/v1/teams/{name}` | 更新 team（含 `containerManaged` 字段） | **修改**（新增字段透传） |
| `POST` | `/api/v1/teams` | 创建 team（含 `containerManaged` 字段） | **修改**（新增字段透传） |

---

## 可配置项

| 环境变量 | 默认值 | 说明 | 回退方式 |
|----------|--------|------|----------|
| `HICLAW_EDGE_READY_TTL` | `120s` | 心跳超时阈值，超过后标记 NotReady | 调大放宽要求 |
| `HICLAW_EDGE_CLEANUP_TTL` | `10m` | 过期清理阈值，超过后自动移除 | 设为 `0` **完全禁用**自动清理 |

---

## CRD 变更

### TeamWorkerSpec 新增字段

```go
// types.go - TeamWorkerSpec
ContainerManaged *bool `json:"containerManaged,omitempty"`
// 默认 nil → DesiredContainerMan() = true（现有行为不变）
// 设为 false → 标记为 edge worker，跳过容器管理
```

### TeamMemberStatus 新增字段

```go
// types.go - TeamMemberStatus
LastReadyAt string `json:"lastReadyAt,omitempty"`
// RFC3339 时间戳，由 POST .../members/{name}/ready 写入
// 空值或超过 TTL → NotReady
```

---

## 文件变更清单

### 新增文件

| 文件 | 模块 | 说明 |
|------|------|------|
| `hiclaw-controller/internal/server/edge_handler.go` | server | Edge worker 注册/移除端点 |

### 修改文件

| 文件 | 模块 | 变更 |
|------|------|------|
| `hiclaw-controller/api/v1beta1/types.go` | CRD | `TeamWorkerSpec.ContainerManaged` + `TeamMemberStatus.LastReadyAt` |
| `hiclaw-controller/internal/controller/team_controller.go` | controller | 透传字段、TTL 检测、过期清理、Phase 分离 |
| `hiclaw-controller/internal/controller/team_controller_test.go` | controller | 2 个新测试 |
| `hiclaw-controller/internal/server/types.go` | server | `TeamWorkerRequest.ContainerManaged` |
| `hiclaw-controller/internal/server/resource_handler.go` | server | CreateTeam/UpdateTeam 透传 `ContainerManaged` |
| `hiclaw-controller/internal/server/lifecycle_handler.go` | server | `TeamMemberReady` 端点（写 CR status） |
| `hiclaw-controller/internal/server/http.go` | server | 路由注册 + ServerDeps 加 Provisioner/Deployer |
| `hiclaw-controller/internal/app/app.go` | app | HTTPServer 注入 Provisioner/Deployer |

---

## 侵入性分析

| 改动点 | 守卫条件 | 无 edge worker 时行为 |
|--------|----------|----------------------|
| `summarizeBackendReadiness` edge 分支 | `!m.Spec.DesiredContainerMan()` | 不进入，走原 backend 查询 |
| Phase 计算分离 | `hasEdgeWorker` (遍历 spec) | `false`，走完全原 switch 逻辑 |
| Step 3b 过期清理 | `edgeWorkerCleanupTTL > 0` + `DesiredContainerMan()` | 循环体跳过所有 worker |
| `teamWorkerSpecToWorkerSpec` 透传 | `ContainerManaged` 字段为 `nil` | `nil` 透传无副作用 |
| `TeamMemberStatus.LastReadyAt` | `omitempty` | 现有数据反序列化为空，不触发 TTL 逻辑 |
| API 端点 | 新增路径 | 不影响现有路径 |
| `ServerDeps` 新字段 | 默认 `nil` | EdgeWorkerHandler 返回 501 |

---

## 使用示例

### 注册

```bash
curl -X POST http://controller:9090/api/v1/teams/alpha/edge-workers \
  -H 'Authorization: Bearer <token>' \
  -d '{"name":"edge-w1","model":"gpt-4","runtime":"copaw"}'
```

### 心跳（外部 pod 定时任务）

```bash
while true; do
  curl -X POST http://controller:9090/api/v1/teams/alpha/members/edge-w1/ready \
    -H 'Authorization: Bearer <token>'
  sleep 60
done
```

### 移除

```bash
curl -X DELETE http://controller:9090/api/v1/teams/alpha/edge-workers/edge-w1 \
  -H 'Authorization: Bearer <token>'
```

### 禁用自动清理

```bash
# Controller 环境变量
HICLAW_EDGE_CLEANUP_TTL=0
```
