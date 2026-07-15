import asyncio
import json
from agent import get_agent_executor
from semantic_layer import get_comprehensive_customer_report

async def test_llm_injection():
    search_term = "aaj holding"
    print(f"1. Running tool for {search_term}...")
    tool_result = get_comprehensive_customer_report.invoke({"search_term": search_term})
    
    print("2. Building LLM prompt...")
    inject_content = (
        f"The user asked: {search_term}\n\n"
        f"[CRM DATA RETRIEVED for '{search_term}'] "
        f"Format this as a 360° customer report per your system prompt formatting rules. "
        f"ONLY use data from below — NO general knowledge, NO descriptions of what the company does. "
        f"Show NUMBERS and TABLES.\n\nRaw CRM Data:\n{tool_result}"
    )
    
    executor = get_agent_executor()
    langchain_history = [{"role": "user", "content": inject_content}]
    
    print("3. Invoking LLM...")
    response = executor.invoke({"messages": langchain_history})
    final_answer = response["messages"][-1].content.strip()
    
    print("\n--- LLM RESPONSE ---")
    print(final_answer)

if __name__ == "__main__":
    asyncio.run(test_llm_injection())
