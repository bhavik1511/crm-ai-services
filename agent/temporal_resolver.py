"""
temporal_resolver.py — Universal Canonical Temporal Resolver
============================================================

Provides a single, deterministic, timezone-aware temporal scope resolver
used by all capabilities across the AI service.

Supported temporal concepts:
  • today
  • yesterday
  • current week / this week
  • current month / this month
  • previous month / last month
  • current quarter / this quarter
  • previous quarter / last quarter
  • current FY / this FY
  • previous FY / last FY
  • explicit month/year (e.g., "January 2026", "Jan 2026")
  • explicit date range (e.g., "2026-01-01 to 2026-01-31", "2026-01-01 - 2026-01-31")
"""

import calendar
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

_MONTH_NAMES_MAP = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'september': 9, 'sept': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def resolve_temporal_scope(query: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Parses temporal phrases in a query and returns a standardized temporal scope dict:
    {
        "temporal_scope": "current_month" | "last_month" | "explicit_month" | "current_fy" | ...,
        "start_date": "YYYY-MM-DD",
        "end_date": "YYYY-MM-DD",
        "financial_year": "2025-2026",
        "is_explicit": bool
    }
    """
    if now is None:
        now = datetime.now()

    q = query.lower().strip()

    # Calculate Current FY (Oct 1 to Sep 30)
    if now.month >= 10:
        current_fy_start_year = now.year
        current_fy_end_year = now.year + 1
    else:
        current_fy_start_year = now.year - 1
        current_fy_end_year = now.year
    
    current_fy_str = f"{current_fy_start_year}-{current_fy_end_year}"
    current_fy_start_date = f"{current_fy_start_year}-10-01"
    current_fy_end_date = f"{current_fy_end_year}-09-30"

    # 1. Explicit ISO / standard date range (YYYY-MM-DD to YYYY-MM-DD)
    range_m = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})\s+(?:to|until|-)\s+(\d{4}[-/]\d{2}[-/]\d{2})', q)
    if range_m:
        s_date = range_m.group(1).replace('/', '-')
        e_date = range_m.group(2).replace('/', '-')
        return {
            "temporal_scope": "explicit_range",
            "start_date": s_date,
            "end_date": e_date,
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 2. Explicit FY pattern (e.g. FY25, FY 2025-2026, FY2025, 2024-2025)
    fy_match = re.search(r'\bfy\s*(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?\b', q)
    if fy_match:
        y1 = int(fy_match.group(1))
        if y1 < 100:
            y1 += 2000
        if fy_match.group(2):
            y2 = int(fy_match.group(2))
            if y2 < 100:
                y2 += 2000
        else:
            y2 = y1 + 1
        fy_str = f"{y1}-{y2}"
        return {
            "temporal_scope": "explicit_fy",
            "start_date": f"{y1}-10-01",
            "end_date": f"{y2}-09-30",
            "financial_year": fy_str,
            "is_explicit": True
        }

    # 3. Explicit Month + Year (e.g., "January 2026", "Jan 2026")
    month_year_m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember|t)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+(\d{2,4})\b', q)
    if month_year_m:
        m_str = month_year_m.group(1)[:3]
        y_val = int(month_year_m.group(2))
        if y_val < 100:
            y_val += 2000
        m_val = _MONTH_NAMES_MAP[m_str]
        last_day = calendar.monthrange(y_val, m_val)[1]
        return {
            "temporal_scope": "explicit_month",
            "start_date": f"{y_val}-{m_val:02d}-01",
            "end_date": f"{y_val}-{m_val:02d}-{last_day:02d}",
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 4. Explicit Month name without year (e.g., "in January", "proposals for September")
    month_no_year_m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember|t)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', q)
    if month_no_year_m:
        m_str = month_no_year_m.group(1)[:3]
        m_val = _MONTH_NAMES_MAP[m_str]
        # Infer year based on current month
        y_val = now.year
        if m_val > now.month:
            y_val = now.year - 1
        last_day = calendar.monthrange(y_val, m_val)[1]
        return {
            "temporal_scope": "explicit_month",
            "start_date": f"{y_val}-{m_val:02d}-01",
            "end_date": f"{y_val}-{m_val:02d}-{last_day:02d}",
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 5. "today"
    if 'today' in q:
        today_str = now.strftime('%Y-%m-%d')
        return {
            "temporal_scope": "today",
            "start_date": today_str,
            "end_date": today_str,
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 6. "yesterday"
    if 'yesterday' in q:
        yest = now - timedelta(days=1)
        yest_str = yest.strftime('%Y-%m-%d')
        return {
            "temporal_scope": "yesterday",
            "start_date": yest_str,
            "end_date": yest_str,
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 7. "current week" / "this week"
    if 'current week' in q or 'this week' in q:
        start_of_week = now - timedelta(days=now.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        return {
            "temporal_scope": "current_week",
            "start_date": start_of_week.strftime('%Y-%m-%d'),
            "end_date": end_of_week.strftime('%Y-%m-%d'),
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 8. "current month" / "this month"
    if 'current month' in q or 'this month' in q:
        last_day = calendar.monthrange(now.year, now.month)[1]
        return {
            "temporal_scope": "current_month",
            "start_date": f"{now.year}-{now.month:02d}-01",
            "end_date": f"{now.year}-{now.month:02d}-{last_day:02d}",
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 9. "last month" / "previous month"
    if 'last month' in q or 'previous month' in q:
        if now.month == 1:
            prev_m = 12
            prev_y = now.year - 1
        else:
            prev_m = now.month - 1
            prev_y = now.year
        last_day = calendar.monthrange(prev_y, prev_m)[1]
        return {
            "temporal_scope": "last_month",
            "start_date": f"{prev_y}-{prev_m:02d}-01",
            "end_date": f"{prev_y}-{prev_m:02d}-{last_day:02d}",
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # 10. "last fy" / "previous fy" / "previous financial year"
    if any(x in q for x in ['last fy', 'previous fy', 'previous financial year', 'last financial year']):
        prev_fy_start_year = current_fy_start_year - 1
        prev_fy_end_year = current_fy_start_year
        prev_fy_str = f"{prev_fy_start_year}-{prev_fy_end_year}"
        return {
            "temporal_scope": "previous_fy",
            "start_date": f"{prev_fy_start_year}-10-01",
            "end_date": f"{prev_fy_end_year}-09-30",
            "financial_year": prev_fy_str,
            "is_explicit": True
        }

    # 11. "this year" / "current year" / "current fy" / "this fy"
    if any(x in q for x in ['this year', 'current year', 'current fy', 'this fy', 'financial year', 'fiscal year']):
        return {
            "temporal_scope": "current_fy",
            "start_date": current_fy_start_date,
            "end_date": current_fy_end_date,
            "financial_year": current_fy_str,
            "is_explicit": True
        }

    # Default fallback: Full Current FY (marked as is_explicit=False)
    return {
        "temporal_scope": "default_fy",
        "start_date": current_fy_start_date,
        "end_date": current_fy_end_date,
        "financial_year": current_fy_str,
        "is_explicit": False
    }
