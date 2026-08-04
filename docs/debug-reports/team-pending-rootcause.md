# Debug 报告：控制器调谐耗时与 Team 长时间 Pending 根因分析

日期：2026-08-04

## 现象

- Team CR 创建后 Phase 长时间停留在 `Pending`，数分钟 ~ 数十分钟后才自愈为 `Active`。
- 控制器创建的 Team **越多，等待时间越长**，近似线性增长。
- 该现象出现在**未合入 403 幂等修复提交**的版本，底层 homeserver 为 **Synapse**（此前为 Tuwunel，不会触发）。

## 环境

- 底层 homeserver：Synapse（从 Tuwunel 切换后出现）。
- 版本：未包含 403 幂等修复（本地分支 `fix/joinroom-already-in-room` 上的 bcca631、70daf6f）。
- 触发点：主要是 `JoinRoom` 场景（重复 join 返回 403 `M_FORBIDDEN already in the room`）。

## 根因分析

### 1. 主根因：reconcile 串行 + 单次调谐耗时大（为什么这么慢）

- 控制器未设置 `MaxConcurrentReconciles`（全仓库无 `SetMaxConcurrentReconciles` 调用）→ controller-runtime 默认 **1 个并发 worker**，所有 CR 的 reconcile 进同一 workqueue **串行消费**。
- 单次 Team reconcile（`reconcileTeamNormal`，`hiclaw-controller/internal/controller/team_controller.go`）本身就是重活：
  - `ProvisionTeamRooms`：创建 **2 个房间**（team room + leader DM），每个房间对**每个成员**执行一次 Invite API 调用；
  - **串行成员循环**：leader 优先，每个成员走完整 `ProvisionWorker`（含 `time.Sleep(2s)` 等待 Higress WASM key-auth 同步，`hiclaw-controller/internal/service/provisioner.go:455`）+ 容器创建；
  - 单次全量 reconcile ≈ **30s ~ 数分钟**。
- 结论：N 个 Team 同时创建 → 队列积压 = **O(N × 单次耗时)**，线性增长，与观察到的"Team 越多越久"吻合。

### 2. 放大器：Synapse 下 team admin JoinRoom 403 → Failed + 指数退避

- Team admin 是房间创建者（`CreatorToken = TeamAdminActorToken`）→ 创建者自动加入 → 后续 `JoinRoom`（`provisioner.go:714` team room、`786-803` leader DM）在 Synapse 下**必然 403**（Tuwunel 对重复 join 返回 200）。
- team room 路径（714）是 **fatal** → `failTeam`（`team_controller.go:664`）：写 `Failed` + `RequeueAfter 30s` + 返回 error → controller-runtime 默认 rate limiter **指数退避（5ms → 1000s）**，失败重跑反复阻塞队首。
- Synapse 默认 rate limit（`rc_login`/`rc_admin`/`rc_message`）的 429 风暴同理（已由 69d2dc2 缓解）。

### 3. 为什么能自愈（不依赖 403 幂等补丁）

- **Worker 场景**：`ProvisionWorker` Step 4b 用自己的 token join 自己的房间，失败是 **non-fatal**（`provisioner.go:426-434`，仅记日志）→ reconcile 继续执行并成功 → **永不降级**。那条 403 日志只是每次 5 分钟周期重复出现的噪音。
- **Team 场景**：403 是**瞬态错误**——仅当"已加入"状态存在时触发。当房间/成员状态变化（成员重建、`LeaveAllWorkerRooms`、合入幂等补丁）后错误消失 → 下一次 reconcile 成功 → 覆盖 `Failed`，即为"自愈"。
- 唯一内置恢复路径：`ensureTeamAdminJoinedLeaderDM` 的 `created=false` 分支（`provisioner.go:786-803`）——JoinRoom 失败后由 leader 用自己 token 重新 invite team admin 再重试 join；team room 的 714 路径没有该恢复逻辑。

### 4. 为什么是 Pending 而不是 Failed

Pending 长时间停留是两种机制叠加：

| 候选 | 机制 | 判定 |
|---|---|---|
| A. 排队未执行 | 串行队列积压：reconcile 被入队但迟迟轮不到 | 两次 "team reconciled" 日志间隔 ≈ 队列长度 × 单次耗时 |
| B. 执行了但容器未就绪 | Phase=Active 要求 `leaderReady && readyWorkers == TotalWorkers`（`team_controller.go:361-366`），等待容器拉起 + `hiclaw worker report-ready` 上报 | 日志 phase=Pending 且 `readyWorkers < TotalWorkers` |

## 证据链（代码定位）

| 现象 | 代码位置 | 结论 |
|---|---|---|
| reconcile 串行 | 全仓库无 `SetMaxConcurrentReconciles` | 默认 1 并发，workqueue 线性积压 |
| 单次 team 耗时高 | `provisioner.go:455` `time.Sleep(2s)`；串行成员循环 `team_controller.go:312-349`；2×CreateRoom | 单 team ≈ 30s ~ 数分钟 |
| worker 403 不降级 | `provisioner.go:426-434`（non-fatal） | 403 只是日志噪音，永不 Failed |
| team 403 fatal | `provisioner.go:714`、`786-803` | Failed + 指数退避，阻塞队首 |
| Pending 而非 Failed | `team_controller.go:361-366`（Active 需全成员 ready） | 排队/等容器就绪期间一直 Pending |
| 自愈 | 403 瞬态 + 状态收敛；`786-803` created=false 恢复分支 | 错误消失后下次 reconcile 覆盖 Failed |

## 影响

- 批量创建/扩容团队时，系统整体处于长时间"不可用"（成员无法工作）状态。
- 失败重跑 + 指数退避放大耗时，最坏单 Team 退避到 1000s。
- 403 日志噪音掩盖真实错误，干扰排障。

## 建议修复

1. **短期**：`SetupWithManager` 设置 `MaxConcurrentReconciles`（如 4-8），直接消除线性积压。
2. **合入修复提交**（本地分支 `fix/joinroom-already-in-room` 已含）：
   - bcca631：`JoinRoom` 对 403 `M_FORBIDDEN already-in-room` 幂等返回 nil；
   - 70daf6f：`ReconcileMemberInfra` 对 403 already 回退到凭据刷新；
   - 69d2dc2：提高 Synapse `rc_login`/`rc_admin`/`rc_message` 上限，缓解 429。
3. **长期**：并行化 `ProvisionTeamRooms` 成员操作；去掉 `time.Sleep(2s)`（改为轮询/异步确认 key-auth 生效）；team room 714 路径补上类似 786-803 的 invite-retry 恢复逻辑。

## 待验证

需要一条实际 controller 日志定案候选 A/B：

- `"failed to join worker into its own room"` → worker Step 4b non-fatal（噪音，可忽略）
- `"team admin join team room"` / `"team admin join leader DM room"` → team fatal（退避来源）
- `"team reconciled"` 行中的 `phase` / `leaderReady` / `readyWorkers` → 判定候选 B
- 相邻两次 `"team reconciled"` 的时间间隔 → 判定候选 A（间隔 ≈ 队列积压量）
