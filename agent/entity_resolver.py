"""
entity_resolver.py — Production-grade Entity Resolver.
Identifies entities from the LLM, validates them against existing Node.js APIs, 
and returns standardized JSON structures with Exact Matches, Clarifications, or Failures.
"""
import logging
import json
import urllib.request
import urllib.parse
import asyncio
import difflib
from typing import Dict, Any, Tuple, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

import os

CRM_API_BASE = os.getenv('CRM_API_BASE', 'http://localhost:3001/api/v1').rstrip('/')


# Centralized Entity Resolution Thresholds
ENTITY_EXACT_MATCH_CONFIDENCE: float = 1.0
ENTITY_DOMINANT_MATCH_THRESHOLD: float = 0.80
ENTITY_AMBIGUITY_GAP: float = 0.10
ENTITY_MIN_MATCH_THRESHOLD: float = 0.50

from enum import Enum
from pydantic import BaseModel, Field

class ResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    AMBIGUOUS_ENTITY_TYPE = "AMBIGUOUS_ENTITY_TYPE"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"
    BACKEND_ERROR = "BACKEND_ERROR"

class EntityResolutionResult(BaseModel):
    status: ResolutionStatus
    entity_type: Optional[str] = None
    input_value: str
    resolved_id: Optional[Any] = None
    resolved_name: Optional[str] = None
    confidence: float = 0.0
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    candidate_entity_types: List[str] = Field(default_factory=list)
    error_code: Optional[str] = None
    raw_record: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump()
        d["status"] = self.status.value
        d["entity_id"] = self.resolved_id
        d["entity_name"] = self.resolved_name
        d["matches"] = self.candidates
        return d


# Entity to API Mapping
ENTITY_API_MAP = {
    "customer": {"endpoint": "/customer", "id_field": "id", "name_fields": ["customer_name", "cust_code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "project": {"endpoint": "/projects", "id_field": "id", "name_fields": ["name", "code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "proposal": {"endpoint": "/proposal", "id_field": "id", "name_fields": ["subject", "proposal_code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "employee": {"endpoint": "/employee", "id_field": "id", "name_fields": ["employee_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "invoice": {"endpoint": "/invoice", "id_field": "id", "name_fields": ["invoice_no"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "task": {"endpoint": "/project-task", "id_field": "id", "name_fields": ["task_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "lead": {"endpoint": "/saleslead", "id_field": "id", "name_fields": ["company_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "department": {"endpoint": "/master/department", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "office": {"endpoint": "/master/offices", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "service_line": {"endpoint": "/master/service-line", "id_field": "id", "name_fields": ["name", "short_code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 50}
}

# ---------------------------------------------------------------------------
# Aggregate Entity Normalization — zero LLM calls, pure set lookup
# ---------------------------------------------------------------------------
# Canonical sentinel for "apply no filter" (company-wide / all entities)
AGGREGATE_SENTINEL = "__ALL__"

# All aliases that mean "every entity of this type"
_AGGREGATE_ALIASES: frozenset[str] = frozenset({
    "all", "every", "overall", "any", "anyone", "anything", "everyone",
    "everything", "entire", "whole", "total", "combined", "company wide",
    "company-wide", "all service lines", "all service line",
    "all departments", "all department", "all customers", "all customer",
    "all employees", "all employee", "all projects", "all project",
    "all regions", "all offices", "all units", "no filter",
})


def is_aggregate_value(value: str) -> bool:
    """
    Returns True when the value means "include everything / apply no filter".
    Checks canonical aliases and multi-word patterns deterministically.
    """
    if not value:
        return False
    normalized = value.strip().lower()
    if normalized in _AGGREGATE_ALIASES:
        return True
    # Handle patterns like "all <entity_type>s" generically (e.g. "all lines", "all depts")
    if normalized.startswith("all ") and len(normalized) > 4:
        return True
    return False

RESERVED_BUSINESS_VOCABULARY: frozenset[str] = frozenset({
    "360", "customer 360", "fy23", "fy24", "fy25", "fy26", "fy27", "fy28", "fy29", "fy30",
    "fy2023", "fy2024", "fy2025", "fy2026", "fy2027", "fy2028",
    "revenue", "receivables", "pipeline", "proposal", "proposals", "projects", "project",
    "kpi", "recoverability", "dashboard", "summary", "top", "highest", "compare",
    "analysis", "trend", "financial year", "year", "customer", "customers", "client", "clients",
    "active projects", "overdue receivables", "active", "completed", "in progress",
    "service line", "department", "employee", "tasks", "task", "leads", "lead",
    "generated the highest revenue this year", "fy24 and", "fy25 and",
    "generated the highest revenue", "performed best", "highest revenue", "best performing",
    "highest", "lowest", "top 5", "top 10", "top 3", "top 1", "worst performing",
    "most revenue", "highest gross profit", "highest gp", "gross profit", "gp"
})

def is_reserved_business_term(val: str) -> bool:
    """
    Returns True if the string is a reserved business keyword, metric name,
    grammatical ranking phrase, or financial year term that must NEVER be extracted/resolved as an entity value.
    """
    if not val or not isinstance(val, str):
        return True
    clean = val.strip().lower()
    if clean in RESERVED_BUSINESS_VOCABULARY:
        return True
    if clean.isdigit() and clean in ("360", "24", "25", "26"):
        return True
    import re as _re
    if _re.match(r'^(fy\d{2,4}|financial\s*year\s*\d{2,4})$', clean):
        return True
    # Filter out common grammatical ranking, temporal, and metric view phrases
    ranking_phrases = [
        "generated the highest revenue", "performed best", "highest revenue",
        "best performing", "top 5", "top 10", "most revenue", "highest gross profit",
        "highest gp", "generated highest revenue", "performed the best",
        "show gp", "view gp", "gp performance", "show revenue",
        "what is total revenue", "what is total gp", "total revenue", "total gp",
        "what is total receivables", "total receivables", "monthly", "show details", "details"
    ]
    if any(phrase in clean for phrase in ranking_phrases) or clean.startswith("what is total"):
        return True
    return False


import re
import calendar

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

def is_fiscal_year_expression(expr: str) -> bool:
    if not expr or not isinstance(expr, str):
        return False
    clean = expr.strip().lower()
    return bool(
        re.search(r'\bfy\s*\d{2,4}\b', clean)
        or re.search(r'\bfinancial\s*year\s*\d{2,4}\b', clean)
        or re.search(r'\b20\d{2}[-/]20?\d{2}\b', clean)
        or clean in ["this year", "current fy", "last year", "previous fy"]
    )

def extract_all_fiscal_years(fy_input: Optional[str]) -> List[Dict[str, str]]:
    if not fy_input or not isinstance(fy_input, str) or not fy_input.strip():
        return []
    raw_str = fy_input.strip().lower()
    results = []
    tokens = re.findall(r'(?:fy|financial\s*year|fiscal\s*year)?\s*(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?', raw_str)
    for t1, t2 in tokens:
        if not t1:
            continue
        y1_raw = int(t1)
        if y1_raw < 20 and not re.search(r'\bfy\s*' + str(y1_raw), raw_str):
            continue
        full_y1 = 2000 + y1_raw if y1_raw < 100 else y1_raw
        if t2:
            y2_raw = int(t2)
            full_y2 = 2000 + y2_raw if y2_raw < 100 else y2_raw
            start_year, end_year = full_y1, full_y2
        else:
            # Single FY number like FY24 means FY ending in 2024 (2023-10-01 to 2024-09-30)
            start_year, end_year = full_y1 - 1, full_y1
        if end_year <= start_year:
            end_year = start_year + 1
        short_start = str(start_year)[-2:]
        short_end = str(end_year)[-2:]
        res = {
            "financial_year": f"FY{short_end}",
            "canonical_fy": f"FY {start_year}-{short_end}",
            "start_date": f"{start_year}-10-01",
            "end_date": f"{end_year}-09-30"
        }
        if res not in results:
            results.append(res)
    return results

def get_current_fiscal_year(dt: Optional[datetime] = None) -> Dict[str, str]:
    from datetime import datetime as dt_class
    if dt is None:
        dt = dt_class.now()
    if dt.month >= 10:
        start_year = dt.year
        end_year = dt.year + 1
    else:
        start_year = dt.year - 1
        end_year = dt.year
    short_start = str(start_year)[-2:]
    short_end = str(end_year)[-2:]
    return {
        "financial_year": f"FY{short_start}",
        "canonical_fy": f"FY {start_year}-{short_end}",
        "start_date": f"{start_year}-10-01",
        "end_date": f"{end_year}-09-30"
    }

def resolve_fiscal_year(fy_input: Optional[str] = None) -> Dict[str, Any]:
    all_fys = extract_all_fiscal_years(fy_input)
    if all_fys:
        all_fys_sorted = sorted(all_fys, key=lambda x: x["start_date"])
        primary = all_fys_sorted[-1].copy()
        primary["all_fiscal_years"] = all_fys_sorted
        return primary
    return get_current_fiscal_year()

def parse_scope_time_filter(time_str: str) -> Dict[str, str]:
    """
    Deterministically parses user time expressions into start_date, end_date, and financial_year boundaries.
    Uses canonical resolve_temporal_scope for guaranteed parity across all modules.
    """
    if not time_str or not isinstance(time_str, str):
        return {}

    ts = time_str.strip().lower()
    from agent.temporal_resolver import resolve_temporal_scope
    resolved = resolve_temporal_scope(ts)
    if resolved.get("is_explicit"):
        return {
            "start_date": resolved["start_date"],
            "end_date": resolved["end_date"],
            "financial_year": resolved.get("financial_year", ""),
            "temporal_scope": resolved.get("temporal_scope", "")
        }

    res = {}

    # 1. Centralized Fiscal Year Resolver
    if is_fiscal_year_expression(ts):
        fy_info = resolve_fiscal_year(ts)
        res["financial_year"] = fy_info["financial_year"]
        res["canonical_fy"] = fy_info["canonical_fy"]
        res["start_date"] = fy_info["start_date"]
        res["end_date"] = fy_info["end_date"]

    # 2. Quarter Matcher (e.g. Q1, Q2, Q3, Q4)
    q_match = re.search(r'\bq([1-4])\b', ts)
    if q_match:
        q_num = int(q_match.group(1))
        base_y1 = int(res.get("financial_year", "FY2025-2026").split("-")[0].replace("FY", "")) if "financial_year" in res else 2025
        base_y2 = base_y1 + 1
        if q_num == 1:
            res["start_date"] = f"{base_y1}-10-01"
            res["end_date"] = f"{base_y1}-12-31 23:59:59"
        elif q_num == 2:
            res["start_date"] = f"{base_y2}-01-01"
            res["end_date"] = f"{base_y2}-03-31 23:59:59"
        elif q_num == 3:
            res["start_date"] = f"{base_y2}-04-01"
            res["end_date"] = f"{base_y2}-06-30 23:59:59"
        elif q_num == 4:
            res["start_date"] = f"{base_y2}-07-01"
            res["end_date"] = f"{base_y2}-09-30 23:59:59"

    # 3. Single Month or Month Range Matcher
    curr_year = datetime.now().year
    m_between = re.search(r'between\s+([a-z]+)\s+and\s+([a-z]+)(?:\s+([0-9]{2,4}))?', ts)
    if m_between:
        m1_name = m_between.group(1)
        m2_name = m_between.group(2)
        raw_y = m_between.group(3)
        if raw_y:
            y_num = int(raw_y)
            year_val = 2000 + y_num if y_num < 100 else y_num
        else:
            year_val = curr_year
        if m1_name in MONTH_NAMES and m2_name in MONTH_NAMES:
            m1_num = MONTH_NAMES[m1_name]
            m2_num = MONTH_NAMES[m2_name]
            last_day = calendar.monthrange(year_val, m2_num)[1]
            res["start_date"] = f"{year_val}-{m1_num:02d}-01"
            res["end_date"] = f"{year_val}-{m2_num:02d}-{last_day:02d} 23:59:59"
    else:
        m_single = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b(?:\s+([0-9]{2,4}))?', ts)
        if m_single:
            m_name = m_single.group(1)
            raw_y = m_single.group(2)
            if raw_y:
                y_num = int(raw_y)
                year_val = 2000 + y_num if y_num < 100 else y_num
            else:
                year_val = curr_year
            m_num = MONTH_NAMES[m_name]
            last_day = calendar.monthrange(year_val, m_num)[1]
            res["start_date"] = f"{year_val}-{m_num:02d}-01"
            res["end_date"] = f"{year_val}-{m_num:02d}-{last_day:02d} 23:59:59"

    # 4. Date Range Matcher (e.g. 01-10-2025 to 30-09-2026 or 2025-10-01 to 2026-09-30)
    dr_match = re.search(r'(\d{2,4}[-/]\d{2}[-/]\d{2,4})\s+(?:to|until|-)\s+(\d{2,4}[-/]\d{2}[-/]\d{2,4})', ts)
    if dr_match:
        d1, d2 = dr_match.group(1), dr_match.group(2)
        def _norm(d):
            parts = re.split(r'[-/]', d)
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                else:
                    return f"{parts[2]}-{int(parts[1]):02d}-{int(parts[0]):02d}"
            return d
        res["start_date"] = _norm(d1)
        res["end_date"] = _norm(d2)

    return res


async def fetch_entity_from_api(
    entity_type: str,
    search_query: str,
    jwt_token: str,
    page_size: Optional[int] = None,
    page: int = 1
) -> Dict[str, Any]:
    """
    Makes the actual HTTP GET call to the CRM backend.
    Runs in a thread to prevent blocking the async loop.
    Supports optional page_size override and page pagination for candidate discovery.
    """
    map_info = ENTITY_API_MAP.get(entity_type.lower())
    if not map_info:
        return {"error": f"Unknown entity type: {entity_type}"}

    search_param = map_info.get("search_parameter", "search")
    search_format = map_info.get("search_payload_format", "string")
    search_key = map_info.get("search_key", "search")
    limit = page_size if page_size is not None else map_info.get("default_limit", 5)

    if search_format == "json":
        payload = {search_key: search_query}
        payload_str = json.dumps(payload)
        safe_query = urllib.parse.quote(payload_str)
    else:
        safe_query = urllib.parse.quote(search_query)

    url = f"{CRM_API_BASE}{map_info['endpoint']}?{search_param}={safe_query}&pageSize={limit}&page={page}"
    
    logger.info(f"[EntityResolver DEBUG] Outgoing URL: GET {url}")
    logger.info(f"[EntityResolver DEBUG] Outgoing Params: {search_param}={safe_query}&pageSize={limit}&page={page}")

    def _sync_fetch():
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                status = response.status
                body = response.read().decode('utf-8')
                logger.info(f"[EntityResolver DEBUG] Response Status: {status}")
                logger.info(f"[EntityResolver DEBUG] Response Body: {body[:500]}...") # truncate for safety
                return json.loads(body)
        except urllib.error.HTTPError as e:
            logger.error(f"[EntityResolver DEBUG] Response Status: {e.code}")
            try:
                err_body = e.read().decode('utf-8')
                logger.error(f"[EntityResolver DEBUG] Response Body: {err_body}")
            except:
                pass
            logger.error(f"[EntityResolver] Backend HTTP {e.code}: {e.reason}")
            fallback_data = _db_lookup_master_entity(entity_type, search_query)
            if fallback_data:
                return {"rows": fallback_data}
            return {"error": f"API Error: {e.code}"}
        except Exception as e:
            logger.error(f"[EntityResolver] Backend Request Failed: {str(e)}")
            fallback_data = _db_lookup_master_entity(entity_type, search_query)
            if fallback_data:
                return {"rows": fallback_data}
            return {"error": f"Connection Failed: {str(e)}"}

    return await asyncio.to_thread(_sync_fetch)

def _db_lookup_master_entity(entity_type: str, search_query: str) -> List[Dict[str, Any]]:
    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        e_type = (entity_type or "").lower()
        with engine.connect() as conn:
            if e_type in ["service_line", "serviceline"]:
                rows = conn.execute(text("SELECT id, name, short_code FROM m_serviceline WHERE is_active = 1")).fetchall()
                return [{"id": r[0], "name": r[1], "short_code": r[2]} for r in rows]
            elif e_type == "department":
                rows = conn.execute(text("SELECT id, name, code FROM m_department WHERE is_active = 1")).fetchall()
                return [{"id": r[0], "name": r[1], "code": r[2]} for r in rows]
            elif e_type == "employee" and search_query:
                from agent.query_parser import _lookup_employee_by_name
                emp_match = _lookup_employee_by_name(search_query)
                if emp_match:
                    return [{"id": emp_match[0], "employee_name": emp_match[1]}]
    except Exception as err:
        logger.warning(f"[EntityResolver DB Fallback Error] {err}")
    return []

def _build_display_name(record: Dict[str, Any], name_fields: List[str]) -> str:
    """Builds a human-readable name from the API record using the primary mapped name field."""
    if not record or not isinstance(record, dict):
        return "Unknown"
    # Prioritize primary canonical name fields
    for key in ["name", "employee_name", "customer_name"]:
        val = record.get(key)
        if val:
            return str(val).strip()
    for f in name_fields:
        val = record.get(f)
        if val:
            return str(val).strip()
    return "Unknown"

EMPLOYEE_TRIGGERS = {
    "employee", "consultant", "resource", "staff", "person", "user", 
    "developer", "manager", "partner", "named", "generated by", 
    "for employee", "by employee"
}

def has_employee_trigger(query: str) -> bool:
    """Check if the user query explicitly contains an employee trigger phrase or preposition reference."""
    if not query:
        return False
    q_lower = query.lower()
    if any(trigger in q_lower for trigger in EMPLOYEE_TRIGGERS):
        return True
    # For GP performance or service line / department queries, generic "for <value>" is NOT an employee trigger
    if "gp" in q_lower or "gross profit" in q_lower or "gp_performance" in q_lower:
        return False
    # Check if query contains "for <name>", "of <name>", or "by <name>"
    m = re.search(r'\b(?:for|of|by)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)*)\b', query, re.IGNORECASE)
    if m:
        cand = m.group(1).strip().lower()
        from agent.entity_resolver import is_reserved_business_term
        if is_reserved_business_term(cand) or cand in ("service_line", "serviceline", "department", "tech", "audit", "tax", "brs", "bps", "growth", "legal", "advisory"):
            return False
        return True
    return False

async def resolve_entities(extracted_entities: List[Dict[str, str]], jwt_token: str, full_query: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Resolves multiple entities concurrently using Context-Aware Entity Resolution Priority.
    Returns a tuple: (list of resolved entities, list of structured entity errors).
    """
    if not extracted_entities:
        return [], []

    has_emp_trigger = has_employee_trigger(full_query)
    q_lower = full_query.lower() if full_query else ""
    is_report_query = any(w in q_lower for w in ["report", "revenue", "recoverability", "kpi", "receivables", "pipeline", "billing", "analytics"])

    logger.info(f"[EntityResolver DEBUG] Intent: is_report_query={is_report_query}, has_employee_trigger={has_emp_trigger} | Full Query: '{full_query}'")

    # Sanitise and deduplicate extracted_entities for report queries
    sanitised_entities = []
    seen_keys = set()

    for entity in extracted_entities:
        e_type = entity.get("type", "").lower()
        e_value = entity.get("value", "").strip()

        if not e_value or is_reserved_business_term(e_value):
            logger.info(f"[EntityResolver] Skipped reserved business term or empty value: '{e_value}'")
            continue

        key = f"{e_type}:{e_value.lower()}"
        if key not in seen_keys:
            seen_keys.add(key)
            sanitised_entities.append({"type": e_type, "value": e_value})

    tasks = []
    for entity in sanitised_entities:
        e_type = entity.get("type", "").lower()
        e_value = entity.get("value", "")

        map_info = ENTITY_API_MAP.get(e_type)
        endpoint = map_info.get("endpoint", "N/A") if map_info else "N/A"
        logger.info(f"[EntityResolver DEBUG] Extracted entity: '{e_value}' | Entity type: '{e_type}' | Selected API endpoint: {CRM_API_BASE}{endpoint}")
            
        tasks.append(_resolve_single_entity_with_priority(e_type, e_value, jwt_token, is_report_query, has_emp_trigger))
        
    results = await asyncio.gather(*tasks)
    
    resolved = []
    entity_errors = []
    
    for res in results:
        status = res.get("status")
        e_type = res.get("entity_type", "Entity")
        query = res.get("query", "Unknown")
        
        if status == "aggregate":
            # Aggregate means "no filter" — treat as a valid resolved entity with __ALL__ sentinel.
            # Never raise an entity error for aggregate values.
            resolved.append(res)
        elif status == "clarification_required":
            entity_errors.append({
                "type": "entity_error",
                "error_type": "multiple_matches",
                "entity_type": e_type,
                "query": query,
                "matches": res.get("matches", [])
            })
        elif status == "not_found":
            entity_errors.append({
                "type": "entity_error",
                "error_type": "not_found",
                "entity_type": e_type,
                "query": query
            })
        elif status == "error":
            entity_errors.append({
                "type": "entity_error",
                "error_type": "api_error",
                "entity_type": e_type,
                "query": query,
                "message": res.get("message")
            })
        elif status not in ("pass_through",):
            resolved.append(res)
            
    logger.info(f"[ENTITY_RESOLVED] count={len(resolved)} | resolved={resolved} | errors={entity_errors}")
    return resolved, entity_errors


def _normalize_name_string(text: str) -> str:
    """Normalizes entity name strings for robust, case-insensitive, punctuation-agnostic matching."""
    if not text:
        return ""
    clean = str(text).lower().strip()
    # Remove dots from abbreviations (e.g. W.L.L. -> wll)
    clean = re.sub(r'\.', '', clean)
    # Strip corporate suffixes
    for suffix in [r'\bwll\b', r'\bllc\b', r'\bcorp\b', r'\bcorporation\b', r'\bltd\b', r'\blimited\b', r'\binc\b']:
        clean = re.sub(suffix, '', clean)
    # Strip remaining non-alphanumeric characters and collapse whitespace
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def _score_record_match(record: Dict[str, Any], map_info: Dict[str, Any], query_str: str) -> Tuple[float, str]:
    """
    Scores how closely a record matches the search query string using token-aware fuzzy matching.
    Returns (confidence_score, matched_display_name).
    """
    query_raw = query_str.strip()
    query_norm = _normalize_name_string(query_raw)
    name_fields = map_info.get("name_fields", [])
    display_name = _build_display_name(record, name_fields)
    
    # 1. Exact ID or Code Match
    id_val = str(record.get(map_info.get("id_field", "id"), "")).strip()
    if id_val and id_val.lower() == query_raw.lower():
        return 1.0, display_name

    if not query_norm:
        return 0.0, display_name

    in_tokens = [t for t in query_norm.split() if t]
    if not in_tokens:
        return 0.0, display_name

    best_score = 0.0

    for f in name_fields:
        val = record.get(f)
        if not val:
            continue
        val_str = str(val).strip()
        val_norm = _normalize_name_string(val_str)
        if not val_norm:
            continue

        # 2. Exact Raw Field Match
        if val_str.lower() == query_raw.lower():
            return 0.99, display_name
            
        # 3. Exact Normalized Field Match
        if val_norm == query_norm:
            return 0.98, display_name

        cand_tokens = [t for t in val_norm.split() if t]
        if not cand_tokens:
            continue

        # 4. Token-aware similarity calculation
        token_sim_scores = []
        exact_matches = 0

        for it in in_tokens:
            best_token_sim = 0.0
            is_exact = False
            for ct in cand_tokens:
                if it == ct:
                    best_token_sim = 1.0
                    is_exact = True
                    break
                else:
                    ratio = difflib.SequenceMatcher(None, it, ct).ratio()
                    # Prefix/suffix/edit-distance bonus if token length >= 3
                    if (len(it) >= 3 and ct.startswith(it)) or (len(ct) >= 3 and it.startswith(ct)) or (len(it) >= 3 and ct.endswith(it)) or (len(ct) >= 3 and it.endswith(ct)):
                        ratio = max(ratio, 0.88)
                    elif len(it) >= 3 and len(ct) >= 3 and abs(len(it) - len(ct)) <= 1 and ratio >= 0.75:
                        # E.g. UDIT (4 chars) vs AUDIT (5 chars) -> ratio is 0.80, edit distance 1
                        ratio = max(ratio, 0.88)
                    if ratio > best_token_sim:
                        best_token_sim = ratio
            
            if is_exact:
                exact_matches += 1
            token_sim_scores.append(best_token_sim)

        avg_token_sim = sum(token_sim_scores) / len(token_sim_scores) if token_sim_scores else 0.0
        exact_ratio = exact_matches / len(in_tokens)

        # Whole-string similarity
        whole_sim = difflib.SequenceMatcher(None, query_norm, val_norm).ratio()

        # Composite score calculation (45% whole-string, 45% token-avg, 10% exact bonus)
        composite = (0.45 * whole_sim) + (0.45 * avg_token_sim) + (0.10 * exact_ratio)

        # If all input tokens have high token similarity (>= 0.75), ensure composite reaches dominant threshold
        if all(s >= 0.75 for s in token_sim_scores) and composite >= 0.65:
            composite = max(composite, 0.85)

        # Token count parity bonus/penalty for single-token queries
        if len(in_tokens) == 1 and len(cand_tokens) == 1:
            composite = min(1.0, composite + 0.08)
        elif len(in_tokens) == 1 and len(cand_tokens) > 1:
            composite = max(0.50, composite - 0.10)

        # Cap composite score at 0.97
        composite = min(round(composite, 2), 0.97)

        if composite > best_score:
            best_score = composite

    return best_score, display_name

async def _resolve_single_entity_with_priority(e_type: str, e_value: str, jwt_token: str, is_report_query: bool, has_emp_trigger: bool) -> Dict[str, Any]:
    """
    Resolves a single entity with context-aware fallback priorities.
    """
    # 1. Primary Attempt
    res = await _resolve_single_entity(e_type, e_value, jwt_token)
    if res.get("status") in ["resolved", "pass_through"]:
        return res

    # 2. Report Context Priority Fallbacks (service_line -> customer -> department -> project)
    if is_report_query and not has_emp_trigger and res.get("status") in ["not_found", "clarification_required", "error"]:
        fallback_order = [t for t in ["service_line", "customer", "department", "project"] if t != e_type]
        for fallback_type in fallback_order:
            logger.info(f"[EntityResolver] Fallback testing '{fallback_type}' for query '{e_value}'...")
            fb_res = await _resolve_single_entity(fallback_type, e_value, jwt_token)
            if fb_res.get("status") in ["resolved", "pass_through"]:
                return fb_res

    # 3. Employee resolution as absolute last resort if explicit trigger was present
    if has_emp_trigger and e_type != "employee" and res.get("status") == "not_found":
        fb_emp = await _resolve_single_entity("employee", e_value, jwt_token)
        if fb_emp.get("status") in ["resolved", "pass_through"]:
            return fb_emp

    return res

async def _resolve_single_entity(e_type: str, e_value: str, jwt_token: str) -> Dict[str, Any]:
    """
    Internal single-type entity resolution function with generic candidate discovery.
    1. Direct search attempt.
    2. Multi-token candidate discovery with pagination and deduplication.
    3. Multi-pass token-aware fuzzy match scoring.
    """
    map_info = ENTITY_API_MAP.get(e_type.lower())
    if not map_info:
        return {
            "status": "error",
            "entity_type": e_type,
            "message": f"Unsupported entity type '{e_type}'"
        }
        
    # 1. Normalization & Sentinel Check
    if not e_value or not e_value.strip():
        return {
            "status": "not_found",
            "entity_type": e_type,
            "entity_value": e_value
        }
        
    if is_aggregate_value(e_value):
        return {
            "status": "aggregate",
            "entity_type": e_type,
            "entity_value": AGGREGATE_SENTINEL,
        }

    if e_type in ("customer", "employee") and e_value.strip().lower() in ["audit", "udit", "tax", "bps", "brs", "growth", "legal", "tech", "audit gp", "audit sme", "audit support"]:
        return {
            "status": "not_found",
            "entity_type": e_type,
            "entity_value": e_value
        }

    # Helper to extract row list from API response formats
    def _extract_rows(resp: Any) -> list:
        if isinstance(resp, dict):
            if "rows" in resp:
                return resp["rows"]
            elif "data" in resp:
                return resp["data"]
            elif resp and "error" not in resp:
                return [resp]
        elif isinstance(resp, list):
            return resp
        return []

    # 2. Candidate Discovery Stage
    search_strategy = "exact_direct"
    response = await fetch_entity_from_api(e_type, e_value, jwt_token, page_size=20)
    
    if "error" in response:
        return {
            "status": "error",
            "entity_type": e_type.capitalize(),
            "query": e_value,
            "message": response["error"]
        }

    data = _extract_rows(response)

    # Check if primary response contains an exact/dominant match (>= 0.98)
    has_exact = False
    if data:
        for r in data:
            sc, _ = _score_record_match(r, map_info, e_value)
            if sc >= 0.98:
                has_exact = True
                break

    # If no exact candidate, execute structured token-based candidate discovery
    if not has_exact and e_value.strip():
        search_strategy = "token_multi_pass"
        tokens = [t for t in _normalize_name_string(e_value).split() if len(t) >= 2 and not is_reserved_business_term(t)]
        combined_records = []
        seen_ids = set()

        # Seed candidate pool with primary results
        for rec in data:
            if isinstance(rec, dict):
                rec_id = str(rec.get(map_info.get("id_field", "id"), "")).strip() or _build_display_name(rec, map_info.get("name_fields", []))
                if rec_id and rec_id not in seen_ids:
                    seen_ids.add(rec_id)
                    combined_records.append(rec)

        # Multi-token pagination search (pageSize=50)
        for tok in tokens:
            for page_num in range(1, 3):  # Paginate up to 2 pages (100 candidates per token)
                tok_resp = await fetch_entity_from_api(e_type, tok, jwt_token, page_size=50, page=page_num)
                tok_data = _extract_rows(tok_resp)
                if not tok_data:
                    break
                for rec in tok_data:
                    if isinstance(rec, dict):
                        rec_id = str(rec.get(map_info.get("id_field", "id"), "")).strip() or _build_display_name(rec, map_info.get("name_fields", []))
                        if rec_id and rec_id not in seen_ids:
                            seen_ids.add(rec_id)
                            combined_records.append(rec)
                if len(tok_data) < 50:
                    break

        # Broad list fallback if token searches yielded < 5 candidates
        if len(combined_records) < 5:
            broad_resp = await fetch_entity_from_api(e_type, "", jwt_token, page_size=50, page=1)
            broad_data = _extract_rows(broad_resp)
            for rec in broad_data:
                if isinstance(rec, dict):
                    rec_id = str(rec.get(map_info.get("id_field", "id"), "")).strip() or _build_display_name(rec, map_info.get("name_fields", []))
                    if rec_id and rec_id not in seen_ids:
                        seen_ids.add(rec_id)
                        combined_records.append(rec)

        data = combined_records

    # Log candidate discovery telemetry
    logger.info(
        f"[ENTITY_DISCOVERY] type={e_type} input=\"{e_value}\" "
        f"search_strategy={search_strategy} backend_candidates={len(data)}"
    )

    # 2.1 Deterministic Service-Line Match (Exact unique short_code or exact unique name)
    if e_type.lower() == "service_line" and data:
        clean_val = e_value.strip().lower()
        
        # Check exact short_code match first
        sc_matches = [r for r in data if isinstance(r, dict) and str(r.get("short_code", "")).strip().lower() == clean_val]
        if len(sc_matches) == 1:
            best_rec = sc_matches[0]
            res_id = best_rec.get(map_info["id_field"])
            res_name = str(best_rec.get("name", "")).strip() or _build_display_name(best_rec, map_info["name_fields"])
            logger.info(
                f"[ENTITY_RESOLUTION] type=service_line candidate_count=1 "
                f"status=RESOLVED resolved_id={res_id} confidence=1.0 match_field=short_code canonical_name=\"{res_name}\""
            )
            return {
                "status": "resolved",
                "entity_type": "Service_line",
                "entity_name": res_name,
                "resolved_name": res_name,
                "entity_id": res_id,
                "resolved_id": res_id,
                "input_value": e_value,
                "confidence": 1.0,
                "match_field": "short_code",
                "raw_record": best_rec
            }

        # Check exact name match second (including canonical aliases like udit -> audit)
        name_matches = [
            r for r in data if isinstance(r, dict) and (
                str(r.get("name", "")).strip().lower() == clean_val
                or (clean_val in ["audit", "udit"] and str(r.get("name", "")).strip().lower() == "audit")
            )
        ]
        if len(name_matches) == 1:
            best_rec = name_matches[0]
            res_id = best_rec.get(map_info["id_field"])
            res_name = str(best_rec.get("name", "")).strip()
            logger.info(
                f"[ENTITY_RESOLUTION] type=service_line candidate_count=1 "
                f"status=RESOLVED resolved_id={res_id} confidence=1.0 match_field=name canonical_name=\"{res_name}\""
            )
            return {
                "status": "resolved",
                "entity_type": "Service_line",
                "entity_name": res_name,
                "resolved_name": res_name,
                "entity_id": res_id,
                "resolved_id": res_id,
                "input_value": e_value,
                "confidence": 1.0,
                "match_field": "name",
                "raw_record": best_rec
            }

    # 2.2 Deterministic Employee First-Name Match
    if e_type.lower() == "employee" and data:
        clean_val = e_value.strip().lower()
        fn_matches = [
            r for r in data if isinstance(r, dict) and r.get("employee_name") and str(r["employee_name"]).strip().split()[0].lower() == clean_val
        ]

        if len(fn_matches) == 1:
            best_rec = fn_matches[0]
            res_id = best_rec.get(map_info["id_field"])
            res_name = str(best_rec.get("employee_name", "")).strip()
            logger.info(
                f"[ENTITY_RESOLUTION] type=employee candidate_count=1 "
                f"status=RESOLVED resolved_id={res_id} confidence=1.0 match_field=first_name canonical_name=\"{res_name}\""
            )
            return {
                "status": "resolved",
                "entity_type": "Employee",
                "entity_name": res_name,
                "resolved_name": res_name,
                "entity_id": res_id,
                "resolved_id": res_id,
                "input_value": e_value,
                "confidence": 1.0,
                "match_field": "first_name",
                "raw_record": best_rec
            }
        elif len(fn_matches) > 1:
            logger.info(
                f"[ENTITY_RESOLUTION] type=employee candidate_count={len(fn_matches)} status=AMBIGUOUS resolved_id=None match_field=first_name"
            )
            matches_list = []
            for r in fn_matches:
                matches_list.append({
                    "entity_id": r.get(map_info["id_field"]),
                    "entity_name": str(r.get("employee_name", "")).strip(),
                    "confidence": 0.95
                })
            return {
                "status": "clarification_required",
                "entity_type": "Employee",
                "query": e_value,
                "input_value": e_value,
                "matches": matches_list,
                "candidates": matches_list
            }

    # 3. Score complete candidate pool using token-aware scoring
    scored_results = []
    for record in data:
        score, display_name = _score_record_match(record, map_info, e_value)
        if score >= 0.50:
            scored_results.append((score, record, display_name))

    if not scored_results:
        logger.info(f"[ENTITY_RESOLUTION] type={e_type} candidate_count=0 status=NOT_FOUND resolved_id=None confidence=0.0")
        return {
            "status": "not_found",
            "entity_type": e_type.capitalize(),
            "query": e_value,
            "input_value": e_value
        }

    scored_results.sort(key=lambda x: x[0], reverse=True)
    best_score, best_record, best_display_name = scored_results[0]
    second_score = scored_results[1][0] if len(scored_results) > 1 else 0.0
    score_gap = best_score - second_score

    # Strict Candidate Rule:
    # 0 candidates -> NOT_FOUND (handled above)
    # 1 candidate  -> RESOLVED (a single candidate must NEVER produce multiple_matches)
    # 2+ candidates -> RESOLVED if score_gap >= 0.10, otherwise AMBIGUOUS
    is_dominant = (len(scored_results) == 1 and best_score >= 0.50) or (best_score >= 0.80 and score_gap >= 0.10) or (score_gap >= 0.15 and best_score >= 0.70)

    if best_record and is_dominant:
        res_id = best_record.get(map_info["id_field"])
        logger.info(
            f"[ENTITY_RESOLUTION] type={e_type} candidate_count={len(scored_results)} "
            f"status=RESOLVED resolved_id={res_id} confidence={best_score}"
        )
        return {
            "status": "resolved",
            "entity_type": e_type.capitalize(),
            "entity_name": best_display_name,
            "resolved_name": best_display_name,
            "entity_id": res_id,
            "resolved_id": res_id,
            "input_value": e_value,
            "confidence": best_score,
            "raw_record": best_record
        }

    # 4. AMBIGUOUS MATCH (multiple candidates with close scores)
    logger.info(
        f"[ENTITY_RESOLUTION] type={e_type} candidate_count={len(scored_results)} "
        f"status=AMBIGUOUS resolved_id=None confidence={best_score}"
    )
    matches_list = []
    for score, record, d_name in scored_results[:3]:
        matches_list.append({
            "entity_id": record.get(map_info["id_field"]),
            "entity_name": d_name,
            "confidence": score
        })

    return {
        "status": "clarification_required",
        "entity_type": e_type.capitalize(),
        "query": e_value,
        "input_value": e_value,
        "matches": matches_list
    }


async def resolve_entity(
    input_value: str,
    entity_type: Optional[str] = None,
    jwt_token: str = ""
) -> EntityResolutionResult:
    """
    Production-grade generic entity resolution authority.
    Resolves single-type or multi-type CRM entity inputs against authoritative backend APIs.
    """
    if not input_value or not isinstance(input_value, str) or not input_value.strip():
        return EntityResolutionResult(
            status=ResolutionStatus.INVALID,
            input_value=input_value or "",
            error_code="EMPTY_INPUT"
        )

    clean_input = input_value.strip()
    extracted_candidates = extract_entities_from_text(clean_input)
    search_input = clean_input
    if extracted_candidates:
        search_input = extracted_candidates[0]["value"]
        if not entity_type or str(entity_type).lower() in ("unknown", "generic", "none", "other", "all"):
            entity_type = extracted_candidates[0]["type"]

    # If explicit non-generic entity_type provided, resolve directly
    if entity_type and str(entity_type).lower() not in ("unknown", "generic", "none", "other", "all"):
        e_type_norm = str(entity_type).lower()
        if e_type_norm in ("department", "customer", "employee") and search_input.lower() in ["audit", "udit", "tax", "bps", "brs", "growth", "legal", "tech"]:
            e_type_norm = "service_line"
        res_dict = await _resolve_single_entity(e_type_norm, search_input, jwt_token)
        
        status_str = str(res_dict.get("status", "")).lower()
        if status_str in ("resolved", "pass_through"):
            return EntityResolutionResult(
                status=ResolutionStatus.RESOLVED,
                entity_type=e_type_norm,
                input_value=clean_input,
                resolved_id=res_dict.get("resolved_id") or res_dict.get("entity_id"),
                resolved_name=res_dict.get("resolved_name") or res_dict.get("entity_name") or clean_input,
                confidence=res_dict.get("confidence", 1.0),
                raw_record=res_dict.get("raw_record")
            )
        elif status_str in ("clarification_required", "ambiguous"):
            raw_matches = res_dict.get("matches") or res_dict.get("candidates") or []
            candidates = []
            for m in raw_matches:
                candidates.append({
                    "id": m.get("entity_id") or m.get("id"),
                    "name": m.get("entity_name") or m.get("name"),
                    "confidence": m.get("confidence", 0.0),
                    "entity_type": e_type_norm
                })
            return EntityResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                entity_type=e_type_norm,
                input_value=clean_input,
                candidates=candidates
            )
        elif status_str == "aggregate":
            return EntityResolutionResult(
                status=ResolutionStatus.RESOLVED,
                entity_type=e_type_norm,
                input_value=clean_input,
                resolved_id=AGGREGATE_SENTINEL,
                resolved_name=AGGREGATE_SENTINEL,
                confidence=1.0
            )
        else:
            return EntityResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                entity_type=e_type_norm,
                input_value=clean_input,
                candidates=[]
            )

    # Multi-Entity Type Resolution (e.g. unknown entity_type)
    search_types = ["employee", "customer", "project", "service_line", "department"]
    type_results: Dict[str, Dict[str, Any]] = {}

    for st in search_types:
        s_res = await _resolve_single_entity(st, search_input, jwt_token)
        st_status = str(s_res.get("status", "")).lower()
        if st_status in ("resolved", "clarification_required", "ambiguous"):
            type_results[st] = s_res

    if not type_results:
        logger.info(f"[ENTITY_RESOLUTION] input=\"{clean_input}\" candidate_count=0 status=NOT_FOUND")
        return EntityResolutionResult(
            status=ResolutionStatus.NOT_FOUND,
            input_value=clean_input,
            candidates=[]
        )

    # Check for candidates spanning multiple entity types
    if len(type_results) > 1:
        type_summary = []
        for t_name, t_res in type_results.items():
            st_status = str(t_res.get("status")).lower()
            if st_status == "resolved":
                type_summary.append((t_res.get("confidence", 0.9), t_name, t_res))
            else:
                top_m = max((m.get("confidence", 0.0) for m in t_res.get("matches", [])), default=0.0)
                if top_m > 0:
                    type_summary.append((top_m, t_name, t_res))

        type_summary.sort(key=lambda x: x[0], reverse=True)
        if type_summary:
            best_conf, best_type, best_res = type_summary[0]
            second_conf = type_summary[1][0] if len(type_summary) > 1 else 0.0
            type_gap = best_conf - second_conf

            # Dominant type resolution if confidence gap >= 0.08
            if best_conf >= 0.75 and (len(type_summary) == 1 or type_gap >= 0.08):
                logger.info(f"[ENTITY_RESOLUTION] input=\"{clean_input}\" dominant_type={best_type} conf={best_conf} type_gap={type_gap:.2f}")
                return EntityResolutionResult(
                    status=ResolutionStatus.RESOLVED,
                    entity_type=best_type,
                    input_value=clean_input,
                    resolved_id=best_res.get("resolved_id") or best_res.get("entity_id"),
                    resolved_name=best_res.get("resolved_name") or best_res.get("entity_name") or clean_input,
                    confidence=best_conf,
                    raw_record=best_res.get("raw_record")
                )

        active_types = [t[1] for t in type_summary]
        all_candidates = []
        for t_conf, t_name, t_res in type_summary:
            if str(t_res.get("status")).lower() == "resolved":
                all_candidates.append({
                    "id": t_res.get("resolved_id") or t_res.get("entity_id"),
                    "name": t_res.get("resolved_name") or t_res.get("entity_name"),
                    "confidence": t_conf,
                    "entity_type": t_name
                })
            else:
                for m in t_res.get("matches", []):
                    all_candidates.append({
                        "id": m.get("entity_id") or m.get("id"),
                        "name": m.get("entity_name") or m.get("name"),
                        "confidence": m.get("confidence", 0.8),
                        "entity_type": t_name
                    })

        logger.info(f"[ENTITY_RESOLUTION] input=\"{clean_input}\" candidate_types={active_types} status=AMBIGUOUS_ENTITY_TYPE")
        return EntityResolutionResult(
            status=ResolutionStatus.AMBIGUOUS_ENTITY_TYPE,
            input_value=clean_input,
            candidates=all_candidates,
            candidate_entity_types=active_types
        )

    # Single matching entity type found
    single_type = list(type_results.keys())[0]
    st_res = type_results[single_type]
    st_status = str(st_res.get("status", "")).lower()

    if st_status == "resolved":
        return EntityResolutionResult(
            status=ResolutionStatus.RESOLVED,
            entity_type=single_type,
            input_value=clean_input,
            resolved_id=st_res.get("resolved_id") or st_res.get("entity_id"),
            resolved_name=st_res.get("resolved_name") or st_res.get("entity_name"),
            confidence=st_res.get("confidence", 1.0),
            raw_record=st_res.get("raw_record")
        )
    else:
        raw_matches = st_res.get("matches") or st_res.get("candidates") or []
        candidates = []
        for m in raw_matches:
            candidates.append({
                "id": m.get("entity_id") or m.get("id"),
                "name": m.get("entity_name") or m.get("name"),
                "confidence": m.get("confidence", 0.0),
                "entity_type": single_type
            })
        return EntityResolutionResult(
            status=ResolutionStatus.AMBIGUOUS,
            entity_type=single_type,
            input_value=clean_input,
            candidates=candidates
        )


def extract_entities_from_text(query: str) -> List[Dict[str, str]]:
    """
    Dynamically extracts candidate entity references (customer name, project code, employee, FY) from prompt text.
    Delegates entity validation and resolution to backend API matching.
    """
    if not query or not isinstance(query, str):
        return []

    entities = []
    
    # 0. Explicit Employee reference pattern (e.g. 'employee Shashank', 'staff John', 'kpi report for Shashank Arya')
    emp_match = re.search(r'(?:employee|staff|consultant|partner|manager|person|resource|by)\s+([A-Za-z0-9\s\.\-_&]+)', query, re.IGNORECASE)
    if emp_match:
        val = emp_match.group(1).strip()
        val = re.sub(r'\s+(?:in|for|fy\d+|20\d{2}|what|how|show|get|run|status|got\s+it|please|download|give|button|thanks|ok|okay).*', '', val, flags=re.IGNORECASE).strip()
        val = re.sub(r'[^a-zA-Z0-9\s]', '', val).strip()
        if val and len(val) > 1 and val.lower() not in ["customer", "projects", "proposals", "revenue", "receivables", "all", "the"]:
            entities.append({"type": "employee", "value": val})

    # 1. Customer/Project/Person 'for <name>' reference pattern
    cust_match = re.search(r'(?:for|customer|project)\s+([A-Za-z0-9\s\.\-_&]+)', query, re.IGNORECASE)
    if cust_match:
        val = cust_match.group(1).strip()
        val = re.sub(r'\s+(?:in|for|fy\d+|20\d{2}|what|how|show|get|run|status|got\s+it|please|download|give|button|thanks|ok|okay).*', '', val, flags=re.IGNORECASE).strip()
        clean_val = re.sub(r'[^a-zA-Z0-9\s]', '', val).strip()
        if clean_val and len(clean_val) > 1 and clean_val.lower() not in ["customer", "projects", "proposals", "revenue", "receivables", "all", "the"]:
            val = clean_val
            sl_terms = ["audit", "udit", "tax", "bps", "brs", "growth", "legal", "tech", "audit gp", "audit sme", "audit support"]
            if val.lower() in sl_terms:
                entities.append({"type": "service_line", "value": val})
            else:
                # If 2 words with no company suffixes, candidate could be an employee or customer
                biz_suffixes = ["ltd", "inc", "corp", "solutions", "group", "holdings", "services", "co", "llc", "wll", "w.l.l"]
                is_company = any(s in val.lower() for s in biz_suffixes)
                words = val.split()
                if not is_company and len(words) == 2 and not any(e["value"].lower() == val.lower() for e in entities if e["type"] == "employee"):
                    entities.append({"type": "employee", "value": val})
                elif not any(e["value"].lower() == val.lower() for e in entities if e["type"] == "customer"):
                    entities.append({"type": "customer", "value": val})

    # 2. Prompt Prefix Entity Pattern (e.g. "DOO Technology Solutions - Audit 2025 what is the status of tis project")
    prefix_match = re.search(r'^([A-Za-z0-9\s\.\-_&]+?)(?:\s*(?:what|how|show|get|run|status|is the|which|where|\?))', query, re.IGNORECASE)
    if prefix_match:
        val = prefix_match.group(1).strip()
        if val and len(val) >= 3 and val.lower() not in ["customer", "projects", "proposals", "revenue", "receivables", "all", "the", "show", "get", "what"]:
            entities.append({"type": "project", "value": val})

    # 3. Financial Year pattern (e.g. FY25, FY24)
    fy_match = re.search(r'\b(FY\d{2}|FY\d{4}|20\d{2})\b', query, re.IGNORECASE)
    if fy_match:
        entities.append({"type": "financial_year", "value": fy_match.group(1)})

    return entities


def resolve_ambiguity_selection(
    user_input: str,
    candidates: List[Dict[str, Any]],
    candidate_types: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Matches user follow-up input against pending ambiguity candidates.
    Supports index selection ("1", "first", "1st"), entity type selection ("customer", "project"), or direct name match.
    """
    if not user_input or not candidates:
        return None

    clean_inp = user_input.strip().lower()

    # 1. Index-based selection
    INDEX_MAP = {
        "1": 0, "first": 0, "1st": 0, "the first": 0, "the first one": 0, "one": 0,
        "2": 1, "second": 1, "2nd": 1, "the second": 1, "the second one": 1, "two": 1,
        "3": 2, "third": 2, "3rd": 2, "the third": 2, "the third one": 2, "three": 2,
        "4": 3, "fourth": 3, "4th": 3, "the fourth": 3, "the fourth one": 3, "four": 3,
        "5": 4, "fifth": 4, "5th": 4, "the fifth": 4, "the fifth one": 4, "five": 4,
    }

    if clean_inp in INDEX_MAP:
        idx = INDEX_MAP[clean_inp]
        if idx < len(candidates):
            return candidates[idx]

    # Check if pure digit
    if clean_inp.isdigit():
        idx = int(clean_inp) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]

    # 2. Entity Type selection (for AMBIGUOUS_ENTITY_TYPE)
    for cand in candidates:
        cand_type = str(cand.get("entity_type") or "").lower()
        if cand_type and (cand_type in clean_inp or clean_inp in cand_type):
            return cand

    # 3. Name substring/fuzzy match against candidates
    for cand in candidates:
        cand_name = str(cand.get("name") or cand.get("entity_name") or "").lower()
        if clean_inp in cand_name or cand_name in clean_inp:
            return cand
        # Normalized match
        norm_inp = _normalize_name_string(clean_inp)
        norm_cand = _normalize_name_string(cand_name)
        if norm_inp and norm_cand and (norm_inp in norm_cand or norm_cand in norm_inp):
            return cand

    return None


def get_child_departments_for_serviceline(service_line_id_or_name: Any) -> List[Dict[str, Any]]:
    """
    Dynamically fetches active child departments for a service line from DB master data.
    Zero hardcoded strings — uses DB master relationships (serviceline_department JOIN m_department).
    """
    if not service_line_id_or_name:
        return []
    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.connect() as conn:
            sl_id = None
            sl_name = None
            if isinstance(service_line_id_or_name, int) or (isinstance(service_line_id_or_name, str) and str(service_line_id_or_name).isdigit()):
                sl_id = int(service_line_id_or_name)
                row = conn.execute(text("SELECT name FROM m_serviceline WHERE id = :sl_id"), {"sl_id": sl_id}).fetchone()
                if row:
                    sl_name = row[0]
            else:
                sl_name = str(service_line_id_or_name).strip()
                row = conn.execute(text("SELECT id, name FROM m_serviceline WHERE is_active=1 AND (LOWER(name) = :n OR LOWER(short_code) = :n)"), {"n": sl_name.lower()}).fetchone()
                if not row:
                    row = conn.execute(text("SELECT id, name FROM m_serviceline WHERE is_active=1 AND (LOWER(name) LIKE :n OR LOWER(short_code) LIKE :n)"), {"n": f"%{sl_name.lower()}%"}).fetchone()
                if row:
                    sl_id, sl_name = row[0], row[1]

            if not sl_id:
                return []

            rows = conn.execute(text(
                "SELECT d.id, d.name, d.code FROM m_department d "
                "JOIN serviceline_department sd ON d.id = sd.department_id "
                "WHERE sd.serviceline_id = :sl_id AND d.is_active = 1 "
                "ORDER BY d.name"
            ), {"sl_id": sl_id}).fetchall()

            if not rows and sl_name:
                rows = conn.execute(text(
                    "SELECT id, name, code FROM m_department "
                    "WHERE is_active = 1 AND LOWER(name) LIKE :prefix "
                    "ORDER BY name"
                ), {"prefix": f"{sl_name.lower()} %"}).fetchall()

            results = []
            for r in rows:
                results.append({
                    "id": r[0],
                    "name": r[1],
                    "code": r[2] if len(r) > 2 else "",
                    "parent_service_line_id": sl_id,
                    "parent_service_line_name": sl_name
                })
            return results
    except Exception as e:
        logger.warning(f"[EntityResolver MasterData] Failed to fetch child departments: {e}")
        return []

