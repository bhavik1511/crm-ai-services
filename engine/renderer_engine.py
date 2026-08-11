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
from typing import Dict, Any, List, Optional
from registry.metadata_registry import get_registry, register_renderer

logger = logging.getLogger(__name__)

def _format_currency_val(val: Any) -> str:
    """Helper to format numeric amounts into currency strings."""
    if isinstance(val, (int, float)):
        if val > 100 or isinstance(val, float):
            return f"BHD {val:,.2f}"
    return str(val)

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
    if status == "error":
        payload_data = payload_envelope.get("payload", {})
        err_msg = payload_data.get("error_message") if isinstance(payload_data, dict) else "Information unavailable."
        return f"⚠️ {err_msg}"

    payload = payload_envelope.get("payload")
    if payload is None:
        return "The requested dataset is empty."

    cap_id = capability_metadata.get("id", "")
    schema = capability_metadata.get("response_schema", {})
    primary_metric = capability_metadata.get("primary_metric")

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
        elif op_type == "aggregate":
            title = f"Total {metric}"
        else:
            title = cap_id.replace("_", " ").title()
    else:
        title = cap_id.replace("_", " ").title()

    lines = [f"### 📊 {title}\n"]

    # 1. Handle List / Table Payload Shape
    if isinstance(payload, list):
        if not payload:
            return f"### 📊 {title}\n\nNo records found."
        
        # Render Table
        first_row = payload[0]
        if isinstance(first_row, dict):
            headers = [k for k in first_row.keys() if not k.startswith("_")]
            lines.append("| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |")
            lines.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in payload[:15]:  # Display top 15 rows
                row_vals = []
                for h in headers:
                    v = row.get(h, "")
                    row_vals.append(_format_currency_val(v))
                lines.append("| " + " | ".join(row_vals) + " |")
            
            if len(payload) > 15:
                lines.append(f"\n_*Showing top 15 of {len(payload)} records.*_")
        else:
            for item in payload[:10]:
                lines.append(f"- {_format_currency_val(item)}")

        return "\n".join(lines)

    # 2. Handle Dict Payload Shape
    if isinstance(payload, dict):
        # Extract scalar metrics & nested row tables
        scalar_fields = []
        table_fields = []

        for k, v in payload.items():
            if k.startswith("_"):
                continue
            if isinstance(v, list):
                table_fields.append((k, v))
            elif isinstance(v, dict):
                # Flatten single object details
                for sub_k, sub_v in v.items():
                    if not sub_k.startswith("_") and not isinstance(sub_v, (dict, list)):
                        scalar_fields.append((f"{k} - {sub_k}", sub_v))
            else:
                scalar_fields.append((k, v))

        # Render KPI Cards / Key-Value Summary
        if scalar_fields:
            for field_name, val in scalar_fields:
                formatted_name = field_name.replace("_", " ").title()
                formatted_val = _format_currency_val(val)
                lines.append(f"- **{formatted_name}:** `{formatted_val}`")
            lines.append("")

        # Render nested tables if present
        for tbl_name, tbl_rows in table_fields:
            tbl_title = tbl_name.replace("_", " ").title()
            lines.append(f"#### {tbl_title}")
            if tbl_rows and isinstance(tbl_rows[0], dict):
                headers = [k for k in tbl_rows[0].keys() if not k.startswith("_")]
                lines.append("| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                for r in tbl_rows[:10]:
                    row_vals = [_format_currency_val(r.get(h, "")) for h in headers]
                    lines.append("| " + " | ".join(row_vals) + " |")
                if len(tbl_rows) > 10:
                    lines.append(f"\n_*Showing top 10 of {len(tbl_rows)} entries.*_\n")
            lines.append("")

        return "\n".join(lines).strip()

    return f"### 📊 {title}\n\n{str(payload)}"

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
