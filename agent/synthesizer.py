"""
synthesizer.py — Merges output from multiple executed tools into a final LLM response.
"""
import json
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 15000

def trim_report_payload(capability: str, data: Any, max_list_items: int = 5) -> Any:
    """
    Generic metadata-driven payload trimmer.

    Reads `response_schema` from the Capability Catalog for the given capability to
    identify which top-level fields are business-relevant. All other fields are passed
    through (minus debug/SQL keys). Large arrays are capped at `max_list_items` entries.

    This function contains ZERO capability-specific logic. Every report follows the same
    path. Adding a new report only requires adding metadata to the Capability Catalog.
    """
    if not isinstance(data, dict):
        if isinstance(data, list):
            if len(data) > max_list_items:
                return {
                    "total_records": len(data),
                    "summary_sample": data[:max_list_items],
                    "note": f"Dataset contained {len(data)} items; top {max_list_items} shown."
                }
        return data

    # Fetch schema fields from catalog (gracefully falls back to all fields)
    from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES
    resolved_cap = CAPABILITY_ALIASES.get(capability, capability)
    cap_meta = get_capability_metadata(resolved_cap) or {}

    # Internal fields that must never be forwarded to the LLM
    _SUPPRESSED = frozenset({"raw_sql", "sql", "query", "debug", "trace", "metadata_instructions"})

    summary = {}

    for key, val in data.items():
        if key in _SUPPRESSED:
            continue
        if isinstance(val, list):
            summary[key] = val[:max_list_items] if len(val) > max_list_items else val
            if len(val) > max_list_items:
                summary[f"{key}_total_count"] = len(val)
        elif isinstance(val, dict):
            summary[key] = {k: v for k, v in val.items() if k not in _SUPPRESSED}
        else:
            summary[key] = val

    return summary if summary else data




async def synthesize_response(original_query: str, tool_results: List[Dict[str, Any]], llm_client=None) -> Dict[str, Any]:
    """
    Takes the raw JSON output of all executed tools and formats a final answer.
    Trims large payloads before sending them to the LLM to prevent context window overflow.
    Preserves raw tool_results for internal inspection / full report exports.
    """
    logger.info(f"Synthesizing response dynamically for {len(tool_results)} tool outputs.")
    
    chart_data = None
    navigate_to = None
    action = None
    navigation_id = None
    synth_token_usage = {}

    # Policy Enforcement Step 1: Check Envelope Status & Errors across tool outputs
    failed_node = next((res for res in tool_results if res.get("status") in ["error", "unavailable"] or res.get("error")), None)
    if failed_node and not any(res.get("status") == "success" for res in tool_results):
        err_payload = failed_node.get("result", {})
        fallback_msg = (
            err_payload.get("error_message") if isinstance(err_payload, dict) else None
        ) or failed_node.get("error") or "The requested metric is currently unavailable. Please try again later."
        
        logger.warning(f"[Synthesizer Envelope Short-Circuit] Output status is non-success for capability '{failed_node.get('capability')}'. Suppressing synthesis.")
        return {
            "type": "done",
            "content": fallback_msg,
            "error_code": "capability_unavailable",
            "chart_data": None,
            "navigate_to": None,
            "navigation_links": None,
            "export_data": None,
            "auto_expand": False,
            "suggested_questions": None,
            "report_intent": None,
            "kpi_payload": None,
            "raw_tool_results": tool_results,
            "token_usage": {}
        }

    # Step 2: Trim tool results into lightweight summary objects for LLM consumption
    lightweight_tool_results = []
    for res in tool_results:
        cap = res.get("capability", "")
        raw_res = res.get("result")
        err = res.get("error")
        
        if err:
            lightweight_tool_results.append({"capability": cap, "error": err})
        elif raw_res:
            trimmed = trim_report_payload(cap, raw_res)
            lightweight_tool_results.append({"capability": cap, "result": trimmed})
        else:
            lightweight_tool_results.append(res)
    
    # Step 3: Serialize lightweight tool results and check size
    serialized_results = json.dumps(lightweight_tool_results, default=str, indent=2)
    if len(serialized_results) > MAX_PAYLOAD_CHARS:
        logger.warning(f"Lightweight payload size ({len(serialized_results)} chars) exceeds threshold. Truncating further.")
        for node in lightweight_tool_results:
            res_obj = node.get("result")
            if isinstance(res_obj, dict):
                for k, v in list(res_obj.items()):
                    if isinstance(v, list) and len(v) > 2:
                        res_obj[k] = v[:2]
        serialized_results = json.dumps(lightweight_tool_results, default=str, indent=2)
        if len(serialized_results) > MAX_PAYLOAD_CHARS:
            serialized_results = serialized_results[:MAX_PAYLOAD_CHARS] + "\n... [Summary data truncated for context window limits] ..."
    
    # Check if this is purely a navigation command
    nav_result = next((res for res in tool_results if res.get("capability") == "ui_navigation" and not res.get("error")), None)
    
    if nav_result:
        target = nav_result.get("result", {}).get("target")
        if target:
            action = "navigate"
            navigation_id = target
            target_name = target.replace('_', ' ').title()
            final_text = f"Opening the {target_name}..."
            
    # If we have an LLM Client configured and it's not a pure navigation command:
    if llm_client and not nav_result:
        from langchain_core.messages import SystemMessage, HumanMessage
        
        # Check if multiple reports or business capabilities were executed
        report_results = [r for r in tool_results if r.get("capability") != "ui_navigation"]
        is_multi_report = len(report_results) > 1

        if is_multi_report:
            system_prompt = (
                "You are an Executive Business Analyst for an Enterprise CRM.\n"
                "When multiple reports are provided, generate ONE consolidated, intelligent executive report.\n"
                "DO NOT concatenate raw reports one after another. Summarize and merge all insights into a single executive document.\n\n"
                "EXECUTIVE REPORT STRUCTURE (STRICT MANDATE):\n"
                "# Executive Summary\n"
                "Provide a concise, high-level overview combining insights across all executed reports.\n\n"
                "# Key Business Metrics\n"
                "Merge all important KPIs across reports into a single consolidated summary section (e.g. Revenue, Recoverability, Receivables, Pipeline, Margins, Project/Proposal counts). Remove duplicates. Display each metric only once.\n\n"
                "# Report Highlights\n"
                "Create concise subsections for each requested report (e.g., '## Revenue Highlights', '## Recoverability Highlights', '## KPI Highlights'). Include ONLY key business metrics, trends, and numbers. Omit verbose explanations, duplicate headers, or raw tables.\n\n"
                "# Overall Business Insights\n"
                "Identify relationships and cross-cutting trends between reports (e.g., 'Revenue is increasing while Recoverability is decreasing', 'Pipeline is strong but Receivables are growing').\n\n"
                "# Recommended Actions\n"
                "Provide 3-5 clear, actionable executive recommendations based on the combined findings.\n\n"
                "STRICT RULES:\n"
                "1. Never output raw reports sequentially.\n"
                "2. Never repeat the same KPI or metric twice.\n"
                "3. Keep descriptions concise, executive-ready, and analytical.\n"
                "4. NEVER invent or hallucinate data. Only use data provided in the tool results.\n"
                "5. Do not expose internal technical terms like 'capabilities', 'JSON', or 'SQL'.\n"
                "6. CURRENCY MANDATE: The system operates for Grant Thornton Bahrain (BHD). ALWAYS format financial values using 'BHD' (e.g., 'BHD 1,155,574'). NEVER use dollar signs ($) or USD."
            )
        else:
            system_prompt = (
                "You are an Executive Business Analyst for an Enterprise CRM.\n"
                "EXECUTIVE SINGLE METRIC MANDATE:\n"
                "1. Answer ONLY the user's specific requested metric in 1-2 concise sentences (e.g., 'The total active project count is 32.').\n"
                "2. Do NOT output unrequested KPI summary tables, target comparisons, or secondary breakdown sections unless explicitly requested.\n"
                "3. NEVER use technical implementation language or mention database/SQL/backend details.\n"
                "   STRICTLY BANNED PHRASES: 'The backend returned', 'The dataset contained', 'The data contains various statuses', 'The response includes a subset of records', 'raw JSON', 'SQL', 'error context', 'next steps'.\n"
                "4. Present the metric clearly and directly.\n"
                "5. NEVER substitute one metric for another (e.g. do not state Revenue when Receivables was requested).\n"
                "6. NEVER invent or hallucinate data. Only use data provided in the tool results.\n"
                "7. CURRENCY MANDATE: The system operates for Grant Thornton Bahrain (BHD). ALWAYS format monetary amounts using 'BHD' (e.g., 'BHD 1,155,574'). NEVER use dollar signs ($) or USD."
            )
        
        prompt = f"User Query: {original_query}\n\nSummarized Tool Results:\n{serialized_results}\n\nFormat this into a clear, direct executive answer."
        
        synth_token_usage = {}
        try:
            response = await llm_client.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
            final_text = response.content
            # Post-process: convert any remaining $ currency symbols to BHD
            import re
            final_text = re.sub(r'\$(\d)', r'BHD \1', final_text)
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                synth_token_usage = {
                    "input_tokens": response.usage_metadata.get("input_tokens", 0),
                    "output_tokens": response.usage_metadata.get("output_tokens", 0),
                    "total_tokens": response.usage_metadata.get("total_tokens", 0),
                }
                logger.info(f"[Synthesizer Tokens] In: {synth_token_usage['input_tokens']} | Out: {synth_token_usage['output_tokens']} | Total: {synth_token_usage['total_tokens']}")
        except Exception as e:
            logger.error(f"Dynamic synthesis failed: {e}")
            final_text = "I couldn't complete the request at the moment. Please try again later."
            return {
                "type": "done",
                "content": final_text,
                "error_code": "synthesizer_error",
                "chart_data": None,
                "navigate_to": None,
                "navigation_links": None,
                "export_data": None,
                "auto_expand": False,
                "suggested_questions": None,
                "report_intent": None,
                "kpi_payload": None,
                "raw_tool_results": tool_results,
                "token_usage": synth_token_usage
            }
    elif not nav_result:
        final_text = "I couldn't format the requested information at the moment. Please try again later."
    
    # Generic metadata-driven routing & chart extraction
    # Reads 'ui_action' and 'chart_config' from the Capability Catalog metadata.
    # Zero capability names are hardcoded here. New reports auto-support action/chart
    # by adding these fields to capability_catalog.py.
    action = None
    navigation_id = None
    chart_data = None
    navigate_to = None
    primary_tool = "general"
    primary_intent = "summary"

    from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES

    for res in tool_results:
        tool_name = res.get("capability")
        intent_val = res.get("intent")
        data = res.get("result", {})
        if tool_name:
            primary_tool = tool_name
        if intent_val:
            primary_intent = intent_val

        if res.get("error") or not tool_name:
            continue

        resolved_cap = CAPABILITY_ALIASES.get(tool_name, tool_name)
        cap_meta = get_capability_metadata(resolved_cap) or {}

        # Read ui_action from metadata (e.g. "navigate", "chart")
        ui_action = cap_meta.get("ui_action")
        if ui_action == "navigate":
            action = "navigate"
            navigation_id = tool_name

        # Read chart_config from metadata for auto chart generation
        chart_cfg = cap_meta.get("chart_config")
        if chart_cfg and isinstance(data, dict):
            data_key = chart_cfg.get("data_key")
            x_field = chart_cfg.get("x_field")
            y_field = chart_cfg.get("y_field")
            chart_label = chart_cfg.get("label", tool_name.replace("_", " ").title())
            chart_type = chart_cfg.get("type", "bar")
            if data_key and x_field and y_field:
                series = data.get(data_key)
                if isinstance(series, list) and series:
                    chart_data = {
                        "type": chart_type,
                        "labels": [item.get(x_field, "") for item in series],
                        "datasets": [{"label": chart_label, "data": [item.get(y_field, 0) for item in series]}]
                    }

    return {
        "type": "done",
        "content": final_text,
        "chart_data": chart_data,
        "navigate_to": navigate_to,
        "action": action,
        "navigation_id": navigation_id,
        "suggested_questions": _generate_executable_suggestions(primary_tool, primary_intent),
        "raw_tool_results": tool_results,
        "token_usage": synth_token_usage
    }


def _generate_executable_suggestions(tool_name: str, intent: str = "summary") -> list:
    """
    Dynamically generates intent & capability-aware follow-up suggested questions.
    """
    suggestions_map = {
        "pipeline_analysis": [
            "View proposals by Service Line",
            "View proposals by Partner",
            "View open proposals by Status",
            "Show proposal win rate percentage"
        ],
        "pipeline_metrics": [
            "View proposals by Service Line",
            "View proposals by Partner",
            "View open proposals by Status"
        ],
        "get_pipeline_and_proposals": [
            "View proposals by Service Line",
            "View proposals by Partner",
            "View open proposals by Status"
        ],
        "get_job_estimation_metrics": [
            "Job Estimations breakdown by Status",
            "View Approved Job Estimations",
            "View Job Estimations by Service Line"
        ],
        "revenue_analysis": [
            "Monthly Revenue Trend",
            "Revenue by Service Line",
            "Revenue by Office",
            "Revenue Comparison with Previous FY"
        ],
        "get_revenue_metrics": [
            "Monthly Revenue Trend",
            "Revenue by Service Line",
            "Revenue by Office",
            "Revenue Comparison with Previous FY"
        ],
        "receivables_analysis": [
            "View by Ageing Bucket",
            "View by Service Line",
            "Overdue Invoices (>90 Days)"
        ],
        "get_receivables_metrics": [
            "View by Ageing Bucket",
            "View by Service Line",
            "Overdue Invoices (>90 Days)"
        ],
        "recoverability_analysis": [
            "View Low Recoverability Projects (<80%)",
            "Recoverability by Service Line",
            "Show staff billing report"
        ],
        "get_project_recoverability_report": [
            "View Low Recoverability Projects (<80%)",
            "Recoverability by Service Line",
            "Show staff billing report"
        ]
    }
    
    default_suggestions = ["Show executive KPI summary", "Show revenue analysis", "Show project recoverability report"]
    return suggestions_map.get(tool_name, default_suggestions)





def format_data_response(user_query: str, tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    DATA MODE Formatter (0 Synthesizer LLM Calls).
    Formats raw tool results directly into clean markdown tables, lists, and totals.
    Used for simple retrieval queries ('Show projects', 'Show proposals', etc.).
    """
    if not tool_results:
        return {
            "type": "done",
            "content": "No matching records were found.",
            "response_mode": "DATA",
            "synthesizer_invoked": False,
            "raw_tool_results": []
        }

    lines = []
    primary_tool = "general"
    primary_intent = "data"

    for res in tool_results:
        cap = res.get("capability", "Data")
        primary_tool = cap
        data = res.get("result", {})
        err = res.get("error")

        if err:
            lines.append(f"⚠️ **{cap}**: {err}")
            continue

        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = data.get("rows") or data.get("data") or data.get("projects") or data.get("proposals") or [data]
        else:
            records = [data]

        if not records:
            lines.append(f"No matching records were found for **{cap}**.")
            continue

        # Format list of records into markdown table
        if isinstance(records, list) and len(records) > 0 and isinstance(records[0], dict):
            keys = list(records[0].keys())
            # Select key business display headers
            display_headers = [k for k in keys if k.lower() not in ["id", "raw_record", "tenant_id", "created_at", "updated_at"]][:6]
            if not display_headers:
                display_headers = keys[:4]

            # Header row
            header_str = "| " + " | ".join([h.replace("_", " ").title() for h in display_headers]) + " |"
            sep_str = "| " + " | ".join(["---"] * len(display_headers)) + " |"
            lines.append(header_str)
            lines.append(sep_str)

            # Data rows
            for row in records[:25]: # Cap at top 25 for display
                row_vals = []
                for h in display_headers:
                    val = row.get(h, "")
                    if isinstance(val, float):
                        val_str = f"{val:,.2f}"
                    elif val is None:
                        val_str = "-"
                    else:
                        val_str = str(val)
                    row_vals.append(val_str.replace("|", "/"))
                lines.append("| " + " | ".join(row_vals) + " |")

            lines.append(f"\n*Total Records Returned: {len(records)}*")
        else:
            json_str = json.dumps(records, indent=2)
            lines.append(f"```json\n{json_str}\n```")

    final_content = "\n".join(lines) if lines else "No matching records were found."

    return {
        "type": "done",
        "content": final_content,
        "response_mode": "DATA",
        "synthesizer_invoked": False,
        "suggested_questions": _generate_executable_suggestions(primary_tool, primary_intent),
        "raw_tool_results": tool_results,
        "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_name": "data_mode_formatter"}
    }
