from __future__ import annotations

import redis.asyncio as redis


class RedisStore:
    def __init__(self, url: str) -> None:
        self.client = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self.client.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        await self.client.set(key, value, ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        await self.client.delete(key)