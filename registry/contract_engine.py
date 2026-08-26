"""
contract_engine.py — Response Contract Engine
================================================
Validates requested capabilities, operations, filters, entity requirements,
and presentation contracts against registered metadata schemas.

Ensures the AI assistant reasons entirely from contract descriptors without
hardcoding assumptions about backend REST endpoints or reports.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from registry.metadata_registry import get_registry

logger = logging.getLogger(__name__)

class ContractEngine:
    """
    Evaluates capabilities and query parameters against registered response contracts.
    """
    def __init__(self, registry=None):
        self.registry = registry or get_registry()

    def get_contract(self, capability_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve response contract for a capability."""
        cap = self.registry.get_capability(capability_id)
        if not cap:
            return None
        return cap.get("response_contract")

    def validate_capability_contract(
        self, capability_id: str, query_params: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Validates capability call requirements against its contract and context metadata.
        Returns (is_valid, list_of_missing_or_invalid_fields).
        """
        cap = self.registry.get_capability(capability_id)
        if not cap:
            return False, [f"Capability '{capability_id}' is not registered."]

        issues = []
        req_context = cap.get("required_business_context", {})
        for param_name, param_meta in req_context.items():
            is_req = param_meta.get("required", False) if isinstance(param_meta, dict) else False
            if is_req and param_name not in query_params:
                issues.append(f"Missing required parameter: '{param_name}'")

        schema = cap.get("response_schema", {})
        if not schema:
            logger.debug(f"[ContractEngine] Capability '{capability_id}' has no explicit response_schema; contract pass.")

        return len(issues) == 0, issues

    def get_supported_presentation_modes(self, capability_id: str) -> List[str]:
        """Returns list of supported presentation modes for capability."""
        contract = self.get_contract(capability_id)
        if not contract:
            return ["INSIGHT"]
        
        modes = []
        if contract.get("supports_report"):
            modes.append("REPORT")
        if contract.get("supports_summary"):
            modes.append("INSIGHT")
        if contract.get("supports_chart"):
            modes.append("CHART")
        if contract.get("supports_table"):
            modes.append("TABLE")
        if contract.get("supports_comparison"):
            modes.append("COMPARISON")
        
        default = contract.get("default_presentation")
        if default and default not in modes:
            modes.insert(0, default)
            
        return modes or ["INSIGHT"]

def resolve_presentation_intent(canonical_intent: Optional[Dict[str, Any]] = None, capability_id: str = "") -> str:
    """
    Metadata-driven presentation intent resolution.
    Reads presentation_action directly from structured CanonicalIntent / ExecutionContract metadata.
    Defaults to capability metadata default or 'VIEW' (Zero raw text keyword parsing).
    """
    if canonical_intent and isinstance(canonical_intent, dict):
        action = canonical_intent.get("presentation_action") or canonical_intent.get("presentation_intent") or canonical_intent.get("action")
        if action and str(action).upper() in ("VIEW", "EXPORT", "GENERATE", "REPORT"):
            return str(action).upper()

    if capability_id:
        engine = get_contract_engine()
        contract = engine.get_contract(capability_id) or {}
        default_pres = contract.get("default_presentation")
        if default_pres:
            return str(default_pres).upper()

    return "VIEW"


def build_presentation_actions(
    capability_id: str,
    presentation_intent: str,
    query_params: Optional[Dict[str, Any]] = None,
    export_payload: Optional[Dict[str, Any]] = None,
    export_available: bool = False
) -> List[Dict[str, Any]]:
    """
    Generates presentation action DTOs based on response_contract metadata, resolved intent,
    and single-authority PresentationPolicy export decision.
    """
    engine = get_contract_engine()
    contract = engine.get_contract(capability_id) or {}
    supports_export = contract.get("supports_export", False)
    supports_report = contract.get("supports_report", False)

    actions = []

    if presentation_intent == "EXPORT":
        if supports_export or export_available:
            actions.append({
                "label": "Download Excel Report",
                "type": "EXPORT",
                "intent": "EXPORT",
                "action": "export",
                "export_data": export_payload,
                "capability_id": capability_id
            })
        actions.append({
            "label": "View Summary",
            "type": "VIEW",
            "intent": "VIEW",
            "action": "view",
            "capability_id": capability_id
        })

    elif presentation_intent == "GENERATE":
        if supports_export or export_available:
            actions.append({
                "label": "Export to Excel",
                "type": "EXPORT",
                "intent": "EXPORT",
                "action": "export",
                "export_data": export_payload,
                "capability_id": capability_id
            })
        actions.append({
            "label": "View Details",
            "type": "VIEW",
            "intent": "VIEW",
            "action": "view",
            "capability_id": capability_id
        })

    else:  # VIEW / Ambiguous
        if supports_report:
            actions.append({
                "label": "Generate Full Report",
                "type": "GENERATE",
                "intent": "GENERATE",
                "action": "generate",
                "capability_id": capability_id
            })
        if export_available and supports_export:
            actions.append({
                "label": "Export to Excel",
                "type": "EXPORT",
                "intent": "EXPORT",
                "action": "export",
                "export_data": export_payload,
                "capability_id": capability_id
            })

    return actions


def wrap_presentation_intent(res_dict: Dict[str, Any], canonical_intent: Optional[Dict[str, Any]] = None, capability_id: str = "report") -> Dict[str, Any]:
    """
    Wraps response payload with metadata-driven presentation_intent, actions array,
    and structured export metadata evaluated by PresentationPolicy.
    """
    if not isinstance(res_dict, dict):
        return res_dict

    from engine.presentation_policy import PresentationPolicy

    can_dict = canonical_intent if isinstance(canonical_intent, dict) else None
    p_intent = resolve_presentation_intent(can_dict, capability_id=capability_id)
    
    export_policy = PresentationPolicy.evaluate_export_policy(p_intent, payload=res_dict)
    export_avail = export_policy.get("export_available", False)
    
    export_payload = res_dict.get("export_data")
    actions = build_presentation_actions(
        capability_id,
        p_intent,
        export_payload=export_payload,
        export_available=export_avail
    )

    final_export = export_payload if export_avail else None

    res_dict["presentation_action"] = export_policy["presentation_action"]
    res_dict["presentation_intent"] = p_intent
    res_dict["presentation_type"] = "single_entity_kpi" if export_policy.get("row_count") == 1 else "table"
    res_dict["export_available"] = export_avail
    res_dict["export_format"] = export_policy["export_format"]
    res_dict["row_count"] = export_policy["row_count"]
    res_dict["column_count"] = export_policy["column_count"]
    res_dict["actions"] = actions
    res_dict["export_data"] = final_export
    return res_dict


def get_contract_engine() -> ContractEngine:
    return ContractEngine()


