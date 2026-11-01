"""HTTP routes for the webhook surface.

Phase 1 exposed the liveness endpoint. Phase 2 added ``POST /api/guard`` for
raw-body validation and event recognition. Phase 3 authenticates every guard
request with HMAC-SHA256 before any JSON parsing or business logic. Phase 4
validates refunds deterministically against the configured AED limit. Phase 5
reports Redis connectivity from ``GET /health``. Phase 6 makes refund processing
idempotent with an atomic Redis ``SET NX EX`` lock. Phase 7 answers FAQ events
from a normalized in-memory catalog with a Redis cache-aside layer.

Refunds are simulated only: no payment provider is contacted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Any, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import Settings
from app.services.faq import FAQQuestionError, answer_faq
from app.services.idempotency import (
    TransactionReferenceError,
    execute_once,
    validate_transaction_reference,
)
from app.services.refund import (
    RefundValidation,
    format_amount,
    json_amount,
    validate_refund,
)
from app.services.redis_client import RedisOperationError, RedisUnavailableError
from app.services.security import (
    SignatureError,
    normalize_signature,
    verify_signature,
)
from app.utils.response import success_response

logger = logging.getLogger(__name__)

router = APIRouter()

REFUND_EVENT = "refund.requested"
FAQ_EVENT = "faq.query"
SUPPORTED_EVENTS = frozenset({REFUND_EVENT, FAQ_EVENT})
FAQ_QUESTION_FIELDS = ("question", "message", "customer_message", "text", "query")


class WebhookRequestError(Exception):
    """A client-visible webhook validation error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Normalized event extracted from an incoming webhook request."""

    name: str
    payload: dict[str, Any]
    tx_ref: str | None = None
    question: str | None = None

    @property
    def kind(self) -> str:
        """Return the event family used in responses and future routing."""

        return "refund" if self.name == REFUND_EVENT else "faq"


def _require_non_empty_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    code: str,
    event_label: str,
) -> str:
    """Extract a required, non-blank string field from the event payload."""

    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WebhookRequestError(
            code=code,
            message=f"{event_label} event requires a non-empty payload.{field} string.",
            details={"field": f"payload.{field}"},
        )
    return value.strip()


def parse_webhook_event(raw_body: bytes) -> WebhookEvent:
    """Validate a raw request body and normalize a supported event.

    Parsing is deliberately independent of FastAPI request models so the raw
    body remains available for HMAC verification in Phase 3.

    Raises:
        WebhookRequestError: For empty bodies, malformed JSON, non-object JSON,
            missing/unsupported events, missing payloads, or missing required
            event-specific fields.
    """

    if not raw_body or not raw_body.strip():
        raise WebhookRequestError("empty_body", "Request body is required.")

    try:
        document = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise WebhookRequestError(
            "invalid_json",
            "Request body must be valid JSON.",
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    except UnicodeDecodeError as exc:
        raise WebhookRequestError(
            "invalid_json",
            "Request body must be valid UTF-8 JSON.",
        ) from exc

    if not isinstance(document, dict):
        raise WebhookRequestError(
            "invalid_json_structure",
            "Request body must be a JSON object.",
        )

    event_name = document.get("event")
    if event_name is None or (isinstance(event_name, str) and not event_name.strip()):
        raise WebhookRequestError(
            "missing_event_type",
            "Request body must include a non-empty 'event' string.",
        )
    if not isinstance(event_name, str):
        raise WebhookRequestError(
            "invalid_event_type",
            "The 'event' field must be a string.",
        )
    event_name = event_name.strip()

    if event_name not in SUPPORTED_EVENTS:
        raise WebhookRequestError(
            "unsupported_event",
            f"Unsupported event type: {event_name!r}.",
            details={
                "event": event_name,
                "supported_events": sorted(SUPPORTED_EVENTS),
            },
        )

    payload = document.get("payload")
    if "payload" not in document or payload is None:
        raise WebhookRequestError(
            "missing_payload",
            "Request body must include a 'payload' object.",
        )
    if not isinstance(payload, dict):
        raise WebhookRequestError(
            "invalid_payload",
            "The 'payload' field must be a JSON object.",
        )

    if event_name == REFUND_EVENT:
        try:
            tx_ref = validate_transaction_reference(payload.get("tx_ref_3126"))
        except TransactionReferenceError as exc:
            raise WebhookRequestError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                details=exc.details,
            ) from exc
        return WebhookEvent(name=event_name, payload=payload, tx_ref=tx_ref)

    # ``question`` is preferred; the aliases tolerate other FAQ senders.
    question_field = next(
        (
            field
            for field in FAQ_QUESTION_FIELDS
            if isinstance(payload.get(field), str) and payload[field].strip()
        ),
        "question",
    )
    question = _require_non_empty_string(
        payload,
        question_field,
        code="missing_question",
        event_label="FAQ",
    )
    return WebhookEvent(name=event_name, payload=payload, question=question)


async def _process_faq_event(
    request: Request,
    event: WebhookEvent,
    settings: Settings,
    *,
    started_at: float,
) -> JSONResponse:
    """Answer a FAQ question from cache or the in-memory catalog.

    FAQ events never touch refund idempotency locks. Redis is only a cache:
    known questions remain answerable when it is unavailable. ``latency_ms`` is
    FAQ service time; ``total_processing_ms`` covers this webhook's application
    processing after the route was entered. Neither includes network time.
    """

    redis_manager = getattr(request.app.state, "redis", None)
    try:
        result = await answer_faq(
            question=event.question or "",
            redis=redis_manager,
            ttl_seconds=settings.faq_cache_ttl_seconds,
        )
    except FAQQuestionError as exc:
        raise WebhookRequestError(
            code=exc.code,
            message=exc.message,
            status_code=400,
            details=exc.details,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - never return an incorrect answer
        logger.exception("FAQ processing failed.")
        raise WebhookRequestError(
            "faq_processing_failed",
            "FAQ processing failed; no answer was returned.",
            status_code=500,
        ) from exc

    total_ms = (perf_counter() - started_at) * 1000.0
    message = (
        "FAQ answer returned from the configured catalog."
        if result.matched
        else "No configured FAQ answer is available for that question."
    )
    response = success_response(
        {
            "event": event.name,
            "event_type": event.kind,
            "processed": result.matched,
            "answered": result.matched,
            "question": event.question,
            "matched_question": result.matched_question,
            "answer": result.answer,
            "placeholder": result.placeholder,
            "source": result.source,
            "cache_hit": result.cache_hit,
            "cache_status": result.cache_status,
            "latency_ms": round(result.latency_ms, 3),
            "total_processing_ms": round(total_ms, 3),
            "message": message,
        },
        status_code=200,
        status="success",
    )
    response.headers["x-faq-processing-ms"] = f"{result.latency_ms:.3f}"
    response.headers["x-faq-cache"] = result.cache_status
    response.headers["x-processing-time-ms"] = f"{total_ms:.3f}"
    return response


def _refund_approval(validation: RefundValidation) -> dict[str, Any]:
    """Describe an approved, simulated refund without moving money."""

    return {
        "event": REFUND_EVENT,
        "event_type": "refund",
        "processed": False,
        "approved": True,
        "simulated": True,
        "tx_ref": validation.tx_ref,
        "refund_aed": format_amount(validation.amount_aed),
        "currency": validation.currency,
        "limit_aed": format_amount(validation.limit_aed),
        "message": validation.message,
    }


def build_refund_decision(
    event: WebhookEvent,
    *,
    limit_aed: Decimal,
) -> tuple[dict[str, Any], int]:
    """Validate a refund and return its response data and HTTP status.

    Raises:
        WebhookRequestError: When the refund is invalid or exceeds the
            server-side limit. The limit always comes from configuration.
    """

    validation = validate_refund(event.payload, limit_aed=limit_aed)
    if not validation.approved:
        raise WebhookRequestError(
            code=validation.code,
            message=validation.message,
            status_code=validation.status_code,
            details=validation.details,
        )

    return _refund_approval(validation), validation.status_code


async def _process_refund_operation(
    event: WebhookEvent,
    *,
    limit_aed: Decimal,
) -> dict[str, Any]:
    """Execute the permitted simulated refund exactly once for this request."""

    data, _ = build_refund_decision(event, limit_aed=limit_aed)
    return data


async def _process_refund_event(
    request: Request,
    event: WebhookEvent,
    settings: Settings,
) -> JSONResponse:
    """Validate, claim, and simulate a refund with Redis idempotency.

    Refund validation happens before any Redis interaction, so malformed or
    over-limit requests never consume a transaction reference.
    """

    validation = validate_refund(
        event.payload,
        limit_aed=settings.refund_limit_aed,
    )
    if not validation.approved:
        raise WebhookRequestError(
            code=validation.code,
            message=validation.message,
            status_code=validation.status_code,
            details=validation.details,
        )

    if event.tx_ref is None:
        raise WebhookRequestError(
            "missing_tx_ref",
            "Refund event requires a non-empty payload.tx_ref_3126 string.",
            details={"field": "payload.tx_ref_3126"},
        )

    redis_manager = getattr(request.app.state, "redis", None)
    if redis_manager is None:
        raise WebhookRequestError(
            "idempotency_unavailable",
            "Transaction idempotency store is unavailable.",
            status_code=503,
        )

    try:
        outcome = await execute_once(
            redis=redis_manager,
            tx_ref=event.tx_ref,
            ttl_seconds=settings.idempotency_ttl_seconds,
            operation=lambda: _process_refund_operation(
                event,
                limit_aed=settings.refund_limit_aed,
            ),
        )
    except (RedisUnavailableError, RedisOperationError) as exc:
        logger.warning(
            "Idempotency store unavailable (%s).",
            type(exc).__name__,
        )
        raise WebhookRequestError(
            "idempotency_unavailable",
            "Transaction idempotency store is unavailable.",
            status_code=503,
        ) from exc
    except WebhookRequestError:
        raise
    except Exception as exc:  # noqa: BLE001 - never report a failed refund as processed
        logger.exception("Refund processing failed.")
        raise WebhookRequestError(
            "refund_processing_failed",
            "Refund processing failed; no refund was recorded.",
            status_code=500,
        ) from exc

    if outcome.status == "duplicate":
        return JSONResponse(
            status_code=409,
            content={"status": "duplicate"},
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "processed",
            "tx_ref_3126": event.tx_ref,
            "refund_aed": json_amount(validation.amount_aed),
            "currency": validation.currency,
        },
    )


def _verify_request_signature(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
) -> None:
    """Authenticate the request before parsing or event processing.

    Missing and malformed headers are rejected with 401. If no signing secret is
    configured (only possible outside production), the endpoint is unavailable
    rather than insecure.
    """

    if not settings.signing_secret:
        raise WebhookRequestError(
            code="signature_verification_unavailable",
            message="Webhook signature verification is not configured.",
            status_code=503,
        )

    signature = headers.get(settings.signature_header)
    try:
        received_signature = normalize_signature(signature)
    except SignatureError as exc:
        raise WebhookRequestError(
            code=exc.code,
            message=exc.message,
            status_code=401,
        ) from exc

    if not verify_signature(
        raw_body=raw_body,
        signature=received_signature,
        secret=settings.signing_secret,
    ):
        raise WebhookRequestError(
            code="invalid_signature",
            message="Invalid webhook signature.",
            status_code=401,
        )


@router.get(
    "/health",
    tags=["system"],
    summary="Liveness and Redis health probe",
    description=(
        "Reports application and Redis status. Returns 200 with status "
        "'healthy' when Redis responds to PING, or 'degraded' when it does not. "
        "Redis credentials and connection strings are never exposed."
    ),
)
async def health_check(request: Request) -> JSONResponse:
    """Report application and Redis health without leaking connection details."""

    settings: Settings = request.app.state.settings
    redis_manager = getattr(request.app.state, "redis", None)

    redis_connected = False
    if redis_manager is not None:
        try:
            redis_connected = await redis_manager.ping()
        except Exception as exc:  # noqa: BLE001 - health must never fail on Redis
            logger.warning(
                "Redis health check failed (%s).",
                type(exc).__name__,
            )

    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy" if redis_connected else "degraded",
            "app": "ok",
            "redis": "connected" if redis_connected else "disconnected",
            "service": settings.app_name,
            "version": settings.app_version,
            "environment": settings.app_env,
        },
    )


@router.post(
    "/api/guard",
    tags=["webhook"],
    status_code=200,
    summary="Receive a PinHire webhook event",
    description=(
        "Requires a valid HMAC-SHA256 signature, then validates and recognizes "
        "refund and FAQ events. Refunds are checked against the configured AED "
        "limit and simulated only: no payment provider is contacted. A newly "
        "processed refund and every FAQ answer return 200; a duplicate refund "
        "returns 409 and validation failures use the shared error envelope."
    ),
    responses={
        200: {"description": "Refund processed or FAQ answered."},
        400: {"description": "Malformed request, event, or event payload."},
        401: {"description": "Missing or invalid HMAC signature."},
        403: {"description": "Refund exceeds the configured AED limit."},
        409: {"description": "Duplicate transaction reference."},
        503: {"description": "Signature verification or idempotency unavailable."},
    },
)
async def guard_webhook(request: Request) -> JSONResponse:
    """Authenticate, validate, and answer a webhook event."""

    started_at = perf_counter()
    settings: Settings = request.app.state.settings
    raw_body = await request.body()
    headers = request.headers

    _verify_request_signature(
        raw_body=raw_body,
        headers=headers,
        settings=settings,
    )

    event = parse_webhook_event(raw_body)
    if event.name == REFUND_EVENT:
        return await _process_refund_event(request, event, settings)

    return await _process_faq_event(
        request,
        event,
        settings,
        started_at=started_at,
    )
