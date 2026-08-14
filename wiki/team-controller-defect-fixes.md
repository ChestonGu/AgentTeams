# HiClaw 控制器缺陷与优化总结

> 版本: v1.0
> 日期: 2026-08-06
> 基线分支: `fix/team-cr-blocking-defects`
> 关联文档: [controller-tuning-sop.md](controller-tuning-sop.md)（调优 SOP）
> 调研底稿（本地 .issue/ 笔记，未入库）: team-controller-defects.md、team-controller-performance.md、team-controller-troubleshooting.md

---

## 1. 背景与范围

本分支针对 hiclaw-controller 的 **Team / Human 调谐（reconcile）阻塞与性能**问题做了系统性修复。缺陷源起于 PR #3（`7abe329`，2026-07-30，Team reconcile timing logs 的初始实现）引入的调谐语义缺陷，以及既有的性能瓶颈（单线程串行、无 ObservedGeneration 短路、mc 子进程风暴、Failed 抢占队列）。

**提交范围**：相对 `origin/dev` 共 19 个提交。本文档只覆盖**缺陷修复与优化**类提交；纯调试/观测工具类提交（bench、pprof、telemetry、timing 日志、指标）单列于 §4，构成调优 SOP 的诊断链。

---

## 2. 缺陷与优化总览

### 2.1 已修复缺陷（本分支落地）

| 编号 | 缺陷 | 根因 | 优化方案 | 提交 |
|------|------|------|----------|------|
| F-01 | failTeam/failHuman 双重 Requeue | `Result{RequeueAfter}` 与 error 同时触发，controller-runtime 的 rate limiter 指数退避（5ms→1000s）叠加在预期 30s 之上，重试节奏完全失控 | 失败路径改为 `Result{RequeueAfter: delay}, nil`（Result-only，不触发 rate limiter），退避由显式 `failBackoffFor` 控制 | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-02 | Finalizer 更新无 Conflict 重试 | `r.Update` 遇到并发写直接以 Conflict 错误终止整轮 reconcile | finalizer 增删改用 `r.Patch(ctx, &team, client.MergeFrom(base))` merge patch | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-03 | Reconcile 无 Context Deadline | 外部依赖（OSS/Matrix/凭据刷新）挂起时无限占用 worker 槽 | 新增 `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`（默认 0=关闭，保持旧行为），开启后单次 pass 快速失败交退避 | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-04 | 单线程串行瓶颈 | controller-runtime 默认 `MaxConcurrentReconciles=1`，一个慢/挂起 Team 阻塞全部 Team（含新建 CR 长期 Phase="" / Pending） | 并发提升至 5（`48ce4aa` 硬编码）→ 改为 `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` 可配置、默认 1 保持串行（`c01aaec`） | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa)、[c01aaec](https://github.com/agentscope-ai/HiClaw/commit/c01aaec) |
| F-05 | TeamStatus 缺 ObservedGeneration，无 Active 短路 | 重启/informer re-sync 后无法区分"spec 真变了"与"List 入队"，已收敛 Active Team 也跑全量调谐链（rooms、storage、每成员几十次 mc） | 新增 `observedGeneration`；`Phase==Active && Generation==ObservedGeneration` 且成员全部就绪 → 短路快路径跳过全量链路（不写 status patch，避免自触发入队）；成功 pass 写入、失败重置 | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-06 | Failed/Degraded Team 抢占队列 | 每 30s 固定重试、无退避、无上限，持续挤占 workqueue，新建 Team 永远排不上 | 指数退避 30s→1m→2m→4m→8m（封顶 10m）；连续失败 5 次后 `maxRetriesReached=true` 停止自动重试，`hiclaw.io/retry` 注解重新武装（守卫置于 deletion 处理之后，保证可删）；另加退避守卫防止 status patch 触发 informer 立即重入打乱退避节奏 | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-07 | 周期 requeue 固定且无抖动 | 收敛 Team 每 5min 无谓调谐；多 Team 同时唤醒形成调谐风暴 | `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS`（默认 300s）+ 0~10% 正向抖动避免锁步 | [462f84d](https://github.com/agentscope-ai/HiClaw/commit/462f84d) |
| F-08 | 周期调谐仍多余 | 事件驱动已足够覆盖（pod 事件/spec 修改），定时器只制造队列负载 | `HICLAW_TEAM_ACTIVE_NO_REQUEUE=true`：已收敛且 spec 未变的 Active Team 只按事件调谐 | [84a9429](https://github.com/agentscope-ai/HiClaw/commit/84a9429) |
| F-09 | mc 子进程风暴（Step 4 分钟级主因） | 每次 OSS 操作 `exec` fork mc 子进程 + TLS 握手 + 无连接池；每成员每轮 10~20 次调用，本地 MinIO 200~400ms/次、云 OSS 400~800ms+/次 | minio-go SDK 存储驱动（`HICLAW_STORAGE_DRIVER=sdk` 默认）：HTTP 连接池复用 + 2s dial 超时 + 30s 总重试窗口；bench 实测每成员 config 阶段延迟降 **~5.8×**；`mc` 驱动保留可回滚，动态 STS 凭据两驱动均支持 | [95dcae1](https://github.com/agentscope-ai/HiClaw/commit/95dcae1) |
| F-10 | 存储不稳定导致分钟级硬等待 | 端点不可达时 mc 默认 30s dial 超时逐调用叠加（每成员 10~20 次）；SDK 默认 10×30s 重试 | 全部存储操作（SDK+mc 驱动）2s 拨号 + 30s 总重试窗口（退避 0.5s→5s，确定性 4xx 不重试）；`probeStorage` 前置 30s 探测与窗口一致，抖动在窗口内恢复不触发 requeue | [e620ba2](https://github.com/agentscope-ai/HiClaw/commit/e620ba2) |
| F-11 | builtin skill 全量 mirror 重传 | 每次调谐 `mc mirror --overwrite` 全量重传，每成员 25~35s | content-compare 逐文件推送：GET-compare 跳过未变更对象，秒级完成，同时保留新镜像技能更新传播 | [e620ba2](https://github.com/agentscope-ai/HiClaw/commit/e620ba2) |
| F-12 | Human 与 Team 状态字段不对齐 | HumanStatus 缺 observedGeneration / 退避计数 | `HumanStatus` 补 `observedGeneration` + `consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`，与 Team 同套指数退避 + `hiclaw.io/retry` 武装机制 | [48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa) |
| F-13 | 控制器镜像缺 shared-lib | 构建未携带 `shared/lib`（hiclaw-env.sh 等），镜像内脚本运行环境不完整 | Makefile/Dockerfile 改用命名 build-context 携带 shared/lib 构建 | [82a75e8](https://github.com/agentscope-ai/HiClaw/commit/82a75e8) |
| F-14 | 脚本缺 base.sh 时 `log: command not found` | 控制器/worker 镜像无 base.sh 时脚本首个日志调用即 exit 127，掩盖真实错误 | `hiclaw-env.sh` 定义最小 `log()` fallback | [c895d3a](https://github.com/agentscope-ai/HiClaw/commit/c895d3a) |
| F-15 | package 下载仍 fork mc | `DeployPackage` 下载路径绕过 StorageClient 硬编码 `mc cp`/`mc stat`，OSS 抖动时 `dial tcp io timeout` 报错且无重试 | `PackageResolver` 注入 StorageClient，下载走 SDK（继承 2s 拨号 + 30s 重试 + 连接池）；ETag 内容缓存 + 原子写；`mc` 仅作 nil-storage fallback | 本分支 |
| F-16 | status.message 多层嵌套 | 一次 mc 读写失败带 6+ 层包装（deploy package → resolve/extract → download → mc stderr → exit status） | oss 层 `OpError` 单层化（`storage get <key>: dial tcp ...`）；Team/Worker/Manager/Human 统一 `conciseStatusMessage` 写根因 + 512 字符截断 | 本分支 |
| F-17 | worker-management 脚本 `log: command not found` | `push-worker-skills.sh` 等在缺 base.sh 镜像（或 hiclaw-env 未加载）时 mc 失败后的 `|| log WARNING` 直接 exit 127 | 三个脚本就地补防御性 `log()` fallback，mc 失败只记简洁 WARNING | 本分支 |

### 2.2 缺陷编号对照（调研底稿）

| 调研底稿编号 | 状态 | 对应本文档 |
|--------------|------|-----------|
| D-02 failTeam 双重 requeue | ✅ 已修复 | F-01 |
| D-07 Finalizer Conflict 重试 | ✅ 已修复 | F-02 |
| D-08 Reconcile 无 Deadline | ✅ 已修复 | F-03 |
| 性能 2.1 单线程瓶颈 / 6.1.1 | ✅ 已修复 | F-04 |
| 性能 2.2 缺 ObservedGeneration / 6.2.1 | ✅ 已修复 | F-05 |
| 性能 2.3 Active 无短路 / 6.1.2 | ✅ 已修复 | F-05、F-07 |
| 性能 2.4 Failed 无退避 / 6.2.2 | ✅ 已修复 | F-06 |
| 性能 6.3.1 MinIO mc → SDK | ✅ 已修复 | F-09 |
| 存储不稳定卡死 / 技能全量重传 | ✅ 已修复（分支内新增优化项） | F-10、F-11 |
| D-01 HTTP API 绕过 Reconciler | ⏳ 未修复 | — |
| D-03 Status 多次 Patch 非原子 | ⏳ 未修复 | — |
| D-04 无 K8s Events | ⏳ 未修复 | — |
| D-05 无 Conditions | ⏳ 未修复 | — |
| D-06 Healthz 恒 OK | ⏳ 未修复 | — |
| D-09 Pod 无 SecurityContext | ⏳ 未修复 | — |
| D-10 Phase 语义不一致 | ⏳ 未修复 | — |
| 性能 6.1.3 Manager 并发对齐 | ⏳ 未做 | — |
| 性能 6.3.2 Team 内成员并行化 | ⏳ 未做 | — |
| 性能 6.4.1 双 Controller 分离 | ⏳ 未做（评估后收益递减） | — |
| 性能 6.4.2 镜像变更分离 | ⏳ 未做 | — |

---

## 3. 关键设计决策说明

### 3.1 失败路径统一为"Result-only + 显式退避"

controller-runtime 对 `RequeueAfter` 与 error 的处理是**叠加**的：返回 error 会再走 workqueue rate limiter（默认 5ms→10s→…→1000s）。修复后失败路径一律 `reconcile.Result{RequeueAfter: delay}, nil`，退避节奏完全由 `failBackoffFor` 掌控（30s→8m，封顶 10m），且连续失败 5 次后停止自动重试，杜绝 Failed CR 无限抢占队列。

### 3.2 退避守卫（backoff guard）

`failTeam`/`failHuman` 通过 status patch 递增 `ConsecutiveFailures`，而 status patch 会触发 informer 立即重新入队——若不拦截，指数退避形同虚设。Reconcile 入口在 `Phase==Failed` 且退避窗口未过时直接返回（无错误、无 requeue），由原始 `RequeueAfter` 唤醒点续跑。

### 3.3 短路快路径不发 status patch

已收敛 Active Team 的短路路径**故意不写任何 status**：patch 会 bump resourceVersion 并再次触发 informer 入队，抵消短路意义。因此 `observedGeneration` 只在全量 pass 成功尾部写入。

### 3.4 重试上限的运维语义

`maxRetriesReached=true` 后 CR 完全退出队列（`Reconcile` 入口守卫返回空 Result）。排障完成后需人工武装：

```bash
kubectl annotate team <name> hiclaw.io/retry=""
kubectl annotate human <name> hiclaw.io/retry=""
```

守卫位于 deletion 处理**之前**的判定、但返回逻辑确保被冻结 CR 仍可正常删除（deletion 分支不受其影响）。

### 3.5 SDK 驱动对云 OSS 的适配

- `HICLAW_STORAGE_DRIVER=sdk`（默认），云 OSS 建议配 VPC 内网 endpoint 以规避公网延迟与限流；
- 静态长效凭据 `HICLAW_FS_ACCESS_KEY` / `HICLAW_FS_SECRET_KEY`；动态 STS（credential-provider sidecar）两驱动均支持；
- SDK 驱动通过 `bucketAndKey` 解析 alias/prefix，与 mc 驱动的 alias 路径语义对齐（`mapNotExist` 保证 `os.ErrNotExist` 契约一致，404 探活语义不变）。

---

## 4. 观测与调试工具链（支撑 SOP 的诊断能力）

| 提交 | 工具 | 用途 |
|------|------|------|
| [c01aaec](https://github.com/agentscope-ai/HiClaw/commit/c01aaec)、[b810e45](https://github.com/agentscope-ai/HiClaw/commit/b810e45) | 每步 timing 日志（`team reconcile: step N … elapsed=`）、`DeployWorkerConfig` 分阶段耗时 | 一眼定位慢步骤 |
| [ce1a531](https://github.com/agentscope-ai/HiClaw/commit/ce1a531) | team-scoped logger（`team=`/`teamUID=` 注入 ctx）、`timed` helper（成功/失败/取消均记 elapsed）、`mc slow call`（>300ms 阈值）、`modifyAIRoutes` 409 重试计数、docker 镜像拉取完成日志、panic 兜底 | 全链路可 grep 追踪；失败路径也有耗时 |
| [77ef4b8](https://github.com/agentscope-ai/HiClaw/commit/77ef4b8) | `hiclaw_storage_op_duration_seconds`（op×driver）、`hiclaw_storage_op_errors_total`（op×driver×class: network/timeout/not_found/other）、`hiclaw_storage_probe_failures_total`、`hiclaw_member_reconcile_duration_seconds`（kind×result） | 存储端点不稳定告警（network/timeout 上升）；成员收敛成本量化 |
| [6cf6f86](https://github.com/agentscope-ai/HiClaw/commit/6cf6f86) | `ENABLE_PPROF=true` 构建开关（`-tags pprof`），默认 no-op stub 零调试面；`HICLAW_PPROF_ADDR`（默认 6060） | 深度采样（CPU/goroutine/block/mutex/trace） |
| [a12c5b3](https://github.com/agentscope-ai/HiClaw/commit/a12c5b3) | `bench/bench_s3.go`：mc vs SDK 双驱动、真实桶只读复现（12 GET+3 PUT+2 STAT+1 LIST）、写空间 `bench-probe/` 隔离自动清理 | 判定瓶颈在调用模式还是 S3 服务端 |

---

## 5. 优化效果参考

| 场景 | 修复前 | 修复后（预期/实测） |
|------|--------|---------------------|
| 滚动升级全量调谐（100 个 Active Team，并发=1） | ~8.3h | ObservedGeneration 短路后秒级 re-sync |
| 新建 Team 首轮收敛 | 无限等待（队列被 Failed/挂起 Team 占满） | 并发提升 + 退避封顶后 <1min 量级 |
| 单成员 config 阶段（OSS） | mc 子进程 200~800ms×15~25 次 | SDK 驱动实测 **~5.8× 提速** |
| builtin skill 推送 | 全量 mirror 25~35s/成员 | content-compare 秒级 |
| Failed Team 重试 | 每 30s 无界抢占 | 指数退避 + 5 次封顶，队列零占用 |
| 存储端点不可达 | 每 op 卡 30s（数十次叠加） | 前置探测 10s 快速失败 |

---

## 6. 待办（未纳入本分支，需后续评估）

1. **D-01** HTTP API（Wake/Sleep/EnsureReady）绕过 Reconciler 直写 Backend/Status —— 改 spec-only + 202 异步返回；
2. **D-03** Team Status 多次 Patch 非原子 —— 统一 defer 单次 patch；
3. **D-04** 注入 EventRecorder，`kubectl describe` 可见事件；
4. **D-05** TeamStatus 增加 Conditions（InfraReady/ConfigReady/ContainerReady/LeaderReady）；
5. **D-06** Healthz 结合 workqueue/最后调谐时间真实检查（卡死返回 503）；
6. **D-09** Pod SecurityContext（RunAsNonRoot、只读根文件系统、drop ALL）；
7. **D-10** Phase 语义统一（Running vs Active）；
8. **性能 6.1.3** Manager 并发对齐；
9. **性能 6.3.2** Team 内成员并行化（errgroup，leader 串行 + workers 并行）；
10. **性能 6.4.2** 镜像变更与配置变更分离（排除 Image/Runtime 的成员 hash）。
