#!/usr/bin/env python3
"""Purge every non-system synapse room so element-web shows a clean slate.

Shuts down all rooms via the admin API (block=false, purge=true): members
are removed/kicked, rooms vanish from directory and account room lists.
Usage (on 105): python3 purge_rooms.py [--dry]
"""
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
headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

dry = "--dry" in sys.argv
rooms = json.loads(urllib.request.urlopen(
    urllib.request.Request(f"{base}/_synapse/admin/v1/rooms?limit=500", headers=headers),
    timeout=30).read())
purged = skipped = 0
for room in rooms.get("rooms", []):
    rid = room.get("room_id", "")
    name = (room.get("name") or "").replace("\n", " ")[:50]
    if dry:
        print(f"would purge {rid} ({name})")
        continue
    try:
        req = urllib.request.Request(
            f"{base}/_synapse/admin/v1/rooms/{urllib.parse.quote(rid, safe='')}",
            method="DELETE",
            data=json.dumps({"block": False, "purge": True}).encode(),
            headers=headers)
        urllib.request.urlopen(req, timeout=60).read()
        print(f"purged {rid} ({name})")
        purged += 1
    except Exception as exc:
        print(f"SKIP {rid} ({name}): {str(exc)[:120]}")
        skipped += 1
print(f"done: purged={purged} skipped={skipped} (dry={dry})")
