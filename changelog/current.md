# Changelog (Unreleased)

Target release: `v1.2.0`

Comparison baseline: `v1.2.0-beta.1`

Record release-facing changes here before the next release.

---

**Breaking Changes / Migration Notes**

- **AgentTeams naming is now the only active runtime contract**: Installer and Helm entrypoints, controller packages, the `agt` CLI, environment variables, runtime paths, resource names, and storage prefixes use the AgentTeams contract end to end. Retired HiClaw wrappers and active-source compatibility branches have been removed; deployments upgrading from older manifests or scripts must move to the AgentTeams names. ([#1063](https://github.com/agentscope-ai/AgentTeams/pull/1063), [#1065](https://github.com/agentscope-ai/AgentTeams/pull/1065))
- **Team and Worker resources use the final v1.2 contract**: Team resources reference independently managed Worker CRs through `spec.workerMembers`. Inline Worker members, registry migration paths, and dependent legacy scripts are no longer supported. ([#1072](https://github.com/agentscope-ai/AgentTeams/pull/1072))

**What's New**

- **Optional AgentTeams Dashboard**: Local installation can deploy the AgentTeams Dashboard for visual Worker, Team, Human, Manager, and Matrix management. Dashboard versioning remains independent from the AgentTeams release. ([#1075](https://github.com/agentscope-ai/AgentTeams/pull/1075), [#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **Worker push skips non-UTF-8 file names**: `base.sh` gains `is_utf8_name` / `collect_nonutf8_files` helpers (python3-based, safe for invalid byte sequences). The worker change-triggered sync loop and `update-worker-config.sh` build `mc mirror --exclude` lists from them, so a single non-UTF-8 file name no longer fails the whole push; the sync loop also ignores such files so it cannot spin on unpushable ones.

- **SDK first-create detection fix**: `os.ErrNotExist` now passes through the SDK storage driver unwrapped 鈥?service-layer `os.IsNotExist` checks (which peel only os-package error types, not arbitrary `Unwrap` chains) can again distinguish "object missing (first create)" from real storage failures, so initialization generate-and-injects missing configs (seeded agent files, SOUL.md, AGENTS.md, openclaw.json merge) instead of aborting. Seed/mirror pushes also skip files with non-UTF-8 names (S3 keys must be valid UTF-8) instead of failing the whole operation.

- **base.sh shipped to all images**: `shared/lib/base.sh` provides `log()`, `waitForService()`, `waitForHTTP()`, and `generateKey()` in every image (manager/worker/controller/copaw/hermes), so `hiclaw-env.sh` consumers get the full `log()` (timestamped with date) instead of the minimal fallback; stale comments claiming base.sh was Manager-only are corrected.

- **MinIO admin API via SDK**: Embedded-mode MinIO user/policy management (`EnsureUser`/`EnsurePolicy`/`DeleteUser` during member provisioning) now follows the `HICLAW_STORAGE_DRIVER` switch instead of always forking `mc admin` subprocesses. `sdk` (default) uses the madmin-go Admin API with the same connection-pooled transport and fast-fail dial timeout as the SDK storage driver; `mc` keeps the legacy `mc admin` CLI path for rollback/parity.

- **S3 SDK storage driver (default)**: The controller's object-storage layer now defaults to the minio-go S3 SDK (`HICLAW_STORAGE_DRIVER=sdk`), replacing the per-call `mc` subprocess fork with a connection-pooled HTTP client 鈥?~5.8脳 lower per-member config latency per the `bench_s3` measurements, with static long-lived AK/SK credentials (`HICLAW_FS_ACCESS_KEY`/`HICLAW_FS_SECRET_KEY`) for cloud S3. `HICLAW_STORAGE_DRIVER=mc` restores the legacy driver, and dynamic STS credential sources remain supported on both drivers.

- **Storage stability observability**: New Prometheus metrics expose S3 health and reconcile cost: `hiclaw_storage_op_duration_seconds` (per-op latency histogram, op 脳 driver), `hiclaw_storage_op_errors_total` (op 脳 driver 脳 class: network/timeout/not_found/other), `hiclaw_storage_probe_failures_total`, and `hiclaw_member_reconcile_duration_seconds` (per-member full flow: infra 鈫?SA 鈫?config 鈫?container 鈫?expose, kind 脳 result). A rising network/timeout series is the "storage endpoint unstable" alarm that previously showed up only as reconcile stalls.

- **Flaky-storage resilience**: `DeployWorkerConfig` probes storage reachability first (30s bounded, matching the retry window) so a short OSS blip recovers inside the window instead of failing the pass; every SDK operation uses a **2s connect timeout** with transient-failure retries within a **30s total window** (exponential backoff 0.5s鈫?s cap; deterministic 4xx/missing-key errors never retried); the mc driver shares the same 30s retry window. A permanently dead endpoint still aborts and requeues with a concise status message instead of stalling on the CLI's 30s dial per op.

- **Package downloads via the SDK driver**: `PackageResolver` now downloads MinIO/OSS packages through the same `StorageClient` (minio-go SDK) instead of forking `mc cp`/`mc stat` subprocesses, which bypassed the driver selection and carried the CLI's own 30s dial timeout 鈥?this was the source of `deploy package: package resolve/extract failed: ... mc: unable to prepare URL for copying` under OSS jitter. ETag-based content caching (skip re-download when the etag-named local cache file exists) and atomic temp+rename writes are preserved; `mc` remains only as a nil-storage fallback.

- **Concise Status.Message on reconcile failure**: Storage-layer errors are now single-layer (`storage get <key>: dial tcp ...: i/o timeout`) via `oss.OpError`, and Worker/Manager/Human/Team controllers write a condensed root-cause message (opaque subprocess-exit leaves skipped, 512-char cap) to `status.message` instead of the full multi-layer wrap chain.

- **Worker-management scripts log fallback**: `push-worker-skills.sh`, `generate-worker-config.sh`, and `enable-peer-mentions.sh` now define a defensive `log()` when `hiclaw-env.sh`/`base.sh` did not provide one, so a failed `mc` call logs its `WARNING` properly instead of aborting with a misleading `log: command not found` in images without `base.sh` (controller/worker) or when the shared lib failed to load.

- **Builtin skill push content-compare**: `pushBuiltinSkills` now pushes per-file, skipping unchanged objects (GET-compare instead of a full `mc mirror --overwrite` re-upload), cutting the per-member skill phase from ~25鈥?5s to a few seconds while still propagating skill updates from new controller images.

- **Active Team no-requeue mode**: `HICLAW_TEAM_ACTIVE_NO_REQUEUE=true` stops the periodic requeue for fully converged Active Teams whose spec is unchanged 鈥?they reconcile only on events (pod phase changes, spec edits) instead of on the 5m timer. Default false preserves the existing periodic behavior.

- **On-demand skill push off by default**: The controller-side local skill push (`spec.skills` via `push-worker-skills.sh`) is skipped by default (`HICLAW_LOCAL_SKILL_PUSH=true` re-enables it) 鈥?the script reads the Manager's local `workers-registry.json`, which does not exist in the controller container, so the push always failed there. Remote (nacos) skills still push via Go.

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

- **Worker storage sync I/O amplification**: Upload changed workspace files once per successful watermark, keep jq 1.7 fallback pulls alive, and limit embedded Controller mirrors to control-plane configuration. Concurrent Worker creation and large unknown workspace paths retain their existing persistence semantics without repeated whole-workspace mirrors. ([#1110](https://github.com/agentscope-ai/AgentTeams/pull/1110))
- **CoPaw Team routing and workspace projection**: Route Team Leader assignments to the Team Room, including localpart mentions, and project Worker prompts, skills, tool configuration, and Matrix settings into CoPaw's default workspace. ([#1060](https://github.com/agentscope-ai/AgentTeams/pull/1060), [9074def](https://github.com/agentscope-ai/AgentTeams/commit/9074def3), [973e291](https://github.com/agentscope-ai/AgentTeams/commit/973e291), [92c8145](https://github.com/agentscope-ai/AgentTeams/commit/92c8145))
- **Team room and Worker lifecycle convergence**: Keep referenced Worker CRs protected, enforce required Team roles, remove Manager from regular Team Worker personal rooms, and restore standalone membership when a Worker leaves a Team. ([d96f1ed](https://github.com/agentscope-ai/AgentTeams/commit/d96f1ed), [43545c2](https://github.com/agentscope-ai/AgentTeams/commit/43545c2), [b5b0add](https://github.com/agentscope-ai/AgentTeams/commit/b5b0add), [a5d6435](https://github.com/agentscope-ai/AgentTeams/commit/a5d6435))
- **Pre-v1.2 installer compatibility**: Select the legacy environment contract and storage prefix for v1.1.2 images while keeping the canonical AgentTeams contract for v1.2.0 and newer images. Custom version input such as `1.2.0.beta.1` is normalized to the published tag form. ([#1079](https://github.com/agentscope-ai/AgentTeams/pull/1079), [#1100](https://github.com/agentscope-ai/AgentTeams/pull/1100))
- **Dashboard installation reliability**: Preserve quick-start and keep-all behavior, restore Worker credentials during upgrade, improve gateway probing and verification, and clean up Dashboard data on uninstall. ([#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **Tooling and diagnostics safety**: Reject unsafe plugin archive links, redact complete Matrix events in debug bundles, generate runnable Worker ZIP import commands, analyze current OpenClaw cron files, capture immediate replay replies, and persist containerized skopeo authentication. ([#1043](https://github.com/agentscope-ai/AgentTeams/pull/1043), [#1045](https://github.com/agentscope-ai/AgentTeams/pull/1045), [#1047](https://github.com/agentscope-ai/AgentTeams/pull/1047), [#1048](https://github.com/agentscope-ai/AgentTeams/pull/1048), [#1049](https://github.com/agentscope-ai/AgentTeams/pull/1049), [#1050](https://github.com/agentscope-ai/AgentTeams/pull/1050))

- **Team reconcile unblocking**: Team reconcile parallelism is configurable via `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` (default 1, preserving legacy serial behavior; raise it so one slow/hung Team can no longer stall every other Team, including newly created ones stuck in Phase ""/Pending). An optional per-pass deadline (`HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS`, disabled by default) bounds a single pass against hung external calls; `failTeam` uses exponential backoff (30s 鈫?10min cap) and stops requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation; finalizer add/remove use merge patches instead of `Update`. `TeamStatus` gains `observedGeneration`, so unchanged Active teams skip the full provisioning chain after a controller restart or informer re-sync; the periodic requeue for converged teams defaults to 5m, is configurable via `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS`, and carries 0鈥?0% positive jitter so teams do not wake in lockstep.

- **Team reconcile observability**: Each Team reconcile pass logs per-step timing (`team reconcile: admin actor / step 1 rooms / step 1 storage / 鈥), and `DeployWorkerConfig` logs per-phase upload timing, so slow steps (e.g. hung object-storage syncs) are identifiable at a glance. Per-request STS credential INFO logs are commented out (not removed) to reduce log noise; WARN/ERROR paths still log.

- **Team reconcile telemetry**: `Reconcile` injects a team-scoped logger (`team=<name>`, `teamUID=<uid>`) into the context so every downstream layer (deployer, oss, gateway, backend) is tagged with the Team identity 鈥?`grep "team=<name>"` covers the whole reconcile span. A unified `timed` helper logs elapsed on success, failure, and cancellation, and member phases now log elapsed on failure too (previously success-only). The oss layer logs `mc slow call` for any mc invocation over 300ms (with op type) to quantify the S3-layer latency distribution; `ProvisionWorker` logs per-step elapsed (Matrix register / MinIO user / room / join / gateway consumer / AI-route auth); `modifyAIRoutes` logs overall elapsed plus 409-conflict retry count; Docker image pulls log completion; a panic guard records reconcile panics with team context and requeues through the error path.

- **S3 benchmark harness**: New `bench/` module with `bench_s3.go` 鈥?a read-only reproduction benchmark over the real scenario bucket (mc subprocess vs minio-go SDK drivers, config-phase op mix 12 GET + 3 PUT + 2 STAT + 1 LIST, per-op latency percentiles + per-round wall-clock; write space isolated under `bench-probe/` and auto-cleaned).

- **Debug pprof build switch**: The controller image build accepts `ENABLE_PPROF=true` (`--build-arg`), compiling `cmd/controller` with `-tags pprof` to expose `/debug/pprof` on port 6060 (`HICLAW_PPROF_ADDR` overridable) with block/mutex sampling enabled. Default builds compile a no-op stub 鈥?no pprof code, no extra listener, zero debug surface in release images.

- **Human reconcile backoff and parity**: `HumanStatus` gains `observedGeneration` (parity with Worker/Team/Manager) plus `consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`. Infra failures now use exponential backoff (30s 鈫?10min cap) and stop requeuing after 5 consecutive failures until re-armed via the `hiclaw.io/retry` annotation, replacing the previous double-requeue (`RequeueAfter` + rate-limiter error) pattern.

---

**鐮村潖鎬у彉鏇?/ 鍗囩骇璇存槑**

- **AgentTeams 鍛藉悕鎴愪负鍞竴鐢熸晥鐨勮繍琛屾椂濂戠害**锛氬畨瑁呭櫒涓?Helm 鍏ュ彛銆丆ontroller 鍖呫€乣agt` CLI銆佺幆澧冨彉閲忋€佽繍琛屾椂璺緞銆佽祫婧愬悕绉板拰瀛樺偍鍓嶇紑宸茬鍒扮缁熶竴涓?AgentTeams銆傛棫 HiClaw wrapper 鍜屾椿璺冩簮鐮佷腑鐨勫吋瀹瑰垎鏀凡绉婚櫎锛涗粠鏃ф竻鍗曟垨鑴氭湰鍗囩骇鏃跺繀椤诲垏鎹㈠埌 AgentTeams 鍛藉悕銆?[#1063](https://github.com/agentscope-ai/AgentTeams/pull/1063), [#1065](https://github.com/agentscope-ai/AgentTeams/pull/1065))
- **Team 涓?Worker 璧勬簮浣跨敤 v1.2 鏈€缁堝绾?*锛歍eam 閫氳繃 `spec.workerMembers` 寮曠敤鐙珛绠＄悊鐨?Worker CR锛涗笉鍐嶆敮鎸佸唴鑱?Worker 鎴愬憳銆乺egistry 杩佺Щ璺緞鍙婂叾渚濊禆鐨勬棫鑴氭湰銆?[#1072](https://github.com/agentscope-ai/AgentTeams/pull/1072))

**鏂板鍔熻兘**

- **鍙€?AgentTeams Dashboard**锛氭湰鍦板畨瑁呭彲閮ㄧ讲 AgentTeams Dashboard锛岀敤浜庡彲瑙嗗寲绠＄悊 Worker銆乀eam銆丠uman銆丮anager 鍜?Matrix锛汥ashboard 鐗堟湰缁х画涓?AgentTeams 鐗堟湰鐙珛銆?[#1075](https://github.com/agentscope-ai/AgentTeams/pull/1075), [#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **Worker 鎺ㄩ€佽烦杩囬潪 UTF-8 鏂囦欢鍚?*: `base.sh` 鏂板 `is_utf8_name` / `collect_nonutf8_files` 杈呭姪鍑芥暟锛坧ython3 鏍￠獙锛屽鏃犳晥瀛楄妭瀹夊叏锛夈€倃orker 鍙樻洿瑙﹀彂鍚屾寰幆鍜?`update-worker-config.sh` 鎹鏋勯€?`mc mirror --exclude`锛屽崟涓潪 UTF-8 鏂囦欢鍚嶄笉鍐嶅鑷存暣鎵规帹閫佸け璐ワ紱鍚屾寰幆鍚屾椂蹇界暐杩欑被鏂囦欢锛岄伩鍏嶇┖杞€?

- **SDK 棣栨鍒涘缓鍒ゅ畾淇**: SDK 瀛樺偍椹卞姩瀵?`os.ErrNotExist` 涓嶅啀鍋?`OpError` 鍖呰锛岀洿鎺ラ€忎紶鈥斺€攕ervice 灞備緷璧栫殑 `os.IsNotExist`锛堝彧鍓?os 鍖呴敊璇被鍨嬨€佷笉閫掑綊浠绘剰 Unwrap锛夋仮澶嶇敓鏁堬紝鍒濆鍖栨椂 OSS 閰嶇疆鏂囦欢涓嶅瓨鍦ㄤ細琚纭瘑鍒负棣栨鍒涘缓锛堢敓鎴愭敞鍏ワ細绉嶅瓙鏂囦欢銆丼OUL.md銆丄GENTS.md銆乷penclaw.json 鍚堝苟锛夛紝涓嶅啀鎶ラ敊涓柇锛涙帹閫佸璞℃椂璺宠繃闈?UTF-8 鏂囦欢鍚嶇殑鏂囦欢锛圫3 key 蹇呴』鍚堟硶 UTF-8锛夛紝閬垮厤鍗曚釜鍧忔枃浠跺悕瀵艰嚧鏁存壒鎺ㄩ€佸け璐ャ€?

- **base.sh 闅?shared/lib 杩涘叆鎵€鏈夐暅鍍?*: `shared/lib/base.sh` 鎻愪緵 `log()`銆乣waitForService()`銆乣waitForHTTP()`銆乣generateKey()`锛宮anager/worker/controller/copaw/hermes 闀滃儚鍧囧寘鍚紝`hiclaw-env.sh` 鐨勬秷璐规柟鑴氭湰鎷垮埌瀹屾暣 `log()`锛堝甫鏃ユ湡鏃堕棿鎴筹級鑰屼笉鍐嶆槸鏈€灏?fallback锛涘悓姝ヤ慨姝ｄ簡 "base.sh 浠?Manager 鐢? 鐨勮繃鏃舵敞閲娿€?

- **MinIO Admin API 璧?SDK**: embedded MinIO 鐨勭敤鎴?绛栫暐绠＄悊锛堟垚鍛樿皟璋愪腑鐨?`EnsureUser`/`EnsurePolicy`/`DeleteUser`锛変笉鍐嶅浐瀹?fork `mc admin` 瀛愯繘绋嬶紝鏀逛负璺熼殢 `HICLAW_STORAGE_DRIVER` 鍒囨崲锛歚sdk`锛堥粯璁わ級鐢?madmin-go Admin API锛屽鐢?SDK 瀛樺偍椹卞姩鐨勮繛鎺ユ睜涓庡揩閫熷け璐?dial 瓒呮椂锛沗mc` 淇濈暀鍘?`mc admin` CLI 璺緞浠ヤ究鍥炴粴/瀵规瘮銆?

- **鏈湴瀹夎榛樿浼樺厛 QwenPaw**: 瀹夎鑴氭湰鐜板湪浼樺厛灞曠ず QwenPaw 浣滀负榛樿 Worker 杩愯鏃讹紝鍗囩骇鏃舵敮鎸?keep-all 鍜屽洖杞︿繚鐣欏凡鏈夊弬鏁帮紝骞跺己鍖栦簡闈炰氦浜掓ā寮忎笅鐨勯槻璇墽琛屼繚鎶ゃ€?

- **Team 鏀寔浜虹被鍗忚皟鍛?*: Team 璧勬簮鏀寔澹版槑浜虹被鍗忚皟鍛樻垚鍛橈紝Team Room 鐢?team-admin 褰掑睘锛屽苟鍚屾鏇存柊 Team Leader / Worker 鎻愮ず璇嶏紝纭繚鍗忎綔鏀舵暃鍦?Team Room 涓€?

- **Team Leader 鍗忎綔鑳藉姏鍒锋柊**: Team Leader 鍐呯疆鑳藉姏鍥寸粫椤圭洰瑙勫垝銆丏AG 浠诲姟鎵ц銆佹枃浠跺叡浜€佹矡閫氥€佺粍缁囥€乵cporter 浣跨敤鍜?Worker 鐢熷懡鍛ㄦ湡鍗忎綔閲嶆柊鏁寸悊锛涘悓姝?Worker 鐨?anti-loop 鍥炲瑙勫垯锛涜縼绉诲畬鎴愬悗绉婚櫎浜嗘棫鐨?Team Leader 鎶€鑳藉埆鍚嶃€?

- **CoPaw 杩愯鏃跺崗浣滃伐鍏?*: CoPaw Worker 鏂板浠诲姟娴併€侀」鐩祦銆佹秷鎭€佹枃浠跺悓姝ャ€佽緭鍑烘竻娲椼€佸嚟鎹繚鎶ゃ€佸仴搴锋帰閽堛€佹洿瀹屾暣鐨勫氨缁鏌ョ浉鍏?hooks / tools锛屽苟鏀寔閰嶇疆 ReAct 鏈€澶ц凯浠ｆ鏁般€?

- **Nacos 杩滅▼鎶€鑳戒笌鍑嵁**: 鎺у埗鍣ㄥ彲鍚?Worker 浼犻€?skills API 榛樿鍊煎拰姣忎釜鍖呯殑 Nacos 璁よ瘉閰嶇疆锛屾敮鎸?`authType=nacos|sts-hiclaw|none` 浠ュ強 `ai-registry` STS 鏉冮檺鑼冨洿銆?

- **Worker 韬唤瑙ｈ€?*: 鎺у埗鍣ㄨ祫婧愬悕涓庤繍琛屾椂 Worker 鍚嶇О鍦ㄨ韩浠姐€佸嚟鎹€佸瓨鍌ㄩ粯璁ゅ€煎拰灏辩华鐘舵€佷腑瑙ｈ€︼紝闄嶄綆 CR 鍚嶇О涓?Agent 瀵瑰鍚嶇О鐨勮€﹀悎銆?

- **鎺у埗鍣ㄥ彲瑙傛祴鎬?*: 澧炲姞鎺у埗鍣?reconcile 鎸囨爣銆丠TTP 鏈嶅姟涓庡悗鍙?goroutine 鐨勪紭闆呴€€鍑猴紝浠ュ強娴嬭瘯澶辫触璇婃柇淇℃伅锛屼究浜庢帓鏌ヨ繍琛屾椂鍜?CI 闂銆?

**Bug 淇**

- **Worker 瀛樺偍鍚屾 I/O 鏀惧ぇ**锛氬熀浜庢垚鍔?watermark 鍙笂浼犲彉鍖栨枃浠讹紝淇濇寔 jq 1.7 fallback pull 瀛樻椿锛屽苟灏?embedded Controller mirror 闄愬畾涓烘帶鍒堕潰閰嶇疆銆傚苟鍙戝垱寤?Worker 鍜屾湭鐭ュ伐浣滅洰褰曚粛淇濇寔鍘熸湁鎸佷箙鍖栬涔夛紝涓嶅啀鍙嶅鎵ц鍏ㄩ噺 workspace mirror銆?[#1110](https://github.com/agentscope-ai/AgentTeams/pull/1110))
- **CoPaw Team 璺敱涓?workspace 鎶曞奖**锛氬皢 Team Leader 鍒嗛厤锛堝寘鎷?localpart mention锛夎矾鐢卞埌 Team Room锛屽苟鎶?Worker prompt銆乻kills銆佸伐鍏烽厤缃拰 Matrix 璁剧疆鎶曞奖鍒?CoPaw 榛樿 workspace銆?[#1060](https://github.com/agentscope-ai/AgentTeams/pull/1060), [9074def](https://github.com/agentscope-ai/AgentTeams/commit/9074def3), [973e291](https://github.com/agentscope-ai/AgentTeams/commit/973e291), [92c8145](https://github.com/agentscope-ai/AgentTeams/commit/92c8145))
- **Team Room 涓?Worker 鐢熷懡鍛ㄦ湡鏀舵暃**锛氫繚鎶よ寮曠敤鐨?Worker CR锛屽己鍒舵牎楠?Team 蹇呭～瑙掕壊锛屽皢 Manager 绉诲嚭鏅€?Team Worker 鐨勪釜浜烘埧闂达紝骞跺湪 Worker 绂诲紑 Team 鍚庢仮澶?standalone 鎴愬憳鍏崇郴銆?[d96f1ed](https://github.com/agentscope-ai/AgentTeams/commit/d96f1ed), [43545c2](https://github.com/agentscope-ai/AgentTeams/commit/43545c2), [b5b0add](https://github.com/agentscope-ai/AgentTeams/commit/b5b0add), [a5d6435](https://github.com/agentscope-ai/AgentTeams/commit/a5d6435))
- **v1.2 涔嬪墠闀滃儚鐨勫畨瑁呭吋瀹?*锛歷1.1.2 闀滃儚浣跨敤鏃х幆澧冨彉閲忓绾﹀拰瀛樺偍鍓嶇紑锛寁1.2.0 鍙婃洿鏂伴暅鍍忎娇鐢?AgentTeams 濂戠害锛沗1.2.0.beta.1` 绛夎嚜瀹氫箟杈撳叆浼氳鑼冨寲涓哄凡鍙戝竷鐨?Tag 鏍煎紡銆?[#1079](https://github.com/agentscope-ai/AgentTeams/pull/1079), [#1100](https://github.com/agentscope-ai/AgentTeams/pull/1100))
- **Dashboard 瀹夎鍙潬鎬?*锛氫繚鐣?quick-start 涓?keep-all 琛屼负锛屽崌绾ф椂鎭㈠ Worker 鍑嵁锛屾敼杩涚綉鍏虫帰娴嬩笌瀹夎楠岃瘉锛屽苟鍦ㄥ嵏杞芥椂娓呯悊 Dashboard 鏁版嵁銆?[#1081](https://github.com/agentscope-ai/AgentTeams/pull/1081))
- **宸ュ叿涓庤瘖鏂畨鍏ㄦ€?*锛氭嫆缁濅笉瀹夊叏鐨勬彃浠跺綊妗ｉ摼鎺ワ紝瀹屾暣鑴辨晱璋冭瘯鍖呬腑鐨?Matrix 浜嬩欢锛岀敓鎴愬彲鐩存帴杩愯鐨?Worker ZIP 瀵煎叆鍛戒护锛岃瘑鍒綋鍓?OpenClaw cron 鏍煎紡锛屾崟鑾峰嵆鏃?replay 鍥炲锛屽苟鎸佷箙鍖栧鍣ㄥ寲 skopeo 鐨勮璇佷俊鎭€?[#1043](https://github.com/agentscope-ai/AgentTeams/pull/1043), [#1045](https://github.com/agentscope-ai/AgentTeams/pull/1045), [#1047](https://github.com/agentscope-ai/AgentTeams/pull/1047), [#1048](https://github.com/agentscope-ai/AgentTeams/pull/1048), [#1049](https://github.com/agentscope-ai/AgentTeams/pull/1049), [#1050](https://github.com/agentscope-ai/AgentTeams/pull/1050))

- **Team 璋冭皭瑙ｉ櫎闃诲**: Team 璋冭皭骞跺彂搴﹀彲閫氳繃 `HICLAW_TEAM_MAX_CONCURRENT_RECONCILES` 閰嶇疆锛堥粯璁?1锛屼繚鎸佸師鏈変覆琛岃涓猴紱璋冮珮鍚庡崟涓?Team 缂撴參鎴栨寕璧蜂笉鍐嶆嫋鍨叾浠栨墍鏈?Team锛屽惈鏂板缓銆佸仠鍦?Phase ""/Pending 鐨勶級銆傚彲閫氳繃 `HICLAW_TEAM_RECONCILE_TIMEOUT_SECONDS` 寮€鍚崟娆¤皟璋愯秴鏃讹紙榛樿鍏抽棴锛屼繚鎸佸師鏈夎涓猴級锛沗failTeam` 鏀逛负鎸囨暟閫€閬匡紙30s 鈫?10min 灏侀《锛夛紝杩炵画澶辫触 5 娆″悗鍋滄鑷姩閲嶈瘯锛岄€氳繃 `hiclaw.io/retry` 娉ㄨВ閲嶆柊鍚敤锛沠inalizer 澧炲垹鏀圭敤 merge patch 鑰岄潪 `Update`銆俙TeamStatus` 鏂板 `observedGeneration`锛屾帶鍒跺櫒閲嶅惎鎴?informer re-sync 鍚庢湭鍙樻洿鐨?Active Team 璺宠繃鍏ㄩ噺璋冭皭閾撅紱宸叉敹鏁?Team 鐨勫懆鏈?requeue 榛樿 5min锛屽彲閫氳繃 `HICLAW_TEAM_RECONCILE_INTERVAL_SECONDS` 閰嶇疆锛屽苟甯?0鈥?0% 姝ｅ悜鎶栧姩閬垮厤鍚?Team 鍚屾椂鍞ら啋銆?

- **Team 璋冭皭鍙娴嬫€?*: Team 璋冭皭姣忔鎵ц鎸夋楠ゆ墦鍗拌€楁椂鏃ュ織锛坄team reconcile: admin actor / step 1 rooms / step 1 storage / 鈥锛夛紝`DeployWorkerConfig` 鎵撳嵃鍚勪笂浼犻樁娈佃€楁椂锛屼究浜庡揩閫熷畾浣嶆參姝ラ锛堝瀵硅薄瀛樺偍鍚屾鎸傝捣锛夈€係TS 鍑嵁閫愯姹?INFO 鏃ュ織鏀逛负娉ㄩ噴淇濈暀锛堜笉鍒犻櫎锛変互闄嶄綆鏃ュ織鍣０锛沇ARN/ERROR 璺緞浠嶄細鎵撳嵃銆?

- **Team 璋冭皭閬ユ祴澧炲己**: `Reconcile` 鍏ュ彛灏?team-scoped logger锛坄team=<name>`銆乣teamUID=<uid>`锛夋敞鍏?context锛屼笅娓稿悇灞傦紙deployer/oss/gateway/backend锛夋棩蹇楄嚜鍔ㄦ惡甯?Team 韬唤锛宍grep "team=<name>"` 鍗冲彲瑕嗙洊鏁存潯璋冭皭閾捐矾銆傛柊澧炵粺涓€ `timed` helper锛屾垚鍔?澶辫触/鍙栨秷鍧囪褰?elapsed锛沵ember 鍚勯樁娈靛け璐ヨ矾寰勮ˉ涓?elapsed锛堝師鍏堜粎鎴愬姛鏃舵墦鍗帮級銆侽SS 灞傚瓒呰繃 300ms 鐨?mc 璋冪敤鎵?`mc slow call`锛堝惈 op 绫诲瀷锛夛紝閲忓寲 S3 灞傚崟娆¤皟鐢ㄥ欢杩熷垎甯冿紱`ProvisionWorker` 鍚勬楠わ紙Matrix 娉ㄥ唽 / MinIO 鐢ㄦ埛 / 寤烘埧 / join / gateway consumer / AI 璺敱鎺堟潈锛夎ˉ elapsed锛沗modifyAIRoutes` 璁板綍鏁翠綋鑰楁椂涓?409 鍐茬獊閲嶈瘯娆℃暟锛汥ocker 闀滃儚鎷夊彇瀹屾垚琛ユ棩蹇楋紱鏂板 panic 鍏滃簳锛宲anic 鏃跺甫 team 涓婁笅鏂囪褰曞苟璧伴敊璇矾寰?requeue銆?

- **S3 鍩哄噯澶嶇幇宸ュ叿**: 鏂板 `bench/` module锛屽惈 `bench_s3.go` 鈥斺€?澶嶇敤鐪熷疄鍦烘櫙妗剁殑鍙澶嶇幇鍩哄噯锛坢c 瀛愯繘绋?vs minio-go SDK 鍙岄┍鍔紝瀵归綈 config 闃舵鎿嶄綔閰嶆瘮 12 GET + 3 PUT + 2 STAT + 1 LIST锛岃緭鍑哄悇鎿嶄綔寤惰繜鍒嗕綅鏁颁笌鍗曟垚鍛樿疆娆″閽熻€楁椂锛涘啓绌洪棿闅旂鍦?`bench-probe/` 鍓嶇紑涓嬪苟鑷姩娓呯悊锛夈€?

- **pprof 璋冭瘯鏋勫缓寮€鍏?*: 鎺у埗鍣ㄩ暅鍍忔瀯寤烘敮鎸?`ENABLE_PPROF=true`锛坄--build-arg`锛夛紝浠?`-tags pprof` 缂栬瘧 `cmd/controller`锛屾毚闇?6060 绔彛 `/debug/pprof`锛堝彲鐢?`HICLAW_PPROF_ADDR` 瑕嗙洊锛夊苟寮€鍚?block/mutex 閲囨牱銆傞粯璁ゆ瀯寤虹紪璇?no-op stub鈥斺€斾笉鍚?pprof 浠ｇ爜銆佷笉寮€棰濆绔彛锛屽彂甯冮暅鍍忛浂璋冭瘯闈€?

- **Human 璋冭皭閫€閬夸笌瀛楁瀵归綈**: `HumanStatus` 鏂板 `observedGeneration`锛堜笌 Worker/Team/Manager 瀵归綈锛変互鍙?`consecutiveFailures` / `maxRetriesReached` / `phaseTransitionTime`銆侷nfra 澶辫触鏀逛负鎸囨暟閫€閬匡紙30s 鈫?10min 灏侀《锛夛紝杩炵画澶辫触 5 娆″悗鍋滄鑷姩閲嶈瘯锛岄€氳繃 `hiclaw.io/retry` 娉ㄨВ閲嶆柊鍚敤锛涗慨澶嶄簡鍘熷厛 `RequeueAfter` + error 鐨勫弻閲?requeue 妯″紡銆?

---

**Change list / 鍙樻洿鍒楄〃**
- fix(controller): pass os.ErrNotExist through the SDK storage driver unwrapped 鈥?restores os.IsNotExist first-create detection (generate-and-inject) that OpError wrapping had broken; skip non-UTF-8 file names in seed/mirror pushes ([9393e72](https://github.com/agentscope-ai/HiClaw/commit/9393e72))
- feat(shared): skip non-UTF-8 file names in mc mirror pushes 鈥?base.sh is_utf8_name/collect_nonutf8_files helpers; worker-entrypoint.sh sync loop and update-worker-config.sh exclude them so one bad name cannot fail the whole push ([185a54e](https://github.com/agentscope-ai/HiClaw/commit/185a54e))
- feat(shared): ship base.sh in all images via shared/lib 鈥?full log()/waitForService/waitForHTTP/generateKey, stale Manager-only comments corrected ([b1d31bb](https://github.com/agentscope-ai/HiClaw/commit/b1d31bb))
- feat(controller): expose storage connect/retry/probe tuning as `HICLAW_STORAGE_*` env vars 鈥?connect timeout (`HICLAW_STORAGE_CONNECT_TIMEOUT_SECONDS`), retry window (`HICLAW_STORAGE_RETRY_WINDOW_SECONDS`), backoff base/cap (`HICLAW_STORAGE_RETRY_BACKOFF_MS` / `_MAX_MS`), SDK internal retries (`HICLAW_STORAGE_SDK_MAX_RETRIES`), config-phase probe (`HICLAW_STORAGE_PROBE_TIMEOUT_SECONDS`); defaults unchanged (2s / 30s / 500ms鈫?s / 2 / 30s) ([7898244](https://github.com/agentscope-ai/HiClaw/commit/7898244))
- feat(controller): add madmin-go admin provider for embedded MinIO user/policy management, following the HICLAW_STORAGE_DRIVER switch (sdk default; mc admin CLI kept as legacy provider) ([7898244](https://github.com/agentscope-ai/HiClaw/commit/7898244))
- feat(controller): add minio-go S3 SDK storage driver 鈥?HICLAW_STORAGE_DRIVER=sdk default with mc fallback, flaky-storage resilience (probe fast-abort, bounded retries), content-compare builtin skill push ([95dcae1](https://github.com/agentscope-ai/HiClaw/commit/95dcae1))
- feat(controller): optional no-requeue for converged Active teams 鈥?HICLAW_TEAM_ACTIVE_NO_REQUEUE, plus team member reconcile flow metrics ([84a9429](https://github.com/agentscope-ai/HiClaw/commit/84a9429))
- fix(shared): add log fallback when base.sh is absent 鈥?scripts fail on the real error instead of exit 127 ([c895d3a](https://github.com/agentscope-ai/HiClaw/commit/c895d3a))
- feat(controller): add storage stability and member reconcile metrics 鈥?storage op duration/errors (op x driver x class), probe failures, member flow duration ([77ef4b8](https://github.com/agentscope-ai/HiClaw/commit/77ef4b8))
- feat(controller): add team-scoped reconcile telemetry 鈥?ctx logger injection (team/teamUID), timed helper, failure-path elapsed, mc slow-call threshold logs, ProvisionWorker/modifyAIRoutes/ensureImage timing, panic guard ([ce1a531](https://github.com/agentscope-ai/HiClaw/commit/ce1a531))
- test(bench): add S3 reproduction benchmark under bench/ (mc vs minio-go SDK, real-bucket read-only) ([a12c5b3](https://github.com/agentscope-ai/HiClaw/commit/a12c5b3))
- feat(controller): add build-time pprof switch 鈥?ENABLE_PPROF build arg with -tags pprof, no-op stub in default builds ([6cf6f86](https://github.com/agentscope-ai/HiClaw/commit/6cf6f86))
- feat(controller): make Active Team reconcile interval configurable with positive jitter ([462f84d](https://github.com/agentscope-ai/HiClaw/commit/462f84d))
- fix(controller): build hiclaw-controller image with shared/lib via named build context ([82a75e8](https://github.com/agentscope-ai/HiClaw/commit/82a75e8))
- feat(controller): add per-step Team reconcile timing logs, configurable max concurrency, and quieter STS INFO logs ([c01aaec](https://github.com/agentscope-ai/HiClaw/commit/c01aaec))
- fix(controller): unblock Team reconcile 鈥?concurrency, failTeam backoff, observedGeneration fast path ([48ce4aa](https://github.com/agentscope-ai/HiClaw/commit/48ce4aa))
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

- `688ec362` fix(cli): reject unsafe plugin archive links (#1043)
- `dc4aafb4` fix(scripts): redact complete Matrix events (#1047)
- `31d89998` fix(migrate): analyze current cron job format (#1049)
- `ced0f183` fix(migrate): print runnable ZIP import command (#1048)
- `47b4d07a` fix(replay): capture immediate manager replies (#1045)
- `bdc4f640` fix(hack): persist containerized skopeo auth (#1050)
- `ad941d26` chore: archive changelog for v1.2.0-beta.1 (#1058)
- `82ef6d24` fix(copaw): route Team Leader assignments to Team Room (#1060)
- `e84c67ad` fix(install): allow selecting the docker.sock mounted by the installer (#553)
- `c6249864` docs: clarify Element homeserver port (#978)
- `ad3f661c` docs: clarify Higress AI route matching (#980)
- `f301d4d2` docs: clarify OpenAI-compatible provider setup (#1013)
- `e6fa64c0` refactor: complete AgentTeams runtime rename (#1063)
- `687b6d94` refactor: complete the AgentTeams hard-cut rename (#1065)
- `2540c968` docs: add v1.2.0-beta.1 release news (#1066)
- `0ff89f07` ci: make integration tests safe for fork PRs (#1073)
- `37c31b77` refactor: hard cut Team and Worker CR contracts (#1072)
- `82cbd5fe` feat(install): integrate AgentTeams Dashboard as an optional component (#1075)
- `785c2db5` fix(install): support pre-v1.2 image environment contract (#1079)
- `8de237da` fix(dashboard): quick-start, Worker credential, and verification follow-ups (#1081)
- `c789b706` docs: update Chinese README with new features and releases (#1092)
- `be1f0481` docs: add AgentLoop integration to README (#1093)
- `fcd4297e` fix(install): use legacy storage prefix for pre-v1.2 images (#1100)
- `7ba2efba` docs: update AgentLoop link in Chinese README (#1108)
- `5aec8d96` docs: update AgentLoop link in English README (#1109)
- `45fd4db2` fix: remove Worker storage sync I/O amplification (#1110)

**Also in this window / 鍚屾湡鍏朵粬鍙樻洿**

- Documentation clarifies Element homeserver ports, Higress AI route matching, OpenAI-compatible provider setup, AgentLoop integration, and the v1.2.0-beta.1 release experience. ([#978](https://github.com/agentscope-ai/AgentTeams/pull/978), [#980](https://github.com/agentscope-ai/AgentTeams/pull/980), [#1013](https://github.com/agentscope-ai/AgentTeams/pull/1013), [#1066](https://github.com/agentscope-ai/AgentTeams/pull/1066), [#1092](https://github.com/agentscope-ai/AgentTeams/pull/1092), [#1093](https://github.com/agentscope-ai/AgentTeams/pull/1093))
- Integration Tests now use the unprivileged `pull_request` context for fork and Dependabot changes while preserving the complete trusted-branch matrix. ([#1073](https://github.com/agentscope-ai/AgentTeams/pull/1073))
