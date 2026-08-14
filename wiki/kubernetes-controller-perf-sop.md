# Kubernetes CRD 控制器（Operator）性能问题排查定位调优 SOP

> 版本: v2.0
> 日期: 2026-08-06
> 适用范围: 基于 controller-runtime / client-go 构建的任意 Kubernetes operator（CRD 控制器）
> 结构: 完整链路四段式 —— **现象 → 定位 → 常见问题的解法 → 推荐的实践**
> 定位: 通用方法论，不绑定具体项目参数；HiClaw 控制器为完整实例，见 [controller-tuning-sop.md](controller-tuning-sop.md)（实例参数）与 [team-controller-defect-fixes.md](team-controller-defect-fixes.md)（实例缺陷清单）

---

# 第一部分：现象（Symptoms）—— 识别与量化

## 1.1 现象清单（先对号入座）

| 组 | 编号 | 现象 | 常见误判 |
|----|------|------|----------|
| 收敛 | P-1 | CR 长期不收敛，Phase/Status 停滞或为空 | 以为是业务 bug，实际是队列被堵 |
| 收敛 | P-2 | 新建 CR 长时间无响应（无 Phase/无 Status 写入） | 以为是 CRD 问题，实际是 worker 全被占用 |
| 收敛 | P-3 | 滚动升级/重启后所有 CR 全量重调谐，耗时数小时 | 以为是并发问题，实际是缺 observedGeneration 短路 |
| 收敛 | P-4 | 周期调谐风暴：固定时间点所有 CR 同时唤醒 | 以为是集群问题，实际是 requeue 无抖动 |
| 耗时 | P-5 | 单次调谐分钟级，远超预期 | 以为网络慢，实际是外部调用模式问题（子进程/串行） |
| 耗时 | P-6 | 调谐偶发卡死数分钟后恢复 | 以为偶发抖动，实际是外部依赖超时逐调用叠加 |
| 耗时 | P-7 | 全局 CR 越多，单次调谐越慢 | 以为算法 O(N)，实际是共享依赖（存储/registry）规模退化 |
| 资源 | P-8 | CPU 持续高位 | 以为是业务计算，实际是子进程 exec / JSON 编解码风暴 |
| 资源 | P-9 | 内存持续膨胀 | 泄漏 / 无界缓存 / informer cache 过大 |
| 资源 | P-10 | goroutine 数量暴涨 | 泄漏 / 无超时的同步调用堆积 / 每调用起 goroutine |
| 集群 | P-11 | APIServer 被打爆，出现 429/限流 | 以为是集群容量，实际是控制器高频 requeue / 直连 API |
| 集群 | P-12 | etcd 延迟升高 | List 全量 / 写放大 / watch 风暴 |
| 稳定 | P-13 | 同一 CR 反复失败重试，日志刷屏 | 以为是外部故障，实际是无退避/双重 requeue |
| 稳定 | P-14 | Conflict 错误刷屏，reconcile 整轮失败 | 以为是并发写冲突，实际是 Update 滥用（该用 Patch） |
| 稳定 | P-15 | 多副本时 leader 频繁切换 / 只有一个在干活 | 期望是扩容，实际 leader election 只保证 HA |

## 1.2 现象量化（指标先行，零代码改动）

controller-runtime 自带 `/metrics`（`sigs.k8s.io/controller-runtime/pkg/metrics`），排查第一步先拿这些基线：

| 指标 | 含义 | 判读 |
|------|------|------|
| `workqueue_depth` | 队列积压量 | 持续高位且不回落 = 处理跟不上入队 |
| `workqueue_adds_total` | 累计入队数（看 rate） | 尖峰 = 事件风暴 |
| `workqueue_queue_duration_seconds` | 入队→出队等待 | 高 = 并发不足（排队久） |
| `workqueue_work_duration_seconds` | 单次 reconcile 实际耗时 | 高 = 处理本身慢 |
| `workqueue_retries_total` | 失败重试次数 | 高 = 错误模式（无退避/双重 requeue） |
| `rest_client_requests_total` | 对 APIServer 调用量 | 异常增长 = API 滥用 |
| `rest_client_rate_limiter_duration_seconds` | 被 APIServer 限流的等待 | >0 = 已触发 429 保护 |
| `leader_election_master_status` | 本副本是否为 leader | 多副本时判断谁在干活 |
| `process_*` | Go 运行时 CPU/内存/goroutine | 资源类现象直接读数 |

```bash
# 三个核心查询：积压 / 单次耗时 P95 / 入队速率
sum(workqueue_depth{name=~"<controller>"})
histogram_quantile(0.95, sum(rate(workqueue_work_duration_seconds_bucket[5m])) by (le))
sum(rate(workqueue_adds_total[5m])) by (name)
```

**原则**：对比"异常时段 vs 基线时段"比看绝对值更有用。没有基线的第一件事是**建立基线**（记录当前值，之后才有对比对象）。

## 1.3 现象 → 指标映射

| 现象 | 看什么指标 | 判定 |
|------|-----------|------|
| P-1/P-2 不收敛/新建无响应 | `workqueue_depth` | 高位 = 队列堵；为 0 = 根本没入队（informer/标签问题） |
| P-3 升级风暴 | 升级时刻 `workqueue_adds_total` | 全量尖峰 = 无 observedGeneration 短路 |
| P-5/P-6 单次慢/卡死 | `workqueue_work_duration` | 高 = 处理慢，进第二部分拆解 |
| P-8/P-10 CPU/goroutine 高 | `process_cpu_seconds_total`、`go_goroutines` | 结合 pprof 定位 |
| P-11/P-12 打爆集群 | `rest_client_requests_total`、429 计数 | 高频 requeue / API 滥用 |
| P-13 反复失败 | `workqueue_retries_total` | 高 = 失败风暴，查退避与错误处理 |

---

# 第二部分：定位（Diagnosis）—— 从现象到根因

## 2.1 五步定位法

```
Step 0  量化基线       拿到指标（§1.2），确认现象真实存在、锁定方向
   │
Step 1  队列侧二分     深度高时：queue_duration 高 → 并发不足
   │                  work_duration 高 → 单次慢
   │                  retries 高 → 失败风暴
   │                  adds 尖峰 → 事件风暴
   │
Step 2  耗时拆解       单次慢 → 代码路径打点，找到占比最大的阶段
   │
Step 3  深挖根因       pprof（自身）/ APIServer·etcd（集群）/ 依赖侧（外部）
   │
Step 4  根因归类       对照常见问题模式表（§3.1），确定解法
   │
Step 5  验证回归       同指标前后对比 + 灰度 + 回滚预案
```

## 2.2 Step 2 关键动作：耗时打点（成败都要有 elapsed）

```go
// 1. Reconcile 入口注入 request 级 logger，日志全链路可 grep
ctx = log.IntoContext(ctx, log.FromContext(ctx).WithValues(
    "cr", req.NamespacedName.String(), "uid", string(cr.UID)))

// 2. 统一 timed helper：成功/失败/ctx 取消三态都记录
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

// 3. 外部依赖每调用打阈值日志（如 >300ms），量化依赖层延迟分布
// 4. 顶层 panic 兜底：recover 后带上下文记日志，按错误 requeue
```

拆解模板（每个业务阶段一个 `timed`）：

```
resolve-identity(200ms) → create-resources(1.2s) → ensure-storage(300ms)
→ per-item × N: infra(220ms) + config(1.5~2s) + container(100ms+) + expose(200ms)
```

**判断原则**：找占比最大阶段下钻。慢通常集中在外部依赖（存储/消息/HTTP）；代码内先排查 O(N) 全局遍历、子进程调用、全局锁。

## 2.3 Step 3 关键动作：三侧深挖

**3a. 控制器自身（pprof）** —— 卡死首选 goroutine 快照，热点用 CPU profile：

```bash
# 卡死/慢调用首选：全量 goroutine 栈，直接看阻塞在哪
curl -s "http://<pod>:6060/debug/pprof/goroutine?debug=2" -o goroutine.txt
grep -B2 -A8 'exec|io\.Copy|http\.RoundTrip|sync\.Mutex' goroutine.txt

# CPU 热点（采样窗口必须覆盖一次真实操作）
curl -s -o cpu.pprof "http://<pod>:6060/debug/pprof/profile?seconds=120"
go tool pprof -top -nodecount=30 cpu.pprof
go tool pprof -traces -cum cpu.pprof           # 谁调用了谁
go tool pprof -list '<可疑函数>' cpu.pprof      # 行级定位

# 锁竞争/阻塞（需开启 block/mutex 采样率）
curl -s -o mutex.pprof "http://<pod>:6060/debug/pprof/mutex"
curl -s -o block.pprof  "http://<pod>:6060/debug/pprof/block"
```

**3b. 集群侧（APIServer/etcd）** —— 控制器"打爆集群"时：

```bash
sum(rate(apiserver_request_duration_seconds_bucket[5m])) by (verb, resource)
sum(apiserver_watch_events_sizes_bytes) by (resource)   # watch 带宽
histogram_quantile(0.99, rate(etcd_request_duration_seconds_bucket[5m]))
```

**3c. 依赖侧** —— 用简单复现基准（对真实数据只读）区分"调用模式问题"vs"服务端问题"：同负载分别用"子进程/短连接"与"SDK/连接池"跑，服务端快慢立现。

## 2.4 Step 4 根因归类判定表

| 定位证据 | 根因归类 | 解法入口 |
|----------|----------|----------|
| queue_duration 高、work 正常 | 并发不足 | §3.2 C-1 |
| work_duration 高、外部调用占比大 | 依赖调用模式差 | §3.2 C-7 |
| retries 高、退避节奏乱 | 失败处理缺陷 | §3.2 C-2/C-3 |
| adds 升级后尖峰 | 无短路 | §3.2 C-1 |
| conflict 刷屏 | Update 滥用 | §3.2 C-4 |
| goroutine 卡在 exec/io | 子进程/无超时 | §3.2 C-5/C-7 |
| 全局锁排队 | 共享串行点 | §3.2 C-8 |

---

# 第三部分：常见问题的解法（Common Problems & Fixes）

## 3.1 常见问题模式 → 解法对照表

| # | 常见问题（模式） | 典型现象 | 解法（要点） | 风险 |
|---|------------------|----------|--------------|------|
| C-1 | 无 observedGeneration、无收敛短路 | P-3 升级全量重调谐 | 加字段；`Generation==ObservedGeneration` 且已收敛 → 跳过全量链路；**短路路径不写 status**（patch 会 bump resourceVersion 再触发入队） | 低 |
| C-2 | 双重 requeue（Result.RequeueAfter + error） | P-13 重试节奏失控 | 失败路径统一 `Result{RequeueAfter: delay}, nil`（Result-only），退避显式控制，不触发 rate limiter | 低 |
| C-3 | 失败无退避/无上限 | P-13 失败 CR 无限抢占队列 | 指数退避（如 30s→8m 封顶）+ 连续失败 N 次后停重试 + 运维注解重新武装；**退避守卫**防止 status patch 触发 informer 立即重入 | 低 |
| C-4 | Finalizer/Status 用 Update | P-14 conflict 风暴 | 改 Merge Patch（并发写不再整轮失败）；status 字段用 `Status().Patch()`，spec+status 同改需两次写 | 低 |
| C-5 | 无 per-reconcile 超时 | P-6 外部依赖挂起占 worker 槽 | Reconcile 入口 `context.WithTimeout`（可配置，默认关保持兼容） | 低 |
| C-6 | 并发=1 串行 | P-1/P-2 单 CR 拖垮全部 | `MaxConcurrentReconciles` 提升（**先确认共享状态并发安全**） | 低 |
| C-7 | 外部调用子进程/无连接池 | P-5 固定开销×调用次数放大 | SDK/连接池化（连接复用 + 短 dial 超时 + 有界重试） | 中 |
| C-8 | 全局共享锁串行 | P-7 所有 CR 互相排队 | 锁粒度细化 / 无锁化 / 读写分离 | 中 |
| C-9 | 周期 requeue 过短、无抖动 | P-4 锁步风暴 | 收敛态 requeue 拉长 + 0~10% 正向抖动 | 低 |
| C-10 | 事件风暴（pod 阶段级联入队） | P-3/P-11 级联重调谐 | 过滤 predicate（只看关心字段变化）/ 事件合并 | 中 |
| C-11 | 全量重传（无增量对比） | P-5 无用 I/O 放大 | GET-compare / 内容 hash 跳过未变更对象 | 中 |
| C-12 | 绕过 informer 直连 API | P-11/P-12 双重负载 | 统一走 informer cache；List 加 field/label selector 收窄 | 低 |
| C-13 | 无观测（无耗时日志/指标/pprof） | 一切"说不清" | 内置观测（见 §4.1），从第一天就加 | 低 |
| C-14 | Status 多次 patch 非原子 | 写放大、patch 基线错乱 | 统一单次 patch（defer 收口） | 中 |
| C-15 | 水平扩展当扩容用 | P-15 加副本不加速 | leader election 只保 HA；扩容需分片（按 namespace/标签 hash） | 高 |

## 3.2 解法实施顺序（按风险/收益）

| 批次 | 内容 | 预期收益 | 风险 |
|------|------|----------|------|
| 第一批（改配置/常量） | C-2、C-5、C-6、C-9 | 解队列堵塞、失败风暴，吞吐 5× | 零风险 |
| 第二批（CRD 加字段） | C-1、C-3、C-4 | 升级风暴消除（15000×）、失败归零 | 低风险，向后兼容 |
| 第三批（核心路径） | C-7、C-8、C-11、C-12 | 单次耗时数量级下降 | 中风险 |
| 第四批（架构） | C-10、C-14、C-15 | 极端场景兜底 | 高风险，评估后做 |

## 3.3 典型解法代码要点

**C-3 退避守卫**（关键细节：status patch 会触发 informer 立即重入，必须拦截）：

```go
// Reconcile 入口，deletion 处理之后：
if cr.Status.Phase == "Failed" && !cr.Status.MaxRetriesReached &&
    cr.Status.ConsecutiveFailures > 0 && cr.Status.PhaseTransitionTime != nil {
    if time.Since(cr.Status.PhaseTransitionTime.Time) < failBackoffFor(cr.Status.ConsecutiveFailures) {
        return reconcile.Result{}, nil // 窗口内直接丢弃，等原 RequeueAfter 唤醒
    }
}
```

**C-4 重试武装语义**（退避达上限后 CR 退出队列，排障完人工武装）：

```bash
kubectl annotate <cr-kind> <name> hiclaw.io/retry=""
```

守卫需放在 deletion 处理**之前**的判定、但返回逻辑保证被冻结 CR 仍可正常删除。

---

# 第四部分：推荐的实践（Best Practices）

## 4.1 设计实践（写控制器时从第一天就做）

| # | 实践 | 说明 | 防什么 |
|---|------|------|--------|
| B-1 | `observedGeneration` 标配 | 每个 CRD 的 Status 都带，成功 pass 写入 | C-1 升级风暴 |
| B-2 | 收敛短路 + 事件驱动 | 已收敛且 spec 未变的 CR 跳过全量链路；必要时彻底去定时器（no-requeue） | C-1/C-9/C-10 |
| B-3 | 失败三件套 | 指数退避 + 重试上限 + 运维武装注解 | C-3 失败风暴 |
| B-4 | Result-only 错误处理 | 失败路径永不返回 `RequeueAfter`+error 组合 | C-2 |
| B-5 | Patch 优先 | finalizer/status 用 merge patch；spec+status 同改分两次写 | C-4/C-14 |
| B-6 | 每 Reconcile 有超时 | 可配置 per-pass deadline，挂起快速失败交退避 | C-5 |
| B-7 | 并发安全先行 | 提并发前审计共享状态（全局锁/缓存/并发写） | C-6/C-8 |
| B-8 | 依赖连接池化 | SDK 直连 + 连接复用 + 短 dial 超时 + 有界重试；绝不每调用起子进程 | C-7 |
| B-9 | 前置可达性探测 | 长路径前短超时探测依赖，不可达快速失败 | C-5/C-7 变体 |
| B-10 | 增量对比替代全量同步 | 推送类操作 GET-compare / hash 跳过未变更 | C-11 |
| B-11 | 事件过滤收窄 | predicate 只监听关心字段；watch 走 informer，不直连 API | C-10/C-12 |
| B-12 | 观测内置 | 指标 + 耗时日志 + pprof 从第一天进代码（不是出问题才加） | C-13 |

## 4.2 编码实践（模板）

```go
// Reconcile 骨架（推荐的完整模式）
func (r *Reconciler) Reconcile(ctx context.Context, req reconcile.Request) (res reconcile.Result, err error) {
    logger := log.FromContext(ctx)
    start := time.Now()
    defer func() { metrics.Observe("kind", start, err) }()          // B-12 指标
    defer func() { if p := recover(); p != nil { err = fmt.Errorf("panic: %v", p) } }() // B-12 panic 兜底

    ctx = log.IntoContext(ctx, logger.WithValues("cr", req.String())) // B-12 日志上下文
    if r.Timeout > 0 {                                               // B-6 超时
        var cancel context.CancelFunc
        ctx, cancel = context.WithTimeout(ctx, r.Timeout)
        defer cancel()
    }
    // 取对象 → 失败守卫（C-3）→ deletion/finalizer（Patch，B-5）→ 收敛短路（B-2）→ 业务
    // 业务内部每阶段 timed(...)（B-12）
    return
}

// 失败路径：Result-only（B-4）
func (r *Reconciler) fail(ctx context.Context, cr *MyCR, msg string) (reconcile.Result, error) {
    cr.Status.ConsecutiveFailures++
    if cr.Status.ConsecutiveFailures > maxRetries {
        cr.Status.MaxRetriesReached = true
        _ = r.Status().Patch(ctx, cr, client.MergeFrom(cr.DeepCopy()))
        return reconcile.Result{}, nil
    }
    return reconcile.Result{RequeueAfter: failBackoffFor(cr.Status.ConsecutiveFailures)}, nil
}
```

## 4.3 运维实践

| # | 实践 | 说明 |
|---|------|------|
| O-1 | 基线留存 | 上线即记录正常时段指标（depth/耗时 P95/retries），排障才有对比 |
| O-2 | 告警先行 | 对 workqueue_depth、retries、429 计数、外部依赖错误配告警，故障早于用户感知 |
| O-3 | 灰度升级 | 先小流量/单 CR 验证新镜像，确认向后兼容（新字段 omitempty）再全量 |
| O-4 | 升级注意 CRD | `helm upgrade` 不更新已安装 CRD；schema 变更需显式 apply；先核对集群 CRD 实际字段 |
| O-5 | 回滚预案 | 每个调优项明确回滚方式（配置还原/常量还原/字段停止写入） |
| O-6 | 风暴压测回归 | 升级、批量建 CR、删 pod 三类风暴场景在每次改动后回归 |
| O-7 | pprof 只进调试镜像 | 构建开关（build tag）控制，发布镜像零调试面 |
| O-8 | 排障留痕 | 每轮排障产出：症状、指标证据、根因、解法、验证结果（本文档 §2 流程的产物） |

---

# 附：快速决策树（一页速查）

```
CR 不收敛 / 集群感知到控制器异常
│
├─ 看指标：depth / queue_duration / work_duration / retries / 429
│
├─ depth 高 & queue_duration 高 → 并发不足 → C-6（先审计并发安全）
│     └─ 仍堵？→ 单 CR 挂起占 worker → C-5 超时 + C-3 退避
│
├─ work_duration 高 → 单次慢 → 打点拆解（§2.2）
│     ├─ 外部依赖占比大 → C-7 连接池化 / B-9 前置探测 / 依赖侧调优
│     └─ 代码内慢 → O(N) 遍历 / 子进程 / 全局锁 → C-7/C-8
│
├─ adds 升级后尖峰 → C-1 observedGeneration 短路
├─ adds 平时尖峰 → C-10 predicate 过滤 / 事件合并
├─ retries 高 → C-2 Result-only + C-3 退避上限
├─ 429 / apiserver 高负载 → C-12 走 informer / C-4 Patch / 降 requeue 频率
└─ CPU/内存异常 → pprof：exec/io.Copy 卡死 / 锁竞争 / 泄漏（§2.3）
```

---

# 附：排障检查清单

- [ ] 拿到基线（异常 vs 正常时段对比），不是只看绝对值
- [ ] 区分队列堵 vs 处理慢（queue_duration vs work_duration）
- [ ] 打点覆盖成功/失败/取消三态，日志带 CR 级上下文
- [ ] 外部依赖量化（阈值日志 / 依赖侧指标 / 只读复现基准）
- [ ] 覆盖"重启/升级后"场景（observedGeneration 短路验证）
- [ ] 失败路径：退避有界、有上限、有运维武装手段、有退避守卫
- [ ] 写回用 Patch、status 单次写
- [ ] 并发提升前审计共享状态
- [ ] 调优后同指标量化对比 + 灰度 + 明确回滚
- [ ] 新增状态字段 omitempty + CRD schema 已同步

---

## 实例映射：HiClaw 控制器如何落地本 SOP

| 本 SOP 条目 | HiClaw 落地（提交） |
|-------------|---------------------|
| §1.2 指标先行 | 内置 `workqueue_*` + 新增 `hiclaw_storage_op_*`、`hiclaw_member_reconcile_duration_seconds`（77ef4b8） |
| §2.2 打点拆解 | 每步 timing 日志 + team-scoped logger + `timed` helper + `mc slow call` 阈值日志（c01aaec/ce1a531） |
| §2.3 pprof / 复现基准 | `ENABLE_PPROF=true` 构建开关（6cf6f86）；bench_s3 区分"调用模式 vs 服务端"（a12c5b3） |
| C-1 observedGeneration 短路 | `TeamStatus.observedGeneration` + Active 快路径（48ce4aa） |
| C-2 双重 requeue | `failTeam`/`failHuman` Result-only + `failBackoffFor`（48ce4aa） |
| C-3 退避+上限+武装 | 30s→8m 指数退避、5 次封顶、`hiclaw.io/retry` 注解（48ce4aa） |
| C-4 Update→Patch | finalizer 增删 merge patch（48ce4aa） |
| C-5 per-reconcile 超时 | `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`（48ce4aa） |
| C-6 并发 | `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES`（c01aaec） |
| C-7 子进程→连接池 | minio-go SDK 驱动，bench 实测 ~5.8× 提速（95dcae1） |
| C-9 周期+jitter | `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` + 0~10% 抖动（462f84d）；`HICLAW_TEAM_ACTIVE_NO_REQUEUE` 事件驱动（84a9429） |
| C-11 全量重传 | builtin skill content-compare 增量推送（e620ba2） |
| B-9 前置探测 | `probeStorage` 10s 快速失败（e620ba2） |
| B-12 观测内置 | 上述全部观测提交即"出问题前已内置"的实践 |

HiClaw 具体参数配置与运维动作见 [controller-tuning-sop.md](controller-tuning-sop.md)，缺陷逐项详情见 [team-controller-defect-fixes.md](team-controller-defect-fixes.md)。
