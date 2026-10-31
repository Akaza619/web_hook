"""FastAPI application entry point.

Creates the ASGI application, configures logging, registers lifecycle hooks,
request correlation IDs, and consistent JSON error handling. The lifespan owns
the shared asynchronous Redis client and closes it on shutdown.

Run locally with::

    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from http import HTTPStatus
from time import perf_counter
from typing import AsyncIterator, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import VALID_LOG_LEVELS, Settings, get_settings
from app.routes.webhook import WebhookRequestError
from app.routes.webhook import router as webhook_router
from app.services.faq import preload_faq_cache
from app.services.redis_client import RedisManager
from app.utils.response import error_response

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    """Configure process-wide logging with a consistent, timestamped format."""

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def _bootstrap_logging() -> None:
    """Configure logging before settings are loaded, so early warnings are formatted."""

    level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    configure_logging(level if level in VALID_LOG_LEVELS else "INFO")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Initialize shared resources on startup and release them on shutdown."""

    settings: Settings = application.state.settings
    redis_manager: RedisManager = application.state.redis

    logger.info(
        "Starting %s v%s (env=%s, candidate=%s)",
        settings.app_name,
        settings.app_version,
        settings.app_env,
        settings.candidate_id,
    )
    logger.info(
        "Policy: refund limit=%s AED | idempotency TTL=%ss | "
        "FAQ cache TTL=%ss | FAQ target=<%sms",
        settings.refund_limit_aed,
        settings.idempotency_ttl_seconds,
        settings.faq_cache_ttl_seconds,
        settings.faq_latency_target_ms,
    )
    if settings.signing_secret:
        logger.info(
            "HMAC-SHA256 verification enabled (header=%s).",
            settings.signature_header,
        )

    redis_connected = False
    try:
        redis_connected = await redis_manager.connect()
    except Exception as exc:  # noqa: BLE001 - startup must survive Redis failures
        logger.warning(
            "Unexpected Redis startup failure (%s); /health will report degraded.",
            type(exc).__name__,
        )

    if redis_connected:
        try:
            faq_count = await preload_faq_cache(
                redis=redis_manager,
                ttl_seconds=settings.faq_cache_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - FAQ must survive cache failures
            logger.warning(
                "FAQ cache preload failed (%s); the in-memory catalog remains available.",
                type(exc).__name__,
            )
        else:
            logger.info(
                "FAQ cache preloaded (%s entries, TTL=%ss).",
                faq_count,
                settings.faq_cache_ttl_seconds,
            )

    try:
        yield
    finally:
        try:
            await redis_manager.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must complete
            logger.warning(
                "Unexpected Redis shutdown failure (%s).",
                type(exc).__name__,
            )
        logger.info("Stopped %s", settings.app_name)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def http_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Render HTTP errors (404, 405, ...) using the shared error envelope."""

    # Starlette registers this handler for StarletteHTTPException only.
    http_exc = cast(StarletteHTTPException, exc)

    try:
        code = HTTPStatus(http_exc.status_code).name.lower()
    except ValueError:
        code = "http_error"

    if isinstance(http_exc.detail, str):
        message = http_exc.detail
        details = None
    else:
        message = "HTTP error."
        details = jsonable_encoder(http_exc.detail)

    return error_response(
        code=code,
        message=message,
        status_code=http_exc.status_code,
        details=details,
        request_id=_request_id(request),
        headers=http_exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Return a predictable 422 payload instead of FastAPI's default shape."""

    # FastAPI registers this handler for RequestValidationError only.
    validation_exc = cast(RequestValidationError, exc)

    return error_response(
        code="validation_error",
        message="Request validation failed.",
        status_code=422,
        details=jsonable_encoder(validation_exc.errors()),
        request_id=_request_id(request),
    )


async def webhook_request_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Render webhook validation failures using the shared error envelope."""

    # The app registers this handler for WebhookRequestError only.
    webhook_exc = cast(WebhookRequestError, exc)

    return error_response(
        code=webhook_exc.code,
        message=webhook_exc.message,
        status_code=webhook_exc.status_code,
        details=webhook_exc.details,
        request_id=_request_id(request),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unexpected failures and avoid leaking internals to the caller."""

    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(
        code="internal_error",
        message="An unexpected error occurred.",
        status_code=500,
        request_id=_request_id(request),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Args:
        settings: Optional pre-built settings, useful in tests. When omitted,
            configuration is loaded from the environment / ``.env``.
    """

    resolved = settings
    if resolved is None:
        _bootstrap_logging()
        resolved = get_settings()
    configure_logging(resolved.log_level)

    application = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        description=(
            "Pinhire webhook server (Phases 1-9: HMAC-protected refund "
            "idempotency and cached FAQ answers integrated behind one endpoint)."
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.state.redis = RedisManager(
        resolved.redis_url,
        timeout_seconds=resolved.redis_timeout_seconds,
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        """Attach a correlation ID to every request and log its duration."""

        request_id = request.headers.get("x-request-id", "").strip() or uuid4().hex
        request.state.request_id = request_id
        started = perf_counter()

        response = await call_next(request)

        elapsed_ms = (perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        response.headers["x-processing-time-ms"] = f"{elapsed_ms:.3f}"
        logger.debug(
            "%s %s -> %s in %.1f ms (request_id=%s)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,
    )
    application.add_exception_handler(
        WebhookRequestError,
        webhook_request_exception_handler,
    )
    application.add_exception_handler(Exception, unhandled_exception_handler)

    application.include_router(webhook_router)
    return application


app = create_app()
