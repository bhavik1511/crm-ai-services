"""
planner.py — The Enterprise Business Reasoning Engine.
Designed using SOLID principles. 
Strictly handles business understanding, capability detection, and missing information.
Completely unaware of tool implementations, APIs, or semantic layers.
"""
import os
import re
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
from .secure_log_sanitizer import sanitize_for_log

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
    value: Optional[str] = Field(default="", description="The name or code of the entity to search for.")

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


def _filter_tool_results_by_entity_scope(tool_results: List[Dict[str, Any]], shared_ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Enforces downstream entity-scoped filtering on tool_results payloads before rendering.
    Isolates target entity rows when backend API returns multi-row responses.
    """
    if not tool_results or not isinstance(tool_results, list) or not shared_ctx:
        return tool_results

    req_sl_id = shared_ctx.get("service_line_id")
    req_sl_name = (
        shared_ctx.get("service_line") 
        or shared_ctx.get("service_line_name")
        or shared_ctx.get("short_name")
        or shared_ctx.get("short_code")
        or shared_ctx.get("entity_name")
    )
    req_dept_id = shared_ctx.get("department_id")
    req_dept_name = shared_ctx.get("department") or shared_ctx.get("department_name")

    for tr in tool_results:
        if not isinstance(tr, dict):
            continue
        res_val = tr.get("result")

        target_list = None
        is_dict_wrapper = False
        dict_key = None

        if isinstance(res_val, list):
            target_list = res_val
        elif isinstance(res_val, dict):
            if isinstance(res_val.get("rows"), list):
                target_list = res_val.get("rows")
                is_dict_wrapper = True
                dict_key = "rows"
            elif isinstance(res_val.get("data"), list):
                target_list = res_val.get("data")
                is_dict_wrapper = True
                dict_key = "data"

        if target_list is not None and len(target_list) > 0:
            filtered = target_list
            if req_sl_id is not None or req_sl_name:
                sl_matches = []
                req_sl_clean = str(req_sl_name).strip().lower() if req_sl_name else ""
                for r in filtered:
                    if isinstance(r, dict):
                        r_sl_id = r.get("service_line_id") or r.get("serviceLineId") or r.get("sl_id")
                        r_sl_name = str(r.get("service_line") or r.get("service_line_name") or "").strip().lower()
                        r_short = str(r.get("short_name") or r.get("short_code") or "").strip().lower()

                        if req_sl_id is not None and r_sl_id is not None and str(r_sl_id) == str(req_sl_id):
                            sl_matches.append(r)
                        elif req_sl_clean:
                            if req_sl_clean == r_sl_name or req_sl_clean == r_short or req_sl_clean in r_sl_name or req_sl_clean in r_short or r_short in req_sl_clean:
                                sl_matches.append(r)
                            elif req_sl_clean in ["brs", "business risk services", "business risk"] and (r_short == "brs" or "business risk" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["bps", "business process services", "business process"] and (r_short == "bps" or "business process" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["audit", "a&a", "assurance"] and (r_short in ["a&a", "audit"] or "audit" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["tax", "taxation"] and (r_short == "tax" or "tax" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["growth", "growth advisory"] and (r_short == "growth" or "growth" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["tech", "technology", "technology advisory"] and (r_short == "tech" or "technology" in r_sl_name):
                                sl_matches.append(r)
                            elif req_sl_clean in ["legal", "corporate & regulatory advisory services", "regulatory"] and (r_short in ["legal", "advisory"] or "regulatory" in r_sl_name or "corporate" in r_sl_name):
                                sl_matches.append(r)
                filtered = sl_matches

            if req_dept_id is not None or req_dept_name:
                dept_matches = []
                for r in filtered:
                    if isinstance(r, dict):
                        r_dept_id = r.get("department_id") or r.get("departmentId") or r.get("id")
                        r_dept_name = r.get("department_name") or r.get("name") or r.get("department")
                        if req_dept_id is not None and r_dept_id is not None and str(r_dept_id) == str(req_dept_id):
                            dept_matches.append(r)
                        elif req_dept_id is None and req_dept_name and r_dept_name and str(req_dept_name).strip().lower() in str(r_dept_name).strip().lower():
                            dept_matches.append(r)
                if dept_matches:
                    filtered = dept_matches

            if is_dict_wrapper:
                res_val[dict_key] = filtered
            else:
                tr["result"] = filtered

    return tool_results


def sanitize_planner_json(content: str) -> str:
    """
    Sanitizes raw LLM output strings for Pydantic parsing.
    - Strips markdown formatting and introductory/explanatory text.
    - Removes <think>...</think> reasoning blocks.
    - Resolves $ref JSON schema references generated by smaller/fallback LLMs.
    - Converts echoed Pydantic schema definitions into concrete plan objects.
    """
    from config.llm_factory import clean_think_tags
    if not content or not isinstance(content, str):
        return content
    content = clean_think_tags(content).strip()
    
    # 1. Extract JSON block if surrounded by markdown or preamble chatter
    start_idx = content.find("{")
    end_idx = content.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        content = content[start_idx:end_idx+1]

    # 2. Parse dict and resolve $ref or echoed schema declarations
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            # If LLM echoed Pydantic schema declaration instead of instantiating object
            if "properties" in data and "business_goal" in data.get("properties", {}):
                data = {
                    "business_goal": "Execute business query",
                    "confidence_score": 1.0,
                    "reasoning_summary": "Executing matching business capability",
                    "ambiguity_detected": False,
                    "entities": [],
                    "scope": ["organization"],
                    "business_capabilities": [{"id": "revenue_analysis", "scope": "organization", "intent": "generate_report"}],
                    "missing_information": [],
                    "entity_errors": [],
                    "presentation_mode": "REPORT",
                    "analysis_depth": "summary"
                }

            defs = data.pop("$defs", {})
            caps = data.get("business_capabilities", [])
            new_caps = []
            for cap in caps:
                if isinstance(cap, dict) and "$ref" in cap:
                    ref_path = cap["$ref"].split("/")
                    def_key = ref_path[-1] if ref_path else ""
                    def_obj = defs.get(def_key, {})
                    resolved = {
                        "id": "proposal_search",
                        "scope": data.get("scope", ["organization"])[0] if isinstance(data.get("scope"), list) and data.get("scope") else "organization",
                        "intent": "generate_report"
                    }
                    new_caps.append(resolved)
                else:
                    new_caps.append(cap)
            data["business_capabilities"] = new_caps

            # Clean entities array if LLM passed list of strings, dicts, or key-value objects
            entities = data.get("entities", [])
            new_entities = []
            if isinstance(entities, dict):
                for k, v in entities.items():
                    if str(v).strip():
                        new_entities.append({"type": str(k), "value": str(v).strip()})
            elif isinstance(entities, list):
                for ent in entities:
                    if isinstance(ent, str):
                        if ent.strip():
                            new_entities.append({"type": ent.strip(), "value": ""})
                    elif isinstance(ent, dict):
                        normalized_ent = dict(ent)
                        if "name" in normalized_ent and "type" not in normalized_ent:
                            normalized_ent["type"] = str(normalized_ent.pop("name"))
                        
                        if "type" in normalized_ent and "value" in normalized_ent:
                            if str(normalized_ent.get("value", "")).strip():
                                new_entities.append(normalized_ent)
                        elif len(normalized_ent) == 1:
                            k, v = next(iter(normalized_ent.items()))
                            if str(v).strip():
                                new_entities.append({"type": str(k), "value": str(v).strip()})
                        else:
                            if str(normalized_ent.get("value", "")).strip():
                                new_entities.append(normalized_ent)
            # Filter out any lingering empty value entities
            new_entities = [e for e in new_entities if str(e.get("value", "")).strip()]
            data["entities"] = new_entities

            return json.dumps(data)
    except Exception:
        pass
    return content


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
        
        # Security Guardrail Check for Schema / Internal System Queries
        from config.security_guard import check_security_guardrail
        sec_block = check_security_guardrail(context.question or "")
        if sec_block:
            return {
                "type": "done",
                "content": sec_block["content"],
                "is_clarification": False
            }

        # 0. Structured Input Intent Gate Evaluation
        from .input_intent_gate import evaluate_input_intent, InputType
        previous_plan = context.user_context.get("previous_execution_plan")
        has_pending_clar = bool(previous_plan and (previous_plan.get("missing_information") or previous_plan.get("is_clarification")))
        
        ctx_user = dict(context.user_context or {})
        ctx_user["request_id"] = tracker.request_id
        gate_res = await evaluate_input_intent(
            question=context.question,
            has_pending_clarification=has_pending_clar,
            user_context=ctx_user
        )
        if hasattr(gate_res, "token_usage") and gate_res.token_usage:
            context.request_metadata["intent_gate_token_usage"] = gate_res.token_usage

        if gate_res.input_type == InputType.TECHNICAL_PASTE:
            logger.info(f"[INPUT_INTENT_GATE] Short-circuiting execution for TECHNICAL_PASTE.")
            return {
                "type": "done",
                "content": "It looks like you've pasted raw technical content (such as a log, API response, or debug trace). Please let me know what you would like me to do with this information, or ask a CRM question.",
                "is_clarification": False,
                "input_type": "TECHNICAL_PASTE"
            }

        if gate_res.input_type == InputType.AMBIGUOUS and not has_pending_clar:
            logger.info(f"[INPUT_INTENT_GATE] Short-circuiting execution for AMBIGUOUS input.")
            return {
                "type": "done",
                "content": "Could you please clarify your request? Let me know which CRM report, employee, customer, or business data you'd like to view.",
                "is_clarification": True,
                "input_type": "AMBIGUOUS"
            }

        # 1. Check for Clarification State Persistence
        is_internal = context.request_metadata.get("is_internal", False)
        
        # [DIAG-7] Log previous_execution_plan status received by planner
        logger.info(
            f"[DIAG-7] Planner.execute_turn | session_id={context.session_id} | "
            f"has_previous_execution_plan={bool(previous_plan)} | "
            f"previous_plan_caps={[c.get('id') for c in previous_plan.get('business_capabilities', [])] if previous_plan else None}"
        )

        execution_plan = None
        is_resumed_turn = False
        
        if previous_plan:
            logger.info(f"[Req: {tracker.request_id}] Found previous execution plan in context.")
            
            missing_info = previous_plan.get("missing_information", [])
            has_pending_clarification = bool(missing_info or previous_plan.get("is_clarification"))
            
            is_new = True
            if has_pending_clarification and not is_internal:
                # If there's an active clarification, run Intent Gate
                is_new = self._is_new_request(context.question, previous_plan)
                
            if not is_new and has_pending_clarification:
                # RESUME branch
                is_resumed_turn = True
                logger.info(f"[Req: {tracker.request_id}] RESUME BRANCH ENTERED — Resuming previous execution plan for pending clarification.")
                logger.info(f"[REQUEST_CLASSIFICATION] request_type=CLARIFICATION previous_state_used=true")
                from .intent_normalizer import merge_clarification_intent
                execution_plan = merge_clarification_intent(previous_plan, context.question, context.user_context)
            else:
                logger.info(f"[Req: {tracker.request_id}] NEW PLANNING BRANCH ENTERED — Discarding previous state.")
                logger.info(f"[REQUEST_CLASSIFICATION] request_type=NEW_REQUEST previous_state_used=false")
                context.user_context.pop("previous_execution_plan", None)
        else:
            logger.info(f"[Req: {tracker.request_id}] NEW PLANNING BRANCH ENTERED — No previous_execution_plan in user_context.")
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=NEW_REQUEST previous_state_used=false")

        # Retrieve previous session executive memory for request classification & context inheritance
        from memory.session_manager import get_session_memory
        session_memory = await get_session_memory(context.session_id) or {}
        from .conversation_manager import conversation_manager
        req_type = conversation_manager.classify_request_type(context.question, session_memory)
        
        if req_type == "CONTEXT_RESET":
            logger.info(f"[LOGGING] request_type=CONTEXT_RESET previous_context_cleared=true")
            stale_keys = [
                "search_term", "business_goal", "question", "start_date", "end_date",
                "period", "date_range", "customer", "customer_name", "customer_id",
                "department", "department_name", "department_id", "service_line",
                "service_line_name", "service_line_id", "project", "employee",
                "employee_name", "employee_id", "operation", "metric", "dimension",
                "ranking", "comparison", "comparison_type", "comparison_periods",
                "temporal_scope", "financial_year", "limit", "sort_order"
            ]
            for stale_key in stale_keys:
                context.user_context.pop(stale_key, None)
        elif req_type == "FOLLOW_UP":
            logger.info(f"[LOGGING] request_type=FOLLOW_UP previous_context_used=true active_filters={session_memory.get('active_filters')}")

        # Check active follow-up choices
        active_followup_opts = session_memory.get("active_followup_options", [])
        if not execution_plan and active_followup_opts:
            matched_opt = conversation_manager.resolve_followup_input(context.question, active_followup_opts)
            if matched_opt and isinstance(matched_opt, dict):
                if matched_opt.get("type") == "clarification_needed":
                    tracker.dump_trace()
                    return {
                        "type": "done",
                        "content": matched_opt.get("content"),
                        "answer": matched_opt.get("content"),
                        "is_clarification": True,
                        "options": matched_opt.get("options"),
                        "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                    }
                
                # Execute follow-up option deterministically (0 LLM Tokens)
                e_type = matched_opt.get("entity_type")
                e_id = matched_opt.get("entity_id")
                e_name = matched_opt.get("label")
                inh_ctx = matched_opt.get("inherited_context", {})
                
                merged_ctx = dict(inh_ctx)
                if e_type in ["service_line", "serviceline"]:
                    merged_ctx["service_line"] = e_name
                    merged_ctx["service_line_id"] = e_id
                elif e_type == "department":
                    merged_ctx["department"] = e_name
                    merged_ctx["department_id"] = e_id

                cap_id = matched_opt.get("capability") or "gp_performance"
                execution_plan = {
                    "business_goal": f"Retrieve {cap_id} for {e_name}",
                    "confidence_score": 1.0,
                    "business_capabilities": [{
                        "id": cap_id,
                        "confidence_score": 1.0,
                        "context": merged_ctx
                    }],
                    "entities": [{"type": e_type, "value": e_name}],
                    "resolved_entities": [{
                        "status": "RESOLVED",
                        "entity_type": e_type.capitalize(),
                        "entity_name": e_name,
                        "resolved_name": e_name,
                        "entity_id": e_id,
                        "resolved_id": e_id,
                        "confidence": 1.0
                    }],
                    "presentation_mode": "table",
                    "request_type": "FOLLOW_UP",
                    "service_line_id": merged_ctx.get("service_line_id"),
                    "department_id": merged_ctx.get("department_id"),
                    "metric": merged_ctx.get("metric", "GP"),
                    "financial_year": merged_ctx.get("financial_year", "FY2526")
                }
                context.request_metadata["token_usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model_name": "deterministic_followup_fast_path"
                }
                tracker.record_planner_output(execution_plan)
                logger.info(f"[LOGGING] request_type=FOLLOW_UP resolved_followup_option='{e_name}' merged_context={merged_ctx}")

        # Active Session Context Continuity Fast-Path for metric queries like 'show GP'
        if not execution_plan and context.question.strip().lower() in ["show gp", "gp", "gp performance", "view gp", "audit gp", "show audit gp"]:
            active_filters = session_memory.get("active_filters") or {}
            sl_id = active_filters.get("service_line_id") or session_memory.get("service_line_id")
            sl_name = active_filters.get("service_line") or session_memory.get("service_line")
            dept_id = active_filters.get("department_id") or session_memory.get("department_id")
            dept_name = active_filters.get("department") or session_memory.get("department")

            if sl_id or dept_id or sl_name or dept_name:
                merged_ctx = dict(active_filters)
                if sl_id: merged_ctx["service_line_id"] = sl_id
                if sl_name: merged_ctx["service_line"] = sl_name
                if dept_id: merged_ctx["department_id"] = dept_id
                if dept_name: merged_ctx["department"] = dept_name

                execution_plan = {
                    "business_goal": f"Retrieve GP performance for active context ({dept_name or sl_name or 'Audit'})",
                    "confidence_score": 1.0,
                    "business_capabilities": [{
                        "id": "gp_performance",
                        "confidence_score": 1.0,
                        "context": merged_ctx
                    }],
                    "entities": [{"type": "service_line" if sl_id else "department", "value": sl_name or dept_name or "Audit"}],
                    "resolved_entities": [{
                        "status": "RESOLVED",
                        "entity_type": "Service_line" if sl_id else "Department",
                        "entity_name": sl_name or dept_name or "Audit",
                        "resolved_name": sl_name or dept_name or "Audit",
                        "entity_id": sl_id or dept_id or 1,
                        "resolved_id": sl_id or dept_id or 1,
                        "confidence": 1.0
                    }],
                    "presentation_mode": "table",
                    "request_type": "FOLLOW_UP",
                    "service_line_id": merged_ctx.get("service_line_id"),
                    "department_id": merged_ctx.get("department_id"),
                    "metric": "GP",
                    "financial_year": merged_ctx.get("financial_year", "FY2526")
                }
                context.request_metadata["token_usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model_name": "active_context_gp_fast_path"
                }
                tracker.record_planner_output(execution_plan)
                logger.info(f"[LOGGING] request_type=FOLLOW_UP active_context_reused merged_context={merged_ctx}")

        # Deterministic Entity Resolution Fast-Path Check
        if not execution_plan and len(context.question.strip().split()) <= 4:
            from .entity_resolver import resolve_entity
            res_obj = await resolve_entity(context.question, jwt_token=context.jwt_token)
            res_dict = res_obj.to_dict()
            if res_dict.get("status") == "RESOLVED":
                e_type = str(res_dict.get("entity_type", "")).lower()
                e_id = res_dict.get("resolved_id") or res_dict.get("entity_id")
                e_name = res_dict.get("resolved_name") or res_dict.get("entity_name")
                
                prev_filters = (session_memory.get("active_filters") or {}) if req_type == "FOLLOW_UP" else {}
                merged_ctx = dict(prev_filters)
                if e_type in ["service_line", "serviceline"]:
                    merged_ctx["service_line"] = e_name
                    merged_ctx["service_line_id"] = e_id
                elif e_type == "department":
                    merged_ctx["department"] = e_name
                    merged_ctx["department_id"] = e_id

                cap_id = "gp_performance"
                if "revenue" in context.question.lower():
                    cap_id = "revenue_analysis"

                execution_plan = {
                    "business_goal": f"Retrieve {cap_id} for {e_name}",
                    "confidence_score": 1.0,
                    "business_capabilities": [{
                        "id": cap_id,
                        "confidence_score": 1.0,
                        "context": merged_ctx
                    }],
                    "entities": [{"type": e_type, "value": e_name}],
                    "resolved_entities": [res_dict],
                    "presentation_mode": "table",
                    "request_type": req_type,
                    "service_line_id": merged_ctx.get("service_line_id"),
                    "department_id": merged_ctx.get("department_id"),
                    "metric": merged_ctx.get("metric", "GP"),
                    "financial_year": merged_ctx.get("financial_year", "FY2526")
                }

                if e_type in ["service_line", "serviceline"]:
                    followups = conversation_manager.build_dynamic_followup_options(
                        entity_type=e_type,
                        entity_id=e_id,
                        entity_name=e_name,
                        capability=cap_id,
                        current_context=merged_ctx
                    )
                    execution_plan["available_followup_options"] = followups

                context.request_metadata["token_usage"] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "model_name": "deterministic_entity_fast_path"
                }
                tracker.record_planner_output(execution_plan)
                logger.info(f"[LOGGING] request_type={req_type} resolved_entity_type={e_type} resolved_entity_id={e_id} name='{e_name}' merged_context={merged_ctx}")

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
                    from config.llm_factory import extract_token_usage
                    context.request_metadata["token_usage"] = extract_token_usage(raw_msg)

                    execution_plan = plan_obj.model_dump()
                    from registry.contract_engine import resolve_presentation_intent
                    execution_plan["presentation_intent"] = resolve_presentation_intent(context.question)

                    from .intent_normalizer import to_canonical_intent
                    canonical_intent = to_canonical_intent(execution_plan, context.question)
                    execution_plan["canonical_intent"] = canonical_intent.model_dump()
                    execution_plan["operation"] = canonical_intent.operation
                    execution_plan["metric"] = canonical_intent.metric
                    execution_plan["dimension"] = canonical_intent.dimension
                    execution_plan["expected_result_type"] = canonical_intent.expected_result_type
                    if canonical_intent.ranking:
                        execution_plan["limit"] = canonical_intent.ranking.limit
                        execution_plan["sort_order"] = canonical_intent.ranking.direction
                        execution_plan["ranking"] = canonical_intent.ranking.model_dump()
                    if canonical_intent.temporal:
                        execution_plan["start_date"] = canonical_intent.temporal.start_date
                        execution_plan["end_date"] = canonical_intent.temporal.end_date
                        execution_plan["financial_year"] = canonical_intent.temporal.financial_year
                        execution_plan["temporal_scope"] = canonical_intent.temporal.type
                        execution_plan["is_explicit"] = canonical_intent.temporal.is_explicit
                    if canonical_intent.missing_information:
                        execution_plan["missing_information"] = canonical_intent.missing_information
                        logger.info(
                            f"[INTENT_PENDING] capability={canonical_intent.capability} | operation={canonical_intent.operation} | "
                            f"metric={canonical_intent.metric} | dimension={canonical_intent.dimension} | "
                            f"limit={canonical_intent.ranking.limit if canonical_intent.ranking else None} | "
                            f"expected_result_type={canonical_intent.expected_result_type} | "
                            f"missing_fields={canonical_intent.missing_information}"
                        )
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

        logger.info(f"Active Business Execution Plan: {json.dumps(sanitize_for_log(execution_plan), indent=2)}")

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
        if is_resumed_turn:
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
                        logger.info(f"[ExecutiveMemory] Inherited {len(prev_entities)} entities from session memory for clarification turn.")
                        execution_plan.setdefault("resolved_entities", prev_entities)
            except Exception as mem_err:
                logger.warning(f"[ExecutiveMemory] Session memory read failed (non-fatal): {mem_err}")

        # Reset entity_errors and missing_information per turn (stateless execution context)
        execution_plan["entity_errors"] = []
        execution_plan["missing_information"] = []

        # Multi-turn ambiguity resolution check
        if is_resumed_turn and previous_plan and previous_plan.get("entity_errors"):
            prev_errs = previous_plan.get("entity_errors", [])
            if prev_errs:
                err0 = prev_errs[0]
                matches = err0.get("matches", [])
                from .entity_resolver import resolve_ambiguity_selection
                matched_cand = resolve_ambiguity_selection(context.question, matches, err0.get("candidate_entity_types"))
                if matched_cand:
                    c_id = matched_cand.get("id") or matched_cand.get("entity_id")
                    c_name = matched_cand.get("name") or matched_cand.get("entity_name") or matched_cand.get("resolved_name")
                    c_type = matched_cand.get("entity_type") or err0.get("entity_type") or "entity"
                    resolved_entity_obj = {
                        "status": "resolved",
                        "entity_type": c_type.capitalize(),
                        "entity_id": c_id,
                        "resolved_id": c_id,
                        "entity_name": c_name,
                        "resolved_name": c_name,
                        "confidence": 1.0
                    }
                    execution_plan["resolved_entities"] = [resolved_entity_obj]
                    execution_plan["entity_errors"] = []
                    logger.info(f"[ENTITY_RESOLUTION] Matched multi-turn selection candidate: type={c_type} id={c_id} name='{c_name}'")

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
                # Do not inject user profile context as query entities on resumed turns
                pass
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
            if e_val and not is_reserved_business_term(e_val):
                if not allowed_entity_types or e_type in allowed_entity_types or e_type in ("customer", "employee", "project", "service_line", "serviceline", "department"):
                    clean_extracted_entities.append(e)
                else:
                    logger.info(f"[Pipeline Instrumentation] Ignored entity '{e_val}' of type '{e_type}' (Not required by capability)")
            else:
                logger.info(f"[Pipeline Instrumentation] Filtered reserved vocabulary term or empty value '{e_val}' from entity resolution.")

        extracted_entities = clean_extracted_entities
        
        # Candidate Entity Recovery: Extract potential candidate entity when extracted_entities is empty
        if not extracted_entities:
            q_lower = context.question.lower()
            has_emp_trig = has_employee_trigger(context.question)
            
            cand_phrase = None
            for prep in [" for ", " by "]:
                if prep in q_lower:
                    cand_phrase = context.question[q_lower.find(prep) + len(prep):].strip()
                    break
            
            if cand_phrase:
                for suffix in ["service line", "department", "team"]:
                    if cand_phrase.lower().endswith(" " + suffix):
                        cand_phrase = cand_phrase[:-len(" " + suffix)].strip()

            cand_target = cand_phrase
            if not cand_target and not is_reserved_business_term(context.question):
                cand_target = context.question

            if cand_target:
                cand_target = re.sub(r'^(?:now\s+)?(?:show|get|generate|view|run)\s+', '', cand_target, flags=re.IGNORECASE).strip()
                cand_target = re.sub(r'\s+(?:revenue|performance|report|metrics|gp)$', '', cand_target, flags=re.IGNORECASE).strip()

            target_entity_type = None
            if cand_target and not is_reserved_business_term(cand_target):
                primary_cap = requested_capabilities[0].get("id") if requested_capabilities else ""

                if primary_cap == "gp_performance":
                    if has_emp_trig and any(kw in q_lower for kw in ["employee", "staff", "consultant", "resource"]):
                        target_entity_type = "employee"
                    else:
                        target_entity_type = "service_line"
                elif has_emp_trig or ("employee" in allowed_entity_types):
                    target_entity_type = "employee"
                    for prefix in [
                        "generate the kpi report for", "get the kpi report for", "show kpi report for",
                        "generate kpi report for", "kpi report for", "kpi summary for", "kpi for", "report for", "generate report for"
                    ]:
                        if prefix in cand_target.lower():
                            cand_target = cand_target[cand_target.lower().find(prefix) + len(prefix):].strip()
                            break
                elif "service_line" in allowed_entity_types or "serviceline" in allowed_entity_types:
                    target_entity_type = "service_line"
                elif "department" in allowed_entity_types:
                    target_entity_type = "department"
                elif "customer" in allowed_entity_types:
                    target_entity_type = "customer"

            logger.info(f"[CANDIDATE_RECOVERY_DECISION] capability={primary_cap or 'none'} candidate='{cand_target}' target_entity_type={target_entity_type or 'none'} reason=candidate_extraction")

            if target_entity_type and cand_target and not is_reserved_business_term(cand_target) and len(cand_target) >= 2:
                extracted_entities = [{"type": target_entity_type, "value": cand_target}]
                requires_entities_flag = True
                logger.info(f"[Planner Entity Recovery] Extracted candidate {target_entity_type} entity '{cand_target}' from query.")
                if primary_cap == "gp_performance":
                    logger.info(f'[GP_ENTITY_RECOVERY] candidate="{cand_target}" entity_type={target_entity_type}')

        resolved_entities = execution_plan.get("resolved_entities", [])
        entity_errors = []

        # Check for name-only entity discovery query (e.g. "shashan arya")
        is_name_only_query = any(c.get("id") == "entity_discovery" for c in requested_capabilities)
        if not is_name_only_query and not extracted_entities and not requires_entities_flag:
            q_clean = context.question.strip()
            q_words = q_clean.split()
            if 1 <= len(q_words) <= 4 and not is_reserved_business_term(q_clean):
                action_verbs = {"what", "how", "show", "generate", "get", "run", "list", "total", "kpi", "report", "compare", "vs", "status", "export"}
                if not any(w.lower() in action_verbs for w in q_words):
                    is_name_only_query = True

        if is_name_only_query and not resolved_entities:
            from .entity_resolver import resolve_entity
            res_obj = await resolve_entity(context.question, entity_type=None, jwt_token=context.jwt_token)
            res_dict = res_obj.to_dict()
            status = res_dict.get("status")
            if status == "resolved":
                resolved_entities = [res_dict]
                entity_errors = []
            elif status == "ambiguous":
                entity_errors = [{
                    "type": "entity_error",
                    "error_type": "multiple_matches",
                    "entity_type": res_dict.get("entity_type", "entity"),
                    "query": context.question,
                    "matches": res_dict.get("matches", [])
                }]
            elif status == "ambiguous_entity_type":
                entity_errors = [{
                    "type": "entity_error",
                    "error_type": "ambiguous_entity_type",
                    "query": context.question,
                    "matches": res_dict.get("matches", []),
                    "candidate_entity_types": res_dict.get("candidate_entity_types", [])
                }]
            elif status == "not_found":
                entity_errors = [{
                    "type": "entity_error",
                    "error_type": "not_found",
                    "entity_type": res_dict.get("entity_type", "entity"),
                    "query": context.question
                }]

        # If capabilities require NO input entities AND no entities were extracted from query, skip Entity Resolver
        elif not requires_entities_flag and not any(c.get("id") == "customer_360_profile" for c in requested_capabilities) and not extracted_entities:
            logger.info(f"[Pipeline Instrumentation] Capabilities {[c.get('id') for c in requested_capabilities]} require NO input entities and query extracted no entities. Skipping Entity Resolver.")
            resolved_entities = []
            entity_errors = []
        else:
            if not resolved_entities and extracted_entities:
                extracted_entities = [e for e in extracted_entities if str(e.get("value", "")).lower() != "all"]
                if extracted_entities:
                    with tracker.track_time("entity_resolver_ms"):
                        resolved_entities, entity_errors = await resolve_entities(extracted_entities, context.jwt_token, full_query=context.question)

        logger.info(f"[Pipeline Instrumentation] Resolved Entities: {sanitize_for_log(resolved_entities)} | Entity Errors: {entity_errors}")
        tracker.record_entity_resolution(resolved_entities, entity_errors)

        if is_name_only_query and resolved_entities and not entity_errors:
            from engine.presentation_policy import PresentationPolicy
            r0 = resolved_entities[0]
            fmt_res = PresentationPolicy.format_entity_resolution({
                "status": "RESOLVED",
                "entity_type": r0.get("entity_type", "entity"),
                "resolved_name": r0.get("resolved_name") or r0.get("entity_name"),
                "has_detail_capability": False
            })
            fmt_res["execution_plan"] = execution_plan
            tracker.dump_trace()
            return fmt_res
        
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

        # Propagate resolved entities (employee, customer, project) into shared_ctx and override capability context
        explicit_entity_overrides = {}
        for ent in resolved_entities:
            if isinstance(ent, dict):
                e_type = str(ent.get("entity_type") or ent.get("type") or "").lower()
                e_id = ent.get("entity_id") or ent.get("resolved_id") or ent.get("id")
                e_name = ent.get("resolved_name") or ent.get("entity_name") or ent.get("name") or ent.get("value")
                if "employee" in e_type:
                    if e_id:
                        shared_ctx["employee_id"] = e_id
                        explicit_entity_overrides["employee_id"] = e_id
                    if e_name:
                        shared_ctx["employee_name"] = e_name
                        explicit_entity_overrides["employee_name"] = e_name
                elif "customer" in e_type:
                    if e_id:
                        shared_ctx["customer_id"] = e_id
                        explicit_entity_overrides["customer_id"] = e_id
                    if e_name:
                        shared_ctx["customer_name"] = e_name
                        explicit_entity_overrides["customer_name"] = e_name
                elif "project" in e_type:
                    if e_id:
                        shared_ctx["project_id"] = e_id
                        explicit_entity_overrides["project_id"] = e_id
                    if e_name:
                        shared_ctx["project_name"] = e_name
                        explicit_entity_overrides["project_name"] = e_name
                elif "service_line" in e_type or "serviceline" in e_type:
                    if e_id:
                        shared_ctx["service_line_id"] = e_id
                        explicit_entity_overrides["service_line_id"] = e_id
                    if e_name:
                        shared_ctx["service_line"] = e_name
                        explicit_entity_overrides["service_line"] = e_name
                    shared_ctx["dimension"] = "service_line"
                    explicit_entity_overrides["dimension"] = "service_line"
                    shared_ctx["scope"] = ["filtered"]
                    explicit_entity_overrides["scope"] = ["filtered"]
                    shared_ctx.pop("department", None)
                    shared_ctx.pop("department_id", None)
                    explicit_entity_overrides.pop("department", None)
                    explicit_entity_overrides.pop("department_id", None)
                    logger.info(
                        f"[ENTITY_RESOLUTION] type=service_line input='{ent.get('query') or ent.get('value') or e_name}' "
                        f"resolved_id={e_id} resolved_name=\"{e_name}\""
                    )
                elif "department" in e_type:
                    if e_id:
                        shared_ctx["department_id"] = e_id
                        explicit_entity_overrides["department_id"] = e_id
                    if e_name:
                        shared_ctx["department"] = e_name
                        explicit_entity_overrides["department"] = e_name
                    shared_ctx["dimension"] = "department"
                    explicit_entity_overrides["dimension"] = "department"
                    shared_ctx["scope"] = ["filtered"]
                    explicit_entity_overrides["scope"] = ["filtered"]
                    logger.info(
                        f"[ENTITY_RESOLUTION] type=department input='{ent.get('query') or ent.get('value') or e_name}' "
                        f"resolved_id={e_id} resolved_name=\"{e_name}\""
                    )

        # User security profile context remains isolated under user_context/security_context and is NOT auto-injected into query filters.

        # Sync canonical_intent fields with resolved entity details
        if "canonical_intent" in execution_plan and isinstance(execution_plan["canonical_intent"], dict):
            c_intent = execution_plan["canonical_intent"]
            if c_intent.get("capability") == "kpi_summary" and explicit_entity_overrides.get("employee_id"):
                c_intent["operation"] = "summary"
                c_intent["expected_result_type"] = "summary"
                c_intent["ranking"] = None
                execution_plan["operation"] = "summary"
                execution_plan["expected_result_type"] = "summary"
                execution_plan["ranking"] = None

        # Propagate operational metadata fields (ranking, comparison, metric, intent, scope, dimension, limit, sort_order, etc.)
        for field in [
            "business_goal", "intent", "operation", "metric", "dimension",
            "ranking", "limit", "sort_order", "comparison", "comparison_periods",
            "aggregation", "scope", "expected_result_type",
            "temporal_scope", "start_date", "end_date", "financial_year", "is_explicit"
        ]:
            if execution_plan.get(field) and field not in shared_ctx:
                shared_ctx[field] = execution_plan[field]

        if not execution_plan.get("business_capabilities"):
            q_lower = context.question.lower()
            recovered_cap_id = None
            if any(e.get("entity_type") in ("Employee", "employee") for e in (resolved_entities or [])) or "kpi" in q_lower:
                recovered_cap_id = "kpi_summary"
            elif "gp" in q_lower or "gross profit" in q_lower:
                recovered_cap_id = "gp_performance"
            elif "revenue" in q_lower:
                recovered_cap_id = "revenue_analysis"
            elif "receivable" in q_lower:
                recovered_cap_id = "receivables_summary"
            else:
                recovered_cap_id = "kpi_summary"

            logger.info(f"[Planner] Recovered missing business capability '{recovered_cap_id}' for query '{context.question}'")
            execution_plan["business_capabilities"] = [{
                "id": recovered_cap_id,
                "scope": "organization" if not resolved_entities else "filtered",
                "intent": execution_plan.get("operation") or "summary",
                "operation": execution_plan.get("operation") or "summary",
                "context": shared_ctx
            }]

        for cap in execution_plan.get("business_capabilities", []):
            cap_ctx = cap.setdefault("context", {})
            # Explicit entity overrides (resolved from user query) take priority over pre-existing context
            for k, v in explicit_entity_overrides.items():
                cap_ctx[k] = v
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
            clean_errs = [err for err in validation_errors if err]
            err_content = clean_errs[0] if clean_errs else "I need a bit more detail to answer your request accurately. Could you please specify which customer, project, or revenue report you would like to view?"
            return {"type": "done", "content": err_content, "is_clarification": True, "execution_plan": execution_plan}

        user_id = context.user_context.get("user_id", 0) if context.user_context else 0
        primary_cap = execution_plan["business_capabilities"][0]["id"] if execution_plan.get("business_capabilities") else "general"
        fy_val = context.user_context.get("financial_year", "") if context.user_context else ""

        # Strict Fail-Closed Check for Employee KPI Reports
        is_kpi_summary = (primary_cap == "kpi_summary" or any(c.get("id") == "kpi_summary" for c in execution_plan.get("business_capabilities", [])))
        if is_kpi_summary:
            req_emp_in_query = has_employee_trigger(context.question) or any(k in context.question.lower() for k in ["for ", "by ", "employee", "staff"])
            has_res_emp = any(str(e.get("entity_type", "")).lower() == "employee" and e.get("entity_id") is not None for e in resolved_entities)
            if req_emp_in_query and not has_res_emp:
                logger.warning(f"[KPI_FAIL_CLOSED] Employee requested in query '{context.question}' but no valid employee_id resolved. Short-circuiting execution.")
                raw_emp_term = context.question
                for prefix in [
                    "generate the kpi report for", "get the kpi report for", "show kpi report for",
                    "generate kpi report for", "kpi report for", "kpi summary for", "kpi for", "report for"
                ]:
                    if prefix in raw_emp_term.lower():
                        raw_emp_term = raw_emp_term.lower().split(prefix)[-1].strip()
                        break
                tracker.dump_trace()
                return {
                    "type": "done",
                    "content": f"Could not find an active employee matching '{raw_emp_term}' in CRM master data.",
                    "is_clarification": True,
                    "execution_plan": execution_plan,
                    "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                }

        # 3.8 Entity-Aware Cache Check (Post Entity Resolution, Pre-Backend Execution)
        from memory.session_manager import build_entity_cache_key, get_entity_cache, set_entity_cache

        cache_cap_ctx = execution_plan["business_capabilities"][0].get("context", {}) if execution_plan.get("business_capabilities") else {}
        cache_key = build_entity_cache_key(user_id, primary_cap, resolved_entities, financial_year=fy_val, filters=cache_cap_ctx)
        cached_response = None if is_resumed_turn else await get_entity_cache(cache_key)
        if cached_response:
            c_str = str(cached_response.get("content", "")).lower()
            if "unknown customer" in c_str or "no additional data is available" in c_str or ("unknown" in c_str and "0.00" in c_str):
                logger.info(f"[Planner] Discarding stale cached response containing Unknown Customer/0.00 for key='{cache_key}'.")
                cached_response = None
            elif primary_cap == "recoverability_analysis" and not any(p in c_str for p in ["recoverability percentage", "actual recoverability", "portfolio recoverability"]):
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
        
        # Pre-Tool Execution Revenue Scope Validation (Fail Closed)
        is_revenue_analysis = any(c.get("id") == "revenue_analysis" for c in execution_plan.get("business_capabilities", []))
        has_sl_entity_in_query = any(str(e.get("type", "")).lower() in ("service_line", "serviceline") for e in (extracted_entities or [])) or "service_line" in shared_ctx
        if is_revenue_analysis and has_sl_entity_in_query and not shared_ctx.get("service_line_id"):
            logger.error("[REVENUE_SCOPE_VALIDATION] requested=service_line status=FAIL reason=missing_service_line_id")
            tracker.dump_trace()
            return {
                "type": "done",
                "content": "Revenue report generation failed: Could not resolve valid service_line_id for requested service line.",
                "is_clarification": True,
                "execution_plan": execution_plan,
                "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            }

        # Post-Tool Execution KPI Entity Validation
        if is_kpi_summary and shared_ctx.get("employee_id"):
            req_eid = int(shared_ctx["employee_id"])
            raw_kpi = tool_results[0].get("result", {}) if tool_results and isinstance(tool_results[0].get("result"), dict) else {}
            if isinstance(raw_kpi.get("data"), dict):
                raw_kpi = raw_kpi["data"]
            elif isinstance(raw_kpi.get("summary"), dict):
                raw_kpi = raw_kpi["summary"]
            
            ret_eid = raw_kpi.get("employee_id") or raw_kpi.get("emp_id")
            is_org_agg = raw_kpi.get("is_organization_aggregate", False)
            if is_org_agg is True or (ret_eid is not None and int(ret_eid) != req_eid):
                logger.error(f"[KPI_ENTITY_VALIDATION] Fail-closed: requested employee_id={req_eid}, but backend returned employee_id={ret_eid}, is_org_aggregate={is_org_agg}")
                tracker.dump_trace()
                return {
                    "type": "done",
                    "content": f"KPI report generation failed: Employee data mismatch or unavailable for requested employee ID {req_eid}.",
                    "is_clarification": True,
                    "execution_plan": execution_plan,
                    "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                }

        # Post-Tool Execution Revenue Scope Validation
        if is_revenue_analysis and shared_ctx.get("service_line_id"):
            req_sl_id = int(shared_ctx["service_line_id"])
            raw_rev = tool_results[0].get("result", {}) if tool_results and isinstance(tool_results[0].get("result"), dict) else {}
            if isinstance(raw_rev.get("payload"), dict):
                raw_rev = raw_rev["payload"]
            elif isinstance(raw_rev.get("data"), dict):
                raw_rev = raw_rev["data"]
            
            ret_sl_id = raw_rev.get("service_line_id")
            is_org_agg = raw_rev.get("is_organization_aggregate", False)
            if is_org_agg is True or (ret_sl_id is not None and int(ret_sl_id) != req_sl_id):
                logger.error(f"[REVENUE_SCOPE_VALIDATION] requested=service_line requested_id={req_sl_id} returned_id={ret_sl_id} is_org_agg={is_org_agg} status=FAIL")
                tracker.dump_trace()
                return {
                    "type": "done",
                    "content": f"Revenue report generation failed: Returned data does not match requested service line ID {req_sl_id}.",
                    "is_clarification": True,
                    "execution_plan": execution_plan,
                    "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                }
            else:
                logger.info(f"[REVENUE_SCOPE_VALIDATION] requested=service_line requested_id={req_sl_id} status=PASS")

        # Post-Tool Execution GP Scope Validation & Entity Isolation
        is_gp_performance = any(c.get("id") == "gp_performance" for c in execution_plan.get("business_capabilities", []))
        if is_gp_performance and (shared_ctx.get("service_line_id") or shared_ctx.get("department_id") or shared_ctx.get("service_line")):
            _filter_tool_results_by_entity_scope(tool_results, shared_ctx)

            req_sl_id = shared_ctx.get("service_line_id")
            successful_res = None
            for tr in tool_results:
                res_val = tr.get("result") if isinstance(tr, dict) else tr
                if isinstance(res_val, list) and len(res_val) > 0:
                    successful_res = res_val
                    break
                elif isinstance(res_val, dict) and res_val.get("status") != "FAIL":
                    if res_val.get("rows") or res_val.get("data") or res_val.get("result"):
                        successful_res = res_val
                        break

            if successful_res is None:
                logger.error(f"[GP_SCOPE_VALIDATION] status=FAIL reason=entity_not_found_or_scope_mismatch")
                tracker.dump_trace()
                return {
                    "type": "done",
                    "content": f"GP performance report generation failed: Could not find matching entity in backend response.",
                    "answer": f"GP performance report generation failed: Could not find matching entity in backend response.",
                    "is_clarification": True,
                    "execution_plan": execution_plan,
                    "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                }
            else:
                logger.info(f"[GP_SCOPE_VALIDATION] capability=gp_performance requested_service_line_id={req_sl_id} status=PASS")

        # 5. Response Generation (Presentation Mode Governed)
        from .synthesizer import format_data_response, synthesize_response

        q_clean = context.question.lower()
        pres_mode = str(execution_plan.get("presentation_mode") or "").upper()
        is_explicit_table_query = any(phrase in q_clean for phrase in ["show table", "list all", "table format", "raw records", "export table"])

        if pres_mode == "TABLE" and is_explicit_table_query:
            logger.info(f"[Req: {tracker.request_id}] Executing DATA MODE Direct Formatter for explicit table query.")
            tracker.timings["synthesizer_ms"] = 0.0
            final_response = format_data_response(context.question, tool_results)
        elif is_kpi_summary and (execution_plan.get("operation") or "summary") == "summary":
            logger.info(f"[Req: {tracker.request_id}] Executing Deterministic KPI Summary Rendering (0 LLM Tokens).")
            tracker.timings["synthesizer_ms"] = 0.0
            try:
                from main import _build_kpi_contract
                from engine.renderer_engine import get_renderer_engine
                from registry.capability_catalog import get_capability_metadata

                raw_kpi_data = {}
                for tr_item in (tool_results or []):
                    if isinstance(tr_item, str):
                        try:
                            tr_item = json.loads(tr_item)
                        except Exception:
                            pass
                    if isinstance(tr_item, dict):
                        candidates = [
                            tr_item.get("payload"),
                            tr_item.get("result"),
                            tr_item.get("data"),
                            tr_item.get("raw_response"),
                            tr_item
                        ]
                        for cand in candidates:
                            if isinstance(cand, str):
                                try:
                                    cand = json.loads(cand)
                                except Exception:
                                    pass
                            if isinstance(cand, dict) and cand:
                                raw_kpi_data = cand
                                break
                        if raw_kpi_data:
                            break

                if isinstance(raw_kpi_data.get("data"), dict):
                    raw_kpi_data = raw_kpi_data["data"]
                elif isinstance(raw_kpi_data.get("summary"), dict) and raw_kpi_data.get("summary", {}).get("employee_id") is not None:
                    _summary_inner = raw_kpi_data["summary"]
                    raw_kpi_data = {**_summary_inner}
                    if raw_kpi_data.get("date_range"):
                        raw_kpi_data["date_range"] = raw_kpi_data["date_range"]
                    if raw_kpi_data.get("projects_by_status"):
                        raw_kpi_data["projects_by_status"] = raw_kpi_data["projects_by_status"]

                filters_applied = {
                    "service_line": shared_ctx.get("service_line"),
                    "department": shared_ctx.get("department"),
                    "employee_name": shared_ctx.get("employee_name"),
                    "customer": shared_ctx.get("customer"),
                    "financial_year": shared_ctx.get("financial_year"),
                    "date_range": shared_ctx.get("date_range"),
                    "service_line_id": shared_ctx.get("service_line_id"),
                    "department_id": shared_ctx.get("department_id"),
                    "employee_id": shared_ctx.get("employee_id"),
                    "customer_id": shared_ctx.get("customer_id")
                }
                period = {
                    "start_date": shared_ctx.get("start_date", ""),
                    "end_date": shared_ctx.get("end_date", "")
                }
                normalized_contract = _build_kpi_contract(raw_kpi_data, {}, filters_applied, period)

                if isinstance(normalized_contract.get("summary"), dict):
                    if shared_ctx.get("employee_id"):
                        normalized_contract["summary"]["employee_id"] = shared_ctx["employee_id"]
                    if shared_ctx.get("employee_name"):
                        normalized_contract["summary"]["employee_name"] = shared_ctx["employee_name"]

                if raw_kpi_data.get("projects_by_status"):
                    normalized_contract["projects_by_status"] = raw_kpi_data["projects_by_status"]

                envelope = {
                    "status": "success",
                    "confidence": "verified",
                    "source": "kpi_summary_contract",
                    "payload": normalized_contract,
                }

                cap_meta = get_capability_metadata("kpi_summary") or {"id": "kpi_summary"}
                renderer = get_renderer_engine()
                rendered_content = renderer.render(cap_meta, envelope)

                final_response = {
                    "type": "done",
                    "content": rendered_content,
                    "answer": rendered_content,
                    "token_usage": {
                        "model_name": os.getenv("LLM_MODEL") or "openai/gpt-oss-20b",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0
                    }
                }
            except Exception as _kpi_rend_err:
                import traceback
                logger.error(f"[Planner] Deterministic KPI rendering failed ({_kpi_rend_err}), falling back to synthesizer.")
                traceback.print_exc()
                with tracker.track_time("synthesizer_ms"):
                    final_response = await synthesize_response(context.question, tool_results, self.llm, execution_plan=execution_plan)
        elif is_gp_performance:
            logger.info(f"[Req: {tracker.request_id}] Executing Deterministic GP Performance Rendering (0 LLM Tokens).")
            logger.info("[PRESENTATION_MODE] capability=gp_performance mode=DETERMINISTIC")
            tracker.timings["synthesizer_ms"] = 0.0
            try:
                from engine.renderer_engine import get_renderer_engine
                from registry.capability_catalog import get_capability_metadata

                raw_gp_data = {}
                for tr_item in (tool_results or []):
                    if isinstance(tr_item, str):
                        try:
                            tr_item = json.loads(tr_item)
                        except Exception:
                            pass
                    if isinstance(tr_item, dict):
                        res_obj = tr_item.get("result") if tr_item.get("result") is not None else tr_item
                        if isinstance(res_obj, str):
                            try:
                                res_obj = json.loads(res_obj)
                            except Exception:
                                pass
                        cand = None
                        if isinstance(res_obj, dict):
                            cand = res_obj.get("payload") or res_obj.get("rows") or res_obj.get("data") or res_obj.get("items") or res_obj
                        elif isinstance(res_obj, list):
                            cand = res_obj
                        else:
                            cand = tr_item

                        if isinstance(cand, list) and len(cand) == 1:
                            raw_gp_data = cand[0]
                        elif isinstance(cand, list) and len(cand) > 1:
                            raw_gp_data = cand
                        elif isinstance(cand, dict):
                            # If dict has 'rows' inside, unwrap further
                            if isinstance(cand.get("rows"), list) and len(cand["rows"]) == 1:
                                raw_gp_data = cand["rows"][0]
                            elif isinstance(cand.get("rows"), list) and len(cand["rows"]) > 1:
                                raw_gp_data = cand["rows"]
                            else:
                                raw_gp_data = cand

                        if raw_gp_data:
                            break

                envelope = {
                    "status": "success",
                    "confidence": "verified",
                    "source": "gp_performance",
                    "payload": raw_gp_data,
                }
                cap_meta = get_capability_metadata("gp_performance") or {"id": "gp_performance"}
                renderer = get_renderer_engine()
                rendered_content = renderer.render(cap_meta, envelope)

                if not rendered_content or not str(rendered_content).strip():
                    rendered_content = "No GP performance data found for the requested service line."

                final_response = {
                    "type": "done",
                    "content": rendered_content,
                    "answer": rendered_content,
                    "token_usage": {
                        "model_name": os.getenv("LLM_MODEL") or "openai/gpt-oss-20b",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0
                    }
                }
                from registry.contract_engine import wrap_presentation_intent
                final_response = wrap_presentation_intent(final_response, canonical_intent, cap_id)
            except Exception as _gp_rend_err:
                logger.error(f"[Planner] Deterministic GP rendering failed ({_gp_rend_err}), falling back to synthesizer.")
                logger.info("[PRESENTATION_MODE] capability=gp_performance mode=LLM")
                with tracker.track_time("synthesizer_ms"):
                    final_response = await synthesize_response(context.question, tool_results, self.llm, execution_plan=execution_plan)
        elif is_revenue_analysis and (execution_plan.get("operation") or "summary") in ("summary", "aggregate", "report"):
            logger.info(f"[Req: {tracker.request_id}] Executing Deterministic Revenue Analysis Rendering (0 LLM Tokens).")
            logger.info("[PRESENTATION_MODE] capability=revenue_analysis mode=DETERMINISTIC")
            tracker.timings["synthesizer_ms"] = 0.0
            try:
                from engine.renderer_engine import get_renderer_engine
                from registry.capability_catalog import get_capability_metadata

                raw_rev_data = {}
                for tr_item in (tool_results or []):
                    if isinstance(tr_item, str):
                        try:
                            tr_item = json.loads(tr_item)
                        except Exception:
                            pass
                    if isinstance(tr_item, dict):
                        cand = tr_item.get("payload") or tr_item.get("result") or tr_item.get("data") or tr_item
                        if isinstance(cand, str):
                            try:
                                cand = json.loads(cand)
                            except Exception:
                                pass
                        if isinstance(cand, dict) and cand:
                            raw_rev_data = cand
                            break

                envelope = {
                    "status": "success",
                    "confidence": "verified",
                    "source": "revenue_analysis",
                    "payload": raw_rev_data,
                }
                cap_meta = get_capability_metadata("revenue_analysis") or {"id": "revenue_analysis"}
                renderer = get_renderer_engine()
                rendered_content = renderer.render(cap_meta, envelope)

                final_response = {
                    "type": "done",
                    "content": rendered_content,
                    "answer": rendered_content,
                    "token_usage": {
                        "model_name": os.getenv("LLM_MODEL") or "openai/gpt-oss-20b",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0
                    }
                }
            except Exception as _rev_rend_err:
                logger.error(f"[Planner] Deterministic Revenue rendering failed ({_rev_rend_err}), falling back to synthesizer.")
                logger.info("[PRESENTATION_MODE] capability=revenue_analysis mode=LLM")
                with tracker.track_time("synthesizer_ms"):
                    final_response = await synthesize_response(context.question, tool_results, self.llm, execution_plan=execution_plan)
        else:
            logger.info(f"[Req: {tracker.request_id}] Executing Executive Response Synthesis (presentation_mode='{pres_mode}').")
            with tracker.track_time("synthesizer_ms"):
                final_response = await synthesize_response(context.question, tool_results, self.llm, execution_plan=execution_plan)
            
            # Pass token usage up and attach execution plan, tool results, and telemetry
            if isinstance(final_response, dict):
                planner_toks_dict = context.request_metadata.get("token_usage") or {}
                synth_toks_dict = final_response.get("token_usage") or {}
                
                planner_in = planner_toks_dict.get("input_tokens", 0)
                planner_out = planner_toks_dict.get("output_tokens", 0)
                planner_tot = planner_toks_dict.get("total_tokens", 0)

                synth_in = synth_toks_dict.get("input_tokens", 0)
                synth_out = synth_toks_dict.get("output_tokens", 0)
                synth_tot = synth_toks_dict.get("total_tokens", 0)

                tot_in = planner_in + synth_in
                tot_out = planner_out + synth_out
                tot_all = planner_tot + synth_tot
                active_model = synth_toks_dict.get("model_name") or planner_toks_dict.get("model_name") or os.getenv("LLM_MODEL") or "openai/gpt-oss-20b"

                final_response["token_usage"] = {
                    "model_name": active_model,
                    "input_tokens": tot_in,
                    "output_tokens": tot_out,
                    "total_tokens": tot_all
                }

                final_response["telemetry"] = {
                    "fast_path": True if synth_tot == 0 else False,
                    "execution_path": "DETERMINISTIC_0_TOKEN" if synth_tot == 0 else ("PLANNER_LLM" if planner_tot > 0 else "SYNTHESIZER_ONLY"),
                    "capability_id": primary_cap or "general_query",
                    "model": active_model,
                    "input_tokens": tot_in,
                    "output_tokens": tot_out,
                    "total_tokens": tot_all,
                    "planner_tokens": planner_tot,
                    "synthesizer_tokens": synth_tot,
                    "backend_ms": tracker.timings.get("tool_execution_ms", 0),
                    "execution_ms": tracker.timings.get("total_ms", 0)
                }
                final_response["execution_plan"] = execution_plan
                final_response["tool_results"] = tool_results


            # Save to Entity Cache only if response is valid (no Unknown Customer sentinels)
            c_content = str(final_response.get("content", "")).lower()
            if "unknown customer" not in c_content and "no additional data is available" not in c_content:
                await set_entity_cache(cache_key, final_response)
            else:
                logger.warning(f"[Planner] Skipping entity cache write for invalid/empty response key='{cache_key}'")

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
        ig_toks = (context.request_metadata.get("intent_gate_token_usage", {}) or {}).get("total_tokens", 0)
        planner_toks = (context.request_metadata.get("planner_token_usage", {}) or {}).get("total_tokens", 0)
        
        is_gp_summary_entity = is_gp_performance and shared_ctx.get("service_line_id") is not None
        synth_invoked = False if (is_kpi_summary or (pres_mode == "TABLE" and is_explicit_table_query) or (is_revenue_analysis and shared_ctx.get("service_line_id") is not None) or is_gp_summary_entity) else True

        if not synth_invoked:
            synth_toks = 0
            synth_calls = 0
        else:
            synth_toks = final_response.get("telemetry", {}).get("synthesizer_tokens", 0) if (isinstance(final_response, dict) and final_response.get("telemetry")) else (final_response.get("token_usage", {}).get("output_tokens", 0) if isinstance(final_response, dict) else 0)
            synth_calls = 1 if synth_toks > 0 else 0

        tot_toks = ig_toks + planner_toks + synth_toks
        budget_pct = round((tot_toks / 5000) * 100, 1)
        llm_calls_cnt = (1 if ig_toks > 0 else 0) + (1 if planner_toks > 0 else 0) + synth_calls

        cust_id_resolved = "None"
        for re_item in (resolved_entities or []):
            if str(re_item.get("entity_type", "")).lower() == "customer":
                cust_id_resolved = str(re_item.get("entity_id"))

        mode_str = "DETERMINISTIC_0_TOKEN" if not synth_invoked else pres_mode

        logger.info(
            f"[Production Telemetry] "
            f"PresentationMode={mode_str} | "
            f"Capability={primary_cap} | "
            f"ResolvedCustomer={cust_id_resolved} | "
            f"FY={fy_val or 'Default'} | "
            f"CacheStatus={'HIT' if final_response.get('was_cached') else 'MISS'} | "
            f"SynthesizerInvoked={synth_invoked} | "
            f"LLMCalls={llm_calls_cnt} | "
            f"IntentGateTokens={ig_toks} | "
            f"PlannerTokens={planner_toks} | "
            f"SynthTokens={synth_toks} | "
            f"TotalTokens={tot_toks} | "
            f"BudgetUsage={budget_pct}%"
        )

        tracker.record_synthesis(final_response)
        tracker.dump_trace()
        
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
        
        capability_schemas = json.dumps(get_planner_capabilities_schema(), separators=(',', ':'))
        
        from datetime import datetime
        now = datetime.now()
        curr_date_str = now.strftime("%Y-%m-%d")
        curr_year = now.year

        prompt = ChatPromptTemplate.from_messages([
            ("system", 
             f"You are the Enterprise Business Analyst for a CRM system.\n"
             f"DATE: {curr_date_str} (Year: {curr_year}, FY: FY{str(curr_year)[2:]})\n"
             f"TEMPORAL RULE: When a month name is mentioned without a year (e.g. 'Jan', 'March'), assume current year ({curr_year}).\n\n"
             "Map user query to abstract Business Capabilities.\n\n"
             "AVAILABLE BUSINESS CAPABILITIES:\n{capabilities}\n\n"
             "RULES:\n"
             "1. INTENT & OPERATION: Map phrasing to intent & operation: 'ranking' (for top/best/highest/biggest/lowest/worst/limit/grouping questions), 'comparison' (for vs/compare/multi-period questions), 'summary' (for totals/overviews), 'generate_report'.\n"
             "2. DIMENSION FOR RANKING: For ranking questions, set 'dimension' explicitly whenever requested grouping is identifiable:\n"
             "   - 'five biggest customers' -> dimension: 'customer', operation: 'ranking', metric: 'revenue', limit: 5\n"
             "   - 'highest revenue department' -> dimension: 'department', operation: 'ranking', metric: 'revenue', limit: 1\n"
             "   - 'best performing service line' -> dimension: 'service_line', operation: 'ranking', metric: 'revenue', limit: 1\n"
             "   - 'top 5 employees by billing' -> dimension: 'employee', operation: 'ranking', metric: 'revenue', limit: 5\n"
             "3. CAPABILITIES: Map terms: Revenue->'revenue_analysis', GP Performance/Gross Profit/GP->'gp_performance', Recoverability->'recoverability_analysis', Receivables->'receivables_analysis', Proposals/Win Rate->'pipeline_analysis', KPI->'kpi_summary', Billing->'staff_billing_report', Projects->'active_projects', Ranking->'analytical_query'. For multi-reports, include all matching capabilities.\n"
             "4. ENTITIES: Extract 'service_line', 'customer', 'department', 'project', 'employee', 'financial_year', 'date_range' into 'entities' array.\n"
             "5. SCOPE: 'organization' for company-wide summaries, 'entity' for specific named entity, 'filtered' for service line/dept filters.\n"
             "6. MISSING INFO: For organization queries, set missing_information=[].\n"
             "7. PRESENTATION: Set presentation_action ('VIEW', 'EXPORT', 'GENERATE') and presentation_mode ('REPORT', 'INSIGHT', 'KPI_CARD', 'TABLE', 'COMPARISON', 'EXECUTIVE_BRIEF') on plan and capability.\n"
             "8. CRITICAL: Do NOT output <think> tags, 'Thinking Process:', or reasoning text. Respond IMMEDIATELY with raw JSON starting with '{{' on line 1.\n\n"
             "OUTPUT: Raw JSON object matching this schema ONLY:\n{json_schema}\n"
            ),
            ("user", "{query}")
        ])
        
        json_schema_str = (
            '{\n'
            '  "business_goal": "Short goal summary",\n'
            '  "confidence_score": 1.0,\n'
            '  "reasoning_summary": "Reasoning for plan",\n'
            '  "ambiguity_detected": false,\n'
            '  "entities": [],\n'
            '  "scope": ["organization"],\n'
            '  "business_capabilities": [\n'
            '    {\n'
            '      "id": "exact_capability_id_from_catalog",\n'
            '      "scope": "organization",\n'
            '      "intent": "ranking",\n'
            '      "operation": "ranking",\n'
            '      "metric": "revenue",\n'
            '      "dimension": "customer",\n'
            '      "ranking": "desc",\n'
            '      "limit": 5,\n'
            '      "presentation_action": "VIEW"\n'
            '    }\n'
            '  ],\n'
            '  "missing_information": [],\n'
            '  "entity_errors": [],\n'
            '  "presentation_action": "VIEW",\n'
            '  "presentation_mode": "REPORT",\n'
            '  "analysis_depth": "summary"\n'
            '}'
        )
        
        chain = prompt | self.llm
        req_id = getattr(self, "current_request_id", None) or "unknown"
        
        # Attempt 1
        attempt = 1
        try:
            try:
                logger.info(f"[LLM_CALL] stage=planner request_id={req_id}")
                raw_msg = await chain.ainvoke({"capabilities": capability_schemas, "json_schema": json_schema_str, "query": query})
            except Exception as primary_err:
                err_str = str(primary_err)
                if "429" in err_str or "rate_limit" in err_str.lower() or "quota" in err_str.lower():
                    fallback_model = os.getenv("FALLBACK_MODEL") or os.getenv("FAST_MODEL") or os.getenv("LLM_MODEL")
                    logger.warning(f"[Planner] Primary model rate-limited (429). Triggering fallback to {fallback_model}...")
                    from config.llm_factory import get_llm
                    fallback_llm = get_llm(model_name=fallback_model, temperature=0.0, stage="planner_retry")
                    fallback_chain = prompt | fallback_llm
                    logger.info(f"[LLM_CALL] stage=planner_retry request_id={req_id}")
                    raw_msg = await fallback_chain.ainvoke({"capabilities": capability_schemas, "json_schema": json_schema_str, "query": query})
                else:
                    raise primary_err

            content = raw_msg.content.strip()
            content = sanitize_planner_json(content)
            plan_obj = BusinessExecutionPlan.model_validate_json(content)
            logger.info(f"[PLANNER_SCHEMA_VALIDATION] attempt={attempt} status=PASS")
            logger.info(f"[PLANNER_SCHEMA_FINAL] status=PASS")
            return {"parsed": plan_obj, "raw": raw_msg}
        except Exception as e1:
            err1_reason = str(e1)
            logger.warning(f"[PLANNER_SCHEMA_VALIDATION] attempt={attempt} status=FAIL")
            logger.warning(f"[PLANNER_SCHEMA_RETRY] reason={err1_reason[:256]}")
            
            # Attempt 2: Bounded retry with compact schema correction prompt
            attempt = 2
            correction_prompt = ChatPromptTemplate.from_messages([
                ("system",
                 f"You are the Enterprise Business Analyst for a CRM system.\n"
                 f"Your previous output failed JSON schema validation with error: {err1_reason[:200]}\n"
                 "CRITICAL SCHEMA CORRECTION REQUIREMENT:\n"
                 "- 'entities' MUST be an array of objects with keys 'type' (e.g. 'service_line', 'customer', 'employee') and 'value' (e.g. 'Audit').\n"
                 "- Do NOT use 'name' instead of 'type'. Use ONLY 'type' and 'value'.\n"
                 "Respond IMMEDIATELY with raw JSON starting with '{{\'."
                ),
                ("user", "{query}")
            ])
            try:
                retry_chain = correction_prompt | self.llm
                logger.info(f"[LLM_CALL] stage=planner_retry request_id={req_id}")
                raw_msg_2 = await retry_chain.ainvoke({"query": query})
                content_2 = raw_msg_2.content.strip()
                content_2 = sanitize_planner_json(content_2)
                plan_obj_2 = BusinessExecutionPlan.model_validate_json(content_2)
                logger.info(f"[PLANNER_SCHEMA_VALIDATION] attempt={attempt} status=PASS")
                logger.info(f"[PLANNER_SCHEMA_FINAL] status=PASS")
                return {"parsed": plan_obj_2, "raw": raw_msg_2}
            except Exception as e2:
                logger.error(f"[PLANNER_SCHEMA_VALIDATION] attempt={attempt} status=FAIL")
                logger.error(f"[PLANNER_SCHEMA_FINAL] status=FAIL")
                raise e2

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

        # Check for explicit temporal answer when a clarification is pending
        if previous_plan:
            missing_info = previous_plan.get("missing_information", [])
            has_pending_clarification = bool(missing_info or previous_plan.get("is_clarification"))
            if has_pending_clarification:
                from .temporal_resolver import resolve_temporal_scope
                temp_res = resolve_temporal_scope(q)
                if temp_res.get("is_explicit"):
                    logger.info(f"[IntentGate] Detected explicit temporal answer for pending clarification: '{q[:60]}'")
                    return False  # ANSWER

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
