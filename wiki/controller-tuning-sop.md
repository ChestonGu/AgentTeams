# HiClaw 控制器调优指南 SOP

> 版本: v1.1
> 日期: 2026-08-07
> 基线: 分支 `fix/team-cr-blocking-defects`（含 `48ce4aa` 起的 Team/Human 调谐解堵、S3 SDK 存储驱动、可观测性改造；storage 层超时/重试参数已环境变量化）
> 关联文档: [team-controller-defect-fixes.md](team-controller-defect-fixes.md)（缺陷与优化总结）
> 调研底稿（本地 .issue/ 笔记，未入库）: team-controller-defects.md（缺陷清单）、team-controller-performance.md（性能定位）、team-controller-troubleshooting.md（耗时定位与观测）

---

## 0. 适用范围与目标

本 SOP 面向 **hiclaw-controller**（Kubernetes operator）的 Team / Human / Worker / Manager 调谐（reconcile）环节，提供从**症状识别 → 日志/指标定位 → 参数调优 → 深度诊断 → 运维操作**的标准作业流程。

调优的本质是回答三个问题：

1. **队列为什么堵**（并发不足 / 单 CR 挂起 / Failed 抢占）
2. **单次调谐为什么慢**（OSS 子进程 / 存储性能 / 外部依赖延迟）
3. **什么该被调**（并发、超时、周期、存储驱动、退避策略）

---

## 1. 调优参数总表（本分支引入）

所有参数为控制器容器环境变量，修改后需重启控制器 Pod（Deployment 滚动更新）。

| 环境变量 | 默认 | 语义 | 建议 |
|----------|------|------|------|
| `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` | `1`（串行） | Team 调谐并发 worker 数 | 慢/挂起 Team 阻塞全队列时调大（建议 3~5） |
| `AGENTTEAMS_WORKER_MAX_CONCURRENT_RECONCILES`（agentteams 分支；helm 值 `controller.workerTuning.maxConcurrentReconciles`） | `1`（串行） | Worker 调谐并发 worker 数。Worker controller 为每个 Worker CR 跑完整成员供给链（Matrix 账号/房间、OSS 配置推送、容器创建），是全系统最重的调谐，也是各 Team Active 判定的依赖 | Worker 数量多、成批扩容时调大（建议 3~5）；单个挂起 Worker 阻塞全队列时立即调大 |
| `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS` | `0`（关闭） | 单次调谐超时上限 | 外部依赖（OSS/Matrix）偶发挂起时开启（如 300~600） |
| `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` | `300`（5min） | 已收敛 Active Team 的周期 requeue（带 0~10% 正向抖动） | 减少无谓调谐时可调大（600~1800） |
| `HICLAW_TEAM_ACTIVE_NO_REQUEUE` | `false` | 已收敛且 spec 未变的 Active Team 只按事件调谐，不再周期 requeue | 需要极低队列负载时置 `true` |
| `HICLAW_STORAGE_DRIVER` | `sdk` | 对象存储驱动：`sdk`（minio-go 连接池）\| `mc`（mc 子进程）。同时决定 embedded MinIO 的 admin provider（用户/策略管理）：`sdk` 用 madmin-go Admin API，`mc` fork `mc admin` | 默认 `sdk`；`mc` 仅作回滚/对比 |
| `HICLAW_STORAGE_CONNECT_TIMEOUT_SECONDS` | `2` | 单次 TCP/TLS 连接超时（秒）。连接池只复用存活连接，池未命中即重新 dial——此值正是该场景的硬上限 | 端点偶发慢时适当调大（如 5），长期不可达保持小值快速失败 |
| `HICLAW_STORAGE_RETRY_WINDOW_SECONDS` | `30` | 单次存储操作对瞬时错误（dial 超时/连接重置/5xx）的总重试窗口（秒）；确定性问题（4xx、key 不存在）不重试 | 短 OSS blip 会在窗口内自愈，调谐不失败；端点稳定可调小 |
| `HICLAW_STORAGE_RETRY_BACKOFF_MS` | `500` | 重试初始退避（毫秒），每轮翻倍至上限 | 一般不动 |
| `HICLAW_STORAGE_RETRY_BACKOFF_MAX_MS` | `5000` | 单次退避上限（毫秒） | 一般不动 |
| `HICLAW_STORAGE_SDK_MAX_RETRIES` | `2` | minio-go 内部自动重试次数（外层重试窗口为主，内层仅覆盖立即重试） | 一般不动 |
| `HICLAW_STORAGE_PROBE_TIMEOUT_SECONDS` | `30` | config 阶段前置存储可达性探测上限（秒）；默认与重试窗口一致，短 blip 在窗口内自愈后继续调谐 | 与重试窗口联动调整 |
| `HICLAW_FS_ACCESS_KEY` / `HICLAW_FS_SECRET_KEY` | 无 | 云 OSS 静态长效凭据（驱动无关） | 外部 OSS 无 STS sidecar 时配置 |
| `HICLAW_PPROF_ADDR` | `0.0.0.0:6060` | pprof 监听地址 | 仅调试镜像生效（见 §5） |
| `ENABLE_PPROF`（构建 arg） | `false` | 是否以 `-tags pprof` 编译控制器 | 仅调试镜像置 `true`，发布镜像必须保持关闭 |

### 内置常量（不可配置）

| 常量 | 值 | 位置 |
|------|-----|------|
| `maxTeamRetries` / `maxHumanRetries` | `5` | team_controller.go / human_controller.go |
| `maxFailBackoff` | `10min` | team_controller.go |
| 失败退避表 | 30s → 1m → 2m → 4m → 8m（封顶 10m） | `failBackoffFor` |
| 重试武装注解 | `hiclaw.io/retry`（Team 与 Human 共用） | Reconcile 入口守卫 |
| mc 慢调用日志阈值 | 300ms | minio.go（`mcSlowCallThreshold`） |

> storage 层的连接超时 / 重试窗口 / 退避 / SDK 重试次数 / 探测超时已升级为环境变量（见上表 `HICLAW_STORAGE_*`），默认值即原内置常量，无需改动即可保持既有行为。

---

## 2. 症状识别（先分类，再动手）

| 编号 | 症状 | 根因方向 | 对应调优 |
|------|------|----------|----------|
| S-1 | 新建 Team 长时间 `Phase=""` 或停滞 Pending | 并发=1 时队列被慢/挂起 Team 占满；或 CR 未被控制器看见（informer 标签/ControllerName 变更） | `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES`；核对 `hiclaw.io/controller` 标签与 leader lease |
| S-2 | 单次调谐耗时异常（Step 4 分钟级） | OSS 层 mc 子进程风暴 / 存储端点变慢（team 数正反馈） | `HICLAW_STORAGE_DRIVER=sdk`；核对存储端点（VPC 内网优先） |
| S-3 | 控制器滚动升级后全量调谐风暴 | TeamStatus 缺 `observedGeneration` 时无法区分 spec 变化与 re-sync 入队（本分支已修复）；周期 requeue 过短 | 升级到含 `48ce4aa` 的镜像；`HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` / `HICLAW_TEAM_ACTIVE_NO_REQUEUE` |
| S-4 | Failed Team 每 30s 抢占队列 | failTeam 无退避、无上限（本分支已修复为指数退避 + 5 次封顶） | 确认 `maxRetriesReached` 生效；排障后 `hiclaw.io/retry` 重新武装 |
| S-5 | 调谐偶发卡死数分钟 | 存储端点不稳定：mc 默认 30s dial 超时逐调用叠加；无单次调谐超时 | `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`；存储侧排查（限流/网络） |
| S-6 | 控制器 CPU/内存异常 | mc 子进程 exec 风暴；锁竞争；GC 抖动 | pprof 采样定位（§5）；考虑 `sdk` 驱动 |

---

## 3. 日志定位（零改动，现行版本即可用）

本分支起所有调谐日志带 **team 上下文**（`team=<name>`、`teamUID=<uid>`），且每步有 elapsed，一条 grep 覆盖整条链路：

```bash
# 1. 某 Team 的完整调谐链路（含下游 deployer/oss/gateway 层）
kubectl logs -n <ns> deployment/hiclaw-controller --tail 5000 \
  | grep "team=<team-name>"

# 2. 只看耗时记录（步骤级 + 成员级 + OSS 慢调用）
kubectl logs -n <ns> deployment/hiclaw-controller --tail 5000 \
  | grep "team=<team-name>" \
  | grep -E "team reconcile: step|member reconcile:|timed-call|mc slow call|deploy worker config"

# 3. 失败/退避/重试上限（判断 S-4）
kubectl logs -n <ns> deployment/hiclaw-controller --tail 5000 \
  | grep -E "phase transition: Failed|max retries reached|backing off"
```

### 日志关键行释义

| 日志 | 含义 |
|------|------|
| `team reconcile: step N <阶段> elapsed=<d>` | 各步骤墙钟耗时，直接暴露慢步骤 |
| `mc slow call cmd=<…> op=<op> elapsed=<d>` | 单次 OSS 调用 >300ms（op: get/put/stat/list…），量化 S3 层延迟分布 |
| `timed-call <phase> elapsed=<d>` | 统一计时：成功/失败/取消均记录（失败路径不再无日志返回） |
| `member reconcile: <phase> failed elapsed=<d>` | 成员 5 子阶段失败且带耗时 |
| `team healthy, skipping full reconcile` | Active + spec 未变的短路快路径命中 |
| `reconcile panic …` | panic 兜底，带 team 上下文并按错误路径 requeue |

---

## 4. 指标核对（Prometheus）

控制器暴露的稳定性/调谐指标（`/metrics`）：

| 指标 | 维度 | 判读 |
|------|------|------|
| `hiclaw_reconcile_duration_seconds` / `_total` / `_errors_total` | kind | 各 CRD 调谐频率与错误率基线 |
| `hiclaw_storage_op_duration_seconds` | op × driver | get/put 分位数是存储端点健康度的直接读数；尾部上升 ≈ config 阶段卡顿 |
| `hiclaw_storage_op_errors_total` | op × driver × class（network/timeout/not_found/other） | **network/timeout 持续上升 = 存储端点不稳定告警**（S-5 主证据） |
| `hiclaw_storage_probe_failures_total` | — | config 阶段前置探测快速失败次数（存储不可达的快速信号） |
| `hiclaw_member_reconcile_duration_seconds` | kind（team/worker）× result | 单成员全流程耗时；优化存储驱动/技能推送后应明显下降 |

查询示例：

```bash
# 存储延迟 P95 趋势（op=get, driver=sdk）
rate(hiclaw_storage_op_duration_seconds_bucket{op="get",driver="sdk",le="+Inf"}[5m])
# 网络类错误
sum(rate(hiclaw_storage_op_errors_total{class="network"}[5m])) by (op, driver)
```

---

## 5. 深度诊断：pprof 采样（构建时开关）

> 需要先以调试参数重新构建镜像，**仅限调试环境**。

```bash
# 构建 pprof 调试镜像（远端构建机）
make build-hiclaw-controller DOCKER_BUILD_ARGS="--build-arg ENABLE_PPROF=true"

# 部署后采样：port-forward + curl，数据落本地
kubectl port-forward -n <ns> <controller-pod> 6060:6060 &
curl -s -o /tmp/cpu.pprof  "http://127.0.0.1:6060/debug/pprof/profile?seconds=120"
curl -s -o /tmp/goroutine.txt "http://127.0.0.1:6060/debug/pprof/goroutine?debug=2"
curl -s -o /tmp/heap.pprof "http://127.0.0.1:6060/debug/pprof/heap"
curl -s -o /tmp/block.pprof "http://127.0.0.1:6060/debug/pprof/block"
curl -s -o /tmp/mutex.pprof "http://127.0.0.1:6060/debug/pprof/mutex"
kill %1
```

### 采样窗口必须覆盖真实调谐（触发方式）

| 触发方式 | 效果 | 适用 |
|----------|------|------|
| `kubectl delete pod <member-pod>` | 短路检查失败 → 走全量调谐，不重建容器 | **存量调谐热点（首选）** |
| 创建临时 Team | 全新首次收敛 | 建队耗时分析 |
| 改 spec（如 leader.env） | generation++ → 全量 + 容器重建 | 镜像/容器阶段 |
| 重启控制器 | re-sync 全量入队；已收敛 Team 走短路（秒级） | 不适合采 Step4 热点 |

### 常见热点 profile 特征

| 症状 | profile 特征 | 指向 |
|------|--------------|------|
| `os/exec` + syscall 频繁 | CPU profile 大量 exec 帧 | mc 子进程风暴（改用 sdk 驱动） |
| goroutine 阻塞在 `exec.(*Cmd).Wait` / `io.Copy` | goroutine.txt 同类栈成百上千 | mc 子进程 / docker `ensureImage` 卡死 |
| `sync.(*Mutex).Lock` 排队 | mutex.pprof 高持有时间 | `LegacyCompat.mu` registry 串行 |
| goroutine 阻塞在 HTTP roundtrip | net/http 等待帧 | Matrix / Higress / MinIO 网络往返 |

---

## 6. S3 层基准复现（bench_s3）

回答"瓶颈在调用模式还是 S3 服务端"：

```bash
cd bench && go mod tidy
# mc vs sdk 双驱动、对齐 config 阶段操作配比（12 GET + 3 PUT + 2 STAT + 1 LIST）
go run bench_s3.go -endpoint <endpoint> -ak <ak> -sk <sk> \
  -bucket <bucket> -prefix agents/ -mc <mc-path> -alias bench \
  -drivers mc,sdk -workers 1,5 -rounds 10

# 只读探测（不改动桶任何数据）
go run bench_s3.go ... -write=false

# 规模对比：同一命令换 -bucket/-prefix 指向不同规模桶，比对 CSV 的 background 列
```

判读：`sdk` 驱动显著优于 `mc` 且两者都随规模变慢 → 服务端性能问题（限流/对象规模），调存储侧；`sdk` 优但 `mc` 是主要差距 → 调用模式问题，保持 `sdk`。

---

## 7. 调优决策流程（决策树）

```
S-1 新建 Team 停滞 / S-3 调谐风暴
  └─> 并发是否 =1 且存在慢/挂起 Team？
       是 ──> HICLAW_TEAM_MAX_CONCURRENT_RECONCILES=3~5，重启控制器
       否 ──> 核对 CR 是否带 hiclaw.io/controller=<ControllerName> 标签
              （kubectl apply 的手写 YAML 不会自动打标签，REST API 会）
       仍堵 ──> kubectl get lease 核对 leader 持有者；检查 informer 报错

S-2 单次调谐 Step4 慢（分钟级）
  └─> 日志确认 config 阶段 mc slow call 密集？
       是 ──> HICLAW_STORAGE_DRIVER=sdk（默认已是）
       仍慢 ──> 存储端点问题：云 OSS 优先 VPC 内网 endpoint；
                核对对象规模（team 数 × 每成员几十个小对象）
       需要量化 ──> bench_s3 + storage_op_duration 指标

S-5 调谐偶发卡死（数分钟硬等待）
  └─> 存储不稳定（storage_op_errors class=network/timeout 上升）
       ──> 开 HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS（如 600），
           让单次 pass 快速失败交给退避，而不是无限占 worker 槽
       ──> 存储侧排查：限流（E8）、公网→内网、SeaweedFS 降级评估

S-4 Failed 抢占 / 反复失败
  └─> 已自动处理：指数退避 30s→8m + 5 次封顶
       ──> 排障完成后 kubectl annotate team <name> hiclaw.io/retry=""
       ──> Human 同理：kubectl annotate human <name> hiclaw.io/retry=""

S-6 CPU/内存异常
  └─> pprof 采样（§5）定位；核对是否误用 mc 驱动 / LegacyCompat 串行
```

---

## 8. 运维操作手册

### 8.1 重试武装（Failed/卡死 CR 恢复）

```bash
# Team 连续失败达上限（status.maxRetriesReached=true）后，排障完成重新武装：
kubectl annotate team <name> hiclaw.io/retry=""
# Human 同理：
kubectl annotate human <name> hiclaw.io/retry=""
# 控制器下一轮 reconcile 清除注解并重置 ConsecutiveFailures=0，恢复正常重试
```

### 8.2 CRD schema 核对（字段写不进去时）

> 注意：`helm upgrade` 不会更新已安装的 CRD，`crds/` 目录只在首次 `helm install` 应用。

```bash
# 核对集群 CRD 实际 schema
kubectl get crd teams.hiclaw.io \
  -o jsonpath='{.spec.versions[0].schema.openAPIV3Schema.properties.status.properties}' | jq 'keys'

# 探针实验：区分 CRD 缺字段 vs 控制器问题
kubectl patch team <name> --subresource=status --type=merge -p '{"status":{"reconcileAttempt":999}}'
kubectl get team <name> -o jsonpath='{.status.reconcileAttempt}'
# 空 → CRD schema 缺字段（被 pruning 裁剪），需 kubectl apply -f helm/hiclaw/crds/teams.hiclaw.io.yaml
```

### 8.3 版本核对

```bash
# 确认控制器镜像包含本分支修复（observedGeneration 短路、退避、sdk 驱动）
kubectl get deploy -n <ns> hiclaw-controller -o jsonpath='{.spec.template.spec.containers[0].image}'
# 镜像构建时间应晚于 48ce4aa 提交时间（2026-08-05）
```

### 8.4 回滚方案

| 调优项 | 回滚方式 |
|--------|----------|
| 并发/超时/周期/no-requeue | 环境变量改回默认（1 / 0 / 300 / false），重启控制器 |
| storage 连接/重试/探测 | `HICLAW_STORAGE_*` 改回默认（2 / 30 / 500 / 5000 / 2 / 30），重启控制器；不设置即用默认 |
| 存储驱动 | `HICLAW_STORAGE_DRIVER=mc`（legacy 驱动保留） |
| 退避/重试上限 | 无法通过配置回滚（内置常量）；重置 `ConsecutiveFailures=0` + `MaxRetriesReached=false` 可恢复单个 CR |
| 新增状态字段 | `omitempty` 向后兼容，旧镜像忽略未知字段；不写即不出现 |

### 8.5 升级注意事项

- 滚动升级后若 Team 全部停滞：先查 leader lease 与 informer 标签（§7 S-1 分支），这是滚动更新最常见的隐性故障（旧 Pod 持 lease、新 Pod 静默等待）。
- 升级后未变更 Active Team 应走短路快路径（日志 `team healthy, skipping full reconcile`），秒级完成 re-sync。
- 存储驱动切换 `mc` → `sdk` 后验证一次完整建队/调谐（sdk 的 bucket/key 解析逻辑与 mc 的 alias 前缀不同，见 minio_sdk.go `bucketAndKey`）。

---

## 9. 参数与症状速查（一句话版）

| 症状 | 一句话动作 |
|------|-----------|
| 队列堵、新建 Team 无 Phase | 调大 `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` |
| 调谐挂起数分钟 | 开 `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`；若为存储层硬等待，调 `HICLAW_STORAGE_*`（连接超时/重试窗口） |
| 周期调谐太频繁 | 调大 `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` 或 `HICLAW_TEAM_ACTIVE_NO_REQUEUE=true` |
| Step4 慢 | 确认 `HICLAW_STORAGE_DRIVER=sdk`，查 `mc slow call` 与存储指标 |
| 存储端点偶发抖动导致调谐失败 | 调大 `HICLAW_STORAGE_RETRY_WINDOW_SECONDS` 让短 blip 在窗口内自愈；端点长期不可达则保持小值快速失败 |
| Failed 卡死 | 排障后 `kubectl annotate team <name> hiclaw.io/retry=""` |
| 说不清为什么慢 | pprof 采样（`ENABLE_PPROF=true` 调试镜像）+ bench_s3 复现 |
