# T11 模拟编排层联调记录

**日期** 2026-09-01 · **结果** ✅ SIM PASSED（none / mc 双模式，11 项检查全过；**同日 v2.1 重构后 13 项、2026-09-02 v2.2 扩至 18 项双模式复测通过，见文末**）

**交付物**：`verify/simulator.py`（纯 stdlib 单文件模拟器，本记录）

## 模拟器覆盖的契约面

模拟**编排层三职责 + copaw Leader 对手方**，worker 为剧本化 agent（无 LLM，
行为序列 = Runtime Notes [3] 段所规定的序列，经沙箱部署路径调真实 taskflow CLI）：

| 模拟对象 | 契约条目 | 实现 |
|---|---|---|
| 沙箱初始化 | §3 | `sandbox_init()`：模板 skills/AGENTS.md + SOUL.md 部署到 `agents/<worker>/`；mc 模式重放 `*.sh` 执行位 |
| system prompt 渲染 | §6 | `render_system_prompt()`：4 段拼接（[3] 段从模板代码块**逐字提取**）；`{{TEAM_MEMBERS_TABLE}}` 渲染为 3 人对照表 |
| 触发消息构造 | §4 | `build_trigger()`：双 marker 逐字同构（与 copaw matrix_channel.py:154-158）；history 上限 50；空 history 只发 current 段 |
| 回复转发 | §4 | `forward_reply()`：非 NO_REPLY 原样写入房间日志；NO_REPLY 吞掉（文件尺寸不变断言） |
| copaw Leader | §7 | `FakeLeader`：delegate（写 meta.json+spec.md，mc 模式 mirror 推）→ read_back（mc 模式 mirror 拉回，读 meta/result）——leader 的推拉是独立实现，与 worker 的 taskflow 路径互为字节级对手方 |
| opencode worker | §5.1 | `FakeWorker.run_task()`：check → ack（断言 spec 全文在输出）→ 写 deliverable → submit → 回复 `@coordinator TASK_COMPLETED ...` |

## 验证点（11 项）

**prompt 渲染（5）**：[1] SOUL 打头 / [2] 成员表已渲染 / [3] 运行时段逐字 /
[4] 环境段含 worker+team / 无 `{{` 残留。

**消息契约（2）**：双 marker + `{sender}: {body} [id:{n}]` 行格式**逐字节等于**
预期串；空 history → 仅 current 段。

**全链路（4）**：delegate → 触发 → check/ack/干活/submit → forward →
leader 读回 `status=submitted`、`assigned_to` 未变、result.md 协议字段、
回复转发落日志。

## 执行环境与结果

| 模式 | 环境 | 结果 |
|---|---|---|
| `--mode none` | Windows 开发机（本地 shared/ 权威，无 MinIO） | ✅ 11/11 |
| `--mode mc` | 10.254.254.105（minio-sim 真实 MinIO，隔离 team=`sim-<ts>`，§1 三元组 + `AGENTTEAMS_TEAM` 注入） | ✅ 11/11，远端清理后零残留 |

mc 模式下的关键意义：**Leader（独立 mc mirror 实现）↔ MinIO ↔ worker（taskflow
--sync mc）三方闭环**——delegate 从 leader 侧推上去的任务，worker 经 env 契约
拉取/确认/回推，leader 再拉回读到的 result.md 与协议逐字段一致。

## 过程中发现的问题

1. 模拟器初版 `cond and ok(...) or fail(...)` 写法：`ok()` 返回 None，条件真也
   会执行 `fail()`——首轮"11 项全 FAIL"实为误报。改 `check(cond, ok, fail)`
   显式分支（模拟器自身 bug，不涉及交付 CLI）。
2. result.md 断言串笔误（`- workspace/...` vs 实际补全后的
   `- shared/tasks/<id>/workspace/...`）——产物本身正确，裸路径补全行为与
   契约 §5.1 一致，反而顺带实证了该行为。

## v2.1 重构（同日，架构 v2 + §6 转换工具化 + leader 侧 projectflow）

架构 v2 定稿与"bridge 直传转换后 agent.md"决策落地后，模拟器同步重构，
上表中的三处实现被替换（其余不变）：

| 项 | v1 实现 | v2.1 实现 |
|---|---|---|
| 沙箱初始化 | agents/<worker>/ 树 + SOUL.md + skills | **仅镜像预装 skills**（无 agent 树、无 SOUL，§2 v2） |
| prompt 渲染 | `render_system_prompt()` 4/3 段拼接模板 | **直接跑真转换工具** `bridge/convert_agent_md.py`（fixture=copaw canonical AGENTS.md 输入，输出即 system prompt，§6 v2.1） |
| FakeLeader | 手写 meta/spec + 独立 mc mirror | **真 projectflow CLI**：create-project → add-tasks → delegate → delegate-commit → check（§5.4） |

验证点扩至 **13 项**：prompt 段改为（前导 opencode 化 / §2 中性段透传自
canonical / 团队表本 worker 行居首 / 环境段 / 无 find-skills 与 QwenPaw 残留 /
无 SOUL / 无 `{{` 残留）+ 消息契约 2 项 + 全链路 4 项（leader 读回新增
`effective_for_acceptance: yes`、`assigned_to` 从 roster MXID 规范化为
logical name 两断言）+ NO_REPLY。

### 重跑结果

| 模式 | 环境 | 结果 |
|---|---|---|
| `--mode none` | Windows 开发机 | ✅ 13/13 |
| `--mode mc` | 10.254.254.105（同上隔离 team） | ✅ 13/13，零残留 |

mc 模式现在的关键意义升级为：**projectflow（leader，推送无排除）↔ MinIO ↔
taskflow（worker，推送排除 spec/base）双方经同一存储闭环**——远端对象快照
（清理前）恰好实证 leader 推了 plan.md/spec.md/meta.json、worker 推了
workspace/ + result.md，职责分界与契约 §5.4.1 一致。

配套全量单测（同日服务器同目录跑）：bridge 14 + taskflow 35 + sync 10 +
projectflow 20 = **79 全绿**。

## v2.2 扩展（2026-09-02，persona 合并 + 统一日志 + 部署副本护栏）

三项新增（验证点扩至 **18 项**）：

1. **部署副本漂移检查**（1 项，覆盖 11 个副本）：启动时校验全部模板
   scripts 部署副本与 `cli/` 源逐字节一致。**当日即抓出 3 个过期副本**
   （worker taskflow.py、file-sharing agentteams_sync.py、leader
   projectflow.py——日志接线只更新了 cli/ 源，模板副本未刷新，导致
   worker 侧无日志）。该检查为此常驻。
2. **转换器日志检查**（1 项）：converter 的统一日志 JSONL 含 `converted`
   事件且带 tool/run_id/worker_name 戳（stderr 断言改为 env 静音 + 独立
   日志文件，stdout 字节契约不变）。
3. **单文件双工具检查**（2 项）：全链路后 `$FS_ROOT/logs/agentteams.log`
   同一文件内同时有 `tool: projectflow` 与 `tool: taskflow` 记录，且含
   `t-sim-1` 的 `status_change` 轨迹。

服务器回归（10.254.254.105）：四套单测 40+10+20+22=**92 全绿** + none/mc
双模式 18 项全过。期间发现并修复：非 root 环境缺省日志路径不可写时，
降级告警打 stderr 破坏 CLI stderr 契约 → 告警现随 `AGENTTEAMS_LOG_STDERR=0`
一并静默（mc 模式复跑失败现场即该日志链路的完整展示：cmd_start→push→
rollback→error→cmd_end）。

## 复现

```bash
# 本地（无 MinIO）
python verify/simulator.py --mode none
# 服务器（真实 MinIO，隔离 team 前缀）
AGENTTEAMS_FS_ENDPOINT=<ep> AGENTTEAMS_FS_ACCESS_KEY=<ak> \
AGENTTEAMS_FS_SECRET_KEY=<sk> AGENTTEAMS_STORAGE_ALIAS=<alias> \
AGENTTEAMS_TEAM="sim-$(date +%s)" python3 verify/simulator.py --mode mc
```

## v2.3 转换器数据源重构（2026-09-02，需求方定夺）

需求方以**真实生产 MinIO 文件**推翻了 v2.1 的参数式数据流：团队信息已在
controller 注入的 Coordination 块里（AGENTS.md 原样可用）；身份信息在
runtime.yaml（`agents/<name>/runtime/runtime.yaml`，MemberRuntimeConfig，
controller 调谐写入、worker sync 每 60s 刷新——`runtime_config.go` 写端 +
`copaw sync.py pull_all` 读端双向查证）。落地：

1. **fixture 全部换成真实生产形态**（desensitized）：team/solo 两份
   AGENTS.md（builtin-start + DO NOT EDIT + §2-§8 + builtin-end + team
   fence + Coordination 含 Coordinator Members）+ runtime.yaml。
2. **转换器**：删 `--worker-name/--matrix-id/--team/--storage-prefix/
   --members` 与 §9 名册表；新增 `--runtime-config`（copaw
   `_runtime_config_field` 同款逐行解析，无 PyYAML）；**标记骨架与 copaw
   prompt 完全同构**。真实形态当场暴露并修复两个隐藏 bug：前导
   builtin-start/DO NOT EDIT 块随前言替换被丢弃；§8 段尾 fence 前的
   **空行**令搬移循环提前止步、builtin-end 随替换段丢失（旧 fixture 围栏
   无空行相邻故从未触发；末段之后的 fence 现落成 fence-only 尾段）。
3. **验证点 18 → 22**：prompt 检查改为标记骨架序（builtin-start 打头 /
   builtin-end 收 builtin 段、先于 Coordination / Coordination 含
   Coordinator Members / 无 §9 无名册表）+ Environment 段字段全部来自
   runtime.yaml。

### 重跑结果（双环境）

| 模式 | 环境 | 结果 |
|---|---|---|
| `--mode none` | Windows 开发机 | ✅ 22/22 |
| `--mode none` / `--mode mc` | 10.254.254.105（真实 MinIO，隔离 team） | ✅ 22/22 双模式，远端 teams/ 前缀清理零残留 |

配套单测：bridge 重写为 **30**（golden=模板+环境段、标记骨架、runtime
解析、solo/team 双形态、persona、CLI、日志），总计
**30+40+10+20 = 100 全绿**（双环境一致；bridge 22→30）。

## v2.4 生成架构重构（2026-09-03，需求方定夺）

需求方以"转换器是字符串替换型、太脆弱"推翻 v2.3 的 canonical 解析路线：
**canonical AGENTS.md 彻底退役为非输入**。agent.md 改由生成工具从结构化
数据源渲染（`bridge/generate_agent_md.py`）：

1. **数据源**：源模板（`template/opencode-worker-agent/AGENTS.md` 占位符化：
   骨架固化 + §2-§8 静态 + 仅 `{{COORDINATION}}`/`{{ENVIRONMENT}}` 两个
   占位符，镜像预装）+ runtime.yaml（**结构化解析** PyYAML `safe_load`，
   身份与团队事实唯一来源）+ SOUL/PROFILE（前置 `## Persona` 接缝段
   逐字合并——身份归属声明 + 不覆盖 Worker 角色）。
2. **Coordination 渲染**：worker 形态从 runtime.yaml team 段渲染
   Coordinator（team_leader 成员）/Team Admin/Coordinator Members，文案
   与 controller `coordination.go` 逐行对齐（4 种 Respond 变体、
   牛津逗号规则、coordinator 去重 + admin 排除）；standalone 形态渲染
   Manager。v2.3 的两个 markdown 手术 bug（前导标记丢失、空行吞 fence）
   连同类消除。
3. **fail-loud**：缺身份/matrixId 无域/worker 无 team_leader 成员/
   role=team_leader/模板占位符数≠2/未知占位符/残留 `{{`/非法 YAML →
   退出 1，bridge 拒开会话。结构化解析当日即抓出 v2.3 fixture 尾部
   污染（teamRoomId 后粘了对话残留文本，逐行解析从未发现）——
   证明该路线的容错价值。
4. **模拟器适配**：`generate_agent_md()` 自写完整形态 sim runtime.yaml
   （leader/admin/coordinator 成员齐全）→ 仅 `--runtime-config` 调真
   生成器；prompt 检查改为 Coordination 渲染自 team 事实（leader/
   admin/coordinator 三断言）+ 无 Persona 接缝（未配 SOUL 时）+
   generated 事件 tool=generate_agent_md。转换器 fixture
   （canonical AGENTS.md ×2）删除，golden 换两份渲染快照。

### 重跑结果（双环境）

| 模式 | 环境 | 结果 |
|---|---|---|
| `--mode none` | Windows 开发机 | ✅ 22/22 |
| `--mode none` / `--mode mc` | 10.254.254.105（真实 minio-sim，隔离 team） | ✅ 22/22 双模式，远端 teams/ 前缀清理零残留 |

配套单测：bridge 重写为 **41**（golden×2 逐字节锁定、coordination.go 文案
对齐、PyYAML 解析器校验含非法 YAML 拒收、Persona 接缝序、CLI/日志），
总计 **41+40+10+20 = 111 全绿**（双环境一致；服务器 PyYAML 6.0.1 已在）。
**前提备忘**：opencode worker 的 Member 须落 qwenpaw/edge 调谐分支
（`member_reconcile.go:454`，生产同型部署已确认）——runtime.yaml 才会被
写入；该分支本就不跑 canonical AGENTS.md 合成，与生成架构天然契合。
