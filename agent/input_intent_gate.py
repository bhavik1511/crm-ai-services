"""
Input Intent Gate (agent/input_intent_gate.py)
==============================================
Lightweight structured Input Intent Gate executed BEFORE EnterprisePlanner.
Classifies incoming user input to prevent pasted technical material (stack traces,
debug logs, JSON responses, internal telemetry) from executing CRM capabilities or backend endpoints.

Architectural Rules:
- BUSINESS_QUERY -> continue to EnterprisePlanner
- ENTITY_ONLY -> EntityResolver entity discovery flow
- CLARIFICATION -> resume existing pending execution plan
- TECHNICAL_PASTE -> safe response asking user intent; NO CRM capabilities executed
- AMBIGUOUS -> clarification question; NO CRM capability execution

Classification is strictly LLM/structured-output driven (NO keyword matching rules).
"""

import json
import logging
import re
from enum import Enum
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InputType(str, Enum):
    BUSINESS_QUERY = "BUSINESS_QUERY"
    ENTITY_ONLY = "ENTITY_ONLY"
    CLARIFICATION = "CLARIFICATION"
    TECHNICAL_PASTE = "TECHNICAL_PASTE"
    AMBIGUOUS = "AMBIGUOUS"


class InputIntentResult(BaseModel):
    input_type: InputType = Field(description="Classification of user input")
    confidence: float = Field(default=1.0, description="Confidence score between 0.0 and 1.0")
    requires_planner: bool = Field(default=True, description="True if EnterprisePlanner should execute business query")
    reason: str = Field(default="", description="Reason for input classification")


async def evaluate_input_intent(
    question: str,
    has_pending_clarification: bool = False,
    user_context: Optional[Dict[str, Any]] = None
) -> InputIntentResult:
    """
    Evaluates input intent via structured LLM classification BEFORE Planner/ToolRegistry/API execution.
    """
    if not question or not str(question).strip():
        return InputIntentResult(
            input_type=InputType.AMBIGUOUS,
            confidence=1.0,
            requires_planner=False,
            reason="Empty user input"
        )

    # 1. Active pending clarification turn short-circuit
    if has_pending_clarification:
        logger.info("[INPUT_INTENT_GATE] Input classified as CLARIFICATION due to pending session state.")
        return InputIntentResult(
            input_type=InputType.CLARIFICATION,
            confidence=1.0,
            requires_planner=True,
            reason="User responding to pending clarification"
        )

    # 2. LLM-driven structured classification
    try:
        from config.llm_factory import get_llm, clean_think_tags, extract_token_usage
        import os

        model_name = os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL")
        llm = get_llm(model_name, temperature=0.0, max_tokens=256, stage="intent_gate")

        prompt = (
            "You are the Input Intent Gate for an Enterprise CRM AI Assistant.\n"
            "Your job is to classify the user input into EXACTLY ONE of these 5 categories:\n\n"
            "1. TECHNICAL_PASTE: The user pasted raw technical content, structured JSON objects/arrays, "
            "backend API responses, HTTP payloads, stack traces, execution logs, code snippets, or system telemetry.\n"
            "2. ENTITY_ONLY: The user entered ONLY an entity name, customer name, employee name, or project code without a business request "
            "(e.g., 'Shashank Arya', 'Phoenix Project', 'ACME Corp').\n"
            "3. CLARIFICATION: The user is answering a previous question with a choice, index, or single requested parameter.\n"
            "4. BUSINESS_QUERY: A natural language CRM business question or reporting request expressed by a human user "
            "(e.g., 'show revenue for FY24', 'generate KPI report for Shashank Arya', 'compare FY24 and FY25').\n"
            "5. AMBIGUOUS: Unintelligible text or random noise.\n\n"
            "CRITICAL DIRECTIVES:\n"
            "- If the input is a JSON object/array, log trace, or API response dump, classify as TECHNICAL_PASTE.\n"
            "- Legitimate business questions ask for reports, revenue, billing, or metrics. Classify those as BUSINESS_QUERY.\n\n"
            "Return JSON ONLY with this schema:\n"
            "{\n"
            '  "input_type": "BUSINESS_QUERY | ENTITY_ONLY | CLARIFICATION | TECHNICAL_PASTE | AMBIGUOUS",\n'
            '  "confidence": 0.95,\n'
            '  "reason": "explanation"\n'
            "}\n\n"
            f'User Input:\n{question}'
        )

        req_id = (user_context or {}).get("request_id") or (user_context or {}).get("tracker_request_id") or "unknown"
        logger.info(f"[LLM_CALL] stage=intent_gate request_id={req_id}")

        resp = await llm.ainvoke([{"role": "user", "content": prompt}])
        gate_token_usage = extract_token_usage(resp)
        raw_text = clean_think_tags(resp.content.strip())


        # Extract JSON from LLM output
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
        else:
            json_str = raw_text

        data = json.loads(json_str)
        raw_type = str(data.get("input_type", "BUSINESS_QUERY")).upper().strip()
        confidence = float(data.get("confidence", 0.9))
        reason = str(data.get("reason", ""))

        if raw_type not in InputType.__members__:
            raw_type = "BUSINESS_QUERY"

        input_type = InputType(raw_type)
        requires_planner = input_type in (InputType.BUSINESS_QUERY, InputType.ENTITY_ONLY, InputType.CLARIFICATION)

        result = InputIntentResult(
            input_type=input_type,
            confidence=confidence,
            requires_planner=requires_planner,
            reason=reason
        )

        logger.info(
            f"[INPUT_INTENT_GATE] input_type={result.input_type.value} confidence={result.confidence} "
            f"requires_planner={result.requires_planner} reason='{result.reason}'"
        )
        return result

    except Exception as e:
        logger.warning(f"[INPUT_INTENT_GATE] LLM evaluation error: {e}; evaluating fallback")
        q_strip = question.strip()
        if (q_strip.startswith("{") and q_strip.endswith("}")) or (q_strip.startswith("[") and q_strip.endswith("]")):
            return InputIntentResult(
                input_type=InputType.TECHNICAL_PASTE,
                confidence=0.95,
                requires_planner=False,
                reason="Raw JSON structure detected"
            )

        return InputIntentResult(
            input_type=InputType.BUSINESS_QUERY,
            confidence=0.5,
            requires_planner=True,
            reason=f"Fallback due to evaluation error: {e}"
        )
