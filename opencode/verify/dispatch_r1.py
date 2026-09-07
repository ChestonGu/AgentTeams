#!/usr/bin/env python3
"""Dispatch round-1 project briefs to the three team leaders (runs on 105).

Mention format matches copaw's _was_mentioned (matrix/channel.py): structured
m.mentions.user_ids + matrix.to formatted_body, so the leader actually gets
triggered AND the mention renders as a real ping in Element/dashboard.

Briefs deliberately do NOT assign tasks to specific workers: the leader owns
decomposition, member selection and sequencing — that collaboration flow is
exactly what this round verifies.
Credentials come from helm values at runtime and are never printed.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NS = "opencode-team-test"
DOMAIN = "agentteams-oct-synapse.opencode-team-test.svc.cluster.local"

TEAMS = {
    "t1-pipeline": ("pl-lead", """@pl-lead 项目委托：用 Python 开发一个 Markdown→HTML 静态站点生成器（项目名 multimd）。

目标产物：core/md_parser.py（解析：标题/段落/列表/代码块/行内粗斜体）、core/renderer.py（渲染为语义化 HTML）、各自配套单测、一份端到端集成测试与测试报告 report.md。

约束：解析与渲染必须解耦——渲染端只允许依赖解析端公开接口，接口约定要有文档；各环节产出在团队共享工作区可追溯。

由你决策拆分成几个任务、派给谁、按什么顺序推进（建议体现依赖关系：下游开工前先读上游产出确认接口）。全程走 taskflow 状态流转，全部完成后在房间给出项目总结与各任务状态核对结果。"""),

    "t2-parallel": ("pa-lead", """@pa-lead 项目委托：开发一个日志分析工具集（项目名 logkit），包含三个独立 CLI 组件与一次集成。

目标产物：①日志级别统计工具（含高频关键词 Top10）②按日期/级别过滤工具 ③HTML 报告生成工具，外加一个把三者串联跑通的 pipeline demo。

约束：三个组件互不依赖、可并行开发，成员间文件命名不得冲突；每个组件自带 fixture 日志与最小自测。

由你决策任务拆分与成员分配（并行任务建议同时派出以压缩总时长），收齐后安排集成验证并实际跑 demo。全程 taskflow，完成后在房间汇总各组件与集成的状态。"""),

    "t3-review": ("rv-lead", """@rv-lead 项目委托：开发一个 JSON 配置校验器（项目名 jsoncheck），并执行完整的 实现→审查→返工→复审 质量循环。

目标产物：validator.py（validate(config, schema) -> list[str]，空列表=通过；支持 type/required/enum/嵌套对象校验）、配套单测与样例 schema、审查报告 review.md。

要求：实现与审查必须是不同成员；审查必须实质挑毛病（至少 2-3 个真实问题：边界条件/异常处理/测试覆盖缺口方向），有问题就打回要求返工，复审通过才算项目完成；打回与通过都要在房间说明理由。

由你决策任务拆分、成员角色与流程编排。全程 taskflow，最终以复审通过收尾并核对全部任务状态。"""),
}


def run(*cmd: str) -> str:
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {out.stderr.decode()[:200]}")
    return out.stdout.decode()


def http(url: str, method: str, token: str = "", body: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode())


def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_content(brief: str, leader: str, leader_mxid: str) -> dict:
    """body keeps the human-readable @short-name; formatted_body wraps it in a
    matrix.to link; m.mentions makes it a structured mention."""
    tag = f"@{leader}"
    rest = brief[len(tag):] if brief.startswith(tag) else brief
    link = f'<a href="https://matrix.to/#/{leader_mxid}">{tag}</a>'
    return {
        "msgtype": "m.text",
        "body": brief,
        "format": "org.matrix.custom.html",
        "formatted_body": link + html_escape(rest).replace("\n", "<br>"),
        "m.mentions": {"user_ids": [leader_mxid]},
    }


def main() -> int:
    vals = run("helm", "get", "values", "agentteams-oct", "-n", NS)
    user = pw = ""
    for ln in vals.splitlines():
        if ln.strip().startswith("adminUser:"):
            user = ln.split(":", 1)[1].strip()
        elif ln.strip().startswith("adminPassword:"):
            pw = ln.split(":", 1)[1].strip()
    if user.startswith("@"):
        user = user[1:].split(":")[0]
    mxid = f"@{user}:{DOMAIN}"

    svc_ip = run("kubectl", "-n", NS, "get", "svc", "agentteams-oct-synapse",
                 "-o", "jsonpath={.spec.clusterIP}").strip()
    base = f"http://{svc_ip}:8008"

    tok = http(f"{base}/_matrix/client/v3/login", "POST", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": pw,
    }).get("access_token", "")
    if not tok:
        print("admin login failed")
        return 1

    for team, (leader, brief) in TEAMS.items():
        room = run("kubectl", "-n", NS, "get", "team", team,
                   "-o", "jsonpath={.status.teamRoomID}").strip()
        if not room.startswith("!"):
            print(f"{team}: no teamRoomID, skip")
            continue
        try:
            joined = http(f"{base}/_synapse/admin/v1/join/{urllib.parse.quote(room, safe='')}",
                          "POST", tok, {"user_id": mxid})
            print(f"{team}: admin joined {joined.get('room_id', '?')}")
        except urllib.error.HTTPError as exc:
            # "already in the room" is fine — the controller invites the actor
            # (admin) at team creation; re-joining just 403s.
            if b"already in the room" in exc.read():
                print(f"{team}: admin already in room")
            else:
                raise
        leader_mxid = f"@{leader}:{DOMAIN}"
        content = build_content(brief, leader, leader_mxid)
        resp = http(f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                    f"/send/m.room.message/oct-r1b-{int(time.time())}", "PUT", tok, content)
        print(f"{team}: brief -> {leader} (structured mention) event={resp.get('event_id', '?')}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
