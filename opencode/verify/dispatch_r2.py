#!/usr/bin/env python3
"""Dispatch round-2 project briefs to the three team leaders (runs on 105).

Same structured-mention format as dispatch_r1 (matches copaw's
_was_mentioned): m.mentions.user_ids + matrix.to formatted_body. Briefs give
only the overall goal — leaders own decomposition and member assignment.
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
    "t4-iterate": ("it-lead", """@it-lead 项目委托：开发一个命令行待办管理器（项目名 taskcli），并完成三轮迭代交付。

迭代规划：
- v1：todo.json 的增删查（add/list/done），含单测。
- v2：增加优先级与 due 日期、list 过滤排序，向后兼容 v1 数据文件，含单测。
- v3：增加 todo.txt 纯文本导入导出与错误容错，含单测。

要求：每轮交付后由你组织评审（亲自运行验证），评审发现的改进点作为下一轮需求下达；三轮全部通过评审后输出 CHANGELOG.md 汇总演进史。

由你决策任务拆分与成员分配（同轮内可并行）。全程 taskflow 状态流转，最终核对全部任务状态并给出三轮迭代总结。"""),

    "t5-fullstack": ("fs-lead", """@fs-lead 项目委托：交付一个个人书签管理服务（项目名 bookmarkd）的完整小项目。

目标产物：
- 后端 api.py：REST 接口（书签 CRUD + 按标签查询），Flask 或纯标准库均可，数据存 JSON 文件。
- 前端 index.html + app.js：单页界面，能列出/添加/删除书签，调用后端接口。
- 文档 README.md：含启动步骤、接口说明与示例。
- 集成验证：实际启动服务并验证前后端联通。

要求：先定接口契约（前后端共同遵守并落盘），再并行开发；文档作者必须实际按 README 步骤启动验证过才能交付。

由你决策任务拆分、成员分配与编排顺序。全程 taskflow，完成后核对全部任务状态并给出项目总结。"""),

    "t6-stress": ("st-lead", """@st-lead 项目委托：一次性交付文本处理工具集（项目名 textkit），至少 6 个相互独立的 CLI 小工具，外加一个汇总验证。

工具建议方向（具体由你定夺，可增删）：词频统计、大小写转换、行数统计、去重排序、查找替换、CSV 转markdown 表格等。每个工具：独立脚本 + 自带 fixture 输入 + 最小自测。

要求：工具之间零依赖、可完全并行开发；最后安排一次汇总验证（全部工具实际各跑一遍）并输出 SUMMARY.md 登记每个工具的验证结果。

由你决策拆分粒度与成员分配（建议一次并行派出以验证并发承接能力）。全程 taskflow，完成后逐个核对任务状态并汇总成功/失败统计。"""),
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
            if b"already in the room" in exc.read():
                print(f"{team}: admin already in room")
            else:
                raise
        leader_mxid = f"@{leader}:{DOMAIN}"
        content = build_content(brief, leader, leader_mxid)
        resp = http(f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                    f"/send/m.room.message/oct-r2b-{int(time.time())}", "PUT", tok, content)
        print(f"{team}: brief -> {leader} (structured mention) event={resp.get('event_id', '?')}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
