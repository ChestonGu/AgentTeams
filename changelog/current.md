# Changelog (Unreleased)

Record image-affecting changes to `manager/`, `worker/`, `copaw/`, `hermes/`, `openclaw-base/`, `hiclaw-controller/`, and release-facing install/chart changes here before the next release.

---

**What's New**

- **S3 SDK storage driver (default)**: The controller's object-storage layer now defaults to the minio-go S3 SDK (`HICLAW_STORAGE_DRIVER=sdk`), replacing the per-call `mc` subprocess fork with a connection-pooled HTTP client — ~5.8× lower per-member config latency per the `bench_s3` measurements, with static long-lived AK/SK credentials (`HICLAW_FS_ACCESS_KEY`/`HICLAW_FS_SECRET_KEY`) for cloud S3. `HICLAW_STORAGE_DRIVER=mc` restores the legacy driver, and dynamic STS credential sources remain supported on both drivers.

- **Storage stability observability**: New Prometheus metrics expose S3 health and reconcile cost: `hiclaw_storage_op_duration_seconds` (per-op latency histogram, op × driver), `hiclaw_storage_op_errors_total` (op × driver × class: network/timeout/not_found/other), `hiclaw_storage_probe_failures_total`, and `hiclaw_member_reconcile_duration_seconds` (per-member full flow: infra → SA → config → container → expose, kind × result). A rising network/timeout series is the "storage endpoint unstable" alarm that previously showed up only as reconcile stalls.

- **Flaky-storage resilience**: `DeployWorkerConfig` now probes storage reachability first (10s bounded) and aborts fast instead of burning the mc/SDK dial timeout on every subsequent op; the mc driver retries transport-level failures once after 1s; the SDK driver uses a 5s dial timeout with bounded retries (3) instead of the default 10×30s stall.

- **Builtin skill push content-compare**: `pushBuiltinSkills` now pushes per-file, skipping unchanged objects (GET-compare instead of a full `mc mirror --overwrite` re-upload), cutting the per-member skill phase from ~25–35s to a few seconds while still propagating skill updates from new controller images.

- **Active Team no-requeue mode**: `HICLAW_TEAM_ACTIVE_NO_REQUEUE=true` stops the periodic requeue for fully converged Active Teams whose spec is unchanged — they reconcile only on events (pod phase changes, spec edits) instead of on the 5m timer. Default false preserves the existing periodic behavior.

- **On-demand skill push off by default**: The controller-side local skill push (`spec.skills` via `push-worker-skills.sh`) is skipped by default (`HICLAW_LOCAL_SKILL_PUSH=true` re-enables it) — the script reads the Manager's local `workers-registry.json`, which does not exist in the controller container, so the push always failed there. Remote (nacos) skills still push via Go.

**Bug Fixes**

- **Script `log` fallback**: `hiclaw-env.sh` now defines a minimal `log()` when `base.sh` is absent (controller/worker images), so scripts fail on the real error instead of `log: command not found` (exit 127).

- **QwenPaw-first local install flow**: The installer now presents QwenPaw as the default worker runtime, supports keep-all upgrades with enter-to-keep prompts for existing parameters, and improves non-interactive guardrails for scripted installs.

- **Team human coordinators**: Team resources can include human coordinator members, with team-admin-owned Matrix rooms and updated Team Leader / Worker prompts so coordination stays inside the Team Room.

- **Team Leader coordination refresh**: Team Leader built-ins were refreshed for project planning, DAG task execution, file sharing, communication, organization, mcporter usage, and worker lifecycle coordination. Worker-style anti-loop reply rules were mirrored for Team Leader, and legacy Team Leader skill aliases were removed after migration.

- **CoPaw runtime coordination tools**: CoPaw workers now include runtime hooks and tools for task flow, project flow, messaging, file sync, output sanitizing, credential guarding, health probes, richer readiness handling, and configurable ReAct iteration limits.

- **Nacos remote skills and credentials**: The controller can pass skills API defaults and per-package Nacos authentication to workers, including `authType=nacos|sts-hiclaw|none` and `ai-registry` STS access scope.

- **Worker identity separation**: Controller resource names are separated from runtime worker names across identity, credentials, storage defaults, and readiness reporting, making CR naming and agent-facing names less tightly coupled.

- **Controller observability**: Controller-side reconcile metrics, graceful HTTP/background goroutine shutdown, and test diagnostics were added to make runtime and CI failures easier to inspect.

**Bug Fixes**

- **Installer robustness**: Rootless Podman socket detection, retry behavior for too-short admin passwords, multi-line error output, GitHub repository URL defaults, stable fallback version handling, and Windows stream-idle-timeout propagation were corrected.

- **Helm cleanup and Matrix display names**: Helm uninstall now cleans up Manager/Worker pods, and Tuwunel's default display-name suffix is disabled in the chart.

- **Manager worker lifecycle API**: Manager local container operations now use the controller's `/api/v1` paths, and `groupAllowFrom` is hot-reloaded when Workers are created.

- **Agent-facing docs and safeguards**: Agent prompts and skills now use `roomID` for `hiclaw get workers` / `hiclaw create worker` JSON, quote colon-containing frontmatter descriptions, and explicitly prohibit direct credential file access in CoPaw worker and Team Leader prompts.

- **CoPaw message handling**: CoPaw workers avoid swallowing fresh Matrix messages during startup, handle targeted readiness probes directly, stop typing indicators on empty/cancelled runs, require slash-prefixed control commands, normalize Element double-slash commands, and use display names in mention bodies.

- **CoPaw storage and context sync**: CoPaw workers align the install directory with the HOME-backed workspace path, seed the heartbeat interval at 10 minutes, skip static `mc` alias setup for k8s wrapper credentials, exclude inbound Matrix thread messages from room-history context, and suppress noisy warnings for optional missing MinIO objects.

- **Controller config preservation**: Reconcile now preserves runtime-mutated package files, default object-storage access entries, and user plugin customizations while still applying controller-managed defaults.

- **Gateway and auth stability**: The configured AI stream idle timeout is applied to the self-hosted Higress gateway, observability/stream-timeout env is propagated during bootstrap, and TokenReview cache entries are capped and swept.

- **Team reconcile unblocking**: Team reconcile parallelism is configurable via `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` (default 1, preserving legacy serial behavior; raise it so one slow/hung Team can no longer stall every other Team, including newly created ones stuck in Phase ""/Pending). An optional per-pass deadline (`HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`, disabled by default) bounds a single pass against hung external calls; `failTeam` uses exponential backoff (30s → 10min cap) and stops requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation; finalizer add/remove use merge patches instead of `Update`. `TeamStatus` gains `observedGeneration`, so unchanged Active teams skip the full provisioning chain after a controller restart or informer re-sync; the periodic requeue for converged teams defaults to 5m, is configurable via `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS`, and carries 0–10% positive jitter so teams do not wake in lockstep.

- **Team reconcile observability**: Each Team reconcile pass logs per-step timing (`team reconcile: admin actor / step 1 rooms / step 1 storage / …`), and `DeployWorkerConfig` logs per-phase upload timing, so slow steps (e.g. hung object-storage syncs) are identifiable at a glance. Per-request STS credential INFO logs are commented out (not removed) to reduce log noise; WARN/ERROR paths still log.

- **Team reconcile telemetry**: `Reconcile` injects a team-scoped logger (`team=<name>`, `teamUID=<uid>`) into the context so every downstream layer (deployer, oss, gateway, backend) is tagged with the Team identity — `grep "team=<name>"` covers the whole reconcile span. A unified `timed` helper logs elapsed on success, failure, and cancellation, and member phases now log elapsed on failure too (previously success-only). The oss layer logs `mc slow call` for any mc invocation over 300ms (with op type) to quantify the S3-layer latency distribution; `ProvisionWorker` logs per-step elapsed (Matrix register / MinIO user / room / join / gateway consumer / AI-route auth); `modifyAIRoutes` logs overall elapsed plus 409-conflict retry count; Docker image pulls log completion; a panic guard records reconcile panics with team context and requeues through the error path.

- **S3 benchmark harness**: New `bench/` module with `bench_s3.go` — a read-only reproduction benchmark over the real scenario bucket (mc subprocess vs minio-go SDK drivers, config-phase op mix 12 GET + 3 PUT + 2 STAT + 1 LIST, per-op latency percentiles + per-round wall-clock; write space isolated under `bench-probe/` and auto-cleaned).

- **Debug pprof build switch**: The controller image build accepts `ENABLE_PPROF=true` (`--build-arg`), compiling `cmd/controller` with `-tags pprof` to expose `/debug/pprof` on port 6060 (`HICLAW_PPROF_ADDR` overridable) with block/mutex sampling enabled. Default builds compile a no-op stub — no pprof code, no extra listener, zero debug surface in release images.

- **Human reconcile backoff and parity**: `HumanStatus` gains `observedGeneration` (parity with Worker/Team/Manager) plus `consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`. Infra failures now use exponential backoff (30s → 10min cap) and stop requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation, replacing the previous double-requeue (`RequeueAfter` + rate-limiter error) pattern.

---

**新增功能**

- **本地安装默认优先 QwenPaw**: 安装脚本现在优先展示 QwenPaw 作为默认 Worker 运行时，升级时支持 keep-all 和回车保留已有参数，并强化了非交互模式下的防误执行保护。

- **Team 支持人类协调员**: Team 资源支持声明人类协调员成员，Team Room 由 team-admin 归属，并同步更新 Team Leader / Worker 提示词，确保协作收敛在 Team Room 中。

- **Team Leader 协作能力刷新**: Team Leader 内置能力围绕项目规划、DAG 任务执行、文件共享、沟通、组织、mcporter 使用和 Worker 生命周期协作重新整理；同步 Worker 的 anti-loop 回复规则；迁移完成后移除了旧的 Team Leader 技能别名。

- **CoPaw 运行时协作工具**: CoPaw Worker 新增任务流、项目流、消息、文件同步、输出清洗、凭据保护、健康探针、更完整的就绪检查相关 hooks / tools，并支持配置 ReAct 最大迭代次数。

- **Nacos 远程技能与凭据**: 控制器可向 Worker 传递 skills API 默认值和每个包的 Nacos 认证配置，支持 `authType=nacos|sts-hiclaw|none` 以及 `ai-registry` STS 权限范围。

- **Worker 身份解耦**: 控制器资源名与运行时 Worker 名称在身份、凭据、存储默认值和就绪状态中解耦，降低 CR 名称与 Agent 对外名称的耦合。

- **控制器可观测性**: 增加控制器 reconcile 指标、HTTP 服务与后台 goroutine 的优雅退出，以及测试失败诊断信息，便于排查运行时和 CI 问题。

**Bug 修复**

- **安装脚本稳健性**: 修复 rootless Podman socket 检测、管理员密码过短时的重试、多行错误输出、GitHub 仓库 URL 默认值、稳定版本 fallback，以及 Windows 下 stream idle timeout 的传递。

- **Helm 清理与 Matrix 显示名**: Helm 卸载时会清理 Manager/Worker Pod；Chart 中关闭 Tuwunel 默认 display-name suffix。

- **Manager Worker 生命周期 API**: Manager 本地容器操作改为使用控制器 `/api/v1` 路径，并在 Worker 创建后热更新 `groupAllowFrom`。

- **Agent 文档与安全边界**: Agent 提示词和技能统一使用 `roomID` 解析 `hiclaw get workers` / `hiclaw create worker` JSON，修复含冒号 frontmatter 描述的引用，并在 CoPaw Worker 与 Team Leader 提示词中加入不可覆盖的凭据文件直接访问禁令。

- **CoPaw 消息处理**: CoPaw Worker 避免启动时吞掉新 Matrix 消息，直接处理定向就绪探针，空回复或取消运行时停止 typing indicator，要求运行时控制命令以 slash 开头，兼容 Element 双 slash，并在 mention 文本中使用显示名。

- **CoPaw 存储与上下文同步**: CoPaw Worker 安装目录与 HOME 工作区对齐，默认心跳间隔设为 10 分钟；在 k8s wrapper 凭据场景跳过静态 `mc` alias；房间历史上下文排除入站 Matrix thread 消息；缺失可选 MinIO 对象时不再输出噪声警告。

- **控制器配置保留**: Reconcile 过程保留运行时已变更的包文件、默认对象存储访问项和用户插件自定义配置，同时继续下发控制器托管默认值。

- **网关与认证稳定性**: 自托管 Higress 网关应用配置的 AI stream idle timeout；启动时传递 observability / stream-timeout 环境变量；TokenReview 缓存增加容量上限和清理机制。

- **Team 调谐解除阻塞**: Team 调谐并发度可通过 `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` 配置（默认 1，保持原有串行行为；调高后单个 Team 缓慢或挂起不再拖垮其他所有 Team，含新建、停在 Phase ""/Pending 的）。可通过 `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS` 开启单次调谐超时（默认关闭，保持原有行为）；`failTeam` 改为指数退避（30s → 10min 封顶），连续失败 5 次后停止自动重试，通过 `hiclaw.io/retry` 注解重新启用；finalizer 增删改用 merge patch 而非 `Update`。`TeamStatus` 新增 `observedGeneration`，控制器重启或 informer re-sync 后未变更的 Active Team 跳过全量调谐链；已收敛 Team 的周期 requeue 默认 5min，可通过 `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` 配置，并带 0–10% 正向抖动避免各 Team 同时唤醒。

- **Team 调谐可观测性**: Team 调谐每次执行按步骤打印耗时日志（`team reconcile: admin actor / step 1 rooms / step 1 storage / …`），`DeployWorkerConfig` 打印各上传阶段耗时，便于快速定位慢步骤（如对象存储同步挂起）。STS 凭据逐请求 INFO 日志改为注释保留（不删除）以降低日志噪声；WARN/ERROR 路径仍会打印。

- **Team 调谐遥测增强**: `Reconcile` 入口将 team-scoped logger（`team=<name>`、`teamUID=<uid>`）注入 context，下游各层（deployer/oss/gateway/backend）日志自动携带 Team 身份，`grep "team=<name>"` 即可覆盖整条调谐链路。新增统一 `timed` helper，成功/失败/取消均记录 elapsed；member 各阶段失败路径补上 elapsed（原先仅成功时打印）。OSS 层对超过 300ms 的 mc 调用打 `mc slow call`（含 op 类型），量化 S3 层单次调用延迟分布；`ProvisionWorker` 各步骤（Matrix 注册 / MinIO 用户 / 建房 / join / gateway consumer / AI 路由授权）补 elapsed；`modifyAIRoutes` 记录整体耗时与 409 冲突重试次数；Docker 镜像拉取完成补日志；新增 panic 兜底，panic 时带 team 上下文记录并走错误路径 requeue。

- **S3 基准复现工具**: 新增 `bench/` module，含 `bench_s3.go` —— 复用真实场景桶的只读复现基准（mc 子进程 vs minio-go SDK 双驱动，对齐 config 阶段操作配比 12 GET + 3 PUT + 2 STAT + 1 LIST，输出各操作延迟分位数与单成员轮次墙钟耗时；写空间隔离在 `bench-probe/` 前缀下并自动清理）。

- **pprof 调试构建开关**: 控制器镜像构建支持 `ENABLE_PPROF=true`（`--build-arg`），以 `-tags pprof` 编译 `cmd/controller`，暴露 6060 端口 `/debug/pprof`（可用 `HICLAW_PPROF_ADDR` 覆盖）并开启 block/mutex 采样。默认构建编译 no-op stub——不含 pprof 代码、不开额外端口，发布镜像零调试面。

- **Human 调谐退避与字段对齐**: `HumanStatus` 新增 `observedGeneration`（与 Worker/Team/Manager 对齐）以及 `consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`。Infra 失败改为指数退避（30s → 10min 封顶），连续失败 5 次后停止自动重试，通过 `hiclaw.io/retry` 注解重新启用；修复了原先 `RequeueAfter` + error 的双重 requeue 模式。

---

- feat(controller): add minio-go S3 SDK storage driver — HICLAW_STORAGE_DRIVER=sdk default with mc fallback, flaky-storage resilience (probe fast-abort, bounded retries), content-compare builtin skill push ([95dcae1](https://github.com/agentscope-ai/HiClaw/commit/95dcae1))
- feat(controller): optional no-requeue for converged Active teams — HICLAW_TEAM_ACTIVE_NO_REQUEUE, plus team member reconcile flow metrics ([84a9429](https://github.com/agentscope-ai/HiClaw/commit/84a9429))
- fix(shared): add log fallback when base.sh is absent — scripts fail on the real error instead of exit 127 ([c895d3a](https://github.com/agentscope-ai/HiClaw/commit/c895d3a))
- feat(controller): add storage stability and member reconcile metrics — storage op duration/errors (op x driver x class), probe failures, member flow duration ([77ef4b8](https://github.com/agentscope-ai/HiClaw/commit/77ef4b8))
- feat(controller): add team-scoped reconcile telemetry — ctx logger injection (team/teamUID), timed helper, failure-path elapsed, mc slow-call threshold logs, ProvisionWorker/modifyAIRoutes/ensureImage timing, panic guard ([ce1a531](https://github.com/agentscope-ai/HiClaw/commit/ce1a531))
- test(bench): add S3 reproduction benchmark under bench/ (mc vs minio-go SDK, real-bucket read-only) ([a12c5b3](https://github.com/agentscope-ai/HiClaw/commit/a12c5b3))
- feat(controller): add build-time pprof switch — ENABLE_PPROF build arg with -tags pprof, no-op stub in default builds ([6cf6f86](https://github.com/agentscope-ai/HiClaw/commit/6cf6f86))
- feat(controller): make Active Team reconcile interval configurable with positive jitter ([462f84d](https://github.com/agentscope-ai/HiClaw/commit/462f84d))
- fix(controller): build hiclaw-controller image with shared/lib via named build context ([82a75e8](https://github.com/agentscope-ai/HiClaw/commit/82a75e8))
- feat(controller): add per-step Team reconcile timing logs, configurable max concurrency, and quieter STS INFO logs ([c01aaec](https://github.com/agentscope-ai/HiClaw/commit/c01aaec))
- fix(controller): unblock Team reconcile — concurrency, failTeam backoff, observedGeneration fast path ([48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa))
- fix(controller): add Human reconcile exponential backoff and observedGeneration parity ([48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa))
- fix(install): add non-interactive deep-defense guards to step functions ([6cbec18](https://github.com/agentscope-ai/HiClaw/commit/6cbec18))
- chore(helm): bump chart to 1.1.1 and update repo URLs ([fd09d98](https://github.com/agentscope-ai/HiClaw/commit/fd09d98))
- fix(install): update GitHub repo URL to agentscope-ai/HiClaw and bump stable fallback to v1.1.1 ([f39601a](https://github.com/agentscope-ai/HiClaw/commit/f39601a))
- fix(helm): clean up Manager/Worker pods on helm uninstall ([6570402](https://github.com/agentscope-ai/HiClaw/commit/6570402))
- fix(manager): align container-api.sh paths with controller /api/v1 ([5c9a653](https://github.com/agentscope-ai/HiClaw/commit/5c9a653))
- feat(install): swap runtime selection order to make QwenPaw the default ([d3e33e8](https://github.com/agentscope-ai/HiClaw/commit/d3e33e8))
- feat(install): support keep-all upgrade mode and enter-to-keep for all params ([c9ab98f](https://github.com/agentscope-ai/HiClaw/commit/c9ab98f))
- fix(agent): use roomID when parsing hiclaw get workers JSON output ([efcb544](https://github.com/agentscope-ai/HiClaw/commit/efcb544))
- fix(install): make error() multi-line safe by splitting exit into die() ([e21ac83](https://github.com/agentscope-ai/HiClaw/commit/e21ac83))
- fix(install): retry on too-short admin password instead of exiting ([19777eb](https://github.com/agentscope-ai/HiClaw/commit/19777eb))
- fix(auth): cap and sweep TokenReview cache ([2991d06](https://github.com/agentscope-ai/HiClaw/commit/2991d06))
- chore(controller): graceful shutdown for HTTP server and background goroutines ([fc99788](https://github.com/agentscope-ai/HiClaw/commit/fc99788))
- feat(controller): export per-CRD reconcile metrics ([5d7e721](https://github.com/agentscope-ai/HiClaw/commit/5d7e721))
- fix(legacy): preserve user plugin customizations on Manager config push ([f07a32f](https://github.com/agentscope-ai/HiClaw/commit/f07a32f))
- fix(helm): disable Tuwunel default displayname suffix ([ab5cdcf](https://github.com/agentscope-ai/HiClaw/commit/ab5cdcf))
- feat(controller): support Nacos remote skills with STS auth ([fb01fe6](https://github.com/agentscope-ai/HiClaw/commit/fb01fe6))
- fix(bootstrap): propagate observability and stream timeout env ([df98989](https://github.com/agentscope-ai/HiClaw/commit/df98989))
- fix(agent): quote coding CLI skill frontmatter ([bd11844](https://github.com/agentscope-ai/HiClaw/commit/bd11844))
- feat(install): optimize container runtime socket detection for rootless podman ([b1f103b](https://github.com/agentscope-ai/HiClaw/commit/b1f103b))
- fix(copaw): stop typing indicator on empty completion ([78418b5](https://github.com/agentscope-ai/HiClaw/commit/78418b5))
- fix(copaw): use display name instead of MXID in mention body ([02ff138](https://github.com/agentscope-ai/HiClaw/commit/02ff138))
- fix(controller): preserve runtime package files on reconcile ([8cb9f46](https://github.com/agentscope-ai/HiClaw/commit/8cb9f46))
- feat(copaw): make ReAct max iterations configurable ([933a600](https://github.com/agentscope-ai/HiClaw/commit/933a600))
- feat(controller): separate CR names from runtime worker names ([12da1ce](https://github.com/agentscope-ai/HiClaw/commit/12da1ce))
- fix(copaw): require slash-prefixed control commands ([e94aceb](https://github.com/agentscope-ai/HiClaw/commit/e94aceb))
- feat(agent): prohibit direct credential file access ([046537b](https://github.com/agentscope-ai/HiClaw/commit/046537b))
- fix(manager): hot-reload groupAllowFrom when Workers are created ([94bde15](https://github.com/agentscope-ai/HiClaw/commit/94bde15))
- fix(copaw): seed worker heartbeat interval ([ec0f57d](https://github.com/agentscope-ai/HiClaw/commit/ec0f57d))
- fix(copaw): align install dir with worker home ([c0bca77](https://github.com/agentscope-ai/HiClaw/commit/c0bca77))
- fix(copaw): exclude inbound thread messages from room history ([8d6a852](https://github.com/agentscope-ai/HiClaw/commit/8d6a852))
- fix(copaw): skip mc alias setup in k8s mode ([fc1b934](https://github.com/agentscope-ai/HiClaw/commit/fc1b934))
- fix(controller): preserve default object-storage access entries ([a940d94](https://github.com/agentscope-ai/HiClaw/commit/a940d94))
- fix(copaw): suppress missing MinIO object warnings ([53d270e](https://github.com/agentscope-ai/HiClaw/commit/53d270e))
- feat(controller): propagate skills API defaults to workers ([e4a3506](https://github.com/agentscope-ai/HiClaw/commit/e4a3506))
- feat(team-leader): refresh coordination builtins ([bfd99cd](https://github.com/agentscope-ai/HiClaw/commit/bfd99cd))
- fix(controller): apply Higress stream idle timeout ([8d81c9f](https://github.com/agentscope-ai/HiClaw/commit/8d81c9f))
- feat(controller): support team human coordinators ([16e87c2](https://github.com/agentscope-ai/HiClaw/commit/16e87c2))
- feat(copaw): add runtime coordination tools ([4a2ced6](https://github.com/agentscope-ai/HiClaw/commit/4a2ced6))
- fix(install): pass stream idle timeout on Windows ([fece949](https://github.com/agentscope-ai/HiClaw/commit/fece949))
- refactor(team-leader): remove legacy skill aliases ([67a6daf](https://github.com/agentscope-ai/HiClaw/commit/67a6daf))
- fix(team-leader): mirror worker's anti-loop reply rules ([2a7cd17](https://github.com/agentscope-ai/HiClaw/commit/2a7cd17))

**Also in this window (docs / repo metadata / tests; not image-facing)**

- chore: archive changelog for v1.1.1 ([d62aecb](https://github.com/agentscope-ai/HiClaw/commit/d62aecb))
- Revert "chore: archive changelog for v1.1.1" ([c78b469](https://github.com/agentscope-ai/HiClaw/commit/c78b469))
- chore: remove duplicate CLAUDE.md entry from .gitignore ([8c262f7](https://github.com/agentscope-ai/HiClaw/commit/8c262f7))
- feat(test): add CoPaw metrics collection via token_usage.json ([724d80b](https://github.com/agentscope-ai/HiClaw/commit/724d80b))
- docs(copaw): add CredAgent config reference ([9bae51d](https://github.com/agentscope-ai/HiClaw/commit/9bae51d))
- test(controller): cover team leader ready auth ([41ac30b](https://github.com/agentscope-ai/HiClaw/commit/41ac30b))
- docs(controller): note Nacos auth type example ([d522966](https://github.com/agentscope-ai/HiClaw/commit/d522966))
- docs: sync zh-CN architecture docs ([58cdded](https://github.com/agentscope-ai/HiClaw/commit/58cdded))
- test: dump diagnostics on wait/probe failures ([e07feb8](https://github.com/agentscope-ai/HiClaw/commit/e07feb8))
