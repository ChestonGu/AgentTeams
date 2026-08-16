# v1.1.2 -> dev-v1.2.2 迁移坑分析（Helm / K8s 场景）

> 日期: 2026-08-16
> 基线: 生产环境为基于 tag `v1.1.2`（HiClaw 命名时代）魔改分支的 Helm 部署；目标为 `dev-v1.2.2`（AgentTeams 命名，含 Synapse 支持、Team 契约重写、调谐参数 env 化）
> 环境前提: **K8s 集群 Helm 部署 + 外接 S3 endpoint（静态 AK）+ Synapse homeserver（fork 自加）**
> 关联文档: [cimicode-runtime-integration.md](cimicode-runtime-integration.md)（迁移稳定后的新运行时接入）、[controller-tuning-sop.md](controller-tuning-sop.md)（调优参数手册）

---

## 0. 总结论

**这不是一次 `helm upgrade` 能完成的升级，而是一次"硬切割改名 + 契约重写"迁移。**

- chart 名、CRD API group、环境变量前缀、镜像名、控制面存储前缀全部从 `hiclaw` 改为 `agentteams`
- Team CR 契约被重写（内联 workers -> `workerMembers` 引用）
- 上游**没有提供任何 hiclaw->agentteams 的 K8s/Helm 迁移工具**（`migrate/` 是 OpenClaw 导入工具；`install/` 的 keep-all 升级只覆盖 Docker 场景；`docs/synapse-setup.md` 只覆盖全新安装）

> ⚠️ 前提校验：本报告基于上游 v1.1.2 原版命名（`hiclaw.io` / `HICLAW_*`）。若实际生产分支已做过部分改名（如后续 `c9f65b5` 的 hiclaw 残留清理），M-1 / M-7 / M-8 的严重度相应下降，逐条核对即可。

---

## 1. 调谐字段迁移核查（结论：全部生效，无丢失）

v1.1.2 时期加的调谐优化（当时路径 `hiclaw-controller`、前缀 `HICLAW_`，经 1.2.0 硬切重命名 + 1.2.2 + feature-0830 三次合并）在 HEAD 逐字段核对结果：

### Team/Human 调谐（fc3081a / b0f20e4 重做版）

| Env | 默认值 | 接线点 | 状态 |
|-----|--------|--------|------|
| `AGENTTEAMS_TEAM_MAX_CONCURRENT_RECONCILES` | 1 | config.go -> app.go -> controller Options | 生效 |
| `AGENTTEAMS_TEAM_RECONCILE_TIMEOUT_SECONDS` | 0（禁用） | 每次 reconcile 的 context deadline | 生效 |
| `AGENTTEAMS_TEAM_RECONCILE_INTERVAL_SECONDS` | 300s + 0-10% jitter | activeRequeue() | 生效 |
| `AGENTTEAMS_TEAM_ACTIVE_NO_REQUEUE` | false | Active 团队纯事件驱动 | 生效 |

配套：Team/Human 熔断（连续失败 5 次 + 30s->10min 指数退避 + `agentteams.io/retry` 注解重启）、CRD status 字段（observedGeneration/consecutiveFailures/maxRetriesReached/phaseTransitionTime）均在。

### 存储调优（7898244 引入，`HICLAW_STORAGE_*` 已随改名变 `AGENTTEAMS_STORAGE_*`）

| Env | 默认值 | 消费点 | 状态 |
|-----|--------|--------|------|
| `AGENTTEAMS_STORAGE_CONNECT_TIMEOUT_SECONDS` | 2s | minio_sdk.go dial/TLS 超时 | 生效 |
| `AGENTTEAMS_STORAGE_RETRY_WINDOW_SECONDS` | 30s | retry.go 单操作总重试窗口 | 生效 |
| `AGENTTEAMS_STORAGE_RETRY_BACKOFF_MS` | 500ms | retry.go 初始退避 | 生效 |
| `AGENTTEAMS_STORAGE_RETRY_BACKOFF_MAX_MS` | 5000ms | retry.go 退避封顶 | 生效 |
| `AGENTTEAMS_STORAGE_SDK_MAX_RETRIES` | 2 | minio-go 内部重试 | 生效 |
| `AGENTTEAMS_STORAGE_PROBE_TIMEOUT_SECONDS` | 30s | deployer.go 配置阶段探活 | 生效 |

关联开关：`AGENTTEAMS_STORAGE_DRIVER`（默认 sdk，ed3cdc4 补回）、`AGENTTEAMS_MATRIX_PROVIDER`（67cbf37 去重）均已核实修复，无同类残留。

### 两个注意点

1. **并发默认值语义变化**：早期硬编码 5 并发、周期 30min；HEAD 默认 1 并发（串行）、300s。**迁移后若依赖并发提速，必须显式设 `AGENTTEAMS_TEAM_MAX_CONCURRENT_RECONCILES=5`**，否则体感变慢。
2. **存储调优 env 未暴露到 helm values**（代码默认值已内置生效）：K8s 部署只能靠 `controller.env: {}` 手动注入。建议后续给 values.yaml 加 `controller.storageTuning` 段。

---

## 2. 必须处理（不处理必坏）

### M-1. CRD API group 硬切割：已有 CR 全部失效

- 依据: `agentteams-controller/api/v1beta1/types.go` `GroupName = "agentteams.io"`（v1.1.2 为 `hiclaw.io`）；新 controller 只 watch `*.agentteams.io`。
- 表现: 所有 agent **静默失联，不报错**。
- 操作:
  1. 备份 `kubectl get workers.hiclaw.io,teams.hiclaw.io,humans.hiclaw.io,managers.hiclaw.io -o yaml > cr-backup.yaml`。**status 里的 matrixUserID/roomID 是找回 Matrix 房间的唯一线索**。
  2. 在新 chart 下按新契约重建 CR（见 M-2）。
  3. 删旧 CRD 前先摘旧 CR 的 finalizer，否则 delete 卡住：
     `kubectl patch <cr> --type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]'`

### M-2. Team CR 契约重写

- 依据: `spec.leader` + 内联 `spec.workers` 被删除，改为 `spec.workerMembers`（`TeamWorkerRef{Name, Role}`），且必须**恰好一个** `role: team_leader`（CRD required 校验）。
- 旧 Team YAML 直接 apply 会被静默丢字段。操作：leader 和每个内联 worker 先转成独立 Worker CR（WorkerSpec 基本只增不减，`model` 仍必填），再组 Team。

### M-3. 旧 release 卸载会删 Matrix 房间

- 依据: v1.1.2 uninstall hook Job 删光 CR -> finalizer 清理（LeaveAllWorkerRooms + DeleteWorkerRoom + 删 gateway consumer/MinIO user）。S3 `agents/<name>/` 工作区数据不删，**但 Matrix 房间会被删/被离开**。
- 操作: 旧 release 卸载必须 `--set controller.uninstallHook.enabled=false`，并确认旧 controller 缩容后再删 CRD。绝不能让 hook Job 跑完。

### M-4. Synapse server_name 必须显式固定

- 依据: `helm/agentteams/templates/_helpers.tpl` 中 serverName 为空时默认取 `<release>-synapse.<ns>.svc.cluster.local`。
- server_name 嵌在所有 user ID / room ID / alias 里，一旦变成集群内 FQDN，现网全部作废（postgres 数据虽在但对不上号）。
- 操作: `matrix.serverName: <现网 server_name>`。

### M-5. Synapse 数据搬迁（mode 只支持 managed）

- 依据: `00-validate.yaml` 对 `matrix.mode=existing` 直接 fail；chart 模板硬编码 StatefulSet 名 `<release>-synapse`（PVC `data-<release>-synapse-0`）、postgres `<release>-synapse-pg`、DB user/db 名均为 `synapse`、DB 密码默认 `synapse-change-me`。
- 操作（方案 A，官方路径）:
  1. pg_dump 旧库 -> 导入新 `<release>-synase-pg`；旧库 user/db 名不同则在导入时统一为 `synapse`；`matrix.synapse.postgres.password` 显式设为与旧库一致。
  2. 旧 Synapse `/data`（**signing.key**、media_store）拷入新 PVC。**signing.key 是房间/用户签名信任链的根，丢了整库信任链断裂**。E2EE 私钥在 Element 客户端侧，signing key + DB 保住即不丢。
  3. `matrix.synapse.macaroonSecret` / `formSecret` 不在 values 里、默认 `change-me-*`，沿用旧集群必须把现网值补进 values（macaroon 变更 -> 所有 access token 失效，全员掉线重登）。
- 备选（方案 B，不推荐）: 继续用自建 Synapse，`controller.env` 注入 `AGENTTEAMS_MATRIX_URL/DOMAIN/PROVIDER=synapse`，但需自己改模板放宽 `00-validate` 校验，工作量大。

### M-6. AppService token 连续性 + 凭据全部显式给死

- 依据: `matrix.appservice.asToken/hsToken` 默认 `change-me-*`，synapse 模式下强制非空；token 轮转在 Synapse 上是"仅重启"（RotateToken 返回 501）。runtime-env Secret 名随 chart 改名 -> `lookup` 保不住自动生成的凭据。
- 操作: 现网 appservice registration 的 `as_token`/`hs_token`/`id`/`sender_localpart` **原样**填进 values；adminPassword、registrationToken 等所有凭据同样显式给死。
- 注意: AS 用户 namespace 是**故意非 exclusive**（exclusive 会把 admin 建号挡成 M_EXCLUSIVE）；共享 Synapse 必须收窄 `userNamespaceRegex`（如 `@agentteams-.*:<domain>`）。

### M-7. 环境变量契约整体改名（无兼容回退）

- `HICLAW_*` -> `AGENTTEAMS_*`，controller 代码只读新前缀，grep 无任何回退逻辑（Docker 安装器的 `_controller_env_prefix()` 切换逻辑不覆盖 K8s/helm）。
- 排查范围: fork 自定义 env、values 的 `controller.env` 注入项、自建镜像里读 `HICLAW_FS_*` 的脚本。重点: `AGENTTEAMS_FS_ENDPOINT/FS_BUCKET/FS_ACCESS_KEY/FS_SECRET_KEY`、`AGENTTEAMS_STORAGE_PREFIX`、`AGENTTEAMS_MATRIX_URL/DOMAIN/PROVIDER`。
- 注意: changelog/current.md 部分 v1.2.2 条目仍写 `HICLAW_STORAGE_DRIVER`，是陈旧文案，**以 config.go 为准（`AGENTTEAMS_STORAGE_DRIVER`）**。

### M-8. 镜像名 / 资源前缀 / release 资源名全变

- `hiclaw/` -> `agentteams/` 镜像；`resourcePrefix: hiclaw-` -> `agentteams-`（旧 `hiclaw-*` Pod/SA/Service 新 controller 不认领，手动清理）；Chart 名变 -> fullname 变 -> **release 内所有资源名全变**（runtime-env Secret、Deployment 等）。
- 操作: 不要原地 `helm upgrade`（会产生一套全新资源名 + 旧资源变孤儿）。**新命名空间/新 release 干净安装，旧资源人工退役**。

### M-9. 外接 S3 配置（正确路径已打通，无需 sidecar）

- 依据: `00-validate.yaml` 外接存储只允许 `storage.provider=oss + mode=existing`，`storage.oss.region` 校验必填（任意非空即可）；app.go 中 oss provider 下 `AGENTTEAMS_FS_ENDPOINT` **必须非空**（v1.1.2 可由 STS 响应带回，HEAD 不行）；静态 AK/SK 成对给出即可免除 credential-provider sidecar。
- values 模板:

```yaml
storage:
  provider: oss
  mode: existing
  bucket: <现网同一个 bucket 名>   # 关键：数据面 key 兼容，见 S-1
  oss:
    region: <任意非空值>
    endpoint: http(s)://<你的 S3 endpoint>
    accessKey: <AK>
    secretKey: <SK>
```

- 副作用: 静态 AK 下所有 worker 共享 controller 同一对凭证（WorkerEnv 下发），无 per-worker 最小权限隔离（v1.1.2 managed MinIO 有 per-user policy，外接 S3 本来就没有）。
- worker 侧已配套: `af5ce7b`（worker-entrypoint 静态 AK 优先配 mc alias）；默认 `AGENTTEAMS_STORAGE_DRIVER=sdk`（minio-go），必要时可回退 `mc` 驱动。

---

## 3. 建议处理（有坑但有 workaround）

### S-1. 控制面 S3 前缀改名（数据面兼容）

- 数据面 key **完全兼容**：`agents/<name>/…`、`shared/…`、`teams/<team>/…`，两版一致（SDK 驱动会剥掉 mc alias 段）。**已有 agent 工作区无需搬迁**，`storage.bucket` 填现网 bucket 即可。
- 控制面前缀 `hiclaw-config/{workers,teams,humans}/` -> `agentteams-config/…`，package URI 同步改名，HEAD 无读取旧前缀的兼容代码。旧 `hiclaw-config/` 下如有导出 CR/package，手动 `mc cp --recursive` 迁移，否则 controller 视为首次初始化重灌。
- 默认 bucket 名 `hiclaw` -> `agentteams-storage`（用现网值即可，无影响）。

### S-2. Matrix 房间大概率重开

- roomID 只在 CR status 里，新建 agentteams.io CR 的 status 为空 -> controller 重新建房，旧房间成孤儿。
- worker 账号可幂等复用（Synapse `EnsureUser` 走 `PUT /_synapse/admin/v2/users/<id>` 且已修 `logout_devices:false` 不踢会话）。**保持 workerName 不变是账号复用的前提**（localpart 由 workerName 派生）。
- 要保留旧房间: 新 CR 建好后参照备份 roomID 手工拉回成员（controller 只把 status.roomID 当缓存）。

### S-3. preflight LLM 探针

`pre-install/pre-upgrade` hook，`strict` 默认开、失败即中止。升级窗口集群到 LLM 端点不稳时 `--set preflight.llm.strict=false`。

### S-4. AppService 默认开启

HEAD 的 `AGENTTEAMS_MATRIX_APPSERVICE_ENABLED` 默认 true（helm 显式注入 provider=synapse）。若想继续走 registration_token 密码流，需 `matrix.appservice.enabled=false`。

### S-5. 新 chart 卸载语义更激进

新 uninstall hook 的清理范围描述包含 **OSS 数据**（比 v1.1.2 更激进）。调试期建议 `controller.uninstallHook.enabled=false`，防止误卸载引发清理。

### S-6. RBAC / 认证模型变化

controller 为每个 worker 建独立 SA（`agentteams-worker-<name>`），走 TokenReview + audience 认证；ClusterRole 需要 `tokenreviews`、CRD、PVC/SC 权限。若 fork 改过 rbac.yaml，以新模板为准重放定制。

---

## 4. 可忽略 / 低风险

- Worker/Human spec 字段变化基本是增量（新增 displayName/modelProvider/channels/resources/idleTimeout/credentialBindings/deployMode 等，旧字段未删），重建 CR 时老字段可直接平移。
- Status 新增字段（SpecHash/ObservedGeneration/ConsecutiveFailures 等）由 controller 自动填。
- Team 调优项 `controller.teamTuning.*` 默认关闭，保持默认即旧行为（并发默认值变化见第 1 节注意点）。
- mc alias 名 `hiclaw` -> `agentteams`：只影响容器内 CLI 习惯，S3 key 不受影响。
- changelog 中残留的 `HICLAW_` 文案：陈旧文案，代码为准。
- `migrate/`（OpenClaw 导入）与 `install/`（Docker 单机）对 K8s Helm 迁移均不适用；**上游没有 1.1.2 -> 1.2 的 helm 升级文档，本文件即为自建方案**。

---

## 5. 推荐迁移顺序（Runbook）

```
1. 备份
   - 全部 hiclaw.io CR YAML（含 status）
   - Synapse pg_dump + /data（signing.key、media_store）
   - 确认 S3 bucket 名与 agents/ 数据在位
2. 旧 release 退役
   - --set controller.uninstallHook.enabled=false 后卸载
   - 保住 Synapse 与 S3 不被 finalizer 清理
3. 安装新 chart（建议新命名空间）
   - storage: oss + existing + endpoint + 静态 AK + 现网 bucket
   - matrix.serverName = 现网域名；appservice token = 现网原值
   - 所有凭据（adminPassword/registrationToken/as+hs token）values 显式给死
   - preflight 视网络情况 strict=false
   - 调谐并发显式设置 AGENTTEAMS_TEAM_MAX_CONCURRENT_RECONCILES（默认已回落为 1）
4. Synapse 数据搬入
   - pg_dump 导入新 postgres（对齐硬编码 DB 名 synapse/密码）
   - /data 拷入新 PVC（signing.key 必须带上）
   - macaroonSecret/formSecret 补现网值
5. 重建 CR
   - 先 Worker（workerName 保持不变）
   - 后 Team（workerMembers 新契约 + 唯一 team_leader）
6. 验证与清理
   - S3 agents/<name>/ 被新 controller 读取
   - Matrix 老账号复用（logout_devices:false 不掉线）
   - 旧 hiclaw-config/ 前缀内容迁移
   - 清理旧 hiclaw-* Pod/SA 与旧 CRD（删前摘 finalizer）
```
