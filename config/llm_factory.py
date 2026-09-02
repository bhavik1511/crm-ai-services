import os
from typing import Optional
from dotenv import load_dotenv

# Automatically load environment variables from .env
load_dotenv()

def get_llm(model_name: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None, is_vision: bool = False, reasoning_effort: Optional[str] = None):
    """
    LLM Factory driven 100% by .env variables.
    Reads LLM_PROVIDER, LLM_MODEL, and LLM_API_KEY directly from .env without hardcoded model defaults.
    """
    provider = os.getenv("LLM_PROVIDER", "").lower().strip()
    api_key = os.getenv("LLM_API_KEY") or os.getenv("EMAIL_API_KEY")

    if not model_name:
        if is_vision:
            model_name = os.getenv("VISION_MODEL") or os.getenv("LLM_MODEL")
        else:
            model_name = os.getenv("LLM_MODEL")

    if not model_name:
        raise RuntimeError("LLM_MODEL is missing from .env")

    kwargs = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    print(f"[DEBUG LLM_FACTORY] Provider: '{provider}' | Model: '{model_name}'")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY is missing from .env")
        return ChatOpenAI(model=model_name, api_key=api_key, **kwargs)

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        if not api_key:
            raise RuntimeError("LLM_API_KEY is missing from .env")
        return ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, **kwargs)

    else:
        # Default: Groq
        from langchain_groq import ChatGroq
        if not api_key:
            raise RuntimeError("LLM_API_KEY is missing from .env")
        return ChatGroq(model_name=model_name, groq_api_key=api_key, **kwargs)




