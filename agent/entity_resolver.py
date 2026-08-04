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
    "service_line": {"endpoint": "/master/service-line", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 50}
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
    "generated the highest revenue this year", "fy24 and", "fy25 and"
})

def is_reserved_business_term(val: str) -> bool:
    """
    Returns True if the string is a reserved business keyword, metric name,
    or financial year term that must NEVER be extracted/resolved as an entity value.
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
    tokens = re.findall(r'(?:fy|financial\s*year)?\s*(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?', raw_str)
    for t1, t2 in tokens:
        if not t1:
            continue
        y1_raw = int(t1)
        if y1_raw < 20 and not re.search(r'\bfy\s*' + str(y1_raw), raw_str):
            continue
        start_year = 2000 + y1_raw if y1_raw < 100 else y1_raw
        if t2:
            y2_raw = int(t2)
            end_year = 2000 + y2_raw if y2_raw < 100 else y2_raw
        else:
            end_year = start_year + 1
        if end_year <= start_year:
            end_year = start_year + 1
        short_start = str(start_year)[-2:]
        short_end = str(end_year)[-2:]
        res = {
            "financial_year": f"FY{short_start}",
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
    """
    if not time_str or not isinstance(time_str, str):
        return {}

    ts = time_str.strip().lower()
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
    m_between = re.search(r'between\s+([a-z]+)\s+and\s+([a-z]+)(?:\s+(\d{4}))?', ts)
    if m_between:
        m1_name = m_between.group(1)
        m2_name = m_between.group(2)
        year_val = int(m_between.group(3)) if m_between.group(3) else 2025
        if m1_name in MONTH_NAMES and m2_name in MONTH_NAMES:
            m1_num = MONTH_NAMES[m1_name]
            m2_num = MONTH_NAMES[m2_name]
            last_day = calendar.monthrange(year_val, m2_num)[1]
            res["start_date"] = f"{year_val}-{m1_num:02d}-01"
            res["end_date"] = f"{year_val}-{m2_num:02d}-{last_day:02d} 23:59:59"
    else:
        m_single = re.search(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b(?:\s+(\d{4}))?', ts)
        if m_single:
            m_name = m_single.group(1)
            year_val = int(m_single.group(2)) if m_single.group(2) else 2025
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


async def fetch_entity_from_api(entity_type: str, search_query: str, jwt_token: str) -> Dict[str, Any]:
    """
    Makes the actual HTTP GET call to the CRM backend.
    Runs in a thread to prevent blocking the async loop.
    """
    map_info = ENTITY_API_MAP.get(entity_type.lower())
    if not map_info:
        return {"error": f"Unknown entity type: {entity_type}"}

    search_param = map_info.get("search_parameter", "search")
    search_format = map_info.get("search_payload_format", "string")
    search_key = map_info.get("search_key", "search")
    limit = map_info.get("default_limit", 5)

    if search_format == "json":
        payload = {search_key: search_query}
        payload_str = json.dumps(payload)
        safe_query = urllib.parse.quote(payload_str)
    else:
        safe_query = urllib.parse.quote(search_query)

    url = f"{CRM_API_BASE}{map_info['endpoint']}?{search_param}={safe_query}&pageSize={limit}"
    
    logger.info(f"[EntityResolver DEBUG] Outgoing URL: GET {url}")
    logger.info(f"[EntityResolver DEBUG] Outgoing Params: {search_param}={safe_query}&pageSize={limit}")

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
            return {"error": f"API Error: {e.code}"}
        except Exception as e:
            logger.error(f"[EntityResolver] Backend Request Failed: {str(e)}")
            return {"error": f"Connection Failed: {str(e)}"}

    return await asyncio.to_thread(_sync_fetch)

def _build_display_name(record: Dict[str, Any], name_fields: List[str]) -> str:
    """Builds a human-readable name from the API record using the mapped name fields."""
    parts = []
    for f in name_fields:
        val = record.get(f)
        if val:
            parts.append(str(val))
    return " - ".join(parts) if parts else "Unknown"

EMPLOYEE_TRIGGERS = {
    "employee", "consultant", "resource", "staff", "person", "user", 
    "developer", "manager", "partner", "named", "generated by", 
    "for employee", "by employee"
}

def has_employee_trigger(query: str) -> bool:
    """Check if the user query explicitly contains an employee trigger phrase."""
    if not query:
        return False
    q_lower = query.lower()
    return any(trigger in q_lower for trigger in EMPLOYEE_TRIGGERS)

async def resolve_entities(extracted_entities: List[Dict[str, str]], jwt_token: str, full_query: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Resolves multiple entities concurrently using Context-Aware Entity Resolution Priority.
    For report queries without explicit employee indicators, prioritizes business entities:
    1. Service Line
    2. Financial Year
    3. Customer
    4. Department
    5. Project
    6. Employee (LAST)
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

        # If labeled as employee without an explicit employee trigger in a report request:
        if e_type in ["employee", "employee_name"] and not has_emp_trigger:
            logger.info(f"[EntityResolver DEBUG] Entity '{e_value}' reclassified from '{e_type}' to 'service_line' (No employee trigger in query).")
            e_type = "service_line"

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
            
    return resolved, entity_errors


def _normalize_name_string(text: str) -> str:
    """Normalizes entity name strings for robust, case-insensitive, punctuation-agnostic matching."""
    if not text:
        return ""
    clean = str(text).lower().strip()
    # Strip corporate suffixes
    for suffix in [r'\bw\.l\.l\.\b', r'\bwll\b', r'\bllc\b', r'\bcorp\b', r'\bcorporation\b', r'\bltd\b', r'\blimited\b', r'\binc\b']:
        clean = re.sub(suffix, '', clean)
    # Strip punctuation and collapse whitespace
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def _score_record_match(record: Dict[str, Any], map_info: Dict[str, Any], query_str: str) -> Tuple[float, str]:
    """
    Scores how closely a record matches the search query string.
    Returns (confidence_score, matched_display_name).
    """
    query_raw = query_str.strip()
    query_norm = _normalize_name_string(query_raw)
    
    # 1. Exact ID or Code Match
    id_val = str(record.get(map_info.get("id_field", "id"), "")).strip()
    if id_val and id_val.lower() == query_raw.lower():
        return 1.0, _build_display_name(record, map_info["name_fields"])

    name_fields = map_info.get("name_fields", [])
    
    for f in name_fields:
        val = record.get(f)
        if not val:
            continue
        val_str = str(val).strip()
        val_norm = _normalize_name_string(val_str)
        
        # 2. Exact Raw Field Match
        if val_str.lower() == query_raw.lower():
            return 0.99, _build_display_name(record, name_fields)
            
        # 3. Exact Normalized Field Match
        if query_norm and val_norm == query_norm:
            return 0.98, _build_display_name(record, name_fields)

        # 4. Substring / Prefix Match
        if query_norm and (query_norm in val_norm or val_norm in query_norm) and len(query_norm) >= 3:
            return 0.92, _build_display_name(record, name_fields)

        # 5. Fuzzy Match using difflib SequenceMatcher
        if query_norm and val_norm:
            ratio = difflib.SequenceMatcher(None, query_norm, val_norm).ratio()
            if ratio >= 0.82:
                return round(ratio, 2), _build_display_name(record, name_fields)

    return 0.0, _build_display_name(record, name_fields)

async def _resolve_single_entity_with_priority(e_type: str, e_value: str, jwt_token: str, is_report_query: bool, has_emp_trigger: bool) -> Dict[str, Any]:
    """
    Resolves a single entity with context-aware fallback priorities.
    """
    # 1. Primary Attempt
    res = await _resolve_single_entity(e_type, e_value, jwt_token)
    if res.get("status") in ["resolved", "pass_through"]:
        return res

    # 2. Report Context Priority Fallbacks (service_line -> customer -> department -> project)
    if is_report_query and not has_emp_trigger and res.get("status") == "not_found":
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


def _normalize_name_string(text: str) -> str:
    """Normalizes entity name strings for robust, case-insensitive, punctuation-agnostic matching."""
    if not text:
        return ""
    clean = str(text).lower().strip()
    # Strip corporate suffixes
    for suffix in [r'\bw\.l\.l\.\b', r'\bwll\b', r'\bllc\b', r'\bcorp\b', r'\bcorporation\b', r'\bltd\b', r'\blimited\b', r'\binc\b']:
        clean = re.sub(suffix, '', clean)
    # Strip punctuation and collapse whitespace
    clean = re.sub(r'[^a-z0-9\s]', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean

def _score_record_match(record: Dict[str, Any], map_info: Dict[str, Any], query_str: str) -> Tuple[float, str]:
    """
    Scores how closely a record matches the search query string.
    Returns (confidence_score, matched_display_name).
    """
    query_raw = query_str.strip()
    query_norm = _normalize_name_string(query_raw)
    
    # 1. Exact ID or Code Match
    id_val = str(record.get(map_info.get("id_field", "id"), "")).strip()
    if id_val and id_val.lower() == query_raw.lower():
        return 1.0, _build_display_name(record, map_info["name_fields"])

    name_fields = map_info.get("name_fields", [])
    
    for f in name_fields:
        val = record.get(f)
        if not val:
            continue
        val_str = str(val).strip()
        val_norm = _normalize_name_string(val_str)
        
        # 2. Exact Raw Field Match
        if val_str.lower() == query_raw.lower():
            return 0.99, _build_display_name(record, name_fields)
            
        # 3. Exact Normalized Field Match
        if query_norm and val_norm == query_norm:
            return 0.98, _build_display_name(record, name_fields)

        # 4. Substring / Prefix Match
        if query_norm and (query_norm in val_norm or val_norm in query_norm) and len(query_norm) >= 3:
            return 0.92, _build_display_name(record, name_fields)

        # 5. Fuzzy Match using difflib SequenceMatcher
        if query_norm and val_norm:
            ratio = difflib.SequenceMatcher(None, query_norm, val_norm).ratio()
            if ratio >= 0.82:
                return round(ratio, 2), _build_display_name(record, name_fields)

    return 0.0, _build_display_name(record, name_fields)

async def _resolve_single_entity(e_type: str, e_value: str, jwt_token: str) -> Dict[str, Any]:
    """Resolves a single entity and returns the standardized JSON format."""
    # 1. Validation
    if not e_value:
        return {
            "status": "error",
            "entity_type": e_type,
            "message": "Empty search query provided."
        }
        
    map_info = ENTITY_API_MAP.get(e_type)
    if not map_info:
        # Pass-through generic entities that don't need API validation (like 'FinancialYear')
        return {
            "status": "pass_through",
            "entity_type": e_type,
            "entity_value": e_value
        }
        
    if is_aggregate_value(e_value):
        return {
            "status": "aggregate",
            "entity_type": e_type,
            "entity_value": AGGREGATE_SENTINEL,
        }

    # 2. API Call
    response = await fetch_entity_from_api(e_type, e_value, jwt_token)
    
    # 3. Handle API Failure
    if "error" in response:
        return {
            "status": "error",
            "entity_type": e_type.capitalize(),
            "query": e_value,
            "message": response["error"]
        }
        
    # 4. Parse Results
    if isinstance(response, dict):
        if "rows" in response:
            data = response["rows"]
        elif "data" in response:
            data = response["data"]
        else:
            # Maybe the object itself is the single entity or a wrapper
            data = [response] if response else []
    elif isinstance(response, list):
        data = response
    else:
        data = []
    
    # 5. Handle No Match
    if not data or len(data) == 0:
        return {
            "status": "not_found",
            "entity_type": e_type.capitalize(),
            "query": e_value
        }
        
    # 6. Multi-Pass Match Scoring across API results
    best_record = None
    best_score = 0.0
    best_display_name = ""

    scored_results = []
    for record in data:
        score, display_name = _score_record_match(record, map_info, e_value)
        scored_results.append((score, record, display_name))
        if score > best_score:
            best_score = score
            best_record = record
            best_display_name = display_name

    # Log detailed Entity Resolution Telemetry
    norm_query = _normalize_name_string(e_value)
    logger.info(
        f"[EntityResolver Telemetry] Original Query: '{e_value}' | Normalized: '{norm_query}' | "
        f"Entity Type: '{e_type}' | Endpoint: '{map_info.get('endpoint')}' | Top Match ID: {best_record.get(map_info['id_field']) if best_record else 'None'} | "
        f"Confidence: {best_score} | Rows Returned: {len(data)}"
    )

    # 7. Accept match if confidence is high (>= 0.80) or if exactly 1 result returned with positive score
    if best_record and (best_score >= 0.80 or len(data) == 1):
        return {
            "status": "resolved",
            "entity_type": e_type.capitalize(),
            "entity_name": best_display_name,
            "entity_id": best_record.get(map_info["id_field"]),
            "confidence": best_score,
            "raw_record": best_record
        }

    # 8. If ambiguity exists (multiple candidates with low/similar scores), request clarification
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
        "matches": matches_list
    }


def extract_entities_from_text(query: str) -> List[Dict[str, str]]:
    """
    Dynamically extracts candidate entity references (customer name, project code, FY) from prompt text.
    Delegates entity validation and resolution to backend API matching.
    """
    if not query or not isinstance(query, str):
        return []

    entities = []
    # 1. Customer reference pattern (e.g., 'for Grove Resort', 'customer Grove Resort')
    cust_match = re.search(r'(?:for|customer)\s+([A-Za-z0-9\s\.\-_&]+)', query, re.IGNORECASE)
    if cust_match:
        val = cust_match.group(1).strip()
        val = re.sub(r'\s+(?:in|for|fy\d+|20\d{2}).*', '', val, flags=re.IGNORECASE).strip()
        if val and len(val) > 1 and val.lower() not in ["customer", "projects", "proposals", "revenue", "receivables", "all", "the"]:
            entities.append({"type": "customer", "value": val})

    # 2. Financial Year pattern (e.g. FY25, FY24)
    fy_match = re.search(r'(FY\d{2}|FY\d{4}|20\d{2})', query, re.IGNORECASE)
    if fy_match:
        entities.append({"type": "financial_year", "value": fy_match.group(1)})

    return entities
