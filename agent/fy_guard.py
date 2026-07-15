"""
fy_guard.py — Universal Financial Year Clarification Guard
===========================================================

Intercepts ANY time-sensitive chatbot query that does NOT explicitly specify
a Financial Year (FY) and asks the user to clarify which FY they mean.

Design goals:
  • Dynamic — automatically covers every topic that has FY relevance
    (revenue, GP, utilization, receivables, proposals, projects, KPIs, etc.)
    without any topic-by-topic maintenance.
  • Context-aware — if the conversation history already contains an FY
    answer from the user, that FY is reused (no repeated asking).
  • Precise — narrows the topic in the clarification message so the user
    knows exactly what the bot wants to clarify.
  • Non-intrusive — pure utility functions; no FastAPI dependency.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .intent_classifier import classify_intent


# ---------------------------------------------------------------------------
# Financial year helpers
# ---------------------------------------------------------------------------

def _current_fy() -> str:
    """Returns the current FY as a human-readable string, e.g. '2025-2026'."""
    now = datetime.utcnow()
    if now.month >= 10:
        return f"{now.year}-{now.year + 1}"
    return f"{now.year - 1}-{now.year}"


def _fy_options() -> list[str]:
    """Returns the three most relevant FY strings: previous, current, next."""
    now = datetime.utcnow()
    if now.month >= 10:
        base = now.year
    else:
        base = now.year - 1
    return [
        f"{base - 1}-{base}",
        f"{base}-{base + 1}",
        f"{base + 1}-{base + 2}",
    ]


# ---------------------------------------------------------------------------
# Core guard functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Core guard functions
# ---------------------------------------------------------------------------

# Patterns that explicitly mention an FY — if matched the guard is bypassed
_EXPLICIT_FY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bfy\s*20\d{2}\b", re.IGNORECASE),                        # fy2024, fy 2024
    re.compile(r"\bfy\s*20\d{2}\s*[-/]\s*20\d{2}\b", re.IGNORECASE),      # fy2024-2025
    re.compile(r"\b20\d{2}\s*[-/]\s*20\d{2}\b"),                           # 2024-2025
    re.compile(r"\bfinancial\s+year\s+20\d{2}", re.IGNORECASE),            # financial year 2024
    re.compile(r"\bfiscal\s+year\s+20\d{2}", re.IGNORECASE),               # fiscal year 2024
    re.compile(r"\bfy\s*'\d{2}\b", re.IGNORECASE),                         # fy'24
    re.compile(r"\b(this|current|last|previous)\s+(fy|fiscal\s+year|financial\s+year)\b", re.IGNORECASE),
]

# Patterns that refer to a specific month/date range — also bypass FY guard
_SPECIFIC_PERIOD_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.IGNORECASE),
    re.compile(r"\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b", re.IGNORECASE),
    re.compile(r"\bthis\s+month\b", re.IGNORECASE),
    re.compile(r"\bcurrent\s+month\b", re.IGNORECASE),
    re.compile(r"\blast\s+month\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b"),                        # explicit date
    re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}\b"),                            # ISO date
    re.compile(r"\bq[1-4]\b", re.IGNORECASE),                              # Q1/Q2/Q3/Q4
    re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.IGNORECASE),
]

async def _is_time_sensitive(question: str) -> bool:
    """Return True if the question touches a time-scoped financial/operational topic."""
    intent = await classify_intent(question)
    return intent in ["kpi_summary", "revenue", "receivables", "proposals", "projects", "resources", "recoverability", "staff_billing"]


def _has_explicit_fy(question: str) -> bool:
    """Return True if the question already specifies an FY."""
    return any(p.search(question) for p in _EXPLICIT_FY_PATTERNS)


def _has_specific_period(question: str) -> bool:
    """Return True if the question mentions a specific month, quarter or date."""
    return any(p.search(question) for p in _SPECIFIC_PERIOD_PATTERNS)


def _extract_fy_from_history(history: Optional[list[dict]]) -> Optional[str]:
    """
    Scan conversation history (newest-first) for a previously stated FY.
    Looks in both user messages and assistant messages that confirm filters.
    Returns a string like '2024-2025' or None.
    """
    if not history:
        return None

    # Patterns to extract year ranges from prior messages
    fy_value_re = re.compile(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b")
    fy_word_re = re.compile(
        r"\b(?:fy|financial\s+year|fiscal\s+year)[:\s]+(?:fy)?(20\d{2}[-/]20\d{2}|'?\d{2}[-/]'?\d{2})\b",
        re.IGNORECASE,
    )
    this_fy_re = re.compile(
        r"\b(this|current)\s+(?:fy|fiscal\s+year|financial\s+year)\b",
        re.IGNORECASE,
    )
    last_fy_re = re.compile(
        r"\b(last|previous)\s+(?:fy|fiscal\s+year|financial\s+year)\b",
        re.IGNORECASE,
    )

    for msg in reversed(history):
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if not content:
            continue
        content = str(content)

        # "this FY" / "current FY" → resolve to current FY string
        if this_fy_re.search(content):
            return _current_fy()

        # "last FY" → resolve to previous FY
        if last_fy_re.search(content):
            now = datetime.utcnow()
            base = now.year - 1 if now.month >= 10 else now.year - 2
            return f"{base}-{base + 1}"

        # Explicit "2024-2025" style
        m = fy_value_re.search(content)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

        # "FY 2024-2025" style
        m = fy_word_re.search(content)
        if m:
            return m.group(1)

    return None


async def _identify_topic(question: str) -> str:
    """Return a short human-readable topic label for the clarification message.
    Uses centralized intelligent intent classification instead of hardcoded keywords.
    """
    intent = await classify_intent(question)
    intent_map = {
        "kpi_summary": "KPI report",
        "revenue": "revenue",
        "receivables": "receivables",
        "proposals": "proposals",
        "projects": "projects",
        "resources": "resource utilization",
        "recoverability": "project recoverability",
        "staff_billing": "staff billing",
        "other": "this report"
    }
    return intent_map.get(intent, "this report")


async def needs_fy_clarification(question: str, history: Optional[list[dict]] = None) -> tuple[bool, Optional[str]]:
    """
    Central guard function.

    Returns:
        (needs_clarification: bool, resolved_fy: Optional[str])

    If resolved_fy is not None, the caller should inject that FY into the query
    instead of asking the user again.
    If needs_clarification is True, the caller should return a clarification response.
    """
    if not await _is_time_sensitive(question):
        return False, None
        
    # If the question was generated by a picker (has explicit \n filters), bypass the guard
    # to avoid an infinite loop of showing the picker again.
    if "\nservice line:" in question.lower() or "\nproject partner:" in question.lower() or "\nemployee name:" in question.lower() or "\ncustomer name:" in question.lower() or "\ndate range:" in question.lower():
        return False, None
        
    q_lower = question.lower()
    if any(k in q_lower for k in ["staff billing", "staff cost", "billing for employee", "employee billing", "partner billing"]):
        if not _has_specific_period(question):
            return True, "staff_billing_picker"
        return False, None
        
    if _has_explicit_fy(question) or _has_specific_period(question):
        return False, None

    # Check history for a previously stated FY
    fy_from_history = _extract_fy_from_history(history)
    if fy_from_history:
        # FY was already established in prior turns — inject silently
        return False, fy_from_history

    # Needs clarification
    return True, None


async def build_fy_clarification_response(question: str, intent: str = "fy_clarification") -> dict:
    """
    Build the standard clarification response dict returned to the frontend
    when FY is missing or a specific picker is requested.

    Includes a structured `fy_picker` payload so the frontend can render
    an interactive FY + month selector instead of just markdown text.
    """
    topic = await _identify_topic(question)
    options = _fy_options()
    current = _current_fy()

    if intent == "staff_billing_picker":
        answer = (
            f"👥 **Staff Billing Filters**\n\n"
            f"Please select your desired filters to generate the Staff Billing report."
        )
    else:
        answer = (
            f"📅 **Which Financial Year are you referring to?**\n\n"
            f"Please select a Financial Year and, optionally, one or more specific months "
            f"for your **{topic}** query.\n\n"
            f"Our Financial Year runs **October → September**."
        )

    suggested = [
        f"{question.strip()} for FY {options[0]}",
        f"{question.strip()} for FY {options[1]}",
        f"{question.strip()} for FY {options[2]}",
    ]

    # Determine if the question already contains a specific entity (customer, employee, project).
    # If so, there's no need to show the Service Line dropdown — the entity already implies the scope.
    from .query_parser import _extract_person_name
    _has_entity = bool(_extract_person_name(question))

    # Structured data for the FyPickerPanel component
    fy_picker = {
        "original_question": question.strip(),
        "topic": topic,
        "current_fy": current,
        "fy_options": options,        # list of 3 FY strings: prev, current, next
        "show_service_line": not _has_entity,  # hide SL dropdown when entity is already known
    }

    return {
        "answer": answer,
        "chart_data": None,
        "navigate_to": None,
        "navigation_links": [],
        "suggested_questions": suggested,
        "export_data": None,
        "auto_expand": False,
        # Signal to frontend what picker to show
        "report_intent": intent,
        "kpi_payload": None,
        # Extra payload used by FyPickerPanel widget
        "fy_picker": fy_picker,
    }


def inject_fy_into_question(question: str, fy: str) -> str:
    """
    Append the resolved FY to the question so downstream handlers
    can parse it normally (e.g. '2024-2025' triggers _extract_date_range).
    """
    return f"{question.strip()} for FY {fy}"
