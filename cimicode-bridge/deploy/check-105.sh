#!/bin/bash
# cimicode-bridge @105 部署后检查：/status + Matrix 连接日志
set -u
NS=agentos
POD=$(kubectl -n "$NS" get pod -l app=cimicode-bridge -o jsonpath="{.items[0].metadata.name}")

echo "== /status =="
kubectl -n "$NS" exec "$POD" -- python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8081/status").read().decode())' 2>/dev/null

echo "== /readyz =="
kubectl -n "$NS" exec "$POD" -- python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8081/readyz").read().decode())' 2>/dev/null

echo "== 最近日志（Matrix/Gateway 相关） =="
kubectl -n "$NS" logs "$POD" --tail=200 2>/dev/null | grep -Ei "whoami|sync|matrix|gateway|error|warning" | tail -12

echo "== bot 是否在房间里（synapse 侧查询 joined_members） =="
kubectl -n agentos exec agentteams-synapse-0 -c synapse -- python3 -c "
import json,urllib.request
r=urllib.request.Request('http://127.0.0.1:8008/_matrix/client/v3/rooms/%21oCOZLhucNczxLoAoUU%3Aagentteams-synapse.agentos.svc.cluster.local/joined_members')
r.add_header('Authorization','Bearer syt_ZnJvbnRlbmQtYWdlbnQ_jCCgeOdRNsmNqwNZQWpj_4A00M1')
print(urllib.request.urlopen(r).read().decode()[:400])
" 2>/dev/null
