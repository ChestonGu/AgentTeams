"""委派指派文本构造（回复送达形态，契约 interface-contract §5.4）。

delegate-commit 从不发送任何消息——它打印 paste-ready 指派文本，leader
会话回复原样粘贴送达；本模块只负责文本构造，零网络调用。

Flat sibling of projectflow.py (same scripts/ dir): the CLI imports it
lazily inside cmd_delegate_commit, so the --no-notify manual path never
loads this module at all.
"""
from __future__ import annotations

SPEC_PREVIEW_CHARS = 500


def build_assignment_text(mxid: str, task_id: str, title: str, spec: str) -> str:
    preview = spec.strip()[:SPEC_PREVIEW_CHARS]
    if len(spec.strip()) > SPEC_PREVIEW_CHARS:
        preview += "…"
    head = f"@{mxid} You are assigned task **{task_id}**: {title}"
    return f"{head}\n\n{preview}" if preview else head
