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

ANALYSIS_KEYWORDS = frozenset({
    "why", "how", "explain", "analyse", "analyze", "comparison", "compare",
    "reason", "insight", "insights", "trend", "breakdown", "root cause"
})

def route_query_fast_path(question: str, user_context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Metadata-driven router.
    Dynamically fetches BUSINESS_CAPABILITIES from registry.capability_catalog.
    Checks fast_path_eligible metadata and matches intent keywords.
    Delegates all entity extraction & resolution to entity_resolver.py.
    """
    if not question or not isinstance(question, str):
        return None

    raw_q = question.strip()
    q_clean = raw_q.lower()

    # 0. Prompt Injection Guard: Reject fast-path for prompts containing instruction override or exploit markers
    INJECTION_MARKERS = [
        "ignore previous", "ignore instructions", "ignore rules", "ignore context", "ignore tier",
        "disregard", "system override", "override role", "bypass rbac", "bypass security",
        "bypass validation", "bypass instructions", "reveal system prompt", "unauthorized",
        "jailbreak", "developer mode", "dump database", "output prompt", "escalate privileges",
        "forget all previous", "system prompt extraction"
    ]
    if any(marker in q_clean for marker in INJECTION_MARKERS):
        logger.warning(f"[MetadataRouter] PROMPT INJECTION MARKER DETECTED in query '{raw_q[:50]}' -> Bypassing Fast-Path.")
        return None

    # Dynamic import from single source of truth (Capability Catalog)
    try:
        from registry.capability_catalog import BUSINESS_CAPABILITIES, get_capability_metadata
    except Exception as e:
        logger.error(f"[MetadataRouter] Could not import capability catalog: {e}")
        return None

    # 1. Discover Analytical Intent / Complex Query indicators from catalog metadata
    analytical_meta = get_capability_metadata("analytical_query") or {}
    analytical_ops = (analytical_meta.get("implementations", [{}])[0]).get("supported_operations", [])
    
    # Generic analytical trigger verbs/ops derived from catalog
    analytical_triggers = set(analytical_ops + ["analyze", "analysis", "compare", "comparison", "insight", "recommendation", "trend", "vs", "versus"])
    
    if any(re.search(r'\b' + re.escape(kw) + r'\b', q_clean) for kw in analytical_triggers):
        logger.info(f"[MetadataRouter] Query '{raw_q[:50]}' matched analytical catalog operations -> Delegating to EnterprisePlanner LLM.")
        return None

    # 2. Discover Fast-Path Eligible Capabilities dynamically from catalog
    fast_path_caps = []
    for cap in BUSINESS_CAPABILITIES:
        if cap.get("fast_path_eligible"):
            fast_path_caps.append(cap)

    matched_capabilities = []

    for cap in fast_path_caps:
        cap_id = cap["id"]
        keywords = cap.get("intent_keywords", [])
        
        # Also derive fallback keywords from capability ID
        if not keywords:
            keywords = [cap_id.replace("_", " ")]

        for kw in keywords:
            pattern = r'\b' + re.escape(kw.lower()) + r'\b'
            if re.search(pattern, q_clean):
                matched_capabilities.append(cap)
                break

    # If prompt matches multiple business capabilities -> Fall back to EnterprisePlanner LLM
    if len(matched_capabilities) > 1:
        cap_ids = [c["id"] for c in matched_capabilities]
        logger.info(f"[MetadataRouter] Query matched multiple catalog capabilities ({cap_ids}) -> Delegating to EnterprisePlanner LLM.")
        return None

    # 3. Fast-Path Match Found
    if len(matched_capabilities) == 1:
        target_cap = matched_capabilities[0]
        cap_id = target_cap["id"]
        
        logger.info(
            f"[MetadataRouter] FAST-PATH HIT: Query '{raw_q[:50]}' -> "
            f"Catalog Capability '{cap_id}' (Metadata-Driven, 0 Planner LLM Calls)."
        )

        # Delegate ALL entity extraction & resolution to Entity Resolver (agent/entity_resolver.py)
        return {
            "business_capabilities": [
                {
                    "id": cap_id,
                    "time_filter": None,
                    "filters": {}
                }
            ],
            "scope": "organization", # Entity Resolver will dynamically adjust scope if customer/project entity is resolved
            "entities": [], # Entity Resolver extracts and resolves entities dynamically from live CRM DB
            "missing_information": [],
            "confidence_score": 1.0,
            "response_mode": "DATA",
            "routed_by": "metadata_driven_router"
        }

    return None
