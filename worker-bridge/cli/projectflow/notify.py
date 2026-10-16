"""委派通知出站客户端:POST bridge /api/v1/bridge/send(契约 interface-contract §4)。

leader 沙箱内 projectflow 唯一的网络出口;零第三方依赖(urllib)。

Flat sibling of projectflow.py (same scripts/ dir): the CLI imports it
lazily inside cmd_delegate_commit, so the --no-notify manual path never
loads this module at all.
"""
from __future__ import annotations

import json
import os
import urllib.request

SPEC_PREVIEW_CHARS = 500


class NotifyError(RuntimeError):
    """通知发送失败——调用方保持任务 prepared 态,重试安全(txn 幂等)。"""


def bridge_send_url() -> str:
    url = os.environ.get("BRIDGE_SEND_URL", "").rstrip("/")
    if not url:
        # Defensive only: cmd_delegate_commit short-circuits an empty
        # BRIDGE_SEND_URL into reply-delivery mode (contract §5.4) before
        # this module is loaded, so a leader CLI reaching here means the
        # endpoint vanished mid-session or notify was called directly.
        raise NotifyError(
            "BRIDGE_SEND_URL is not set — no notification endpoint is "
            "configured in this deployment; the assignment must be "
            "delivered in your session reply body with the assignee's "
            "full Matrix ID written with the @ prefix "
            "(e.g. @worker-name:domain)")
    return url


def build_assignment_text(mxid: str, task_id: str, title: str, spec: str) -> str:
    preview = spec.strip()[:SPEC_PREVIEW_CHARS]
    if len(spec.strip()) > SPEC_PREVIEW_CHARS:
        preview += "…"
    head = f"@{mxid} You are assigned task **{task_id}**: {title}"
    return f"{head}\n\n{preview}" if preview else head


def send_assignment(room_id: str, mxid: str, task_id: str, title: str,
                    spec: str, *, url: str | None = None) -> str:
    """发送委派通知并返回 Matrix event_id;任何失败抛 NotifyError。"""
    endpoint = (url or bridge_send_url()) + "/api/v1/bridge/send"
    body = json.dumps({
        "room_id": room_id,
        "text": build_assignment_text(mxid, task_id, title, spec),
        "txn_id": f"delegate-{task_id}",
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise NotifyError(f"assignment notification failed: {exc}") from exc
    if not payload.get("ok"):
        raise NotifyError(
            f"assignment notification rejected: {payload.get('error')}")
    event_id = payload.get("event_id")
    if not event_id:
        raise NotifyError("bridge send returned no event_id")
    return str(event_id)
