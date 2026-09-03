from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class RuntimeCapabilities(BaseModel):
    supports_session_destroy: bool = False
    supports_interrupt_event: bool = False
    supports_artifact: bool = False
    supports_streaming: bool = True


class RuntimeAdapter(Protocol):
    name: str

    async def health(self) -> bool:
        ...

    async def chat(self, request: Any) -> Any:
        ...

    def capabilities(self) -> RuntimeCapabilities:
        ...


@runtime_checkable
class EventDialect(Protocol):
    name: str

    def translate(self, raw_event: dict[str, Any]) -> list[Any]:
        ...


class AuthProvider(ABC):
    @abstractmethod
    async def attach(self, request: Any) -> Any:
        ...
