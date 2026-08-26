"""
capability_verifier.py — Capability Verification Layer
=========================================================
Verifies candidate capability readiness BEFORE backend tool execution.

Checks:
- Entity requirements satisfied (customer, project, employee ID)
- Mandatory business context / parameters present
- Supported operations & presentation mode compatibility

If verification fails, safe fallback to Planner or clarification is triggered.
"""

import logging
from typing import Dict, Any, List, Tuple
from registry.contract_engine import get_contract_engine

logger = logging.getLogger(__name__)

class CapabilityVerifier:
    """
    Validates capability prerequisites prior to execution.
    """
    def __init__(self, contract_engine=None):
        self.contract_engine = contract_engine or get_contract_engine()

    def verify_capability_readiness(
        self, capability_metadata: Dict[str, Any], resolved_context: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Verifies if capability execution prerequisites are met.
        Returns (is_verified, failure_reasons).
        """
        cap_id = capability_metadata.get("id", "unknown")
        reasons = []

        # 1. Verify dependencies
        dependencies = capability_metadata.get("dependencies", [])
        for dep_id in dependencies:
            # Check if dependency entity/context exists
            if dep_id == "customer_resolution" and not any(k in resolved_context for k in ["customer_id", "customer", "search_term"]):
                reasons.append("Requires customer resolution but no customer search term or ID resolved.")

        # 2. Verify required business context parameters
        req_context = capability_metadata.get("required_business_context", {})
        for param_name, param_meta in req_context.items():
            if isinstance(param_meta, dict) and param_meta.get("required", False):
                if param_name not in resolved_context or not resolved_context[param_name]:
                    reasons.append(f"Missing mandatory business parameter: '{param_name}'")

        # 3. Verify contract compliance
        is_contract_valid, contract_issues = self.contract_engine.validate_capability_contract(cap_id, resolved_context)
        if not is_contract_valid:
            reasons.extend(contract_issues)

        is_verified = len(reasons) == 0
        from utils.structured_logger import log_stage
        status_str = "PASS" if is_verified else "FAIL"
        log_stage(logger, "ENTITY", Status=status_str, Capability=cap_id, Reasons=reasons if reasons else "None")

        return is_verified, reasons

def get_capability_verifier() -> CapabilityVerifier:
    return CapabilityVerifier()
