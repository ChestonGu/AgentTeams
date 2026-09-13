# worker-bridge 运行时 — 实施目录

**统一命名（v3 规整）**：runtime 名为 `worker-bridge`（原 opencode 链路全量退役）。
桥接 pod 伪装成 worker（同 Matrix 身份、同 runtime.yaml 投影），会话循环在外部
运行时上，由 adapter 决定形态：

- `cimicode-stateless`：bridge 直调外部 cimicode 平台（绑定来自 Worker CR 的
  `cimicodeGatewayUrl`/`sessionId`/`sandboxId`/`templateId`，经 controller 投影进
  runtime.yaml 顶层 bridge 段）；
- `cimicode-pod`：`operator/` 供给单 cimicode pod（`cimicode-runtime/` 合并镜像：
  换皮 opencode + 协作工具 + skills，单容器无 sandbox）——Deployment+svc（runtime
  :4096 / helper :4097 双端口）+ FS 凭据 Secret（bridge pod env 明文只读复制），
  并 patch Worker CR env `BRIDGE_RUNTIME_ADAPTER`/`_BASE_URL`/`_HELPER_URL`，
  bridge 自愈轮询接上。传输契约见
  [contract/adapter-contract.md](contract/adapter-contract.md)。

裁决顺序：显式 env > runtime.yaml bridge 段 > 未定态（bridge 不建 client，轮询等）。
设计/操作权威：[docs/worker-bridge运行时替换与协作流转详解.md](docs/worker-bridge运行时替换与协作流转详解.md)、
[docs/worker-bridge-worker运行时迁移方案.md](docs/worker-bridge-worker运行时迁移方案.md)
（迁移史；D0 总原则：一切以 copaw 机制为准，唯一改变是工具载体）。
本目录随 **AgentTeams 主仓库**管理（分支 `feature/worker-bridge-merge`）。

**架构 v2.4（契约 §0/§6）**：无独立编排层；沙箱无状态（无 SOUL/memory/heartbeat
文件，拉推仅 `shared/`；persona 内容经生成器合并进 prompt）；skills/CLI
镜像统一预装；agent.md 每次会话由 bridge 调**生成工具**产出（源模板 +
runtime.yaml 渲染，**不再解析 canonical AGENTS.md**）后作为 system prompt
下发；**所有工具日志统一写入一个 JSONL 文件**（契约 §5.5，`agentteams_log.py`）。

## 目录 ↔ 任务模块

| 目录 | 内容 | 对应任务 |
|---|---|---|
| `docs/` | 设计方案（唯一权威文档） | — |
| `cli/taskflow/` | taskflow CLI（= copaw taskflow 工具的 check_task/ack_task/submit_task，worker 侧） | T1 ✅ / T2 ✅ |
| `cli/taskflow/mc_sync.py` | mc 同步后端（vendor 自 copaw sync.py 共享路径子集；filesync CLI 与 projectflow 复用同源副本） | T2 ✅ |
| `cli/taskflow/agentteams_log.py` | **统一日志模块（契约 §5.5）**：所有工具 → 一个 JSONL 文件（cmd_start/业务事件/cmd_end，tool/worker/run_id 戳）；逐字节部署到各 scripts 目录 + bridge/ | 日志 ✅ |
| `cli/sync/` | agentteams-sync CLI（= copaw filesync 的 pull/push/stat/list） | T3 ✅ |
| `cli/projectflow/` | **projectflow CLI（leader 侧，预置未部署）**：project/tasks/plan-dag/plan-loop/ready/delegate/delegate-commit/check；core 为 copaw task.py 全量 vendor | leader 预置 ✅ |
| `bridge/` | **bridge 进程本体 + 其工具**（原顶层 `cimicode-bridge/` 已并入；镜像名 `agentteams/cimicode-bridge` 不变，构建上下文=仓库根）：`src/` FastAPI 进程（matrix 防护、turn 编排、adapter 裁决）+ `generate_agent_md.py`/`agentteams_log.py`（agent.md **生成**工具与统一日志模块，部署进镜像 /opt/agenttools/，生成器源模板 + runtime.yaml 结构化渲染，fail-loud）+ `tests/unit/`（进程 pytest）与 `tests/`（生成器 golden） | 契约 §6 v2.4 ✅ |
| `template/worker-bridge-agent/` | worker 模板（**AGENTS.md = 源模板**：骨架固化 + §2-§8 静态 + 仅 `{{COORDINATION}}`/`{{ENVIRONMENT}}` 两个占位符；5 skills + scripts 部署副本） | T4-T7 ✅ |
| `template/worker-bridge-leader-agent/` | leader 模板 starter（AGENTS.md 参考 + task-management leader 版 skill + projectflow 三件套），未部署 | leader 预置 ✅ |
| `contract/` | `interface-contract.md` v2.4（§0 架构决策 / env / 镜像布局 / shared 唯一同步 / 消息 / 命令（worker §5.1-5.3 + leader §5.4）/ **统一日志 §5.5** / agent.md **生成契约 §6**（源模板 + runtime.yaml + persona）/ 职责矩阵）+ `controller-handover.md` v2.4（零代码改动 + qwenpaw/edge 分支前提）+ `adapter-contract.md` v1.0（adapter 两形态传输契约：stateless SSE / pod opencode REST+轮询、端口与接线三键、已知限制） | T8-T9 ✅（v2 重写；adapter 契约 2026-09-13 增补） |
| `operator/` | **worker-bridge-operator**：watch runtime=worker-bridge 的 Worker CR，按 `spec.adapterMode` 分派——`cimicode-stateless` 零供给；`cimicode-pod`（含空值）供给单 cimicode Deployment+svc（命名 `<w>-cimicode` / `<w>-cimicode-svc`，角色后缀式；双端口 runtime:4096/helper:4097，`CIMICODE_IMAGE` 必填、端口/探针默认按 cimicode-runtime 镜像契约）+ FS 凭据 Secret `<w>-cimicode-fs`（从 controller 组装的 bridge pod env 明文只读复制，secretKeyRef 注入）+ 工作 env（WORKER_NAME/FS_*/TEAM/MATRIX_USER_ID——team 经 Team CR workerMembers 反查，MATRIX_USER_ID 以 status.matrixUserID 为权威）并 patch Worker env `BRIDGE_RUNTIME_*` 三键；bridge pod 未起时本轮推迟（下一轮重试） | v3 §4.3 ✅（真实运行时改造 2026-09-13） |
| `cimicode-runtime/` | **cimicode pod 合并镜像构建上下文**（单容器：opencode serve :4096 + sandbox helper :4097 + taskflow/agentteams-sync/mc + skills 全套 + 烘焙智谱 provider；`ZHIPU_API_KEY` build-arg 必填，仓库内占位符） | 真实运行时 ✅（2026-09-13） |
| `cimicode-sandbox/` | cimicode 运行时自建沙箱的基础镜像构建上下文（stateless 形态 cimicode 自用时） | v3 ✅ |

冒烟/模拟器等测试资产（`verify/`、smoke 记录）按分支规整方案不随本分支搬运，留在源分支 `dev-v1.2.2-opencode-ben-test` 可追溯。

## 进度

- [x] T1 taskflow CLI：状态机与命令（core vendor 自 copaw task.py + check/ack/submit + 冒烟通过）
- [x] T2 护栏与推送：mc 同步后端（k8s/STS/静态三模式 alias、teams/{team}/shared/ 远端布局）+ submit 远端 `mc stat` verify + push/verify 失败回滚 + 33 单测。对齐 copaw 精确流程：check/ack pull、submit 不 pull、push 恒 exclude `spec.md`+`base/`
- [x] T3 agentteams-sync CLI（filesync 四 action，10 单测）
- [x] T4-T7 模板与 skills 改写：AGENTS.md 六点适配（§9 团队表后于 v2.3 移除，团队事实走 Coordination 块透传）；skills JSON 样例→CLI 命令；**find-skills 已删除**（无外网，v2 §0.5）；scripts 部署副本与 cli/ 源逐字节一致
- [x] T8-T9 契约与移交（v2 重写）：契约 v2.1；controller **零代码改动**（v1 builtinAgentDir case 取消）
- [x] T10 真环境冒烟：10.254.254.105 minio-sim 六步 18 项全过；**发现并修复 `--sync` 默认值 bug**（none→mc，裸调才暴露）
- [x] T11 模拟器（v2.1 复测）：none/mc 双模式 13 项全过——prompt 来自**真转换工具**、FakeLeader 走**真 projectflow**（leader↔MinIO↔worker 经同一存储闭环）
- [x] 架构 v2 落地：find-skills 删除 + 引用清理；system-prompt 模板**弃用删除** → `bridge/convert_agent_md.py`（30 单测含 golden：转换输出==模板+环境段，逐字节；controller 标记骨架保持、`## Coordination` 块透传、SOUL/PROFILE 合并）
- [x] leader 预置：`cli/projectflow/`（core=task.py 全量 vendor，CLI 只暴露 leader 动词，**推送无排除**=协议所有方）+ 20 单测（含与 worker taskflow 的协议闭环）+ leader 模板 starter
- [x] 统一日志（v2.2 §5.5）：`agentteams_log.py` 一个 JSONL 文件收全部工具（taskflow/projectflow/agentteams-sync/convert_agent_md），run_id 归并、状态变迁/回滚/错误事件、截断与降级保证；模拟器验证 leader+worker 双工具同文件 + 部署副本漂移检查（曾抓出 3 个过期脚本副本）
- [x] 转换器数据源重构（v2.3 §6）：**身份/团队信息零 CLI 参数**——删 `--worker-name/--matrix-id/--team/--storage-prefix/--members` 与 §9 团队表（团队事实唯一来源=透传的 Coordination 块）；Environment 段渲染自 `--runtime-config`（MinIO `agents/<name>/runtime/runtime.yaml`，copaw 同款逐行解析）；**标记骨架与 copaw 对齐**（builtin-start/DO NOT EDIT/builtin-end/team-context 围栏全保留；顺带修掉前导标记丢失、段尾空行吞 fence 两个隐藏 bug）；fixture 换成真实生产形态（desensitized），bridge 不再需要 controller 名册查询
- [x] **生成架构重构（v2.4 §6）：canonical AGENTS.md 彻底退役**——转换器（字符串替换型，v2.3 两个 bug 的根源）删除，换 `bridge/generate_agent_md.py`：源模板（占位符化）+ runtime.yaml **结构化解析**（PyYAML `safe_load`，曾抓出 v2.3 fixture 尾部污染）→ Coordination 块渲染（文案与 controller `coordination.go` 逐行对齐，4 种 Respond 变体/牛津逗号规则/去重）+ 环境段；SOUL/PROFILE 前置 `## Persona` 接缝段逐字合并（身份归属 + 不覆盖 Worker 角色）；fail-loud 全链（缺身份/无 leader 成员/非法规括号/模板占位符数≠2 → 退出 1）。41 单测（golden 两份逐字节锁定 + coordination.go 文案对齐 + 解析器校验）+ 模拟器 22 项全绿。**前提：Member 落 qwenpaw/edge 调谐分支（生产同型部署已确认）**
- [ ] T12-T13 真环境联调 / V1-V4 验收（**前置：opencode 沙箱镜像构建（含 PyYAML + 源模板 + 生成器）+ bridge 接入生成工具**，见 contract/ 移交说明；V5 跨会话记忆已随 v2 取消）

## 本地验证

```bash
# 单测（共 113 个；开发机需 pip install pyyaml——bridge 生成工具做结构化解析）
cd cli/taskflow && python -m unittest discover -s tests   # 40 个（taskflow + mc_sync + 统一日志）
cd cli/sync && python -m unittest discover -s tests       # 10 个（agentteams-sync）
cd cli/projectflow && python -m unittest discover -s tests # 20 个（leader core + CLI + 与 worker 闭环）
cd bridge && python -m unittest discover -s tests -p "test_generate_agent_md.py" # 43 个（生成工具：golden×2 + coordination.go 文案对齐 + 解析器/Persona/CLI；进程单测另跑 pytest tests/unit）
cd ../operator && python -m pytest tests -q    # 18 个（operator：svc 双端口/env+secretKeyRef/drift/三键 patch/GC/推迟供给）

# 生成工具（bridge 调用形态；runtime.yaml/SOUL/PROFILE 均从 MinIO 拉下后传路径）
python bridge/generate_agent_md.py --runtime-config <runtime.yaml> \
    [--soul-file SOUL.md --profile-file PROFILE.md]   # stdout 即 system prompt
```

沙箱内实际部署：`~/skills/task-management/scripts/taskflow.py`（worker）、
`projectflow.py`（未来 leader）；本地跑须显式 `--sync none`（默认已是 mc）。
统一日志缺省落在 `$AGENTTEAMS_FS_ROOT/logs/agentteams.log`（与 `shared/`
平级，永不同步）；排查时看这一个文件即可（契约 §5.5）。

## 与 copaw 的已知差异（有意为之）

1. **CLI 层前置 CAS**：copaw 的 ack_task/submit_task 只查身份+room，不查当前状态（重 ack 会把 submitted 打回 in_progress）。本 CLI 在命令层拒绝：ack 要求 assigned（in_progress 幂等成功）、submit 要求 in_progress。core 层（taskflow_core.py / projectflow_core.py）保持与 copaw 逐行等价，产物协议逐字段一致。
2. **显式 UTF-8 + LF**：core 的文件读写显式 `encoding="utf-8"`、写入显式 `newline="\n"`（copaw 依赖 Linux 平台默认；Windows 开发机上默认编码是 GBK 且会把 `\n` 翻成 CRLF 上传 MinIO）。Linux 沙箱下两者字节完全一致。
3. **push/verify 失败回滚**：copaw 的 submit 推送失败后本地已是 submitted、无回滚；worker CLI 恢复命令前快照的 meta.json，leader CLI（projectflow）快照全部被改协议文件（§5.4.2）。
4. **projectflow 推送无排除**：与 worker taskflow 恒 exclude `spec.md`+`base/` 相对——leader 是协议所有方，plan/spec/meta 从 leader 推向存储（§5.4.1）。
