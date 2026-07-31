"""
query_parser.py — Intent-to-Query Parser
=========================================
Parses the user's natural language question into a structured QueryIntent,
then generates a PRECISE, verified SQL query from that intent.

This eliminates hallucination by:
1. Extracting the exact entity (employee name, customer, project, etc.) from the question
2. Verifying the entity EXISTS in the database before generating SQL
3. Returning a confirmed SQL + the verified entity ID — so the LLM never needs to guess

Usage:
    intent = await parse_query_intent(question)
    if intent.verified_sql:
        # Execute intent.verified_sql directly — it is correct
    else:
        # Fall through to LLM SQL generation with intent.context_hint
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class QueryIntent:
    """Structured representation of what the user is asking for."""
    raw_question:       str           = ""
    metric_type:        str           = "unknown"   # e.g. "resource_utilization", "leave", "salary", ...
    entity_type:        str           = "unknown"   # e.g. "employee", "project", "customer", "service_line"
    entity_name:        str           = ""          # raw name extracted from question
    entity_id:          Optional[int] = None        # verified DB id (None if not found)
    date_from:          Optional[str] = None        # YYYY-MM-DD
    date_to:            Optional[str] = None        # YYYY-MM-DD
    date_was_specified: bool          = False       # True when user explicitly mentioned a year/month
    navigate_to:        Optional[str] = None        # Frontend route to redirect to
    extra:              Dict          = field(default_factory=dict)
    verified_sql:       Optional[str] = None        # Ready-to-execute SQL (set only when entity is confirmed)
    context_hint:       str           = ""          # Hint injected into LLM SQL prompt when SQL is not pre-built

# ---------------------------------------------------------------------------
# Entity extraction helpers (Restored for agent.py fast router)
# ---------------------------------------------------------------------------

_FOR_PATTERN   = re.compile(r'\bfor\s+([A-Z][a-z]+(?:\s+[A-Za-z]+){0,4})', re.IGNORECASE)
_OF_PATTERN    = re.compile(r'\bof\s+([A-Z][a-z]+(?:\s+[A-Za-z]+){0,4})',   re.IGNORECASE)
_NAME_PATTERN  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')         # "First Last"

def _is_company_name(name: str) -> bool:
    name_lower = name.lower()
    company_words = {'w.l.l', 'wll', 'w.l.l.', 'w', 'l.l.c', 'llc', 'ltd', 'limited', 'inc', 'corp', 'company', 'co.', 'transport', 'marketing', 'trading', 'services', 'technologies', 'solutions', 'enterprises', 'general'}
    words = set(name_lower.split())
    return bool(words & company_words)

_MONTH_NAMES_SET = {'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december', 'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'}
_MONTH_NAMES_DICT = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6, 'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}

KNOWN_SERVICE_LINES = {
    "audit", "tax", "advisory", "consulting", "deals", "strategy", 
    "risk advisory", "financial advisory", "accounting", "legal", 
    "technology", "assurance", "corporate finance"
}

def _extract_service_line_name(question: str) -> str:
    """Extracts known service line names from question text."""
    if not question:
        return ""
    q_lower = question.lower()
    for sl in KNOWN_SERVICE_LINES:
        pattern = r'\b' + re.escape(sl) + r'\b'
        if re.search(pattern, q_lower):
            return sl.title()
    return ""

def _extract_person_name(question: str) -> str:
    """Extracts person name ONLY when explicit employee trigger words are present."""
    from agent.entity_resolver import has_employee_trigger
    if not has_employee_trigger(question):
        return ""
    lines = question.strip().splitlines()
    if any(re.match(r'^\s*(Date Range|Financial Year|Service Line|Duration|Employee Name|Project Name)\s*:', l, re.IGNORECASE) for l in lines):
        question = lines[0] if lines else question

    m = _FOR_PATTERN.search(question)
    if m:
        candidate = m.group(1).strip()
        candidate = re.sub(r'^(the|a|an)\s+', '', candidate, flags=re.IGNORECASE).strip()
        candidate = re.sub(r'\b(report|summary|data|details?|for|in|on|last|next|this|year|month|quarter|fy|20\d\d)\b.*$', '', candidate, flags=re.IGNORECASE).strip()
        if len(candidate) > 2 and not _is_company_name(candidate) and candidate.lower() not in _MONTH_NAMES_SET and candidate.lower() not in KNOWN_SERVICE_LINES:
            return candidate

    m = _OF_PATTERN.search(question)
    if m:
        candidate = m.group(1).strip()
        candidate = re.sub(r'^(the|a|an)\s+', '', candidate, flags=re.IGNORECASE).strip()
        candidate = re.sub(r'\b(report|summary|data|details?|for|in|on|last|next|this|year|month|quarter|fy|20\d\d)\b.*$', '', candidate, flags=re.IGNORECASE).strip()
        if len(candidate) > 2 and not _is_company_name(candidate) and candidate.lower() not in _MONTH_NAMES_SET and candidate.lower() not in KNOWN_SERVICE_LINES:
            return candidate
    return ""

def _extract_date_range(question: str) -> tuple[Optional[str], Optional[str], bool]:
    q = question.lower()
    from datetime import datetime
    now = datetime.now()

    dd_mm_range = re.search(r'(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})', q)
    if dd_mm_range:
        def _dmy_to_ymd(s: str) -> str:
            d, m, y = s.split('-')
            return f"{y}-{m}-{d}"
        return _dmy_to_ymd(dd_mm_range.group(1)), _dmy_to_ymd(dd_mm_range.group(2)), True

    range_m = re.search(r'(\d{4}[-/]\d{2}[-/]\d{2})\s+(?:to|until|-)\s+(\d{4}[-/]\d{2}[-/]\d{2})', q)
    if range_m:
        return range_m.group(1).replace('/', '-'), range_m.group(2).replace('/', '-'), True

    month_m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+(\d{4})\b', q)
    month_no_year_m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', q)

    if month_m or month_no_year_m:
        if month_m:
            m_str = month_m.group(1)[:3]
            y_val = int(month_m.group(2))
        else:
            m_str = month_no_year_m.group(1)[:3]
            y_val = now.year
        m_val = _MONTH_NAMES_DICT[m_str]
        if not month_m and m_val > now.month:
            y_val = now.year - 1
        import calendar
        last_day = calendar.monthrange(y_val, m_val)[1]
        return f"{y_val}-{m_val:02d}-01", f"{y_val}-{m_val:02d}-{last_day:02d}", True

    year_m = re.search(r'\b(20\d{2})\b', q)
    if year_m:
        yr = int(year_m.group(1))
        return f"{yr}-10-01", f"{yr+1}-09-30", True

    if 'last month' in q:
        if now.month == 1:
            return f"{now.year-1}-12-01", f"{now.year-1}-12-31", True
        import calendar
        m = now.month - 1
        last = calendar.monthrange(now.year, m)[1]
        return f"{now.year}-{m:02d}-01", f"{now.year}-{m:02d}-{last}", True

    if any(x in q for x in ['this year', 'current year', 'fy', 'financial year', 'fiscal year']):
        if now.month >= 10:
            return f"{now.year}-10-01", f"{now.year+1}-09-30", True
        else:
            return f"{now.year-1}-10-01", f"{now.year}-09-30", True

    if now.month >= 10:
        return f"{now.year}-10-01", f"{now.year+1}-09-30", False
    return f"{now.year-1}-10-01", f"{now.year}-09-30", False
# ---------------------------------------------------------------------------
# Typo / abbreviation normalizer — maps common CRM mistyping to canonical phrases
# ---------------------------------------------------------------------------
_TYPO_MAP = [
    # service line performance variants
    (re.compile(r'\bservice\s+li[a-z]{0,3}\b', re.IGNORECASE), 'service line'),
    (re.compile(r'\bperform[a-z]{0,5}\b', re.IGNORECASE), 'performance'),
    (re.compile(r'\bserv[a-z]{0,3}\s+line\b', re.IGNORECASE), 'service line'),
    # GP / gross profit
    (re.compile(r'\bgp\s+perf[a-z]{0,7}\b', re.IGNORECASE), 'gp performance'),
    (re.compile(r'\bgross\s+prof[a-z]{0,2}\b', re.IGNORECASE), 'gross profit'),
    # department utilization
    (re.compile(r'\bdep[a-z]{0,6}\s+util[a-z]{0,6}\b', re.IGNORECASE), 'department utilization'),
    (re.compile(r'\butili[zs]a[a-z]{0,4}\b', re.IGNORECASE), 'utilization'),
    # receivables
    (re.compile(r'\brecei[a-z]{0,6}\b', re.IGNORECASE), 'receivables'),
    # revenue
    (re.compile(r'\brevenu[a-z]{0,2}\b', re.IGNORECASE), 'revenue'),
]

def _normalize_question(question: str) -> str:
    """Apply typo corrections so downstream matching works on canonical CRM terms."""
    normalized = question
    for pattern, replacement in _TYPO_MAP:
        normalized = pattern.sub(replacement, normalized)
    return normalized


_METRIC_PATTERNS: List[tuple[str, List[str]]] = [
    # ── NEW: Dashboard-specific metrics with exact SQL builders ──────────────
    ("service_line_performance", [
        "service line performance", "service line revenue", "serviceline performance",
        "service line breakdown", "revenue by service line", "billing by service line",
        "team billing", "service line actual",
    ]),
    ("gp_performance",           [
        "gp performance", "gross profit performance", "gp by service line",
        "gp target", "gp vs target", "performing vs target", "gross profit by service line",
        "gp performance by service line",
    ]),
    ("department_utilization",   [
        "department utilization", "dept utilization", "department utilisation",
        "utilization by department", "dept utilisation", "hours per department",
        "department hours", "utilization rate by department",
    ]),
    # ── Existing metric types ────────────────────────────────────────────────
    ("resource_utilization",  ["resource", "utilization", "utilisation", "utilizaation",
                                "billable", "chargeable"]),
    ("leave",                 ["leave", "annual leave", "sick leave", "leave balance",
                                "leave request", "days off", "absence"]),
    ("salary",                ["salary", "payroll", "pay slip", "salary slip",
                                "basic salary", "gross salary", "net salary", "allowance"]),
    ("project_tasks",         ["task", "tasks", "overdue task", "my tasks",
                                "project task", "to do", "in progress"]),
    ("project_summary",       ["project", "projects", "active project", "wip",
                                "project status", "project summary"]),
    ("leads",                 ["lead", "leads", "service lead", "pipeline lead"]),
    ("proposals",             ["proposal", "proposals", "engagement letter", "win rate"]),
    ("receivables",           ["receivable", "receivables", "aging", "ageing",
                                "outstanding", "overdue invoice"]),
    ("revenue",               ["revenue", "billing", "invoice amount", "total revenue"]),
    ("kpi",                   ["kpi", "kpi summary", "budget vs actual", "target"]),
    ("employee_info",         ["employee", "staff", "headcount", "designation",
                                "department", "who is", "profile"]),
    ("customer_info",         ["customer", "client", "company", "account"]),
]

def _detect_metric_type(q_lower: str) -> str:
    import difflib
    
    # 1. Fast exact substring match (longer/more-specific patterns must come first)
    for m_type, keywords in _METRIC_PATTERNS:
        for kw in keywords:
            if kw in q_lower:
                return m_type
                
    # 2. Fuzzy match on single words and bi-grams
    words = q_lower.split()
    bi_grams = [" ".join(words[i:i+2]) for i in range(len(words)-1)]
    tri_grams = [" ".join(words[i:i+3]) for i in range(len(words)-2)]
    tokens_to_test = tri_grams + bi_grams + words
    
    # Map all keywords to their metric type
    all_keywords = {}
    for m_type, keywords in _METRIC_PATTERNS:
        for kw in keywords:
            all_keywords[kw] = m_type
            
    # Test each token against the keywords
    for token in tokens_to_test:
        if len(token) < 4: continue # skip short words
        matches = difflib.get_close_matches(token, all_keywords.keys(), n=1, cutoff=0.75)
        if matches:
            return all_keywords[matches[0]]
            
    return "general"


# ---------------------------------------------------------------------------
# DB entity lookup
# ---------------------------------------------------------------------------

def _lookup_employee_by_name(name: str) -> Optional[tuple[int, str]]:
    if not name or len(name) < 2:
        return None
    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        import difflib
        
        engine = get_db_engine()
        with engine.connect() as conn:
            # Fetch ALL active employees (very fast for a few hundred rows)
            rows = conn.execute(text("SELECT id, employee_name FROM employees WHERE is_active = 1")).fetchall()
            
        if not rows:
            return None
            
        # Map lowercased names to their actual data
        name_map = {str(r[1]).lower(): (int(r[0]), str(r[1])) for r in rows}
        target_name = name.lower().strip()
        
        # 1. Exact match (case/space insensitive by stripping spaces)
        target_no_spaces = target_name.replace(' ', '')
        for emp_name_lower, emp_data in name_map.items():
            if target_no_spaces in emp_name_lower.replace(' ', ''):
                return emp_data
                
        # 2. Fuzzy match full name
        matches = difflib.get_close_matches(target_name, name_map.keys(), n=1, cutoff=0.7)
        if matches:
            return name_map[matches[0]]
            
        # 3. Fuzzy match first name partial
        parts = target_name.split()
        if parts:
            first_part = parts[0]
            if len(first_part) > 2:
                for emp_name_lower, emp_data in name_map.items():
                    emp_parts = emp_name_lower.split()
                    if emp_parts and difflib.SequenceMatcher(None, first_part, emp_parts[0]).ratio() > 0.8:
                        return emp_data
                        
        return None
    except Exception as e:
        logger.warning(f"[QueryParser] Employee lookup failed: {e}")
    return None


# ---------------------------------------------------------------------------
# SQL builder per metric type
# ---------------------------------------------------------------------------

def _get_monthly_standard_hours(date_from: str, date_to: str) -> dict:
    from db.database import get_db_engine
    from sqlalchemy import text
    import datetime
    
    try:
        engine = get_db_engine()
        start_date = datetime.datetime.strptime(date_from, '%Y-%m-%d').date()
        end_date = datetime.datetime.strptime(date_to, '%Y-%m-%d').date()
        
        today = datetime.date.today()
        if end_date > today:
            end_date = today
            
        if start_date > end_date:
            return {"OVERALL TOTAL": 0}

        with engine.connect() as conn:
            holidays = conn.execute(text("SELECT holiday_from, holiday_to FROM holidays_setting WHERE is_active=1")).fetchall()
            
        holiday_dates = set()
        for h_from, h_to in holidays:
            if h_from and h_to:
                curr = h_from
                while curr <= h_to:
                    holiday_dates.add(curr)
                    curr += datetime.timedelta(days=1)
                    
        monthly_hours = {}
        total_working_days = 0
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5 and curr not in holiday_dates:
                month_key = curr.strftime('%Y-%m')
                monthly_hours[month_key] = monthly_hours.get(month_key, 0) + 8
                total_working_days += 1
            curr += datetime.timedelta(days=1)
            
        monthly_hours["OVERALL TOTAL"] = total_working_days * 8
        return monthly_hours
    except Exception as e:
        logger.error(f"Error calculating standard hours: {e}")
        return {"OVERALL TOTAL": 0}

def _build_service_line_performance_sql(date_from: str, date_to: str) -> str:
    """Exact service line performance SQL matching the dashboard widget."""
    return f"""
SELECT
  sl.name AS service_line,
  sl.short_code,
  ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS actual_revenue,
  COALESCE((
    SELECT ROUND(SUM(km.target_value), 2)
    FROM kpi_master km
    JOIN serviceline_department sd ON km.department_id = sd.department_id
    WHERE sd.serviceline_id = sl.id
  ), 0) AS target_revenue
FROM m_serviceline sl
LEFT JOIN invoice i
  ON i.service_line_id = sl.id
  AND i.is_active = 1
  AND i.created_at BETWEEN '{date_from}' AND '{date_to}'
WHERE sl.is_active = 1
GROUP BY sl.id, sl.name, sl.short_code
HAVING actual_revenue > 0 OR target_revenue > 0
ORDER BY actual_revenue DESC
""".strip()


def _build_gp_performance_sql(date_from: str, date_to: str) -> str:
    """GP Performance by service line — matches the dashboard GP Performance widget exactly."""
    return f"""
SELECT
  sl.name AS service_line,
  sl.short_code,
  ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS performing,
  COALESCE((
    SELECT ROUND(SUM(km.target_value), 2)
    FROM kpi_master km
    JOIN serviceline_department sd ON km.department_id = sd.department_id
    WHERE sd.serviceline_id = sl.id
  ), 0) AS target,
  ROUND(
    COALESCE(SUM(i.total_amt_ex_vat), 0)
    / NULLIF(
        COALESCE((
          SELECT SUM(km2.target_value)
          FROM kpi_master km2
          JOIN serviceline_department sd2 ON km2.department_id = sd2.department_id
          WHERE sd2.serviceline_id = sl.id
        ), 0), 0
      ) * 100, 2
  ) AS achievement_pct
FROM m_serviceline sl
LEFT JOIN invoice i
  ON i.service_line_id = sl.id
  AND i.is_active = 1
  AND i.created_at BETWEEN '{date_from}' AND '{date_to}'
WHERE sl.is_active = 1
GROUP BY sl.id, sl.name, sl.short_code
HAVING performing > 0 OR target > 0
ORDER BY performing DESC
""".strip()


def _build_department_utilization_sql(date_from: str, date_to: str) -> str:
    """Department utilization using emp_payroll_timesheet — matches the dashboard exactly."""
    return f"""
SELECT
  d.name AS department,
  ROUND(SUM(TIME_TO_SEC(ept.total_approved_hrs)) / 3600, 2) AS approved_hours,
  ROUND(SUM(TIME_TO_SEC(ept.total_eligible_hrs))  / 3600, 2) AS eligible_hours,
  ROUND(SUM(TIME_TO_SEC(ept.total_working_hrs))   / 3600, 2) AS working_hours,
  ROUND(
    IF(
      SUM(TIME_TO_SEC(ept.total_eligible_hrs)) > 0,
      SUM(TIME_TO_SEC(ept.total_working_hrs)) / SUM(TIME_TO_SEC(ept.total_eligible_hrs)) * 100,
      0
    ), 2
  ) AS utilization_pct
FROM emp_payroll_timesheet ept
JOIN employees e  ON ept.employee_id    = e.id
JOIN m_department d ON e.emp_department_id = d.id
WHERE
  STR_TO_DATE(CONCAT('01-', ept.effective_month), '%d-%b-%Y')
    BETWEEN '{date_from}' AND '{date_to}'
  AND ept.is_active = 1
GROUP BY d.id, d.name
ORDER BY utilization_pct DESC
""".strip()


def _build_resource_utilization_sql(employee_id: int, employee_name: str,
                                      date_from: str, date_to: str) -> str:
    
    std_hours_map = _get_monthly_standard_hours(date_from, date_to)
    
    cte_rows = []
    for month_key, hrs in std_hours_map.items():
        cte_rows.append(f"SELECT '{month_key}' AS mth, {hrs} AS std_hrs")
    
    cte_sql = " UNION ALL ".join(cte_rows)
    if not cte_sql:
        cte_sql = "SELECT 'OVERALL TOTAL' AS mth, 0 AS std_hrs"
        
    return f"""
WITH StandardHours AS (
  {cte_sql}
),
MonthlyCharged AS (
  SELECT
    COALESCE(DATE_FORMAT(tpd.project_date, '%Y-%m'), 'OVERALL TOTAL') AS mth,
    ROUND(SUM(TIME_TO_SEC(tpd.hours)) / 3600, 2) AS charged_hours
  FROM timesheet_project tp
  JOIN ts_project_date tpd ON tp.id = tpd.timesheet_id
  WHERE tp.employee_id = {employee_id}
    AND tp.status_id = 3
    AND tpd.project_date BETWEEN '{date_from}' AND '{date_to}'
  GROUP BY DATE_FORMAT(tpd.project_date, '%Y-%m') WITH ROLLUP
)
SELECT 
  mc.mth AS Period,
  mc.charged_hours AS Charged_Hours,
  COALESCE(sh.std_hrs, 0) AS Standard_Hours,
  ROUND(mc.charged_hours / NULLIF(sh.std_hrs, 0) * 100, 2) AS Utilization_Pct
FROM MonthlyCharged mc
LEFT JOIN StandardHours sh ON sh.mth = mc.mth
""".strip()


def _build_leave_sql(employee_id: int, employee_name: str) -> str:
    return f"""
SELECT
  e.employee_name,
  lt.name   AS leave_type,
  lr.from_date,
  lr.to_date,
  lr.total_days,
  ls.name   AS status,
  lr.remarks
FROM leave_request lr
JOIN employees e        ON lr.employee_id   = e.id
JOIN m_leave_request_type lt ON lr.leave_type_id = lt.id
JOIN m_leave_status ls  ON lr.status_id     = ls.id
WHERE lr.employee_id = {employee_id}
ORDER BY lr.from_date DESC
LIMIT 50
""".strip()


def _build_leave_balance_sql(employee_id: int) -> str:
    return f"""
SELECT
  e.employee_name,
  lt.name   AS leave_type,
  elb.leave_balance AS balance
FROM employee_leave_balance elb
JOIN employees e              ON elb.emp_id        = e.id
JOIN m_leave_request_type lt  ON elb.leave_type_id = lt.id
WHERE elb.emp_id = {employee_id}
ORDER BY lt.name
""".strip()


def _build_salary_sql(employee_id: int) -> str:
    return f"""
SELECT
  e.employee_name,
  d.name   AS department,
  des.name AS designation,
  e.emp_basic_salary,
  e.emp_gross_salary
FROM employees e
LEFT JOIN m_department  d   ON e.emp_department_id  = d.id
LEFT JOIN m_designation des ON e.emp_designation_id = des.id
WHERE e.id = {employee_id}
LIMIT 1
""".strip()


# ---------------------------------------------------------------------------
# LLM Initialization Helper
# ---------------------------------------------------------------------------
def _build_parser_llm():
    from config.llm_factory import get_llm
    import os
    fast_model = os.getenv("FAST_MODEL", "llama-3.1-8b-instant")
    return get_llm(model_name=fast_model, temperature=0.0)

# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

async def parse_query_intent(question: str) -> QueryIntent:
    """
    Parse the user's question into a structured QueryIntent using an LLM.
    """
    intent = QueryIntent(raw_question=question)
    normalized_q = _normalize_question(question)
    
    from pydantic import BaseModel, Field
    
    class LLMQueryIntent(BaseModel):
        metric_type: str = Field(description="The primary metric category. Must be one of: 'service_line_performance', 'gp_performance', 'department_utilization', 'resource_utilization', 'leave', 'salary', 'project_tasks', 'project_summary', 'leads', 'proposals', 'receivables', 'revenue', 'kpi', 'employee_info', 'customer_info', or 'general'")
        entity_type: str = Field(description="The type of entity requested. Must be one of: 'employee', 'project', 'customer', 'all_service_lines', 'all_departments', 'all_employees', 'general'. E.g. 'clients' -> 'customer'")
        entity_name: str = Field(description="The specific proper name extracted (e.g. 'John Doe', 'Acme Corp'). Leave empty if not applicable or asking for a general list.")
        date_from: str = Field(description="The start date inferred from the query in YYYY-MM-DD format. Leave empty if none.")
        date_to: str = Field(description="The end date inferred from the query in YYYY-MM-DD format. Leave empty if none.")
        date_was_specified: bool = Field(description="True ONLY if the user explicitly mentioned a date, month, year, quarter, or time period.")

    import datetime
    now = datetime.datetime.now()
    current_fy_start = f"{now.year}-10-01" if now.month >= 10 else f"{now.year-1}-10-01"
    current_fy_end = f"{now.year+1}-09-30" if now.month >= 10 else f"{now.year}-09-30"

    system_prompt = f"""You are a CRM query intent parser. Extract the user's intent.
Current Date: {now.strftime('%Y-%m-%d')}
Default Financial Year: {current_fy_start} to {current_fy_end}

If the user does not specify a date, return the default financial year and set date_was_specified=false.
If the user specifies "Q3 of last year" or similar, calculate the exact dates and set date_was_specified=true.

Example: "Show me a list of clients who had a Proposal approved"
-> metric_type: 'customer_info', entity_type: 'customer', entity_name: ''

Example: "What is Bhavik's utilization?"
-> metric_type: 'resource_utilization', entity_type: 'employee', entity_name: 'Bhavik'
"""
    
    try:
        llm = _build_parser_llm()
        structured_llm = llm.with_structured_output(LLMQueryIntent)
        parsed: LLMQueryIntent = await structured_llm.ainvoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ])
        
        intent.metric_type = parsed.metric_type
        intent.entity_type = parsed.entity_type
        intent.entity_name = parsed.entity_name
        intent.date_from = parsed.date_from or current_fy_start
        intent.date_to = parsed.date_to or current_fy_end
        intent.date_was_specified = parsed.date_was_specified

        from agent.entity_resolver import is_fiscal_year_expression, resolve_fiscal_year
        if is_fiscal_year_expression(question):
            fy_info = resolve_fiscal_year(question)
            intent.date_from = fy_info["start_date"]
            intent.date_to = fy_info["end_date"]
            intent.date_was_specified = True
            intent.extra["financial_year"] = fy_info["financial_year"]
    except Exception as e:
        logger.error(f"[QueryParser] LLM extraction failed, falling back to legacy: {e}")
        # Extremely basic fallback
        intent.metric_type = _detect_metric_type(normalized_q.lower())
        intent.date_from = current_fy_start
        intent.date_to = current_fy_end
        intent.date_was_specified = False

    # Set navigate_to for routes that have known dashboard pages
    _ROUTE_MAP = {
        "resource_utilization":    "/projects/reports/resource-utilization-report",
        "leave":                   "/self-services/leave-request",
        "kpi":                     "/projects/reports/kpi-summary-report",
        "receivables":             "/billing/reports/receivable-report",
        "proposals":               "/proposal",
        "leads":                   "/service-lead",
        "project_summary":         "/projects-list",
        "service_line_performance": "/crm-dashboard",
        "gp_performance":          "/projects/reports/kpi-summary-report",
        "department_utilization":  "/projects/reports/resource-utilization-report",
    }
    intent.navigate_to = _ROUTE_MAP.get(intent.metric_type)

    date_from = intent.date_from
    date_to = intent.date_to
    q_lower = normalized_q.lower()

    # ── Dashboard-level verified SQL metrics (no employee lookup needed) ────
    if intent.metric_type == "service_line_performance":
        intent.entity_type = "all_service_lines"
        intent.verified_sql = _build_service_line_performance_sql(date_from, date_to)
        intent.context_hint = (
            f"Service Line Performance for FY {date_from} to {date_to}. "
            f"Shows actual_revenue and target_revenue per service line from invoice + kpi_master."
        )
        logger.info("[QueryParser] service_line_performance → verified SQL")
        return intent

    if intent.metric_type == "gp_performance":
        intent.entity_type = "all_service_lines"
        intent.verified_sql = _build_gp_performance_sql(date_from, date_to)
        intent.context_hint = (
            f"GP Performance by Service Line for FY {date_from} to {date_to}. "
            f"Shows performing (actual invoice revenue), target, and achievement_pct per service line."
        )
        logger.info("[QueryParser] gp_performance → verified SQL")
        return intent

    if intent.metric_type == "department_utilization":
        intent.entity_type = "all_departments"
        intent.verified_sql = _build_department_utilization_sql(date_from, date_to)
        intent.context_hint = (
            f"Department Utilization for {date_from} to {date_to} using payroll-confirmed hours "
            f"(emp_payroll_timesheet). Shows approved_hours, eligible_hours, working_hours, "
            f"and utilization_pct per department."
        )
        logger.info("[QueryParser] department_utilization → verified SQL")
        return intent

    employee_metrics = {
        "resource_utilization", "leave", "salary", "project_tasks", "project_summary", "kpi", "receivables", "revenue"
    }
    if intent.metric_type in employee_metrics and intent.entity_type == "employee":
        from agent.entity_resolver import has_employee_trigger
        if not has_employee_trigger(question):
            sl_cand = _extract_service_line_name(question)
            if sl_cand:
                intent.entity_type = "service_line"
                intent.entity_name = sl_cand
            else:
                intent.entity_type = "general"
                intent.entity_name = ""
        else:
            name = intent.entity_name
            if name:
                intent.entity_type = "employee"
                intent.entity_name = name

                # Verify in DB
                result = _lookup_employee_by_name(name)
                if result:
                    emp_id, emp_name = result
                    intent.entity_id    = emp_id
                    intent.entity_name  = emp_name   # use canonical DB name
                    logger.info(f"[QueryParser] Resolved '{name}' → employee_id={emp_id} ({emp_name})")

                    # Build confirmed SQL
                    if intent.metric_type == "resource_utilization":
                        intent.verified_sql = _build_resource_utilization_sql(
                            emp_id, emp_name, date_from, date_to
                        )
                        intent.context_hint = (
                            f"Employee '{emp_name}' (id={emp_id}) verified in DB. "
                            f"Query provides month-wise and overall breakdown including exact Standard Hours and Utilization %. "
                            f"NOTE: Display this exact data professionally as a Markdown table."
                        )

                    elif "leave balance" in q_lower:
                        intent.verified_sql = _build_leave_balance_sql(emp_id)
                        intent.context_hint = f"Employee '{emp_name}' id={emp_id}. Query employee_leave_balance."

                    elif intent.metric_type == "leave":
                        intent.verified_sql = _build_leave_sql(emp_id, emp_name)
                        intent.context_hint = f"Employee '{emp_name}' id={emp_id}. Query leave_request."

                    elif intent.metric_type == "salary":
                        intent.verified_sql = _build_salary_sql(emp_id)
                        intent.context_hint = f"Employee '{emp_name}' id={emp_id}. Use emp_basic_salary, emp_gross_salary from employees."

                else:
                    logger.info(f"[QueryParser] Could not find employee '{name}' in DB")
                    intent.context_hint = (
                        f"WARNING: Employee '{name}' not found in database. "
                        f"Use LIKE '%{name}%' search on employees.employee_name. "
                        f"If still not found, return 'No employee named {name!r} was found.'"
                    )
            else:
                # No specific employee — aggregate query
                intent.entity_type = "all_employees"
                if intent.metric_type == "resource_utilization":
                    intent.context_hint = (
                        f"Aggregate resource utilization for ALL employees. "
                        f"Query timesheet_project JOIN ts_project_date (tpd) JOIN employees (status_id=3). "
                        f"NOTE: Display this exact data professionally as a Markdown table."
                    )

    # ── Set a generic hint for LLM fallback ───────────────────────────────
    if not intent.context_hint:
        intent.context_hint = (
            f"Metric: {intent.metric_type}. "
            f"Entity: {intent.entity_type} = '{intent.entity_name}'. "
            f"Date range: {date_from} to {date_to}."
        )

    return intent


async def get_grounded_sql(question: str) -> tuple[Optional[str], str, 'QueryIntent']:
    """
    High-level helper: returns (verified_sql, context_hint, intent).
    verified_sql is None if we couldn't build a confirmed query.
    context_hint always has useful metadata for the LLM.
    intent holds full parsed metadata including navigate_to and date_was_specified.
    """
    intent = await parse_query_intent(question)
    return intent.verified_sql, intent.context_hint, intent
