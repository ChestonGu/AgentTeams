#!/usr/bin/env python3
"""Tail a team room's recent messages (runs on 105). Usage: room_tail.py <team> [limit]"""
import json
import subprocess
import sys
import urllib.parse
import urllib.request

NS = "opencode-team-test"


def run(*cmd):
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{cmd[0]}: {out.stderr.decode()[:200]}")
    return out.stdout.decode()


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
req = urllib.request.Request(f"{base}/_matrix/client/v3/login", method="POST",
                             data=json.dumps({"type": "m.login.password",
                                              "identifier": {"type": "m.id.user", "user": user},
                                              "password": pw}).encode(),
                             headers={"Content-Type": "application/json"})
tok = json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]

team = sys.argv[1]
limit = sys.argv[2] if len(sys.argv) > 2 else "10"
room = run("kubectl", "-n", NS, "get", "team", team, "-o", "jsonpath={.status.teamRoomID}").strip()
msgs = urllib.request.urlopen(urllib.request.Request(
    f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}/messages?dir=b&limit={limit}",
    headers={"Authorization": f"Bearer {tok}"}), timeout=30).read()
for c in json.loads(msgs).get("chunk", []):
    if c.get("type") != "m.room.message":
        continue
    body = (c.get("content", {}).get("body") or "").replace("\n", " ")[:130]
    ts = c.get("origin_server_ts", 0)
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M:%S")
    print(f"{when} {c.get('sender','').split(':')[0]:12} | {body}")
