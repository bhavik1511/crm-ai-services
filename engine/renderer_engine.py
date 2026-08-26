"""
renderer_engine.py — Deterministic Renderer Engine
===================================================
Contract-driven deterministic response renderer. Formats structured backend data
into Markdown tables, KPI cards, summary key-value lists, and report envelopes
with ZERO LLM tokens.

Strictly metadata-driven:
ZERO capability-specific if/else statements.
ZERO report-specific switch statements.
Renders strictly using response_contract + response_schema + payload shape.
"""

import logging
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from registry.metadata_registry import get_registry, register_renderer

logger = logging.getLogger(__name__)

IGNORED_KEYS = {
    "operation", "dimension", "metric", "aggregation", 
    "query_operation", "execution_contract", "status_filter", 
    "temporal_scope", "status", "rejection_reasons", "full_capability_spec",
    "capability", "capability_id", "intent", "confidence", "source",
    "implementation_type", "priority", "function_call", "execution_time_ms",
    "http_status", "error", "endpoint", "backend_endpoint", "authoritative",
    "requested_metric", "returned_metric"
}

def _format_date_val(val: Any) -> str:
    """Helper to convert ISO timestamps to clean human-readable dates."""
    if isinstance(val, str) and ("T" in val or "-" in val):
        iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2}))?", val)
        if iso_match:
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.strftime("%d %b %Y")
            except Exception:
                pass
    return str(val)

def _format_cell_val(val: Any, col_name: str = "") -> str:
    """Format cell values with appropriate currency, date, integer count, or string formatting."""
    if val is None or val == "":
        return "-"

    col_lower = str(col_name).lower().strip()

    # 1. Percentage / Recoverability fields — strictly NO currency prefix
    if any(k in col_lower for k in ["pct", "percent", "percentage", "rate", "recoverability", "margin"]):
        if isinstance(val, (int, float)):
            return f"{val:.2f}%"
        if isinstance(val, str) and not val.endswith("%"):
            try:
                f_val = float(val.replace(",", "").strip())
                return f"{f_val:.2f}%"
            except ValueError:
                pass
        return str(val)

    # 2. Integer / Count / ID fields — strictly NO currency formatting
    if any(k in col_lower for k in ["count", "records", "total_records", "quantity", "id", "number", "year", "projects", "proposals", "total_projects", "total_proposals", "strictly_active_projects_count"]):
        if isinstance(val, (int, float)):
            return f"{int(val):,}"
        if isinstance(val, str) and val.isdigit():
            return f"{int(val):,}"
        return str(val)

    # 3. Date columns
    if any(k in col_lower for k in ["date", "created_at", "updated_at", "created at", "period", "month", "time"]):
        return _format_date_val(val)

    # 4. Financial / Currency columns
    is_currency_col = any(k in col_lower for k in [
        "amount", "receivables", "revenue", "fee", "cost", "budget", 
        "net", "gross", "billing", "metric_val", "price", "due", "paid",
        "balance_to_achieve", "performing", "target", "secured_business", "secured"
    ]) and not any(k in col_lower for k in ["count", "records", "id", "projects", "proposals"])

    if isinstance(val, (int, float)):
        if is_currency_col or (isinstance(val, float) and val > 100):
            return f"BHD {val:,.2f}"
        if isinstance(val, float):
            return f"{val:,.2f}"
        return f"{val:,}"

    if isinstance(val, str):
        try:
            f_val = float(val.replace(",", "").replace("BHD", "").strip())
            if is_currency_col:
                return f"BHD {f_val:,.2f}"
        except ValueError:
            pass

        return _format_date_val(val)

    return str(val)


def _format_trend_val(val: Any) -> str:
    """
    Generic visual trend/percentage formatter.
    Positive: 🟢 ↑ +12.50%
    Negative: 🔴 ↓ -22.74%
    Zero/Neutral: 🟡 → 0.00%
    """
    if val is None or val == "":
        return "-"

    val_str = str(val).strip()
    try:
        clean_str = val_str.rstrip("%").lstrip("+")
        num_val = float(clean_str)
    except Exception:
        return val_str

    formatted_pct = f"{abs(num_val):,.2f}%"

    if num_val > 0.0001:
        return f"🟢 ↑ +{formatted_pct}"
    elif num_val < -0.0001:
        return f"🔴 ↓ -{formatted_pct}"
    else:
        return f"🟡 → 0.00%"


def _format_progress_bar(actual: float, target: float) -> Optional[str]:
    """
    Generates a compact visual progress indicator against target from actual numeric values.
    Example: Progress: [████████████░░░] 77.3%
    """
    if not isinstance(actual, (int, float)) or not isinstance(target, (int, float)):
        return None
    if target <= 0:
        return None

    pct = (actual / target) * 100.0
    total_blocks = 15
    filled_blocks = max(0, min(total_blocks, int(round((pct / 100.0) * total_blocks))))
    empty_blocks = total_blocks - filled_blocks

    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"**Progress:** [{bar}] **{pct:.1f}%**"


def _render_single_entity_kpi(row: Dict[str, Any], title: str) -> str:
    """
    Renders a single-entity dataset into an executive KPI layout with:
    - Dynamic Contextual Title (e.g., GP Performance — Audit)
    - Primary Metric & Target Pairs
    - Target Progress Bar Visualization (if actual & target exist)
    - Visual Trend Indicator (🟢 ↑ +X%, 🔴 ↓ -Y%, 🟡 → 0%)
    - Secondary Financial / Quantitative Metrics
    - Suppresses internal ID / short name fields
    """
    if not isinstance(row, dict) or not row:
        return f"### 📊 {title}\n\nNo records found."

    # 1. Identify Primary Entity Name / Context Field
    entity_name = None
    entity_key = None
    name_priority_keys = [
        "service_line_name", "department_name", "customer_name", "project_name",
        "employee_name", "entity_name", "service_line", "department", "customer",
        "project", "name", "title", "label"
    ]
    for k in name_priority_keys:
        if row.get(k) and isinstance(row.get(k), str) and str(row.get(k)).strip():
            entity_name = str(row[k]).strip()
            entity_key = k
            break

    # Construct clean title
    clean_base_title = title.replace("Gp Performance", "GP Performance")
    if entity_name and entity_name.lower() not in clean_base_title.lower():
        display_title = f"{clean_base_title} — {entity_name}"
    else:
        display_title = clean_base_title

    lines = [f"### 📊 {display_title}\n"]

    # 2. Extract Primary Performing, Target, and Trend metrics
    performing_val = None
    performing_key = None
    target_val = None
    target_key = None
    trend_val = None
    trend_key = None

    for k, v in row.items():
        k_lower = k.lower()
        if k_lower in IGNORED_KEYS or k.startswith("_"):
            continue
        if performing_val is None and any(p in k_lower for p in ["performing", "actual", "achieved"]):
            if isinstance(v, (int, float)):
                performing_val = v
                performing_key = k
        elif target_val is None and any(p in k_lower for p in ["target", "budget", "goal", "quota"]):
            if isinstance(v, (int, float)):
                target_val = v
                target_key = k
        elif trend_val is None and any(p in k_lower for p in ["trend", "variance", "growth", "change_pct", "pct"]):
            trend_val = v
            trend_key = k

    # 3. Render Primary Performance & Target Section
    has_primary_section = False
    if performing_val is not None or target_val is not None:
        has_primary_section = True
        cards = []
        if performing_key and performing_val is not None:
            cards.append(f"**{performing_key.replace('_', ' ').title()}:** **{_format_cell_val(performing_val, performing_key)}**")
        if target_key and target_val is not None:
            cards.append(f"**{target_key.replace('_', ' ').title()}:** **{_format_cell_val(target_val, target_key)}**")

        if cards:
            lines.append(" &nbsp;&nbsp;│&nbsp;&nbsp; ".join(cards))

    # 4. Render Target Progress Bar Visualization
    if performing_val is not None and target_val is not None and isinstance(performing_val, (int, float)) and isinstance(target_val, (int, float)) and target_val > 0:
        progress_str = _format_progress_bar(performing_val, target_val)
        if progress_str:
            lines.append(f"\n{progress_str}")

    # 5. Render Visual Trend Indicator
    if trend_val is not None:
        trend_formatted = _format_trend_val(trend_val)
        if trend_formatted:
            trend_label = trend_key.replace("_", " ").title() if trend_key else "Trend"
            lines.append(f"\n- **{trend_label}:** {trend_formatted}")

    if has_primary_section or trend_val is not None:
        lines.append("")

    # 6. Secondary / Remaining Metrics
    secondary_bullets = []
    keys_to_skip = {
        performing_key, target_key, trend_key, entity_key,
        "service_line_id", "serviceLineId", "sl_id",
        "department_id", "departmentId",
        "customer_id", "customerId",
        "project_id", "projectId",
        "employee_id", "employeeId",
        "id", "short_name", "short_code"
    }

    for k, v in row.items():
        k_lower = k.lower()
        if k.startswith("_") or k_lower in IGNORED_KEYS or k in keys_to_skip or k_lower in keys_to_skip:
            continue

        if any(p in k_lower for p in ["trend", "pct", "percentage", "variance", "growth", "rate"]):
            formatted_val = _format_trend_val(v)
        else:
            formatted_val = _format_cell_val(v, k)

        label = k.replace("_", " ").title()
        secondary_bullets.append(f"- **{label}:** {formatted_val}")

    if secondary_bullets:
        lines.append("#### 📌 Key Metrics")
        lines.extend(secondary_bullets)

    return "\n".join(lines).strip()


@register_renderer("generic_deterministic_renderer")
def render_deterministic_response(
    capability_metadata: Dict[str, Any], payload_envelope: Dict[str, Any]
) -> str:
    """
    Metadata-driven deterministic renderer.
    Renders structured payload envelopes into clean executive Markdown.
    Uses 0 LLM tokens.
    """
    if not payload_envelope or not isinstance(payload_envelope, dict):
        return "No data returned from system."

    status = payload_envelope.get("status")
    if status in ["error", "AUTH_ERROR", "BACKEND_ERROR", "VALIDATION_ERROR"]:
        payload_data = payload_envelope.get("payload", {})
        err_msg = (
            (payload_data.get("error_message") if isinstance(payload_data, dict) else None)
            or payload_envelope.get("error_message")
            or "Sorry, I couldn't retrieve the data for the requested comparison. The CRM service returned an error. No comparison has been calculated."
        )
        return err_msg

    payload = payload_envelope.get("payload")
    if payload is None:
        if "comparison_periods" in payload_envelope or payload_envelope.get("result_type") == "comparison_table":
            payload = payload_envelope
        else:
            return "The requested dataset is empty."

    cap_id = capability_metadata.get("id", "")

    # Dynamic Title derived from Query Operation if present
    query_op = payload_envelope.get("query_operation", {})
    if isinstance(query_op, dict) and query_op.get("operation"):
        op_type = query_op.get("operation")
        limit = query_op.get("limit")
        dim = str(query_op.get("dimension") or "Category").replace("_", " ").title()
        metric = str(query_op.get("metric") or "Metric").replace("_", " ").title()

        if op_type == "ranking" and limit:
            title = f"Top {limit} {dim}s by {metric}"
        elif op_type == "breakdown":
            title = f"{metric} by {dim}"
        elif op_type == "trend":
            title = f"Monthly {metric} Trend"
        elif op_type == "comparison":
            title = f"{metric} Comparison"
        elif op_type == "summary":
            title = f"{metric} Summary"
        elif op_type == "aggregate":
            title = f"Total {metric}"
        else:
            title = cap_id.replace("_", " ").title()
    else:
        title = cap_id.replace("_", " ").title()

    lines = [f"### 📊 {title}\n"]

    # 1. Handle List Payload Shape
    if isinstance(payload, list):
        if not payload:
            return f"### 📊 {title}\n\nNo records found."

        # Single Entity List (1 row) -> Executive KPI Card Presentation
        if len(payload) == 1 and isinstance(payload[0], dict):
            return _render_single_entity_kpi(payload[0], title)

        # Multi-Entity List (2+ rows) -> Comparison Table Presentation
        first_row = payload[0]
        if isinstance(first_row, dict):
            has_name = any(k in first_row for k in ["service_line_name", "department_name", "customer_name", "project_name", "employee_name", "name"])
            omit_keys = set(IGNORED_KEYS)
            if has_name:
                omit_keys.update({"service_line_id", "serviceLineId", "sl_id", "department_id", "departmentId", "customer_id", "projectId", "project_id", "employee_id", "id", "short_name", "short_code"})

            headers = [k for k in first_row.keys() if not k.startswith("_") and k.lower() not in omit_keys]
            lines.append("| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |")
            lines.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in payload[:15]:
                row_vals = []
                for h in headers:
                    val = row.get(h, "")
                    if any(p in h.lower() for p in ["trend", "variance", "pct", "percentage", "growth", "rate"]):
                        row_vals.append(_format_trend_val(val))
                    else:
                        row_vals.append(_format_cell_val(val, h))
                lines.append("| " + " | ".join(row_vals) + " |")

            if len(payload) > 15:
                lines.append(f"\n_*Showing top 15 of {len(payload)} records.*_")
        else:
            for item in payload[:10]:
                lines.append(f"- {_format_cell_val(item)}")

        return "\n".join(lines)

    # 2. Handle Dict Payload Shape
    if isinstance(payload, dict):
        # 2a. Dedicated Ranking Table Renderer
        if payload.get("result_type") == "ranking_table" or "ranking_data" in payload:
            dim_title = str(payload.get("dimension") or "Category").replace("_", " ").title()
            metric_title = str(payload.get("metric_label") or payload.get("metric") or "Revenue").replace("_", " ").title()
            ranking_data = payload.get("ranking_data") or []
            limit_val = payload.get("limit") or len(ranking_data)

            lines = [f"### 📊 Top {limit_val} {dim_title}s by {metric_title}\n"]
            lines.append(f"| Rank | {dim_title} | {metric_title} |")
            lines.append("| --- | --- | --- |")
            for item in ranking_data:
                rank = item.get("rank", "-")
                name = item.get("entity_name", "-")
                amt_str = item.get("formatted_amount") or _format_cell_val(item.get("amount"), "amount")
                lines.append(f"| {rank} | {name} | {amt_str} |")
            return "\n".join(lines)

        # 2b. Dedicated Comparison Table Renderer
        if payload.get("result_type") == "comparison_table" or "comparison_periods" in payload:
            if payload.get("status") in ["AUTH_ERROR", "BACKEND_ERROR", "VALIDATION_ERROR", "PERIOD_MISMATCH"] or payload.get("is_valid") is False:
                return payload.get("error_message") or "Sorry, I couldn't retrieve the revenue data needed for the requested comparison. The CRM data service could not return the required data, so no comparison was calculated."

            metric_title = str(payload.get("metric") or "Revenue").replace("_", " ").title()
            periods = payload.get("comparison_periods") or []

            lines = [f"### 📊 Multi-Period {metric_title} Comparison\n"]
            lines.append(f"| Period | Start Date | End Date | {metric_title} |")
            lines.append("| --- | --- | --- | --- |")
            for p in periods:
                label = p.get("period") or p.get("label") or "-"
                s_date = _format_date_val(p.get("start_date", ""))
                e_date = _format_date_val(p.get("end_date", ""))
                amt_str = p.get("formatted_amount") or _format_cell_val(p.get("amount"), "amount")
                lines.append(f"| {label} | {s_date} | {e_date} | {amt_str} |")

            variance_str = payload.get("formatted_variance")
            if variance_str:
                lines.append(f"\n- **Variance ({periods[0].get('period', 'P1')} vs {periods[-1].get('period', 'P2')}):** {variance_str}")
            return "\n".join(lines)

        # Check for nested list of rows/records/data/items
        rows_list = payload.get("rows") or payload.get("records") or payload.get("data") or payload.get("items") or payload.get("projects") or payload.get("proposals")
        if isinstance(rows_list, list):
            if len(rows_list) == 1 and isinstance(rows_list[0], dict):
                return _render_single_entity_kpi(rows_list[0], title)
            elif len(rows_list) > 1:
                first_row = rows_list[0]
                if isinstance(first_row, dict):
                    has_name = any(k in first_row for k in ["service_line_name", "department_name", "customer_name", "project_name", "employee_name", "name"])
                    omit_keys = set(IGNORED_KEYS)
                    if has_name:
                        omit_keys.update({"service_line_id", "serviceLineId", "sl_id", "department_id", "departmentId", "customer_id", "projectId", "project_id", "employee_id", "id", "short_name", "short_code"})

                    headers = [k for k in first_row.keys() if not k.startswith("_") and k.lower() not in omit_keys]
                    lines = [f"### 📊 {title}\n"]
                    lines.append("| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |")
                    lines.append("| " + " | ".join("---" for _ in headers) + " |")
                    for row in rows_list[:15]:
                        row_vals = []
                        for h in headers:
                            val = row.get(h, "")
                            if any(p in h.lower() for p in ["trend", "variance", "pct", "percentage", "growth", "rate"]):
                                row_vals.append(_format_trend_val(val))
                            else:
                                row_vals.append(_format_cell_val(val, h))
                        lines.append("| " + " | ".join(row_vals) + " |")

                    if len(rows_list) > 15:
                        lines.append(f"\n_*Showing top 15 of {len(rows_list)} records.*_")
                    return "\n".join(lines)

        # Check if dict itself is a single-entity record with metric fields
        has_metric_keys = any(k.lower() in ["performing", "target", "revenue", "trend", "actual", "secured_business", "balance_to_achieve"] for k in payload.keys())
        if has_metric_keys:
            return _render_single_entity_kpi(payload, title)

        scalar_fields = []
        table_fields = []

        for k, v in payload.items():
            if k.startswith("_") or k.lower() in IGNORED_KEYS:
                continue
            if isinstance(v, list):
                table_fields.append((k, v))
            elif isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if not sub_k.startswith("_") and sub_k.lower() not in IGNORED_KEYS and not isinstance(sub_v, (dict, list)):
                        scalar_fields.append((f"{k} - {sub_k}", sub_v))
            else:
                scalar_fields.append((k, v))

        # Render Summary Cards / Key-Value List without monospace code backticks
        if scalar_fields:
            for field_name, val in scalar_fields:
                formatted_name = field_name.replace("_", " ").title()
                if any(p in field_name.lower() for p in ["trend", "variance", "pct", "percentage", "growth", "rate"]):
                    formatted_val = _format_trend_val(val)
                else:
                    formatted_val = _format_cell_val(val, field_name)
                lines.append(f"- **{formatted_name}:** {formatted_val}")
            lines.append("")

        # Render nested tables if present
        for tbl_name, tbl_rows in table_fields:
            tbl_title = tbl_name.replace("_", " ").title()
            lines.append(f"#### {tbl_title}")
            if tbl_rows and isinstance(tbl_rows[0], dict):
                headers = [k for k in tbl_rows[0].keys() if not k.startswith("_") and k.lower() not in IGNORED_KEYS]
                lines.append("| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                for r in tbl_rows[:10]:
                    row_vals = []
                    for h in headers:
                        val = r.get(h, "")
                        if any(p in h.lower() for p in ["trend", "variance", "pct", "percentage", "growth", "rate"]):
                            row_vals.append(_format_trend_val(val))
                        else:
                            row_vals.append(_format_cell_val(val, h))
                    lines.append("| " + " | ".join(row_vals) + " |")
                if len(tbl_rows) > 10:
                    lines.append(f"\n_*Showing top 10 of {len(tbl_rows)} entries.*_\n")
            lines.append("")

        return "\n".join(lines).strip()

    return f"### 📊 {title}\n\n{str(payload)}"

@register_renderer("kpi_summary")
def render_kpi_summary_response(
    capability_metadata: Dict[str, Any], payload_envelope: Dict[str, Any]
) -> str:
    """
    Dedicated Executive KPI Summary Report Renderer.
    Renders structured KPI targets, actuals, proposals, and project status distributions.
    """
    if not payload_envelope or not isinstance(payload_envelope, dict):
        return "No KPI data returned from system."

    status = payload_envelope.get("status")
    if status == "error":
        payload_data = payload_envelope.get("payload", {})
        err_msg = payload_data.get("error_message") if isinstance(payload_data, dict) else "Information unavailable."
        return f"⚠️ {err_msg}"

    payload = payload_envelope.get("payload") if isinstance(payload_envelope.get("payload"), dict) else (payload_envelope.get("data") if isinstance(payload_envelope.get("data"), dict) else payload_envelope)
    if not payload or not isinstance(payload, dict):
        return "The requested KPI dataset is empty."

    # Extract summary dictionary and date range flexibly
    summary = {}
    if isinstance(payload.get("summary"), dict):
        summary.update(payload.get("summary"))
    if isinstance(payload.get("data"), dict):
        data_obj = payload.get("data")
        if isinstance(data_obj.get("summary"), dict):
            summary.update(data_obj.get("summary"))
        summary.update({k: v for k, v in data_obj.items() if k != "summary"})
    if not summary:
        summary = payload

    emp_name = summary.get("employee_name") or summary.get("user_name") or "Organization Aggregate"
    date_range = payload.get("date_range") or summary.get("date_range") or {}
    start_date_str = _format_date_val(date_range.get("start", "")) if isinstance(date_range, dict) else ""
    end_date_str = _format_date_val(date_range.get("end", "")) if isinstance(date_range, dict) else ""

    period_str = f"{start_date_str} – {end_date_str}" if start_date_str and end_date_str else "Current Financial Year"

    # Extract summary cards map directly from normalized contract payload
    summary_cards_data = payload.get("summary_cards") or summary.get("summary_cards") or []
    cards_map = {}
    if isinstance(summary_cards_data, list):
        cards_map = {c.get("key"): c.get("value") for c in summary_cards_data if isinstance(c, dict) and "key" in c}
    elif isinstance(summary_cards_data, dict):
        cards_map = summary_cards_data

    # Inspect billing table total row for targets and variance if present
    billing_table = payload.get("billing_revenue_gp_table") or summary.get("billing_revenue_gp_table") or summary.get("rows") or []
    total_row = {}
    if isinstance(billing_table, list):
        total_row = next((r for r in billing_table if isinstance(r, dict) and str(r.get("month", "")).lower() == "total"), {})

    # Extract metrics prioritizing cards_map -> total_row -> summary backend keys
    target_rev = float(cards_map.get("target_revenue") if cards_map.get("target_revenue") is not None else (total_row.get("target_all_value") if total_row.get("target_all_value") is not None else (total_row.get("target_value") if total_row.get("target_value") is not None else (summary.get("target_revenue") or summary.get("target_value") or 0.0))))
    target_gp = float(cards_map.get("target_gp") if cards_map.get("target_gp") is not None else (total_row.get("target_gp") if total_row.get("target_gp") is not None else (summary.get("target_gp") or 0.0)))

    b_vs_a_rev = float(cards_map.get("budget_vs_actual_revenue") if cards_map.get("budget_vs_actual_revenue") is not None else (total_row.get("variance") if total_row.get("variance") is not None else (total_row.get("budget_vs_actual_revenu") if total_row.get("budget_vs_actual_revenu") is not None else (summary.get("budget_vs_actual_revenue") or 0.0))))
    b_vs_a_gp = float(cards_map.get("budget_vs_actual_gp") if cards_map.get("budget_vs_actual_gp") is not None else (total_row.get("variance_gp") if total_row.get("variance_gp") is not None else (total_row.get("budget_vs_actual_gp_percent") if total_row.get("budget_vs_actual_gp_percent") is not None else (summary.get("budget_vs_actual_gp") or 0.0))))

    proj_in_hand = int(cards_map.get("project_in_hand") if cards_map.get("project_in_hand") is not None else (summary.get("project_all") if summary.get("project_all") is not None else (summary.get("project_in_hand") if summary.get("project_in_hand") is not None else (total_row.get("total_projects") or summary.get("total_projects") or 0))))
    open_props = int(cards_map.get("open_proposals") if cards_map.get("open_proposals") is not None else (summary.get("proposals_all") if summary.get("proposals_all") is not None else (summary.get("open_proposals") if summary.get("open_proposals") is not None else (summary.get("total_proposals") or 0))))

    t_inv = float(total_row.get("total_invoice_amount_with_credit") if total_row.get("total_invoice_amount_with_credit") is not None else (summary.get("gross_invoiced_revenue") or summary.get("total_invoice_amount_with_credit") or 0.0))

    if cards_map.get("secured_business") is not None:
        secured_biz = float(cards_map["secured_business"])
    elif summary.get("secured_business") is not None and summary.get("project_all") is None:
        secured_biz = float(summary["secured_business"])
    else:
        secured_biz = t_inv + proj_in_hand

    if cards_map.get("balance_to_achieve") is not None:
        balance = float(cards_map["balance_to_achieve"])
    else:
        balance = target_rev - secured_biz

    total_props = int(summary.get("total_proposals") if summary.get("total_proposals") is not None else open_props)
    total_prop_val = float(summary.get("total_proposal_value") if summary.get("total_proposal_value") is not None else 0.0)
    total_projs = int(summary.get("total_projects") if summary.get("total_projects") is not None else proj_in_hand)
    active_projs = int(summary.get("strictly_active_projects_count") if summary.get("strictly_active_projects_count") is not None else (summary.get("active_projects") or 0))

    utilization_val = cards_map.get("utilization") or summary.get("utilization") or summary.get("utilization_pct") or summary.get("utilisation") or summary.get("utilisation_pct")
    utilization_str = f"{float(utilization_val):.0f}%" if (utilization_val is not None and str(utilization_val).strip() not in ("", "%")) else "%"

    achievement_pct = (secured_biz / target_rev * 100) if target_rev > 0 else 0.0

    logger.info(f"[KPI_REPORT_RENDER] employee=\"{emp_name}\" status=PASS")

    lines = [
        f"### 📊 Executive KPI Summary Report: {emp_name}",
        f"**Period:** {period_str}\n",
        "#### 📌 Summary KPI Cards",
        "| KPI Metric | Value |",
        "| --- | ---: |",
        f"| **Budget vs Actual Revenue** | {b_vs_a_rev:,.0f} |",
        f"| **Budget vs Actual GP** | {b_vs_a_gp:,.0f} |",
        f"| **Project in Hand** | {proj_in_hand:,} |",
        f"| **Open Proposals** | {open_props:,} |",
        f"| **Secured Business** | {secured_biz:,.0f} |",
        f"| **Balance to Achieve** | {balance:,.0f} |",
        f"| **Utilization** | {utilization_str} |\n",
        "#### 📈 Financial Targets & Performance",
        "| Metric | Target Value | Secured / Actual | Balance to Achieve | Achievement % |",
        "| --- | --- | --- | --- | --- |",
        f"| **Revenue Target** | BHD {target_rev:,.2f} | BHD {secured_biz:,.2f} | BHD {balance:,.2f} | {achievement_pct:.1f}% |",
        f"| **Gross Profit (GP) Target** | BHD {target_gp:,.2f} | - | - | - |",
        f"| **Proposals Pipeline** | - | BHD {total_prop_val:,.2f} ({total_props} proposals) | - | - |\n",
        "#### 📂 Projects Portfolio Overview",
        f"- **Total Projects Managed:** {total_projs:,}",
        f"- **Strictly Active Projects:** {active_projs:,}\n"
    ]

    projects_by_status = payload.get("projects_by_status") or summary.get("projects_by_status")
    if not projects_by_status and isinstance(payload.get("data"), dict):
        projects_by_status = payload["data"].get("projects_by_status")

    if projects_by_status and isinstance(projects_by_status, list):
        active_statuses = [r for r in projects_by_status if isinstance(r, dict) and int(r.get("count", 0)) > 0]
        if active_statuses:
            lines.append("#### 📊 Project Breakdown by Status")
            lines.append("| Project Status | Count |")
            lines.append("| --- | --- |")
            for r in active_statuses:
                lines.append(f"| {r.get('status_name', 'Unknown')} | {int(r.get('count', 0)):,} |")

    return "\n".join(lines)

class DeterministicRendererEngine:
    """
    Renders structured capability responses deterministically using registered renderers.
    """
    def __init__(self, registry=None):
        self.registry = registry or get_registry()

    def render(self, capability_metadata: Dict[str, Any], payload_envelope: Dict[str, Any]) -> str:
        """Render response deterministically with 0 LLM tokens."""
        cap_id = capability_metadata.get("id", "")
        renderer_fn = self.registry.get_renderer(cap_id) or self.registry.get_renderer("generic_deterministic_renderer")
        res_text = renderer_fn(capability_metadata, payload_envelope)

        from utils.structured_logger import log_stage
        log_stage(logger, "RENDER", Mode="DETERMINISTIC_0_TOKEN", Capability=cap_id, LengthBytes=len(res_text))
        return res_text

def get_renderer_engine() -> DeterministicRendererEngine:
    return DeterministicRendererEngine()
