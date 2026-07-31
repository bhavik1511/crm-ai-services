"""
tools_new.py — New MCP server-side tool functions for the CRM AI Agent.

Contains 6 tools registered via mcp_server.py:
  1. get_dashboard_snapshot   — concurrent KPI snapshot from all semantic layer metrics
  2. get_anomaly_alerts       — proactive business anomaly scanner
  3. compare_periods          — metric comparison across two date ranges + chart data
  4. get_entity_profile       — 360-degree profile for customer / project / employee
  5. submit_feedback          — user thumbs-up/down on AI answers (writes to MongoDB)
  6. handle_edit_intent       — returns current CRM data for RBAC-gated edit confirmation

All functions are async and follow the existing semantic_layer / _run_query patterns.
They do NOT mutate any database — they are strictly read + MongoDB-write for feedback only.
"""

import asyncio
import json
from typing import Optional
from datetime import datetime

from semantic.semantic_layer import (
    get_revenue_metrics,
    get_receivables_metrics,
    get_pipeline_and_proposals,
    get_active_projects_metrics,
    get_comprehensive_customer_report,
    get_total_estimation_report,
    _resolve_rbac_params,
    _build_ownership_sql,
    _run_query,
    get_fiscal_info,
)
from db.database_mongo import get_vector_cache_collection


# ---------------------------------------------------------------------------
# Tool 1: get_dashboard_snapshot
# ---------------------------------------------------------------------------
async def get_dashboard_snapshot(user_id: int, role: str) -> dict:
    """
    Returns a complete role-scoped KPI snapshot in one call.
    Calls all four semantic layer metric tools concurrently.
    Used by the MCP server tool of the same name.
    """
    try:
        fy = get_fiscal_info()

        revenue_raw, receivables_raw, pipeline_raw, projects_raw = await asyncio.gather(
            get_revenue_metrics.ainvoke({}),
            get_receivables_metrics.ainvoke({}),
            get_pipeline_and_proposals.ainvoke({}),
            get_active_projects_metrics.ainvoke({}),
        )

        revenue = json.loads(revenue_raw) if isinstance(revenue_raw, str) else revenue_raw
        receivables = json.loads(receivables_raw) if isinstance(receivables_raw, str) else receivables_raw
        pipeline = json.loads(pipeline_raw) if isinstance(pipeline_raw, str) else pipeline_raw
        projects = json.loads(projects_raw) if isinstance(projects_raw, str) else projects_raw

        return {
            "fiscal_year": f"{fy['fy_start']} to {fy['fy_end']}",
            "revenue": {
                "total_ytd": revenue.get("total_revenue_ytd", 0),
                "current_team_billing": revenue.get("current_team_billing_period_total", 0),
                "by_month": revenue.get("revenue_by_month", []),
            },
            "receivables": {
                "total_outstanding": receivables.get("total_receivables", 0),
                "ageing_buckets": receivables.get("ageing_buckets", []),
            },
            "pipeline": {
                "open_leads": pipeline.get("service_pipeline_leads_summary", {}),
                "open_proposals": pipeline.get("open_proposals", {}),
                "win_rate": pipeline.get("proposal_win_rate", 0),
            },
            "projects": {
                "total": projects.get("total_projects", 0),
                "overdue_tasks": projects.get("overdue_tasks", 0),
                "completion_pct": projects.get("overall_completion_percentage", "0"),
            },
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tool 2: get_anomaly_alerts
# ---------------------------------------------------------------------------
async def get_anomaly_alerts(user_id: int, role: str) -> list:
    """
    Proactively scans for business anomalies.
    Returns a list of alert dicts with: severity (high/medium/low), category, message, count, action.
    Severity: high = requires immediate attention, medium = review soon, low = informational.
    """
    employee_id, user_tier = _resolve_rbac_params(user_id, None)
    ownership_sql = _build_ownership_sql(employee_id, user_tier, "i", True)

    alerts = []

    try:
        # 1. Invoices overdue > 90 days
        overdue_q = f"""
            SELECT COUNT(*) as cnt, ROUND(SUM(
                i.total_net_amount
                - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
                - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)
            ), 2) as total_amount
            FROM invoice i
            WHERE i.is_active = 1
              AND i.payment_status_id NOT IN (2, 4)
              AND DATEDIFF(CURDATE(), i.created_at) > 90
              AND {ownership_sql}
        """
        overdue = await _run_query(overdue_q)
        if overdue and overdue[0].get('cnt', 0) > 0:
            alerts.append({
                "severity": "high",
                "category": "receivables",
                "message": f"{overdue[0]['cnt']} invoice(s) overdue by more than 90 days",
                "amount": overdue[0].get('total_amount', 0),
                "action": "Review overdue receivables",
                "navigate_to": "/billing/reports/receivable-report"
            })

        # 2. Proposals stalled (open and not updated in 30+ days)
        stalled_q = """
            SELECT COUNT(*) as cnt
            FROM proposal p
            WHERE p.is_active = 1
              AND p.proposal_status_id IN (1, 7, 8)
              AND p.project_id IS NULL
              AND DATEDIFF(CURDATE(), p.updated_at) > 30
        """
        stalled = await _run_query(stalled_q)
        if stalled and stalled[0].get('cnt', 0) > 0:
            alerts.append({
                "severity": "medium",
                "category": "pipeline",
                "message": f"{stalled[0]['cnt']} open proposal(s) have not been updated in 30+ days",
                "action": "Follow up on stalled proposals",
                "navigate_to": "/proposal"
            })

        # 3. Overdue project tasks
        tasks_q = """
            SELECT COUNT(*) as cnt
            FROM project_tasks pt
            WHERE pt.status != 'Finished'
              AND pt.due_date < CURDATE()
        """
        overdue_tasks = await _run_query(tasks_q)
        if overdue_tasks and overdue_tasks[0].get('cnt', 0) > 0:
            alerts.append({
                "severity": "medium",
                "category": "projects",
                "message": f"{overdue_tasks[0]['cnt']} project task(s) are overdue",
                "action": "Review overdue tasks",
                "navigate_to": "/projects-list"
            })

        # 4. Leads with no follow-up activity (open > 14 days)
        cold_leads_q = """
            SELECT COUNT(*) as cnt
            FROM saleslead sl
            JOIN m_leadstatus ls ON sl.lead_status_id = ls.id
            WHERE ls.name = 'Open'
              AND DATEDIFF(CURDATE(), sl.lead_date) > 14
        """
        cold = await _run_query(cold_leads_q)
        if cold and cold[0].get('cnt', 0) > 0:
            alerts.append({
                "severity": "low",
                "category": "leads",
                "message": f"{cold[0]['cnt']} open lead(s) have been inactive for 14+ days",
                "action": "Review cold leads",
                "navigate_to": "/service-lead"
            })

    except Exception as e:
        alerts.append({"severity": "low", "category": "system", "message": f"Alert scan error: {str(e)}"})

    # Sort: high → medium → low
    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda x: severity_order.get(x.get("severity", "low"), 2))
    return alerts


# ---------------------------------------------------------------------------
# Tool 3: compare_periods
# ---------------------------------------------------------------------------
async def compare_periods(
    metric: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    user_id: int,
    role: str,
) -> dict:
    """
    Compares a metric between two date ranges.
    metric: one of 'revenue', 'receivables', 'proposals', 'leads'
    Returns: { period_a: value, period_b: value, delta: value, delta_pct: float, chart_data: dict }
    """
    employee_id, user_tier = _resolve_rbac_params(user_id, None)
    ownership_sql = _build_ownership_sql(employee_id, user_tier, "i", True)

    async def _get_metric(start: str, end: str) -> float:
        if metric == "revenue":
            q = f"SELECT ROUND(COALESCE(SUM(total_amt_ex_vat), 0), 2) AS val FROM invoice i WHERE is_active = 1 AND created_at BETWEEN '{start}' AND '{end}' AND {ownership_sql}"
        elif metric == "receivables":
            q = f"""SELECT ROUND(COALESCE(SUM(
                i.total_net_amount
                - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
                - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)
            ), 0), 2) AS val FROM invoice i WHERE i.is_active = 1 AND i.payment_status_id NOT IN (2,4) AND i.created_at BETWEEN '{start}' AND '{end}' AND {ownership_sql}"""
        elif metric == "proposals":
            q = f"SELECT COUNT(*) AS val FROM proposal WHERE is_active = 1 AND created_at BETWEEN '{start}' AND '{end}'"
        elif metric == "leads":
            q = f"SELECT COUNT(*) AS val FROM saleslead WHERE lead_date BETWEEN '{start}' AND '{end}'"
        else:
            return 0.0
        rows = await _run_query(q)
        return float(rows[0]['val']) if rows and rows[0]['val'] is not None else 0.0

    val_a, val_b = await asyncio.gather(
        _get_metric(period_a_start, period_a_end),
        _get_metric(period_b_start, period_b_end),
    )

    delta = round(val_b - val_a, 2)
    delta_pct = round((delta / val_a * 100) if val_a != 0 else 0, 2)

    return {
        "metric": metric,
        "period_a": {"start": period_a_start, "end": period_a_end, "value": val_a},
        "period_b": {"start": period_b_start, "end": period_b_end, "value": val_b},
        "delta": delta,
        "delta_pct": delta_pct,
        "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
        "chart_data": {
            "type": "bar",
            "labels": [f"{period_a_start[:7]}", f"{period_b_start[:7]}"],
            "datasets": [{"label": metric.title(), "data": [val_a, val_b]}],
        }
    }


# ---------------------------------------------------------------------------
# Tool 4: get_entity_profile
# ---------------------------------------------------------------------------
async def get_entity_profile(entity_type: str, entity_name: str, user_id: int, role: str) -> dict:
    """
    Fetch a rich profile for an entity.
    entity_type: 'customer' | 'project' | 'employee'
    entity_name: the search term (name, code, or ID)
    Returns a structured profile dict with all related data.
    """
    employee_id, user_tier = _resolve_rbac_params(user_id, None)

    if entity_type == "customer":
        raw = await get_comprehensive_customer_report.ainvoke({"search_term": entity_name})
        return json.loads(raw) if isinstance(raw, str) else raw

    elif entity_type == "project":
        q = f"""
            SELECT p.id, p.name, p.code, p.start_date, p.end_date, p.approved_fees,
                   ps.name AS status, sl.name AS service_line,
                   CONCAT(e.first_name, ' ', e.last_name) AS incharge_name
            FROM projects p
            LEFT JOIN m_project_status ps ON p.status_id = ps.id
            LEFT JOIN m_serviceline sl ON p.service_line_id = sl.id
            LEFT JOIN employees e ON p.main_incharge = e.id
            WHERE p.is_active = 1
              AND (p.name LIKE '%{entity_name}%' OR p.code LIKE '%{entity_name}%')
            LIMIT 1
        """
        project = await _run_query(q)
        if not project:
            return {"error": f"No project found matching '{entity_name}'"}

        proj = project[0]
        proj_id = proj['id']

        tasks_q = "SELECT status, COUNT(*) as count FROM project_tasks WHERE project_id = :proj_id GROUP BY status"
        invoices_q = "SELECT invoice_no, total_amt_ex_vat, payment_status_id FROM invoice WHERE project_id = :proj_id AND is_active = 1"
        tasks, invoices = await asyncio.gather(_run_query(tasks_q, {"proj_id": proj_id}), _run_query(invoices_q, {"proj_id": proj_id}))

        return {
            "project": proj,
            "tasks_summary": tasks,
            "invoices": invoices,
        }

    elif entity_type == "employee":
        q = f"""
            SELECT e.id, e.first_name, e.last_name, e.emp_email,
                   d.name AS designation, dep.name AS department
            FROM employees e
            LEFT JOIN m_designation d ON e.emp_designation_id = d.id
            LEFT JOIN m_department dep ON e.emp_department_id = dep.id
            WHERE e.is_active = 1
              AND (CONCAT(e.first_name, ' ', e.last_name) LIKE '%{entity_name}%'
                   OR e.emp_email LIKE '%{entity_name}%')
            LIMIT 1
        """
        emp = await _run_query(q)
        if not emp:
            return {"error": f"No employee found matching '{entity_name}'"}

        # Tier check: block salary/HR data for lower-tier users
        if user_tier and user_tier >= 5:
            return {"employee": emp[0], "note": "Detailed HR data restricted to your access level."}

        return {"employee": emp[0]}

    return {"error": f"Unknown entity_type: {entity_type}. Use 'customer', 'project', or 'employee'."}


# ---------------------------------------------------------------------------
# Tool 5: submit_feedback
# ---------------------------------------------------------------------------
async def submit_feedback(
    question: str,
    feedback: str,   # "positive" or "negative"
    user_id: int,
    role: str,
    comment: Optional[str] = None,
) -> dict:
    """
    Store user feedback on an AI answer.
    Negative feedback marks the cache entry for LLM-retry on next similar query.
    feedback: 'positive' or 'negative'
    """
    try:
        col = get_vector_cache_collection()

        # Find the most recent cache entry for this question + role
        entry = await col.find_one(
            {"question": {"$regex": question[:60], "$options": "i"}, "role_scope": role},
            sort=[("created_at", -1)]
        )

        feedback_doc = {
            "user_id": user_id,
            "feedback": feedback,
            "comment": comment,
            "timestamp": datetime.utcnow(),
        }

        update_fields = {
            "$push": {"feedback_history": feedback_doc},
        }

        if feedback == "negative":
            # Mark entry as low-quality: next similar query will bypass cache
            update_fields["$set"] = {
                "quality_flag": "negative",
                "bypass_cache": True,
            }
        else:
            update_fields["$set"] = {"quality_flag": "positive"}

        if entry:
            await col.update_one({"_id": entry["_id"]}, update_fields)

        # Also log to a feedback collection for analytics
        from db.database_mongo import get_mongo_db
        db = get_mongo_db()
        if db is not None:
            await db["ai_feedback_log"].insert_one({
                "question": question,
                "feedback": feedback,
                "comment": comment,
                "user_id": user_id,
                "role": role,
                "timestamp": datetime.utcnow(),
            })

        return {"status": "ok", "message": "Thank you for your feedback."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Tool 6: handle_edit_intent
# ---------------------------------------------------------------------------
async def handle_edit_intent(
    entity_type: str,
    entity_name: str,
    user_id: int,
    role: str,
) -> dict:
    """
    Called when the agent detects is_edit_intent=True.
    Returns a structured form payload for the frontend to show a confirmation dialog.
    Does NOT write any data — that's handled by the CRM backend via the existing API.
    """
    from config.role_tier_config import get_tier_for_role
    tier = get_tier_for_role(role)

    # Block write intent for tier 7+ (view-only staff)
    if tier >= 7:
        return {
            "allowed": False,
            "message": f"Your role ({role}) does not have permission to edit CRM records via the AI assistant."
        }

    profile = await get_entity_profile(entity_type, entity_name, user_id, role)

    return {
        "allowed": True,
        "edit_intent": True,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "current_data": profile,
        "message": f"Here is the current data for {entity_name}. Please use the CRM form to make changes, or confirm which field you want to update.",
        "action": "show_edit_confirmation",
    }
