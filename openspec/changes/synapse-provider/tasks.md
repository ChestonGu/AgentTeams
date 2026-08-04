# Synapse Provider Support Tasks

> Change: `synapse-provider`
> 状态：Tasks
> 前置：`proposal.md`、`design.md`、delta spec `specs/matrix-provider/spec.md`
> 吸收来源：`openspec/changes/support-synapse-homeserver`（组 7-11：Helm/Manager/安装器/镜像/文档/验证）

每个任务标注验收映射（`REQ-MP-x 场景` / 愿景 §DoD），完成后勾选。
组 1-6（controller Go 工作）与组 7-8（chart + Manager 工作）在 `AGENTTEAMS_MATRIX_PROVIDER` 环境变量契约确定后相互独立；组 9-11 依赖两者。

## 1. Controller：提供商配置接线（D1）

- [ ] `internal/config/config.go`：`Config` 新增 `MatrixProvider string`，从 `AGENTTEAMS_MATRIX_PROVIDER` 加载，默认 `"tuwunel"`；无效值启动即失败（panic，带清晰信息）——`REQ-MP-8` 无效提供商场景
- [ ] `AGENTTEAMS_MATRIX_PROVIDER` 加入 `WorkerEnvDefaults`，并在 `ManagerAgentEnv()` / `WorkerEnv` 传播（Manager/Worker 容器可分支）——`REQ-MP-14`
- [ ] `internal/matrix/client.go`：新增工厂 `matrix.NewClient(provider string, cfg Config, http *http.Client) Client`，返回 `*TuwunelClient` 或 `*SynapseClient`；保留 `NewTuwunelClient` 作为现有约 25 个调用点的薄包装——`REQ-MP-8`
- [ ] 生产环境 `NewTuwunelClient` 构建点（app 引导 / initializer）迁移到 `matrix.NewClient(cfg.MatrixProvider, ...)`

## 2. Controller：契约冻结（B P0）

- [ ] `internal/matrix/client.go`：将 `SyncMessages` 从 `Client` 接口移除（含接口定义 :1005 区域）——`REQ-MP-4`
- [ ] 同步清理 `SyncMessages` 的 mock 与测试引用（`test/testutil/mocks/`、`client_test.go`）
- [ ] `internal/matrix/client.go`：`CreateRoom` 去除 admin token 兜底（:485-492），无 `CreatorToken` 时返回明确错误——`REQ-MP-3`
- [ ] `internal/service/provisioner.go`：:412 移除 `adminMatrixID` invite（worker 房间不再邀请 admin）——`REQ-MP-7`
- [ ] 验收：`go build ./...` + 单测绿

## 3. Controller：SynapseClient + AdminOps（B P1 + D2）

- [ ] 抽取两个客户端共用的 CS-API 基础设施（doJSON、令牌缓存、whoami、别名/房间/成员/消息辅助函数）到公共基类（行为不变）——`D2`
- [ ] 新建 `internal/matrix/synapse_client.go`：`SynapseClient` 满足 `matrix.Client`（注册/登录走 `m.login.registration_token`、AS 注册/登录、CreateRoom/别名/join/leave/SetRoomName/SetRoomState/SendMessage/ListRoomMembers——标准 CS API；`InviteToRoom` 以创建者身份 invite + 目标身份 join 两步合一自动进房、`ForceLeaveRoom` 以创建者身份走 C-S API kick，见组 5）——`REQ-MP-12`/`REQ-MP-10`
- [ ] 用户端点：`GetUser`（GET v2/users）、`UpdateUser`（PUT v2/users：password/logout_devices/deactivated）、`DeactivateUser`（POST v1/deactivate，erase:false）——`REQ-MP-10`（**不含 RegisterUser——注册走 registration_token，零分支**）
- [ ] 房间端点：`GetRoom`（GET v1/rooms/{id}，含顶层 creator 字段）、`DeleteRoom` + `GetDeleteStatus`（DELETE v2/rooms 异步 → delete_id + 轮询 `GET v2/rooms/delete_status/{delete_id}`：TaskStatus 枚举 `scheduled|active|complete|failed`，响应仅 delete_id/status/shutdown_room）、`ListRoomMembers`（GET v1/rooms/{id}/members）——`REQ-MP-10`（**无 SetRoomMembership**：Synapse 无 admin invite/kick 端点，房间内成员操作以创建者身份走 C-S API，见组 5）
- [ ] `SetPasswordAsAdmin` 实现：`UpdateUser`（password + logout_devices）——`REQ-MP-10`
- [ ] 重做 `EnsureUser` 孤儿恢复：Synapse 路径 `GetUser` 查 deactivated → `UpdateUser` 设 deactivated:false + 新密码 → 重试登录；Tuwunel 行为不变——`REQ-MP-2` 孤儿恢复场景
- [ ] `DeactivateHumanUser` 实现：`DeactivateUser`（erase:false）——SSO Human 删除路径依赖（externalsso/source.go:108）——`REQ-MP-2`
- [ ] `AdminCommand` 实现：返回明确"Synapse 不支持 admin bot"错误——`REQ-MP-2`
- [ ] 错误语义实现：`AdminAPIError`（Status/Endpoint/Body）、429 指数退避（上限 5 次）、幂等处理——design 错误语义表
- [ ] 单测（`synapse_client_test.go`，httptest mock Synapse）：registration-token 注册、M_USER_IN_USE 登录回退、孤儿恢复、AS 冒烟路径、直接 `AdminCommand` 报错、各端点请求/响应/幂等/429——`REQ-MP-10` 全部场景

## 4. Controller：AppService 注册提供商分支（D3）

- [ ] `internal/matrix/appservice.go`：`RegisterAppService`/`UnregisterAppService` 按提供商分支——Tuwunel 保留 admin-bot 流程；Synapse 返回 nil（配置文件注册，`AppServiceSmokeTest` 仍执行）——`REQ-MP-6`
- [ ] 验证 `RenderAppServiceRegistration` 对两个提供商输出一致（与 Helm 将挂载的 YAML 形状对齐，新增测试）——`REQ-MP-13`
- [ ] 审计所有 `AdminCommand` 调用方（`appservice.go`、`client.go`）只通过映射到提供商的接口方法调用；新增测试断言 Synapse 上无生产路径直接调用 `AdminCommand`

## 5. Controller：创建者机制（B P2）

- [ ] 新建 `internal/matrix/room_creator.go`：`RoomCreator`（AdminOps + AS 代登录 + `roomID→creator` 缓存）——`REQ-MP-9`
- [ ] `ResolveCreator`：Room Details API 顶层 `creator` 字段优先；兜底 alias 解析 + 成员列表最高 power 的 AgentTeams 用户
- [ ] `ActAsCreator`：`LoginAppServiceUser`（无密码）→ 回调注入 token
- [ ] `InviteToRoom`/`ForceLeaveRoom`：经 `ActAsCreator` 以创建者身份调用 C-S API `POST /_matrix/client/v3/rooms/{id}/invite` 与 `/kick`；`InviteToRoom` 随后以目标身份（目标自有 token 或 AS 代登录）`POST /_matrix/client/v3/rooms/{id}/join` 自动进房——`REQ-MP-10` 邀请/踢出场景（Matrix invite 默认需目标手动 join；controller 代为 join 实现"邀请后自动进房"，与 Tuwunel 路径 `provisioner.go:492` 一致）
- [ ] `internal/matrix/client.go`：`SendMessageAsAdmin` 改为创建者身份执行（探测 → 代登录 → `SendMessage`）——`REQ-MP-4` 欢迎语场景
- [ ] 集成测试（模拟 Synapse）：创建者探测成功/失败兜底两条路径——`REQ-MP-9` 全部场景

## 6. Controller：启动分支 + AS 校验（B P3/P4 合并）

- [ ] `internal/app/app.go`：`newMatrixClient` 按 `MatrixProvider` 分支（synapse → `NewSynapseClient` + `VerifyAppService` fail-fast；default → `NewTuwunelClient` 现状）——`REQ-MP-8`
- [ ] `internal/matrix/appservice.go`：`AppServiceSmokeTest` 语义从"等待异步注册"改为"校验部署正确性"——通过则继续（永不重试）；失败则 fail-fast 报错并退出（不重试循环）——`REQ-MP-11`
- [ ] `internal/matrix/appservice_config.go`：`EnsureTokens()` 语义收紧——仅首次初始化生成并写入 Secret，之后读取既有值——`REQ-MP-11`
- [ ] 启动时序测试：as_token 漂移场景 → controller fail-fast 退出——`REQ-MP-11` 漂移场景
- [ ] 验收：`REQ-MP-11` 三个场景（一致→启动成功 / 漂移→fail-fast / 显式轮换）

## 7. Helm chart：synapse 提供商（A 4.x + D6）

- [ ] `helm/agentteams/values.yaml`：用完整 values 替换空 `matrix.synapse: {}`：`image`（repo/tag/pullPolicy，默认同步镜像）、`replicaCount`、`resources`、`service.type/port`（8008）、`persistence`（enabled/size/storageClassName/mountPath `/data`）、`registrationSharedSecret`、`extraEnv`——`REQ-MP-13`
- [ ] `00-validate.yaml`：接受 `matrix.provider` 为 `tuwunel` 或 `synapse` 且 `matrix.mode=managed`；失败信息列出受支持提供商——`REQ-MP-8`/`REQ-MP-13` 拒绝未知场景
- [ ] `templates/_helpers.tpl`：新增 `agentteams.synapse.fullname`/`clusterFQDN`/`internalURL`/`serverName` 辅助函数；`_helpers.infra.tpl` 的 `agentteams.matrix.internalURL`/`serverName` 按提供商分支（synapse 客户端端口 8008）——`REQ-MP-13`
- [ ] 新增 `templates/matrix/synapse-statefulset.yaml`（镜像、8008、环境变量：服务器名/注册共享密钥/AS 令牌、探针）+ `synapse-service.yaml`（ClusterIP 8008），均以 `eq .Values.matrix.provider "synapse"` 保护——`REQ-MP-13` 渲染场景
- [ ] 新增 `templates/matrix/synapse-homeserver-config.yaml` ConfigMap：渲染 `homeserver.yaml`（server_name、8008 client 监听器、enable_registration:true、registration_requires_token:true、registration_shared_secret、媒体路径、`app_service_config_files: [/data/synapse/appservices/agentteams-controller.yaml]`）——`REQ-MP-12`
- [ ] 新增 `templates/matrix/synapse-appservice-registration.yaml` ConfigMap：从 runtime Secret 的 asToken/hsToken（+ push URL）渲染 AS 注册 YAML，挂载到 StatefulSet 的 `app_service_config_files` 路径——`REQ-MP-11`
- [ ] 新增 `templates/matrix/synapse-bootstrap.yaml` ConfigMap：含 `bootstrap-synapse.sh`（D4：签名密钥 → `register_new_matrix_user` 创建 admin → `registration_tokens/new` 创建注册令牌 → exec synapse），接线为 StatefulSet 容器 `command`——`REQ-MP-12` 引导场景
- [ ] `templates/controller/deployment.yaml`：新增 `AGENTTEAMS_MATRIX_PROVIDER` 环境变量（值 `.Values.matrix.provider`）；`templates/secrets/runtime-env.yaml` 传播给 Manager/Workers——`REQ-MP-14`
- [ ] 渲染检查：`helm template` 在 `matrix.provider=synapse` 下产出 synapse 资源且无 tuwunel；`tuwunel` 下与现状一致（记录为 Makefile/CI 检查）——`REQ-MP-13` 双渲染场景
- [ ] `matrix.synapse` 块 values 文档（README 表格 / values 注释）——`D7`

## 8. Manager 容器：按提供商感知启动引导（A 5.x + D5）

- [ ] `manager/scripts/init/start-manager-agent.sh`：就绪探测按提供商感知——双提供商主探测 `GET /_matrix/client/versions`；`/_tuwunel/server_version` 仅 `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` 时作为附加检查——`REQ-MP-14` 就绪场景
- [ ] 同一脚本 k8s 模式：从 `AGENTTEAMS_MATRIX_PROVIDER` 派生 Higress service-source 与 `/_matrix` 路由目标（`tuwunel.dns` vs `synapse.dns`）——`REQ-MP-14` Higress 场景
- [ ] 新增 `manager/scripts/init/start-synapse.sh`（嵌入式）：从环境渲染 `homeserver.yaml`（server_name、签名密钥、shared_secret、注册令牌、媒体路径）+ AS 注册 YAML，执行 D4 引导，`exec python -m synapse.app.homeserver`——`REQ-MP-12`/`REQ-MP-15`
- [ ] 新增 `manager/scripts/init/start-matrix.sh` 分发器（按 `AGENTTEAMS_MATRIX_PROVIDER` exec `start-tuwunel.sh`/`start-synapse.sh`）；`manager/supervisord.conf` 的 `[program:matrix]` 指向它；验证嵌入式 Tuwunel 启动不变——`REQ-MP-14`
- [ ] `start-synapse.sh` + 分发器烤进 manager 镜像（Dockerfile / 嵌入式 controller 镜像拷贝）；`manager/tests/smoke-test.sh` 在分发器下通过

## 9. 安装器与本地安装（A 6.x）

- [ ] `install/agentteams-install.sh`（及 `.ps1`）：`wait_matrix_ready` 切换为探测 `/_matrix/client/versions`（双提供商可用），tuwunel 提供商时保留 Tuwunel 特定探测——`REQ-MP-15` 健康检查场景
- [ ] 新增安装器提供商选择（默认 `tuwunel`）；把 `AGENTTEAMS_MATRIX_PROVIDER` 及 Synapse 环境变量（server_name、registrationSharedSecret、AS 令牌）传入嵌入式 controller 容器——`REQ-MP-15`
- [ ] 验证嵌入式 all-in-one：`tuwunel` 安装不变；记录/接线标志以 `synapse` 安装会启动 `start-synapse.sh` 并完成 Manager 注册 + 建房间——`REQ-MP-15` 嵌入式场景

## 10. 镜像、文档、技能、changelog（A 7.x + D7）

- [ ] `hack/mirror-images.sh`：新增固定版本 `matrixdotorg/synapse` 镜像映射（`|synapse|`，**固定 `v1.127.1`——CVE-2025-30355 修复版，禁止 `v1.127.0`**）；更新嵌入式镜像清单
- [ ] `manager/agent/skills/matrix-server-management/SKILL.md`：新增提供商感知小节——Tuwunel admin-bot 命令 vs Synapse admin API，以 `AGENTTEAMS_MATRIX_PROVIDER` 为条件；明确禁止对 Synapse 使用 `!admin ...`——`REQ-MP-14`/`REQ-MP-15` 文档场景
- [ ] 更新文档：README/README.zh-CN（Helm 小节）、docs/quickstart.md、docs/architecture.md（matrix 提供商表格），注明切换提供商需全新安装——Migration Plan
- [ ] `tests/lib/test-helpers.sh`、`tests/test-01-manager-boot.sh` 探测更新为 `/_matrix/client/versions`（双提供商通过）
- [ ] `changelog/current.md` 按仓库政策新增条目（每个逻辑变更一条，带关联提交哈希）

## 11. 端到端验证 + 收尾（B P5 + A 8.x）

- [ ] `go build ./...` + `go test ./internal/matrix/...` 全绿
- [ ] 双提供商 `helm template` 渲染检查 + `helm lint` 干净
- [ ] `start-matrix.sh`、`start-synapse.sh`、修改后的 `start-manager-agent.sh`、安装器通过 ShellCheck/`bash -n`
- [ ] E2E 冒烟（`matrix.provider=synapse` 全新安装）：controller 协调 Manager CR、Manager 完成注册、admin 用户登录 Element、Worker 创建/删除的房间清理可用
- [ ] E2E 断言：全局 admin 不在任何 AgentTeams 房间，且全生命周期操作成功（worker/team/human：创建、加房、消息、删除、停用）——`REQ-MP-7` 场景
- [ ] 回归（默认 `tuwunel` 全新安装）：通过同样 E2E 冒烟
- [ ] 对照愿景 §2.1 功能点清单（28 项）逐项确认绿
- [ ] 文档收尾：愿景/设计/OpenSpec 与实现同步
- [ ] 验收：愿景 §8 DoD 全项通过

## 依赖关系

```
组1(配置接线) ──┬──> 组6(启动分支) ──> 组11(端到端)
组2(契约冻结) ──┼──> 组3(SynapseClient) ──> 组4(AS分支) ──┘
组5(创建者)  ──┘
组7(Helm)  ──┬──> 组11
组8(Manager) ─┴──> 组9(安装器) ──> 组10(镜像/文档)
```
