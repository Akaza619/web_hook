"""Phase 2-4 tests for POST /api/guard.

Every guard request is signed with HMAC-SHA256 over the exact bytes sent.
FastAPI's TestClient is HTTPX-based; one test also uses HTTPX's ASGITransport
directly. Refund requests are validated against the configured AED limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.fakes import InMemoryRedisManager

SIGNING_SECRET = "test-only-signing-secret"
SIGNATURE_HEADER = "x-pinhire-hmac-3126"

TEST_ENV = {
    "APP_ENV": "test",
    "APP_NAME": "Pinhire Webhook Server",
    "APP_VERSION": "0.1.0",
    "SIGNING_SECRET": SIGNING_SECRET,
    "SIGNATURE_HEADER": SIGNATURE_HEADER,
    "REDIS_URL": "redis://localhost:6379/15",
    "REDIS_TIMEOUT_SECONDS": "0.1",
}

REFUND_PAYLOAD = {
    "event": "refund.requested",
    "payload": {"tx_ref_3126": "TX1001", "refund_aed": 200},
}

FAQ_PAYLOAD = {
    "event": "faq.query",
    "payload": {"question": "What is your refund policy?"},
}


def encode_json(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_body(body: bytes, secret: str = SIGNING_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post_signed(
    client: TestClient,
    body: bytes,
    *,
    secret: str = SIGNING_SECRET,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = {
        "content-type": "application/json",
        SIGNATURE_HEADER: sign_body(body, secret),
    }
    if headers:
        request_headers.update(headers)
    return client.post("/api/guard", content=body, headers=request_headers)


def post_payload(client: TestClient, payload: object, **kwargs) -> httpx.Response:
    return post_signed(client, encode_json(payload), **kwargs)


@pytest.fixture()
def client() -> TestClient:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = InMemoryRedisManager()
    with TestClient(application) as test_client:
        yield test_client


def test_guard_is_available(client: TestClient) -> None:
    response = post_payload(client, REFUND_PAYLOAD)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_guard_works_through_raw_httpx_asgi_transport() -> None:
    """Exercise the endpoint directly with HTTPX (not only TestClient)."""

    application = create_app(load_settings(env=TEST_ENV))
    body = encode_json(FAQ_PAYLOAD)

    async def call_guard() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http_client:
            return await http_client.post(
                "/api/guard",
                content=body,
                headers={SIGNATURE_HEADER: sign_body(body)},
            )

    response = asyncio.run(call_guard())

    assert response.status_code == 200
    assert response.json()["data"]["event"] == "faq.query"


def test_refund_event_is_recognized(client: TestClient) -> None:
    response = post_payload(client, REFUND_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}


def test_faq_event_is_recognized(client: TestClient) -> None:
    response = post_payload(client, FAQ_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["data"]["event"] == "faq.query"
    assert body["data"]["event_type"] == "faq"
    assert body["data"]["question"] == "What is your refund policy?"
    assert body["data"]["processed"] is True
    assert body["data"]["answered"] is True
    assert body["data"]["answer"]
    assert body["data"]["placeholder"] is True
    assert body["data"]["cache_status"] in {"hit", "miss"}
    assert body["data"]["latency_ms"] >= 0
    assert response.headers["x-faq-processing-ms"]
    assert response.headers["x-processing-time-ms"]


def test_raw_json_body_is_parsed_without_a_content_type(client: TestClient) -> None:
    body = b'{"event":"faq.query","payload":{"question":"Do you ship internationally?"}}'
    response = client.post(
        "/api/guard",
        content=body,
        headers={SIGNATURE_HEADER: sign_body(body)},
    )

    assert response.status_code == 200
    assert response.json()["data"]["question"] == "Do you ship internationally?"


def test_empty_body_is_rejected_after_signature_verification(client: TestClient) -> None:
    response = post_signed(client, b"")

    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "empty_body"
    assert body["error"]["request_id"]


def test_whitespace_only_body_is_rejected(client: TestClient) -> None:
    response = post_signed(client, b"   \n\t ")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_body"


def test_malformed_json_is_rejected_with_a_valid_signature(client: TestClient) -> None:
    response = post_signed(client, b'{"event": "refund.requested", "payload": {')

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_json"
    assert body["error"]["details"]["line"] >= 1


def test_non_object_json_is_rejected(client: TestClient) -> None:
    response = post_signed(client, b'["refund.requested"]')

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json_structure"


def test_missing_event_type_is_rejected(client: TestClient) -> None:
    response = post_payload(client, {"payload": {"tx_ref_3126": "TX1001"}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_event_type"


def test_non_string_event_type_is_rejected(client: TestClient) -> None:
    response = post_payload(client, {"event": 123, "payload": {}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_event_type"


def test_missing_payload_is_rejected(client: TestClient) -> None:
    response = post_payload(client, {"event": "refund.requested"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_payload"


def test_non_object_payload_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        {"event": "faq.query", "payload": "not-an-object"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payload"


def test_unsupported_event_type_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        {"event": "order.created", "payload": {"id": "ORD-1"}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "unsupported_event"
    assert body["error"]["details"]["event"] == "order.created"
    assert "refund.requested" in body["error"]["details"]["supported_events"]


def test_refund_without_transaction_reference_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        {"event": "refund.requested", "payload": {"refund_aed": 200}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "missing_tx_ref"
    assert body["error"]["details"]["field"] == "payload.tx_ref_3126"


def test_blank_transaction_reference_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        {
            "event": "refund.requested",
            "payload": {"tx_ref_3126": "   ", "refund_aed": 200},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_tx_ref"


def test_faq_without_question_is_rejected(client: TestClient) -> None:
    response = post_payload(client, {"event": "faq.query", "payload": {}})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "missing_question"
    assert body["error"]["details"]["field"] == "payload.question"


def test_guard_returns_request_id_on_success(client: TestClient) -> None:
    response = post_payload(
        client,
        FAQ_PAYLOAD,
        headers={"x-request-id": "guard-correlation-id"},
    )

    assert response.headers["x-request-id"] == "guard-correlation-id"


def test_missing_signature_is_rejected(client: TestClient) -> None:
    body = encode_json(REFUND_PAYLOAD)
    response = client.post(
        "/api/guard",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    body_json = response.json()
    assert body_json["error"]["code"] == "missing_signature"
    assert body_json["error"]["request_id"]


def test_invalid_signature_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        REFUND_PAYLOAD,
        headers={SIGNATURE_HEADER: "0" * 64},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_malformed_signature_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        REFUND_PAYLOAD,
        headers={SIGNATURE_HEADER: "not-a-hex-signature"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "malformed_signature"


def test_modified_body_is_rejected(client: TestClient) -> None:
    original_body = encode_json(REFUND_PAYLOAD)
    modified_body = encode_json(
        {
            "event": "refund.requested",
            "payload": {"tx_ref_3126": "TX-ATTACKER", "refund_aed": 200},
        }
    )
    response = post_signed(
        client,
        modified_body,
        headers={SIGNATURE_HEADER: sign_body(original_body)},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_signature_from_a_different_secret_is_rejected(client: TestClient) -> None:
    response = post_payload(
        client,
        REFUND_PAYLOAD,
        secret="a-different-signing-secret",
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_signature_must_cover_exact_raw_bytes(client: TestClient) -> None:
    spaced_body = (
        b'{ "event" : "faq.query" , "payload" : '
        b'{ "question" : "What is your refund policy?" } }'
    )
    compact_body = encode_json(FAQ_PAYLOAD)

    wrong_signature_response = post_signed(
        client,
        spaced_body,
        headers={SIGNATURE_HEADER: sign_body(compact_body)},
    )
    correct_signature_response = post_signed(client, spaced_body)

    assert wrong_signature_response.status_code == 401
    # A successful FAQ request returns 200; the OpenAPI document says so too.
    assert correct_signature_response.status_code == 200
    assert correct_signature_response.json()["data"]["answered"] is True


def test_openapi_documents_the_actual_response_status_codes() -> None:
    """Regression: the route once advertised 202 while returning 200."""

    application = create_app(load_settings(env=TEST_ENV))
    operation = application.openapi()["paths"]["/api/guard"]["post"]
    documented = operation["responses"]

    assert "200" in documented
    assert "202" not in documented
    for status_code in ("400", "401", "403", "409", "503"):
        assert status_code in documented, (
            f"/api/guard does not document response {status_code}"
        )


def test_production_requires_a_valid_signature() -> None:
    production_settings = load_settings(
        env={
            "APP_ENV": "production",
            "SIGNING_SECRET": "prod-signing-secret-abc123",
            "REDIS_URL": "redis://redis.internal:6379/0",
            "REDIS_TIMEOUT_SECONDS": "0.1",
        }
    )
    application = create_app(production_settings)
    application.state.redis = InMemoryRedisManager()
    body = encode_json(REFUND_PAYLOAD)

    with TestClient(application) as test_client:
        unsigned_response = test_client.post("/api/guard", content=body)
        signed_response = test_client.post(
            "/api/guard",
            content=body,
            headers={
                SIGNATURE_HEADER: sign_body(body, "prod-signing-secret-abc123"),
            },
        )

    assert unsigned_response.status_code == 401
    assert unsigned_response.json()["error"]["code"] == "missing_signature"
    assert signed_response.status_code == 200


def test_guard_is_unavailable_when_no_secret_is_configured() -> None:
    application = create_app(
        load_settings(env={"APP_ENV": "test", "REDIS_TIMEOUT_SECONDS": "0.1"})
    )
    body = encode_json(REFUND_PAYLOAD)

    with TestClient(application) as test_client:
        response = test_client.post(
            "/api/guard",
            content=body,
            headers={SIGNATURE_HEADER: sign_body(body)},
        )
        health_response = test_client.get("/health")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "signature_verification_unavailable"
    assert health_response.status_code == 200


def test_custom_signature_header_is_used() -> None:
    custom_header = "x-custom-hmac"
    application = create_app(
        load_settings(
            env={
                **TEST_ENV,
                "SIGNATURE_HEADER": custom_header,
            }
        )
    )
    body = encode_json(FAQ_PAYLOAD)

    with TestClient(application) as test_client:
        default_header_response = test_client.post(
            "/api/guard",
            content=body,
            headers={SIGNATURE_HEADER: sign_body(body)},
        )
        custom_header_response = test_client.post(
            "/api/guard",
            content=body,
            headers={custom_header: sign_body(body)},
        )

    assert default_header_response.status_code == 401
    assert default_header_response.json()["error"]["code"] == "missing_signature"
    assert custom_header_response.status_code == 200
