"""Phase 3 tests for HMAC-SHA256 signature generation and verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from app.config import load_settings
from app.main import create_app
from app.routes import webhook as webhook_module
from app.services import security as security_module
from app.services.security import (
    MalformedSignatureError,
    MissingSignatureError,
    generate_signature,
    normalize_signature,
    verify_signature,
)
from tests.fakes import InMemoryRedisManager

SIGNING_SECRET = "security-test-signing-secret"
SIGNATURE_HEADER = "x-pinhire-hmac-3126"

# RFC 4231, test case 2.
RFC_KEY = "Jefe"
RFC_BODY = b"what do ya want for nothing?"
RFC_DIGEST = (
    "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
)

TEST_ENV = {
    "APP_ENV": "test",
    "SIGNING_SECRET": SIGNING_SECRET,
    "SIGNATURE_HEADER": SIGNATURE_HEADER,
    "REDIS_URL": "redis://127.0.0.1:6379/15",
    "REDIS_TIMEOUT_SECONDS": "0.1",
}


def sign(body: bytes, secret: str = SIGNING_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def encode_json(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def request(
    application,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Call the ASGI app directly with HTTPX."""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http_client:
            return await http_client.request(
                method,
                path,
                content=body,
                headers=headers or {},
            )

    return asyncio.run(call())


def test_generate_signature_matches_rfc4231_vector() -> None:
    assert generate_signature(RFC_BODY, RFC_KEY) == RFC_DIGEST


def test_verify_signature_accepts_a_valid_signature() -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=RFC_DIGEST,
            secret=RFC_KEY,
        )
        is True
    )


def test_verify_signature_accepts_uppercase_hex() -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=RFC_DIGEST.upper(),
            secret=RFC_KEY,
        )
        is True
    )


def test_verify_signature_rejects_an_incorrect_signature() -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature="0" * 64,
            secret=RFC_KEY,
        )
        is False
    )


@pytest.mark.parametrize("signature", [None, "", "   "])
def test_verify_signature_rejects_missing_signatures(signature) -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=signature,
            secret=RFC_KEY,
        )
        is False
    )


@pytest.mark.parametrize(
    "signature",
    [
        "not-hex",
        "abc",
        "g" * 64,
        "0" * 63,
        "0" * 65,
        "sha256=" + "0" * 64,
    ],
)
def test_verify_signature_rejects_malformed_signatures(signature: str) -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=signature,
            secret=RFC_KEY,
        )
        is False
    )


def test_verify_signature_rejects_a_modified_body() -> None:
    signature = generate_signature(b'{"amount":100}', SIGNING_SECRET)

    assert (
        verify_signature(
            raw_body=b'{"amount":900}',
            signature=signature,
            secret=SIGNING_SECRET,
        )
        is False
    )


def test_verify_signature_rejects_a_different_secret() -> None:
    signature = generate_signature(RFC_BODY, RFC_KEY)

    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=signature,
            secret="a-different-secret",
        )
        is False
    )


def test_verify_signature_uses_the_original_raw_bytes() -> None:
    spaced_body = b'{"event": "faq.query", "payload": {"question": "Hi"}}'
    compact_body = b'{"event":"faq.query","payload":{"question":"Hi"}}'
    spaced_signature = generate_signature(spaced_body, SIGNING_SECRET)

    assert spaced_signature != generate_signature(compact_body, SIGNING_SECRET)
    assert (
        verify_signature(
            raw_body=spaced_body,
            signature=spaced_signature,
            secret=SIGNING_SECRET,
        )
        is True
    )
    assert (
        verify_signature(
            raw_body=compact_body,
            signature=spaced_signature,
            secret=SIGNING_SECRET,
        )
        is False
    )


def test_verify_signature_returns_false_when_no_secret_is_configured() -> None:
    assert (
        verify_signature(
            raw_body=RFC_BODY,
            signature=RFC_DIGEST,
            secret=None,
        )
        is False
    )


def test_normalize_signature_rejects_missing_values() -> None:
    with pytest.raises(MissingSignatureError):
        normalize_signature(None)
    with pytest.raises(MissingSignatureError):
        normalize_signature("   ")


def test_normalize_signature_rejects_malformed_values() -> None:
    with pytest.raises(MalformedSignatureError):
        normalize_signature("not-a-signature")


def test_normalize_signature_lowercases_valid_values() -> None:
    assert normalize_signature("A" * 64) == "a" * 64


def test_generate_signature_requires_a_secret() -> None:
    with pytest.raises(ValueError):
        generate_signature(b"body", "")


def test_generate_signature_requires_bytes() -> None:
    with pytest.raises(TypeError):
        generate_signature("body", SIGNING_SECRET)  # type: ignore[arg-type]


def test_invalid_signature_cannot_reach_event_processing(monkeypatch) -> None:
    application = create_app(load_settings(env=TEST_ENV))
    body = encode_json(
        {
            "event": "refund.requested",
            "payload": {"tx_ref_3126": "TX1001", "refund_aed": 200},
        }
    )

    def must_not_run(*args, **kwargs):
        raise AssertionError("event processing ran before signature verification")

    monkeypatch.setattr(webhook_module, "parse_webhook_event", must_not_run)
    monkeypatch.setattr(webhook_module, "_process_refund_event", must_not_run)
    monkeypatch.setattr(webhook_module, "_process_faq_event", must_not_run)

    response = request(
        application,
        "POST",
        "/api/guard",
        body=body,
        headers={SIGNATURE_HEADER: "0" * 64},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_invalid_signature_is_rejected_before_json_parsing() -> None:
    application = create_app(load_settings(env=TEST_ENV))

    response = request(
        application,
        "POST",
        "/api/guard",
        body=b'{"event":',
        headers={SIGNATURE_HEADER: "0" * 64},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_missing_signature_is_rejected() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    body = encode_json({"event": "faq.query", "payload": {"question": "Hi"}})

    response = request(application, "POST", "/api/guard", body=body)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_signature"


def test_valid_signature_allows_processing() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    body = encode_json({"event": "faq.query", "payload": {"question": "Hi"}})

    response = request(
        application,
        "POST",
        "/api/guard",
        body=body,
        headers={SIGNATURE_HEADER: sign(body)},
    )

    assert response.status_code == 200
    assert response.json()["data"]["question"] == "Hi"


def test_valid_signature_with_malformed_json_returns_400() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    body = b'{"event":'

    response = request(
        application,
        "POST",
        "/api/guard",
        body=body,
        headers={SIGNATURE_HEADER: sign(body)},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_health_does_not_require_a_signature() -> None:
    application = create_app(load_settings(env=TEST_ENV))

    response = request(application, "GET", "/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] in {"healthy", "degraded"}
    assert body["app"] == "ok"
    assert body["redis"] in {"connected", "disconnected"}


def test_changing_one_raw_body_byte_invalidates_the_signature() -> None:
    original = b'{"event":"faq.query","payload":{"question":"Hi"}}'
    mutated = b'{"event":"faq.query","payload":{"question":"Hj"}}'
    signature = sign(original)

    assert len(original) == len(mutated)
    assert sum(left != right for left, right in zip(original, mutated)) == 1
    assert generate_signature(mutated, SIGNING_SECRET) != signature
    assert verify_signature(
        raw_body=mutated,
        signature=signature,
        secret=SIGNING_SECRET,
    ) is False

    application = create_app(load_settings(env=TEST_ENV))
    response = request(
        application,
        "POST",
        "/api/guard",
        body=mutated,
        headers={SIGNATURE_HEADER: signature},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_signature_verification_uses_hmac_compare_digest(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    real_compare_digest = hmac.compare_digest

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(security_module.hmac, "compare_digest", spy)

    assert verify_signature(
        raw_body=RFC_BODY,
        signature=RFC_DIGEST,
        secret=RFC_KEY,
    ) is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    "headers, expected_code",
    [
        ({SIGNATURE_HEADER: "0" * 64}, "invalid_signature"),
        ({}, "missing_signature"),
    ],
)
def test_rejected_signatures_cannot_run_business_logic_or_write_idempotency(
    headers: dict[str, str],
    expected_code: str,
    monkeypatch,
) -> None:
    application = create_app(load_settings(env=TEST_ENV))
    manager = InMemoryRedisManager()
    application.state.redis = manager

    def must_not_run(*args, **kwargs):
        raise AssertionError(
            "business logic ran for a request with an unacceptable signature"
        )

    monkeypatch.setattr(webhook_module, "parse_webhook_event", must_not_run)
    monkeypatch.setattr(webhook_module, "_process_refund_event", must_not_run)
    monkeypatch.setattr(webhook_module, "_process_faq_event", must_not_run)
    monkeypatch.setattr(webhook_module, "execute_once", must_not_run)

    events = [
        {
            "event": "refund.requested",
            "payload": {"tx_ref_3126": "TX-NO-BUSINESS", "refund_aed": 255},
        },
        {"event": "faq.query", "payload": {"question": "What is your refund policy?"}},
    ]

    for event in events:
        response = request(
            application,
            "POST",
            "/api/guard",
            body=encode_json(event),
            headers=headers,
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == expected_code

    assert [
        key
        for key in manager._values
        if key.startswith("pinhire:idempotency:")
    ] == []
