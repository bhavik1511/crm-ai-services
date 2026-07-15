"""
Redis connection module using redis.asyncio.
Singleton pattern — one shared async Redis client across the app.
Automatically detects TLS (rediss://) for Upstash prod vs plain (redis://) for dev.
"""

import os
import logging
from redis.asyncio import Redis
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------
_redis_client: Redis | None = None


def get_redis() -> Redis:
    """
    Returns the async Redis client (singleton).
    Reads REDIS_URL from env.
    - redis://  → plain connection (dev / local)
    - rediss:// → TLS connection  (prod / Upstash)
    """
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    use_ssl = redis_url.startswith("rediss://")

    _redis_client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )

    logger.info(
        f"[Redis] Connected to {'TLS (prod)' if use_ssl else 'plain (dev)'} endpoint"
    )

    return _redis_client
