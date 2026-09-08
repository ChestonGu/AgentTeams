#!/usr/bin/env python3
"""Send a structured-mention nudge to a worker in a team room (runs on 105).

Usage: python3 remind.py <team> <worker> <message...>
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


def main() -> int:
    team, worker, msg = sys.argv[1], sys.argv[2], sys.argv[3]
    vals = run("helm", "get", "values", "agentteams-oct", "-n", NS)
    user = pw = ""
    for ln in vals.splitlines():
        if ln.strip().startswith("adminUser:"):
            user = ln.split(":", 1)[1].strip()
        elif ln.strip().startswith("adminPassword:"):
            pw = ln.split(":", 1)[1].strip()
    if user.startswith("@"):
        user = user[1:].split(":")[0]

    svc_ip = run("kubectl", "-n", NS, "get", "svc", "agentteams-oct-synapse",
                 "-o", "jsonpath={.spec.clusterIP}").strip()
    base = f"http://{svc_ip}:8008"
    tok = http(f"{base}/_matrix/client/v3/login", "POST", body={
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user},
        "password": pw,
    }).get("access_token", "")
    room = run("kubectl", "-n", NS, "get", "team", team,
               "-o", "jsonpath={.status.teamRoomID}").strip()
    worker_mxid = f"@{worker}:{DOMAIN}"
    tag = f"@{worker}"
    rest = msg[len(tag):] if msg.startswith(tag) else " " + msg
    esc = rest.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    content = {
        "msgtype": "m.text",
        "body": msg,
        "format": "org.matrix.custom.html",
        "formatted_body": f'<a href="https://matrix.to/#/{worker_mxid}">{tag}</a>' + esc.replace("\n", "<br>"),
        "m.mentions": {"user_ids": [worker_mxid]},
    }
    resp = http(f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}"
                f"/send/m.room.message/oct-nudge-{int(time.time())}", "PUT", tok, content)
    print(f"{team}: nudge -> {worker} event={resp.get('event_id', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
