# Matrix Provider Specification（Delta）

> Change: `synapse-provider`
> 状态：Delta（相对于 `openspec/specs/matrix-provider/spec.md`）
> 应用后：本文件描述目标行为；与现状 spec 合并即为应用后真相。
> 吸收来源：`openspec/changes/support-synapse-homeserver`（REQ-MP-12/13/14/15 启动引导与部署面）

## Purpose

将 Matrix 提供者层从"Tuwunel 专用"演进为"提供者无关"，Synapse 作为第一公民支持。
核心约束：**全局 admin 不驻留任何 AgentTeams 房间**；房间内操作以房间创建者身份执行，
服务端操作走 Synapse Admin API；AS 注册为部署期静态配置（Helm 渲染 + fail-fast 校验，二态模型）；
用户注册保持 `m.login.registration_token`，与 Tuwunel **逐字节一致、零分支**。

## ADDED Requirements

### REQ-MP-8: 提供者运行时选择

系统 SHALL 支持 `matrix.provider=tuwunel | synapse`，并在 `internal/app/app.go` 按 provider
分支实例化 `TuwunelClient` 或 `SynapseClient`。controller 的 `Config` SHALL 从
`AGENTTEAMS_MATRIX_PROVIDER` 加载 `MatrixProvider`（默认 `tuwunel`），无效值 SHALL 启动即失败
（不静默回退）。Helm 校验模板 SHALL 放行 `tuwunel` 与 `synapse`（均要求 `matrix.mode=managed`），
渲染时 SHALL 拒绝任何其他值；`values.yaml` SHALL 将 provider 枚举补全为 `tuwunel | synapse`。

#### Scenario: Synapse 模式启动
- **GIVEN** `matrix.provider=synapse`
- **WHEN** 控制器启动
- **THEN** 实例化 `SynapseClient` 并执行一次 AS 一致性校验（见 REQ-MP-11）
- **AND** 校验通过后继续正常运行

#### Scenario: Tuwunel 模式无回归
- **GIVEN** `matrix.provider=tuwunel`（或未设置）
- **WHEN** 控制器启动
- **THEN** 行为与 1.2.0 完全一致（`TuwunelClient` 不删不改）

#### Scenario: 无效提供商启动失败
- **GIVEN** `AGENTTEAMS_MATRIX_PROVIDER` 为未知值（如 `dendrite`）
- **WHEN** 控制器启动
- **THEN** 控制器退出并报错，指明无效的提供商，不静默回退

### REQ-MP-9: 创建者身份机制

系统 SHALL 支持以"房间创建者"身份执行房间内操作，机制为：探测创建者 → AS 代登录（`LoginAppServiceUser`，无密码）→ 以创建者 token 执行。
创建者探测 SHALL 首选 `GET /_synapse/admin/v1/rooms/{roomID}` 响应中顶层 `creator` 字段（Room Details API 直接返回，无需解析 `m.room.create` state）；
系统 SHALL 维护 `roomID → creator` 映射缓存。探测失败时 SHALL 兜底：解析房间别名 + 在成员列表中选择 power level 最高的 AgentTeams 受管用户。

#### Scenario: 创建者探测成功
- **GIVEN** 房间已存在且由 worker 创建
- **WHEN** 控制器需要以房间内身份执行操作（如发送欢迎语）
- **THEN** 通过 Room Details API 读取顶层 `creator` 字段得到创建者
- **AND** AS 代登录创建者并以该 token 执行操作，全程无密码、admin 不在房

#### Scenario: 创建者探测失败兜底
- **GIVEN** Room Details API 无法返回 `creator` 字段（房间已存在/别名复用）
- **WHEN** 控制器需要确定操作身份
- **THEN** 解析房间别名定位房间，再选成员列表中 power 最高的 AgentTeams 受管用户作为操作身份

### REQ-MP-10: 服务端 Admin API 操作层

系统 SHALL 通过 Synapse Admin API 执行服务端操作，端点如下（用户**注册**除外——注册走
`m.login.registration_token`，见 REQ-MP-12；**invite/kick 除外**——Synapse 无 admin invite/kick
端点，`/rooms/{roomID}/members` 仅有 GET，房间内成员操作以创建者身份走 C-S API，见 REQ-MP-9）：

| 功能点 | 端点 |
|---|---|
| 改密码（`SetPasswordAsAdmin`） | `PUT /_synapse/admin/v2/users/{userID}` `{"password": pw, "logout_devices": true}` |
| 孤儿恢复 | `GET /_synapse/admin/v2/users/{userID}` → 若 `deactivated` → `PUT` 设 `deactivated:false` + 新密码 |
| 停用（`DeactivateHumanUser`） | `POST /_synapse/admin/v1/deactivate/{userID}` `{"erase": false}` |
| 房间删除（`deleteRoom`） | `DELETE /_synapse/admin/v2/rooms/{roomID}` `{"block": false, "purge": true}`（异步，返回 delete_id）+ 轮询 `GET /_synapse/admin/v2/rooms/delete_status/{delete_id}`（状态机 `scheduled→active→complete`，失败为 `failed`——`TaskStatus` 枚举，见 v1.127.0 `synapse/types/__init__.py:1389`；任务保留 7 天后被清理，响应仅含 delete_id/status/shutdown_room，无 error 字段） |
| 强制离房（`ForceLeaveRoom`） | 创建者身份 `POST /_matrix/client/v3/rooms/{roomID}/kick`（REQ-MP-9 机制；**无 admin kick 端点**） |
| 邀请（`InviteToRoom`） | 创建者身份 `POST /_matrix/client/v3/rooms/{roomID}/invite` + controller 以目标身份 `POST /_matrix/client/v3/rooms/{roomID}/join`（两步合一：邀请后自动进房，目标无需手动接受；REQ-MP-9 机制；**无 admin invite 端点**） |
| 成员列表（`ListRoomMembers`） | `GET /_synapse/admin/v1/rooms/{roomID}/members`（服务端） |

#### Scenario: 邀请后自动进房（不依赖 admin 在房、不依赖目标手动接受）
- **GIVEN** admin 不在目标房间内
- **WHEN** 控制器需要邀请成员
- **THEN** 探测创建者并以创建者身份调用 `POST /_matrix/client/v3/rooms/{roomID}/invite`（AS 代登录，无密码）
- **AND** controller 以目标身份（目标自有 token 或 AS 代登录无密码）调用 `POST /_matrix/client/v3/rooms/{roomID}/join`——目标 membership 直接为 `join`，自动进房
- **AND** 创建者在房间内拥有邀请权限（创建者 power=100），admin 无需在房

#### Scenario: 踢出高 power 成员
- **GIVEN** 目标成员 power level 高于任意普通成员
- **WHEN** 控制器需要踢出该成员
- **THEN** 探测创建者并以创建者身份调用 `POST /_matrix/client/v3/rooms/{roomID}/kick`（创建者 power=100，高于任意普通成员）
- **AND** 顺带消除 `internal/service/interfaces.go:57-59` 注释中"高 power 用户普通 kick 踢不掉"的老限制

#### Scenario: 异步房间删除
- **GIVEN** 需要删除房间
- **WHEN** 控制器发起 `DELETE /_synapse/admin/v2/rooms/{roomID}`（purge）
- **THEN** 获得异步 delete_id，轮询 `GET /_synapse/admin/v2/rooms/delete_status/{delete_id}` 直至 `complete`
- **AND** 轮询遇 `failed` 或 404（任务已过 7 天保留期被清理）时以明确错误报告

### REQ-MP-11: AS 静态注册机制（P0 决策）

AS 注册 SHALL 为**部署期静态配置**：Helm SHALL 从 runtime Secret 中的 as_token/hs_token 渲染
AppService 注册 YAML（与 Tuwunel 共用同一 Secret 源），将其挂载进 Synapse 容器，并在生成的
`homeserver.yaml` 的 `app_service_config_files` 下列出。controller 在 Synapse 路径 SHALL **跳过**
`RegisterAppService`/`UnregisterAppService`（返回 nil），仅执行 `AppServiceSmokeTest`。
controller 启动 SHALL 执行**一次** `AppServiceSmokeTest`（语义从"等待异步注册"改为"校验部署正确性"）：
通过则继续（之后永不重试、永不失效）；失败则 fail-fast 明确报错并退出（不静默、不重试循环）。
`EnsureTokens()` SHALL 仅在首次初始化时生成 as_token 并写入 Secret，此后永不重新生成。
as_token 轮换 SHALL 为显式双端一致运维操作（更新 Secret → 重新渲染 chart → 重启 Synapse 与 controller）。

#### Scenario: 配置一致 → 启动成功
- **GIVEN** Helm 已从同一 Secret 渲染 AS 注册 YAML 与 homeserver.yaml（as_token 一致）
- **WHEN** controller 启动执行 `AppServiceSmokeTest`
- **THEN** 校验通过，controller 继续；此后永不重试、永不失效

#### Scenario: token 漂移 → fail-fast
- **GIVEN** controller Secret 被重建导致 `EnsureTokens()` 生成了新 as_token（与 homeserver.yaml 不一致）
- **WHEN** controller 启动执行 `AppServiceSmokeTest`
- **THEN** 校验失败，controller 明确报错并退出（提示检查 as_token 与 homeserver.yaml 一致性）

#### Scenario: 显式轮换
- **GIVEN** 运维需要轮换 as_token
- **WHEN** 运维更新 Secret 并重新渲染 chart（homeserver.yaml 与 AS 注册 YAML 同步更新），重启 Synapse 与 controller
- **THEN** 旧 token 失效、新 token 生效，controller 以新 token 启动成功

### REQ-MP-12: Synapse 启动引导（鸡生蛋解决）

系统 SHALL 在全新 Synapse 服务器上解决"注册令牌与 admin 用户从哪来"的引导问题：
容器启动 homeserver 前 SHALL 执行引导脚本，依次（幂等）：
1. 在数据卷生成/保留签名密钥（`homeserver.signing.key`）；
2. 若 admin 用户不存在，用 `register_new_matrix_user -c homeserver.yaml -a -u $ADMIN_USER -p $ADMIN_PASSWORD` 创建（经 `registration_shared_secret` 认证）；
3. 用登录得到的 admin 令牌 `POST /_synapse/admin/v1/registration_tokens/new {"token": "<AGENTTEAMS_REGISTRATION_TOKEN>"}` 创建注册令牌（已存在则跳过）；
4. `exec python -m synapse.app.homeserver -c homeserver.yaml`。

生成的 `homeserver.yaml` SHALL 设置：`server_name`、8008 客户端监听器、`enable_registration: true`、
`registration_requires_token: true`、`registration_shared_secret`、媒体存储路径、`app_service_config_files`。
用户注册 SHALL 在两个提供商上保持 `m.login.registration_token` 流程（controller/Manager 注册代码路径**零分支**）。

#### Scenario: 全新 Synapse 首次引导
- **GIVEN** 数据卷为空（全新部署）
- **WHEN** Synapse 容器启动执行引导脚本
- **THEN** 生成签名密钥与 admin 用户，预创建 `AGENTTEAMS_REGISTRATION_TOKEN`，随后启动 homeserver
- **AND** controller/Manager 用 registration_token 完成首个用户注册（与 Tuwunel 路径一致）

#### Scenario: 注册令牌生效
- **GIVEN** 引导已完成且设置了注册令牌
- **WHEN** 客户端以 `m.login.registration_token` 发起 `POST /_matrix/client/v3/register`
- **THEN** 有效令牌注册成功；令牌缺失或无效时拒绝注册

### REQ-MP-13: 受管 Synapse 部署（Helm）

当 `matrix.provider=synapse` 且 `matrix.mode=managed` 时，Helm chart SHALL 部署 Synapse：
StatefulSet（镜像、8008 端口、环境变量、探针）、ClusterIP Service（8008）、ConfigMap 持有生成的
`homeserver.yaml`（见 REQ-MP-12）、ConfigMap 持有 AppService 注册 YAML（见 REQ-MP-11）、ConfigMap
持有 `bootstrap-synapse.sh`（作为容器 `command`）。`matrix.synapse.*` values SHALL 镜像
`matrix.tuwunel` 的形状：`image.repository/tag/pullPolicy`、`replicaCount`、`resources`、
`service.type/port`、`persistence`（`/data`）、`registrationSharedSecret`（空则自动生成进 Secret）、`extraEnv`。
`matrix.internalURL` SHALL 按提供商分支指向 Synapse 客户端端口（8008）或 Tuwunel 端口；`matrix.serverName` SHALL 等于 Synapse `server_name`。
当 `matrix.provider=synapse` 时 SHALL NOT 渲染任何 Tuwunel 资源（反之亦然）。

#### Scenario: 渲染 Synapse 资源
- **WHEN** chart 以 `matrix.provider=synapse` 渲染
- **THEN** 存在 `<release>-synapse` StatefulSet 与 ClusterIP Service（8008），且不渲染任何 Tuwunel 资源

#### Scenario: 渲染 Tuwunel 资源（默认无回归）
- **WHEN** chart 未设置 `matrix.provider`（默认 `tuwunel`）渲染
- **THEN** 输出与现状完全一致，无任何 Synapse 资源

#### Scenario: 拒绝未知提供商
- **WHEN** 用户以不受支持的 `matrix.provider`（如 `dendrite`）安装 chart
- **THEN** `helm install` 以清晰错误失败，列出受支持提供商，不创建任何资源

### REQ-MP-14: Manager 启动引导按提供商感知

Manager 容器 SHALL 使用双提供商通用的就绪检查 `GET /_matrix/client/versions` 等待 homeserver 就绪
（`/_tuwunel/server_version` 仅在 `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` 时作为附加检查保留）。
k8s 模式下 Higress service-source 名称与 `/_matrix` 路由目标 SHALL 由 `AGENTTEAMS_MATRIX_PROVIDER`
决定（`tuwunel.dns` vs `synapse.dns`）。controller SHALL 通过 `ManagerAgentEnv`/`WorkerEnvDefaults`
把 `AGENTTEAMS_MATRIX_PROVIDER` 传播到 Manager/Worker 环境变量。嵌入式模式下
`manager/supervisord.conf` SHALL 以单个 `[program:matrix]` 分发器（`start-matrix.sh`）按提供商
exec `start-tuwunel.sh` 或 `start-synapse.sh`。Manager/Worker 注册与建房间步骤在两个提供商上
SHALL 保持不变（标准 C-S API + registration_token）。

#### Scenario: 嵌入式 Synapse 就绪检查
- **GIVEN** 嵌入式栈以 `AGENTTEAMS_MATRIX_PROVIDER=synapse` 启动
- **WHEN** Manager 等待 homeserver 就绪
- **THEN** 探测 `GET /_matrix/client/versions`，不依赖 `/_tuwunel/server_version`

#### Scenario: Higress 路由指向当前提供商
- **WHEN** Manager 在 k8s 模式下以 `matrix.provider=synapse` 初始化 Higress 路由
- **THEN** `/_matrix` 路由指向 `<release>-synapse` service source（而非 `tuwunel`）
- **AND** `matrix.provider=tuwunel` 时继续指向 `tuwunel`

### REQ-MP-15: 嵌入式安装器与本地安装

嵌入式 all-in-one controller 镜像与本地安装器 SHALL 支持双提供商。对于 Synapse：
镜像 SHALL 内置 `start-synapse.sh`（从环境变量生成 `homeserver.yaml`：服务器名、签名密钥、
共享密钥、注册令牌、AS 文件，并执行 REQ-MP-12 引导）；安装器 SHALL 生成 AppService 注册文件并
传入 AS 令牌环境变量；健康检查 SHALL 使用适合提供商的端点（`/_matrix/client/versions` 为主，
Tuwunel 特定探测仅保留于 tuwunel 提供商）。提供商选择 SHALL 默认 `tuwunel`，现有安装不受影响。

#### Scenario: 嵌入式 Synapse 启动
- **WHEN** 嵌入式栈以 Synapse 提供商启动
- **THEN** `start-synapse.sh` 生成配置并启动 Synapse 进程，Manager 引导完成注册与建房间

#### Scenario: 安装器健康检查按提供商感知
- **WHEN** 安装器以 Synapse 提供商等待 Matrix 就绪
- **THEN** 探测 `/_matrix/client/versions`，仅当 homeserver 应答时报告就绪

## MODIFIED Requirements

### REQ-MP-2（修改）: 用户生命周期（迁移到 Admin API，注册零分支）

系统 SHALL 通过 Synapse Admin API 完成用户生命周期操作（改密/恢复/停用），不再使用 Tuwunel bot 命令：
`EnsureUser` **注册段** SHALL 保持 `m.login.registration_token`（与 Tuwunel 零分支，见 REQ-MP-12），不引入 admin register 端点；
孤儿恢复 → `GET`/`PUT /_synapse/admin/v2/users/{userID}`；`SetPasswordAsAdmin` → `PUT /_synapse/admin/v2/users/{userID}`；
`DeactivateHumanUser` → `POST /_synapse/admin/v1/deactivate/{userID}`（`erase: false`，SSO Human 删除路径依赖，保留）。
`AdminCommand`（client.go:834）在 Synapse 路径 SHALL NOT 被调用。

#### Scenario: 孤儿用户恢复
- **GIVEN** 某用户注册过但账号处于 deactivated/孤儿状态
- **WHEN** `EnsureUser` 需要恢复该用户
- **THEN** 通过 Admin API 读取用户状态，设 `deactivated:false` 并重设密码
- **AND** 重试登录成功

### REQ-MP-3（修改）: 房间生命周期（创建者驱动 + Agent 删除清理）

`CreateRoom` SHALL 去除 admin token 兜底：worker 房间 SHALL 由 AS 登录 worker 创建（worker 为创建者/成员，admin 不在房）。
团队房间 / Leader DM SHALL 保持现状（`TeamAdminActorToken` 创建 + admin 主动退出，provisioner.go:829-836）。
`deleteRoom` SHALL 走 `DELETE /_synapse/admin/v2/rooms/{roomID}`（purge）+ `delete_status` 轮询；
`ForceLeaveRoom` SHALL 以创建者身份走 C-S API kick（REQ-MP-9 机制，Synapse 无 admin kick 端点）。
`provisioner.go:412` SHALL 不再将 `adminMatrixID` 加入 invite 列表。
Agent 删除时的房间清理 SHALL 在 Synapse 上通过 admin API（purge/delete）完成数据清理，
使同名 Agent 重建时从干净房间开始（替代 Tuwunel 的 `CONDUWUIT_DELETE_ROOMS_AFTER_LEAVE` 环境变量折叠）。

#### Scenario: worker 房间由 worker 创建
- **GIVEN** 需要为 worker 创建房间
- **WHEN** 控制器执行房间创建
- **THEN** 以 AS 代登录的 worker token 创建，worker 是创建者/成员
- **AND** 全局 admin 不在房间内

#### Scenario: 删除 Agent 时清理房间
- **GIVEN** Worker 在 Synapse 上运行期间被删除
- **WHEN** 控制器执行房间清理
- **THEN** controller 离开这些房间并通过 Synapse admin API purge/删除房间数据
- **AND** 同名 Agent 重建时从干净房间开始

### REQ-MP-4（修改）: 消息（创建者身份 + SyncMessages 移除）

`SendMessageAsAdmin` SHALL 以房间创建者身份执行（探测创建者 → AS 代登录 → `SendMessage`）——
Synapse 无"以任意身份发消息"的 Admin 端点。
`SyncMessages` SHALL 从 `Client` 接口移除（1.2.0 无生产调用方；同步清理接口、mock 与测试引用）。

#### Scenario: 注入系统欢迎语（Synapse 路径）
- **GIVEN** Manager 房间已建立
- **WHEN** 控制器需要注入欢迎语（原 `SendManagerWelcomeMessage`）
- **THEN** 以创建者身份执行发送（AS 代登录），admin 不在房

### REQ-MP-5（修改）: 成员与房间枚举（服务端）

`ListRoomMembers` SHALL 通过 `GET /_synapse/admin/v1/rooms/{roomID}/members`（服务端）实现，
不再要求 admin 为房间成员。`ListJoinedRooms`、别名解析等保持 C-S API 现状。

### REQ-MP-6（修改）: Application Service 集成（静态注册 + fail-fast 校验）

`RegisterAppService`/`UnregisterAppService` SHALL 在 Synapse 路径返回 nil（跳过——注册由 Helm
部署期渲染完成，见 REQ-MP-11）；Tuwunel 路径 SHALL 保留运行时 admin-bot 流程（现状）。
`AppServiceSmokeTest` 语义 SHALL 从"等待异步注册完成"改为"校验部署正确性，失败即停"。
AS namespace SHALL 保持独占（exclusive），仅适用于 AgentTeams 独占 homeserver。

### REQ-MP-7（修改）: 隐藏前提消除 —— admin 永不进房

系统 SHALL NOT 将全局 admin 驻留任何 AgentTeams 房间（worker / 团队 / Leader DM 均不邀请 admin）。
所有房间内操作 SHALL 以创建者身份执行（REQ-MP-9），所有服务端操作 SHALL 走 Admin API（REQ-MP-10）。

#### Scenario: admin 不在任何房间且操作成功
- **GIVEN** 全局 admin 未加入任何 AgentTeams 房间
- **WHEN** 控制器执行 invite / kick / 发消息 / 删除等全生命周期操作
- **THEN** 所有操作成功执行，无需 admin 作为房间成员
