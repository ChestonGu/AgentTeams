#!/usr/bin/env python3
"""Round-3 regression dispatch (runs on 105).

r3a-verify: configkit — the brief REQUIRES workers to read the
  task-management skill via the skill tool before taskflow (verifies the
  startup-race fix); v3-w1 carries pre-seeded user customization files
  (AGENTS.md tail / TOOL.md / IDENTITY.md) that must surface in its replies.
r3b-quick: timetool — plain fast round on the new images.

Structured mentions (m.mentions.user_ids + matrix.to formatted_body), same
as dispatch_r2. Briefs give only the overall goal.
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
    "r3a-verify": ("v3-lead", """@v3-lead 项目委托：开发一个配置读取小工具（项目名 configkit）。

目标产物：
- configkit.py：读取 INI 配置文件，提供 get(section,key)、sections()、keys(section) 三个函数，支持默认值与类型标注（str/int/float/bool）。
- config.md：使用说明含 3 个示例。
- 单元测试覆盖类型转换与缺省路径，全部通过。

执行要求（重要，请原样传达给每位成员）：
1. 成员动手前必须先用 skill 工具读取 task-management skill 的说明，再按其中指导执行 taskflow ack/submit——不要凭记忆直接敲 taskflow 命令。
2. 拆分与分配由你决定；交付后你用 taskflow check 验收并核对交付物存在。
3. 最终给出项目总结。"""),

    "r3b-quick": ("v4-lead", """@v4-lead 项目委托：交付一个时间小工具（项目名 timetool）。

目标产物：
- timetool.py：CLI 支持 now（ISO8601 本地时间）、fmt <pattern>（strftime）、until <ISO>（距今年月日），纯标准库。
- README.md 简要用法。
- 单元测试≥4 个并全部通过。

执行要求：成员动手前必须先用 skill 工具读取 task-management skill 再执行 taskflow 流程；交付后你用 taskflow check 验收并完结项目，给出简短总结。"""),
}


def run(*cmd):
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: {out.stderr.decode()[:200]}")
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
                    f"/send/m.room.message/oct-r3-{int(time.time())}", "PUT", tok, content)
        print(f"{team}: brief -> {leader} (structured mention) event={resp.get('event_id', '?')}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
