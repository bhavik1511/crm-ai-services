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
    get_staff_billing_report
)

logger = logging.getLogger(__name__)

async def call_revenue_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_revenue_metrics."""
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
    logger.info(f"Calling get_receivables_metrics with args: {args}")
    try:
        result_str = await get_receivables_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_receivables_metrics: {e}")
        return {"error": str(e)}

async def call_pipeline_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_pipeline_and_proposals."""
    logger.info(f"Calling get_pipeline_and_proposals with args: {args}")
    try:
        result_str = await get_pipeline_and_proposals.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_pipeline_metrics: {e}")
        return {"error": str(e)}

async def call_project_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_active_projects_metrics."""
    logger.info(f"Calling get_active_projects_metrics with args: {args}")
    try:
        result_str = await get_active_projects_metrics.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_project_metrics: {e}")
        return {"error": str(e)}

async def call_customer_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_comprehensive_customer_report."""
    logger.info(f"Calling get_comprehensive_customer_report with args: {args}")
    try:
        result_str = await get_comprehensive_customer_report.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_customer_report: {e}")
        return {"error": str(e)}

async def call_project_recoverability_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_project_recoverability_report."""
    logger.info(f"Calling get_project_recoverability_report with args: {args}")
    try:
        result_str = await get_project_recoverability_report.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_project_recoverability_report: {e}")
        return {"error": str(e)}

async def call_staff_billing_report(args: Dict[str, Any]) -> Dict[str, Any]:
    """Wrapper for get_staff_billing_report."""
    logger.info(f"Calling get_staff_billing_report with args: {args}")
    try:
        result_str = await get_staff_billing_report.ainvoke(args)
        return json.loads(result_str) if isinstance(result_str, str) else result_str
    except Exception as e:
        logger.error(f"Error in call_staff_billing_report: {e}")
        return {"error": str(e)}

# Central map for the orchestrator to call
SEMANTIC_TOOL_MAP = {
    "get_revenue_metrics": call_revenue_metrics,
    "get_receivables_metrics": call_receivables_metrics,
    "get_pipeline_and_proposals": call_pipeline_metrics,
    "get_active_projects_metrics": call_project_metrics,
    "get_comprehensive_customer_report": call_customer_report,
    "get_project_recoverability_report": call_project_recoverability_report,
    "get_staff_billing_report": call_staff_billing_report
}
