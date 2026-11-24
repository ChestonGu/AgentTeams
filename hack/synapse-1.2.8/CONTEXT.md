# Synapse 改造 — 完成状态 / 交接（2026-11-24 12:50 UTC）

> **状态：已完成并上线验证。** 105 上 AgentTeams v1.2.8 已从 Tuwunel 切换到 Synapse v1.127.0，
> 数据库为 105 宿主机 PostgreSQL 16。端到端验证：admin 经 gateway 在 Manager DM 发消息，
> Manager 回复 "OK"；Element / Dashboard / Matrix gateway 均正常。
>
> 运行手册与实际切换记录见同目录 [README.md](README.md)（§8 是本次 as-built 记录）。
> 明文账号口令**不写在仓库**：见桌面 `agentos-teams信息汇总.txt` 与 `render/ACCOUNTS.local.md`（gitignored）。

## 1. 仓库与分支

| 项 | 值 |
|----|----|
| fork 仓库 | `D:\workbach\fork\AgentTeams` |
| 分支 | `feat/v1.2.8-synapse`（基于 upstream tag `v1.2.8`，`dev-v1.2.3` 未动） |
| 远端 | origin = ChestonGu/AgentTeams |
| 构建 | `GOWORK=off`；Windows 本地无法 `go build ./internal/...`（kine/sqlite 需要 CGO，改在 105 的 golang 容器里构建）；`internal/matrix`、`internal/config`、`internal/service` 等包可本地 build/test |

提交（按时间）：

```
235e069e feat(matrix): add Synapse provider support to v1.2.8 controller
f7090c39 feat(helm): allow matrix.provider=synapse with an existing homeserver
<next>   fix(matrix): converge equal-power kicks + repair matrix route on homeserver switch
<next>   fix(helm): add Degraded to Human phase enum
<next>   docs(synapse): add reference Synapse deployment manifests, scripts and runbook
```

## 2. 代码改动一览（v1.2.8 → Synapse）

| 位置 | 改动 |
|------|------|
| `internal/matrix/client.go` | 抽出 provider 中立的 `matrixClient`（全部 CS API/登录缓存/AS 用户流程）；`TuwunelClient` 变为薄封装，保留 admin bot 专属方法（EnsureUser 注册 token 流程、SetPasswordAsAdmin、AdminCommand、AppService 注册） |
| `internal/matrix/provider.go` | `NewClient(provider, cfg, httpClient)` 工厂：`""`/`tuwunel` → TuwunelClient；`synapse` → SynapseClient；其它报错 |
| `internal/matrix/synapse_client.go` | `SynapseClient`：EnsureUser（admin PUT /_synapse/admin/v2/users + login）、SetPassword/ResetPassword（admin REST）、AdminCommand 显式报错、声明式 AppService（verify-only/不可注销）、CreateRoom 修正（creator 注入 PL、从 invite 去掉 creator）、踢人/invite/state/系统消息的发送方权限恢复；`DeleteRoom`/`ForceLeaveRoom`/`DeactivateUser`（ProviderAdminOps） |
| `internal/matrix/synapse_admin.go` | Synapse admin REST 原语（synAdminCall/reset_password/make_room_admin/force-join）、失败分类与恢复、`synDemoteAndKick`（等权目标先降 PL 再踢，失败回滚 PL）、JSON 数字归一化 |
| `internal/matrix/provider_admin.go` | `ProviderAdminOps` 接口 + Tuwunel admin-bot 实现 |
| `internal/config/config.go` | `MatrixProvider` 字段 + `AGENTTEAMS_MATRIX_PROVIDER` 解析/校验 + `UsesSynapse()`；`MatrixConfig()` 传 Provider |
| `internal/app/app.go` | `matrix.NewClient(...)` 工厂 |
| `internal/server/appservice_mgmt_handler.go` | synapse 下 rotate-token 返回 501（声明式注册，无法运行时轮换） |
| `internal/service/provisioner.go` | `deleteRoom` 走 `ProviderAdminOps.DeleteRoom` |
| `internal/service/provisioner_human.go` | `ForceLeaveRoom`/`DeactivateHumanUser` 走 `ProviderAdminOps`；等权目标兜底用目标本人 token self-leave；`matrixTokenForUserID` 解析凭证 |
| `internal/initializer/initializer.go` | `matrix-homeserver` 路由改为 `EnsureManagedRoute`（切换 homeserver 时修复 upstream 端口） |
| `helm/agentteams/templates/00-validate.yaml` | 允许 `matrix.provider=synapse` + `mode=existing`（要求 internalURL/serverName） |
| `helm/agentteams/templates/controller/deployment.yaml` | 注入 `AGENTTEAMS_MATRIX_PROVIDER` |
| `helm/agentteams/values.yaml` | 文档化 synapse/existing 契约 |
| `helm/agentteams/crds/humans…` + `config/crd/humans…` | Human phase enum 补 `Degraded`（v1.2.8 上游遗漏） |
| `internal/matrix/synapse_test.go` | 新增单测：工厂选择、EnsureUser admin API、CreateRoom creator 修正、AdminCommand 守卫、声明式 AppService、错误分类、PL 数字归一化 |

## 3. 105 部署现状（as-built）

- Helm `agentteams-128` rev **9**，namespace `agentos`。
- Controller 镜像：`agentteams/agentteams-controller:v1.2.8-synapse.4`（105 docker 与两节点 k3s containerd 均已导入）。
- Synapse：`ghcr.io/element-hq/synapse:v1.127.0`，pod `agentteams-128-synapse-0`；
  Service `agentteams-128-synapse:8008`；server_name `agentteams-128-synapse.agentos.svc.cluster.local`。
- 配置：Secret `agentteams-synapse-config`（homeserver.yaml/log_config）、
  `agentteams-synapse-appservice`（declarative AS registration）、`agentteams-synapse-secrets`（长期密钥）。
- DB：105 宿主机 PG16 `synapse` 库 / `agi-synapse` 用户（口令不入库）。
- 限流已放宽（rc_message/rc_joins/rc_invites 等 1000/s）以避免 controller 批量操作 429。
- CR：Manager default Running、Worker reporter/worker-01 Running、Team team-report Active、
  Human admin/cheston Active，全部 Matrix ID 已在新 server_name 下。
- 旧 `data-agentteams-128-tuwunel-0` PVC 保留（回滚用，数据未迁移）。
- k3s-103 磁盘曾 89% 触发镜像 GC（自建 controller 镜像被清）；已清理到 75%，
  后续导镜像务必两节点 `crictl images` 确认。

## 4. 复现/运维入口

```bash
# 105 上
cd ~/hack/synapse-1.2.8
PG_PASSWORD='<PG_PASSWORD>' ./scripts/deploy-synapse.sh   # 幂等：部署/更新 Synapse + AS 注册
./scripts/verify-synapse.sh                                # CS API/admin REST/AS 三连验证

# 切 controller（overlay 在 hack/synapse-1.2.8/values-synapse-agentos.yaml）
helm upgrade agentteams-128 ~/deploy-128/src/helm/agentteams -n agentos \
  -f ~/deploy-128/values-128-agentos.yaml \
  -f ~/hack/synapse-1.2.8/values-synapse-agentos.yaml \
  --set controller.image.repository=agentteams/agentteams-controller \
  --set controller.image.tag=v1.2.8-synapse.4 \
  --set controller.image.pullPolicy=IfNotPresent \
  --wait --timeout 10m

# CRD（helm upgrade 不自动升级 CRD）
kubectl apply -f ~/deploy-128/src/helm/agentteams/crds/
```

## 5. 可选后续（未做，不阻塞当前状态）

1. **切换时强制重 provision 的自动化**：本次是人工清 CR status + 删 `agentteams-creds-*` Secret。
   可在 controller 里加“homeserver/domain 变化 → 重置 member status 并重新 provision”的迁移逻辑
   （v1.2.8 的 reconcile 以 `status.matrixUserID != ""` 为已就绪，不会自动迁移）。
2. **Synapse 侧的房间删除/踢人补强**：等权 PL 已用 self-leave 兜底；如需彻底在 homeserver 侧解决，
   可评估 Synapse admin API 或 make target 退出后清 PL。
3. 向上游提 PR（`Client` 接口注释已预留 SynapseClient）；提交 `hack/synapse-1.2.8` 清单 + 本 CONTEXT。
4. 观察期结束后清理旧 Tuwunel PVC / Synapse 不再需要时的回滚演练。
5. 旧 controller 镜像 `.1`~`.3` 可从 105 docker/containerd 清理。
