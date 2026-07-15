import os
from typing import Optional

def get_llm(model_name: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None, is_vision: bool = False):
    """
    Universal LLM Factory.
    Reads from .env and returns the correct Langchain ChatModel (Groq, OpenAI, Gemini, etc).
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    
    # Resolve Model Name
    if not model_name:
        if is_vision:
            model_name = os.getenv("VISION_MODEL", "llama-3.2-90b-vision-preview")
        else:
            model_name = os.getenv("PRIMARY_MODEL", "llama-3.3-70b-versatile")
            
    kwargs = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
        
    print(f"[DEBUG LLM_FACTORY] Provider: {provider} | Model: {model_name}")

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["api_key"] = api_key
        return ChatOpenAI(**kwargs)
        
    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is missing from .env")
        kwargs["model"] = model_name
        kwargs["google_api_key"] = api_key
        return ChatGoogleGenerativeAI(**kwargs)
        
    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        if not api_key or not endpoint:
            raise RuntimeError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT are required in .env")
        kwargs["azure_deployment"] = model_name
        kwargs["api_key"] = api_key
        kwargs["azure_endpoint"] = endpoint
        kwargs["api_version"] = os.getenv("AZURE_API_VERSION", "2024-02-15-preview")
        return AzureChatOpenAI(**kwargs)

    else:
        # Default: Groq
        from langchain_groq import ChatGroq
        api_key = os.getenv("CHATBOT_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("EMAIL_API_KEY")
        
        # Safe fallback if user has OpenAI key but no Groq key configured
        if not api_key and os.getenv("OPENAI_API_KEY"):
            print("[DEBUG LLM_FACTORY] Groq unavailable; automatically falling back to OpenAI")
            from langchain_openai import ChatOpenAI
            kwargs["model"] = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            kwargs["api_key"] = os.getenv("OPENAI_API_KEY")
            return ChatOpenAI(**kwargs)
            
        if not api_key:
            raise RuntimeError("LLM API key missing! Please set CHATBOT_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY in .env")
            
        kwargs["model_name"] = model_name
        kwargs["groq_api_key"] = api_key
        return ChatGroq(**kwargs)
