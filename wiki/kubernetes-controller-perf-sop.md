# Kubernetes CRD 控制器（Operator）性能问题排查定位调优 SOP

> 版本: v1.0
> 日期: 2026-08-06
> 适用范围: 基于 controller-runtime / client-go 构建的任意 Kubernetes operator（CRD 控制器）
> 定位: **通用方法论**，不绑定具体项目参数；HiClaw 控制器作为完整实例，见 [controller-tuning-sop.md](controller-tuning-sop.md)（实例参数）与 [team-controller-defect-fixes.md](team-controller-defect-fixes.md)（实例缺陷清单）

---

## 1. 控制器的性能模型（先建立心智模型）

所有调优决策都基于下图所示的四条通路：

```
                 ┌──────────────────────────────────────────────┐
   APIServer ──► │ Informer (List+Watch → cache)                 │
                 │        │  Add/Update/Delete 事件              │
                 │        ▼                                      │
                 │ Workqueue (RateLimiting, 去重)                │
                 │        │  出队                                 │
                 │        ▼                                      │
                 │ Worker × N (MaxConcurrentReconciles)          │
                 │        │  Reconcile(ctx)                      │
                 │        ▼                                      │
                 │ 业务逻辑 → 外部依赖 (DB/存储/HTTP/消息)        │
                 │        │                                      │
                 │        ▼                                      │
                 │ 写回 APIServer (Update/Patch/Status)          │
                 └──────────────────────────────────────────────┘
```

**四个瓶颈点，对应四类问题：**

| 瓶颈点 | 表现 | 典型根因 |
|--------|------|----------|
| ① 入队侧（Informer/cache） | 事件风暴、watch 带宽打满、缓存内存膨胀 | 无过滤 predicate、resync 全量入队、pod phase 级联事件 |
| ② 队列侧（Workqueue） | depth 持续高位，CR 长期不收敛 | 并发不足、处理慢、失败重试风暴 |
| ③ 处理侧（Reconcile） | 单次耗时异常、worker 被长期占用 | 外部依赖慢、同步阻塞、子进程/无连接池 |
| ④ 写回侧（APIServer） | conflict 风暴、status 写不进、429 限流 | Update 滥用、多写非原子、无缓存复用 |

**三条性能定律（排查时的判断依据）：**

1. **吞吐 = 并发数 / 单次 Reconcile 耗时** —— 想提吞吐，要么提并发，要么降单次耗时；
2. **队列堆积 = 入队速率 > 处理速率** —— depth 涨说明入队比处理快，先分清是"事件太多"还是"处理太慢"；
3. **端到端耗时 = 最慢的外部依赖 × 串行次数** —— 单次 reconcile 的耗时上限由同步外部调用决定，异步化/批量化/连接池是降耗三件套。

---

## 2. 症状分类（先分类，再动手）

| 类型 | 症状 | 主判断指标 | 排查方向 |
|------|------|------------|----------|
| **T-1 队列堆积** | CR 长期不收敛、新建 CR 无 Phase/无 Status | `workqueue_depth` 持续高位 | 并发不足 / 单 CR 挂起占满 worker / 事件风暴 |
| **T-2 单次调谐慢** | 单 CR 收敛耗时远超预期 | `workqueue_work_duration` 高 | 外部依赖慢 / 代码路径长 / 同步阻塞（先拆解再定位） |
| **T-3 重启/升级风暴** | 滚动升级后全量重调谐、API 打爆 | 升级时 `workqueue_adds_total` 尖峰 | 无 ObservedGeneration 短路 / informer resync 全量入队 |
| **T-4 资源占用异常** | CPU/内存/goroutine 持续高位 | container 资源指标、pprof | 泄漏 / 事件风暴 / 子进程风暴 / 无界缓存 |
| **T-5 打爆 APIServer** | 429/限流、apiserver 高负载 | `rest_client_requests_total` 429 计数 | 高频 requeue / watch 过多 / 不用 informer 缓存直连 API |

---

## 3. 五步排查流程

### Step 0 — 量化症状（指标先行，先拿基线）

controller-runtime **自带** `/metrics`（`sigs.k8s.io/controller-runtime/pkg/metrics`），第一优先看这些，零代码改动：

| 指标 | 判读 |
|------|------|
| `workqueue_depth` | 队列积压量。持续 >0 且不回落 = 处理跟不上入队 |
| `workqueue_adds_total` | 入队速率。看事件风暴（rate 是否异常尖峰） |
| `workqueue_queue_duration_seconds` | 入队到出队的等待。高 = 并发不足（排队久） |
| `workqueue_work_duration_seconds` | 单次 reconcile 实际耗时。高 = 处理本身慢 |
| `workqueue_retries_total` | 失败重试次数。高 = 错误模式（无退避/双重 requeue） |
| `rest_client_requests_total` / `rest_client_rate_limiter_duration_seconds` | 对 APIServer 的调用量与限流等待 |
| `leader_election_master_status` | 是否为 leader（多副本时判断哪个 Pod 在干活） |
| `process_*`（go 运行时） | CPU/内存/goroutine 数 |

```bash
# 判断"队列堵 vs 处理慢"的核心命令
# 积压：持续高位
sum(workqueue_depth{name=~"<controller>"})
# 单次耗时：P95
histogram_quantile(0.95, sum(rate(workqueue_work_duration_seconds_bucket[5m])) by (le))
# 入队速率：看风暴
sum(rate(workqueue_adds_total[5m])) by (name)
```

**同一时刻对比正常/异常**（升级前 vs 升级后、故障中 vs 恢复后）比看绝对值更有用。

### Step 1 — 队列侧判断（堵在入队还是处理）

```
workqueue_depth 高？
├─ queue_duration 高（入队后等很久才处理） → 并发不足 → MaxConcurrentReconciles / 降单次耗时
├─ work_duration 高（一旦开始处理就很慢）  → 单次慢 → 进 Step 2
├─ retries 高（反复失败重试）              → 错误风暴 → 查失败模式（§5 缺陷清单）
└─ adds 尖峰（事件量爆炸）                → 事件风暴 → 查 predicate/入队源
```

### Step 2 — 单次耗时拆解（代码路径打点）

原则：**成败都要有耗时**，**每次调用都有上下文标识**。

```go
// 1. Reconcile 入口注入 request 级 logger（日志全链路可 grep）
ctx = log.IntoContext(ctx, log.FromContext(ctx).WithValues(
    "cr", req.NamespacedName.String(), "uid", string(cr.UID)))

// 2. 统一 timed helper：成功/失败/ctx 取消都记录 elapsed
func timed(ctx context.Context, phase string, fn func() error) error {
    start := time.Now()
    err := fn()
    if ctx.Err() != nil {
        log.FromContext(ctx).Error(ctx.Err(), "timed-call cancelled", "phase", phase, "elapsed", time.Since(start))
    } else if err != nil {
        log.FromContext(ctx).Error(err, "timed-call failed", "phase", phase, "elapsed", time.Since(start))
    } else {
        log.FromContext(ctx).Info("timed-call", "phase", phase, "elapsed", time.Since(start))
    }
    return err
}

// 3. 外部依赖每调用打阈值日志（>300ms 之类），量化依赖层延迟分布
// 4. 顶层 panic 兜底（recover 后带上下文记日志并按错误 requeue）
```

拆解模板（每个业务阶段一个 `timed`）：

```
resolve-admin(200ms) → create-rooms(1.2s) → ensure-storage(300ms)
→ per-member × N: infra(220ms) + config(1.5~2s) + container(100ms+) + expose(200ms)
```

**判断原则**：找到占比最大的阶段，再下钻一层。慢通常集中在外部依赖（存储/消息/HTTP），代码内循环先排查 O(N) 全局遍历。

### Step 3 — 深挖根因（pprof / 依赖侧 / APIServer 侧）

**3a. pprof（控制器自身）** —— 卡死首选 goroutine 快照，热点用 CPU profile：

```bash
# 卡死/慢调用首选：全量 goroutine 栈，直接看阻塞在哪个调用
curl -s "http://<pod>:6060/debug/pprof/goroutine?debug=2" -o goroutine.txt
grep -B2 -A8 'exec|io.Copy|http\.RoundTrip|sync\.Mutex' goroutine.txt

# CPU 热点（覆盖一次真实操作的采样窗口）
curl -s -o cpu.pprof "http://<pod>:6060/debug/pprof/profile?seconds=120"
go tool pprof -top -nodecount=30 cpu.pprof
go tool pprof -traces -cum cpu.pprof          # 谁调用了谁
go tool pprof -list '<可疑函数>' cpu.pprof     # 行级定位

# 锁竞争/阻塞
curl -s -o mutex.pprof "http://<pod>:6060/debug/pprof/mutex"
curl -s -o block.pprof  "http://<pod>:6060/debug/pprof/block"
```

> pprof 属调试手段，生产默认关闭，用构建开关（build tag）或独立调试镜像暴露，采样窗口必须覆盖一次真实 Reconcile。

**3b. APIServer / etcd 侧** —— 控制器"打爆集群"时看：

```bash
# 各请求 verb/资源 的耗时与错误
sum(rate(apiserver_request_duration_seconds_bucket[5m])) by (verb, resource)
# watch 数量与带宽（watch 过多=informer 设计问题）
sum(apiserver_watch_events_sizes_bytes) by (resource)
# etcd 延迟（控制器大量 List/写放大）
histogram_quantile(0.99, rate(etcd_request_duration_seconds_bucket[5m]))
```

**3c. 外部依赖侧** —— 存储/消息/HTTP 的自身指标 + 简单基准（复现脚本），区分"调用模式问题（子进程/短连接/无连接池）"vs"服务端性能问题（限流/规模/网络）"。

### Step 4 — 根因归类 → 对照通用缺陷清单修复（§5）

### Step 5 — 验证与回归

```text
1. 量化前后对比：同一指标（depth / work_duration P95 / 429 计数）修复前 vs 后
2. 灰度：先小流量/单个 CR 验证，再全量；新字段/新行为确认向后兼容
3. 回滚预案：每个改动明确回滚方式（配置改回 / 常量还原 / 字段不再写入）
4. 压测回归：构造事件风暴（批量建 CR / 升级 / 删 pod）验证不再复发
```

---

## 4. 通用调优手段库（按风险/收益排序）

> 排序原则：优先**低风险、低复杂度、长期收益高**；改 CRD API 放第二批；改架构放最后。

### 第一批：零风险（配置/常量，不改 API 不改核心路径）

| 手段 | 说明 | 适用症状 |
|------|------|----------|
| 提升 `MaxConcurrentReconciles` | 并行 worker 数。**先确认并发安全**（共享状态/全局锁） | T-1 队列堆积、queue_duration 高 |
| 定制 RateLimiter | 失败重试退避窗口（默认 5ms→10s→1000s，通常过猛） | T-1 失败风暴 |
| 失败路径 Result-only | `Result{RequeueAfter: delay}, nil`，避免 `RequeueAfter` + error 双重 requeue（rate limiter 叠加） | T-1 retries 高 |
| 周期 requeue 调整 + jitter | 收敛态 requeue 拉长 + 0~10% 抖动防锁步 | T-3 周期风暴 |
| Per-reconcile Context 超时 | 挂起的外部调用快速失败交退避，不占 worker 槽 | T-2 卡死、T-1 单 CR 挂起占满 |

### 第二批：低风险（CRD/API 加字段，向后兼容）

| 手段 | 说明 | 适用症状 |
|------|------|----------|
| `observedGeneration` + 已收敛短路 | 区分"spec 真变了"与"resync 入队"；收敛态跳过全量链路。**注意短路路径不写 status**（patch 会 bump resourceVersion 再次触发入队） | T-3 升级风暴（收益最大） |
| 指数退避 + 重试上限 + 人工武装 | 失败 CR 退避到上限后退出队列，运维排障后注解重新武装 | T-1 失败 CR 抢占队列 |
| Finalizer/status 用 Merge Patch | `Update` 遇并发写 → Conflict 整轮失败；patch 幂等 | T-5 conflict 风暴 |
| 事件驱动模式 | 收敛态只按事件调谐（`no-requeue`），去掉定时器 | T-3 周期风暴 |

### 第三批：中风险（核心路径优化）

| 手段 | 说明 |
|------|------|
| 外部依赖 SDK/连接池化 | 替代每次调用的子进程/新连接（连接复用 + 短 dial 超时 + 有界重试） |
| 批量化 | 多对象合并处理、批量 API（`Patch` 多个、bulk 存储操作） |
| 增量对比推送 | 全量同步（mirror/overwrite）改 GET-compare 跳过未变更对象 |
| 业务缓存/预取 | informer cache 之外的高频查询结果缓存（注意一致性） |
| 失败快速前置探测 | 长路径前先探测依赖可达性（短超时），不可达快速失败不逐调用硬等 |

### 第四批：高风险（架构级，评估后决定）

| 手段 | 说明 | 风险点 |
|------|------|--------|
| 拆分控制器 | 按 phase/职责拆多 controller（新建 vs 存量、配置 vs 容器） | 共享 reconciler 实例的状态竞争 |
| 事件合并（coalescing） | 高频事件合并成一次处理 | 语义复杂度 |
| 依赖异步化 | 长任务出队转异步 + 状态机推进 | 状态一致性 |
| 水平扩展 | 多副本 + leader election 只保证 HA 不保证扩容；需分片（按 namespace/标签 hash 到不同 controller 实例） | 分片迁移成本 |

---

## 5. 通用缺陷清单（对照自查，大多数"性能问题"是这些模式）

| # | 缺陷模式 | 典型后果 | 修复要点 |
|---|----------|----------|----------|
| G-01 | 无 `observedGeneration` | 重启/升级全量重调谐 | 加字段 + 收敛短路 |
| G-02 | 双重 requeue（Result + Error） | rate limiter 退避失控 | Result-only |
| G-03 | 失败无退避/无上限 | Failed CR 无限抢占队列 | 指数退避 + 上限 + 武装 |
| G-04 | Finalizer/Status 用 Update | conflict 风暴、整轮失败 | Merge Patch |
| G-05 | 无 per-reconcile 超时 | 外部依赖挂起占 worker 槽 | ctx.WithTimeout |
| G-06 | 并发=1 串行 | 单 CR 慢拖垮全部 | MaxConcurrentReconciles |
| G-07 | 外部调用子进程/无连接池 | 固定开销 × 调用次数放大 | SDK/连接池化 |
| G-08 | 周期 requeue 过短、无抖动 | 锁步风暴 | 拉长 + jitter |
| G-09 | 无观测（无耗时日志/指标/pprof） | 定位靠猜 | 本 SOP §3 |
| G-10 | Status 多次 patch 非原子 | patch 基线错乱、写放大 | 统一单次 patch |
| G-11 | 事件风暴（pod 阶段变化级联入队） | 级联重调谐 | 过滤 predicate / 事件合并 |
| G-12 | 全量重传（无增量对比） | 无用 I/O 放大 | GET-compare / 内容 hash |
| G-13 | 绕过 informer 直连 API | watch 与 API 负载双高 | 统一走 informer cache |
| G-14 | 全局共享锁串行 | 所有 CR 互相排队 | 锁粒度 / 无锁化 |
| G-15 | 水平扩展当扩容用 | 加了副本不加速 | 分片，leader 只保 HA |

---

## 6. 快速决策树

```
CR 不收敛 / 集群感知到控制器异常
│
├─ Step 0 指标：depth / queue_duration / work_duration / retries
│
├─ depth 高 & queue_duration 高 → 并发不足
│     └─ 提 MaxConcurrentReconciles（先查并发安全）→ 仍堵？
│          └─ 单 CR 挂起占 worker → per-reconcile 超时 + 退避
│
├─ work_duration 高 → 单次慢
│     └─ Step 2 打点拆解 → 找到最慢阶段
│          ├─ 外部依赖慢 → SDK/连接池/批量化/异步化（或依赖侧调优）
│          └─ 代码内慢 → O(N) 遍历/子进程/全局锁 → 对照 G-07/G-14
│
├─ adds 尖峰 → 事件风暴
│     └─ 升级后尖峰 → observedGeneration 短路（G-01）
│     └─ 平时尖峰 → predicate 过滤 / 事件合并（G-11）
│
├─ retries 高 → 失败风暴
│     └─ 双重 requeue（G-02）/ 无退避（G-03）
│
├─ rest_client 429 / apiserver 高负载 → 打爆集群
│     └─ 高频 requeue / 直连 API（G-13）/ Update 滥用（G-04）
│
└─ CPU/内存异常 → pprof：exec/io.Copy 卡死 / 锁竞争 / 泄漏
```

---

## 7. 排障检查清单（Checklist）

- [ ] 拿到基线：正常时段的 depth / work_duration P95 / retries（先量化再动刀）
- [ ] 区分队列堵 vs 处理慢（queue_duration vs work_duration）
- [ ] 单次耗时打点是否覆盖：成功/失败/取消三态 + 每阶段 elapsed
- [ ] 日志是否带 CR 级上下文（`grep "<cr-name>"` 覆盖全链路）
- [ ] 外部依赖是否量化（每调用阈值日志 / 依赖侧指标 / 复现基准）
- [ ] 是否漏掉"重启/升级后"场景（ObservedGeneration 短路验证）
- [ ] 失败路径是否退避有界、是否有重试上限、是否有运维武装手段
- [ ] 写回是否用 Patch 而非 Update、status 是否单次写
- [ ] 并发提升前是否确认共享状态安全（全局锁/缓存/并发写）
- [ ] 调优后是否量化对比 + 灰度 + 明确回滚方式
- [ ] pprof 采样窗口是否覆盖真实 Reconcile（不是空转采样）
- [ ] 新增状态字段是否 `omitempty` 向后兼容、CRD schema 是否已同步

---

## 8. 实例映射：HiClaw 控制器如何落地本方法论

本 SOP 第 1~7 节为通用方法。HiClaw 分支 `fix/team-cr-blocking-defects` 是对该方法的完整实践，可对照学习：

| 本 SOP 步骤/手段 | HiClaw 落地（提交） |
|------------------|---------------------|
| Step 0 指标先行 | 内置 `workqueue_*` + 新增 `hiclaw_storage_op_*`、`hiclaw_member_reconcile_duration_seconds`（77ef4b8） |
| Step 2 打点拆解 | 每步 timing 日志 + team-scoped logger + `timed` helper + `mc slow call` 阈值日志（c01aaec/ce1a531） |
| Step 3 pprof | `ENABLE_PPROF=true` 构建开关（6cf6f86）；bench_s3 复现基准区分"调用模式 vs 服务端"（a12c5b3） |
| G-01 observedGeneration 短路 | `TeamStatus.observedGeneration` + Active 快路径（48ce4aa） |
| G-02 双重 requeue | `failTeam`/`failHuman` Result-only + `failBackoffFor`（48ce4aa） |
| G-03 退避+上限+武装 | 30s→8m 指数退避、5 次封顶、`hiclaw.io/retry` 注解（48ce4aa） |
| G-04 Update→Patch | finalizer 增删 merge patch（48ce4aa） |
| G-05 per-reconcile 超时 | `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`（48ce4aa） |
| G-06 并发 | `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES`（c01aaec） |
| G-07 子进程→连接池 | minio-go SDK 驱动，bench 实测 ~5.8× 提速（95dcae1） |
| G-08 周期+jitter | `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` + 0~10% 抖动（462f84d）；`HICLAW_TEAM_ACTIVE_NO_REQUEUE` 事件驱动（84a9429） |
| G-12 全量重传 | builtin skill content-compare 增量推送（e620ba2） |
| 前置探测 | `probeStorage` 10s 快速失败（e620ba2） |

HiClaw 具体参数配置与运维动作见 [controller-tuning-sop.md](controller-tuning-sop.md)，缺陷逐项详情见 [team-controller-defect-fixes.md](team-controller-defect-fixes.md)。
