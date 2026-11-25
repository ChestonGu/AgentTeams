# MinIO 访问入口（管理控制台 + 只读数据查看器）

本目录提供 105 `agentos` 环境 MinIO 的**外部访问入口**（chart 内的 MinIO Service 是
headless ClusterIP，默认只能在集群内访问）。

| 用途 | 地址 | 说明 |
|------|------|------|
| MinIO 管理控制台（浏览器） | http://10.254.254.105:32001 | 官方 Console，可浏览/上传/下载/删除对象、管理 bucket/用户/策略 |
| MinIO S3 API（浏览器/工具） | http://10.254.254.105:32000 | S3 兼容端点，供桌面查看器或 mc/SDK 使用 |
| 集群内 S3 API | `agentteams-128-minio.agentos.svc.cluster.local:9000` | AgentTeams 组件使用 |
| 集群内 Console | `agentteams-128-minio.agentos.svc.cluster.local:9001` | — |

- Bucket：`agentteams-storage`
- 账号：root 用户 `agentteams128`，密码见桌面 `agentos-teams信息汇总.txt` / 本地运行手册（不入库）
- 这两个 NodePort 由本目录的 **独立 Service** 提供，不在 Helm release 内：
  `helm upgrade/uninstall` 不会删除它；重装集群后重新 `kubectl apply` 即可。

## 1. 部署外部入口 Service

```bash
# 105 上（仓库 hack/minio/ 已同步到 ~/deploy-128/ 或直接上传）
kubectl apply -f minio-external-service.yaml
kubectl -n agentos get svc agentteams-minio-external
curl -s -o /dev/null -w 'console:%{http_code}\n' http://127.0.0.1:32001/
curl -s -o /dev/null -w 'api:%{http_code}\n'     http://127.0.0.1:32000/minio/health/live
```

Service 内容：NodePort `32000 → api(9000)`、`32001 → console(9001)`，selector 指向
chart 创建的 `agentteams-128-minio-0` Pod（`app.kubernetes.io/component=minio` 等标签）。
NodePort 若与现网冲突，改 YAML 后重新 apply。

## 2. 桌面只读查看器（minio-viewer.html）

`minio-viewer.html` 是一个**单文件、无后端、无第三方依赖**的静态页面：

- 用浏览器 WebCrypto 在本地实现 S3 SigV4 签名，直接调用 `http://10.254.254.105:32000`；
- 功能：目录浏览、搜索过滤、自动刷新、大小/时间、预签名下载（1h 有效）、
  文本/JSON/图片在线预览；
- 配置（Endpoint / AccessKey / SecretKey / Bucket、自动刷新）保存在浏览器 localStorage；
- 已预填本环境默认值，打开即用；需要上传/删除等写操作请用 MinIO 管理控制台。

部署/使用：

1. 把 `minio-viewer.html` 复制到桌面（本环境桌面已有 `MinIO数据查看器.html`）。
2. 双击用 Chrome/Edge 打开，点“连接 / 刷新”。
3. 若浏览器拦截，检查 `http://10.254.254.105:32000` 可达，以及 MinIO CORS 配置
   （本环境已验证：`mc admin config get local api` → `cors_allow_origin=*`，
   预检响应 `Access-Control-Allow-Origin: null` 且允许 `authorization` 头）。

> 安全提示：NodePort 暴露到 10.254.254.0/24 内网，且使用 root 账号访问；仅适用于
> 本内网开发环境。若需收敛，可改为只暴露 Console、或换成只读子账号 + bucket policy。

## 3. 快速自检（不打开浏览器）

```bash
# Console 是否可达
curl -sI http://10.254.254.105:32001/ | head -1
# S3 API 是否可达（匿名返回 403 即说明服务正常）
curl -s -o /dev/null -w '%{http_code}\n' 'http://10.254.254.105:32000/agentteams-storage?list-type=2'
# CORS 预检（模拟 file:// 页面的 Origin: null）
curl -s -i -X OPTIONS 'http://10.254.254.105:32000/agentteams-storage?list-type=2' \
  -H 'Origin: null' -H 'Access-Control-Request-Method: GET' \
  -H 'Access-Control-Request-Headers: authorization,x-amz-date,x-amz-content-sha256' | head -6
```

查看器页面本身已用 jsdom + 真实 MinIO 做过 DOM 级回归（连接、目录列出、进入目录、渲染行）。
