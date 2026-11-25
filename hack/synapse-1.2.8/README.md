# Synapse for AgentTeams v1.2.8（matrix.provider=synapse）— 部署与切换手册

> 目标：在不重装 AgentTeams 应用栈的前提下，把 105 上的 **v1.2.8** 从 Tuwunel 切到 **Synapse v1.127.0**，
> 数据库使用 **105 宿主机 PostgreSQL 16**（非 k8s 内 PG）。
>
> 代码改造见分支 `feat/v1.2.8-synapse`：controller 增加 `AGENTTEAMS_MATRIX_PROVIDER=synapse`
> 与 `SynapseClient`（admin REST + declarative AppService）；Helm chart 放开
> `matrix.provider=synapse` + `matrix.mode=existing`。Synapse 本身不在 chart 内，由本目录清单部署。

## 1. 架构与命名

```
K8s namespace agentos
├─ agentteams-128-controller        (provider=synapse, 连接下面 Synapse)
├─ agentteams-128-synapse-0         Synapse v1.127.0  (ghcr.io/element-hq/synapse:v1.127.0)
│    ├─ /config/homeserver.yaml     ← Secret agentteams-synapse-config
│    ├─ /as-registrations/appservice.yaml ← Secret agentteams-synapse-appservice
│    └─ /data (PVC 10Gi)            media store + signing key
├─ agentteams-128-{minio,element-web,dashboard}
└─ higress-{gateway,controller,console}
       ↑ /_matrix/* 路由由 controller initializer 指向 agentteams-128-synapse:8008

宿主机 10.254.254.105:5432 (PostgreSQL 16)
└─ database synapse  owner agi-synapse   (UTF8, LC_COLLATE=C → 无需 allow_unsafe_locale)
```

| 项 | 值 |
|----|----|
| Synapse 镜像 | `ghcr.io/element-hq/synapse:v1.127.0` |
| Synapse Service | `agentteams-128-synapse.agentos.svc.cluster.local:8008`（headless） |
| Matrix server_name | `agentteams-128-synapse.agentos.svc.cluster.local`（用户 ID 的域名部分） |
| public_baseurl | `http://10.254.254.105:32210`（Higress gateway NodePort，Element 使用） |
| PG 主机/库/用户 | `10.254.254.105:5432` / `synapse` / `agi-synapse` |
| PG 角色权限 | CREATEDB、非 superuser；密码与 postgres 主密码相同，部署时用 `PG_PASSWORD` 环境变量传入（**不写进仓库**） |
| Matrix admin | localpart 由 `AGENTTEAMS_ADMIN_USER` 决定（当前 `admin`），密码在 Secret `agentteams-128-runtime-env` 的 `AGENTTEAMS_ADMIN_PASSWORD`；`register_new_matrix_user -a` 创建，必须是 Synapse server admin |
| AppService | id `agentteams-controller`，`as_token`/`hs_token` 取自 `agentteams-128-runtime-env`；Synapse 通过 `app_service_config_files` 声明式加载 |
| controller 镜像 | `agentteams/agentteams-controller:v1.2.8-synapse.<n>`（由 `feat/v1.2.8-synapse` 分支构建） |

## 2. 前置条件

### 2.1 PostgreSQL 角色与库（已在本环境完成）

```sql
-- 以 postgres 超级用户执行
CREATE ROLE "agi-synapse" WITH LOGIN PASSWORD '<PG_PASSWORD>' CREATEDB;
CREATE DATABASE synapse OWNER "agi-synapse"
  ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
GRANT ALL PRIVILEGES ON DATABASE synapse TO "agi-synapse";
```

校验：

```bash
PGPASSWORD='<PG_PASSWORD>' psql -h 10.254.254.105 -U agi-synapse -d synapse \
  -c "SELECT current_user, current_database(), pg_encoding_to_char(encoding), datcollate FROM pg_database WHERE datname=current_database();"
# 期望：agi-synapse | synapse | UTF8 | C
```

pg_hba 需允许 `10.42.0.0/16`（k3s Pod 网段）以 scram-sha-256 连接；本环境已配置
（`pg_hba_file_rules` 第 9 条）。

### 2.2 镜像

```bash
docker pull ghcr.io/element-hq/synapse:v1.127.0        # 105 上已验证可拉
docker pull higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/golang:1.26-alpine  # 仅构建 controller 用
```

### 2.3 构建并导入带 Synapse 支持的 controller 镜像

```bash
# 开发机：打包 controller 源码
tar -czf controller-src.tgz agentteams-controller

# 105：
mkdir -p ~/synapse-build && cd ~/synapse-build && tar -xzf ~/controller-src.tgz
mkdir -p agentteams-controller/build-bin
docker run --rm -v "$PWD/agentteams-controller:/src" -w /src \
  -e CGO_ENABLED=1 -e GOPROXY=https://goproxy.cn,direct \
  higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/golang:1.26-alpine \
  sh -c 'apk add --no-cache gcc musl-dev && CGO_ENABLED=1 go build -ldflags "-extldflags \"-static\"" -o /src/build-bin/agentteams-controller ./cmd/controller/'

# delta 镜像：以现网 v1.2.8 镜像为基座，只覆盖二进制
cat > Dockerfile.controller.synapse <<'EOF'
FROM higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-controller:v1.2.8
COPY build-bin/agentteams-controller /usr/local/bin/agentteams-controller
RUN chmod +x /usr/local/bin/agentteams-controller
EOF
docker build -f Dockerfile.controller.synapse -t agentteams/agentteams-controller:v1.2.8-synapse.1 .
```

镜像需同时导入 105 与 k3s-103 两个节点的 containerd（两边均可 ssh）：

```bash
# 105 本机
docker save agentteams/agentteams-controller:v1.2.8-synapse.1 | sudo k3s ctr -n k8s.io images import -
# 从 105 导到 k3s-103
docker save agentteams/agentteams-controller:v1.2.8-synapse.1 | \
  sshpass -p '<105 password>' ssh -o StrictHostKeyChecking=no extvdiadmin@10.254.254.103 \
  'sudo -S k3s ctr -n k8s.io images import -'
```

## 3. 部署 Synapse（controller 仍停留在 Tuwunel，可安全执行）

```bash
cd hack/synapse-1.2.8

# PG_PASSWORD 必须显式传入；其余默认值见脚本头部
PG_PASSWORD='<PG_PASSWORD>' ./scripts/deploy-synapse.sh
```

脚本分三个阶段（幂等，可重复执行）：

1. **phase 1**：渲染 `homeserver.yaml`（不含 AppService）→ 创建 `agentteams-synapse-config` Secret
   → apply Service/StatefulSet → 等待就绪 → 运行 bootstrap Job 用
   `register_new_matrix_user -a` 创建 **Synapse server admin**（账号密码取自
   `agentteams-128-runtime-env`，重复执行视为成功）。
   *顺序原因*：controller 的 `EnsureUser` 走 Synapse admin API，启动前必须已有 admin 身份。
2. **phase 2**：渲染 `appservice.yaml`（as/hs token 取自 controller 的 Secret）→ Secret
   `agentteams-synapse-appservice` → `homeserver.yaml` 追加
   `app_service_config_files: [/as-registrations/appservice.yaml]` → 滚动重启 Synapse。
   Synapse 的 AppService **只能声明式注册**，没有运行时注册 API，`rotate-token` 接口在
   synapse provider 下返回 501。
3. **phase 3**：`scripts/verify-synapse.sh` 校验 CS API、admin 登录/admin REST、
   AppService 登录（`m.login.application_service` + as_token）。

单独复验：

```bash
./scripts/verify-synapse.sh
```

## 4. 切换 controller/Manager/Worker 到 Synapse

⚠️ **破坏性**：Synapse 是全新 homeserver，原有 Tuwunel 上的 Matrix 用户、房间、DM
（`@admin`、`@cheston`、`@manager`、`@reporter`、`team-report` 等）不会迁移；切换后
controller 会按现有 CR（Manager/Worker/Human/Team/Loop）在新 homeserver 上**重新创建**
用户与房间。旧 Tuwunel PVC 保留，可回滚但**不回迁数据**。

```bash
# 1) 用 overlay 升级 release（chart 停止渲染 tuwunel，controller 切 provider 并换镜像）
helm upgrade agentteams-128 ~/deploy-128/src/helm/agentteams -n agentos \
  -f ~/deploy-128/values-128-agentos.yaml \
  -f hack/synapse-1.2.8/values-synapse-agentos.yaml \
  --set controller.image.repository=agentteams/agentteams-controller \
  --set controller.image.tag=v1.2.8-synapse.1 \
  --set controller.image.pullPolicy=IfNotPresent \
  --wait --timeout 10m
```

> 若 helm upgrade 后 controller Pod 被调度到 k3s-103 且镜像未导入，会 ImagePullBackOff；
> 先按 §2.3 导入两节点，或用 `kubectl -n agentos patch deploy agentteams-128-controller
> --type=json -p '[{"op":"add","path":"/spec/template/spec/nodeSelector","value":{"kubernetes.io/hostname":"extvdiadmin-vmware-virtual-platform"}}]'`
> 临时固定在 105 节点。

切换后检查：

```bash
kubectl -n agentos get pods
kubectl -n agentos logs deploy/agentteams-128-controller --tail=100 | grep -Ei 'matrix|initializer|error'

# controller 启动序列应依次通过：
#   "Matrix is ready" → "admin account ready" → "admin Human registration failed" 不应出现
#   → "Matrix AppService registered and verified" → gateway routes → "cluster initialization complete"

# Element（http://10.254.254.105:32410）用 admin 账号登录，应看到 @admin:<server_name>
# Higress gateway 的 /_matrix 路由已由 initializer 指向 Synapse：
curl -s http://10.254.254.105:32210/_matrix/client/versions | head -c 300
```

Manager / Worker 验证：

```bash
kubectl -n agentos get pods | grep -E 'manager|worker'
kubectl -n agentos logs deploy/agentteams-128-controller --tail=200 | grep -Ei 'ensure|provision|room'
# 在 Element 里由 admin 对 manager 发消息，确认 Manager 能回复
```

## 5. 运维

| 操作 | 方法 |
|------|------|
| 查看日志 | `kubectl -n agentos logs sts/agentteams-128-synapse -f` |
| 重启 | `kubectl -n agentos rollout restart sts/agentteams-128-synapse` |
| 改配置 | 修改 `templates/homeserver.yaml.tmpl` / `templates/appservice.yaml.tmpl` 后重跑 `deploy-synapse.sh`（幂等） |
| 更新 AppService token | 更新 `agentteams-128-runtime-env` Secret → 重跑 `deploy-synapse.sh`（重渲染 appservice 并重启 Synapse）→ 重启 controller |
| 重建 admin 密码 | `kubectl -n agentos exec -it sts/agentteams-128-synapse -- register_new_matrix_user -c /config/homeserver.yaml -u admin -p '<new>' -a --exists-ok http://127.0.0.1:8008`（`--exists-ok` 视镜像版本，报 already taken 即账号已存在） |
| DB 备份 | 宿主机 `pg_dump -h 127.0.0.1 -U postgres synapse`（或对 `synapse` 库做常规备份） |

## 6. 已知限制 / 注意

- **AppService 声明式**：`/_synapse/admin` 无 AS 注册 API；改 token 必须改文件 + 重启 Synapse；
  `/api/v1/appservice/rotate-token` 在 synapse 下返回 501（有明确提示）。
- **用户不可硬删除**：Synapse 只能 deactivate（`erase=false` 保留数据）；controller 的
  delete→recreate 用 `deactivated=false` 的 PUT 复活的路径已实现（EnsureUser）。
- **无联邦**：`trusted_key_servers: []`，仅服务集群内用户；server_name 是集群内 DNS 名，
  Element 通过 gateway `/_matrix` 访问，不做 `.well-known` 外部解析。
- **房间删除/踢人**：Synapse 1.127 没有 admin kick 端点；controller 通过
  `make_room_admin` + CS API 提权后操作，`DeleteRoom` 走
  `DELETE /_synapse/admin/v2/rooms/{id}`（异步 purge）。
- **单副本**：SQLite→PostgreSQL 已切好，但 Synapse 仍单进程部署；水平扩展需拆
  workers + Redis，不在本次范围。

## 7. 回滚（仅限切换到 Synapse 后仍需旧 Matrix 数据的场景）

```bash
# 1) 回滚 helm release（恢复 tuwunel provider + 原 controller 镜像）
helm rollback agentteams-128 -n agentos
# 2) 观察 tuwunel pod 与 PVC 恢复；tuwunel 数据未删除（PVC agentteams-128-tuwunel 仍在）
kubectl -n agentos get pvc | grep tuwunel
# 3) 可保留 Synapse 资源用于事后分析；确认不再需要后：
#    kubectl -n agentos delete sts agentteams-128-synapse
#    kubectl -n agentos delete pvc data-agentteams-128-synapse-0
#    kubectl -n agentos delete secret agentteams-synapse-config agentteams-synapse-appservice agentteams-synapse-secrets
```

注意：回滚**不会**把 Synapse 上新建的数据带回 Tuwunel；如需数据请从 Synapse DB 导出。

## 8. 本次实际切换记录（2026-11-24，105 / agentos）

**结果**：`agentteams-128` 于 20:38 完成 Tuwunel → Synapse 切换，全链路验证通过
（admin 经 gateway 在 Manager DM 发消息，@manager 回复 "OK"；Element/Dashboard 均 200）。

| 项 | 实际值 |
|----|--------|
| Helm release | `agentteams-128` rev **9**（rev 5 因 copaw-gate 镜像拉取失败，rev 6 起生效） |
| Controller 镜像 | `agentteams/agentteams-controller:v1.2.8-synapse.4`（.1→.4 迭代：Synapse 客户端 → 等权踢人/self-leave → 路由修复） |
| Synapse | `ghcr.io/element-hq/synapse:v1.127.0`，namespace `agentos`，pod `agentteams-128-synapse-0` |
| server_name | `agentteams-128-synapse.agentos.svc.cluster.local` |
| Controller env | `AGENTTEAMS_MATRIX_PROVIDER=synapse`，URL/domain 指向上面 Synapse |
| 数据库 | 105 宿主机 PG16：`synapse` 库，`agi-synapse` 用户（密码见本地运行手册，不入库） |
| 自建镜像位置 | 105 `docker` + 105/k3s-103 两节点 `k3s ctr -n k8s.io images import` |

**切换步骤（可复现）**：

1. `deploy-synapse.sh` 幂等部署 Synapse（phase1 无 AppService → bootstrap admin → phase2 挂 AppService → verify）。
2. 打包/构建 controller 镜像并导入**两个节点**的 containerd（见 §2.3）。
3. `helm upgrade`（`--set controller.image.tag=...` + overlay，见 §4）。
4. `kubectl apply -f ~/deploy-128/src/helm/agentteams/crds/` —— **helm upgrade 不会升级 CRD**；
   本分支补了 humans CRD 的 `Degraded` phase enum（v1.2.8 上游遗漏）。
5. 切换后 v1.2.8 的 reconcile 以 `status.matrixUserID` 非空为“已就绪”，不会自动迁移旧
   homeserver 身份。本次通过重置 CR status（manager/worker/team/human 的
   `matrixUserID`/`roomID`）+ 删除旧 `agentteams-creds-*` Secret + 重建 Pod 强制重新
   provision（**不删除 CR**，MinIO 工作区数据保留）。
6. 等 controller 把 Manager/Worker/Team 重新 provision 到 Synapse（本环境约 2~3 分钟），
   再用 Manager DM 做端到端验证。

**本次踩到的坑（版本相关问题，已修复/规避）**：

- `element-hq/synapse` 镜像 entrypoint 是 `/start.py`：必须以 `args: [run]` +
  `SYNAPSE_CONFIG_PATH` 启动；且镜像无常驻非 root 用户、local-path PVC 属 root，
  故设 `UID=0/GID=0` 让 start.py 不做 gosu 降权。
- bootstrap Job 首次失败："This user ID is reserved by an application service"——AppService
  registration 已挂载时 admin 用户 ID 被独占命名空间占用。现在 Job 先验证 admin 登录 +
  admin REST（已存在则直接成功），并兼容该错误；重跑幂等。
- Synapse 默认限流（`rc_message` 等）低于 controller 批量建房间/写状态的速率 → 大量
  `M_LIMIT_EXCEEDED`；homeserver.yaml 已把相关 rc_* 全部放宽到 1000/s。
- 等权 PL 踢人：Manager 在 Worker 个人房间是 PL 100，controller admin 也是 PL 100，
  Matrix 拒绝踢等权用户，且 admin 无法降等权用户的 PL。controller 侧修复：
  ① `SynapseClient` 先 `make_room_admin`/`force-join` 恢复；② 仍失败则由业务层用目标
  用户自己的 token 做 **self-leave**（`Provisioner.ForceLeaveRoom` 兜底）。
- homeserver 切换后网关 `matrix-homeserver` 路由仍指向旧端口 6167（Tuwunel 时代路由是
  create-only）→ 503。initializer 已改为 `EnsureManagedRoute` 原地修复 upstream port；
  升级后 `curl http://10.254.254.105:32210/_matrix/client/versions` 返回 200。
- k3s-103 磁盘 89% 触发 kubelet 镜像 GC，自建 controller 镜像被清掉导致
  `ImagePullBackOff`。已清理 docker 无用数据 + journal（→75%），并在导入后立即升级；
  后续导入自建镜像务必两个节点都 `crictl images | grep <tag>` 确认。
- `agentteams-128-copaw-gate` pre-upgrade hook 使用新 controller 镜像，镜像未就绪时会
  等 5 分钟超时；导入镜像后正常。若再遇到：`helm upgrade --no-hooks` 可跳过该 legacy gate。

**账号/密码**：仓库只记录用户名、地址与 Secret 名称；明文口令记录在**本地**（不入库）：
桌面 `agentos-teams信息汇总.txt` 与 `hack/synapse-1.2.8/render/ACCOUNTS.local.md`（gitignored）。

## 9. 高可用一期：Redis + client workers（已于 2026-11-25 启用）

> 完整分析/演进路线见 [HA-DESIGN.md](HA-DESIGN.md)。本节只给操作入口。

**拓扑**：`main`（StatefulSet 1 副本，PVC media+签名密钥）永远是单活（Synapse 不支持多 main
active-active）；新增 Redis（replication 事件流 + 共享缓存）与 **2 个 client worker**
（Deployment，无状态、无 PVC、反亲和）。Higress 只把
`/_matrix/client/v3/sync`、`/_matrix/client/r0/sync`、`/_matrix/client/v3/events` 路由到
worker Service，其余 `/_matrix` 仍走 main。

```
# 一键启用（幂等；会重启 main 一次，约 10~30s；之后 worker 承接 /sync）
cd hack/synapse-1.2.8
./scripts/enable-ha.sh

# 校验（Redis/main replication listener/workers/网关路由）
./scripts/verify-ha.sh
```

组件与文件：

| 对象 | 文件 |
|------|------|
| Redis（StatefulSet+Service+AOF PVC） | `manifests/synapse-redis.yaml`（Secret 由 enable-ha.sh 渲染，不入库） |
| main 稳定 ClusterIP（8008 + 9093 replication） | `manifests/synapse-main-service.yaml` |
| client workers（Deployment+Service+PDB+worker 配置） | `manifests/synapse-worker.yaml` |
| main 配置增量 | `homeserver.yaml.tmpl` 的 `__REPLICATION_LISTENER__` / `__HA_TOP_LEVEL__` 占位（deploy 脚本检测到 HA 密钥后自动渲染） |
| 网关路由 | `scripts/enable-sync-workers.sh`（Higress Console API，幂等） |
| 启用/校验 | `scripts/enable-ha.sh`、`scripts/verify-ha.sh` |

要点：

- **签名密钥**：main 仍在 `/data/agentteams.signing.key`；enable-ha.sh 把它复制到 Secret
  `agentteams-synapse-secrets` 的 `signing_key`，worker 以只读方式挂到 `/signing-key/`（同一把
  key，**不要重新生成**）。
- **Redis**：不是可选缓存，是 Synapse 多进程的 replication 总线（v1.67 起 direct TCP 已被移除）；
  只存通知/缓存，丢数据不影响消息（消息在 PostgreSQL），但 Redis 重启后建议滚动重启 worker。
- **网关路由顺序**：实测 Higress 按最长前缀匹配，sync 路由会优先于 `/_matrix` 兜底，无需删除兜底路由。
- **回滚**：删除 `matrix-homeserver-sync*`/`-events` 路由与 `synapse-client` service source，
  删除 worker/redis 对象，去掉 main 配置中的 replication/redis/instance_map 块后重启 main。
- **内部 agent 的 /sync**：Manager/Worker 的 Harness 仍直连 main（`AGENTTEAMS_MATRIX_URL` 同时被
  controller 的 admin REST 使用），所以一期只覆盖外部客户端；拆分该 URL 需要 controller 小改动
  （见 HA-DESIGN §10 第 5 项）。

