"""
synthesizer.py — Presentation Layer. Merges backend tool outputs into executive-quality responses.
Phase 3.1.10: Fully presentation-mode driven. Zero raw data policy enforced.
"""
import json
import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 15000

# Fields that must NEVER be forwarded to the LLM or shown to the user
_SUPPRESSED_FIELDS = frozenset({
    "raw_sql", "sql", "query", "debug", "trace", "metadata_instructions",
    "tenant_id", "created_at", "updated_at", "internal_id", "_id", "source"
})

def _get_matching_breakdown_array(query: str, data: dict) -> tuple:
    """
    Selects the matching array, title, x_field, and y_field from multi-array tool responses based on query intent.
    """
    if not isinstance(data, dict):
        return None, None, None, None

    q_lower = query.lower()

    # 1. GP Performance / Service Line GP
    if ("gp" in q_lower or "gross profit" in q_lower or "performance" in q_lower) and "gp_performance_ytd_breakdown" in data:
        arr = data.get("gp_performance_ytd_breakdown")
        if isinstance(arr, list) and arr:
            return ("GP Performance by Service Line", arr, "name", "performing")

    # 2. Team Billing / Billing by Service Line
    if ("billing" in q_lower or "staff" in q_lower or "team" in q_lower) and ("current_team_billing_breakdown" in data or "team_billing_breakdown" in data):
        arr = data.get("current_team_billing_breakdown") or data.get("team_billing_breakdown")
        if isinstance(arr, list) and arr:
            return ("Team Billing Breakdown", arr, "name", "performing")

    # 3. Top Customers
    if ("customer" in q_lower or "client" in q_lower or "top 5" in q_lower or "top customer" in q_lower) and "top_5_customers" in data:
        arr = data.get("top_5_customers")
        if isinstance(arr, list) and arr:
            return ("Top 5 Customers by Revenue", arr, "customer_name", "revenue")

    # 4. Monthly Revenue Breakdown
    if ("month" in q_lower or "monthly" in q_lower or "trend" in q_lower) and "revenue_by_month" in data:
        arr = data.get("revenue_by_month")
        if isinstance(arr, list) and arr:
            return ("Revenue by Month Breakdown", arr, "month", "amount")

    # Default fallback if query was generic revenue or service line
    if "gp_performance_ytd_breakdown" in data and isinstance(data["gp_performance_ytd_breakdown"], list) and ("gp" in q_lower or "service line" in q_lower or "serviceline" in q_lower):
        return ("GP Performance by Service Line", data["gp_performance_ytd_breakdown"], "name", "performing")

    return None, None, None, None


# ---------------------------------------------------------------------------
# Payload Normalizer
# ---------------------------------------------------------------------------
def _normalize_payload(data: Any, cap_id: str = "") -> Any:
    """
    Sanitizes backend payload before it reaches the LLM or user.
    - Strips all internal/technical fields.
    - Formats monetary floats as BHD strings.
    - Formats ISO date strings to human-readable DD MMM YYYY.
    NEVER modifies business calculations or metric values.
    """
    import datetime

    def _fmt_value(val):
        if isinstance(val, float) and val > 100:
            return f"BHD {val:,.2f}"
        if isinstance(val, str):
            # Try ISO date formatting
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
                try:
                    dt = datetime.datetime.strptime(val[:19], fmt)
                    return dt.strftime("%d %b %Y")
                except ValueError:
                    pass
        return val

    if isinstance(data, dict):
        return {
            k: _normalize_payload(v, cap_id)
            for k, v in data.items()
            if k not in _SUPPRESSED_FIELDS
        }
    elif isinstance(data, list):
        return [_normalize_payload(item, cap_id) for item in data]
    else:
        return _fmt_value(data)


def _select_presentation_mode(execution_plan: Optional[dict], tool_results: List[dict]) -> str:
    """
    Reads the presentation_mode determined by the Planner.
    Falls back to payload-shape heuristic only if the Planner didn't set one.
    The Synthesizer NEVER guesses user intent — it follows the Planner.
    """
    if execution_plan:
        # Plan-level mode
        mode = execution_plan.get("presentation_mode")
        if mode:
            return mode.upper()
        # Capability-level mode from first capability
        caps = execution_plan.get("business_capabilities") or []
        if caps:
            cap_mode = caps[0].get("presentation_mode")
            if cap_mode:
                return cap_mode.upper()

    # Fallback: payload-shape heuristic
    if len(tool_results) > 1:
        return "EXECUTIVE_BRIEF"
    return "INSIGHT"


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
    response_schema = cap_meta.get("response_schema") or {}

    # Internal fields that must never be forwarded to the LLM
    _SUPPRESSED = frozenset({"raw_sql", "sql", "query", "debug", "trace", "metadata_instructions", "created_at", "updated_at", "created_by", "updated_by"})

    def _clean_node(val: Any) -> Any:
        """Recursively strip nulls, empty strings, dashes, zero values, and empty structures."""
        if isinstance(val, dict):
            cleaned = {
                k: _clean_node(v)
                for k, v in val.items()
                if k not in _SUPPRESSED
            }
            return {
                k: v for k, v in cleaned.items()
                if v not in (None, "", "-", [], {})
            }
        elif isinstance(val, list):
            sliced = val[:max_list_items]
            cleaned_list = [_clean_node(item) for item in sliced]
            return [item for item in cleaned_list if item not in (None, "", "-", [], {})]
        else:
            return val

    summary = {}

    for key, val in data.items():
        if key in _SUPPRESSED:
            continue
        if response_schema and key not in response_schema and not key.startswith("total_") and key not in {"status", "records", "data"}:
            continue
        cleaned_val = _clean_node(val)
        if cleaned_val not in (None, "", "-", [], {}):
            summary[key] = cleaned_val
            if isinstance(val, list) and len(val) > max_list_items:
                summary[f"{key}_total_count"] = len(val)

    return summary if summary else data




async def synthesize_response(original_query: str, tool_results: List[Dict[str, Any]], llm_client=None, execution_plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Takes the raw JSON output of all executed tools and formats a final answer.
    Trims large payloads before sending them to the LLM to prevent context window overflow.
    Preserves raw tool_results for internal inspection / full report exports.
    """
    if not llm_client:
        try:
            from config.llm_factory import get_llm
            llm_client = get_llm()
        except Exception as exc:
            logger.warning(f"Could not load default LLM in synthesizer: {exc}")
    
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

        # Read presentation mode from Planner (never guess)
        if not execution_plan:
            for res in tool_results:
                if res.get("execution_plan"):
                    execution_plan = res["execution_plan"]
                    break
        presentation_mode = _select_presentation_mode(execution_plan, tool_results)

        # Normalize payloads (strip technical fields, format BHD, format dates)
        normalized_results = []
        for res in lightweight_tool_results:
            cap = res.get("capability", "")
            raw_result = res.get("result")
            err = res.get("error")
            
            # Check for raw SQL execution errors and sanitize them to prevent technical leaks
            raw_str = str(raw_result or err or "")
            if "SQL execution error" in raw_str or "pymysql" in raw_str or "Unknown column" in raw_str or "1054" in raw_str:
                normalized_results.append({
                    "capability": cap,
                    "error": "Data query could not be completed for the requested criteria."
                })
            elif err:
                normalized_results.append({"capability": cap, "error": str(err)})
            elif raw_result is not None:
                normalized_results.append({"capability": cap, "result": _normalize_payload(raw_result, cap)})
            else:
                normalized_results.append(res)

        serialized_results = json.dumps(normalized_results, default=str, indent=2)
        if len(serialized_results) > MAX_PAYLOAD_CHARS:
            serialized_results = serialized_results[:MAX_PAYLOAD_CHARS] + "\n... [truncated] ..."

        # Build system prompt based on presentation mode — Synthesizer follows Planner
        is_multi = len([r for r in tool_results if r.get("capability") != "ui_navigation"]) > 1
        requires_report = False
        requires_summary = False
        requires_comparison = False
        if execution_plan:
            caps = execution_plan.get("business_capabilities") or []
            requires_report = any(c.get("requires_report") for c in caps)
            requires_summary = any(c.get("requires_summary") for c in caps)
            requires_comparison = any(c.get("requires_comparison") for c in caps)

        if presentation_mode == "KPI_CARD":
            system_prompt = (
                "You are an Executive AI Assistant for an Enterprise CRM.\n"
                "MANDATE: State the primary requested metric/answer clearly in the VERY FIRST sentence in bold.\n"
                "Examples:\n"
                "- 'Total revenue for January 2026 was **BHD 322,559.65**.'\n"
                "- 'The total actual recoverability for January 2026 is **92.4%**.'\n"
                "- 'There are currently **1,302 active projects** across all service lines.'\n"
                "STRICT RULES:\n"
                "1. Always put the main answer/metric in bold in the very first sentence.\n"
                "2. Maximum 1-3 direct, executive sentences.\n"
                "3. Format monetary values as BHD (e.g., BHD 322,559.65). Never use $ or USD.\n"
                "4. PERCENTAGES & RECOVERABILITY: Recoverability and percentage metrics are ALWAYS percentages (e.g. 44.99% or 92.4%). NEVER prefix recoverability or percentages with BHD or currency symbols!\n"
                "5. NEVER mention JSON, backend, SQL, raw datasets, or internal code terms.\n"
                "6. NEVER fabricate numbers. Use only the data provided.\n"
            )
        elif presentation_mode == "COMPARISON":
            system_prompt = (
                "You are an Executive Business Analyst for an Enterprise CRM.\n"
                "MANDATE: Provide a clear executive comparison answering whether there was a profit/loss or gain/decline in the VERY FIRST paragraph.\n"
                "STRUCTURE (use this exactly):\n"
                "## Executive Comparison Summary\n"
                "Direct 1-2 sentence statement of the overall change (e.g., profit/loss, growth rate, variance).\n\n"
                "## Key Variances\n"
                "Clean markdown table comparing Period A vs Period B vs Variance (% / BHD).\n\n"
                "## Executive Insights\n"
                "2-3 concise analytical observations about the comparison.\n\n"
                "STRICT RULES:\n"
                "1. State the profit/loss status or main growth finding in the very first sentence.\n"
                "2. Format monetary values as BHD. Never use $ or USD.\n"
                "3. PERCENTAGES & RECOVERABILITY: Recoverability and percentage metrics are ALWAYS percentages (e.g. 44.99% or 92.4%). NEVER prefix recoverability or percentages with BHD!\n"
                "4. NEVER fabricate numbers. Use only data provided.\n"
                "5. NEVER expose raw stringified JSON, python dicts, or SQL fields.\n"
            )
        elif presentation_mode in ("EXECUTIVE_BRIEF", "INSIGHT") or is_multi:
            system_prompt = (
                "You are an Executive AI Assistant for an Enterprise CRM.\n"
                "MANDATE: Provide a structured, executive-grade response. NEVER output plain monolithic text blocks or ugly paragraphs.\n\n"
                "FORMATTING & TABLE MANDATE:\n"
                "1. FOR ANY LIST, RANKING, BREAKDOWN, OR MULTI-ITEM DATA WITH 3 OR MORE ITEMS (e.g. top customers, revenue by month, service lines, aging buckets, project lists):\n"
                "   - Render a clean Markdown Table with bold headers (e.g. `| Rank | Name / Item | Amount (BHD) |`).\n"
                "   - Include a 1-line bold executive summary header above the table.\n"
                "2. FOR 1 OR 2 ITEMS OR SINGLE METRIC QUERIES:\n"
                "   - DO NOT create a table for 1 or 2 items! Render directly as 1-2 clean, bold bullet points.\n"
                "3. WHEN SPECIFIC DATA IS NOT IN THE PRE-CALCULATED REPORT:\n"
                "   - DO NOT write long apologetic text blocks.\n"
                "   - State available top-level metrics clearly in bold bullet points, followed by a concise 1-line note advising where to find detailed data.\n\n"
                "PERFORMANCE & GOAL EVALUATION RULE:\n"
                "Whenever answering performance or target queries ('how did it perform', 'how is X performing', 'performance vs target'):\n"
                "- Explicitly state in the VERY FIRST line whether the entity is BEHIND GOAL (shortfall) or ON TRACK / EXCEEDING GOAL (surplus), along with the percentage achieved and total shortfall/surplus.\n"
                "- Example: '**Overall Status:** 🔴 **BEHIND GOAL** (achieved BHD 1,057,021 of BHD 1,860,000 target | 56.8% achieved | BHD 802,978 shortfall)'.\n\n"
                "STRICT RULES:\n"
                "1. NEVER include 'Business Insights', 'Recommended Actions', or unsolicited advice sections unless specifically requested.\n"
                "2. Be concise and to-the-point. Eliminate unnecessary fluff, jargon, or monolithic text walls.\n"
                "3. Format monetary values as BHD (e.g., BHD 1,155,574). Never use $ or USD.\n"
                "4. PERCENTAGES & RECOVERABILITY: Recoverability and percentage metrics are ALWAYS percentages (e.g. 44.99% or 92.4%). NEVER prefix recoverability or percentages with BHD!\n"
                "   RECOVERABILITY MANDATE: When answering recoverability queries, if recoverability percentage (actual_recoverability_pct / portfolio_recoverability_pct / total_actual_recoverability_percentage) is present in the data, ALWAYS state the Recoverability Percentage explicitly in bold in the final answer alongside total projects and cost.\n"
                "5. NEVER fabricate or hallucinate data. Only use data provided.\n"
                "6. NEVER expose JSON, SQL, Python dicts, internal technical terms, or internal dataset source names (e.g. CRM_API_RECOVERABILITY_REPORT, CRM_DATABASE_SQL, API endpoints).\n"
                "7. ERROR MASKING MANDATE: If data retrieval failed or contains database errors, state politely: 'I was unable to retrieve the requested metrics at this moment. Please try again or try asking different questions.' NEVER display technical errors, raw SQL, column names, or stack traces.\n"
            )
        elif presentation_mode == "REPORT_AND_INSIGHT" or presentation_mode == "REPORT":
            system_prompt = (
                "You are an Executive AI Assistant for an Enterprise CRM.\n"
                "RECOVERABILITY REPORT MANDATE:\n"
                "When answering Recoverability queries or reports, you MUST display ALL key summary metrics as bold bullet points:\n"
                "- **Total Projects:** [count]\n"
                "- **Total Approved Fees:** [BHD amount] (if available)\n"
                "- **Total Actual Cost:** [BHD amount]\n"
                "- **Actual Recoverability:** [percentage%]\n\n"
                "STRICT RULES:\n"
                "1. ALWAYS include Total Projects, Total Actual Cost, AND Actual Recoverability Percentage (e.g. 117.49%) in the response bullet points, even if the user query only asked for one or two of them.\n"
                "2. Format monetary values as BHD (e.g., BHD 18,342.50). Never use $ or USD.\n"
                "3. PERCENTAGES & RECOVERABILITY: Recoverability and percentage metrics are ALWAYS percentages (e.g. 117.49%). NEVER prefix recoverability or percentages with BHD!\n"
                "4. NEVER fabricate or hallucinate numbers. Use only data provided in tool results.\n"
                "5. NEVER expose raw JSON, SQL, technical metadata, or internal source names.\n"
            )
        else:
            # Default: single-metric or general INSIGHT
            system_prompt = (
                "You are an Executive AI Assistant for an Enterprise CRM.\n"
                "EXECUTIVE SINGLE METRIC MANDATE:\n"
                "1. Answer ONLY the user's specific requested metric in 1-2 concise, formatted sentences or bullet points with key figures in bold.\n"
                "2. If tabular/list data is present, format as a clean Markdown table.\n"
                "3. NEVER use technical implementation language or mention database/SQL/backend details.\n"
                "   BANNED PHRASES & TERMS: 'The backend returned', 'The dataset contained', 'raw JSON', 'SQL', 'The provided data does not include', 'CRM_API_RECOVERABILITY_REPORT', 'CRM_DATABASE_SQL'.\n"
                "4. Present metrics clearly and directly in bold.\n"
                "5. PERCENTAGES & RECOVERABILITY: Recoverability and percentage metrics are ALWAYS percentages (e.g. 44.99% or 92.4%). NEVER prefix recoverability or percentages with BHD!\n"
                "6. NEVER fabricate data. Only use data provided in tool results.\n"
                "7. Format all monetary values as BHD (e.g., BHD 1,155,574). Never use $ or USD.\n"
            )
        
        prompt = f"User Query: {original_query}\n\nSummarized Tool Results:\n{serialized_results}\n\nFormat this into a clear, direct executive answer."
        
        synth_token_usage = {}
        try:
            try:
                response = await llm_client.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
            except Exception as primary_err:
                err_str = str(primary_err)
                if "429" in err_str or "rate_limit" in err_str.lower() or "quota" in err_str.lower():
                    logger.warning("[Synthesizer] Primary model rate-limited (429). Retrying with llama-3.1-8b-instant fallback model...")
                    from config.llm_factory import get_llm
                    fallback_llm = get_llm(model_name="llama-3.1-8b-instant")
                    response = await fallback_llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
                else:
                    raise primary_err

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
            logger.error(f"Dynamic synthesis failed ({e}). Falling back to 0-token DATA mode formatter.")
            fallback_res = format_data_response(original_query, tool_results)
            fallback_res["error_code"] = "synthesizer_fallback"
            return fallback_res
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
        if isinstance(data, dict):
            dyn_title, dyn_arr, dyn_x, dyn_y = _get_matching_breakdown_array(original_query, data)
            chart_cfg = cap_meta.get("chart_config") or {}
            chart_type = chart_cfg.get("type", "bar")
            
            if dyn_arr and dyn_x and dyn_y:
                chart_data = {
                    "type": chart_type,
                    "labels": [item.get(dyn_x, "") for item in dyn_arr],
                    "datasets": [{"label": dyn_title, "data": [item.get(dyn_y, 0) for item in dyn_arr]}]
                }
            elif chart_cfg:
                data_key = chart_cfg.get("data_key")
                x_field = chart_cfg.get("x_field")
                y_field = chart_cfg.get("y_field")
                chart_label = chart_cfg.get("label", tool_name.replace("_", " ").title())
                if data_key and x_field and y_field:
                    series = data.get(data_key)
                    if isinstance(series, list) and series:
                        chart_data = {
                            "type": chart_type,
                            "labels": [item.get(x_field, "") for item in series],
                            "datasets": [{"label": chart_label, "data": [item.get(y_field, 0) for item in series]}]
                        }

    res = {
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
    from registry.contract_engine import wrap_presentation_intent
    return wrap_presentation_intent(res, original_query, primary_tool)


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
            lines.append(f"⚠️ **{cap.replace('_', ' ').title()}**: {err}")
            continue

        if not data:
            lines.append(f"No matching records were found for **{cap.replace('_', ' ').title()}**.")
            continue

        if isinstance(data, dict):
            summary_bullets = []
            
            for k, v in data.items():
                if k in _SUPPRESSED_FIELDS or k in (
                    "rows", "data", "projects", "proposals", "status_breakdown", 
                    "Proposalstatus", "dashboard_proposal_metrics_breakdown", 
                    "dashboard_engagement_metrics_breakdown", 
                    "dashboard_continuous_engagement_metrics_breakdown", 
                    "service_leads_breakdown"
                ):
                    continue
                
                label = k.replace("_", " ").title()
                
                if isinstance(v, (int, float)):
                    if "rate" in k.lower() or "pct" in k.lower() or "percentage" in k.lower():
                        summary_bullets.append(f"- **{label}:** **{v:.2f}%**")
                    elif isinstance(v, float) and v > 100:
                        summary_bullets.append(f"- **{label}:** **BHD {v:,.2f}**")
                    else:
                        summary_bullets.append(f"- **{label}:** **{v:,}**")
                elif isinstance(v, str):
                    summary_bullets.append(f"- **{label}:** {v}")
                elif isinstance(v, dict):
                    cnt = v.get("count") if v.get("count") is not None else v.get("total_entries")
                    bgt = v.get("total_budget") if v.get("total_budget") is not None else v.get("value")
                    parts = []
                    if cnt is not None:
                        parts.append(f"**{cnt:,}** entries")
                    if bgt is not None and isinstance(bgt, (int, float)):
                        parts.append(f"**BHD {bgt:,.2f}**")
                    if parts:
                        summary_bullets.append(f"- **{label}:** " + " | ".join(parts))

            if summary_bullets:
                lines.append("### Key Summary Metrics")
                lines.extend(summary_bullets)
                lines.append("")

            sub_lists = []
            dyn_title, dyn_arr, _, _ = _get_matching_breakdown_array(user_query, data)
            if dyn_title and dyn_arr:
                sub_lists.append((dyn_title, dyn_arr))
            elif "rows" in data and isinstance(data["rows"], list):
                sub_lists.append(("Records", data["rows"]))
            elif "projects" in data and isinstance(data["projects"], list):
                sub_lists.append(("Projects", data["projects"]))
            elif "proposals" in data and isinstance(data["proposals"], list):
                sub_lists.append(("Proposals", data["proposals"]))
            elif "gp_performance_ytd_breakdown" in data and isinstance(data["gp_performance_ytd_breakdown"], list):
                sub_lists.append(("GP Performance by Service Line", data["gp_performance_ytd_breakdown"]))
            elif "status_breakdown" in data and isinstance(data["status_breakdown"], list):
                sub_lists.append(("Status Breakdown", data["status_breakdown"]))
            elif "Proposalstatus" in data and isinstance(data["Proposalstatus"], list):
                sub_lists.append(("Proposal Status Breakdown", data["Proposalstatus"]))
            elif "revenue_by_month" in data and isinstance(data["revenue_by_month"], list):
                sub_lists.append(("Revenue by Month Breakdown", data["revenue_by_month"]))

            for title, records in sub_lists:
                if records and isinstance(records[0], dict):
                    clean_records = [
                        r for r in records 
                        if not (r.get("totalEntries") == 0 and r.get("totalBudget") is None)
                        and not (r.get("count") == 0 and r.get("proposed_fees") == 0)
                    ]
                    if not clean_records:
                        clean_records = records[:10]

                    keys = list(clean_records[0].keys())
                    display_headers = [
                        k for k in keys 
                        if k.lower() not in _SUPPRESSED_FIELDS 
                        and k.lower() not in {"id", "status_id", "raw_record"}
                    ][:6]

                    if display_headers:
                        lines.append(f"### {title}")
                        if len(clean_records) < 3:
                            for row in clean_records:
                                name = row.get("name") or row.get("title") or row.get("label") or row.get("status_name")
                                parts = []
                                for h in display_headers:
                                    if h.lower() in ("name", "title", "label", "status_name"):
                                        continue
                                    val = row.get(h)
                                    if val is not None:
                                        if isinstance(val, float) and val > 100:
                                            val_str = f"BHD {val:,.2f}"
                                        elif isinstance(val, (int, float)):
                                            val_str = f"{val:,}"
                                        else:
                                            val_str = str(val)
                                        parts.append(f"**{h.replace('_', ' ').title()}:** {val_str}")
                                prefix = f"- **{name}:** " if name else "- "
                                lines.append(prefix + " | ".join(parts))
                            lines.append("")
                        else:
                            header_str = "| " + " | ".join([h.replace("_", " ").title() for h in display_headers]) + " |"
                            sep_str = "| " + " | ".join(["---"] * len(display_headers)) + " |"
                            lines.append(header_str)
                            lines.append(sep_str)

                            for row in clean_records[:25]:
                                row_vals = []
                                for h in display_headers:
                                    val = row.get(h, "")
                                    if isinstance(val, float):
                                        val_str = f"BHD {val:,.2f}" if val > 100 else f"{val:,.2f}"
                                    elif isinstance(val, int):
                                        val_str = f"{val:,}"
                                    elif val is None:
                                        val_str = "-"
                                    else:
                                        val_str = str(val)
                                    row_vals.append(val_str.replace("|", "/"))
                                lines.append("| " + " | ".join(row_vals) + " |")
                            lines.append("")

        elif isinstance(data, list) and data and isinstance(data[0], dict):
            keys = list(data[0].keys())
            display_headers = [
                k for k in keys 
                if k.lower() not in _SUPPRESSED_FIELDS 
                and k.lower() not in {"id", "status_id", "raw_record"}
            ][:6]
            if display_headers:
                if len(data) < 3:
                    for row in data:
                        name = row.get("name") or row.get("title") or row.get("label") or row.get("status_name")
                        parts = []
                        for h in display_headers:
                            if h.lower() in ("name", "title", "label", "status_name"):
                                continue
                            val = row.get(h)
                            if val is not None:
                                if isinstance(val, float) and val > 100:
                                    val_str = f"BHD {val:,.2f}"
                                elif isinstance(val, (int, float)):
                                    val_str = f"{val:,}"
                                else:
                                    val_str = str(val)
                                parts.append(f"**{h.replace('_', ' ').title()}:** {val_str}")
                        prefix = f"- **{name}:** " if name else "- "
                        lines.append(prefix + " | ".join(parts))
                else:
                    header_str = "| " + " | ".join([h.replace("_", " ").title() for h in display_headers]) + " |"
                    sep_str = "| " + " | ".join(["---"] * len(display_headers)) + " |"
                    lines.append(header_str)
                    lines.append(sep_str)

                    for row in data[:25]:
                        row_vals = []
                        for h in display_headers:
                            val = row.get(h, "")
                            if isinstance(val, float):
                                val_str = f"BHD {val:,.2f}" if val > 100 else f"{val:,.2f}"
                            elif isinstance(val, int):
                                val_str = f"{val:,}"
                            elif val is None:
                                val_str = "-"
                            else:
                                val_str = str(val)
                            row_vals.append(val_str.replace("|", "/"))
                        lines.append("| " + " | ".join(row_vals) + " |")

    final_content = "\n".join(lines).strip() if lines else "No matching records were found."

    res = {
        "type": "done",
        "content": final_content,
        "response_mode": "DATA",
        "synthesizer_invoked": False,
        "suggested_questions": _generate_executable_suggestions(primary_tool, primary_intent),
        "raw_tool_results": tool_results,
        "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_name": "data_mode_formatter"}
    }
    from registry.contract_engine import wrap_presentation_intent
    return wrap_presentation_intent(res, user_query, primary_tool)
