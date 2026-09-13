"""探针状态模型：/healthz /readyz /status 的统一数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbeStatus:
    """探针响应（status + 任意明细字段）。"""

    status: str
    details: dict[str, Any] = field(default_factory=dict)


def create_probe_status(status: str, details: dict[str, Any] | None = None) -> ProbeStatus:
    """构造 ProbeStatus 的便捷工厂。"""
    return ProbeStatus(status=status, details=dict(details or {}))
