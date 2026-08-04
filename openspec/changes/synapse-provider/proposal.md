# Synapse Provider Support Proposal

> Capability: `matrix-provider`
> 对应设计文档：`docs/design/synapse-support-vision.md`
> 状态：Proposal / 提案
> 吸收来源：`openspec/changes/support-synapse-homeserver`（全栈交付面、启动引导、registration_token 零分支）

## Why

AgentTeams 的 Matrix 层当前与 Tuwunel（conduwuit fork）强绑定，存在三层问题：

1. **Tuwunel admin bot 是唯一管理通道**——`!admin` 命令承载用户生命周期、房间删除、AS 动态注册，生态小众、运维依赖 bot 回执。
2. **全局 admin 驻留每个房间**——worker 房间由 admin token 兜底创建并保留为成员（`provisioner.go:412`），D 类房间内操作（invite/kick/listMembers/sendAsAdmin）以 admin token 执行。这是安全与最小权限的隐患。
3. **无提供者抽象**——`app.go:288` 无条件 `NewTuwunelClient`；Helm 校验只放行 `provider=tuwunel`。

目标：把 Matrix 提供者层演进为**提供者无关（provider-agnostic）**，并将 **Synapse 作为第一公民**端到端交付——Helm 部署、controller 客户端、Manager 启动引导、嵌入式安装器全部支持双提供商；通过组合标准 Client-Server API、Application Service 代登录、Synapse Admin REST API 三类接口，在 **"全局 admin 不驻留任何房间"** 的前提下，覆盖当前 Tuwunel admin bot 的全部能力。

一句话：**"房间里的活交给房间创建者干，房间外的活交给服务端 Admin API 干，admin 本人永不进房。"**

## What Changes

### Capability 变更（delta：`specs/matrix-provider/spec.md`）

- **ADDED**：提供者运行时选择（`matrix.provider=tuwunel | synapse`），按 provider 分支实例化客户端；Helm 端到端渲染 Synapse StatefulSet/Service/配置（受管部署）。
- **ADDED**：创建者身份机制——探测房间创建者（Room Details API 顶层 `creator` 字段，兜底最高 power 成员）→ AS 代登录创建者（无密码）→ 以创建者 token 执行房间内操作。
- **ADDED**：服务端 Admin API 操作层（`SynapseClient` + `AdminOps` 接口）——用户生命周期、房间删除（异步 purge + 轮询）、成员列表端点。
- **ADDED**：AS 静态注册机制——Helm 部署期渲染 AS 注册 YAML（与 Tuwunel 共用 runtime Secret 的 as_token/hs_token）+ 挂载进 `app_service_config_files` + controller 启动 fail-fast 校验（无状态机）。
- **ADDED**：Synapse 启动引导（鸡生蛋解决）——bootstrap 预创建 admin 用户与注册令牌；注册流程保持 `m.login.registration_token`，与 Tuwunel **逐字节一致、零分支**。
- **ADDED**：Manager 容器按提供商感知（就绪检查、Higress 路由、`start-matrix.sh` 分发器）。
- **ADDED**：嵌入式安装器双提供商支持、镜像同步、文档与技能更新。
- **MODIFIED**：用户生命周期（改密/孤儿恢复/停用）从 bot 命令迁移到 Synapse Admin API；注册段保持 registration_token 零分支。
- **MODIFIED**：房间删除迁移到 Synapse Admin API（purge + delete_status 轮询）；强制离房/邀请以房间创建者身份走 C-S API（Synapse 无 admin invite/kick 端点，创建者 power=100 可踢任意普通成员）；邀请为两步合一——创建者 invite + controller 以目标身份 join（Matrix invite 默认需目标手动接受，controller 代为 join 实现"邀请后自动进房"）。
- **MODIFIED**：`SendMessageAsAdmin` 以创建者身份执行（Synapse 无"以任意身份发消息"端点）。
- **MODIFIED**：`CreateRoom` 去除 admin token 兜底，worker 房间由 AS 登录 worker 创建。
- **REMOVED**：`SyncMessages`（1.2.0 无生产调用方，仅接口+mock）。
- **MODIFIED**：`REQ-MP-7` 隐藏前提消除——全局 admin 不再驻留任何 AgentTeams 房间。

### 不改（Non-Goals）

- **不删除 Tuwunel 路径**：`TuwunelClient` 保留为第二实现，行为与 1.2.0 完全一致（无回归）。
- **不做动态 AS 注册**：Synapse 无此 API；注册为部署期静态配置（Helm 渲染 + 挂载）。
- **不做任意身份发消息的 Admin 端点**（Synapse 不存在）；仅通过创建者身份实现等价能力。
- **不引入第三方 Matrix SDK**：继续使用自研轻量 HTTP 客户端。
- **`matrix.mode=existing` + 外部 Synapse**：chart 对两个提供商都只支持 managed；外部服务器支持（对活服务器引导注册令牌/admin、收紧用户命名空间正则）为后续工作。
- **不做 Tuwunel→Synapse 在线数据迁移**：切换提供商需要全新安装。
- **不做 Synapse Postgres 后端**：managed/嵌入式默认 SQLite；Postgres 调优交给运维。

## Impact

| 影响面 | 说明 |
|---|---|
| `internal/matrix/` | 新增 `SynapseClient`、`synapse_client.go`、`room_creator.go`；`appservice.go` 注册语义改造；`SyncMessages` 移出 `Client` 接口 |
| `internal/app/app.go` | `NewTuwunelClient`（:288）改为按 `MatrixProvider` 分支的工厂（保留薄包装给测试） |
| `internal/config/config.go` | 新增 `MatrixProvider`（`AGENTTEAMS_MATRIX_PROVIDER`，默认 `tuwunel`，无效值启动失败） |
| `internal/service/provisioner.go` | worker 房间创建改为 AS 创建者 token；:412 去 admin invite；团队房间逻辑保留 |
| `internal/service/provisioner_human.go` | `ForceLeaveRoom` 以创建者身份走 C-S API kick；`DeactivateHumanUser` 走 Admin API（SSO 删除依赖 deactivate，保留） |
| `helm/agentteams/` | values `matrix.synapse.*` 补全；`00-validate.yaml` 放行 synapse；新增 `synapse-statefulset/service/homeserver-config/appservice-registration/bootstrap` 模板；controller env 加 `AGENTTEAMS_MATRIX_PROVIDER` |
| `manager/` | `start-synapse.sh`、`start-matrix.sh` 分发器、supervisord 接线、就绪检查/Higress 路由按提供商分支 |
| `install/` | 安装器提供商选择 + 健康检查按提供商感知 |
| `hack/mirror-images.sh` | Synapse 镜像同步 |
| 文档/技能/测试 | README/quickstart/architecture、`matrix-server-management` 技能、`tests/lib`、changelog |

## Capabilities Referenced

- `matrix-provider`（本 change 修改）
