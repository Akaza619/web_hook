"""Atomic Redis idempotency for refund webhook transactions.

A transaction reference is claimed with ``SET key value NX EX 300``. The stored
value is a JSON record containing a unique owner token and a state:

* ``processing`` - this request owns the transaction and is executing it.
* ``completed``  - the simulated refund succeeded; the record stays for the
  remaining TTL so retries cannot execute it again.

Only the owner can move the record to ``completed`` or delete it after a
processing failure. Both operations use Lua compare-and-delete/compare-and-set
scripts, so an expired lock that was re-acquired by another request is never
overwritten. There is no in-memory transaction set.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.redis_client import RedisManager

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_PREFIX = "pinhire:idempotency:"
DEFAULT_LOCK_TTL_SECONDS = 300
MAX_TX_REF_LENGTH = 128
TX_REF_FIELD = "payload.tx_ref_3126"

# Redis keys are binary-safe; only whitespace and control characters are refused.
_TX_REF_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]+$")

MISSING_TX_REF = "missing_tx_ref"
INVALID_TX_REF = "invalid_tx_ref"
PROCESSING_STATE = "processing"
COMPLETED_STATE = "completed"
DUPLICATE_STATE = "duplicate"


class TransactionReferenceError(ValueError):
    """A client-visible transaction-reference validation failure."""

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
class TransactionLock:
    """Result of attempting to acquire a transaction reference."""

    tx_ref: str
    key: str
    owner_token: str
    acquired: bool
    state: str
    value: str


@dataclass(frozen=True, slots=True)
class IdempotencyOutcome:
    """Result of executing an operation at most once."""

    status: str
    value: Any = None


def validate_transaction_reference(value: Any) -> str:
    """Validate and normalize ``payload.tx_ref_3126``.

    The reference must be a non-empty string (or integer) of at most 128
    characters without whitespace or control characters.

    Raises:
        TransactionReferenceError: For missing, non-string, empty, overlong, or
            character-invalid references.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)

    if value is None:
        raise TransactionReferenceError(
            MISSING_TX_REF,
            "Refund event requires a payload.tx_ref_3126 value.",
            details={"field": TX_REF_FIELD},
        )

    if not isinstance(value, str):
        raise TransactionReferenceError(
            INVALID_TX_REF,
            "Transaction reference must be a string.",
            details={"field": TX_REF_FIELD, "type": type(value).__name__},
        )

    tx_ref = value.strip()
    if not tx_ref:
        raise TransactionReferenceError(
            MISSING_TX_REF,
            "Refund event requires a non-empty payload.tx_ref_3126 string.",
            details={"field": TX_REF_FIELD},
        )

    if len(tx_ref) > MAX_TX_REF_LENGTH:
        raise TransactionReferenceError(
            INVALID_TX_REF,
            f"Transaction reference must be at most {MAX_TX_REF_LENGTH} characters.",
            details={"field": TX_REF_FIELD, "max_length": MAX_TX_REF_LENGTH},
        )

    if not _TX_REF_PATTERN.fullmatch(tx_ref):
        raise TransactionReferenceError(
            INVALID_TX_REF,
            "Transaction reference contains invalid characters.",
            details={
                "field": TX_REF_FIELD,
                "allowed": "no whitespace or control characters",
            },
        )

    return tx_ref


def idempotency_key(tx_ref: str) -> str:
    """Return the Redis key used for a transaction reference."""

    return f"{IDEMPOTENCY_KEY_PREFIX}{tx_ref}"


def _validate_ttl(ttl_seconds: int) -> int:
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("Idempotency TTL must be a positive integer number of seconds.")
    return ttl_seconds


def _record(
    *,
    tx_ref: str,
    owner_token: str,
    state: str,
) -> str:
    return json.dumps(
        {
            "tx_ref": tx_ref,
            "owner": owner_token,
            "state": state,
            "updated_at": time.time(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def acquire_transaction_lock(
    *,
    redis: RedisManager,
    tx_ref: str,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> TransactionLock:
    """Atomically claim ``tx_ref`` with SET NX EX.

    The first caller receives ``acquired=True``. Concurrent or later callers
    receive ``acquired=False`` and must be treated as duplicates.
    """

    normalized = validate_transaction_reference(tx_ref)
    _validate_ttl(ttl_seconds)

    key = idempotency_key(normalized)
    owner_token = secrets.token_urlsafe(32)
    value = _record(
        tx_ref=normalized,
        owner_token=owner_token,
        state=PROCESSING_STATE,
    )

    acquired = await redis.set_if_absent(key, value, ttl_seconds)
    return TransactionLock(
        tx_ref=normalized,
        key=key,
        owner_token=owner_token,
        acquired=acquired,
        state=PROCESSING_STATE if acquired else DUPLICATE_STATE,
        value=value,
    )


async def claim_transaction(
    *,
    redis: RedisManager,
    tx_ref: str,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    """Return True only for the caller that acquired the transaction lock."""

    lock = await acquire_transaction_lock(
        redis=redis,
        tx_ref=tx_ref,
        ttl_seconds=ttl_seconds,
    )
    return lock.acquired


async def complete_transaction(
    *,
    redis: RedisManager,
    lock: TransactionLock,
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> bool:
    """Mark an owned transaction completed without releasing its protection.

    The completed record keeps a fresh TTL so retries remain duplicates for the
    full idempotency window.
    """

    if not lock.acquired:
        return False
    _validate_ttl(ttl_seconds)

    value = _record(
        tx_ref=lock.tx_ref,
        owner_token=lock.owner_token,
        state=COMPLETED_STATE,
    )
    return await redis.compare_and_set_with_expiration(
        lock.key,
        lock.value,
        value,
        ttl_seconds,
    )


async def release_transaction(
    *,
    redis: RedisManager,
    lock: TransactionLock,
) -> bool:
    """Release an owned processing lock after a failure.

    The Lua compare-and-delete only removes the key when the owner token still
    matches, so another request's lock is never deleted.
    """

    if not lock.acquired:
        return False

    return await redis.compare_and_delete(
        lock.key,
        lock.value,
    )


async def execute_once(
    *,
    redis: RedisManager,
    tx_ref: str,
    operation: Callable[[], Awaitable[Any]],
    ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
) -> IdempotencyOutcome:
    """Run ``operation`` at most once for ``tx_ref`` within the TTL window.

    * A duplicate returns ``status="duplicate"`` without running ``operation``.
    * A successful operation is recorded as completed and returns
      ``status="processed"``.
    * If the operation fails, the owned lock is released and the exception is
      re-raised, so a retry can run and no success is ever reported.
    * If the operation succeeds but recording completion fails, the
      ``processing`` lock is kept until its TTL expires, so a retry cannot run
      the operation a second time. The error is re-raised.
    """

    lock = await acquire_transaction_lock(
        redis=redis,
        tx_ref=tx_ref,
        ttl_seconds=ttl_seconds,
    )
    if not lock.acquired:
        return IdempotencyOutcome(status=DUPLICATE_STATE)

    try:
        value = await operation()
    except BaseException:
        try:
            await release_transaction(redis=redis, lock=lock)
        except Exception as exc:  # noqa: BLE001 - preserve the original failure
            logger.warning(
                "Could not release idempotency lock after failure (%s).",
                type(exc).__name__,
            )
        raise

    # The operation has run: never release the lock from here on.
    completed = await complete_transaction(
        redis=redis,
        lock=lock,
        ttl_seconds=ttl_seconds,
    )
    if not completed:
        # Ownership was lost (for example the lock expired). Do not claim the
        # refund was processed; another request may own the transaction now.
        logger.warning("Idempotency ownership was lost while completing a transaction.")
        return IdempotencyOutcome(status=DUPLICATE_STATE)

    return IdempotencyOutcome(status="processed", value=value)


async def get_transaction_state(
    *,
    redis: RedisManager,
    tx_ref: str,
) -> str | None:
    """Return the stored idempotency state, or None when no record exists."""

    normalized = validate_transaction_reference(tx_ref)
    raw = await redis.get(idempotency_key(normalized))
    if raw is None:
        return None

    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None

    if not isinstance(document, dict):
        return None
    state = document.get("state")
    return state if isinstance(state, str) else None
