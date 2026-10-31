"""Deterministic AED refund validation.

The refund limit is always supplied by server-side configuration
(``Settings.refund_limit_aed``). Nothing in an incoming payload can change it.

Only three payload fields are read:

* ``tx_ref_3126`` - transaction reference
* ``refund_aed``  - requested amount
* ``currency``    - optional, defaults to AED

Every other field, including ``customer_message``, is untrusted data and is
deliberately ignored. There is no AI-based approval path: the decision is a
pure Decimal comparison against the configured limit.

All monetary arithmetic uses :class:`decimal.Decimal`. Floating-point values are
only converted through their string representation to avoid binary artefacts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SUPPORTED_CURRENCY = "AED"

# Stable validation codes shared with the HTTP error envelope.
MISSING_AMOUNT = "missing_refund_amount"
INVALID_AMOUNT = "invalid_refund_amount"
NON_POSITIVE_AMOUNT = "non_positive_refund_amount"
UNSUPPORTED_CURRENCY = "unsupported_currency"
LIMIT_EXCEEDED = "refund_limit_exceeded"
MISSING_TX_REF = "missing_tx_ref"
APPROVED = "refund_approved"


class RefundValidationError(ValueError):
    """A client-visible refund validation failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True, slots=True)
class RefundValidation:
    """Structured result of validating a refund request."""

    approved: bool
    code: str
    message: str
    status_code: int
    tx_ref: str | None
    amount_aed: Decimal | None
    currency: str
    limit_aed: Decimal
    details: dict[str, Any] | None = None


def format_amount(value: Decimal) -> str:
    """Render a Decimal as a plain JSON-safe string without exponent notation."""

    return format(value, "f")


def parse_refund_amount(value: Any) -> Decimal:
    """Parse a JSON refund amount into a finite, positive Decimal.

    Accepts integers, finite floats, numeric strings, and Decimals. Booleans are
    rejected even though ``bool`` is an ``int`` subclass.

    Raises:
        RefundValidationError: For missing, malformed, non-finite, zero, or
            negative amounts.
    """

    if value is None:
        raise RefundValidationError(
            MISSING_AMOUNT,
            "Refund event requires a payload.refund_aed amount.",
            details={"field": "payload.refund_aed"},
        )

    if isinstance(value, bool):
        raise RefundValidationError(
            INVALID_AMOUNT,
            "Refund amount must be a positive number.",
            details={"field": "payload.refund_aed"},
        )

    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, int):
            amount = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidOperation
            # str() preserves the decimal literal the sender intended and
            # avoids exposing binary floating-point artefacts.
            amount = Decimal(str(value))
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise InvalidOperation
            amount = Decimal(text)
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RefundValidationError(
            INVALID_AMOUNT,
            "Refund amount must be a valid finite number.",
            details={"field": "payload.refund_aed"},
        ) from exc

    if not amount.is_finite():
        raise RefundValidationError(
            INVALID_AMOUNT,
            "Refund amount must be a finite number; NaN and infinity are not allowed.",
            details={"field": "payload.refund_aed"},
        )

    if amount <= 0:
        raise RefundValidationError(
            NON_POSITIVE_AMOUNT,
            "Refund amount must be greater than zero.",
            details={"field": "payload.refund_aed"},
        )

    return amount


def validate_currency(value: Any) -> str:
    """Return the normalized currency code, defaulting to AED when absent.

    Raises:
        RefundValidationError: If the currency is not a supported string code.
    """

    if value is None:
        return SUPPORTED_CURRENCY

    if not isinstance(value, str) or not value.strip():
        raise RefundValidationError(
            UNSUPPORTED_CURRENCY,
            f"Only {SUPPORTED_CURRENCY} refunds are supported.",
            details={"field": "payload.currency", "supported_currency": SUPPORTED_CURRENCY},
        )

    currency = value.strip().upper()
    if currency != SUPPORTED_CURRENCY:
        raise RefundValidationError(
            UNSUPPORTED_CURRENCY,
            f"Only {SUPPORTED_CURRENCY} refunds are supported.",
            details={
                "field": "payload.currency",
                "currency": currency,
                "supported_currency": SUPPORTED_CURRENCY,
            },
        )

    return currency


def enforce_refund_limit(
    *,
    refund_amount_aed: Decimal,
    limit_aed: Decimal,
) -> None:
    """Reject a refund strictly above the configured AED limit.

    Equality is accepted: with a 320 AED limit, 320 AED is allowed and 320.01
    AED is rejected.

    Raises:
        RefundValidationError: When the amount exceeds the limit.
    """

    if refund_amount_aed > limit_aed:
        raise RefundValidationError(
            LIMIT_EXCEEDED,
            "Refund exceeds the configured refund limit.",
            status_code=403,
            details={
                "requested_aed": format_amount(refund_amount_aed),
                "limit_aed": format_amount(limit_aed),
                "currency": SUPPORTED_CURRENCY,
            },
        )


def validate_refund(
    payload: Mapping[str, Any],
    *,
    limit_aed: Decimal,
) -> RefundValidation:
    """Validate a refund payload against the server-side limit.

    The payload is treated as untrusted data. Only ``tx_ref_3126``,
    ``refund_aed``, and ``currency`` are consulted, so fields such as
    ``customer_message`` cannot influence the decision.
    """

    tx_ref_value = payload.get("tx_ref_3126")
    tx_ref = tx_ref_value.strip() if isinstance(tx_ref_value, str) else None

    try:
        if not tx_ref:
            raise RefundValidationError(
                MISSING_TX_REF,
                "Refund event requires a non-empty payload.tx_ref_3126 string.",
                details={"field": "payload.tx_ref_3126"},
            )
        currency = validate_currency(payload.get("currency"))
        amount = parse_refund_amount(payload.get("refund_aed"))
        enforce_refund_limit(refund_amount_aed=amount, limit_aed=limit_aed)
    except RefundValidationError as exc:
        return RefundValidation(
            approved=False,
            code=exc.code,
            message=exc.message,
            status_code=exc.status_code,
            tx_ref=tx_ref,
            amount_aed=None,
            currency=SUPPORTED_CURRENCY,
            limit_aed=limit_aed,
            details=exc.details,
        )

    return RefundValidation(
        approved=True,
        code=APPROVED,
        message=(
            "Refund approved for simulation; no payment provider was contacted."
        ),
        status_code=200,
        tx_ref=tx_ref,
        amount_aed=amount,
        currency=currency,
        limit_aed=limit_aed,
    )
