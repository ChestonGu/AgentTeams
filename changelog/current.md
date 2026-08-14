# Changelog (Unreleased)

Target release: `v1.2.2`

Comparison baseline: `v1.2.1`

Record image-affecting changes to `manager/`, `worker/`, `copaw/`, `hermes/`, `openclaw-base/`, and `agentteams-controller/` here before the next release.

---

**What's New**

- **Optional AgentTeams Dashboard**: Local installation can deploy the AgentTeams Dashboard for visual Worker, Team, Human, Manager, and Matrix management. Dashboard versioning remains independent from the AgentTeams release. ([#1075](https://github.com/agentscope-ai/AgentTeams/pull/1075), [#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **Worker push skips non-UTF-8 file names**: `base.sh` gains `is_utf8_name` / `collect_nonutf8_files` helpers (python3-based, safe for invalid byte sequences). The worker change-triggered sync loop and `update-worker-config.sh` build `mc mirror --exclude` lists from them, so a single non-UTF-8 file name no longer fails the whole push; the sync loop also ignores such files so it cannot spin on unpushable ones.

- **SDK first-create detection fix**: `os.ErrNotExist` now passes through the SDK storage driver unwrapped 閳?service-layer `os.IsNotExist` checks (which peel only os-package error types, not arbitrary `Unwrap` chains) can again distinguish "object missing (first create)" from real storage failures, so initialization generate-and-injects missing configs (seeded agent files, SOUL.md, AGENTS.md, openclaw.json merge) instead of aborting. Seed/mirror pushes also skip files with non-UTF-8 names (S3 keys must be valid UTF-8) instead of failing the whole operation.

- **base.sh shipped to all images**: `shared/lib/base.sh` provides `log()`, `waitForService()`, `waitForHTTP()`, and `generateKey()` in every image (manager/worker/controller/copaw/hermes), so `hiclaw-env.sh` consumers get the full `log()` (timestamped with date) instead of the minimal fallback; stale comments claiming base.sh was Manager-only are corrected.

- **MinIO admin API via SDK**: Embedded-mode MinIO user/policy management (`EnsureUser`/`EnsurePolicy`/`DeleteUser` during member provisioning) now follows the `HICLAW_STORAGE_DRIVER` switch instead of always forking `mc admin` subprocesses. `sdk` (default) uses the madmin-go Admin API with the same connection-pooled transport and fast-fail dial timeout as the SDK storage driver; `mc` keeps the legacy `mc admin` CLI path for rollback/parity.

- **S3 SDK storage driver (default)**: The controller's object-storage layer now defaults to the minio-go S3 SDK (`HICLAW_STORAGE_DRIVER=sdk`), replacing the per-call `mc` subprocess fork with a connection-pooled HTTP client 閳?~5.8鑴?lower per-member config latency per the `bench_s3` measurements, with static long-lived AK/SK credentials (`HICLAW_FS_ACCESS_KEY`/`HICLAW_FS_SECRET_KEY`) for cloud S3. `HICLAW_STORAGE_DRIVER=mc` restores the legacy driver, and dynamic STS credential sources remain supported on both drivers.

- **Storage stability observability**: New Prometheus metrics expose S3 health and reconcile cost: `hiclaw_storage_op_duration_seconds` (per-op latency histogram, op 鑴?driver), `hiclaw_storage_op_errors_total` (op 鑴?driver 鑴?class: network/timeout/not_found/other), `hiclaw_storage_probe_failures_total`, and `hiclaw_member_reconcile_duration_seconds` (per-member full flow: infra 閳?SA 閳?config 閳?container 閳?expose, kind 鑴?result). A rising network/timeout series is the "storage endpoint unstable" alarm that previously showed up only as reconcile stalls.

- **Flaky-storage resilience**: `DeployWorkerConfig` probes storage reachability first (30s bounded, matching the retry window) so a short OSS blip recovers inside the window instead of failing the pass; every SDK operation uses a **2s connect timeout** with transient-failure retries within a **30s total window** (exponential backoff 0.5s閳?s cap; deterministic 4xx/missing-key errors never retried); the mc driver shares the same 30s retry window. A permanently dead endpoint still aborts and requeues with a concise status message instead of stalling on the CLI's 30s dial per op.

- **Package downloads via the SDK driver**: `PackageResolver` now downloads MinIO/OSS packages through the same `StorageClient` (minio-go SDK) instead of forking `mc cp`/`mc stat` subprocesses, which bypassed the driver selection and carried the CLI's own 30s dial timeout 閳?this was the source of `deploy package: package resolve/extract failed: ... mc: unable to prepare URL for copying` under OSS jitter. ETag-based content caching (skip re-download when the etag-named local cache file exists) and atomic temp+rename writes are preserved; `mc` remains only as a nil-storage fallback.

- **Concise Status.Message on reconcile failure**: Storage-layer errors are now single-layer (`storage get <key>: dial tcp ...: i/o timeout`) via `oss.OpError`, and Worker/Manager/Human/Team controllers write a condensed root-cause message (opaque subprocess-exit leaves skipped, 512-char cap) to `status.message` instead of the full multi-layer wrap chain.

- **Worker-management scripts log fallback**: `push-worker-skills.sh`, `generate-worker-config.sh`, and `enable-peer-mentions.sh` now define a defensive `log()` when `hiclaw-env.sh`/`base.sh` did not provide one, so a failed `mc` call logs its `WARNING` properly instead of aborting with a misleading `log: command not found` in images without `base.sh` (controller/worker) or when the shared lib failed to load.

- **Builtin skill push content-compare**: `pushBuiltinSkills` now pushes per-file, skipping unchanged objects (GET-compare instead of a full `mc mirror --overwrite` re-upload), cutting the per-member skill phase from ~25閳?5s to a few seconds while still propagating skill updates from new controller images.

- **Active Team no-requeue mode**: `HICLAW_TEAM_ACTIVE_NO_REQUEUE=true` stops the periodic requeue for fully converged Active Teams whose spec is unchanged 閳?they reconcile only on events (pod phase changes, spec edits) instead of on the 5m timer. Default false preserves the existing periodic behavior.

- **On-demand skill push off by default**: The controller-side local skill push (`spec.skills` via `push-worker-skills.sh`) is skipped by default (`HICLAW_LOCAL_SKILL_PUSH=true` re-enables it) 閳?the script reads the Manager's local `workers-registry.json`, which does not exist in the controller container, so the push always failed there. Remote (nacos) skills still push via Go.

**Bug Fixes**

- **Script `log` fallback**: `hiclaw-env.sh` now defines a minimal `log()` when `base.sh` is absent (controller/worker images), so scripts fail on the real error instead of `log: command not found` (exit 127).

- **QwenPaw-first local install flow**: The installer now presents QwenPaw as the default worker runtime, supports keep-all upgrades with enter-to-keep prompts for existing parameters, and improves non-interactive guardrails for scripted installs.

- **Team human coordinators**: Team resources can include human coordinator members, with team-admin-owned Matrix rooms and updated Team Leader / Worker prompts so coordination stays inside the Team Room.

- **Team Leader coordination refresh**: Team Leader built-ins were refreshed for project planning, DAG task execution, file sharing, communication, organization, mcporter usage, and worker lifecycle coordination. Worker-style anti-loop reply rules were mirrored for Team Leader, and legacy Team Leader skill aliases were removed after migration.

- **CoPaw runtime coordination tools**: CoPaw workers now include runtime hooks and tools for task flow, project flow, messaging, file sync, output sanitizing, credential guarding, health probes, richer readiness handling, and configurable ReAct iteration limits.

- **Nacos remote skills and credentials**: The controller can pass skills API defaults and per-package Nacos authentication to workers, including `authType=nacos|sts-hiclaw|none` and `ai-registry` STS access scope.

- **Worker identity separation**: Controller resource names are separated from runtime worker names across identity, credentials, storage defaults, and readiness reporting, making CR naming and agent-facing names less tightly coupled.

- **Controller observability**: Controller-side reconcile metrics, graceful HTTP/background goroutine shutdown, and test diagnostics were added to make runtime and CI failures easier to inspect.
- **Manager-to-Worker custom Skill delivery**: A Manager can validate, upload, and assign a custom Worker Skill before updating `Worker.spec.skills`; QwenPaw Workers synchronize the assigned Skill into their native workspace and refresh plus enable it without a restart. ([c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))

**Bug Fixes**

- **Worker custom Skill loading safeguards**: Grant Managers access to Worker storage prefixes, avoid recursive CR updates during reconciliation, keep local/default MinIO paths off unavailable STS, restore Manager MinIO access after Controller restarts, verify uploaded `SKILL.md` files, and restore Worker file-sync notifications. ([c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **QwenPaw Manager custom Skill hot loading**: Continuously mirror workspace Skills into the native QwenPaw workspace, then refresh and enable additions or updates without restarting the Manager. ([c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **QwenPaw Manager Skill attachments**: Initialize the Matrix attachment directory at startup so an admin can send a complete Worker Skill ZIP to the Manager for validation and distribution. ([c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **Team Room membership convergence**: Explicitly join Team Leaders and Workers with their own Matrix tokens after invitation, so pending invites cannot leave a Team Active while Hermes members remain unreachable. ([c15ba378](https://github.com/agentscope-ai/AgentTeams/commit/c15ba378f9b868778e252bc689e7ea3f5f23a695), [#1142](https://github.com/agentscope-ai/AgentTeams/issues/1142), [#1149](https://github.com/agentscope-ai/AgentTeams/pull/1149))

- **Team reconcile unblocking**: Team reconcile parallelism is configurable via `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` (default 1, preserving legacy serial behavior; raise it so one slow/hung Team can no longer stall every other Team, including newly created ones stuck in Phase ""/Pending). An optional per-pass deadline (`HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`, disabled by default) bounds a single pass against hung external calls; `failTeam` uses exponential backoff (30s 閳?10min cap) and stops requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation; finalizer add/remove use merge patches instead of `Update`. `TeamStatus` gains `observedGeneration`, so unchanged Active teams skip the full provisioning chain after a controller restart or informer re-sync; the periodic requeue for converged teams defaults to 5m, is configurable via `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS`, and carries 0閳?0% positive jitter so teams do not wake in lockstep.

- **Team reconcile observability**: Each Team reconcile pass logs per-step timing (`team reconcile: admin actor / step 1 rooms / step 1 storage / 閳ヮ泦), and `DeployWorkerConfig` logs per-phase upload timing, so slow steps (e.g. hung object-storage syncs) are identifiable at a glance. Per-request STS credential INFO logs are commented out (not removed) to reduce log noise; WARN/ERROR paths still log.

- **Team reconcile telemetry**: `Reconcile` injects a team-scoped logger (`team=<name>`, `teamUID=<uid>`) into the context so every downstream layer (deployer, oss, gateway, backend) is tagged with the Team identity 閳?`grep "team=<name>"` covers the whole reconcile span. A unified `timed` helper logs elapsed on success, failure, and cancellation, and member phases now log elapsed on failure too (previously success-only). The oss layer logs `mc slow call` for any mc invocation over 300ms (with op type) to quantify the S3-layer latency distribution; `ProvisionWorker` logs per-step elapsed (Matrix register / MinIO user / room / join / gateway consumer / AI-route auth); `modifyAIRoutes` logs overall elapsed plus 409-conflict retry count; Docker image pulls log completion; a panic guard records reconcile panics with team context and requeues through the error path.

- **S3 benchmark harness**: New `bench/` module with `bench_s3.go` 閳?a read-only reproduction benchmark over the real scenario bucket (mc subprocess vs minio-go SDK drivers, config-phase op mix 12 GET + 3 PUT + 2 STAT + 1 LIST, per-op latency percentiles + per-round wall-clock; write space isolated under `bench-probe/` and auto-cleaned).

- **Debug pprof build switch**: The controller image build accepts `ENABLE_PPROF=true` (`--build-arg`), compiling `cmd/controller` with `-tags pprof` to expose `/debug/pprof` on port 6060 (`HICLAW_PPROF_ADDR` overridable) with block/mutex sampling enabled. Default builds compile a no-op stub 閳?no pprof code, no extra listener, zero debug surface in release images.

- **Human reconcile backoff and parity**: `HumanStatus` gains `observedGeneration` (parity with Worker/Team/Manager) plus `consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`. Infra failures now use exponential backoff (30s 閳?10min cap) and stop requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation, replacing the previous double-requeue (`RequeueAfter` + rate-limiter error) pattern.
**鏂板鍔熻兘**

- **Manager 鍚?Worker 涓嬪彂鑷畾涔?Skill**锛歁anager 鍙湪鏇存柊 `Worker.spec.skills` 鍓嶆牎楠屻€佷笂浼犲苟鍒嗛厤鑷畾涔?Worker Skill锛決wenPaw Worker 浼氬皢宸插垎閰?Skill 鍚屾鍒板師鐢?workspace锛屽苟鍦ㄦ棤闇€閲嶅惎鐨勬儏鍐典笅鍒锋柊鍜屽惎鐢ㄣ€?[c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))

**Bug 淇**

- **Worker 鑷畾涔?Skill 鍔犺浇淇濇姢**锛氭巿浜?Manager 瀵?Worker 瀛樺偍鍓嶇紑鐨勮闂潈闄愶紝閬垮厤璋冨拰鏈熼棿閫掑綊鏇存柊 CR锛岄伩鍏嶆湰鍦版垨榛樿 MinIO 璺緞璇姹備笉鍙敤鐨?STS锛孋ontroller 閲嶅惎鍚庢仮澶?Manager 鐨?MinIO 璁块棶骞舵牎楠屽凡涓婁紶鐨?`SKILL.md`锛屽悓鏃舵仮澶?Worker 鐨?file-sync 閫氱煡銆?[c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **QwenPaw Manager 鑷畾涔?Skill 鐑姞杞?*锛氭寔缁皢 workspace Skill 鎶曞奖鍒?QwenPaw 鍘熺敓 workspace锛屽苟鑷姩鍒锋柊銆佸惎鐢ㄦ柊澧炴垨鏇存柊鐨?Skill锛屾棤闇€閲嶅惎 Manager銆?[c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **QwenPaw Manager Skill 闄勪欢**锛氬惎鍔ㄦ椂鍒涘缓 Matrix 闄勪欢鐩綍锛屼娇绠＄悊鍛樺彲浠ュ皢瀹屾暣鐨?Worker Skill ZIP 鍙戦€佺粰 Manager锛岀敱鍏舵牎楠屽苟鍒嗗彂銆?[c2d84b09](https://github.com/agentscope-ai/AgentTeams/commit/c2d84b09d3547b258decbb1d24b245c10a98c088), [#1153](https://github.com/agentscope-ai/AgentTeams/pull/1153))
- **Team Room 鎴愬憳鏀舵暃**锛氶個璇?Team Leader 鍜?Worker 鍚庯紝浣跨敤鍚勮嚜鐨?Matrix token 鏄惧紡鍔犲叆 Team Room锛岄伩鍏?Team 宸插浜?Active 鐘舵€佹椂 Hermes 鎴愬憳浠嶅仠鐣欏湪 invite銆佹棤娉曟帴鏀舵秷鎭€?[c15ba378](https://github.com/agentscope-ai/AgentTeams/commit/c15ba378f9b868778e252bc689e7ea3f5f23a695), [#1142](https://github.com/agentscope-ai/AgentTeams/issues/1142), [#1149](https://github.com/agentscope-ai/AgentTeams/pull/1149))

---

**閻潙娼栭幀褍褰夐弴?/ 閸楀洨楠囩拠瀛樻**

- **AgentTeams 閸涜棄鎮曢幋鎰礋閸烆垯绔撮悽鐔告櫏閻ㄥ嫯绻嶇悰灞炬婵傛垹瀹?*閿涙艾鐣ㄧ憗鍛珤娑?Helm 閸忋儱褰涢妴涓唎ntroller 閸栧懌鈧梗agt` CLI閵嗕胶骞嗘晶鍐ㄥ綁闁插繈鈧浇绻嶇悰灞炬鐠侯垰绶為妴浣界カ濠ф劕鎮曠粔鏉挎嫲鐎涙ê鍋嶉崜宥囩磻瀹歌尙顏崚鎵伂缂佺喍绔存稉?AgentTeams閵嗗倹妫?HiClaw wrapper 閸滃本妞跨捄鍐╃爱閻椒鑵戦惃鍕悑鐎圭懓鍨庨弨顖氬嚒缁夊娅庨敍娑楃矤閺冄勭閸楁洘鍨ㄩ懘姘拱閸楀洨楠囬弮璺虹箑妞よ鍨忛幑銏犲煂 AgentTeams 閸涜棄鎮曢妴?[#1063](https://github.com/agentscope-ai/AgentTeams/pull/1063), [#1065](https://github.com/agentscope-ai/AgentTeams/pull/1065))
- **Team 娑?Worker 鐠у嫭绨担璺ㄦ暏 v1.2 閺堚偓缂佸牆顨栫痪?*閿涙瓖eam 闁俺绻?`spec.workerMembers` 瀵洜鏁ら悪顒傜彌缁狅紕鎮婇惃?Worker CR閿涙稐绗夐崘宥嗘暜閹镐礁鍞撮懕?Worker 閹存劕鎲抽妴涔篹gistry 鏉╀胶些鐠侯垰绶為崣濠傚従娓氭繆绂嗛惃鍕＋閼存碍婀伴妴?[#1072](https://github.com/agentscope-ai/AgentTeams/pull/1072))

**閺傛澘顤冮崝鐔诲厴**

- **閸欘垶鈧?AgentTeams Dashboard**閿涙碍婀伴崷鏉跨暔鐟佸懎褰查柈銊ц AgentTeams Dashboard閿涘瞼鏁ゆ禍搴″讲鐟欏棗瀵茬粻锛勬倞 Worker閵嗕箑eam閵嗕笭uman閵嗕府anager 閸?Matrix閿涙饱ashboard 閻楀牊婀扮紒褏鐢绘稉?AgentTeams 閻楀牊婀伴悪顒傜彌閵?[#1075](https://github.com/agentscope-ai/AgentTeams/pull/1075), [#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **Worker 閹恒劑鈧浇鐑︽潻鍥姜 UTF-8 閺傚洣娆㈤崥?*: `base.sh` 閺傛澘顤?`is_utf8_name` / `collect_nonutf8_files` 鏉堝懎濮崙鑺ユ殶閿涘潷ython3 閺嶏繝鐛欓敍灞筋嚠閺冪姵鏅ョ€涙濡€瑰鍙忛敍澶堚偓鍊僶rker 閸欐ɑ娲跨憴锕€褰傞崥灞绢劄瀵邦亞骞嗛崪?`update-worker-config.sh` 閹诡喗顒濋弸鍕偓?`mc mirror --exclude`閿涘苯宕熸稉顏堟姜 UTF-8 閺傚洣娆㈤崥宥勭瑝閸愬秴顕遍懛瀛樻殻閹佃甯归柅浣搞亼鐠愩儻绱遍崥灞绢劄瀵邦亞骞嗛崥灞炬韫囩晫鏆愭潻娆戣閺傚洣娆㈤敍宀勪缉閸忓秶鈹栨潪顑锯偓?

- **SDK 妫ｆ牗顐奸崚娑樼紦閸掋倕鐣炬穱顔碱槻**: SDK 鐎涙ê鍋嶆す鍗炲З鐎?`os.ErrNotExist` 娑撳秴鍟€閸?`OpError` 閸栧懓顥婇敍宀€娲块幒銉┾偓蹇庣炊閳ユ柡鈧敃ervice 鐏炲倷绶风挧鏍畱 `os.IsNotExist`閿涘牆褰ч崜?os 閸栧懘鏁婄拠顖滆閸ㄥ鈧椒绗夐柅鎺戠秺娴犵粯鍓?Unwrap閿涘浠径宥囨晸閺佸牞绱濋崚婵嗩潗閸栨牗妞?OSS 闁板秶鐤嗛弬鍥︽娑撳秴鐡ㄩ崷銊ょ窗鐞氼偅顒滅涵顔跨槕閸掝偂璐熸＃鏍偧閸掓稑缂撻敍鍫㈡晸閹存劖鏁為崗銉窗缁夊秴鐡欓弬鍥︽閵嗕讣OUL.md閵嗕竸GENTS.md閵嗕狗penclaw.json 閸氬牆鑻熼敍澶涚礉娑撳秴鍟€閹躲儵鏁婃稉顓熸焽閿涙稒甯归柅浣割嚠鐠炩剝妞傜捄瀹犵箖闂?UTF-8 閺傚洣娆㈤崥宥囨畱閺傚洣娆㈤敍鍦? key 韫囧懘銆忛崥鍫熺《 UTF-8閿涘绱濋柆鍨帳閸楁洑閲滈崸蹇旀瀮娴犺泛鎮曠€佃壈鍤ч弫瀛樺閹恒劑鈧礁銇戠拹銉ｂ偓?

- **base.sh 闂?shared/lib 鏉╂稑鍙嗛幍鈧張澶愭殔閸?*: `shared/lib/base.sh` 閹绘劒绶?`log()`閵嗕梗waitForService()`閵嗕梗waitForHTTP()`閵嗕梗generateKey()`閿涘anager/worker/controller/copaw/hermes 闂€婊冨剼閸у洤瀵橀崥顐礉`hiclaw-env.sh` 閻ㄥ嫭绉风拹瑙勬煙閼存碍婀伴幏鍨煂鐎瑰本鏆?`log()`閿涘牆鐢弮銉︽埂閺冨爼妫块幋绛圭礆閼板奔绗夐崘宥嗘Ц閺堚偓鐏?fallback閿涙稑鎮撳銉ゆ叏濮濓絼绨?"base.sh 娴?Manager 閻? 閻ㄥ嫯绻冮弮鑸垫暈闁插鈧?

- **MinIO Admin API 鐠?SDK**: embedded MinIO 閻ㄥ嫮鏁ら幋?缁涙牜鏆愮粻锛勬倞閿涘牊鍨氶崨妯跨殶鐠嬫劒鑵戦惃?`EnsureUser`/`EnsurePolicy`/`DeleteUser`閿涘绗夐崘宥呮祼鐎?fork `mc admin` 鐎涙劘绻樼粙瀣剁礉閺€閫涜礋鐠虹喖娈?`HICLAW_STORAGE_DRIVER` 閸掑洦宕查敍姝歴dk`閿涘牓绮拋銈忕礆閻?madmin-go Admin API閿涘苯顦查悽?SDK 鐎涙ê鍋嶆す鍗炲З閻ㄥ嫯绻涢幒銉︾潨娑撳骸鎻╅柅鐔枫亼鐠?dial 鐡掑懏妞傞敍娌梞c` 娣囨繄鏆€閸?`mc admin` CLI 鐠侯垰绶炴禒銉ょ┒閸ョ偞绮?鐎佃鐦妴?

- **閺堫剙婀寸€瑰顥婃妯款吇娴兼ê鍘?QwenPaw**: 鐎瑰顥婇懘姘拱閻滄澘婀导妯哄帥鐏炴洜銇?QwenPaw 娴ｆ粈璐熸妯款吇 Worker 鏉╂劘顢戦弮璁圭礉閸楀洨楠囬弮鑸垫暜閹?keep-all 閸滃苯娲栨潪锔跨箽閻ｆ瑥鍑￠張澶婂棘閺佸府绱濋獮璺哄繁閸栨牔绨￠棃鐐版唉娴滄帗膩瀵繋绗呴惃鍕Щ鐠囶垱澧界悰灞肩箽閹躲們鈧?

- **Team 閺€顖涘瘮娴滆櫣琚崡蹇氱殶閸?*: Team 鐠у嫭绨弨顖涘瘮婢圭増妲戞禍铏硅閸楀繗鐨熼崨妯诲灇閸涙﹫绱漈eam Room 閻?team-admin 瑜版帒鐫橀敍灞借嫙閸氬本顒為弴瀛樻煀 Team Leader / Worker 閹绘劗銇氱拠宥忕礉绾喕绻氶崡蹇庣稊閺€鑸垫殐閸?Team Room 娑擃厹鈧?

- **Team Leader 閸楀繋缍旈懗钘夊閸掗攱鏌?*: Team Leader 閸愬懐鐤嗛懗钘夊閸ュ绮い鍦窗鐟欏嫬鍨濋妴涓廇G 娴犺濮熼幍褑顢戦妴浣规瀮娴犺泛鍙℃禍顐犫偓浣圭煛闁哎鈧胶绮嶇紒鍥モ偓涔礳porter 娴ｈ法鏁ら崪?Worker 閻㈢喎鎳￠崨銊︽埂閸楀繋缍旈柌宥嗘煀閺佸鎮婇敍娑樻倱濮?Worker 閻?anti-loop 閸ョ偛顦茬憴鍕灟閿涙稖绺肩粔璇茬暚閹存劕鎮楃粔濠氭珟娴滃棙妫惃?Team Leader 閹垛偓閼宠棄鍩嗛崥宥冣偓?

- **CoPaw 鏉╂劘顢戦弮璺哄礂娴ｆ粌浼愰崗?*: CoPaw Worker 閺傛澘顤冩禒璇插濞翠降鈧線銆嶉惄顔界ウ閵嗕焦绉烽幁顖樷偓浣规瀮娴犺泛鎮撳銉ｂ偓浣界翻閸戠儤绔诲ú妞尖偓浣稿殶閹诡喕绻氶幎銈冣偓浣镐淮鎼撮攱甯伴柦鍫涒偓浣规纯鐎瑰本鏆ｉ惃鍕皑缂侇亝顥呴弻銉ф祲閸?hooks / tools閿涘苯鑻熼弨顖涘瘮闁板秶鐤?ReAct 閺堚偓婢堆嗗嚡娴狅絾顐奸弫鑸偓?

- **Nacos 鏉╂粎鈻奸幎鈧懗鎴掔瑢閸戭厽宓?*: 閹貉冨煑閸ｃ劌褰查崥?Worker 娴肩娀鈧?skills API 姒涙顓婚崐鐓庢嫲濮ｅ繋閲滈崠鍛畱 Nacos 鐠併倛鐦夐柊宥囩枂閿涘本鏁幐?`authType=nacos|sts-hiclaw|none` 娴犮儱寮?`ai-registry` STS 閺夊啴妾洪懠鍐ㄦ纯閵?

- **Worker 闊偂鍞ょ憴锝堚偓?*: 閹貉冨煑閸ｃ劏绁┃鎰倳娑撳氦绻嶇悰灞炬 Worker 閸氬秶袨閸︺劏闊╂禒濮愨偓浣稿殶閹诡喓鈧礁鐡ㄩ崒銊╃帛鐠併倕鈧厧鎷扮亸杈╁崕閻樿埖鈧椒鑵戠憴锝堚偓锔肩礉闂勫秳缍?CR 閸氬秶袨娑?Agent 鐎电懓顦婚崥宥囆為惃鍕偓锕€鎮庨妴?

- **閹貉冨煑閸ｃ劌褰茬憴鍌涚ゴ閹?*: 婢х偛濮為幒褍鍩楅崳?reconcile 閹稿洦鐖ｉ妴涓燭TP 閺堝秴濮熸稉搴℃倵閸?goroutine 閻ㄥ嫪绱梿鍛粹偓鈧崙鐚寸礉娴犮儱寮峰ù瀣槸婢惰精瑙︾拠濠冩焽娣団剝浼呴敍灞肩┒娴滃孩甯撻弻銉ㄧ箥鐞涘本妞傞崪?CI 闂傤噣顣介妴?

**Bug 娣囶喖顦?*

- **Worker 鐎涙ê鍋嶉崥灞绢劄 I/O 閺€鎯с亣**閿涙艾鐔€娴滃孩鍨氶崝?watermark 閸欘亙绗傛导鐘插綁閸栨牗鏋冩禒璁圭礉娣囨繃瀵?jq 1.7 fallback pull 鐎涙ɑ妞块敍灞借嫙鐏?embedded Controller mirror 闂勬劕鐣炬稉鐑樺付閸掑爼娼伴柊宥囩枂閵嗗倸鑻熼崣鎴濆灡瀵?Worker 閸滃本婀惌銉ヤ紣娴ｆ粎娲拌ぐ鏇氱矝娣囨繃瀵旈崢鐔告箒閹镐椒绠欓崠鏍嚔娑斿绱濇稉宥呭晙閸欏秴顦查幍褑顢戦崗銊╁櫤 workspace mirror閵?[#1110](https://github.com/agentscope-ai/AgentTeams/pull/1110))
- **CoPaw Team 鐠侯垳鏁辨稉?workspace 閹舵洖濂?*閿涙艾鐨?Team Leader 閸掑棝鍘ら敍鍫濆瘶閹?localpart mention閿涘鐭鹃悽鍗炲煂 Team Room閿涘苯鑻熼幎?Worker prompt閵嗕够kills閵嗕礁浼愰崗鐑藉帳缂冾喖鎷?Matrix 鐠佸墽鐤嗛幎鏇炲閸?CoPaw 姒涙顓?workspace閵?[#1060](https://github.com/agentscope-ai/AgentTeams/pull/1060), [9074def](https://github.com/agentscope-ai/AgentTeams/commit/9074def3), [973e291](https://github.com/agentscope-ai/AgentTeams/commit/973e291), [92c8145](https://github.com/agentscope-ai/AgentTeams/commit/92c8145))
- **Team Room 娑?Worker 閻㈢喎鎳￠崨銊︽埂閺€鑸垫殐**閿涙矮绻氶幎銈堫潶瀵洜鏁ら惃?Worker CR閿涘苯宸遍崚鑸电墡妤?Team 韫囧懎锝炵憴鎺曞閿涘苯鐨?Manager 缁夎鍤弲顕€鈧?Team Worker 閻ㄥ嫪閲滄禍鐑樺煣闂傝揪绱濋獮璺烘躬 Worker 缁傝绱?Team 閸氬孩浠径?standalone 閹存劕鎲抽崗宕囬兇閵?[d96f1ed](https://github.com/agentscope-ai/AgentTeams/commit/d96f1ed), [43545c2](https://github.com/agentscope-ai/AgentTeams/commit/43545c2), [b5b0add](https://github.com/agentscope-ai/AgentTeams/commit/b5b0add), [a5d6435](https://github.com/agentscope-ai/AgentTeams/commit/a5d6435))
- **v1.2 娑斿澧犻梹婊冨剼閻ㄥ嫬鐣ㄧ憗鍛悑鐎?*閿涙1.1.2 闂€婊冨剼娴ｈ法鏁ら弮褏骞嗘晶鍐ㄥ綁闁插繐顨栫痪锕€鎷扮€涙ê鍋嶉崜宥囩磻閿涘瘉1.2.0 閸欏﹥娲块弬浼存殔閸嶅繋濞囬悽?AgentTeams 婵傛垹瀹抽敍娌?.2.0.beta.1` 缁涘鍤滅€规矮绠熸潏鎾冲弳娴兼俺顫夐懠鍐ㄥ娑撳搫鍑￠崣鎴濈閻?Tag 閺嶇厧绱￠妴?[#1079](https://github.com/agentscope-ai/AgentTeams/pull/1079), [#1100](https://github.com/agentscope-ai/AgentTeams/pull/1100))
- **Dashboard 鐎瑰顥婇崣顖炴浆閹?*閿涙矮绻氶悾?quick-start 娑?keep-all 鐞涘奔璐熼敍灞藉磳缁狙勬閹垹顦?Worker 閸戭厽宓侀敍灞炬暭鏉╂稓缍夐崗铏赴濞村绗岀€瑰顥婃宀冪槈閿涘苯鑻熼崷銊ュ祻鏉炶姤妞傚〒鍛倞 Dashboard 閺佺増宓侀妴?[#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **瀹搞儱鍙挎稉搴ょ槚閺傤厼鐣ㄩ崗銊︹偓?*閿涙碍瀚嗙紒婵呯瑝鐎瑰鍙忛惃鍕絻娴犺泛缍婂锝夋懠閹恒儻绱濈€瑰本鏆ｉ懘杈ㄦ櫛鐠嬪啳鐦崠鍛厬閻?Matrix 娴滃娆㈤敍宀€鏁撻幋鎰讲閻╁瓨甯存潻鎰攽閻?Worker ZIP 鐎电厧鍙嗛崨鎴掓姢閿涘矁鐦戦崚顐㈢秼閸?OpenClaw cron 閺嶇厧绱￠敍灞惧礋閼惧嘲宓嗛弮?replay 閸ョ偛顦查敍灞借嫙閹镐椒绠欓崠鏍ь啇閸ｃ劌瀵?skopeo 閻ㄥ嫯顓荤拠浣蜂繆閹垬鈧?[#1043](https://github.com/agentscope-ai/AgentTeams/pull/1043), [#1045](https://github.com/agentscope-ai/AgentTeams/pull/1045), [#1047](https://github.com/agentscope-ai/AgentTeams/pull/1047), [#1048](https://github.com/agentscope-ai/AgentTeams/pull/1048), [#1049](https://github.com/agentscope-ai/AgentTeams/pull/1049), [#1050](https://github.com/agentscope-ai/AgentTeams/pull/1050))

- **Team 鐠嬪啳鐨憴锝夋珟闂冭顢?*: Team 鐠嬪啳鐨獮璺哄絺鎼达箑褰查柅姘崇箖 `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` 闁板秶鐤嗛敍鍫ョ帛鐠?1閿涘奔绻氶幐浣稿斧閺堝瑕嗙悰宀冾攽娑撶尨绱辩拫鍐彯閸氬骸宕熸稉?Team 缂傛挻鍙冮幋鏍ㄥ瘯鐠ц渹绗夐崘宥嗗珛閸喖鍙炬禒鏍ㄥ閺?Team閿涘苯鎯堥弬鏉跨紦閵嗕礁浠犻崷?Phase ""/Pending 閻ㄥ嫸绱氶妴鍌氬讲闁俺绻?`HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS` 瀵偓閸氼垰宕熷▎陇鐨熺拫鎰Т閺冭绱欐妯款吇閸忔娊妫撮敍灞肩箽閹镐礁甯張澶庮攽娑撶尨绱氶敍娌梖ailTeam` 閺€閫涜礋閹稿洦鏆熼柅鈧柆鍖＄礄30s 閳?10min 鐏忎線銆婇敍澶涚礉鏉╃偟鐢绘径杈Е 5 濞嗏€虫倵閸嬫粍顒涢懛顏勫З闁插秷鐦敍宀勨偓姘崇箖 `hiclaw.io/retry` 濞夈劏袙闁插秵鏌婇崥顖滄暏閿涙矤inalizer 婢х偛鍨归弨鍦暏 merge patch 閼板矂娼?`Update`閵嗕繖TeamStatus` 閺傛澘顤?`observedGeneration`閿涘本甯堕崚璺烘珤闁插秴鎯庨幋?informer re-sync 閸氬孩婀崣妯绘纯閻?Active Team 鐠哄疇绻冮崗銊╁櫤鐠嬪啳鐨柧鎾呯幢瀹稿弶鏁归弫?Team 閻ㄥ嫬鎳嗛張?requeue 姒涙顓?5min閿涘苯褰查柅姘崇箖 `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` 闁板秶鐤嗛敍灞借嫙鐢?0閳?0% 濮濓絽鎮滈幎鏍уЗ闁灝鍘ら崥?Team 閸氬本妞傞崬銈夊晪閵?

- **Team 鐠嬪啳鐨崣顖濐潎濞村鈧?*: Team 鐠嬪啳鐨В蹇旑偧閹笛嗩攽閹稿顒炴銈嗗ⅵ閸楁媽鈧妞傞弮銉ョ箶閿涘潉team reconcile: admin actor / step 1 rooms / step 1 storage / 閳ヮ泦閿涘绱漙DeployWorkerConfig` 閹垫挸宓冮崥鍕瑐娴肩娀妯佸▓浣冣偓妤佹閿涘奔绌舵禍搴℃彥闁喎鐣炬担宥嗗弮濮濄儵顎冮敍鍫濐洤鐎电钖勭€涙ê鍋嶉崥灞绢劄閹稿倽鎹ｉ敍澶堚偓淇俆S 閸戭厽宓侀柅鎰嚞濮?INFO 閺冦儱绻旈弨閫涜礋濞夈劑鍣存穱婵堟殌閿涘牅绗夐崚鐘绘珟閿涘浜掗梽宥勭秵閺冦儱绻旈崳顏勶紣閿涙矅ARN/ERROR 鐠侯垰绶炴禒宥勭窗閹垫挸宓冮妴?

- **Team 鐠嬪啳鐨柆銉︾ゴ婢х偛宸?*: `Reconcile` 閸忋儱褰涚亸?team-scoped logger閿涘潉team=<name>`閵嗕梗teamUID=<uid>`閿涘鏁為崗?context閿涘奔绗呭〒绋挎倗鐏炲偊绱檇eployer/oss/gateway/backend閿涘妫╄箛妤勫殰閸斻劍鎯＄敮?Team 闊偂鍞ら敍瀹峠rep "team=<name>"` 閸楀啿褰茬憰鍡欐磰閺佸瓨娼拫鍐毉闁炬崘鐭鹃妴鍌涙煀婢х偟绮烘稉鈧?`timed` helper閿涘本鍨氶崝?婢惰精瑙?閸欐牗绉烽崸鍥唶瑜?elapsed閿涙驳ember 閸氬嫰妯佸▓闈涖亼鐠愩儴鐭惧鍕夋稉?elapsed閿涘牆甯崗鍫滅矌閹存劕濮涢弮鑸靛ⅵ閸楀府绱氶妴渚絊S 鐏炲倸顕搾鍛扮箖 300ms 閻?mc 鐠嬪啰鏁ら幍?`mc slow call`閿涘牆鎯?op 缁鐎烽敍澶涚礉闁插繐瀵?S3 鐏炲倸宕熷▎陇鐨熼悽銊ユ鏉╃喎鍨庣敮鍐跨幢`ProvisionWorker` 閸氬嫭顒炴銈忕礄Matrix 濞夈劌鍞?/ MinIO 閻劍鍩?/ 瀵ょ儤鍩?/ join / gateway consumer / AI 鐠侯垳鏁遍幒鍫熸綀閿涘藟 elapsed閿涙矖modifyAIRoutes` 鐠佹澘缍嶉弫缈犵秼閼版妞傛稉?409 閸愯尙鐛婇柌宥堢槸濞嗏剝鏆熼敍姹cker 闂€婊冨剼閹峰褰囩€瑰本鍨氱悰銉︽）韫囨绱遍弬鏉款杻 panic 閸忔粌绨抽敍瀹瞐nic 閺冭泛鐢?team 娑撳﹣绗呴弬鍥唶瑜版洖鑻熺挧浼存晩鐠囶垵鐭惧?requeue閵?

- **S3 閸╁搫鍣径宥囧箛瀹搞儱鍙?*: 閺傛澘顤?`bench/` module閿涘苯鎯?`bench_s3.go` 閳ユ柡鈧?婢跺秶鏁ら惇鐔风杽閸︾儤娅欏鍓佹畱閸欘亣顕版径宥囧箛閸╁搫鍣敍鍧 鐎涙劘绻樼粙?vs minio-go SDK 閸欏矂鈹嶉崝顭掔礉鐎靛綊缍?config 闂冭埖顔岄幙宥勭稊闁板秵鐦?12 GET + 3 PUT + 2 STAT + 1 LIST閿涘矁绶崙鍝勬倗閹垮秳缍斿鎯扮箿閸掑棔缍呴弫棰佺瑢閸楁洘鍨氶崨妯跨枂濞嗏€愁暰闁界喕鈧妞傞敍娑樺晸缁屾椽妫块梾鏃傤瀲閸?`bench-probe/` 閸撳秶绱戞稉瀣嫙閼奉亜濮╁〒鍛倞閿涘鈧?

- **pprof 鐠嬪啳鐦弸鍕紦瀵偓閸?*: 閹貉冨煑閸ｃ劑鏆呴崓蹇旂€鐑樻暜閹?`ENABLE_PPROF=true`閿涘潉--build-arg`閿涘绱濇禒?`-tags pprof` 缂傛牞鐦?`cmd/controller`閿涘本姣氶棁?6060 缁旑垰褰?`/debug/pprof`閿涘牆褰查悽?`HICLAW_PPROF_ADDR` 鐟曞棛娲婇敍澶婅嫙瀵偓閸?block/mutex 闁插洦鐗遍妴鍌炵帛鐠併倖鐎铏圭椽鐠?no-op stub閳ユ柡鈧柧绗夐崥?pprof 娴狅絿鐖滈妴浣风瑝瀵偓妫版繂顦荤粩顖氬經閿涘苯褰傜敮鍐殔閸嶅繘娴傜拫鍐槸闂堫潿鈧?

- **Human 鐠嬪啳鐨柅鈧柆澶哥瑢鐎涙顔岀€靛綊缍?*: `HumanStatus` 閺傛澘顤?`observedGeneration`閿涘牅绗?Worker/Team/Manager 鐎靛綊缍堥敍澶変簰閸?`consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`閵嗕痉nfra 婢惰精瑙﹂弨閫涜礋閹稿洦鏆熼柅鈧柆鍖＄礄30s 閳?10min 鐏忎線銆婇敍澶涚礉鏉╃偟鐢绘径杈Е 5 濞嗏€虫倵閸嬫粍顒涢懛顏勫З闁插秷鐦敍宀勨偓姘崇箖 `hiclaw.io/retry` 濞夈劏袙闁插秵鏌婇崥顖滄暏閿涙稐鎱ㄦ径宥勭啊閸樼喎鍘?`RequeueAfter` + error 閻ㄥ嫬寮婚柌?requeue 濡€崇础閵?

---

**Change list / 閸欐ɑ娲块崚妤勩€?*
- fix(controller): pass os.ErrNotExist through the SDK storage driver unwrapped 閳?restores os.IsNotExist first-create detection (generate-and-inject) that OpError wrapping had broken; skip non-UTF-8 file names in seed/mirror pushes ([9393e72](https://github.com/agentscope-ai/HiClaw/commit/9393e72))
- feat(shared): skip non-UTF-8 file names in mc mirror pushes 閳?base.sh is_utf8_name/collect_nonutf8_files helpers; worker-entrypoint.sh sync loop and update-worker-config.sh exclude them so one bad name cannot fail the whole push ([185a54e](https://github.com/agentscope-ai/HiClaw/commit/185a54e))
- feat(shared): ship base.sh in all images via shared/lib 閳?full log()/waitForService/waitForHTTP/generateKey, stale Manager-only comments corrected ([b1d31bb](https://github.com/agentscope-ai/HiClaw/commit/b1d31bb))
- feat(controller): expose storage connect/retry/probe tuning as `HICLAW_STORAGE_*` env vars 閳?connect timeout (`HICLAW_STORAGE_CONNECT_TIMEOUT_SECONDS`), retry window (`HICLAW_STORAGE_RETRY_WINDOW_SECONDS`), backoff base/cap (`HICLAW_STORAGE_RETRY_BACKOFF_MS` / `_MAX_MS`), SDK internal retries (`HICLAW_STORAGE_SDK_MAX_RETRIES`), config-phase probe (`HICLAW_STORAGE_PROBE_TIMEOUT_SECONDS`); defaults unchanged (2s / 30s / 500ms閳?s / 2 / 30s) ([7898244](https://github.com/agentscope-ai/HiClaw/commit/7898244))
- feat(controller): add madmin-go admin provider for embedded MinIO user/policy management, following the HICLAW_STORAGE_DRIVER switch (sdk default; mc admin CLI kept as legacy provider) ([7898244](https://github.com/agentscope-ai/HiClaw/commit/7898244))
- feat(controller): add minio-go S3 SDK storage driver 閳?HICLAW_STORAGE_DRIVER=sdk default with mc fallback, flaky-storage resilience (probe fast-abort, bounded retries), content-compare builtin skill push ([95dcae1](https://github.com/agentscope-ai/HiClaw/commit/95dcae1))
- feat(controller): optional no-requeue for converged Active teams 閳?HICLAW_TEAM_ACTIVE_NO_REQUEUE, plus team member reconcile flow metrics ([84a9429](https://github.com/agentscope-ai/HiClaw/commit/84a9429))
- fix(shared): add log fallback when base.sh is absent 閳?scripts fail on the real error instead of exit 127 ([c895d3a](https://github.com/agentscope-ai/HiClaw/commit/c895d3a))
- feat(controller): add storage stability and member reconcile metrics 閳?storage op duration/errors (op x driver x class), probe failures, member flow duration ([77ef4b8](https://github.com/agentscope-ai/HiClaw/commit/77ef4b8))
- feat(controller): add team-scoped reconcile telemetry 閳?ctx logger injection (team/teamUID), timed helper, failure-path elapsed, mc slow-call threshold logs, ProvisionWorker/modifyAIRoutes/ensureImage timing, panic guard ([ce1a531](https://github.com/agentscope-ai/HiClaw/commit/ce1a531))
- test(bench): add S3 reproduction benchmark under bench/ (mc vs minio-go SDK, real-bucket read-only) ([a12c5b3](https://github.com/agentscope-ai/HiClaw/commit/a12c5b3))
- feat(controller): add build-time pprof switch 閳?ENABLE_PPROF build arg with -tags pprof, no-op stub in default builds ([6cf6f86](https://github.com/agentscope-ai/HiClaw/commit/6cf6f86))
- feat(controller): make Active Team reconcile interval configurable with positive jitter ([462f84d](https://github.com/agentscope-ai/HiClaw/commit/462f84d))
- fix(controller): build hiclaw-controller image with shared/lib via named build context ([82a75e8](https://github.com/agentscope-ai/HiClaw/commit/82a75e8))
- feat(controller): add per-step Team reconcile timing logs, configurable max concurrency, and quieter STS INFO logs ([c01aaec](https://github.com/agentscope-ai/HiClaw/commit/c01aaec))
- fix(controller): unblock Team reconcile 閳?concurrency, failTeam backoff, observedGeneration fast path ([48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa))
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

- `c15ba378` fix(controller): join Team members to Team Room (#1149)
- `c2d84b09` fix(skills): complete manager and worker skill loading (#1153)
- `90f861c0` test(skills): verify Manager-to-Worker distribution (#1156)

**Also in this window / 閸氬本婀￠崗鏈电铂閸欐ɑ娲?*

- Archive the v1.2.1 changelog before collecting the v1.2.2 release window. ([#1146](https://github.com/agentscope-ai/AgentTeams/pull/1146))
- Add integration coverage for Manager-to-Worker Skill distribution through `Worker.spec.skills`, Worker storage, and the QwenPaw Skill API. ([#1156](https://github.com/agentscope-ai/AgentTeams/pull/1156))
