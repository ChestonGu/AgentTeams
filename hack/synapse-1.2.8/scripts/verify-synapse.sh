#!/usr/bin/env bash
# Verify the reference Synapse homeserver: CS API up, admin login + admin REST
# working, and the declarative AppService registration active.
set -euo pipefail

NS="${NS:-agentos}"
RELEASE="${RELEASE:-agentteams-128}"
SYNAPSE_NAME="${RELEASE}-synapse"
SERVER_NAME="${SERVER_NAME:-${SYNAPSE_NAME}.${NS}.svc.cluster.local}"
URL="http://${SYNAPSE_NAME}.${NS}.svc.cluster.local:8008"

POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name="$SYNAPSE_NAME" -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "no Synapse pod found (label app.kubernetes.io/name=$SYNAPSE_NAME)" >&2
  exit 1
fi

rt_data() {
  kubectl -n "$NS" get secret "${RELEASE}-runtime-env" -o jsonpath="{.data.$1}" | base64 -d
}
ADMIN_USER="$(rt_data AGENTTEAMS_ADMIN_USER)"
ADMIN_PASSWORD="$(rt_data AGENTTEAMS_ADMIN_PASSWORD)"
AS_TOKEN="$(rt_data AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN)"
SENDER_LOCALPART="${SENDER_LOCALPART:-agentteams-controller}"

echo "==> running verification inside pod $POD"
kubectl -n "$NS" exec -i "$POD" -- env \
  ADMIN_USER="$ADMIN_USER" \
  ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  AS_TOKEN="$AS_TOKEN" \
  SENDER_LOCALPART="$SENDER_LOCALPART" \
  SERVER_NAME="$SERVER_NAME" \
  SYNAPSE_URL="$URL" \
  python3 - <<'PY'
import json, os, sys, urllib.error, urllib.request

url = os.environ["SYNAPSE_URL"].rstrip("/")
server_name = os.environ["SERVER_NAME"]
failures = []

def call(method, path, body=None, token=None, token_as_bearer=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")

# 1. CS API version
status, body = call("GET", "/_matrix/client/versions")
version = body.get("server_version") or ",".join(body.get("versions", [])[-3:])
print(f"[1] CS API           : HTTP {status} versions={version}")
if status != 200:
    failures.append("versions endpoint not OK")

# 2. Admin login
status, body = call("POST", "/_matrix/client/v3/login", {
    "type": "m.login.password",
    "identifier": {"type": "m.id.user", "user": os.environ["ADMIN_USER"]},
    "password": os.environ["ADMIN_PASSWORD"],
})
admin_token = body.get("access_token", "")
admin_user = body.get("user_id", "")
print(f"[2] admin login      : HTTP {status} user_id={admin_user} token={'yes' if admin_token else 'NO'}")
if status != 200 or not admin_token:
    failures.append("admin login failed")

# 3. Admin REST + server admin flag
if admin_token:
    status, body = call("GET", "/_synapse/admin/v1/server_version", token=admin_token)
    print(f"[3] admin REST       : HTTP {status} server_version={body.get('server_version')} python={body.get('python_version')}")
    if status != 200:
        failures.append("admin REST call failed (is AGENTTEAMS_ADMIN_USER a Synapse server admin?)")
    status, body = call("GET", "/_synapse/admin/v2/users?from=0&limit=1", token=admin_token)
    print(f"[4] admin users list : HTTP {status} total={body.get('total')}")

# 5. Declarative AppService registration: log in as the sender localpart with
#    m.login.application_service and the as_token.
status, body = call("POST", "/_matrix/client/v3/login", {
    "type": "m.login.application_service",
    "identifier": {"type": "m.id.user", "user": os.environ["SENDER_LOCALPART"]},
}, token=os.environ["AS_TOKEN"])
as_user = body.get("user_id", "")
print(f"[5] appservice login : HTTP {status} user_id={as_user} token={'yes' if body.get('access_token') else 'NO'}")
if status != 200 or not body.get("access_token"):
    failures.append("AppService registration not active (app_service_config_files not loaded?)")

# 6. User ID namespace sanity
expected = "@%s:%s" % (os.environ["ADMIN_USER"], server_name)
print(f"[6] expected admin ID: {expected} (server_name={server_name})")
if admin_user and admin_user != expected:
    failures.append(f"admin user_id {admin_user} does not match expected {expected}")

if failures:
    print("\nFAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nOK: Synapse homeserver + admin REST + AppService registration are healthy.")
PY
