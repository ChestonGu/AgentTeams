# Leader 走 worker-bridge 无状态化 P0 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `member.role=team_leader` 的 Worker 以 runtime=worker-bridge 跑通最小闭环(DM 收请求 → 立项 → 委派自动通知 → worker TASK_COMPLETED → check → 终报),新增 bridge 出站 send 端点与 projectflow 自动通知。

**Architecture:** 复用同一 bridge 进程与镜像;生成器按 `member.role` 自动分派 leader/worker 源模板(bridge 调用链零改动);跨房发送走 bridge 新增 HTTP send 端点(复用 `gateway.send_text` 的 m.mentions 自动解析,新增可选 txn 幂等);委派通知由 `projectflow delegate-commit` 工具内自动发送(对齐 copaw `taskflow(delegate_task)` 工具语义);leader 沙箱镜像为独立变体(skills 替换式,worker 镜像字节不变)。

**Tech Stack:** Python 3.12(FastAPI/pydantic/matrix-nio/httpx)、pytest、Docker multi-stage。

**Spec:** 本计划自带规格(2026-10-15 四路考古+用户拍板:出站通道=HTTP send 端点+CLI;委派通知=工具内自动发;heartbeat 一期放弃;模板分发=镜像内置;worker 路径零变化)。

## Global Constraints

- **worker 语义零变化**:`template/worker-bridge-agent/**`、`cli/taskflow/**`、`bridge/src/cimicode_bridge/runtime/**`、`matrix/filter.py`、`config.py`、`operator/**`、`agentteams-controller/**` 全部不改动;`bridge/tests/fixtures/golden-team.md` 与 `golden-standalone.md` 逐字节不变(既有 golden 测试继续通过即为锁)。
- **leader/worker 切分**:模板独立目录;生成器按 role 走独立渲染分支(worker 分支函数体不动);leader 沙箱镜像独立 tag(skills 替换式,不与 worker skills 并存);CLI 改动只落 `cli/projectflow/`(leader 专属)。
- **不做的事(本计划明确排除)**:mention 白名单角色化(现状 everyone=human 放行,leader 形态现状即工作,语义收紧留 P1 单独决策);媒体归档(P1);global-shared 通道(P1);pod 模式 runtime 镜像 leader 变体(P2);controller/operator 任何改动。
- **fail-loud**:生成器对非法输入 exit 1 的政策不变;通知发送失败不 commit(保持 prepared,可重试)。
- **CRLF**:所有新写文件用 LF;golden 逐字节比对,生成器已归一 CRLF,但 fixtures 文件本身必须 LF 落盘。
- **测试命令**:bridge 侧 `cd worker-bridge/bridge && python -m pytest tests/ -q`;projectflow 侧 `cd worker-bridge/cli/projectflow && python -m pytest tests/ -q`。
- 每个 Task 结束单独 commit,commit message 前缀 `feat(worker-bridge)` / `test(worker-bridge)`。

---

### Task 1: gateway.send_text 支持可选 txn 幂等参数

**Files:**
- Modify: `worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:202-221`
- Test: `worker-bridge/bridge/tests/unit/test_gateway_mentions.py`

**Interfaces:**
- Produces: `MatrixGateway.send_text(room_id, body, *, content=None, tx_id: str | None = None) -> str | None`——后续 Task 2 的 send 端点透传 `tx_id`;不传时行为与现状逐字节一致(默认 None)。

- [ ] **Step 1: 写失败测试**(追加到 test_gateway_mentions.py 末尾)

```python
def test_send_text_forwards_tx_id():
    class _RecordingClient(FakeNioClient):
        def __init__(self):
            super().__init__()
            self.kwargs = None

        async def room_send(self, room_id, etype, content, **kwargs):
            self.kwargs = kwargs
            return await super().room_send(room_id, etype, content, **kwargs)

    client = _RecordingClient()
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "notify", tx_id="delegate-t-1"))

    assert client.kwargs.get("tx_id") == "delegate-t-1"
    assert client.sent[0][0] == ROOM


def test_send_text_tx_id_default_none():
    client = FakeNioClient([_Member(LEAD), _Member(OCW)])
    gateway = _gateway(client)

    asyncio.run(gateway.send_text(ROOM, "plain reply"))

    # 现状回归:不传 tx_id 时 room_send 不收到 tx_id 关键字或收到 None
    assert client.sent[0][0] == ROOM
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd worker-bridge/bridge && python -m pytest tests/unit/test_gateway_mentions.py -q`
Expected: FAIL —— `send_text() got an unexpected keyword argument 'tx_id'`

- [ ] **Step 3: 最小实现**(gateway.py:202 起签名与 room_send 调用处)

```python
async def send_text(self, room_id: str, body: str, *,
                    content: dict[str, Any] | None = None,
                    tx_id: str | None = None) -> str | None:
    """发送 m.room.message（markdown 渲染 + 三层 mention），返回 event_id。

    tx_id: Matrix 客户端事务 ID（可选）。相同 tx_id 重发被 homeserver 去重
    ——委派通知等"恰好一次"语义的出站消息用它兜底重试。
    """
        # …(现有 message 构造与 m.mentions 解析不动)…
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            message,
            ignore_unverified_devices=True,
            tx_id=tx_id,
        )
```

注意:执行时先核对 matrix-nio `AsyncClient.room_send` 的关键字参数名(用 `python -c "import inspect, nio; print(inspect.signature(nio.AsyncClient.room_send))"`);若参数名不是 `tx_id`,以 nio 实际签名为准并同步修正测试断言。`FakeNioClient.room_send(**kwargs)` 吞掉多余关键字,现有测试不受影响;真 nio 在 `tx_id=None` 时行为与不传一致。

- [ ] **Step 4: 跑全量 gateway 测试确认通过**

Run: `cd worker-bridge/bridge && python -m pytest tests/unit/test_gateway_mentions.py -q`
Expected: PASS(全部,含既有用例)

- [ ] **Step 5: Commit**

```bash
git add worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py worker-bridge/bridge/tests/unit/test_gateway_mentions.py
git commit -m "feat(worker-bridge): send_text 可选 tx_id 事务幂等参数（默认 None 行为不变）"
```

---

### Task 2: bridge 出站 send HTTP 端点

**Files:**
- Modify: `worker-bridge/bridge/src/cimicode_bridge/api/routes.py`
- Test: `worker-bridge/bridge/tests/unit/test_send_api.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `send_text(..., tx_id=...)`。
- Produces: `POST /api/v1/bridge/send`,请求体 `{"room_id": str, "text": str, "txn_id": str?}`,响应 `{"ok": bool, "event_id": str?, "error": str?}`。Task 5 的 projectflow notify 客户端按此契约调用。

- [ ] **Step 1: 写失败测试**(新建 tests/unit/test_send_api.py)

```python
"""POST /api/v1/bridge/send 出站端点:沙箱 CLI 经 bridge 往指定房间发消息。

leader 委派通知的唯一出站通道;worker 形态不调用(模板禁止跨房),
端点对两种形态一视同仁——纯基础设施。
"""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cimicode_bridge.api.routes import register_routes
from cimicode_bridge.app import BridgeApp


class FakeGateway:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_text(self, room_id, text, *, content=None, tx_id=None):
        if self.fail:
            raise RuntimeError("nio down")
        self.calls.append((room_id, text, tx_id))
        return "$evt-1"


def _client(gateway) -> TestClient:
    bridge = BridgeApp()
    bridge.matrix_gateway = gateway
    api = FastAPI()
    register_routes(api, bridge)
    return TestClient(api)


def test_send_ok_returns_event_id():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "@w:dom assigned", "txn_id": "delegate-t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "event_id": "$evt-1"}
    assert gw.calls == [("!team:x", "@w:dom assigned", "delegate-t1")]


def test_send_without_txn_id_passes_none():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hello"})
    assert resp.json()["ok"] is True
    assert gw.calls[0][2] is None


def test_send_missing_room_id_or_text_rejected():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={"text": "hi"})
    assert resp.json() == {"ok": False, "error": "room_id and text are required"}
    resp = _client(gw).post("/api/v1/bridge/send",
                            json={"room_id": "!team:x", "text": "  "})
    assert resp.json()["ok"] is False


def test_send_no_reply_text_rejected():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "NO_REPLY"})
    assert resp.json()["ok"] is False
    assert "NO_REPLY" in resp.json()["error"]


def test_send_gateway_not_ready():
    bridge = BridgeApp()  # matrix_gateway 默认 None
    api = FastAPI()
    register_routes(api, bridge)
    resp = TestClient(api).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hi"})
    assert resp.json() == {"ok": False, "error": "matrix gateway not ready"}


def test_send_gateway_failure_reported():
    resp = _client(FakeGateway(fail=True)).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hi"})
    assert resp.json()["ok"] is False
    assert "nio down" in resp.json()["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd worker-bridge/bridge && python -m pytest tests/unit/test_send_api.py -q`
Expected: FAIL —— 404(端点不存在)

- [ ] **Step 3: 实现端点**(routes.py 的 register_routes 内,handle-message 端点之后追加)

```python
    @app.post("/api/v1/bridge/send")
    async def send_message(payload: dict[str, Any]) -> dict[str, Any]:
        """出站通道:沙箱 CLI(leader 委派通知)经 bridge 往指定房间发消息。

        纯基础设施:复用 gateway.send_text(m.mentions 自动解析 + markdown
        渲染),txn_id 透传 homeserver 事务幂等。契约 interface-contract §4。
        """
        room_id = str(payload.get("room_id", "")).strip()
        text = str(payload.get("text", ""))
        if not room_id or not text.strip():
            return {"ok": False, "error": "room_id and text are required"}
        if text.strip() == "NO_REPLY":
            return {"ok": False,
                    "error": "NO_REPLY must not be sent to any room"}
        gateway = bridge.matrix_gateway
        if gateway is None:
            return {"ok": False, "error": "matrix gateway not ready"}
        txn_id = str(payload.get("txn_id", "")).strip() or None
        try:
            event_id = await gateway.send_text(room_id, text, tx_id=txn_id)
        except Exception as exc:  # nio 失败必须可见,调用方据此保持 prepared 态
            return {"ok": False, "error": f"send failed: {exc}"}
        return {"ok": True, "event_id": event_id}
```

- [ ] **Step 4: 跑测试确认通过 + 既有探针测试不破**

Run: `cd worker-bridge/bridge && python -m pytest tests/unit/test_send_api.py tests/unit/test_uvicorn_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add worker-bridge/bridge/src/cimicode_bridge/api/routes.py worker-bridge/bridge/tests/unit/test_send_api.py
git commit -m "feat(worker-bridge): 出站通道 POST /api/v1/bridge/send（txn 幂等透传,NO_REPLY 拒发）"
```

---

### Task 3: 生成器 leader 化(role 白名单放行 + leader 渲染分支 + golden)

**Files:**
- Modify: `worker-bridge/bridge/generate_agent_md.py`(:85-87 模板常量、:181-191 白名单、:297-343 render_coordination、:346-353 render_environment、:407-483 main)
- Modify: `worker-bridge/bridge/tests/test_generate_agent_md.py`(:214-220 team_leader 拒绝用例改语义 + 新增 leader 用例)
- Create: `worker-bridge/bridge/tests/fixtures/runtime-leader.yaml`、`worker-bridge/bridge/tests/fixtures/golden-leader.md`
- Test: 同上 test_generate_agent_md.py

**Interfaces:**
- Produces: `parse_runtime_config` 接受 `member.role=team_leader`(要求 team 段存在);`render_coordination`/`render_environment` 对 team_leader 走独立分支;CLI `--template` 缺省时按 role 自动分派 `DEFAULT_LEADER_TEMPLATE = <generator 目录>/../template/worker-bridge-leader-agent/AGENTS.md`。Task 4 提供 leader 模板文件(本任务先以占位骨架模板让 golden 落地,Task 4 重写正文后重新生成 golden——两步各自可测)。

**设计要点(worker/standalone 分支函数体与输出零改动,既有 golden 锁死):**
- leader 版 Coordination 块内容(从 runtime.yaml team 段渲染):Team Admin(named mxid)、Team Workers(roster 中 role=worker 的成员,带 description——委派对象名册)、Coordinator Members(如有)、Team Roster(全员,排除 self,复用 `_roster_lines`)、路由规则行(assignments/TASK_COMPLETED/blockers/question → Team Room;requester reports @requester+@Team Admin 双锚)。
- leader 版 Environment:标题行 `## Environment` 同款六行,但第一行 `Leader name:`(worker 版 `Worker name:` 保持不变)。

- [ ] **Step 1: 新建 leader runtime fixture**(tests/fixtures/runtime-leader.yaml,LF)

```yaml
apiVersion: agentteams.io/v1beta1
kind: MemberRuntimeConfig
metadata:
  name: t-8a3f2c-leader
member:
  name: t-8a3f2c-leader
  matrixUserId: "@t-8a3f2c-leader:example.org"
  role: team_leader
  displayName: Tower Lead
team:
  name: t-8a3f2c
  teamRoomId: "!teamroom-8a3f2c:example.org"
  leaderDmRoomId: "!leaderdm-8a3f2c:example.org"
  admin:
    matrixUserId: "@u1234:example.org"
    displayName: Alice
  members:
  - runtimeName: t-8a3f2c-leader
    matrixUserId: "@t-8a3f2c-leader:example.org"
    role: team_leader
    displayName: Tower Lead
    description: "Decomposes incoming tasks and assigns sub-tasks to team workers"
  - runtimeName: t-8a3f2c-a9c417e2d5b83f60a1c7e9d2b4f6a8
    matrixUserId: "@t-8a3f2c-a9c417e2d5b83f60a1c7e9d2b4f6a8:example.org"
    role: worker
    displayName: Frontend Dev
    description: "Builds and reviews web UI components"
  - runtimeName: t-8a3f2c-c3e91f7a5d20b8e6f4a2c9d1b3e5f7
    matrixUserId: "@t-8a3f2c-c3e91f7a5d20b8e6f4a2c9d1b3e5f7:example.org"
    role: worker
    displayName: QA Tester
storage:
  sharedPrefix: teams/t-8a3f2c/shared
```

- [ ] **Step 2: 写失败测试**(test_generate_agent_md.py;先改 :214-220 再追加用例)

把 `test_team_leader_role_rejected` 改为(白名单收窄语义回归——coordinator 等仍拒):

```python
    def test_team_leader_role_accepted_with_team(self):
        cfg = gen.parse_runtime_config(
            "member:\n"
            "  name: l1\n"
            "  matrixUserId: '@l1:example.org'\n"
            "  role: team_leader\n"
            "team:\n"
            "  name: t1\n"
            "  members:\n"
            "  - matrixUserId: '@l1:example.org'\n"
            "    role: team_leader\n"
            "  - matrixUserId: '@w1:example.org'\n"
            "    role: worker\n")
        self.assertEqual(cfg["role"], "team_leader")

    def test_team_leader_without_team_rejected(self):
        text = ("member:\n"
                "  name: l1\n"
                "  matrixUserId: '@l1:example.org'\n"
                "  role: team_leader\n")
        with self.assertRaisesRegex(gen.GenerateError, "team_leader.*team|team.*team_leader"):
            gen.parse_runtime_config(text)
```

并在文件头部常量区追加:

```python
RUNTIME_LEADER = os.path.join(FIXTURES, "runtime-leader.yaml")
GOLDEN_LEADER = os.path.join(FIXTURES, "golden-leader.md")
LEADER_TEMPLATE = os.path.join(REPO, "template", "worker-bridge-leader-agent", "AGENTS.md")
```

在 `GoldenTest` 类追加(leader golden 比对 + CLI 自动分派):

```python
    def test_leader_golden(self):
        cfg = gen.load_runtime_config(RUNTIME_LEADER)
        self.assertEqual(cfg["role"], "team_leader")
        out = gen.generate(cfg, read(LEADER_TEMPLATE), date=DATE)
        self.assertEqual(out, read(GOLDEN_LEADER))

    def test_worker_and_leader_goldens_unchanged(self):
        # 硬锁:worker/standalone golden 与本任务前逐字节一致
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE)
        self.assertEqual(out, read(GOLDEN_TEAM))
        out = gen.generate(cfg_standalone(), read(TEMPLATE), date=DATE)
        self.assertEqual(out, read(GOLDEN_STANDALONE))
```

在 `CoordinationTest` 追加 leader 块断言:

```python
    def test_leader_block(self):
        cfg = gen.load_runtime_config(RUNTIME_LEADER)
        block = gen.render_coordination(cfg)
        self.assertIn("- **Team**: t-8a3f2c", block)
        self.assertIn("- **Team Admin**: Alice (@u1234:example.org) "
                      "(has admin authority within this team)", block)
        self.assertIn("- **Team Workers**:", block)
        self.assertIn("  - Frontend Dev "
                      "(@t-8a3f2c-a9c417e2d5b83f60a1c7e9d2b4f6a8:example.org, "
                      "worker) — Builds and reviews web UI components", block)
        # leader 自己不出现在名册里(self 排除)
        self.assertNotIn("Tower Lead (@t-8a3f2c-leader:example.org", block)
        self.assertIn("- Team Room is the collaboration surface", block)
```

在 `CliTest` 追加模板自动分派:

```python
    def test_leader_template_autodispatch(self):
        # --template 缺省时按 role 自动选 leader 模板(bridge 调用链不传 template)
        proc = self.run_cli("--runtime-config", RUNTIME_LEADER, "--date", DATE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, read(GOLDEN_LEADER))
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd worker-bridge/bridge && python -m pytest tests/test_generate_agent_md.py -q`
Expected: FAIL(team_leader 仍被拒 / leader 模板无占位符)

- [ ] **Step 4: 实现**(generate_agent_md.py)

a) 模板常量(:85-87 附近)拆分:

```python
_TEMPLATE_BASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "template")
DEFAULT_TEMPLATE = os.path.join(_TEMPLATE_BASE, "worker-bridge-agent", "AGENTS.md")
DEFAULT_LEADER_TEMPLATE = os.path.join(
    _TEMPLATE_BASE, "worker-bridge-leader-agent", "AGENTS.md")
```

b) 白名单(:181-191)改为:

```python
    has_team = bool(team.get("name"))
    role = member.get("role") or ("worker" if has_team else "standalone")
    if role == "team_leader":
        if not has_team:
            raise GenerateError(
                "member.role=team_leader without a team section — a leader "
                "only exists inside a team")
    elif role not in ("worker", "standalone"):
        # whitelist: any other role (e.g. coordinator, a real roster value)
        # has no template form — fail loud instead of rendering a malformed
        # Coordination block
        raise GenerateError(
            f"member.role={role}: unsupported role (worker/standalone/"
            "team_leader only; a coordinator member is not an agent)")
```

c) `render_coordination`(:297)在 standalone 分支后加 leader 分支(worker 分支函数体一行不动):

```python
    if cfg["role"] == "team_leader":
        lines.append(f"- **Team**: {cfg['team_name']}")
        if cfg["admin_id"]:
            lines.append(f"- **Team Admin**: "
                         f"{_named_mxid(cfg['admin_display'], cfg['admin_id'])} "
                         "(has admin authority within this team)")
        workers = [e for e in cfg["roster"]
                   if e["role"] == "worker" and e["matrix_user_id"]]
        if workers:
            lines.append("- **Team Workers**:")
            for entry in workers:
                head = (f"  - {_named_mxid(entry['display_name'] or entry['name'], "
                        f"entry['matrix_user_id'])}")
                lines.append(f"{head} — delegate assignments to them"
                             + (f"; {entry['description']}"
                                if entry["description"] else ""))
        if cfg["coordinators"]:
            lines.append("- **Coordinator Members**:")
            for coord in cfg["coordinators"]:
                lines.append(f"  - {_named_mxid(coord['display_name'], coord['matrix_user_id'])} "
                             "— can assign tasks and make decisions within the team")
        roster = _roster_lines(cfg)
        if roster:
            lines.append("- **Team Roster**:")
            lines.extend(roster)
        lines.append("- Team Room is the collaboration surface: task "
                     "assignments, worker results, blockers, and questions "
                     "all happen there")
        lines.append("- Requester-facing reports @mention both the requester "
                     "and the Team Admin by full Matrix ID (once each; skip "
                     "the duplicate when they are the same person)")
        lines.append("- Leader DM is for direct human conversation only — "
                     "it is not a report channel")
        return "\n".join(lines)
```

d) `render_environment`(:346)按 role 分派第一行标签(worker 版输出不变):

```python
def render_environment(cfg, date):
    name_label = "Leader name" if cfg["role"] == "team_leader" else "Worker name"
    section = ENV_SECTION_TEMPLATE.replace(
        "- Worker name: {worker_name}", f"- {name_label}: {{worker_name}}")
    return section.format(
        worker_name=cfg["worker_name"],
        display_name=cfg["display_name"],
        matrix_id=cfg["matrix_id"],
        team=cfg["team_name"] or "standalone",
        storage_prefix=cfg["storage_prefix"] or "agentteams",
        date=date)
```

e) `main()` 的 `--template` 默认改 `None`,加载 runtime 后按 role 分派:

```python
    ap.add_argument("--template", default=None,
                    help="source template path (default: auto by member.role — "
                         f"{DEFAULT_TEMPLATE} or {DEFAULT_LEADER_TEMPLATE})")
    # …runtime 解析后、读模板前:
        template_path = args.template or (
            DEFAULT_LEADER_TEMPLATE if runtime["role"] == "team_leader"
            else DEFAULT_TEMPLATE)
```

(`try` 块里现有 `open(args.template)` 改为 `open(template_path)`;docstring 的 Inputs 节同步一句。)

- [ ] **Step 5: 生成 golden-leader.md**

先用最小合法 leader 模板(把现 `template/worker-bridge-leader-agent/AGENTS.md` 顶部加 builtin 标记围栏,底部加 team-context 围栏与两占位符,Task 4 再重写正文):

```
<!-- agentteams-builtin-start -->
<!-- DO NOT EDIT ... (对齐 worker 模板第 1-2 行格式) -->
…(现有正文)…
<!-- agentteams-builtin-end -->

<!-- agentteams-team-context-start -->
{{COORDINATION}}
<!-- agentteams-team-context-end -->

{{ENVIRONMENT}}
```

然后:`cd worker-bridge/bridge && python generate_agent_md.py --runtime-config tests/fixtures/runtime-leader.yaml --date 2026-09-03 --output tests/fixtures/golden-leader.md`,人工 review 生成物(占位符已替换、无残留 `{{`)。

- [ ] **Step 6: 跑全量生成器测试**

Run: `cd worker-bridge/bridge && python -m pytest tests/test_generate_agent_md.py -q`
Expected: PASS(既有全部 + 新增;`git diff tests/fixtures/golden-team.md tests/fixtures/golden-standalone.md` 为空)

- [ ] **Step 7: Commit**

```bash
git add worker-bridge/bridge/generate_agent_md.py worker-bridge/bridge/tests/ worker-bridge/template/worker-bridge-leader-agent/AGENTS.md
git commit -m "feat(worker-bridge): 生成器 leader 化——role 白名单放行 team_leader+自动分派 leader 模板+leader 版 Coordination/Environment+golden（worker/standalone 渲染与 golden 逐字节不变）"
```

---

### Task 4: leader 源模板重写(桥化 copaw leader 基线)

**Files:**
- Rewrite: `worker-bridge/template/worker-bridge-leader-agent/AGENTS.md`
- Rewrite: `worker-bridge/template/worker-bridge-leader-agent/skills/task-management/SKILL.md`
- Create: `worker-bridge/template/worker-bridge-leader-agent/skills/communication/SKILL.md`
- Modify: `worker-bridge/template/worker-bridge-leader-agent/README.md`
- Test: Task 3 的 golden 测试(模板正文变化 → 重新生成 golden-leader.md 后必须全绿)

**Interfaces:**
- Consumes: Task 3 的占位符框架(`{{COORDINATION}}`/`{{ENVIRONMENT}}` 各恰好一处,builtin 标记围栏结构)。
- Produces: leader agent.md 源模板(英文,结构对齐 `template/worker-bridge-agent/AGENTS.md` 骨架:builtin 围栏 + §1-§8 正文 + team-context 围栏内 `{{COORDINATION}}` + `{{ENVIRONMENT}}`)。

**内容规格(基线 = `manager/agent/team-leader-agent/AGENTS.md` copaw 权威版,做四类桥化改写):**

1. **工具面替换**:MCP 工具(`taskflow(delegate_task)`/`filesync`/`message`)→ bash CLI(`projectflow …`/`taskflow check`/`agentteams-sync …`)。§7 的 Project/tool boundary 段保留 shell 禁令(禁手改 `shared/projects|tasks` 协议文件),工具清单改 CLI。
2. **委派序改写**(copaw §9 taskflow(delegate_task) 自动通知段的等价物):三步 `ready → delegate → delegate-commit`,`delegate-commit` **自动发送** Team Room 通知(带 @worker 全 ID 结构化 mention + txn 幂等 + event_id 记账),成功后**禁止**再手动发第二条 assignment 消息(对齐 copaw "Do NOT send a second assignment message");通知需要 `--assignee-mxid`(从 Coordination 块 Team Workers 取全 ID)与 delegate 时的 `--room-id`(= Team Room)。
3. **copaw §5 NO_REPLY 的 DM 规则 / §6 anti-loop / §9 requester 双锚路由 + Leader DM 定位段 + Authorization Failures 凭据协议 v5 全文**:语义逐条保留,仅把"taskflow(delegate_task) 自动通知"表述替换为"projectflow delegate-commit 自动通知",MCP 语气改 CLI 语气。
4. **删除**:§8 Heartbeat Entry 段(一期放弃);`HEARTBEAT.md`/`memory/` 工作区引用;coupaw §1 工作区清单改为桥版(无 HEARTBEAT/SOUL 文件——persona 经 system prompt 注入,不提文件)。

**新增桥化专属段落**(正文 §5 NO_REPLY 之后或 §9 内,措辞对齐 worker 模板 §63 行协议消息归属段风格):

```
## Cross-Room Sending

Your session reply returns to the room the triggering message came from.
That is the only room your reply text reaches. Messages that must reach a
different room are sent by tools, never by your session reply: task
assignment notifications are sent by `projectflow delegate-commit`. Do not
attempt to send Matrix messages yourself; there is no send command in your
toolchain.
```

communication skill(新建,≤80 行)内容规格:路由表(当前房直答;assignment=delegate-commit 自动;requester reports=Team Room @双锚;Leader DM=人类直接对话非报告通道)、anti-loop 三条(低信息 @ 禁令/两轮无新事即停/仅在完成、blocker、需决策时 @)、DM 项目请求处理期 NO_REPLY 规则。

task-management SKILL.md 重写要点:projectflow 全动词参考(create-project/add-tasks/plan-dag/plan-loop/ready/delegate/delegate-commit/check/record-iteration/complete-project)、委派三步与 `--assignee-mxid`/`--room-id` 用法、TASK_COMPLETED 处理流(check→accept=replan→下一波)、check 的 `effective_for_acceptance`。

README.md 更新:去掉 "starter,未部署" 与 convert_agent_md.py 过时引用,改为现行机制描述(generate_agent_md.py 自动分派、镜像内置路径、部署形态)。

- [ ] **Step 1: 重写 AGENTS.md**(按上述规格;占位符恰好各一处,`{{` 总数=2;LF 落盘)
- [ ] **Step 2: 重写 task-management/SKILL.md、新建 communication/SKILL.md、更新 README.md**
- [ ] **Step 3: 重新生成 golden 并 review**

```bash
cd worker-bridge/bridge && python generate_agent_md.py \
  --runtime-config tests/fixtures/runtime-leader.yaml --date 2026-09-03 \
  --output tests/fixtures/golden-leader.md
git diff --stat tests/fixtures/golden-leader.md
```

人工 review diff(新段落齐、协议句与 copaw 基线逐条对上)。

- [ ] **Step 4: 跑生成器测试全绿**

Run: `cd worker-bridge/bridge && python -m pytest tests/test_generate_agent_md.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add worker-bridge/template/worker-bridge-leader-agent/
git commit -m "feat(worker-bridge): leader 模板重写——copaw 基线桥化（CLI 工具面/delegate-commit 自动通知/双锚路由/凭据 v5/anti-loop,heartbeat 删除）+ communication skill 新增"
```

---

### Task 5: projectflow delegate-commit 自动通知

**Files:**
- Create: `worker-bridge/cli/projectflow/notify.py`
- Modify: `worker-bridge/cli/projectflow/projectflow.py`(:397-445 cmd_delegate/cmd_delegate_commit、:555 附近 delegate-commit 子命令参数)
- Test: `worker-bridge/cli/projectflow/tests/test_notify.py`(新建)
- Modify(同步副本): `worker-bridge/template/worker-bridge-leader-agent/skills/task-management/scripts/`(与 cli/ 五件套逐字节一致)

**Interfaces:**
- Consumes: Task 2 的 `POST /api/v1/bridge/send` 契约。
- Produces: `notify.send_assignment(room_id, text, txn_id, url) -> str`(返回 event_id,失败抛 `NotifyError`);`cmd_delegate_commit` 新参数 `--assignee-mxid`(自动通知必填)与 `--no-notify`(手动通道:配 `--event-id` 走旧记账路径)。env `BRIDGE_SEND_URL`(如 `http://agentteams-worker-l1-bridge:8081`)。

**行为规格(对齐 copaw `taskflow(delegate_task)` 的 notify→commit 原子序):**
- `delegate-commit` 默认自动通知:文本 = `@{mxid} You are assigned task **{task_id}**: {title}` + 空行 + spec.md 前 500 字(超长省略号);`txn_id = delegate-{task_id}`(homeserver 去重,重试不重发)。
- 前置校验:`meta.room_id` 非空(否则 exit 1 提示 re-delegate with --room-id);`--assignee-mxid` 以 `@` 开头含 `:`(全 ID)。
- 发送成功 → `commit_task_assignment(event_id=resp.event_id)` → push;发送失败 → 打印错误 exit 1,meta 保持 prepared(重试安全:txn 幂等)。
- 幂等短路:`meta.status == "assigned"` 且已有 event_id → 跳过发送直接打印(重试安全)。
- `--no-notify` → 旧行为原样(`--event-id` 手动记账)。
- `cmd_delegate` 的 `[next]` 打印改为:`[next] projectflow delegate-commit {pid} {tid} --assignee-mxid <mxid>`(通知已内建,不再让 agent 手发)。

- [ ] **Step 1: 写失败测试**(tests/test_notify.py;参照现有 tests 的构造方式——先读 `cli/projectflow/tests/` 现有文件确认 store/sync fake 的写法)

```python
"""delegate-commit 自动通知:txn 幂等文本、失败保持 prepared、--no-notify 旧路径。"""
import json
import os
import unittest
from unittest import mock

from projectflow import notify


class NotifyClientTest(unittest.TestCase):
    def test_send_assignment_posts_contract_body(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode("utf-8"))
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps(
                {"ok": True, "event_id": "$evt-9"}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with mock.patch.object(notify.urllib.request, "urlopen", fake_urlopen):
            event_id = notify.send_assignment(
                "!team:x", "@w:dom", "t-1", "Build UI",
                "spec " * 200, url="http://127.0.0.1:8081")
        self.assertEqual(event_id, "$evt-9")
        self.assertEqual(captured["url"], "http://127.0.0.1:8081/api/v1/bridge/send")
        body = captured["data"]
        self.assertEqual(body["room_id"], "!team:x")
        self.assertEqual(body["txn_id"], "delegate-t-1")
        self.assertIn("@w:dom You are assigned task **t-1**: Build UI",
                      body["text"])
        # spec 预览截断到 500 字
        self.assertLessEqual(len(body["text"].split("\n\n", 1)[1]), 501)

    def test_send_failure_raises(self):
        with mock.patch.object(
                notify.urllib.request, "urlopen",
                side_effect=OSError("conn refused")):
            with self.assertRaises(notify.NotifyError):
                notify.send_assignment("!team:x", "@w:dom", "t-1", "T", "s",
                                       url="http://127.0.0.1:8081")

    def test_ok_false_response_raises(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(
            {"ok": False, "error": "matrix gateway not ready"}).encode("utf-8")
        resp.__enter__.return_value = resp
        with mock.patch.object(notify.urllib.request, "urlopen",
                               return_value=resp):
            with self.assertRaisesRegex(notify.NotifyError, "not ready"):
                notify.send_assignment("!team:x", "@w:dom", "t-1", "T", "s",
                                       url="http://127.0.0.1:8081")
```

(集成级用例:用现有测试的 fake store/sync 跑 `cmd_delegate_commit` 主路径——assigned 幂等跳过、发送失败 exit 1 且 meta 仍 prepared、--no-notify 走 --event-id。执行时按现有 tests 的 fake 具体形态补全,断言点如上三句。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd worker-bridge/cli/projectflow && python -m pytest tests/test_notify.py -q`
Expected: FAIL —— `No module named 'projectflow.notify'`

- [ ] **Step 3: 实现 notify.py**

```python
"""委派通知出站客户端:POST bridge /api/v1/bridge/send(契约 interface-contract §4)。

leader 沙箱内 projectflow 唯一的网络出口;零第三方依赖(urllib)。
"""
from __future__ import annotations

import json
import os
import urllib.request

SPEC_PREVIEW_CHARS = 500


class NotifyError(RuntimeError):
    """通知发送失败——调用方保持任务 prepared 态,重试安全(txn 幂等)。"""


def bridge_send_url() -> str:
    url = os.environ.get("BRIDGE_SEND_URL", "").rstrip("/")
    if not url:
        raise NotifyError(
            "BRIDGE_SEND_URL is not set — the bridge send endpoint is "
            "required for assignment notifications")
    return url


def build_assignment_text(mxid: str, task_id: str, title: str, spec: str) -> str:
    preview = spec.strip()[:SPEC_PREVIEW_CHARS]
    if len(spec.strip()) > SPEC_PREVIEW_CHARS:
        preview += "…"
    head = f"@{mxid} You are assigned task **{task_id}**: {title}"
    return f"{head}\n\n{preview}" if preview else head


def send_assignment(room_id: str, mxid: str, task_id: str, title: str,
                    spec: str, *, url: str | None = None) -> str:
    """发送委派通知并返回 Matrix event_id;任何失败抛 NotifyError。"""
    endpoint = (url or bridge_send_url()) + "/api/v1/bridge/send"
    body = json.dumps({
        "room_id": room_id,
        "text": build_assignment_text(mxid, task_id, title, spec),
        "txn_id": f"delegate-{task_id}",
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise NotifyError(f"assignment notification failed: {exc}") from exc
    if not payload.get("ok"):
        raise NotifyError(
            f"assignment notification rejected: {payload.get('error')}")
    event_id = payload.get("event_id")
    if not event_id:
        raise NotifyError("bridge send returned no event_id")
    return str(event_id)
```

- [ ] **Step 4: 改造 cmd_delegate_commit / cmd_delegate / 子命令参数**

`cmd_delegate_commit`(projectflow.py:427)在 pull 之后、commit 之前插入自动通知块:

```python
def cmd_delegate_commit(store, args, sync) -> int:
    _log("pull", project=args.project_id, task=args.task_id,
         backend=sync.describe())
    sync.pull_project(args.project_id)
    sync.pull_task(args.task_id)
    meta = store.read_task_meta(args.task_id)
    event_id = args.event_id
    if not getattr(args, "no_notify", False):
        if meta.status == "assigned" and meta.event_id:
            print(f"[commit] {meta.task_id}: already assigned "
                  f"(event_id={meta.event_id}) — notification skipped")
            print(meta_json(meta))
            return 0
        if not (meta.room_id or "").strip():
            print("error: task has no room_id — re-delegate with --room-id",
                  file=sys.stderr)
            return 1
        if not (getattr(args, "assignee_mxid", "") or "").strip():
            print("error: --assignee-mxid is required for auto-notification "
                  "(full Matrix ID from the Coordination Team Workers list); "
                  "or pass --no-notify --event-id for the manual path",
                  file=sys.stderr)
            return 1
        spec = store.read_task_spec(args.task_id)
        from projectflow import notify as _notify  # 延迟导入,--no-notify 路径零网络面
        try:
            event_id = _notify.send_assignment(
                meta.room_id, args.assignee_mxid.lstrip("@"),
                meta.task_id, meta.task_title, spec)
        except _notify.NotifyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    snap = _snapshot(_task_paths(store, args.task_id))
    meta = commit_task_assignment(
        store, project_id=args.project_id, task_id=args.task_id,
        event_id=event_id)
    # …(_log/_push_with_rollback/打印 段保持现状,打印追加 notification 标记)
```

注:`send_assignment` 的 mxid 参数在 notify.py 内统一 `@` 前缀(text 里 `@{mxid}`),CLI 侧 `lstrip("@")` 容错。子命令参数(:555 附近)加:

```python
    p.add_argument("--assignee-mxid", default="",
                   help="assigned worker's full Matrix ID (auto-notification)")
    p.add_argument("--no-notify", action="store_true",
                   help="manual path: no auto-notification, use --event-id")
```

`cmd_delegate` 的 `[next]` 打印(:422-423)改为:

```python
    print("[next] run "
          f"'projectflow delegate-commit {args.project_id} {meta.task_id} "
          f"--assignee-mxid <full-mxid>' — it notifies the worker "
          "automatically; do NOT send the assignment yourself")
```

- [ ] **Step 5: 跑 projectflow 全量测试**

Run: `cd worker-bridge/cli/projectflow && python -m pytest tests/ -q`
Expected: PASS(既有 20 个不受影响——它们走 --event-id 或无 notify 参数;若既有用例因新默认行为失败,检查其是否缺 --no-notify,补参数而非改逻辑)

- [ ] **Step 6: 同步 scripts 五件套到模板目录并比对逐字节一致**

```bash
cp worker-bridge/cli/projectflow/{projectflow.py,projectflow_core.py,mc_sync.py,agentteams_log.py,notify.py} \
   worker-bridge/template/worker-bridge-leader-agent/skills/task-management/scripts/
diff -r worker-bridge/cli/projectflow/projectflow.py \
     worker-bridge/template/worker-bridge-leader-agent/skills/task-management/scripts/projectflow.py
```

- [ ] **Step 7: Commit**

```bash
git add worker-bridge/cli/projectflow/ worker-bridge/template/worker-bridge-leader-agent/skills/task-management/scripts/
git commit -m "feat(worker-bridge): projectflow delegate-commit 自动通知——bridge send 端点+txn delegate-{task_id} 幂等+失败保持 prepared+--no-notify 手动通道"
```

---

### Task 6: 镜像资产(bridge 模板打包 + cimicode-sandbox leader 变体)

**Files:**
- Modify: `worker-bridge/bridge/Dockerfile`(COPY leader 源模板,与 worker 模板同目录结构)
- Modify: `worker-bridge/cimicode-sandbox/Dockerfile`(multi-stage 增加 leader target)
- Modify: `Makefile`(新增 build 目标,参照既有 build-opencode-runtime 模式)

**Interfaces:**
- Consumes: Task 3 的 `DEFAULT_LEADER_TEMPLATE` 相对路径(`generator 目录/../template/worker-bridge-leader-agent/AGENTS.md` → 镜像内 `/opt/template/worker-bridge-leader-agent/AGENTS.md`)。
- Produces: 镜像 `agentteams/worker-bridge`(含两模板,role 自动分派);镜像 `agentteams/cimicode-sandbox:leader`(skills 替换为 leader 版五件 skill;`:worker`/默认 target 与现状字节一致)。**105 stateless 平台为 leader 沙箱选 leader 变体镜像——部署层约定,代码零改动。**

- [ ] **Step 1: bridge/Dockerfile**——在现有 worker 模板 COPY 行旁加一行 leader 模板 COPY(目标路径保持 `template/worker-bridge-leader-agent/AGENTS.md` 相对结构;执行时先读该 Dockerfile 确认 COPY 布局再改)。
- [ ] **Step 2: cimicode-sandbox/Dockerfile**——读现有文件,把 skills COPY 段(:49-60 附近)参数化:现有内容归入 `worker` stage(字节不变),复制出 `leader` stage 仅改 skills 源路径为 `template/worker-bridge-leader-agent/skills/`,并把 :52 的失实注释改为实况;两个 stage 各自 `FROM … AS worker|leader`。
- [ ] **Step 3: Makefile 加目标**(名字 `build-cimicode-sandbox-leader`,docker build `--target leader`,tag `agentteams/cimicode-sandbox-leader`;版本变量对齐既有目标)。
- [ ] **Step 4: 本地构建冒烟**(若本机可 build):

```bash
make build-worker-bridge 2>/dev/null || docker build -f worker-bridge/bridge/Dockerfile -t agentteams/worker-bridge:dev worker-bridge/
docker build -f worker-bridge/cimicode-sandbox/Dockerfile --target worker -t agentteams/cimicode-sandbox:dev-check worker-bridge/
docker build -f worker-bridge/cimicode-sandbox/Dockerfile --target leader -t agentteams/cimicode-sandbox:leader-dev worker-bridge/
docker run --rm agentteams/worker-bridge:dev ls /opt/template/worker-bridge-leader-agent/AGENTS.md
docker run --rm agentteams/cimicode-sandbox:leader-dev ls /opt/agentteams/skills/task-management/scripts/projectflow.py
```

Expected: 两文件均存在;`worker` target 的镜像层 diff 与改前一致(仅 Dockerfile 指令序变化不影响内容时)。

- [ ] **Step 5: Commit**

```bash
git add worker-bridge/bridge/Dockerfile worker-bridge/cimicode-sandbox/Dockerfile Makefile
git commit -m "feat(worker-bridge): 镜像资产——bridge 镜像打包 leader 源模板+cimicode-sandbox leader 变体（skills 替换式,worker target 字节不变）"
```

---

### Task 7: 契约与文档

**Files:**
- Modify: `worker-bridge/contract/interface-contract.md`(§1 env 表加 `BRIDGE_SEND_URL`;§4 增补出站端点契约;§5.4 delegate-commit 语义更新;§6 生成器 leader 形态;变更记录加 v2.7)
- Modify: `worker-bridge/README.md`(§4 模板表 leader 行更新"已接线";§5.3 leader 表述;§6 测试计数)
- Modify: `changelog/current.md`(按仓库 changelog 政策,一条 bullet 概括本批)

**要点:**
- §4 增补文本必须包含:端点路径与请求/响应体(Task 2 契约)、`txn_id` 幂等语义、"session reply 只达触发房,跨房必经工具"的路由纪律、NO_REPLY 拒发。
- §5.4 更新:delegate-commit 默认自动通知、`--assignee-mxid`/`--no-notify`、幂等短路、失败保持 prepared;"notify 由 agent 自发"的旧描述删除。
- v2.7 变更记录行:一句话概括(出站端点+生成器 leader+自动通知+leader 模板接线)。

- [ ] **Step 1: 按要点改三处文档**
- [ ] **Step 2: Commit**

```bash
git add worker-bridge/contract/interface-contract.md worker-bridge/README.md changelog/current.md
git commit -m "docs(worker-bridge): 契约 v2.7——§4 出站端点/§5.4 自动通知/§6 生成器 leader 形态+BRIDGE_SEND_URL env+changelog"
```

---

### Task 8: 全量回归与 worker 零变化审计

**Files:** 无新改动(验证任务)

- [ ] **Step 1: bridge 全量测试**

```bash
cd worker-bridge/bridge && python -m pytest tests/ -q
```
Expected: 全 PASS(既有 146 + 新增)。

- [ ] **Step 2: projectflow 全量测试**

```bash
cd worker-bridge/cli/projectflow && python -m pytest tests/ -q
```
Expected: 全 PASS。

- [ ] **Step 3: worker 零变化审计**(关键红线)**

```bash
git diff --stat <P0_起点commit>..HEAD -- \
  worker-bridge/template/worker-bridge-agent \
  worker-bridge/cli/taskflow \
  worker-bridge/bridge/tests/fixtures/golden-team.md \
  worker-bridge/bridge/tests/fixtures/golden-standalone.md \
  worker-bridge/bridge/tests/fixtures/runtime.yaml \
  worker-bridge/bridge/tests/fixtures/runtime-standalone.yaml \
  worker-bridge/bridge/src/cimicode_bridge/runtime \
  worker-bridge/bridge/src/cimicode_bridge/matrix/filter.py \
  worker-bridge/bridge/src/cimicode_bridge/config.py \
  worker-bridge/operator agentteams-controller manager
```
Expected: 输出为空(零改动)。`<P0_起点commit>` 取本计划 Task 1 之前的 HEAD(ad49e516)。

- [ ] **Step 4: leader 渲染冒烟(本地模拟 runtime=worker-bridge 的 leader)**

```bash
cd worker-bridge/bridge && python generate_agent_md.py \
  --runtime-config tests/fixtures/runtime-leader.yaml | head -30
```
Expected: 输出含 `Team Leader` 角色正文 + leader 版 Coordination(`Team Admin`/`Team Workers`/`Team Roster`)+ `Leader name:` 环境行,无残留 `{{`。

- [ ] **Step 5: 汇总报告**(commit hash 列表、测试计数、审计结果),向用户汇报 P0 完成、105 E2E 待部署验证。

---

## Self-Review 记录

- 规格覆盖:四项拍板决策(出站 HTTP 端点/工具内自动通知/heartbeat 放弃/镜像内置)全部有对应 Task(2+5 / 5 / Task4 删除段 / 3+6);P0 排除项在 Global Constraints 明示。
- 类型一致:`send_text(..., tx_id=)`(T1)与 T2 端点透传、T5 无关(CLI 走 HTTP);`send_assignment(room_id, mxid, task_id, title, spec, url)` 在 T5 测试与实现一致;`DEFAULT_LEADER_TEMPLATE` 命名 T3 内一致。
- 无占位符:模板正文(Task 4)与 Dockerfile(Task 6)两处因属"重写/参照现有文件结构"类工作,给出内容规格+关键段落全文+参照物路径,未留 TBD。
- 已知依赖风险:matrix-nio `room_send` 的 txn 关键字参数名以运行时 `inspect.signature` 为准(T1 Step 3 注明)。
