# AgentTeams 内网部署指南（离线镜像 + 子 chart 打包 + 私有仓库替换）

本文件适用于：**无公网 / 纯内网 Kubernetes 集群**部署 AgentTeams Helm Chart。
覆盖三件事：

1. 完整镜像清单（含 Higress 子 chart）
2. 把 Higress 子 chart 拉下来、解压、打进当前 chart（vendor）
3. 用内网私有镜像仓库替换所有镜像地址

> 说明：本文镜像清单是把 Higress 子 chart（`higress-2.2.1`）实际下载解压后，
> 逐项解析 `values.yaml` + 模板渲染逻辑得到的，等价于 `helm template` 的输出。
> 你可用本文件末尾的命令自行复核。本环境未安装 helm CLI，故用 `curl + tar` 完成等价操作。

---

## 1. 完整镜像清单

源镜像仓库均为阿里云 ACR 公共只读仓库：`higress-registry.cn-hangzhou.cr.aliyuncs.com`。
内网部署需把下列镜像同步到你的私有 registry。

### 1.0 镜像 tag 从哪来 / 版本确认（⚠️ 必读，避免 404）

AgentTeams 自有镜像（controller / manager / worker）的 tag 解析顺序（见 [_helpers.tpl](templates/_helpers.tpl)）：

```
组件 image.tag  →  global.imageTag  →  printf "v%s" .Chart.AppVersion
```

- [Chart.yaml](Chart.yaml) 第 6 行 `appVersion: "1.1.1"`，`global.imageTag` 默认空 → 会解析成 **`v1.1.1`**。
- 🚨 **但 `v1.1.1` 这个 tag 在仓库里根本不存在！** 实测 `docker pull ...:v1.1.1` 报
  `manifest unknown`。chart 的 `appVersion` 是**过时的**，从来没发布过 v1.1.1 镜像。
- **必须显式设 `global.imageTag` 为一个真实存在的 tag**。已实测（2026-08）`agentteams/agentteams-controller`
  的可用 tag：`latest`、`v1.1.2`、`v1.2.0`、`v1.2.0-beta.1`、`v1.2.0.hotfix1`、`v1.2.0.hotfix2`、`v1.2.1`。
  manager / worker 同此列表。
- **推荐用 `v1.2.0`**（与仓库当前发行版一致，稳定可复现）；想用最新可换 `v1.2.1`，追新用 `latest`（不可复现）。
  本文示例统一用 `v1.2.0`。
- Higress 子 chart 镜像 tag 取其 `appVersion` = `2.2.1`（已实测 gateway/higress/pilot/console:2.2.1 与 plugin-server:1.0.0 均存在）；
  tuwunel/minio/element-web 用日期 tag `20260216`（已实测存在）。这些**不用改**。
- 同步前自检任意 tag 是否存在：
  ```bash
  TOKEN=$(curl -sS "https://dockerauth.cn-hangzhou.aliyuncs.com/auth?service=registry.aliyuncs.com:cn-hangzhou:china:cri-r0xfyoxudmtseqq7&scope=repository:agentteams/agentteams-controller:pull" | python -c "import sys,json;print(json.load(sys.stdin)['token'])")
  curl -sS -H "Authorization: Bearer $TOKEN" https://higress-registry.cn-hangzhou.cr.aliyuncs.com/v2/agentteams/agentteams-controller/tags/list
  ```

### 1.1 默认必装（install 后一定会拉取）

**AgentTeams 自有组件**（tag **必须设 `global.imageTag`**，本文用 `v1.2.0`，见 1.0）：

| # | 镜像 | 用途 |
|---|------|------|
| 1 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-controller:v1.2.0` | 控制器（必装；preflight/uninstall hook 也复用它） |
| 2 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager:v1.2.0` | Manager Agent（manager.enabled=true） |
| 3 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-worker:v1.2.0` | Worker，默认 openclaw runtime（按需创建） |
| 4 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/tuwunel:20260216` | Matrix 主服务（默认 provider=tuwunel） |
| 5 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/minio:20260216` | 对象存储（默认 provider=minio） |
| 6 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/element-web:20260216` | IM 前端（elementWeb.enabled=true） |

**Higress 子 chart（gateway.provider=higress + mode=managed 时部署）**，
渲染规则 `{global.hub}/higress/{image}:{tag|appVersion=2.2.1}`：

| # | 镜像 | 用途 |
|---|------|------|
| 7 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/gateway:2.2.1` | Envoy 数据面网关 |
| 8 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/higress:2.2.1` | Higress Controller（控制面） |
| 9 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/pilot:2.2.1` | Istio Pilot（与 controller 同 Pod） |
| 10 | `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/console:2.2.1` | Higress 控制台（higress-console） |

### 1.2 可选镜像（仅当对应开关打开时才需要）

| 触发条件 | 镜像 |
|----------|------|
| Worker 用 copaw runtime | `.../agentteams/agentteams-copaw-worker:v1.2.0` |
| Worker 用 hermes runtime | `.../agentteams/agentteams-hermes-worker:v1.2.0` |
| Worker 用 openhuman | `.../higress/agentteams-openhuman-worker:<tag>`（**实测该路径当前无 tag**，openhuman 较新，用前先按 1.0 方法确认） |
| `matrix.provider=synapse` | `ghcr.io/element-hq/synapse:v1.127.0`、`postgres:16-alpine`（Docker Hub） |
| `higress.global.enableRedis=true` | `.../higress/redis-stack-server:7.4.0-v3` |
| `higress.global.enablePluginServer=true` | `.../higress/plugin-server:1.0.0` |
| `higress.higress-console.o11y.enabled=true` | `.../higress/grafana:9.3.6`、`.../higress/prometheus:v2.40.7`、`.../higress/loki:2.9.4` |
| `higress.higress-console.certmanager.enabled=true` | `.../higress/cert-manager:v1.11.0` |
| `storage.provider=oss` 或 `gateway.provider=ai-gateway` | `credentialProvider.image.repository`（chart 不提供，需自定义） |

> `...` = `higress-registry.cn-hangzhou.cr.aliyuncs.com`。
> 默认配置下 redis / **plugin-server** / o11y / certmanager 都是关闭的（`enableRedis:false`、`enablePluginServer:false`、`o11y.enabled:false`、`certmanager.enabled:false`）。

#### 你需要 plugin-server → 同步这个镜像并打开开关

镜像：`higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/plugin-server:1.0.0`

在 `values-internal.yaml` 里加（`higress.global.hub` 覆盖后，渲染成 `<内网hub>/higress/plugin-server:1.0.0`）：

```yaml
higress:
  global:
    enablePluginServer: true      # 打开 plugin-server Deployment
```

> plugin-server 负责 WASM 插件（如 key-auth、限流等）的独立运行/加载。开启后镜像清单要把 `plugin-server:1.0.0` 一并同步。

---

## 2. 把 Higress 子 chart 拉下来、解压、打进当前 chart（vendor）

`helm/agentteams/Chart.yaml` 声明依赖 `higress` 2.2.1（仓库 `https://higress.io/helm-charts`），
但仓库里**没有 `charts/` 目录**，所以离线前必须先在有公网的机器上把子 chart 拿到手。

> 本仓库当前已按「方式 B（解压成目录）」vendor 了子 chart：`helm/agentteams/charts/higress/`。
> 如果你重新拉取，请按下面任选一种。

### 2.0 下载包与地址（Higress 子 chart）

| 项 | 值 |
|----|----|
| Chart 名 / 版本 | `higress` / `2.2.1` |
| Helm 仓库 | `https://higress.io/helm-charts` |
| 仓库索引 | `https://higress.io/helm-charts/index.yaml` |
| 直接 tgz | `https://higress.io/helm-charts/higress-2.2.1.tgz` |
| digest（来自 [Chart.lock](Chart.lock)） | `sha256:bfda3317506f04c1088d398ca7b10137409999ec54e1d36b7b5d525145ee931b` |
| 官方配置文档 | https://higress.io/en/docs/latest/user/configurations/ |

四种等价拉取方式（任选其一）：

```bash
# ① helm + 仓库（先加 repo）
helm repo add higress https://higress.io/helm-charts
helm pull higress/higress --version 2.2.1

# ② helm 直接指定 repo（不用 repo add）
helm pull higress --version 2.2.1 --repo https://higress.io/helm-charts

# ③ helm dependency build（在 chart 目录里，按 Chart.yaml/Chart.lock 解析）
helm dependency build helm/agentteams      # 产出 helm/agentteams/charts/higress-2.2.1.tgz

# ④ 纯 curl（无 helm 也能拉）
curl -fsSL -o higress-2.2.1.tgz https://higress.io/helm-charts/higress-2.2.1.tgz
```

校验完整性（digest 应与 Chart.lock 一致）：

```bash
# sha256(下载的 tgz) 应等于 Chart.lock 里的 digest
sha256sum higress-2.2.1.tgz
# 或用 helm 验证依赖一致性
helm dependency list helm/agentteams       # STATUS = ok
```

> 该 tgz 是**自包含**的：解压后 `charts/higress/charts/` 内已自带 `higress-core` 与 `higress-console`，
> 安装时无需再联网拉取任何子依赖。

### 方式 A：标准 `helm dependency build`（保留 tgz，最标准）

```bash
cd helm/agentteams
helm dependency build
# 产出 helm/agentteams/charts/higress-2.2.1.tgz
helm dependency list          # STATUS 应为 ok
```

内网安装时把整个 `helm/agentteams/`（含 `charts/higress-2.2.1.tgz`）拷过去即可。

### 方式 B：解压成目录（vendor 进仓库，便于审计/修改，推荐用于离线）

```bash
cd helm/agentteams

# 1) 拉取 tgz（无需 helm，纯 curl 也可）
helm pull higress --version 2.2.1 --repo https://higress.io/helm-charts \
  -d charts/
# 或纯 curl：
# curl -fsSL -o charts/higress-2.2.1.tgz https://higress.io/helm-charts/higress-2.2.1.tgz

# 2) 解压成目录
tar -xzf charts/higress-2.2.1.tgz -C charts/

# 3) 删除 tgz，避免「目录 + tgz 同时存在」造成 Helm 依赖重复告警
rm charts/higress-2.2.1.tgz

# 4) 验证：解压后的 chart 内部已自带 higress-core / higress-console，是自包含的
ls charts/higress/charts            # 应见 higress-core  higress-console
grep -E '^(name|version):' charts/higress/Chart.yaml   # name: higress, version: 2.2.1
```

完成后 `helm template helm/agentteams` / `helm install` 会直接使用 `charts/higress/`，**不再访问公网**。
可以把整个 `helm/agentteams/`（含 `charts/higress/`）提交到内网代码库。

> 注意：解压后的 `charts/higress/` 与 `Chart.yaml` 里的 `dependencies:` 条目并存是正常的——
> Helm 优先用 `charts/` 下已存在的 chart，`helm dependency build` 不会重复下载。
> 只要**别同时保留 `charts/higress/` 目录和 `charts/higress-2.2.1.tgz`** 即可。

---

## 3. 内网镜像仓库替换

### 3.1 关键认知（避免踩坑）

- ⚠️ `global.imageRegistry` 是**孤儿值**——没有任何模板引用它，改它**不生效**。
- AgentTeams 各组件镜像走**完整路径**的 `image.repository`，必须**逐个覆盖**。
- Higress **核心**镜像走 `global.hub` 模式（`{hub}/higress/{image}`），改 `higress.global.hub` 即可。
- 但 `higress-console` 的镜像走**完整路径** `image.repository`，**不**走 hub 模式，需单独覆盖。
- 下文示例假设内网仓库为 `harbor.corp.local`，镜像按原路径（`agentteams/*`、`higress/*`）平铺。

### 3.2 第一步：把镜像同步到内网仓库

源仓库是阿里云 ACR 公共只读，匿名可拉。推荐 `skopeo`（无需 docker daemon，最适合离线）：

```bash
# 在一台能同时访问公网和内网仓库的跳板机上执行
SRC=higress-registry.cn-hangzhou.cr.aliyuncs.com
DST=harbor.corp.local      # 改成你的内网仓库

# 默认必装（10 个）—— AgentTeams 镜像用 v1.2.0（见 1.0，v1.1.1 不存在）
IMAGES=(
  agentteams/agentteams-controller:v1.2.0
  agentteams/agentteams-manager:v1.2.0
  agentteams/agentteams-worker:v1.2.0
  higress/tuwunel:20260216
  higress/minio:20260216
  higress/element-web:20260216
  higress/gateway:2.2.1
  higress/higress:2.2.1
  higress/pilot:2.2.1
  higress/console:2.2.1
)
for img in "${IMAGES[@]}"; do
  skopeo copy --retry-times 3 \
    docker://${SRC}/${img} \
    docker://${DST}/${img}
done

# 你需要 plugin-server → 追加
# skopeo copy docker://${SRC}/higress/plugin-server:1.0.0 docker://${DST}/higress/plugin-server:1.0.0
# 如启用其他 runtime，按需追加（示例）
# skopeo copy docker://${SRC}/agentteams/agentteams-copaw-worker:v1.2.0 docker://${DST}/agentteams/agentteams-copaw-worker:v1.2.0
# synapse 模式（来自 Docker Hub / ghcr，需跳板机能访问）
# skopeo copy docker://ghcr.io/element-hq/synapse:v1.127.0 docker://${DST}/element-hq/synapse:v1.127.0
# skopeo copy docker://docker.io/library/postgres:16-alpine   docker://${DST}/library/postgres:16-alpine
```

> 没有 `skopeo` 时，用 `docker pull ${SRC}/${img} && docker tag ${SRC}/${img} ${DST}/${img} && docker push ${DST}/${img}` 等价替代。

### 3.3 第二步：写一份内网覆盖 values

新建 `helm/agentteams/values-internal.yaml`（示例，按你的实际仓库/标签改）：

```yaml
global:
  imageTag: "v1.2.0"        # ⚠️ 必填：v1.1.1 不存在！见 1.0。AgentTeams 自有镜像 tag 都取它

# 内网仓库需要拉取凭证时（私有 registry）配这里；controller Pod 会用
imagePullSecrets:
  - name: corp-registry-pull

# —— AgentTeams 组件：逐个覆盖完整 repository ——
controller:
  image:
    repository: harbor.corp.local/agentteams/agentteams-controller
matrix:
  tuwunel:
    image:
      repository: harbor.corp.local/higress/tuwunel
storage:
  minio:
    image:
      repository: harbor.corp.local/higress/minio
elementWeb:
  image:
    repository: harbor.corp.local/higress/element-web
manager:
  image:
    repository: harbor.corp.local/agentteams/agentteams-manager
worker:
  defaultImage:
    openclaw:
      repository: harbor.corp.local/agentteams/agentteams-worker
    # 用到其他 runtime 再加：
    # copaw:   { repository: harbor.corp.local/agentteams/agentteams-copaw-worker }
    # hermes:  { repository: harbor.corp.local/agentteams/agentteams-hermes-worker }
    # openhuman: { repository: harbor.corp.local/higress/agentteams-openhuman-worker }

# —— Higress 子 chart ——
higress:
  global:
    local: true
    hub: harbor.corp.local          # 覆盖核心镜像的 hub（gateway / higress / pilot 等）
    imagePullSecrets:               # Higress Pod 用这个拉私有镜像
      - corp-registry-pull
  higress-console:
    image:
      repository: harbor.corp.local/higress/console   # console 走完整路径，单独覆盖
    # 若启用 o11y，再逐个覆盖 grafana/prometheus/loki 的 image.repository
```

> 注意 Higress 核心镜像渲染为 `{global.hub}/higress/{image}`，所以把 `hub` 设成内网仓库根地址后，
> 内网里镜像必须保留 `higress/` 子路径（即上一步同步成 `harbor.corp.local/higress/gateway` 等）。

### 3.4 第三步：安装 + 验证

```bash
# 先渲染，肉眼检查所有 image: 都已指向内网仓库
helm template agentteams helm/agentteams \
  -f helm/agentteams/values-internal.yaml \
  --set gateway.publicURL=http://agentteams.internal:18080 \
  | grep -E '^\s*image:' | sort -u

# 确认无误后安装
helm install agentteams helm/agentteams \
  -n agentteams --create-namespace \
  -f helm/agentteams/values-internal.yaml \
  --set gateway.publicURL=http://agentteams.internal:18080 \
  --set credentials.llmApiKey=sk-xxx

# 看是否还有 ImagePullBackOff / ErrImagePull
kubectl get pods -n agentteams -w
kubectl describe pod -n agentteams <拉取失败的 pod> | grep -A5 -i events
```

若出现 `ImagePullBackOff` 且事件提示 `401/403`：内网仓库需登录——确认 `imagePullSecrets` 已创建
（`kubectl create secret docker-registry corp-registry-pull ...`）且 Higress 侧也配了 `higress.global.imagePullSecrets`。

---

## 4. 两套可观测性（observability）是干什么的

values 里出现 **两套互不相关** 的可观测性，别混淆：

### 4.1 Higress o11y（grafana + prometheus + loki）—— 网关监控

开 `higress.higress-console.o11y.enabled: true` 才部署。监控的是 **Higress 网关本身**的流量/错误/日志：

| 组件 | 镜像 | 作用 |
|------|------|------|
| Prometheus | `higress/prometheus:v2.40.7` | 抓取并存储网关指标（QPS、延迟、错误率等） |
| Grafana | `higress/grafana:9.3.6` | 指标可视化面板（看图） |
| Loki | `higress/loki:2.9.4` | 网关日志聚合（查日志） |

> 内网纯跑 AgentTeams、不关心网关运维指标的话，**可以不开**（默认就是关的）。
> 要开就同步这 3 个镜像，并在 `values-internal.yaml` 里逐个覆盖 `image.repository`（它们走完整路径，不走 `global.hub`）。

### 4.2 AgentTeams `cms`（阿里云 ARMS / CMS 2.0）—— 上云 APM

见 [values.yaml](values.yaml) 的 `cms:` 块（默认 `enabled: false`）。这是把 **Manager 和所有 Worker** 的 OTLP
trace/metric 上报到**阿里云 ARMS**，需要 `endpoint/licenseKey/project/workspace`。

> **纯内网/离线部署用不了**（依赖阿里云公网服务），保持 `cms.enabled: false` 即可，无需同步任何镜像。

一句话：**内网部署这两套都不用动**；只有当你想看 Higress 网关的监控图表时才开 4.1（多同步 3 个镜像）。

---

## 5. AgentTeams Dashboard（管理面板）

新版有一个可视化管理面板，用于管理 Worker / Team / Human / Manager / Matrix。

### 5.1 现状：Helm 不带，仅安装脚本支持

- 镜像：`higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-dashboard:v1.2.0-beta.1`
  （版本与 AgentTeams 发行版**独立**，当前默认 `v1.2.0-beta.1`，见 `install/agentteams-install.sh:54`）
- ⚠️ **官方仅支持通过 Bash 安装脚本部署**（`install/agentteams-install.sh:11` 明确写明
  "agentteams-dashboard is currently only supported via the Bash installer"）。
  **Helm chart 里没有任何 dashboard 资源**（已全局搜索确认）。
- 容器端口 **3000**（脚本里 `-p 13000:3000`），数据卷 `/app/db`。

### 5.2 走 Helm 部署又想要 Dashboard：手动起一个 Deployment（DIY，非官方支持）

Dashboard 本质是一个调控制器 REST API 的前端，可以手动在集群里起。镜像先同步进内网仓库：

```bash
skopeo copy docker://higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-dashboard:v1.2.0-beta.1 \
            docker://harbor.corp.local/agentteams/agentteams-dashboard:v1.2.0-beta.1
```

然后部署（服务名以 `kubectl get svc -n agentteams` 实际为准，下面用 `<rel>` 代表 helm release 名）：

```yaml
# dashboard.yaml
apiVersion: v1
kind: Service
metadata:
  name: agentteams-dashboard
  namespace: agentteams
spec:
  selector: { app: agentteams-dashboard }
  ports: [{ port: 80, targetPort: 3000 }]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agentteams-dashboard
  namespace: agentteams
spec:
  replicas: 1
  selector: { matchLabels: { app: agentteams-dashboard } }
  template:
    metadata: { labels: { app: agentteams-dashboard } }
    spec:
      containers:
      - name: dashboard
        image: harbor.corp.local/agentteams/agentteams-dashboard:v1.2.0-beta.1
        ports: [{ containerPort: 3000 }]
        env:
        - { name: AGENTTEAMS_CONTROLLER_URL,      value: "http://<rel>-controller:8090" }
        - { name: NEXT_PUBLIC_MATRIX_API_URL,     value: "http://<rel>-tuwunel:6167" }
        - { name: AGENTTEAMS_FS_ENDPOINT,         value: "http://<rel>-minio:9000" }
        - { name: AGENTTEAMS_FS_BUCKET,           value: "agentteams-storage" }
        - { name: AGENTTEAMS_ADMIN_USER,          value: "admin" }
        - { name: AGENTTEAMS_ADMIN_PASSWORD,      value: "<你的 admin 密码>" }
        # 关键：调用控制器 API 的鉴权 token（见下方说明）
        - { name: AGENTTEAMS_AUTH_TOKEN,          value: "<从控制器获取>" }
        - { name: AGENTTEAMS_AI_GATEWAY_ADMIN_URL,value: "http://<rel>-higress-console:8080" }
```

访问：`kubectl port-forward svc/agentteams-dashboard 13000:80 -n agentteams` → 浏览器开 `http://localhost:13000`。

### 5.3 主要摩擦点：`AGENTTEAMS_AUTH_TOKEN`

Dashboard 调控制器 REST API 需要一个控制器认可的 token（控制器对 `/api/v1/*` 全程有鉴权）。
Bash 安装脚本会自动生成；**Helm 部署下需要你自己拿到**。控制器有 token 过期机制
（`AGENTTEAMS_AUTH_TOKEN_EXPIRATION_SECONDS`，见 `config.go:296`）和 K8s SA token 鉴权。
建议先确认 Helm 部署的控制器如何签发/暴露这个 token（看 `runtime-env` Secret 或控制器日志），
否则 Dashboard 会出现能打开页面但调接口 401 的情况。

> 如果不想折腾 token，**官方推荐的 dashboard 体验还是用 Bash 安装脚本**
> （`./agentteams-install.sh`，`AGENTTEAMS_DASHBOARD=1`，它跑在 Docker 里，自动处理鉴权）。

---

## 6. 内网部署检查清单（`helm install` 前必看）

下面是核对过模板后、最容易让内网安装卡住或失败的点：

### ⚠️ 6.1 Preflight LLM 探针：默认 strict，LLM 不可达会直接装不上

[preflight/llm-job.yaml](templates/preflight/llm-job.yaml) 是个 `pre-install,pre-upgrade` 的 Helm hook Job，
安装/升级时**最先**运行 `agt llm-preflight`，**直连** `credentials.llmBaseUrl` 探测模型（不经 Higress）。
默认 `preflight.llm.enabled=true` 且 `strict=true` → **探针失败 = `helm install` 失败**（`backoffLimit:0`，单次 120s）。

内网常见场景：装的时候内部 LLM 还没起好、或从集群 Pod 不可达 → 安装卡死。两种解法：

```yaml
# 解法 A：首次拉起时彻底关掉探针（装完再开）
preflight:
  llm:
    enabled: false

# 解法 B：保留探针但只告警不阻断（推荐，能提前发现 LLM 问题）
preflight:
  llm:
    strict: false
```

> 探针是从**集群内 Pod** 发起的，所以 `credentials.llmBaseUrl` 必须是集群内能解析/可达的地址
> （如 `http://vllm.internal:8000/v1`），而不是只在你本机能访问的地址。

### ⚠️ 6.2 StorageClass：tuwunel / minio 的 PVC 需要可用存储

tuwunel、minio（以及 synapse 模式下的 postgres/synapse）都是带持久化的 StatefulSet，
通过 `volumeClaimTemplates` 建 PVC。`storageClassName: ""`（默认）= 用集群**默认 StorageClass**。

- 内网裸机/自建 K8s 若**没有默认 StorageClass**，这些 PVC 会一直 Pend，Pod 起不来。
- 解法：要么给集群装一个默认 SC（如 local-path / nfs-subdir-external-provisioner），
  要么显式指定：
  ```yaml
  matrix:
    tuwunel:
      persistence:
        storageClassName: "local-path"
  storage:
    minio:
      persistence:
        storageClassName: "local-path"
  ```
- 或临时关掉持久化（`persistence.enabled: false`）做验证——**数据不持久，仅测试用**。

### ⚠️ 6.3 镜像必须先就位（preflight hook 最先拉 controller 镜像）

preflight hook 在 `pre-install` 阶段就拉 `agentteams-controller` 镜像。所以**装之前**
所有默认镜像（见第 1 节）必须已在内网仓库，且 `imagePullSecrets` / `higress.global.imagePullSecrets` 已配好，
否则第一个失败的就是 preflight Job 的 `ImagePullBackOff`。

### ✅ 6.4 必填 values（不填 `helm install` 直接报错）

- `credentials.llmApiKey`（[llm-secret.yaml](templates/preflight/llm-secret.yaml)、[runtime-env.yaml](templates/secrets/runtime-env.yaml) 都 `required`）
- `gateway.publicURL`（[_helpers.infra.tpl](templates/_helpers.infra.tpl) `required`）—— 浏览器/对外访问地址
- `storage.bucket` 有默认值 `agentteams-storage`，一般不用动

### ℹ️ 6.5 Higress 网关是 ClusterIP（`global.local: true`）

默认 `higress.global.local: true` → 网关 Service 为 ClusterIP，**不创建云 LoadBalancer**。
浏览器访问 Element Web / Matrix 需自行 `kubectl port-forward` 或配 Ingress（见 [NOTES.txt](templates/NOTES.txt)）。
若你的内网集群有可用的 LoadBalancer，可设 `higress.global.local: false` 并调整 Service type。

### ℹ️ 6.6 子 chart vendor / Chart.lock 状态

- `charts/higress/` 已解压 vendor（无残留 tgz）。
- `Chart.lock` 仍记录 `higress 2.2.1` + digest `sha256:bfda...931b`，与 vendor 内容一致，`helm install` 不会再联网。

---

## 附：自行复核镜像清单的命令

```bash
helm dependency build helm/agentteams          # 或确认 charts/higress/ 已 vendor
helm template agentteams helm/agentteams \
  -f helm/agentteams/values-internal.yaml \
  --set gateway.publicURL=http://x \
  | grep -E '^\s*image:' | sed 's/^[[:space:]]*//' | sort -u
```
