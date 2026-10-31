"""FAQ normalization, predefined answers, and Redis cache-aside handling.

The catalog below is the single source of truth for predefined FAQ answers.
Answers are deliberately marked as configurable placeholders: no real refund,
support, payment, or transaction policy has been supplied, so the service must
not present invented details as fact.

Lookups are deterministic and local. Redis is only a cache: a cache miss, a
malformed cache entry, or an unavailable Redis connection falls back to the
in-memory catalog. No external AI API is used for FAQ matching.
"""

from __future__ import annotations

import json
import logging
import string
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.services.redis_client import RedisOperationError, RedisUnavailableError

logger = logging.getLogger(__name__)

FAQ_CACHE_KEY_PREFIX = "pinhire:faq:"
DEFAULT_FAQ_CACHE_TTL_SECONDS = 3600

UNKNOWN_FAQ_ANSWER = (
    "No configured FAQ answer is available for that question. "
    "This is a safe placeholder, not a business policy."
)

_PUNCTUATION_TO_SPACE = str.maketrans(
    {character: " " for character in string.punctuation}
)


class FAQQuestionError(ValueError):
    """A client-visible FAQ question validation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class FAQEntry:
    """A configured FAQ question and answer."""

    question: str
    answer: str
    placeholder: bool = True


# ---------------------------------------------------------------------------
# Centralized catalog. Replace these placeholder answers with approved policy
# text when it is available; normalization and caching then follow automatically.
# ---------------------------------------------------------------------------

FAQ_ENTRIES: tuple[FAQEntry, ...] = (
    FAQEntry(
        question="What is your refund policy?",
        answer=(
            "Configurable placeholder: the refund policy has not been supplied. "
            "Replace this answer in app/services/faq.py with the approved policy."
        ),
    ),
    FAQEntry(
        question="How can I contact support?",
        answer=(
            "Configurable placeholder: support contact details have not been "
            "supplied. Replace this answer with the approved support channel."
        ),
    ),
    FAQEntry(
        question="How long does a refund take?",
        answer=(
            "Configurable placeholder: refund processing times have not been "
            "supplied. Replace this answer with the approved timeline."
        ),
    ),
    FAQEntry(
        question="What payment methods are accepted?",
        answer=(
            "Configurable placeholder: accepted payment methods have not been "
            "supplied. Replace this answer with the approved list."
        ),
    ),
    FAQEntry(
        question="How can I check my transaction status?",
        answer=(
            "Configurable placeholder: transaction-status instructions have not "
            "been supplied. Replace this answer with the approved instructions."
        ),
    ),
)


def normalize_question(question: str) -> str:
    """Normalize a question for deterministic FAQ matching.

    Handles case differences, surrounding/repeated whitespace, and basic
    punctuation differences. Examples that all normalize identically:

    * ``What is your refund policy?``
    * ``what is your refund policy``
    * ``  WHAT   IS YOUR REFUND POLICY?  ``

    Raises:
        FAQQuestionError: When the value is not a string or contains no letters
            or digits after normalization.
    """

    if not isinstance(question, str):
        raise FAQQuestionError(
            "invalid_question",
            "FAQ question must be a string.",
            details={"field": "payload.question"},
        )

    normalized = unicodedata.normalize("NFKC", question).casefold()
    normalized = normalized.translate(_PUNCTUATION_TO_SPACE)
    normalized = " ".join(normalized.split())

    if not normalized:
        raise FAQQuestionError(
            "missing_question",
            "FAQ question must contain at least one letter or digit.",
            details={"field": "payload.question"},
        )

    return normalized


def _build_catalog_index() -> dict[str, FAQEntry]:
    index: dict[str, FAQEntry] = {}
    for entry in FAQ_ENTRIES:
        key = normalize_question(entry.question)
        if key in index:
            raise RuntimeError(f"Duplicate normalized FAQ question: {entry.question!r}")
        index[key] = entry
    return index


_FAQ_BY_NORMALIZED = _build_catalog_index()


def lookup_predefined_faq(normalized_question: str) -> FAQEntry | None:
    """Return the configured entry for a normalized question, if any."""

    return _FAQ_BY_NORMALIZED.get(normalized_question)


def faq_cache_key(normalized_question: str) -> str:
    """Return the Redis key for a normalized FAQ question."""

    return f"{FAQ_CACHE_KEY_PREFIX}{normalized_question}"


def _cache_document(entry: FAQEntry) -> str:
    return json.dumps(
        {
            "version": 1,
            "question": entry.question,
            "answer": entry.answer,
            "placeholder": entry.placeholder,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _entry_from_cache(raw: Any) -> FAQEntry | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None

    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None

    answer = document.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return None

    question = document.get("question")
    if not isinstance(question, str) or not question.strip():
        question = ""

    placeholder = document.get("placeholder", True)
    if not isinstance(placeholder, bool):
        placeholder = True

    return FAQEntry(question=question, answer=answer, placeholder=placeholder)


async def get_cached_faq(
    redis: Any,
    normalized_question: str,
) -> FAQEntry | None:
    """Read a cached FAQ entry, or return ``None`` on a miss/invalid value."""

    raw = await redis.get(faq_cache_key(normalized_question))
    return _entry_from_cache(raw)


async def store_faq_answer(
    redis: Any,
    normalized_question: str,
    entry: FAQEntry,
    ttl_seconds: int,
) -> bool:
    """Cache a FAQ entry under the normalized question with a TTL."""

    return await redis.set_with_expiration(
        faq_cache_key(normalized_question),
        _cache_document(entry),
        ttl_seconds,
    )


@dataclass(frozen=True, slots=True)
class FAQResult:
    """Answer plus diagnostic metadata for one FAQ lookup."""

    question: str
    matched_question: str | None
    answer: str
    matched: bool
    placeholder: bool
    source: str
    cache_hit: bool
    cache_status: str
    latency_ms: float


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


async def answer_faq(
    *,
    question: str,
    redis: Any | None = None,
    ttl_seconds: int = DEFAULT_FAQ_CACHE_TTL_SECONDS,
) -> FAQResult:
    """Answer a question from Redis or the in-memory catalog.

    Cache failures never break a known FAQ answer: the in-memory catalog is the
    safe fallback. Unknown questions receive an explicit placeholder response
    and are not cached. All latency values are server processing time, not
    network round-trip time.
    """

    started_at = time.perf_counter()
    normalized = normalize_question(question)
    original_question = question.strip()

    cache_status = "unavailable" if redis is None else "miss"

    if redis is not None:
        try:
            cached = await get_cached_faq(redis, normalized)
        except (RedisUnavailableError, RedisOperationError) as exc:
            cache_status = "unavailable"
            logger.warning(
                "FAQ cache lookup failed (%s); using the in-memory catalog.",
                type(exc).__name__,
            )
            cached = None
        except Exception as exc:  # noqa: BLE001 - cache must never break FAQ
            cache_status = "unavailable"
            logger.warning(
                "Unexpected FAQ cache lookup failure (%s); using the in-memory catalog.",
                type(exc).__name__,
            )
            cached = None

        if cached is not None:
            return FAQResult(
                question=original_question,
                matched_question=cached.question or None,
                answer=cached.answer,
                matched=True,
                placeholder=cached.placeholder,
                source="cache",
                cache_hit=True,
                cache_status="hit",
                latency_ms=_elapsed_ms(started_at),
            )

    entry = lookup_predefined_faq(normalized)
    if entry is None:
        return FAQResult(
            question=original_question,
            matched_question=None,
            answer=UNKNOWN_FAQ_ANSWER,
            matched=False,
            placeholder=True,
            source="unknown",
            cache_hit=False,
            cache_status=cache_status,
            latency_ms=_elapsed_ms(started_at),
        )

    if redis is not None and cache_status != "unavailable":
        try:
            await store_faq_answer(redis, normalized, entry, ttl_seconds)
        except (RedisUnavailableError, RedisOperationError) as exc:
            cache_status = "unavailable"
            logger.warning(
                "FAQ cache store failed (%s); answer served from memory.",
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - cache must never break FAQ
            cache_status = "unavailable"
            logger.warning(
                "Unexpected FAQ cache store failure (%s); answer served from memory.",
                type(exc).__name__,
            )

    return FAQResult(
        question=original_question,
        matched_question=entry.question,
        answer=entry.answer,
        matched=True,
        placeholder=entry.placeholder,
        source="predefined",
        cache_hit=False,
        cache_status=cache_status,
        latency_ms=_elapsed_ms(started_at),
    )


async def preload_faq_cache(
    *,
    redis: Any,
    ttl_seconds: int = DEFAULT_FAQ_CACHE_TTL_SECONDS,
) -> int:
    """Populate Redis with the predefined catalog and return the entry count.

    This is best-effort startup work. Callers should catch failures so Redis
    problems cannot prevent the application from starting.
    """

    stored = 0
    for normalized, entry in _FAQ_BY_NORMALIZED.items():
        await store_faq_answer(redis, normalized, entry, ttl_seconds)
        stored += 1
    return stored
