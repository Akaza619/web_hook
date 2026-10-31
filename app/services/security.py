"""HMAC-SHA256 signature generation and verification.

The signature is computed over the exact raw request bytes, never over a
reserialized JSON document. Verification uses :func:`hmac.compare_digest` to
avoid timing leaks.

The signing secret is supplied by the caller (loaded from environment-based
configuration); it is never stored or logged in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import re

# SHA-256 produces 32 bytes, rendered as 64 hexadecimal characters.
SIGNATURE_HEX_LENGTH = hashlib.sha256().digest_size * 2
_HEX_SIGNATURE_RE = re.compile(rf"^[0-9a-fA-F]{{{SIGNATURE_HEX_LENGTH}}}$")


class SignatureError(ValueError):
    """Base class for problems with an incoming signature header."""

    code = "invalid_signature"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MissingSignatureError(SignatureError):
    """Raised when the signature header is absent or blank."""

    code = "missing_signature"

    def __init__(self) -> None:
        super().__init__("Missing webhook signature.")


class MalformedSignatureError(SignatureError):
    """Raised when the signature header is not a SHA-256 hex digest."""

    code = "malformed_signature"

    def __init__(self) -> None:
        super().__init__(
            "Webhook signature must be a 64-character hexadecimal "
            "HMAC-SHA256 digest."
        )


def generate_signature(raw_body: bytes, secret: str) -> str:
    """Return the lowercase hexadecimal HMAC-SHA256 of ``raw_body``.

    Args:
        raw_body: Exact bytes received on the wire.
        secret: Signing secret loaded from configuration.

    Raises:
        TypeError: If ``raw_body`` is not bytes.
        ValueError: If ``secret`` is empty.
    """

    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes.")
    if not secret:
        raise ValueError("A signing secret is required to generate a signature.")

    return hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def normalize_signature(signature: str | None) -> str:
    """Validate and normalize an incoming signature header value.

    Surrounding whitespace and uppercase hex digits are accepted. Everything
    else must be exactly 64 hexadecimal characters.

    Raises:
        MissingSignatureError: If the header is absent or blank.
        MalformedSignatureError: If the value is not a SHA-256 hex digest.
    """

    if signature is None or not signature.strip():
        raise MissingSignatureError()

    value = signature.strip()
    if not _HEX_SIGNATURE_RE.fullmatch(value):
        raise MalformedSignatureError()

    return value.lower()


def verify_signature(
    *,
    raw_body: bytes,
    signature: str | None,
    secret: str | None,
) -> bool:
    """Return True only when ``signature`` matches HMAC-SHA256(raw_body).

    Missing, malformed, or incorrect signatures return False. A missing secret
    also returns False so callers can never accidentally accept unsigned
    traffic.
    """

    if not secret:
        return False

    try:
        received = normalize_signature(signature)
    except SignatureError:
        return False

    expected = generate_signature(raw_body, secret)
    return hmac.compare_digest(expected, received)
