# Synapse 高可用改造分析（/sync 由 client worker 承接）

> 状态：**分析稿（未实施）**。基于 2026-11-25 现网 `agentos` 实测与 Synapse 1.127 官方 worker 文档。
> 结论：一期用 **main（单活）+ Redis + N 个 `synapse.app.generic_worker`（client 资源）** 承接
> `/_matrix/client/v3/sync` 等端点，即可把 `/sync` 从"单进程单点"变成"多副本 + 滚动升级不断连"；
> 二期再把事件写入、media、Redis、PostgreSQL 纳入完整 HA。

## 1. 结论先行

| 问题 | 结论 |
|------|------|
| 当前 `/sync` 谁在接 | 单一 Synapse 主进程（`python -m synapse.app.homeserver`，listener 8008，单 Pod） |
| 性能是否瓶颈 | 否。实测 `timeout=5000` 长轮询单进程可同时处理 5 并发，Pod CPU 13m / 内存 200Mi；**问题是可用性，不是吞吐** |
| Synapse 能多副本跑 main 吗 | **不能 active-active**（后台任务/push/AS 事务会重复；官方只有 main+worker 模型） |
| 推荐的 `/sync` HA | Redis + 2~3 个 `generic_worker`（resources: `[client]`）+ 网关按路径把 sync 路由到 worker |
| 一期能到什么程度 | `/sync`/`initialSync`/`events` 多副本、worker 挂掉无感、滚动升级不断连；**main 重启窗口（约 10~30s）内发消息/media 会失败** |
| 要 main 重启也不影响发消息 | 需二期：`stream_writers.events` 下放到 event persister worker（支持多 writer，按 room 分片）+ 其他 stream 下放 + media 迁 S3 |
| 全链路 HA 必须补的 | Redis（HA）、PostgreSQL（当前 105 宿主机单实例，是最大单点）、media 存储（当前 RWO PVC 无法多 Pod 共享） |

## 2. 现网实测结论（2026-11-25）

| 项 | 实测值 |
|----|--------|
| Synapse Pod | 1 个 `agentteams-128-synapse-0`，镜像 `ghcr.io/element-hq/synapse:v1.127.0`，0 重启 |
| 进程 | 单进程 `python -m synapse.app.homeserver --config-path /config/homeserver.yaml` |
| listener | 仅 `8008 / http / resources: [client]`（没有任何 worker / replication listener） |
| 配置 | 无 `redis`、无 `instance_map`、无 `stream_writers`；`media_store_path: /data/media`（RWO PVC） |
| `/sync` 无 token | `HTTP 401`，4ms |
| `/sync` 长轮询 | `timeout=5000` → 实测 5.007s 返回；5 并发同时 5.007s 全部成功（网关 `routeTimeout: 0` 不截断） |
| 资源 | Synapse Pod CPU 13m / 内存 200Mi；master CPU 16%/内存 26%，k3s-103 CPU 8%/内存 19%，有余量 |
| DB | 105 宿主机 PostgreSQL 16 单实例；synapse 库当前 12 个 idle 连接 |
| Redis | 本 release 无 Redis（`keda`/`openapi-poc` 命名空间的 Redis 属于其他业务，不复用） |
| 镜像能力 | 自带 `psycopg2`、`txredisapi`、`hiredis`（Synapse 1.127 的 Redis replication 走 `txredisapi`），**一期无需自建镜像** |

**单点清单**

1. Synapse 单 Pod：删除/重启/节点故障 → 所有长轮询 `/sync`、登录、收消息立即中断；客户端会同时重连造成瞬时风暴。
2. Synapse 单进程：`/sync` 长连接数增长只能吃单进程内存/CPU；后台任务（push、清理、用户目录）与 API 抢同一进程。
3. media 本地 RWO PVC：无法多 Pod 共享；Pod 被调度到另一节点时读不到旧 media（local-path 绑定节点）。
4. 无 Redis：worker 模式无法启用（replication 通道缺失）。
5. PostgreSQL 单实例、单机：整个 Matrix（以及所有依赖 controller 的组件）的数据库单点。
6. 网关：`routeTimeout=0` 对 `/sync` 长轮询是必要的（但意味着慢请求会长期占用连接，需要多副本分摊）。

## 3. Synapse worker 模型要点（1.127 文档）

- 所有进程**共享同一个 PostgreSQL**；进程间通过 **Redis pub/sub** 传递 replication stream（也作共享缓存）。
- **main 进程**需要：
  - 一个 HTTP `replication` listener（示例端口 9093，**绝不可暴露公网**）；
  - `redis.enabled: true` + `instance_map.main`（worker 通过它找到 main 的 replication 端口）；
  - 可选 `worker_replication_secret` 给 replication 流量加共享密钥。
- **worker 进程**（`synapse.app.generic_worker`）在自己的配置文件里指定：
  - `worker_app`、唯一的 `worker_name`、`worker_listeners`（http，resources `[client]`，`x_forwarded: true`）；
  - 启动方式：`SYNAPSE_WORKER=synapse.app.generic_worker` + `run --config-path homeserver.yaml --config-path worker.yaml`
    （官方镜像 entrypoint `/start.py` 会据此插入 `-m synapse.app.generic_worker`）；
  - 1.73+ 不再需要 `worker_main_http_uri`。
- `generic_worker` 官方支持承接的端点（节选，含我们关心的）：
  `^/_matrix/client/(r0|v3)/sync$`、`events`、`initialSync`、`/_matrix/client/versions`、
  `login`、`register`、`createRoom`、`rooms/*/send|state|join|invite|kick|redact`、
  `keys/*`、`whoami`、`devices`、`joined_rooms`、`directory/room/*`、`search`、`notifications` 等；
  **`rooms/*/messages` 分页要求同一 room 的请求落在同一实例**（实现时需要注意）。
- `stream_writers`：可把某些 stream 的写责任固定到指定实例（如 `events: event_persister1`）；
  `events` stream **支持多个 writer**（按 room_id 分片，增删 writer 需重启所有 worker 实例）。
- media：`synapse.app.media_repository` worker 处理 `/_matrix/media/*`；多 media worker 必须在同一台机器/共享存储上，
  或用 S3 storage provider（官方镜像不带，需自建）。
- 官方推荐的 `/sync` 优化：同一用户的 sync 尽量哈希到同一 worker（按 access token 中用户名做一致性哈希），
  且 initial sync（无 `since`）与后续 sync（有 `since`）分开路由，避免 initial sync 的资源消耗影响持续 sync。

### 3.1 为什么 worker 模式必须有 Redis？能不能不用？

**结论：Redis 是 Synapse 多进程的 replication（事件流）总线，不是可选缓存；在 1.127 上拆 worker 就必须有 Redis
（或 Redis 协议兼容服务），没有“无 Redis 的 worker 模式”。**

1. **官方要求**：workers 文档明确写 "A Redis server is required to manage the communication between the processes."
   并给出配置步骤：main 开 HTTP replication listener + 开 `redis.enabled` + `instance_map`。
2. **旧的点对点 TCP replication 已被移除**：在 1.127 镜像代码里，如果 worker 配置了历史参数
   `worker_replication_port`，会直接 `ConfigError`：
   ```
   DIRECT_TCP_ERROR = """Using direct TCP replication for workers is no longer supported.
   Please see .../upgrade.html#direct-tcp-replication-is-no-longer-supported-migrate-to-redis"""
   ```
   也就是说“worker 直连 main 的 TCP 复制”这条老路在代码层面已被禁止。
   **版本分界（重要）**：Redis 支持 v1.13.0 引入、v1.18.0 成为推荐方式；
   **v1.67.0 正式移除 direct TCP replication**（upgrade notes：
   "Direct TCP replication is no longer supported: migrate to Redis"）。
   所以：**Synapse < 1.67 可以拆 client worker 而不用 Redis**（worker 用 `worker_replication_host` /
   `worker_replication_port` 连 main 的 TCP `replication` listener，例如 `synapse.app.client_reader`）；
   **>= 1.67（含我们的 1.127）必须 Redis**。如果内网有“拆了 client 但没用 Redis”的部署，
   优先确认其 Synapse 版本是否 < 1.67（或是否有隐藏的 Redis / 其实跑的是多 main 副本而非 worker）。
3. **Redis 承载什么**：所有进程都需要看到同一份“新写入了什么”的事件流（replication streams）——
   `events`、`receipts`、`typing`、`to_device`、`presence`、`device_lists`、`account_data`、`push_rules` 等，
   外加跨进程共享缓存（房间成员/state 等，降低 PG 查询压力）。
   `/sync` 是长轮询：worker 必须能**立刻被“有新事件”唤醒**才能及时返回；没有这条总线，worker 只能轮询 PG，
   延迟和 DB 压力都会不可接受，且各 worker 缓存无法保持一致。
4. **Redis 不存消息数据**：消息/房间/用户都在 PostgreSQL；Redis 只做通知与缓存。
   因此 Redis 挂掉**不会丢数据**，但 replica 进程会失去 replication 流，通常需要重启 worker 重新同步；
   一期用单副本 + AOF（1Gi PVC）即可，二期再上 Sentinel/托管 Redis。
5. **成本很低**：1 个 Redis（100m CPU / 128Mi 内存 / AOF 1Gi PVC）+ ClusterIP + 密码即可；
   也可以换成 Valkey/KeyDB/云 Redis（只要兼容 Redis pub/sub），但不能不要。
6. **不想引入 Redis 的替代选择**：
   - **不拆 worker**：单 main + 快速重启（10~30s 全断）——这是“高可恢复”，不是高可用，达不到本次目标；
   - **复用集群里已有的 Redis**（如 `keda`/`openapi-poc` 命名空间的实例）：技术上可行，但跨命名空间共享
     别人的 Redis 有容量/隔离/生命周期风险，不建议；如要走这条路需要确认版本、容量、能否改密码/DB。
   - **双 main**：官方不支持 active-active，会重复后台任务/push/AS 事务，排除。
7. **镜像依赖**：实测 `element-hq/synapse:v1.127.0` 已自带 `txredisapi`、`hiredis`（matrix-synapse 的 `[redis]` extra
   正好只依赖这两个包），**一期不需要为 Redis 自建镜像**。

## 4. 方案对比

| 方案 | 改动量 | `/sync` HA | 发消息 HA | media HA | main 故障影响 | DB 单点 | 回滚 |
|------|--------|-----------|-----------|----------|----------------|---------|------|
| **A：main + Redis + 2~3 client worker，网关只把 sync 路由到 worker**（推荐一期） | 中（新增 Redis/worker/路由，main 配置加 replication+redis） | ✅ 多副本、滚动升级不断连 | ❌ main 重启期间写失败（10~30s） | ❌ 仍单 main | 窗口内写/media 失败，读 sync 继续 | 仍存在 | 删新增对象 + 恢复 main 配置，简单 |
| **B：A + event persister（events stream 多 writer）+ 其余 stream 下放到 worker + media 迁 S3 + Redis/PG HA** | 大（需自建带 s3 provider 的镜像；stream 增删要重启全部 worker） | ✅ | ✅（main 不在写路径） | ✅ | 基本无感（只剩 admin/media 后台） | 需 PG HA | 复杂，需分阶段 |
| **C：双 main active-active** | 小 | ❌ 官方不支持 | ❌ | — | 数据/任务重复，不可行 | — | — |

> 说明：方案 A 里 main 仍然承担：事件持久化（events stream writer=main）、push、AS 通知、后台任务、media、
> admin REST。因此 main 重启窗口内"读 sync 可能还能继续（worker 读 DB），但发消息会失败"。
> 这是官方架构的固有边界，要消除只能做方案 B。

## 5. 一期实施方案（方案 A，可直接照此落地）

### 5.1 组件拓扑

```
Element / 客户端
      │  http://10.254.254.105:32210
      ▼
 Higress gateway
   ├─ /_matrix/client/v3/sync ──► Service: agentteams-128-synapse-client  ──► client-worker-0/1（2~3 副本，无状态）
   └─ /_matrix/*（其余）        ──► Service: agentteams-128-synapse（main）──► main（1 副本，PVC /data，后台任务/media/admin/AS）
                                        │
                                        ├─ PostgreSQL 105:5432/synapse（共享）
                                        └─ Redis（新增，StatefulSet 1 副本 + AOF PVC）
                                             ▲ replication streams
                                             └── client workers
```

### 5.2 新增/修改清单

| 对象 | 内容 |
|------|------|
| `redis` StatefulSet + Service | 镜像 `redis:7-alpine`；`--appendonly yes`；PVC 1Gi；资源 requests 100m/128Mi、limits 500m/512Mi；密码放 Secret（`requirepass`）；Service name `agentteams-128-synapse-redis:6379` |
| `client-worker` StatefulSet（2 副本起） | 同 Synapse 镜像；**无 PVC**；`SYNAPSE_WORKER=synapse.app.generic_worker`；args `run --config-path /config/homeserver.yaml --config-path /config/worker.yaml`；worker 配置由 ConfigMap + initContainer 渲染（`POD_NAME` → `worker_name`，保证唯一）；探针 `/_matrix/client/versions`；requests 200m/512Mi、limits 1 CPU/1Gi；反亲和（尽量分散 master/k3s-103）；PDB `maxUnavailable: 1` |
| `client-worker` Service | ClusterIP（非 headless 也可，普通 ClusterIP 更适合负载均衡），name `agentteams-128-synapse-client:8008` |
| worker 日志配置 | `worker-log.yaml`（同现有 log_config 风格，stdout） |
| main `homeserver.yaml` 增量 | 见 5.3；新增 replication listener、redis、instance_map、worker_replication_secret；**signing key 迁移到共享 Secret**（多 Pod 必须同一签名密钥，见 5.5） |
| Higress 路由 | 新增 service source `synapse-client.dns` → worker Service:8008；新增路由 `matrix-homeserver-sync`（路径 `/_matrix/client/v3/sync`、`/initialSync`、`/events`）→ `synapse-client.dns`；现有 `matrix-homeserver`（`/_matrix` → main）保留为兜底。**路由顺序必须保证 sync 更具体路由在前**（见 5.4） |
| controller | **无需改代码**：controller 仍连 main 的 internalURL（admin REST、EnsureUser、createRoom 等都在 main 执行）；AppService registration 不变 |
| 文档/脚本 | 全部落到 `hack/synapse-1.2.8/`：新增 manifests/scripts，deploy 脚本分阶段幂等 |

### 5.3 main 配置增量（homeserver.yaml）

```yaml
listeners:
  # ... 现有 client 8008 不变 ...
  # 只给 worker 用的 HTTP replication 端口，不暴露到集群外
  - port: 9093
    bind_addresses: ["0.0.0.0"]
    type: http
    resources:
      - names: [replication]

redis:
  enabled: true
  host: agentteams-128-synapse-redis
  port: 6379
  password: "<从 Secret 读取>"

instance_map:
  main:
    host: agentteams-128-synapse.agentos.svc.cluster.local
    port: 9093

worker_replication_secret: "<随机 32 字节 hex，放 Secret>"
```

### 5.4 worker 配置（每副本，worker.yaml）

```yaml
worker_app: synapse.app.generic_worker
worker_name: client-worker-0        # initContainer 用 POD_NAME 生成，保证唯一

worker_listeners:
  - type: http
    port: 8008
    bind_addresses: ["0.0.0.0"]
    x_forwarded: true
    resources:
      - names: [client]

worker_log_config: /config/worker-log.yaml
```

worker 同时挂载共享 `homeserver.yaml`（DB/redis/instance_map/secret/signing key）与自己的 `worker.yaml`。

### 5.5 签名密钥迁移（多 Pod 前提）

当前 main 的签名密钥在 PVC `/data/agentteams.signing.key`（首次启动自动生成）。worker Pod 不挂该 PVC，
但 worker 进程需要同一把签名密钥（`signing_key_path`）。做法：

1. 从 main PVC 取出 `agentteams.signing.key`，写入 Secret `agentteams-synapse-secrets` 的新 key `signing_key`；
2. 把 `homeserver.yaml` 的 `signing_key_path` 改为 `/config/keys/agentteams.signing.key`；
3. main 与 worker 都挂载该 Secret 的这个 key（readOnly）。**不要重新生成密钥**，否则现有设备/签名校验会全部失效。

（同理 `macaroon_secret_key` / `form_secret` 已在 Secret 中，天然共享。）

### 5.6 网关路由实现与顺序（关键风险）

- 现状：controller 的 initializer 只创建一条 `matrix-homeserver: /_matrix → tuwunel.dns`（名字是历史遗留，
  实际指向 main）。Higress Console 中同域名多路由由 Envoy 按**路由列表顺序**匹配，因此必须确保
  `/sync` 路由排在 `/_matrix` 兜底之前。
- 实施方式（脚本，`hack/synapse-1.2.8/scripts/enable-sync-workers.sh`）：
  1. 登录 Higress Console（admin 凭据同 controller）；
  2. 创建 dns service source `synapse-client` → `agentteams-128-synapse-client.agentos.svc.cluster.local:8008`；
  3. 创建 `matrix-homeserver-sync` 路由（PATH `/_matrix/client/v3/sync` 等）→ `synapse-client.dns`；
  4. 兜底路由：删除后由 controller 重启时 `EnsureManagedRoute` 重建（会排在新建路由之后），或脚本最后
     delete+create 兜底路由，保证顺序；
  5. 用 `curl` + main/worker 日志验证：`/sync` 只出现在 worker 访问日志，`/_matrix/client/versions` 在 main。
- 长轮询：全局 `routeTimeout: 0`（已确认）满足 30s/自定义 timeout；worker 路由不要设置更短的 timeout/idleTimeout。
- 可选优化（二期）：按 access token 用户名做一致性哈希（Higress 支持按 header hash 的负载均衡策略，需验证），
  让同一用户的 sync 固定到同一 worker；initial sync 单独一组 worker。

### 5.7 验证清单

- [ ] Redis `PING` 通；main 启动日志无 replication/redis 报错；
- [ ] worker 日志出现 "Starting synapse with args ... generic_worker"，无 replication 异常；
- [ ] `curl .../sync`（带 token）经网关 → worker 日志有记录、main 日志无该请求；响应正常；
- [ ] Element 登录/收发消息/媒体下载/Manager DM 端到端正常（回归现有链路）；
- [ ] 并发压测：50/200 个 `timeout=30s` 长轮询，worker CPU/内存/PG 连接数稳定（每个 worker `cp_max=10`，
      当前 PG max_connections 默认 100，需校核）；
- [ ] 故障演练：`kubectl delete pod client-worker-0` → 正在进行的 sync 客户端重连到另一个 worker，Element 无感；
      `kubectl delete pod agentteams-128-synapse-0` → 记录 main 恢复时间（预期 10~30s），期间写消息失败但 `/sync` 读仍可用；
- [ ] `kubectl get pods` 无 CrashLoop；PDB/反亲和生效（两个 worker 尽量不同节点）。

### 5.8 回滚

1. 删除 `matrix-homeserver-sync` 路由与 `synapse-client` service source（sync 回到 main）；
2. 删除 client-worker StatefulSet/Service/PDB（顺手清理 ConfigMap）；
3. 恢复 main 配置（去掉 replication listener/redis/instance_map/worker_replication_secret；签名密钥路径可保留共享 Secret 方案）；
4. 滚动重启 main；验证原链路。
   —— 所有对象均为新增，脚本幂等；旧 PVC/数据不受影响。

## 6. 二期（完整 HA）要点

1. **事件写入下放**：`stream_writers.events: [event_persister1, event_persister2]`（按 room 分片，支持多 writer）；
   client worker 作为 event_creator 通过 HTTP replication 把事件交给 persister；**增删 writer 需重启所有 worker**。
2. **其余 stream 下放**：typing / receipts / to_device / account_data / presence / push_rules / device_lists
   单写者，集中到一个 stream-writer worker；同时把 `run_background_tasks_on`、`update_user_directory_from_worker`、
   `notify_appservices_from_worker`、`pusher_instances` 指到 worker → main 只剩 media/admin 等，重启影响面最小。
3. **media 迁 S3**：自建镜像 `FROM element-hq/synapse:v1.127.0` + `pip install synapse-s3-storage-provider`，
   配置 `media_storage_providers`（S3 provider）+ 新增 MinIO 桶（建议独立桶 `agentteams-synapse-media` 和专用用户）；
   之后 media worker 可多副本、main 不再需要 RWO PVC，Pod 可任意调度。**这是 media 多副本的唯一干净做法**
   （RWO local-path 无法多 Pod 挂载；RWX 需 NFS，不推荐）。
4. **Redis HA**：3 节点 Redis Sentinel（或托管 Redis）；Synapse redis 配置需验证 sentinel 支持（1.127 配置项需查文档）；
   或至少主从 + K8s 快速重启，并把"Redis 重启后滚动重启 worker"写进运维手册（丢 replication stream 后 worker 需重同步）。
5. **PostgreSQL HA**：当前 105 宿主机单实例是最大单点。方案：Patroni/流复制主备 + VIP，或迁移云 RDS 多可用区；
   切换涉及 DB 迁移与停机窗口，需单独计划（不属于 Synapse 进程内改造）。
6. **main 单活**：K8s StatefulSet 1 副本 + 快探针 + 快速重启；Synapse 无内建 leader election，
   active-passive 切换需外部仲裁（复杂度高，二期评估；通常接受 main 快速重建 + 后台任务可重入）。
7. 观测：Prometheus 指标（synapse_http_server_response_time、replication 延迟、worker 数量）、
   main/worker 重启告警、Redis/PG 连接数告警；日志聚合区分 main/worker。

## 7. 待你确认的决策点

1. **一期范围**：是否就按"只把 `/sync`（含 initialSync、events）分流到 client worker，其余保持 main"实施？（推荐）
2. **资源**：新增 Redis（1 Pod + 1Gi PVC）与 2~3 个 client worker（约 0.5~1Gi 内存）是否可接受？
3. **main 故障窗口**：一期 main 重启时 10~30s 内**发消息/media 会失败**（`/sync` 读不受影响）——是否可接受？
   若不可接受，直接上二期（工作量成倍增加）。
4. **media HA**：是否需要（决定是否做二期 S3 media + 自建镜像）？
5. **PostgreSQL HA**：是否纳入本次范围？（宿主 PG 单机；涉及 DB 主备/迁移，风险和工作量都大）
6. **路由实现方式**：先用脚本改 Higress（不改 controller、可快速回滚），验证后再决定是否把 worker 路由固化进
   controller initializer / chart？推荐先脚本。
7. **实施环境**：是否直接改现网 `agentos`（会重启一次 Synapse main，约 10~30s 中断）？
   还是先在同集群新建 namespace 做验证环境，验证通过再切现网？

## 8. 参考

- Synapse workers（scaling）：https://element-hq.github.io/synapse/latest/workers.html
- Synapse 配置手册（redis / instance_map / stream_writers / listeners）：
  https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html
- reverse proxy（含 sync 一致性哈希示例）：https://element-hq.github.io/synapse/latest/reverse_proxy.html
- 本环境实测数据见第 2 节（2026-11-25）。
