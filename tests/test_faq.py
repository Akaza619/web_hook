"""Phase 7 tests for FAQ normalization, caching, fallbacks, and latency.

Answers come from the centralized catalog in ``app/services/faq.py``. Redis is
only a cache: predefined questions must stay answerable when it is unavailable.
Latency is measured with ``time.perf_counter()``; the final test starts a real
uvicorn server and measures complete HTTP round trips over localhost.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import socket
import threading
import time
from pathlib import Path
from statistics import mean, median

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.services import faq as faq_module
from app.services.faq import (
    FAQ_CACHE_KEY_PREFIX,
    FAQ_ENTRIES,
    UNKNOWN_FAQ_ANSWER,
    FAQQuestionError,
    answer_faq,
    faq_cache_key,
    get_cached_faq,
    lookup_predefined_faq,
    normalize_question,
    preload_faq_cache,
)
from tests.fakes import InMemoryRedisManager

SIGNING_SECRET = "faq-test-signing-secret"
SIGNATURE_HEADER = "x-pinhire-hmac-3126"
FAQ_CACHE_TTL_SECONDS = 120

TEST_ENV = {
    "APP_ENV": "test",
    "SIGNING_SECRET": SIGNING_SECRET,
    "SIGNATURE_HEADER": SIGNATURE_HEADER,
    "REDIS_URL": "redis://127.0.0.1:6379/15",
    "REDIS_TIMEOUT_SECONDS": "0.1",
    "FAQ_CACHE_TTL_SECONDS": str(FAQ_CACHE_TTL_SECONDS),
}

QUESTION = "What is your refund policy?"
FAQ_PAYLOAD = {"event": "faq.query", "payload": {"question": QUESTION}}
REFUND_PAYLOAD = {
    "event": "refund.requested",
    "payload": {"tx_ref_3126": "TX-FAQ-1001", "refund_aed": 200},
}

# Every spelling below must resolve to the same normalized catalog key.
NORMALIZATION_VARIANTS = [
    "What is your refund policy?",
    "what is your refund policy",
    " WHAT IS YOUR REFUND POLICY? ",
    "  What   is\tyour  refund policy?  ",
    "What is your refund policy!!!",
    "What - is - your - refund - policy",
]


def encode_json(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def sign_body(body: bytes, secret: str = SIGNING_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post_signed(client: TestClient, body: bytes) -> httpx.Response:
    return client.post(
        "/api/guard",
        content=body,
        headers={
            "content-type": "application/json",
            SIGNATURE_HEADER: sign_body(body),
        },
    )


def run(coroutine):
    return asyncio.run(coroutine)


def latency_stats(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "min": ordered[0],
        "mean": mean(ordered),
        "median": median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
    }


def report_latency(label: str, samples_ms: list[float]) -> dict[str, float]:
    stats = latency_stats(samples_ms)
    print(
        f"\n[{label}] n={len(samples_ms)} "
        f"min={stats['min']:.2f}ms mean={stats['mean']:.2f}ms "
        f"median={stats['median']:.2f}ms p95={stats['p95']:.2f}ms "
        f"max={stats['max']:.2f}ms"
    )
    return stats


@pytest.fixture()
def redis_manager() -> InMemoryRedisManager:
    return InMemoryRedisManager()


@pytest.fixture()
def client(redis_manager: InMemoryRedisManager) -> TestClient:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = redis_manager
    with TestClient(application) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Question normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", NORMALIZATION_VARIANTS)
def test_question_normalization_variants_share_one_entry(variant: str) -> None:
    normalized = normalize_question(variant)

    assert normalized == "what is your refund policy"
    entry = lookup_predefined_faq(normalized)
    assert entry is not None
    assert entry.question == QUESTION


def test_normalization_keeps_unicode_letters() -> None:
    assert normalize_question("  How  CAN I   CONTACT support?  ") == (
        "how can i contact support"
    )


@pytest.mark.parametrize("question", [None, 123, 4.5, ["a"], {"q": "a"}, b"bytes"])
def test_non_string_questions_are_rejected(question: object) -> None:
    with pytest.raises(FAQQuestionError) as excinfo:
        normalize_question(question)  # type: ignore[arg-type]

    assert excinfo.value.code == "invalid_question"
    assert excinfo.value.details == {"field": "payload.question"}


@pytest.mark.parametrize("question", ["", "   ", "\n\t ", "???"])
def test_empty_questions_are_rejected(question: str) -> None:
    with pytest.raises(FAQQuestionError) as excinfo:
        normalize_question(question)

    assert excinfo.value.code == "missing_question"


def test_faq_cache_key_uses_the_normalized_question() -> None:
    assert faq_cache_key(normalize_question("  WHAT is your refund policy? ")) == (
        f"{FAQ_CACHE_KEY_PREFIX}what is your refund policy"
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_contains_the_documented_questions() -> None:
    questions = {entry.question for entry in FAQ_ENTRIES}

    assert questions == {
        "What is your refund policy?",
        "How can I contact support?",
        "How long does a refund take?",
        "What payment methods are accepted?",
        "How can I check my transaction status?",
    }
    assert all(entry.placeholder is True for entry in FAQ_ENTRIES)


def test_known_question_returns_the_configured_answer() -> None:
    result = run(answer_faq(question=QUESTION))

    entry = lookup_predefined_faq(normalize_question(QUESTION))
    assert entry is not None
    assert result.matched is True
    assert result.answer == entry.answer
    assert result.matched_question == QUESTION
    assert result.placeholder is True
    assert result.source == "predefined"
    assert result.cache_hit is False
    assert result.cache_status == "unavailable"  # no Redis client supplied
    assert result.latency_ms >= 0.0


def test_unknown_question_returns_a_safe_placeholder() -> None:
    redis = InMemoryRedisManager()

    result = run(
        answer_faq(question="Do you ship internationally?", redis=redis)
    )

    assert result.matched is False
    assert result.answer == UNKNOWN_FAQ_ANSWER
    assert result.matched_question is None
    assert result.source == "unknown"
    assert result.cache_hit is False
    assert result.cache_status == "miss"
    assert run(redis.exists(faq_cache_key("do you ship internationally"))) is False


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_repeated_question_is_served_from_cache() -> None:
    redis = InMemoryRedisManager()

    first = run(
        answer_faq(
            question=QUESTION,
            redis=redis,
            ttl_seconds=FAQ_CACHE_TTL_SECONDS,
        )
    )
    second = run(
        answer_faq(
            question="  WHAT IS your refund policy??? ",
            redis=redis,
            ttl_seconds=FAQ_CACHE_TTL_SECONDS,
        )
    )

    # First lookup misses and populates Redis; the second is a normalized hit.
    assert first.source == "predefined"
    assert first.cache_status == "miss"
    assert first.cache_hit is False
    assert second.source == "cache"
    assert second.cache_status == "hit"
    assert second.cache_hit is True
    assert second.answer == first.answer
    assert second.matched_question == QUESTION

    ttl_remaining = redis.remaining_ttl(faq_cache_key(normalize_question(QUESTION)))
    assert ttl_remaining is not None
    assert 0 < ttl_remaining <= FAQ_CACHE_TTL_SECONDS


def test_cache_miss_falls_back_to_the_catalog_and_stores_the_answer() -> None:
    redis = InMemoryRedisManager()
    key = faq_cache_key(normalize_question(QUESTION))

    assert run(get_cached_faq(redis, normalize_question(QUESTION))) is None
    result = run(answer_faq(question=QUESTION, redis=redis))
    cached = run(get_cached_faq(redis, normalize_question(QUESTION)))

    assert result.cache_status == "miss"
    assert result.source == "predefined"
    assert cached is not None
    assert cached.answer == result.answer
    assert redis.remaining_ttl(key) is not None


def test_corrupt_cache_entry_is_ignored_and_repaired() -> None:
    redis = InMemoryRedisManager()
    normalized = normalize_question(QUESTION)
    key = faq_cache_key(normalized)
    run(redis.set(key, "not-json-at-all"))

    result = run(answer_faq(question=QUESTION, redis=redis))
    repaired = run(get_cached_faq(redis, normalized))

    assert result.source == "predefined"
    assert result.answer.startswith("Configurable placeholder")
    assert repaired is not None
    assert repaired.answer == result.answer


def test_preload_populates_every_catalog_entry() -> None:
    redis = InMemoryRedisManager()

    stored = run(preload_faq_cache(redis=redis, ttl_seconds=FAQ_CACHE_TTL_SECONDS))

    assert stored == len(FAQ_ENTRIES)
    for entry in FAQ_ENTRIES:
        cached = run(get_cached_faq(redis, normalize_question(entry.question)))
        assert cached is not None
        assert cached.answer == entry.answer


def test_cache_store_failure_still_returns_the_catalog_answer() -> None:
    class FailingStoreRedis(InMemoryRedisManager):
        async def get(self, key: str):
            return None

        async def set_with_expiration(self, key: str, value, ttl_seconds: int) -> bool:
            from app.services.redis_client import RedisOperationError

            raise RedisOperationError("Redis operation failed.")

    result = run(answer_faq(question=QUESTION, redis=FailingStoreRedis()))

    assert result.matched is True
    assert result.source == "predefined"
    assert result.cache_status == "unavailable"
    assert result.answer.startswith("Configurable placeholder")


def test_unavailable_redis_still_answers_known_questions() -> None:
    redis = InMemoryRedisManager(fail_operations=True)

    result = run(answer_faq(question=QUESTION, redis=redis))

    assert result.matched is True
    assert result.answer.startswith("Configurable placeholder")
    assert result.source == "predefined"
    assert result.cache_status == "unavailable"
    assert result.cache_hit is False


def test_unavailable_redis_endpoint_falls_back_safely() -> None:
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = InMemoryRedisManager(fail_operations=True)
    body = encode_json(FAQ_PAYLOAD)

    with TestClient(application) as test_client:
        response = post_signed(test_client, body)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["answered"] is True
    assert data["answer"].startswith("Configurable placeholder")
    assert data["source"] == "predefined"
    assert data["cache_status"] == "unavailable"
    assert response.headers["x-faq-cache"] == "unavailable"


# ---------------------------------------------------------------------------
# Webhook integration and contract
# ---------------------------------------------------------------------------


def test_endpoint_returns_the_configured_answer(client: TestClient) -> None:
    response = post_signed(client, encode_json(FAQ_PAYLOAD))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["event"] == "faq.query"
    assert data["event_type"] == "faq"
    assert data["question"] == QUESTION
    assert data["matched_question"] == QUESTION
    assert data["processed"] is True
    assert data["answered"] is True
    assert data["answer"].startswith("Configurable placeholder")
    assert data["placeholder"] is True
    # Startup preloads the catalog, so the first request may already hit Redis.
    assert data["source"] in {"cache", "predefined"}
    assert data["latency_ms"] >= 0.0
    assert data["total_processing_ms"] >= 0.0
    assert float(response.headers["x-faq-processing-ms"]) >= 0.0
    assert float(response.headers["x-processing-time-ms"]) >= 0.0


def test_endpoint_uses_the_preloaded_cache(
    client: TestClient,
    redis_manager: InMemoryRedisManager,
) -> None:
    response = post_signed(client, encode_json(FAQ_PAYLOAD))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "cache"
    assert data["cache_hit"] is True
    assert data["cache_status"] == "hit"
    assert response.headers["x-faq-cache"] == "hit"

    ttl_remaining = redis_manager.remaining_ttl(
        faq_cache_key(normalize_question(QUESTION))
    )
    assert ttl_remaining is not None
    assert 0 < ttl_remaining <= FAQ_CACHE_TTL_SECONDS


def test_endpoint_unknown_question_is_not_cached(
    client: TestClient,
    redis_manager: InMemoryRedisManager,
) -> None:
    response = post_signed(
        client,
        encode_json(
            {"event": "faq.query", "payload": {"question": "Do you ship internationally?"}}
        ),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["processed"] is False
    assert data["answered"] is False
    assert data["matched_question"] is None
    assert data["answer"] == UNKNOWN_FAQ_ANSWER
    assert data["source"] == "unknown"
    assert response.headers["x-faq-cache"] == "miss"
    assert (
        run(redis_manager.exists(faq_cache_key("do you ship internationally")))
        is False
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "faq.query", "payload": {}},
        {"event": "faq.query", "payload": {"question": ""}},
        {"event": "faq.query", "payload": {"question": "   \n\t "}},
        {"event": "faq.query", "payload": {"question": "???"}},
        {"event": "faq.query", "payload": {"question": 42}},
    ],
)
def test_missing_or_empty_question_is_rejected(
    client: TestClient,
    payload: dict,
) -> None:
    response = post_signed(client, encode_json(payload))

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "missing_question"
    assert body["error"]["details"]["field"] == "payload.question"


def test_faq_requires_a_valid_hmac(client: TestClient) -> None:
    body = encode_json(FAQ_PAYLOAD)
    response = client.post(
        "/api/guard",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_signature"


def test_faq_does_not_create_refund_idempotency_keys(
    client: TestClient,
    redis_manager: InMemoryRedisManager,
) -> None:
    response = post_signed(client, encode_json(FAQ_PAYLOAD))

    assert response.status_code == 200
    assert run(redis_manager.exists("pinhire:idempotency:TX-FAQ-1001")) is False


def test_faq_does_not_affect_refund_idempotency(client: TestClient) -> None:
    first_refund = post_signed(client, encode_json(REFUND_PAYLOAD))
    faq_response = post_signed(client, encode_json(FAQ_PAYLOAD))
    duplicate_refund = post_signed(client, encode_json(REFUND_PAYLOAD))

    assert first_refund.status_code == 200
    assert faq_response.status_code == 200
    assert duplicate_refund.status_code == 409
    assert duplicate_refund.json() == {"status": "duplicate"}


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def test_repeated_request_latency(client: TestClient) -> None:
    body = encode_json(FAQ_PAYLOAD)
    for _ in range(3):  # warm-up: connection and cache
        assert post_signed(client, body).status_code == 200

    samples_ms: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        response = post_signed(client, body)
        samples_ms.append((time.perf_counter() - started) * 1000.0)

        assert response.status_code == 200
        assert response.json()["data"]["cache_hit"] is True
        assert response.json()["data"]["latency_ms"] < 650.0

    stats = report_latency("FAQ repeated requests via TestClient", samples_ms)
    assert stats["max"] < 650.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_started(server: uvicorn.Server, timeout: float = 20.0) -> None:
    deadline = time.perf_counter() + timeout
    while not server.started:
        if time.perf_counter() > deadline:
            raise RuntimeError("uvicorn did not start within the timeout")
        time.sleep(0.01)


def test_actual_http_endpoint_response_time() -> None:
    """Measure complete HTTP round trips against a real uvicorn server."""

    port = _free_port()
    application = create_app(load_settings(env=TEST_ENV))
    application.state.redis = InMemoryRedisManager()
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        _wait_until_started(server)

        body = encode_json(FAQ_PAYLOAD)
        headers = {
            "content-type": "application/json",
            SIGNATURE_HEADER: sign_body(body),
        }

        samples_ms: list[float] = []
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=10.0,
        ) as http_client:
            for _ in range(3):  # warm-up over the real socket
                assert http_client.post(
                    "/api/guard",
                    content=body,
                    headers=headers,
                ).status_code == 200

            for _ in range(20):
                started = time.perf_counter()
                response = http_client.post(
                    "/api/guard",
                    content=body,
                    headers=headers,
                )
                samples_ms.append((time.perf_counter() - started) * 1000.0)

                assert response.status_code == 200
                data = response.json()["data"]
                assert data["answered"] is True
                assert data["cache_hit"] is True
                assert data["latency_ms"] < 650.0

        stats = report_latency("FAQ real HTTP requests (uvicorn)", samples_ms)
        assert stats["max"] < 650.0
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# No external services, and genuinely measured latency
# ---------------------------------------------------------------------------


def test_faq_service_imports_are_only_stdlib_or_local() -> None:
    """The answering path must not import an AI client or an HTTP library."""

    source_path = Path(faq_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots <= {
        "__future__",
        "app",
        "dataclasses",
        "json",
        "logging",
        "string",
        "time",
        "typing",
        "unicodedata",
    }, f"unexpected FAQ service imports: {sorted(imported_roots)}"


def test_faq_request_makes_no_outbound_network_calls(
    client: TestClient,
    monkeypatch,
) -> None:
    def blocked(*args, **kwargs):
        raise AssertionError("FAQ handling attempted an outbound network call")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    response = post_signed(client, encode_json(FAQ_PAYLOAD))

    assert response.status_code == 200
    assert response.json()["data"]["answered"] is True


def test_faq_latency_is_measured_with_perf_counter(monkeypatch) -> None:
    """Prove ``latency_ms`` comes from the clock, not a hard-coded value."""

    class FakeClock:
        def __init__(self, values: list[float]) -> None:
            self._values = iter(values)

        def perf_counter(self) -> float:
            return next(self._values)

    monkeypatch.setattr(
        faq_module,
        "time",
        FakeClock([100.0, 100.125]),
    )

    result = run(answer_faq(question=QUESTION))

    assert result.latency_ms == pytest.approx(125.0)
    assert result.latency_ms < 650.0
