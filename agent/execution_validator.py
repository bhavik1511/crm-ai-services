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

def validate_execution(execution_plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validates if the business capabilities can be safely executed based on the 
    Confidence-Based Execution logic and Missing Information logic.
    """
    confidence = execution_plan.get("confidence_score", 0.0)
    capabilities = execution_plan.get("business_capabilities", [])
    missing_info = execution_plan.get("missing_information", [])
    
    errors = []
    
    # 1. Clear LLM hallucinated missing information
    # We only care about dynamically verified missing parameters from the Tool Registry.
    execution_plan["missing_information"] = []
    
    # 2. Strict Confidence Check Architecture
    if confidence < 0.60:
        logger.warning(f"Execution blocked: Low confidence ({confidence}).")
        return False, ["I'm not entirely sure I understand the exact report or action you need. Could you clarify your request?"]
    elif confidence < 0.85:
        logger.info(f"Execution proceeding with caution: Medium confidence ({confidence}).")
        
    # 3. Dynamic Capability Validation
    for cap in capabilities:
        cap_id = cap.get("id")
        
        metadata = get_capability_metadata(cap_id)
        if not metadata:
            errors.append(f"Internal Error: Business Capability '{cap_id}' is not registered in the Catalog.")
            continue
            
        # 4. Write Protection Check (Destructive Actions)
        # We check the implementations to see if any require confirmation
        implementations = metadata.get("implementations", [])
        needs_confirmation = any(impl.get("needs_confirmation", False) for impl in implementations)
        
        if needs_confirmation and not execution_plan.get("user_confirmed", False):
            errors.append(f"The capability '{metadata.get('description')}' makes changes to CRM data and requires your explicit confirmation to proceed.")
            
        # 5. Dynamic Parameter Check (from best implementation)
        from registry.tool_registry import tool_registry
        resolved_entities = execution_plan.get("resolved_entities", [])
        ctx = cap.get("context", {})
        selection_result = tool_registry.score_and_select_implementation(cap_id, implementations, list(ctx.keys()), resolved_entities)
        
        best_impl = selection_result.get("implementation")
        missing_params = selection_result.get("missing_parameters", [])
        
        if not best_impl:
            errors.append(f"No valid implementation could be resolved for capability '{cap_id}' based on the available entities.")
        elif missing_params:
            for mp in missing_params:
                # Add to execution plan so Planner knows it's officially requested
                if mp not in execution_plan.setdefault("missing_information", []):
                    execution_plan["missing_information"].append(mp)
                errors.append(f"I need a bit more information: Please provide {mp.replace('_', ' ')}.")
                
    return len(errors) == 0, errors
