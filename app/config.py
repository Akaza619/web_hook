"""Environment-driven configuration for the webhook server.

All runtime configuration is read from environment variables. A local ``.env``
file is loaded when present, but real environment variables always win and
``.env`` is git-ignored.

The configuration deliberately fails fast in production when the signing secret
or Redis URL is missing/placeholder, instead of silently running with an
insecure default. Redis connection timeouts are configurable separately.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

VALID_ENVIRONMENTS = frozenset({"development", "test", "production"})
VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})

# Values that indicate a developer copied .env.example without editing it.
_PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "changeme",
    "replace-me",
    "your-",
    "placeholder",
    "<",
    ">",
)


class ConfigurationError(RuntimeError):
    """Raised when environment variables cannot produce a safe configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable runtime configuration."""

    app_name: str
    app_version: str
    app_env: str
    candidate_id: str
    log_level: str
    host: str
    port: int
    signing_secret: str | None
    signature_header: str
    tx_ref_field: str
    refund_limit_aed: Decimal
    idempotency_ttl_seconds: int
    faq_latency_target_ms: int
    faq_cache_ttl_seconds: int
    redis_url: str
    redis_timeout_seconds: float

    @property
    def is_production(self) -> bool:
        """Return True when the application runs in the production environment."""

        return self.app_env == "production"


def _read(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    """Read a trimmed string from ``env``; empty values fall back to ``default``."""

    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _is_placeholder(value: str | None) -> bool:
    """Return True when a value is absent or looks like an example placeholder."""

    if value is None:
        return True
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS)


def _parse_int(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = _read(env, key)
    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be an integer, got {raw!r}.") from exc

    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{key} must be between {minimum} and {maximum}, got {value}."
        )
    return value


def _parse_float(
    env: Mapping[str, str],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = _read(env, key)
    if raw is None:
        return default

    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a number, got {raw!r}.") from exc

    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{key} must be a finite number between {minimum} and {maximum}, "
            f"got {raw!r}."
        )
    return value


def _parse_decimal(
    env: Mapping[str, str],
    key: str,
    default: Decimal,
    *,
    minimum: Decimal,
) -> Decimal:
    raw = _read(env, key)
    if raw is None:
        return default

    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{key} must be a decimal number, got {raw!r}.") from exc

    if not value.is_finite() or value < minimum:
        raise ConfigurationError(f"{key} must be a finite number >= {minimum}, got {raw!r}.")
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> Settings:
    """Build and validate :class:`Settings`.

    Args:
        env: Optional explicit mapping, primarily useful for tests. When omitted,
            the process environment is used.
        env_file: Optional ``.env`` path loaded when ``env`` is omitted. Pass
            ``None`` to skip loading a file.

    Raises:
        ConfigurationError: If a value is malformed or production-required
            configuration is missing or still a placeholder.
    """

    if env is None:
        if env_file is not None and env_file.is_file():
            # Do not override variables that are already set in the real
            # environment (important in containers and CI).
            load_dotenv(env_file, override=False)
        env = os.environ

    app_env = (_read(env, "APP_ENV", "development") or "development").lower()
    if app_env not in VALID_ENVIRONMENTS:
        allowed = ", ".join(sorted(VALID_ENVIRONMENTS))
        raise ConfigurationError(f"APP_ENV must be one of [{allowed}], got {app_env!r}.")

    log_level = (_read(env, "LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        logger.warning("Unsupported LOG_LEVEL %r; falling back to INFO.", log_level)
        log_level = "INFO"

    signing_secret = _read(env, "SIGNING_SECRET")
    redis_url_raw = _read(env, "REDIS_URL")
    is_production = app_env == "production"

    if is_production:
        missing = []
        if _is_placeholder(signing_secret):
            missing.append("SIGNING_SECRET")
        if _is_placeholder(redis_url_raw):
            missing.append("REDIS_URL")

        if missing:
            raise ConfigurationError(
                "Missing required production configuration: "
                + ", ".join(missing)
                + ". Set real values in the environment; never commit them."
            )
    else:
        if _is_placeholder(signing_secret):
            logger.warning(
                "SIGNING_SECRET is missing or a placeholder; POST /api/guard "
                "will return 503 until it is configured."
            )
            signing_secret = None
        if _is_placeholder(redis_url_raw):
            logger.warning(
                "REDIS_URL is missing; falling back to redis://localhost:6379/0 "
                "for local development."
            )
            redis_url_raw = "redis://localhost:6379/0"

    return Settings(
        app_name=_read(env, "APP_NAME", "Pinhire Webhook Server") or "Pinhire Webhook Server",
        app_version=_read(env, "APP_VERSION", "0.1.0") or "0.1.0",
        app_env=app_env,
        candidate_id=_read(env, "CANDIDATE_ID", "PH-2026-3126") or "PH-2026-3126",
        log_level=log_level,
        host=_read(env, "HOST", "127.0.0.1") or "127.0.0.1",
        port=_parse_int(env, "PORT", 8000, minimum=1, maximum=65535),
        signing_secret=signing_secret,
        signature_header=(
            _read(env, "SIGNATURE_HEADER", "x-pinhire-hmac-3126")
            or "x-pinhire-hmac-3126"
        ).lower(),
        tx_ref_field=(
            _read(env, "TX_REF_FIELD", "payload.tx_ref_3126")
            or "payload.tx_ref_3126"
        ),
        refund_limit_aed=_parse_decimal(
            env,
            "REFUND_LIMIT_AED",
            Decimal("320"),
            minimum=Decimal("0"),
        ),
        idempotency_ttl_seconds=_parse_int(
            env,
            "IDEMPOTENCY_TTL_SECONDS",
            300,
            minimum=1,
            maximum=604_800,
        ),
        faq_latency_target_ms=_parse_int(
            env,
            "FAQ_LATENCY_TARGET_MS",
            650,
            minimum=1,
            maximum=60_000,
        ),
        faq_cache_ttl_seconds=_parse_int(
            env,
            "FAQ_CACHE_TTL_SECONDS",
            3600,
            minimum=1,
            maximum=604_800,
        ),
        redis_url=redis_url_raw or "redis://localhost:6379/0",
        redis_timeout_seconds=_parse_float(
            env,
            "REDIS_TIMEOUT_SECONDS",
            2.0,
            minimum=0.1,
            maximum=30.0,
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once on first use."""

    return load_settings()
