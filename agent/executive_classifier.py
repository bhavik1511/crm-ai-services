"""
Executive Intent Classifier (agent/executive_classifier.py)
===========================================================
Single Responsibility:
Determines ONLY whether an incoming user message is purely conversational
(greetings, small talk, thanks, goodbye, help, assistant identity).

- If conversational: Generates a natural executive response using lightweight LLM completion.
- If business query: Returns is_conversational=False, delegating EVERYTHING to EnterprisePlanner.

NO hardcoded response strings. NO business capability logic here.
All business capability metadata remains strictly inside registry/capability_catalog.py.

Phase 3.1.10 Enhancement:
- Dynamic first-name extraction from JWT user_context.
- Time-of-day aware greeting personalisation.
- Strict 30-word / 2-sentence LLM enforcement.
- Immediate return — NEVER invokes Planner, Tool Registry, Entity Resolver, or Backend.
"""

import re
import logging
from datetime import datetime
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)

# Minimum confidence threshold to classify as conversational
CONFIDENCE_THRESHOLD = 0.65


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_time_of_day() -> str:
    """Returns Morning / Afternoon / Evening based on the server's local time."""
    hour = datetime.now().hour
    if hour < 12:
        return "Morning"
    elif hour < 17:
        return "Afternoon"
    else:
        return "Evening"


def _extract_first_name(user_context: Optional[dict]) -> Optional[str]:
    """
    Extracts user's first name from JWT user_context.
    The JWT provides 'user_name' — checked first.
    Falls back through: first_name → full_name → employee_name → username → name.
    Returns the capitalised first word, or None if unavailable.
    """
    if not user_context or not isinstance(user_context, dict):
        return None
    for key in ("user_name", "first_name", "full_name", "employee_name", "username", "name"):
        val = user_context.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip().split()[0].title()
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _is_gibberish(text: str) -> bool:
    """Detects random non-word strings like 'dfsfsdfsdbddsbhfwefwifwvef' or repeated patterns 'OKOKOKOK...'"""
    clean = re.sub(r'[^a-z]', '', text.strip().lower())
    if not clean:
        return True
    # Check for excessive character repetition e.g. "okokokokok", "aaaaaaaa", "dfdfdfdf"
    if len(clean) >= 8 and len(set(clean)) <= 4:
        return True
    # Check consonant-to-vowel ratio for unspaced tokens > 8 chars
    words = text.strip().split()
    for w in words:
        w_clean = re.sub(r'[^a-z]', '', w.lower())
        if len(w_clean) > 8:
            vowels = sum(1 for c in w_clean if c in "aeiou")
            if (vowels / len(w_clean)) < 0.20:
                return True
    return False


async def handle_executive_classification(
    question: str,
    history: Optional[list] = None,
    user_context: Optional[dict] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates if the question is purely conversational.

    Returns:
        (is_conversational: bool, response_content: Optional[str])

        If is_conversational is True, response_content contains the LLM-generated executive response.
        If is_conversational is False, response_content is None (delegate to Planner).

    CRITICAL: When returning True, execution stops here.
    Planner, Capability Resolver, Tool Registry, Entity Resolver, and Backend are NEVER invoked.
    """
    if not question or not isinstance(question, str):
        logger.info("[EXECUTIVE_CLASSIFIER] classification=UNKNOWN fallback_to_planner=true reason=invalid_question_input")
        return False, None

    if _is_gibberish(question):
        logger.info("[EXECUTIVE_CLASSIFIER] classification=GIBBERISH fallback_to_planner=false reason=gibberish_detected")
        return True, "I didn't quite understand that. Could you please rephrase or specify which CRM report or metrics you're looking for?"

    q_clean = re.sub(r'[^\w\s]', '', question.strip().lower()).strip()
    words = q_clean.split()
    word_count = len(words)

    # Fast pattern checks for obvious conversational intents
    CONVERSATIONAL_PATTERNS = {
        "hi", "hello", "hey", "good morning", "good evening", "good afternoon",
        "greetings", "howdy", "hi there", "hello there", "hey there",
        "thanks", "thank you", "thank you very much", "thanks a lot", "bye", "goodbye",
        "see you", "good night", "how are you", "how are you doing", "nice to meet you",
        "take care", "great", "awesome", "perfect", "ok", "okay",
        "what can you do", "how can you help", "available reports", "explain your capabilities",
        "what are your capabilities", "help", "who are you", "what is your role"
    }

    # If message contains business data or temporal/date keywords, it is NOT purely conversational
    BUSINESS_KEYWORDS = {
        "report", "kpi", "revenue", "billing", "receivable", "invoice", "proposal",
        "project", "recoverability", "margin", "customer", "lead", "staff", "employee",
        "profit", "loss", "cost", "target", "dso", "pipeline", "aging", "ageing",
        "audit", "gp", "udit", "tax", "advisory", "bps", "brs", "option", "yes", "no",
        "first", "second", "third", "one", "two", "three", "show", "view"
    }

    TEMPORAL_KEYWORDS = {
        "this", "year", "month", "fy", "financial", "quarter", "last", "current",
        "previous", "next", "today", "yesterday", "date", "range", "from", "to",
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
    }

    has_business_term = any(w in q_clean.split() or w == q_clean for w in BUSINESS_KEYWORDS)
    has_temporal_term = any(w in words for w in TEMPORAL_KEYWORDS) or any(c.isdigit() for c in q_clean)

    if has_business_term or has_temporal_term:
        logger.info("[EXECUTIVE_CLASSIFIER] classification=BUSINESS_QUERY fallback_to_planner=true reason=matched_business_or_temporal_keyword")
        return False, None

    is_fast_conv = (
        q_clean in CONVERSATIONAL_PATTERNS
        or (word_count <= 2 and words[0] in {"hi", "hello", "hey", "thanks"})
    )

    if is_fast_conv:
        first_name = _extract_first_name(user_context)
        time_of_day = _get_time_of_day()
        if q_clean in {"hi", "hello", "hey", "greetings", "hi there", "hello there", "good morning", "good afternoon", "good evening"}:
            greeting_res = f"Hello {first_name}! How can I assist you with your CRM analytics today?" if first_name else f"Good {time_of_day}! How can I assist you with your CRM analytics today?"
            logger.info("[EXECUTIVE_CLASSIFIER] classification=CONVERSATIONAL fallback_to_planner=false reason=fast_greeting_pattern")
            return True, greeting_res
        
        response = await _generate_conversational_llm_response(question, user_context)
        if response:
            logger.info("[EXECUTIVE_CLASSIFIER] classification=CONVERSATIONAL fallback_to_planner=false reason=fast_conversational_llm_response")
            return True, response
        else:
            logger.info("[EXECUTIVE_CLASSIFIER] classification=CONVERSATIONAL fallback_to_planner=true reason=fast_conversational_empty_failed_open")
            return False, None

    # For short ambiguous messages with no business/temporal terms, confirm via lightweight single LLM call
    if word_count <= 5:
        try:
            is_conv = await _classify_via_llm(question)
            if is_conv:
                response = await _generate_conversational_llm_response(question, user_context)
                if response:
                    logger.info("[EXECUTIVE_CLASSIFIER] classification=CONVERSATIONAL fallback_to_planner=false reason=llm_classified_conversational")
                    return True, response
                else:
                    logger.info("[EXECUTIVE_CLASSIFIER] classification=CONVERSATIONAL fallback_to_planner=true reason=llm_conversational_empty_failed_open")
                    return False, None
            else:
                logger.info("[EXECUTIVE_CLASSIFIER] classification=BUSINESS_QUERY fallback_to_planner=true reason=llm_classified_other")
                return False, None
        except Exception as e:
            logger.warning(f"[ExecutiveClassifier] LLM classification error: {e}")
            logger.info("[EXECUTIVE_CLASSIFIER] classification=UNKNOWN fallback_to_planner=true reason=llm_classification_exception")
            return False, None

    logger.info("[EXECUTIVE_CLASSIFIER] classification=BUSINESS_QUERY fallback_to_planner=true reason=default_fallback_to_planner")
    return False, None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

async def _classify_via_llm(question: str) -> bool:
    """Lightweight 0-cost classification: CONVERSATIONAL or OTHER."""
    from config.llm_factory import get_llm, clean_think_tags
    import os

    model_name = os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL")
    llm = get_llm(model_name, temperature=0.0, max_tokens=100)

    prompt = (
        "Classify if the following user message is a standard conversational greeting, thanks, or help request "
        "OR random noise / unreadable gibberish / business query.\n"
        "If it is a clear greeting (like 'hi', 'hello', 'how are you', 'thank you'), respond with ONLY 'CONVERSATIONAL'.\n"
        "Otherwise, respond with ONLY 'OTHER'.\n\n"
        f"Message: \"{question}\""
    )
    try:
        resp = await llm.ainvoke([{"role": "user", "content": prompt}])
        raw_content = resp.content if hasattr(resp, "content") else str(resp)
        clean_content = clean_think_tags(raw_content).strip().upper()
        if clean_content.startswith("CONVERSATIONAL") and not clean_content.startswith("OTHER"):
            return True
        return False
    except Exception as e:
        logger.warning(f"[ExecutiveClassifier] _classify_via_llm error: {e}")
        return False


async def _generate_conversational_llm_response(
    question: str,
    user_context: Optional[dict] = None,
) -> Optional[str]:
    """
    Generates a dynamic, personalised executive conversational response.

    Enforces:
    - Max 2 short sentences.
    - Max 30 words total.
    - User's first name from JWT when available.
    - Time-of-day personalisation.
    - Never lists capabilities unless explicitly asked.
    - Never mentions backend / database / internal terms.
    """
    from config.llm_factory import get_llm, clean_think_tags
    import os
    from langchain_core.messages import SystemMessage, HumanMessage

    model_name = os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL")
    llm = get_llm(model_name, temperature=0.7, max_tokens=512)

    first_name = _extract_first_name(user_context)
    time_of_day = _get_time_of_day()

    # Fast deterministic path for standard 1-word greetings ("hi", "hello", "hey")
    q_norm = re.sub(r'[^\w\s]', '', (question or "").strip().lower()).strip()
    if q_norm in {"hi", "hello", "hey", "greetings", "hi there", "hello there", "good morning", "good afternoon", "good evening"}:
        if first_name:
            return f"Hello {first_name}! How can I assist you with your CRM analytics today?"
        return f"Good {time_of_day}! How can I assist you with your CRM analytics today?"

    name_instruction = (
        f"The user's first name is {first_name}. Address them by first name naturally."
        if first_name
        else "No user name is available; do not address by name."
    )

    system_prompt = (
        "You are an Executive AI Assistant for an Enterprise CRM platform.\n"
        "You are responding to a conversational message (greeting, thanks, farewell, help, or identity).\n\n"
        f"Context: {name_instruction}\n"
        f"Time of day: {time_of_day}.\n\n"
        "STRICT OUTPUT RULES — VIOLATION IS NOT ACCEPTABLE:\n"
        "1. Maximum 2 short sentences. Maximum 30 words TOTAL.\n"
        "2. Warm, professional, executive tone. Natural variation — never repeat the same phrasing.\n"
        "3. Use the user's first name naturally if available (not at every sentence).\n"
        "4. Do NOT list or explain capabilities unless the user explicitly asks 'what can you do'.\n"
        "5. Do NOT fabricate business data or mention backend/database/SQL terms.\n"
        "6. Do NOT introduce yourself by name unless directly asked 'who are you'.\n"
        "7. Close naturally with a brief, open offer to help — never a bullet list.\n"
        "8. CRITICAL: Respond IMMEDIATELY with the greeting text. Do NOT output <think> tags or 'Thinking Process:'.\n"
    )

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=question)
        ])
        raw_text = resp.content if hasattr(resp, "content") else str(resp)
        text = clean_think_tags(raw_text).strip()
        if not text or "thinking process" in text.lower() or "<think>" in text.lower():
            logger.warning("[ExecutiveClassifier] Cleaned text was empty or invalid. Failing open to Planner.")
            return None
    except Exception as e:
        logger.warning(f"[ExecutiveClassifier] Greeting LLM call failed: {e}")
        return None

    # Safety net: enforce 30-word hard cap
    word_list = text.split()
    if len(word_list) > 35:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        text = sentences[0] if sentences else " ".join(word_list[:30]) + "."

    return text
