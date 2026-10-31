"""Tests for environment loading and production safety checks."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import ConfigurationError, load_settings


def test_development_defaults_are_loaded() -> None:
    settings = load_settings(env={"APP_ENV": "development"})

    assert settings.app_name == "Pinhire Webhook Server"
    assert settings.app_version == "0.1.0"
    assert settings.candidate_id == "PH-2026-3126"
    assert settings.signing_secret is None
    assert settings.signature_header == "x-pinhire-hmac-3126"
    assert settings.tx_ref_field == "payload.tx_ref_3126"
    assert settings.refund_limit_aed == Decimal("320")
    assert settings.idempotency_ttl_seconds == 300
    assert settings.faq_latency_target_ms == 650
    assert settings.faq_cache_ttl_seconds == 3600
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.redis_timeout_seconds == 2.0
    assert settings.is_production is False


def test_custom_values_are_loaded() -> None:
    settings = load_settings(
        env={
            "APP_ENV": "test",
            "APP_NAME": "Custom Service",
            "SIGNING_SECRET": "custom-secret",
            "SIGNATURE_HEADER": "X-Custom-HMAC",
            "TX_REF_FIELD": "payload.custom_ref",
            "REFUND_LIMIT_AED": "99.50",
            "IDEMPOTENCY_TTL_SECONDS": "120",
            "FAQ_LATENCY_TARGET_MS": "500",
            "FAQ_CACHE_TTL_SECONDS": "900",
            "REDIS_URL": "redis://localhost:6379/9",
            "REDIS_TIMEOUT_SECONDS": "0.75",
        }
    )

    assert settings.app_name == "Custom Service"
    assert settings.signing_secret == "custom-secret"
    assert settings.signature_header == "x-custom-hmac"
    assert settings.tx_ref_field == "payload.custom_ref"
    assert settings.refund_limit_aed == Decimal("99.50")
    assert settings.idempotency_ttl_seconds == 120
    assert settings.faq_latency_target_ms == 500
    assert settings.faq_cache_ttl_seconds == 900
    assert settings.redis_url == "redis://localhost:6379/9"
    assert settings.redis_timeout_seconds == 0.75


def test_production_requires_signing_secret_and_redis_url() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env={"APP_ENV": "production"})

    message = str(excinfo.value)
    assert "SIGNING_SECRET" in message
    assert "REDIS_URL" in message


@pytest.mark.parametrize(
    "secret",
    [
        "replace-me-with-a-strong-random-secret",
        "your-secret-here",
        "changeme",
        "",
    ],
)
def test_production_rejects_placeholder_signing_secrets(secret: str) -> None:
    with pytest.raises(ConfigurationError, match="SIGNING_SECRET"):
        load_settings(
            env={
                "APP_ENV": "production",
                "SIGNING_SECRET": secret,
                "REDIS_URL": "redis://redis.internal:6379/0",
            }
        )


def test_production_rejects_placeholder_redis_url() -> None:
    with pytest.raises(ConfigurationError, match="REDIS_URL"):
        load_settings(
            env={
                "APP_ENV": "production",
                "SIGNING_SECRET": "prod-signing-secret-abc123",
                "REDIS_URL": "redis://placeholder-host:6379/0",
            }
        )


def test_production_accepts_complete_configuration() -> None:
    settings = load_settings(
        env={
            "APP_ENV": "production",
            "SIGNING_SECRET": "prod-signing-secret-abc123",
            "REDIS_URL": "redis://redis.internal:6379/0",
        }
    )

    assert settings.is_production is True
    assert settings.signing_secret == "prod-signing-secret-abc123"


def test_invalid_environment_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        load_settings(env={"APP_ENV": "prod"})


def test_invalid_integer_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="IDEMPOTENCY_TTL_SECONDS"):
        load_settings(env={"APP_ENV": "development", "IDEMPOTENCY_TTL_SECONDS": "soon"})


def test_out_of_range_ttl_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="IDEMPOTENCY_TTL_SECONDS"):
        load_settings(env={"APP_ENV": "development", "IDEMPOTENCY_TTL_SECONDS": "0"})


def test_negative_refund_limit_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="REFUND_LIMIT_AED"):
        load_settings(env={"APP_ENV": "development", "REFUND_LIMIT_AED": "-1"})


def test_invalid_refund_limit_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="REFUND_LIMIT_AED"):
        load_settings(env={"APP_ENV": "development", "REFUND_LIMIT_AED": "lots"})


@pytest.mark.parametrize("value", ["0", "-1", "31", "NaN", "Infinity", "soon"])
def test_invalid_redis_timeout_is_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError, match="REDIS_TIMEOUT_SECONDS"):
        load_settings(
            env={"APP_ENV": "development", "REDIS_TIMEOUT_SECONDS": value}
        )


def test_process_environment_is_used_when_no_mapping_is_given(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SIGNING_SECRET", "from-process-environment")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/11")

    settings = load_settings(env_file=None)

    assert settings.app_env == "test"
    assert settings.signing_secret == "from-process-environment"
    assert settings.redis_url == "redis://localhost:6379/11"
