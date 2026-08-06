# Synapse 部署指南（含 ServiceAccount 场景）

本文档是 **操作手册**：从零到 AgentTeams on Synapse 跑起来，重点覆盖生产 K8s 部署的 **ServiceAccount 认证模型**。概念原理（为什么 Synapse、声明式 AS、make_room_admin 等）见 [`synapse.md`](synapse.md)；本文只讲"装什么、配什么、怎么验证"。

> **TL;DR**：Helm 默认就是 Synapse + Postgres + AppService 模式。生产部署只需一条命令 + 覆盖 4 个值（LLM key、admin 密码、publicURL、AS token）。Worker 默认用 K8s ServiceAccount 认证，无需额外配置。

---

## 1. 前置条件

| 项目 | 要求 | 验证命令 |
|------|------|----------|
| Kubernetes 集群 | 1.24+（kind / minikube / k3s / 托管 K8s 均可） | `kubectl version --short` |
| Helm | 3.7+ | `helm version --short` |
| 默认 StorageClass | **必需**（Synapse + Postgres + MinIO 各要一个 PVC） | `kubectl get sc` |
| LLM API Key | 任一 OpenAI 兼容服务 | — |
| 镜像可达性 | 能拉 `ghcr.io/element-hq/synapse:v1.127.0` + `postgres:16-alpine` + AgentTeams 镜像 | `docker pull ghcr.io/element-hq/synapse:v1.127.0` |
| 集群管理员权限 | 安装 CRD / ClusterRole 需要 cluster-admin | `kubectl auth can-i '*' '*' --all-namespaces` |

**不需要的**（常见误解）：
- ❌ 不需要预装 Synapse —— Helm 会以 StatefulSet 部署
- ❌ 不需要预装 Postgres —— Helm 一起部署
- ❌ 不需要 Matrix 域名 —— managed 模式用集群内 FQDN（`<release>-synapse.<ns>.svc.cluster.local`）
- ❌ 不需要预创建 AppService 注册 —— Helm 渲染 Secret，Synapse 启动时加载

---

## 2. 最小可用安装（默认即 Synapse）

```bash
# 1. 加 repo
helm repo add higress.io https://higress.io/helm-charts
helm repo update

# 2. 安装（matrix.provider 默认 = synapse）
helm install agentteams higress.io/agentteams \
  -n agentteams-system --create-namespace \
  --set credentials.llmApiKey=<你的-LLM-key> \
  --set credentials.adminPassword=<你的-admin-密码> \
  --set gateway.publicURL=http://localhost:18080
```

这就完成了。Helm 部署：
- **Synapse + Postgres**（两个 StatefulSet，各一个 PVC）
- **AppService 注册 Secret**（`<release>-synapse-appservice`，内含 AS 注册 YAML）
- **Controller**（Deployment，带 Role + ClusterRole 做 SA 管理 + TokenReview）
- **Higress 网关 + MinIO + Element Web**（Helm 子 chart）
- **Manager CR**（首次启动 bootstrap admin 用户 + 注册 AppService + 跑 smoke test）

**验证安装**：
```bash
# Pod 全部 Running
kubectl get pods -n agentteams-system

# Synapse 加载了 AS 注册（日志无 M_UNKNOWN_TOKEN）
kubectl logs -n agentteams-system -l app.kubernetes.io/component=synapse | grep -i appservice

# Controller smoke test 通过（无 "matrix appservice token not active yet" 硬错误）
kubectl logs -n agentteams-system -l app.kubernetes.io/component=controller | grep -i appservice

# 访问 Element Web（端口转发）
kubectl port-forward -n agentteams-system svc/agentteams-higress-gateway 18080:80
# 浏览器打开 http://localhost:18080
```

---

## 3. 生产部署：覆盖默认值

`values.yaml` 里的默认值是为了"开箱即跑"，**生产部署必须覆盖**以下项：

### 3.1 AppService token（强制）

Helm 无法在首次安装时把自动生成的 token 同步到两个 Secret（runtime-env 给 Controller，synapse-appservice 给 Synapse），所以 `values.yaml` 自带 `"change-me-as-token"` / `"change-me-hs-token"` 占位值。**生产环境必须覆盖**，否则 AS 注册可被预测：

```bash
helm install agentteams higress.io/agentteams \
  -n agentteams-system --create-namespace \
  --set credentials.llmApiKey=<key> \
  --set credentials.adminPassword=<pw> \
  --set gateway.publicURL=https://agentteams.example.com \
  --set matrix.appservice.asToken=$(openssl rand -hex 32) \
  --set matrix.appservice.hsToken=$(openssl rand -hex 32)
```

> 用 values 文件更稳：
> ```yaml
> matrix:
>   appservice:
>     asToken: "<生成的-as-token>"
>     hsToken: "<生成的-hs-token>"
> ```
> `helm install -f my-values.yaml ...`

### 3.2 命名空间安全（共享 Synapse 时强制）

默认 AppService 声明 `@.*:<domain>` 独占用户命名空间，意味着 `as_token` 可以冒充该 homeserver 上**任何本地用户**。仅在 homeserver 被 AgentTeams 独占管理时安全（managed 模式）。

如果指向**已存在 / 共享的 Synapse**（公司 Matrix 等），**必须**收窄命名空间：
```yaml
matrix:
  appservice:
    userNamespaceRegex: "@agentteams-.*:your-server-name"
```
然后把所有 AgentTeams 用户（Worker / Human / Manager）创建在该前缀下。

> 注意：当前 `matrix.mode` 锁定为 `managed`（Helm 部署独立 Synapse），此条主要面向未来 `mode=existing` 或手动指向外部 Synapse 的场景。

### 3.3 镜像加速（中国区）

默认 `ghcr.io/element-hq/synapse` 在国内拉取慢。用区域 registry 或镜像：

```bash
helm install agentteams higress.io/agentteams \
  -n agentteams-system --create-namespace \
  --set global.imageRegistry=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress \
  --set matrix.synapse.image.repository=<你的-synapse-镜像> \
  --set matrix.synapse.image.tag=v1.127.0 \
  ...
```

### 3.4 资源与持久化

Synapse + Postgres 默认资源偏小（256Mi~1Gi）。按实际 Worker 数量调：
```yaml
matrix:
  synapse:
    resources:
      requests: { cpu: 500m, memory: 1Gi }
      limits: { cpu: 2, memory: 4Gi }
    persistence:
      size: 50Gi
      storageClassName: <你的-SC>
    postgres:
      persistence:
        size: 50Gi
```

---

## 4. ServiceAccount 场景（生产 K8s 认证模型）

这是 AgentTeams 在 K8s 上跑的**默认且推荐**的认证模型。理解它对运维和排障至关重要。

### 4.1 模型总览

```
┌─────────────────────────────────────────────────────────────┐
│  agentteams-system 命名空间                                   │
│                                                              │
│  Controller Pod                                              │
│  ├─ SA: agentteams-controller（Helm 创建）                   │
│  ├─ Role: pods/configmaps/secrets/services/serviceaccounts  │
│  ├─ ClusterRole: tokenreviews/CRDs/PVs/storageclasses        │
│  │                                                          │
│  │  为每个 Worker 创建独立 SA：                                │
│  │  ┌─ agentteams-worker-alice（SA）                         │
│  │  ├─ agentteams-worker-bob（SA）                           │
│  │  └─ agentteams-admin（SA，Manager 用）                    │
│  │                                                          │
│  │  Worker Pod 启动时：                                       │
│  │  1. 挂载自己的 SA token                                    │
│  │  2. 用 token 调 Controller API                            │
│  │  3. Controller 做 TokenReview 验证（K8s API）              │
│  │  4. 校验 audience = agentteams-controller                 │
│  └──────────────────────────────────────────────────────────│
│                                                              │
│  Worker Pod (alice)                                          │
│  ├─ SA: agentteams-worker-alice（Controller 创建）            │
│  └─ projectedServiceAccountToken（audience-bound）            │
└─────────────────────────────────────────────────────────────┘
```

**关键点**：
- **Worker 不持有任何长期密码** —— 用短时 SA token（K8s 自动轮转）
- **Controller 不需要预共享密钥** —— 通过 K8s TokenReview 验证身份
- **每个 Worker 隔离** —— 独立 SA，独立 RBAC（可进一步收窄）
- **audience 绑定** —— token 只能用于 AgentTeams Controller，不能挪用

### 4.2 Helm 如何配置

**默认就启用**（`controller.workerBackend: "k8s"` + `controller.serviceAccount.create: true`）。无需手动设置。相关 values：

```yaml
controller:
  workerBackend: "k8s"           # 默认；用 K8s SA 模型（vs "docker" 本地模式）
  resourcePrefix: "agentteams-"  # SA 名前缀：agentteams-worker-<name> / agentteams-admin
  resourceAutoPrefix: true       # 命名冲突时自动加随机后缀
  serviceAccount:
    create: true                 # 创建 controller 自己的 SA + RBAC
    name: ""                     # 空 = 自动命名 agentteams-controller
    annotations: {}              # 可加 IRSA / Workload Identity 注解
```

**Controller 读取的 env**（Helm 已自动注入）：
- `AGENTTEAMS_K8S_NAMESPACE` — Worker SA 创建在哪个命名空间（Controller 所在 ns）
- `AGENTTEAMS_AUTH_AUDIENCE` — 默认 `agentteams-controller`；token 必须匹配此 audience
- `AGENTTEAMS_CONTROLLER_WORKER_BACKEND` — `k8s`（控制是否走 SA 路径）

### 4.3 RBAC 要求（Helm 已自动创建）

Controller 的 SA 需要**两层 RBAC**（Helm 的 `templates/controller/rbac.yaml` 自动渲染）：

**namespace-scoped Role**（管理 Worker 资源）：
```yaml
resources: [pods, pods/log, pods/exec, configmaps, secrets, services, serviceaccounts, events]
verbs:   [get, list, watch, create, update, patch, delete]
resources: [workers, teams, humans, managers]   # CRD
```

**cluster-scoped ClusterRole**（TokenReview 必需）：
```yaml
resources: [tokenreviews]                         # 认证 Worker
verbs:   [create]
resources: [customresourcedefinitions]           # 启动时注册 CRD
resources: [persistentvolumes, persistentvolumeclaims, storageclasses]  # MinIO/Synapse PVC
```

> **如果你用自定义 SA**（`controller.serviceAccount.create=false` + 指定 `name`），**必须手动**给该 SA 绑定上述 RBAC，否则 Controller 无法创建 Worker SA、无法 TokenReview、无法注册 CRD。

### 4.4 云上 IRSA / Workload Identity（可选增强）

如果你的集群跑在阿里云 / AWS / GCP，可给 Controller SA 注解云身份，让 Controller 直接用云 IAM（不用静态 AK/SK）：

```yaml
controller:
  serviceAccount:
    annotations:
      # 阿里云 RAM 角色（ACK + RRSA）
      ack.aliyun.com/role-arn: "acs:ram::<account>:role/agentteams-controller"
      ack.aliyun.com/oidc-provider-arn: "acs:ram::<account>:oidc-provider/ack-rrsa-<id>"
      # 或 AWS IRSA
      # eks.amazonaws.com/role-arn: "arn:aws:iam::<account>:role/agentteams-controller"
```

这样 Controller 访问 OSS / APIG 等云资源时用临时凭证，无需在 values 里写静态 AK/SK。

### 4.5 Worker 创建流程（SA 视角）

当 `kubectl apply -f worker.yaml` 创建一个 Worker CR 时：

1. **Controller reconciler** 收到事件
2. `EnsureServiceAccount(workerName)` —— 创建 `agentteams-worker-<name>` SA（带 label `agentteams.io/worker=<name>`）
3. `EnsureWorkerDeployment` —— 创建 Worker Pod，`serviceAccountName: agentteams-worker-<name>`
4. Worker Pod 启动，K8s 自动把 SA token 投影到 `/var/run/secrets/kubernetes.io/serviceaccount/token`（bound-audience）
5. Worker runtime（OpenClaw / QwenPaw / Hermes）调 Controller API 时带这个 token
6. Controller `TokenReview`（K8s API 校验签名 + audience），拿到 Worker 身份
7. Controller 据此身份下发该 Worker 的 Matrix 凭证、MinIO 凭证、Gateway consumer key

**Worker 删除时**：`DeleteServiceAccount(workerName)` 清理对应 SA —— 但 SA token 文件在 Pod 卷里由 K8s 管理，Pod 删了就没了。

### 4.6 SA 场景的排障清单

| 症状 | 可能原因 | 排查 |
|------|---------|------|
| Worker Pod 起不来，日志报 `403 Forbidden` | Controller SA 缺 `serviceaccounts create` 权限 | `kubectl describe role agentteams-controller -n agentteams-system` 看 rules 是否含 serviceaccounts |
| Worker 调 Controller API 报 `unauthorized` | audience 不匹配 | 检查 Controller env `AGENTTEAMS_AUTH_AUDIENCE` 与 Worker 期望是否一致 |
| Worker Pod 日志 `token has expired` | K8s token 轮转 + Worker 未刷新 | 正常情况 K8s 1.21+ 自动轮转；检查 Pod 是否长时间未重启（>1年） |
| Controller 日志 `tokenreviews.authentication.k8s.io is forbidden` | ClusterRole 缺失或被删 | `kubectl get clusterrolebinding agentteams-controller-tokenreview` |
| 创建 Worker 卡住，CR `status` 不更新 | Controller SA 缺 CRD write 权限 | `kubectl auth can-i update workers/agentteams.io --as system:serviceaccount:agentteams-system:agentteams-controller` |

---

## 5. 非 SA 场景（本地 / Docker 模式）

如果不用 K8s SA（本地 Docker、`workerBackend: docker`），Controller 在 Worker 容器里挂静态凭证文件，不走 TokenReview。这种模式下：
- `controller.workerBackend: "docker"`
- 不需要 Controller 的 `serviceaccounts` / `tokenreviews` RBAC
- Worker 用预共享的 consumer key 认证 Gateway

**但本文档聚焦 Synapse 生产部署，默认就是 K8s + SA 模式**。本地模式主要用于开发，见 [docs/quickstart.md](quickstart.md)。

---

## 6. 切换 provider（Synapse ↔ Tuwunel）

```bash
# 切到 Tuwunel（轻量本地场景）
helm upgrade agentteams higress.io/agentteams \
  -n agentteams-system --reuse-values \
  --set matrix.provider=tuwunel

# 切回 Synapse（默认）
helm upgrade agentteams higress.io/agentteams \
  -n agentteams-system --reuse-values \
  --set matrix.provider=synapse
```

> **重要**：Tuwunel 和 Synapse 是两套独立数据库。切换不会迁移房间/账号，切换后需重新 provision Worker/Team。计划维护窗口操作。

---

## 7. 卸载

```bash
# 默认：uninstall hook 先清 CR（触发 Controller finalizer 清理 Matrix 用户/OSS 数据）
helm uninstall agentteams -n agentteams-system

# 完全清理
kubectl delete namespace agentteams-system
```

`controller.uninstallHook.enabled: true`（默认）会在卸载前跑一个 Job，确保 Controller 还活着时清完所有 Worker/Manager/Team/Human CR，触发 finalizer 清理 Synapse 用户、释放 OSS 数据、删 Worker SA。设为 `false` 跳过（需手动删 CR）。

---

## 8. 完整 values 示例（生产 K8s + Synapse + SA）

`prod-values.yaml`：
```yaml
# LLM
credentials:
  llmApiKey: "<你的-key>"
  llmBaseUrl: "https://api.openai.com/v1"   # 非 OpenAI 必填
  defaultModel: "gpt-4o"
  adminPassword: "<强密码>"

# 网关
gateway:
  publicURL: "https://agentteams.example.com"   # 必填，与 Ingress 域名一致

# Matrix = Synapse（默认）
matrix:
  provider: synapse
  appservice:
    asToken: "<openssl rand -hex 32>"
    hsToken: "<openssl rand -hex 32>"
    # 共享 Synapse 时收窄（见 3.2）
    # userNamespaceRegex: "@agentteams-.*:agentteams.example.com"
  synapse:
    resources:
      requests: { cpu: 500m, memory: 1Gi }
      limits: { cpu: 2, memory: 4Gi }
    persistence:
      size: 50Gi

# Controller（SA 模型默认启用）
controller:
  workerBackend: "k8s"
  serviceAccount:
    create: true
    # 云上 IRSA（可选，见 4.4）
    # annotations:
    #   ack.aliyun.com/role-arn: "acs:ram::<account>:role/agentteams-controller"

# 镜像（中国区加速）
global:
  imageRegistry: higress-registry.cn-hangzhou.cr.aliyuncs.com/higress
```

```bash
helm install agentteams higress.io/agentteams \
  -n agentteams-system --create-namespace \
  -f prod-values.yaml
```

---

## 9. 相关参考

- [Synapse 支持概念指南](synapse.md) —— 声明式 AS、make_room_admin、命名空间安全模型
- [快速开始](quickstart.md) —— 本地 Docker 安装（Tuwunel）
- [Helm values 完整清单](../helm/agentteams/values.yaml) —— 所有可配项
- [架构总览](architecture.md) —— Manager/Worker/Matrix/Gateway/Storage 分层
- [`design/synapse-interface-contracts.md`](../design/synapse-interface-contracts.md) —— Synapse 1.127 接口契约（内部）
