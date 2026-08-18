"""
Memory Manager — orchestrates the 3-tier cache system.
Sits between API routes and the LangGraph agent.
Prevents repeat SQL queries by checking Redis → Vector → Agent.

Redis key:  qa:{sha256(question.strip().lower())}
Redis TTL:  3600 seconds (1 hour)
"""

import os
import json
import time
import hashlib
import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from db.database_redis import get_redis
from db.database import get_db_engine
from sqlalchemy import text
from rag import vector_store_v2 as vector_store
from agent.agent import ask_question
from db.database_mongo import get_vector_cache_collection, get_chat_history_collection

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

REDIS_QA_TTL = 3600  # 1 hour
KPI_SUMMARY_ROUTE = "/projects/reports/kpi-summary-report"
KPI_FILTER_SETUP_PROMPT = """**📋 KPI SUMMARY REPORT — Filter Setup**
<hr />"""


def _is_kpi_summary_query(question: str) -> bool:
    q = (question or "").lower()

    # Explicit exclusions — these must NEVER be treated as KPI summary
    _EXCLUSION_PATTERNS = [
        "resource utilization", "utilization report", "utilisation report",
        "resource report", "resource allocation", "billable hours",
        "timesheet", "staff utilization", "staff utilisation",
        "project", "projects",
    ]
    if any(pat in q for pat in _EXCLUSION_PATTERNS):
        return False

    kpi_tokens = [
        "kpi",
        "kpi summary",
        "kpi report",
        "kpi summary report",
    ]
    if any(token in q for token in kpi_tokens):
        return True

    has_report_word = "report" in q or "summary" in q
    # NOTE: "utilization" and "utilisation" intentionally removed — those belong to
    # resource utilization queries, NOT KPI summary.
    has_kpi_metric = any(token in q for token in [
        "budget vs actual", "gp performance", "gross profit", "variance",
        "secured business", "balance to achieve", "project in hand",
        "open proposals", "receivable aging", "receivable ageing",
    ])
    return has_report_word and has_kpi_metric


def _apply_kpi_overrides(response: dict, question: str) -> dict:
    if not response:
        return response
    if not _is_kpi_summary_query(question):
        return response

    response["report_intent"] = "kpi_summary"
    response["navigate_to"] = KPI_SUMMARY_ROUTE
    if not response.get("navigation_links"):
        response["navigation_links"] = [{"label": "KPI Summary Report", "url": KPI_SUMMARY_ROUTE}]
    return response


def _extract_filter_line_value(text: str, label: str) -> Optional[str]:
    if not text:
        return None
    pattern = re.compile(rf"^\s*{label}\s*(?:[:=→-]\s*|\s+)(.+?)\s*$", re.IGNORECASE)
    for line in str(text).splitlines():
        m = pattern.match(line.strip())
        if m:
            value = m.group(1).strip()
            return value or None
    return None


def _is_placeholder_filter_value(value: Optional[str]) -> bool:
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    lower = v.lower()
    # Reject template/example placeholders so instruction text is not treated as user filters.
    if "e.g." in lower or "example" in lower:
        return True
    if lower.startswith("(") and lower.endswith(")"):
        return True
    return False


def _clean_filter_value(value: Optional[str]) -> Optional[str]:
    if _is_placeholder_filter_value(value):
        return None
    return str(value).strip()


def _extract_named_filter(question: str, marker: str) -> Optional[str]:
    q = (question or "").strip()
    pattern = rf"{marker}\s+([a-zA-Z0-9&\-\s\.]+)"
    m = re.search(pattern, q, flags=re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    value = re.split(r"\b(for|from|to|in|this|current|summary|report|kpi)\b", value, flags=re.IGNORECASE)[0].strip()
    return value or None


def _extract_kpi_filters_from_text(text: str) -> dict:
    q = text or ""
    filters = {
        "service_line": _clean_filter_value(_extract_filter_line_value(q, r"Service\s*Line")),
        "department": _clean_filter_value(_extract_filter_line_value(q, r"Department")),
        "employee_name": _clean_filter_value(_extract_filter_line_value(q, r"Employee\s*Name")),
        "financial_year": _clean_filter_value(_extract_filter_line_value(q, r"Financial\s*Year")),
        "date_range": _clean_filter_value(_extract_filter_line_value(q, r"Date\s*Range")),
    }

    # Natural-language fallback: "for Damcy", "of Damcy Dudeja", "for employee Damcy"
    # Only apply if no label-based employee extraction worked.
    if not filters["employee_name"]:
        # Match "for [Name]", "of [Name]", "for employee [Name]" patterns (case insensitive)
        nl_emp = re.search(
            r"\bfor\s+(?:employee\s+)?([a-zA-Z]+(?:\s+[a-zA-Z]+)*)\b"
            r"|\bof\s+(?:employee\s+)?([a-zA-Z]+(?:\s+[a-zA-Z]+)*)\b",
            q,
            re.IGNORECASE
        )
        if nl_emp:
            raw = (nl_emp.group(1) or nl_emp.group(2) or "").strip()
            # Don't treat generic KPI keywords as names
            if raw.lower() not in ("all", "kpi", "report", "summary", "the", "a"):
                filters["employee_name"] = raw

    customer = _clean_filter_value(_extract_filter_line_value(q, r"Customer"))
    if not customer:
        customer = _clean_filter_value(_extract_filter_line_value(q, r"Customer\s*Name"))
    if not customer:
        customer = _extract_named_filter(q, r"customer") or None
    filters["customer"] = customer

    fy_match = re.search(r"\b(20\d{2})\s*[\-–/]\s*(20\d{2})\b", q)
    if not filters["financial_year"] and fy_match:
        filters["financial_year"] = f"{fy_match.group(1)}-{fy_match.group(2)}"

    date_match = re.search(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*(?:to|→|->|-)\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})", q, re.IGNORECASE)
    if not filters["date_range"] and date_match:
        s = date_match.group(1).replace("/", "-")
        e = date_match.group(2).replace("/", "-")
        filters["date_range"] = f"{s} → {e}"

    return filters


def _extract_kpi_filters_from_history(history: list[dict]) -> dict:
    merged = {
        "service_line": None,
        "department": None,
        "employee_name": None,
        "customer": None,
        "financial_year": None,
        "date_range": None,
    }
    # Entity keys where "All" means "unspecified" and should be ignored in favour of specific values deeper in history
    entity_keys = {"service_line", "department", "employee_name", "customer"}
    for msg in reversed(history or []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            continue
        candidate = _extract_kpi_filters_from_text(content)
        for key, value in candidate.items():
            if merged.get(key) in (None, "") and value not in (None, ""):
                # For entity filters, skip generic "All" — keep scanning for a real value
                if key in entity_keys and str(value).lower() == "all":
                    continue
                merged[key] = value
        # Only stop early if all fields (including entity ones) have non-All values
        if all(merged.get(k) not in (None, "") for k in merged):
            break
    return merged


def _merge_kpi_filters(question_filters: dict, history_filters: dict) -> dict:
    merged = dict(history_filters or {})
    for key, value in (question_filters or {}).items():
        if value not in (None, ""):
            # If the widget sends "All", don't overwrite a specific filter from history
            if str(value).lower() == "all" and merged.get(key) not in (None, "", "All", "all"):
                continue
            merged[key] = value
    return merged


def _kpi_filters_complete(filters: dict) -> bool:
    required = ["service_line", "department", "employee_name", "financial_year", "date_range"]
    for key in required:
        value = (filters or {}).get(key)
        if _is_placeholder_filter_value(value):
            return False
    return True


def _is_kpi_filter_submission_text(question: str) -> bool:
    """Treat only explicit filter payload messages as filter submission."""
    q = question or ""
    # Require explicit label/value structure (with separators), not just keyword presence.
    required_patterns = [
        r"service\s*line\s*[:=\-→]",
        r"department\s*[:=\-→]",
        r"employee\s*name\s*[:=\-→]",
        r"financial\s*year\s*[:=\-→]",
        r"date\s*range\s*[:=\-→]",
    ]
    if not all(re.search(pattern, q, re.IGNORECASE) for pattern in required_patterns):
        return False
    return _kpi_filters_complete(_extract_kpi_filters_from_text(q))


def _history_has_kpi_filter_confirmation(history: list[dict]) -> bool:
    for msg in reversed(history or []):
        content = (msg or {}).get("content", "")
        if "✅ Confirmed Filters:" in content:
            return True
    return False


def _is_kpi_confirmation_text(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return bool(re.search(r"\b(confirm|confirmed|proceed|go ahead|continue)\b", q))


def _format_kpi_confirmed_filters(filters: dict) -> str:
    return "\n".join([
        "✅ Confirmed Filters:",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "| Filter         | Selected Value         |",
        "|----------------|------------------------|",
        f"| Service Line   | {filters.get('service_line', 'All')} |",
        f"| Department     | {filters.get('department', 'All')} |",
        f"| Employee Name  | {filters.get('employee_name', 'All')} |",
        f"| Customer Name  | {filters.get('customer', 'All')} |",
        f"| Financial Year | {filters.get('financial_year', 'All')} |",
        f"| Date Range     | {filters.get('date_range', 'All')} |",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "Reply with 'Confirm' to generate the KPI report.",
    ])


def _is_kpi_filter_context(question: str, history: list[dict]) -> bool:
    """Return True ONLY when the current question is actively part of a KPI filter flow.

    BUG FIX: Previously this scanned the ENTIRE history, so one KPI conversation
    would poison all subsequent questions in the same session — unrelated questions
    like 'show overdue invoices' would return the KPI filter prompt forever.

    New logic:
      1. If the current question IS a KPI query → True.
      2. If the LAST assistant message was the KPI filter setup AND the current question
         looks like a filter response (has filter labels or 'confirm') → True.
      3. Otherwise → False (do NOT contaminate the session).
    """
    if _is_kpi_summary_query(question):
        return True

    # Only check the very last assistant message — not the entire history.
    # This prevents session contamination from previous KPI conversations.
    last_assistant_content = ""
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            last_assistant_content = msg.get("content", "")
            break

    last_was_kpi_prompt = (
        "KPI SUMMARY REPORT — Filter Setup" in last_assistant_content
        or "\u2705 Confirmed Filters:" in last_assistant_content
    )
    if not last_was_kpi_prompt:
        return False

    # Last message was KPI prompt — only continue if current question looks like
    # a filter response, not an unrelated question about invoices/customers/etc.
    q = (question or "").strip().lower()
    looks_like_filter_response = (
        _is_kpi_filter_submission_text(question)
        or _is_kpi_confirmation_text(question)
        or any(kw in q for kw in [
            "service line:", "department:", "employee name:",
            "financial year:", "date range:", "all service",
        ])
    )
    return looks_like_filter_response


def _deterministic_identity_answer(question: str, user_context: dict) -> Optional[str]:
    """
    Return a deterministic RBAC answer for identity/access questions.
    This bypasses cache + LLM to avoid stale or hallucinated role-rights responses.
    EXTREMELY BROAD pattern matching to catch all variations.
    """
    q = (question or "").strip().lower()
    role_name = (user_context.get("role") or user_context.get("role_name") or "Unknown").strip()
    department = (user_context.get("department") or "Unknown").strip()

    if not q:
        return None

    from config.role_tier_config import get_tier_for_role, get_tier_label, _build_tier_access_rules

    tier = get_tier_for_role(role_name)
    tier_label = get_tier_label(tier)

    # Tightly scoped patterns to match ONLY role/rights queries, not "can i see the report"
    is_role_question = bool(
        re.search(
            r"^(what\s+is|who\s+am\s+i|tell\s+me)\s+(my\s+)?(role|designation|position|title|job|tier)$",
            q,
            re.IGNORECASE
        )
    ) or q in ["what is my role", "what's my role", "my role", "who am i"]

    is_rights_question = bool(
        re.search(
            r"^(what\s+are\s+my\s+|what\s+|my\s+)(rights|access\s+rights|permissions|privileges)$",
            q,
            re.IGNORECASE
        )
    ) or q in [
        "what can i access",
        "what are my permissions",
        "my access",
        "my permissions",
        "my permission",
        "access level"
    ]

    # Log the matching (debug)
    if is_role_question or is_rights_question:
        logger.info(
            f"[Memory] DETERMINISTIC MATCH: role_q={is_role_question}, rights_q={is_rights_question}, "
            f"resolved_role={role_name}, tier={tier}, q_excerpt='{q[:60]}...'"
        )

    if is_role_question and not is_rights_question:
        ans = (
            f"Your role is {role_name} (Tier {tier} — {tier_label}) in the {department} department."
        )
        logger.info(f"[Memory] Returning deterministic role answer for {role_name}")
        return ans

    if is_rights_question:
        tier_rules, _ = _build_tier_access_rules(tier, role_name)
        ans = (
            f"Your role is {role_name} (Tier {tier} — {tier_label}) in the {department} department.\n\n"
            f"{tier_rules}"
        )
        logger.info(f"[Memory] Returning deterministic rights answer for {role_name}")
        return ans

    return None


# ---------------------------------------------------------------------------
# Helper: extract SQL from agent result tuple
# ---------------------------------------------------------------------------
def _extract_sql_from_result(agent_result: tuple) -> Optional[str]:
    """
    agent.ask_question returns a 7-tuple:
    (answer, chart_data, navigate_to, navigation_links, export_data, auto_expand, suggested_questions)
    There is no dedicated sql field, so we return None unless we add one later.
    """
    # The agent currently doesn't return sql_executed separately.
    # If it does in the future, extract from index 2 (navigate_to repurposed? — no).
    # For now, return None; the sql_executed is tracked internally by the agent.
    return None


def _current_fiscal_window() -> tuple[str, str]:
    now = datetime.utcnow()
    if now.month >= 10:
        return f"{now.year}-10-01", f"{now.year + 1}-09-30 23:59:59"
    return f"{now.year - 1}-10-01", f"{now.year}-09-30 23:59:59"


def _deterministic_high_value_answer(question: str) -> Optional[dict]:
    """Return a database-backed answer for high-value proposal questions.

    This bypasses cache/LLM behavior so the chatbot returns the same live CRM
    rows the dashboard shows instead of a synthesized summary.
    """
    q = (question or "").strip().lower()
    if not q:
        return None

    high_value_trigger = bool(
        re.search(r"high[-\s]?value", q)
        or re.search(r"highest[-\s]?value", q)
        or "top 5" in q
        or "largest proposal" in q
        or "biggest proposal" in q
    )
    if not high_value_trigger or "proposal" not in q:
        return None

    start_date, end_date = _current_fiscal_window()
    query = text(
        """
        SELECT
            p.id AS proposal_id,
            COALESCE(c.customer_name, co.cd_company_name, 'N/A') AS client_name,
            p.total_costs AS budget_value,
            DATEDIFF(CURDATE(), p.created_at) AS age_in_days
        FROM proposal p
        LEFT JOIN customers c ON p.client_id = c.id
        LEFT JOIN contacts co ON p.contact_id = co.id
        WHERE p.is_active = 1
          AND p.created_at BETWEEN :start_date AND :end_date
        ORDER BY p.total_costs DESC, p.created_at DESC
        LIMIT 5
        """
    )

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                query,
                {"start_date": start_date, "end_date": end_date},
            ).fetchall()

        title = "High Value Proposals by Customer" if "customer" in q else "High Value Proposals"
        answer_lines = [
            f"### {title}",
            "",
            "| Client | Proposal Code | Value (BHD) | Ageing Days |",
            "|---|---:|---:|---:|",
        ]

        for row in rows:
            data = row._mapping
            client_name = data.get("client_name") or "N/A"
            proposal_id = data.get("proposal_id") or "N/A"
            budget_value = float(data.get("budget_value") or 0)
            age_in_days = data.get("age_in_days") if data.get("age_in_days") is not None else 0
            answer_lines.append(
                f"| {client_name} | {proposal_id} | {budget_value:,.2f} | {age_in_days} |"
            )

        return {
            "answer": "\n".join(answer_lines).strip(),
            "chart_data": None,
            "navigate_to": "/proposal",
            "navigation_links": [{"label": "Proposals", "url": "/proposal"}],
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": ["Show open proposals", "Show proposal status breakdown"],
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": 0,
        }
    except Exception as e:
        return {
            "answer": f"Sorry, I could not retrieve the high value proposals from the CRM database: {str(e)}",
            "chart_data": None,
            "navigate_to": None,
            "navigation_links": None,
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": None,
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": 0,
        }


def _deterministic_top_customers_by_revenue(question: str) -> Optional[dict]:
    """Return a live SQL answer for 'top N customers by revenue' queries.

    Bypasses cache and LLM entirely — joins customers + invoice tables directly
    so the chatbot always returns real customer names, never service-line data.
    """
    q = (question or "").strip().lower()
    if not q:
        return None

    # Match patterns like: "top 5 customers by revenue", "show top customers",
    # "customers with highest revenue", "who are the top clients by billing"
    has_customer = any(kw in q for kw in ["customer", "customers", "client", "clients"])
    has_revenue_metric = any(kw in q for kw in [
        "revenue", "billing", "invoiced", "billed", "invoice amount",
        "highest revenue", "most revenue", "top revenue",
    ])
    if not (has_customer and has_revenue_metric):
        return None

    # Extract N (how many top customers to show)
    n = 5
    m = re.search(r"\btop\s+(\d+)\b", q)
    if m:
        n = min(int(m.group(1)), 20)  # cap at 20

    # Extract date range if user selected a timeframe/FY
    start_date, end_date = None, None
    try:
        from agent.query_parser import _extract_date_range
        s, e, date_specified = _extract_date_range(question)
        if date_specified and s and e:
            start_date, end_date = f"{s} 00:00:00", f"{e} 23:59:59"
    except Exception:
        pass

    date_filter = "AND i.created_at BETWEEN :start_date AND :end_date" if start_date else ""
    params = {"n": n}
    if start_date:
        params["start_date"] = start_date
        params["end_date"] = end_date

    query = text(
        f"""
        SELECT
            c.customer_name,
            ROUND(SUM(i.total_amt_ex_vat), 2) AS total_revenue,
            COUNT(DISTINCT i.id) AS invoice_count
        FROM customers c
        JOIN invoice i ON i.client_name_id = c.id
        WHERE i.is_active = 1 {date_filter}
        GROUP BY c.id, c.customer_name
        ORDER BY total_revenue DESC
        LIMIT :n
        """
    )

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            # Try alternate join column
            query2 = text(
                f"""
                SELECT
                    c.customer_name,
                    ROUND(SUM(i.total_amt_ex_vat), 2) AS total_revenue,
                    COUNT(DISTINCT i.id) AS invoice_count
                FROM customers c
                JOIN invoice i ON i.client_id = c.id
                WHERE i.is_active = 1 {date_filter}
                GROUP BY c.id, c.customer_name
                ORDER BY total_revenue DESC
                LIMIT :n
                """
            )
            with engine.connect() as conn:
                rows = conn.execute(query2, params).fetchall()

        # If date filtering produced no rows (e.g. historical data outside selected FY), fallback to all-time query
        if not rows and start_date:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT c.customer_name, ROUND(SUM(i.total_amt_ex_vat), 2) AS total_revenue, COUNT(DISTINCT i.id) AS invoice_count
                        FROM customers c JOIN invoice i ON i.client_name_id = c.id WHERE i.is_active = 1 GROUP BY c.id, c.customer_name ORDER BY total_revenue DESC LIMIT :n
                    """),
                    {"n": n}
                ).fetchall()

        if not rows:
            return {
                "answer": "No customer revenue data found in the database.",
                "chart_data": None,
                "navigate_to": "/billing",
                "navigation_links": [{"label": "Billing", "url": "/billing"}],
                "export_data": None,
                "auto_expand": False,
                "suggested_questions": ["Show total revenue by service line", "Show overdue invoices"],
                "sql_executed": None,
                "cache_tier": "fresh",
                "was_cached": False,
                "latency_ms": 0,
            }

        answer_lines = [
            f"### 🏆 Top {n} Customers by Revenue",
            "",
            "| # | Customer Name | Revenue (BHD) | Invoices |",
            "|---|---|---:|---:|",
        ]
        for i, row in enumerate(rows, 1):
            data = row._mapping
            name = data.get("customer_name") or "N/A"
            rev = float(data.get("total_revenue") or 0)
            inv_count = int(data.get("invoice_count") or 0)
            answer_lines.append(f"| {i} | {name} | BHD {rev:,.2f} | {inv_count} |")

        return {
            "answer": "\n".join(answer_lines).strip(),
            "chart_data": None,
            "navigate_to": "/billing",
            "navigation_links": [
                {"label": "Billing Overview", "url": "/billing"},
                {"label": "Invoice Summary Report", "url": "/billing/reports/invoice-summary-report"},
            ],
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": [
                "What is the total revenue this fiscal year?",
                "Show top customers by number of invoices",
                "What are the overdue invoices?",
            ],
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": 0,
        }

    except Exception as e:
        logger.error(f"[Deterministic] Top customers by revenue failed: {e}")
        return None  # Fall through to normal LLM path


# ---------------------------------------------------------------------------
# Live-data detection — these questions must NEVER be served from cache
# ---------------------------------------------------------------------------
_LIVE_DATA_KEYWORDS = {
    # KPI reports & insights
    "kpi", "kpi summary", "kpi report", "kpi summary report", "report", "summary",
    # Financial metrics
    "revenue", "billing", "invoice", "invoices", "receivable", "receivables",
    "outstanding", "aging", "ageing", "overdue", "collection", "payment",
    "paid", "unpaid", "remaining", "amount", "vat", "total", "credit note",
    # Pipeline / proposals
    "lead", "leads", "proposal", "proposals", "pipeline", "win rate",
    "engagement", "job estimation",
    # Projects / tasks
    "project", "projects", "task", "tasks", "milestone", "timesheet",
    "utilization", "utilisation", "hours", "billable",
    # HR / employees
    "employee", "employees", "salary", "leave", "attendance", "payroll",
    "headcount", "staff", "resource", "allocation",
    # Temporal signals
    "today", "this month", "this week", "this year", "current", "latest",
    "recent", "last month", "last week", "yesterday",
    # Specific entity lookups — always need live data
    "customer", "client", "survey", "cash advance", "travel request", "leave request",
    "loan", "advance", "holiday", "announcement",
}


def _needs_live_data(question: str) -> bool:
    """
    Returns True when the question requires a fresh DB query.
    These questions MUST bypass all caches to avoid stale/hallucinated answers.
    Only purely navigational or identity questions are cacheable.
    """
    q = (question or "").lower()
    return any(kw in q for kw in _LIVE_DATA_KEYWORDS)


# ---------------------------------------------------------------------------
# 3-Tier resolve answer
# ---------------------------------------------------------------------------
async def resolve_answer(
    question: str,
    session_id: str,
    user_context: dict,
    history: list[dict],
) -> dict:
    """
    3-tier cache resolution:
      Tier 1 — Redis exact match           (~1ms)
      Tier 2 — MongoDB vector similarity    (~30-50ms)
      Tier 3 — Fresh LangGraph agent        (~1-3s)

    Returns:
    {
        "answer": str,
        "chart_data": dict | None,
        "sql_executed": str | None,
        "cache_tier": "redis" | "vector" | "fresh",
        "was_cached": bool,
        "latency_ms": int
    }
    """
    start = time.time()

    # KPI Filter-First Gate (applies to both cached and fresh paths)
    if _is_kpi_summary_query(question) and not _is_kpi_filter_submission_text(question):
        quick_filters = _extract_kpi_filters_from_text(question)
        named_employee = quick_filters.get("employee_name")  # e.g. "Shashank Arya"
        q_low = question.lower()
        has_direct_action = any(w in q_low for w in ["generate", "download", "show", "run", "get", "create", "export"])

        if has_direct_action or (named_employee and named_employee.lower() not in ("all", "")):
            # Direct report generation requested — execute _deterministic_kpi_response immediately with complete defaults
            from main import _deterministic_kpi_response
            question_filters = _extract_kpi_filters_from_text(question)
            history_filters = _extract_kpi_filters_from_history(history)
            merged_filters = _merge_kpi_filters(question_filters, history_filters)
            if named_employee and not merged_filters.get("employee_name"):
                merged_filters["employee_name"] = named_employee
            if not merged_filters.get("financial_year"):
                merged_filters["financial_year"] = "2025-2026"
            if not merged_filters.get("date_range"):
                merged_filters["date_range"] = "01-10-2025 to 30-09-2026"
            if not merged_filters.get("service_line"):
                merged_filters["service_line"] = "All"
            if not merged_filters.get("department"):
                merged_filters["department"] = "All"

            auth_token = user_context.get("jwt_token") if isinstance(user_context, dict) else None
            res = await _deterministic_kpi_response(history, question, user_context, auth_token)
            if res:
                res["cache_tier"] = "fresh"
                res["was_cached"] = False
                res["latency_ms"] = int((time.time() - start) * 1000)
                return _apply_kpi_overrides(res, question)

        # No specific action or employee -> show the full filter panel
        return {
            "answer": KPI_FILTER_SETUP_PROMPT,
            "chart_data": None,
            "navigate_to": KPI_SUMMARY_ROUTE,
            "navigation_links": [{"label": "KPI Summary Report", "url": KPI_SUMMARY_ROUTE}],
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": [
                "Service Line: All",
                "Department: All",
                "Employee Name: All",
                "Financial Year: 2025-2026",
                "Date Range: 01-10-2025 to 30-09-2026",
            ],
            "report_intent": "kpi_summary",
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": int((time.time() - start) * 1000),
        }

    if _is_kpi_filter_context(question, history):
        question_filters = _extract_kpi_filters_from_text(question)
        history_filters = _extract_kpi_filters_from_history(history)
        merged_filters = _merge_kpi_filters(question_filters, history_filters)

        if not _kpi_filters_complete(merged_filters):
            return {
                "answer": KPI_FILTER_SETUP_PROMPT,
                "chart_data": None,
                "navigate_to": KPI_SUMMARY_ROUTE,
                "navigation_links": [{"label": "KPI Summary Report", "url": KPI_SUMMARY_ROUTE}],
                "export_data": None,
                "auto_expand": False,
                "suggested_questions": [
                    "Service Line: All",
                    "Department: All",
                    "Employee Name: All",
                    "Financial Year: 2025-2026",
                    "Date Range: 01-10-2025 to 30-09-2026",
                ],
                "report_intent": "kpi_summary",
                "sql_executed": None,
                "cache_tier": "fresh",
                "was_cached": False,
                "latency_ms": int((time.time() - start) * 1000),
            }

    deterministic_high_value = _deterministic_high_value_answer(question)
    if deterministic_high_value:
        deterministic_high_value["latency_ms"] = int((time.time() - start) * 1000)
        return deterministic_high_value

    # ─── DETERMINISTIC: Top customers by revenue ───────────────────────────
    # Bypass cache entirely — these questions need live SQL data from the customers table.
    _customer_revenue_answer = _deterministic_top_customers_by_revenue(question)
    if _customer_revenue_answer:
        _customer_revenue_answer["latency_ms"] = int((time.time() - start) * 1000)
        return _customer_revenue_answer

    # Deterministic identity/RBAC responses should never depend on cache or LLM.
    deterministic_answer = _deterministic_identity_answer(question, user_context)
    if deterministic_answer:
        return {
            "answer": deterministic_answer,
            "chart_data": None,
            "navigate_to": None,
            "navigation_links": None,
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": None,
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": int((time.time() - start) * 1000),
        }

    role = user_context.get("role", "Staff")
    employee_id = user_context.get("employee_id", 0)
    from config.role_tier_config import get_tier_for_role
    user_tier = get_tier_for_role(role)
    if user_tier >= 4 and employee_id:
        scope_key = f"{role}:{employee_id}"
    else:
        scope_key = role

    # Proactive anomaly check — once per 30 minutes per session
    anomaly_note = ""
    try:
        redis = get_redis()
        anomaly_key = f"anomaly_checked:{session_id}"
        already_checked = await redis.get(anomaly_key)
        
        if not already_checked:
            from agent.tools_new import get_anomaly_alerts
            user_id = user_context.get("user_id", 0) or 0
            role_for_anomaly = user_context.get("role_name", "Staff")
            alerts = await get_anomaly_alerts(user_id, role_for_anomaly)
            
            high_alerts = [a for a in alerts if a.get("severity") == "high"]
            if high_alerts:
                alert = high_alerts[0]
                anomaly_note = (
                    f"\n\n---\n⚠️ **Heads up:** {alert['message']}"
                )
                if alert.get("amount"):
                    anomaly_note += f" (BHD {alert['amount']:,.3f})"
                anomaly_note += " — want me to pull the full details?"
            
            # Mark as checked for 30 minutes
            await redis.setex(anomaly_key, 1800, "1")
    except Exception as e:
        logger.warning(f"[Anomaly] Check failed (non-fatal): {e}")
        anomaly_note = ""

    from datetime import datetime
    current_month = datetime.utcnow().strftime("%Y-%m")
    
    DATA_KEYWORDS = ["revenue", "receivable", "invoice", "project", 
                     "proposal", "lead", "employee", "leave", "salary",
                     "task", "customer", "timesheet", "kpi", "budget"]
    q_norm = question.strip().lower()
    is_data_question = any(kw in q_norm for kw in DATA_KEYWORDS)
    
    if is_data_question:
        cache_key_input = f"{q_norm}:{scope_key}:{current_month}"
        vector_scope_key = f"{scope_key}:{current_month}"
    else:
        cache_key_input = f"{q_norm}:{scope_key}"
        vector_scope_key = scope_key
        
    redis_key = f"qa:{hashlib.sha256(cache_key_input.encode()).hexdigest()}"

    KNOWLEDGE_KEYWORDS = ["formula", "how does", "what is the rule", 
                          "how is calculated", "what formula", "gosi rule",
                          "leave policy", "how to", "what does"]
    is_knowledge = any(kw in q_norm for kw in KNOWLEDGE_KEYWORDS)
    
    ttl = 86400 if is_knowledge else 300

    # ── LIVE DATA GATE ────────────────────────────────────────────────────────
    # Questions about real DB data (financials, employees, projects, etc.) must
    # NEVER be served from cache — always go straight to the agent for a fresh
    # DB query. Only static questions (navigation, greetings, role info) are cacheable.
    if _needs_live_data(question):
        logger.info(f"[Memory] Live-data gate: bypassing ALL cache tiers for: {question[:60]}")
        # Jump directly to Tier 3
        try:
            agent_result = await ask_question(history, user_context)
        except Exception as e:
            logger.error(f"[Memory] Agent (live-data gate) failed: {e}")
            return _apply_kpi_overrides({
                "answer": f"Sorry, I couldn't retrieve that data right now. Please try again.",
                "chart_data": None, "navigate_to": None, "navigation_links": None,
                "export_data": None, "auto_expand": False, "suggested_questions": None,
                "report_intent": None, "sql_executed": None,
                "cache_tier": "fresh", "was_cached": False,
                "latency_ms": int((time.time() - start) * 1000),
            }, question)
        # Unpack agent result and return WITHOUT storing in cache
        if isinstance(agent_result, (list, tuple)) and len(agent_result) >= 7:
            ans, chart, nav, nav_links, exp, auto_exp, sugg = agent_result[:7]
            entity_name = agent_result[7] if len(agent_result) > 7 else None
            entity_type = agent_result[8] if len(agent_result) > 8 else None
            is_edit = agent_result[9] if len(agent_result) > 9 else False
            rep_intent = agent_result[10] if len(agent_result) > 10 else None
        else:
            ans = str(agent_result)
            chart = nav = nav_links = exp = sugg = entity_name = entity_type = rep_intent = None
            auto_exp = is_edit = False
            
        result = {
            "answer": ans or "",
            "chart_data": chart, "navigate_to": nav, "navigation_links": nav_links,
            "export_data": exp, "auto_expand": auto_exp,
            "suggested_questions": sugg, "report_intent": rep_intent,
            "entity_name": entity_name, "entity_type": entity_type,
            "is_edit_intent": is_edit, "sql_executed": None,
            "cache_tier": "fresh", "was_cached": False,
            "latency_ms": int((time.time() - start) * 1000),
        }
        
        if anomaly_note and not result.get("was_cached") and "error" not in result.get("answer", "").lower()[:50]:
            result["answer"] = result["answer"] + anomaly_note
            
        return _apply_kpi_overrides(result, question)
    # ── END LIVE DATA GATE ────────────────────────────────────────────────────

    redis = get_redis()

    # ═══════════════════════════════════════════════
    # TIER 1 — Redis exact match
    # ═══════════════════════════════════════════════
    try:
        cached = await redis.get(redis_key)
        if cached:
            data = json.loads(cached)
            if data.get("role_scope") == vector_scope_key:
                # Increment hit count and refresh TTL
                data["hit_count"] = data.get("hit_count", 0) + 1
                await redis.setex(redis_key, ttl, json.dumps(data))
                logger.info(f"[Memory] Tier 1 Redis hit for: {question[:60]}…")
                return _apply_kpi_overrides({
                    "answer": data["answer"],
                    "chart_data": data.get("chart_data"),
                    "navigate_to": data.get("navigate_to"),
                    "navigation_links": data.get("navigation_links"),
                    "export_data": data.get("export_data"),
                    "auto_expand": data.get("auto_expand", False),
                    "suggested_questions": data.get("suggested_questions"),
                    "report_intent": data.get("report_intent"),
                    "sql_executed": data.get("sql_executed"),
                    "cache_tier": "redis",
                    "was_cached": True,
                    "latency_ms": int((time.time() - start) * 1000),
                }, question)
    except Exception as e:
        logger.warning(f"[Memory] Redis Tier 1 check failed (non-fatal): {e}")

    # ═══════════════════════════════════════════════
    # TIER 1.5 — MongoDB exact match (Fallback if Redis is down)
    # ═══════════════════════════════════════════════
    try:
        col = get_vector_cache_collection()
        exact_match = await col.find_one({
            "question": question.strip().lower(),
            "role_scope": vector_scope_key
        })
        if exact_match:
            logger.info(f"[Memory] Tier 1.5 Mongo exact match hit for: {question[:60]}…")
            return _apply_kpi_overrides({
                "answer": exact_match["answer"],
                "chart_data": exact_match.get("chart_data"),
                "navigate_to": exact_match.get("navigate_to"),
                "navigation_links": exact_match.get("navigation_links"),
                "export_data": exact_match.get("export_data"),
                "auto_expand": exact_match.get("auto_expand", False),
                "suggested_questions": exact_match.get("suggested_questions"),
                "report_intent": exact_match.get("report_intent"),
                "sql_executed": exact_match.get("sql_executed"),
                "cache_tier": "mongo_exact",
                "was_cached": True,
                "latency_ms": int((time.time() - start) * 1000),
            }, question)
    except Exception as e:
        logger.warning(f"[Memory] Tier 1.5 Mongo Exact check failed: {e}")

    # ═══════════════════════════════════════════════
    # TIER 2 — MongoDB vector similarity
    # ═══════════════════════════════════════════════
    try:
        # Use scope_key to ensure strict tier RBAC separation for all users
        vector_hit = await vector_store.get_cached_answer(question, vector_scope_key)
        if vector_hit:
            # Warm Redis so next identical call is Tier 1
            payload = {
                "answer": vector_hit["answer"],
                "chart_data": vector_hit.get("chart_data"),
                "role_scope": vector_scope_key,
                "hit_count": 1,
                "timestamp": datetime.utcnow().isoformat(),
            }
            try:
                await redis.setex(redis_key, ttl, json.dumps(payload))
            except Exception as e:
                logger.warning(f"[Memory] Redis warm-up after vector hit failed: {e}")

            logger.info(f"[Memory] Tier 2 Vector hit (score={vector_hit.get('score', '?')})")
            return _apply_kpi_overrides({
                "answer": vector_hit["answer"],
                "chart_data": vector_hit.get("chart_data"),
                "navigate_to": vector_hit.get("navigate_to"),
                "navigation_links": vector_hit.get("navigation_links"),
                "export_data": vector_hit.get("export_data"),
                "auto_expand": vector_hit.get("auto_expand", False),
                "suggested_questions": vector_hit.get("suggested_questions"),
                "report_intent": vector_hit.get("report_intent"),
                "sql_executed": None,
                "cache_tier": "vector",
                "was_cached": True,
                "latency_ms": int((time.time() - start) * 1000),
            }, question)
    except Exception as e:
        logger.warning(f"[Memory] Tier 2 vector search failed (falling to agent): {e}")

    # ═══════════════════════════════════════════════
    # TIER 3 — Fresh LangGraph agent
    # ═══════════════════════════════════════════════
    try:
        agent_result = await ask_question(history, user_context)
    except Exception as e:
        logger.error(f"[Memory] Agent execution failed: {e}")
        return _apply_kpi_overrides({
            "answer": f"Sorry, I encountered an error processing your question: {str(e)}",
            "chart_data": None,
            "navigate_to": None,
            "navigation_links": None,
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": None,
            "entity_name": None,
            "entity_type": None,
            "is_edit_intent": False,
            "report_intent": None,
            "sql_executed": None,
            "cache_tier": "fresh",
            "was_cached": False,
            "latency_ms": int((time.time() - start) * 1000),
        }, question)

    # agent_result is now an 11-tuple:
    # (answer, chart_data, navigate_to, navigation_links, export_data, auto_expand, suggested_questions, entity_name, entity_type, is_edit_intent, report_intent)
    answer = agent_result[0]
    chart_data = agent_result[1]
    navigate_to = agent_result[2] if len(agent_result) > 2 else None
    navigation_links = agent_result[3] if len(agent_result) > 3 else None
    export_data = agent_result[4] if len(agent_result) > 4 else None
    auto_expand = agent_result[5] if len(agent_result) > 5 else False
    suggested_questions = agent_result[6] if len(agent_result) > 6 else None
    entity_name = agent_result[7] if len(agent_result) > 7 else None
    entity_type = agent_result[8] if len(agent_result) > 8 else None
    is_edit_intent = agent_result[9] if len(agent_result) > 9 else False
    report_intent = agent_result[10] if len(agent_result) > 10 else None
    sql_executed = _extract_sql_from_result(agent_result)

    # Store in both caches in parallel (non-fatal)
    payload = {
        "answer": answer,
        "chart_data": chart_data,
        "navigate_to": navigate_to,
        "navigation_links": navigation_links,
        "export_data": export_data,
        "auto_expand": auto_expand,
        "suggested_questions": suggested_questions,
        "sql_executed": sql_executed,
        "entity_name": entity_name,
        "entity_type": entity_type,
        "is_edit_intent": is_edit_intent,
        "report_intent": report_intent,
        "suggested_questions": suggested_questions,
        "role_scope": vector_scope_key,
        "hit_count": 0,
        "timestamp": datetime.utcnow().isoformat(),
    }

    async def _store_redis():
        try:
            # Cache full payload including navigation and suggested questions
            await redis.setex(redis_key, ttl, json.dumps(payload))
        except Exception as e:
            logger.warning(f"[Memory] Redis cache store failed: {e}")

    async def _store_vector():
        try:
            await vector_store.store_vector_cache(
                question=question.strip().lower(), 
                answer=answer, 
                chart_data=chart_data, 
                sql_executed=sql_executed, 
                role=vector_scope_key,

                navigate_to=navigate_to,
                navigation_links=navigation_links,
                export_data=export_data,
                auto_expand=auto_expand if auto_expand else False,
                suggested_questions=suggested_questions
            )
        except Exception as e:
            logger.warning(f"[Memory] Vector cache store failed: {e}")

    try:
        await asyncio.gather(_store_redis(), _store_vector())
    except Exception as e:
        logger.warning(f"[Memory] Cache store gather failed: {e}")

    logger.info(f"[Memory] Tier 3 Fresh answer in {int((time.time() - start)*1000)}ms")

    result = {
        "answer": answer,
        "chart_data": chart_data,
        "navigate_to": navigate_to,
        "navigation_links": navigation_links,
        "export_data": export_data,
        "auto_expand": auto_expand,
        "suggested_questions": suggested_questions,
        "report_intent": report_intent,
        "sql_executed": sql_executed,
        "entity_name": entity_name,
        "entity_type": entity_type,
        "is_edit_intent": is_edit_intent,
        "cache_tier": "fresh",
        "was_cached": False,
        "latency_ms": int((time.time() - start) * 1000),
    }

    if anomaly_note and not result.get("was_cached") and "error" not in result.get("answer", "").lower()[:50]:
        result["answer"] = result["answer"] + anomaly_note

    return _apply_kpi_overrides(result, question)


# ---------------------------------------------------------------------------
# Cache statistics (admin only)
# ---------------------------------------------------------------------------
async def get_cache_stats() -> dict:
    """
    Returns cache statistics:
    - Redis key count (qa:* pattern)
    - Vector cache document count
    - Cache hits today
    - Top 10 most-hit questions
    """
    stats = {
        "redis_keys": 0,
        "vector_cache_docs": 0,
        "cache_hits_today": 0,
        "top_questions": [],
    }

    # Redis key count
    try:
        redis = get_redis()
        keys = []
        async for key in redis.scan_iter(match="qa:*", count=1000):
            keys.append(key)
        stats["redis_keys"] = len(keys)
    except Exception as e:
        logger.warning(f"[CacheStats] Redis scan failed: {e}")

    # Vector cache doc count
    try:
        col = get_vector_cache_collection()
        stats["vector_cache_docs"] = await col.count_documents({})
    except Exception as e:
        logger.warning(f"[CacheStats] Vector cache count failed: {e}")

    # Cache hits today
    try:
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        history_col = get_chat_history_collection()
        stats["cache_hits_today"] = await history_col.count_documents({
            "was_cache_hit": True,
            "timestamp": {"$gte": today_start},
        })
    except Exception as e:
        logger.warning(f"[CacheStats] History hit count failed: {e}")

    # Top 10 most-hit questions
    try:
        col = get_vector_cache_collection()
        cursor = (
            col.find(
                {},
                {"question": 1, "hit_count": 1, "role_scope": 1, "_id": 0},
            )
            .sort("hit_count", -1)
            .limit(10)
        )
        stats["top_questions"] = await cursor.to_list(length=10)
    except Exception as e:
        logger.warning(f"[CacheStats] Top questions query failed: {e}")

    return stats
