"""Phase 5 tests for asynchronous Redis management and health reporting.

Most tests use an in-memory fake that mimics the small async surface the
manager relies on. A real-Redis round trip runs when a local server is
available and is skipped otherwise.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import AuthenticationError, ConnectionError as RedisConnectionError

from app.config import load_settings
from app.main import create_app
from app.services import redis_client as redis_module
from app.services.redis_client import (
    RedisManager,
    RedisOperationError,
    RedisUnavailableError,
    check_redis_connectivity,
    delete_value,
    get_value,
    key_exists,
    set_value,
    set_value_with_expiration,
)

REDIS_URL = "redis://localhost:6379/15"
TEST_ENV = {
    "APP_ENV": "test",
    "SIGNING_SECRET": "redis-test-signing-secret",
    "REDIS_URL": REDIS_URL,
    "REDIS_TIMEOUT_SECONDS": "0.1",
}


class FakeRedis:
    """Minimal async Redis double with optional failure injection."""

    def __init__(
        self,
        *,
        ping_error: BaseException | None = None,
        operation_error: BaseException | None = None,
    ) -> None:
        self.values: dict[str, Any] = {}
        self.expiries: dict[str, int | None] = {}
        self.ping_error = ping_error
        self.operation_error = operation_error
        self.ping_calls = 0
        self.close_calls = 0
        self.last_set_kwargs: dict[str, Any] | None = None

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def get(self, key: str) -> Any:
        self._raise_if_needed()
        return self.values.get(key)

    async def set(self, key: str, value: Any, *, ex: int | None = None) -> bool:
        self._raise_if_needed()
        self.last_set_kwargs = {"ex": ex}
        self.values[key] = value
        self.expiries[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self._raise_if_needed()
        removed = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                self.expiries.pop(key, None)
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        self._raise_if_needed()
        return 1 if key in self.values else 0

    async def aclose(self) -> None:
        self.close_calls += 1

    def _raise_if_needed(self) -> None:
        if self.operation_error is not None:
            raise self.operation_error


class StubManager:
    """Health-only stand-in for the real manager."""

    def __init__(self, *, connected: bool) -> None:
        self.connected = connected
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> bool:
        self.connect_calls += 1
        return self.connected

    async def ping(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_calls += 1


def make_manager(**fake_kwargs: Any) -> tuple[RedisManager, FakeRedis]:
    fake = FakeRedis(**fake_kwargs)
    return RedisManager(REDIS_URL, timeout_seconds=0.5, client=fake), fake


# ---------------------------------------------------------------------------
# Connection initialization and PING
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_connect_initializes_and_pings_the_client() -> None:
    manager, fake = make_manager()

    assert manager.connected is False
    assert await manager.connect() is True
    assert manager.connected is True
    assert fake.ping_calls == 1
    assert manager.client is fake


@pytest.mark.anyio
async def test_connect_creates_one_client_with_timeouts(monkeypatch) -> None:
    fake = FakeRedis()
    captured: dict[str, Any] = {}

    def fake_from_url(url: str, **kwargs: Any) -> FakeRedis:
        captured["url"] = url
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(
        redis_module.redis_asyncio.Redis,
        "from_url",
        fake_from_url,
    )

    manager = RedisManager(REDIS_URL, timeout_seconds=1.25)
    assert await manager.connect() is True
    assert await manager.connect() is True
    await manager.get("some-key")

    assert captured["url"] == REDIS_URL
    assert captured["socket_connect_timeout"] == 1.25
    assert captured["socket_timeout"] == 1.25
    assert captured["decode_responses"] is True
    assert captured["max_connections"] == redis_module.DEFAULT_MAX_CONNECTIONS
    assert fake.ping_calls == 2


@pytest.mark.anyio
async def test_successful_ping_updates_connected_state() -> None:
    manager, fake = make_manager()
    manager._client = fake

    assert await manager.ping() is True
    assert manager.connected is True


@pytest.mark.anyio
async def test_ping_without_a_client_attempts_connection(monkeypatch) -> None:
    fake = FakeRedis()

    monkeypatch.setattr(
        redis_module.redis_asyncio.Redis,
        "from_url",
        lambda url, **kwargs: fake,
    )

    manager = RedisManager(REDIS_URL, timeout_seconds=0.5)
    assert await manager.ping() is True
    assert manager.connected is True
    assert fake.ping_calls == 1


# ---------------------------------------------------------------------------
# Key/value utilities
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_set_and_get_round_trip() -> None:
    manager, fake = make_manager()
    await manager.connect()

    assert await manager.set("greeting", "hello") is True
    assert await manager.get("greeting") == "hello"
    assert fake.values["greeting"] == "hello"


@pytest.mark.anyio
async def test_get_returns_none_for_missing_key() -> None:
    manager, _ = make_manager()
    await manager.connect()

    assert await manager.get("absent") is None


@pytest.mark.anyio
async def test_delete_removes_keys_and_reports_count() -> None:
    manager, _ = make_manager()
    await manager.connect()
    await manager.set("a", "1")
    await manager.set("b", "2")

    assert await manager.delete("a", "b", "missing") == 2
    assert await manager.exists("a") is False
    assert await manager.exists("b") is False
    assert await manager.delete() == 0


@pytest.mark.anyio
async def test_exists_reflects_key_presence() -> None:
    manager, _ = make_manager()
    await manager.connect()

    assert await manager.exists("tx") is False
    await manager.set("tx", "TX1001")
    assert await manager.exists("tx") is True


@pytest.mark.anyio
async def test_set_with_expiration_passes_ttl_to_redis() -> None:
    manager, fake = make_manager()
    await manager.connect()

    assert await manager.set_with_expiration("session", "value", 300) is True
    assert fake.last_set_kwargs == {"ex": 300}
    assert fake.expiries["session"] == 300


@pytest.mark.anyio
async def test_invalid_expiry_values_are_rejected_before_redis_calls() -> None:
    manager, fake = make_manager()
    await manager.connect()

    with pytest.raises(ValueError):
        await manager.set("key", "value", ex=0)
    with pytest.raises(ValueError):
        await manager.set_with_expiration("key", "value", 0)
    with pytest.raises(ValueError):
        await manager.set_with_expiration("key", "value", -5)
    with pytest.raises(ValueError):
        await manager.set_with_expiration("key", "value", 1.5)  # type: ignore[arg-type]

    assert fake.values == {}


@pytest.mark.anyio
async def test_module_level_helpers_delegate_to_the_manager() -> None:
    manager, _ = make_manager()
    await manager.connect()

    assert await check_redis_connectivity(manager) is True
    assert await set_value(manager, "faq:1", "answer") is True
    assert await get_value(manager, "faq:1") == "answer"
    assert await key_exists(manager, "faq:1") is True
    assert await set_value_with_expiration(manager, "faq:2", "cached", 60) is True
    assert await delete_value(manager, "faq:1") == 1
    assert await key_exists(manager, "faq:1") is False


# ---------------------------------------------------------------------------
# Failure behavior: unavailable, timeout, authentication, operation errors
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_unavailable_redis_does_not_break_connect_and_operations_fail(
    monkeypatch,
) -> None:
    def failing_from_url(url: str, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(
        redis_module.redis_asyncio.Redis,
        "from_url",
        failing_from_url,
    )

    manager = RedisManager(REDIS_URL, timeout_seconds=0.1)

    assert await manager.connect() is False
    assert manager.connected is False

    with pytest.raises(RedisUnavailableError):
        await manager.get("key")


@pytest.mark.anyio
async def test_operation_after_failed_ping_fails_safely() -> None:
    manager, fake = make_manager(
        ping_error=RedisConnectionError("unreachable"),
        operation_error=RedisConnectionError("unreachable"),
    )

    assert await manager.connect() is False

    with pytest.raises(RedisOperationError):
        await manager.get("key")

    assert fake.values == {}


@pytest.mark.anyio
async def test_connection_failure_is_reported_without_raising(monkeypatch) -> None:
    def failing_from_url(url: str, **kwargs: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(
        redis_module.redis_asyncio.Redis,
        "from_url",
        failing_from_url,
    )

    manager = RedisManager(REDIS_URL, timeout_seconds=0.1)

    assert await manager.connect() is False
    assert manager.connected is False
    with pytest.raises(RedisUnavailableError):
        await manager.get("key")


@pytest.mark.anyio
async def test_authentication_failure_is_logged_without_credentials(
    caplog,
) -> None:
    secret_url = "redis://:super-secret-password@example.invalid:6379/0"
    fake = FakeRedis(ping_error=AuthenticationError("WRONGPASS"))
    manager = RedisManager(secret_url, timeout_seconds=0.5, client=fake)

    with caplog.at_level(logging.WARNING, logger=redis_module.__name__):
        assert await manager.connect() is False

    assert manager.connected is False
    assert "authentication failed" in caplog.text
    assert "super-secret-password" not in caplog.text
    assert secret_url not in caplog.text


@pytest.mark.anyio
async def test_operation_failure_raises_and_marks_disconnected() -> None:
    manager, fake = make_manager(operation_error=RedisConnectionError("lost"))
    await manager.connect()
    assert manager.connected is True

    with pytest.raises(RedisOperationError):
        await manager.get("key")

    assert manager.connected is False


@pytest.mark.anyio
async def test_ping_failure_returns_false_and_marks_disconnected() -> None:
    manager, fake = make_manager()
    await manager.connect()
    fake.ping_error = RedisConnectionError("lost")

    assert await manager.ping() is False
    assert manager.connected is False


@pytest.mark.anyio
async def test_no_in_memory_fallback_is_used_after_failure() -> None:
    manager, fake = make_manager(operation_error=RedisConnectionError("lost"))
    await manager.connect()

    with pytest.raises(RedisOperationError):
        await manager.set("tx:1", "value")

    assert fake.values == {}


# ---------------------------------------------------------------------------
# Lifecycle and health endpoint
# ---------------------------------------------------------------------------


def test_startup_connects_and_shutdown_closes() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    fake = FakeRedis()
    manager = RedisManager(REDIS_URL, timeout_seconds=0.5, client=fake)
    application.state.redis = manager

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        assert manager.connected is True
        assert fake.ping_calls >= 1

    assert fake.close_calls == 1
    assert manager.connected is False
    assert manager._client is None


def test_startup_survives_redis_unavailable() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    fake = FakeRedis(ping_error=RedisConnectionError("unreachable"))
    application.state.redis = RedisManager(
        REDIS_URL,
        timeout_seconds=0.5,
        client=fake,
    )

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_health_reports_connected_redis() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    fake = FakeRedis()
    application.state.redis = RedisManager(
        REDIS_URL,
        timeout_seconds=0.5,
        client=fake,
    )

    with TestClient(application) as client:
        body = client.get("/health").json()

    assert body["status"] == "healthy"
    assert body["app"] == "ok"
    assert body["redis"] == "connected"
    assert body["service"] == "Pinhire Webhook Server"
    assert body["environment"] == "test"


def test_health_reports_disconnected_redis() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = StubManager(connected=False)

    with TestClient(application) as client:
        response = client.get("/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["app"] == "ok"
    assert body["redis"] == "disconnected"


def test_health_never_exposes_redis_credentials() -> None:
    secret_url = "redis://:hidden-password@example.invalid:6379/0"
    application = create_app(
        load_settings(env={**TEST_ENV, "REDIS_URL": secret_url})
    )
    fake = FakeRedis(ping_error=AuthenticationError("WRONGPASS"))
    application.state.redis = RedisManager(
        secret_url,
        timeout_seconds=0.5,
        client=fake,
    )

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["redis"] == "disconnected"
    assert "hidden-password" not in response.text
    assert secret_url not in response.text


def test_health_handles_a_manager_that_raises_unexpectedly() -> None:
    class ExplodingManager:
        async def ping(self) -> bool:
            raise RuntimeError("unexpected")

        async def close(self) -> None:
            return None

    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = ExplodingManager()

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["redis"] == "disconnected"


def test_shutdown_without_a_client_is_safe() -> None:
    import asyncio

    manager = RedisManager(REDIS_URL, timeout_seconds=0.5)
    asyncio.run(manager.close())
    asyncio.run(manager.close())

    assert manager.connected is False


# ---------------------------------------------------------------------------
# Optional real Redis integration test
# ---------------------------------------------------------------------------


def test_real_redis_round_trip_when_available() -> None:
    import asyncio

    async def round_trip() -> None:
        manager = RedisManager(REDIS_URL, timeout_seconds=0.3)
        connected = await manager.connect()
        if not connected:
            pytest.skip("Local Redis is not available")

        key = "pinhire:phase5:test"
        try:
            assert await manager.ping() is True
            assert await manager.set(key, "hello") is True
            assert await manager.get(key) == "hello"
            assert await manager.exists(key) is True
            assert await manager.set_with_expiration(key, "temp", 30) is True
            assert await manager.get(key) == "temp"
            assert await manager.delete(key) == 1
            assert await manager.exists(key) is False
        finally:
            await manager.delete(key)
            await manager.close()

    asyncio.run(round_trip())
