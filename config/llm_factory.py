import os
from typing import Optional

def get_llm(model_name: Optional[str] = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None, is_vision: bool = False):
    """
    Universal LLM Factory.
    Reads from .env and returns the correct Langchain ChatModel (Groq, OpenAI, Gemini, Anthropic, Ollama, etc).
    This architecture is completely provider-agnostic.
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()

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
        return FakeListChatModel(responses=[mock_plan_json] * 100)
    
    # 1. Resolve Model Name
    if not model_name:
        if is_vision:
            model_name = os.getenv("VISION_MODEL", "llama-3.2-90b-vision-preview")
        else:
            model_name = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL", "llama-3.3-70b-versatile")
            
    # 2. Resolve API Key
    # We prioritize LLM_API_KEY to be provider-agnostic, but fallback to specific keys for backward compatibility.
    api_key = os.getenv("LLM_API_KEY")
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
    
    print(f"[DEBUG LLM_FACTORY] Provider: {provider} | Model: {model_name}")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY or OPENAI_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY or GEMINI_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["google_api_key"] = api_key
        return ChatGoogleGenerativeAI(**kwargs)

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
        return ChatAnthropic(**kwargs)

    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or base_url
        if not api_key or not endpoint:
            raise RuntimeError("LLM_API_KEY and AZURE_OPENAI_ENDPOINT are required in .env for Azure")
        kwargs["azure_deployment"] = model_name
        kwargs["api_key"] = api_key
        kwargs["azure_endpoint"] = endpoint
        kwargs["api_version"] = os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
        return AzureChatOpenAI(**kwargs)

    else:
        # Default: Groq
        from langchain_groq import ChatGroq
        if not api_key:
            raise RuntimeError("LLM_API_KEY or GROQ_API_KEY missing from .env")
        kwargs["model_name"] = model_name
        kwargs["groq_api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        return ChatGroq(**kwargs)
