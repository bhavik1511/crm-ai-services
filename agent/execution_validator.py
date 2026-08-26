"""
execution_validator.py — Gatekeeper node that prevents premature execution.
Enforces Confidence Scores, Missing Information, and Write Protections at the Business Capability level.
"""
import logging
from typing import Dict, Any, Tuple, List
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from registry.capability_catalog import get_capability_metadata

logger = logging.getLogger(__name__)

def _is_parameter_provided(param_key: str, ctx: dict, resolved_entities: list, time_filter: str, user_ctx: dict) -> bool:
    """Helper to check if a parameter is provided in query context, entities, time filter, or user session context."""
    p_lower = param_key.lower()
    
    if param_key in ctx and ctx[param_key]:
        return True
        
    if any(k in p_lower for k in ["temporal", "year", "date", "period", "month", "time"]):
        if time_filter and (any(c.isdigit() for c in str(time_filter)) or any(m in str(time_filter).lower() for m in ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "month", "year", "fy"])):
            return True
        if ctx.get("financial_year") or ctx.get("start_date") or ctx.get("end_date") or ctx.get("date") or ctx.get("month") or ctx.get("time_filter"):
            return True
        if user_ctx and (user_ctx.get("financial_year") or user_ctx.get("start_date")):
            return True

    if "service_line" in p_lower:
        if ctx.get("service_line") or ctx.get("serviceline_id"):
            return True
        if any(e.get("entity_type") in ("service_line", "serviceline") or e.get("type") in ("service_line", "serviceline") for e in resolved_entities):
            return True
        if user_ctx and user_ctx.get("service_line") and str(user_ctx.get("service_line")).lower() != "all":
            return True

    if "customer" in p_lower or "client" in p_lower:
        if ctx.get("customer_id") or ctx.get("client_id") or ctx.get("search_term"):
            return True
        if any(e.get("entity_type") == "customer" for e in resolved_entities):
            return True

    if p_lower == "search_term":
        if ctx.get("search_term") or ctx.get("query"):
            return True
        if resolved_entities:
            return True

    return False


def validate_execution(execution_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validates if the business capabilities can be safely executed based on the 
    Confidence-Based Execution logic and Missing Information logic.
    """
    confidence = execution_plan.get("confidence_score", 0.0)
    capabilities = execution_plan.get("business_capabilities", [])
    
    errors = []
    
    # Initialize missing_information list if absent
    if "missing_information" not in execution_plan or not isinstance(execution_plan["missing_information"], list):
        execution_plan["missing_information"] = []
    
    # Check Entity Resolution Errors
    entity_errors = execution_plan.get("entity_errors", [])
    if entity_errors:
        for err in entity_errors:
            errors.append(f"Entity Error: {err.get('query')}")
        return False, errors
        
    # Strict Confidence Check Architecture
    if confidence < 0.60:
        logger.warning(f"Execution blocked: Low confidence ({confidence}).")
        return False, ["I'm not entirely sure I understand the exact report or action you need. Could you clarify your request?"]
    elif confidence < 0.85:
        logger.info(f"Execution proceeding with caution: Medium confidence ({confidence}).")
        
    resolved_entities = execution_plan.get("resolved_entities", [])
    user_ctx = execution_plan.get("user_context", {}) or {}
    
    # Dynamic Capability Validation
    for cap in capabilities:
        cap_id = cap.get("id")
        
        metadata = get_capability_metadata(cap_id)
        if not metadata:
            errors.append(f"Internal Error: Business Capability '{cap_id}' is not registered in the Catalog.")
            continue
            
        # Write Protection Check
        implementations = metadata.get("implementations", [])
        needs_confirmation = any(impl.get("needs_confirmation", False) for impl in implementations)
        
        if needs_confirmation and not execution_plan.get("user_confirmed", False):
            errors.append(f"The capability '{metadata.get('description')}' makes changes to CRM data and requires your explicit confirmation to proceed.")
            
        # Dynamic Parameter Check
        from registry.tool_registry import tool_registry
        ctx = cap.get("context", {})
        time_filter = cap.get("time_filter") or execution_plan.get("time_filter")
        
        selection_result = tool_registry.score_and_select_implementation(cap_id, implementations, list(ctx.keys()), resolved_entities)
        best_impl = selection_result.get("implementation")
        missing_params = selection_result.get("missing_parameters", [])
        
        if not best_impl:
            errors.append(f"No valid implementation could be resolved for capability '{cap_id}' based on the available entities.")
            continue

        missing_keys = set(missing_params)
        
        # Metadata-Driven Dependency & Scope Validation (Single Source of Truth)
        cap_scope = cap.get("scope", "organization")
        depends_on_entities = metadata.get("depends_on_entities", [])
        supports_org_scope = metadata.get("supports_organization_scope", True)
        param_meta = metadata.get("parameter_metadata", {})
        required_context = metadata.get("required_context", [])

        # Enforce catalog required_context (e.g. temporal_scope)
        for req_ctx_item in required_context:
            if not _is_parameter_provided(req_ctx_item, ctx, resolved_entities, time_filter, user_ctx):
                missing_keys.add(req_ctx_item)

        # Filter out parameters that are provided or have smart defaults (e.g. current_fy)
        cleaned_missing = set()
        for mp in missing_keys:
            p_info = param_meta.get(mp, {})
            if isinstance(p_info, dict) and p_info.get("smart_default") is not None:
                continue
            if mp in ("temporal_scope", "date_range"):
                # Standard smart default (current_fy) applies automatically via TemporalResolver
                continue
            if _is_parameter_provided(mp, ctx, resolved_entities, time_filter, user_ctx):
                continue
            cleaned_missing.add(mp)
        missing_keys = cleaned_missing

        # 1. If capability supports organization/filtered scope and is requested at organization/filtered level, OR depends_on_entities is empty:
        #    -> NEVER require search_term or entity lookup parameters.
        if (cap_scope in ("organization", "filtered") and supports_org_scope) or not depends_on_entities:
            missing_keys = {mp for mp in missing_keys if mp not in ("search_term", "project_name", "customer_name", "proposal_name")}
        else:
            # 2. If capability strictly depends on specific entities (e.g. customer_360_profile), verify entity presence
            for req_ent in depends_on_entities:
                has_ent = any(
                    e.get("entity_type") == req_ent or e.get("type") == req_ent
                    for e in resolved_entities
                )
                if not has_ent and not _is_parameter_provided(req_ent, ctx, resolved_entities, time_filter, user_ctx):
                    missing_keys.add(f"{req_ent}_name")

        if missing_keys:
            for mp in missing_keys:
                missing_entry = {"key": mp, "capability_id": cap_id}
                if missing_entry not in execution_plan["missing_information"]:
                    execution_plan["missing_information"].append(missing_entry)
                errors.append(f"I need a bit more information: Please provide {mp.replace('_', ' ')}.")
                
    return len(errors) == 0, errors
