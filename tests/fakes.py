"""Deterministic in-memory Redis test double.

This is test-only scaffolding. Production code never substitutes an in-memory
store when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services.redis_client import RedisOperationError


class InMemoryRedisManager:
    """Small async Redis stand-in with atomic SET NX semantics."""

    def __init__(self, *, fail_operations: bool = False) -> None:
        self._values: dict[str, tuple[Any, float | None]] = {}
        self._lock = asyncio.Lock()
        self._clock_offset = 0.0
        self.connected = False
        self.connect_calls = 0
        self.close_calls = 0
        self.fail_operations = fail_operations

    def _now(self) -> float:
        return time.monotonic() + self._clock_offset

    def _purge(self, key: str) -> None:
        entry = self._values.get(key)
        if entry is None:
            return
        _, expires_at = entry
        if expires_at is not None and expires_at <= self._now():
            del self._values[key]

    def _raise_if_failing(self) -> None:
        if self.fail_operations:
            raise RedisOperationError("Redis operation failed.")

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connected = True
        return True

    async def ping(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    async def get(self, key: str) -> Any:
        async with self._lock:
            self._raise_if_failing()
            self._purge(key)
            entry = self._values.get(key)
            return None if entry is None else entry[0]

    async def exists(self, key: str) -> bool:
        async with self._lock:
            self._raise_if_failing()
            self._purge(key)
            return key in self._values

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            self._raise_if_failing()
            removed = 0
            for key in keys:
                self._purge(key)
                if key in self._values:
                    del self._values[key]
                    removed += 1
            return removed

    async def set(self, key: str, value: Any, *, ex: int | None = None) -> bool:
        async with self._lock:
            self._raise_if_failing()
            if ex is not None and ex <= 0:
                raise ValueError("Redis expiry must be greater than zero.")
            expires_at = None if ex is None else self._now() + ex
            self._values[key] = (value, expires_at)
            return True

    async def set_with_expiration(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
    ) -> bool:
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("Redis TTL must be a positive integer number of seconds.")
        return await self.set(key, value, ex=ttl_seconds)

    async def set_if_absent(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
    ) -> bool:
        async with self._lock:
            self._raise_if_failing()
            self._purge(key)
            if key in self._values:
                return False
            self._values[key] = (value, self._now() + ttl_seconds)
            return True

    async def compare_and_set_with_expiration(
        self,
        key: str,
        expected_value: Any,
        new_value: Any,
        ttl_seconds: int,
    ) -> bool:
        async with self._lock:
            self._raise_if_failing()
            self._purge(key)
            entry = self._values.get(key)
            if entry is None or entry[0] != expected_value:
                return False
            self._values[key] = (new_value, self._now() + ttl_seconds)
            return True

    async def compare_and_delete(
        self,
        key: str,
        expected_value: Any,
    ) -> bool:
        async with self._lock:
            self._raise_if_failing()
            self._purge(key)
            entry = self._values.get(key)
            if entry is None or entry[0] != expected_value:
                return False
            del self._values[key]
            return True

    def remaining_ttl(self, key: str) -> float | None:
        """Return remaining TTL in seconds, or None for a missing key."""

        self._purge(key)
        entry = self._values.get(key)
        if entry is None or entry[1] is None:
            return None
        return entry[1] - self._now()

    def advance(self, seconds: float) -> None:
        """Advance the fake clock to test TTL expiry without sleeping."""

        self._clock_offset += seconds
