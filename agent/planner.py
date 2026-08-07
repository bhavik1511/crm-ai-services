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
    scope: str = Field("organization", description="Scope of query: 'organization' for company-wide/total, 'entity' for specific customer/project, or 'filtered' for service line/dept filters.")
    filters: Dict[str, Any] = Field(default_factory=dict, description="Explicit filters (e.g. financial_year, service_line, department).")
    context: Dict[str, Any] = Field(default_factory=dict, description="Required business context (parameters) for the capability.")
    intent: Optional[str] = Field(None, description="User intent: 'generate_report', 'compare', 'summarize', 'explain', 'analyze', 'list', 'trend'.")
    entity: Optional[str] = Field(None, description="The primary business entity (e.g., service lead, proposal, customer).")
    metric: Optional[str] = Field(None, description="The metric being measured (e.g., revenue, budget, count).")
    operation: Optional[str] = Field(None, description="The mathematical operation (e.g., sum, count, average, filter).")
    aggregation: Optional[str] = Field(None, description="How the data is aggregated.")
    ranking: Optional[str] = Field(None, description="Ranking requirement (e.g., highest, lowest, top 5).")
    comparison: Optional[str] = Field(None, description="Comparison requirement (e.g., vs last year, between Audit and Tax).")
    group_by: Optional[str] = Field(None, description="Grouping requirement (e.g., by service line, by month).")
    sort_order: Optional[str] = Field(None, description="Sort direction (e.g., ascending, descending).")
    limit: Optional[int] = Field(None, description="Limit on the number of results.")
    time_filter: Optional[str] = Field(None, description="Time filter (e.g., this month, Q3, FY 2026).")
    # Phase 3.1.10 — Presentation metadata (determined by Planner, consumed by Synthesizer)
    presentation_mode: Optional[str] = Field(None, description="Presentation mode: REPORT, INSIGHT, REPORT_AND_INSIGHT, KPI_CARD, TABLE, COMPARISON, EXECUTIVE_BRIEF.")
    requires_report: Optional[bool] = Field(None, description="Whether the user explicitly requested a full report UI.")
    requires_summary: Optional[bool] = Field(None, description="Whether an executive summary is required.")
    requires_chart: Optional[bool] = Field(None, description="Whether chart data is required.")
    requires_table: Optional[bool] = Field(None, description="Whether a table is required.")
    requires_comparison: Optional[bool] = Field(None, description="Whether a comparison is required.")
    requires_export: Optional[bool] = Field(None, description="Whether export capability is required.")
    analysis_depth: Optional[str] = Field(None, description="Analysis depth: summary, detailed, executive_briefing.")

class BusinessExecutionPlan(BaseModel):
    business_goal: str = Field(description="A short summary of the user's business objective.")
    confidence_score: float = Field(description="Confidence score between 0.0 and 1.0 that this plan perfectly addresses the user's intent.")
    reasoning_summary: Optional[str] = Field(None, description="Brief explanation of why this plan was selected.")
    ambiguity_detected: bool = Field(False, description="True if the user's intent is ambiguous and clarification would improve accuracy.")
    entities: list[EntityInfo] = Field(description="ONLY genuine, specific business entities (e.g., 'Phoenix Project', 'ABC Ltd'). DO NOT include broad scopes, metrics, or temporal expressions (e.g., 'January', 'Q1') here.")
    scope: list[str] = Field(description="Broad intent modifiers or query scopes (e.g., 'All Projects', 'All Customers', 'Company Wide').")
    business_capabilities: list[CapabilityCallInfo] = Field(description="The abstract business capabilities required to satisfy the goal.")
    missing_information: list[str] = Field(description="Any critical business context missing from the user's query (e.g., 'Financial Year').")
    entity_errors: list[str] = Field(description="Populated internally if entity resolution fails.")
    # Phase 3.1.10 — Plan-level presentation intent
    presentation_mode: Optional[str] = Field(None, description="Overall presentation mode for the response: REPORT, INSIGHT, REPORT_AND_INSIGHT, KPI_CARD, TABLE, COMPARISON, EXECUTIVE_BRIEF.")
    analysis_depth: Optional[str] = Field(None, description="Overall analysis depth: summary, detailed, executive_briefing.")


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

    async def execute_turn(self, context: RequestContext) -> Dict[str, Any]:
        """
        Main dynamic execution loop for a single conversational turn.
        Flow: User -> Planner -> Entity Resolver -> Execution Validator -> Tool Registry -> Executor -> Synthesizer
        """
        tracker = DiagnosticsTracker(context.session_id)
        if context.request_metadata is None:
            context.request_metadata = {}
        context.request_metadata["request_id"] = tracker.request_id
        tracker.record_request_context(context.question, len(context.history or []), context.user_context.get("role", "Unknown"))
        
        logger.info(f"[Req: {tracker.request_id}] Starting business reasoning for query: {context.question}")
        
        # 1. Check for Clarification State Persistence (DIAG-7, 8, 9)
        previous_plan = context.user_context.get("previous_execution_plan")
        is_internal = context.request_metadata.get("is_internal", False)
        
        # [DIAG-7] Log previous_execution_plan status received by planner
        logger.info(
            f"[DIAG-7] Planner.execute_turn | session_id={context.session_id} | "
            f"has_previous_execution_plan={bool(previous_plan)} | "
            f"previous_plan_caps={[c.get('id') for c in previous_plan.get('business_capabilities', [])] if previous_plan else None}"
        )

        execution_plan = None
        
        if previous_plan:
            logger.info(f"[Req: {tracker.request_id}] Found previous execution plan in context.")
            
            missing_info = previous_plan.get("missing_information", [])
            has_pending_clarification = bool(missing_info or previous_plan.get("is_clarification"))
            
            is_new = True
            if has_pending_clarification and not is_internal:
                # If there's an active clarification, run Intent Gate
                is_new = self._is_new_request(context.question, previous_plan)
                
            if not is_new and has_pending_clarification:
                # [DIAG-8] Log entering RESUME branch
                logger.info(f"[DIAG-8] [Req: {tracker.request_id}] RESUME BRANCH ENTERED — Resuming previous execution plan for pending clarification.")
                execution_plan = previous_plan
                
                # Deterministic Context Injection:
                slot_answer = context.user_context.get("slot_answer")
                if slot_answer:
                    key = slot_answer.get("key")
                    val = slot_answer.get("value")
                    if key and val:
                        for cap in execution_plan.get("business_capabilities", []):
                            cap.setdefault("context", {})[key] = val
                else:
                    # Free-text answer: inject into the first pending missing field.
                    if not is_internal:
                        if missing_info:
                            first_missing = missing_info[0]
                            miss_key = first_missing if isinstance(first_missing, str) else first_missing.get("key")
                            from .entity_resolver import is_aggregate_value, AGGREGATE_SENTINEL
                            resolved_value = AGGREGATE_SENTINEL if is_aggregate_value(context.question) else context.question
                            for cap in execution_plan.get("business_capabilities", []):
                                cap.setdefault("context", {})[miss_key] = resolved_value
                        
                execution_plan["missing_information"] = []
            else:
                # [DIAG-8/9] Log entering NEW branch due to Intent Gate or completed previous plan
                logger.info(f"[DIAG-8/9] [Req: {tracker.request_id}] NEW PLANNING BRANCH ENTERED — Discarding previous state.")
                context.user_context.pop("previous_execution_plan", None)
        else:
            logger.info(f"[DIAG-8/9] [Req: {tracker.request_id}] NEW PLANNING BRANCH ENTERED — No previous_execution_plan in user_context.")

        # Purge stale context fields from previous turns if this is a new plan
        if not execution_plan:
            for stale_key in ["search_term", "business_goal", "question", "start_date", "end_date", "period", "date_range", "customer", "project"]:
                context.user_context.pop(stale_key, None)
        
        if not execution_plan:
            # 0. Lightweight Router Fast-Path Check (0 Planner LLM Calls)
            from .router import route_query_fast_path
            fast_plan = route_query_fast_path(context.question, context.user_context)
            if fast_plan:
                execution_plan = fast_plan
                context.request_metadata["token_usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model_name": "lightweight_router_fast_path"
                }
                tracker.record_planner_output(execution_plan)
                logger.info(f"[Req: {tracker.request_id}] Lightweight Router bypassed Planner LLM (0 tokens used).")
            else:
                # Generate Business Execution Plan from scratch
                try:
                    with tracker.track_time("planner_ms"):
                        plan_result = await self._generate_execution_plan(context.question)
                    plan_obj = plan_result["parsed"]
                    raw_msg = plan_result["raw"]
                    if hasattr(raw_msg, "usage_metadata") and raw_msg.usage_metadata:
                        context.request_metadata["token_usage"] = {
                            "input_tokens": raw_msg.usage_metadata.get("input_tokens", 0),
                            "output_tokens": raw_msg.usage_metadata.get("output_tokens", 0),
                            "total_tokens": raw_msg.usage_metadata.get("total_tokens", 0),
                            "model_name": getattr(raw_msg, "response_metadata", {}).get("model_name", "unknown")
                        }
                    execution_plan = plan_obj.model_dump()
                    tracker.record_planner_output(execution_plan)
                except Exception as e:
                    err_str = str(e)
                    logger.error(f"[Req: {tracker.request_id}] Failed to generate business plan: {e}")

                    # ── Classify error and persist to DB ────────────────────────────
                    is_rate_limit = (
                        "429" in err_str
                        or "rate_limit_exceeded" in err_str
                        or "RateLimitError" in type(e).__name__
                        or "rate limit" in err_str.lower()
                        or "quota exceeded" in err_str.lower()
                    )
                    is_not_found = "404" in err_str or "not found" in err_str.lower()
                    error_type = "rate_limit" if is_rate_limit else ("model_not_found" if is_not_found else "planner_error")
                    
                    try:
                        import os
                        from db.database import save_token_usage_async
                        model_name = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "unknown"
                        emp_id = context.user_context.get("employee_id", 0) or 0
                        # Use await (not create_task) so the DB write completes before we return
                        await save_token_usage_async(
                            employee_id=emp_id,
                            session_id=context.session_id,
                            model_name=model_name,
                            input_tokens=0,
                            output_tokens=0,
                            total_tokens=0,
                            total_cost_usd=0.0,
                            status="failed",
                            error_type=error_type,
                            error_message=err_str[:512]
                        )
                        logger.info(f"[TokenTracker] Saved failure record: status=failed, type={error_type}")
                    except Exception as db_err:
                        import traceback
                        logger.error(f"[TokenTracker] CRITICAL: Could not persist failure record: {db_err}")
                        logger.error(f"[TokenTracker] Traceback: {traceback.format_exc()}")

                    if is_rate_limit:
                        import re as _re
                        retry_match = _re.search(r"try again in ([0-9]+m[0-9.]+s|[0-9.]+ ?seconds?)", err_str, _re.IGNORECASE)
                        retry_hint = retry_match.group(1) if retry_match else "a few minutes"
                        logger.warning(f"[Req: {tracker.request_id}] Rate limit hit. Retry in {retry_hint}.")
                        return {
                            "type": "done",
                            "content": (
                                f"⏳ The AI service is temporarily at capacity. "
                                f"Please try again in **{retry_hint}**.\n\n"
                                "_If this persists, the daily token quota may be exhausted — "
                                "please contact your system administrator._"
                            ),
                            "is_clarification": True,
                            "retry_after": retry_hint,
                            "error_code": "rate_limit_exceeded",
                        }

                    # ── Generic planner failure ──────────────────────────────────────
                    logger.error(f"[Req: {tracker.request_id}] Planner error ({error_type}): {err_str}")
                    return {
                        "type": "done",
                        "content": "I encountered an error while analysing your request. Please try rephrasing or simplifying your question.",
                        "is_clarification": True,
                        "error_code": error_type,
                    }

        logger.info(f"Active Business Execution Plan: {json.dumps(execution_plan, indent=2)}")

        # --- Phase 3.1.10: Confidence-Based Execution Guard ---
        conf_score = execution_plan.get("confidence_score", 1.0)
        ambiguity = execution_plan.get("ambiguity_detected", False)
        CONFIDENCE_THRESHOLD = 0.60
        if conf_score < CONFIDENCE_THRESHOLD or ambiguity:
            reasoning = execution_plan.get("reasoning_summary", "")
            logger.warning(
                f"[Req: {tracker.request_id}] Confidence guard triggered: "
                f"score={conf_score:.2f}, ambiguity={ambiguity}. Requesting clarification."
            )
            clarification_msg = (
                "I want to make sure I provide the right information. Could you please clarify:\n\n"
                + (f"_{reasoning}_\n\n" if reasoning else "")
                + "Please provide more details so I can assist you accurately."
            )
            tracker.dump_trace()
            return {
                "type": "done",
                "content": clarification_msg,
                "is_clarification": True,
                "error_code": "low_confidence",
            }

        # --- Phase 3.1.10: Session Memory Context Inheritance for Follow-ups ---
        try:
            from memory.session_manager import get_session_memory
            session_memory = await get_session_memory(context.session_id)
            if session_memory:
                prev_filters = session_memory.get("active_filters") or {}
                prev_entities = session_memory.get("active_entities") or []
                # Inherit previous filters into capabilities that have no overriding filter
                for cap in execution_plan.get("business_capabilities", []):
                    cap_ctx = cap.setdefault("context", {})
                    for fk, fv in prev_filters.items():
                        if fv and fk not in cap_ctx:
                            cap_ctx[fk] = fv
                # Inherit previous resolved entities if none in current plan
                if not execution_plan.get("entities") and prev_entities:
                    logger.info(f"[ExecutiveMemory] Inherited {len(prev_entities)} entities from session memory for follow-up.")
                    execution_plan.setdefault("resolved_entities", prev_entities)
        except Exception as mem_err:
            logger.warning(f"[ExecutiveMemory] Session memory read failed (non-fatal): {mem_err}")

        # Reset entity_errors and missing_information per turn (stateless execution context)
        execution_plan["entity_errors"] = []
        execution_plan["missing_information"] = []

        # 2. Capability-Aware Entity Resolution
        from registry.capability_catalog import get_capability_entity_requirements
        from .entity_resolver import is_reserved_business_term, has_employee_trigger

        requested_capabilities = execution_plan.get("business_capabilities", [])
        
        # Check if capabilities strictly require input entities
        requires_entities_flag = False
        allowed_entity_types = set()
        
        for cap in requested_capabilities:
            cap_id = cap.get("id")
            cap_req_entities, cap_req_types = get_capability_entity_requirements(cap_id)
            if cap_req_entities:
                requires_entities_flag = True
                allowed_entity_types.update(cap_req_types)

        logger.info(
            f"[Pipeline Instrumentation] Request ID: {tracker.request_id} | "
            f"Selected Capabilities: {[c.get('id') for c in requested_capabilities]} | "
            f"Requires Entities: {requires_entities_flag} | "
            f"Required Entity Types: {list(allowed_entity_types)}"
        )

        extracted_entities = execution_plan.get("entities", [])

        # Guarantee fresh entity extraction for NEW queries
        is_resumed_turn = bool(previous_plan and not is_new)

        if context.user_context:
            has_emp_trig = has_employee_trigger(context.question)
            
            # Sanitise context.user_context if legacy employee_name was set without trigger
            if not has_emp_trig and "employee_name" in context.user_context:
                emp_val = context.user_context.get("employee_name")
                if emp_val and not has_employee_trigger(str(emp_val)):
                    logger.info(f"[Planner] Stripping legacy user_context['employee_name']='{emp_val}' because query lacks employee trigger.")
                    context.user_context.pop("employee_name", None)

            if is_resumed_turn:
                for entity_type in ["service_line", "department", "employee_name", "customer"]:
                    val = context.user_context.get(entity_type)
                    if val and str(val).lower() != "all" and not is_reserved_business_term(str(val)):
                        if entity_type == "employee_name" and not has_emp_trig:
                            continue
                        if not any(e.get("type", "").lower() in [entity_type, "employee"] and e.get("value", "").lower() == str(val).lower() for e in extracted_entities):
                            mapped_type = "employee" if entity_type == "employee_name" else entity_type
                            extracted_entities.append({"type": mapped_type, "value": str(val)})
            else:
                for entity_type in ["customer", "customer_id", "customer_name"]:
                    if entity_type in context.user_context and not any(e.get("type", "").lower() in ["customer", "customer_id"] for e in extracted_entities):
                        logger.info(f"[Planner Entity Isolation] Purged stale session entity '{entity_type}' from user_context for fresh query.")
                        context.user_context.pop(entity_type, None)

        # Filter extracted entities against reserved vocabulary and capability requirements
        clean_extracted_entities = []
        for e in extracted_entities:
            e_val = str(e.get("value", "")).strip()
            e_type = str(e.get("type", "")).lower()
            if not is_reserved_business_term(e_val):
                if not allowed_entity_types or e_type in allowed_entity_types or e_type == "customer":
                    clean_extracted_entities.append(e)
                else:
                    logger.info(f"[Pipeline Instrumentation] Ignored entity '{e_val}' of type '{e_type}' (Not required by capability)")
            else:
                logger.info(f"[Pipeline Instrumentation] Filtered reserved vocabulary term '{e_val}' from entity resolution.")

        extracted_entities = clean_extracted_entities
        resolved_entities = execution_plan.get("resolved_entities", [])
        entity_errors = []

        # If capabilities require NO input entities (e.g. Revenue/Receivable summary, top queries, KPI reports), skip Entity Resolver
        if not requires_entities_flag and not any(c.get("id") == "customer_360_profile" for c in requested_capabilities):
            logger.info(f"[Pipeline Instrumentation] Capabilities {[c.get('id') for c in requested_capabilities]} require NO input entities. Skipping Entity Resolver.")
            resolved_entities = []
            entity_errors = []
        else:
            if not resolved_entities and extracted_entities:
                extracted_entities = [e for e in extracted_entities if str(e.get("value", "")).lower() != "all"]
                if extracted_entities:
                    with tracker.track_time("entity_resolver_ms"):
                        resolved_entities, entity_errors = await resolve_entities(extracted_entities, context.jwt_token, full_query=context.question)

        logger.info(f"[Pipeline Instrumentation] Resolved Entities: {resolved_entities} | Entity Errors: {entity_errors}")
        tracker.record_entity_resolution(resolved_entities, entity_errors)
        
        # Clean user_context by stripping previous_execution_plan and runtime objects to avoid circular references
        clean_user_ctx = {}
        if context.user_context and isinstance(context.user_context, dict):
            for k, v in context.user_context.items():
                if k not in ("previous_execution_plan", "raw_tool_results") and isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    clean_user_ctx[k] = v
        execution_plan["user_context"] = clean_user_ctx
        execution_plan["resolved_entities"] = resolved_entities
        if entity_errors:
            execution_plan["entity_errors"] = entity_errors

        # 2.5 Propagate shared parameter context across all requested capabilities
        shared_ctx = {}
        shared_time_filter = execution_plan.get("time_filter")

        for cap in execution_plan.get("business_capabilities", []):
            ctx = cap.get("context", {}) or {}
            for k, v in ctx.items():
                if v and k not in shared_ctx:
                    shared_ctx[k] = v
            if cap.get("time_filter") and not shared_time_filter:
                shared_time_filter = cap.get("time_filter")

        # Propagate user context filters (employee, department, customer, project, etc.) into shared_ctx
        if context.user_context:
            for filter_key in [
                "financial_year", "service_line", "department", "employee", "employee_name", 
                "customer", "customer_id", "project", "office", "manager", "status", "date_range"
            ]:
                val = context.user_context.get(filter_key)
                if val and str(val).lower() != "all" and filter_key not in shared_ctx:
                    shared_ctx[filter_key] = val

        # Propagate operational metadata fields (ranking, comparison, metric, intent, scope)
        for field in ["business_goal", "intent", "metric", "ranking", "comparison", "aggregation", "scope"]:
            if execution_plan.get(field) and field not in shared_ctx:
                shared_ctx[field] = execution_plan[field]

        for cap in execution_plan.get("business_capabilities", []):
            cap_ctx = cap.setdefault("context", {})
            for k, v in shared_ctx.items():
                if k not in cap_ctx or not cap_ctx[k]:
                    cap_ctx[k] = v
            if shared_time_filter and not cap.get("time_filter"):
                cap["time_filter"] = shared_time_filter

            # Deterministic Time Boundary Enforcement for time_filter / month mentions
            tf = cap.get("time_filter") or cap_ctx.get("date_range") or context.question
            if tf and isinstance(tf, str):
                from .entity_resolver import parse_scope_time_filter
                tf_res = parse_scope_time_filter(tf)
                if tf_res.get("start_date") and tf_res.get("end_date"):
                    cap.setdefault("filters", {})["start_date"] = tf_res["start_date"]
                    cap["filters"]["end_date"] = tf_res["end_date"]
                    cap_ctx["start_date"] = tf_res["start_date"]
                    cap_ctx["end_date"] = tf_res["end_date"]
        
        # 3. Execution Validator
        is_valid, validation_errors = validate_execution(execution_plan)
        tracker.record_validation(is_valid, validation_errors)
        
        # 3.5 Conversation Manager (Slot Fill & Smart Defaults)
        from .conversation_manager import conversation_manager
        clarification_response = await conversation_manager.evaluate_confidence_and_slots(
            execution_plan, 
            validation_errors, 
            user_query=context.question, 
            llm=self.llm
        )
        
        if clarification_response:
            logger.info(f"[Req: {tracker.request_id}] ConversationManager intercepted flow: {clarification_response.get('type')}")
            if "token_usage" in context.request_metadata:
                clarification_response["token_usage"] = context.request_metadata["token_usage"]
            tracker.dump_trace()
            return clarification_response
        
        if not is_valid:
            logger.warning(f"[Req: {tracker.request_id}] Execution blocked by Validator.")
            tracker.dump_trace()
            return {"type": "done", "content": "⚠️ **Execution Blocked**\n\n" + "\n".join([f"- {err}" for err in validation_errors]), "is_clarification": True, "execution_plan": execution_plan}
            
        # 3.8 Entity-Aware Cache Check (Post Entity Resolution, Pre-Backend Execution)
        from memory.session_manager import build_entity_cache_key, get_entity_cache, set_entity_cache

        user_id = context.user_context.get("user_id", 0) if context.user_context else 0
        primary_cap = execution_plan["business_capabilities"][0]["id"] if execution_plan.get("business_capabilities") else "general"
        fy_val = context.user_context.get("financial_year", "") if context.user_context else ""

        cache_cap_ctx = execution_plan["business_capabilities"][0].get("context", {}) if execution_plan.get("business_capabilities") else {}
        cache_key = build_entity_cache_key(user_id, primary_cap, resolved_entities, financial_year=fy_val, filters=cache_cap_ctx)
        cached_response = await get_entity_cache(cache_key)
        if cached_response:
            c_str = str(cached_response.get("content", "")).lower()
            if primary_cap == "recoverability_analysis" and not any(p in c_str for p in ["recoverability percentage", "actual recoverability", "portfolio recoverability"]):
                logger.info(f"[Planner] Discarding stale cached response for recoverability_analysis key='{cache_key}' (missing recoverability %).")
                cached_response = None

        if cached_response:
            logger.info(f"[Req: {tracker.request_id}] Returning cached response via Entity Cache key='{cache_key}' (0 LLM/Backend calls).")
            cached_response["was_cached"] = True
            cached_response["cache_tier"] = "entity_cache"
            tracker.dump_trace()
            return cached_response

        # 4. Tool Registry Resolution & Execution
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from registry.tool_registry import tool_registry

        logger.info(f"[Req: {tracker.request_id}] Resolving physical implementations via Tool Registry...")
        execution_graph = tool_registry.resolve_implementations(
            execution_plan["business_capabilities"],
            resolved_entities=resolved_entities,
            user_context=context.user_context
        )
        tracker.record_registry_selection(execution_graph)
        
        logger.info(f"[Req: {tracker.request_id}] Executing resolved implementations...")
        with tracker.track_time("tool_execution_ms"):
            tool_results = await tool_registry.execute_resolved_implementations(
                execution_graph, 
                resolved_entities, 
                context.jwt_token, 
                context.user_context,
                context.question
            )
            
        tracker.record_tool_execution(tool_results)
        
        # 5. Response Generation (Presentation Mode Governed)
        from .synthesizer import format_data_response, synthesize_response

        q_clean = context.question.lower()
        pres_mode = str(execution_plan.get("presentation_mode") or "").upper()
        is_explicit_table_query = any(phrase in q_clean for phrase in ["show table", "list all", "table format", "raw records", "export table"])

        with tracker.track_time("synthesizer_ms"):
            if pres_mode == "TABLE" and is_explicit_table_query:
                logger.info(f"[Req: {tracker.request_id}] Executing DATA MODE Direct Formatter for explicit table query.")
                final_response = format_data_response(context.question, tool_results)
            else:
                logger.info(f"[Req: {tracker.request_id}] Executing Executive Response Synthesis (presentation_mode='{pres_mode}').")
                final_response = await synthesize_response(context.question, tool_results, self.llm, execution_plan=execution_plan)
            
            # Pass token usage up and attach execution plan and tool results
            if isinstance(final_response, dict):
                if "token_usage" in context.request_metadata:
                    final_response["token_usage"] = context.request_metadata["token_usage"]
                final_response["execution_plan"] = execution_plan
                final_response["tool_results"] = tool_results

            # Save to Entity Cache
            await set_entity_cache(cache_key, final_response)

            # --- Phase 3.1.10: Update Executive Session Memory ---
            try:
                from memory.session_manager import update_session_memory
                # Inject plan-level presentation_mode from first capability if not set
                if not execution_plan.get("presentation_mode"):
                    first_cap = (execution_plan.get("business_capabilities") or [{}])[0]
                    execution_plan["presentation_mode"] = first_cap.get("presentation_mode")
                await update_session_memory(context.session_id, execution_plan, tool_results)
            except Exception as mem_err:
                logger.warning(f"[ExecutiveMemory] update_session_memory failed (non-fatal): {mem_err}")
            
        # Structured Observability Telemetry Output
        planner_toks = (context.request_metadata.get("token_usage", {}) or {}).get("total_tokens", 0)
        synth_toks = (final_response.get("token_usage", {}) or {}).get("total_tokens", 0) if isinstance(final_response, dict) else 0
        tot_toks = planner_toks + synth_toks
        budget_pct = round((tot_toks / 5000) * 100, 1)
        llm_calls_cnt = (1 if planner_toks > 0 else 0) + (1 if synth_toks > 0 else 0)

        cust_id_resolved = "None"
        for re_item in (resolved_entities or []):
            if str(re_item.get("entity_type", "")).lower() == "customer":
                cust_id_resolved = str(re_item.get("entity_id"))

        logger.info(
            f"[Production Telemetry] "
            f"PresentationMode={pres_mode} | "
            f"Capability={primary_cap} | "
            f"ResolvedCustomer={cust_id_resolved} | "
            f"FY={fy_val or 'Default'} | "
            f"CacheStatus={'HIT' if final_response.get('was_cached') else 'MISS'} | "
            f"SynthesizerInvoked={pres_mode != 'TABLE' or not is_explicit_table_query} | "
            f"LLMCalls={llm_calls_cnt} | "
            f"PlannerTokens={planner_toks} | "
            f"SynthTokens={synth_toks} | "
            f"TotalTokens={tot_toks} | "
            f"BudgetUsage={budget_pct}%"
        )

        tracker.record_synthesis(final_response)
        tracker.dump_trace()
        
        return final_response

    async def _generate_execution_plan(self, query: str) -> Dict[str, Any]:
        """
        Dynamically calls the LLM with the Business Capabilities Catalog to generate a structured execution plan.
        The Planner NEVER sees APIs, SQL, or wrappers.
        """
        import sys
        import os
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from registry.capability_catalog import get_planner_capabilities_schema
        
        capability_schemas = json.dumps(get_planner_capabilities_schema(), indent=2)
        
        from datetime import datetime
        now = datetime.now()
        curr_date_str = now.strftime("%Y-%m-%d")
        curr_year = now.year

        prompt = ChatPromptTemplate.from_messages([
            ("system", 
             f"You are the Enterprise Business Analyst for a CRM system.\n"
             f"CURRENT SYSTEM DATE: {curr_date_str} (Current Calendar Year: {curr_year}, Current Fiscal Year: FY{str(curr_year)[2:]})\n"
             f"TEMPORAL ANCHOR RULE: When a month name is mentioned without a year (e.g. 'Jan', 'January', 'March'), ALWAYS assume the current year ({curr_year}). NEVER use past years (like 2024 or 2025) unless explicitly requested by the user.\n\n"
             "Your ONLY job is to understand the user's goal and map it to abstract Business Capabilities.\n\n"
             "AVAILABLE BUSINESS CAPABILITIES:\n{capabilities}\n\n"
             "RULES (NATURAL LANGUAGE UNDERSTANDING & SEMANTIC INTERPRETATION):\n"
             "1. USER INTENT CLASSIFICATION:\n"
             "   Identify the primary intent of the user query across equivalent phrasing variations:\n"
             "   - 'generate_report': 'Generate', 'Show', 'Run', 'Fetch', 'Display', 'Give me', 'Report for', 'I need'\n"
             "   - 'compare': 'Compare', 'vs', 'versus', 'difference between', 'side-by-side'\n"
             "   - 'summarize': 'Summarize', 'Executive summary', 'High-level overview', 'Brief me'\n"
             "   - 'explain': 'Explain', 'Why did', 'Break down', 'Details of'\n"
             "   - 'analyze': 'Analyze', 'Analysis of', 'Performance review', 'Deep dive'\n"
             "   - 'list': 'List', 'Show all', 'Find all', 'Which projects/customers'\n"
             "   - 'trend': 'Trend', 'Over time', 'Monthly growth', 'Historical'\n"
             "   Store this in the 'intent' field of each requested business capability.\n\n"
             "2. REQUESTED REPORTS & MULTI-REPORT RECOGNITION:\n"
             "   Map business terms and synonyms to catalog capability IDs:\n"
             "   - Revenue / Invoicing / Sales / Billings / Top line -> 'revenue_analysis'\n"
             "   - Recoverability / Profitability / Margin / Actual vs Estimated Cost -> 'recoverability_analysis'\n"
             "   - Receivables / Outstanding / Overdue Invoices / Aging / Debtors -> 'receivables_analysis'\n"
             "   - Pipeline / Proposals / Leads / Sales Opportunities / Win Rate -> 'pipeline_analysis'\n"
             "   - KPI / Key Indicators / Executive Summary -> 'kpi_summary'\n"
             "   - Billing / Staff Billing / Employee Billing / Resource Cost -> 'staff_billing_report'\n"
             "   - Projects / Active Projects / WIP -> 'active_projects'\n"
             "   - Top Performing Projects / Best Projects / Highest Revenue Projects / Ranking Projects -> 'analytical_query'\n"
             "   - Specific Customer / Specific Project / Project Status of named entity -> 'project_search' or 'customer_360_profile'\n"
             "   MULTI-REPORT MANDATE: When user requests multiple reports in one prompt (e.g. 'Revenue + Recoverability', 'Revenue and KPI', 'Receivables, Pipeline and Billing'), extract ALL matching capabilities into the 'business_capabilities' array.\n\n"
             "3. BUSINESS ENTITIES EXTRACTION (UNVALIDATED):\n"
             "   Extract named business entities into the 'entities' array without attempting database validation:\n"
             "   - 'service_line': Audit, Tax, Advisory, BRS, BPS, Growth, Consulting, etc.\n"
             "   - 'customer': ABC Ltd, Acme Corp, etc.\n"
             "   - 'department': Finance, Audit, Legal, HR, etc.\n"
             "   - 'project': Phoenix Project, PRJ001, etc.\n"
             "   - 'employee': ONLY when explicitly triggered by words like 'employee', 'consultant', 'staff', 'resource', 'person', 'developer', 'manager', 'partner', 'named', or 'by'.\n"
             "   - 'financial_year': FY25, FY 2025-26, 2025-26, FY2025, etc. MANDATE: Extract ONLY the financial_year identifier (e.g., 'FY25'). NEVER invent 'start_date' or 'end_date' fields for financial years; date ranges are computed deterministically downstream.\n"
             "   - 'date_range': Specific dates or time periods (e.g., 'Oct 2025 to Sep 2026').\n"
             "   Do NOT validate entities. Entity resolution will be handled downstream by Entity Resolver.\n\n"
             "4. SCOPE CLASSIFICATION RULES (MANDATORY):\n"
             "   - Assign 'scope' = 'organization' when the user asks for total, organization-wide, or company counts/summaries (e.g. 'How many active projects do we have?', 'Total revenue', 'Open proposals count', 'Total receivables', 'Recoverability summary').\n"
             "   - Assign 'scope' = 'entity' ONLY when the user names a specific customer, project, proposal, or employee (e.g. 'Active projects for ABC Ltd', 'Phoenix Project details').\n"
             "   - Assign 'scope' = 'filtered' when the user filters by service line, department, or office without naming a specific entity (e.g. 'Active projects for Audit service line').\n\n"
             "5. MISSING INFORMATION STRUCTURE:\n"
             "   For organization-wide or aggregate queries ('scope' = 'organization'), DO NOT list search_term or project name as missing information. Set missing_information = [].\n\n"
             "6. INTENT CONSISTENCY:\n"
             "   Ensure distinct phrasings conveying identical business requests map to identical execution plans.\n"
             "   Calculate a confidence_score between 0.0 and 1.0 reflecting plan accuracy.\n"
             "   Set ambiguity_detected=true if the query is ambiguous and clarification would materially improve the response.\n"
             "   Always populate reasoning_summary with a brief explanation of why you selected this plan.\n\n"
             "7. PRESENTATION MODE (MANDATORY — Synthesizer depends on this):\n"
             "   Set presentation_mode on the plan AND on each capability based on user intent:\n"
             "   - 'REPORT': User explicitly says 'Generate', 'Run', 'Show me the report', 'Get the report'.\n"
             "   - 'REPORT_AND_INSIGHT': User requests report AND summary/analysis (e.g. 'Generate KPI Report and summarize it').\n"
             "   - 'INSIGHT': User says 'Explain', 'Analyze', 'Tell me about', 'What is happening with'.\n"
             "   - 'KPI_CARD': Simple scalar count/metric questions (e.g. 'How many open proposals?', 'What is total revenue?').\n"
             "   - 'TABLE': User explicitly asks for a list or table of records.\n"
             "   - 'COMPARISON': User says 'Compare', 'vs', 'difference between', 'versus'.\n"
             "   - 'EXECUTIVE_BRIEF': Broad business health questions (e.g. 'How is our company performing?', 'What should management focus on?', 'Company overview').\n"
             "   Set requires_report=true when presentation_mode is REPORT or REPORT_AND_INSIGHT.\n"
             "   Set requires_summary=true when presentation_mode is REPORT_AND_INSIGHT, INSIGHT, or EXECUTIVE_BRIEF.\n"
             "   Set requires_comparison=true when presentation_mode is COMPARISON.\n\n"
             "OUTPUT INSTRUCTIONS:\n"
             "You must return ONLY a raw JSON object strictly adhering to the following JSON schema.\n"
             "Do not include any markdown formatting like ```json or any other text.\n"
             "JSON SCHEMA:\n{json_schema}\n"
            ),
            ("user", "{query}")
        ])
        
        json_schema_str = json.dumps(BusinessExecutionPlan.model_json_schema(), indent=2)
        chain = prompt | self.llm
        
        raw_msg = await chain.ainvoke({"capabilities": capability_schemas, "json_schema": json_schema_str, "query": query})
        
        content = raw_msg.content.strip()
        # Robustly strip markdown json blocks if the LLM ignores instructions
        if content.startswith("```"):
            lines = content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()
            
        try:
            plan_obj = BusinessExecutionPlan.model_validate_json(content)
            return {"parsed": plan_obj, "raw": raw_msg}
        except Exception as e:
            logger.error(f"Failed to parse LLM JSON output: {content}")
            raise e

    def _is_new_request(self, query: str, previous_plan: Dict[str, Any]) -> bool:
        """
        Deterministic Intent Gate — ZERO LLM calls.

        Decision logic (metadata-driven, no hardcoding):
        1. If the query is a short response (< 6 words) AND it cannot be matched
           to any known capability ID or description keyword — treat it as ANSWER.
        2. If the query contains a known capability keyword from the catalog AND
           is NOT just restating the existing capability — treat it as NEW_REQUEST.
        3. Default: ANSWER (resume existing plan).

        This replaces the former LLM-based Intent Gate and produces identical routing
        behaviour at zero token cost.
        """
        from registry.capability_catalog import BUSINESS_CAPABILITIES
        from .entity_resolver import is_aggregate_value

        q = query.strip()
        q_lower = q.lower()
        words = q_lower.split()

        # Action/question verbs indicating a brand new request regardless of length
        action_verbs = {"what", "how", "show", "generate", "get", "run", "list", "active", "total", "kpi", "report", "status", "actual", "recoverability", "revenue"}
        if any(w in words for w in action_verbs):
            logger.info(f"[IntentGate] Detected NEW_REQUEST via action verb: '{q[:60]}'")
            return True  # NEW_REQUEST

        # Rule 1: Very short non-verb responses are clarification answers.
        # ("FY2026", "All", "Audit", "Yes", "No", "Current FY", "Last month")
        if len(words) <= 5:
            return False  # ANSWER

        # Rule 2: Aggregate values are always answers.
        if is_aggregate_value(q):
            return False  # ANSWER

        # Build a vocabulary of capability-triggering keywords from the catalog.
        # This is computed once per call (the catalog is a small in-memory list).
        cap_keywords: set[str] = set()
        existing_cap_ids: set[str] = {cap["id"] for cap in previous_plan.get("business_capabilities", [])}
        for cap in BUSINESS_CAPABILITIES:
            desc_words = cap["description"].lower().split()
            # Only use salient nouns (3+ chars, skip stopwords)
            cap_keywords.update(w.rstrip(".,:") for w in desc_words if len(w) >= 4)

        # Rule 3: If the query contains a capability trigger keyword AND doesn't
        # simply repeat one of the already-planned capabilities, treat as new.
        trigger_threshold = 2  # require at least 2 capability keywords to avoid false positives
        hit_count = sum(1 for w in words if w in cap_keywords)
        if hit_count >= trigger_threshold:
            # Extra check: if every matched cap is already in the plan, user is
            # just providing more context for the same request — still an ANSWER.
            new_cap_hit = any(
                cap["id"] not in existing_cap_ids
                for cap in BUSINESS_CAPABILITIES
                if any(w in q_lower for w in cap["description"].lower().split() if len(w) >= 4)
            )
            if new_cap_hit:
                logger.info(f"[IntentGate] Detected NEW_REQUEST (deterministic, {hit_count} cap keywords): '{q[:60]}'")
                return True  # NEW_REQUEST

        logger.info(f"[IntentGate] Detected ANSWER (deterministic): '{q[:60]}'")
        return False  # ANSWER
