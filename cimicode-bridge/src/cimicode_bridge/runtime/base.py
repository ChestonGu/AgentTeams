"""runtime SPI：适配器/方言/鉴权提供方的接口契约（core 只依赖这里）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from pydantic import BaseModel


class RuntimeCapabilities(BaseModel):
    """runtime 能力声明（不支持的能力走降级路径）。"""

    supports_session_destroy: bool = False   # 是否支持 destroy（不支持则 no-op）
    supports_interrupt_event: bool = False   # 是否主动推送中断事件
    supports_artifact: bool = False          # 是否支持产物通道
    supports_streaming: bool = True          # 是否支持流式


class RuntimeAdapter(Protocol):
    """runtime 适配器接口（"充电头"）：每个 runtime 一个实现。"""

    name: str

    async def health(self) -> bool:
        """健康检查。"""
        ...

    async def chat(self, request: Any) -> Any:
        """提交 turn（bridge 唯一调用的运行时入口）。"""
        ...

    def capabilities(self) -> RuntimeCapabilities:
        """能力声明。"""
        ...


class AuthProvider(ABC):
    """鉴权提供方接口（cimicode 当前不鉴权，扩展点留给其他 runtime）。"""

    @abstractmethod
    async def attach(self, request: Any) -> Any:
        """把鉴权信息附加到请求上。"""
        ...
