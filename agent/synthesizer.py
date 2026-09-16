"""
synthesizer.py — Presentation Layer. Merges backend tool outputs into executive-quality responses.
Phase 3.1.10: Fully presentation-mode driven. Zero raw data policy enforced.
"""
import json
import re
import logging
from typing import List, Dict, Any, Optional
import os
logger = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 15000

# Fields that must NEVER be forwarded to the LLM or shown to the user
_SUPPRESSED_FIELDS = frozenset({
    "raw_sql", "sql", "query", "debug", "trace", "metadata_instructions",
    "tenant_id", "created_at", "updated_at", "internal_id", "_id", "source",
    "capability", "capability_id", "intent", "confidence", "implementation_type",
    "priority", "function_call", "execution_time_ms", "http_status", "error",
    "endpoint", "backend_endpoint", "authoritative", "requested_metric", "returned_metric",
    "status", "metric", "ranking_field", "result_type", "dimension", "operation",
    "missing_information", "execution_contract", "financial_year",
    "created_by", "createdBy", "id", "status_id", "service_line_id", "department_id",
    "customer_id", "client_id", "proposal_id", "project_id", "employee_id",
    "chargeable_hours", "raw_record", "sort_order", "order"
})

def _get_matching_breakdown_array(data: dict, capability_id: str = "") -> tuple:
    """
    Selects the matching breakdown array, title, x_field, and y_field from structured tool response payload.
    Determined purely by validated response metadata and payload data structure (Zero hardcoded report/array key names).
    """
    if not isinstance(data, dict):
        return None, None, None, None

    # 1. Ranking Data / Leaderboard
    ranking_arr = data.get("ranking_data") or data.get("leaderboard")
    if isinstance(ranking_arr, list) and ranking_arr:
        dim_label = (data.get("dimension") or "Item").replace("_", " ").title()
        metric_name = data.get("ranking_field") or data.get("metric") or "count"
        metric_label = (data.get("metric_label") or metric_name).replace("_", " ").title()
        title = f"Top {len(ranking_arr)} {dim_label}s by {metric_label}"
        
        first_item = ranking_arr[0] if isinstance(ranking_arr[0], dict) else {}
        x_field = next((k for k in ["name", "customer_name", "employee_name", "title"] if k in first_item), "name")
        y_field = next((k for k in [metric_name, "amount", "performing", "value", "total", "count"] if k in first_item), "count")
        return (title, ranking_arr, x_field, y_field)

    # 2. Dynamic Array Search (Generic List of Object Records)
    name_fields = ["name", "customer_name", "employee_name", "service_line_name", "department_name", "month", "label", "period", "title", "short_code", "short_name"]
    metric_fields = ["performing", "amount", "revenue", "value", "total", "count", "actual", "total_tokens", "total_queries", "budget", "target"]

    for k, val in data.items():
        if k.startswith("_") or k in ("summary", "date_range"):
            continue
        if isinstance(val, list) and val and len(val) > 0 and isinstance(val[0], dict):
            first_item = val[0]
            matched_x = next((f for f in name_fields if f in first_item), None)
            matched_y = next((f for f in metric_fields if f in first_item), None)

            if matched_x and matched_y:
                clean_title = k.replace("_", " ").replace("ytd", "YTD").replace("gp", "GP").title()
                return (clean_title, val, matched_x, matched_y)

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


TECHNICAL_OMIT_KEYS = frozenset({
    "created_by", "createdBy", "created_at", "createdAt", "updated_at", "updatedAt",
    "id", "service_line_id", "serviceLineId", "sl_id", "department_id", "departmentId",
    "customer_id", "customerId", "client_id", "clientId", "projectId", "project_id",
    "employee_id", "employeeId", "status_id", "statusId", "proposal_id", "proposalId",
    "short_name", "short_code", "chargeable_hours", "sort_order", "order", "month_order"
})

def flatten_nested_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generically flattens nested business entity dicts into clean, top-level business fields.
    Extracts human-readable names and authoritative metrics while suppressing technical IDs and raw JSON objects.
    """
    if not isinstance(row, dict):
        return row

    flat = {}
    
    # 1. Copy top-level scalar keys
    for k, v in row.items():
        if not isinstance(v, (dict, list)):
            flat[k] = v

    # 2. Extract meaningful fields from nested dicts
    for k, v in row.items():
        if isinstance(v, dict):
            name_val = v.get("name") or v.get("customer_name") or v.get("employee_name") or v.get("title") or v.get("label")
            if name_val is not None:
                if k in ("clientDetail", "client", "customer"):
                    flat.setdefault("customer_name", name_val)
                elif k in ("serviceLine", "service_line"):
                    flat.setdefault("service_line", name_val)
                elif k in ("projectStatus", "status"):
                    flat.setdefault("project_status", name_val)
                elif k in ("serviceType", "service_type"):
                    flat.setdefault("service_type", name_val)
                elif k in ("clientRelation", "client_relation"):
                    flat.setdefault("client_relation", name_val)
                elif k in ("managerEmployee", "manager"):
                    flat.setdefault("manager", name_val)
                elif k in ("partnerInventory", "partner"):
                    flat.setdefault("partner", name_val)
                elif k in ("custGroup", "customer_group"):
                    flat.setdefault("customer_group", name_val)
                else:
                    clean_k = re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower()
                    flat.setdefault(clean_k, name_val)

            if k in ("proposal", "jobEstimation", "job_estimation"):
                for prop_metric in ("approved_fees", "agreed_fees", "proposed_fees", "total_costs", "actual_recoverability"):
                    if v.get(prop_metric) is not None:
                        flat.setdefault(prop_metric, v[prop_metric])
                if v.get("recoverability") is not None:
                    flat.setdefault("estimated_recoverability", v["recoverability"])
                if isinstance(v.get("jobEstimation"), dict):
                    je = v["jobEstimation"]
                    for je_metric in ("actual_recoverability", "total_actual_cost", "total_costs"):
                        if je.get(je_metric) is not None:
                            flat.setdefault(je_metric, je[je_metric])
                    if je.get("recoverability") is not None:
                        flat.setdefault("estimated_recoverability", je["recoverability"])

    # 3. Standardize common business field aliases
    if "project_name" not in flat and "name" in flat:
        flat["project_name"] = flat["name"]
    if "customer_name" not in flat and "customer" in flat:
        flat["customer_name"] = flat["customer"]
    if flat.get("actual_recoverability") is None:
        rec = flat.get("actualRecoverability") or flat.get("actual_recoverability_percentage")
        if rec is not None:
            flat["actual_recoverability"] = rec

    return flat


def project_markdown_table(table_text: str, requested_columns: Optional[List[str]]) -> str:
    """
    Ensures that if the user explicitly requested specific projected columns,
    any Markdown table in the text projects ONLY those requested columns.
    If requested_columns is not provided, leaves the table untouched.
    """
    if not requested_columns or not isinstance(requested_columns, list):
        return table_text

    req_norm = [c.lower().replace(" ", "_").strip() for c in requested_columns if c]
    if not req_norm:
        return table_text

    lines = table_text.split("\n")
    header_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and not re.match(r'^\s*\|\s*[-:\s|]+\|\s*$', s):
            if i + 1 < len(lines) and re.match(r'^\s*\|\s*[-:\s|]+\|\s*$', lines[i + 1].strip()):
                header_idx = i
                break

    if header_idx is None:
        return table_text

    raw_headers = [c.strip() for c in lines[header_idx].strip()[1:-1].split("|")]
    keep_indices = []
    for req in req_norm:
        for idx, h in enumerate(raw_headers):
            h_norm = h.lower().replace(" ", "_").strip()
            if req == h_norm or (req in h_norm and len(req) > 4) or (h_norm in req and len(h_norm) > 4):
                if idx not in keep_indices:
                    keep_indices.append(idx)
                    break

    if not keep_indices or len(keep_indices) == len(raw_headers):
        return table_text

    keep_indices.sort()
    new_lines = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            if re.match(r'^\s*\|\s*[-:\s|]+\|\s*$', s):
                new_lines.append("| " + " | ".join(["---"] * len(keep_indices)) + " |")
            else:
                cells = [c.strip() for c in s[1:-1].split("|")]
                if len(cells) >= max(keep_indices) + 1:
                    new_lines.append("| " + " | ".join([cells[k] for k in keep_indices]) + " |")
                else:
                    new_lines.append(line)
        else:
            new_lines.append(line)

    return "\n".join(new_lines)


def trim_report_payload(capability: str, data: Any, max_list_items: int = 15, requested_columns: Optional[List[str]] = None) -> Any:
    """
    Generic metadata-driven payload trimmer.

    Reads `response_schema` and `default_columns` from the Capability Catalog for the given capability to
    identify which top-level fields are business-relevant. Flattens nested business entity objects
    and excludes technical IDs and raw JSON from user-facing summary payloads.
    Honors explicitly requested_columns over default_columns when specified.
    """
    if not isinstance(data, dict):
        if isinstance(data, list):
            flat_list = [flatten_nested_record(item) if isinstance(item, dict) else item for item in data]
            if len(flat_list) > max_list_items:
                return {
                    "total_records": len(flat_list),
                    "summary_sample": flat_list[:max_list_items],
                    "note": f"Dataset contained {len(flat_list)} items; top {max_list_items} shown."
                }
            return flat_list
        return data

    # Fetch schema fields from catalog (gracefully falls back to all fields)
    from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES
    resolved_cap = CAPABILITY_ALIASES.get(capability, capability)
    cap_meta = get_capability_metadata(resolved_cap) or {}
    response_schema = cap_meta.get("response_schema") or {}
    contract = cap_meta.get("response_contract") or {}
    default_cols = contract.get("default_columns") or cap_meta.get("default_columns")
    active_cols = requested_columns if requested_columns else default_cols

    # Internal fields that must never be forwarded to the LLM
    _SUPPRESSED = frozenset({"raw_sql", "sql", "query", "debug", "trace", "metadata_instructions", "created_at", "updated_at", "created_by", "updated_by"})

    effective_max_items = max_list_items
    if isinstance(data, dict):
        if data.get("limit") and isinstance(data.get("limit"), int):
            effective_max_items = max(max_list_items, data["limit"])
        if str(data.get("dimension")).lower().strip() == "month":
            effective_max_items = max(effective_max_items, 12)

    def _clean_node(val: Any) -> Any:
        """Recursively flatten nested records and strip technical/suppressed keys."""
        if isinstance(val, dict):
            flat = flatten_nested_record(val)
            cleaned = {}
            for k, v in flat.items():
                if k in _SUPPRESSED or k in TECHNICAL_OMIT_KEYS:
                    continue
                if active_cols and k not in active_cols and not k.startswith("total_") and not k.startswith("avg_"):
                    continue
                cv = _clean_node(v)
                if cv not in (None, "", "-", [], {}):
                    cleaned[k] = cv
                elif active_cols and k in active_cols:
                    cleaned[k] = "-"
            return cleaned
        elif isinstance(val, list):
            sliced = val[:effective_max_items]
            cleaned_list = [_clean_node(item) for item in sliced]
            return [item for item in cleaned_list if item not in (None, "", "-", [], {})]
        else:
            return val

    summary = {}

    for key, val in data.items():
        if key in _SUPPRESSED:
            continue
        if response_schema and key not in response_schema and not key.startswith("total_") and key not in {
            "status", "records", "data", "rows", "projects", "proposals", "items", "ranking_data", "comparison_periods", "result_type", "operation",
            "requested_metric", "returned_metric", "metric", "metric_type", "metric_label", "dimension",
            "limit", "sort_order", "variance", "variance_pct", "formatted_variance", "authoritative",
            "source", "start_date", "end_date", "summary", "count"
        }:
            continue
        cleaned_val = _clean_node(val)
        if cleaned_val not in (None, "", "-", [], {}):
            summary[key] = cleaned_val
            if isinstance(val, list) and len(val) > effective_max_items:
                summary[f"{key}_total_count"] = len(val)

    if "count" in data and "count" not in summary:
        summary["total_records"] = data["count"]

    return summary if summary else data




def render_generic_ranking_presentation(data: Dict[str, Any], user_query: str = "") -> Optional[str]:
    """
    Metadata-driven executive ranking presentation renderer.
    Renders structured ranking payloads (operation=='ranking', result_type=='ranking_data',
    or payload containing 'ranking_data' / 'leaderboard') into concise executive cards/tables.
    
    Uses generic response metadata contract:
    - operation ("ranking")
    - result_type ("ranking_data", "ranking_table")
    - dimension ("employee", "customer", etc.)
    - metric / metric_label / returned_metric / requested_metric
    - ranking_field ("total_tokens", "total_queries", "total_cost_usd", "revenue", etc.)
    - ranking_data / leaderboard (array of ranked items)
    
    Zero capability names or report questions hardcoded.
    """
    if not isinstance(data, dict):
        return None

    # Resolve inner payload envelope if nested
    inner_payload = data.get("result") if isinstance(data.get("result"), dict) else data
    if isinstance(inner_payload.get("data"), dict):
        payload_data = inner_payload["data"]
    else:
        payload_data = inner_payload

    # Metadata extraction
    op = str(data.get("operation") or inner_payload.get("operation") or payload_data.get("operation") or "").lower()
    res_type = str(data.get("result_type") or inner_payload.get("result_type") or payload_data.get("result_type") or "").lower()
    
    ranking_records = (
        payload_data.get("ranking_data") 
        or payload_data.get("leaderboard") 
        or inner_payload.get("ranking_data") 
        or inner_payload.get("leaderboard")
        or data.get("ranking_data") 
        or data.get("leaderboard")
    )

    if not isinstance(ranking_records, list) or not ranking_records:
        if op != "ranking" and res_type not in ("ranking_data", "ranking_table"):
            return None
        return "No ranking records found."

    # 1. Extract Dimension
    dimension = str(
        data.get("dimension") 
        or inner_payload.get("dimension") 
        or payload_data.get("dimension") 
        or "Item"
    ).replace("_", " ").title()

    # 2. Extract Generic Metric Metadata (Precedence: metric_label -> ranking_field -> returned_metric -> requested_metric -> metric)
    raw_metric_label = (
        data.get("metric_label") or inner_payload.get("metric_label") or payload_data.get("metric_label")
        or data.get("ranking_field") or inner_payload.get("ranking_field") or payload_data.get("ranking_field")
        or data.get("returned_metric") or inner_payload.get("returned_metric") or payload_data.get("returned_metric")
        or data.get("requested_metric") or inner_payload.get("requested_metric") or payload_data.get("requested_metric")
        or data.get("metric") or inner_payload.get("metric") or payload_data.get("metric")
    )

    ranking_field_name = str(
        data.get("ranking_field") or inner_payload.get("ranking_field") or payload_data.get("ranking_field") or ""
    ).strip()

    metric_name = str(
        data.get("metric") or inner_payload.get("metric") or data.get("returned_metric") or inner_payload.get("returned_metric") or ""
    ).strip().lower()

    metric_type = str(
        data.get("metric_type") or inner_payload.get("metric_type") or payload_data.get("metric_type") or ""
    ).strip().lower()

    if not raw_metric_label:
        metric_label = "Count"
    else:
        label_str = str(raw_metric_label).strip()
        if label_str == "total_tokens":
            metric_label = "Total Tokens"
        elif label_str == "total_queries":
            metric_label = "Total Queries"
        elif label_str == "total_cost_usd":
            metric_label = "Total Cost (USD)"
        elif label_str == "total_sessions":
            metric_label = "Total Sessions"
        elif "_" in label_str and " " not in label_str:
            metric_label = label_str.replace("_", " ").title()
        else:
            metric_label = label_str.title()

    # Formatter & Value Resolution Helpers
    def _format_metric_val(val: Any, val_key: str) -> Optional[str]:
        if val is None:
            return None
        val_str = str(val).strip()
        if not val_str or val_str == "-":
            return None
            
        # If already formatted string with currency or units (e.g. "BHD 125,430.50" or "$0.3864")
        if any(c in val_str for c in ("BHD", "$", "USD", "EUR", "GBP")):
            return val_str

        is_monetary = (
            metric_type == "monetary"
            or metric_name in ("revenue", "cost", "total_cost", "total_cost_usd", "budget", "amount", "fee", "fees")
            or val_key in ("revenue", "amount", "cost", "total_cost_usd", "total_budget", "value", "approved_fees")
        )

        if is_monetary:
            try:
                f_val = float(val_str.replace(",", "").replace("$", "").strip())
                if "usd" in val_key or "usd" in metric_name or "$" in val_str:
                    if f_val == 0:
                        return "$0.00"
                    if abs(f_val) < 1.0:
                        return f"${f_val:,.4f}"
                    return f"${f_val:,.2f}"
                else:
                    if f_val == 0:
                        return "BHD 0.00"
                    return f"BHD {f_val:,.2f}"
            except ValueError:
                return val_str

        # Integer / Count / Metric quantity formatting
        try:
            f_val = float(val_str.replace(",", "").strip())
            return f"{int(round(f_val)):,}"
        except ValueError:
            return val_str

    def _resolve_record_value(rec: Dict[str, Any]) -> tuple[Any, Optional[str]]:
        if not isinstance(rec, dict):
            return None, None

        # 1. Prefer formatted_amount if present for monetary display
        if rec.get("formatted_amount") is not None:
            fmt_str = str(rec["formatted_amount"]).strip()
            if fmt_str and fmt_str != "-":
                return rec.get("amount", rec["formatted_amount"]), fmt_str

        # 2. Check candidate keys derived from response contract metadata
        candidate_keys = []
        if ranking_field_name:
            candidate_keys.append(ranking_field_name)
        if metric_name:
            candidate_keys.append(metric_name)
            candidate_keys.append(f"total_{metric_name}")

        # Standard generic value keys
        candidate_keys.extend(["amount", "value", "total", "count", "metric_value"])

        for k in candidate_keys:
            if k in rec and rec[k] is not None:
                v = rec[k]
                formatted = _format_metric_val(v, k)
                if formatted is not None:
                    return v, formatted

        # 3. Fallback: inspect any non-identifier numeric key in record
        ignore_keys = {
            "rank", "id", "employee_id", "customer_id", "project_id", "department_id",
            "name", "employee_name", "customer_name", "entity_name", "title"
        }
        for k, v in rec.items():
            if k.lower() not in ignore_keys and v is not None:
                formatted = _format_metric_val(v, k)
                if formatted is not None:
                    return v, formatted

        return None, None

    # Check single top-1 card answer vs top-N list table
    query_lower = (user_query or "").lower()
    is_top_1 = (
        len(ranking_records) == 1 
        or any(term in query_lower for term in ["the most", "top user", "highest", "top 1", "number one", "no 1", "top employee", "top customer"])
    )

    if is_top_1:
        top_rec = ranking_records[0]
        raw_name = (
            top_rec.get("customer_name")
            or top_rec.get("employee_name") 
            or top_rec.get("name") 
            or top_rec.get("entity_name") 
            or top_rec.get("title") 
            or f"{dimension} #{top_rec.get('customer_id', top_rec.get('employee_id', top_rec.get('id', '')))}"
        )
        entity_name = " ".join(str(raw_name).split())

        raw_val, formatted_val = _resolve_record_value(top_rec)
        if formatted_val is None:
            return f"⚠️ Data contract error: The requested metric '{metric_label}' could not be resolved in the ranking dataset."

        # Generic Executive Card Header
        card_title = f"TOP {dimension.upper()}"

        return (
            f"### {card_title}\n\n"
            f"**{entity_name}**\n\n"
            f"**{formatted_val}**\n"
            f"*{metric_label}*"
        )

    else:
        limit_val = data.get("limit") or inner_payload.get("limit") or payload_data.get("limit")
        if not limit_val:
            import re
            m = re.search(r"top\s+(\d+)", query_lower)
            if m:
                limit_val = int(m.group(1))
            else:
                limit_val = len(ranking_records)
        
        if isinstance(limit_val, int) and limit_val > 0:
            ranking_records = ranking_records[:limit_val]

        cap_id = str(data.get("capability") or inner_payload.get("capability") or "").lower()
        
        if "chatbot" in cap_id or "chatbot" in query_lower:
            card_title = f"Top {limit_val} Chatbot Users by {metric_label}"
        else:
            card_title = f"Top {limit_val} {dimension}s by {metric_label}"

        lines = [f"### {card_title}\n"]
        lines.append(f"| Rank | {dimension} | {metric_label} |")
        lines.append("| --- | --- | --- |")

        for idx, rec in enumerate(ranking_records, start=1):
            rank = rec.get("rank", idx)
            raw_name = (
                rec.get("customer_name")
                or rec.get("employee_name") 
                or rec.get("name") 
                or rec.get("entity_name") 
                or f"{dimension} #{rec.get('customer_id', rec.get('employee_id', rec.get('id', '')))}"
            )
            name = " ".join(str(raw_name).split())

            raw_val, v_str = _resolve_record_value(rec)
            if v_str is None:
                return f"⚠️ Data contract error: The requested metric '{metric_label}' could not be resolved in the ranking dataset."

            lines.append(f"| {rank} | {name} | {v_str} |")

        return "\n".join(lines)


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
    failed_node = next((
        res for res in tool_results
        if res.get("status") in ["error", "unavailable", "AUTH_ERROR", "BACKEND_ERROR", "VALIDATION_ERROR"]
        or res.get("error")
        or (isinstance(res.get("result"), dict) and res.get("result", {}).get("status") in ["AUTH_ERROR", "BACKEND_ERROR", "VALIDATION_ERROR", "ERROR"])
    ), None)

    if failed_node:
        err_payload = failed_node.get("result", {}) if isinstance(failed_node.get("result"), dict) else {}
        fallback_msg = (
            err_payload.get("error_message")
            or err_payload.get("error")
            or failed_node.get("error")
            or "Sorry, I couldn't retrieve the data for the requested comparison. The CRM service returned an error. No comparison has been calculated."
        )

        logger.warning(f"[Synthesizer Envelope Short-Circuit] Output status is non-success for capability '{failed_node.get('capability')}'. Suppressing synthesis.")
        return {
            "type": "done",
            "content": fallback_msg,
            "error_code": err_payload.get("error_code") or "capability_unavailable",
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

    # Step 1.5: Check for generic metadata-driven ranking presentation
    for node in tool_results:
        res_payload = node.get("result") if isinstance(node.get("result"), dict) else node
        ranking_card = render_generic_ranking_presentation(res_payload, original_query)
        if ranking_card:
            return {
                "type": "done",
                "content": ranking_card,
                "response_mode": "DATA",
                "synthesizer_invoked": False,
                "suggested_questions": _generate_executable_suggestions(node.get("capability", "general"), "ranking"),
                "raw_tool_results": tool_results,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_name": "ranking_presentation_formatter"}
            }

    # Step 2: Trim tool results into lightweight summary objects for LLM consumption
    requested_cols = (
        (execution_plan.get("requested_columns") if isinstance(execution_plan, dict) else None)
        or (execution_plan.get("filters", {}).get("requested_columns") if isinstance(execution_plan, dict) and isinstance(execution_plan.get("filters"), dict) else None)
    )
    lightweight_tool_results = []
    for res in tool_results:
        cap = res.get("capability", "")
        raw_res = res.get("result")
        err = res.get("error")
        res_req_cols = (
            requested_cols
            or (res.get("context", {}).get("requested_columns") if isinstance(res.get("context"), dict) else None)
            or (res.get("filters", {}).get("requested_columns") if isinstance(res.get("filters"), dict) else None)
        )
        
        if err:
            lightweight_tool_results.append({"capability": cap, "error": err})
        elif raw_res:
            trimmed = trim_report_payload(cap, raw_res, requested_columns=res_req_cols)
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
            mode_prompt = (
                "MANDATE: State the primary requested metric/answer clearly in the VERY FIRST sentence in bold.\n"
                "Examples:\n"
                "- 'Total revenue for January 2026 was **BHD 322,559.65**.'\n"
                "- 'The total actual recoverability for January 2026 is **92.4%**.'\n"
                "RULES:\n"
                "1. Always put the main answer/metric in bold in the very first sentence.\n"
                "2. Maximum 1-3 direct, executive sentences.\n"
            )
        elif presentation_mode == "COMPARISON":
            mode_prompt = (
                "MANDATE: Provide a clear executive comparison answering whether there was a profit/loss or gain/decline in the VERY FIRST paragraph.\n"
                "STRUCTURE:\n"
                "## Executive Comparison Summary\n"
                "Direct 1-2 sentence statement of overall change (profit/loss, growth rate, variance).\n\n"
                "## Key Variances\n"
                "Clean markdown table comparing Period A vs Period B vs Variance (% / BHD).\n\n"
                "## Executive Insights\n"
                "2-3 concise analytical observations about the comparison.\n"
            )
        elif presentation_mode in ("EXPLANATION", "EXECUTIVE_EXPLANATION"):
            try:
                from .analytical_reasoning import build_analytical_context
                analytical_ctx = build_analytical_context(tool_results, execution_plan)
                analytical_instructions = analytical_ctx.format_as_prompt_instructions()
                logger.info(f"[TRACE_EXECUTION] rows passed to analytical reasoning={len(analytical_ctx.rows)} | analytical reasoning invoked=True | final response mode='{presentation_mode}'")
            except Exception as e:
                logger.warning(f"[Synthesizer] Failed to build analytical context: {e}")
                analytical_instructions = ""

            mode_prompt = (
                "EXECUTIVE ANALYTICAL EXPLANATION MANDATE:\n"
                "Synthesize a concise, direct executive explanation answering WHY the outcome occurred based ONLY on the authoritative pre-computed evidence below.\n\n"
                "STYLE & PRESENTATION RULES:\n"
                "- Write in smooth, natural, professional executive prose (1-2 short paragraphs, or a brief opening statement followed by 2 clean bullets).\n"
                "- Do NOT show 'Conclusion', 'Supporting Evidence', 'Evidence Limitation', 'Observed outcome', 'Magnitude of the shortfall', 'Temporal pattern', 'Drivers and root-cause analysis', 'Key takeaway', 'Stage', 'Level', or similar phrases as visible headings or bold labels.\n"
                "- When explaining a negative shortfall, phrase naturally: 'fell short of target by BHD 169,929.34, a 9.09% shortfall' (or stating total variance of BHD -169,929.34) without double negative phrasing like 'fell short by -BHD X'.\n"
                "- Mention supporting evidence (such as peak shortfall period or key contributing factors) only when useful, without repeating total variance figures.\n"
                "- If secondary driver data is not available in the dataset, state the evidence limitation once, briefly, in a single closing sentence noting that granular drivers (e.g. revenue/cost breakdowns) are not present in this dataset to determine deeper root causes.\n"
                "- If no records are found for a requested customer/entity, state directly and clearly that no authoritative records or revenue data are available for that customer in the CRM dataset for the selected period, so performance cannot be evaluated. NEVER calculate or state 0.00 variance, 0.0% variance, or say 'across all customers'. NEVER assume poor performance when data is absent.\n"
                "- Do NOT invent causes. Attribute drivers only when supported by authoritative data in the context.\n\n"
                f"{analytical_instructions}\n"
            )
        elif presentation_mode in ("EXECUTIVE_BRIEF", "INSIGHT") or is_multi:
            mode_prompt = (
                "MANDATE: Provide a structured, executive-grade response.\n"
                "1. FOR LISTS/RANKINGS WITH 3 OR MORE ITEMS: Render a clean Markdown Table with bold headers.\n"
                "2. FOR 1 OR 2 ITEMS: Render directly as 1-2 clean, bold bullet points.\n"
                "3. PERFORMANCE & GOAL EVALUATION: Explicitly state in the first sentence whether the entity is BEHIND GOAL (shortfall) or ON TRACK / EXCEEDING GOAL (surplus), along with percentage achieved and shortfall/surplus amount.\n"
            )
        elif presentation_mode in ("REPORT_AND_INSIGHT", "REPORT"):
            if requested_cols:
                col_titles = [c.replace("_", " ").title() for c in requested_cols]
                sample_header = "| " + " | ".join(col_titles) + " |"
                mode_prompt = (
                    "MANDATE: REPORT GENERATION (PROJECTED COLUMNS)\n"
                    "1. If authoritative summary metadata exists (e.g. total records/count), mention it briefly (e.g. 'Total Projects: 1,020'). Do NOT invent, calculate, or hallucinate summary totals or averages not present in the backend.\n"
                    f"2. The user explicitly requested ONLY the following columns: {sample_header}. Render the Markdown table with ONLY these requested columns in this exact header projection.\n"
                    "3. Each data record must be its own row in the table.\n"
                    "4. Actual Recoverability: Display the authoritative actual recoverability percentage from the backend (e.g. 20.83%). If actual recoverability is null, missing, unrecorded, or '-', display '-'. NEVER substitute estimated or proposal recoverability into Actual Recoverability. NEVER prefix with BHD.\n"
                    "5. Monetary values: Format monetary amounts with BHD (e.g. BHD 450.00).\n"
                )
            else:
                mode_prompt = (
                    "MANDATE: REPORT GENERATION\n"
                    "1. If authoritative summary metadata exists (e.g. total records/count), mention it briefly (e.g. 'Total Projects: 1,020'). Do NOT invent, calculate, or hallucinate summary totals or averages not present in the backend.\n"
                    "2. Present the dataset as a clean, properly formatted Markdown table with separate columns and standard headers without bold formatting in the header cells (e.g. '| Project Name | Customer Name | Service Line | Approved Fees | Actual Recoverability | Project Status |').\n"
                    "3. Each data record must be its own row in the table.\n"
                    "4. Actual Recoverability: Display the authoritative actual recoverability percentage from the backend (e.g. 20.83%). If actual recoverability is null, missing, unrecorded, or '-', display '-'. NEVER substitute estimated or proposal recoverability into Actual Recoverability. NEVER prefix with BHD.\n"
                    "5. Monetary values: Format approved fees with BHD (e.g. BHD 450.00).\n"
                )
        else:
            mode_prompt = (
                "EXECUTIVE ANSWER MANDATE:\n"
                "1. Answer the user's specific query clearly in 1-3 concise, formatted sentences or bullet points with key figures in bold.\n"
                "2. If tabular/list data is present, format as a clean Markdown table.\n"
            )
        
        system_prompt = (
            "You are an Executive AI Assistant for an Enterprise CRM.\n"
            f"{mode_prompt}\n"
            "STRICT SYSTEM-WIDE RULES:\n"
            "1. Be concise and direct. Eliminate unnecessary fluff, jargon, or monolithic text walls.\n"
            "2. Format monetary values as BHD (e.g., BHD 20,395.46). Never use $ or USD.\n"
            "3. PERCENTAGES: Percentages are ALWAYS formatted with % (e.g. 44.99% or 92.4%). NEVER prefix percentages with BHD or currency symbols!\n"
            "4. NEVER fabricate or hallucinate numbers or business causes. Only use data provided in tool results.\n"
            "5. NEVER expose JSON, SQL, Python dicts, internal technical terms, or internal dataset source names.\n"
            "6. Clean up metric names: Convert snake_case keys into Title Case.\n"
            "7. CRITICAL: Do NOT output <think> tags, 'Thinking Process:', or reasoning blocks. Start IMMEDIATELY with the response text."
        )
        
        # Use trimmed, normalized, contract-flattened results for LLM prompt
        raw_json_str = serialized_results
        if len(raw_json_str) > 12000:
            raw_json_str = raw_json_str[:12000] + "\n... [TRUNCATED DATA DUE TO PAYLOAD SIZE LIMIT]"

        prompt = f"User Query: {original_query}\n\nTool Results:\n{raw_json_str}\n\nFormat this into a clear, professional answer."
        
        synth_token_usage = {}
        try:
            from agent.pseudonymizer import prepare_for_external_llm, unmask_data, PrivacySecurityError
            privacy_res = prepare_for_external_llm(prompt)
            if not privacy_res.safe:
                logger.error(f"Privacy validation failed in synthesizer: {privacy_res.blocked_reason}")
                final_text = "Data formatting paused due to security privacy policy enforcement."
            else:
                try:
                    req_id = (execution_plan or {}).get("request_id") or "unknown"
                    logger.info(f"[LLM_CALL] stage=synthesizer request_id={req_id}")
                    response = await llm_client.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=privacy_res.masked_text)])
                except Exception as primary_err:
                    err_str = str(primary_err)
                    if "429" in err_str or "rate_limit" in err_str.lower() or "quota" in err_str.lower():
                        fallback_model = os.getenv("FALLBACK_MODEL") or os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL")
                        logger.warning(f"[Synthesizer] Primary model rate-limited (429). Retrying with {fallback_model} fallback model...")
                        from config.llm_factory import get_llm
                        fallback_llm = get_llm(model_name=fallback_model, stage="synthesizer")
                        logger.info(f"[LLM_CALL] stage=synthesizer_retry request_id={req_id}")
                        response = await fallback_llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=privacy_res.masked_text)])
                    else:
                        raise primary_err

                raw_unmasked = unmask_data(response.content, privacy_res.token_mapping)
                privacy_res.clear_mapping()

                from config.llm_factory import clean_think_tags
                final_text = clean_think_tags(raw_unmasked)

                # Post-process: convert any remaining $ or USD currency symbols to BHD & clean up unformatted numbers/keys
                import re
                final_text = re.sub(r'\$(\d)', r'BHD \1', final_text)
                final_text = re.sub(r'\bUSD\b', r'BHD', final_text)

                # Clean up table rows: strip bold markdown in headers and format snake_case cells
                cleaned_table_lines = []
                for line in final_text.split("\n"):
                    s_line = line.strip()
                    if s_line.startswith("|") and s_line.endswith("|"):
                        if not re.match(r'^\s*\|\s*[-:\s|]+\|\s*$', s_line):
                            line = re.sub(r'[*_]{1,2}([^*_|\n]+)[*_]{1,2}', r'\1', line)
                            cells = [c.strip() for c in line.strip()[1:-1].split("|")]
                            cleaned_cells = [re.sub(r'^[a-z0-9]+(?:_[a-z0-9]+)+$', lambda m: m.group(0).replace('_', ' ').title(), c) for c in cells]
                            line = "| " + " | ".join(cleaned_cells) + " |"
                    cleaned_table_lines.append(line)
                final_text = "\n".join(cleaned_table_lines)
                if requested_cols:
                    final_text = project_markdown_table(final_text, requested_cols)

                def _format_float(m):
                    try:
                        val = float(m.group(0))
                        return f"{val:,.2f}"
                    except Exception:
                        return m.group(0)
                final_text = re.sub(r'\b\d{4,}\.\d{3,}\b', _format_float, final_text)

                # Defensive post-processing: remove any leaked LEVEL, STAGE, or internal analytical stage markers/labels
                final_text = re.sub(r'(?i)\*{0,2}\b(?:LEVEL|STAGE)\s*\d+\s*[-–—:]*\s*', '', final_text)
                final_text = re.sub(r'(?i)(?:^|\n)\s*#{1,4}\s*(?:Conclusion|Supporting Evidence|Evidence Limitation|Observed outcome|Magnitude of the shortfall|Temporal pattern|Drivers and root-cause analysis|Key takeaway)[\s:]*', '\n', final_text)
                final_text = re.sub(r'(?i)\*\*(?:Conclusion|Supporting Evidence|Evidence Limitation|Observed outcome|Magnitude of the shortfall|Temporal pattern|Drivers and root-cause analysis|Key takeaway):\*\*\s*', '', final_text)
                final_text = re.sub(r'(?i)\b(?:Conclusion|Supporting Evidence|Evidence Limitation):\s*', '', final_text)

                from config.llm_factory import extract_token_usage
                synth_token_usage = extract_token_usage(response)
                if not synth_token_usage.get("model_name"):
                    synth_token_usage["model_name"] = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "openai/gpt-oss-20b"
                logger.info(f"[Synthesizer Tokens] Model: {synth_token_usage['model_name']} | In: {synth_token_usage['input_tokens']} | Out: {synth_token_usage['output_tokens']} | Total: {synth_token_usage['total_tokens']}")

        except Exception as e:
            logger.error(f"Dynamic synthesis failed ({e}). Falling back to 0-token DATA mode formatter.")
            fallback_res = format_data_response(original_query, tool_results, execution_plan=execution_plan)
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
            dyn_title, dyn_arr, dyn_x, dyn_y = _get_matching_breakdown_array(data, tool_name)
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





def format_data_response(user_query: str, tool_results: List[Dict[str, Any]], execution_plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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

    req_cols = (
        (execution_plan.get("requested_columns") if isinstance(execution_plan, dict) else None)
        or (execution_plan.get("filters", {}).get("requested_columns") if isinstance(execution_plan, dict) and isinstance(execution_plan.get("filters"), dict) else None)
    )
    if not req_cols and user_query:
        try:
            from agent.intent_normalizer import to_canonical_intent
            ci = to_canonical_intent(execution_plan or {}, user_query)
            if ci.filters and "requested_columns" in ci.filters:
                req_cols = ci.filters["requested_columns"]
        except Exception:
            pass

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
            ranking_card = render_generic_ranking_presentation(data, user_query)
            if ranking_card:
                lines.append(ranking_card)
                continue

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
            dyn_title, dyn_arr, _, _ = _get_matching_breakdown_array(data)
            if dyn_title and dyn_arr:
                sub_lists.append((dyn_title, dyn_arr))
            elif isinstance(data, dict):
                for k, v in data.items():
                    if k.startswith("_") or k in ("summary", "date_range"):
                        continue
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        clean_t = k.replace("_", " ").replace("ytd", "YTD").replace("gp", "GP").title()
                        sub_lists.append((clean_t, v))
                        break

            for title, records in sub_lists:
                if records and isinstance(records[0], dict):
                    from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES
                    from engine.renderer_engine import _format_cell_val, _format_trend_val, flatten_nested_record, TECHNICAL_OMIT_KEYS
                    resolved_cap = CAPABILITY_ALIASES.get(primary_tool, primary_tool)
                    cap_meta = get_capability_metadata(resolved_cap) or {}
                    contract = cap_meta.get("response_contract") or {}
                    default_cols = contract.get("default_columns") or cap_meta.get("default_columns")
                    requested_cols = (
                        req_cols
                        or (data.get("requested_columns") if isinstance(data, dict) else None)
                    )
                    omit_keys = set(_SUPPRESSED_FIELDS) | set(TECHNICAL_OMIT_KEYS)

                    flat_clean_records = [
                        flatten_nested_record(r) for r in records
                        if isinstance(r, dict)
                        and not (r.get("totalEntries") == 0 and r.get("totalBudget") is None)
                        and not (r.get("count") == 0 and r.get("proposed_fees") == 0)
                    ]
                    if not flat_clean_records:
                        flat_clean_records = [flatten_nested_record(r) for r in records[:10] if isinstance(r, dict)]

                    if flat_clean_records:
                        first_r = flat_clean_records[0]
                        if requested_cols and any(c in first_r for c in requested_cols):
                            display_headers = [c for c in requested_cols if c in first_r or any(c in r for r in flat_clean_records)]
                        elif default_cols and any(c in first_r for c in default_cols):
                            display_headers = [c for c in default_cols if c in first_r or any(c in r for r in flat_clean_records)]
                        else:
                            display_headers = [
                                k for k in first_r.keys()
                                if k.lower() not in omit_keys
                                and not k.startswith("_")
                                and not isinstance(first_r.get(k), (dict, list))
                            ][:6]

                        if display_headers:
                            lines.append(f"### {title}")
                            if len(flat_clean_records) == 1:
                                from engine.renderer_engine import _render_single_entity_kpi
                                lines.append(_render_single_entity_kpi(flat_clean_records[0], title))
                            elif len(flat_clean_records) < 3:
                                for row in flat_clean_records:
                                    name = row.get("project_name") or row.get("name") or row.get("title") or row.get("label") or row.get("status_name")
                                    parts = []
                                    for h in display_headers:
                                        if h.lower() in ("name", "project_name", "title", "label", "status_name"):
                                            continue
                                        val = row.get(h)
                                        if val is not None:
                                            parts.append(f"**{h.replace('_', ' ').title()}:** {_format_cell_val(val, h)}")
                                    prefix = f"- **{name}:** " if name else "- "
                                    lines.append(prefix + " • ".join(parts))
                                lines.append("")
                            else:
                                header_str = "| " + " | ".join([h.replace("_", " ").title() for h in display_headers]) + " |"
                                sep_str = "| " + " | ".join(["---"] * len(display_headers)) + " |"
                                lines.append(header_str)
                                lines.append(sep_str)

                                for row in flat_clean_records[:25]:
                                    row_vals = []
                                    for h in display_headers:
                                        val = row.get(h, "")
                                        if any(p in h.lower() for p in ["trend", "growth_pct", "rate_pct"]):
                                            row_vals.append(_format_trend_val(val))
                                        else:
                                            row_vals.append(_format_cell_val(val, h).replace("|", "/"))
                                    lines.append("| " + " | ".join(row_vals) + " |")
                                lines.append("")

        elif isinstance(data, list) and data and isinstance(data[0], dict):
            from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES
            from engine.renderer_engine import _format_cell_val, _format_trend_val, flatten_nested_record, TECHNICAL_OMIT_KEYS
            resolved_cap = CAPABILITY_ALIASES.get(primary_tool, primary_tool)
            cap_meta = get_capability_metadata(resolved_cap) or {}
            contract = cap_meta.get("response_contract") or {}
            default_cols = contract.get("default_columns") or cap_meta.get("default_columns")
            requested_cols = (
                req_cols
                or (data.get("requested_columns") if isinstance(data, dict) else None)
            )
            omit_keys = set(_SUPPRESSED_FIELDS) | set(TECHNICAL_OMIT_KEYS)

            flat_data = [flatten_nested_record(r) for r in data if isinstance(r, dict)]
            if len(flat_data) == 1:
                from engine.renderer_engine import _render_single_entity_kpi
                lines.append(_render_single_entity_kpi(flat_data[0], cap.replace("_", " ").title()))
            elif flat_data:
                first_r = flat_data[0]
                if requested_cols and any(c in first_r for c in requested_cols):
                    display_headers = [c for c in requested_cols if c in first_r or any(c in r for r in flat_data)]
                elif default_cols and any(c in first_r for c in default_cols):
                    display_headers = [c for c in default_cols if c in first_r or any(c in r for r in flat_data)]
                else:
                    display_headers = [
                        k for k in first_r.keys()
                        if k.lower() not in omit_keys
                        and not k.startswith("_")
                        and not isinstance(first_r.get(k), (dict, list))
                    ][:6]

                if display_headers:
                    header_str = "| " + " | ".join([h.replace("_", " ").title() for h in display_headers]) + " |"
                    sep_str = "| " + " | ".join(["---"] * len(display_headers)) + " |"
                    lines.append(header_str)
                    lines.append(sep_str)

                    for row in flat_data[:25]:
                        row_vals = []
                        for h in display_headers:
                            val = row.get(h, "")
                            if any(p in h.lower() for p in ["trend", "growth_pct", "rate_pct"]):
                                row_vals.append(_format_trend_val(val))
                            else:
                                row_vals.append(_format_cell_val(val, h).replace("|", "/"))
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
