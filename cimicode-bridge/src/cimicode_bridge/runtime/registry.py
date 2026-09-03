from __future__ import annotations

from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.runtime.client import HttpSseRuntime
from cimicode_bridge.runtime.opencode_adapter import OpenCodeAdapter


def build_runtime_adapter(runtime: RuntimeConfig):
    """Dispatch on ``runtime.adapter`` (spec §3.1 Runtime SPI).

    The bridge core must stay runtime-agnostic: this factory is the only
    place that knows which adapter class serves which name.
    """
    if runtime.adapter == "cimicode":
        return HttpSseRuntime(
            runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    if runtime.adapter == "opencode":
        return OpenCodeAdapter(
            runtime.base_url,
            helper_url=runtime.helper_url or runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
            poll_interval_seconds=runtime.poll_interval_seconds,
        )
    raise ValueError(f"unknown runtime adapter: {runtime.adapter!r}")
