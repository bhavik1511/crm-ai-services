"""
semantic_wrappers.py — Thin wrappers around existing Semantic Layer tools.
We wrap them here to expose them to the new AI Orchestrator without modifying the original semantic_layer.py.
"""
import json
import logging
from typing import Dict, Any, Optional

# We import the existing LangChain tools.
# Because they are LangChain @tool objects in semantic_layer.py, we must use .ainvoke()
from semantic.semantic_layer import (
    get_revenue_metrics,
    get_receivables_metrics,
    get_pipeline_and_proposals,
    get_active_projects_metrics,
    get_comprehensive_customer_report,
    get_total_estimation_report,
    get_project_recoverability_report,
    get_staff_billing_report,
    get_job_estimation_metrics
)

logger = logging.getLogger(__name__)

def _clean_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Strip aggregate sentinels (e.g. __ALL__) so tools perform un-filtered queries."""
    if not isinstance(args, dict):
        return args
    cleaned = {}
    for k, v in args.items():
        if isinstance(v, str) and v.strip().upper() in ("__ALL__", "ALL", "NONE", "NULL", "ANY", "*", ""):
            cleaned[k] = None
        else:
            cleaned[k] = v
    return cleaned

async def call_revenue_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_revenue_metrics."""
    args = _clean_args(args)
    logger.info(f"Calling get_revenue_metrics with args: {args}")
    try:
        # LangChain tools expect a dict for ainvoke
        result_str = await get_revenue_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_revenue_metrics: {e}")
        return {"error": str(e)}

async def call_receivables_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_receivables_metrics."""
    args = _clean_args(args)
    logger.info(f"Calling get_receivables_metrics with args: {args}")
    try:
        result_str = await get_receivables_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_receivables_metrics: {e}")
        return {"error": str(e)}

async def call_pipeline_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_pipeline_and_proposals."""
    args = _clean_args(args)
    logger.info(f"Calling get_pipeline_and_proposals with args: {args}")
    try:
        result_str = await get_pipeline_and_proposals.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_pipeline_metrics: {e}")
        return {"error": str(e)}

async def call_project_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_active_projects_metrics."""
    args = _clean_args(args)
    logger.info(f"Calling get_active_projects_metrics with args: {args}")
    try:
        result_str = await get_active_projects_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_project_metrics: {e}")
        return {"error": str(e)}

async def call_customer_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_comprehensive_customer_report."""
    args = _clean_args(args)
    search_term = args.get("search_term") or args.get("customer_name") or args.get("customer") or args.get("question", "")
    logger.info(f"Calling get_comprehensive_customer_report with search_term: {search_term}")
    try:
        result_str = await get_comprehensive_customer_report.ainvoke({"search_term": str(search_term)})
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_customer_report: {e}")
        return {"error": str(e)}

async def call_project_recoverability_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_project_recoverability_report."""
    args = _clean_args(args)
    logger.info(f"Calling get_project_recoverability_report with args: {args}")
    try:
        result_str = await get_project_recoverability_report.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_project_recoverability_report: {e}")
        return {"error": str(e)}

async def call_staff_billing_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_staff_billing_report."""
    args = _clean_args(args)
    logger.info(f"Calling get_staff_billing_report with args: {args}")
    try:
        result_str = await get_staff_billing_report.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_staff_billing_report: {e}")
        return {"error": str(e)}

async def call_job_estimation_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_job_estimation_metrics."""
    args = _clean_args(args)
    logger.info(f"Calling get_job_estimation_metrics with args: {args}")
    try:
        result_str = await get_job_estimation_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_job_estimation_metrics: {e}")
        return {"error": str(e)}

async def call_analytical_query(args: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"Calling ad_hoc_sql_query via wrapper with args: {args}")
    try:
        from agent.agent import ad_hoc_sql_query
        question = args.get("question", "")
        if not question:
            return {"error": "Missing original question for analytical query generation."}
            
        result_str = await ad_hoc_sql_query.ainvoke({"question": question})
        
        try:
            return json.loads(result_str)
        except:
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
    "call_analytical_query": call_analytical_query,
    "call_ui_navigation": call_ui_navigation
}
