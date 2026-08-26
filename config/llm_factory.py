import os
from typing import Optional, Any, Tuple, Dict
from dotenv import load_dotenv

# Automatically load environment variables from .env
load_dotenv()

import re

def clean_think_tags(text: str) -> str:
    """Strips <think>...</think> and Thinking Process: reasoning blocks from LLM responses."""
    if not text or not isinstance(text, str):
        return text
    # 1. Strip <think>...</think>
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if '<think>' in cleaned and '</think>' not in cleaned:
        cleaned = re.sub(r'<think>.*', '', cleaned, flags=re.DOTALL).strip()

    # 2. Strip "Here's a thinking process:" / "Thinking Process:" / "Thought process:" headers and reasoning
    cleaned = re.sub(r'(?:Here\'?s a |Here is a |My |The )?(?:thinking|thought)\s+process:.*?(?:\n\s*\n|\Z)', '', cleaned, flags=re.DOTALL | re.IGNORECASE).strip()

    # 3. Strip any residual leading lines starting with "Here's a thinking process:" or numbers like 1. 2.
    cleaned = re.sub(r'^(?:Here\'?s a\s*)?(?:thinking|thought)\s+process:?\s*', '', cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r'^(?:\d+\.\s*.*?\n?)+', '', cleaned).strip()
    cleaned = re.sub(r'^(?:Here\'?s a\s*)?(?:thinking|thought)\s+process:?\s*', '', cleaned, flags=re.IGNORECASE).strip()
    return cleaned

def extract_token_usage(msg_or_response) -> dict:
    """
    Extracts token usage metadata (input_tokens, output_tokens, total_tokens, model_name)
    from a LangChain response object, AIMessage, or dictionary.
    Supports Groq, OpenAI, Anthropic, Gemini, Azure, and OpenRouter formats.
    """
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    model_name = ""

    if not msg_or_response:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_name": ""}

    # 1. Check usage_metadata attribute (LangChain AIMessage standard)
    usage_meta = getattr(msg_or_response, "usage_metadata", None)
    if isinstance(usage_meta, dict) and usage_meta:
        input_tokens = usage_meta.get("input_tokens") or usage_meta.get("prompt_tokens") or 0
        output_tokens = usage_meta.get("output_tokens") or usage_meta.get("completion_tokens") or 0
        total_tokens = usage_meta.get("total_tokens") or (input_tokens + output_tokens)

    # 2. Check response_metadata attribute (ChatGroq, ChatOpenAI, etc.)
    resp_meta = getattr(msg_or_response, "response_metadata", None)
    if isinstance(resp_meta, dict) and resp_meta:
        if not model_name:
            model_name = resp_meta.get("model_name") or resp_meta.get("model") or ""
        token_usage = resp_meta.get("token_usage") or resp_meta.get("usage") or resp_meta.get("tokenUsage") or {}
        if isinstance(token_usage, dict) and token_usage:
            if input_tokens == 0:
                input_tokens = token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
            if output_tokens == 0:
                output_tokens = token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
            if total_tokens == 0:
                total_tokens = token_usage.get("total_tokens") or (input_tokens + output_tokens)

    # 3. Handle dictionary inputs (e.g., token_usage or telemetry dicts)
    if isinstance(msg_or_response, dict):
        tu = msg_or_response.get("token_usage") or msg_or_response.get("telemetry") or msg_or_response
        if isinstance(tu, dict):
            in_t = tu.get("input_tokens") or tu.get("planner_tokens") or tu.get("prompt_tokens") or 0
            out_t = tu.get("output_tokens") or tu.get("synthesizer_tokens") or tu.get("completion_tokens") or 0
            tot_t = tu.get("total_tokens") or (in_t + out_t)
            m_name = tu.get("model_name") or tu.get("model") or ""
            if in_t or out_t or tot_t:
                input_tokens = max(input_tokens, int(in_t))
                output_tokens = max(output_tokens, int(out_t))
                total_tokens = max(total_tokens, int(tot_t))
            if m_name and not model_name:
                model_name = m_name

    if total_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
        total_tokens = input_tokens + output_tokens

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
        "model_name": str(model_name)
    }


from langchain_core.runnables import Runnable
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
import logging

logger = logging.getLogger("SecureLLMWrapper")

class SecureLLMWrapper(Runnable):
    """
    Transparent Fail-Closed Privacy Wrapper for External LLM Clients.
    Guarantees 100% PII / CRM confidential data pseudonymization before outbound LLM requests,
    performs post-response leak validation, and executes request-scoped in-memory unmasking.
    Does NOT create duplicate LLM calls.
    """
    def __init__(self, underlying_llm: Any, stage: str = "llm_call"):
        self.underlying_llm = underlying_llm
        self.stage = stage

    def __getattr__(self, name: str) -> Any:
        return getattr(self.underlying_llm, name)

    def _mask_input_payload(self, input_obj: Any) -> Tuple[Any, Any]:
        from agent.pseudonymizer import prepare_for_external_llm, PrivacySecurityError

        if isinstance(input_obj, str):
            res = prepare_for_external_llm(input_obj)
            if not res.safe:
                raise PrivacySecurityError(f"Fail-closed: Outbound LLM request blocked by privacy middleware: {res.blocked_reason}")
            return res.masked_text, res

        if hasattr(input_obj, "to_string"):
            full_str = input_obj.to_string()
            res = prepare_for_external_llm(full_str)
            if not res.safe:
                raise PrivacySecurityError(f"Fail-closed: Outbound LLM request blocked by privacy middleware: {res.blocked_reason}")

            if hasattr(input_obj, "to_messages"):
                msgs = input_obj.to_messages()
                masked_msgs = []
                for m in msgs:
                    m_str = str(getattr(m, "content", ""))
                    for tok, val in res.token_mapping.items():
                        if val and isinstance(val, str) and val in m_str:
                            m_str = m_str.replace(val, tok)
                    if isinstance(m, SystemMessage):
                        masked_msgs.append(SystemMessage(content=m_str))
                    elif isinstance(m, HumanMessage):
                        masked_msgs.append(HumanMessage(content=m_str))
                    elif isinstance(m, AIMessage):
                        masked_msgs.append(AIMessage(content=m_str))
                    else:
                        m_copy = type(m)(content=m_str) if hasattr(type(m), "__call__") else m
                        masked_msgs.append(m_copy)
                return masked_msgs, res
            return res.masked_text, res

        if isinstance(input_obj, list):
            text_pieces = []
            for item in input_obj:
                if isinstance(item, BaseMessage):
                    text_pieces.append(str(item.content))
                elif isinstance(item, dict):
                    text_pieces.append(str(item.get("content", "")))
                else:
                    text_pieces.append(str(item))

            full_text = "\n\n".join(text_pieces)
            res = prepare_for_external_llm(full_text)
            if not res.safe:
                raise PrivacySecurityError(f"Fail-closed: Outbound LLM request blocked by privacy middleware: {res.blocked_reason}")

            masked_list = []
            for item in input_obj:
                if isinstance(item, BaseMessage):
                    m_str = str(item.content)
                    for tok, val in res.token_mapping.items():
                        if val and isinstance(val, str) and val in m_str:
                            m_str = m_str.replace(val, tok)
                    if isinstance(item, SystemMessage):
                        masked_list.append(SystemMessage(content=m_str))
                    elif isinstance(item, HumanMessage):
                        masked_list.append(HumanMessage(content=m_str))
                    elif isinstance(item, AIMessage):
                        masked_list.append(AIMessage(content=m_str))
                    else:
                        masked_list.append(type(item)(content=m_str))
                elif isinstance(item, dict):
                    item_copy = dict(item)
                    m_str = str(item_copy.get("content", ""))
                    for tok, val in res.token_mapping.items():
                        if val and isinstance(val, str) and val in m_str:
                            m_str = m_str.replace(val, tok)
                    item_copy["content"] = m_str
                    masked_list.append(item_copy)
                else:
                    masked_list.append(item)
            return masked_list, res

        full_str = str(input_obj)
        res = prepare_for_external_llm(full_str)
        if not res.safe:
            raise PrivacySecurityError(f"Fail-closed: Outbound LLM request blocked by privacy middleware: {res.blocked_reason}")
        return res.masked_text, res

    async def ainvoke(self, input_obj: Any, config: Optional[Any] = None, **kwargs: Any) -> Any:
        from agent.pseudonymizer import validate_and_unmask_response
        masked_input, privacy_res = self._mask_input_payload(input_obj)
        try:
            raw_response = await self.underlying_llm.ainvoke(masked_input, config=config, **kwargs)
            return validate_and_unmask_response(raw_response, privacy_res)
        except Exception:
            if privacy_res:
                privacy_res.clear_mapping()
            raise

    def invoke(self, input_obj: Any, config: Optional[Any] = None, **kwargs: Any) -> Any:
        from agent.pseudonymizer import validate_and_unmask_response
        masked_input, privacy_res = self._mask_input_payload(input_obj)
        try:
            raw_response = self.underlying_llm.invoke(masked_input, config=config, **kwargs)
            return validate_and_unmask_response(raw_response, privacy_res)
        except Exception:
            if privacy_res:
                privacy_res.clear_mapping()
            raise


def get_llm(model_name: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None, is_vision: bool = False, stage: str = "llm_call"):
    """
    Universal LLM Factory driven by .env variables.
    Reads LLM_PROVIDER, LLM_MODEL, and LLM_API_KEY directly from .env with fallbacks for Groq, OpenAI, Gemini, Anthropic, Azure, and Ollama.
    All returned models are wrapped with SecureLLMWrapper for fail-closed privacy boundary enforcement.
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower().strip()

    # 0. Check for Mock / Offline Mode (ZERO token usage guarantee)
    is_mock = os.getenv("MOCK_LLM_MODE", "false").lower() in ("true", "1", "yes")
    if is_mock or provider == "mock":
        from langchain_community.chat_models.fake import FakeListChatModel
        import json
        mock_plan_json = json.dumps({
            "business_goal": "Mock offline execution plan",
            "confidence_score": 0.95,
            "entities": [],
            "scope": ["Organization"],
            "business_capabilities": [
                {
                    "id": "kpi_summary",
                    "scope": "organization",
                    "filters": {},
                    "context": {},
                    "intent": "generate_report"
                }
            ],
            "missing_information": [],
            "entity_errors": []
        })
        print("[MOCK LLM] MOCK_LLM_MODE is active — Returning Zero-Token Fake LLM Stub with structured JSON plan.")
        return SecureLLMWrapper(FakeListChatModel(responses=[mock_plan_json] * 100), stage=stage)

    # 1. Resolve Model Name dynamically from .env
    if not model_name:
        if is_vision:
            model_name = os.getenv("VISION_MODEL") or os.getenv("LLM_MODEL")
        else:
            model_name = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL", "openai/gpt-oss-20b")

    if not model_name:
        model_name = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")

    # 2. Resolve API Key
    api_key = os.getenv("LLM_API_KEY") or os.getenv("EMAIL_API_KEY") or os.getenv("CHATBOT_API_KEY")
    if not api_key:
        if provider == "groq":
            api_key = os.getenv("CHATBOT_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("EMAIL_API_KEY")
        elif provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("CHATBOT_API_KEY")
        elif provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        elif provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")

    # 3. Resolve base configuration
    kwargs = {}
    temp = temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.0"))
    kwargs["temperature"] = temp

    tokens = max_tokens if max_tokens is not None else os.getenv("LLM_MAX_TOKENS")
    if tokens:
        kwargs["max_tokens"] = int(tokens)

    base_url = os.getenv("LLM_BASE_URL")
    print(f"[DEBUG LLM_FACTORY] Provider: '{provider}' | Model: '{model_name}'")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY or OPENAI_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        underlying = ChatOpenAI(**kwargs)

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY or GEMINI_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["google_api_key"] = api_key
        underlying = ChatGoogleGenerativeAI(**kwargs)

    elif provider == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError:
            raise RuntimeError("Please install langchain-anthropic to use Claude.")
        if not api_key:
            raise RuntimeError("LLM_API_KEY or ANTHROPIC_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        underlying = ChatAnthropic(**kwargs)

    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or base_url
        if not api_key or not endpoint:
            raise RuntimeError("LLM_API_KEY and AZURE_OPENAI_ENDPOINT are required in .env for Azure")
        kwargs["azure_deployment"] = model_name
        kwargs["api_key"] = api_key
        kwargs["azure_endpoint"] = endpoint
        kwargs["api_version"] = os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
        underlying = AzureChatOpenAI(**kwargs)

    else:
        # Default: Groq
        from langchain_groq import ChatGroq
        if not api_key:
            raise RuntimeError("LLM_API_KEY or GROQ_API_KEY is missing from .env")
        kwargs["model_name"] = model_name
        kwargs["groq_api_key"] = api_key
        kwargs["max_retries"] = 1
        if base_url:
            kwargs["base_url"] = base_url
        underlying = ChatGroq(**kwargs)

    return SecureLLMWrapper(underlying, stage=stage)

