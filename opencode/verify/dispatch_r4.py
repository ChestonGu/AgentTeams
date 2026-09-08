#!/usr/bin/env python3
"""Round-4 natural-flow dispatch (runs on 105). Brief mentions no skills."""
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
    "r4-natural": ("v5-lead", """@v5-lead 项目委托：交付一个单位换算工具（项目名 unitconv）。

目标产物：
- unitconv.py：CLI 支持 长度（m/km/mi/ft）、重量（kg/g/lb）、温度（c/f/k）三类互转，精度可配置。
- README.md 用法示例。
- 单元测试覆盖精度与边界（如绝对零度），全部通过。

由你决定拆分与分派；验收后完结项目并给出总结。"""),
}


def run(*cmd):
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: {out.stderr.decode()[:200]}")
    return out.stdout.decode()


def http(url, method, token="", body=None):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode())


def build_content(brief, leader, leader_mxid):
    tag = f"@{leader}"
    rest = brief[len(tag):] if brief.startswith(tag) else brief
    link = f'<a href="https://matrix.to/#/{leader_mxid}">{tag}</a>'
    esc = rest.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return {
        "msgtype": "m.text",
        "body": brief,
        "format": "org.matrix.custom.html",
        "formatted_body": link + esc.replace("\n", "<br>"),
        "m.mentions": {"user_ids": [leader_mxid]},
    }


def main():
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
    svc = run("kubectl", "-n", NS, "get", "svc", "agentteams-oct-synapse",
              "-o", "jsonpath={.spec.clusterIP}").strip()
    base = f"http://{svc}:8008"
    tok = http(f"{base}/_matrix/client/v3/login", "POST", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": pw}).get("access_token", "")
    for team, (leader, brief) in TEAMS.items():
        room = run("kubectl", "-n", NS, "get", "team", team,
                   "-o", "jsonpath={.status.teamRoomID}").strip()
        if not room.startswith("!"):
            print(f"{team}: no teamRoomID, skip")
            continue
        try:
            http(f"{base}/_synapse/admin/v1/join/{urllib.parse.quote(room, safe='')}",
                 "POST", tok, {"user_id": mxid})
            print(f"{team}: admin in room")
        except urllib.error.HTTPError as exc:
            if b"already in the room" not in exc.read():
                raise
        content = build_content(brief, leader, f"@{leader}:{DOMAIN}")
        resp = http(f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                    f"/send/m.room.message/oct-r4-{int(time.time())}", "PUT", tok, content)
        print(f"{team}: brief -> {leader} event={resp.get('event_id', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
