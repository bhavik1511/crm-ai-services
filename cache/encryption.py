"""
AES-256-GCM Encryption Manager for Redis Hot-Cache.
Provides application-side authenticated encryption and decryption for confidential CRM data.
"""

import os
import json
import base64
import logging
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# Version & Algorithm Constants
ENCRYPTION_VERSION = 1
ENCRYPTION_ALGORITHM = "AES-256-GCM"


class CacheEncryptionManager:
    """
    Handles AES-256-GCM encryption and decryption of cached payloads.
    Guarantees zero plaintext confidential data in external storage.
    """

    def __init__(self, key_override: Optional[bytes] = None):
        self._raw_key = key_override or self._get_secret_key()
        self._aesgcm = AESGCM(self._raw_key)

    def _get_secret_key(self) -> bytes:
        """
        Derive 32-byte (256-bit) AES key from REDIS_CACHE_ENCRYPTION_KEY environment variable.
        Supports hex, base64, or raw string formats.
        NEVER derives key from JWT_SECRET.
        """
        env_key = os.getenv("REDIS_CACHE_ENCRYPTION_KEY")
        if env_key:
            env_key_str = env_key.strip()
            # If 64 hex characters -> hex decode
            if len(env_key_str) == 64:
                try:
                    return bytes.fromhex(env_key_str)
                except ValueError:
                    pass
            # If base64 encoded
            try:
                decoded = base64.b64decode(env_key_str, validate=True)
                if len(decoded) == 32:
                    return decoded
            except Exception:
                pass
            # Otherwise SHA-256 hash the key string to derive 32 bytes
            return hashlib.sha256(env_key_str.encode("utf-8")).digest()

        is_production = os.getenv("ENVIRONMENT", "").lower() in ("production", "prod")
        if is_production:
            raise ValueError("[CRITICAL SECURITY ERROR] REDIS_CACHE_ENCRYPTION_KEY must be set in production! Startup aborted.")

        logger.warning("[CacheEncryption] REDIS_CACHE_ENCRYPTION_KEY not set. Using dedicated dev fallback key. Set REDIS_CACHE_ENCRYPTION_KEY in production!")
        dev_secret = "dev_redis_cache_encryption_secret_key_32_bytes_v1!"
        return hashlib.sha256(dev_secret.encode("utf-8")).digest()

    def encrypt(self, payload: Dict[str, Any]) -> str:
        """
        Encrypt dictionary payload into versioned JSON envelope.
        
        Payload is serialized to JSON -> encrypted via AES-256-GCM with 12-byte random nonce.
        Returns serialized envelope string.
        """
        if not isinstance(payload, dict):
            raise TypeError("Payload must be a dictionary.")

        def _json_default(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        plaintext_bytes = json.dumps(payload, default=_json_default).encode("utf-8")
        
        # 12-byte cryptographically secure random nonce
        nonce = os.urandom(12)
        
        # AES-256-GCM encrypt -> returns ciphertext + tag appended
        ciphertext = self._aesgcm.encrypt(nonce, plaintext_bytes, associated_data=None)

        envelope = {
            "v": ENCRYPTION_VERSION,
            "alg": ENCRYPTION_ALGORITHM,
            "nonce": base64.b64encode(nonce).decode("utf-8"),
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
        }
        return json.dumps(envelope)

    def decrypt(self, envelope_str: str) -> Optional[Dict[str, Any]]:
        """
        Decrypt versioned JSON envelope back into dictionary payload.
        
        Fails closed (returns None) if envelope is malformed, version mismatch,
        invalid authentication tag, or decryption error.
        """
        if not envelope_str or not isinstance(envelope_str, str):
            return None

        try:
            envelope = json.loads(envelope_str)
            if not isinstance(envelope, dict):
                return None

            if envelope.get("v") != ENCRYPTION_VERSION or envelope.get("alg") != ENCRYPTION_ALGORITHM:
                logger.warning(f"[CacheEncryption] Unsupported envelope version/alg: v={envelope.get('v')}, alg={envelope.get('alg')}")
                return None

            nonce_b64 = envelope.get("nonce")
            ciphertext_b64 = envelope.get("ciphertext")
            if not nonce_b64 or not ciphertext_b64:
                return None

            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ciphertext_b64)

            # Decrypt & verify authentication tag (AESGCM verifies tag automatically)
            plaintext_bytes = self._aesgcm.decrypt(nonce, ciphertext, associated_data=None)
            return json.loads(plaintext_bytes.decode("utf-8"))

        except Exception as e:
            # Fail-closed: log warning without revealing ciphertext or key details
            logger.warning(f"[CacheEncryption] Decryption failed (integrity/tag error or invalid envelope): {e}")
            return None


# Singleton instance
_encryption_manager: Optional[CacheEncryptionManager] = None


def get_encryption_manager() -> CacheEncryptionManager:
    """Return singleton CacheEncryptionManager instance."""
    global _encryption_manager
    if _encryption_manager is None:
        _encryption_manager = CacheEncryptionManager()
    return _encryption_manager
