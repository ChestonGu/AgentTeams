#!/bin/bash
# gateway-mock 端到端验证：① mock 自身 chat SSE → ② bridge 全链路
set -u
NS=agentos

echo "== 1. mock healthz =="
POD=$(kubectl -n $NS get pod -l app=cimicode-gateway-mock -o jsonpath="{.items[0].metadata.name}")
kubectl -n $NS exec $POD -- python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8080/healthz").read().decode())'

echo "== 2. mock chat SSE 直接调用（agentMd=system, userMessage=user → LLM） =="
kubectl -n $NS exec -i $POD -- python - <<'PYEOF'
import json, urllib.request
body = json.dumps({
    "sessionId": "sess-cimicode-agent-001",
    "sandboxId": "sbx-cimicode-agent-001",
    "turnId": "verify-$1",
    "agentMd": "你是严谨的前端工程师 cimicode-agent，用中文简洁回答。",
    "history": [],
    "userMessage": "[Current message - respond to this]\nadmin: 用一句话说明什么是 SSE",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/v1/gateway/session/chat",
    data=body, method="POST", headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=90) as resp:
    deltas, done, err = 0, None, None
    for raw in resp:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        evt = json.loads(line[5:])
        if evt.get("event") == "message":
            deltas += 1
        elif evt.get("event") == "done":
            done = evt.get("content", "")
        elif evt.get("event") == "error":
            err = evt
    if err:
        print("LLM-ERROR:", err)
    else:
        print(f"SSE-OK deltas={deltas}")
        print("done.content:", (done or "")[:200])
PYEOF

echo
echo "== 3. bridge → mock 链路（bridge 日志最近 Gateway/chat 记录） =="
BPOD=$(kubectl -n $NS get pod -l app=cimicode-bridge -o jsonpath="{.items[0].metadata.name}")
kubectl -n $NS logs $BPOD --tail=100 2>/dev/null | grep -Ei "gateway|chat|turn" | tail -5
echo "(若为空属正常——还没人在群里 @ 过 bridge)"

echo
echo "== 4. 群成员确认 =="
kubectl -n agentos exec agentteams-synapse-0 -c synapse -- python3 -c "
import urllib.request
r=urllib.request.Request('http://127.0.0.1:8008/_matrix/client/v3/rooms/%21oCOZLhucNczxLoAoUU%3Aagentteams-synapse.agentos.svc.cluster.local/joined_members')
r.add_header('Authorization','Bearer syt_ZnJvbnRlbmQtYWdlbnQ_jCCgeOdRNsmNqwNZQWpj_4A00M1')
import json
d=json.loads(urllib.request.urlopen(r).read().decode())
print(' room members:', ', '.join(d['joined'].keys()))
" 2>/dev/null

cat <<'TIP'

== 5. 全链路手工测试 ==
在 Element Web (http://10.254.254.105:32397) 用 admin 登录，群里发送：
  @cimicode-agent:agentteams-synapse.agentos.svc.cluster.local 你好，介绍一下自己
预期：cimicode-agent 用 GLM 的回复出现在群里（矩阵消息），同时：
  kubectl -n agentos logs -f deployment/cimicode-bridge   # 可见 chat turnId=...
  kubectl -n agentos logs -f deployment/cimicode-gateway-mock  # 可见 LLM 调用
TIP
