# Worker / Team `displayName` 实现计划（Plan）

> 目标：按本计划可在干净分支上完全复现已完成的 `displayName` 功能。
> 前置：已阅读 `docs/zh-cn/worker-team-displayname-spec.md`。
> 约定：所有命令在 `agentteams-controller/` 目录执行；包范围缩写见 §6。

---

## Phase 0 · 准备（5 min）

- [ ] 确认工作区干净：`git status --short` 只含预期文件。
- [ ] 确认 Go 工具链可用：`go version`。
- [ ] 通读目标文件：
  - `api/v1beta1/types.go`（`WorkerSpec` / `TeamSpec` / `WorkerStatus` / `TeamStatus`）
  - `internal/controller/member_reconcile.go`（`ReconcileMemberInfra`、`MemberContext`、`MemberState`）
  - `internal/service/provisioner.go`（`ProvisionTeamRooms`）
  - `internal/controller/worker_controller.go`、`team_controller.go`
  - `internal/server/types.go`、`resource_handler.go`、`cmd/agt/{create,update,get}.go`

---

## Phase 1 · 数据模型与 CRD（15 min）

1. `api/v1beta1/types.go`
   - `WorkerSpec` 在 `Image` 与 `WorkerName` 之间加 `DisplayName string`（json `displayName,omitempty`，行尾注释）。
   - `TeamSpec` 在 `Description` 之后加 `DisplayName string`。
   - `WorkerStatus` 加 `DisplayNameSyncedGeneration int64`（含说明注释）。
   - `TeamStatus` 加 `DisplayNameSyncedGeneration int64`（含说明注释）。
2. 手工编辑 CRD 源 `config/crd/workers.agentteams.io.yaml` 与 `teams.agentteams.io.yaml`：
   - `spec.properties` 中对应位置加 `displayName: {type: string, description: ...}`。
   - `status.properties` 加 `displayNameSyncedGeneration: {type: integer, format: int64, description: ...}`。
   - 保持与相邻字段（`image`/`workerName`、`description`/`teamName`）相同的键顺序。
3. 同步 Helm：复制 4 个 CRD 至 `helm/agentteams/crds/`。
4. 验证：
   ```powershell
   # 逐字节对比
   Get-ChildItem agentteams-controller/config/crd -Filter *.yaml | ForEach-Object {
     $h = Join-Path helm/agentteams/crds $_.Name
     (Get-FileHash $_.FullName).Hash -eq (Get-FileHash $h).Hash
   }
   ```
5. 说明：字段均为值类型，`zz_generated.deepcopy.go` 无需重新生成（`*out = *in` 覆盖）。

---

## Phase 2 · Worker 同步链路（20 min）

1. `internal/service/interfaces.go`：`WorkerProvisioner` 接口新增
   ```go
   SetDisplayName(ctx context.Context, userID, accessToken, displayName string) error
   ```
   确认 `test/testutil/mocks/provisioner.go` 已有该方法（Human 复用），否则补齐。
2. `internal/controller/member_reconcile.go`
   - `MemberContext` 加 `DisplayName string`、`DisplayNameSyncedGeneration int64`（放在 `ExistingRoomID` 之后）。
   - `MemberState` 加 `DisplayNameSynced bool`。
   - `ReconcileMemberInfra` 的两条返回前路径都调用新增 helper：
     - refresh 路径（`ExistingMatrixUserID != "" && ExistingRoomID != ""`）：
       `syncMemberDisplayName(ctx, d.Provisioner, m, m.ExistingMatrixUserID, refreshResult.MatrixToken, state)`
     - provision 路径（末尾）：
       `syncMemberDisplayName(ctx, d.Provisioner, m, provResult.MatrixUserID, provResult.MatrixToken, state)`
   - 新增 helper（非致命）：
   ```go
   func syncMemberDisplayName(ctx context.Context, prov service.WorkerProvisioner, m MemberContext, userID, accessToken string, state *MemberState) {
       if m.DisplayName == "" || accessToken == "" || m.Generation == m.DisplayNameSyncedGeneration {
           return
       }
       logger := log.FromContext(ctx)
       if err := prov.SetDisplayName(ctx, userID, accessToken, m.DisplayName); err != nil {
           logger.Error(err, "failed to sync member displayName (non-fatal)", "name", m.Name, "userID", userID)
           return
       }
       logger.Info("member displayName synced", "name", m.Name, "userID", userID, "displayName", m.DisplayName)
       state.DisplayNameSynced = true
   }
   ```
3. `internal/controller/worker_controller.go`
   - `workerMemberContextWithSpec` 的 `MemberContext{...}` 加：
     ```go
     DisplayName:                 spec.DisplayName,
     DisplayNameSyncedGeneration: w.Status.DisplayNameSyncedGeneration,
     ```
     （新增键是字面量中最长的，需按 gofmt 重新对齐整块，见 §7。）
   - `applyMemberStateToWorker` 加：
     ```go
     if state.DisplayNameSynced {
         w.Status.DisplayNameSyncedGeneration = w.Generation
     }
     ```
   - `hashAppliedWorkerSpec` 加 `spec.DisplayName = ""`（避免 displayName 变更触发 Pod 重建），同步更新函数头注释中 “Excluded” 列表。

---

## Phase 3 · Team 同步链路（20 min）

1. `internal/service/provisioner.go`
   - `TeamRoomRequest` 加 `DisplayName string`、`Generation int64`、`DisplayNameSyncedGeneration int64`（后两者带注释）。
   - `TeamRoomResult` 加 `DisplayNameSynced bool`。
   - `ProvisionTeamRooms`：
     - 计算房间名：`DisplayName != "" ? "Team: <DisplayName>" : "Team: <TeamName>"`，`CreateRoom.Name` 用之。
     - 团队房间创建成功后：
       ```go
       result := &TeamRoomResult{TeamRoomID: teamRoom.RoomID, LeaderDMRoomID: ""}
       if req.DisplayName != "" && req.Generation != req.DisplayNameSyncedGeneration {
           if err := p.matrix.SetRoomName(ctx, teamRoom.RoomID, teamRoomName, req.TeamAdminActorToken); err != nil {
               return nil, fmt.Errorf("rename team room: %w", err)
           }
           result.DisplayNameSynced = true
       }
       ```
     - 函数末尾把原 `return &TeamRoomResult{...}` 改为 `result.LeaderDMRoomID = leaderDMRoom.RoomID; return result, nil`。
2. `internal/controller/team_controller.go` `reconcileTeam`
   - `TeamRoomRequest` 字面量加：`DisplayName: t.Spec.DisplayName`、`Generation: t.Generation`、`DisplayNameSyncedGeneration: t.Status.DisplayNameSyncedGeneration`。
   - `t.Status.TeamRoomID = ...` 之后加：
     ```go
     if rooms.DisplayNameSynced {
         t.Status.DisplayNameSyncedGeneration = t.Generation
     }
     ```
   - 新增键为最长，需按 gofmt 重新对齐字面量。

---

## Phase 4 · REST API 与 CLI（15 min）

1. `internal/server/types.go`：
   - `CreateWorkerRequest` / `UpdateWorkerRequest` / `WorkerResponse` 加 `DisplayName string`（json `displayName,omitempty`）。
   - `CreateTeamRequest` / `UpdateTeamRequest` / `TeamResponse` 加 `DisplayName string`。
2. `internal/server/resource_handler.go`：
   - `CreateWorker` 的 `WorkerSpec{...}` 加 `DisplayName: req.DisplayName`。
   - `UpdateWorker` 加 `if req.DisplayName != "" { worker.Spec.DisplayName = req.DisplayName }`。
   - `CreateTeam` 的 `TeamSpec{...}` 加 `DisplayName: req.DisplayName`。
   - `UpdateTeam` 加 `if req.DisplayName != "" { team.Spec.DisplayName = req.DisplayName }`。
   - `workerToResponse` / `teamToResponse` 加 `DisplayName: ...`。
3. `cmd/agt/create.go`：
   - `createWorkerCmd` / `createTeamCmd` 的 `var (...)` 加 `displayName string`；注册 `--display-name` flag；请求体 `setIfNotEmpty(req, "displayName", displayName)`。
4. `cmd/agt/update.go`：`updateWorkerCmd` / `updateTeamCmd` 同样加 flag 与 `setIfNotEmpty`。
5. `cmd/agt/get.go`：
   - `workerResp` / `teamResp` 加 `DisplayName` 字段。
   - 列表 headers 加 `"DISPLAY-NAME"`，行数据加 `or(w.DisplayName, "-")` / `or(t.DisplayName, "-")`。
   - `workerDetail` / `teamDetail` 加 `{"DisplayName", ...}`。

---

## Phase 5 · 测试与变更记录（20 min）

1. `internal/service/provisioner_team_test.go` 追加 3 个用例（`TestProvisionTeamRoomsRenamesTeamRoomForDisplayName`、`...SkipsRenameWhenDisplayNameGenerationSynced`、`...FallsBackToTeamNameWithoutDisplayName`）。利用既有 `fakeTeamMatrix.roomNames` / `createRooms[0].Name` 断言。
2. `internal/controller/member_reconcile_test.go` 追加 3 个用例（`TestReconcileMemberInfraSyncsDisplayNameWhenConfigured`、`...SkipsDisplayNameSyncWhenGenerationSynced`、`...SkipsDisplayNameSyncWithoutDisplayName`）。利用 `mocks.NewMockProvisioner().Calls.SetDisplayName`。
3. `changelog/current.md` 的 “What's New” 加一条（提交后补 commit 链接）。

---

## Phase 6 · 验证（20 min）

```powershell
# 6.1 构建（跳过 kine/CGO 的 ./cmd/controller，与环境无关）
go build ./api/... ./internal/controller/... ./internal/service/... ./internal/server/... ./cmd/agt/...

# 6.2 静态检查
go vet ./api/... ./internal/controller/... ./internal/service/... ./internal/server/... ./cmd/agt/...

# 6.3 单元测试
go test -count=1 ./api/... ./internal/controller/... ./internal/service/... ./internal/server/... ./cmd/agt/...

# 6.4 gofmt（如 gofmt 偶发崩溃则重试；以 LF 归一化后比较）
# 6.5 CRD 同步哈希对比（见 Phase 1.4）
```

注意事项：
- 若本机 `gofmt` 抖动（Go 运行时崩溃），用 LF 归一化临时副本做 `gofmt -d` 校验，不要对全仓文件直接 `gofmt -w`（仓库为混合换行）。
- 验证完毕后 `git diff --stat` 应只包含 §11 清单中的文件。

---

## 风险与注意事项

| 风险 | 说明 | 缓解 |
|------|------|------|
| 字面量对齐 | 新增最长键会使 gofmt 重排整块 `MemberContext{...}` / `TeamRoomRequest{...}` | 用 LF 归一化副本 + `gofmt -d` 提取正确块后回填 |
| 无 TeamAdmin 团队重命名 | `TeamAdminActorToken` 为空 | `TuwunelClient.SetRoomName` 空 token 回退 `ensureAdminToken`（已验证） |
| spec hash 抖动 | displayName 进入 `specHash` 会误触发 Pod 重建 | `hashAppliedWorkerSpec` 显式清零该字段 |
| 清空语义 | API 只写非空值 | 接受为既有惯例，不额外支持清空 |
| `make` 不可用环境 | `make check-crd-sync` 跑不了 | 用 PowerShell 哈希对比 CRD |
| `./cmd/controller` 全量构建 | 依赖 `kine` sqlite（CGO），无环境时失败 | 已确认与本次改动无关；CI 负责该路径 |

## 提交建议

建议拆 2 个 commit（需用户确认后执行，本计划不含自动提交）：
1. `feat(controller): add displayName to Worker and Team with Matrix sync`
   —— 类型 + CRD + sync 链路 + API + CLI + 测试。
2. `docs(changelog): record Worker/Team displayName`（或并入 1）
   —— 在 commit 后回填 `changelog/current.md` 的 commit 链接。
