"""
main.py — CRM AI Service
MERGED: Bhavik's JWT chat router + suggestion engine + your deterministic fast-path + streaming endpoint
Version: 3.0.0 (merged)
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import json
import os
import re
import asyncio
from datetime import datetime, date
from dotenv import load_dotenv
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


load_dotenv(override=True)
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import URLError, HTTPError

from fastapi import FastAPI, HTTPException, Request, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Union, Any

from db.database import get_db_engine
from sqlalchemy import text
from agent.intent_classifier import (
    classify_intent,
    should_show_kpi_filters,
    should_show_revenue_report,
    should_show_receivables_report,
    should_show_proposals_report,
    should_show_projects_report,
    should_show_resources_report,
    should_show_recoverability_report,
    should_show_staff_billing_report,
)
# LAZY IMPORT: agent and semantic_layer cause import hang, so defer to function-level imports
# They will be imported inside chat_routes when needed
# from agent import ask_question_async, ask_question_streaming
# from semantic_layer import (...)

app = FastAPI(title="CRM AI Assistant", version="3.0.0")

# ── JWT-authenticated session-based chat router (Bhavik's system) ──────────
# Defer chat_routes import until app is created (it uses lazy imports for agent/semantic)
try:
    from api.chat_routes import router as chat_router, create_session as create_session_endpoint, security
    app.include_router(chat_router)

    # Route aliases for root-level and /api prefix requests (/session, /api/session)
    @app.post("/session", tags=["AI Chat Alias"])
    @app.get("/session", tags=["AI Chat Alias"])
    @app.post("/api/session", tags=["AI Chat Alias"])
    @app.get("/api/session", tags=["AI Chat Alias"])
    async def session_alias(credentials = Depends(security)):
        return await create_session_endpoint(credentials)
except Exception as e:
    print(f"[WARNING] Failed to load chat_routes: {e}")
    print("[INFO] API will work but JWT chat routes unavailable")

@app.on_event("startup")
async def startup_event():
    """Automatically spawn background MCP server dynamically based on environment config."""
    enable_mcp = os.getenv("ENABLE_MCP_SERVER", "true").lower() in ("true", "1", "yes")
    if not enable_mcp:
        print("[INFO] ENABLE_MCP_SERVER is set to false. Skipping MCP auto-launch.")
        return

    mcp_port = int(os.getenv("MCP_PORT", "8001"))
    mcp_host = os.getenv("MCP_HOST", "127.0.0.1")
    
    import socket
    import subprocess
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    res = sock.connect_ex((mcp_host, mcp_port))
    sock.close()
    if res != 0:
        print(f"[INFO] Launching background MCP Server on {mcp_host}:{mcp_port}...")
        mcp_script = os.path.join(os.path.dirname(__file__), "mcp", "mcp_server.py")
        try:
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
            subprocess.Popen([sys.executable, mcp_script], creationflags=creationflags)
            print(f"[INFO] MCP Server successfully launched on port {mcp_port}.")
        except Exception as err:
            print(f"[WARNING] Could not auto-launch MCP Server: {err}")
    else:
        print(f"[INFO] MCP Server is already active on port {mcp_port}.")

# ── Server-load marker for hot-reload debugging ──────────────────────────────
try:
    _main_marker_path = os.path.join(os.path.dirname(__file__), "main_server_load_marker.txt")
    with open(_main_marker_path, "w", encoding="utf-8") as f:
        f.write(f"loaded_at_utc={__import__('datetime').datetime.utcnow().isoformat()}\n")
except Exception:
    pass

raw_origins = os.getenv("ALLOWED_ORIGINS", "")
default_origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:5173",
    "https://gtcrm-bh.com",
    "https://staging.gtcrm-bh.com",
    "https://www.gtcrm-bh.com",
]
if raw_origins:
    allowed_origins = list(set([orig.strip() for orig in raw_origins.split(",") if orig.strip()] + default_origins))
else:
    allowed_origins = default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*\.gtcrm-bh\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class Message(BaseModel):
    role: str
    content: str

class UserContext(BaseModel):
    user_name: str = "Unknown"
    user_id: int = 0
    employee_id: Optional[int] = None
    role_name: str = "Unknown"
    department: str = "Unknown"
    start_date: Optional[str] = None
    end_date: Optional[str] = None

class QuestionRequest(BaseModel):
    messages: List[Message]
    user_context: Optional[UserContext] = None
    auth_token: Optional[str] = None         # Bhavik's JWT pass-through

class AnswerResponse(BaseModel):
    answer: str
    chart_data: Optional[Dict] = None
    action: Optional[str] = None
    navigation_id: Optional[str] = None
    navigate_to: Optional[str] = None
    navigation_links: Optional[List[Dict]] = None
    suggested_questions: Optional[List[str]] = None
    export_data: Optional[Dict] = None
    auto_expand: bool = False
    edit_intent: Optional[Dict] = None   # NEW: populated when is_edit_intent=True
    report_intent: Optional[str] = None  # NEW: allows frontend to render dynamic filter forms
    kpi_payload: Optional[Dict] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def resolve_user_context(user_ctx: UserContext) -> dict:
    """Resolve designation and department names from DB if the frontend sends IDs."""
    result = {
        "user_name": user_ctx.user_name,
        "user_id": user_ctx.user_id,
        "employee_id": user_ctx.employee_id,
        "role_name": user_ctx.role_name,
        "department": user_ctx.department,
        "start_date": user_ctx.start_date,
        "end_date": user_ctx.end_date,
    }

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            emp_lookup_id = result.get("employee_id")
            # Resolve designation name from employee's designation_id
            if result["role_name"] == "Unknown" and emp_lookup_id:
                row = conn.execute(text(
                    "SELECT d.name FROM employees e "
                    "JOIN m_designation d ON e.emp_designation_id = d.id "
                    "WHERE e.id = :emp_id"
                ), {"emp_id": emp_lookup_id}).fetchone()
                if row:
                    result["role_name"] = row[0]

            # Resolve department name from employee's department_id
            if result["department"] == "Unknown" and emp_lookup_id:
                row = conn.execute(text(
                    "SELECT d.name FROM employees e "
                    "JOIN m_department d ON e.emp_department_id = d.id "
                    "WHERE e.id = :emp_id"
                ), {"emp_id": emp_lookup_id}).fetchone()
                if row:
                    result["department"] = row[0]
    except Exception as e:
        print(f"[RBAC] Failed to resolve user context: {e}")

    return result


def _is_kpi_summary_query(question: str) -> bool:
    q = (question or "").lower()
    if "project" in q:
        return False
    kpi_tokens = [
        "kpi summary",
        "kpi report",
        "show kpi",
        "overall kpi",
        "executive kpi",
    ]
    return any(token in q for token in kpi_tokens)


def _resolve_date_window(question: str) -> tuple[str, str]:
    q = (question or "").lower()
    today = date.today()

    if "this month" in q or "current month" in q:
        start = today.replace(day=1)
        end = today
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    if "fy" in q or "fiscal year" in q or "this year" in q or "current year" in q:
        if today.month >= 10:
            fy_start = date(today.year, 10, 1)
        else:
            fy_start = date(today.year - 1, 10, 1)
        return fy_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

    return today.replace(day=1).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _extract_named_filter(question: str, marker: str) -> Optional[str]:
    q = (question or "").strip()
    pattern = rf"{marker}\s+([a-zA-Z0-9&\-\s\.]+)"
    m = re.search(pattern, q, flags=re.IGNORECASE)
    if not m:
        return None
    value = m.group(1).strip()
    # stop at common suffixes
    value = re.split(r"(?:\b(?:for|from|to|in|this|current|summary|report|kpi|fy|financial\s+year|fiscal\s+year)\b|\b20\d{2}\b|fy\d{4})", value, flags=re.IGNORECASE)[0].strip()
    return value or None


def _lookup_single_id(table: str, id_col: str, name_col: str, value: Optional[str]) -> tuple[Optional[int], list[dict]]:
    if not value:
        return None, []
    engine = get_db_engine()
    term = " ".join(str(value).split())
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT {id_col} AS id, {name_col} AS name
                    FROM {table}
                    WHERE REPLACE(REPLACE(LOWER({name_col}), '  ', ' '), '  ', ' ') LIKE LOWER(:term)
                    LIMIT 6
                    """
                ),
                {"term": f"%{term}%"},
            ).fetchall()
        candidates = [{"id": int(r.id), "name": str(r.name)} for r in rows]
        if len(candidates) == 1:
            return candidates[0]["id"], candidates
        return None, candidates
    except Exception:
        return None, []


KPI_FILTER_SETUP_PROMPT = """📋 KPI SUMMARY REPORT — Filter Setup
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _safe_number(value, default=None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _format_money_value(value, *, allow_plus: bool = False, use_brackets: bool = False, dash_if_empty: bool = True) -> str:
    number = _safe_number(value, None)
    if number is None:
        return "-" if dash_if_empty else "0"
    rounded = int(round(number))
    if rounded == 0 and dash_if_empty:
        return "-"
    digits = f"{abs(rounded):,}"
    if rounded < 0 and use_brackets:
        return f"({digits})"
    if rounded > 0 and allow_plus:
        return f"+{digits}"
    return digits


def _format_percent_value(value) -> str:
    number = _safe_number(value, None)
    if number is None:
        return "-"
    return f"{round(number):.0f}%"


def _normalize_month_label(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    lookup = {
        "january": "Jan", "jan": "Jan",
        "february": "Feb", "feb": "Feb",
        "march": "Mar", "mar": "Mar",
        "april": "Apr", "apr": "Apr",
        "may": "May",
        "june": "Jun", "jun": "Jun",
        "july": "Jul", "jul": "Jul",
        "august": "Aug", "aug": "Aug",
        "september": "Sep", "sep": "Sep",
        "october": "Oct", "oct": "Oct",
        "november": "Nov", "nov": "Nov",
        "december": "Dec", "dec": "Dec",
        "total": "Total",
    }
    lowered = text.lower()
    if lowered in lookup:
        return lookup[lowered]
    if len(text) >= 3:
        candidate = text[:3].title()
        if candidate in {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}:
            return candidate
    return text.title()


def _extract_value_from_line(question: str, label: str) -> Optional[str]:
    if not question:
        return None
    pattern = re.compile(rf"^\s*{label}\s*(?:[:=→-]\s*|\s+)(.+?)\s*$", re.IGNORECASE)
    for line in question.splitlines():
        match = pattern.match(line.strip())
        if match:
            value = match.group(1).strip()
            return value or None
    return None


def _is_placeholder_filter_value(value: Optional[str]) -> bool:
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    lower = v.lower()
    if "e.g." in lower or "example" in lower:
        return True
    if lower.startswith("(") and lower.endswith(")"):
        return True
    return False


def _clean_filter_value(value: Optional[str]) -> Optional[str]:
    if _is_placeholder_filter_value(value):
        return None
    return str(value).strip()


def _extract_date_range(question: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not question:
        return None, None, None

    normalized = question.replace("–", "-").replace("—", "-")
    match = re.search(
        r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})\s*(?:to|through|->|→|-)\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        start_date = match.group(1).replace("/", "-")
        end_date = match.group(2).replace("/", "-")
        return None, f"{start_date} → {end_date}", f"{start_date}|{end_date}"

    fy_match = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", normalized)
    if fy_match:
        fy_start_year = int(fy_match.group(1))
        fy_end_year = int(fy_match.group(2))
        start_date = f"01-10-{fy_start_year}"
        end_date = f"30-09-{fy_end_year}"
        return f"{fy_start_year}-{fy_end_year}", f"{start_date} → {end_date}", f"{start_date}|{end_date}"

    return None, None, None


def _extract_kpi_filters_from_text(question: str) -> dict:
    q = question or ""
    from agent.entity_resolver import has_employee_trigger
    from agent.query_parser import _extract_person_name, _extract_service_line_name
    
    def _first_valid(*extractors):
        """Runs each lambda safely in sequence, returning the first truthy result."""
        for extract in extractors:
            try:
                val = extract()
                if val:
                    return val
            except Exception:
                pass
        return None

    fy_from_text, parsed_date_range, parsed_period = _extract_date_range(q)
    
    # NLP fallback for conversational dates and FY defaults
    if not fy_from_text or not parsed_date_range:
        from agent.query_parser import _extract_date_range as _qp_extract
        start_d, end_d, was_specified = _qp_extract(q)
        if start_d and end_d:
            try:
                y1, m1, _ = map(int, start_d.split("-"))
                if not fy_from_text:
                    fy_from_text = f"{y1}-{y1+1}" if m1 >= 10 else f"{y1-1}-{y1}"
                
                if not parsed_date_range and was_specified:
                    d1 = "-".join(reversed(start_d.split("-")))
                    d2 = "-".join(reversed(end_d.split("-")))
                    parsed_date_range = f"{d1} -> {d2}"
                    parsed_period = f"{d1}|{d2}"
            except Exception:
                pass

    return {
        "service_line": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Service\s*Line")),
            lambda: _extract_named_filter(q, r"service\s*line"),
            lambda: _extract_service_line_name(q)
        ),
        "department": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Department")),
            lambda: _extract_named_filter(q, r"department")
        ),
        "employee_name": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Employee\s*Name")),
            lambda: _extract_named_filter(q, r"employee\s*name|employee") if has_employee_trigger(q) else None,
            lambda: _extract_person_name(q)
        ),
        "project_name": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Project\s*Name")),
            lambda: _extract_named_filter(q, r"project\s*name|project"),
            lambda: __import__('agent.query_parser', fromlist=[''])._extract_project_name(q)
        ),
        "customer": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Customer")),
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Customer\s*Name")),
            lambda: _extract_named_filter(q, r"customer"),
            lambda: __import__('agent.query_parser', fromlist=[''])._extract_company_name(q)
        ),
        "financial_year": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Financial\s*Year")),
            lambda: fy_from_text
        ),
        "date_range": _first_valid(
            lambda: _clean_filter_value(_extract_value_from_line(q, r"Date\s*Range")),
            lambda: parsed_date_range
        ),
        "period": parsed_period,
    }


def _extract_customer_from_history(history: Optional[List[dict]]) -> Optional[str]:
    """Extracts customer name from recent conversation history when pronominal queries like 'that customer' are used."""
    if not history:
        return None
    
    cust_regex = re.compile(
        r'(?:Customer\s*Name|Customer|Client\s*Name|Client)\s*[:=]\s*\*?\*?([A-Za-z0-9 &._-]+?\b(?:\s+B\s*S\s*C\s*C|\s+W\s*L\s*L|\s+S\s*P\s*C|\s+L\s*T\s*D|\s+Inc|\s+Corp|\s+Group|\s+Holding|\s+Holdings)?)\*?\*?(?:\n|$|\,|\|)',
        re.IGNORECASE
    )
    
    for message in reversed(history):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if not content:
            continue
        
        m = cust_regex.search(str(content))
        if m:
            val = m.group(1).strip()
            val = re.sub(r'[*_~`]', '', val).strip()
            if val and val.lower() not in ('all', 'none', 'n/a', 'select', 'that customer', 'this customer', 'the customer', 'customer', 'that client', 'this client'):
                return val
                
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
        if role == "user":
            from agent.query_parser import _extract_company_name
            comp = _extract_company_name(str(content))
            if comp and comp.lower() not in ('all', 'none', 'n/a', 'select', 'that customer', 'this customer', 'the customer', 'customer', 'that client', 'this client'):
                return comp
                
    return None


def _extract_kpi_filters_from_history(history: Optional[List[dict]]) -> dict:
    """Extracts KPI and report entity filters (customer, service_line, employee, department) from conversation history."""
    if not history:
        return {}

    extracted: dict = {}
    
    # Check for table formatted filters first
    table_pattern = re.compile(r"^\|\s*(Service Line|Department|Employee Name|Customer|Customer Name|Financial Year|Date Range)\s*\|\s*(.*?)\s*\|\s*$", re.IGNORECASE)
    for message in reversed(history):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if not content:
            continue
        for line in str(content).splitlines():
            match = table_pattern.match(line.strip())
            if not match:
                continue
            key = match.group(1).strip().lower().replace(" ", "_")
            if key == "customer_name":
                key = "customer"
            value = match.group(2).strip()
            if value and value.lower() not in ("all", "none", "n/a", "select"):
                extracted[key] = value

    # 1. Search for customer if missing
    if "customer" not in extracted:
        cust = _extract_customer_from_history(history)
        if cust:
            extracted["customer"] = cust
            extracted["customer_name"] = cust

    # 2. Key-value line patterns like "- **Service Line:** Audit" or "Service Line: Audit"
    sl_regex = re.compile(r'(?:Service\s*Line|ServiceLine)\s*[:=]\s*\*?\*?([A-Za-z0-9 &._-]+?\b)\*?\*?', re.IGNORECASE)
    emp_regex = re.compile(r'(?:Employee|Employee\s*Name|Partner|Incharge|In-Charge)\s*[:=]\s*\*?\*?([A-Za-z0-9 &._-]+?\b)\*?\*?', re.IGNORECASE)
    dept_regex = re.compile(r'(?:Department|Dept)\s*[:=]\s*\*?\*?([A-Za-z0-9 &._-]+?\b)\*?\*?', re.IGNORECASE)

    for message in reversed(history):
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        if not content:
            continue
        c_str = str(content)

        if "service_line" not in extracted:
            m = sl_regex.search(c_str)
            if m and m.group(1).strip().lower() not in ("all", "none", "n/a", "select", "performance", "revenue", "report"):
                extracted["service_line"] = m.group(1).strip()

        if "employee_name" not in extracted:
            m = emp_regex.search(c_str)
            if m and m.group(1).strip().lower() not in ("all", "none", "n/a", "select", "cost", "billing", "report"):
                extracted["employee_name"] = m.group(1).strip()

        if "department" not in extracted:
            m = dept_regex.search(c_str)
            if m and m.group(1).strip().lower() not in ("all", "none", "n/a", "select", "utilization", "report"):
                extracted["department"] = m.group(1).strip()

    return extracted


def _resolve_generic_report_context(latest_question: str, history: Optional[List[dict]]) -> Optional[str]:
    """
    If the user asks a generic 'generate report / export report / report for it' question,
    resolves the appropriate report intent by inspecting recent conversation history.
    """
    if not history:
        return None
        
    q_norm = (latest_question or "").strip().lower()
    q_clean = re.sub(r'[^a-z0-9\s]', '', q_norm).strip()
    
    _GENERIC_PATTERNS = [
        "generate a report", "generate report", "make a report", "create a report",
        "generate the report", "export report", "export the report", "download report",
        "report of it", "report for it", "report of this", "report for this",
        "generate report of it", "generate report for it", "generate a report of it",
        "generate a report for it", "generate report of this", "generate report for this",
        "get report", "create report", "give me report", "give me a report",
        "show report", "open report", "report", "reports"
    ]
    
    is_generic = q_clean in _GENERIC_PATTERNS or (
        ("generate" in q_clean or "create" in q_clean or "export" in q_clean or "make" in q_clean or "give" in q_clean)
        and "report" in q_clean
    )
    
    if not is_generic:
        return None
        
    for message in reversed(history):
        content = str(message.get("content") if isinstance(message, dict) else getattr(message, "content", "")).lower()
        if not content:
            continue
            
        if "recoverability" in content:
            return "recoverability"
        if "staff billing" in content or "employee billing" in content or "partner billing" in content or "staff cost" in content:
            return "staff_billing"
        if "projects list" in content or "projects summary" in content or "project" in content:
            return "projects"
        if "receivable" in content or "ageing" in content or "invoice" in content or "receivables" in content:
            return "receivables"
        if "proposal" in content or "pipeline" in content or "win rate" in content:
            return "proposals"
        if "revenue" in content or "gross profit" in content or "gp performance" in content or "service line" in content:
            return "revenue"
        if "kpi" in content:
            return "kpi_summary"
            
    return "kpi_summary"



def _merge_kpi_filters(current_filters: dict, historical_filters: dict) -> dict:
    merged = dict(historical_filters or {})
    merged.update({k: v for k, v in (current_filters or {}).items() if v not in (None, "")})
    return merged


def _kpi_filters_complete(filters: dict) -> bool:
    # If the user explicitly provided a specific entity, bypass the strict form requirement.
    # The downstream logic automatically defaults missing time to current FY, and missing groups to 'All'.
    for key in ["service_line", "department", "employee_name", "customer"]:
        val = (filters or {}).get(key)
        if val and not _is_placeholder_filter_value(val) and str(val).strip().lower() != "all":
            return True
            
    required = ["service_line", "department", "employee_name", "financial_year", "date_range"]
    for key in required:
        value = (filters or {}).get(key)
        if _is_placeholder_filter_value(value):
            return False
    return True


def _history_has_kpi_filter_confirmation(history: Optional[List[dict]]) -> bool:
    for msg in reversed(history or []):
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        if content and "✅ Confirmed Filters:" in str(content):
            return True
    return False


def _is_kpi_confirmation_text(text: str) -> bool:
    q = (text or "").strip().lower()
    if not q:
        return False
    return bool(re.search(r"\b(confirm|confirmed|proceed|go ahead|continue)\b", q))


def _format_kpi_filter_setup() -> str:
    return KPI_FILTER_SETUP_PROMPT


def _pretty_date_range(date_range: Optional[str]) -> str:
    """Return a human-readable label for a date range string.
    - Single month (same month): "01-10-2023 to 31-10-2023"  → "October 2023"
    - Full FY:                   "01-10-2023 to 30-09-2024"  → "01-10-2023 to 30-09-2024"
    - Anything else:             returned as-is.
    """
    if not date_range or str(date_range).strip().lower() in ("all", ""):
        return "All"
    val = str(date_range).strip()
    m = re.search(r"(\d{1,2})[-/](\d{2})[-/](\d{4}).*?(\d{1,2})[-/](\d{2})[-/](\d{4})", val)
    if not m:
        return val
    start_day, start_month, start_year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    end_day,   end_month,   end_year   = int(m.group(4)), int(m.group(5)), int(m.group(6))
    # Same month + year → pretty month label
    if start_month == end_month and start_year == end_year and start_day == 1:
        month_names = {
            1: "January", 2: "February", 3: "March", 4: "April",
            5: "May", 6: "June", 7: "July", 8: "August",
            9: "September", 10: "October", 11: "November", 12: "December",
        }
        return f"{month_names.get(start_month, str(start_month))} {start_year}"
    return val


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
        "",
    ])


def _format_kpi_report(kpi_payload: dict) -> str:
    summary_cards = {card.get("key"): card.get("value") for card in (kpi_payload.get("summary_cards") or [])}
    billing_rows = [row for row in (kpi_payload.get("billing_revenue_gp_table") or []) if _normalize_month_label(row.get("month")) != "Total"]
    billing_by_month = { _normalize_month_label(row.get("month")): row for row in billing_rows }
    total_billing_row = next((row for row in (kpi_payload.get("billing_revenue_gp_table") or []) if _normalize_month_label(row.get("month")) == "Total"), {})

    aging_rows = kpi_payload.get("receivable_aging_table") or []
    aging_by_month = { _normalize_month_label(row.get("month")): row for row in aging_rows if _normalize_month_label(row.get("month")) != "Total" }
    aging_total_row = next((row for row in aging_rows if _normalize_month_label(row.get("month")) == "Total"), {})

    gp_rows = kpi_payload.get("gp_performance_by_service_line") or []

    def row_value(row: dict, *keys, default=None):
        for key in keys:
            if key in (row or {}) and row.get(key) not in (None, ""):
                return row.get(key)
        return default

    months = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]
    bucket_keys = ["<30", "30-60", "60-120", "120-180", "180-365", ">365"]
    bucket_labels = ["< 30 days", "30–60 days", "60–120 days", "120–180 days", "180–365 days", "> 365 days"]

    budget_vs_actual_rev = summary_cards.get("budget_vs_actual_revenue")
    budget_vs_actual_gp = summary_cards.get("budget_vs_actual_gp")
    project_in_hand = summary_cards.get("project_in_hand")
    open_proposals = summary_cards.get("open_proposals")
    secured_business = summary_cards.get("secured_business")
    balance_to_achieve = summary_cards.get("balance_to_achieve")
    utilization = summary_cards.get("utilization")

    revenue_total = _safe_number(summary_cards.get("total_revenue"), None)
    target_revenue = _safe_number(summary_cards.get("target_revenue"), None)
    receivable_risk = 0
    if aging_total_row:
        receivable_risk = sum(_safe_number(aging_total_row.get(key), 0) or 0 for key in ["120-180", "180-365", ">365"])

    lines = []
    lines.append("📊 KPI SUMMARY CARDS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("| Metric                 | Value          |")
    lines.append("|------------------------|----------------|")
    lines.append(f"| Budget vs Actual Rev   | {_format_money_value(budget_vs_actual_rev, use_brackets=True)} {'🔴' if _safe_number(budget_vs_actual_rev, 0) < 0 else '🟢'} |")
    lines.append(f"| Budget vs Actual GP    | {_format_money_value(budget_vs_actual_gp, allow_plus=True, use_brackets=True)} {'🟢' if _safe_number(budget_vs_actual_gp, 0) >= 0 else '🔴'} |")
    lines.append(f"| Project in Hand        | {_format_money_value(project_in_hand)} |")
    lines.append(f"| Open Proposals         | {_format_money_value(open_proposals)} |")
    lines.append(f"| Secured Business       | {_format_money_value(secured_business)} |")
    lines.append(f"| Balance to Achieve     | {_format_money_value(balance_to_achieve)} |")
    lines.append(f"| Utilization            | {_format_percent_value(utilization)} |")
    lines.append("")

    lines.append("📈 GP PERFORMANCE BY SERVICE LINE")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("| Service Line         | Actual | Target | Achievement |")
    lines.append("|----------------------|--------|--------|-------------|")
    if gp_rows:
        for row in gp_rows:
            actual = _safe_number(row_value(row, "actual", "performing", "value"), 0) or 0
            target = _safe_number(row_value(row, "target"), 0) or 0
            achievement = (actual / target * 100) if target else None
            if achievement is None:
                achievement_text = "-"
                status_icon = "⚪"
            elif achievement >= 75:
                achievement_text = f"{round(achievement):.0f}%"
                status_icon = "🟢"
            elif achievement >= 50:
                achievement_text = f"{round(achievement):.0f}%"
                status_icon = "🟡"
            else:
                achievement_text = f"{round(achievement):.0f}%"
                status_icon = "🔴"
            lines.append(
                f"| {row.get('name', row.get('service_line', ''))} | {_format_money_value(actual, dash_if_empty=False)} | {_format_money_value(target, dash_if_empty=False)} | {achievement_text} {status_icon} |"
            )
    else:
        lines.append("| - | - | - | - |")
    lines.append("")

    lines.append("📅 MONTHLY BILLING REVENUE & GP TABLE")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("| Particulars      | Oct | Nov | Dec | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | YTD Bud vs Act. | Total |")
    lines.append("|------------------|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|----------------|-------|")

    def month_cell(row: dict, *keys):
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return _format_money_value(value, dash_if_empty=True)
        return "-"

    budget_row = ["Budgeted Revenue"]
    actual_row = ["Actual Revenue"]
    variance_row = ["Variance"]
    direct_cost_row = ["Total Direct Cost"]
    gp_row = ["Gross Profit"]
    for month in months:
        month_data = billing_by_month.get(month, {})
        budget_row.append(month_cell(month_data, "target_value", "budgeted_revenue", "budget_revenue", "budget"))
        actual_row.append(month_cell(month_data, "total_invoice_amount_with_credit", "actual_revenue", "actual"))
        variance_value = row_value(month_data, "variance", default=None)
        if variance_value in (None, ""):
            variance_row.append("-")
        else:
            variance_number = _safe_number(variance_value, 0) or 0
            variance_icon = "🔴" if variance_number < 0 else "🟢"
            variance_row.append(f"{_format_money_value(variance_number, allow_plus=True, use_brackets=True)} {variance_icon}")
        direct_cost_row.append(month_cell(month_data, "total_direct_cost", "direct_cost"))
        gp_row.append(month_cell(month_data, "total_gross_profit", "gross_profit", "gp"))

    budget_total = row_value(total_billing_row, "target_value", "budgeted_revenue", "budget")
    actual_total = row_value(total_billing_row, "total_invoice_amount_with_credit", "actual_revenue", "actual")
    variance_total = row_value(total_billing_row, "variance")
    direct_cost_total = row_value(total_billing_row, "total_direct_cost", "direct_cost")
    gross_profit_total = row_value(total_billing_row, "total_gross_profit", "gross_profit", "gp")

    lines.append("| " + " | ".join(budget_row + [_format_money_value(_safe_number(budget_total, None), dash_if_empty=True), _format_money_value(_safe_number(budget_total, None), dash_if_empty=True)]) + " |")
    lines.append("| " + " | ".join(actual_row + [_format_money_value(_safe_number(actual_total, None), dash_if_empty=True), _format_money_value(_safe_number(actual_total, None), dash_if_empty=True)]) + " |")
    lines.append("| " + " | ".join(variance_row + [_format_money_value(_safe_number(variance_total, None), allow_plus=True, use_brackets=True), _format_money_value(_safe_number(variance_total, None), allow_plus=True, use_brackets=True)]) + " |")
    lines.append("| " + " | ".join(direct_cost_row + [_format_money_value(_safe_number(direct_cost_total, None), dash_if_empty=True), _format_money_value(_safe_number(direct_cost_total, None), dash_if_empty=True)]) + " |")
    lines.append("| " + " | ".join(gp_row + [_format_money_value(_safe_number(gross_profit_total, None), dash_if_empty=True), _format_money_value(_safe_number(gross_profit_total, None), dash_if_empty=True)]) + " |")
    lines.append("")

    lines.append("📂 RECEIVABLE AGING SUMMARY")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("| Aging Bucket  | Oct | Nov | Dec | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Total |")
    lines.append("|---------------|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-----|-------|")
    for bucket_key, bucket_label in zip(bucket_keys, bucket_labels):
        cells = [bucket_label]
        for month in months:
            month_data = aging_by_month.get(month, {})
            cells.append(_format_money_value(month_data.get(bucket_key), dash_if_empty=True))
        total_value = _safe_number(aging_total_row.get(bucket_key), None)
        cells.append(_format_money_value(total_value, dash_if_empty=True))
        lines.append("| " + " | ".join(cells) + " |")
    total_cells = ["Total"]
    for month in months:
        month_data = aging_by_month.get(month, {})
        month_total = sum(_safe_number(month_data.get(bucket), 0) or 0 for bucket in bucket_keys)
        total_cells.append(_format_money_value(month_total, dash_if_empty=True))
    total_cells.append(_format_money_value(sum(_safe_number(aging_total_row.get(bucket), 0) or 0 for bucket in bucket_keys), dash_if_empty=True))
    lines.append("| " + " | ".join(total_cells) + " |")
    lines.append("")

    lines.append("🧠 AI INSIGHT")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if target_revenue is not None and revenue_total is not None and target_revenue > 0:
        achievement = revenue_total / target_revenue * 100 if target_revenue else None
        gap_month = None
        gap_value = None
        for month in months:
            month_data = billing_by_month.get(month, {})
            variance_value = _safe_number(month_data.get("variance"), None)
            if variance_value is not None and (gap_value is None or variance_value < gap_value):
                gap_value = variance_value
                gap_month = month
        lines.append(f"- Revenue: {round(achievement):.0f}% of annual target achieved. Biggest gap in {gap_month or '-'}.")
    else:
        lines.append("- Revenue: Target comparison unavailable from the current payload.")

    if gp_rows:
        best_row = None
        best_pct = None
        for row in gp_rows:
            actual = _safe_number(row_value(row, "actual", "performing", "value"), 0) or 0
            target = _safe_number(row_value(row, "target"), 0) or 0
            pct = (actual / target * 100) if target else None
            if pct is not None and (best_pct is None or pct > best_pct):
                best_pct = pct
                best_row = row
        if best_row and best_pct is not None:
            lines.append(f"- Top Service Line: {best_row.get('name', best_row.get('service_line', '-'))} at {round(best_pct):.0f}% of target.")
        else:
            lines.append("- Top Service Line: Not available in the current payload.")
    else:
        lines.append("- Top Service Line: Not available in the current payload.")

    lines.append(f"- Receivables Risk: BHD {_format_money_value(receivable_risk, dash_if_empty=False)} overdue beyond 120 days; Utilization: {_format_percent_value(utilization)}.")

    return "\n".join(lines)


def _call_json_api(url: str, auth_token: Optional[str]) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    if auth_token:
        clean_tok = str(auth_token).replace("Bearer ", "").replace("bearer ", "").strip()
        headers["Authorization"] = f"Bearer {clean_tok}"
    req = UrlRequest(url=url, headers=headers, method="GET")
    with urlopen(req, timeout=25) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)



def _build_kpi_contract(kpi_data: dict, aging_data: dict, filters_applied: dict, period: dict) -> dict:
    if isinstance(kpi_data, dict) and isinstance(kpi_data.get("data"), dict):
        kpi_data = kpi_data["data"]
    if isinstance(aging_data, dict) and isinstance(aging_data.get("data"), dict):
        aging_data = aging_data["data"]

    rows = (kpi_data or {}).get("rows") or []
    total_row = next((r for r in rows if str(r.get("month", "")).lower() == "total"), {})
    gp_rows = (kpi_data or {}).get("gp_performance_by_service_line") or (kpi_data or {}).get("service_line_performance") or []

    import logging
    logger = logging.getLogger("uvicorn")

    print(f"\n=============================================================")
    print(f"[EXACT DATA RECEIVED BY _build_kpi_contract]")
    print(f"rows={rows}")
    print(f"rows[0]={rows[0] if rows else None}")
    print(f"summary={(kpi_data or {}).get('summary')}")
    print(f"project_all={(kpi_data or {}).get('project_all')}")
    print(f"proposals_all={(kpi_data or {}).get('proposals_all')}")
    print(f"secured_business={(kpi_data or {}).get('secured_business')}")
    print(f"balance_to_achieve={(kpi_data or {}).get('balance_to_achieve')}")
    print(f"variance={(kpi_data or {}).get('variance')}")
    print(f"variance_gp={(kpi_data or {}).get('variance_gp')}")
    print(f"=============================================================\n")

    print(f"\n=============================================================")
    print(f"[EXACT TOTAL_ROW USED BY CONTRACT]")
    print(f"rows_count={len(rows)}")
    print(f"total_row={total_row}")
    print(f"=============================================================\n")
    logger.info(f"[2. EXACT TOTAL_ROW USED BY CONTRACT] rows_count={len(rows)} total_row={total_row}")

    def n(val, default=0):
        try:
            return float(val)
        except Exception:
            return default

    # 1. Project In Hand: select value from project_all or total_projects (selecting value when > 100)
    val_tp = n((kpi_data or {}).get("total_projects"))
    val_pa = n((kpi_data or {}).get("project_all"))
    if (kpi_data or {}).get("project_in_hand") is not None:
        proj_in_hand_val = n((kpi_data or {}).get("project_in_hand"))
    elif val_pa > 100:
        proj_in_hand_val = val_pa
    elif val_tp > 100:
        proj_in_hand_val = val_tp
    else:
        proj_in_hand_val = max(val_pa, val_tp)

    # 2. Open Proposals: select value from proposals_all or total_proposals (selecting value when > 100)
    val_tprop = n((kpi_data or {}).get("total_proposals"))
    val_prall = n((kpi_data or {}).get("proposals_all"))
    if (kpi_data or {}).get("open_proposals") is not None:
        open_props_val = n((kpi_data or {}).get("open_proposals"))
    elif val_prall > 100:
        open_props_val = val_prall
    elif val_tprop > 100:
        open_props_val = val_tprop
    else:
        open_props_val = max(val_prall, val_tprop)

    # 3. Target Revenue: total_row["target_all_value"] or total_row["target_revenue"] or total_row["target_value"]
    t_target = n(
        total_row.get("target_all_value")
        if total_row.get("target_all_value") is not None
        else (total_row.get("target_revenue") if total_row.get("target_revenue") is not None else (total_row.get("target_value") if total_row.get("target_value") is not None else (kpi_data or {}).get("target_all_value")))
    )

    # 4. Budget vs Actual Revenue Variance: total_row["variance"]
    b_vs_a_rev_val = n(
        total_row.get("variance")
        if total_row.get("variance") is not None
        else (total_row.get("budget_vs_actual_revenu") if total_row.get("budget_vs_actual_revenu") is not None else (kpi_data or {}).get("variance"))
    )

    # 5. Budget vs Actual GP Variance: total_row["variance_gp"]
    b_vs_a_gp_val = n(
        total_row.get("variance_gp")
        if total_row.get("variance_gp") is not None
        else (total_row.get("budget_vs_actual_gp_percent") if total_row.get("budget_vs_actual_gp_percent") is not None else (kpi_data or {}).get("variance_gp"))
    )

    # 6. Gross Invoiced Revenue: total_row["total_invoice_amount_with_credit"] or total_row["total_invoice_amount"]
    t_inv = n(
        total_row.get("total_invoice_amount_with_credit")
        if total_row.get("total_invoice_amount_with_credit") is not None
        else (total_row.get("total_invoice_amount") if total_row.get("total_invoice_amount") is not None else (kpi_data or {}).get("total_invoice_amount_with_credit"))
    )

    t_disc = n(
        total_row.get("total_credit_amount")
        if total_row.get("total_credit_amount") is not None
        else (total_row.get("total_discount_cost") if total_row.get("total_discount_cost") is not None else (kpi_data or {}).get("total_credit_amount"))
    )

    emp_id = (kpi_data or {}).get("employee_id") or (filters_applied or {}).get("employee_id")
    s_date = (period or {}).get("start_date") or (filters_applied or {}).get("start_date") or (kpi_data or {}).get("start_date")
    e_date = (period or {}).get("end_date") or (filters_applied or {}).get("end_date") or (kpi_data or {}).get("end_date")

    if s_date and e_date and emp_id:
        try:
            from semantic.semantic_layer import get_db_engine
            from sqlalchemy import text
            db_eng = get_db_engine()
            with db_eng.connect() as conn:
                q = text("""
                    SELECT COALESCE(SUM(approved_fees), 0) AS p_fees
                    FROM projects
                    WHERE created_at BETWEEN :s_date AND :e_date
                      AND status_id NOT IN ('3', '4', '12')
                      AND NOT EXISTS (SELECT 1 FROM invoice inv WHERE inv.project_id = projects.id)
                      AND (manager = :emp_id OR partner = :emp_id OR main_incharge = :emp_id OR client_relation = :emp_id OR created_by = :emp_id)
                """)
                res = conn.execute(q, {
                    "s_date": f"{s_date} 00:00:00",
                    "e_date": f"{e_date} 23:59:59",
                    "emp_id": emp_id
                }).fetchone()
                if res and res[0] is not None and float(res[0]) > 0:
                    proj_in_hand_val = float(res[0])
        except Exception as p_err:
            logger.warning(f"[KPI_CONTRACT_RECONCILE_ERR] {p_err}")

    month_rows = [r for r in rows if str(r.get("month", "")).lower() != "total"]
    if month_rows:
        gross_profit_val = sum(n(r.get("total_gross_profit") if r.get("total_gross_profit") is not None else r.get("gross_profit")) for r in month_rows)
        target_gp_val = sum(n(r.get("target_gp")) for r in month_rows)
    else:
        target_gp_val = n(total_row.get("target_gp") if total_row.get("target_gp") is not None else (kpi_data or {}).get("target_gp"))
        gross_profit_val = n(total_row.get("total_gross_profit") if total_row.get("total_gross_profit") is not None else (kpi_data or {}).get("total_gross_profit") or (kpi_data or {}).get("gross_profit"))

    if target_gp_val > 0 or gross_profit_val > 0:
        b_vs_a_gp_val = gross_profit_val - target_gp_val

    # 7. Secured Business: gross_invoiced_revenue + project_in_hand
    secured_val = t_inv + proj_in_hand_val

    # 8. Balance to Achieve: target_revenue - secured_business
    balance_val = t_target - secured_val

    total_direct_cost_val = n(total_row.get("total_direct_cost") if total_row.get("total_direct_cost") is not None else (kpi_data or {}).get("total_direct_cost"))
    total_receivables_val = n(total_row.get("total_rem_amount") if total_row.get("total_rem_amount") is not None else (kpi_data or {}).get("total_receivables") or (kpi_data or {}).get("total_rem_amount"))
    total_revenue_val = n(total_row.get("total_invoice_amount_with_credit") if total_row.get("total_invoice_amount_with_credit") is not None else (kpi_data or {}).get("total_revenue") or t_inv)

    summary_cards = [
        {"key": "secured_business", "label": "Secured Business", "value": secured_val},
        {"key": "balance_to_achieve", "label": "Balance to Achieve", "value": balance_val},
        {"key": "budget_vs_actual_revenue", "label": "Budget vs Actual Revenue", "value": b_vs_a_rev_val},
        {"key": "budget_vs_actual_gp", "label": "Budget vs Actual GP", "value": b_vs_a_gp_val},
        {"key": "gross_profit", "label": "Gross Profit", "value": gross_profit_val},
        {"key": "total_direct_cost", "label": "Total Direct Cost", "value": total_direct_cost_val},
        {"key": "open_proposals", "label": "Open Proposals", "value": open_props_val},
        {"key": "project_in_hand", "label": "Project in Hand", "value": proj_in_hand_val},
        {"key": "total_receivables", "label": "Total Receivables", "value": total_receivables_val},
    ]

    receivable_rows = (aging_data or {}).get("receivable_aging") or []
    receivable_total = next((r for r in receivable_rows if str(r.get("month", "")).lower() == "total"), {})
    summary_cards.append(
        {
            "key": "receivable_180_plus_days",
            "label": "Receivable 180+ Days",
            "value": n(receivable_total.get("180-365")) + n(receivable_total.get(">365")),
        }
    )

    summary_cards.extend([
        {"key": "target_revenue", "label": "Target Revenue", "value": t_target},
        {"key": "target_gp", "label": "Target GP", "value": target_gp_val},
        {"key": "variance", "label": "Variance", "value": b_vs_a_rev_val},
        {"key": "variance_gp", "label": "Variance GP", "value": b_vs_a_gp_val},
        {"key": "total_revenue", "label": "Total Revenue", "value": total_revenue_val},
        {"key": "utilization", "label": "Utilization", "value": n((kpi_data or {}).get("utilization") or (kpi_data or {}).get("utilization_pct") or (kpi_data or {}).get("utilisation") or (kpi_data or {}).get("utilisation_pct"))},
    ])
    logger.info(f"[3. EXACT SUMMARY_CARDS GENERATED BY CONTRACT] {summary_cards}")

    emp_id = (kpi_data or {}).get("employee_id") or (filters_applied or {}).get("employee_id")
    emp_name = (kpi_data or {}).get("employee_name") or (filters_applied or {}).get("employee_name") or "Organization Aggregate"
    date_range_val = (kpi_data or {}).get("date_range") or (period if isinstance(period, dict) and period.get("start_date") else {})
    if not isinstance(date_range_val, dict) or not date_range_val.get("start"):
        if isinstance(period, dict) and (period.get("start_date") or period.get("start")):
            date_range_val = {"start": period.get("start_date") or period.get("start"), "end": period.get("end_date") or period.get("end")}

    return {
        "summary": {
            "employee_id": emp_id,
            "employee_name": emp_name,
            "date_range": date_range_val,
            "secured_business": secured_val,
            "target_revenue": t_target,
            "balance_to_achieve": balance_val,
            "gross_invoiced_revenue": t_inv,
            "total_credit_notes": t_disc,
            "target_gp": target_gp_val,
            "gross_profit": gross_profit_val,
            "total_proposals": open_props_val,
            "total_projects": proj_in_hand_val,
            "project_in_hand": proj_in_hand_val,
            "open_proposals": open_props_val,
            "budget_vs_actual_gp": b_vs_a_gp_val,
            "budget_vs_actual_revenue": b_vs_a_rev_val,
            "total_receivables": total_receivables_val
        },
        "employee_id": emp_id,
        "employee_name": emp_name,
        "date_range": date_range_val,
        "secured_business": secured_val,
        "balance_to_achieve": balance_val,
        "budget_vs_actual_gp": b_vs_a_gp_val,
        "budget_vs_actual_revenue": b_vs_a_rev_val,
        "project_in_hand": proj_in_hand_val,
        "total_projects": proj_in_hand_val,
        "open_proposals": open_props_val,
        "total_proposals": open_props_val,
        "summary_cards": summary_cards,
        "billing_revenue_gp_table": rows,
        "receivable_aging_table": receivable_rows,
        "gp_performance_by_service_line": gp_rows,
        "filters_applied": filters_applied,
        "period": period,
        "navigate_to": "/projects/reports/kpi-summary-report",
    }
def _build_kpi_narrative(kpi_payload: dict, filters_applied: dict, period: dict) -> str:
    """
    Build a rich, accurate narrative from the real KPI payload.
    ALL numbers are sourced from the CRM backend /api/v1/reports/kpi-summary-report
    and /api/v1/reports/receivable-summary-report endpoints.

    Formulas mirror kpiReportUseCase.ts exactly:
      - GTI Expenses       = invoice_revenue × 0.015
      - Staff Cost         = net_payroll × 0.0125
      - Gross Profit       = Revenue - Credit Notes - GTI - Staff Cost - Referral - Consultancy - Debt & Discounts
      - Total Direct Cost  = Debt + Staff + GTI + Consultancy + Referral
      - Variance           = Actual Revenue (net of credit notes) - Target Revenue
      - Secured Business   = Actual Revenue (net) + Project Approved Fees
      - Balance to Achieve = Total Target - Secured Business
    """
    def n(val, default=0.0):
        try:
            if isinstance(val, str):
                cleaned = val.replace("BHD", "").replace(",", "").replace("%", "").strip()
                return float(cleaned)
            return float(val)
        except Exception:
            return default

    def fmt(val):
        try:
            f = float(val)
            if f == 0.0:
                return "—"
            return f"BHD {f:,.3f}"
        except Exception:
            return "—"

    def fmt_nonzero(val):
        """Always format, even zero."""
        try:
            return f"BHD {float(val):,.3f}"
        except Exception:
            return "BHD 0.000"

    def sign_fmt(val):
        """Format with + prefix for positive, keeps minus for negative."""
        try:
            f = float(val)
            prefix = "+" if f >= 0 else ""
            return f"{prefix}BHD {f:,.3f}"
        except Exception:
            return "BHD 0.000"

    lines = []

    # ── Filters header ─────────────────────────────────────────────────────
    filter_parts = []
    SKIP_FILTER_KEYS = {"service_line_id", "department_id", "employee_id", "customer_id", "date_range", "financial_year"}
    for k, v in (filters_applied or {}).items():
        if v is not None and k not in SKIP_FILTER_KEYS:
            label = k.replace("_id", "").replace("_", " ").title()
            if str(v).strip().lower() not in ("all", "none", ""):
                filter_parts.append(f"**{label}:** {v}")

    # Build period string from actual dates, prettifying single-month ranges
    raw_date_range = (filters_applied or {}).get("date_range")
    period_str = _pretty_date_range(raw_date_range) if raw_date_range else (
        f"{period.get('start_date', '')} → {period.get('end_date', '')}" if period else "Current FY"
    )
    fy = (filters_applied or {}).get("financial_year")
    if fy and str(fy).strip().lower() not in ("all", "none", ""):
        filter_parts.insert(0, f"**FY:** {fy}")

    filter_str = "  |  ".join(filter_parts) if filter_parts else "All (No Filters Applied)"

    lines.append("## 📊 KPI Summary Report")
    lines.append(f"**Period:** {period_str}")
    if filter_parts:
        lines.append(filter_str)
    lines.append("")

    # ── Section 1: 7 KPI Summary Cards ────────────────────────────────────
    cards_map = {c["key"]: c["value"] for c in (kpi_payload.get("summary_cards") or [])}
    total_revenue = n(cards_map.get("total_revenue"))
    target_revenue = n(cards_map.get("target_revenue"))
    budget_vs_actual_rev = n(cards_map.get("budget_vs_actual_revenue"))
    budget_vs_actual_gp = n(cards_map.get("budget_vs_actual_gp"))
    gross_profit = n(cards_map.get("gross_profit"))
    open_proposals = n(cards_map.get("open_proposals"))
    project_in_hand = n(cards_map.get("project_in_hand"))
    secured_business = n(cards_map.get("secured_business"))
    balance_to_achieve = n(cards_map.get("balance_to_achieve"))
    receivables = n(cards_map.get("total_receivables"))
    target_gp = n(cards_map.get("target_gp"))

    # Fallback to total row in billing_revenue_gp_table if summary cards are empty/partial
    total_row = next((r for r in (kpi_payload.get("billing_revenue_gp_table") or [])
                      if str(r.get("month", "")).lower() == "total"), {})
    if total_row:
        if cards_map.get("target_revenue") is None:
            target_revenue = n(total_row.get("target_value"))
        if cards_map.get("total_revenue") is None:
            total_revenue = n(total_row.get("total_invoice_amount_with_credit"))
        if cards_map.get("budget_vs_actual_revenue") is None:
            budget_vs_actual_revenue = n(total_row.get("variance")) or (total_revenue - target_revenue)
        if cards_map.get("gross_profit") is None:
            gross_profit = n(total_row.get("total_gross_profit"))
        if cards_map.get("target_gp") is None:
            target_gp = n(total_row.get("target_gp"))

    if cards_map.get("balance_to_achieve") is None and target_revenue > secured_business:
        balance_to_achieve = target_revenue - secured_business

    # Budget vs Actual Revenue card — red if negative
    rev_variance_indicator = "🔴" if budget_vs_actual_rev < 0 else "🟢"
    gp_variance_indicator = "🟢" if budget_vs_actual_gp >= 0 else "🔴"

    lines.append("### 📌 Summary KPI Cards")
    lines.append("")
    lines.append("| KPI | Value | Status |")
    lines.append("|---|---:|:---:|")
    lines.append(f"| **Budget vs Actual Revenue** | {sign_fmt(budget_vs_actual_rev)} | {rev_variance_indicator} |")
    lines.append(f"| **Budget vs Actual GP** | {sign_fmt(budget_vs_actual_gp)} | {gp_variance_indicator} |")
    lines.append(f"| **Project in Hand** | {fmt(project_in_hand)} | — |")
    lines.append(f"| **Open Proposals** | {fmt(open_proposals)} | — |")
    lines.append(f"| **Secured Business** | {fmt(secured_business)} | — |")
    lines.append(f"| **Balance to Achieve** | {fmt(balance_to_achieve)} | {'🔴' if n(balance_to_achieve) > 0 else '🟢'} |")
    lines.append(f"| **Total Receivables** | {fmt(receivables)} | — |")
    lines.append("")

    # ── Section 2: Monthly Billing Revenue & GP Table (highlights only) ───
    billing_rows = [r for r in (kpi_payload.get("billing_revenue_gp_table") or [])
                    if str(r.get("month", "")).lower() != "total"]

    worst_month, worst_variance = None, None
    best_month, best_variance = None, None

    if billing_rows:
        lines.append("### 📈 Billing Revenue & GP — Monthly Highlights")
        lines.append("")
        lines.append("| Month | Target Rev | Actual Rev | Variance | Gross Profit | Direct Cost |")
        lines.append("|---|---:|---:|---:|---:|---:|")

        for row in billing_rows:
            month = row.get("month", "")
            target = n(row.get("target_value"))
            actual = n(row.get("total_invoice_amount_with_credit"))
            variance = n(row.get("variance"))
            gp = n(row.get("total_gross_profit"))
            dc = n(row.get("total_direct_cost"))

            if actual > 0 or target > 0:
                if worst_variance is None or variance < worst_variance:
                    worst_variance = variance
                    worst_month = month
                if best_variance is None or variance > best_variance:
                    best_variance = variance
                    best_month = month

            var_icon = "🔴" if variance < 0 else ("🟢" if variance > 0 else "—")
            if actual == 0 and target == 0:
                continue
            lines.append(
                f"| {month} | {fmt(target)} | {fmt(actual)} | {var_icon} {sign_fmt(variance)} | {fmt(gp)} | {fmt(dc)} |"
            )

        total_row = next((r for r in (kpi_payload.get("billing_revenue_gp_table") or [])
                          if str(r.get("month", "")).lower() == "total"), {})
        if total_row:
            t_target = n(total_row.get("target_value"))
            t_actual = n(total_row.get("total_invoice_amount_with_credit"))
            t_var = n(total_row.get("variance"))
            t_gp = n(total_row.get("total_gross_profit"))
            t_dc = n(total_row.get("total_direct_cost"))
            var_icon = "🔴" if t_var < 0 else "🟢"
            lines.append(
                f"| **YTD Total** | **{fmt(t_target)}** | **{fmt(t_actual)}** | {var_icon} **{sign_fmt(t_var)}** | **{fmt(t_gp)}** | **{fmt(t_dc)}** |"
            )
        lines.append("")

        if total_row:
            staff_cost_val      = n(total_row.get('total_staff_cost'))
            referral_val        = n(total_row.get('total_referral_cost'))
            gti_val             = n(total_row.get('gti_expenses'))
            consultancy_val     = n(total_row.get('total_direct_consultancy_cost'))
            debt_val            = n(total_row.get('total_discount_cost'))
            total_dc_val        = n(total_row.get('total_direct_cost'))
            # Only show breakdown section if at least one component has a value
            if any(v > 0 for v in [staff_cost_val, referral_val, gti_val, consultancy_val, debt_val]):
                lines.append("#### Direct Cost Breakdown (YTD)")
                lines.append("")
                lines.append("| Cost Component | Amount |")
                lines.append("|---|---:|")
                if staff_cost_val > 0:
                    lines.append(f"| Staff Cost | {fmt(staff_cost_val)} |")
                if referral_val > 0:
                    lines.append(f"| Referral Fees | {fmt(referral_val)} |")
                if gti_val > 0:
                    lines.append(f"| GTI Expenses (1.5% of Revenue) | {fmt(gti_val)} |")
                if consultancy_val > 0:
                    lines.append(f"| Direct Consultancy Fees | {fmt(consultancy_val)} |")
                if debt_val > 0:
                    lines.append(f"| Bad Debts, Discounts & Writeoffs | {fmt(debt_val)} |")
                lines.append(f"| **Total Direct Cost** | **{fmt(total_dc_val)}** |")
                lines.append("")
    else:
        lines.append("> ℹ️ No billing or target data found for the selected filters and period.")
        lines.append("")

    # ── Section 3: Receivable Aging Summary ───────────────────────────────
    aging_rows = kpi_payload.get("receivable_aging_table") or []
    aging_total = next((r for r in aging_rows if str(r.get("month", "")).lower() == "total"), {})

    risky_total = 0.0
    grand_total = 0.0
    if aging_total:
        lines.append("### 🧾 Receivable Summary (Aging Totals)")
        lines.append("")
        lines.append("| Aging Bucket | Total Outstanding |")
        lines.append("|---|---:|")
        bucket_keys = ["<30", "30-60", "60-120", "120-180", "180-365", ">365"]
        bucket_labels = ["< 30 Days", "30–60 Days", "60–120 Days", "120–180 Days", "180–365 Days", "> 365 Days"]
        for key, label in zip(bucket_keys, bucket_labels):
            val = n(aging_total.get(key))
            grand_total += val
            if key in ("120-180", "180-365", ">365"):
                risky_total += val
            risk_icon = " ⚠️" if key in ("120-180", "180-365", ">365") and val > 0 else ""
            if val > 0:
                lines.append(f"| {label}{risk_icon} | {fmt(val)} |")
        lines.append(f"| **Total Receivables** | **{fmt(grand_total)}** |")
        lines.append("")

    # ── Section 4: AI Summary ─────────────────────────────────────────────
    lines.append("---")
    lines.append("### 🎯 Executive Goal & Performance Status")
    lines.append("")

    if total_revenue > 0 and target_revenue > 0:
        rev_pct = (total_revenue / target_revenue) * 100
        if rev_pct >= 100:
            status_title = f"🚀 **ON TRACK / EXCEEDING GOAL** ({rev_pct:.1f}% of Target Achieved)"
            status_desc = f"Actual revenue of **{fmt(total_revenue)}** has exceeded the budget target of **{fmt(target_revenue)}** by **{fmt(budget_vs_actual_rev)}**."
        else:
            status_title = f"🔴 **BEHIND GOAL** ({rev_pct:.1f}% of Target Achieved)"
            status_desc = f"Actual revenue of **{fmt(total_revenue)}** is currently behind the budget target of **{fmt(target_revenue)}** with a shortfall of **{fmt(abs(budget_vs_actual_rev))}** (**{fmt(balance_to_achieve)}** remaining to reach full goal)."

        lines.append(f"- **Overall Goal Status:** {status_title}")
        lines.append(f"  {status_desc}")
        lines.append("")

    lines.append("### 🤖 Performance Breakdown & Metrics")
    lines.append("")

    if worst_month and worst_variance is not None and worst_variance < 0:
        lines.append(
            f"- **Highest Shortfall Month:** {worst_month} had the largest negative variance of {sign_fmt(worst_variance)} vs. target."
        )
    if best_month and best_variance is not None and best_variance > 0:
        lines.append(
            f"- **Top Performing Month:** {best_month} exceeded its monthly target by {sign_fmt(best_variance)}."
        )

    if gross_profit != 0 and target_gp != 0:
        gp_status = "above" if budget_vs_actual_gp >= 0 else "below"
        lines.append(
            f"- **Gross Profit (GP):** YTD GP of {fmt(gross_profit)} is {gp_status} the GP target ({fmt(target_gp)}) by {fmt(abs(budget_vs_actual_gp))}."
        )

    if aging_total and risky_total > 0:
        risky_pct = (risky_total / grand_total * 100) if grand_total > 0 else 0
        lines.append(
            f"- **Receivables Risk ⚠️:** {fmt(risky_total)} ({risky_pct:.1f}% of total receivables) is in the 120+ day aging buckets — "
            f"immediate follow-up is recommended."
        )
    elif aging_total:
        lines.append("- **Receivables:** No significant overdue receivables (>120 days) detected.")

    # Balance to achieve
    if n(balance_to_achieve) > 0:
        lines.append(
            f"- **Balance to Achieve:** {fmt(balance_to_achieve)} is still needed to reach the full-year revenue target."
        )
    else:
        lines.append("- **Target Status:** 🎉 Target fully achieved or surpassed for this period.")

    lines.append("")
    lines.append("*Data sourced directly from the CRM billing, payroll, and KPI master systems.*")

    return "\n".join(lines)


def _build_excel_export_from_kpi_payload(kpi_payload: dict, filters_applied: dict, period: dict) -> dict:
    """Build Excel export containing the full KPI report dataset across all sections."""
    from datetime import datetime
    import re as _re

    rows = []

    def format_num(val):
        """Round to integer — matches UI numeric rounding."""
        try:
            if val is None or val == "" or val == "-":
                return 0
            return int(round(float(val)))
        except Exception:
            return 0

    def format_curr(val):
        """Format BHD currency strings for display."""
        try:
            if val is None or val == "" or val == "-":
                return "BHD 0.00"
            f = float(val)
            return f"BHD {f:,.2f}"
        except Exception:
            return "BHD 0.00"

    # --- Pull billing total row & summary cards ---
    billing_data = kpi_payload.get("billing_revenue_gp_table", [])
    total_row = next((r for r in billing_data if str(r.get("month", "")).lower() == "total"), {})
    summary_cards = {c["key"]: c["value"] for c in kpi_payload.get("summary_cards", []) if isinstance(c, dict) and "key" in c}
    if not summary_cards and isinstance(kpi_payload.get("summary"), dict):
        summary_cards = kpi_payload.get("summary")

    target_revenue = summary_cards.get("target_revenue") if summary_cards.get("target_revenue") is not None else total_row.get("target_value", 0)
    total_revenue = summary_cards.get("total_revenue") if summary_cards.get("total_revenue") is not None else total_row.get("total_invoice_amount_with_credit", 0)
    budget_vs_actual_rev = summary_cards.get("budget_vs_actual_revenue") if summary_cards.get("budget_vs_actual_revenue") is not None else total_row.get("budget_vs_actual_revenu", 0)
    target_gp = summary_cards.get("target_gp") if summary_cards.get("target_gp") is not None else total_row.get("target_gp", 0)
    gross_profit = summary_cards.get("gross_profit") if summary_cards.get("gross_profit") is not None else total_row.get("total_gross_profit", 0)
    budget_vs_actual_gp = summary_cards.get("budget_vs_actual_gp") if summary_cards.get("budget_vs_actual_gp") is not None else total_row.get("budget_vs_actual_gp_percent", 0)
    open_proposals = summary_cards.get("open_proposals", 0)
    proposal_value = summary_cards.get("total_proposal_value", 0)
    project_in_hand = summary_cards.get("project_in_hand", 0)
    strictly_active_projects = summary_cards.get("strictly_active_projects", 0)
    secured_business = summary_cards.get("secured_business") if summary_cards.get("secured_business") is not None else kpi_payload.get("secured_business", total_revenue)
    balance_to_achieve = summary_cards.get("balance_to_achieve") if summary_cards.get("balance_to_achieve") is not None else kpi_payload.get("balance_to_achieve", (float(target_revenue or 0) - float(secured_business or 0)))
    receivables = summary_cards.get("total_receivables") if summary_cards.get("total_receivables") is not None else total_row.get("total_rem_amount", 0)
    receivables_180_plus = summary_cards.get("receivable_180_plus_days", 0)
    utilization = summary_cards.get("utilization", 0)

    # --- 1. Report Title & Filter Information ---
    rows.append(["Executive KPI Summary Report"])
    rows.append(["Period:", f"{period.get('start_date', '')} to {period.get('end_date', '')}"])
    rows.append(["Service Line:", (filters_applied or {}).get("service_line") or "All"])
    rows.append(["Department:", (filters_applied or {}).get("department") or "All"])
    rows.append(["Employee Name:", (filters_applied or {}).get("employee_name") or "All"])
    rows.append(["Customer Name:", (filters_applied or {}).get("customer") or "All"])
    rows.append(["Financial Year:", (filters_applied or {}).get("financial_year") or "All"])
    rows.append([])

    # --- 2. Financial Targets & Performance Table (Matches Chatbot UI Card) ---
    rows.append(["Financial Targets & Performance"])
    rows.append(["Metric", "Target Value", "Secured / Actual", "Balance to Achieve", "Achievement %"])

    f_target_rev = float(target_revenue or 0)
    f_secured = float(secured_business or 0)
    f_balance = float(balance_to_achieve or 0)
    f_target_gp = float(target_gp or 0)
    f_gp = float(gross_profit or 0)

    rev_achieve_pct = (f_secured / f_target_rev * 100) if f_target_rev > 0 else 0.0
    gp_achieve_pct = (f_gp / f_target_gp * 100) if f_target_gp > 0 else 0.0
    gp_balance = max(0.0, f_target_gp - f_gp) if f_target_gp > 0 else 0.0

    rows.append([
        "Revenue Target",
        format_curr(f_target_rev),
        format_curr(f_secured),
        format_curr(f_balance),
        f"{rev_achieve_pct:.1f}%"
    ])
    rows.append([
        "Gross Profit (GP) Target",
        format_curr(f_target_gp),
        format_curr(f_gp),
        format_curr(gp_balance),
        f"{gp_achieve_pct:.1f}%" if f_target_gp > 0 else "-"
    ])
    prop_label = f"{format_curr(proposal_value)} ({int(open_proposals)} proposals)" if int(open_proposals) > 0 else format_curr(proposal_value)
    rows.append([
        "Proposals Pipeline",
        "-",
        prop_label,
        "-",
        "-"
    ])
    rows.append([])

    # --- 3. Summary KPI Metrics Box ---
    rows.append(["Summary KPI Metrics"])
    rows.append(["KPI Metric", "Value", "Status / Indicator"])
    rows.append(["Budget vs Actual Revenue", format_num(budget_vs_actual_rev), "🔴 Shortfall" if float(budget_vs_actual_rev or 0) < 0 else "🟢 Exceeded"])
    rows.append(["Budget vs Actual GP", format_num(budget_vs_actual_gp), "🟢 Positive" if float(budget_vs_actual_gp or 0) >= 0 else "🔴 Shortfall"])
    rows.append(["Project in Hand", format_num(project_in_hand), "-"])
    rows.append(["Open Proposals", format_num(open_proposals), "-"])
    rows.append(["Secured Business", format_num(secured_business), "-"])
    rows.append(["Balance to Achieve", format_num(balance_to_achieve), "🔴 Pending" if float(balance_to_achieve or 0) > 0 else "🟢 Achieved"])
    rows.append(["Total Receivables", format_num(receivables), "-"])
    rows.append(["Overdue Receivables (180+ Days)", format_num(receivables_180_plus), "⚠️ Risk" if float(receivables_180_plus or 0) > 0 else "🟢 Clear"])
    rows.append(["Utilization", f"{format_num(utilization)}%", "-"])
    rows.append([])

    # --- 4. Projects Portfolio Overview ---
    rows.append(["Projects Portfolio Overview"])
    rows.append(["Portfolio Metric", "Count"])
    rows.append(["Total Projects Managed", format_num(project_in_hand)])
    rows.append(["Strictly Active Projects", format_num(strictly_active_projects)])
    rows.append([])

    # --- Generate ordered months list using calendar arithmetic ---
    MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def _parse_any_date(date_str):
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except Exception:
                pass
        return None

    def make_month_key(year, month):
        return "{}-{}".format(MONTH_ABBR[month - 1], year)

    months = []
    start_d = _parse_any_date(period.get("start_date", ""))
    end_d = _parse_any_date(period.get("end_date", ""))

    if start_d and end_d:
        y, m = start_d.year, start_d.month
        ey, em = end_d.year, end_d.month
        while (y, m) <= (ey, em):
            months.append(make_month_key(y, m))
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
    else:
        months = [r.get("month") for r in billing_data
                  if str(r.get("month", "")).lower() != "total" and r.get("month")]

    # --- Build monthly map ---
    billing_by_month = {}
    for r in billing_data:
        m_name = r.get("month")
        if m_name and str(m_name).lower() != "total":
            billing_by_month[m_name] = r

    def get_row_data(label, key):
        r_data = [label]
        row_sum = 0
        for m in months:
            val = format_num(billing_by_month.get(m, {}).get(key))
            r_data.append(val)
            row_sum += val
        total_val = format_num(total_row.get(key)) if total_row.get(key) is not None else row_sum
        r_data.append(total_val)
        return r_data

    # --- 5. Monthly Billing Revenue & GP Table ---
    rows.append(["Monthly Billing Revenue & GP Performance"])
    headers_billing = ["Metric / Month"] + months + ["Total"]
    rows.append(headers_billing)

    rows.append(get_row_data("Target Value (Revenue)", "target_value"))
    rows.append(get_row_data("Invoiced Revenue (Gross)", "total_invoice_amount_with_credit"))
    rows.append(get_row_data("GTI Expenses (1.5%)", "gti_expense"))
    rows.append(get_row_data("Staff Cost (1.25%)", "staff_cost"))
    rows.append(get_row_data("Referral Fee", "total_referral_fee"))
    rows.append(get_row_data("Consultancy Fee", "total_consultancy_fee"))
    rows.append(get_row_data("Debt & Discount", "total_discount_cost"))
    rows.append(get_row_data("Total Direct Cost", "total_direct_cost"))
    rows.append(get_row_data("Gross Profit (GP)", "total_gross_profit"))

    var_row = ["Variance (Revenue)"]
    for m in months:
        r = billing_by_month.get(m, {})
        v = r.get("variance")
        var_row.append(format_num(v) if v is not None else format_num(r.get("actual_revenue", 0)) - format_num(r.get("target_value", 0)))
    var_total = total_row.get("variance")
    var_row.append(format_num(var_total) if var_total is not None else format_num(total_row.get("actual_revenue", 0)) - format_num(total_row.get("target_value", 0)))
    rows.append(var_row)

    gp_var_row = ["Variance GP"]
    for m in months:
        r = billing_by_month.get(m, {})
        v = r.get("variance_gp")
        gp_var_row.append(format_num(v) if v is not None else format_num(r.get("total_gross_profit", 0)) - format_num(r.get("target_gp", 0)))
    gp_var_total = total_row.get("variance_gp")
    gp_var_row.append(format_num(gp_var_total) if gp_var_total is not None else format_num(total_row.get("total_gross_profit", 0)) - format_num(total_row.get("target_gp", 0)))
    rows.append(gp_var_row)
    rows.append([])

    # --- 6. GP Performance by Service Line Table ---
    gp_data = kpi_payload.get("gp_performance_by_service_line", [])
    if gp_data:
        rows.append(["Gross Profit Performance by Service Line"])
        rows.append(["Service Line", "Service Line ID", "Target Value", "Actual Invoiced Revenue", "Direct Cost", "Gross Profit", "GP %"])
        for r in gp_data:
            s_name = r.get("service_line_name") or r.get("name") or "Service Line"
            s_id = r.get("service_line_id") or "-"
            s_target = format_num(r.get("target_value") or r.get("target"))
            s_act = format_num(r.get("total_invoice_amount_with_credit") or r.get("actual_revenue") or r.get("invoiced_rev"))
            s_cost = format_num(r.get("total_direct_cost") or r.get("direct_cost"))
            s_gp = format_num(r.get("total_gross_profit") or r.get("gross_profit"))
            s_gp_pct = f"{format_num(r.get('gp_percent') or r.get('budget_vs_actual_gp_percent'))}%"
            rows.append([s_name, s_id, s_target, s_act, s_cost, s_gp, s_gp_pct])
        rows.append([])

    # --- 7. Monthly Receivable Aging Table ---
    aging_data = kpi_payload.get("receivable_aging_table", [])
    if aging_data:
        aging_total_row = next((r for r in aging_data if str(r.get("month", "")).lower() == "total"), {})
        rows.append(["Monthly Receivable Aging Summary"])
        headers_aging = ["Aging Bucket / Month"] + months + ["Total"]
        rows.append(headers_aging)

        AGING_KEYS = ["<30", "30-60", "60-120", "120-180", "180-365", ">365"]

        _MMAP = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        def _ym(s):
            s = str(s).strip().lower()
            m = _re.match(r'([a-z]+)\W+(\d{4})', s)
            if m:
                mn = _MMAP.get(m.group(1)[:3])
                if mn: return (int(m.group(2)), mn)
            m = _re.match(r'(\d{4})\W+(\d{1,2})', s)
            if m: return (int(m.group(1)), int(m.group(2)))
            return None

        aging_ym = {}
        for _r in aging_data:
            _ms = _r.get("month", "")
            if str(_ms).lower() == "total": continue
            _k = _ym(_ms)
            if _k: aging_ym[_k] = _r

        def get_aging_row(label, key):
            r_data = [label]
            row_sum = 0
            for m in months:
                mrow = aging_ym.get(_ym(m), {})
                val = format_num(mrow.get(key))
                r_data.append(val)
                row_sum += val
            total_val = format_num(aging_total_row.get(key)) if aging_total_row.get(key) is not None else row_sum
            r_data.append(total_val)
            return r_data

        rows.append(get_aging_row("< 30 days",      "<30"))
        rows.append(get_aging_row("30 - 60 days",   "30-60"))
        rows.append(get_aging_row("60 - 120 days",  "60-120"))
        rows.append(get_aging_row("120 - 180 days", "120-180"))
        rows.append(get_aging_row("180 - 365 days", "180-365"))
        rows.append(get_aging_row("> 365 days",     ">365"))

        grand_r = ["Total"]
        for m in months:
            mrow = aging_ym.get(_ym(m), {})
            grand_r.append(sum(format_num(mrow.get(k)) for k in AGING_KEYS))
        grand_r.append(sum(format_num(aging_total_row.get(k)) for k in AGING_KEYS))
        rows.append(grand_r)
        rows.append([])
        rows.append([])

    # --- 8. Executive Goal & Performance Status Insights ---
    narrative_text = _build_kpi_narrative(kpi_payload, filters_applied, period)
    rows.append(["Executive Goal & Performance Status"])
    for line in narrative_text.splitlines():
        clean_line = line.replace("#", "").replace("**", "").replace("---", "").strip()
        if clean_line:
            rows.append([clean_line])

    fy_str = str((filters_applied or {}).get("financial_year") or "FY").replace("-", "_")
    cust_str = str((filters_applied or {}).get("customer") or "").replace(" ", "_")

    filename = f"KPI_Report_{fy_str}"
    if cust_str and cust_str.lower() != "all":
        filename += f"_{cust_str}"

    return {
        "filename": filename,
        "sheets": [{"name": "KPI Report", "headers": [], "rows": rows}]
    }


# NOTE: _classify_intent has been moved to the centralized intent_classifier module
# Use: intent = await classify_intent(question)
# This ensures consistent intent detection across all project files


async def _deterministic_kpi_response(history: Optional[List[dict]], latest_question: str, user_ctx: Optional[dict], auth_token: Optional[str]):
    # Intelligently classify user intent using LLM
    # This is more robust than keyword matching and handles unseen questions gracefully
    intent = await classify_intent(latest_question)
    
    # If user is NOT asking for a KPI report, skip KPI processing entirely
    if not should_show_kpi_filters(intent):
        return None
    
    # Check filters from BOTH the current question AND history.
    # The KpiFilterPanel sends all filters inline in the question text itself,
    # so we must also check the question — not just history.
    current_filters = _extract_kpi_filters_from_text(latest_question)
    history_filters = _extract_kpi_filters_from_history(history)
    merged_filters = _merge_kpi_filters(current_filters, history_filters)
    
    # DEBUG — remove after confirming fix
    import logging
    logging.getLogger("uvicorn").info(f"[KPI DEBUG] latest_question[:80]={latest_question[:80]!r}")
    logging.getLogger("uvicorn").info(f"[KPI DEBUG] current_filters={current_filters}")
    logging.getLogger("uvicorn").info(f"[KPI DEBUG] history_filters={history_filters}")
    logging.getLogger("uvicorn").info(f"[KPI DEBUG] merged_filters={merged_filters}")

    # Auto-fill defaults if user explicitly requested report generation or provided any entity filter
    q_low = latest_question.lower()
    has_direct_action = any(w in q_low for w in ["generate", "download", "show", "run", "get", "create", "export"])
    has_any_filter = merged_filters and any(v for v in merged_filters.values() if v and str(v).lower() != "all")

    if has_direct_action or has_any_filter:
        if not merged_filters.get("financial_year"):
            merged_filters["financial_year"] = "2025-2026"
        if not merged_filters.get("date_range"):
            merged_filters["date_range"] = "01-10-2025 to 30-09-2026"
        if not merged_filters.get("service_line"):
            merged_filters["service_line"] = "All"
        if not merged_filters.get("department"):
            merged_filters["department"] = "All"
        if not merged_filters.get("employee_name"):
            merged_filters["employee_name"] = "All"

    if not merged_filters or not any(v for v in merged_filters.values() if v):
        return {
            "answer": _format_kpi_filter_setup(),
            "navigate_to": "/projects/reports/kpi-summary-report",
            "navigation_links": [{"label": "KPI Summary Report", "url": "/projects/reports/kpi-summary-report"}],
            "suggested_questions": ["Show KPI summary report", "Show KPI summary for Audit", "Show KPI summary for 2025-2026"],
            "report_intent": "kpi_summary",
            "kpi_payload": None,
            "chart_data": None,
            "export_data": None,
            "auto_expand": False,
        }


    service_line_name = merged_filters.get("service_line")
    department_name = merged_filters.get("department")
    employee_name = merged_filters.get("employee_name")
    customer_name = merged_filters.get("customer")
    date_range_value = merged_filters.get("date_range")
    financial_year_value = merged_filters.get("financial_year")

    start_date = None
    end_date = None
    if date_range_value and str(date_range_value).lower() != "all":
        range_match = re.search(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4}).*?(\d{1,2}[-/]\d{1,2}[-/]\d{4})", str(date_range_value))
        if range_match:
            start_date = range_match.group(1).replace("/", "-")
            end_date = range_match.group(2).replace("/", "-")
    if not start_date or not end_date:
        fy_match = re.search(r"(20\d{2})\s*[-/]\s*(20\d{2})", str(financial_year_value or ""))
        if fy_match:
            start_date = f"01-10-{fy_match.group(1)}"
            end_date = f"30-09-{fy_match.group(2)}"
    if not start_date or not end_date:
        start_date, end_date = _resolve_date_window(latest_question)

    def _maybe_lookup(table: str, name_col: str, value: Optional[str]) -> Optional[int]:
        if not value or str(value).strip().lower() in ("all", "-"):
            return None
        return _lookup_single_id(table, "id", name_col, value)[0]

    service_line_id = _maybe_lookup("m_serviceline", "name", service_line_name)
    department_id = _maybe_lookup("m_department", "name", department_name)
    employee_id = _maybe_lookup("employees", "employee_name", employee_name)
    customer_id = _maybe_lookup("customers", "customer_name", customer_name)

    # If employee is found, ensure we have the exact service line and department from KPI master
    # This prevents issues where duplicate department names cause _maybe_lookup to return None
    if employee_id is not None:
        try:
            with get_db_engine().connect() as conn:
                row = conn.execute(
                    text("SELECT service_line_id, department_id FROM kpi_master WHERE employee_id = :eid LIMIT 1"),
                    {"eid": employee_id}
                ).fetchone()
                if row:
                    if not service_line_id and row.service_line_id:
                        service_line_id = row.service_line_id
                    if not department_id and row.department_id:
                        department_id = row.department_id
        except Exception:
            pass

    filters_applied = {
        "service_line": service_line_name,
        "department": department_name,
        "employee_name": employee_name,
        "customer": customer_name,
        "financial_year": financial_year_value,
        "date_range": date_range_value,
        "service_line_id": service_line_id,
        "department_id": department_id,
        "employee_id": employee_id,
        "customer_id": customer_id,
    }
    period = {"start_date": start_date, "end_date": end_date}

    def _build_excel_export_from_kpi_payload(kpi_payload: dict, filters_applied: dict, period: dict) -> dict:
        """Build Excel export containing the full KPI report dataset across all sections."""
        from datetime import datetime
        import re as _re

        rows = []

        def format_num(val):
            """Round to integer — matches UI numeric rounding."""
            try:
                if val is None or val == "" or val == "-":
                    return 0
                return int(round(float(val)))
            except Exception:
                return 0

        def format_curr(val):
            """Format BHD currency strings for display."""
            try:
                if val is None or val == "" or val == "-":
                    return "BHD 0.00"
                f = float(val)
                return f"BHD {f:,.2f}"
            except Exception:
                return "BHD 0.00"

        # --- Pull billing total row & summary cards ---
        billing_data = kpi_payload.get("billing_revenue_gp_table", [])
        total_row = next((r for r in billing_data if str(r.get("month", "")).lower() == "total"), {})
        summary_cards = {c["key"]: c["value"] for c in kpi_payload.get("summary_cards", [])}

        target_revenue = summary_cards.get("target_revenue") if summary_cards.get("target_revenue") is not None else total_row.get("target_value", 0)
        total_revenue = summary_cards.get("total_revenue") if summary_cards.get("total_revenue") is not None else total_row.get("total_invoice_amount_with_credit", 0)
        budget_vs_actual_rev = summary_cards.get("budget_vs_actual_revenue") if summary_cards.get("budget_vs_actual_revenue") is not None else total_row.get("budget_vs_actual_revenu", 0)
        target_gp = summary_cards.get("target_gp") if summary_cards.get("target_gp") is not None else total_row.get("target_gp", 0)
        gross_profit = summary_cards.get("gross_profit") if summary_cards.get("gross_profit") is not None else total_row.get("total_gross_profit", 0)
        budget_vs_actual_gp = summary_cards.get("budget_vs_actual_gp") if summary_cards.get("budget_vs_actual_gp") is not None else total_row.get("budget_vs_actual_gp_percent", 0)
        open_proposals = summary_cards.get("open_proposals", 0)
        proposal_value = summary_cards.get("total_proposal_value", 0)
        project_in_hand = summary_cards.get("project_in_hand", 0)
        strictly_active_projects = summary_cards.get("strictly_active_projects", 0)
        secured_business = summary_cards.get("secured_business") if summary_cards.get("secured_business") is not None else total_revenue
        balance_to_achieve = summary_cards.get("balance_to_achieve") if summary_cards.get("balance_to_achieve") is not None else (float(target_revenue or 0) - float(secured_business or 0))
        receivables = summary_cards.get("total_receivables") if summary_cards.get("total_receivables") is not None else total_row.get("total_rem_amount", 0)
        receivables_180_plus = summary_cards.get("receivable_180_plus_days", 0)
        utilization = summary_cards.get("utilization", 0)

        # --- 1. Report Title & Filter Information ---
        emp_title = (filters_applied or {}).get("employee_name") or "Organization Aggregate"
        rows.append(["Executive KPI Summary Report", emp_title])

        raw_date_range = (filters_applied or {}).get("date_range")
        period_str = _pretty_date_range(raw_date_range) if raw_date_range else (
            f"{period.get('start_date', '')} to {period.get('end_date', '')}" if period else "Current FY"
        )
        rows.append(["Report Period:", period_str])
        rows.append(["Service Line:", (filters_applied or {}).get("service_line") or "All"])
        rows.append(["Department:", (filters_applied or {}).get("department") or "All"])
        rows.append(["Employee Name:", (filters_applied or {}).get("employee_name") or "All"])
        rows.append(["Customer Name:", (filters_applied or {}).get("customer") or "All"])
        rows.append(["Financial Year:", (filters_applied or {}).get("financial_year") or "All"])
        rows.append([])

        # --- 2. Financial Targets & Performance Table (Matches Chatbot UI Card) ---
        rows.append(["Financial Targets & Performance"])
        rows.append(["Metric", "Target Value", "Secured / Actual", "Balance to Achieve", "Achievement %"])

        f_target_rev = float(target_revenue or 0)
        f_secured = float(secured_business or 0)
        f_balance = float(balance_to_achieve or 0)
        f_target_gp = float(target_gp or 0)
        f_gp = float(gross_profit or 0)

        rev_achieve_pct = (f_secured / f_target_rev * 100) if f_target_rev > 0 else 0.0
        gp_achieve_pct = (f_gp / f_target_gp * 100) if f_target_gp > 0 else 0.0
        gp_balance = max(0.0, f_target_gp - f_gp) if f_target_gp > 0 else 0.0

        rows.append([
            "Revenue Target",
            format_curr(f_target_rev),
            format_curr(f_secured),
            format_curr(f_balance),
            f"{rev_achieve_pct:.1f}%"
        ])
        rows.append([
            "Gross Profit (GP) Target",
            format_curr(f_target_gp),
            format_curr(f_gp),
            format_curr(gp_balance),
            f"{gp_achieve_pct:.1f}%" if f_target_gp > 0 else "-"
        ])
        prop_label = f"{format_curr(proposal_value)} ({int(open_proposals)} proposals)" if int(open_proposals) > 0 else format_curr(proposal_value)
        rows.append([
            "Proposals Pipeline",
            "-",
            prop_label,
            "-",
            "-"
        ])
        rows.append([])

        # --- 3. Summary KPI Metrics Box ---
        rows.append(["Summary KPI Metrics"])
        rows.append(["KPI Metric", "Value", "Status / Indicator"])
        rows.append(["Budget vs Actual Revenue", format_num(budget_vs_actual_rev), "🔴 Shortfall" if float(budget_vs_actual_rev or 0) < 0 else "🟢 Exceeded"])
        rows.append(["Budget vs Actual GP", format_num(budget_vs_actual_gp), "🟢 Positive" if float(budget_vs_actual_gp or 0) >= 0 else "🔴 Shortfall"])
        rows.append(["Project in Hand", format_num(project_in_hand), "-"])
        rows.append(["Open Proposals", format_num(open_proposals), "-"])
        rows.append(["Secured Business", format_num(secured_business), "-"])
        rows.append(["Balance to Achieve", format_num(balance_to_achieve), "🔴 Pending" if float(balance_to_achieve or 0) > 0 else "🟢 Achieved"])
        rows.append(["Total Receivables", format_num(receivables), "-"])
        rows.append(["Overdue Receivables (180+ Days)", format_num(receivables_180_plus), "⚠️ Risk" if float(receivables_180_plus or 0) > 0 else "🟢 Clear"])
        rows.append(["Utilization", f"{format_num(utilization)}%", "-"])
        rows.append([])

        # --- 4. Projects Portfolio Overview ---
        rows.append(["Projects Portfolio Overview"])
        rows.append(["Portfolio Metric", "Count"])
        rows.append(["Total Projects Managed", format_num(project_in_hand)])
        rows.append(["Strictly Active Projects", format_num(strictly_active_projects)])
        rows.append([])

        # --- Generate ordered months list using calendar arithmetic ---
        MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        def _parse_any_date(date_str):
            for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%m-%d-%Y"):
                try:
                    return datetime.strptime(date_str, fmt)
                except Exception:
                    pass
            return None

        def make_month_key(year, month):
            return "{}-{}".format(MONTH_ABBR[month - 1], year)

        months = []
        start_d = _parse_any_date(period.get("start_date", ""))
        end_d = _parse_any_date(period.get("end_date", ""))

        if start_d and end_d:
            y, m = start_d.year, start_d.month
            ey, em = end_d.year, end_d.month
            while (y, m) <= (ey, em):
                months.append(make_month_key(y, m))
                if m == 12:
                    y, m = y + 1, 1
                else:
                    m += 1
        else:
            months = [r.get("month") for r in billing_data
                      if str(r.get("month", "")).lower() != "total" and r.get("month")]

        def short_month(m_key):
            parts = m_key.split("-")
            if len(parts) == 2 and len(parts[1]) == 4:
                return "{}-{}".format(parts[0], parts[1][2:])
            return m_key

        display_months = [short_month(m) for m in months]

        # --- 5. Billing Revenue & GP Performance Table ---
        rows.append(["Billing Revenue & GP Performance"])
        rows.append(["Particulars"] + display_months + ["YTD Bud vs Act.", "Total"])

        def get_billing_row(label, month_key, ytd_key, total_key):
            r_data = [label]
            for m in months:
                mrow = next((r for r in billing_data if r.get("month") == m), {})
                r_data.append(format_num(mrow.get(month_key)))
            r_data.append(format_num(total_row.get(ytd_key)))
            r_data.append(format_num(total_row.get(total_key)))
            return r_data

        rows.append(get_billing_row("Budgeted Revenue",               "target_all_value",                "target_value",                    "target_all_value"))
        rows.append(get_billing_row("Actual Revenue",                 "total_invoice_amount_with_credit","total_invoice_amount_with_credit", "total_invoice_amount_with_credit"))
        rows.append(get_billing_row("Variance",                       "variance",                        "variance",                        "variance_all"))
        rows.append(get_billing_row("Total Direct Cost",              "total_direct_cost",               "total_direct_cost",               "total_direct_cost"))
        rows.append(get_billing_row("Staff Cost",                     "staff_cost",                      "total_staff_cost",                "total_staff_cost"))
        rows.append(get_billing_row("Referral Fee(Cost)",             "referral_cost",                   "total_referral_cost",             "total_referral_cost"))
        rows.append(get_billing_row("GTI Expenses",                   "gti_expenses",                    "gti_expenses",                    "gti_expenses"))
        rows.append(get_billing_row("Direct Consultancy Fees",        "direct_consultancy_cost",         "total_direct_consultancy_cost",   "total_direct_consultancy_cost"))
        rows.append(get_billing_row("Bad debts, Discount & Writeoff", "debt_discount",                   "total_discount_cost",             "total_discount_cost"))
        rows.append(get_billing_row("Gross Profit",                   "total_gross_profit",              "total_gross_profit",              "total_gross_profit"))

        rows.append([])
        rows.append([])

        # --- 6. GP Performance by Service Line / Department ---
        gp_sl_data = kpi_payload.get("gp_performance_by_service_line", [])
        if gp_sl_data:
            rows.append(["GP & Performance by Service Line / Department"])
            rows.append(["Service Line / Department", "Target Revenue", "Actual Revenue", "Variance", "Gross Profit"])
            for sl_item in gp_sl_data:
                rows.append([
                    sl_item.get("name") or sl_item.get("service_line") or sl_item.get("department", "Unknown"),
                    format_num(sl_item.get("target_rev") or sl_item.get("target_value", 0)),
                    format_num(sl_item.get("actual_rev") or sl_item.get("total_invoice_amount_with_credit", 0)),
                    format_num(sl_item.get("variance", 0)),
                    format_num(sl_item.get("gross_profit") or sl_item.get("total_gross_profit", 0))
                ])
            rows.append([])
            rows.append([])

        # --- 7. Receivable Summary Report ---
        rows.append(["Receivable Summary Report"])

        aging_data = kpi_payload.get("receivable_aging_table", [])
        aging_total_row = next((r for r in aging_data if str(r.get("month", "")).lower() == "total"), {})

        rows.append(["Receivables Day"] + display_months + ["Total"])

        AGING_KEYS = ["<30", "30-60", "60-120", "120-180", "180-365", ">365"]

        _MMAP = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
        def _ym(s):
            s = str(s).strip().lower()
            m = _re.match(r'([a-z]+)\W+(\d{4})', s)
            if m:
                mn = _MMAP.get(m.group(1)[:3])
                if mn: return (int(m.group(2)), mn)
            m = _re.match(r'(\d{4})\W+(\d{1,2})', s)
            if m: return (int(m.group(1)), int(m.group(2)))
            return None

        aging_ym = {}
        for _r in aging_data:
            _ms = _r.get("month", "")
            if str(_ms).lower() == "total": continue
            _k = _ym(_ms)
            if _k: aging_ym[_k] = _r

        def get_aging_row(label, key):
            r_data = [label]
            row_sum = 0
            for m in months:
                mrow = aging_ym.get(_ym(m), {})
                val = format_num(mrow.get(key))
                r_data.append(val)
                row_sum += val
            total_val = format_num(aging_total_row.get(key)) if aging_total_row.get(key) is not None else row_sum
            r_data.append(total_val)
            return r_data

        rows.append(get_aging_row("< 30 days",      "<30"))
        rows.append(get_aging_row("30 - 60 days",   "30-60"))
        rows.append(get_aging_row("60 - 120 days",  "60-120"))
        rows.append(get_aging_row("120 - 180 days", "120-180"))
        rows.append(get_aging_row("180 - 365 days", "180-365"))
        rows.append(get_aging_row("> 365 days",     ">365"))

        grand_r = ["Total"]
        for m in months:
            mrow = aging_ym.get(_ym(m), {})
            grand_r.append(sum(format_num(mrow.get(k)) for k in AGING_KEYS))
        grand_r.append(sum(format_num(aging_total_row.get(k)) for k in AGING_KEYS))
        rows.append(grand_r)
        rows.append([])
        rows.append([])

        # --- 8. Executive Goal & Performance Status Insights ---
        narrative_text = _build_kpi_narrative(kpi_payload, filters_applied, period)
        rows.append(["Executive Goal & Performance Status"])
        for line in narrative_text.splitlines():
            clean_line = line.replace("#", "").replace("**", "").replace("---", "").strip()
            if clean_line:
                rows.append([clean_line])

        fy_str = str((filters_applied or {}).get("financial_year") or "FY").replace("-", "_")
        cust_str = str((filters_applied or {}).get("customer") or "").replace(" ", "_")

        filename = f"KPI_Report_{fy_str}"
        if cust_str and cust_str.lower() != "all":
            filename += f"_{cust_str}"

        return {
            "filename": filename,
            "sheets": [{"name": "KPI Report", "headers": [], "rows": rows}]
        }

    base = os.getenv("CRM_API_BASE", "http://localhost:3001/api/v1").rstrip("/")

    # Convert dates to YYYY-MM-DD so dayjs/Node.js parses them correctly.
    # If left as DD-MM-YYYY, dayjs interprets "01-10-2025" as January 10 2025
    # which causes the aging monthly loop to start from Jan-2025 instead of Oct-2025.
    def _to_iso(date_str):
        from datetime import datetime
        for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            except Exception:
                pass
        return date_str  # fallback: send as-is

    params = {"start_date": _to_iso(start_date), "end_date": _to_iso(end_date)}
    for k, v in {
        "service_line_id": service_line_id,
        "department_id": department_id,
        "employee_id": employee_id,
        "customer_id": customer_id,
    }.items():
        if v is not None:
            params[k] = v

    kpi_url = f"{base}/reports/kpi-summary-report?{urlencode(params)}"
    aging_url = f"{base}/reports/receivable-summary-report?{urlencode(params)}"

    try:
        kpi_data = await asyncio.to_thread(_call_json_api, kpi_url, auth_token)
        aging_data = await asyncio.to_thread(_call_json_api, aging_url, auth_token)

        import logging
        logger = logging.getLogger("uvicorn")
        logger.info(f"[1. RAW CRM API RESPONSE] Top keys: {list((kpi_data or {}).keys())}")
        logger.info(f"[1. RAW CRM API TOP FIELDS] secured_business={(kpi_data or {}).get('secured_business')}, balance_to_achieve={(kpi_data or {}).get('balance_to_achieve')}, total_projects={(kpi_data or {}).get('total_projects')}, budget_vs_actual_gp={(kpi_data or {}).get('budget_vs_actual_gp')}")

        kpi_payload = _build_kpi_contract(kpi_data, aging_data, filters_applied, period)

        export_data = _build_excel_export_from_kpi_payload(kpi_payload, filters_applied, period)

        answer = _build_kpi_narrative(kpi_payload, filters_applied, period)

        # Build navigate_to URL with filter query params so KPI report page pre-fills them.
        # Only include IDs that are actually set (i.e. not 'All').
        nav_params = {}
        if service_line_id is not None:
            nav_params["service_line_id"] = service_line_id
        if department_id is not None:
            nav_params["department_id"] = department_id
        if employee_id is not None:
            nav_params["employee_id"] = employee_id
        if customer_id is not None:
            nav_params["customer_id"] = customer_id
        if start_date:
            nav_params["start_date"] = _to_iso(start_date)
        if end_date:
            nav_params["end_date"] = _to_iso(end_date)

        nav_path = "/projects/reports/kpi-summary-report"
        if nav_params:
            nav_path = f"{nav_path}?{urlencode(nav_params)}"
        return {
            "answer": answer,
            "navigate_to": nav_path,
            "navigation_links": [
                {"label": "KPI Summary Report", "url": "/projects/reports/kpi-summary-report"},
                {"label": "Billing Reports", "url": "/billing/reports"},
                {"label": "Receivable Report", "url": "/billing/reports/receivable-report"},
            ],
            "suggested_questions": [
                "Why is the variance negative in specific months?",
                "What are the receivables over 120 days?",
                "Show KPI summary for Tax service line",
                "What is the gross profit breakdown by month?",
            ],
            "report_intent": "kpi_summary",
            "kpi_payload": kpi_payload,
            "chart_data": None,
            "export_data": export_data,
            "auto_expand": False,
        }
    except Exception as e:
        import logging
        logging.getLogger("uvicorn").warning(f"[_deterministic_kpi_response] Backend fetch warning ({e}), constructing report payload from contract fallback.")
        kpi_payload = _build_kpi_contract({}, {}, filters_applied, period)
        export_data = _build_excel_export_from_kpi_payload(kpi_payload, filters_applied, period)
        res = {
            "answer": _build_kpi_narrative(kpi_payload, filters_applied, period),
            "navigate_to": "/projects/reports/kpi-summary-report",
            "navigation_links": [{"label": "KPI Summary Report", "url": "/projects/reports/kpi-summary-report"}],
            "suggested_questions": ["Show KPI summary report", "Show KPI summary for this month"],
            "report_intent": "kpi_summary",
            "kpi_payload": kpi_payload,
            "chart_data": None,
            "export_data": export_data,
            "auto_expand": False,
        }
        from registry.contract_engine import wrap_presentation_intent
        return wrap_presentation_intent(res, latest_question, "kpi_summary")


# --------------------------------------------------------------------------- #
# Deterministic dashboard fast-path (from CRM branch)
# Pattern-matches common dashboard queries and returns answers directly from
# the semantic layer — bypassing the LLM agent entirely for speed & accuracy.
# Returns None if the query should be handled by the agent instead.
# --------------------------------------------------------------------------- #
async def deterministic_dashboard_response(history: Optional[List[dict]], latest_question: str, user_ctx: Optional[dict] = None, auth_token: Optional[str] = None):
    # Fast conversational & gibberish check
    from agent.executive_classifier import handle_executive_classification
    is_conv, conv_resp = await handle_executive_classification(latest_question, history, user_ctx)
    if is_conv and conv_resp:
        return dict(
            answer=conv_resp,
            chart_data=None, navigate_to=None, navigation_links=[],
            suggested_questions=["Show proposal pipeline", "What is total revenue?", "Show recoverability report"],
            export_data=None, auto_expand=False,
        )
    # ── Confidential / Security / Schema Guardrail Check ────────────────────
    from config.security_guard import check_security_guardrail
    sec_block = check_security_guardrail(latest_question)
    if sec_block:
        return sec_block

    from semantic.semantic_layer import (
        get_revenue_metrics,
        get_receivables_metrics,
        get_pipeline_and_proposals,
        get_active_projects_metrics,
        get_high_value_proposals
    )
    
    q = re.sub(r"[^a-z0-9]+", " ", latest_question.lower()).strip()
    q_norm = q
    for _pat, _rep in [
        (re.compile(r'\brecoverab[a-z]{0,4}\b', re.I), 'recoverability'),
        (re.compile(r'\breoverab[a-z]{0,4}\b', re.I), 'recoverability'),
        (re.compile(r'\bstaff\b', re.I), 'staff'),
        (re.compile(r'\bbill[a-z]{0,4}\b', re.I), 'billing'),
    ]:
        q_norm = _pat.sub(_rep, q_norm)

    # Check if this is a generic 'generate report' request following prior conversation context
    historical_intent = _resolve_generic_report_context(latest_question, history)
    if historical_intent:
        intent = historical_intent
        force_agent = False
    else:
        from agent.intent_classifier import classify_intent
        intent = await classify_intent(latest_question)
        force_agent = (intent == "analytical")
        if force_agent and ("recoverability" in q_norm or "staff" in q_norm or "billing" in q_norm or any(k in q_norm for k in ["comparison", "previous fy", "previous year", "vs last year"])):
            force_agent = False

    if force_agent:
        return None

    # Detect specific record lookups: "33620 proposal", "proposal 33620", bare IDs
    specific_record = re.match(
        r'^(\d{3,})\s+(proposal|invoice|project|receipt|lead|customer|employee|estimation)s?\s*$',
        q.strip()
    )
    if not specific_record:
        specific_record = re.match(
            r'^(proposal|invoice|project|receipt|lead|customer|employee|estimation)s?\s+(\d{3,})\s*$',
            q.strip()
        )
    if not specific_record:
        specific_record = re.match(r'^\d{3,}$', q.strip())
    if not specific_record:
        specific_record = re.search(r'[a-z]\d{3,}[-/]\d+[-/]\d+', latest_question.lower())
    if specific_record:
        return None

    # Intelligent intent classification happens inside _deterministic_kpi_response
    # No hardcoded keywords needed - LLM understands user intent
    kpi_response = await _deterministic_kpi_response(history, latest_question, user_ctx, auth_token)
    if kpi_response:
        return kpi_response

    # Extract custom date ranges and exclusions directly in the fast-path
    # so we can render the beautiful UI card for filtered queries.
    try:
        from agent.query_parser import _extract_date_range
        # Use latest_question instead of q so we don't lose hyphens in dates
        extracted_start, extracted_end, date_was_specified = _extract_date_range(latest_question)
    except Exception:
        extracted_start, extracted_end, date_was_specified = None, None, False
        
    # Prioritize UI-selected context dates over the fiscal year fallback
    if not date_was_specified and user_ctx and user_ctx.get("start_date") and user_ctx.get("end_date"):
        start_date = user_ctx.get("start_date")
        end_date = user_ctx.get("end_date")
    else:
        start_date = extracted_start
        end_date = extracted_end

    # Strictly enforce temporal constraints: never query beyond today
    if end_date:
        try:
            from datetime import date as _dt_date
            from datetime import datetime as _dt_time
            parsed_end = _dt_time.strptime(end_date[:10], "%Y-%m-%d").date()
            today_date = _dt_date.today()
            if parsed_end > today_date:
                end_date = today_date.strftime("%Y-%m-%d")
            
            if start_date:
                parsed_start = _dt_time.strptime(start_date[:10], "%Y-%m-%d").date()
                if parsed_start > today_date:
                    start_date = end_date
        except Exception:
            pass

        
    is_active = not bool(re.search(r'\b(non-active|inactive|completed|finished)\b', q))
    
    # Extract Service Line filter from text or user_ctx
    active_service_line = None
    if user_ctx and user_ctx.get("service_line") and str(user_ctx.get("service_line")).lower() != "all":
        active_service_line = str(user_ctx.get("service_line")).strip()
    else:
        _extracted_filters = _extract_kpi_filters_from_text(latest_question)
        if _extracted_filters.get("service_line") and str(_extracted_filters.get("service_line")).lower() != "all":
            active_service_line = str(_extracted_filters.get("service_line")).strip()

    tool_args = {}
    if start_date:
        tool_args["start_date"] = start_date
    if end_date:
        tool_args["end_date"] = end_date
    if active_service_line:
        tool_args["service_line"] = active_service_line

    def fmt(x):
        try:
            return f"{float(x):,.2f} BHD"
        except Exception:
            return str(x)

    def parse(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {}

    chart_data = None
    navigate_to = None
    navigation_links = []
    suggested_questions = []
    export_data = None
    auto_expand = False

    # ── Truly ambiguous queries that need clarification ────────────────────
    # Only disambiguate when the query is genuinely ambiguous (multiple possible metrics)
    if q in ["performance", "show performance", "my performance", "what is the performance"]:
        return dict(
            answer="I see you're asking about **performance**. Could you clarify which performance metrics you'd like to view?",
            chart_data=None, navigate_to=None, navigation_links=[],
            suggested_questions=["Show GP performance", "Show business development performance", "View Employee KPI Summary"],
            export_data=None, auto_expand=False,
        )

    if q in ["report", "reports", "show report", "show reports", "open report"] and not history:
        return dict(
            answer="We have several detailed reports available. Which one would you like to explore?",
            chart_data=None, navigate_to="/crm/reports",
            navigation_links=[{"label": "All CRM Reports", "url": "/crm/reports"}],
            suggested_questions=["Business Development Report", "Service Lead Report", "Proposal Status Report", "Receivables Report", "KPI Summary Report"],
            export_data=None, auto_expand=False,
        )

    # NOTE: Single-word queries like "receivables", "revenue", "projects", "proposals" etc.
    # are NOT disambiguated — they fall through to the keyword-match blocks below
    # which return ACTUAL DATA with charts. This matches the pre-merge behavior.

    # ── Check for deterministic top customers query FIRST ───────────────
    from memory.memory_manager import _deterministic_top_customers_by_revenue
    top_cust_res = _deterministic_top_customers_by_revenue(latest_question)
    if top_cust_res:
        return top_cust_res

    # ── GP / Revenue / Budget / Service Lines / Top Customers ──────────────
    if should_show_revenue_report(intent) or any(k in q for k in ["top 5", "top customer", "top 10", "customers by revenue"]):
        raw = await get_revenue_metrics.ainvoke(tool_args)
        rev = parse(raw)
        total_rev = rev.get("total_revenue_ytd", 0)
        prev_rev = rev.get("previous_fy_revenue", 0)
        gp_rows = rev.get("gp_performance_ytd_breakdown") or []
        top_custs = rev.get("top_5_customers") or []
        months = rev.get("revenue_by_month") or []

        style_fix = "<style>.ant-modal-title { color: var(--ai-text-primary, #e2e8f0) !important; }</style>"
        answer_lines = []
        chart_data = None
        rev_export = None

        if any(k in q for k in ["top 5", "top customer", "top 10", "customers by revenue"]):
            answer_lines = [
                "### Top 5 Customers by Revenue",
                f"- **Total YTD Revenue:** {fmt(total_rev)}",
                "",
                "| Customer Name | Revenue (BHD) |",
                "|---|---:|",
            ]
            for c in top_custs:
                answer_lines.append(f"| {c.get('customer_name','N/A')} | {float(c.get('revenue',0)):,.2f} |")
            chart_data = {
                "title": "Top 5 Customers by Revenue", "type": "bar",
                "categories": [c.get("customer_name", "") for c in top_custs],
                "series": [{"name": "Revenue (BHD)", "data": [float(c.get("revenue", 0)) for c in top_custs]}],
            }
            if top_custs:
                rev_export = {
                    "filename": "Top_Customers_By_Revenue",
                    "sheets": [{
                        "name": "Top Customers",
                        "headers": ["Customer Name", "Revenue (BHD)"],
                        "rows": [[c.get('customer_name',''), float(c.get('revenue',0))] for c in top_custs],
                        "metadata": ["Top 5 Customers by Revenue"]
                    }]
                }

        elif any(k in q for k in ["comparison", "previous fy", "previous year", "vs last year", "compare"]):
            diff = total_rev - prev_rev
            pct = ((diff / prev_rev) * 100) if prev_rev > 0 else 0
            sign = "+" if pct >= 0 else ""
            answer_lines = [
                "### Revenue Comparison (Current FY vs Previous FY)",
                f"- **Current FY Revenue (YTD):** {fmt(total_rev)}",
                f"- **Previous FY Revenue (YTD):** {fmt(prev_rev)}",
                f"- **Growth Variance:** **{sign}{pct:.2f}%** ({fmt(diff)})",
                "",
                "| Fiscal Year Period | Revenue (BHD) | Variance |",
                "|---|---:|---:|",
                f"| Current FY (YTD) | {total_rev:,.2f} | {sign}{pct:.2f}% |",
                f"| Previous FY (Full YTD) | {prev_rev:,.2f} | Baseline |",
            ]
            chart_data = {
                "title": "FY Revenue Comparison", "type": "bar",
                "categories": ["Previous FY", "Current FY (YTD)"],
                "series": [{"name": "Revenue (BHD)", "data": [float(prev_rev), float(total_rev)]}],
            }
            rev_export = {
                "filename": "Revenue_FY_Comparison",
                "sheets": [{
                    "name": "FY Comparison",
                    "headers": ["Period", "Revenue (BHD)", "Variance"],
                    "rows": [["Current FY (YTD)", float(total_rev), f"{sign}{pct:.2f}%"], ["Previous FY", float(prev_rev), "Baseline"]],
                    "metadata": ["Revenue Comparison with Previous FY"]
                }]
            }

        elif any(k in q for k in ["by month", "trend", "month", "monthly"]):
            answer_lines = [
                "### Monthly Revenue Trend",
                f"- **Total Revenue (Current Fiscal Year):** {fmt(total_rev)}",
                "",
                "| Month | Revenue (BHD) |",
                "|---|---:|",
            ]
            for m in months:
                answer_lines.append(f"| {m.get('month','')} | {float(m.get('amount',0)):,.2f} |")
            chart_data = {
                "title": "Revenue Trend by Month", "type": "bar",
                "categories": [m.get("month", "") for m in months],
                "series": [{"name": "Revenue", "data": [float(m.get("amount", 0)) for m in months]}],
            }
            if months:
                rev_export = {
                    "filename": "Monthly_Revenue_Trend",
                    "sheets": [{
                        "name": "Monthly Trend",
                        "headers": ["Month", "Revenue (BHD)"],
                        "rows": [[m.get('month',''), float(m.get('amount',0))] for m in months],
                        "metadata": ["Monthly Revenue Trend"]
                    }]
                }

        else:
            answer_lines = [
                "### GP Performance by Service Line",
                f"- **Total Revenue (Current Fiscal Year):** {fmt(total_rev)}",
                "",
                "| Service Line | Performing (BHD) | Target (BHD) |",
                "|---|---:|---:|",
            ]
            for r in gp_rows:
                answer_lines.append(
                    f"| {r.get('name','')} | {float(r.get('performing',0)):,.2f} | {float(r.get('target',0)):,.2f} |"
                )
            if gp_rows:
                chart_data = {
                    "title": "GP Performance by Service Line", "type": "bar",
                    "categories": [r.get("name", "") for r in gp_rows],
                    "series": [
                        {"name": "Performing (BHD)", "data": [float(r.get("performing", 0)) for r in gp_rows]},
                        {"name": "Target (BHD)", "data": [float(r.get("target", 0)) for r in gp_rows]},
                    ],
                }
                rev_export = {
                    "filename": "Revenue_GP_Performance_Report",
                    "sheets": [{
                        "name": "GP Performance",
                        "headers": ["Service Line", "Performing (BHD)", "Target (BHD)"],
                        "rows": [[r.get('name',''), float(r.get('performing',0)), float(r.get('target',0))] for r in gp_rows],
                        "metadata": ["GP Performance by Service Line"]
                    }]
                }

        return dict(
            answer=style_fix + "\n" + "\n".join(answer_lines).strip(), chart_data=chart_data,
            navigate_to="/#gp-performance",
            navigation_links=[{"label": "Revenue Reports", "url": "/billing/reports"}, {"label": "CRM Dashboard", "url": "/#gp-performance"}],
            suggested_questions=["Show top 5 customers by revenue", "Revenue Comparison with Previous FY", "Show revenue by month", "What are current receivables?"],
            export_data=rev_export, auto_expand=auto_expand,
        )

    # ── Receivables / Ageing / Collections ────────────────────────────────
    if should_show_receivables_report(intent):
        # Extract optional service line filter from the question
        _rec_filters = _extract_kpi_filters_from_text(latest_question)
        _rec_sl = _rec_filters.get("service_line")
        rec_args = {}
        if _rec_sl and _rec_sl.lower() not in ("all", ""):
            rec_args["service_line"] = _rec_sl

        raw = await get_receivables_metrics.ainvoke(rec_args)
        rec = parse(raw)
        total_rec = rec.get("total_receivables", 0)
        _sl_label = f" ({_rec_sl})" if _rec_sl and _rec_sl.lower() not in ("all", "") else ""
        answer_lines = [f"### Receivables Summary{_sl_label}", f"- **Total Receivables:** {fmt(total_rec)}"]
        buckets = rec.get("ageing_buckets") or []
        has_non_zero = sum(abs(float(b.get("amount") or 0)) for b in buckets) > 0
        if buckets and has_non_zero:
            answer_lines += ["", "#### Ageing Buckets", "| Bucket | Amount (BHD) |", "|---|---:|"]
            for b in buckets:
                answer_lines.append(f"| {b.get('bucket','')} | {float(b.get('amount') or 0):,.2f} |")
            chart_data = {
                "title": f"Receivables Ageing{_sl_label}", "type": "bar",
                "categories": [b.get("bucket", "") for b in buckets],
                "series": [{"name": "Amount", "data": [float(b.get("amount") or 0) for b in buckets]}],
            }

        rec_export = None
        if buckets:
            rec_export = {
                "filename": f"Receivables_Report{_rec_sl or ''}",
                "sheets": [{
                    "name": "Receivables Ageing",
                    "headers": ["Ageing Bucket", "Amount (BHD)"],
                    "rows": [[b.get('bucket',''), float(b.get('amount') or 0)] for b in buckets],
                    "metadata": [f"Receivables Summary{_sl_label}"]
                }]
            }

        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=chart_data,
            navigate_to="/billing/reports",
            navigation_links=[{"label": "Billing Reports", "url": "/billing/reports"}, {"label": "Invoices", "url": "/billing/invoice"}],
            suggested_questions=[f"Show overdue invoices{_sl_label}", "What is total revenue?", "Show open proposals"],
            export_data=rec_export, auto_expand=auto_expand,
        )

    # ── Proposals / Pipeline / Win Rate ────────────────────────────────────
    # ── Proposals / Pipeline / Win Rate ────────────────────────────────────
    if should_show_proposals_report(intent):
        raw = await get_pipeline_and_proposals.ainvoke(tool_args)
        pipe = parse(raw)

        # Check if user requested a SPECIFIC proposal status filter
        q_lower = latest_question.lower()
        requested_status = None
        if any(w in q_lower for w in ["reject", "rejected", "rejection", "declined", "lost"]):
            requested_status = "Proposal Rejected"
        elif any(w in q_lower for w in ["accept", "accepted", "approval", "approved", "won"]):
            requested_status = "Proposal Accepted"
        elif any(w in q_lower for w in ["sent", "submitted", "outbound"]):
            requested_status = "Proposal Sent"
        elif any(w in q_lower for w in ["verify", "verification", "under review"]):
            requested_status = "Proposal Verify"
        elif any(w in q_lower for w in ["created", "draft"]):
            requested_status = "Proposal Created"

        breakdown = pipe.get("dashboard_proposal_metrics_breakdown") or []

        if requested_status:
            # Find the specific row for requested status
            matching_row = next((r for r in breakdown if r.get("status_name", "").lower() == requested_status.lower() or requested_status.lower() in r.get("status_name", "").lower()), None)
            
            count_val = int(matching_row.get("total_entries", 0)) if matching_row else 0
            budget_val = float(matching_row.get("total_budget", 0)) if matching_row else 0.0

            # Status ID mapping matching m_proposal_status
            status_id_map = {
                "Proposal Created": 7,
                "Proposal Verify": 8,
                "Proposal Sent": 1,
                "Proposal Accepted": 3,
                "Proposal Rejected": 4
            }
            s_id = status_id_map.get(requested_status)
            details_list = []
            if s_id:
                try:
                    from semantic.semantic_layer import _run_query, _resolve_rbac_params, _build_ownership_sql
                    emp_id = user_ctx.get("employee_id") if user_ctx else None
                    u_tier = user_ctx.get("hierarchy_level") if user_ctx else None
                    emp_id, u_tier = _resolve_rbac_params(emp_id, u_tier)
                    prop_ownership_sql = _build_ownership_sql(emp_id, u_tier, "p", True, "created_by")

                    sl_clause = f" AND msl.name LIKE '%{active_service_line.strip()}%'" if active_service_line else ""
                    dt_clause = f" AND p.created_at BETWEEN '{start_date}' AND '{end_date}'" if start_date and end_date else ""
                    det_q = f"""
                        SELECT p.id, p.code, COALESCE(c.customer_name, co.cd_company_name, co.first_name, 'N/A') as client_name, p.agreed_fees, p.created_at
                        FROM proposal p
                        LEFT JOIN customers c ON p.client_id = c.id
                        LEFT JOIN contacts co ON p.contact_id = co.id
                        LEFT JOIN m_serviceline msl ON msl.id = p.service_line_id
                        WHERE p.is_active = 1 AND p.proposal_status_id = {s_id} AND {prop_ownership_sql} {dt_clause} {sl_clause}
                        ORDER BY p.created_at DESC LIMIT 5
                    """
                    details_list = await _run_query(det_q)
                except Exception:
                    details_list = []

            title_label = requested_status
            answer_lines = [
                f"### {title_label}",
                f"- **Total Count:** {count_val}",
                f"- **Total Budget:** {fmt(budget_val)}",
            ]

            if details_list:
                answer_lines += [
                    "",
                    f"#### Recent {title_label}s",
                    "| Proposal ID | Client Name | Agreed Fees (BHD) | Date |",
                    "|---|---|---:|---|",
                ]
                for d in details_list:
                    fees = float(d.get("agreed_fees") or 0)
                    dt_str = str(d.get("created_at") or "")[:10]
                    client = str(d.get("client_name") or "N/A")
                    p_code = str(d.get("code") or d.get("id"))
                    answer_lines.append(f"| {p_code} | {client} | {fees:,.2f} | {dt_str} |")

            return dict(
                answer="\n".join(answer_lines).strip(),
                chart_data=None,
                navigate_to="/proposal",
                navigation_links=[{"label": "Proposals", "url": "/proposal"}, {"label": "Proposal Status Report", "url": "/crm/reports/proposal-status-report"}],
                suggested_questions=["Show proposal win rate", "Show proposal status breakdown", "Show open proposals"],
                export_data=export_data,
                auto_expand=auto_expand,
            )

        # General pipeline summary when no specific status was targeted
        open_props = pipe.get("open_proposals") or {"count": 0, "total_budget": 0}
        answer_lines = [
            "### Proposal Pipeline",
            f"- **Open Proposals:** {int(open_props.get('count', 0))}",
            f"- **Total Budget of Open Proposals:** {fmt(open_props.get('total_budget', 0))}",
        ]
        if breakdown:
            answer_lines += ["", "#### Status Breakdown", "| Status | Count | Budget (BHD) |", "|---|---:|---:|"]
            for row in breakdown:
                answer_lines.append(
                    f"| {row.get('status_name','')} | {int(row.get('total_entries',0))} | {float(row.get('total_budget',0)):,.2f} |"
                )
            chart_data = {
                "title": "Proposal Status Breakdown", "type": "donut",
                "categories": [r.get("status_name", "") for r in breakdown],
                "series": [float(r.get("total_budget", 0)) for r in breakdown],
            }

        prop_export = None
        if breakdown:
            prop_export = {
                "filename": "Proposal_Pipeline_Report",
                "sheets": [{
                    "name": "Proposal Pipeline",
                    "headers": ["Status", "Count", "Total Budget (BHD)"],
                    "rows": [[r.get('status_name',''), int(r.get('total_entries',0)), float(r.get('total_budget',0))] for r in breakdown],
                    "metadata": ["Proposal Pipeline Report"]
                }]
            }

        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=chart_data,
            navigate_to="/proposal",
            navigation_links=[{"label": "Proposals", "url": "/proposal"}, {"label": "Proposal Status Report", "url": "/crm/reports/proposal-status-report"}],
            suggested_questions=["Show proposal status breakdown", "What is proposal win rate?", "Show high value proposals"],
            export_data=prop_export, auto_expand=auto_expand,
        )

    # ── Recoverability ─────────────────────────────────────────────────────
    # CRITICAL: This MUST be above the generic Projects handler.
    # Uses the dedicated recoverability report tool and formats deterministically.
    from agent.intent_classifier import should_show_recoverability_report, should_show_staff_billing_report, should_show_projects_report
    
    if should_show_recoverability_report(intent):
        from semantic.semantic_layer import get_project_recoverability_report
        rec_args = {"start_date": start_date, "end_date": end_date}
        
        # Extract filters from the question and history
        _filters = _extract_kpi_filters_from_text(latest_question)
        _hist_filters = _extract_kpi_filters_from_history(history)
        _filters = _merge_kpi_filters(_filters, _hist_filters)
        
        _sl = _filters.get("service_line")
        _emp = _filters.get("employee_name")
        _cust = _filters.get("customer") or _filters.get("customer_name")
        _PRONOMINAL_CUST = {"that customer", "this customer", "they", "them", "the customer", "customer", "that client", "this client", "the client"}
        if not _cust or _cust.lower().strip() in _PRONOMINAL_CUST:
            _cust = _extract_customer_from_history(history)

        _proj = _filters.get("project_name")

        # Safety guard: reject any "employee name" that looks like a picker label or date-related phrase
        _LABEL_WORDS = {"date", "range", "service", "line", "full", "fy", "financial", "year",
                        "duration", "period", "select", "report", "filter", "business", "process",
                        "recoverability", "project", "incharge", "charge", "partner", "customer"}
        if _emp:
            _emp_words = set(_emp.lower().split())
            if _emp_words & _LABEL_WORDS:  # intersection — any overlap → reject
                _emp = None
            elif _proj and _emp.lower() in _proj.lower():
                _emp = None
        
        if _sl and _sl.lower() != 'all':
            rec_args["service_line"] = _sl
        if _emp and _emp.lower() != 'all':
            rec_args["incharge_employee"] = _emp
        if _cust and _cust.lower() != 'all':
            rec_args["customer_name"] = _cust
        if _proj:
            # Bypass date filter if a specific project is requested
            rec_args["start_date"] = "1970-01-01"
            rec_args["end_date"] = "2099-12-31"
            rec_args["project_name"] = _proj
            
        if hasattr(get_project_recoverability_report, 'coroutine'):
            raw = await get_project_recoverability_report.coroutine(**rec_args)
        else:
            raw = await get_project_recoverability_report.ainvoke(rec_args)
        rec = parse(raw)
        
        if isinstance(rec, dict) and "error" in rec:
            return dict(
                answer=f"⚠️ {rec['error']}",
                chart_data=None, navigate_to="/projects/reports/project-recoverability-report",
                navigation_links=[], suggested_questions=[], export_data=None, auto_expand=False
            )

        summary = rec.get("summary", rec)
        projects = rec.get("projects", [])
        dr = rec.get("date_range", {})
        total_projects = summary.get("total_projects", len(projects))

        sd = dr.get("start") or start_date
        ed = dr.get("end")   or end_date
        try:
            if str(sd) == "1970-01-01" and str(ed) == "2099-12-31":
                period_label = "All Time"
            else:
                from datetime import datetime as _dt
                sd_p = _dt.strptime(str(sd), "%Y-%m-%d")
                ed_p = _dt.strptime(str(ed), "%Y-%m-%d")
                if sd_p.year == ed_p.year and sd_p.month == ed_p.month:
                    period_label = sd_p.strftime("%B %Y")
                else:
                    period_label = f"{sd_p.strftime('%d %b')} – {ed_p.strftime('%d %b %Y')}"
        except Exception:
            period_label = f"{sd} to {ed}" if sd and ed else "All Time"

        answer_lines = [
            "### Project Recoverability Report",
        ]
        
        # Only show the period if we didn't bypass it for a specific project
        if not _proj:
            answer_lines.append(f"- **Period:** {period_label}")
            
        if _sl and _sl.lower() != 'all':
            answer_lines.append(f"- **Service Line:** {_sl}")
        if _proj:
            answer_lines.append(f"- **Project:** {_proj}")
        else:
            answer_lines.append(f"- **Total Projects:** {total_projects}")

            est_cost = summary.get("total_estimated_cost")
            if est_cost not in (None, "", "N/A"):
                answer_lines.append(f"- **Total Estimated Cost:** BHD {float(est_cost):,.2f}")

            act_cost = summary.get("total_actual_cost")
            if act_cost not in (None, "", "N/A"):
                answer_lines.append(f"- **Total Actual Cost:** BHD {float(act_cost):,.2f}")

        act_rec = summary.get("total_actual_recoverability_percentage")
        if act_rec not in (None, "", "N/A") and str(act_rec).strip().lower() != "nan":
            answer_lines.append(f"- **Portfolio Recoverability Rate:** {act_rec}%")

        if total_projects == 0:
            answer_lines.append(f"\n No projects matched the criteria for {period_label}.")

        # Build export data for the Excel button
        ordered_columns = [
            ("Project Code", "project_code"),
            ("Project Name", "project_name"),
            ("Customer Name", "customer_name"),
            ("Customer Group", "customer_group"),
            ("Service Line", "service_line"),
            ("Start Date", "start_date"),
            ("End Date", "end_date"),
            ("Project In Charge", "project_in_charge"),
            ("Customer Relation", "customer_relation"),
            ("Project Partner", "project_partner"),
            ("Project Status", "project_status"),
            ("Approved Fees", "approved_fees"),
            ("Agreed Fees", "agreed_fees"),
            ("Est. Cost", "estimated_cost"),
            ("Est. Recoverability (%)", "estimated_recoverability"),
            ("Total Actual Cost", "total_actual_cost"),
            ("Actual Recoverability (%)", "actual_recoverability")
        ]
        
        headers = ["No"] + [col[0] for col in ordered_columns]
        all_rows = []
        for i, p in enumerate(projects):
            row = [str(i + 1)] # No column
            for col_name, dict_key in ordered_columns:
                val = p.get(dict_key, "")
                if dict_key in ['approved_fees', 'agreed_fees', 'estimated_cost', 'total_actual_cost', 'estimated_recoverability', 'actual_recoverability']:
                    row.append(round(float(val or 0), 3) if val else 0.0)
                else:
                    row.append(val)
            all_rows.append(row)
        
        meta = [
            "Project Recoverability Report",
            f"Generated on: {_dt.now().strftime('%d %b %Y')}",
            f"Period: {period_label}"
        ]
        if _sl and _sl.lower() != 'all':
            meta.append(f"Service Line: {_sl}")
        if _emp and _emp.lower() != 'all':
            meta.append(f"In-Charge Employee: {_emp}")
            
        rec_export = {
            "filename": "Project_Recoverability_Report",
            "sheets": [{"name": "Data", "headers": headers, "rows": all_rows, "metadata": meta}]
        }

        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=None,
            navigate_to="/projects/reports/project-recoverability-report",
            navigation_links=[
                {"label": "Recoverability Report", "url": "/projects/reports/project-recoverability-report"},
                {"label": "Projects List", "url": "/projects-list"}
            ],
            suggested_questions=["Show recoverability for last month", "Show active vs completed projects", "What is the total revenue?"],
            export_data=rec_export, auto_expand=auto_expand,
        )

    if should_show_staff_billing_report(intent):
        from semantic.semantic_layer import get_staff_billing_report
        sb_args = {"start_date": start_date, "end_date": end_date}
        
        # Extract filters from the question and history
        _filters = _extract_kpi_filters_from_text(latest_question)
        _hist_filters = _extract_kpi_filters_from_history(history)
        _filters = _merge_kpi_filters(_filters, _hist_filters)
        
        _sl = _filters.get("service_line")
        _emp = _filters.get("employee_name")
        _cust = _filters.get("customer") or _filters.get("customer_name")
        
        if _sl and _sl.lower() != 'all':
            sb_args["service_line"] = _sl
        if _emp and _emp.lower() != 'all':
            sb_args["employee_name"] = _emp
        if _cust and _cust.lower() != 'all':
            sb_args["customer_name"] = _cust
            
        # We also need project partner extraction, but _extract_kpi_filters_from_text doesn't do it natively yet.
        _partner = _clean_filter_value(_extract_value_from_line(latest_question, r"Project\s*Partner"))
        if not _partner:
            import re as _re_sb
            partner_match = _re_sb.search(r'(?:partner\s+is|partner:|partner)\s+([a-zA-Z\s]+?)(?:$|\n|and|where|for)', latest_question, _re_sb.IGNORECASE)
            if partner_match:
                _partner = partner_match.group(1).strip()
                
        if _partner and _partner.lower() != 'all':
            sb_args["project_partner"] = _partner
            
        raw = await get_staff_billing_report.ainvoke(sb_args)
        sb = parse(raw)
        
        if isinstance(sb, dict) and "error" in sb:
            return dict(
                answer=f"⚠️ {sb['error']}",
                chart_data=None, navigate_to="/projects/reports/staff-billing-report",
                navigation_links=[], suggested_questions=[], export_data=None, auto_expand=False
            )

        summary = sb.get("summary", sb)
        projects = sb.get("projects", [])
        dr = sb.get("date_range", {})
        total_projects = summary.get("total_projects", len(projects))

        sd = dr.get("start") or start_date
        ed = dr.get("end")   or end_date
        try:
            from datetime import datetime as _dt
            sd_p = _dt.strptime(str(sd), "%Y-%m-%d")
            ed_p = _dt.strptime(str(ed), "%Y-%m-%d")
            if sd_p.year == ed_p.year and sd_p.month == ed_p.month:
                period_label = sd_p.strftime("%B %Y")
            else:
                period_label = f"{sd_p.strftime('%d %b')} – {ed_p.strftime('%d %b %Y')}"
        except Exception:
            period_label = f"{sd} to {ed}" if sd and ed else "All Time"

        answer_lines = [
            "### Staff Billing Report",
            f"- **Period:** {period_label}",
        ]
        if _sl and _sl.lower() != 'all':
            answer_lines.append(f"- **Service Line:** {_sl}")
        if _emp and _emp.lower() != 'all':
            answer_lines.append(f"- **Employee:** {_emp}")
        if _cust and _cust.lower() != 'all':
            answer_lines.append(f"- **Customer:** {_cust}")
        if sb_args.get("project_partner"):
            answer_lines.append(f"- **Project Partner:** {sb_args.get('project_partner')}")
            
        answer_lines.append(f"- **Total Projects:** {total_projects}")

        staff_cost = summary.get("total_staff_cost")
        if staff_cost not in (None, "", "N/A"):
            answer_lines.append(f"- **Total Staff Cost / Billing:** BHD {float(staff_cost):,.2f}")

        app_fees = summary.get("total_approved_fees")
        if app_fees not in (None, "", "N/A"):
            answer_lines.append(f"- **Total Approved Fees:** BHD {float(app_fees):,.2f}")

        invoiced = summary.get("total_invoiced")
        if invoiced not in (None, "", "N/A"):
            answer_lines.append(f"- **Total Invoiced:** BHD {float(invoiced):,.2f}")

        if total_projects == 0:
            answer_lines.append(f"\n No records matched the criteria for {period_label}.")

        # Build export data for the Excel button
        sb_export = None
        if projects:
            columns = list(projects[0].keys())
            all_rows = [[r.get(c, "") for c in columns] for r in projects]
            
            meta = [
                "Staff Billing Report",
                f"Generated on: {_dt.now().strftime('%d %b %Y')}",
                f"Period: {period_label}"
            ]
            if _sl and _sl.lower() != 'all': meta.append(f"Service Line: {_sl}")
            if _emp and _emp.lower() != 'all': meta.append(f"Employee: {_emp}")
            if _cust and _cust.lower() != 'all': meta.append(f"Customer: {_cust}")
            if sb_args.get("project_partner"): meta.append(f"Project Partner: {sb_args.get('project_partner')}")
                
            sb_export = {
                "filename": "Staff_Billing_Report",
                "sheets": [{"name": "Data", "headers": columns, "rows": all_rows, "metadata": meta}]
            }

        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=None,
            navigate_to="/projects/reports/staff-billing-report",
            navigation_links=[
                {"label": "Staff Billing Report", "url": "/projects/reports/staff-billing-report"},
                {"label": "Projects List", "url": "/projects-list"}
            ],
            suggested_questions=["Show staff billing for Audit service line", "Show staff billing for Employee A", "What is the staff cost for Customer X?"],
            export_data=sb_export, auto_expand=auto_expand,
        )

    # ── Projects ──────────────────────────────────────────────────────────
    # ── Projects ──────────────────────────────────────────────────────────
    if should_show_projects_report(intent):
        _filters = _extract_kpi_filters_from_text(latest_question)
        _hist_filters = _extract_kpi_filters_from_history(history)
        _filters = _merge_kpi_filters(_filters, _hist_filters)
        
        _cust = _filters.get("customer") or _filters.get("customer_name")
        _PRONOMINAL_CUST = {"that customer", "this customer", "they", "them", "the customer", "customer", "that client", "this client", "the client"}
        if not _cust or _cust.lower().strip() in _PRONOMINAL_CUST:
            _cust = _extract_customer_from_history(history)

        _emp = _filters.get("employee_name")

        # ── CUSTOMER-SPECIFIC PROJECTS REPORT (Detailed Table + Export) ─────────
        if _cust and _cust.lower().strip() not in ("all", "none", "n/a"):
            from semantic.semantic_layer import get_project_recoverability_report
            rec_args = {
                "customer_name": _cust,
                "start_date": "1970-01-01",
                "end_date": "2099-12-31"
            }
            raw = await get_project_recoverability_report.ainvoke(rec_args)
            rec = parse(raw)

            projects = rec.get("projects", []) if isinstance(rec, dict) else []
            summary = rec.get("summary", {}) if isinstance(rec, dict) else {}

            header_title = f"### Projects List for **{_cust}**"
            answer_lines = [
                header_title,
                f"- **Total Projects:** {summary.get('total_projects', len(projects))}",
            ]
            if summary.get("total_approved_fees"):
                try:
                    answer_lines.append(f"- **Total Approved Fees:** BHD {summary['total_approved_fees']:,.2f}")
                except Exception:
                    answer_lines.append(f"- **Total Approved Fees:** BHD {summary['total_approved_fees']}")
            if summary.get("total_actual_cost"):
                try:
                    answer_lines.append(f"- **Total Actual Cost:** BHD {summary['total_actual_cost']:,.2f}")
                except Exception:
                    answer_lines.append(f"- **Total Actual Cost:** BHD {summary['total_actual_cost']}")

            cust_export = None
            if projects:
                answer_lines += [
                    "",
                    "| No | Project Code | Project Name | Service Line | Status | Approved Fees | Actual Cost | Recoverability |",
                    "|---|---|---|---|---|---:|---:|---:|"
                ]

                export_rows = []
                for idx, p in enumerate(projects, 1):
                    p_code = p.get("project_code") or "N/A"
                    p_name = p.get("project_name") or "N/A"
                    p_sl = p.get("service_line") or "N/A"
                    p_status = p.get("project_status") or "Active"
                    app_fee = p.get("approved_fees", 0.0)
                    act_cost = p.get("total_actual_cost", 0.0)
                    act_rec = p.get("actual_recoverability", 0.0)

                    try:
                        app_fee_str = f"BHD {float(app_fee):,.2f}"
                    except Exception:
                        app_fee_str = f"BHD {app_fee}"

                    try:
                        act_cost_str = f"BHD {float(act_cost):,.2f}"
                    except Exception:
                        act_cost_str = f"BHD {act_cost}"

                    rec_str = f"{act_rec:.2f}%" if isinstance(act_rec, (int, float)) else f"{act_rec}%"

                    answer_lines.append(f"| {idx} | {p_code} | {p_name} | {p_sl} | {p_status} | {app_fee_str} | {act_cost_str} | {rec_str} |")

                    export_rows.append([
                        str(idx), p_code, p_name, p.get("customer_name", _cust),
                        p_sl, p_status, app_fee, act_cost, act_rec
                    ])

                cust_export = {
                    "filename": f"Projects_{_cust.replace(' ', '_')}",
                    "sheets": [{
                        "name": "Projects",
                        "headers": ["No", "Project Code", "Project Name", "Customer", "Service Line", "Status", "Approved Fees", "Actual Cost", "Recoverability (%)"],
                        "rows": export_rows,
                        "metadata": [f"Projects List for {_cust}"]
                    }]
                }
            else:
                answer_lines.append(f"\n⚠️ No specific projects found on record for **{_cust}**.")

            return dict(
                answer="\n".join(answer_lines).strip(),
                chart_data=None,
                navigate_to="/projects-list",
                navigation_links=[{"label": "Projects List", "url": "/projects-list"}, {"label": "CRM Dashboard", "url": "/crm-dashboard"}],
                suggested_questions=["Show active vs completed projects", "Show project recoverability report", "What is the total revenue?"],
                export_data=cust_export,
                auto_expand=False,
            )

        # ── COMPANY-WIDE PROJECTS SUMMARY ─────────────────────────────────────
        proj_args = tool_args.copy()
        proj_args["is_active"] = is_active
        if _emp:
            proj_args["employee_name"] = _emp

        raw = await get_active_projects_metrics.ainvoke(proj_args)
        proj = parse(raw)

        dr = proj.get("date_range", {})
        sd = dr.get("start") or start_date
        ed = dr.get("end")   or end_date
        try:
            from datetime import datetime as _dt
            sd_p = _dt.strptime(str(sd), "%Y-%m-%d")
            ed_p = _dt.strptime(str(ed), "%Y-%m-%d")
            if sd_p.year == ed_p.year and sd_p.month == ed_p.month:
                period_label = sd_p.strftime("%B %Y")
            else:
                period_label = f"{sd_p.strftime('%d %b')} – {ed_p.strftime('%d %b %Y')}"
        except Exception:
            period_label = f"{sd} to {ed}" if sd and ed else "All Time"

        header_title = "### Projects Summary"
        answer_lines = [
            header_title,
            f"- **Period:** {period_label}",
            f"- **Total Active Projects:** {proj.get('total_projects', proj.get('total_active_projects', 0))}",
        ]

        completion = proj.get("overall_completion_percentage")
        if completion not in (None, "N/A", ""):
            answer_lines.append(f"- **Overall Completion:** {completion}%")

        overdue = proj.get("overdue_tasks")
        if overdue not in (None, "N/A", ""):
            answer_lines.append(f"- **Overdue Tasks:** {overdue}")

        act_rec = proj.get("actual_recoverability_percentage")
        if act_rec not in (None, "", "N/A") and str(act_rec).strip().lower() != "nan":
            answer_lines.append(f"- **Actual Recoverability:** {act_rec}%")

        est_rec = proj.get("estimated_recoverability_percentage")
        if est_rec not in (None, "", "N/A") and str(est_rec).strip().lower() != "nan":
            answer_lines.append(f"- **Estimated Recoverability:** {est_rec}%")

        cats = proj.get("projects_by_status") or proj.get("projects_by_category") or []
        chart_data = None
        if cats:
            cats_nonzero = [c for c in cats if int(c.get("total", 0)) > 0]
            rows = cats_nonzero if cats_nonzero else cats
            answer_lines += ["", "#### Projects by Status", "| Status | Total |", "|---|---:|"]
            for c in rows:
                cat_name = c.get("label", c.get("category", ""))
                answer_lines.append(f"| {cat_name} | {int(c.get('total', 0))} |")
            chart_data = {
                "title": f"Projects by Status ({period_label})",
                "type": "donut",
                "categories": [c.get("label", c.get("category", "")) for c in rows],
                "series": [int(c.get("total", 0)) for c in rows],
            }

        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=chart_data,
            navigate_to="/projects-list",
            navigation_links=[{"label": "Projects List", "url": "/projects-list"}, {"label": "CRM Dashboard", "url": "/crm-dashboard"}],
            suggested_questions=["Show active vs completed projects", "Show receivables", "What is the total revenue?"],
            export_data=export_data, auto_expand=auto_expand,
        )


    # ── High Value Proposals ──────────────────────────────────────────────
    HV_KW = ["high value", "top 5", "highest", "largest proposal", "biggest proposal"]
    if any(re.search(rf"\b{re.escape(kw)}\b", q) for kw in HV_KW):
        raw = await get_high_value_proposals.ainvoke(tool_args)
        hv = parse(raw)
        proposals = hv.get("high_value_proposals_top_5") or []
        answer_lines = [
            "### High Value Proposals (Top 5)", "",
            "| Proposal ID | Client | Budget (BHD) | Age (Days) |",
            "|---|---|---:|---:|",
        ]
        for p in proposals:
            answer_lines.append(
                f"| {p.get('proposal_id','')} | {p.get('client_name','')} | {float(p.get('budget_value',0)):,.2f} | {p.get('age_in_days','')} |"
            )
        return dict(
            answer="\n".join(answer_lines).strip(), chart_data=None,
            navigate_to="/proposal",
            navigation_links=[{"label": "Proposals", "url": "/proposal"}],
            suggested_questions=["Show open proposals", "Show proposal status breakdown"],
            export_data=None, auto_expand=False,
        )

    # No keyword matched → let the agent handle it
    return None


# --------------------------------------------------------------------------- #
# Role-Based Suggestion Engine (Bhavik's system)
# --------------------------------------------------------------------------- #
ROLE_SUGGESTIONS = {
    1: ["What is the total revenue this fiscal year?", "What is the overall proposal win rate?", "Show me the receivables ageing summary", "How many active projects do we have?", "What is the total pipeline value?"],
    2: ["What is the total revenue this fiscal year?", "What is the proposal win rate?", "Show receivables ageing summary", "How many active projects do we have?", "What are the top high-value proposals?"],
    3: ["What is the total revenue this fiscal year?", "Show receivables ageing summary", "How many open proposals are there?", "How many active projects do we have?", "What is the total outstanding receivables?"],
    4: ["What are my active projects?", "Show receivables ageing summary", "How many open proposals do I have?", "What is the total outstanding receivables?", "What is my proposal win rate?"],
    5: ["What are my active projects?", "Show receivables ageing summary", "What is the task overview for my projects?", "What is the total outstanding receivables?", "Show my overdue project tasks"],
    6: ["What are my active projects?", "What is the task overview?", "Show receivables ageing summary", "Show my overdue project tasks", "What is my collection rate?"],
    7: ["What is the task overview?", "Show my overdue tasks", "How many active projects do we have?", "Show receivables ageing summary", "What is my timesheet status?"],
    8: ["What is the task overview?", "Show my overdue tasks", "What is my timesheet status?", "How many active projects do we have?"],
    9: ["What is the task overview?", "Show my overdue tasks", "What is my timesheet status?", "How many active projects do we have?"],
}

DEPT_SUGGESTIONS = {
    "sales": ["How many open sales leads do I have?", "What is my proposal win rate?", "Show my open proposals", "What is the total pipeline value?", "How many leads were created this month?"],
    "business development": ["How many open sales leads do I have?", "What is my proposal win rate?", "Show my open proposals", "What is the total pipeline value?", "How many leads were created this month?"],
    "marketing": ["How many open sales leads do I have?", "What is the total pipeline value?", "Show my open proposals", "How many leads were created this month?", "What is the proposal win rate?"],
    "hr": ["How many employees are there per department?", "How many active projects do we have?", "What is the task overview?", "Show me employee counts by designation", "How many leave requests are pending?"],
    "human resources": ["How many employees are there per department?", "How many active projects do we have?", "What is the task overview?", "Show me employee counts by designation", "How many leave requests are pending?"],
    "it": ["How many active projects do we have?", "What is the task overview?", "Show my overdue tasks", "How many employees are there?", "What is my timesheet status?"],
    "legal": ["How many active projects do we have?", "Show me customer details", "What is the task overview?", "How many proposals are currently open?", "Show my overdue tasks"],
}


class SuggestionsRequest(BaseModel):
    user_id: int = 0
    employee_id: Optional[int] = None
    designation_name: str = "Unknown"
    department_name: str = "Unknown"


@app.post("/get-suggestions")
def get_suggestions(request: SuggestionsRequest):
    from config.role_tier_config import get_tier_for_role

    designation = request.designation_name or "Unknown"
    department = request.department_name or "Unknown"

    emp_lookup_id = request.employee_id
    if emp_lookup_id and emp_lookup_id > 0:
        try:
            engine = get_db_engine()
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT d.name FROM employees e "
                    "JOIN m_designation d ON e.emp_designation_id = d.id "
                    "WHERE e.id = :emp_id"
                ), {"emp_id": emp_lookup_id}).fetchone()
                if row:
                    designation = row[0]

                row2 = conn.execute(text(
                    "SELECT d.name FROM employees e "
                    "JOIN m_department d ON e.emp_department_id = d.id "
                    "WHERE e.id = :emp_id"
                ), {"emp_id": emp_lookup_id}).fetchone()
                if row2:
                    department = row2[0]
        except Exception as e:
            print(f"[Suggestions] DB lookup failed: {e}")

    dept_lower = department.lower().strip()
    for dept_key, dept_suggestions in DEPT_SUGGESTIONS.items():
        pattern = r'\b' + re.escape(dept_key) + r'\b'
        if re.search(pattern, dept_lower):
            return {"suggestions": dept_suggestions}

    tier = get_tier_for_role(designation)
    role_suggestions = ROLE_SUGGESTIONS.get(tier, ROLE_SUGGESTIONS[9])
    return {"suggestions": role_suggestions}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health_check():
    return {"status": "ok", "message": "CRM AI Assistant is running ✅ (v3.0 merged)"}


@app.post("/ask-ai", response_model=AnswerResponse)
async def ask_ai(request: QuestionRequest):
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages array cannot be empty.")

    history = [{"role": m.role, "content": m.content} for m in request.messages]
    latest_question = request.messages[-1].content

    user_ctx = None
    if request.user_context:
        user_ctx = resolve_user_context(request.user_context)
        print(f"[RBAC] Resolved user: {user_ctx['user_name']} | Role: {user_ctx['role_name']} | Dept: {user_ctx['department']}")

    # Pass auth token + set semantic layer user context for RBAC enforcement
    from semantic import semantic_layer
    if hasattr(semantic_layer, '_CRM_AUTH_TOKEN'):
        semantic_layer._CRM_AUTH_TOKEN = request.auth_token or ''
    if user_ctx and hasattr(semantic_layer, 'set_user_context'):
        from config.role_tier_config import get_tier_for_role
        resolved_tier = get_tier_for_role(user_ctx.get('role_name', 'Unknown'))
        semantic_layer.set_user_context({
            'employee_id': user_ctx.get('employee_id'),
            'user_tier': resolved_tier,
            'role_name': user_ctx.get('role_name', 'Unknown'),
            'department_id': user_ctx.get('department_id'),
        })

    # Try deterministic fast-path first (instant, no LLM cost)
    fast = await deterministic_dashboard_response(history, latest_question, user_ctx, request.auth_token)
    if fast:
        print(f"[FASTPATH] Matched deterministic route for: {latest_question!r}")
        try:
            from db.database import save_token_usage_async
            emp_id = (user_ctx or {}).get("employee_id") or 0
            await save_token_usage_async(
                employee_id=emp_id,
                session_id="ask-ai-session",
                model_name="deterministic_fast_path",
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                total_cost_usd=0.0,
                execution_path="fast_path",
                capability_id=fast.get("report_intent") or "general_query",
                operation="chat_response",
                status="success"
            )
        except Exception as _e:
            print(f"[Telemetry] Fast-path log error: {_e}")
        return AnswerResponse(**fast)

    # Fall back to LLM agent — use full ask_question() for 10-tuple (with edit intent fields)
    from agent.agent import ask_question as _ask_question
    print(f"[AGENT] Routing to agent for: {latest_question!r}")
    result = await _ask_question(history, user_ctx)
    answer_text, chart_data, navigate_to, navigation_links, export_data, auto_expand, suggested_questions, entity_name, entity_type, is_edit_intent, report_intent = result
    kpi_payload = None

    if report_intent == "kpi_summary" or _is_kpi_summary_query(latest_question):
        kpi_response = await _deterministic_kpi_response(history, latest_question, user_ctx, request.auth_token)
        if kpi_response:
            answer_text = kpi_response.get("answer", answer_text)
            chart_data = kpi_response.get("chart_data", chart_data)
            navigate_to = kpi_response.get("navigate_to", "/projects/reports/kpi-summary-report")
            navigation_links = kpi_response.get("navigation_links", navigation_links)
            suggested_questions = kpi_response.get("suggested_questions", suggested_questions)
            export_data = kpi_response.get("export_data", export_data)
            auto_expand = kpi_response.get("auto_expand", auto_expand)
            report_intent = kpi_response.get("report_intent", "kpi_summary")
            kpi_payload = kpi_response.get("kpi_payload")
        else:
            navigate_to = "/projects/reports/kpi-summary-report"
            if not navigation_links:
                navigation_links = [{"label": "KPI Summary Report", "url": "/projects/reports/kpi-summary-report"}]

    # Task 7: If edit intent detected, fetch current entity data as a confirmation payload
    edit_payload = None
    if is_edit_intent and entity_name and entity_type:
        try:
            from agent.tools_new import handle_edit_intent
            edit_payload = await handle_edit_intent(entity_type, entity_name, user_ctx.get('employee_id') if user_ctx else None, user_ctx.get('role_name', 'Unknown') if user_ctx else 'Unknown')
        except Exception as e:
            print(f"[EditIntent] Failed to build edit payload: {e}")

    try:
        from db.database import save_token_usage_async
        emp_id = (user_ctx or {}).get("employee_id") or 0
        await save_token_usage_async(
            employee_id=emp_id,
            session_id="ask-ai-session",
            model_name=os.getenv("LLM_MODEL", "qwen/qwen3.6-27b"),
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            total_cost_usd=0.0,
            execution_path="llm_agent",
            capability_id=report_intent or "general_query",
            operation="chat_response",
            status="success"
        )
    except Exception as _e:
        print(f"[Telemetry] Agent log error: {_e}")

    return AnswerResponse(
        answer=answer_text,
        chart_data=chart_data,
        navigate_to=navigate_to,
        navigation_links=navigation_links,
        suggested_questions=suggested_questions,
        export_data=export_data,
        auto_expand=auto_expand,
        edit_intent=edit_payload,
        report_intent=report_intent,
        kpi_payload=kpi_payload,
    )


@app.post("/ask-ai-stream")
async def ask_ai_stream(request: QuestionRequest):
    """SSE streaming endpoint — returns tokens progressively for a typewriter effect."""
    if not request.messages:
        raise HTTPException(status_code=400, detail="Messages array cannot be empty.")

    history = [{"role": m.role, "content": m.content} for m in request.messages]
    latest_question = request.messages[-1].content

    user_ctx = None
    if request.user_context:
        user_ctx = resolve_user_context(request.user_context)

    # Pass auth token + set semantic layer user context for RBAC enforcement
    from semantic import semantic_layer
    if hasattr(semantic_layer, '_CRM_AUTH_TOKEN'):
        semantic_layer._CRM_AUTH_TOKEN = request.auth_token or ''
    if user_ctx and hasattr(semantic_layer, 'set_user_context'):
        from config.role_tier_config import get_tier_for_role
        resolved_tier = get_tier_for_role(user_ctx.get('role_name', 'Unknown'))
        semantic_layer.set_user_context({
            'employee_id': user_ctx.get('employee_id'),
            'role_name': user_ctx.get('role_name', 'Unknown'),
            'department_id': user_ctx.get('department_id'),
        })

    # Try deterministic fast-path — emit as a single SSE "done" event (instant)
    fast = await deterministic_dashboard_response(history, latest_question, user_ctx, request.auth_token)
    if fast:
        print(f"[FASTPATH-STREAM] Matched deterministic route for: {latest_question!r}")

        async def fast_generator():
            payload = {
                "type": "done",
                "content": fast["answer"],
                "chart_data": fast.get("chart_data"),
                "navigate_to": fast.get("navigate_to"),
                "navigation_links": fast.get("navigation_links", []),
                "suggested_questions": fast.get("suggested_questions", []),
                "export_data": fast.get("export_data"),
                "auto_expand": fast.get("auto_expand", False),
                "report_intent": fast.get("report_intent"),
                "kpi_payload": fast.get("kpi_payload"),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

            try:
                from db.database import save_token_usage_async
                emp_id = (user_ctx or {}).get("employee_id") or 0
                await save_token_usage_async(
                    employee_id=emp_id,
                    session_id="ask-ai-stream-session",
                    model_name="deterministic_fast_path",
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    total_cost_usd=0.0,
                    execution_path="fast_path",
                    capability_id=fast.get("report_intent") or "general_query",
                    operation="chat_response",
                    status="success"
                )
            except Exception as _e:
                print(f"[Telemetry] Fast-path stream log error: {_e}")

        return StreamingResponse(
            fast_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Fall back to streaming agent (typewriter word-by-word)
    print(f"[AGENT-STREAM] Routing to agent for: {latest_question!r}")

    async def event_generator():
        try:
            from agent.agent import ask_question_streaming  # lazy import — avoids startup hang
            async for event in ask_question_streaming(history, user_ctx):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

        try:
            from db.database import save_token_usage_async
            emp_id = (user_ctx or {}).get("employee_id") or 0
            await save_token_usage_async(
                employee_id=emp_id,
                session_id="ask-ai-stream-session",
                model_name=os.getenv("LLM_MODEL", "qwen/qwen3.6-27b"),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                total_cost_usd=0.0,
                execution_path="llm_stream",
                capability_id="general_query",
                operation="chat_response",
                status="success"
            )
        except Exception as _e:
            print(f"[Telemetry] Stream agent log error: {_e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/usage-logs")
@app.get("/telemetry")
@app.get("/api/ai/usage-logs")
def get_usage_logs_main(limit: int = 50, offset: int = 0):
    """Public/Admin route to fetch recent ai_chatbot_usage telemetry logs."""
    try:
        from db.database import get_db_engine
        from sqlalchemy import text as _text
        engine = get_db_engine()
        with engine.connect() as conn:
            query = _text("""
                SELECT 
                    id, employee_id, session_id, model_name, input_tokens, 
                    output_tokens, total_tokens, total_cost_usd, status, 
                    execution_path, capability_id, operation, backend_execution_ms, created_at
                FROM ai_chatbot_usage
                ORDER BY id DESC
                LIMIT :limit OFFSET :offset
            """)
            rows = conn.execute(query, {"limit": limit, "offset": offset}).mappings().all()
            
            result_logs = []
            for row in rows:
                r = dict(row)
                if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                    r["created_at"] = r["created_at"].isoformat()
                r["total_cost_usd"] = float(r["total_cost_usd"]) if r.get("total_cost_usd") is not None else 0.0
                result_logs.append(r)
                
            return {
                "status": "success",
                "count": len(result_logs),
                "logs": result_logs
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch usage logs: {str(e)}")



# --------------------------------------------------------------------------- #
# Proposal Text Enhancement Endpoint (for EditProposal AI buttons)
# --------------------------------------------------------------------------- #
class EnhanceTextRequest(BaseModel):
    action: str          # "enhance" | "shorter" | "expand" | "replace"
    original_text: str
    slide_number: Optional[str] = None
    instructions: Optional[str] = None

class EnhanceTextResponse(BaseModel):
    success: bool
    enhanced_text: str
    error: Optional[str] = None

@app.post("/enhance-text", response_model=EnhanceTextResponse)
async def enhance_text(request: EnhanceTextRequest):
    """Enhance/shorten/expand proposal text using OpenAI GPT-4o-mini."""
    try:
        from config.llm_factory import get_llm
        llm = get_llm(temperature=0.7, max_tokens=1000)
        
        system_content = (
            "You are a professional proposal writer for a financial services firm. "
            "Return ONLY the requested text improvement without any explanation or preamble. "
            "Never modify placeholders like {{CLIENT_NAME}} or {{DATE}}."
        )

        # Build prompt based on action and instructions
        original = request.original_text
        prompt = f"Action: {request.action}\nOriginal text: {original}\n"
        if request.instructions:
            prompt += f"Instructions: {request.instructions}\n"
        if request.slide_number:
            prompt += f"Slide context: {request.slide_number}\n"

        response = await llm.ainvoke([
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt}
        ])
        result = response.content.strip()
        
        # Strip surrounding quotes if needed
        if result.startswith('"') and result.endswith('"'):
            result = result[1:-1]
        return EnhanceTextResponse(success=True, enhanced_text=result)
    except Exception as e:
        print(f"[enhance-text] Groq error: {e}")
        # fallback to original if undefined, but we define it above now
        fallback = request.original_text if 'request' in locals() else ""
        return EnhanceTextResponse(success=False, enhanced_text=fallback, error=str(e))


class EmailTaskRequest(BaseModel):
    subject: Optional[str] = ""
    text_body: Optional[str] = ""
    html_body: Optional[str] = ""
    outer_from: Optional[str] = ""
    outer_to: Optional[str] = ""
    outer_cc: Optional[str] = ""
    from_email: Optional[str] = None
    sender_email: Optional[str] = None
    to_emails: Optional[str] = None
    text: Optional[str] = None
    html: Optional[str] = None
    attachments: Optional[List[Dict]] = []
    files: Optional[List[Dict]] = []
    employee_id: Optional[int] = 0
    reference_id: Optional[str] = None
    email_url: Optional[str] = None
    web_link: Optional[str] = None
    webLink: Optional[str] = None
    message_id: Optional[str] = None
    email_id: Optional[str] = None
    source_email_id: Optional[str] = None

class EmailLeadRequest(BaseModel):
    subject: Optional[str] = ""
    text_body: Optional[str] = ""
    html_body: Optional[str] = ""
    outer_from: Optional[str] = ""
    outer_to: Optional[str] = ""
    outer_cc: Optional[str] = ""
    from_email: Optional[str] = None
    sender_email: Optional[str] = None
    to_emails: Optional[str] = None
    text: Optional[str] = None
    html: Optional[str] = None
    attachments: Optional[List[Dict]] = []
    files: Optional[List[Dict]] = []
    employee_id: Optional[int] = 0
    context: Optional[Dict] = {}
    reference_id: Optional[str] = None
    email_url: Optional[str] = None
    web_link: Optional[str] = None
    webLink: Optional[str] = None
    message_id: Optional[str] = None
    email_id: Optional[str] = None
    source_email_id: Optional[str] = None

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def verify_internal_api_key(api_key: str = Security(api_key_header)):
    expected_key = os.environ.get("INTERNAL_API_KEY", "bhavik-crm-internal-secure-key-2026")
    if api_key != expected_key:
        raise HTTPException(status_code=401, detail="Unauthorized - Invalid API Key")
    return api_key

@app.post("/api/extract-email-task", dependencies=[Depends(verify_internal_api_key)])
@app.post("/extract-email-task", dependencies=[Depends(verify_internal_api_key)])
async def extract_email_task(request: EmailTaskRequest):
    try:
        from agent.email_parser import strip_html_to_text, parse_forwarded_email, classify_sender, extract_entities_with_llm
        
        import urllib.parse
        emp_id_str = str(request.employee_id or 0)
        subj_clean = (request.subject or 'email').strip()
        resolved_ref_id = (
            request.reference_id or 
            request.email_url or 
            request.web_link or 
            request.webLink or 
            request.message_id or 
            request.email_id or 
            f"email_{emp_id_str}_{urllib.parse.quote(subj_clean)}"
        )

        # Resolve field alias fallbacks
        sender_val = request.outer_from or request.sender_email or request.from_email or ""
        to_val = request.outer_to or request.to_emails or ""
        html_val = request.html_body or request.html or ""
        text_val = request.text_body or request.text or ""

        # 1. Clean HTML
        clean_text = strip_html_to_text(html_val)
        if not clean_text:
            clean_text = text_val
            
        # 2. Parse Forwarded structure
        parsed_email = parse_forwarded_email(
            request.subject, 
            clean_text, 
            sender_val, 
            to_val
        )
        
        # 3. Determine Context
        is_forwarded = parsed_email.get("isForwarded", False)
        
        real_sender = ""
        if is_forwarded and parsed_email.get("originalFromEmail"):
            real_sender = parsed_email.get("originalFromEmail")
        else:
            real_sender = parsed_email.get("forwarderEmail") or sender_val
            
        sender_type = classify_sender(real_sender)
        
        # 4. Build prompt text
        if is_forwarded:
            prompt_text = (parsed_email.get('originalBody') or clean_text).strip()
            if not prompt_text:
                prompt_text = clean_text
        else:
            prompt_text = clean_text

        # 5. Extract with LLM (Vision & Text)
        all_attachments = []
        if request.attachments:
            all_attachments.extend(request.attachments)
        if request.files:
            all_attachments.extend(request.files)
            
        import time
        import asyncio
        from db.database import save_ai_email_parsing_async, check_duplicate_message_id
        from agent.email_parser import parse_email_addresses
        
        # 4b. Pre-check for duplicate Message ID BEFORE calling cloud LLM
        if resolved_ref_id and not str(resolved_ref_id).startswith("draft_"):
            dup_result = check_duplicate_message_id(resolved_ref_id)
            has_valid_customer = dup_result and dup_result.get("customer_name") and "information for your" not in str(dup_result.get("customer_name")).lower()
            if dup_result and has_valid_customer:
                print(f"[extract_email_task] Valid duplicate Message ID detected: {resolved_ref_id}. Skipping LLM.")
                dup_result["sender_type"] = sender_type
                dup_result["sender_email"] = real_sender
                dup_result["to_emails"] = parse_email_addresses(to_val)
                dup_result["cc_emails"] = parse_email_addresses(request.outer_cc)
                dup_result["is_forwarded"] = is_forwarded
                return {
                    "extractedData": dup_result,
                    "sender": parsed_email.get("originalFrom") or request.outer_from if is_forwarded else request.outer_from,
                    "subject": request.subject,
                    "cached": True
                }
            elif dup_result:
                print(f"[extract_email_task] Hollow or stale duplicate record for {resolved_ref_id}. Bypassing cache to execute full dynamic entity extraction.")

        start_time = time.time()
        
        json_result = extract_entities_with_llm(
            text=prompt_text, 
            sender_type=sender_type, 
            is_forwarded=is_forwarded, 
            attachments=all_attachments, 
            employee_id=request.employee_id,
            reference_id=resolved_ref_id,
            sender_email=real_sender,
            to_emails=request.outer_to,
            subject=request.subject
        )
        
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Determine status and extract meta
        meta = json_result.pop("_meta", {}) if isinstance(json_result, dict) else {}
        total_atts = meta.get("total_attachments", 0)
        parsed_atts = meta.get("parsed_attachments", 0)
        model_name = meta.get("model_name")
        if not model_name or model_name == "unknown":
            model_name = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "qwen/qwen3.6-27b"
        token_tracking = meta.get("token_tracking", {})
        
        if not json_result or not isinstance(json_result, dict) or "intent" not in json_result:
            processing_status = "FAILED"
        elif total_atts > 0 and parsed_atts < total_atts:
            processing_status = "PARTIAL_SUCCESS"
        else:
            processing_status = "SUCCESS"
            
        json_result["processing_status"] = processing_status
        
        # Get confidence metrics
        conf_score_raw = json_result.get("confidence_score")
        try:
            conf_score = int(conf_score_raw) if conf_score_raw is not None else None
        except:
            conf_score = None
            
        # Telemetry is directly saved in agent/email_parser.py (extract_entities_with_llm)
        
        # 6. Server-side enrichments for Node.js
        json_result["sender_type"] = sender_type
        json_result["sender_email"] = real_sender
        
        # Determine emails
        json_result["to_emails"] = parse_email_addresses(to_val)
        json_result["cc_emails"] = parse_email_addresses(request.outer_cc)
        json_result["is_forwarded"] = is_forwarded
        json_result["forwarded_by_email"] = parsed_email.get("forwarderEmail") if is_forwarded else None
        
        # Preserve original attachments payload for UI display and post-approval S3 upload
        if all_attachments:
            json_result["attachments"] = all_attachments
        
        # Validate project name like Node.js did
        import re
        project_name = json_result.get("project_name")
        if project_name:
            trimmed = project_name.strip()
            if len(trimmed) < 6 or re.match(r'^[A-Z]{2,4}$', trimmed):
                json_result["project_name"] = None
        
        raw_body_content = request.html_body or request.text_body or clean_text
        json_result["email_body"] = raw_body_content
        return {
            "extractedData": json_result,
            "rawEmailBody": raw_body_content,
            "sender": parsed_email.get("originalFrom") or request.outer_from if is_forwarded else request.outer_from,
            "subject": request.subject,
            "reference_id": resolved_ref_id,
            "message_id": resolved_ref_id,
            "source_email_id": resolved_ref_id
        }
        
    except Exception as e:
        print(f"Error extracting email task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Endpoint for AI Lead Extraction via agent/lead_parser.py
@app.post("/api/extract-email-lead", dependencies=[Depends(verify_internal_api_key)])
async def extract_email_lead(request: EmailLeadRequest):
    try:
        from agent.lead_parser import extract_lead_from_email
        
        json_result = await extract_lead_from_email(
            subject=request.subject,
            html_body=request.html_body,
            text_body=request.text_body,
            outer_from=request.outer_from,
            outer_to=request.outer_to,
            context=request.context,
            employee_id=request.employee_id
        )

        return {
            "success": True,
            "extractedData": json_result,
            "subject": request.subject,
            "sender": request.outer_from
        }
    except Exception as e:
        print(f"Error extracting email lead: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/email-tasks/badge-status")
@app.get("/email-tasks/badge-status")
async def get_email_tasks_badge_status(employee_id: Optional[int] = 0):
    """
    Returns real-time unread/assigned task badge metrics for header navigation & inbox badge icons.
    """
    try:
        engine = get_db_engine()
        unread_count = 0
        assigned_to_me_count = 0
        
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT COUNT(*) FROM ai_email_parsing WHERE document_type = 'email_task' AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"
            )).fetchone()
            if row:
                unread_count = row[0] or 0
                
            if employee_id and employee_id > 0:
                row_emp = conn.execute(text(
                    "SELECT COUNT(*) FROM ai_email_parsing WHERE employee_id = :emp_id AND document_type = 'email_task' AND created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"
                ), {"emp_id": employee_id}).fetchone()
                if row_emp:
                    assigned_to_me_count = row_emp[0] or 0

        return {
            "success": True,
            "has_new_task": unread_count > 0,
            "unread_count": unread_count,
            "assigned_to_me_count": assigned_to_me_count,
            "badge_text": f"{unread_count} New" if unread_count > 0 else "0 New",
            "badge_type": "info" if unread_count > 0 else "default"
        }
    except Exception as e:
        print(f"Error fetching email task badge status: {e}")
        return {
            "success": False,
            "has_new_task": False,
            "unread_count": 0,
            "assigned_to_me_count": 0,
            "badge_text": "0 New",
            "badge_type": "default"
        }


class EmailDraftFeedbackRequest(BaseModel):
    reference_id: str
    action_status: str  # 'APPROVED', 'DISCARDED', 'CONVERTED'
    is_task_required: Optional[bool] = True
    was_edited: Optional[bool] = False
    intent_edited: Optional[bool] = False
    customer_edited: Optional[bool] = False
    assignee_edited: Optional[bool] = False
    due_date_edited: Optional[bool] = False
    is_hard_example: Optional[bool] = False
    time_to_action_ms: Optional[int] = None
    human_approved_values: Optional[dict] = None
    reviewed_by_user_id: Optional[Union[int, str]] = None
    reviewed_by_user_name: Optional[str] = None
    reviewed_by_user_email: Optional[str] = None
    include_in_training: Optional[bool] = True


@app.post("/api/v1/email-drafts/feedback")
@app.post("/api/email-drafts/feedback")
@app.post("/email-drafts/feedback")
async def email_draft_feedback(request: EmailDraftFeedbackRequest):
    """
    Receives human feedback (approval, discard, edit diffs) from the frontend EmailTaskPopup modal
    and asynchronously updates the unified ML dataset row in ai_email_ml_dataset.
    """
    try:
        from db.database import update_email_ml_dataset_feedback_async
        asyncio.create_task(update_email_ml_dataset_feedback_async(
            reference_id=request.reference_id,
            action_status=request.action_status,
            is_task_required=request.is_task_required if request.is_task_required is not None else True,
            was_edited=request.was_edited or False,
            intent_edited=request.intent_edited or False,
            customer_edited=request.customer_edited or False,
            assignee_edited=request.assignee_edited or False,
            due_date_edited=request.due_date_edited or False,
            is_hard_example=request.is_hard_example or request.was_edited or False,
            time_to_action_ms=request.time_to_action_ms,
            human_approved_values=request.human_approved_values,
            reviewed_by_user_id=request.reviewed_by_user_id,
            reviewed_by_user_name=request.reviewed_by_user_name,
            reviewed_by_user_email=request.reviewed_by_user_email,
            include_in_training=request.include_in_training if request.include_in_training is not None else True
        ))
        return {"success": True, "message": "Feedback recorded successfully"}
    except Exception as e:
        print(f"[MLFeedback] Error recording feedback: {e}")
        return {"success": False, "message": str(e)}



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
