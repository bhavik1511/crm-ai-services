"""
synthesizer.py — Merges output from multiple executed tools into a final LLM response.
"""
import json
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

async def synthesize_response(original_query: str, tool_results: List[Dict[str, Any]], llm_client=None) -> Dict[str, Any]:
    """
    Takes the raw JSON output of all executed tools and formats a final answer.
    If an llm_client is provided, it uses it to dynamically format the markdown.
    """
    logger.info(f"Synthesizing response dynamically for {len(tool_results)} tool outputs.")
    
    chart_data = None
    navigate_to = None
    
    # If we have an LLM Client configured, we dynamically generate the text:
    if llm_client:
        from langchain_core.messages import SystemMessage, HumanMessage
        
        system_prompt = (
            "You are an Enterprise CRM Assistant. Format the following raw JSON tool outputs into a concise, professional markdown response for the user.\n"
            "Rules:\n"
            "1. Merge data from multiple tools naturally.\n"
            "2. Remove duplicate information.\n"
            "3. Format data as markdown tables when appropriate.\n"
            "4. If a capability failed or returned an error, explain it gracefully but still present any successful data.\n"
            "5. NEVER invent or hallucinate data. Only use the provided JSON.\n"
            "6. Do not expose internal technical terms like 'capabilities', 'JSON', or 'SQL'.\n"
            "7. Do not explain your reasoning process."
        )
        
        # Truncate overly large JSON payload to prevent exceeding LLM rate limits (e.g. 8k TPM)
        raw_json_str = json.dumps(tool_results, default=str, indent=2)
        if len(raw_json_str) > 12000:
            compact_json_str = json.dumps(tool_results, default=str)
            if len(compact_json_str) > 12000:
                raw_json_str = compact_json_str[:12000] + "\n... [TRUNCATED DATA DUE TO PAYLOAD SIZE LIMIT]"
            else:
                raw_json_str = compact_json_str

        prompt = f"User Query: {original_query}\n\nTool Results:\n{raw_json_str}\n\nFormat this into a clear, professional answer."
        
        try:
            from agent.pseudonymizer import prepare_for_external_llm, unmask_data, PrivacySecurityError
            privacy_res = prepare_for_external_llm(prompt)
            if not privacy_res.safe:
                logger.error(f"Privacy validation failed in synthesizer: {privacy_res.blocked_reason}")
                final_text = "Data formatting paused due to security privacy policy enforcement."
            else:
                response = await llm_client.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=privacy_res.masked_text)])
                final_text = unmask_data(response.content, privacy_res.token_mapping)
                privacy_res.clear_mapping()
        except Exception as e:
            logger.error(f"Dynamic synthesis failed: {e}")
            final_text = "I encountered an issue formatting your response securely."
    else:
        # Fallback to structured formatting
        final_text = "Here is the raw information you requested:\n\n```json\n" + json.dumps(tool_results, default=str, indent=2) + "\n```"
    
    # We can still extract basic routing metadata if needed
    for res in tool_results:
        tool_name = res.get("capability")
        data = res.get("result", {})
        
        if tool_name == "receivables_analysis" and not res.get("error"):
            navigate_to = "/billing/reports/receivable-report"
            
        elif tool_name == "revenue_analysis" and not res.get("error"):
            if isinstance(data, dict) and "revenue_by_month" in data:
                # Dynamically generate chart payload for the frontend
                chart_data = {
                    "type": "bar",
                    "labels": [m.get("month", "") for m in data["revenue_by_month"]],
                    "datasets": [
                        {
                            "label": "Revenue",
                            "data": [m.get("amount", 0) for m in data["revenue_by_month"]]
                        }
                    ]
                }
    
    return {
        "type": "done",
        "content": final_text,
        "chart_data": chart_data,
        "navigate_to": navigate_to,
        "suggested_questions": ["Show full report", "Compare with last year", "Create a task for this"]
    }
