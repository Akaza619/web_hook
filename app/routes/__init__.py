"""HTTP routing layer.

Phase 2 exposed ``GET /health`` and ``POST /api/guard``. Phase 3 enforces
HMAC-SHA256 authentication, Phase 4 validates refunds against the configured
AED limit, Phase 5 reports Redis health alongside application health, and
Phase 6 makes refund processing idempotent with an atomic Redis lock.
"""
