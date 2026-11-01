"""Phase 1/5 smoke tests for the application and health endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app

BASE_ENV = {
    "APP_ENV": "test",
    "APP_NAME": "Pinhire Webhook Server",
    "APP_VERSION": "0.1.0",
    "SIGNING_SECRET": "test-only-signing-secret",
    "REDIS_URL": "redis://127.0.0.1:6379/15",
    "REDIS_TIMEOUT_SECONDS": "0.1",
}


class StubRedisManager:
    """Deterministic Redis stand-in for health/lifecycle tests."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.connect_called = False
        self.close_called = False

    async def connect(self) -> bool:
        self.connect_called = True
        return self.connected

    async def ping(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_called = True


@pytest.fixture()
def client() -> TestClient:
    """Create a TestClient with its lifespan (startup/shutdown) running."""

    application = create_app(load_settings(env=BASE_ENV))
    application.state.redis = StubRedisManager(connected=True)
    with TestClient(application) as test_client:
        yield test_client


def test_health_returns_http_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_reports_healthy_status_and_service_metadata(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body == {
        "status": "healthy",
        "app": "ok",
        "redis": "connected",
        "service": "Pinhire Webhook Server",
        "version": "0.1.0",
        "environment": "test",
    }


def test_health_response_contains_request_id(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["x-request-id"]


def test_caller_supplied_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"x-request-id": "test-correlation-id"})

    assert response.headers["x-request-id"] == "test-correlation-id"


def test_unknown_route_returns_structured_error(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"]


def test_app_starts_without_a_dev_signing_secret() -> None:
    """A missing secret is tolerated outside production, but disabled."""

    application = create_app(
        load_settings(env={"APP_ENV": "test", "REDIS_TIMEOUT_SECONDS": "0.1"})
    )
    with TestClient(application) as test_client:
        assert test_client.get("/health").status_code == 200


def test_validation_errors_use_the_standard_envelope() -> None:
    application = create_app(load_settings(env=BASE_ENV))

    @application.get("/needs-int")
    async def needs_int(value: int) -> dict[str, int]:
        return {"value": value}

    with TestClient(application) as test_client:
        response = test_client.get("/needs-int", params={"value": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]


def test_unhandled_errors_do_not_leak_internal_details() -> None:
    application = create_app(load_settings(env=BASE_ENV))

    @application.get("/boom")
    async def boom() -> None:
        raise RuntimeError("sensitive internal detail")

    with TestClient(application, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "internal_error"
    assert "sensitive internal detail" not in response.text
    assert body["error"]["request_id"]
