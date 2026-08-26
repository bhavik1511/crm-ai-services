"""
HMAC-SHA256 Deterministic Cache Key Generator with RBAC Scope Isolation.
Ensures zero exposure of user questions, customer names, or CRM data in Redis keys.
"""

import os
import hmac
import json
import hashlib
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


def _get_hmac_secret() -> bytes:
    """Retrieve or derive HMAC secret key. NEVER reuses JWT_SECRET."""
    env_secret = os.getenv("CACHE_HMAC_SECRET")
    if env_secret:
        return env_secret.strip().encode("utf-8")
    
    enc_key = os.getenv("REDIS_CACHE_ENCRYPTION_KEY")
    if enc_key:
        return hashlib.sha256((enc_key + ":hmac_key_v1").encode("utf-8")).digest()
    
    is_production = os.getenv("ENVIRONMENT", "").lower() in ("production", "prod")
    if is_production:
        raise ValueError("[CRITICAL SECURITY ERROR] CACHE_HMAC_SECRET or REDIS_CACHE_ENCRYPTION_KEY must be set in production!")
    
    dev_secret = "dev_cache_hmac_secret_key_v1!"
    return hashlib.sha256(dev_secret.encode("utf-8")).digest()


def build_rbac_scope_fingerprint(user_context: Optional[Dict[str, Any]]) -> str:
    """
    Build structured authorization scope string for RBAC cache isolation.
    Includes user_id, role, hierarchy_level, department_id, and service_line_id.
    """
    if not user_context or not isinstance(user_context, dict):
        return "role:public|user:0|level:9"

    user_id = str(user_context.get("user_id") or 0)
    emp_id = str(user_context.get("employee_id") or "")
    role = str(user_context.get("role") or user_context.get("role_name") or "Staff")
    level = str(user_context.get("hierarchy_level") or 4)
    dept_id = str(user_context.get("department_id") or "")
    svc_id = str(user_context.get("service_line_id") or "")

    return f"u:{user_id}|e:{emp_id}|r:{role}|l:{level}|d:{dept_id}|s:{svc_id}"


def generate_secure_cache_key(
    prefix: str,
    user_context: Optional[Dict[str, Any]],
    query_or_intent: str,
    extra_params: Optional[Dict[str, Any]] = None,
    data_version: str = "v1"
) -> str:
    """
    Generate an opaque, deterministic HMAC-SHA256 cache key.
    
    Format: <prefix>:<64_char_hmac_hex>
    Example: entity_cache:8f71c9a3b...
    
    Redis key contains ZERO plaintext questions, customer names, or amounts.
    """
    hmac_secret = _get_hmac_secret()
    rbac_scope = build_rbac_scope_fingerprint(user_context)
    
    # Normalize query/intent string
    norm_query = str(query_or_intent or "").strip().lower()

    # Sort extra parameters (filters, financial_year, etc.)
    sorted_params = ""
    if extra_params and isinstance(extra_params, dict):
        clean_params = {
            k: v for k, v in sorted(extra_params.items())
            if v is not None and k not in ("raw_tool_results", "jwt_token")
        }
        sorted_params = json.dumps(clean_params, sort_keys=True, default=str)

    raw_fingerprint = f"scope:[{rbac_scope}]|query:[{norm_query}]|params:[{sorted_params}]|ver:[{data_version}]"
    
    digest = hmac.new(hmac_secret, raw_fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()
    
    clean_prefix = str(prefix or "cache").strip().rstrip(":")
    return f"{clean_prefix}:{digest}"


def generate_session_cache_key(session_id: str) -> str:
    """Session IDs are already opaque UUIDs. Format as session:<opaque_session_id>."""
    return f"session:{session_id}"


def generate_clarification_cache_key(session_id: str) -> str:
    """Clarification keys format as clarification:<opaque_session_id>."""
    return f"clarification:{session_id}"
