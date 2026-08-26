"""
presentation_policy.py — Centralized Unified Presentation Policy
================================================================
Single presentation authority for the CRM Chatbot Service.
Decouples intent understanding and backend execution from response formatting.

Rules:
1. Purely presentation driven — does NOT call APIs, infer intent, or modify numbers.
2. Driven by CanonicalIntent.expected_result_type or missing clarification metadata.
3. Completely sanitizes internal system names (analytical_query, temporal_scope, ExecutionContract, etc.).
4. Formats compact conversational Markdown for clarifications and concise tables/cards for results.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# System field names to NEVER expose to users
FORBIDDEN_USER_TERMS = {
    "analytical_query", "analytical query", "kpi_summary", "kpi summary",
    "temporal_scope", "executioncontract", "business_capabilities",
    "capability_id", "entityresolver", "canonicalintent", "confidence",
    "resolved_id", "ambiguous"
}

class PresentationPolicy:
    """
    Unified Presentation Authority.
    """

    @staticmethod
    def should_offer_export(presentation_action: str, row_count: int, column_count: int) -> Tuple[bool, str]:
        """
        Determines export availability based on explicit presentation action or result matrix dimensions.
        Strict condition: row_count > 5 AND column_count > 6, OR presentation_action in {"EXPORT", "GENERATE"}.
        """
        action = str(presentation_action or "VIEW").upper().strip()
        if action in ("EXPORT", "GENERATE"):
            return True, "explicit_export"
        if row_count > 5 and column_count > 6:
            return True, "large_result"
        return False, "small_result"

    @staticmethod
    def extract_matrix_dimensions(payload: Optional[Dict[str, Any]]) -> Tuple[int, int]:
        """
        Extracts structural row and column counts from payload dataset envelopes.
        """
        if not payload or not isinstance(payload, dict):
            return 0, 0
        
        p = payload.get("payload") if isinstance(payload.get("payload"), dict) else (payload.get("data") if isinstance(payload.get("data"), dict) else payload)
        if not isinstance(p, dict):
            return 0, 0

        items = p.get("ranking_data") or p.get("ranking_list") or p.get("rows") or p.get("records") or p.get("items") or p.get("projects") or p.get("employees") or []
        if isinstance(items, dict) and "rows" in items:
            items = items["rows"]
        
        if isinstance(items, list) and items:
            row_count = len(items)
            sample_item = items[0]
            if isinstance(sample_item, dict):
                col_count = len(sample_item.keys())
            else:
                col_count = 3
            if p.get("operation") == "ranking" or p.get("result_type") in ("ranking_table", "ranking"):
                col_count = 3
            return row_count, col_count

        if "summary" in p or "total_outstanding" in p or "total_revenue" in p:
            return 1, 3

        return 0, 0

    @staticmethod
    def evaluate_export_policy(
        presentation_action: str,
        payload: Optional[Dict[str, Any]] = None,
        row_count: Optional[int] = None,
        column_count: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Evaluates export availability and returns structured metadata envelope.
        Emits [EXPORT_POLICY] structured log.
        """
        action = str(presentation_action or "VIEW").upper().strip()
        if row_count is None or column_count is None:
            r_cnt, c_cnt = PresentationPolicy.extract_matrix_dimensions(payload)
            row_count = row_count if row_count is not None else r_cnt
            column_count = column_count if column_count is not None else c_cnt

        export_available, reason = PresentationPolicy.should_offer_export(action, row_count, column_count)
        export_format = "xlsx" if export_available else None

        logger.info(f"[EXPORT_POLICY] action={action} rows={row_count} columns={column_count} export_available={str(export_available).lower()} reason={reason}")

        return {
            "presentation_action": action,
            "export_available": export_available,
            "export_format": export_format,
            "row_count": row_count,
            "column_count": column_count,
            "reason": reason
        }

    @staticmethod
    def format_entity_resolution(resolution: Dict[str, Any]) -> Dict[str, Any]:
        """
        Converts a structured EntityResolutionResult into a clean, user-facing Markdown message.
        Guarantees zero internal jargon (confidence scores, internal IDs, resolver status strings).
        """
        status = str(resolution.get("status", "")).upper()
        e_type = str(resolution.get("entity_type") or "record").lower().strip()
        e_type_label = e_type.replace("_", " ")
        input_val = resolution.get("input_value") or ""
        resolved_name = resolution.get("resolved_name") or resolution.get("entity_name") or ""
        candidates = resolution.get("candidates") or resolution.get("matches") or []

        if status == "RESOLVED":
            has_detail_capability = resolution.get("has_detail_capability", False)
            if has_detail_capability:
                msg = f"I found **{resolved_name}**. What would you like to know about this {e_type_label}?"
            else:
                msg = (
                    f"I found **{resolved_name}**, but I don't currently have a registered operation "
                    f"for retrieving general {e_type_label} details. You can ask me for a supported report "
                    f"or performance summary for **{resolved_name}**."
                )
            return {
                "type": "done",
                "content": msg,
                "answer": msg,
                "is_clarification": False,
                "suggestions": [f"Show metrics for {resolved_name}"]
            }

        elif status == "AMBIGUOUS":
            candidate_names = []
            for c in candidates:
                c_name = c.get("name") or c.get("entity_name") or c.get("resolved_name")
                if c_name and c_name not in candidate_names:
                    candidate_names.append(c_name)

            bullet_list = "\n".join([f"• **{name}**" for name in candidate_names])
            msg = (
                f"I found multiple matching {e_type_label}s. Which one did you mean?\n\n"
                f"{bullet_list}"
            )
            return {
                "type": "done",
                "content": msg,
                "answer": msg,
                "is_clarification": True,
                "suggestions": candidate_names
            }

        elif status == "AMBIGUOUS_ENTITY_TYPE":
            cand_types = resolution.get("candidate_entity_types") or []
            if not cand_types and candidates:
                cand_types = list(set(c.get("entity_type") for c in candidates if c.get("entity_type")))
            
            type_labels = " or ".join([t.replace("_", " ") for t in cand_types]) if cand_types else "customer or project"
            msg = f"I found this name under multiple CRM records. Are you referring to the {type_labels}?"
            return {
                "type": "done",
                "content": msg,
                "answer": msg,
                "is_clarification": True,
                "suggestions": [t.replace("_", " ").title() for t in cand_types]
            }

        elif status in ("NOT_FOUND", "INVALID"):
            msg = f"I couldn't find a matching {e_type_label} for '**{input_val}**'. Please check the name and try again."
            return {
                "type": "done",
                "content": msg,
                "answer": msg,
                "is_clarification": False,
                "suggestions": []
            }

        else:
            msg = f"Sorry, I couldn't verify the CRM record for '**{input_val}**'."
            return {
                "type": "done",
                "content": msg,
                "answer": msg,
                "is_clarification": False,
                "suggestions": []
            }

    @staticmethod
    def format_clarification(structured_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Formats structured clarification metadata into a clean, conversational Markdown request.
        
        Input structured_data:
          - missing_field: e.g. "temporal_scope", "dimension", "customer", etc.
          - allowed_values: Optional[List[str]]
          - original_intent: CanonicalIntent or Dict
          - user_query: str
        """
        missing_field = str(structured_data.get("missing_field") or "temporal_scope").lower().strip()

        if "temporal" in missing_field or "time" in missing_field or "year" in missing_field:
            msg = (
                "Sure — what time period should I use for this analysis?\n\n"
                "You can choose:\n"
                "**Current month** · **Current financial year** · **Previous financial year** · **Specific date range**"
            )
            suggestions = [
                "Current month",
                "Current financial year",
                "Previous financial year",
                "Specific date range"
            ]
        elif missing_field == "dimension" or "dimension" in missing_field:
            msg = (
                "Sure — which dimension would you like to group or rank this analysis by?\n\n"
                "You can choose:\n"
                "**Customer** · **Department** · **Service line** · **Employee**"
            )
            suggestions = ["Customer", "Department", "Service line", "Employee"]
        elif "customer" in missing_field:
            msg = "Sure — which customer should I run this analysis for?"
            suggestions = []
        elif "department" in missing_field:
            msg = "Sure — which department should I analyze?"
            suggestions = []
        elif "service_line" in missing_field or "service line" in missing_field:
            msg = "Sure — which service line would you like to include?"
            suggestions = []
        else:
            field_label = missing_field.replace("_", " ").title()
            msg = f"Sure — could you specify the {field_label} you'd like me to use?"
            suggestions = []

        # Sanity check: Ensure zero internal terms leak into clarification output
        for forbidden in FORBIDDEN_USER_TERMS:
            if forbidden in msg.lower():
                logger.error(f"[PRESENTATION_POLICY_LEAK] Forbidden term '{forbidden}' detected in clarification message! Sanitizing.")
                msg = msg.replace(forbidden, "analysis")

        logger.info(f"[PRESENTATION_CLARIFICATION] missing_field={missing_field} | suggestions={len(suggestions)}")

        return {
            "type": "done",
            "content": msg,
            "is_clarification": True,
            "suggestions": suggestions
        }

    @staticmethod
    def format_result(
        expected_result_type: str,
        payload_envelope: Dict[str, Any],
        capability_metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Renders validated payload envelopes deterministically based on expected_result_type.
        """
        from engine.renderer_engine import render_deterministic_response
        
        result_type = (expected_result_type or "summary").lower().strip()
        
        if result_type == "ranking":
            return PresentationPolicy._render_ranking(payload_envelope)
        elif result_type in ["receivables_summary", "receivables_ageing", "receivables"]:
            return PresentationPolicy._render_receivables(payload_envelope, result_type)
        elif result_type in ["comparison", "period_comparison"]:
            return PresentationPolicy._render_comparison(payload_envelope)
        elif result_type in ["proposal_list", "proposals", "proposal"]:
            return PresentationPolicy._render_proposals(payload_envelope)
        else:
            cap_meta = capability_metadata or {"id": "generic_deterministic_renderer"}
            return render_deterministic_response(cap_meta, payload_envelope)

    @staticmethod
    def _render_ranking(payload_envelope: Dict[str, Any]) -> str:
        """Concise, polished ranking table presentation."""
        if not payload_envelope or payload_envelope.get("status") == "error":
            payload_data = payload_envelope.get("payload", {}) if isinstance(payload_envelope, dict) else {}
            err_msg = payload_data.get("error_message") if isinstance(payload_data, dict) else "No data available."
            return f"⚠️ {err_msg}"

        payload = payload_envelope.get("payload") if isinstance(payload_envelope.get("payload"), dict) else (payload_envelope.get("data") if isinstance(payload_envelope.get("data"), dict) else payload_envelope)
        
        if not payload or not isinstance(payload, dict):
            return "No matching ranking data was returned for the requested criteria."

        items = payload.get("ranking_list") or payload.get("rows") or payload.get("data") or []
        if isinstance(items, dict) and "rows" in items:
            items = items["rows"]
        if not isinstance(items, list) or not items:
            return "No matching ranking data was returned for the requested criteria."

        dim = payload.get("dimension") or "Entity"
        dim_title = str(dim).replace("_", " ").title()
        metric_title = str(payload.get("metric") or "Revenue").replace("_", " ").title()
        period_str = payload.get("financial_year") or "Current Financial Year"

        lines = [
            f"### 🏆 Top {len(items)} Ranking by {metric_title}",
            f"**Period:** {period_str}\n",
            f"| Rank | {dim_title} | {metric_title} |",
            "| :---: | :--- | ---: |"
        ]

        from engine.renderer_engine import _format_cell_val
        for idx, row in enumerate(items, 1):
            if isinstance(row, dict):
                entity_name = row.get("entity_name") or row.get("name") or row.get("customer_name") or row.get("department_name") or row.get("service_line_name") or row.get("id") or "Unknown"
                val = row.get("metric_value") if row.get("metric_value") is not None else (row.get("revenue") or row.get("value") or 0.0)
                formatted_val = _format_cell_val(val, metric_title)
                lines.append(f"| {idx} | **{entity_name}** | {formatted_val} |")

        return "\n".join(lines)

    @staticmethod
    def _render_receivables(payload_envelope: Dict[str, Any], result_type: str) -> str:
        """Concise receivables summary / ageing presentation without dumping revenue tables."""
        if not payload_envelope or payload_envelope.get("status") == "error":
            return "⚠️ Receivables data unavailable."

        payload = payload_envelope.get("payload") if isinstance(payload_envelope.get("payload"), dict) else (payload_envelope.get("data") if isinstance(payload_envelope.get("data"), dict) else payload_envelope)
        if not payload or not isinstance(payload, dict):
            return "No receivables data was returned."

        total_outstanding = payload.get("total_outstanding") or payload.get("total_receivables") or payload.get("outstanding_amount") or 0.0
        record_count = payload.get("total_records") or payload.get("record_count") or len(payload.get("rows", []))
        
        from engine.renderer_engine import _format_cell_val
        lines = [
            "### 💳 Receivables Summary",
            f"- **Total Outstanding Receivables:** {_format_cell_val(total_outstanding, 'revenue')}",
            f"- **Outstanding Records:** {record_count:,}\n"
        ]

        ageing = payload.get("ageing_breakdown") or payload.get("ageing") or payload.get("buckets")
        if isinstance(ageing, dict) and ageing:
            lines.append("#### ⏳ Ageing Breakdown")
            lines.append("| Ageing Bucket | Amount |")
            lines.append("| --- | ---: |")
            for bucket, amt in ageing.items():
                bucket_title = str(bucket).replace("_", " ").title()
                lines.append(f"| {bucket_title} | {_format_cell_val(amt, 'revenue')} |")

        return "\n".join(lines)

    @staticmethod
    def _render_comparison(payload_envelope: Dict[str, Any]) -> str:
        """Concise comparison presentation."""
        from engine.renderer_engine import render_deterministic_response
        return render_deterministic_response({"id": "generic_deterministic_renderer"}, payload_envelope)

    @staticmethod
    def _render_proposals(payload_envelope: Dict[str, Any]) -> str:
        """Concise proposals list presentation."""
        from engine.renderer_engine import render_deterministic_response
        return render_deterministic_response({"id": "generic_deterministic_renderer"}, payload_envelope)

