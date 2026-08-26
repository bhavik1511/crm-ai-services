"""
Cache package initialization.
Provides secure Redis hot-cache operations with AES-256-GCM encryption and HMAC keys.
"""

from cache.encryption import get_encryption_manager, CacheEncryptionManager
from cache.cache_key import (
    generate_secure_cache_key,
    generate_session_cache_key,
    generate_clarification_cache_key,
    build_rbac_scope_fingerprint,
)
from cache.secure_cache_manager import get_secure_cache_manager, SecureCacheManager

__all__ = [
    "get_encryption_manager",
    "CacheEncryptionManager",
    "generate_secure_cache_key",
    "generate_session_cache_key",
    "generate_clarification_cache_key",
    "build_rbac_scope_fingerprint",
    "get_secure_cache_manager",
    "SecureCacheManager",
]
