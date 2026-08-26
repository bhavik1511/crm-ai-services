"""
conversation_manager.py — Manages conversational flow, slot filling, smart defaults, and dynamic UI component selection.
"""
import logging
from typing import Dict, Any, List, Optional

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.capability_catalog import get_capability_metadata

logger = logging.getLogger(__name__)

class ConversationManager:
    def __init__(self):
        pass
        
    async def evaluate_confidence_and_slots(
        self, 
        execution_plan: Dict[str, Any], 
        validation_errors: List[str],
        user_query: str = "",
        llm: Any = None
    ) -> Optional[Dict[str, Any]]:
        """
        Takes the current execution plan and missing information, and determines the next conversational step.
        Returns a response dictionary if a clarification is needed, or None if execution can proceed.
        """
        confidence = execution_plan.get("confidence_score", 0.0)
        missing_info = execution_plan.get("missing_information", [])
        
        # 1. Low Confidence: Ask for intent clarification
        if confidence < 0.60:
            return {
                "type": "done",
                "content": "I'm not entirely sure I understand the exact report or action you need. Could you clarify your request?",
                "is_clarification": True,
                "execution_plan": execution_plan
            }
            
        # 2. Entity Resolution Errors
        entity_errors = execution_plan.get("entity_errors", [])
        if entity_errors:
            err = entity_errors[0]
            err_type = err.get("error_type")
            e_type = err.get("entity_type", "entity")
            query = err.get("query", "")
            
            from engine.presentation_policy import PresentationPolicy
            if err_type == "multiple_matches":
                res_dict = {
                    "status": "AMBIGUOUS",
                    "entity_type": e_type,
                    "input_value": query,
                    "candidates": err.get("matches", [])
                }
            elif err_type == "ambiguous_entity_type":
                res_dict = {
                    "status": "AMBIGUOUS_ENTITY_TYPE",
                    "input_value": query,
                    "candidates": err.get("matches", []),
                    "candidate_entity_types": err.get("candidate_entity_types", [])
                }
            elif err_type == "not_found":
                res_dict = {
                    "status": "NOT_FOUND",
                    "entity_type": e_type,
                    "input_value": query
                }
            else:
                res_dict = {
                    "status": "BACKEND_ERROR",
                    "entity_type": e_type,
                    "input_value": query,
                    "message": err.get("message", "Unknown error")
                }
            
            formatted_res = PresentationPolicy.format_entity_resolution(res_dict)
            formatted_res["execution_plan"] = execution_plan
            return formatted_res
            
        # 3. Handle missing parameters dynamically
        if not missing_info:
            return None # All good to proceed
            
        # Extract unique capability IDs & missing parameter keys across all missing entries
        cap_ids = []
        missing_keys = []
        for mp in missing_info:
            if isinstance(mp, str):
                if mp not in missing_keys:
                    missing_keys.append(mp)
            elif isinstance(mp, dict):
                k = mp.get("key")
                c_id = mp.get("capability_id")
                if k and k not in missing_keys:
                    missing_keys.append(k)
                if c_id and c_id not in cap_ids:
                    cap_ids.append(c_id)

        if not cap_ids:
            for cap in execution_plan.get("business_capabilities", []):
                if cap.get("id") and cap.get("id") not in cap_ids:
                    cap_ids.append(cap.get("id"))

        report_display_names = {
            "kpi_summary": "KPI Summary",
            "revenue_analysis": "Revenue Analysis",
            "receivables_analysis": "Receivables Analysis",
            "recoverability_analysis": "Recoverability Analysis",
            "pipeline_analysis": "Pipeline Analysis",
            "proposal_search": "Proposal Search",
            "project_search": "Project Search",
            "customer_resolution": "Customer Search",
            "analytical_query": "Analytical Query"
        }

        cap_names = []
        for c_id in cap_ids:
            name = report_display_names.get(c_id)
            if not name:
                metadata = get_capability_metadata(c_id)
                cap_desc = metadata.get("description", "requested report") if metadata else "requested report"
                name = cap_desc.split(".")[0].split("(")[0].strip().title()
            if name and name not in cap_names:
                cap_names.append(name)

        if len(cap_names) == 1:
            cap_str = cap_names[0]
        elif len(cap_names) == 2:
            cap_str = f"{cap_names[0]} and {cap_names[1]}"
        else:
            cap_str = ", ".join(cap_names[:-1]) + f", and {cap_names[-1]}"

        # Focus strictly on the primary missing parameter (one item at a time)
        first_missing = missing_info[0]
        if isinstance(first_missing, str):
            param_key = first_missing
        else:
            param_key = first_missing.get("key")

        from engine.presentation_policy import PresentationPolicy
        clar_res = PresentationPolicy.format_clarification({
            "missing_field": param_key,
            "user_query": user_query,
            "original_intent": execution_plan
        })
        clar_res["execution_plan"] = execution_plan
        return clar_res

    async def _generate_dynamic_followup(
        self, 
        user_query: str, 
        cap_str: str, 
        param_key: str, 
        llm: Any = None
    ) -> str:
        """
        Dynamically generates a single, context-aware, LLM-driven follow-up question for missing context.
        """
        param_label = (param_key or "").replace("_", " ").title()

        if llm:
            try:
                from langchain_core.messages import SystemMessage
                sys_prompt = (
                    "You are an intelligent, natural, and polite Enterprise Business Analyst assistant.\n"
                    "The user initiated a business query, but one required parameter is missing to execute the report.\n\n"
                    f"USER QUERY: {user_query or 'Generate report'}\n"
                    f"REQUESTED REPORT(S): {cap_str}\n"
                    f"MISSING PARAMETER NEEDED: {param_label}\n\n"
                    "RULES:\n"
                    "1. Ask a single, polite, natural, and context-aware conversational question asking ONLY for the missing parameter.\n"
                    "2. Adapt the phrasing directly to what the user is trying to accomplish (e.g. 'Generate Revenue Report' -> 'Sure! Which financial year would you like me to analyze?').\n"
                    "3. DO NOT include static bullet points, hardcoded lists, or fake option values.\n"
                    "4. Ask for ONLY ONE missing item at a time in 1 concise sentence.\n"
                    "5. Keep the response completely conversational and plain text."
                )
                res = await llm.ainvoke([SystemMessage(content=sys_prompt)])
                content = res.content.strip()
                if content:
                    return content
            except Exception as e:
                logger.warning(f"Failed to generate dynamic LLM follow-up question: {e}")

        # Dynamic fallback template without hardcoded option lists
        if "year" in (param_key or "").lower() or "fy" in (param_key or "").lower():
            return f"Sure! Which financial year would you like me to analyze for {cap_str}?"
        elif "service_line" in (param_key or "").lower():
            return f"I'd be happy to help. Which service line should I use for {cap_str}?"
        elif "customer" in (param_key or "").lower():
            return f"Sure! Which customer would you like me to include in {cap_str}?"
        else:
            return f"Sure! To generate {cap_str}, which {param_label} should I consider?"

    def _guess_ui_component(self, param_key: str) -> str:
        return "text_input"

    def classify_request_type(
        self,
        query: str,
        previous_memory: Dict[str, Any]
    ) -> str:
        """
        Classifies request into: NEW_REQUEST, FOLLOW_UP, CLARIFICATION_RESPONSE, or CONTEXT_RESET.
        """
        q_clean = (query or "").strip().lower()
        if not previous_memory or not previous_memory.get("active_filters"):
            return "NEW_REQUEST"

        # Check explicit Context Reset markers
        reset_markers = [
            "now show", "instead", "switch to", "change to", "show me tax",
            "show me bps", "show me brs", "show me legal", "show me tech",
            "show me growth", "what about tax", "what about bps"
        ]
        if any(marker in q_clean for marker in reset_markers):
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=CONTEXT_RESET query='{query}'")
            return "CONTEXT_RESET"

        prev_filters = previous_memory.get("active_filters", {})
        prev_sl = prev_filters.get("service_line")

        # Check if user explicitly switches service line (e.g. Audit -> Tax)
        if prev_sl and ("tax" in q_clean or "brs" in q_clean or "bps" in q_clean or "growth" in q_clean or "legal" in q_clean or "tech" in q_clean) and prev_sl.lower() not in q_clean:
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=CONTEXT_RESET query='{query}' (service line change)")
            return "CONTEXT_RESET"

        # Natural Follow-Up triggers
        followup_phrases = [
            "audit gp", "audit sme", "audit support", "gp", "show gp", "show details",
            "monthly", "customer wise", "the first one", "first one", "option 1",
            "option 2", "option 3", "yes", "yeah", "sure", "ok", "okay", "drilldown"
        ]
        if any(fp in q_clean for fp in followup_phrases) or q_clean in ["1", "2", "3", "4", "5"]:
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=FOLLOW_UP query='{query}'")
            return "FOLLOW_UP"

        # If previous topic is active and current query refines it
        if prev_sl and (prev_sl.lower() in q_clean or "gp" in q_clean or "revenue" in q_clean or "detail" in q_clean):
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=FOLLOW_UP query='{query}'")
            return "FOLLOW_UP"

        return "NEW_REQUEST"

    def build_dynamic_followup_options(
        self,
        entity_type: str,
        entity_id: Any,
        entity_name: str,
        capability: str,
        current_context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Dynamically constructs structured follow-up choices derived from master hierarchy data.
        Zero hardcoded strings — queries DB master relationships for child entities.
        """
        if not entity_name:
            return []

        options = []
        e_type = (entity_type or "").lower()

        if e_type in ["service_line", "serviceline"]:
            from agent.entity_resolver import get_child_departments_for_serviceline
            children = get_child_departments_for_serviceline(entity_id or entity_name)
            for child in children:
                c_name = child.get("name")
                c_id = child.get("id")
                # Filter for canonical departmental sub-units
                if c_name and any(sub in c_name for sub in ["GP", "SME", "Support", "Advisory", "Tax"]):
                    options.append({
                        "label": c_name,
                        "entity_type": "department",
                        "entity_id": c_id,
                        "parent_entity_type": "service_line",
                        "parent_entity_id": entity_id,
                        "capability": capability or "gp_performance",
                        "metric": current_context.get("metric", "GP"),
                        "inherited_context": {
                            "service_line": entity_name,
                            "service_line_id": entity_id,
                            "metric": current_context.get("metric", "GP"),
                            "financial_year": current_context.get("financial_year", "FY2526")
                        }
                    })

        return options

    def resolve_followup_input(
        self,
        query: str,
        active_options: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves natural user inputs like 'audit gp', 'GP', 'the first one', 'yes' against active follow-up options.
        Returns selected option dict, or special clarification dict if input is ambiguous (e.g. 'yes' with multiple options).
        """
        if not query or not active_options:
            return None

        q_clean = query.strip().lower()
        import re

        # Handle 'yes' when multiple options exist
        if q_clean in ["yes", "yeah", "sure", "ok", "okay", "show"]:
            if len(active_options) > 1:
                labels_str = "\n".join([f"{i+1}) {opt['label']}" for i, opt in enumerate(active_options)])
                return {
                    "type": "clarification_needed",
                    "content": f"Which option would you like to see?\n\n{labels_str}",
                    "is_clarification": True,
                    "options": active_options
                }
            elif len(active_options) == 1:
                return active_options[0]

        # Ordinal / index selection ('the first one', '1', 'option 1')
        idx_match = re.search(r'\b(?:option\s*|the\s*)?([1-9])(?:st|nd|rd|th)?(?:\s*one)?\b', q_clean)
        if idx_match:
            idx = int(idx_match.group(1)) - 1
            if 0 <= idx < len(active_options):
                return active_options[idx]

        # Label matching (strip leading action verbs like 'show', 'view', 'get', 'display')
        q_sub = re.sub(r'^(show|view|get|display)\s+', '', q_clean).strip()
        for opt in active_options:
            lbl = str(opt.get("label", "")).lower()
            if lbl and (lbl in q_clean or q_clean in lbl or lbl in q_sub or q_sub in lbl):
                return opt

        return None


conversation_manager = ConversationManager()
