"""Runtime SPI 工厂：按 ``runtime.adapter`` 配置分派到具体 adapter 实现。

bridge 核心必须保持 runtime 无关——本工厂是唯一知道"哪个 adapter 类服务
哪个名字"的地方（spec §3.1 Runtime SPI）。两种形态共享 CimicodeAdapter
的 SSE 传输基座（docking 团队持续维护），各自子类化为独立演进分叉点。
"""
from __future__ import annotations

from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.runtime.cimicode_pod_adapter import CimicodePodAdapter
from cimicode_bridge.runtime.cimicode_stateless_adapter import CimicodeStatelessAdapter

VALID_ADAPTERS = ("cimicode-stateless", "cimicode-pod")


def build_runtime_adapter(runtime: RuntimeConfig):
    """按配置构建 runtime adapter。

    adapter 为空（未定）或 base_url 为空时抛 ValueError——bridge 装配层
    （app.py）负责在两者齐备后才调用本工厂，绝不静默回落到任何默认地址。
    """
    if not runtime.adapter:
        raise ValueError("runtime adapter undetermined (no BRIDGE_RUNTIME_ADAPTER env, no bridge section)")
    if not runtime.base_url:
        raise ValueError(f"runtime base_url is empty for adapter {runtime.adapter!r}")
    if runtime.adapter == "cimicode-stateless":
        return CimicodeStatelessAdapter(
            runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    if runtime.adapter == "cimicode-pod":
        return CimicodePodAdapter(
            runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    raise ValueError(f"unknown runtime adapter: {runtime.adapter!r} (expected one of {VALID_ADAPTERS})")
