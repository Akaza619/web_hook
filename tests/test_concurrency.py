"""Phase 6 concurrency tests for atomic refund idempotency.

The fake manager uses an asyncio lock to model Redis SET NX. The real-Redis
tests are skipped when no local server is available, but they are the proof of
atomic behavior against Redis itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest

from app.config import load_settings
from app.main import create_app
from app.routes import webhook as webhook_module
from app.services.idempotency import (
    acquire_transaction_lock,
    idempotency_key,
)
from app.services.redis_client import RedisManager
from tests.fakes import InMemoryRedisManager

SIGNING_SECRET = "concurrency-test-signing-secret"
SIGNATURE_HEADER = "x-pinhire-hmac-3126"
REDIS_URL = "redis://localhost:6379/15"

TEST_ENV = {
    "APP_ENV": "test",
    "SIGNING_SECRET": SIGNING_SECRET,
    "SIGNATURE_HEADER": SIGNATURE_HEADER,
    "REDIS_URL": REDIS_URL,
    "REDIS_TIMEOUT_SECONDS": "0.3",
    "IDEMPOTENCY_TTL_SECONDS": "300",
}


def encode_json(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_body(body: bytes) -> str:
    return hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def refund_body(tx_ref: str, amount: int = 100) -> bytes:
    return encode_json(
        {
            "event": "refund.requested",
            "payload": {
                "tx_ref_3126": tx_ref,
                "refund_aed": amount,
                "currency": "AED",
            },
        }
    )


def build_app(manager) -> object:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = manager
    return application


async def send_simultaneously(
    application,
    body: bytes,
    *,
    count: int = 10,
) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=application)
    headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: sign_body(body),
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        return await asyncio.gather(
            *(
                client.post("/api/guard", content=body, headers=headers)
                for _ in range(count)
            )
        )


def test_ten_simultaneous_identical_requests_process_once(monkeypatch) -> None:
    manager = InMemoryRedisManager()
    application = build_app(manager)
    original = webhook_module._process_refund_operation
    calls = 0

    async def counting_operation(event, *, limit_aed):
        nonlocal calls
        calls += 1
        # Yield so all requests overlap instead of completing sequentially.
        await asyncio.sleep(0)
        return await original(event, limit_aed=limit_aed)

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        counting_operation,
    )

    responses = asyncio.run(
        send_simultaneously(application, refund_body("TX-CONCURRENT-FAKE"))
    )

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 9
    assert sum(
        1 for response in responses if response.json() == {"status": "processed"}
    ) == 1
    assert sum(
        1 for response in responses if response.json() == {"status": "duplicate"}
    ) == 9
    assert calls == 1
    assert manager.remaining_ttl(
        "pinhire:idempotency:TX-CONCURRENT-FAKE"
    ) is not None


def test_ten_simultaneous_lock_claims_only_one_wins_fake() -> None:
    async def scenario() -> None:
        manager = InMemoryRedisManager()
        await manager.connect()

        locks = await asyncio.gather(
            *(
                acquire_transaction_lock(
                    redis=manager,
                    tx_ref="TX-LOCK-RACE-FAKE",
                    ttl_seconds=300,
                )
                for _ in range(10)
            )
        )

        assert sum(1 for lock in locks if lock.acquired) == 1
        assert sum(1 for lock in locks if not lock.acquired) == 9

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Real Redis integration: these run when a server is available.
# ---------------------------------------------------------------------------


def test_real_redis_only_one_concurrent_lock_acquisition() -> None:
    async def scenario() -> None:
        manager = RedisManager(REDIS_URL, timeout_seconds=0.3)
        if not await manager.connect():
            pytest.skip("Local Redis is not available")

        tx_ref = f"PH6-LOCK-{uuid4().hex}"
        key = idempotency_key(tx_ref)
        try:
            locks = await asyncio.gather(
                *(
                    acquire_transaction_lock(
                        redis=manager,
                        tx_ref=tx_ref,
                        ttl_seconds=300,
                    )
                    for _ in range(10)
                )
            )

            assert sum(1 for lock in locks if lock.acquired) == 1
            assert sum(1 for lock in locks if not lock.acquired) == 9
            ttl = await manager.client.ttl(key)
            assert 295 <= int(ttl) <= 300
        finally:
            await manager.delete(key)
            await manager.close()

    asyncio.run(scenario())


def test_real_redis_ten_simultaneous_requests_process_once(monkeypatch) -> None:
    manager = RedisManager(REDIS_URL, timeout_seconds=0.3)
    application = build_app(manager)
    original = webhook_module._process_refund_operation
    calls = 0

    async def counting_operation(event, *, limit_aed):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return await original(event, limit_aed=limit_aed)

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        counting_operation,
    )

    tx_ref = f"PH6-CONC-{uuid4().hex}"
    key = idempotency_key(tx_ref)

    async def scenario() -> list[httpx.Response]:
        if not await manager.connect():
            pytest.skip("Local Redis is not available")
        try:
            return await send_simultaneously(
                application,
                refund_body(tx_ref),
            )
        finally:
            await manager.delete(key)
            await manager.close()

    responses = asyncio.run(scenario())

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 9
    assert calls == 1
