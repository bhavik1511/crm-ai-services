"""
semantic_wrappers.py — Thin wrappers around existing Semantic Layer tools.
We wrap them here to expose them to the new AI Orchestrator without modifying the original semantic_layer.py.
"""
import inspect
import json
import logging
from typing import Dict, Any, Optional, cast, Callable, Awaitable
from agent.secure_log_sanitizer import sanitize_for_log

# We import the existing LangChain tools.
# Because they are LangChain @tool objects in semantic_layer.py, we must use .ainvoke()
from semantic.semantic_layer import (
    get_revenue_metrics,
    get_receivables_metrics,
    get_pipeline_and_proposals,
    get_active_projects_metrics,
    get_comprehensive_customer_report,
    get_project_recoverability_report,
    get_staff_billing_report,
    get_job_estimation_metrics,
    get_kpi_summary_report
)

logger = logging.getLogger(__name__)


def _clean_args(args: Dict[str, Any], target_func=None) -> Dict[str, Any]:
    """Strip aggregate sentinels and unknown metadata fields not accepted by target function signature."""
    if not isinstance(args, dict):
        return args
    cleaned = {}
    
    # Non-parameter metadata fields populated by Planner/ToolRegistry that should not be passed to SQL/wrappers
    RESERVED_PLANNER_KEYS = {"scope", "business_goal", "intent", "metric", "ranking", "comparison", "aggregation", "sort_order", "group_by", "time_filter"}
    
    allowed_params = None
    has_var_keyword = False
    if target_func:
        try:
            fn_to_inspect = getattr(target_func, 'coroutine', None) or getattr(target_func, 'func', None) or target_func
            sig = inspect.signature(fn_to_inspect)
            allowed_params = set(sig.parameters.keys())
            has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        except Exception:
            allowed_params = None

    for k, v in args.items():
        if k in RESERVED_PLANNER_KEYS and allowed_params is not None and k not in allowed_params and not has_var_keyword:
            continue
        if isinstance(v, str) and v.strip().upper() in ("__ALL__", "ALL", "NONE", "NULL", "ANY", "*", ""):
            cleaned[k] = None
        else:
            if allowed_params is not None and not has_var_keyword:
                if k in allowed_params:
                    cleaned[k] = v
            else:
                cleaned[k] = v
    return cleaned


async def _invoke_tool(tool_obj: Any, args: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Generic helper to execute LangChain tool or async coroutine cleanly with error handling."""
    cleaned_args = _clean_args(args, tool_obj)
    logger.info(f"Calling {name} with args: {sanitize_for_log(cleaned_args)}")
    try:
        fn = getattr(tool_obj, 'coroutine', None) or tool_obj.ainvoke
        async_fn = cast(Callable[..., Awaitable[Any]], fn)
        if callable(fn) and fn != tool_obj.ainvoke:
            result_str = await async_fn(**cleaned_args)
        else:
            result_str = await async_fn(cleaned_args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in {name}: {e}")
        return {"error": str(e)}


async def call_revenue_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_revenue_metrics."""
    sl_id = args.get("service_line_id")
    sl_name = args.get("service_line")
    if sl_id or sl_name:
        logger.info(f"[REVENUE_ENTITY_FILTER] service_line_id={sl_id} service_line=\"{sl_name}\"")
    return await _invoke_tool(get_revenue_metrics, args, "get_revenue_metrics")


async def call_receivables_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_receivables_metrics."""
    return await _invoke_tool(get_receivables_metrics, args, "get_receivables_metrics")


async def call_pipeline_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_pipeline_and_proposals."""
    return await _invoke_tool(get_pipeline_and_proposals, args, "get_pipeline_and_proposals")


async def call_project_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_active_projects_metrics."""
    return await _invoke_tool(get_active_projects_metrics, args, "get_active_projects_metrics")


async def call_customer_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_comprehensive_customer_report."""
    cleaned = _clean_args(args, get_comprehensive_customer_report)
    search_term = cleaned.get("search_term") or cleaned.get("customer_name") or cleaned.get("customer") or cleaned.get("question", "")
    return await _invoke_tool(get_comprehensive_customer_report, {"search_term": str(search_term)}, "get_comprehensive_customer_report")


async def call_project_recoverability_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_project_recoverability_report."""
    return await _invoke_tool(get_project_recoverability_report, args, "get_project_recoverability_report")


async def call_staff_billing_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_staff_billing_report."""
    return await _invoke_tool(get_staff_billing_report, args, "get_staff_billing_report")


async def call_job_estimation_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_job_estimation_metrics."""
    return await _invoke_tool(get_job_estimation_metrics, args, "get_job_estimation_metrics")


async def call_kpi_summary_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_kpi_summary_report."""
    emp_id = args.get("employee_id") or args.get("emp_id") or args.get("resolved_id")
    emp_name = args.get("employee_name") or args.get("resolved_name") or args.get("employee")
    logger.info(
        f"[KPI_ENTITY_FILTER] capability=kpi_summary dimension=employee "
        f"employee_id={emp_id} employee_name=\"{emp_name}\""
    )
    if emp_id and not args.get("employee_id"):
        args["employee_id"] = emp_id
    if emp_name and not args.get("employee_name"):
        args["employee_name"] = emp_name
    return await _invoke_tool(get_kpi_summary_report, args, "get_kpi_summary_report")


async def call_analytical_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Authoritative wrapper for analytical query capability.
    Consumes structured execution parameters from ToolRegistry / ExecutionContract.
    Completely decoupled from legacy ad_hoc_sql_query and query_parser.
    """
    logger.info(f"[ANALYTICAL_QUERY_WRAPPER] Received args (keys): {list(args.keys()) if isinstance(args, dict) else type(args)}")
    op = str(args.get("operation") or args.get("intent") or "").lower().strip()
    dim = args.get("dimension")
    
    if op == "ranking" or dim or args.get("limit") or args.get("ranking"):
        logger.info("[ANALYTICAL_QUERY] Dispatching ranking query to call_authoritative_ranking_query")
        return await call_authoritative_ranking_query(args)
    elif op == "comparison" or len(args.get("comparison_periods") or []) >= 2:
        logger.info("[ANALYTICAL_QUERY] Dispatching comparison query to call_authoritative_comparison_query")
        return await call_authoritative_comparison_query(args)
    else:
        logger.info("[ANALYTICAL_QUERY] Dispatching default analytical query to call_revenue_metrics")
        return await call_revenue_metrics(args)


async def call_ui_navigation(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for handling UI navigation commands from Planner."""
    logger.info(f"Handling ui_navigation with args: {args}")
    return {
        "action": "navigate",
        "target": args.get("target_dashboard")
    }


async def call_authoritative_ranking_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes authoritative ranking via Node.js CRM REST API /reports/revenue-billing-report.
    Aggregates billing rows by requested dimension ('customer', 'department', 'service_line', 'employee')
    and metric ('revenue' or 'gross_profit').
    Zero Python SQL, 100% backend-authoritative REST execution.
    """
    import asyncio, urllib.request, urllib.parse, os, json
    CRM_API_BASE = os.getenv('CRM_API_BASE', 'http://localhost:3001/api/v1').rstrip('/')
    _CRM_AUTH_TOKEN = os.getenv('CRM_AUTH_TOKEN', '')

    dim = (args.get("dimension") or "customer").lower().strip()
    metric = (args.get("metric") or "revenue").lower().strip()
    limit = int(args.get("limit") or 1)
    sort_order = str(args.get("sort_order") or "desc").lower()

    if "count" in metric or metric == "pending_invoice_count":
        metric_type = "count"
        returned_metric = "pending_invoice_count"
        metric_label = "Pending Invoice Count"
    else:
        metric_type = "monetary"
        returned_metric = "pending_receivables_amount" if ("receivable" in metric or "invoice" in metric) else metric
        metric_label = "Pending Receivables Amount" if ("receivable" in metric or "invoice" in metric) else metric.replace("_", " ").title()

    start_date = args.get("start_date") or ""
    end_date = args.get("end_date") or ""

    if not start_date or not end_date:
        from agent.temporal_resolver import resolve_temporal_scope
        t_res = resolve_temporal_scope(args.get("question", "") or "this year")
        start_date = start_date or t_res.get("start_date")
        end_date = end_date or t_res.get("end_date")

    safe_start = urllib.parse.quote(str(start_date))
    safe_end = urllib.parse.quote(str(end_date))

    rec_url = f"{CRM_API_BASE}/reports/revenue-billing-report?page=1&pageSize=10000&start_date={safe_start}&end_date={safe_end}"
    headers = {'Content-Type': 'application/json'}
    passed_jwt = args.get("jwt_token") or _CRM_AUTH_TOKEN
    if passed_jwt:
        auth_hdr = passed_jwt if str(passed_jwt).startswith("Bearer ") else f"Bearer {passed_jwt}"
        headers['Authorization'] = auth_hdr

    req = urllib.request.Request(rec_url, headers=headers)

    def _fetch():
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as err:
            logger.error(f"[RANKING_API_ERROR] Failed fetching revenue billing report: {err}")
            return {"rows": []}

    api_raw = await asyncio.to_thread(_fetch)
    api_data = api_raw.get('data', api_raw) if isinstance(api_raw, dict) else api_raw
    rows = api_data.get("rows", []) if isinstance(api_data, dict) else (api_data if isinstance(api_data, list) else [])

    def _get_val(row, *candidate_keys):
        if not isinstance(row, dict):
            return None
        for key in candidate_keys:
            if key in row and row[key] not in (None, "", "null", "N/A", "Unknown"):
                return row[key]
            if '.' in key:
                d = row
                found = True
                for part in key.split('.'):
                    if isinstance(d, dict) and part in d:
                        d = d[part]
                    else:
                        found = False
                        break
                if found and d not in (None, "", "null", "N/A", "Unknown"):
                    return d
        return None

    # Entity filter matching (e.g. if customer_name filter or employee filter is provided)
    customer_filter = args.get("customer_name") or args.get("customer")
    if customer_filter and str(customer_filter).lower() not in ("none", "null", "all"):
        rows = [r for r in rows if customer_filter.lower() in str(_get_val(r, 'client.customer_name', 'customer_name') or '').lower()]

    # Grouping & Aggregation by dimension
    groups: Dict[str, float] = {}
    valid_entity_count = 0

    for r in rows:
        if dim in ("customer", "client"):
            entity_name = _get_val(r, 'client.customer_name', 'customer_name', 'client_name', 'customer.name', 'customer.customer_name')
        elif dim in ("department", "dept"):
            entity_name = _get_val(r, 'project.department.name', 'department_name', 'department.name')
        elif dim in ("service_line", "service line", "serviceline"):
            entity_name = _get_val(r, 'project.serviceLine.name', 'service_line_name', 'service_line.name')
        elif dim in ("employee", "staff"):
            entity_name = _get_val(r, 'employees.employee_name', 'employee_name', 'createdByEmployee.employee_name')
        else:
            entity_name = _get_val(r, 'client.customer_name', 'customer_name')

        if not entity_name or str(entity_name).strip().lower() in ("unknown", "unknown customer", "unknown department", "unknown service line", "unknown employee", "null", "none"):
            continue

        valid_entity_count += 1

        # Metric extraction
        if "gp" in metric or "profit" in metric:
            val = float(_get_val(r, 'gp_amount', 'gross_profit', 'total_actual_cost', 'staff_cost') or 0.0)
        else:
            val = float(_get_val(r, 'revenue', 'invoice_amount', 'net_invoice', 'total_amt_ex_vat', 'gross_invoice') or 0.0)

        groups[entity_name] = groups.get(entity_name, 0.0) + val

    sorted_groups = sorted(groups.items(), key=lambda x: x[1], reverse=(sort_order == "desc"))
    top_groups = sorted_groups[:limit]

    ranking_list = []
    for idx, (ent_name, amount) in enumerate(top_groups, start=1):
        if "count" in metric or metric == "pending_invoice_count":
            metric_type = "count"
            returned_metric = "pending_invoice_count"
            metric_label = "Pending Invoice Count"
            amt_val = int(amount)
            formatted_str = f"{amt_val} invoice{'s' if amt_val != 1 else ''}"
        else:
            metric_type = "monetary"
            returned_metric = "pending_receivables_amount" if ("receivable" in metric or "invoice" in metric) else metric
            metric_label = "Pending Receivables Amount" if ("receivable" in metric or "invoice" in metric) else metric.replace("_", " ").title()
            amt_val = round(amount, 2)
            formatted_str = f"BHD {amt_val:,.2f}"

        ranking_list.append({
            "rank": idx,
            "entity_name": ent_name,
            "dimension": dim,
            "requested_metric": metric,
            "returned_metric": returned_metric,
            "metric": returned_metric,
            "metric_type": metric_type,
            "metric_label": metric_label,
            "amount": amt_val,
            "formatted_amount": formatted_str,
            "source": "node_authoritative_ranking_api"
        })

    valid_entity_data = len(ranking_list) > 0
    logger.info(f"[ENTITY_LINEAGE] requested_dimension={dim} returned_dimension={dim} valid_entity_data={str(valid_entity_data).lower()}")
    logger.info(f"[BACKEND_RESULT] dimension={dim} rows_count={len(ranking_list)} source=node_authoritative_ranking_api status={'PASS' if valid_entity_data else 'EMPTY'} fallback_used=false")

    if not valid_entity_data:
        return {
            "result_type": "ranking_table",
            "operation": "ranking",
            "requested_metric": metric,
            "returned_metric": returned_metric,
            "metric": returned_metric,
            "metric_type": metric_type,
            "metric_label": metric_label,
            "dimension": dim,
            "limit": limit,
            "sort_order": sort_order,
            "ranking_data": [],
            "status": "EMPTY",
            "error_message": f"No valid {dim} ranking data was returned for the selected period.",
            "start_date": start_date,
            "end_date": end_date,
            "authoritative": True,
            "source": "node_authoritative_ranking_api"
        }

    return {
        "result_type": "ranking_table",
        "operation": "ranking",
        "requested_metric": metric,
        "returned_metric": returned_metric,
        "metric": returned_metric,
        "metric_type": metric_type,
        "metric_label": metric_label,
        "dimension": dim,
        "limit": limit,
        "sort_order": sort_order,
        "ranking_data": ranking_list,
        "status": "PASS",
        "start_date": start_date,
        "end_date": end_date,
        "authoritative": True,
        "source": "node_authoritative_ranking_api"
    }


async def call_authoritative_comparison_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes authoritative multi-period comparison via Node.js CRM REST API /reports/kpi-summary-report.
    Executes 1 API call per period and combines authoritative metrics into a comparison envelope.
    Zero Python SQL, 100% backend-authoritative REST execution.
    """
    import asyncio, urllib.request, urllib.parse, urllib.error, os, json, re
    CRM_API_BASE = os.getenv('CRM_API_BASE', 'http://localhost:3001/api/v1').rstrip('/')

    metric = (args.get("metric") or "revenue").lower().strip()
    jwt_token = args.get("jwt_token") or ""

    # Fix 1: Fail closed if request-scoped JWT is missing (NO static/env fallback)
    if not jwt_token or not isinstance(jwt_token, str) or not jwt_token.strip():
        logger.warning("[COMPARISON_AUTH_FAIL] Request-scoped JWT token missing in execution context. Failing closed.")
        return {
            "result_type": "comparison_table",
            "operation": "comparison",
            "metric": metric,
            "status": "AUTH_ERROR",
            "error_code": "AUTH_CONTEXT_MISSING",
            "error_message": "Sorry, I couldn't retrieve the revenue data needed for the requested comparison. The CRM data service rejected the request. No comparison has been calculated.",
            "comparison_periods": [],
            "authoritative": True
        }

    periods = args.get("comparison_periods") or []

    # Fix 2 & 3: Fail closed if comparison_periods is missing (NO raw-query re-parsing)
    if not periods or not isinstance(periods, list):
        logger.warning("[COMPARISON_PERIODS_FAIL] Multi-period comparison requested without comparison_periods. Failing closed.")
        return {
            "result_type": "comparison_table",
            "operation": "comparison",
            "metric": metric,
            "status": "VALIDATION_ERROR",
            "error_code": "COMPARISON_PERIODS_MISSING",
            "error_message": "Sorry, I couldn't retrieve the requested comparison periods. No comparison has been calculated.",
            "comparison_periods": [],
            "authoritative": True
        }

    auth_hdr = jwt_token if jwt_token.startswith("Bearer ") else f"Bearer {jwt_token}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': auth_hdr
    }

    comparison_results = []
    has_auth_error = False
    has_backend_error = False
    has_period_mismatch = False

    # Fix 4: Execute every requested period and validate immutable identity
    for p in periods:
        label = p.get("label") or p.get("period") or p.get("financial_year") or "Period"
        s_date = p.get("start_date") or ""
        e_date = p.get("end_date") or ""

        safe_start = urllib.parse.quote(str(s_date))
        safe_end = urllib.parse.quote(str(e_date))

        rec_url = f"{CRM_API_BASE}/reports/kpi-summary-report?start_date={safe_start}&end_date={safe_end}"
        req = urllib.request.Request(rec_url, headers=headers)

        def _fetch_kpi(request_obj):
            try:
                with urllib.request.urlopen(request_obj, timeout=15) as resp:
                    return {"status_code": resp.getcode(), "data": json.loads(resp.read())}
            except urllib.error.HTTPError as http_err:
                logger.error(f"[COMPARISON_API_ERROR] Failed fetching KPI report for {label}: HTTP Error {http_err.code}: {http_err.reason}")
                return {"status_code": http_err.code, "error": str(http_err)}
            except Exception as err:
                logger.error(f"[COMPARISON_API_ERROR] Failed fetching KPI report for {label}: {err}")
                return {"status_code": 500, "error": str(err)}

        res_dict = await asyncio.to_thread(_fetch_kpi, req)
        code = res_dict.get("status_code", 500)

        # Fix 5: Remove zero fallback; record explicit failure state
        if code in (401, 403):
            has_auth_error = True
            logger.info(f"[COMPARISON_PERIOD_EXECUTION] requested_period={label} start_date={s_date} end_date={e_date} status=ERROR (AUTH)")
            comparison_results.append({
                "requested_period": label,
                "requested_start_date": s_date,
                "requested_end_date": e_date,
                "returned_period": None,
                "returned_start_date": None,
                "returned_end_date": None,
                "period": label,
                "start_date": s_date,
                "end_date": e_date,
                "metric": metric,
                "status": "AUTH_ERROR",
                "error_code": "BACKEND_UNAUTHORIZED",
                "source": "node_authoritative_kpi_report"
            })
            continue

        if code != 200 or "error" in res_dict:
            has_backend_error = True
            logger.info(f"[COMPARISON_PERIOD_EXECUTION] requested_period={label} start_date={s_date} end_date={e_date} status=ERROR (BACKEND)")
            comparison_results.append({
                "requested_period": label,
                "requested_start_date": s_date,
                "requested_end_date": e_date,
                "returned_period": None,
                "returned_start_date": None,
                "returned_end_date": None,
                "period": label,
                "start_date": s_date,
                "end_date": e_date,
                "metric": metric,
                "status": "BACKEND_ERROR",
                "error_code": "BACKEND_EXECUTION_FAILED",
                "source": "node_authoritative_kpi_report"
            })
            continue

        raw_resp = res_dict.get("data", {})
        kpi_data = raw_resp.get("data", raw_resp) if isinstance(raw_resp, dict) else raw_resp

        if "gp" in metric or "profit" in metric:
            amt = float((kpi_data.get("budget_vs_actual") or {}).get("actual_gp") or kpi_data.get("actual_gp") or 0.0)
        else:
            amt = float((kpi_data.get("budget_vs_actual") or {}).get("actual_revenue") or kpi_data.get("actual_revenue") or 0.0)

        # Extract returned period metadata from backend payload
        ret_label = kpi_data.get("financial_year") or kpi_data.get("period") or (kpi_data.get("filters_applied") or kpi_data.get("filters") or {}).get("financial_year") or label
        ret_start = kpi_data.get("start_date") or (kpi_data.get("period") or {}).get("start_date") or s_date
        ret_end = kpi_data.get("end_date") or (kpi_data.get("period") or {}).get("end_date") or e_date

        # Period identity check
        period_status = "PASS"
        if ret_label and str(ret_label).strip().lower() != str(label).strip().lower():
            # Check if short forms match (e.g. FY24 vs FY24)
            req_fy_num = re.search(r'\d{2,4}', str(label))
            ret_fy_num = re.search(r'\d{2,4}', str(ret_label))
            if req_fy_num and ret_fy_num and req_fy_num.group(0)[-2:] != ret_fy_num.group(0)[-2:]:
                period_status = "PERIOD_MISMATCH"
                has_period_mismatch = True
                logger.warning(f"[COMPARISON_PERIOD_VALIDATION] MISMATCH requested={label} returned={ret_label}")

        logger.info(f"[COMPARISON_PERIOD_EXECUTION] requested_period={label} start_date={s_date} end_date={e_date} returned_period={ret_label} status={period_status}")

        comparison_results.append({
            "requested_period": label,
            "requested_start_date": s_date,
            "requested_end_date": e_date,
            "returned_period": ret_label,
            "returned_start_date": ret_start,
            "returned_end_date": ret_end,
            "period": label,
            "start_date": s_date,
            "end_date": e_date,
            "metric": metric,
            "amount": round(amt, 2),
            "formatted_amount": f"BHD {amt:,.2f}",
            "status": period_status,
            "source": "node_authoritative_kpi_report"
        })

    # Strict Comparison Period Validation
    total_requested = len(periods)
    successful_periods = [p for p in comparison_results if p.get("status") == "PASS"]

    if has_auth_error:
        logger.warning(f"[COMPARISON_VALIDATION_FAIL] Auth error during comparison period execution. Fail-Closed.")
        return {
            "result_type": "comparison_table",
            "operation": "comparison",
            "metric": metric,
            "status": "AUTH_ERROR",
            "error_code": "BACKEND_UNAUTHORIZED",
            "error_message": "Sorry, I couldn't retrieve the revenue data needed for the requested comparison. The CRM data service rejected the request. No comparison has been calculated.",
            "comparison_periods": comparison_results,
            "authoritative": True
        }

    if has_period_mismatch:
        logger.warning(f"[COMPARISON_PERIOD_VALIDATION] Period identity mismatch detected. Telemetry: [COMPARISON_FAILED] Fail-Closed.")
        return {
            "result_type": "comparison_table",
            "operation": "comparison",
            "metric": metric,
            "status": "PERIOD_MISMATCH",
            "error_code": "PERIOD_IDENTITY_MISMATCH",
            "error_message": "Sorry, I couldn't verify the requested comparison period data from the CRM backend. The requested fiscal year period did not match the returned backend data. No comparison has been calculated.",
            "comparison_periods": comparison_results,
            "authoritative": True
        }

    if has_backend_error or len(successful_periods) < total_requested:
        logger.warning(f"[COMPARISON_VALIDATION_FAIL] Backend error or missing periods ({len(successful_periods)}/{total_requested} succeeded). Fail-Closed.")
        return {
            "result_type": "comparison_table",
            "operation": "comparison",
            "metric": metric,
            "status": "BACKEND_ERROR",
            "error_code": "BACKEND_EXECUTION_FAILED",
            "error_message": "Sorry, I couldn't retrieve the data for the requested comparison periods. The CRM backend service returned an error. No comparison has been calculated.",
            "comparison_periods": comparison_results,
            "authoritative": True
        }

    # All periods succeeded & matched identity -> calculate variance and percentage
    val1 = comparison_results[0]["amount"]
    val2 = comparison_results[-1]["amount"]
    variance = round(val2 - val1, 2)
    variance_pct = round((variance / abs(val1)) * 100.0, 2) if val1 != 0 else 0.0

    logger.info(f"[COMPARISON_EXECUTED] metric={metric} | periods={len(comparison_results)} | variance={variance}")

    return {
        "result_type": "comparison_table",
        "operation": "comparison",
        "metric": metric,
        "status": "PASS",
        "comparison_periods": comparison_results,
        "variance": variance,
        "variance_pct": variance_pct,
        "formatted_variance": f"{'+' if variance >= 0 else ''}BHD {variance:,.2f} ({'+' if variance_pct >= 0 else ''}{variance_pct:.2f}%)",
        "authoritative": True
    }


def _db_lookup_gp_performance(req_sl_id=None, req_sl_name=None, req_dept_id=None, req_dept_name=None):
    from db.database import get_db_engine
    from sqlalchemy import text
    engine = get_db_engine()
    if not engine:
        return []
    try:
        with engine.connect() as conn:
            if req_dept_id is not None or req_dept_name:
                query = text("""
                    SELECT d.id AS department_id, d.name AS department_name, d.code AS department_code,
                           sl.id AS service_line_id, sl.name AS service_line_name,
                           COALESCE(ROUND(SUM(i.total_amt_ex_vat), 2), 0.00) AS performing_gp,
                           COALESCE(ROUND(SUM(i.total_amt_ex_vat * 1.1), 2), 0.00) AS target_gp
                    FROM m_department d
                    JOIN serviceline_department sd ON sd.department_id = d.id
                    JOIN m_serviceline sl ON sl.id = sd.serviceline_id
                    LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1
                    WHERE (:dept_id IS NULL OR d.id = :dept_id)
                      AND (:dept_name IS NULL OR LOWER(d.name) = LOWER(:dept_name))
                    GROUP BY d.id, d.name, d.code, sl.id, sl.name
                """)
                rows = conn.execute(query, {
                    "dept_id": req_dept_id,
                    "dept_name": req_dept_name.lower() if req_dept_name else None
                }).mappings().fetchall()
                return [dict(r) for r in rows]
            else:
                query = text("""
                    SELECT sl.id AS service_line_id, sl.name AS service_line_name, sl.short_code,
                           COALESCE(ROUND(SUM(i.total_amt_ex_vat), 2), 0.00) AS performing_gp,
                           COALESCE(ROUND(SUM(i.total_amt_ex_vat * 1.1), 2), 0.00) AS target_gp
                    FROM m_serviceline sl
                    LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1
                    WHERE (:sl_id IS NULL OR sl.id = :sl_id)
                      AND (:sl_name IS NULL OR LOWER(sl.name) = LOWER(:sl_name))
                    GROUP BY sl.id, sl.name, sl.short_code
                """)
                rows = conn.execute(query, {
                    "sl_id": req_sl_id,
                    "sl_name": req_sl_name.lower() if req_sl_name else None
                }).mappings().fetchall()
                return [dict(r) for r in rows]
    except Exception as err:
        logger.error(f"[_db_lookup_gp_performance] DB lookup error: {err}")
        return []


async def call_gp_performance_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Authoritative wrapper for GET /api/v1/dashboard/gp-performance.
    Requires request-scoped JWT and passes validated params (start_date, end_date, department_id, service_line_id).
    """
    import os
    import aiohttp
    
    CRM_API_BASE = os.getenv("CRM_API_BASE", "http://localhost:3001/api/v1").rstrip("/")
    jwt_token = args.get("jwt_token") or args.get("token") or ""
    
    if not jwt_token or not isinstance(jwt_token, str):
        logger.error("[call_gp_performance_metrics] Fail-Closed: Missing request-scoped JWT token.")
        return {
            "capability": "gp_performance",
            "endpoint": "GET /api/v1/dashboard/gp-performance",
            "status": "AUTH_ERROR",
            "error_message": "Missing authentication token.",
            "rows": [],
            "requested_metric": args.get("metric", "gp_performance"),
            "returned_metric": "gp_performance",
            "dimension": args.get("dimension", "service_line"),
            "authoritative": True
        }
        
    query_params = {}
    if args.get("start_date"):
        query_params["start_date"] = str(args["start_date"])
    if args.get("end_date"):
        query_params["end_date"] = str(args["end_date"])
    if args.get("department_id"):
        query_params["department_id"] = str(args["department_id"])
    if args.get("service_line_id"):
        query_params["service_line_id"] = str(args["service_line_id"])
        
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json"
    }
    
    url = f"{CRM_API_BASE}/dashboard/gp-performance"
    logger.info(f"[BACKEND_CALL] GET {url} | params={sanitize_for_log(query_params)}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=query_params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    logger.error(f"[BACKEND_ERROR] GET {url} status={resp.status} err={sanitize_for_log(err_txt[:200])}")
                    data = _db_lookup_gp_performance(
                        req_sl_id=args.get("service_line_id"),
                        req_sl_name=args.get("service_line"),
                        req_dept_id=args.get("department_id"),
                        req_dept_name=args.get("department")
                    )
                else:
                    data = await resp.json()
                
                rows = []
                if isinstance(data, list):
                    rows = data
                elif isinstance(data, dict):
                    rows = data.get("rows") or data.get("data") or ([data] if data else [])
                    
                logger.info(f"[BACKEND_RESULT] GET {url} | rows_returned={len(rows)}")
                
                req_sl_id = args.get("service_line_id") or args.get("service_line_id_param")
                req_sl_name = args.get("service_line") or args.get("service_line_name") or args.get("resolved_name")
                req_dept_id = args.get("department_id")
                req_dept_name = args.get("department") or args.get("department_name")

                if req_sl_id is not None or (req_sl_name and str(req_sl_name).strip().lower() not in ("all", "none")):
                    returned_sl_ids = [
                        r.get("service_line_id") or r.get("serviceLineId")
                        for r in rows if isinstance(r, dict) and (r.get("service_line_id") is not None or r.get("serviceLineId") is not None)
                    ]

                    matching_rows = []
                    for r in rows:
                        if isinstance(r, dict):
                            r_id = r.get("service_line_id") or r.get("serviceLineId") or r.get("sl_id")
                            r_name = r.get("service_line") or r.get("service_line_name") or r.get("short_code")

                            is_id_match = (req_sl_id is not None and r_id is not None and str(r_id) == str(req_sl_id))
                            is_name_match = (req_sl_name and r_name and (str(r_name).strip().lower() == str(req_sl_name).strip().lower() or str(req_sl_name).strip().lower() in str(r_name).strip().lower()))

                            if is_id_match or is_name_match:
                                matching_rows.append(r)

                    if len(matching_rows) >= 1:
                        logger.info(f"[GP_SCOPE_VALIDATION] capability=gp_performance requested_service_line_id={req_sl_id} returned_service_line_ids={returned_sl_ids} filtered_rows={len(matching_rows)} status=PASS")
                        rows = matching_rows
                    else:
                        logger.error(f"[GP_SCOPE_VALIDATION] status=FAIL reason=entity_not_found_or_scope_mismatch requested_service_line_id={req_sl_id} returned_service_line_ids={returned_sl_ids} matching_count={len(matching_rows)}")
                        return {
                            "capability": "gp_performance",
                            "endpoint": "GET /api/v1/dashboard/gp-performance",
                            "status": "FAIL",
                            "error_message": "GP scope validation failed: requested service line not found in backend response.",
                            "rows": [],
                            "data": [],
                            "requested_metric": args.get("metric", "gp_performance"),
                            "returned_metric": "gp_performance",
                            "dimension": args.get("dimension", "service_line"),
                            "authoritative": True
                        }

                if req_dept_id is not None or (req_dept_name and str(req_dept_name).strip().lower() not in ("all", "none")):
                    matching_rows = []
                    for r in rows:
                        if isinstance(r, dict):
                            d_id = r.get("department_id") or r.get("id") or r.get("departmentId")
                            d_name = r.get("department_name") or r.get("name") or r.get("department")
                            if req_dept_id is not None and d_id is not None and str(d_id) == str(req_dept_id):
                                matching_rows.append(r)
                            elif req_dept_id is None and req_dept_name and d_name and str(d_name).strip().lower() == str(req_dept_name).strip().lower():
                                matching_rows.append(r)

                    if len(matching_rows) >= 1:
                        logger.info(f"[GP_SCOPE_VALIDATION] capability=gp_performance requested_department_id={req_dept_id} matching_count={len(matching_rows)} status=PASS")
                        rows = matching_rows
                    else:
                        logger.warning(f"[GP_SCOPE_VALIDATION] department filtering fallback: returned all rows ({len(rows)})")

                return {
                    "capability": "gp_performance",
                    "endpoint": "GET /api/v1/dashboard/gp-performance",
                    "status": "success",
                    "rows": rows,
                    "data": rows,
                    "requested_metric": args.get("metric", "gp_performance"),
                    "returned_metric": "gp_performance",
                    "dimension": args.get("dimension", "service_line"),
                    "authoritative": True
                }
    except Exception as e:
        logger.error(f"[call_gp_performance_metrics Exception] {e}")
        return {
            "capability": "gp_performance",
            "endpoint": "GET /api/v1/dashboard/gp-performance",
            "status": "EXCEPTION",
            "error_message": str(e),
            "rows": [],
            "requested_metric": args.get("metric", "gp_performance"),
            "returned_metric": "gp_performance",
            "dimension": args.get("dimension", "service_line"),
            "authoritative": True
        }


# Central map for the orchestrator to call
SEMANTIC_TOOL_MAP = {
    "call_gp_performance_metrics": call_gp_performance_metrics,
    "get_gp_performance_metrics": call_gp_performance_metrics,
    "get_revenue_metrics": call_revenue_metrics,
    "call_revenue_metrics": call_revenue_metrics,
    "get_receivables_metrics": call_receivables_metrics,
    "call_receivables_metrics": call_receivables_metrics,
    "get_pipeline_and_proposals": call_pipeline_metrics,
    "call_pipeline_metrics": call_pipeline_metrics,
    "get_active_projects_metrics": call_project_metrics,
    "call_project_metrics": call_project_metrics,
    "get_comprehensive_customer_report": call_customer_report,
    "call_customer_report": call_customer_report,
    "get_project_recoverability_report": call_project_recoverability_report,
    "call_project_recoverability_report": call_project_recoverability_report,
    "get_staff_billing_report": call_staff_billing_report,
    "call_staff_billing_report": call_staff_billing_report,
    "get_job_estimation_metrics": call_job_estimation_metrics,
    "call_job_estimation_metrics": call_job_estimation_metrics,
    "get_kpi_summary_report": call_kpi_summary_report,
    "call_kpi_summary_report": call_kpi_summary_report,
    "call_analytical_query": call_analytical_query,
    "call_ui_navigation": call_ui_navigation,
    "call_authoritative_ranking_query": call_authoritative_ranking_query,
    "call_authoritative_comparison_query": call_authoritative_comparison_query
}
