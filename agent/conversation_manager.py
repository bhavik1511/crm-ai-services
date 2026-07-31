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
            # Handle the first entity error
            err = entity_errors[0]
            err_type = err.get("error_type")
            e_type = err.get("entity_type", "entity")
            query = err.get("query", "")
            
            if err_type == "multiple_matches":
                matches = err.get("matches", [])
                match_names = [str(m.get("entity_name")) for m in matches]
                content = f"I found multiple matches for {e_type} '{query}': **{', '.join(match_names)}**. Which one did you mean?"
            elif err_type == "not_found":
                content = f"I could not find a {e_type} matching '{query}'. Please verify the name and try again."
            else:
                content = f"I encountered an error looking up {e_type} '{query}': {err.get('message', 'Unknown error')}."
                
            return {
                "type": "done",
                "content": content,
                "is_clarification": True,
                "execution_plan": execution_plan
            }
            
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

        # Dynamically generate context-aware LLM follow-up question
        content = await self._generate_dynamic_followup(
            user_query=user_query,
            cap_str=cap_str,
            param_key=param_key,
            llm=llm
        )

        return {
            "type": "done",
            "content": content,
            "is_clarification": True,
            "execution_plan": execution_plan,
            "slot": None
        }

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

conversation_manager = ConversationManager()
