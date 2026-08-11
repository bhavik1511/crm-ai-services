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

EXPORT_KEYWORDS = (
    "export", "download", "to excel", "as excel", "excel", "xlsx", "csv", "download report"
)

GENERATE_KEYWORDS = (
    "generate", "create", "build", "run report", "produce", "generate report"
)

VIEW_KEYWORDS = (
    "show", "get", "view", "display", "give me", "fetch", "list", "what is", "tell me"
)

def resolve_presentation_intent(question: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    """
    Metadata-driven intent classification.
    Determines whether user's presentation intent is 'EXPORT', 'GENERATE', or 'VIEW'.
    Defaults to 'VIEW' when ambiguous.
    """
    if not question or not isinstance(question, str):
        return "VIEW"

    import re
    q_clean = question.strip().lower()

    # Rule 2: Explicit EXPORT intent check
    if any(re.search(r'\b' + re.escape(kw) + r'\b', q_clean) for kw in EXPORT_KEYWORDS):
        return "EXPORT"

    # Rule 1: Explicit GENERATE / CREATE intent check
    if any(re.search(r'\b' + re.escape(kw) + r'\b', q_clean) for kw in GENERATE_KEYWORDS):
        return "GENERATE"

    # Rule 3 & 4: Explicit VIEW or Ambiguous (Default to VIEW)
    return "VIEW"


def build_presentation_actions(
    capability_id: str,
    presentation_intent: str,
    query_params: Optional[Dict[str, Any]] = None,
    export_payload: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Generates presentation action DTOs based on response_contract metadata and resolved intent.
    Completely generic and capability-agnostic.
    """
    engine = get_contract_engine()
    contract = engine.get_contract(capability_id) or {}
    supports_export = contract.get("supports_export", False)
    supports_report = contract.get("supports_report", False)

    actions = []

    if presentation_intent == "EXPORT":
        if supports_export:
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
        if supports_export:
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
        if supports_export:
            actions.append({
                "label": "Export to Excel",
                "type": "EXPORT",
                "intent": "EXPORT",
                "action": "export",
                "export_data": export_payload,
                "capability_id": capability_id
            })

    return actions


def wrap_presentation_intent(res_dict: Dict[str, Any], question: str, capability_id: str = "report") -> Dict[str, Any]:
    """
    Wraps response payload with metadata-driven presentation_intent, actions array,
    and conditional export payload enforcement.
    """
    if not isinstance(res_dict, dict):
        return res_dict

    p_intent = resolve_presentation_intent(question)
    export_payload = res_dict.get("export_data")
    actions = build_presentation_actions(capability_id, p_intent, export_payload=export_payload)

    # RULES:
    # 1. GENERATE / EXPORT: Show report result & automatically provide/attach Export action.
    # 2. VIEW / Ambiguous: Show result normally. Do NOT automatically attach primary export_data.
    final_export = export_payload if p_intent in ("GENERATE", "EXPORT") else None

    res_dict["presentation_intent"] = p_intent
    res_dict["actions"] = actions
    res_dict["export_data"] = final_export
    return res_dict


def get_contract_engine() -> ContractEngine:
    return ContractEngine()


