"""Phase 6 tests for Redis-backed refund idempotency."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.routes import webhook as webhook_module
from app.services.idempotency import (
    DEFAULT_LOCK_TTL_SECONDS,
    IDEMPOTENCY_KEY_PREFIX,
    TransactionReferenceError,
    acquire_transaction_lock,
    claim_transaction,
    complete_transaction,
    get_transaction_state,
    idempotency_key,
    validate_transaction_reference,
)
from app.services.redis_client import RedisManager, RedisOperationError
from tests.fakes import InMemoryRedisManager

SIGNING_SECRET = "idempotency-test-signing-secret"
SIGNATURE_HEADER = "x-pinhire-hmac-3126"
REDIS_URL = "redis://127.0.0.1:6379/15"

TEST_ENV = {
    "APP_ENV": "test",
    "SIGNING_SECRET": SIGNING_SECRET,
    "SIGNATURE_HEADER": SIGNATURE_HEADER,
    "REDIS_URL": REDIS_URL,
    "REDIS_TIMEOUT_SECONDS": "0.2",
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


def refund_payload(
    amount: object = 100,
    *,
    tx_ref: object = "TX1001",
    include_tx_ref: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if include_tx_ref:
        payload["tx_ref_3126"] = tx_ref
    payload["refund_aed"] = amount
    payload["currency"] = "AED"
    return {"event": "refund.requested", "payload": payload}


def post_body(client: TestClient, body: bytes):
    return client.post(
        "/api/guard",
        content=body,
        headers={
            "content-type": "application/json",
            SIGNATURE_HEADER: sign_body(body),
        },
    )


def build_client(manager: InMemoryRedisManager | None = None) -> TestClient:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = manager or InMemoryRedisManager()
    return TestClient(application)


# ---------------------------------------------------------------------------
# Transaction-reference validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tx_ref",
    ["TX1001", "TX-1001", "tx_1001", "A.B-9", "TX/1001", "TX@1001", "tx:3126#1", "-TX1001"],
)
def test_valid_transaction_references_are_normalized(tx_ref: str) -> None:
    assert validate_transaction_reference(tx_ref) == tx_ref


def test_integer_transaction_reference_is_accepted_as_text() -> None:
    assert validate_transaction_reference(123) == "123"


def test_transaction_reference_is_trimmed() -> None:
    assert validate_transaction_reference("  TX1001  ") == "TX1001"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_empty_transaction_reference_is_rejected(value) -> None:
    with pytest.raises(TransactionReferenceError) as excinfo:
        validate_transaction_reference(value)

    assert excinfo.value.code == "missing_tx_ref"


@pytest.mark.parametrize("value", [1.5, True, [], {}, ["TX1"]])
def test_non_string_transaction_reference_is_rejected(value) -> None:
    with pytest.raises(TransactionReferenceError) as excinfo:
        validate_transaction_reference(value)

    assert excinfo.value.code == "invalid_tx_ref"


@pytest.mark.parametrize(
    "value",
    [
        "A" * 129,
        "TX 1001",
        "TX\n1001",
        "TX\t1001",
        "TX\x001001",
        "TX\x7f1001",
    ],
)
def test_invalid_transaction_reference_values_are_rejected(value: str) -> None:
    with pytest.raises(TransactionReferenceError) as excinfo:
        validate_transaction_reference(value)

    assert excinfo.value.code == "invalid_tx_ref"


def test_idempotency_key_uses_the_required_format() -> None:
    assert idempotency_key("TX1001") == "pinhire:idempotency:TX1001"
    assert IDEMPOTENCY_KEY_PREFIX == "pinhire:idempotency:"


def test_default_idempotency_ttl_is_300_seconds() -> None:
    assert DEFAULT_LOCK_TTL_SECONDS == 300


# ---------------------------------------------------------------------------
# First request, duplicates, different references
# ---------------------------------------------------------------------------


def test_first_transaction_request_is_processed() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        response = post_body(client, encode_json(refund_payload(100)))

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert manager.remaining_ttl("pinhire:idempotency:TX1001") is not None


def test_repeated_transaction_request_is_a_duplicate() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        first = post_body(client, encode_json(refund_payload(100)))
        second = post_body(client, encode_json(refund_payload(100)))

    assert first.status_code == 200
    assert first.json()["status"] == "processed"
    assert second.status_code == 409
    assert second.json() == {"status": "duplicate"}


def test_same_transaction_sent_three_times_processes_once() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        responses = [
            post_body(client, encode_json(refund_payload(100)))
            for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 409, 409]
    assert responses[0].json()["status"] == "processed"
    assert responses[1].json() == {"status": "duplicate"}
    assert responses[2].json() == {"status": "duplicate"}


def test_different_transaction_references_are_processed_independently() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        first = post_body(
            client,
            encode_json(refund_payload(100, tx_ref="TX-A")),
        )
        second = post_body(
            client,
            encode_json(refund_payload(100, tx_ref="TX-B")),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "processed"


def test_completed_transaction_remains_protected_for_300_seconds() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        first = post_body(client, encode_json(refund_payload(100)))

        assert first.status_code == 200
        assert (
            asyncio.run(get_transaction_state(redis=manager, tx_ref="TX1001"))
            == "completed"
        )

        remaining = manager.remaining_ttl("pinhire:idempotency:TX1001")
        assert remaining is not None
        assert 299 <= remaining <= DEFAULT_LOCK_TTL_SECONDS

        manager.advance(299)
        still_duplicate = post_body(client, encode_json(refund_payload(100)))

        manager.advance(2)
        after_expiry = post_body(client, encode_json(refund_payload(100)))

    assert still_duplicate.status_code == 409
    assert still_duplicate.json() == {"status": "duplicate"}
    assert after_expiry.status_code == 200
    assert after_expiry.json()["status"] == "processed"


def test_processing_lock_expires_after_300_seconds() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        first = post_body(client, encode_json(refund_payload(100)))
        assert first.status_code == 200

        manager.advance(301)
        retried = post_body(client, encode_json(refund_payload(100)))

    assert retried.status_code == 200


# ---------------------------------------------------------------------------
# Invalid references and Redis failures
# ---------------------------------------------------------------------------


def test_missing_transaction_reference_is_rejected_before_redis() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        response = post_body(
            client,
            encode_json(refund_payload(100, include_tx_ref=False)),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_tx_ref"
    assert response.json()["error"]["details"]["field"] == "payload.tx_ref_3126"
    assert manager.remaining_ttl("pinhire:idempotency:TX1001") is None


@pytest.mark.parametrize(
    "tx_ref",
    ["", "   ", 1.5, True, "A" * 129, "TX 1001", "TX\n1001"],
)
def test_invalid_transaction_reference_is_rejected(tx_ref) -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        response = post_body(
            client,
            encode_json(refund_payload(100, tx_ref=tx_ref)),
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] in {
        "missing_tx_ref",
        "invalid_tx_ref",
    }


def test_other_transaction_fields_are_not_used() -> None:
    manager = InMemoryRedisManager()
    body = encode_json(
        {
            "event": "refund.requested",
            "payload": {
                "tx_ref": "TX-OTHER-FIELD",
                "refund_aed": 100,
                "currency": "AED",
            },
        }
    )
    with build_client(manager) as client:
        response = post_body(client, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_tx_ref"


def test_redis_unavailable_fails_safely_without_in_memory_fallback(
    monkeypatch,
) -> None:
    manager = InMemoryRedisManager(fail_operations=True)
    calls = 0
    original = webhook_module._process_refund_operation

    async def counting_operation(event, *, limit_aed):
        nonlocal calls
        calls += 1
        return await original(event, limit_aed=limit_aed)

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        counting_operation,
    )

    with build_client(manager) as client:
        failed = post_body(client, encode_json(refund_payload(100)))

        manager.fail_operations = False
        retried = post_body(client, encode_json(refund_payload(100)))

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "idempotency_unavailable"
    assert "in-memory" not in failed.text.lower()
    assert calls == 1  # only the successful retry ran the refund operation

    # The failed attempt was not recorded anywhere, so a later retry runs.
    assert retried.status_code == 200
    assert retried.json()["status"] == "processed"


def test_hmac_is_required_before_idempotency() -> None:
    manager = InMemoryRedisManager()
    with build_client(manager) as client:
        response = client.post(
            "/api/guard",
            content=encode_json(refund_payload(100)),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_signature"
    assert manager.remaining_ttl("pinhire:idempotency:TX1001") is None


# ---------------------------------------------------------------------------
# Processing failure and recovery
# ---------------------------------------------------------------------------


def test_processing_failure_releases_lock_and_allows_retry(monkeypatch) -> None:
    manager = InMemoryRedisManager()
    original = webhook_module._process_refund_operation
    calls = 0

    async def flaky_operation(event, *, limit_aed):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated processing failure")
        return await original(event, limit_aed=limit_aed)

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        flaky_operation,
    )

    with build_client(manager) as client:
        failed = post_body(client, encode_json(refund_payload(100)))
        lock_after_failure = asyncio.run(
            get_transaction_state(
                redis=manager,
                tx_ref="TX1001",
            )
        )
        retried = post_body(client, encode_json(refund_payload(100)))

    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "refund_processing_failed"
    assert lock_after_failure is None
    assert retried.status_code == 200
    assert retried.json()["status"] == "processed"
    assert calls == 2


def test_failed_processing_is_never_reported_as_processed(monkeypatch) -> None:
    manager = InMemoryRedisManager()

    async def always_fails(event, *, limit_aed):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        always_fails,
    )

    with build_client(manager) as client:
        response = post_body(client, encode_json(refund_payload(100)))

    assert response.status_code == 500
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == "refund_processing_failed"
    assert "processed" not in response.text


def test_completion_failure_keeps_lock_so_refund_never_runs_twice(
    monkeypatch,
) -> None:
    manager = InMemoryRedisManager()
    original = webhook_module._process_refund_operation
    calls = 0

    async def counting_operation(event, *, limit_aed):
        nonlocal calls
        calls += 1
        return await original(event, limit_aed=limit_aed)

    async def failing_complete(*args, **kwargs):
        raise RedisOperationError("Redis EVAL complete failed.")

    monkeypatch.setattr(
        webhook_module,
        "_process_refund_operation",
        counting_operation,
    )
    monkeypatch.setattr(manager, "compare_and_set_with_expiration", failing_complete)

    with build_client(manager) as client:
        failed = post_body(client, encode_json(refund_payload(100)))
        state_after_failure = asyncio.run(
            get_transaction_state(redis=manager, tx_ref="TX1001")
        )
        retried = post_body(client, encode_json(refund_payload(100)))

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "idempotency_unavailable"
    assert state_after_failure == "processing"
    assert retried.status_code == 409
    assert retried.json() == {"status": "duplicate"}
    assert calls == 1


# ---------------------------------------------------------------------------
# Direct service tests
# ---------------------------------------------------------------------------


def test_claim_transaction_is_atomic_and_idempotent() -> None:
    async def scenario() -> None:
        manager = InMemoryRedisManager()
        await manager.connect()

        first = await claim_transaction(
            redis=manager,
            tx_ref="TX-CLAIM",
            ttl_seconds=300,
        )
        second = await claim_transaction(
            redis=manager,
            tx_ref="TX-CLAIM",
            ttl_seconds=300,
        )

        assert first is True
        assert second is False

    asyncio.run(scenario())


def test_complete_and_release_require_owner_token() -> None:
    async def scenario() -> None:
        manager = InMemoryRedisManager()
        await manager.connect()

        lock = await acquire_transaction_lock(
            redis=manager,
            tx_ref="TX-OWNER",
            ttl_seconds=300,
        )
        assert lock.acquired is True

        # A different owner cannot complete or delete this lock.
        forged = type(lock)(
            tx_ref=lock.tx_ref,
            key=lock.key,
            owner_token="other-owner",
            acquired=True,
            state="processing",
            value='{"owner":"other-owner"}',
        )
        assert await complete_transaction(redis=manager, lock=forged) is False
        assert await manager.compare_and_delete(lock.key, forged.value) is False
        assert await manager.exists(lock.key) is True

        assert await complete_transaction(redis=manager, lock=lock) is True
        assert (
            await get_transaction_state(redis=manager, tx_ref="TX-OWNER")
            == "completed"
        )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Optional real Redis integration
# ---------------------------------------------------------------------------


def test_real_redis_sets_a_300_second_ttl_when_available() -> None:
    async def scenario() -> None:
        manager = RedisManager(REDIS_URL, timeout_seconds=0.3)
        if not await manager.connect():
            pytest.skip("Local Redis is not available")

        tx_ref = "PH6-TTL-REAL"
        key = idempotency_key(tx_ref)
        try:
            await manager.delete(key)
            lock = await acquire_transaction_lock(
                redis=manager,
                tx_ref=tx_ref,
                ttl_seconds=DEFAULT_LOCK_TTL_SECONDS,
            )
            assert lock.acquired is True

            ttl = await manager.client.ttl(key)
            assert 295 <= int(ttl) <= 300

            assert await complete_transaction(
                redis=manager,
                lock=lock,
                ttl_seconds=DEFAULT_LOCK_TTL_SECONDS,
            ) is True

            ttl = await manager.client.ttl(key)
            assert 295 <= int(ttl) <= 300
            assert await claim_transaction(
                redis=manager,
                tx_ref=tx_ref,
                ttl_seconds=DEFAULT_LOCK_TTL_SECONDS,
            ) is False
        finally:
            await manager.delete(key)
            await manager.close()

    asyncio.run(scenario())
