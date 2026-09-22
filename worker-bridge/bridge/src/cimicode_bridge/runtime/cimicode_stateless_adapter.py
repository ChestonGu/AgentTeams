"""cimicode-stateless adapter：外部无状态 cimicode 平台的对接形态。

绑定来源（全部来自 runtime.yaml bridge 段，controller 从 Worker CR
spec.runtimeParameter 整 map 投影——契约 v1.3；已知键 baseUrl/sessionId/
sandboxId/templateId 填固定字段，追加键经 chat 请求体透传平台）：
- base_url   外部 cimicode 接口地址
- session_id / sandbox_id   平台预建的会话/沙箱绑定
- template_id    agent 模板追溯

传输实现共享 ``CimicodeAdapter`` 的 SSE 协议（HTTP/SSE gateway 契约对两种
形态一致）；本子类是 stateless 专属演进的独立分叉点（后续如绑定语义、
鉴权等分化只动这里，不影响 pod 形态）。
"""
from __future__ import annotations

from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter


class CimicodeStatelessAdapter(CimicodeAdapter):
    """无状态形态：每 turn 无状态调用外部 cimicode（绑定经 chat 参数透传）。"""
