"""Asynchronous Redis connection management and reusable key/value utilities.

A single :class:`RedisManager` is created per application and stored in
``app.state.redis``. It owns one ``redis.asyncio.Redis`` client (and therefore
one connection pool), so webhook requests reuse connections instead of opening
a new one each time.

Failures are never hidden. Connection problems are reflected in ``/health`` and
logged without exposing the Redis URL or credentials. Command failures raise
:class:`RedisOperationError`; there is deliberately no in-memory fallback for
security-sensitive operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import redis.asyncio as redis_asyncio
from redis.exceptions import (
    AuthenticationError,
    ConnectionError as RedisConnectionError,
    RedisError,
    TimeoutError as RedisTimeoutError,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_CONNECTIONS = 20

# Lua scripts keep ownership checks and writes atomic in Redis. A lock is only
# deleted or completed when its stored owner token still matches.
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_COMPLETE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


class RedisUnavailableError(RuntimeError):
    """Raised when a Redis operation is attempted without an initialized client."""


class RedisOperationError(RuntimeError):
    """Raised when Redis rejects or fails a command.

    Callers must treat this as a real failure. Nothing is silently stored in
    memory or otherwise emulated.
    """


def _error_category(exc: BaseException) -> str:
    """Return a credential-free description of a Redis failure."""

    if isinstance(exc, AuthenticationError):
        return "authentication failed"
    if isinstance(exc, (RedisTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, RedisConnectionError):
        return "connection error"
    if isinstance(exc, RedisError):
        return "Redis error"
    if isinstance(exc, OSError):
        return "network error"
    return type(exc).__name__


class RedisManager:
    """Own and reuse one asynchronous Redis client for the application."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        client: Any | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Redis timeout must be greater than zero.")
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._max_connections = max_connections
        self._client: Any | None = client
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Return the last known connection state."""

        return self._connected

    @property
    def client(self) -> Any:
        """Return the reusable client, or raise if it has not been initialized."""

        if self._client is None:
            raise RedisUnavailableError("Redis client is not initialized.")
        return self._client

    async def connect(self) -> bool:
        """Create the client and verify it with PING.

        Connection failures are logged and return ``False`` so the application
        can still start and report a degraded health status.
        """

        async with self._lock:
            if self._client is None:
                try:
                    self._client = redis_asyncio.Redis.from_url(
                        self._url,
                        decode_responses=True,
                        socket_connect_timeout=self._timeout_seconds,
                        socket_timeout=self._timeout_seconds,
                        health_check_interval=30,
                        max_connections=self._max_connections,
                    )
                except (RedisError, ValueError, OSError) as exc:
                    self._connected = False
                    logger.warning(
                        "Redis client creation failed (%s); health will report degraded.",
                        _error_category(exc),
                    )
                    return False

            try:
                await self._ping()
            except (RedisError, OSError, asyncio.TimeoutError) as exc:
                self._connected = False
                logger.warning(
                    "Redis connection failed (%s); health will report degraded.",
                    _error_category(exc),
                )
                return False

            self._connected = True
            logger.info("Redis connection established.")
            return True

    async def ping(self) -> bool:
        """Check connectivity, updating state, and return a boolean.

        This never raises: ``/health`` must be able to report a degraded state
        even when Redis is down, timing out, or rejecting authentication.
        """

        if self._client is None:
            return await self.connect()

        try:
            await self._ping()
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._connected = False
            logger.warning(
                "Redis ping failed (%s); health will report degraded.",
                _error_category(exc),
            )
            return False

        self._connected = True
        return True

    async def close(self) -> None:
        """Close the client and its connection pool."""

        async with self._lock:
            client = self._client
            self._client = None
            self._connected = False
            if client is None:
                return

            try:
                await client.aclose()
            except (RedisError, OSError) as exc:
                logger.warning(
                    "Redis shutdown encountered %s.",
                    _error_category(exc),
                )
            else:
                logger.info("Redis connection closed.")

    async def get(self, key: str) -> Any:
        """Return a value, or ``None`` when the key does not exist."""

        return await self._execute("GET", lambda client: client.get(key))

    async def set(self, key: str, value: Any, *, ex: int | None = None) -> bool:
        """Set a value, optionally with an expiry in seconds."""

        if ex is not None and ex <= 0:
            raise ValueError("Redis expiry must be greater than zero.")

        result = await self._execute(
            "SET",
            lambda client: client.set(key, value, ex=ex),
        )
        return bool(result)

    async def delete(self, *keys: str) -> int:
        """Delete one or more keys and return the number removed."""

        if not keys:
            return 0
        result = await self._execute("DEL", lambda client: client.delete(*keys))
        return int(result)

    async def exists(self, key: str) -> bool:
        """Return True when a key exists."""

        result = await self._execute("EXISTS", lambda client: client.exists(key))
        return bool(result)

    async def set_with_expiration(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
    ) -> bool:
        """Set a value that expires after ``ttl_seconds``."""

        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("Redis TTL must be a positive integer number of seconds.")
        return await self.set(key, value, ex=ttl_seconds)

    async def set_if_absent(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
    ) -> bool:
        """Atomically set a key only when it does not already exist.

        This is ``SET key value NX EX ttl_seconds``. Exactly one concurrent
        caller can receive ``True`` for a given key.
        """

        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("Redis TTL must be a positive integer number of seconds.")

        result = await self._execute(
            "SET NX",
            lambda client: client.set(key, value, nx=True, ex=ttl_seconds),
        )
        return bool(result)

    async def compare_and_set_with_expiration(
        self,
        key: str,
        expected_value: Any,
        new_value: Any,
        ttl_seconds: int,
    ) -> bool:
        """Replace a value only when the caller still owns it.

        Used to move a transaction from ``processing`` to ``completed`` without
        overwriting a lock that expired and was acquired by another request.
        """

        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("Redis TTL must be a positive integer number of seconds.")

        result = await self._execute(
            "EVAL complete",
            lambda client: client.eval(
                _COMPLETE_LOCK_SCRIPT,
                1,
                key,
                expected_value,
                new_value,
                ttl_seconds,
            ),
        )
        return bool(result)

    async def compare_and_delete(
        self,
        key: str,
        expected_value: Any,
    ) -> bool:
        """Delete a key only when its value matches the expected owner token."""

        result = await self._execute(
            "EVAL release",
            lambda client: client.eval(
                _RELEASE_LOCK_SCRIPT,
                1,
                key,
                expected_value,
            ),
        )
        return bool(result)

    async def _ping(self) -> None:
        if self._client is None:
            raise RedisUnavailableError("Redis client is not initialized.")
        await asyncio.wait_for(
            self._client.ping(),
            timeout=self._timeout_seconds,
        )

    async def _execute(
        self,
        action: str,
        operation: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        if self._client is None:
            raise RedisUnavailableError("Redis client is not initialized.")

        try:
            result = await operation(self._client)
        except (RedisError, OSError, asyncio.TimeoutError) as exc:
            self._connected = False
            logger.warning(
                "Redis %s failed (%s).",
                action,
                _error_category(exc),
            )
            raise RedisOperationError(f"Redis {action} failed.") from exc

        self._connected = True
        return result


# ---------------------------------------------------------------------------
# Reusable module-level helpers for idempotency and FAQ caching.
# ---------------------------------------------------------------------------


async def check_redis_connectivity(manager: RedisManager) -> bool:
    """Return True when Redis responds to PING."""

    return await manager.ping()


async def get_value(manager: RedisManager, key: str) -> Any:
    """Get a value through the shared manager."""

    return await manager.get(key)


async def set_value(manager: RedisManager, key: str, value: Any) -> bool:
    """Set a value through the shared manager."""

    return await manager.set(key, value)


async def delete_value(manager: RedisManager, key: str) -> int:
    """Delete a key through the shared manager."""

    return await manager.delete(key)


async def key_exists(manager: RedisManager, key: str) -> bool:
    """Return True when the key exists."""

    return await manager.exists(key)


async def set_value_with_expiration(
    manager: RedisManager,
    key: str,
    value: Any,
    ttl_seconds: int,
) -> bool:
    """Set a value with a TTL through the shared manager."""

    return await manager.set_with_expiration(key, value, ttl_seconds)
