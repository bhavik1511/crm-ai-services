import json
import asyncio
from decimal import Decimal
from datetime import datetime, date
from typing import Optional
from langchain_core.tools import tool
from sqlalchemy import text
from db.database import get_db_engine
import urllib.request
import urllib.error
import contextvars

import os

# Global auth token set by main.py before each request
_CRM_AUTH_TOKEN = ''
CRM_API_BASE = os.getenv('CRM_API_BASE', 'http://localhost:3001/api/v1').rstrip('/')


# ---------------------------------------------------------------------------
# Server-side RBAC: user context set before every request by main.py / chat_routes.py
# Tools auto-read from this so the LLM cannot bypass RBAC by omitting parameters.
# ---------------------------------------------------------------------------
_CURRENT_USER_CONTEXT = contextvars.ContextVar("_CURRENT_USER_CONTEXT", default={})

def set_user_context(user_ctx: dict):
    """Set the current user context for RBAC enforcement in semantic layer tools.
    Called by main.py and chat_routes.py before each request."""
    _CURRENT_USER_CONTEXT.set(user_ctx or {})
    ctx = _CURRENT_USER_CONTEXT.get()
    emp_id = ctx.get('employee_id', 'N/A')
    tier = ctx.get('user_tier', 'N/A')
    print(f"[RBAC SemanticLayer] User context set: employee_id={emp_id}, user_tier={tier}")

def _resolve_rbac_params(employee_id, user_tier):
    """Resolve employee_id and user_tier from explicit params or _CURRENT_USER_CONTEXT.
    The SQL layer will handle restricting aggregate requests (employee_id=None) to the user's branch."""
    ctx = _CURRENT_USER_CONTEXT.get()
    resolved_emp_id = employee_id if employee_id is not None else ctx.get('employee_id')
    resolved_tier = user_tier if user_tier is not None else ctx.get('user_tier')

    # If LLM passed an ID, use it. Otherwise, leave it as None for aggregate requests.
    if employee_id is not None:
        resolved_emp_id = employee_id
    else:
        resolved_emp_id = None
            
    return resolved_emp_id, resolved_tier

def get_fiscal_info(dt=None):
    if dt is None:
        dt = datetime.now()
    if dt.month >= 10:
        fy_start_year = dt.year
        fy_end_year = dt.year + 1
    else:
        fy_start_year = dt.year - 1
        fy_end_year = dt.year
        
    fy_start = f"{fy_start_year}-10-01"
    fy_end = f"{fy_end_year}-09-30 23:59:59"
    return {"fy_start": fy_start, "fy_end": fy_end}

def _safe_value(v):
    """Convert DB types that are not JSON-serializable to native Python types."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v

async def _run_query(query: str, as_dict: bool = True):
    try:
        import asyncio
        def _sync_run():
            engine = get_db_engine()
            with engine.connect() as conn:
                result = conn.execute(text(query))
                if as_dict:
                    columns = result.keys()
                    return [
                        {k: _safe_value(v) for k, v in zip(columns, row)}
                        for row in result.fetchall()
                    ]
                return result.fetchall()
        return await asyncio.to_thread(_sync_run)
    except Exception as e:
        print(f"[_run_query DB ERROR]: {e}")
        return []
def _build_ownership_sql(employee_id: int, user_tier: int, table_alias: str = "i", check_service_line: bool = True, owner_col: str = "project_in_charge_id", sl_col: str = "service_line_id") -> str:
    if not employee_id:
        # If no employee_id is provided, the LLM is requesting aggregate data.
        ctx_dept_id = _CURRENT_USER_CONTEXT.get().get('department_id')
        ctx_emp_id = _CURRENT_USER_CONTEXT.get().get('employee_id')
        
        # If the JWT token didn't contain the department_id, fetch it from the database
        if not ctx_dept_id and ctx_emp_id:
            engine = get_db_engine()
            with engine.connect() as conn:
                try:
                    emp_res = conn.execute(text(f"SELECT emp_department_id FROM employees WHERE id = {ctx_emp_id}")).fetchone()
                    if emp_res and emp_res[0]:
                        ctx_dept_id = emp_res[0]
                except Exception as e:
                    print(f"[_build_ownership_sql] Error fetching department: {e}")
                    
        # Tier 1 or top management department gets full company data (only if tier <= 4)
        if user_tier == 1 or (ctx_dept_id == 17 and user_tier is not None and user_tier <= 4):
            return "1=1"
            
        # ALL other tiers get aggregate data scoped strictly to their department's Service Line
        if check_service_line and ctx_dept_id:
            engine = get_db_engine()
            with engine.connect() as conn:
                sl_res = conn.execute(text(f"SELECT serviceline_id FROM serviceline_department WHERE department_id = {ctx_dept_id}")).fetchone()
                if sl_res and sl_res[0]:
                    return f"({table_alias}.{sl_col} = {sl_res[0]})"
                    
        # Fallback if no specific service line is found for Tier <= 4
        if user_tier is not None and user_tier <= 4:
            return "1=1"
            
        # Strict fail closed if no identity is found or tier >= 5 without a service line
        return "1=0"
        
    engine = get_db_engine()
    with engine.connect() as conn:
        try:
            emp_res = conn.execute(text(f"SELECT emp_department_id FROM employees WHERE id = {employee_id}")).fetchone()
        except:
            emp_res = None
            
        ctx_dept_id = _CURRENT_USER_CONTEXT.get().get('department_id')
        emp_dep_id = emp_res[0] if (emp_res and emp_res[0] is not None) else ctx_dept_id
        
        has_full_access = False
        full_access_emps = {51, 157, 14, 15, 38, 146}
        # EXACT Parity with TS dashboard: Only Dep 17 (if tier <= 4) or these 6 explicit employee limits get 1=1. 
        if (emp_dep_id == 17 and user_tier is not None and user_tier <= 4) or employee_id in full_access_emps:
            has_full_access = True
            
        if has_full_access:
            return "1=1"
            
        # 1. Department Service Line Restriction
        sl_restrict = ""
        if check_service_line and emp_dep_id:
            sl_res = conn.execute(text(f"SELECT serviceline_id FROM serviceline_department WHERE department_id = {emp_dep_id}")).fetchone()
            if sl_res and sl_res[0]:
                sl_restrict = f" AND {table_alias}.{sl_col} = {sl_res[0]}"
                
        # 2. Hierarchy Restriction
        hierarchy_sql = f"""(
            {table_alias}.created_by = {employee_id}
            OR {table_alias}.{owner_col} = {employee_id}
            OR {table_alias}.created_by IN (SELECT id FROM employees WHERE emp_direct_supervisor_name_id = {employee_id})
            OR {table_alias}.{owner_col} IN (SELECT id FROM employees WHERE emp_direct_supervisor_name_id = {employee_id})
        )"""
        
        return f"({hierarchy_sql}{sl_restrict})"

@tool
async def get_revenue_metrics(start_date: Optional[str] = None, end_date: Optional[str] = None, employee_id: int = None, user_tier: int = None) -> str:
    """Useful for answering questions about Total Revenue, Revenue by Month, and TEAM BILLING (Service Line Breakdown).
    Args:
        start_date: Start of the period (defaults to CURRENT Fiscal Year start: Oct 1)
        end_date: End of the period (defaults to CURRENT Fiscal Year end: Sep 30)
        employee_id: The ID of the employee to filter by. Always pass the user's Employee ID from the system prompt to get personalized dashboard results.
        user_tier: The tier level of the requesting user (1-9). Tier 1-4 can access service_line aggregates. Tier 5+ are restricted to direct involvement only.
    """
    try:
        # RBAC: resolve from server-side context if LLM omits params
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        ownership_sql = _build_ownership_sql(employee_id, user_tier, "i", True)

        total_q = f"SELECT ROUND(SUM(total_amt_ex_vat), 2) AS revenue FROM invoice i WHERE i.is_active = 1 AND i.created_at BETWEEN '{start_date}' AND '{end_date}' AND {ownership_sql}"
        month_q = f"SELECT DATE_FORMAT(i.created_at, '%b-%Y') AS month, ROUND(SUM(i.total_amt_ex_vat), 2) AS amount FROM invoice i WHERE i.is_active = 1 AND i.created_at BETWEEN '{start_date}' AND '{end_date}' AND {ownership_sql} GROUP BY DATE_FORMAT(i.created_at, '%b-%Y') ORDER BY MIN(i.created_at)"
        gp_perf_q = f"SELECT sl.name, sl.short_code, ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS performing, COALESCE((SELECT ROUND(SUM(km.target_value), 2) FROM kpi_master km JOIN serviceline_department sd ON km.department_id = sd.department_id WHERE sd.serviceline_id = sl.id), 0) AS target FROM m_serviceline sl LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at BETWEEN '{start_date}' AND '{end_date}' AND {ownership_sql} WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code HAVING performing > 0 OR target > 0"

        # Force 12 months for the fiscal year (Oct-Sep)
        # We parse the start year and build the 12 month list
        s_date = datetime.strptime(start_date.split()[0], '%Y-%m-%d')
        start_year = s_date.year
        next_year = start_year + 1
        
        fiscal_months = [
            f"Oct-{start_year}", f"Nov-{start_year}", f"Dec-{start_year}",
            f"Jan-{next_year}", f"Feb-{next_year}", f"Mar-{next_year}",
            f"Apr-{next_year}", f"May-{next_year}", f"Jun-{next_year}",
            f"Jul-{next_year}", f"Aug-{next_year}", f"Sep-{next_year}"
        ]
        
        # Limit the output up to the current month to avoid showing future zeroes
        current_ym = (datetime.now().year, datetime.now().month)
        valid_months = []
        for m in fiscal_months:
            m_date = datetime.strptime(m, '%b-%Y')
            valid_months.append(m)
            # Stop appending once we reach the current real-world month
            if (m_date.year, m_date.month) >= current_ym:
                break

        if len(valid_months) >= 2:
            cp_start_m = datetime.strptime(valid_months[-2], '%b-%Y').strftime('%Y-%m-01')
        else:
            cp_start_m = datetime.strptime(valid_months[-1], '%b-%Y').strftime('%Y-%m-01') if valid_months else start_date
        
        team_billing_q = f"SELECT sl.name, sl.short_code, ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS performing FROM m_serviceline sl LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at >= '{cp_start_m}' AND {ownership_sql} WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code HAVING performing > 0"

        # Execute queries concurrently
        total_res, by_month, gp_perf_ytd, team_billing_cp = await asyncio.gather(
            _run_query(total_q),
            _run_query(month_q),
            _run_query(gp_perf_q),
            _run_query(team_billing_q)
        )

        total = total_res[0]['revenue'] if total_res and total_res[0]['revenue'] else 0
        
        # Map DB results to valid months up to current
        db_month_map = {row['month']: row['amount'] for row in by_month}
        full_by_month = [{"month": m, "amount": float(db_month_map.get(m, 0))} for m in valid_months]

        # Calculate Team Billing specifically for the "Current Period" (last two months)
        # This matches the dashboard's Team Billing (CM) widget which sums the visible bars
        current_period_billing = sum(row['performing'] for row in team_billing_cp) if team_billing_cp else 0

        return json.dumps({
            "total_revenue_ytd": total,
            "gp_performance_ytd_breakdown": gp_perf_ytd,
            "current_team_billing_period_total": round(current_period_billing, 2),
            "current_team_billing_breakdown": team_billing_cp,
            "revenue_by_month": full_by_month
        })
    except Exception as e:
        return f"Error retrieving revenue metrics: {str(e)}"

@tool
async def get_receivables_metrics(employee_id: int = None, user_tier: int = None, service_line: str = None) -> str:
    """Useful for answering questions about Total Receivables and Receivables Ageing summary. Does not require dates. Always pass the user's Employee ID from the system prompt to get personalized dashboard results.

    Args:
        employee_id: The ID of the employee to filter by
        user_tier: The tier level of the requesting user (1-9). Tier 1-4 can access service_line aggregates. Tier 5+ are restricted to direct involvement only.
        service_line: Optional service line name to filter by (e.g. "Audit", "Tax", "Advisory"). Leave blank for all service lines.
    """
    try:
        # RBAC: resolve from server-side context if LLM omits params
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        # Use exact backend base logic: Payment status NOT IN (2: Paid, 4: Cancelled)
        # Outstanding = total_net_amount - receipt_applied_amount - credit_note_total
        base_outstanding_sql = """
            (i.total_net_amount
            - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
            - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0))
        """

        ownership_sql = _build_ownership_sql(employee_id, user_tier, "i", True)

        # Build optional service line filter
        sl_join = ""
        sl_filter = ""
        if service_line and service_line.strip().lower() not in ("all", ""):
            sl_join = "LEFT JOIN m_serviceline sl ON sl.id = i.service_line_id"
            sl_filter = f"AND sl.name LIKE '%{service_line.strip()}%'"

        total_q = f"""
            SELECT ROUND(SUM({base_outstanding_sql}), 2) AS total_receivables 
            FROM invoice i 
            {sl_join}
            WHERE i.is_active = 1 
              AND i.payment_status_id NOT IN (2, 4)
              AND {base_outstanding_sql} != 0
              AND {ownership_sql}
              {sl_filter}
        """
        total_res = await _run_query(total_q)
        total = total_res[0]['total_receivables'] if total_res and total_res[0]['total_receivables'] else 0

        ageing_q = f"""
            SELECT 
                CASE 
                    WHEN DATEDIFF(CURDATE(), i.created_at) < 30 THEN '<30 Days' 
                    WHEN DATEDIFF(CURDATE(), i.created_at) < 60 THEN '30-60 Days' 
                    WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days' 
                    WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days' 
                    WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days' 
                    ELSE '>365 Days' 
                END AS bucket, 
                ROUND(SUM({base_outstanding_sql}), 2) AS amount 
            FROM invoice i 
            {sl_join}
            WHERE i.is_active = 1 
              AND i.payment_status_id NOT IN (2, 4) 
              AND {base_outstanding_sql} != 0 
              AND {ownership_sql}
              {sl_filter}
            GROUP BY bucket
        """
        ageing = await _run_query(ageing_q)

        result = {
            "total_receivables": total,
            "ageing_buckets": ageing
        }
        if service_line and service_line.strip().lower() not in ("all", ""):
            result["filter_service_line"] = service_line.strip()

        return json.dumps(result)
    except Exception as e:
        return f"Error retrieving receivables metrics: {str(e)}"

@tool
async def get_pipeline_and_proposals(start_date: Optional[str] = None, end_date: Optional[str] = None, employee_id: int = None, user_tier: int = None) -> str:
    """Useful for answering questions about open leads, proposals, engagement letters, budgets, and WIN RATES.
    This tool returns:
    - service_pipeline_leads_open: Count and value of open leads
    - open_proposals: Count and budget of open proposals
    - won_proposals: Count and budget of WON proposals (converted to projects)
    - total_proposals: Total proposal count and budget
    - proposal_win_rate: Calculated win rate percentage (won_proposals / total_proposals * 100)
    - dashboard_proposal_metrics: Proposal Breakdown matching Dashboard Cards (total amount and count)
    - dashboard_engagement_metrics: Engagement Breakdown matching Dashboard Cards (total amount and count)
    - dashboard_continuous_engagement_metrics: Continuous Engagement Breakdown matching Dashboard Cards (total amount and count)
    Args:
        start_date: Start of the period (defaults to CURRENT Fiscal Year start: Oct 1)
        end_date: End of the period (defaults to CURRENT Fiscal Year end: Sep 30)
        employee_id: The ID of the employee to filter by. Always pass the user's Employee ID from the system prompt to get personalized dashboard results.
        user_tier: The tier level of the requesting user (1-9). Tier 1-4 can access service_line aggregates. Tier 5+ are restricted to direct involvement only.
    """
    try:
        # RBAC: resolve from server-side context if LLM omits params
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        # Build ownership filter matching the backend row-level security exactly
        ownership_sql = _build_ownership_sql(employee_id, user_tier, "sl", True, "lead_owner", "serviceline_id")

        # 1. Full Service Lead Breakdown (Matches the dashboard boxes)
        leads_breakdown_q = f"""
        SELECT ls.name as status_name, COUNT(sl.id) as count, ROUND(COALESCE(SUM(sl.budget_value), 0), 2) as value 
        FROM saleslead sl 
        JOIN m_leadstatus ls ON sl.lead_status_id = ls.id 
        WHERE sl.lead_date BETWEEN '{start_date}' AND '{end_date}'
          AND {ownership_sql}
        GROUP BY ls.name
        """

        # Build proposal ownership filter matching the backend RBAC
        prop_ownership_sql = _build_ownership_sql(employee_id, user_tier, "p", True, "created_by")
        
        # Dashboard Parity: ui_* queries must use the exact backend logic including Inner Joins on JobEstimation & SalesLead
        # and checking ownership against the SalesLead owner/creator, exactly matching crm-api-main/mysqlProposalRepository.ts
        ui_prop_ownership_sql = _build_ownership_sql(employee_id, user_tier, "sl", True, "lead_owner", "serviceline_id")

        # CRITICAL: AND p.project_id IS NULL mirrors the dashboard "Pending Projects" filter.
        # Without this filter the counts are inflated by proposals already converted to projects.
        props_q = f"SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.is_active = 1 AND p.proposal_status_id IN (1, 7, 8) AND p.project_id IS NULL AND p.created_at BETWEEN '{start_date}' AND '{end_date}' AND {prop_ownership_sql}"
        won_props_q = f"SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.is_active = 1 AND p.project_id IS NOT NULL AND p.created_at BETWEEN '{start_date}' AND '{end_date}' AND {prop_ownership_sql}"
        total_props_q = f"SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.is_active = 1 AND p.created_at BETWEEN '{start_date}' AND '{end_date}' AND {prop_ownership_sql}"
        
        # Dashboard chart queries — use proper LEFT JOINs with WHERE clause (MySQL doesn't allow ON filters inside a parenthesized JOIN group).
        # Exclude status ids 9 (Project Pending) and 10 (All Project Created) to match the frontend filter in StatusInfoList.tsx.
        ui_props_q = (
            f"SELECT ps.name as status_name, COUNT(p.id) AS total_entries, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget "
            f"FROM m_proposal_status ps "
            f"LEFT JOIN proposal p ON ps.id = p.proposal_status_id AND p.is_active = 1 AND p.created_at BETWEEN '{start_date}' AND '{end_date}' "
            f"LEFT JOIN job_estimation je ON p.job_estimation_id = je.id "
            f"LEFT JOIN saleslead sl ON je.saleslead_id = sl.id "
            f"WHERE ps.id NOT IN (9, 10) AND ({ui_prop_ownership_sql} OR p.id IS NULL) "
            f"GROUP BY ps.id, ps.name, ps.sequence ORDER BY ps.sequence ASC"
        )
        ui_engs_q = (
            f"SELECT es.name as status_name, COUNT(p.id) AS total_entries, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget "
            f"FROM m_engagement_status es "
            f"LEFT JOIN proposal p ON es.id = p.engagement_status_id AND p.is_active = 1 AND p.created_at BETWEEN '{start_date}' AND '{end_date}' "
            f"LEFT JOIN job_estimation je ON p.job_estimation_id = je.id "
            f"LEFT JOIN saleslead sl ON je.saleslead_id = sl.id "
            f"WHERE ({ui_prop_ownership_sql} OR p.id IS NULL) "
            f"GROUP BY es.id, es.name, es.sequence ORDER BY es.sequence ASC"
        )
        ui_cont_engs_q = (
            f"SELECT ces.name as status_name, COUNT(p.id) AS total_entries, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget "
            f"FROM m_continuous_engagement_status ces "
            f"LEFT JOIN proposal p ON ces.id = p.continuous_engagement_status_id AND p.is_active = 1 AND p.created_at BETWEEN '{start_date}' AND '{end_date}' "
            f"LEFT JOIN job_estimation je ON p.job_estimation_id = je.id "
            f"LEFT JOIN saleslead sl ON je.saleslead_id = sl.id "
            f"WHERE ({ui_prop_ownership_sql} OR p.id IS NULL) "
            f"GROUP BY ces.id, ces.name ORDER BY ces.id ASC"
        )

        import asyncio
        (
            leads_breakdown,
            props,
            won_props,
            total_props,
            ui_props,
            ui_engs,
            ui_cont_engs
        ) = await asyncio.gather(
            _run_query(leads_breakdown_q),
            _run_query(props_q),
            _run_query(won_props_q),
            _run_query(total_props_q),
            _run_query(ui_props_q),
            _run_query(ui_engs_q),
            _run_query(ui_cont_engs_q)
        )

        # Main "Open Leads" summary
        leads_open = next((item for item in leads_breakdown if item['status_name'] == 'Open'), {"count": 0, "value": 0})

        # Calculate win rate
        won_count = int(won_props[0]['count']) if won_props else 0
        total_count = int(total_props[0]['count']) if total_props else 0
        win_rate = (won_count / total_count * 100) if total_count > 0 else 0

        return json.dumps({
            "service_pipeline_leads_summary": leads_open,
            "service_leads_breakdown": leads_breakdown,
            "open_proposals": props[0] if props else {"count": 0, "total_budget": 0},
            "won_proposals": won_props[0] if won_props else {"count": 0, "total_budget": 0},
            "total_proposals": total_props[0] if total_props else {"count": 0, "total_budget": 0},
            "proposal_win_rate": round(win_rate, 2),
            "dashboard_proposal_metrics_breakdown": ui_props,
            "dashboard_engagement_metrics_breakdown": ui_engs,
            "dashboard_continuous_engagement_metrics_breakdown": ui_cont_engs
        })
    except Exception as e:
        return f"Error retrieving pipeline metrics: {str(e)}"

@tool
async def get_active_projects_metrics(start_date: Optional[str] = None, end_date: Optional[str] = None, employee_id: int = None, is_active: Optional[bool] = True) -> str:
    """Useful for answering questions about projects, active projects, WIP projects, completed projects, non-active projects, task overview, overdue tasks, Overall Completion, Actual Recoverability, and Estimated Recoverability.
    This tool calls the same CRM backend API that the dashboard uses, so the numbers will always match the dashboard exactly.
    Always pass the user's Employee ID from the system prompt to get results scoped to the logged-in user.
    Args:
        start_date: Start of the period (defaults to CURRENT Fiscal Year start: Oct 1)
        end_date: End of the period (defaults to CURRENT Fiscal Year end: Sep 30)
        employee_id: The Employee ID of the logged-in user. ALWAYS pass this from the system prompt if the user asks for "my" data.
        is_active: Set to False if the user asks for non-active, completed, or inactive projects. Defaults to True.
    """
    try:
        # RBAC: resolve from server-side context if LLM omits params
        employee_id, _ = _resolve_rbac_params(employee_id, None)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        ownership_sql = _build_ownership_sql(employee_id, _CURRENT_USER_CONTEXT.get().get('user_tier'), "p", True, "incharge")
        is_active_val = "1" if is_active else "0"

        # -----------------------------------------------------------------------
        # STEP 1: Count active projects. To match the CRM UI perfectly, we must filter
        # by 'created_at' since the backend Projects List API uses date_field='created_at'.
        # -----------------------------------------------------------------------
        overlap_count_q = f"""
            SELECT COUNT(DISTINCT p.id) AS total_active
            FROM projects p
            WHERE p.is_active = {is_active_val}
              AND p.created_at BETWEEN '{start_date}' AND '{end_date}'
              AND {ownership_sql}
        """

        # -----------------------------------------------------------------------
        # STEP 2: Count by status (Active, Planned, etc.)
        # -----------------------------------------------------------------------
        status_breakdown_q = f"""
            SELECT ps.name AS label, COUNT(p.id) AS total
            FROM m_project_status ps
            LEFT JOIN projects p
              ON p.status_id = ps.id
             AND p.is_active = {is_active_val}
             AND p.created_at BETWEEN '{start_date}' AND '{end_date}'
             AND {ownership_sql}
            GROUP BY ps.id, ps.name
        """

        # -----------------------------------------------------------------------
        # STEP 3: Compute actual recoverability for these projects
        # -----------------------------------------------------------------------
        recoverability_q = f"""
            SELECT
                COALESCE(SUM(proj_data.approved_fees), 0) AS total_approved_fees,
                COALESCE(SUM(proj_data.actual_cost), 0)   AS total_actual_cost
            FROM (
                SELECT
                    p.id,
                    MAX(COALESCE(pr.approved_fees, 0)) AS approved_fees,
                    COALESCE(
                        SUM(
                            TIME_TO_SEC(tp.total_hrs) / 3600.0
                            * dr.hourly
                        ), 0
                    ) AS actual_cost
                FROM projects p
                LEFT JOIN proposal pr ON p.proposal_id = pr.id
                LEFT JOIN timesheet_project tp
                  ON tp.project_id = p.id
                  AND tp.status_id = 3
                LEFT JOIN employees e2 ON e2.id = tp.employee_id
                LEFT JOIN m_designation_rates dr ON dr.designation_id = e2.emp_designation_id
                WHERE p.is_active = {is_active_val}
                  AND p.created_at BETWEEN '{start_date}' AND '{end_date}'
                  AND {ownership_sql}
                GROUP BY p.id
            ) AS proj_data
        """

        overlap_res, status_res, rec_res = await asyncio.gather(
            _run_query(overlap_count_q),
            _run_query(status_breakdown_q),
            _run_query(recoverability_q),
        )

        total_active = int(overlap_res[0].get('total_active', 0)) if overlap_res else 0
        total_approved = float(rec_res[0].get('total_approved_fees', 0)) if rec_res else 0.0
        total_actual   = float(rec_res[0].get('total_actual_cost', 0))   if rec_res else 0.0
        actual_rec_pct = round((total_approved / total_actual) * 100, 2) if total_actual > 0 else 0.0

        # Also get total lifetime active projects (company-wide) for context
        lifetime_q = f"SELECT COUNT(id) as c FROM projects WHERE is_active = {is_active_val} AND {ownership_sql}"
        lifetime_res = await _run_query(lifetime_q)
        total_lifetime = int(lifetime_res[0]['c']) if lifetime_res else 0

        # -----------------------------------------------------------------------
        # STEP 4: Optionally get task-level metrics from the CRM API
        # -----------------------------------------------------------------------
        task_data = {}
        if _CRM_AUTH_TOKEN and is_active:
            try:
                import urllib.parse
                safe_start = urllib.parse.quote(str(start_date))
                safe_end   = urllib.parse.quote(str(end_date))
                task_url = (
                    f"{CRM_API_BASE}/projects/task-counts?"
                    f"start_date={safe_start}&end_date={safe_end}"
                )
                if employee_id:
                    task_url += f"&emp_id={employee_id}"

                def _fetch_task_counts():
                    req = urllib.request.Request(task_url, headers={
                        'Authorization': f'Bearer {_CRM_AUTH_TOKEN}',
                        'Content-Type': 'application/json'
                    })
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read())

                task_raw = await asyncio.to_thread(_fetch_task_counts)
                task_data = task_raw.get('data', task_raw)
                if not isinstance(task_data, dict):
                    task_data = {}
            except Exception as task_err:
                print(f"[AI] task-counts API failed (non-critical): {task_err}")

        return json.dumps({
            "total_projects": total_active,
            "total_approved_fees": round(total_approved, 2),
            "total_actual_cost": round(total_actual, 2),
            "actual_recoverability_percentage": actual_rec_pct,
            "projects_by_status": status_res,
            "overdue_tasks": task_data.get("overdueTask", "N/A"),
            "overdue_projects": task_data.get("overdueProject", "N/A"),
            "overall_completion_percentage": task_data.get("overallCompletion", "N/A"),
            "date_range": {"start": start_date, "end": end_date},
            "methodology": "Filtered by created_at to exactly match CRM Projects List UI counts. Recoverability = approved_fees / actual_hourly_cost * 100",
            "source": "SQL_QUERY"
        }, default=str)
    except Exception as e:
        return f"Error retrieving active projects metrics: {str(e)}"

@tool
async def get_high_value_proposals(start_date: Optional[str] = None, end_date: Optional[str] = None) -> str:
    """Useful for answering questions about High Value Proposals, top proposals, or biggest proposals.
    Args:
        start_date: Start of the period (defaults to CURRENT Fiscal Year start: Oct 1)
        end_date: End of the period (defaults to CURRENT Fiscal Year end: Sep 30)
    """
    try:
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        query = f"SELECT p.id as proposal_id, COALESCE(c.customer_name, co.cd_company_name, co.first_name, 'N/A') as client_name, p.total_costs as budget_value, DATEDIFF(CURDATE(), p.created_at) as age_in_days FROM proposal p LEFT JOIN customers c ON p.client_id = c.id LEFT JOIN contacts co ON p.contact_id = co.id WHERE p.is_active = 1 AND p.created_at BETWEEN '{start_date}' AND '{end_date}' ORDER BY p.total_costs DESC, p.created_at DESC LIMIT 5"
        proposals = await _run_query(query)
        
        return json.dumps({
            "high_value_proposals_top_5": proposals
        }, default=str)
    except Exception as e:
        return f"Error retrieving high value proposals: {str(e)}"

@tool
async def get_comprehensive_customer_report(search_term: str) -> str:
    """Use this tool when the user asks about a specific customer, client, company, project, or provides any CRM identifier.
    Searches by: customer name, customer code, CR number, project code/name, invoice number, lead code, proposal code.
    Returns a complete 360-degree customer report including: identification, contacts, projects,
    pipeline (leads/proposals), invoices & receivables, aging, and credit notes.
    Args:
        search_term: The customer name, company name, project code, invoice number, or any CRM identifier to search for.
    """
    # RBAC: For tier >= 5, scope customer report to projects the user is involved in
    ctx = _CURRENT_USER_CONTEXT.get()
    ctx_employee_id = ctx.get('employee_id')
    ctx_user_tier = ctx.get('user_tier')
    try:
        # Track whether user searched specifically by project code/name
        matched_project_id = None
        matched_project_code = None

        CUSTOMER_COLS = """id, customer_name, cust_code, cust_cr_no, cust_email, cust_tel_number,
               risk_rating, is_active, cust_group_id, cust_comp_type_id, cust_industry_id,
               cust_country_id, cust_client_rel_id, cust_website, cust_pobox, cust_faxno,
               cust_address, cust_vd_vat_reg_no, created_at"""

        # --- Step 0: Smart 3-tier customer search (exact → prefix → substring) ---
        customers_found = []

        # TIER 1: Exact match on code or name (always returns at most 1)
        exact_q = f"""
        SELECT {CUSTOMER_COLS}
        FROM customers
        WHERE (cust_code = '{search_term}' OR customer_name = '{search_term}' OR cust_cr_no = '{search_term}')
        AND is_active = 1
        LIMIT 1
        """
        customers_found = await _run_query(exact_q)

        # TIER 2: Prefix match (T0018 → T0018X but not YT0018)
        if not customers_found:
            prefix_q = f"""
            SELECT {CUSTOMER_COLS}
            FROM customers
            WHERE (cust_code LIKE '{search_term}%' OR customer_name LIKE '{search_term}%')
            AND is_active = 1
            LIMIT 10
            """
            customers_found = await _run_query(prefix_q)

        # TIER 3: Substring match on name only (for partial company name searches)
        if not customers_found:
            substr_q = f"""
            SELECT {CUSTOMER_COLS}
            FROM customers
            WHERE customer_name LIKE '%{search_term}%'
            AND is_active = 1
            LIMIT 10
            """
            customers_found = await _run_query(substr_q)

        # If no direct customer match, try lookup via project code/name (exact first)
        if not customers_found:
            proj_exact_q = f"""
            SELECT DISTINCT c.{CUSTOMER_COLS.replace('id,', 'id AS id,')}
            FROM customers c
            JOIN projects p ON p.client = c.id
            WHERE p.code = '{search_term}'
            AND c.is_active = 1
            LIMIT 5
            """
            # Rebuild properly since alias trick is tricky - use explicit columns
            proj_exact_q = f"""
            SELECT DISTINCT c.id, c.customer_name, c.cust_code, c.cust_cr_no, c.cust_email,
                   c.cust_tel_number, c.risk_rating, c.is_active, c.cust_group_id,
                   c.cust_comp_type_id, c.cust_industry_id, c.cust_country_id,
                   c.cust_client_rel_id, c.cust_website, c.cust_pobox, c.cust_faxno,
                   c.cust_address, c.cust_vd_vat_reg_no, c.created_at
            FROM customers c
            JOIN projects p ON p.client = c.id
            WHERE p.code = '{search_term}'
            AND c.is_active = 1
            LIMIT 5
            """
            customers_found = await _run_query(proj_exact_q)
            if customers_found:
                proj_id_q = f"SELECT id, code FROM projects WHERE code = '{search_term}' AND is_active = 1 LIMIT 1"
                proj_id_res = await _run_query(proj_id_q)
                if proj_id_res:
                    matched_project_id = proj_id_res[0].get('id')
                    matched_project_code = proj_id_res[0].get('code')

        # Fallback: project name/code substring search
        if not customers_found:
            proj_lookup = f"""
            SELECT DISTINCT c.id, c.customer_name, c.cust_code, c.cust_cr_no, c.cust_email,
                   c.cust_tel_number, c.risk_rating, c.is_active, c.cust_group_id,
                   c.cust_comp_type_id, c.cust_industry_id, c.cust_country_id,
                   c.cust_client_rel_id, c.cust_website, c.cust_pobox, c.cust_faxno,
                   c.cust_address, c.cust_vd_vat_reg_no, c.created_at
            FROM customers c
            JOIN projects p ON p.client = c.id
            WHERE (p.code LIKE '{search_term}%' OR p.name LIKE '%{search_term}%')
            AND c.is_active = 1
            LIMIT 10
            """
            customers_found = await _run_query(proj_lookup)
            if customers_found:
                proj_id_q = f"""
                SELECT id, code FROM projects
                WHERE (code LIKE '{search_term}%' OR name LIKE '%{search_term}%')
                AND is_active = 1
                ORDER BY CASE WHEN code = '{search_term}' THEN 0 ELSE 1 END, id
                LIMIT 1
                """
                proj_id_res = await _run_query(proj_id_q)
                if proj_id_res:
                    matched_project_id = proj_id_res[0].get('id')
                    matched_project_code = proj_id_res[0].get('code')

        # If still not found, try via invoice number
        if not customers_found:
            inv_lookup = f"""
            SELECT DISTINCT c.id, c.customer_name, c.cust_code, c.cust_cr_no, c.cust_email,
                   c.cust_tel_number, c.risk_rating, c.is_active, c.cust_group_id,
                   c.cust_comp_type_id, c.cust_industry_id, c.cust_country_id,
                   c.cust_client_rel_id, c.cust_website, c.cust_pobox, c.cust_faxno,
                   c.cust_address, c.cust_vd_vat_reg_no, c.created_at
            FROM customers c
            JOIN invoice i ON i.client_name_id = c.id
            WHERE i.invoice_no = '{search_term}'
            AND c.is_active = 1
            LIMIT 1
            """
            customers_found = await _run_query(inv_lookup)

        # If still not found, try via lead code or proposal code
        if not customers_found:
            lead_lookup = f"""
            SELECT DISTINCT c.id, c.customer_name, c.cust_code, c.cust_cr_no, c.cust_email,
                   c.cust_tel_number, c.risk_rating, c.is_active, c.cust_group_id,
                   c.cust_comp_type_id, c.cust_industry_id, c.cust_country_id,
                   c.cust_client_rel_id, c.cust_website, c.cust_pobox, c.cust_faxno,
                   c.cust_address, c.cust_vd_vat_reg_no, c.created_at
            FROM customers c
            LEFT JOIN saleslead sl ON sl.customer_id = c.id
            LEFT JOIN proposal pr ON pr.customer_id = c.id
            WHERE (sl.code = '{search_term}' OR pr.code = '{search_term}')
            AND c.is_active = 1
            LIMIT 1
            """
            customers_found = await _run_query(lead_lookup)

        if not customers_found:
            return json.dumps({"error": f"No customer found matching '{search_term}'. Searched by customer name, code, CR number, project code, invoice number, lead code, and proposal code. Please verify the identifier."})

        if len(customers_found) > 1:
            # Check for exact code match among results
            exact_code = [c for c in customers_found if c.get('cust_code', '').lower() == search_term.lower()]
            exact_name = [c for c in customers_found if c.get('customer_name', '').lower() == search_term.lower()]
            if len(exact_code) == 1:
                customers_found = exact_code
            elif len(exact_name) == 1:
                customers_found = exact_name
            elif len(customers_found) <= 10:
                return json.dumps({
                    "multiple_matches": True,
                    "message": f"Found {len(customers_found)} customers whose code starts with '{search_term}'. Pick the correct one:",
                    "navigation_instructions": "Use these customer IDs to build navigation_links in your JSON block as: /customer/edit?id=<id>",
                    "matches": [
                        {
                            "id": c['id'],
                            "name": c['customer_name'],
                            "code": c['cust_code'],
                            "cr_no": c.get('cust_cr_no') or "N/A",
                            "navigate_url": f"/customer/edit?id={c['id']}"
                        }
                        for c in customers_found
                    ]
                }, default=str)

        cust = customers_found[0]
        cust_id = cust['id']
        cust_name = cust['customer_name']

        # --- Section 1: IDENTIFICATION (enriched with joins) ---
        ident_q = f"""
        SELECT c.id, c.customer_name, c.cust_code, c.cust_cr_no, c.cust_email,
               c.cust_tel_number, c.cust_website, c.cust_pobox, c.cust_faxno,
               c.cust_address, c.risk_rating, c.cust_vd_vat_reg_no,
               c.created_at AS customer_since,
               it.name AS industry,
               ct.name AS company_type,
               co.name AS country,
               e.employee_name AS client_relation_manager,
               g.name AS customer_group
        FROM customers c
        LEFT JOIN m_industry_type it ON c.cust_industry_id = it.id
        LEFT JOIN m_company_type ct ON c.cust_comp_type_id = ct.id
        LEFT JOIN m_countries co ON c.cust_country_id = co.id
        LEFT JOIN employees e ON c.cust_client_rel_id = e.id
        LEFT JOIN m_group g ON c.cust_group_id = g.id
        WHERE c.id = {cust_id}
        """
        identification = await _run_query(ident_q)

        # --- Section 2: CONTACTS ---
        contacts_q = f"""
        SELECT ccd.contact_name, ccd.department_name, ccd.tel_no, ccd.mob_no,
               ccd.email_id, ccd.is_default,
               cd.name AS designation
        FROM customer_contact_details ccd
        LEFT JOIN m_contact_designation cd ON ccd.designation_id = cd.id
        WHERE ccd.customer_id = {cust_id}
        """
        contacts = await _run_query(contacts_q)

        # --- Section 3: PROJECTS ---
        projects_q = f"""
        SELECT p.id, p.name, p.code, ps.name AS status, sl.name AS service_line,
               e.employee_name AS incharge, ep.employee_name AS partner,
               p.approved_fees, p.start_date, p.end_date, p.report_sign_date,
               p.audit_year, p.status_id
        FROM projects p
        JOIN m_project_status ps ON p.status_id = ps.id
        LEFT JOIN m_serviceline sl ON p.service_line_id = sl.id
        LEFT JOIN employees e ON p.incharge = e.id
        LEFT JOIN employees ep ON p.partner = ep.id
        WHERE p.client = {cust_id} AND p.is_active = 1
        {'AND (' + f"p.incharge = {ctx_employee_id} OR p.partner = {ctx_employee_id} OR p.main_incharge = {ctx_employee_id} OR p.created_by = {ctx_employee_id} OR p.id IN (SELECT project_id FROM project_team_members WHERE emp_id = {ctx_employee_id})" + ')' if ctx_user_tier is not None and ctx_user_tier >= 5 and ctx_employee_id else ''}
        ORDER BY p.created_at DESC
        LIMIT 50
        """
        projects = await _run_query(projects_q)

        # Compute project counts
        running_count = sum(1 for p in projects if p.get('status_id') in (1, 2, 5))
        completed_count = sum(1 for p in projects if p.get('status_id') in (6, 7, 8, 9, 10))
        pending_count = sum(1 for p in projects if p.get('status_id') not in (1, 2, 5, 6, 7, 8, 9, 10))
        total_project_value = sum(float(p.get('approved_fees') or 0) for p in projects)

        project_summary = {
            "total_projects": len(projects),
            "running": running_count,
            "completed": completed_count,
            "pending_other": pending_count,
            "total_value": round(total_project_value, 3),
            "projects": projects
        }

        # --- Section 4: PIPELINE (Leads, Job Estimations, Proposals) ---
        leads_q = f"""
        SELECT sl.id, sl.code, sl.enquiry_details, sl.budget_value, sl.lead_date,
               ls.name AS status, msl.name AS service_line,
               e.employee_name AS lead_owner
        FROM saleslead sl
        JOIN m_leadstatus ls ON sl.lead_status_id = ls.id
        LEFT JOIN m_serviceline msl ON sl.serviceline_id = msl.id
        LEFT JOIN employees e ON sl.lead_owner = e.id
        WHERE sl.customer_id = {cust_id}
        ORDER BY sl.lead_date DESC
        LIMIT 30
        """
        leads = await _run_query(leads_q)

        je_q = f"""
        SELECT je.id, je.code, je.ref_no, je.proposed_fees, je.approved_fees,
               je.total_costs, je.total_hours, je.recoverability,
               js.name AS status, je.from_date, je.to_date
        FROM job_estimation je
        JOIN m_jobestimation_status js ON je.status_id = js.id
        WHERE je.customer_id = {cust_id}
        ORDER BY je.created_at DESC
        LIMIT 30
        """
        job_estimations = await _run_query(je_q)

        proposals_q = f"""
        SELECT p.id, p.code, p.ref_no, p.proposed_fees, p.approved_fees,
               p.agreed_fees, p.recoverability, p.proposal_date,
               ps.name AS proposal_status, es.name AS engagement_status,
               msl.name AS service_line
        FROM proposal p
        LEFT JOIN m_proposal_status ps ON p.proposal_status_id = ps.id
        LEFT JOIN m_engagement_status es ON p.engagement_status_id = es.id
        LEFT JOIN m_serviceline msl ON p.service_line_id = msl.id
        WHERE p.customer_id = {cust_id}
        ORDER BY p.created_at DESC
        LIMIT 30
        """
        proposals = await _run_query(proposals_q)

        pipeline_summary = {
            "total_leads": len(leads),
            "open_leads": sum(1 for l in leads if l.get('status') == 'Open'),
            "leads": leads,
            "total_job_estimations": len(job_estimations),
            "job_estimations": job_estimations,
            "total_proposals": len(proposals),
            "proposals": proposals
        }

        # --- Section 5: INVOICES & RECEIVABLES ---
        invoices_q = f"""
        SELECT i.id, i.invoice_no, i.invoice_type, i.total_amount,
               i.total_amt_ex_vat, i.total_amt_inc_vat, i.total_vat_amount,
               i.total_net_amount, i.paid_amount, i.discount_amount,
               i.agreed_fees, i.created_at AS invoice_date,
               mis.name AS payment_status,
               msl.name AS service_line,
               p.name AS project_name, p.code AS project_code,
               GREATEST(ROUND(i.total_net_amount - COALESCE((
                   SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id
               ), 0) - COALESCE((
                   SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1
               ), 0), 3), 0) AS outstanding_amount
        FROM invoice i
        LEFT JOIN m_invoice_status mis ON i.payment_status_id = mis.id
        LEFT JOIN m_serviceline msl ON i.service_line_id = msl.id
        LEFT JOIN projects p ON i.project_id = p.id
        WHERE i.client_name_id = {cust_id} AND i.is_active = 1
        ORDER BY i.created_at DESC
        LIMIT 50
        """
        invoices = await _run_query(invoices_q)

        total_invoiced = sum(float(inv.get('total_net_amount') or 0) for inv in invoices)
        total_paid = sum(float(inv.get('paid_amount') or 0) for inv in invoices)
        total_outstanding = sum(float(inv.get('outstanding_amount') or 0) for inv in invoices)
        collection_rate = round((total_paid / total_invoiced * 100), 2) if total_invoiced > 0 else 0

        invoice_summary = {
            "total_invoices": len(invoices),
            "total_invoiced_amount": round(total_invoiced, 3),
            "total_paid_amount": round(total_paid, 3),
            "total_outstanding": round(total_outstanding, 3),
            "collection_rate_pct": collection_rate,
            "invoices": invoices
        }

        # --- Section 6: AGING BUCKETS ---
        aging_q = f"""
        SELECT
            CASE
                WHEN DATEDIFF(CURDATE(), i.created_at) < 30 THEN '<30 Days'
                WHEN DATEDIFF(CURDATE(), i.created_at) < 60 THEN '30-60 Days'
                WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days'
                WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days'
                WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days'
                ELSE '>365 Days'
            END AS bucket,
            COUNT(i.id) AS invoice_count,
            GREATEST(ROUND(SUM(
                i.total_net_amount
                - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
                - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1), 0)
            ), 3), 0) AS outstanding_amount
        FROM invoice i
        WHERE i.client_name_id = {cust_id}
          AND i.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND (i.total_net_amount
               - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
               - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1), 0)
              ) != 0
        GROUP BY bucket
        """
        aging = await _run_query(aging_q)

        # --- Section 7: CREDIT NOTES ---
        cn_q2 = f"""
        SELECT cn.id, cn.creditNoteNumber, cn.credit_amount, cn.tax_per,
               cn.tax_amount, cn.out_of_pocket_amount, cn.total_amount,
               cn.remarks, cn.credit_note_date,
               i.invoice_no AS linked_invoice_no
        FROM credit_note cn
        JOIN invoice i ON cn.invoice_id = i.id
        WHERE i.client_name_id = {cust_id}
          AND cn.is_active = 1
        ORDER BY cn.created_at DESC
        LIMIT 30
        """
        credit_notes = await _run_query(cn_q2)
        
        total_credit_notes_amount = sum(float(cn.get('total_amount') or 0) for cn in credit_notes)

        # --- Build final report ---
        report = {
            "customer_name": cust_name,
            "entity_name": matched_project_code or cust_name,
            "entity_type": "project" if matched_project_id else "customer",
            "metadata_instructions": f"CRITICAL: set your final navigate_to field EXACTLY to: /projects/individual-project?id={matched_project_id}" if matched_project_id else f"CRITICAL: set your final navigate_to field EXACTLY to: /customer/edit?id={cust_id}",
            "report_generated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "identification": identification[0] if identification else cust,
            "contacts": contacts,
            "projects": project_summary,
            "pipeline": pipeline_summary,
            "invoices_and_receivables": invoice_summary,
            "aging_buckets": aging,
            "credit_notes": {
                "total_credit_notes": len(credit_notes),
                "total_credit_amount": round(total_credit_notes_amount, 3),
                "details": credit_notes
            },
            "kpi_summary": {
                "collection_rate": f"{collection_rate}%",
                "total_projects": len(projects),
                "running_projects": running_count,
                "completed_projects": completed_count,
                "total_project_value": round(total_project_value, 3),
                "total_invoiced": round(total_invoiced, 3),
                "total_paid": round(total_paid, 3),
                "total_outstanding": round(total_outstanding, 3),
                "total_credit_notes": len(credit_notes),
                "total_credit_amount": round(total_credit_notes_amount, 3),
                "open_leads": sum(1 for l in leads if l.get('status') == 'Open'),
                "total_proposals": len(proposals)
            }
        }

        return json.dumps(report, default=str)

    except Exception as e:
        return json.dumps({"error": f"Failed to generate customer report: {str(e)}"})


# ---------------------------------------------------------------------------
# Excel KPI Export Generator 
# ---------------------------------------------------------------------------
async def get_kpi_excel_export_data(filters_applied: dict, period: str) -> dict:
    """
    Generates the exact layout for Book1.xlsx by fetching CY, LY, Secured, Open Proposals
    grouped by Service Line from the database.
    """
    import asyncio
    import json
    
    # 1. Parse dates (assuming period comes in as '01-10-2025 to 30-09-2026')
    cy_start = '2025-10-01'
    cy_end = '2026-09-30'
    ly_start = '2024-10-01'
    ly_end = '2025-09-30'
    
    if period and "to" in period:
        parts = [p.strip() for p in period.split("to")]
        if len(parts) == 2:
            try:
                cs = datetime.strptime(parts[0], "%d-%m-%Y")
                ce = datetime.strptime(parts[1], "%d-%m-%Y")
                cy_start = cs.strftime("%Y-%m-%d")
                cy_end = ce.strftime("%Y-%m-%d")
                ly_start = cy_start.replace(str(cs.year), str(cs.year - 1))
                ly_end = cy_end.replace(str(ce.year), str(ce.year - 1))
            except Exception:
                pass

    # Extract ID filters
    s_id = filters_applied.get("service_line_id")
    d_id = filters_applied.get("department_id")
    e_id = filters_applied.get("employee_id")

    # Base where clauses for filtering
    base_where = ""
    if s_id and str(s_id) != "0":
        base_where += f" AND service_line_id = {s_id}"

    # Active Service lines
    sl_query = "SELECT id, name FROM m_serviceline WHERE is_active=1"
    if s_id and str(s_id) != "0":
        sl_query += f" AND id = {s_id}"
    sl_query += " ORDER BY id ASC"
    
    service_lines = await _run_query(sl_query)
    data_map = { sl['id']: {
        'name': sl['name'], 'cy_actual': 0, 'cy_budget': 0, 'ly_actual': 0, 
        'ly_budget': 0, 'actual_ly_full': 0, 'budget_full': 0, 'secured_business': 0, 
        'open_proposals': 0, 'open_leads': 0, 'gp_actual': 0, 'gp_target': 0
    } for sl in service_lines }

    async def fetch_metric(query, key_mapping):
        try:
            res = await _run_query(query)
            for row in res:
                slid = row.get('service_line_id')
                if slid in data_map:
                    for db_key, mem_key in key_mapping.items():
                        data_map[slid][mem_key] = float(row.get(db_key) or 0)
        except Exception as e:
            print(f"[get_kpi_excel_export_data] Error running query: {e}")

    # 1. CY Revenue (Invoice)
    q_cy_rev = f"""
        SELECT service_line_id, SUM(total_amt_ex_vat) as total
        FROM invoice 
        WHERE is_active=1 AND created_at BETWEEN '{cy_start} 00:00:00' AND '{cy_end} 23:59:59' {base_where}
        GROUP BY service_line_id
    """
    await fetch_metric(q_cy_rev, {'total': 'cy_actual'})

    # 2. LY Revenue (Invoice)
    q_ly_rev = f"""
        SELECT service_line_id, SUM(total_amt_ex_vat) as total
        FROM invoice 
        WHERE is_active=1 AND created_at BETWEEN '{ly_start} 00:00:00' AND '{ly_end} 23:59:59' {base_where}
        GROUP BY service_line_id
    """
    await fetch_metric(q_ly_rev, {'total': 'ly_actual'})

    # 3. Budget (KPI Master) CY
    q_cy_budget = f"""
        SELECT service_line_id, SUM(target_value) as target_value, SUM(target_gp) as target_gp
        FROM kpi_master 
        WHERE is_active=1 AND target_month BETWEEN '{cy_start[:8]}01' AND '{cy_end[:8]}31' {base_where}
        GROUP BY service_line_id
    """
    await fetch_metric(q_cy_budget, {'target_value': 'cy_budget', 'target_gp': 'gp_target'})

    # 4. Budget (KPI Master) LY
    q_ly_budget = f"""
        SELECT service_line_id, SUM(target_value) as target_value
        FROM kpi_master 
        WHERE is_active=1 AND target_month BETWEEN '{ly_start[:8]}01' AND '{ly_end[:8]}31' {base_where}
        GROUP BY service_line_id
    """
    await fetch_metric(q_ly_budget, {'target_value': 'ly_budget'})

    # 5. Secured Business (Projects)
    q_secured = f"""
        SELECT service_line_id, SUM(approved_fees) as total
        FROM projects 
        WHERE is_active=1 AND created_at BETWEEN '{cy_start} 00:00:00' AND '{cy_end} 23:59:59' {base_where}
        GROUP BY service_line_id
    """
    await fetch_metric(q_secured, {'total': 'secured_business'})

    # 6. Open Proposals
    q_proposals = f"""
        SELECT serviceline_id as service_line_id, SUM(total_fees) as total
        FROM proposal 
        WHERE is_active=1 AND proposal_status_id IN (1, 2, 4) AND created_at BETWEEN '{cy_start} 00:00:00' AND '{cy_end} 23:59:59'
        GROUP BY serviceline_id
    """
    await fetch_metric(q_proposals, {'total': 'open_proposals'})

    # 7. Open Leads
    q_leads = f"""
        SELECT serviceline_id as service_line_id, SUM(budget_value) as total
        FROM saleslead 
        WHERE is_active=1 AND lead_status_id IN (1, 2) AND created_at BETWEEN '{cy_start} 00:00:00' AND '{cy_end} 23:59:59'
        GROUP BY serviceline_id
    """
    await fetch_metric(q_leads, {'total': 'open_leads'})

    # Compile rows
    headers = [
        ["Department Performance " + cy_start[:4] + "-" + cy_end[2:4], "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        [
            "Description", "CY YTD " + cy_start[:4] + "-" + cy_end[2:4], "", "", "LY YTD " + ly_start[:4] + "-" + ly_end[2:4], "", "", 
            "Actual LY " + ly_start[:4] + "-" + ly_end[2:4], "Budget " + cy_start[:4] + "-" + cy_end[2:4], "Budget", "Secured Business", 
            "Secured Business & YTD", "Open Proposals", "Open Sales Lead", "GP Performance", "", ""
        ],
        [
            "", "Actual", "Budget", "Variance", "Actual", "LY", "Variance", 
            "Variance", "", "Variance", "", "", "Actual", "KPI", "Variance"
        ]
    ]

    merges = [
        {"s": {"r": 0, "c": 0}, "e": {"r": 0, "c": 14}},  
        {"s": {"r": 1, "c": 1}, "e": {"r": 1, "c": 3}},   
        {"s": {"r": 1, "c": 4}, "e": {"r": 1, "c": 6}},   
        {"s": {"r": 1, "c": 14}, "e": {"r": 1, "c": 16}}, 
        {"s": {"r": 1, "c": 0}, "e": {"r": 2, "c": 0}},   
        {"s": {"r": 1, "c": 7}, "e": {"r": 2, "c": 7}},   
        {"s": {"r": 1, "c": 8}, "e": {"r": 2, "c": 8}},   
        {"s": {"r": 1, "c": 9}, "e": {"r": 2, "c": 9}},   
        {"s": {"r": 1, "c": 10}, "e": {"r": 2, "c": 10}}, 
        {"s": {"r": 1, "c": 11}, "e": {"r": 2, "c": 11}}, 
        {"s": {"r": 1, "c": 12}, "e": {"r": 2, "c": 12}}, 
        {"s": {"r": 1, "c": 13}, "e": {"r": 2, "c": 13}}, 
    ]

    rows = [["Revenue", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]]
    
    totals = {k: 0 for k in ['cy_act', 'cy_bud', 'cy_var', 'ly_act', 'ly_bud', 'ly_var', 'ly_full_var', 'cy_full_bud', 'bud_var', 'sec_bus', 'sec_ytd', 'open_prop', 'open_lead', 'gp_act', 'gp_kpi', 'gp_var']}

    for sl in service_lines:
        d = data_map[sl['id']]
        cy_var = d['cy_actual'] - d['cy_budget']
        ly_var = d['ly_actual'] - d['ly_budget']
        
        # Calculate GP Actual (usually CY Revenue - direct costs, but for this template we will just use 0 if not calculated, or assume it's part of GP target)
        gp_var = d['gp_actual'] - d['gp_target']
        
        sec_ytd = d['secured_business'] + d['cy_actual']

        row = [
            d['name'], 
            round(d['cy_actual'], 3) or 0, round(d['cy_budget'], 3) or 0, round(cy_var, 3) or 0,
            round(d['ly_actual'], 3) or 0, round(d['ly_budget'], 3) or 0, round(ly_var, 3) or 0,
            0, # Actual LY Full
            0, # Budget Full
            0, # Budget Variance
            round(d['secured_business'], 3) or 0, round(sec_ytd, 3) or 0, 
            round(d['open_proposals'], 3) or 0, round(d['open_leads'], 3) or 0,
            round(d['gp_actual'], 3) or 0, round(d['gp_target'], 3) or 0, round(gp_var, 3) or 0
        ]
        rows.append(row)

        totals['cy_act'] += d['cy_actual']
        totals['cy_bud'] += d['cy_budget']
        totals['cy_var'] += cy_var
        totals['ly_act'] += d['ly_actual']
        totals['ly_bud'] += d['ly_budget']
        totals['ly_var'] += ly_var
        totals['sec_bus'] += d['secured_business']
        totals['sec_ytd'] += sec_ytd
        totals['open_prop'] += d['open_proposals']
        totals['open_lead'] += d['open_leads']
        totals['gp_act'] += d['gp_actual']
        totals['gp_kpi'] += d['gp_target']
        totals['gp_var'] += gp_var

    rows.append([
        "Total Revenue", 
        round(totals['cy_act'], 3) or 0, round(totals['cy_bud'], 3) or 0, round(totals['cy_var'], 3) or 0,
        round(totals['ly_act'], 3) or 0, round(totals['ly_bud'], 3) or 0, round(totals['ly_var'], 3) or 0,
        0, 0, 0,
        round(totals['sec_bus'], 3) or 0, round(totals['sec_ytd'], 3) or 0, 
        round(totals['open_prop'], 3) or 0, round(totals['open_lead'], 3) or 0,
        round(totals['gp_act'], 3) or 0, round(totals['gp_kpi'], 3) or 0, round(totals['gp_var'], 3) or 0
    ])

    return {
        "filename": f"Department_Performance_{cy_start[:4]}_{cy_end[2:4]}",
        "sheets": [
            {
                "name": "Summary",
                "headers": headers,
                "rows": rows,
                "merges": merges
            }
        ]
    }


# ---------------------------------------------------------------------------
# Utility helpers (merged from CRM branch � used by agent and other modules)
# ---------------------------------------------------------------------------

@tool
async def get_total_estimation_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    customer_name: str = None,
    service_line: str = None,
    incharge_employee: str = None,
    limit: int = 50,
    employee_id: int = None,
    user_tier: int = None,
) -> str:
    """Useful for answering questions about the Total Estimation Report — comparing estimated hours vs actual hours used on projects.
    Returns per-project data with: project code, project name, customer name, customer group, service line,
    start/end dates, project incharge, estimated hours (from job estimation), actual hours used (approved timesheets),
    and the difference (actual - estimated). Positive difference means over budget; negative means under budget.
    Use this when users ask: 'which projects are over estimated hours', 'show me projects exceeding budget hours',
    'total estimation report', 'estimated vs actual hours', 'hour overruns', or similar.

    Args:
        start_date: Filter projects starting from this date (YYYY-MM-DD). Defaults to current fiscal year start.
        end_date: Filter projects ending before this date (YYYY-MM-DD). Defaults to current fiscal year end.
        customer_name: Optional customer name filter (substring match).
        service_line: Optional service line name filter (substring match).
        incharge_employee: Optional project incharge employee name filter (substring match).
        limit: Max number of projects to return (default 50).
        employee_id: The ID of the logged-in employee for RBAC scoping.
        user_tier: The tier level of the requesting user (1-9).
    """
    try:
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        # Build RBAC ownership filter (scoped to project incharge / partner / service line)
        ownership_sql = _build_ownership_sql(employee_id, user_tier, "p", True, "incharge", "service_line_id")

        # Optional filters
        customer_filter = f"AND c.customer_name LIKE '%{customer_name}%'" if customer_name else ""
        sl_filter = f"AND sl.name LIKE '%{service_line}%'" if service_line else ""
        incharge_filter = f"AND e_incharge.employee_name LIKE '%{incharge_employee}%'" if incharge_employee else ""

        query = f"""
            SELECT
                p.id AS project_id,
                p.code AS project_code,
                p.name AS project_name,
                c.customer_name,
                grp.name AS customer_group,
                sl.name AS service_line,
                p.start_date,
                p.end_date,
                e_incharge.employee_name AS project_incharge,
                ROUND(
                    COALESCE((
                        SELECT je.total_hours
                        FROM proposal pr
                        JOIN job_estimation je ON je.id = pr.job_estimation_id
                        WHERE pr.id = p.proposal_id
                        LIMIT 1
                    ), 0), 2
                ) AS estimated_hours,
                ROUND(
                    COALESCE((
                        SELECT SUM(TIME_TO_SEC(tp.total_hrs)) / 3600
                        FROM timesheet_project tp
                        WHERE tp.project_id = p.id AND tp.status_id = 3
                    ), 0), 2
                ) AS actual_hours,
                ROUND(
                    COALESCE((
                        SELECT SUM(TIME_TO_SEC(tp.total_hrs)) / 3600
                        FROM timesheet_project tp
                        WHERE tp.project_id = p.id AND tp.status_id = 3
                    ), 0)
                    -
                    COALESCE((
                        SELECT je.total_hours
                        FROM proposal pr
                        JOIN job_estimation je ON je.id = pr.job_estimation_id
                        WHERE pr.id = p.proposal_id
                        LIMIT 1
                    ), 0), 2
                ) AS hours_difference
            FROM projects p
            LEFT JOIN customers c ON p.client = c.id
            LEFT JOIN m_group grp ON c.cust_group_id = grp.id
            LEFT JOIN m_serviceline sl ON p.service_line_id = sl.id
            LEFT JOIN employees e_incharge ON p.incharge = e_incharge.id
            WHERE p.is_active = 1
              AND p.created_at BETWEEN '{start_date}' AND '{end_date}'
              AND {ownership_sql}
              {customer_filter}
              {sl_filter}
              {incharge_filter}
            ORDER BY hours_difference DESC
            LIMIT {limit}
        """

        rows = await _run_query(query)

        # Summary stats
        over_budget = [r for r in rows if (r.get('hours_difference') or 0) > 0]
        under_budget = [r for r in rows if (r.get('hours_difference') or 0) < 0]
        on_track = [r for r in rows if (r.get('hours_difference') or 0) == 0]

        total_estimated = sum(float(r.get('estimated_hours') or 0) for r in rows)
        total_actual = sum(float(r.get('actual_hours') or 0) for r in rows)
        total_difference = round(total_actual - total_estimated, 2)

        return json.dumps({
            "summary": {
                "total_projects": len(rows),
                "over_budget_projects": len(over_budget),
                "under_budget_projects": len(under_budget),
                "on_track_projects": len(on_track),
                "total_estimated_hours": round(total_estimated, 2),
                "total_actual_hours": round(total_actual, 2),
                "total_hours_difference": total_difference,
                "note": "Positive hours_difference means actual > estimated (over budget). Negative means under budget."
            },
            "projects": rows,
            "date_range": {"start": start_date, "end": end_date}
        }, default=str)
    except Exception as e:
        return f"Error retrieving total estimation report: {str(e)}"


@tool
async def get_project_recoverability_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    customer_name: str = None,
    project_name: str = None,
    service_line: str = None,
    incharge_employee: str = None,
    limit: int = 50,
    employee_id: int = None,
    user_tier: int = None,
) -> str:
    """Useful for answering questions about the Project Recoverability Report, actual recoverability, estimated recoverability, and actual costs vs estimated costs.
    Args:
        start_date: Start of the period.
        end_date: End of the period.
        customer_name: Optional customer name filter.
        project_name: Optional project name filter.
        service_line: Optional service line filter (e.g. 'Audit', 'Tax').
        incharge_employee: Optional project in-charge name.
        limit: Number of records to return (default 50).
        employee_id: The Employee ID of the logged-in user.
        user_tier: The Tier level of the logged-in user (1=SuperAdmin, 2=Management, 3=Partner, etc).
    """
    try:
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        ownership_sql = _build_ownership_sql(employee_id, user_tier, "p", True, "incharge", "service_line_id")

        customer_filter = f" AND c.customer_name LIKE '%{customer_name}%'" if customer_name else ""
        project_filter = f" AND p.name LIKE '%{project_name}%'" if project_name else ""
        sl_filter = f" AND sl.name LIKE '%{service_line}%'" if service_line else ""
        incharge_filter = f" AND e.employee_name LIKE '%{incharge_employee}%'" if incharge_employee else ""

        # PRIMARY PATH: Call the CRM's own /reports/project-recoverability-report API
        # (same endpoint the CRM report page uses, confirmed from browser DevTools).
        # This guarantees 100% data parity with the CRM UI.
        if _CRM_AUTH_TOKEN:
            try:
                import urllib.parse
                safe_start = urllib.parse.quote(str(start_date))
                safe_end = urllib.parse.quote(str(end_date))

                rec_url = (
                    f"{CRM_API_BASE}/reports/project-recoverability-report?"
                    f"page=1&pageSize=10000&statusFilter=-1"
                    f"&start_date={safe_start}&end_date={safe_end}"
                )
                print(f"[AI DEBUG] Recoverability report URL: {rec_url}")
                print(f"[AI DEBUG] Auth token present: {bool(_CRM_AUTH_TOKEN)}, length: {len(_CRM_AUTH_TOKEN) if _CRM_AUTH_TOKEN else 0}")
                if customer_name:
                    rec_url += f"&customerName={urllib.parse.quote(customer_name)}"
                if project_name:
                    rec_url += f"&projectName={urllib.parse.quote(project_name)}"
                if service_line:
                    rec_url += f"&serviceLineFilter={urllib.parse.quote(service_line)}"

                req = urllib.request.Request(rec_url, headers={
                    'Authorization': f'Bearer {_CRM_AUTH_TOKEN}',
                    'Content-Type': 'application/json'
                })

                def _fetch_rec():
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read())

                api_raw = await asyncio.to_thread(_fetch_rec)
                api_data = api_raw.get('data', api_raw)

                # Normalise the rows — the API may return { rows: [...], count: N } or a plain list
                rows = api_data.get("rows", []) if isinstance(api_data, dict) else api_data
                if not isinstance(rows, list): rows = []
                
                # Use the exact logic from the CRM UI
                total_projects = int(api_data.get("count", len(rows))) if isinstance(api_data, dict) else len(rows)

                # The API row shape can vary — normalise field names
                normalised_rows = []
                total_estimated_cost = 0.0
                total_actual_cost = 0.0

                for r in rows:
                    def extract_deep(d, target_keys):
                        if not isinstance(d, dict): return None
                        for k in target_keys:
                            if k in d and d[k]: return d[k]
                        for v in d.values():
                            if isinstance(v, dict):
                                res = extract_deep(v, target_keys)
                                if res: return res
                        return None

                    def extract_service_line(d):
                        val = d.get('service_line') or d.get('serviceLine')
                        if isinstance(val, str): return val
                        if isinstance(val, dict) and val.get('name'): return val.get('name')

                        for k, v in d.items():
                            if isinstance(v, dict):
                                kl = k.lower().replace('_', '')
                                if 'service' in kl and 'line' in kl:
                                    if v.get('name'): return v.get('name')
                        return ""

                    r_customer = extract_deep(r, ['customer_name', 'customerName']) or ""
                    r_service_line = extract_service_line(r) or ""
                    
                    p_detail = r.get('proposal') or {}
                    ic_detail = r.get('inchargeEmployee') or {}
                    r_incharge = r.get('project_in_charge') or r.get('incharge') or r.get('projectInCharge') or ic_detail.get('employee_name') or ""

                    # Apply post-fetch filters since API might ignore them
                    if customer_name and customer_name.lower() not in str(r_customer).lower():
                        continue
                    if project_name and project_name.lower() not in str(r.get('project_name') or r.get('name') or r.get('projectName')).lower():
                        continue
                    if service_line and service_line.lower() not in str(r_service_line).lower():
                        continue
                    if incharge_employee and incharge_employee.lower() not in str(r_incharge).lower():
                        continue

                    est = float(
                        r.get('approved_fees') or p_detail.get('approved_fees') or
                        r.get('estimatedCost') or r.get('estimated_cost') or 
                        r.get('approvedFees') or 0
                    )
                    act = float(
                        r.get('actualCost') or r.get('actual_cost') or
                        r.get('totalActualCost') or r.get('total_actual_cost') or 0
                    )
                    est_rec = float(
                        r.get('recoverability') or p_detail.get('recoverability') or 
                        r.get('estimatedRecoverability') or r.get('estimated_recoverability') or 0
                    )
                    act_rec = round((est / act) * 100, 3) if act > 0 else 0.0

                    total_estimated_cost += est
                    total_actual_cost += act

                    # Extract the missing fields
                    r_customer_group = extract_deep(r, ['customerGroup', 'groupName']) or ""
                    if isinstance(r_customer_group, dict): r_customer_group = r_customer_group.get('name') or ""
                    
                    r_start_date = r.get('start_date') or r.get('startDate') or ""
                    if r_start_date and 'T' in str(r_start_date): r_start_date = str(r_start_date).split('T')[0]
                    
                    r_end_date = r.get('end_date') or r.get('endDate') or ""
                    if r_end_date and 'T' in str(r_end_date): r_end_date = str(r_end_date).split('T')[0]
                    
                    # Project Partner
                    r_partner = extract_deep(r, ['partnerInventory', 'projectPartner', 'partner']) or ""
                    if isinstance(r_partner, dict): r_partner = r_partner.get('employee_name') or r_partner.get('name') or ""
                    elif isinstance(r_partner, int): r_partner = str(r_partner) # fallback if ID
                    
                    # Customer Relation
                    r_cust_relation = extract_deep(r, ['clientRelation', 'customerRelation', 'client_relation']) or ""
                    if isinstance(r_cust_relation, dict): r_cust_relation = r_cust_relation.get('employee_name') or r_cust_relation.get('name') or ""
                    
                    # Project Status
                    r_status = extract_deep(r, ['projectStatus', 'status']) or ""
                    if isinstance(r_status, dict): r_status = r_status.get('name') or ""
                    elif r.get('statusName'): r_status = r.get('statusName')
                    elif r.get('project_status_name'): r_status = r.get('project_status_name')
                    
                    r_approved_fees = float(r.get('approved_fees') or p_detail.get('approved_fees') or r.get('approvedFees') or 0)
                    r_agreed_fees = float(r.get('agreed_fees') or p_detail.get('agreed_fees') or r.get('agreedFees') or 0)

                    normalised_rows.append({
                        "project_code": r.get('project_code') or r.get('code') or r.get('projectCode'),
                        "project_name": r.get('project_name') or r.get('name') or r.get('projectName'),
                        "customer_name": r_customer,
                        "customer_group": r_customer_group,
                        "service_line": r_service_line,
                        "start_date": r_start_date,
                        "end_date": r_end_date,
                        "project_in_charge": r_incharge,
                        "customer_relation": r_cust_relation,
                        "project_partner": r_partner,
                        "project_status": r_status,
                        "approved_fees": r_approved_fees,
                        "agreed_fees": r_agreed_fees,
                        "estimated_cost": est,
                        "estimated_recoverability": est_rec,
                        "total_actual_cost": act,
                        "actual_recoverability": act_rec,
                    })

                total_projects = len(normalised_rows)

                valid_recoverabilities = [r['actual_recoverability'] for r in normalised_rows if r.get('actual_recoverability', 0) > 0]
                total_actual_recoverability = (
                    round(sum(valid_recoverabilities) / len(valid_recoverabilities), 3)
                    if valid_recoverabilities else 0.0
                )

                if total_projects == 0:
                    return json.dumps({
                        "summary": {
                            "total_projects": 0,
                            "message": "CRITICAL: NO PROJECTS EXIST FOR THIS DATE RANGE. You MUST tell the user exactly that 0 projects matched the criteria. Do NOT invent, hallucinate, or generate fake project names (like PRJ001, Project Alpha, etc)."
                        },
                        "projects": [],
                        "date_range": {"start": start_date, "end": end_date}
                    })

                return json.dumps({
                    "summary": {
                        "total_projects": total_projects,
                        "total_estimated_cost": round(total_estimated_cost, 2),
                        "total_actual_cost": round(total_actual_cost, 2),
                        "total_actual_recoverability_percentage": total_actual_recoverability,
                        "note": "Recoverability = Average of Actual Recoverability for projects for logged work hours. ",
                        "source": "CRM_API_RECOVERABILITY_REPORT"
                    },
                    "projects": normalised_rows,
                    "date_range": {"start": start_date, "end": end_date}
                }, default=str)

            except urllib.error.HTTPError as he:
                err_body = he.read().decode('utf-8', errors='ignore')
                print(f"[AI] Recoverability API HTTP Error {he.code}: {err_body}")
            except Exception as api_err:
                print(f"[AI] Recoverability API failed, falling back to SQL: {api_err}")

        # FALLBACK: SQL query if the CRM API is unavailable
        query = f"""
            SELECT 
                p.code AS project_code,
                p.name AS project_name,
                c.customer_name,
                grp.name AS customer_group,
                sl.name AS service_line,
                p.start_date,
                p.end_date,
                e.employee_name AS project_in_charge,
                cr.employee_name AS customer_relation,
                pp.employee_name AS project_partner,
                ps.name AS project_status,
                COALESCE(pr.approved_fees, p.approved_fees, 0) AS approved_fees,
                COALESCE(pr.agreed_fees, 0) AS agreed_fees,
                COALESCE(pr.approved_fees, p.approved_fees, 0) AS estimated_cost,
                COALESCE(pr.recoverability, 0) AS estimated_recoverability,
                COALESCE(SUM(TIME_TO_SEC(tp.total_hrs)/3600 * dr.hourly), 0) AS total_actual_cost
            FROM projects p
            LEFT JOIN customers c ON p.client = c.id
            LEFT JOIN m_group grp ON c.cust_group_id = grp.id
            LEFT JOIN m_serviceline sl ON p.service_line_id = sl.id
            LEFT JOIN employees e ON p.incharge = e.id
            LEFT JOIN employees cr ON c.cust_client_rel_id = cr.id
            LEFT JOIN employees pp ON p.partner = pp.id
            LEFT JOIN m_status ps ON p.status_id = ps.id
            LEFT JOIN proposal pr ON p.id = (
                SELECT id FROM proposal WHERE project_id = p.id ORDER BY id DESC LIMIT 1
            )
            LEFT JOIN timesheet_project tp ON p.id = tp.project_id
            LEFT JOIN employees te ON tp.employee_id = te.id
            LEFT JOIN m_designation_rates dr ON te.emp_designation_id = dr.designation_id
            WHERE p.is_active = 1
              AND p.created_at BETWEEN '{start_date}' AND '{end_date}'
              AND {ownership_sql}
              {customer_filter}
              {project_filter}
              {sl_filter}
              {incharge_filter}
            GROUP BY p.code, p.name, c.customer_name, grp.name, sl.name, p.start_date, p.end_date, e.employee_name, cr.employee_name, pp.employee_name, ps.name, pr.approved_fees, p.approved_fees, pr.agreed_fees, pr.recoverability
            ORDER BY total_actual_cost DESC
            LIMIT {limit}
        """

        rows = await _run_query(query)

        # Compute actual recoverability for each row and aggregates
        total_estimated_cost = 0.0
        total_actual_cost = 0.0

        for r in rows:
            est_cost = float(r.get('estimated_cost') or 0)
            act_cost = float(r.get('total_actual_cost') or 0)
            total_estimated_cost += est_cost
            total_actual_cost += act_cost
            
            if act_cost > 0:
                r['actual_recoverability'] = round((est_cost / act_cost) * 100, 3)
            else:
                r['actual_recoverability'] = 0.0

        valid_recoverabilities = [r['actual_recoverability'] for r in rows if r.get('actual_recoverability', 0) > 0]
        if valid_recoverabilities:
            total_actual_recoverability = round(sum(valid_recoverabilities) / len(valid_recoverabilities), 3)
        else:
            total_actual_recoverability = 0.0

        if len(rows) == 0:
            return json.dumps({
                "summary": {
                    "total_projects": 0,
                    "message": "CRITICAL: NO PROJECTS EXIST FOR THIS DATE RANGE. You MUST tell the user exactly that 0 projects matched the criteria. Do NOT invent, hallucinate, or generate fake project names (like PRJ001, Project Alpha, etc)."
                },
                "projects": [],
                "date_range": {"start": start_date, "end": end_date}
            })

        return json.dumps({
            "summary": {
                "total_projects": len(rows),
                "total_estimated_cost": round(total_estimated_cost, 2),
                "total_actual_cost": round(total_actual_cost, 2),
                "total_actual_recoverability_percentage": total_actual_recoverability,
                "note": "Recoverability = Average of Actual Recoverability for projects with actual cost > 0 (SQL fallback)"
            },
            "projects": rows,
            "date_range": {"start": start_date, "end": end_date}
        }, default=str)
    except Exception as e:
        return f"Error retrieving project recoverability report: {str(e)}"

# ---------------------------------------------------------------------------
from typing import Any

def _nullish(value: Any) -> bool:
    """Return True if value is None, empty string, 'null', 'none', or 'undefined'."""
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip().lower()
        return v in ("", "null", "none", "undefined")
    return False


def _coerce_optional_date(value) -> "Optional[str]":
    """Return None for nullish values, else return value unchanged."""
    return None if _nullish(value) else value


def _coerce_optional_int(value) -> "Optional[int]":
    """Coerce value to int, returning None for nullish or unconvertible values."""
    if _nullish(value):
        return None
    try:
        return int(value)
    except Exception:
        return None

@tool
async def get_staff_billing_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_name: str = None,
    customer_name: str = None,
    service_line: str = None,
    project_partner: str = None,
    limit: int = 10000,
    employee_id: int = None,
    user_tier: int = None,
) -> str:
    """Useful for answering questions about the Staff Billing Report, staff cost, approved fees, and total invoiced.
    Args:
        start_date: Start of the period.
        end_date: End of the period.
        employee_name: Optional employee name filter.
        customer_name: Optional customer name filter.
        service_line: Optional service line filter (e.g. 'Audit', 'Tax').
        project_partner: Optional project partner name.
        limit: Number of records to return (default 50).
        employee_id: The Employee ID of the logged-in user.
        user_tier: The Tier level of the logged-in user (1=SuperAdmin, 2=Management, 3=Partner, etc).
    """
    try:
        employee_id, user_tier = _resolve_rbac_params(employee_id, user_tier)
        fy_info = get_fiscal_info()
        start_date = start_date or fy_info['fy_start']
        end_date = end_date or fy_info['fy_end']

        # Call the CRM's own /reports/staff-billing-report API
        if _CRM_AUTH_TOKEN:
            try:
                import urllib.parse
                safe_start = urllib.parse.quote(str(start_date))
                safe_end = urllib.parse.quote(str(end_date))

                rec_url = (
                    f"{CRM_API_BASE}/reports/revenue-billing-report?"
                    f"page=1&pageSize=10000"
                    f"&start_date={safe_start}&end_date={safe_end}"
                )
                
                req = urllib.request.Request(rec_url, headers={
                    'Authorization': f'Bearer {_CRM_AUTH_TOKEN}',
                    'Content-Type': 'application/json'
                })

                def _fetch_rec():
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read())

                api_raw = await asyncio.to_thread(_fetch_rec)
                api_data = api_raw.get('data', api_raw)

                rows = api_data.get("rows", []) if isinstance(api_data, dict) else api_data
                if not isinstance(rows, list): rows = []
                
                def _nested_get(d, key_path):
                    for k in key_path.split('.'):
                        if isinstance(d, dict):
                            d = d.get(k)
                        else:
                            return None
                    return d

                # Apply Python-level filtering for natural language queries
                if employee_name:
                    rows = [r for r in rows if employee_name.lower() in str(_nested_get(r, 'employees.employee_name') or '').lower()]
                if customer_name:
                    rows = [r for r in rows if customer_name.lower() in str(_nested_get(r, 'client.customer_name') or '').lower()]
                if service_line:
                    rows = [r for r in rows if service_line.lower() in str(_nested_get(r, 'project.serviceLine.name') or '').lower()]
                if project_partner:
                    rows = [r for r in rows if project_partner.lower() in str(_nested_get(r, 'project.partnerInventory.employee_name') or '').lower()]

                if limit and len(rows) > limit:
                    rows = rows[:limit]

                total_staff_cost = sum(float(_nested_get(r, 'staff_cost') or 0) for r in rows)
                total_approved_fees = sum(float(_nested_get(r, 'project.approved_fees') or 0) for r in rows)
                total_invoiced = sum(float(_nested_get(r, 'invoice_amount') or 0) for r in rows)

                if len(rows) == 0:
                    return json.dumps({
                        "summary": {
                            "total_projects": 0,
                            "message": "CRITICAL: NO PROJECTS EXIST FOR THIS DATE RANGE. You MUST tell the user exactly that 0 projects matched the criteria. Do NOT invent, hallucinate, or generate fake project names (like PRJ001, Project Alpha, etc)."
                        },
                        "projects": [],
                        "date_range": {"start": start_date, "end": end_date}
                    })

                return json.dumps({
                    "summary": {
                        "total_projects": len(rows),
                        "total_staff_cost": round(total_staff_cost, 2),
                        "total_approved_fees": round(total_approved_fees, 2),
                        "total_invoiced": round(total_invoiced, 2),
                        "note": "Metrics pulled securely from Staff Billing Report."
                    },
                    "projects": rows,
                    "date_range": {"start": start_date, "end": end_date}
                }, default=str)
            except Exception as e:
                print(f"[AI WARN] API /staff-billing-report failed: {e}")
                # Fallback to empty string to trigger SQL or error
                pass
        
        return json.dumps({"error": "Unable to connect to CRM API for Staff Billing."})

    except Exception as e:
        return f"Error retrieving staff billing report: {str(e)}"

# A list of all semantic tools to easily bind to the LangChain agent
ALL_SEMANTIC_TOOLS = [
    get_revenue_metrics,
    get_receivables_metrics,
    get_pipeline_and_proposals,
    get_active_projects_metrics,
    get_high_value_proposals,
    get_comprehensive_customer_report,
    get_total_estimation_report,
    get_project_recoverability_report,
    get_staff_billing_report,
]
