# opencode Worker 运行时迁移 — 实施仓库

将普通 team worker 从 copaw 迁到 opencode（**leader 保持 copaw 不迁移**；
leader 侧 CLI 已预置未部署，见 `cli/projectflow/`）。
设计文档：[docs/opencode-worker运行时迁移方案.md](docs/opencode-worker运行时迁移方案.md)（D0 总原则：一切以 copaw 机制为准，唯一改变是工具载体）。

**架构 v2.4（契约 §0/§6）**：无独立编排层；沙箱无状态（无 SOUL/memory/heartbeat
文件，拉推仅 `shared/`；persona 内容经生成器合并进 prompt）；skills/CLI
镜像统一预装；agent.md 每次会话由 bridge 调**生成工具**产出（源模板 +
runtime.yaml 渲染，**不再解析 canonical AGENTS.md**）后直传 opencode
作 system prompt；controller 零代码改动。**所有工具日志统一写入一个
JSONL 文件**（契约 §5.5，`agentteams_log.py`）。

## 目录 ↔ 任务模块

| 目录 | 内容 | 对应任务 |
|---|---|---|
| `docs/` | 设计方案（唯一权威文档） | — |
| `cli/taskflow/` | taskflow CLI（= copaw taskflow 工具的 check_task/ack_task/submit_task，worker 侧） | T1 ✅ / T2 ✅ |
| `cli/taskflow/mc_sync.py` | mc 同步后端（vendor 自 copaw sync.py 共享路径子集；filesync CLI 与 projectflow 复用同源副本） | T2 ✅ |
| `cli/taskflow/agentteams_log.py` | **统一日志模块（契约 §5.5）**：所有工具 → 一个 JSONL 文件（cmd_start/业务事件/cmd_end，tool/worker/run_id 戳）；逐字节部署到各 scripts 目录 + bridge/ | 日志 ✅ |
| `cli/sync/` | agentteams-sync CLI（= copaw filesync 的 pull/push/stat/list） | T3 ✅ |
| `cli/projectflow/` | **projectflow CLI（leader 侧，预置未部署）**：project/tasks/plan-dag/plan-loop/ready/delegate/delegate-commit/check；core 为 copaw task.py 全量 vendor | leader 预置 ✅ |
| `bridge/` | **`generate_agent_md.py`**：agent.md **生成**工具，供 matrix bridge 每次会话调用（源模板 + runtime.yaml 结构化渲染：Coordination 块 + 环境段 + SOUL/PROFILE 经 `## Persona` 接缝逐字合并），输出即 system prompt；fail-loud（身份/占位符/残留括号校验不过即退出 1，bridge 拒开会话） | 契约 §6 v2.4 ✅ |
| `template/opencode-worker-agent/` | worker 模板（**AGENTS.md = 源模板**：骨架固化 + §2-§8 静态 + 仅 `{{COORDINATION}}`/`{{ENVIRONMENT}}` 两个占位符；5 skills + scripts 部署副本） | T4-T7 ✅ |
| `template/opencode-leader-agent/` | leader 模板 starter（AGENTS.md 参考 + task-management leader 版 skill + projectflow 三件套），未部署 | leader 预置 ✅ |
| `contract/` | `interface-contract.md` v2.4（§0 架构决策 / env / 镜像布局 / shared 唯一同步 / 消息 / 命令（worker §5.1-5.3 + leader §5.4）/ **统一日志 §5.5** / agent.md **生成契约 §6**（源模板 + runtime.yaml + persona）/ 职责矩阵）+ `controller-handover.md` v2.4（零代码改动 + qwenpaw/edge 分支前提） | T8-T9 ✅（v2 重写） |
| `verify/` | 冒烟（`smoke.sh` + 记录）+ 模拟器（`simulator.py` + 记录；v2.1：prompt 走真转换工具、FakeLeader 走真 projectflow） | T9-T11 ✅ |

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
# 单测（共 111 个；开发机需 pip install pyyaml——bridge 生成工具做结构化解析）
cd cli/taskflow && python -m unittest discover -s tests   # 40 个（taskflow + mc_sync + 统一日志）
cd cli/sync && python -m unittest discover -s tests       # 10 个（agentteams-sync）
cd cli/projectflow && python -m unittest discover -s tests # 20 个（leader core + CLI + 与 worker 闭环）
cd bridge && python -m unittest discover -s tests         # 41 个（生成工具：golden×2 + coordination.go 文案对齐 + 解析器/Persona/CLI）

# 模拟器（本地无 MinIO；含部署副本漂移检查 + 统一日志单文件检查）
python verify/simulator.py --mode none

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
