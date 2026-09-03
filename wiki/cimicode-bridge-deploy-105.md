# cimicode-bridge @105 部署手册（当前 Mock 环境）

> 更新：2026-09-03
> 环境：10.254.254.105（k3s 单节点，hostname `extvdiadmin-vmware-virtual-platform`，另有一个 k3s-103 节点）
> 命名空间：`agentos`
> 镜像：`agentteams/cimicode-bridge:v0.0.3`（本地 docker → 导入 k3s containerd）
>
> **当前状态**：Matrix 收侧链路已通（whoami/sync/mention 过滤/三段式 history），gateway `baseUrl` 为模拟占位，待 cimicode gateway 就绪后只改 MinIO 里的 `openclaw.json`，bridge 代码不动。

---

## 1. S3（MinIO）目录与内容来源

### 1.1 连接信息（与现有 worker 完全一致）

| 项 | 值 |
|---|---|
| Endpoint | `http://minio-sim.s3-sim.svc.cluster.local:9000`（NodePort `30833`，主机可经 `10.254.254.105:30833` 访问） |
| Access Key | `minio-sim-root` |
| Secret Key | `MinioSim#2026` |
| Bucket | `agentteams-storage` |
| Storage Prefix | `agentteams/agentteams-storage` |

> 注意 bucket 内有两层路径：`{STORAGE_PREFIX}/agents/{worker}/...`，即对象 key 形如
> `agentteams-storage/agentteams/agentteams-storage/agents/cimicode-agent/openclaw.json`

### 1.2 bridge 读取的目录结构

bridge 启动时（重试 6 次 × 5 秒）按以下 key 拉取：

```text
agentteams/agentteams-storage/agents/cimicode-agent/
├── openclaw.json     # 行为配置主载体：Matrix token + bridge.gateway 绑定
├── AGENTS.md         # 软指令，拼进每轮 agentMd
└── SOUL.md           # 人格，拼进每轮 agentMd
```

### 1.3 里面的东西是怎么来的

**当前（Mock 阶段）**：全部由 `setup-105.sh`（在 105 的 `/home/extvdiadmin/agentteams-deploy/`）模拟调谐生成：

| 步骤 | 做了什么 | 产出 |
|---|---|---|
| 1. 注册 Matrix bot | 调 Synapse `/register`（m.login.dummy）+ `/login` | bot `@cimicode-agent:agentteams-synapse.agentos.svc.cluster.local` 的 `access_token` |
| 2. 进房 | 用 `frontend-agent` 的 token 发 `/invite`，再用 bot token `/join` | bot 加入房间 `!oCOZLhucNczxLoAoUU:agentteams-synapse...` |
| 3. 写 MinIO | `mc pipe` 写入三个文件 | 下方 `openclaw.json`（token 为第 1 步真实值，gateway 字段为占位） |
| 4. 导镜像 | `docker save` → `k3s ctr images import` | containerd 内可用的 bridge 镜像 |

`openclaw.json` 实际内容（token 为 setup 时 Synapse 返回的真实值）：

```json
{
  "channels": {
    "matrix": {
      "homeserver": "http://agentteams-synapse.agentos.svc.cluster.local:8008",
      "accessToken": "syt_Y2ltaWNvZGUtYWdlbnQ_..."
    }
  },
  "bridge": {
    "runtime": {
      "baseUrl": "http://cimicode-gateway.agentos.svc.cluster.local:8080",
      "templateId": "cimicode-default",
      "sessionId": "sess-cimicode-agent-001",
      "sandboxId": "sbx-cimicode-agent-001"
    }
  }
}
```

**将来（调谐上线后）**：controller 的 deployer 会自动生成并覆盖这些文件——bridge 重新拉取即可，无需改代码。gateway 就绪后只需更新 `baseUrl` 并填入真实的 `sessionId/sandboxId`。

校验/手工查看：

```bash
kubectl -n s3-sim exec deployment/minio-sim -- sh -c \
  "mc alias set local http://127.0.0.1:9000 minio-sim-root 'MinioSim#2026' >/dev/null; \
   mc cat local/agentteams-storage/agentteams/agentteams-storage/agents/cimicode-agent/openclaw.json"
```

---

## 2. bridge 打包与启动

### 2.1 打包（在 105 的 fork 仓库目录）

代码源：`feature/stateless_cimicode_docking` 分支（≥ `1aecc069`，含 CLI `run` 兼容修复）。

```bash
cd <你105上的fork仓库>            # 例: /workbach/AgentTeams
git pull origin feature/stateless_cimicode_docking

# 构建（VERSION 可自定义；构建成功标志：COPY src 层有实际传输，非 CACHED）
make build-cimicode-bridge VERSION=v0.0.3

# 导入 k3s containerd（docker 里的镜像 k3s 看不到，必须导）
docker save agentteams/cimicode-bridge:v0.0.3 -o /tmp/cim-v0.0.3.tar
echo '<sudo密码>' | sudo -S k3s ctr images import /tmp/cim-v0.0.3.tar
sudo k3s ctr images ls | grep cimicode   # 确认 docker.io/agentteams/cimicode-bridge:v0.0.3
```

> 坑位记录：曾出现“全层 CACHED”导致新 tag 装旧代码——**改了代码必须确认 COPY src 层重新执行**。

### 2.2 启动方式

镜像内：

- `pip install .` 安装为 console script `cimicode-bridge`
- entrypoint：`exec cimicode-bridge "$@"`（`run` 是被吞掉的历史参数，直接传参即可）
- 默认 CMD：`--host 0.0.0.0 --port 8081`

K8s 中由 deployment 拉起，无需手工运行。手工冒烟：

```bash
docker run --rm -p 8081:8081 agentteams/cimicode-bridge:v0.0.3
curl http://127.0.0.1:8081/healthz   # {"status":"ok"}
```

### 2.3 验证（`check-105.sh`）

```bash
bash /home/extvdiadmin/agentteams-deploy/check-105.sh
```

预期输出：

```json
{"worker":"cimicode-bridge","phase":"listening","runtime":"cimicode","matrix_connected":true,"runtime_healthy":true,"ready":true}
{"ready":true}
```

以及房间成员列表里包含 `@cimicode-agent:...`。

---

## 3. deployment.yaml（当前 105 实际生效版本）

位置：105 `/home/extvdiadmin/agentteams-deploy/deployment-cimicode-bridge-105.yaml`
（仓库同源文件：`cimicode-bridge/deploy/deployment-cimicode-bridge-105.yaml`）

```yaml
# cimicode-bridge @105 最终部署清单（值来自 2026-09-03 实地勘察）
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cimicode-bridge
  namespace: agentos
  labels:
    app: cimicode-bridge
spec:
  replicas: 1
  selector:
    matchLabels:
      app: cimicode-bridge
  template:
    metadata:
      labels:
        app: cimicode-bridge
    spec:
      terminationGracePeriodSeconds: 30
      nodeSelector:
        kubernetes.io/hostname: extvdiadmin-vmware-virtual-platform   # 镜像只导入 105，防调度到 103
      containers:
        - name: bridge
          image: agentteams/cimicode-bridge:v0.0.3
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8081
          env:
            # ---------- S3/MinIO（与现有 worker 一致） ----------
            - name: AGENTTEAMS_FS_ENDPOINT
              value: "http://minio-sim.s3-sim.svc.cluster.local:9000"
            - name: AGENTTEAMS_FS_SECURE
              value: "false"
            - name: AGENTTEAMS_FS_ACCESS_KEY
              value: "minio-sim-root"
            - name: AGENTTEAMS_FS_SECRET_KEY
              value: "MinioSim#2026"
            - name: AGENTTEAMS_FS_BUCKET
              value: "agentteams-storage"
            - name: AGENTTEAMS_STORAGE_PREFIX
              value: "agentteams/agentteams-storage"
            - name: AGENTTEAMS_WORKER_NAME
              value: "cimicode-agent"

            # ---------- Matrix ----------
            - name: AGENTTEAMS_MATRIX_URL
              value: "http://agentteams-synapse.agentos.svc.cluster.local:8008"
            - name: AGENTTEAMS_MATRIX_DOMAIN
              value: "agentteams-synapse.agentos.svc.cluster.local"
            - name: AGENTTEAMS_WORKER_MATRIX_USER_ID
              value: "@cimicode-agent:agentteams-synapse.agentos.svc.cluster.local"
            - name: AGENTTEAMS_WORKER_ROOM_ID
              value: "!oCOZLhucNczxLoAoUU:agentteams-synapse.agentos.svc.cluster.local"
            # token 主来源是 S3 openclaw.json；此处留空
            - name: AGENTTEAMS_WORKER_MATRIX_TOKEN
              value: ""

            # ---------- 协作上下文 ----------
            - name: COORDINATION_ROLE
              value: "worker"
            - name: COORDINATION_TEAM
              value: "frontend-team"
            - name: COORDINATION_ROOM
              value: "!oCOZLhucNczxLoAoUU:agentteams-synapse.agentos.svc.cluster.local"
            - name: COORDINATION_LEADER
              value: "@web-team-lead:agentteams-synapse.agentos.svc.cluster.local"
            - name: COORDINATION_ADMIN
              value: ""
            - name: COORDINATION_WORKERS
              value: "@frontend-agent:agentteams-synapse.agentos.svc.cluster.local,@product-agent:agentteams-synapse.agentos.svc.cluster.local"

            # ---------- controller（401 token 刷新） ----------
            - name: AGENTTEAMS_CONTROLLER_URL
              value: "http://agentteams-controller.agentos.svc.cluster.local:8090"
            - name: AGENTTEAMS_AUTH_TOKEN_FILE
              value: ""

            # ---------- StateStore ----------
            - name: BRIDGE_REDIS_URL
              value: ""
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            initialDelaySeconds: 15
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 512Mi
---
apiVersion: v1
kind: Service
metadata:
  name: cimicode-bridge
  namespace: agentos
  labels:
    app: cimicode-bridge
spec:
  selector:
    app: cimicode-bridge
  ports:
    - name: http
      port: 8081
      targetPort: http
```

### 3.1 关键设计点

| 项 | 原因 |
|---|---|
| `namespace: agentos` | 与 Synapse/controller/其他 worker 同 ns，service 互访用短域名 |
| `nodeSelector` 固定 105 | 镜像只 import 到 105 的 containerd；103 未导入会 ImagePullBackOff |
| `imagePullPolicy: IfNotPresent` | 镜像来自本地 containerd，不走 registry |
| Matrix token 留空 env | token 唯一有效来源是 S3 `openclaw.json`；env 仅本地开发 fallback |
| `AGENTTEAMS_WORKER_NAME=cimicode-agent` | 必须与 S3 `agents/<name>/` 目录名一致，bootstrap 按此拼 key |

### 3.2 常用运维命令

```bash
# 状态/日志
kubectl -n agentos get pods -l app=cimicode-bridge
kubectl -n agentos logs -f deployment/cimicode-bridge

# 探针（容器内无 curl，用 python）
POD=$(kubectl -n agentos get pod -l app=cimicode-bridge -o jsonpath='{.items[0].metadata.name}')
kubectl -n agentos exec $POD -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8081/status').read().decode())"

# 重新部署（改 yaml 后）
kubectl apply -f /home/extvdiadmin/agentteams-deploy/deployment-cimicode-bridge-105.yaml

# 测试收侧：Element Web (http://10.254.254.105:32397) 用 admin 登录，群里发：
# @cimicode-agent:agentteams-synapse.agentos.svc.cluster.local 你好
# 日志会出现 mention 判定；gateway 未就绪时 chat 报连接失败属预期
```

### 3.3 gateway 就绪后的切换

只需更新 MinIO 中 `openclaw.json` 的 bridge 段（真实 gateway 地址 + 真实 session 绑定），然后重启 bridge：

```bash
kubectl -n agentos rollout restart deployment/cimicode-bridge
```
