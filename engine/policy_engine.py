"""
policy_engine.py — Execution Policy Engine
============================================
Evaluates multi-factor signals (similarity, entity resolution confidence,
candidate separation, ambiguity entropy, reasoning keywords) to decide whether to
bypass the Planner (Fast-Path), invoke the Planner, or ask clarification.

Zero hardcoded capability names or report-specific conditions.
"""

import logging
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

REASONING_OPERATIONS = frozenset({
    "why", "explain", "reason", "root cause", "recommendation"
})

@dataclass
class PolicyDecision:
    action: str  # "FAST_PATH_EXECUTE", "INVOKE_PLANNER", "ASK_CLARIFICATION"
    top_capability: Optional[Dict[str, Any]]
    candidate_capabilities: List[Tuple[Dict[str, Any], float]]
    confidence_score: float
    reason: str
    requires_narrative: bool

class ExecutionPolicyEngine:
    """
    Multi-factor decision policy engine evaluating query context,
    vector similarity scores, candidate margin separation, and entity resolution readiness.
    """
    def __init__(self, confidence_threshold: float = 0.85, separation_threshold: float = 0.15):
        self.confidence_threshold = confidence_threshold
        self.separation_threshold = separation_threshold

    def evaluate_query(
        self,
        query: str,
        retrieved_candidates: List[Tuple[Dict[str, Any], float]],
        user_context: Optional[Dict[str, Any]] = None
    ) -> PolicyDecision:
        """
        Evaluates query signals and returns an execution decision.
        """
        if not retrieved_candidates:
            return PolicyDecision(
                action="INVOKE_PLANNER",
                top_capability=None,
                candidate_capabilities=[],
                confidence_score=0.0,
                reason="No candidate capabilities retrieved from metadata index.",
                requires_narrative=False
            )

        top_cap, top_score = retrieved_candidates[0]
        second_score = retrieved_candidates[1][1] if len(retrieved_candidates) > 1 else 0.0
        candidate_margin = round(top_score - second_score, 4)

        # 1. Check if query explicitly asks for reasoning/explanation/comparison
        q_clean = query.lower()
        words = set(q_clean.split())
        requires_narrative = bool(words.intersection(REASONING_OPERATIONS))

        from utils.structured_logger import log_stage

        if requires_narrative:
            dec = PolicyDecision(
                action="INVOKE_PLANNER",
                top_capability=top_cap,
                candidate_capabilities=retrieved_candidates,
                confidence_score=top_score,
                reason="User query contains analytical/reasoning intent keywords requiring Planner synthesis.",
                requires_narrative=True
            )
            log_stage(logger, "POLICY", Path=dec.action, Reason=dec.reason, Confidence=top_score)
            return dec

        # 2. Check Fast-Path Eligibility from capability metadata
        is_fast_path_eligible = top_cap.get("fast_path_eligible", True)
        if not is_fast_path_eligible:
            dec = PolicyDecision(
                action="INVOKE_PLANNER",
                top_capability=top_cap,
                candidate_capabilities=retrieved_candidates,
                confidence_score=top_score,
                reason=f"Top candidate '{top_cap.get('id')}' is not fast-path eligible per metadata contract.",
                requires_narrative=False
            )
            log_stage(logger, "POLICY", Path=dec.action, Reason=dec.reason, Confidence=top_score)
            return dec

        # 3. Evaluate Multi-Factor Confidence & Candidate Separation
        if top_score >= self.confidence_threshold:
            if len(retrieved_candidates) == 1 or candidate_margin >= self.separation_threshold or top_score >= 0.95:
                dec = PolicyDecision(
                    action="FAST_PATH_EXECUTE",
                    top_capability=top_cap,
                    candidate_capabilities=retrieved_candidates,
                    confidence_score=top_score,
                    reason=f"High confidence match ({top_score:.2f}) with distinct candidate separation ({candidate_margin:.2f}).",
                    requires_narrative=False
                )
                log_stage(logger, "POLICY", Path=dec.action, Reason=dec.reason, Confidence=top_score)
                return dec

        from utils.structured_logger import log_stage
        decision = PolicyDecision(
            action="INVOKE_PLANNER",
            top_capability=top_cap,
            candidate_capabilities=retrieved_candidates,
            confidence_score=top_score,
            reason=f"Confidence ({top_score:.2f}) or margin ({candidate_margin:.2f}) below deterministic threshold.",
            requires_narrative=False
        )
        log_stage(logger, "POLICY", Path=decision.action, Reason=decision.reason, Confidence=top_score)
        return decision

def get_policy_engine() -> ExecutionPolicyEngine:
    return ExecutionPolicyEngine()
