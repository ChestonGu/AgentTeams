#!/usr/bin/env python3
"""Inspect t1-pipeline: recent room messages + task meta.json."""
import json
import subprocess
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
tok = json.loads(run_or_none := urllib.request.urlopen(urllib.request.Request(
    f"{base}/_matrix/client/v3/login", method="POST",
    data=json.dumps({"type": "m.login.password",
                     "identifier": {"type": "m.id.user", "user": user},
                     "password": pw}).encode(),
    headers={"Content-Type": "application/json"}), timeout=30).read())["access_token"]

room = run("kubectl", "-n", NS, "get", "team", "t1-pipeline",
           "-o", "jsonpath={.status.teamRoomID}").strip()
print("room:", room)
msgs = urllib.request.urlopen(urllib.request.Request(
    f"{base}/_matrix/client/v3/rooms/{urllib.parse.quote(room, safe='')}/messages?dir=b&limit=15",
    headers={"Authorization": f"Bearer {tok}"}), timeout=30).read()
for c in json.loads(msgs).get("chunk", []):
    if c.get("type") != "m.room.message":
        continue
    body = (c.get("content", {}).get("body") or "").replace("\n", " ")[:100]
    print(" ", c.get("sender", "").split(":")[0], "|", body)

mc_pod = run("kubectl", "-n", NS, "get", "pod", "-o", "name").strip()
mc_pod = [l.split("/")[-1] for l in mc_pod.splitlines() if "minio" in l][0]


def mc(*args):
    return run("kubectl", "-n", NS, "exec", mc_pod, "--", "/usr/bin/mc", *args)


ak = run("kubectl", "-n", NS, "get", "secret", "agentteams-oct-minio",
         "-o", "jsonpath={.data.MINIO_ROOT_USER}").strip()
import base64
ak = base64.b64decode(ak).decode()
sk = base64.b64decode(run("kubectl", "-n", NS, "get", "secret", "agentteams-oct-minio",
                          "-o", "jsonpath={.data.MINIO_ROOT_PASSWORD}").strip()).decode()
subprocess.run(["kubectl", "-n", NS, "exec", mc_pod, "--", "/usr/bin/mc", "alias", "set",
                "root", "http://127.0.0.1:9000", ak, sk], capture_output=True, timeout=60)
print("== meta.json:")
print(mc("cat", "root/agentteams-storage/teams/t1-pipeline/shared/tasks/multimd-20260907-032515-01/meta.json"))
