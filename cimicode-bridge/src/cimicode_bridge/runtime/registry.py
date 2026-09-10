"""Runtime SPI 工厂：按 ``runtime.adapter`` 配置分派到具体 adapter 实现。

bridge 核心必须保持 runtime 无关——本工厂是唯一知道"哪个 adapter 类服务
哪个名字"的地方（spec §3.1 Runtime SPI）。
"""
from __future__ import annotations

from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter
from cimicode_bridge.runtime.opencode_adapter import OpenCodeAdapter


def build_runtime_adapter(runtime: RuntimeConfig):
    """按配置构建 runtime adapter：cimicode（SSE gateway）/ opencode（REST+轮询）。"""
    if runtime.adapter == "cimicode":
        return CimicodeAdapter(
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
