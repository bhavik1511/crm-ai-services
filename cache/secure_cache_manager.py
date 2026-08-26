"""
SecureCacheManager — High-Level Secure Interface for Redis Hot-Cache.
Enforces AES-256-GCM authenticated encryption and fail-closed error handling.
"""

import os
import logging
from typing import Dict, Any, Optional

from db.database_redis import get_redis
from cache.encryption import get_encryption_manager

logger = logging.getLogger(__name__)


class SecureCacheManager:
    """
    Wraps all Redis hot-cache read and write operations with AES-256-GCM encryption.
    Guarantees that no plaintext confidential data exists in Redis.
    """

    def __init__(self):
        self.encryptor = get_encryption_manager()

    async def get_secure(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve and decrypt cached dictionary from Redis.
        
        Fail-closed: Returns None if key is missing, Redis fails, envelope is invalid,
        or AES-256-GCM decryption/tag verification fails.
        """
        if not key or not isinstance(key, str):
            return None

        try:
            redis = get_redis()
            cached_str = await redis.get(key)
            if not cached_str:
                return None

            decrypted_payload = self.encryptor.decrypt(cached_str)
            if decrypted_payload is None:
                logger.warning(f"[SecureCache] Decryption or tag verification failed for key={key[:24]}...")
                return None

            return decrypted_payload

        except Exception as e:
            logger.warning(f"[SecureCache] Redis read or decryption failed for key={key[:24]}...: {e}")
            return None

    async def set_secure(self, key: str, payload: Dict[str, Any], ttl_seconds: Optional[int] = None) -> bool:
        """
        Encrypt and write dictionary payload to Redis with TTL.
        
        Fail-closed: If payload cannot be encrypted or Redis write fails,
        returns False without writing ANY plaintext fallback.
        """
        if not key or not isinstance(payload, dict):
            return False

        if ttl_seconds is None:
            try:
                ttl_seconds = int(os.getenv("REDIS_CACHE_TTL_SECONDS", "300"))
            except (ValueError, TypeError):
                ttl_seconds = 300

        try:
            envelope_str = self.encryptor.encrypt(payload)
            redis = get_redis()
            await redis.setex(key, ttl_seconds, envelope_str)
            return True

        except Exception as e:
            # Strict fail-closed: NEVER write plaintext on error
            logger.error(f"[SecureCache] Encryption or Redis write failed for key={key[:24]}... (FAIL CLOSED): {e}")
            return False

    async def delete_secure(self, key: str) -> bool:
        """Delete cache key from Redis."""
        if not key:
            return False
        try:
            redis = get_redis()
            await redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"[SecureCache] Redis delete failed for key={key[:24]}...: {e}")
            return False

    async def expire_secure(self, key: str, ttl_seconds: int) -> bool:
        """Update TTL for cache key in Redis."""
        if not key:
            return False
        try:
            redis = get_redis()
            await redis.expire(key, ttl_seconds)
            return True
        except Exception as e:
            logger.warning(f"[SecureCache] Redis expire failed for key={key[:24]}...: {e}")
            return False


# Singleton instance
_secure_cache_manager: Optional[SecureCacheManager] = None


def get_secure_cache_manager() -> SecureCacheManager:
    """Return singleton SecureCacheManager instance."""
    global _secure_cache_manager
    if _secure_cache_manager is None:
        _secure_cache_manager = SecureCacheManager()
    return _secure_cache_manager
