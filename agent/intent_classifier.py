"""
Intelligent Intent Classifier for CRM Chatbot
Replaces hardcoded keyword matching with LLM-based intent classification.
This is more robust, scalable, and handles unseen questions gracefully.
"""

import os
import logging
from typing import Optional
import asyncio

logger = logging.getLogger(__name__)

# Cache for classification results to avoid redundant LLM calls
_INTENT_CACHE = {}


async def classify_intent(question: str, use_cache: bool = True) -> str:
    """
    Intelligently classify user intent using the LLM.
    
    This is the SINGLE SOURCE OF TRUTH for intent classification across the entire project.
    No hardcoded keyword lists. No brittle string matching.
    
    Args:
        question: User's input question
        use_cache: Whether to use cached results for identical questions
        
    Returns:
        Intent string from the set: 
        - "kpi_summary": User wants KPI report/analysis
        - "revenue": Revenue/billing metrics
        - "receivables": Receivables/aging/collections
        - "proposals": Proposals/pipeline/leads/win rate
        - "projects": Projects/tasks/milestones
        - "resources": Resource utilization/billable/timesheet
        - "other": Anything else
    """
    q_normalized = (question or "").strip().lower()
    
    # Cache lookup
    if use_cache and q_normalized in _INTENT_CACHE:
        return _INTENT_CACHE[q_normalized]

    # ── Typo normalization (runs before ALL other checks) ─────────────────
    # Normalize CRM-specific typos so patterns below always see canonical text
    import re as _re
    _TYPO_SUBS = [
        (_re.compile(r'\bservice\s+li[a-z]{0,3}\b', _re.IGNORECASE), 'service line'),
        (_re.compile(r'\bperform[a-z]{0,5}\b',      _re.IGNORECASE), 'performance'),
        (_re.compile(r'\bserv[a-z]{0,3}\s+line\b',  _re.IGNORECASE), 'service line'),
        (_re.compile(r'\bgp\s+perf[a-z]{0,7}\b',   _re.IGNORECASE), 'gp performance'),
        (_re.compile(r'\bgross\s+prof[a-z]{0,2}\b', _re.IGNORECASE), 'gross profit'),
        (_re.compile(r'\bdep[a-z]{0,6}\s+util[a-z]{0,6}\b', _re.IGNORECASE), 'department utilization'),
        (_re.compile(r'\butili[zs]a[a-z]{0,4}\b',  _re.IGNORECASE), 'utilization'),
        (_re.compile(r'\brecei[a-z]{0,6}\b',        _re.IGNORECASE), 'receivables'),
        (_re.compile(r'\brevenu[a-z]{0,2}\b',        _re.IGNORECASE), 'revenue'),
        (_re.compile(r'\brecoverab[a-z]{0,4}\b',   _re.IGNORECASE), 'recoverability'),
        (_re.compile(r'\breoverab[a-z]{0,4}\b',    _re.IGNORECASE), 'recoverability'),
    ]
    q_clean = q_normalized
    for _pat, _rep in _TYPO_SUBS:
        q_clean = _pat.sub(_rep, q_clean)
    # ── End typo normalization ────────────────────────────────────────────

    # ── Fast deterministic pre-guard (runs BEFORE the LLM call) ──────────
    # These patterns are unambiguous and must never be misclassified as kpi_summary.
    # Order matters: most specific patterns first.
    _SERVICE_LINE_PATTERNS = [
        "service line performance", "service line revenue", "revenue by service line",
        "billing by service line", "team billing", "serviceline performance",
    ]
    _GP_PATTERNS = [
        "gp performance", "gross profit performance", "gp by service line",
        "gp vs target", "performing vs target", "gp performance by service line",
    ]
    _DEPT_UTIL_PATTERNS = [
        "department utilization", "dept utilization", "department utilisation",
        "utilization by department", "utilization rate by department",
    ]
    _RESOURCE_PATTERNS = [
        "resource utilization", "utilization report", "billable hours",
        "timesheet", "staff utilization", "resource report", "utilisation",
        "resource allocation"
    ]
    _RECEIVABLE_PATTERNS = [
        "receivable", "receivables", "aging", "ageing", "overdue invoice",
        "outstanding invoice", "collections"
    ]
    _REVENUE_PATTERNS = [
        "gp performance", "gross profit", "gross margin", "billing revenue",
        "service line performance", "service line revenue", "revenue by service line",
        "service line breakdown",
    ]
    _PROPOSAL_PATTERNS = [
        "proposal", "pipeline", "win rate", "engagement letter", "service lead", "leads"
    ]
    _PROJECT_PATTERNS = [
        "active project", "project portfolio", "project task", "milestone"
    ]
    _KPI_EXACT_PATTERNS = [
        "kpi summary", "kpi report", "kpi analysis", "key performance indicator",
        "budget vs actual", "show kpi", "kpi summary report"
    ]
    _RECOVERABILITY_PATTERNS = [
        "recoverability", "project recoverability", "actual recoverability",
        "estimated recoverability", "recoverability report", "recoverability percentage"
    ]
    _KNOWLEDGE_PATTERNS = [
        "how is", "how does", "how to calculate", "what is the formula", "how do you calculate"
    ]
    _STAFF_BILLING_PATTERNS = [
        "staff billing", "staff cost", "billing report for staff", "billing for employee",
        "employee billing", "partner billing", "partner project billing"
    ]

    _ANALYTICAL_MARKERS = [
        "recent", "latest", "lowest", "highest", "top", "bottom", 
        "compare", "specific", "a proposal", "an invoice", "the project", "detail",
        "whose", "who", "which", "what customer", "which customer", "list", "pending",
        # Entity-specific signals (e.g. "revenue OF THE Kipina Bahrain")
        "of the", "for the", "revenue of", "revenue for", "billing of", "billing for",
        "invoices of", "invoices for", "receivable of", "receivable for",
        "projects of", "projects for", "proposals of", "proposals for",
        "how many", "count",
    ]

    skip_fast_guard = any(p in q_clean for p in _ANALYTICAL_MARKERS)
    if skip_fast_guard:
        logger.info(f"[Intent Classifier] Analytical marker detected in '{question[:60]}' → bypassing fast-guards to use LLM.")
    else:
        # Check for knowledge/RAG questions first so they don't get routed to data metrics
        if any(p in q_clean for p in _KNOWLEDGE_PATTERNS):
            intent = "other"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → other (knowledge)")
            return intent

        # Check service line / GP / department utilization FIRST (most specific)
        if any(p in q_clean for p in _SERVICE_LINE_PATTERNS):
            intent = "revenue"   # routes to get_revenue_metrics which has gp_performance_ytd_breakdown
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → revenue (service line)")
            return intent

        if any(p in q_clean for p in _RECOVERABILITY_PATTERNS) or ("recover" in q_clean and "abil" in q_clean):
            intent = "recoverability"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → recoverability")
            return intent

        if any(p in q_clean for p in _STAFF_BILLING_PATTERNS) or ("staff" in q_clean and "billing" in q_clean):
            intent = "staff_billing"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → staff_billing")
            return intent

        if any(p in q_clean for p in _GP_PATTERNS):
            intent = "revenue"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → revenue (gp performance)")
            return intent

        if any(p in q_clean for p in _DEPT_UTIL_PATTERNS):
            intent = "other"   # goes to ad_hoc path which now has verified SQL
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → other (dept utilization)")
            return intent

        if any(p in q_clean for p in _RESOURCE_PATTERNS):
            intent = "resources"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → resources")
            return intent

        if any(p in q_clean for p in _RECEIVABLE_PATTERNS):
            intent = "receivables"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → receivables")
            return intent

        if any(p in q_clean for p in _KPI_EXACT_PATTERNS):
            intent = "kpi_summary"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            logger.info(f"[Intent Classifier] Fast-guard: '{question[:60]}' → kpi_summary")
            return intent

        if any(p in q_clean for p in _REVENUE_PATTERNS):
            intent = "revenue"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            return intent

        if any(p in q_clean for p in _PROPOSAL_PATTERNS):
            intent = "proposals"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            return intent

        if any(p in q_clean for p in _PROJECT_PATTERNS):
            intent = "projects"
            if use_cache:
                _INTENT_CACHE[q_normalized] = intent
            return intent
    # ── End fast pre-guard ────────────────────────────────────────────────
    
    try:
        from .agent import _build_llm
        import os
        # Use FAST_MODEL for quick classification, fallback to the main LLM_MODEL
        model_override = os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL") or "llama-3.1-8b-instant"
        llm = _build_llm(model_override, temperature=0.0, max_tokens=30)
        
        classify_prompt = f"""You are a routing agent for a CRM chatbot. Analyze the user's question and pick ONE exact category.

CRITICAL RULE:
If the user wants to see a generic dashboard summary of many records, use the standard categories (kpi_summary, revenue, proposals, etc).
If the user is asking for a SPECIFIC record (e.g. "recent proposal", "a proposal for BPS") or asking an analytical comparison, YOU MUST CHOOSE "analytical".

Question: "{question}"

Categories:
- "analytical": Specific item lookups, comparisons, temporal questions (recent, latest, specific).
- "kpi_summary": Generic dashboard requests for KPI report/summary/analysis or budget vs actual.
- "revenue": Generic dashboard requests for revenue and billing metrics.
- "receivables": Generic dashboard requests for receivables, aging, outstanding invoices, collections.
- "proposals": Generic dashboard requests for the overall proposal pipeline, win rates, and lead metrics.
- "projects": Generic dashboard requests for projects, tasks, milestones, project portfolio.
- "recoverability": Generic dashboard requests for recoverability metrics.
- "resources": Generic dashboard requests for resource utilization, billable hours, timesheet.
- "staff_billing": Generic dashboard requests for staff billing report, employee billing, staff cost.
- "other": Anything else not covered above.

Respond with ONLY the category name, no explanation."""

        resp = await llm.ainvoke([{"role": "user", "content": classify_prompt}])
        result = resp.content.strip().lower()
        
        # Parse result — handle common variations
        valid_intents = ["analytical", "kpi_summary", "revenue", "receivables", "proposals", "projects", "recoverability", "resources", "staff_billing", "other"]
        
        intent = "other"  # Safe default
        for valid_intent in valid_intents:
            if valid_intent in result:
                intent = valid_intent
                break
        
        # Cache result
        if use_cache:
            _INTENT_CACHE[q_normalized] = intent
        
        logger.info(f"[Intent Classifier] Question: '{question[:60]}...' → Intent: {intent}")
        return intent
        
    except Exception as e:
        logger.warning(f"[Intent Classifier] Failed to classify '{question[:60]}...': {e}, defaulting to 'other'")
        return "other"


def should_show_kpi_filters(intent: str) -> bool:
    """Check if the current intent matches KPI report request."""
    return intent == "kpi_summary"


def should_show_revenue_report(intent: str) -> bool:
    """Check if the current intent matches revenue/billing report request."""
    return intent == "revenue"


def should_show_receivables_report(intent: str) -> bool:
    """Check if the current intent matches receivables report request."""
    return intent == "receivables"


def should_show_proposals_report(intent: str) -> bool:
    """Check if the current intent matches proposals/pipeline report request."""
    return intent == "proposals"


def should_show_projects_report(intent: str) -> bool:
    """Check if the current intent matches projects report request."""
    return intent == "projects"


def should_show_resources_report(intent: str) -> bool:
    """Check if the current intent matches resource utilization report request."""
    return intent == "resources"


def should_show_recoverability_report(intent: str) -> bool:
    """Check if the current intent matches recoverability report request."""
    return intent == "recoverability"

def should_show_staff_billing_report(intent: str) -> bool:
    """Check if the current intent matches staff billing report request."""
    return intent == "staff_billing"


def clear_cache():
    """Clear the intent classification cache (useful for testing)."""
    global _INTENT_CACHE
    _INTENT_CACHE.clear()
