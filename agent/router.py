"""
agent/router.py — Metadata-Driven Capability Router
=====================================================
Dynamically discovers fast-path eligible business capabilities from the
Capability Catalog/Tool Registry metadata.

Zero hardcoded capability dictionaries, zero static regex lists, zero custom
customer extraction logic. Delegates ALL entity resolution to entity_resolver.py.
Automatically extensible when new capabilities are added to capability_catalog.py.
"""

import re
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

def check_security_guards(raw_q: str) -> bool:
    """
    Dedicated Security Guard.
    Validates user query against prompt injection and security exploits.
    Returns True if query passes security check, False if query contains injection markers.
    """
    if not raw_q or not isinstance(raw_q, str):
        return True
    
    q_clean = raw_q.strip().lower()
    INJECTION_MARKERS = [
        "ignore previous", "ignore instructions", "ignore rules", "ignore context", "ignore tier",
        "disregard", "system override", "override role", "bypass rbac", "bypass security",
        "bypass validation", "bypass instructions", "reveal system prompt", "unauthorized",
        "jailbreak", "developer mode", "dump database", "output prompt", "escalate privileges",
        "forget all previous", "system prompt extraction"
    ]
    if any(marker in q_clean for marker in INJECTION_MARKERS):
        logger.warning(f"[SecurityGuard] PROMPT INJECTION MARKER DETECTED in query '{raw_q[:50]}'.")
        return False
    return True

def route_query_fast_path(question: str, user_context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Metadata Router Security Guard.
    Performs security validation and delegates all natural language intent parsing to EnterprisePlanner.
    Zero keyword business-intent matching.
    """
    if not question or not isinstance(question, str):
        return None

    if not check_security_guards(question):
        return None

    # Zero natural language keyword matching for business intent.
    # All queries are routed to EnterprisePlanner to generate a CanonicalIntent.
    return None

def route_canonical_intent(canonical_intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Metadata-driven execution router.
    Routes a validated CanonicalIntent or ExecutionContract to its CapabilityCatalog execution strategy.
    """
    if not canonical_intent or not isinstance(canonical_intent, dict):
        return None
        
    cap_id = canonical_intent.get("capability") or canonical_intent.get("capability_id")
    if not cap_id:
        return None

    try:
        from registry.capability_catalog import get_capability_metadata
        cap_meta = get_capability_metadata(cap_id)
        if cap_meta:
            return {
                "capability": cap_meta,
                "execution_strategy": cap_meta.get("execution_strategy", "DEFAULT")
            }
    except Exception as e:
        logger.error(f"[MetadataRouter] Failed routing canonical intent for capability '{cap_id}': {e}")
    return None
