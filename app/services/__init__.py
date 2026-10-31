"""Business-logic services.

* ``security``     - HMAC-SHA256 signature generation/verification (Phase 3)
* ``refund``       - Deterministic AED refund validation (Phase 4)
* ``redis_client`` - Async Redis connection management and key/value utilities
                     (Phase 5)
* ``idempotency``  - Atomic Redis SET NX transaction de-duplication (Phase 6)
* ``faq``          - FAQ normalization, catalog answers, and Redis cache-aside
                     (Phase 7)
"""
