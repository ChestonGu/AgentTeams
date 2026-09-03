from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbeStatus:
    status: str
    details: dict[str, Any] = field(default_factory=dict)


def create_probe_status(status: str, details: dict[str, Any] | None = None) -> ProbeStatus:
    return ProbeStatus(status=status, details=dict(details or {}))
