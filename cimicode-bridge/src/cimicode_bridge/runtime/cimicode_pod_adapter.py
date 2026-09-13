"""cimicode-pod adapter：operator 供给的 cimicode pod 的对接形态。

baseUrl 来源与 stateless 不同：由 worker-bridge operator 在供给
cimicode-<worker>-svc 后 patch 进 Worker CR env（BRIDGE_RUNTIME_BASE_URL），
bridge 经 controller runtimeEnv 读到；会话/沙箱由 pod 内 cimicode 自管，
无需预建绑定。传输实现共享 ``CimicodeAdapter`` 的 SSE 协议；本子类是
pod 专属演进的独立分叉点（后续如健康探测、pod 生命周期联动只动这里）。
"""
from __future__ import annotations

from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter


class CimicodePodAdapter(CimicodeAdapter):
    """pod 形态：调用 operator 供给的集群内 cimicode 服务。"""
