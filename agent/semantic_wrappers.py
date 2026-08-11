"""
semantic_wrappers.py — Thin wrappers around existing Semantic Layer tools.
We wrap them here to expose them to the new AI Orchestrator without modifying the original semantic_layer.py.
"""
import inspect
import json
import logging
from typing import Dict, Any, Optional, cast, Callable, Awaitable

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
    if target_func:
        try:
            fn_to_inspect = getattr(target_func, 'coroutine', None) or getattr(target_func, 'func', None) or target_func
            sig = inspect.signature(fn_to_inspect)
            allowed_params = set(sig.parameters.keys())
        except Exception:
            allowed_params = None

    for k, v in args.items():
        if k in RESERVED_PLANNER_KEYS and allowed_params is not None and k not in allowed_params:
            continue
        if isinstance(v, str) and v.strip().upper() in ("__ALL__", "ALL", "NONE", "NULL", "ANY", "*", ""):
            cleaned[k] = None
        else:
            if allowed_params is None or k in allowed_params:
                cleaned[k] = v
    return cleaned


async def _invoke_tool(tool_obj: Any, args: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Generic helper to execute LangChain tool or async coroutine cleanly with error handling."""
    cleaned_args = _clean_args(args, tool_obj)
    logger.info(f"Calling {name} with args: {cleaned_args}")
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
    return await _invoke_tool(get_kpi_summary_report, args, "get_kpi_summary_report")


async def call_analytical_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for ad_hoc_sql_query."""
    logger.info(f"Calling ad_hoc_sql_query via wrapper with args: {args}")
    try:
        from agent.agent import ad_hoc_sql_query
        question = args.get("question", "")
        if not question:
            return {"error": "Missing original question for analytical query generation."}
            
        result_str = await ad_hoc_sql_query.ainvoke({"question": question})
        
        try:
            return json.loads(result_str)
        except Exception:
            return {"result": result_str}
    except Exception as e:
        logger.error(f"Error in call_analytical_query: {e}")
        return {"error": str(e)}


async def call_ui_navigation(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for handling UI navigation commands from Planner."""
    logger.info(f"Handling ui_navigation with args: {args}")
    return {
        "action": "navigate",
        "target": args.get("target_dashboard")
    }


# Central map for the orchestrator to call
SEMANTIC_TOOL_MAP = {
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
    "call_ui_navigation": call_ui_navigation
}
