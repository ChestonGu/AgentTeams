"""在 bridge 容器内诊断：httpx-sse 版本 + mock chat SSE 原始字节流解析。"""
import inspect
import json
import os
import sys

import httpx

BASE = os.getenv("GW_URL", "http://cimicode-gateway-mock.agentos.svc.cluster.local:8080")


def main() -> int:
    import httpx_sse

    print("httpx_sse file:", httpx_sse.__file__)
    print("aconnect_sse sig:", inspect.signature(httpx_sse.aconnect_sse))

    body = {
        "sessionId": "sess-cimicode-agent-001",
        "sandboxId": "sbx-cimicode-agent-001",
        "turnId": "diag-$1",
        "agentMd": "你是 cimicode-agent，用中文简洁回答。",
        "history": [],
        "userMessage": "[Current message - respond to this]\nadmin: 回复OK两个字",
    }
    lines: list[str] = []
    with httpx.Client(timeout=90) as client:
        # 不用 httpx-sse，直接手读 SSE 字节流，看服务端到底发了什么
        with client.stream("POST", f"{BASE}/v1/gateway/session/chat", json=body) as resp:
            print("status:", resp.status_code, "ct:", resp.headers.get("content-type"))
            count = 0
            for line in resp.iter_lines():
                if line.strip():
                    lines.append(line)
                    count += 1
                if count >= 8:
                    break
    for ln in lines:
        print("RAW:", ln[:120])
    for ln in lines:
        if ln.startswith("data:"):
            payload = json.loads(ln[5:].strip())
            if payload.get("event") == "done":
                print("DONE-PARSE-OK content:", str(payload.get("content", ""))[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
