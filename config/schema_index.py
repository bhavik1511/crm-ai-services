"""
schema_index.py — Dynamic Schema Selector
Reads the REAL database schema live (cached in memory after first load).
Zero hardcoded column lists — everything comes from the actual DB.
"""

from typing import List, Dict, Optional, Set, Any
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Live schema cache — populated once on first call, reused forever after
# ---------------------------------------------------------------------------
_LIVE_SCHEMA_CACHE: Dict[str, List[str]] = {}   # table -> [col, col, ...]
_SCHEMA_LOADED = False

def _load_live_schema():
    """Read ALL table columns from the real DB once and cache them."""
    global _LIVE_SCHEMA_CACHE, _SCHEMA_LOADED
    if _SCHEMA_LOADED:
        return
    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        engine = get_db_engine()
        with engine.connect() as conn:
            tables = conn.execute(text("SHOW TABLES")).fetchall()
            for (table_name,) in tables:
                try:
                    cols = conn.execute(text(f"SHOW COLUMNS FROM `{table_name}`")).fetchall()
                    _LIVE_SCHEMA_CACHE[table_name] = [c[0] for c in cols]
                except Exception:
                    pass
        _SCHEMA_LOADED = True
        logger.info(f"[SchemaIndex] Loaded {len(_LIVE_SCHEMA_CACHE)} tables from live DB")
    except Exception as e:
        logger.warning(f"[SchemaIndex] Live schema load failed, using fallback: {e}")


TECHNICAL_COLS_TO_SKIP = {
    "created_by", "updated_by", "deleted_at", "updated_at", "is_deleted",
    "emp_emergency_contact", "emp_gosi_number", "emp_passport_no", "emp_passport_expiry_date",
    "emp_visa_no", "emp_visa_expiry_date", "emp_cpr_no", "emp_cpr_expiry_date",
    "emp_photo", "signature", "password", "token", "hash"
}


def _fmt_table(table_name: str, is_primary: bool = True) -> str:
    """Format one table as 'tablename(col1, col2, ...)', omitting internal technical bloat."""
    cols = _LIVE_SCHEMA_CACHE.get(table_name, [])
    if not cols:
        return f"{table_name}(/* columns unknown */)"
    filtered_cols = [c for c in cols if c not in TECHNICAL_COLS_TO_SKIP]
    if not is_primary and len(filtered_cols) > 18:
        filtered_cols = filtered_cols[:18]
    return f"{table_name}({', '.join(filtered_cols)})"


# ---------------------------------------------------------------------------
# KEYWORD → TABLE MAPPING
# Every CRM area is covered. Smart fallback = full schema.
# ---------------------------------------------------------------------------

_KEYWORD_MAP: List[Dict] = [
    {
        "keywords": ["revenue", "billing", "invoice", "collection", "paid", "unpaid",
                     "vat", "tax", "amount", "invoiced", "billed"],
        "primary": ["invoice", "invoice_details"],
        "buffer": ["customers", "employees", "m_serviceline", "receipt_details",
                   "credit_note", "m_invoice_status", "organization"],
    },
    {
        "keywords": ["receivable", "outstanding", "aging", "ageing", "overdue payment",
                     "owed", "remaining", "unsettled", "receipt"],
        "primary": ["invoice", "receipt_details", "receipts", "credit_note"],
        "buffer": ["customers", "m_serviceline", "employees", "m_invoice_status"],
    },
    {
        "keywords": ["lead", "leads", "service lead", "saleslead", "enquiry",
                     "pipeline", "open lead", "untouched", "follow up lead"],
        "primary": ["saleslead", "m_leadstatus", "saleslead_follow_up"],
        "buffer": ["employees", "customers", "m_serviceline", "m_servicetype",
                   "m_sub_servicetype", "m_industry_type", "contacts"],
    },
    {
        "keywords": ["job estimation", "estimation", "estimated", "job est"],
        "primary": ["job_estimation", "m_jobestimation_status",
                    "job_estimation_appointed_designation", "job_estimation_follow_up"],
        "buffer": ["saleslead", "customers", "employees", "m_designation"],
    },
    {
        "keywords": ["proposal", "proposals", "win rate", "conversion", "engagement letter",
                     "agreed fees", "open proposal", "continuous engagement"],
        "primary": ["proposal", "m_proposal_status", "m_engagement_status",
                    "m_continuous_engagement_status", "proposal_follow_up",
                    "proposal_appointed_designation", "payment_terms"],
        "buffer": ["job_estimation", "customers", "employees", "m_serviceline", "projects"],
    },
    {
        "keywords": ["project", "projects", "active project", "wip", "completed project",
                     "incharge", "partner project", "audit year", "milestone",
                     "resource allocation", "gantt", "portfolio"],
        "primary": ["projects", "m_project_status", "project_follow_up",
                    "project_milestone", "project_team_members"],
        "buffer": ["customers", "employees", "m_serviceline", "m_servicetype",
                   "proposal", "resource_allocation", "resource_project"],
    },
    {
        "keywords": ["task", "tasks", "overdue task", "due date", "assignee",
                     "todo", "in progress", "in review", "finished", "priority"],
        "primary": ["project_tasks", "task_activities", "task_comments"],
        "buffer": ["projects", "employees", "project_milestone"],
    },
    {
        "keywords": ["employee", "employees", "staff", "headcount", "designation",
                     "department", "salary", "join date", "who is", "nationality",
                     "gender", "contract", "supervisor", "basic salary", "gross salary",
                     "profile", "hr", "payroll", "workforce"],
        "primary": ["employees", "m_department", "m_designation",
                    "employee_salary_details", "emp_salary_details_history"],
        "buffer": ["m_nationality", "designation_rates", "department_designations",
                   "department_employees", "employees_history"],
    },
    {
        "keywords": ["leave", "leave request", "annual leave", "sick leave",
                     "absence", "leave balance", "leave plan", "leave type",
                     "leave approval", "leave status", "day off", "days off"],
        "primary": ["leave_request", "m_leave_status", "m_leave_request_type",
                    "employee_leave_balance", "leave_entitlements",
                    "leave_plans", "leave_request_approvers"],
        "buffer": ["employees", "m_department"],
    },
    {
        "keywords": ["cash advance", "advance", "advance payment", "cash request"],
        "primary": ["cash_advance", "cash_advance_payment", "cash_advance_approvers",
                    "m_cash_advance_type", "m_cash_advance_status"],
        "buffer": ["employees"],
    },
    {
        "keywords": ["travel", "travel request", "flight", "air ticket", "airline",
                     "ticket", "passage", "travel approval"],
        "primary": ["travel_request", "travel_request_route", "travel_request_approvers",
                    "air_ticket_passage", "air_ticket_details", "m_airlines"],
        "buffer": ["employees", "m_countries"],
    },
    {
        "keywords": ["general query", "general queries", "general request",
                     "it request", "admin request", "hr request"],
        "primary": ["general_queries", "m_gen_request_type", "gen_query_approvers"],
        "buffer": ["employees"],
    },
    {
        "keywords": ["customer", "client", "company", "account", "cr number",
                     "cust_code", "customer code", "vat number", "group",
                     "industry", "company type", "contact person", "branch"],
        "primary": ["customers", "customer_contact_details",
                    "customer_branch_details", "customer_bank_details"],
        "buffer": ["contacts", "m_group", "m_company_type", "m_industry_type",
                   "m_countries", "m_nationality"],
    },
    {
        "keywords": ["contact", "contacts", "person", "mobile", "phone", "email contact"],
        "primary": ["contacts"],
        "buffer": ["customers", "m_countries", "m_nationality"],
    },
    {
        "keywords": ["kpi", "target", "budget target", "gp target", "performance",
                     "gross profit", "gp", "secured business", "balance to achieve",
                     "budget vs actual", "variance", "billing revenue"],
        "primary": ["kpi_master", "m_serviceline", "serviceline_department",
                    "staff_cost_master", "referral_fee_master",
                    "direct_consultancy_fees_master", "debt_discount_master"],
        "buffer": ["m_department", "employees"],
    },
    {
        "keywords": ["timesheet", "hours", "time entry", "billable hours"],
        "primary": ["timesheet_project", "ts_project_date",
                    "timesheet_non_chargeable", "ts_non_chargeable_date",
                    "timesheet_off_duty", "ts_off_duty_date"],
        "buffer": ["projects", "employees", "m_serviceline",
                   "m_non_chargeable", "m_off_duty"],
    },
    {
        "keywords": ["resource utilization", "resource utilisation",
                     "utilization report", "utilisation report",
                     "staff utilization", "staff utilisation",
                     "resource report", "utilization for", "utilisation for",
                     "employee utilization", "billable vs", "chargeable hours",
                     "non-chargeable", "non chargeable"],
        "primary": ["emp_payroll_timesheet", "timesheet_project",
                    "timesheet_non_chargeable", "timesheet_off_duty",
                    "resource_allocation", "resource_project"],
        "buffer": ["employees", "projects", "m_serviceline",
                   "m_department", "m_non_chargeable", "m_off_duty"],
    },
    {
        "keywords": ["service line", "serviceline", "service type", "sub service"],
        "primary": ["m_serviceline", "m_servicetype", "m_sub_servicetype",
                    "serviceline_department", "serviceline_incharge"],
        "buffer": [],
    },
    {
        "keywords": ["survey", "satisfaction", "client feedback", "nps", "client survey"],
        "primary": ["client_survey", "assign_client_survey_question"],
        "buffer": [],
    },
    {
        "keywords": ["payroll", "salary slip", "pay slip", "monthly pay",
                     "allowance", "deduction", "settlement", "final settlement"],
        "primary": ["emp_payroll", "emp_payroll_timesheet", "employee_payroll_report",
                    "emp_final_settlement", "emp_final_settlement_allowance",
                    "emp_other_allowance", "emp_other_deductions",
                    "emp_settlement_recruitment_expenses"],
        "buffer": ["employees", "m_department"],
    },
    {
        "keywords": ["loan", "emp loan", "employee loan", "advance salary"],
        "primary": ["emp_loans", "emp_advances"],
        "buffer": ["employees"],
    },
    {
        "keywords": ["announcement", "notice", "notice board"],
        "primary": ["announcement"],
        "buffer": [],
    },
    {
        "keywords": ["holiday", "holidays", "public holiday", "weekend", "weekday"],
        "primary": ["holidays_setting", "weekdays_setting", "weekends_setting"],
        "buffer": [],
    },
    {
        "keywords": ["bank", "payment type", "receipt type", "cheque", "wire transfer"],
        "primary": ["m_banks", "m_payment_types", "receipts"],
        "buffer": ["organization"],
    },
    {
        "keywords": ["organization", "organisation", "company profile", "firm"],
        "primary": ["organization", "organization_bank_details"],
        "buffer": [],
    },
    {
        "keywords": ["designation rate", "hourly rate", "billing rate", "staff cost"],
        "primary": ["designation_rates", "staff_cost_master"],
        "buffer": ["m_designation", "m_serviceline"],
    },
    {
        "keywords": ["fiscal year", "financial year", "fy setting", "fy period"],
        "primary": ["fiscal_year_setting"],
        "buffer": [],
    },
    {
        "keywords": ["attendance", "check in", "check out", "location", "office attendance"],
        "primary": ["employee_attendance", "employee_location"],
        "buffer": ["employees"],
    },
    {
        "keywords": ["notification", "alert", "reminder"],
        "primary": ["notifications", "notification_status", "notification_templates"],
        "buffer": ["employees"],
    },
    {
        "keywords": ["space", "room", "office space", "seat", "inventory"],
        "primary": ["m_space_inventory", "space_request", "assign_space_designation"],
        "buffer": ["m_department", "m_designation"],
    },
    {
        "keywords": ["credit note", "credit", "refund", "note"],
        "primary": ["credit_note"],
        "buffer": ["invoice", "customers", "organization"],
    },
    {
        "keywords": ["referral", "referral fee"],
        "primary": ["referral_fee_master"],
        "buffer": ["m_serviceline", "m_department", "employees"],
    },
]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# INTENT & TECHNICAL COLUMN MAPPING FOR OPTIMIZED PROMPTS
# ---------------------------------------------------------------------------

INTENT_TABLE_MAP: Dict[str, tuple] = {
    "receivables": (["invoice", "receipt_details", "receipts", "credit_note"], ["customers", "m_serviceline", "employees", "m_invoice_status"]),
    "revenue": (["invoice", "invoice_details"], ["customers", "employees", "m_serviceline", "m_invoice_status"]),
    "resource_utilization": (["emp_payroll_timesheet", "timesheet_project", "ts_project_date"], ["employees", "projects", "m_serviceline", "m_department"]),
    "project_summary": (["projects", "m_project_status"], ["customers", "employees", "m_serviceline", "project_tasks"]),
    "proposals": (["proposal", "m_proposal_status"], ["customers", "employees", "m_serviceline", "projects"]),
    "saleslead": (["saleslead", "m_leadstatus"], ["employees", "customers", "m_serviceline"]),
    "leave_request": (["leave_request", "m_leave_status", "employee_leave_balance"], ["employees", "m_department"]),
    "employee": (["employees", "m_department", "m_designation"], ["employee_salary_details"]),
    "customer": (["customers", "customer_contact_details"], ["contacts", "m_industry_type"]),
}



# ---------------------------------------------------------------------------
# SQL RULES — modularized to reduce input prompt bloat
# ---------------------------------------------------------------------------

BASE_SQL_RULES = """CRITICAL SQL RULES:
- Return EXACTLY ONE SELECT query. No semicolons separating multiple queries.
- ROUND all decimals to 2 places. Currency is BHD (Bahraini Dinar).
- LIMIT 1000 for receivable/export queries. LIMIT 50 for all other query result sets.
- Date filters: saleslead uses lead_date. invoice / proposal / project / job_estimation / leave_request use created_at.
- Status joins: projects.status_id -> m_project_status.id | job_estimation.status_id -> m_jobestimation_status.id.
- Foreign keys: invoice.client_name_id -> customers.id | projects.client -> customers.id | projects.manager -> employees.id | projects.partner -> employees.id | saleslead.lead_owner -> employees.id.
- proposal.project_id IS NULL means proposal is still open/pending.
- project_tasks.status is string ('To Do', 'In Progress', 'Finished'). Pending tasks: status != 'Finished'.
- Name filtering: ALWAYS use LIKE '%name%' (case-insensitive substring match).
- Leave balance: employee_leave_balance (employee_id, leave_type_id, balance, year).
- Payroll: emp_payroll (employee_id, month, year, basic_salary, total_allowances, total_deductions, net_salary).
- Salary: employee_salary_details or employees (emp_basic_salary, emp_gross_salary).
- If asked for two numbers, use conditional aggregation or UNION ALL — never two separate queries."""

RESOURCE_UTILIZATION_TEMPLATE = """--- RESOURCE UTILIZATION QUERY TEMPLATE ---
-- Timesheet hours stored in ts_project_date.hours (TIME column 'HH:MM:SS').
-- ALWAYS filter tp.status_id = 3 (Approved timesheets only).
-- Convert to decimal hours: SUM(TIME_TO_SEC(tpd.hours)) / 3600
-- Date range filter on tpd.project_date BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'.
SELECT
  e.employee_name,
  p.name AS project_name,
  SEC_TO_TIME(SUM(TIME_TO_SEC(tpd.hours))) AS total_hrs_formatted,
  ROUND(SUM(TIME_TO_SEC(tpd.hours)) / 3600, 2) AS total_hours
FROM timesheet_project tp
JOIN ts_project_date tpd ON tp.id = tpd.timesheet_id
JOIN employees e ON tp.employee_id = e.id
JOIN projects p  ON tp.project_id  = p.id
WHERE tp.status_id = 3
  AND tpd.project_date BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'
  AND e.employee_name LIKE '%{employee_name}%'
GROUP BY e.id, e.employee_name, p.id, p.name
ORDER BY total_hours DESC
LIMIT 50;"""

RECEIVABLE_TEMPLATE = """--- FILTERED RECEIVABLE REPORT TEMPLATE ---
SELECT
  DATE_FORMAT(i.created_at, '%d-%m-%Y') AS invoice_date,
  i.invoice_no AS reference_no,
  sl.name AS service_line,
  e.employee_name AS project_in_charge,
  c.customer_name AS customer_name,
  ROUND(i.total_net_amount, 2) AS invoice_amount,
  ROUND(COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0), 2) AS paid_amount,
  ROUND(i.total_net_amount
        - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
        - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0), 2) AS remaining_amount,
  CASE
    WHEN DATEDIFF(CURDATE(), i.created_at) < 30  THEN '<30 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 60  THEN '30-60 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days'
    ELSE '>365 Days'
  END AS ageing_bucket,
  mis.name AS payment_status
FROM invoice i
LEFT JOIN customers c       ON i.client_name_id      = c.id
LEFT JOIN m_serviceline sl  ON i.service_line_id     = sl.id
LEFT JOIN employees e       ON i.project_in_charge_id = e.id
LEFT JOIN m_invoice_status mis ON i.payment_status_id = mis.id
WHERE i.is_active = 1
  AND i.payment_status_id NOT IN (2, 4)
  AND (i.total_net_amount
       - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
       - COALESCE((SELECT SUM(cn.total_amount)   FROM credit_note cn   WHERE cn.invoice_id = i.id), 0)) > 0
ORDER BY i.created_at DESC
LIMIT 1000"""

# Backwards compatibility alias
SQL_RULES = BASE_SQL_RULES


def get_sql_rules_for_question(question: str, intent: Optional[Any] = None) -> str:
    """Returns concise SQL rules and injects specific templates ONLY when relevant."""
    q_lower = question.lower()
    metric_type = str(getattr(intent, "metric_type", "") or "").lower()

    rules = [BASE_SQL_RULES]

    if metric_type in ("resource_utilization", "timesheet") or any(kw in q_lower for kw in ["utilization", "utilisation", "timesheet", "billable hours"]):
        rules.append(RESOURCE_UTILIZATION_TEMPLATE)

    if metric_type in ("receivables", "revenue", "aging") or any(kw in q_lower for kw in ["receivable", "aging", "ageing", "overdue", "outstanding"]):
        rules.append(RECEIVABLE_TEMPLATE)

    return "\n\n".join(rules)


FISCAL_YEAR_HEADER = """FISCAL YEAR:
Current FY: {fy_start} to {fy_end}
"this year" / "current year" / "FY" = above dates.
Quarters: Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep.
"""


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def get_schema_for_question(question: str, intent: Optional[Any] = None) -> str:
    """
    Returns a compact schema string for the given question.
    - Uses intent metadata if available for precise table selection.
    - Matches question keywords against _KEYWORD_MAP.
    - Omits non-queryable internal technical columns to reduce prompt bloat.
    """
    _load_live_schema()

    q_lower = question.lower()
    metric_type = str(getattr(intent, "metric_type", "") or "").lower()

    primary_set: Set[str] = set()
    buffer_set: Set[str] = set()

    # 1. Check intent-based table mapping
    if metric_type and metric_type in INTENT_TABLE_MAP:
        p, b = INTENT_TABLE_MAP[metric_type]
        primary_set.update(p)
        buffer_set.update(b)

    # 2. Check keyword matching
    for entry in _KEYWORD_MAP:
        if any(kw in q_lower for kw in entry["keywords"]):
            primary_set.update(entry["primary"])
            buffer_set.update(entry["buffer"])

    # 3. Smart fallback: if no tables selected, use core CRM tables
    if not primary_set:
        primary_set = {"customers", "employees", "projects", "invoice", "proposal", "saleslead"}
        buffer_set = {"m_serviceline", "m_department", "m_project_status"}

    selected = primary_set.union(buffer_set)

    # Build schema string
    lines = ["RELEVANT TABLES & COLUMNS (from live database):"]
    for tbl in sorted(selected):
        if tbl in _LIVE_SCHEMA_CACHE:
            is_p = tbl in primary_set
            lines.append(f"- {_fmt_table(tbl, is_primary=is_p)}")

    # Add key join hints for selected tables
    joins = _get_joins_for_tables(selected)
    if joins:
        lines.append("\nKEY JOINS:")
        lines.extend(f"  - {j}" for j in joins)

    return "\n".join(lines)


def get_full_schema_string() -> str:
    """Returns the complete schema of every table in the database."""
    _load_live_schema()
    lines = ["COMPLETE DATABASE SCHEMA (all tables):"]
    for tbl in sorted(_LIVE_SCHEMA_CACHE.keys()):
        lines.append(f"- {_fmt_table(tbl)}")
    return "\n".join(lines)


def get_table_count_for_question(question: str) -> int:
    _load_live_schema()
    q_lower = question.lower()
    selected: Set[str] = set()
    for entry in _KEYWORD_MAP:
        if any(kw in q_lower for kw in entry["keywords"]):
            selected.update(entry["primary"])
            selected.update(entry["buffer"])
    return len(selected) if selected else len(_LIVE_SCHEMA_CACHE)


# ---------------------------------------------------------------------------
# Internal join hints
# ---------------------------------------------------------------------------

_JOIN_HINTS = [
    # Billing
    ("invoice", "customers",        "invoice.client_name_id -> customers.id"),
    ("invoice", "projects",         "invoice.project_id -> projects.id"),
    ("invoice", "employees",        "invoice.project_in_charge_id -> employees.id"),
    ("invoice", "m_serviceline",    "invoice.service_line_id -> m_serviceline.id"),
    ("invoice", "m_invoice_status", "invoice.payment_status_id -> m_invoice_status.id"),
    ("invoice_details", "invoice",  "invoice_details.invoice_id -> invoice.id"),
    ("receipt_details", "invoice",  "receipt_details.invoice_id -> invoice.id"),
    ("receipt_details", "customers","receipt_details.client_id -> customers.id"),
    ("receipts", "receipt_details", "receipts.id = receipt_details.receipt_id"),
    ("credit_note", "invoice",      "credit_note.invoice_id -> invoice.id"),
    # CRM pipeline
    ("saleslead", "m_leadstatus",   "saleslead.lead_status_id -> m_leadstatus.id"),
    ("saleslead", "employees",      "saleslead.lead_owner -> employees.id"),
    ("saleslead", "customers",      "saleslead.customer_id -> customers.id"),
    ("saleslead", "m_serviceline",  "saleslead.serviceline_id -> m_serviceline.id"),
    ("job_estimation", "saleslead", "job_estimation.saleslead_id -> saleslead.id"),
    ("job_estimation", "m_jobestimation_status", "job_estimation.status_id -> m_jobestimation_status.id"),
    ("proposal", "job_estimation",  "proposal.job_estimation_id -> job_estimation.id"),
    ("proposal", "projects",        "proposal.project_id -> projects.id (NULL=open)"),
    ("proposal", "m_proposal_status","proposal.proposal_status_id -> m_proposal_status.id"),
    ("proposal", "m_engagement_status","proposal.engagement_status_id -> m_engagement_status.id"),
    ("proposal", "m_continuous_engagement_status","proposal.continuous_engagement_status_id -> m_continuous_engagement_status.id"),
    ("proposal", "customers",       "proposal.customer_id -> customers.id"),
    ("proposal", "m_serviceline",   "proposal.service_line_id -> m_serviceline.id"),
    # Projects
    ("projects", "m_project_status","projects.status_id -> m_project_status.id"),
    ("projects", "employees",       "projects.incharge -> employees.id | projects.partner -> employees.id"),
    ("projects", "customers",       "projects.client -> customers.id"),
    ("projects", "m_serviceline",   "projects.service_line_id -> m_serviceline.id"),
    ("project_tasks", "projects",   "project_tasks.project_id -> projects.id"),
    ("project_tasks", "employees",  "project_tasks.assignee_id -> employees.id"),
    ("project_team_members", "employees", "project_team_members.emp_id -> employees.id"),
    ("project_milestone", "projects","project_milestone.project_id -> projects.id"),
    # Employees
    ("employees", "m_department",   "employees.emp_department_id -> m_department.id"),
    ("employees", "m_designation",  "employees.emp_designation_id -> m_designation.id"),
    ("employees", "m_nationality",  "employees.emp_per_nationality_id -> m_nationality.id"),
    ("employee_salary_details","employees","employee_salary_details.employee_id -> employees.id"),
    ("employee_leave_balance","employees","employee_leave_balance.employee_id -> employees.id"),
    # Leave
    ("leave_request", "employees",  "leave_request.employee_id -> employees.id"),
    ("leave_request", "m_leave_status","leave_request.status_id -> m_leave_status.id"),
    ("leave_request", "m_leave_request_type","leave_request.leave_type_id -> m_leave_request_type.id"),
    # Timesheet
    ("timesheet_project", "projects","timesheet_project.project_id -> projects.id"),
    ("timesheet_project", "employees","timesheet_project.employee_id -> employees.id"),
    # KPI
    ("kpi_master", "m_serviceline", "kpi_master.service_line_id -> m_serviceline.id"),
    ("kpi_master", "m_department",  "kpi_master.department_id -> m_department.id"),
    ("kpi_master", "employees",     "kpi_master.employee_id -> employees.id"),
    ("serviceline_department","m_serviceline","serviceline_department.serviceline_id -> m_serviceline.id"),
    ("serviceline_department","m_department","serviceline_department.department_id -> m_department.id"),
    ("designation_rates","m_designation","designation_rates.designation_id -> m_designation.id"),
    # Customers
    ("customer_contact_details","customers","customer_contact_details.customer_id -> customers.id"),
    ("customer_branch_details","customers","customer_branch_details.customer_id -> customers.id"),
    # Cash advance / travel
    ("cash_advance","employees",    "cash_advance.employee_id -> employees.id"),
    ("travel_request","employees",  "travel_request.employee_id -> employees.id"),
    # Payroll
    ("emp_payroll","employees",     "emp_payroll.employee_id -> employees.id"),
    ("emp_final_settlement","employees","emp_final_settlement.employee_id -> employees.id"),
    ("emp_loans","employees",       "emp_loans.employee_id -> employees.id"),
    # Resource
    ("resource_allocation","employees","resource_allocation.employee_id -> employees.id"),
    ("resource_allocation","projects","resource_allocation.project_id -> projects.id"),
]


def _get_joins_for_tables(selected: Set[str]) -> List[str]:
    result = []
    for t1, t2, hint in _JOIN_HINTS:
        if t1 in selected and t2 in selected:
            result.append(hint)
    return result
