"""
planner.py — The Enterprise Business Reasoning Engine.
Designed using SOLID principles. 
Strictly handles business understanding, capability detection, and missing information.
Completely unaware of tool implementations, APIs, or semantic layers.
"""
import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from dataclasses import dataclass

# LangChain imports for dynamic structured output
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_groq import ChatGroq

from .entity_resolver import resolve_entities
from .execution_validator import validate_execution
from .synthesizer import synthesize_response
from .diagnostics import DiagnosticsTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Future-Proof Request Context
# ---------------------------------------------------------------------------
@dataclass
class RequestContext:
    question: str
    jwt_token: str
    session_id: str
    history: List[Dict[str, str]] = None
    user_context: Dict[str, Any] = None
    request_metadata: Dict[str, Any] = None
    feature_flags: Dict[str, Any] = None
    tenant_info: Dict[str, Any] = None
    client_info: Dict[str, Any] = None

# ---------------------------------------------------------------------------
# Structured Execution Plan Schema (Single Source of Truth)
# ---------------------------------------------------------------------------
class EntityInfo(BaseModel):
    type: str = Field(description="The type of entity, e.g., 'customer', 'project', 'employee'.")
    value: str = Field(description="The name or code of the entity to search for.")

class CapabilityCallInfo(BaseModel):
    id: str = Field(description="The exact ID of the business capability from the catalog.")
    context: Dict[str, Any] = Field(default_factory=dict, description="Required business context (parameters) for the capability.")

class BusinessExecutionPlan(BaseModel):
    business_goal: str = Field(description="A short summary of the user's business objective.")
    confidence_score: float = Field(description="Confidence score between 0.0 and 1.0 that this plan perfectly addresses the user's intent.")
    entities: List[EntityInfo] = Field(default_factory=list, description="Entities that must be resolved before proceeding.")
    business_capabilities: List[CapabilityCallInfo] = Field(default_factory=list, description="The abstract business capabilities required to satisfy the goal.")
    missing_information: List[str] = Field(default_factory=list, description="Any critical business context missing from the user's query (e.g., 'Financial Year').")


# ---------------------------------------------------------------------------
# LLM Factory (Abstracts Provider Logic)
# ---------------------------------------------------------------------------
def build_llm(temperature: float = 0.0):
    """Creates the LLM client using environment configuration."""
    from config.llm_factory import get_llm
    return get_llm(temperature=temperature)


# ---------------------------------------------------------------------------
# Core Orchestrator
# ---------------------------------------------------------------------------
class EnterprisePlanner:
    def __init__(self, llm_client=None):
        self.llm = llm_client or build_llm()
        self.structured_llm = self.llm.with_structured_output(BusinessExecutionPlan)

    async def execute_turn(self, context: RequestContext) -> Dict[str, Any]:
        """
        Main dynamic execution loop for a single conversational turn.
        Flow: User -> Planner -> Entity Resolver -> Execution Validator -> Tool Registry -> Executor -> Synthesizer
        """
        tracker = DiagnosticsTracker(context.session_id)
        context.request_metadata["request_id"] = tracker.request_id
        tracker.record_request_context(context.question, len(context.history or []), context.user_context.get("role", "Unknown"))
        
        logger.info(f"[Req: {tracker.request_id}] Starting business reasoning for query: {context.question}")
        
        # 1. Check for Clarification State Persistence
        previous_plan = context.user_context.get("previous_execution_plan")
        is_internal = context.request_metadata.get("is_internal", False)
        
        execution_plan = None
        
        if previous_plan:
            logger.info(f"[Req: {tracker.request_id}] Found previous execution plan in context.")
            
            is_new = False
            if not is_internal:
                # If free-text, run Intent Gate
                is_new = await self._is_new_request(context.question, previous_plan)
                
            if not is_new:
                logger.info(f"[Req: {tracker.request_id}] Resuming previous execution plan deterministically.")
                execution_plan = previous_plan
                
                # Deterministic Context Injection:
                # Map any provided keys (like 'financial_year') into the capability context.
                for cap in execution_plan.get("business_capabilities", []):
                    # We inject the raw context sent by the frontend UI
                    if context.user_context.get("financial_year"):
                        cap.setdefault("context", {})["financial_year"] = context.user_context["financial_year"]
                    
                    # Or we just inject the user's free text answer if they typed it
                    # (In a real system, the frontend parser maps "FY 2025-26" to financial_year, or we inject the raw query)
                    # To keep it simple, we inject the raw text into a generic key if we don't have a structured key,
                    # but since the backend capabilities map 'financial_year', we will try to extract it, or rely on UI structured data.
                    if not is_internal and not context.user_context.get("financial_year"):
                        cap.setdefault("context", {})["clarification_answer"] = context.question
                        
                execution_plan["missing_information"] = []
            else:
                logger.info(f"[Req: {tracker.request_id}] Intent Gate detected a NEW business request. Discarding previous state.")
        
        if not execution_plan:
            # Generate Business Execution Plan from scratch
            try:
                with tracker.track_time("planner_ms"):
                    plan_obj = await self._generate_execution_plan(context.question)
                execution_plan = plan_obj.model_dump() 
                tracker.record_planner_output(execution_plan)
            except Exception as e:
                logger.error(f"[Req: {tracker.request_id}] Failed to generate business plan: {e}")
                return {"type": "done", "content": "I encountered an error trying to plan your request. Please try rephrasing.", "is_clarification": True}

        logger.info(f"Active Business Execution Plan: {json.dumps(execution_plan, indent=2)}")

        # 2. Entity Resolver
        extracted_entities = execution_plan.get("entities", [])
        resolved_entities = execution_plan.get("resolved_entities", [])
        clarifications = []
        
        # Only resolve if we don't already have them from a resumed plan
        if not resolved_entities and extracted_entities:
            with tracker.track_time("entity_resolver_ms"):
                resolved_entities, clarifications = await resolve_entities(extracted_entities, context.jwt_token)
            
        tracker.record_entity_resolution(resolved_entities, clarifications)
        
        if clarifications:
            logger.info(f"[Req: {tracker.request_id}] Entity Resolver yielded clarifications. Halting execution.")
            tracker.dump_trace()
            return {"type": "done", "content": "\n\n".join(clarifications), "is_clarification": True, "execution_plan": execution_plan}
            
        execution_plan["resolved_entities"] = resolved_entities
        
        # 3. Execution Validator
        is_valid, validation_errors = validate_execution(execution_plan)
        tracker.record_validation(is_valid, validation_errors)
        
        if not is_valid:
            logger.warning(f"[Req: {tracker.request_id}] Execution blocked by Validator.")
            tracker.dump_trace()
            return {"type": "done", "content": "⚠️ **Execution Blocked**\n\n" + "\n".join([f"- {err}" for err in validation_errors]), "is_clarification": True, "execution_plan": execution_plan}
            
        # 4. Tool Registry Resolution & Execution
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from registry.tool_registry import tool_registry

        logger.info(f"[Req: {tracker.request_id}] Resolving physical implementations via Tool Registry...")
        execution_graph = tool_registry.resolve_implementations(execution_plan["business_capabilities"])
        tracker.record_registry_selection(execution_graph)
        
        logger.info(f"[Req: {tracker.request_id}] Executing resolved implementations...")
        with tracker.track_time("tool_execution_ms"):
            tool_results = await tool_registry.execute_resolved_implementations(execution_graph, resolved_entities, context.jwt_token)
            
        tracker.record_tool_execution(tool_results)
        
        # 5. Synthesize Response
        with tracker.track_time("synthesizer_ms"):
            final_response = await synthesize_response(context.question, tool_results, self.llm)
            
        tracker.record_synthesis(final_response)
        tracker.dump_trace()
        
        return final_response

    async def _generate_execution_plan(self, query: str) -> BusinessExecutionPlan:
        """
        Dynamically calls the LLM with the Business Capabilities Catalog to generate a structured execution plan.
        The Planner NEVER sees APIs, SQL, or wrappers.
        """
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from registry.capability_catalog import get_planner_capabilities_schema
        
        capability_schemas = json.dumps(get_planner_capabilities_schema(), indent=2)
        
        prompt = ChatPromptTemplate.from_messages([
            ("system", 
             "You are the Enterprise Business Analyst for a CRM system.\n"
             "Your ONLY job is to understand the user's goal and map it to abstract Business Capabilities.\n\n"
             "AVAILABLE BUSINESS CAPABILITIES:\n{capabilities}\n\n"
             "RULES:\n"
             "1. Extract all business entities into the 'entities' array.\n"
             "2. Select the required capabilities and provide their requested 'context'.\n"
             "3. DO NOT hallucinate capabilities that are not in the list.\n"
             "4. Calculate a confidence_score based on how accurately the capabilities match the goal.\n"
             "5. If a required capability context (e.g., date) is entirely missing from the query, list it in 'missing_information'."
            ),
            ("user", "{query}")
        ])
        
        chain = prompt | self.structured_llm
        plan = await chain.ainvoke({"capabilities": capability_schemas, "query": query})
        
        return plan

    async def _is_new_request(self, query: str, previous_plan: Dict[str, Any]) -> bool:
        """
        Fast Intent Gate. Determines if the user's free-text input is an answer to a clarification
        or an entirely new business request.
        """
        from langchain_core.messages import SystemMessage, HumanMessage
        
        missing_info = previous_plan.get("missing_information", [])
        
        sys_msg = (
            "You are a routing gatekeeper. The user previously initiated a request that was blocked due to missing information.\n"
            f"The missing information requested from the user was: {missing_info}\n\n"
            "Analyze the user's latest message. If it directly provides the missing information or answers the clarification (e.g., 'FY 2025-26', 'Last year', 'Option 1'), return 'ANSWER'.\n"
            "If it represents a completely new, unrelated business request or topic (e.g., 'Show revenue of Air India', 'Open dashboard'), return 'NEW_REQUEST'.\n"
            "Return ONLY the exact string 'ANSWER' or 'NEW_REQUEST'. Do not explain."
        )
        
        try:
            res = await self.llm.ainvoke([SystemMessage(content=sys_msg), HumanMessage(content=query)])
            return "NEW_REQUEST" in res.content.upper()
        except Exception as e:
            logger.error(f"Intent Gate failed: {e}. Defaulting to ANSWER.")
            return False
