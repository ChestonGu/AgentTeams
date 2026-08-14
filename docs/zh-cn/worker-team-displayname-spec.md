# Worker / Team `displayName` 功能规格（Spec）

> 状态：已实现并通过单元测试 · 关联代码分支：`main`（未提交）
> 适用范围：`agentteams-controller` 及其配套的 CRD、REST API、`agt` CLI。
> 本文档用于评审、复盘和在其他分支/版本中复现该功能，所有标识符以当前实现为准。

---

## 1. 背景与目标

AgentTeams 中 Worker 和 Team 在 Matrix 侧使用 `workerName` / `teamName` 作为业务/运行时身份（Matrix localpart、房间别名、存储路径）。当展示名与身份名不一致时（例如中文名、品牌名、易读名），用户缺少一个与身份解耦的“友好显示名”。

目标：
- 为 `Worker` 和 `Team` 分别增加**可选**的 `spec.displayName`。
- 该字段只影响**展示层**（Matrix 资料显示名 / 团队房间名 / CLI / API 输出），**不改变** Matrix 身份、房间别名、存储路径或 Pod。
- 通过 `status.displayNameSyncedGeneration` 保证幂等：仅在 CR 的 `spec` generation 前进时同步，避免每次 reconcile 重复调用远端 API。
- 为空时保持既有回退行为，不产生任何额外调用。

## 2. 需求

| # | 需求 |
|---|------|
| R1 | Worker 支持 `spec.displayName`，非空时写入该 Worker Matrix 用户的 profile displayname。 |
| R2 | Team 支持 `spec.displayName`，非空时团队房间名为 `Team: <displayName>`；为空时回退 `Team: <teamName>`。 |
| R3 | 已存在的 Worker/Team 在 `displayName` 被修改后（generation 前进）自动同步，无需重建/重删。 |
| R4 | 同步失败不得阻塞 Worker/Team 的基础设施（infra）reconcile（非致命）。 |
| R5 | 修改 `displayName` 不得触发 Worker Pod 重建（不参与 `status.specHash`）。 |
| R6 | REST API 与 `agt` CLI 的创建/更新/查询均可读写该字段。 |
| R7 | CRD 同步：`config/crd/` 为唯一来源，Helm `helm/agentteams/crds/` 保持一致。 |

## 3. 术语

- **generation**：CR 的 `metadata.generation`，每次 spec 变更 +1，由 K8s API server 维护。
- **displayNameSyncedGeneration**：status 中记录“最近一次成功同步 displayName 时的 generation”。
- **Matrix profile displayname**：Matrix 用户资料中供人阅读的显示名，与 Matrix UserID（localpart）无关。

## 4. 数据模型（CRD 字段）

### 4.1 `WorkerSpec`（`agentteams-controller/api/v1beta1/types.go`）

```go
DisplayName string `json:"displayName,omitempty"` // 可选；Matrix 资料显示名，回退到 workerName
```

### 4.2 `TeamSpec`

```go
DisplayName string `json:"displayName,omitempty"` // 可选；团队房间名用，回退到 teamName
```

### 4.3 `WorkerStatus`

```go
// 记录最近一次成功同步 spec.displayName 到 Matrix profile 时的 generation。
DisplayNameSyncedGeneration int64 `json:"displayNameSyncedGeneration,omitempty"`
```

### 4.4 `TeamStatus`

```go
// 记录最近一次成功应用 spec.displayName 到团队房间名时的 generation。
DisplayNameSyncedGeneration int64 `json:"displayNameSyncedGeneration,omitempty"`
```

> 深拷贝说明：以上均为 `string` / `int64` 值类型，`zz_generated.deepcopy.go` 中各类的 `*out = *in` 已覆盖，**无需重新生成**。

### 4.5 CRD YAML

来源（手维护，`make generate` 只跑 `controller-gen object` 不生成 CRD）：

- `agentteams-controller/config/crd/workers.agentteams.io.yaml`
- `agentteams-controller/config/crd/teams.agentteams.io.yaml`

新增（两个文件同构）：

```yaml
displayName:
  type: string
  description: ...   # Worker: Matrix profile displayname / Team: Team room name
status:
  ...
  displayNameSyncedGeneration:
    type: integer
    format: int64
```

同步目标：`helm/agentteams/crds/{workers,teams}.agentteams.io.yaml`（逐字节一致）。

## 5. 语义规则

| 场景 | Worker | Team |
|------|--------|------|
| `displayName` 为空 | 不调用 `SetDisplayName`，保持现状 | 房间名 = `Team: <teamName>`，不重命名 |
| `displayName` 非空且 `generation == displayNameSyncedGeneration` | 跳过同步 | 跳过重命名 |
| `displayName` 非空且 `generation != displayNameSyncedGeneration` | 调用 `SetDisplayName`，成功后 stamp | `SetRoomName(Team: <displayName>)`，成功后 stamp |
| 同步失败 | 记日志（non-fatal），不 stamp，下次 reconcile 重试 | 返回错误 → 团队置 Failed，下次重试 |

注意：**不提供“清空”语义**——`update` 接口只在值非空时写入（与既有字段一致），一旦设置不可通过 API 清空。

## 6. 架构与数据流

### 6.1 Worker 链路

```
Worker CR (spec.displayName)
   │  generation / status.displayNameSyncedGeneration
   ▼
WorkerReconciler.workerMemberContextWithSpec   [worker_controller.go]
   │  填充 MemberContext{DisplayName, DisplayNameSyncedGeneration}
   ▼
ReconcileMemberInfra                            [member_reconcile.go]
   │  refresh 路径：ExistingMatrixUserID != "" → RefreshWorkerCredentials
   │  provision 路径：新建用户/房间 → WorkerProvisionResult{MatrixUserID, MatrixToken}
   ▼
syncMemberDisplayName(ctx, prov, m, userID, accessToken, state)
   │  条件：DisplayName != "" && accessToken != "" && Generation != SyncedGeneration
   │  调用 service.WorkerProvisioner.SetDisplayName(ctx, userID, accessToken, displayName)
   │  成功 → state.DisplayNameSynced = true；失败 → 仅记日志
   ▼
applyMemberStateToWorker                        [worker_controller.go]
   │  state.DisplayNameSynced == true → w.Status.DisplayNameSyncedGeneration = w.Generation
   ▼
统一 deferred status patch
```

`hashAppliedWorkerSpec`（`worker_controller.go`）已显式将 `spec.DisplayName = ""` 排除在 `status.specHash` 之外，保证 R5。

### 6.2 Team 链路

```
Team CR (spec.displayName)
   │  generation / status.displayNameSyncedGeneration
   ▼
TeamReconciler.reconcileTeam                    [team_controller.go]
   │  TeamRoomRequest{DisplayName, Generation, DisplayNameSyncedGeneration}
   ▼
ProvisionTeamRooms                              [service/provisioner.go]
   │  teamRoomName = DisplayName != "" ? "Team: <DisplayName>" : "Team: <TeamName>"
   │  1) CreateRoom(Name: teamRoomName, ...)
   │  2) 若 DisplayName != "" && Generation != DisplayNameSyncedGeneration：
   │        p.matrix.SetRoomName(roomID, teamRoomName, TeamAdminActorToken)
   │        （token 为空时由 TuwunelClient 回退 ensureAdminToken）
   │  3) 成功后 result.DisplayNameSynced = true
   ▼
TeamRoomResult{TeamRoomID, LeaderDMRoomID, DisplayNameSynced}
   ▼
TeamReconciler 写回 status：
   rooms.DisplayNameSynced == true → t.Status.DisplayNameSyncedGeneration = t.Generation
```

接口：`service.WorkerProvisioner` 新增

```go
SetDisplayName(ctx context.Context, userID, accessToken, displayName string) error
```

`test/testutil/mocks/provisioner.go` 的 `MockProvisioner` 原本已实现该方法（Human 复用），并记录 `Calls.SetDisplayName`。

## 7. REST API 契约（`internal/server/types.go` / `resource_handler.go`）

| 结构体 | 字段 |
|--------|------|
| `CreateWorkerRequest` | `displayName,omitempty` |
| `UpdateWorkerRequest` | `displayName,omitempty`（非空才写入） |
| `WorkerResponse` | `displayName,omitempty` |
| `CreateTeamRequest` | `displayName,omitempty` |
| `UpdateTeamRequest` | `displayName,omitempty`（非空才写入） |
| `TeamResponse` | `displayName,omitempty` |

## 8. `agt` CLI 契约

| 命令 | 变化 |
|------|------|
| `agt create worker --display-name <v>` | 新 flag，写入 `req["displayName"]` |
| `agt create team --display-name <v>` | 新 flag，写入 `req["displayName"]` |
| `agt update worker --display-name <v>` | 新 flag，`setIfNotEmpty(req, "displayName", ...)` |
| `agt update team --display-name <v>` | 新 flag，同上 |
| `agt get workers` | 新增 `DISPLAY-NAME` 列（空显示 `-`） |
| `agt get teams` | 新增 `DISPLAY-NAME` 列（空显示 `-`） |
| `agt get worker <name>` / `agt get team <name>` | 详情新增 `DisplayName` 行 |

## 9. 边界与兼容性

- 存量 CR 无 `displayName`：不触发任何同步，行为与实现前一致。
- 无 TeamAdmin 的团队：`SetRoomName` 空 token 回退全局 admin token（`TuwunelClient.ensureAdminToken`），仍可重命名。
- 远端同步失败（如 homeserver 不可用）：Worker 非致命，Team 返回错误并置 Failed，均可在下次 reconcile 重试。
- `displayName` 不含默认值、不含校验规则（可含空格/中文/任何字符串）。
- 该字段不参与 `status.specHash`（Worker）与 Pod/容器创建，安全性无新增面。

## 10. 测试计划

### 10.1 `internal/service/provisioner_team_test.go`

| 用例 | 断言 |
|------|------|
| `TestProvisionTeamRoomsRenamesTeamRoomForDisplayName` | 房间名 `Team: Alpha Squad`；`SetRoomName` 恰好 1 次（roomID `!team:localhost`、空 token）；`res.DisplayNameSynced == true` |
| `TestProvisionTeamRoomsSkipsRenameWhenDisplayNameGenerationSynced` | `Generation == DisplayNameSyncedGeneration` 时不调用 `SetRoomName` |
| `TestProvisionTeamRoomsFallsBackToTeamNameWithoutDisplayName` | 无 displayName 时房间名 `Team: alpha`，不调用 `SetRoomName` |

### 10.2 `internal/controller/member_reconcile_test.go`

| 用例 | 断言 |
|------|------|
| `TestReconcileMemberInfraSyncsDisplayNameWhenConfigured` | refresh 路径调用 `SetDisplayName`（userID、displayName 正确），`state.DisplayNameSynced == true` |
| `TestReconcileMemberInfraSkipsDisplayNameSyncWhenGenerationSynced` | 不调用，`state.DisplayNameSynced == false` |
| `TestReconcileMemberInfraSkipsDisplayNameSyncWithoutDisplayName` | 不调用 |

运行：`go test -count=1 ./internal/controller/... ./internal/service/...`

## 11. 涉及文件清单

```
agentteams-controller/api/v1beta1/types.go                       # 字段定义
agentteams-controller/config/crd/workers.agentteams.io.yaml      # CRD 源
agentteams-controller/config/crd/teams.agentteams.io.yaml
helm/agentteams/crds/workers.agentteams.io.yaml                  # 已同步
helm/agentteams/crds/teams.agentteams.io.yaml
agentteams-controller/internal/service/interfaces.go             # WorkerProvisioner.SetDisplayName
agentteams-controller/internal/service/provisioner.go            # 房间名/重命名逻辑
agentteams-controller/internal/controller/member_reconcile.go    # MemberContext/MemberState/syncMemberDisplayName
agentteams-controller/internal/controller/worker_controller.go   # 填充 + stamp + hash 排除
agentteams-controller/internal/controller/team_controller.go     # 传参 + stamp
agentteams-controller/internal/server/types.go                   # API 结构体
agentteams-controller/internal/server/resource_handler.go        # 创建/更新/响应转换
agentteams-controller/cmd/agt/create.go / update.go / get.go     # CLI
agentteams-controller/internal/service/provisioner_team_test.go  # 新测试 ×3
agentteams-controller/internal/controller/member_reconcile_test.go # 新测试 ×3
changelog/current.md                                             # 变更条目
```

## 12. 验收标准

- [ ] `go build ./api/... ./internal/controller/... ./internal/service/... ./internal/server/... ./cmd/agt/...` 通过
- [ ] `go vet`（同包范围）通过
- [ ] 上述 6 个新测试 + 既有测试全部通过
- [ ] 改动文件与仓库基准 gofmt 一致（无新增 `gofmt -l` 命中）
- [ ] `config/crd/*.yaml` 与 `helm/agentteams/crds/*.yaml` 逐字节一致
- [ ] `changelog/current.md` 已记录（提交后补 commit 链接）
