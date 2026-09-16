"""
conversation_manager.py — Manages conversational flow, slot filling, smart defaults, and dynamic UI component selection.
"""
import logging
from typing import Dict, Any, List, Optional

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.capability_catalog import get_capability_metadata

logger = logging.getLogger(__name__)

class ConversationManager:
    def __init__(self):
        pass
        
    async def evaluate_confidence_and_slots(
        self, 
        execution_plan: Dict[str, Any], 
        validation_errors: List[str],
        user_query: str = "",
        llm: Any = None
    ) -> Optional[Dict[str, Any]]:
        """
        Takes the current execution plan and missing information, and determines the next conversational step.
        Returns a response dictionary if a clarification is needed, or None if execution can proceed.
        """
        confidence = execution_plan.get("confidence_score", 0.0)
        missing_info = execution_plan.get("missing_information", [])
        
        # 1. Low Confidence: Ask for intent clarification
        if confidence < 0.60:
            return {
                "type": "done",
                "content": "I'm not entirely sure I understand the exact report or action you need. Could you clarify your request?",
                "is_clarification": True,
                "execution_plan": execution_plan
            }
            
        # 2. Entity Resolution Errors
        entity_errors = execution_plan.get("entity_errors", [])
        if entity_errors:
            err = entity_errors[0]
            err_type = err.get("error_type")
            e_type = err.get("entity_type", "entity")
            query = err.get("query", "")
            
            from engine.presentation_policy import PresentationPolicy
            if err_type == "multiple_matches":
                res_dict = {
                    "status": "AMBIGUOUS",
                    "entity_type": e_type,
                    "input_value": query,
                    "candidates": err.get("matches", [])
                }
            elif err_type == "ambiguous_entity_type":
                res_dict = {
                    "status": "AMBIGUOUS_ENTITY_TYPE",
                    "input_value": query,
                    "candidates": err.get("matches", []),
                    "candidate_entity_types": err.get("candidate_entity_types", [])
                }
            elif err_type == "not_found":
                res_dict = {
                    "status": "NOT_FOUND",
                    "entity_type": e_type,
                    "input_value": query
                }
            else:
                res_dict = {
                    "status": "BACKEND_ERROR",
                    "entity_type": e_type,
                    "input_value": query,
                    "message": err.get("message", "Unknown error")
                }
            
            formatted_res = PresentationPolicy.format_entity_resolution(res_dict)
            formatted_res["execution_plan"] = execution_plan
            return formatted_res
            
        # 3. Handle missing parameters dynamically
        if not missing_info:
            return None # All good to proceed
            
        # Extract unique capability IDs & missing parameter keys across all missing entries
        cap_ids = []
        missing_keys = []
        for mp in missing_info:
            if isinstance(mp, str):
                if mp not in missing_keys:
                    missing_keys.append(mp)
            elif isinstance(mp, dict):
                k = mp.get("key")
                c_id = mp.get("capability_id")
                if k and k not in missing_keys:
                    missing_keys.append(k)
                if c_id and c_id not in cap_ids:
                    cap_ids.append(c_id)

        if not cap_ids:
            for cap in execution_plan.get("business_capabilities", []):
                if cap.get("id") and cap.get("id") not in cap_ids:
                    cap_ids.append(cap.get("id"))

        cap_names = []
        for c_id in cap_ids:
            metadata = get_capability_metadata(c_id)
            if metadata and metadata.get("description"):
                name = metadata["description"].split(".")[0].split("(")[0].strip().title()
            else:
                name = c_id.replace("_", " ").title()
            if name and name not in cap_names:
                cap_names.append(name)

        if len(cap_names) == 1:
            cap_str = cap_names[0]
        elif len(cap_names) == 2:
            cap_str = f"{cap_names[0]} and {cap_names[1]}"
        else:
            cap_str = ", ".join(cap_names[:-1]) + f", and {cap_names[-1]}"

        # Focus strictly on the primary missing parameter (one item at a time)
        first_missing = missing_info[0]
        if isinstance(first_missing, str):
            param_key = first_missing
        else:
            param_key = first_missing.get("key")

        from engine.presentation_policy import PresentationPolicy
        clar_res = PresentationPolicy.format_clarification({
            "missing_field": param_key,
            "user_query": user_query,
            "original_intent": execution_plan
        })
        clar_res["execution_plan"] = execution_plan
        return clar_res

    async def _generate_dynamic_followup(
        self, 
        user_query: str, 
        cap_str: str, 
        param_key: str, 
        llm: Any = None
    ) -> str:
        """
        Dynamically generates a single, context-aware, LLM-driven follow-up question for missing context.
        """
        param_label = (param_key or "").replace("_", " ").title()

        if llm:
            try:
                from langchain_core.messages import SystemMessage
                sys_prompt = (
                    "You are an intelligent, natural, and polite Enterprise Business Analyst assistant.\n"
                    "The user initiated a business query, but one required parameter is missing to execute the report.\n\n"
                    f"USER QUERY: {user_query or 'Generate report'}\n"
                    f"REQUESTED REPORT(S): {cap_str}\n"
                    f"MISSING PARAMETER NEEDED: {param_label}\n\n"
                    "RULES:\n"
                    "1. Ask a single, polite, natural, and context-aware conversational question asking ONLY for the missing parameter.\n"
                    "2. Adapt the phrasing directly to what the user is trying to accomplish (e.g. 'Sure! Which parameter value would you like me to analyze?').\n"
                    "3. DO NOT include static bullet points, hardcoded lists, or fake option values.\n"
                    "4. Ask for ONLY ONE missing item at a time in 1 concise sentence.\n"
                    "5. Keep the response completely conversational and plain text."
                )
                res = await llm.ainvoke([SystemMessage(content=sys_prompt)])
                content = res.content.strip()
                if content:
                    return content
            except Exception as e:
                logger.warning(f"Failed to generate dynamic LLM follow-up question: {e}")

        # Completely generic fallback template
        return f"Sure! Which {param_label} should I consider for {cap_str}?"

    def _guess_ui_component(self, param_key: str) -> str:
        return "text_input"

    def check_context_compatibility(
        self,
        current_context: Optional[Dict[str, Any]],
        previous_plan: Optional[Dict[str, Any]],
        query: str = ""
    ) -> str:
        """
        Generic 3-state context compatibility gate:
        - COMPATIBLE: Current request is an analytical/anaphoric continuation of the previous result
          and introduces no explicit structured conflict.
        - INCOMPATIBLE: Current request introduces a materially different structured intent/scope
          (conflicting capability, new entity, conflicting dimension, conflicting temporal scope,
          or independent operation).
        - AMBIGUOUS: Structured information genuinely cannot determine whether to inherit or start fresh.
        """
        if not previous_plan:
            return "INCOMPATIBLE"

        curr = current_context.get("latest_execution_plan") if isinstance(current_context, dict) and isinstance(current_context.get("latest_execution_plan"), dict) else (current_context or {})
        prev = previous_plan.get("latest_execution_plan") if isinstance(previous_plan, dict) and isinstance(previous_plan.get("latest_execution_plan"), dict) else (previous_plan or {})

        has_prev_context = bool(
            prev.get("business_capabilities") or
            prev.get("capability") or
            prev.get("canonical_intent")
        )
        if not has_prev_context:
            return "INCOMPATIBLE"

        q_clean = (query or "").strip().lower()
        if any(marker in q_clean for marker in ("reset", "clear", "start over")):
            return "INCOMPATIBLE"

        # 1. Structured Capability Extraction
        prev_caps = [c.get("id") for c in prev.get("business_capabilities", []) if isinstance(c, dict) and c.get("id")]
        if not prev_caps and prev.get("capability"):
            prev_caps = [prev.get("capability")]
        prev_cap = prev_caps[0] if prev_caps else None

        curr_caps = [c.get("id") for c in curr.get("business_capabilities", []) if isinstance(c, dict) and c.get("id")]
        if not curr_caps and curr.get("capability"):
            curr_caps = [curr.get("capability")]
        curr_cap = curr_caps[0] if curr_caps else None

        # 2. Structured Operation & Intent
        curr_canon = curr.get("canonical_intent") or {}
        prev_canon = prev.get("canonical_intent") or {}

        curr_op = curr.get("operation") or curr_canon.get("operation")
        prev_op = prev.get("operation") or prev_canon.get("operation")

        # 3. Structured Entities Extraction
        curr_entities = list(curr.get("entities") or [])
        prev_entities = list(prev.get("entities") or [])

        if not curr_entities and query:
            try:
                from agent.entity_resolver import extract_entities_from_text
                curr_entities = extract_entities_from_text(query)
            except Exception:
                pass

        def _get_concrete_entity_set(ents):
            norm = set()
            for e in ents:
                if isinstance(e, dict):
                    v = str(e.get("value") or e.get("entity_name") or "").strip().lower()
                    r = str(e.get("role") or "filter").strip().lower()
                else:
                    v = str(e).strip().lower()
                    r = "filter"
                if v and v not in ("all", "none", "null", "undefined", "") and r != "dimension":
                    norm.add(v)
            return norm

        curr_entity_set = _get_concrete_entity_set(curr_entities)
        prev_entity_set = _get_concrete_entity_set(prev_entities)

        # Entity Conflict Check: If current turn introduces concrete entities not in previous context
        new_entities = curr_entity_set - prev_entity_set
        if new_entities:
            logger.info(f"[ContextCompatibility] INCOMPATIBLE — New concrete entity/scope introduced: {new_entities}")
            return "INCOMPATIBLE"

        # Capability Conflict Check: Explicitly different business capability
        if curr_cap and prev_cap and curr_cap != prev_cap:
            logger.info(f"[ContextCompatibility] INCOMPATIBLE — Capability conflict: current={curr_cap} vs previous={prev_cap}")
            return "INCOMPATIBLE"

        # Dimension Conflict Check: Explicitly differing grouping dimension
        curr_dim = curr.get("dimension") or curr_canon.get("dimension")
        prev_dim = prev.get("dimension") or prev_canon.get("dimension")
        if curr_dim and prev_dim:
            c_dim_clean = str(curr_dim).lower().replace("_", "").replace(" ", "").strip()
            p_dim_clean = str(prev_dim).lower().replace("_", "").replace(" ", "").strip()
            if c_dim_clean and p_dim_clean and c_dim_clean != p_dim_clean:
                logger.info(f"[ContextCompatibility] INCOMPATIBLE — Dimension conflict: current={curr_dim} vs previous={prev_dim}")
                return "INCOMPATIBLE"

        # Temporal Scope Conflict Check: Explicitly differing financial year or dates
        curr_fy = curr.get("financial_year") or (curr.get("context") or {}).get("financial_year")
        prev_fy = prev.get("financial_year") or (prev.get("context") or {}).get("financial_year")
        is_explicit_temporal = curr.get("is_explicit", False)
        if not is_explicit_temporal and isinstance(curr_canon.get("temporal"), dict):
            is_explicit_temporal = curr_canon.get("temporal", {}).get("is_explicit", False)
        if is_explicit_temporal and curr_fy and prev_fy and str(curr_fy).lower() != str(prev_fy).lower():
            logger.info(f"[ContextCompatibility] INCOMPATIBLE — Temporal conflict: current_fy={curr_fy} vs previous_fy={prev_fy}")
            return "INCOMPATIBLE"

        # Operation Conflict Check: Independent ranking/reporting operations
        if curr_op in ("ranking", "generate_report") and curr_op != prev_op:
            logger.info(f"[ContextCompatibility] INCOMPATIBLE — Operation conflict: current={curr_op} vs previous={prev_op}")
            return "INCOMPATIBLE"

        # Filter Conflict Check: Conflicting explicit filters
        curr_filters = curr.get("filters") or {}
        prev_filters = prev.get("filters") or {}
        if curr_filters and prev_filters:
            for fk, fv in curr_filters.items():
                if fk in prev_filters and prev_filters[fk] and str(fv).strip().lower() != str(prev_filters[fk]).strip().lower():
                    logger.info(f"[ContextCompatibility] INCOMPATIBLE — Filter conflict: key={fk} val={fv} vs previous={prev_filters[fk]}")
                    return "INCOMPATIBLE"

        # COMPATIBLE Check: Analytical continuation with no structured conflicts
        explanatory_triggers = ("why", "explain", "cause", "reason", "how come", "what led to", "trend")
        has_explanatory_intent = (
            curr_op == "analyze" or
            curr.get("presentation_mode") in ("EXPLANATION", "EXECUTIVE_EXPLANATION") or
            any(trig in q_clean for trig in explanatory_triggers)
        )
        if has_explanatory_intent:
            logger.info("[ContextCompatibility] COMPATIBLE — Analytical continuation with no structured conflicts.")
            return "COMPATIBLE"

        # AMBIGUOUS Check: Genuine ambiguity without clear continuation or clear independence
        if curr.get("ambiguity_detected") or (curr.get("missing_information") and not curr_op):
            logger.info("[ContextCompatibility] AMBIGUOUS — Structured intent is ambiguous.")
            return "AMBIGUOUS"

        # If not an explanatory follow-up and has fresh independent intent, treat as INCOMPATIBLE (fresh planning)
        logger.info("[ContextCompatibility] INCOMPATIBLE — Non-continuation query with independent intent.")
        return "INCOMPATIBLE"

    def is_analytical_followup(
        self,
        query: str,
        previous_plan: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Determines whether the current request is an analytical follow-up
        referring to the immediately previous execution context.
        """
        if not query or not previous_plan:
            return False

        has_context = bool(
            previous_plan.get("business_capabilities") or
            previous_plan.get("capability") or
            previous_plan.get("canonical_intent") or
            previous_plan.get("latest_execution_plan")
        )
        if not has_context:
            return False

        q_clean = query.strip().lower()
        if any(marker in q_clean for marker in ("reset", "clear", "start over")):
            return False

        explanatory_triggers = ("why", "explain", "cause", "reason", "how come", "what led to")
        return any(trig in q_clean for trig in explanatory_triggers)

    def is_standalone_why_without_context(self, query: str) -> bool:
        """
        Checks if a standalone query asks an abstract why/cause question without context.
        """
        if not query:
            return False
        q_clean = query.strip().lower()
        explanatory_triggers = ("why", "explain", "cause", "reason", "how come")
        return any(trig in q_clean for trig in explanatory_triggers) and any(ref in q_clean for ref in ("this", "that", "it", "trend", "negative", "down", "shortfall"))

    def has_sufficient_analytical_data(
        self,
        previous_tool_results: List[Dict[str, Any]],
        previous_plan: Dict[str, Any],
        query: str = ""
    ) -> bool:
        """
        Determines whether previous authoritative tool results already contain
        sufficient data to answer the analytical question directly without re-executing backend.
        Contract-driven:
        - Extracts rows from standardized authoritative result keys (rows, records, items, data).
        - Ignores non-result collections (entities, warnings, columns, filters, metadata).
        - Validates dimension alignment using plan_dim, plan_dim_name, plan_dim_id.
        - Only checks date/period/month when plan_dim itself is a temporal dimension.
        """
        if not previous_tool_results or not isinstance(previous_tool_results, list):
            return False

        # Extract authoritative rows from previous results generically
        RESULT_KEYS = ("rows", "records", "items", "data")
        IGNORE_KEYS = {"entities", "resolved_entities", "warnings", "columns", "filters", "metadata", "schema", "fields"}

        rows = []
        for tr in previous_tool_results:
            if not isinstance(tr, dict):
                continue
            inner = tr.get("result") if isinstance(tr.get("result"), dict) else tr
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else inner

            found_rows = False
            # 1. Prioritize standardized result keys in payload, inner, and tr
            for container in (payload, inner, tr):
                if not isinstance(container, dict):
                    continue
                for k in RESULT_KEYS:
                    val = container.get(k)
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        rows.extend(val)
                        found_rows = True
                        break
                if found_rows:
                    break

            # 2. If not found in standardized keys, safely check other list values while ignoring non-result collections
            if not found_rows:
                for container in (payload, inner, tr):
                    if not isinstance(container, dict):
                        continue
                    for k, val in container.items():
                        if str(k).lower() in IGNORE_KEYS:
                            continue
                        if isinstance(val, list) and val and isinstance(val[0], dict):
                            rows.extend(val)
                            found_rows = True
                            break
                    if found_rows:
                        break

            # 3. Direct list payload handling
            if not found_rows:
                if isinstance(tr.get("data"), list) and tr["data"] and isinstance(tr["data"][0], dict):
                    rows.extend(tr["data"])
                elif isinstance(tr, list) and tr and isinstance(tr[0], dict):
                    rows.extend(tr)

        if not rows:
            return False

        # Check if requested dimension in plan is represented in rows
        plan_dim = previous_plan.get("dimension")
        if not plan_dim and previous_plan.get("canonical_intent"):
            plan_dim = previous_plan["canonical_intent"].get("dimension")

        if plan_dim:
            plan_dim_str = str(plan_dim).lower().strip()
            plan_dim_clean = plan_dim_str.replace("_", "").replace(" ", "")
            first_row = rows[0] if isinstance(rows[0], dict) else {}
            row_keys_raw = [str(k).lower().strip() for k in first_row.keys()]
            row_keys_clean = [k.replace("_", "").replace(" ", "") for k in row_keys_raw]

            # Determine whether the requested dimension is temporal
            is_temporal_dimension = plan_dim_clean in ("month", "date", "period", "year", "quarter", "day")

            if is_temporal_dimension:
                # Temporal dimension: only treat date, period, month, year, quarter, day as dimension fields
                temporal_keys = {"month", "date", "period", "year", "quarter", "day", "monthorder", "targetmonth", "periodname"}
                has_dim = any(k in temporal_keys for k in row_keys_clean) or any(plan_dim_clean in k for k in row_keys_clean)
            else:
                # Non-temporal dimension (service_line, customer, employee, etc.):
                # Contract-driven: strictly match plan_dim, plan_dim_name, or plan_dim_id.
                # Never consider a row valid merely because it contains a temporal field.
                contract_keys = {
                    plan_dim_str,
                    f"{plan_dim_str}_name",
                    f"{plan_dim_str}_id",
                    plan_dim_clean,
                    f"{plan_dim_clean}name",
                    f"{plan_dim_clean}id"
                }
                has_dim = any(k in contract_keys for k in row_keys_raw) or any(k in contract_keys for k in row_keys_clean)

            if not has_dim:
                return False

        return True

    def build_inherited_analytical_plan(
        self,
        previous_plan: Dict[str, Any],
        query: str,
        user_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Inherits previous authoritative execution context (capability, metric, dimension,
        filters, entities, temporal scope) and changes ONLY the analytical intent:
        operation = analyze, presentation_mode = EXPLANATION, expected_result_type = explanation.
        Strictly preserves previous canonical context:
        - Never mutates metric based on query keywords.
        - Never defaults capability, metric, dimension, or temporal values.
        """
        import copy
        from agent.intent_normalizer import CanonicalIntent, TemporalSpec

        # Unwrap latest_execution_plan if nested
        actual_plan = previous_plan.get("latest_execution_plan") if isinstance(previous_plan.get("latest_execution_plan"), dict) else previous_plan

        caps = actual_plan.get("business_capabilities") or []
        prev_cap = caps[0] if caps and isinstance(caps[0], dict) else {}
        cap_id = prev_cap.get("id") or actual_plan.get("capability")
        if not cap_id and actual_plan.get("canonical_intent"):
            cap_id = actual_plan["canonical_intent"].get("capability")

        # Metric inheritance: purely from previous plan / canonical intent (NO keyword overriding)
        metric = prev_cap.get("metric") or actual_plan.get("metric")
        if not metric and actual_plan.get("canonical_intent"):
            metric = actual_plan["canonical_intent"].get("metric")

        # Dimension inheritance: purely from previous plan / canonical intent
        dimension = prev_cap.get("dimension") or actual_plan.get("dimension")
        if not dimension and actual_plan.get("canonical_intent"):
            dimension = actual_plan["canonical_intent"].get("dimension")

        # Filters and entities inheritance
        prev_ctx = prev_cap.get("context") or actual_plan.get("context") or {}
        inherited_ctx = dict(prev_ctx)
        inherited_filters = dict(actual_plan.get("filters") or {})
        resolved_entities = copy.deepcopy(actual_plan.get("resolved_entities") or [])
        entities = copy.deepcopy(actual_plan.get("entities") or [])

        temporal_scope = actual_plan.get("temporal_scope") or prev_cap.get("temporal_scope")
        financial_year = actual_plan.get("financial_year") or inherited_ctx.get("financial_year")
        start_date = actual_plan.get("start_date") or inherited_ctx.get("start_date")
        end_date = actual_plan.get("end_date") or inherited_ctx.get("end_date")

        inherited_ctx["operation"] = "analyze"
        inherited_ctx["metric"] = metric
        inherited_ctx["dimension"] = dimension
        if financial_year:
            inherited_ctx["financial_year"] = financial_year
        if start_date:
            inherited_ctx["start_date"] = start_date
        if end_date:
            inherited_ctx["end_date"] = end_date

        inherited_cap = {
            "id": cap_id,
            "scope": prev_cap.get("scope"),
            "intent": "analyze",
            "operation": "analyze",
            "metric": metric,
            "dimension": dimension,
            "context": inherited_ctx
        }

        execution_plan = {
            "business_goal": f"Explain {metric or cap_id} analysis based on authoritative previous results",
            "confidence_score": 1.0,
            "reasoning_summary": "Inherited authoritative execution context for conversational analytical follow-up",
            "ambiguity_detected": False,
            "missing_information": [],
            "scope": actual_plan.get("scope"),
            "operation": "analyze",
            "metric": metric,
            "dimension": dimension,
            "expected_result_type": "explanation",
            "presentation_mode": "EXPLANATION",
            "business_capabilities": [inherited_cap],
            "entities": entities,
            "resolved_entities": resolved_entities,
            "filters": inherited_filters,
            "context": inherited_ctx,
            "temporal_scope": temporal_scope,
            "financial_year": financial_year,
            "start_date": start_date,
            "end_date": end_date,
            "ranking": None,
            "limit": None,
            "sort_order": None,
            "original_question": query
        }

        temp_spec = TemporalSpec(
            type=temporal_scope or "unspecified",
            start_date=start_date,
            end_date=end_date,
            financial_year=financial_year,
            is_explicit=bool(start_date or financial_year)
        )
        canonical = CanonicalIntent(
            raw_query=query,
            capability=cap_id,
            operation="analyze",
            metric=metric,
            dimension=dimension,
            filters=inherited_filters,
            temporal=temp_spec,
            ranking=None,
            comparison=None,
            expected_result_type="explanation",
            presentation_mode="EXPLANATION",
            confidence=1.0,
            missing_information=[]
        )
        execution_plan["canonical_intent"] = canonical.model_dump()
        return execution_plan

    def classify_request_type(
        self,
        query: str,
        previous_memory: Dict[str, Any]
    ) -> str:
        """
        Classifies request into: NEW_REQUEST, FOLLOW_UP, CLARIFICATION_RESPONSE, or CONTEXT_RESET.
        """
        q_clean = (query or "").strip().lower()
        if not previous_memory:
            return "NEW_REQUEST"

        # Check explicit Context Reset markers
        reset_markers = ["now show", "instead", "switch to", "change to", "start over", "reset", "clear"]
        if any(marker in q_clean for marker in reset_markers):
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=CONTEXT_RESET query='{query}'")
            return "CONTEXT_RESET"

        # Analytical follow-up check
        if self.is_analytical_followup(query, previous_memory):
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=FOLLOW_UP query='{query}' (analytical follow-up)")
            return "FOLLOW_UP"

        if not previous_memory.get("active_filters"):
            return "NEW_REQUEST"

        prev_filters = previous_memory.get("active_filters", {})

        # Generic continuation/follow-up triggers
        generic_followup_phrases = [
            "show details", "details", "more details", "drilldown", "breakdown",
            "the first one", "first one", "second one", "third one",
            "option 1", "option 2", "option 3", "option 4", "option 5",
            "yes", "yeah", "sure", "ok", "okay"
        ]
        if any(fp in q_clean for fp in generic_followup_phrases) or q_clean in ["1", "2", "3", "4", "5"]:
            logger.info(f"[REQUEST_CLASSIFICATION] request_type=FOLLOW_UP query='{query}'")
            return "FOLLOW_UP"

        # If any active filter from previous memory is referenced in the query, treat as follow-up
        for filter_val in prev_filters.values():
            if filter_val and str(filter_val).lower() in q_clean and len(str(filter_val)) > 2:
                logger.info(f"[REQUEST_CLASSIFICATION] request_type=FOLLOW_UP query='{query}' (filter refinement)")
                return "FOLLOW_UP"

        return "NEW_REQUEST"

    def build_dynamic_followup_options(
        self,
        entity_type: str,
        entity_id: Any,
        entity_name: str,
        capability: str,
        current_context: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Dynamically constructs structured follow-up choices derived from master hierarchy data.
        Zero hardcoded strings — queries DB master relationships for child entities.
        """
        if not entity_name:
            return []

        options = []
        e_type = (entity_type or "").lower()

        if e_type in ["service_line", "serviceline"]:
            from agent.entity_resolver import get_child_departments_for_serviceline
            children = get_child_departments_for_serviceline(entity_id or entity_name)
            ctx_metric = current_context.get("metric")
            ctx_fy = current_context.get("financial_year")
            for child in children:
                c_name = child.get("name")
                c_id = child.get("id")
                if c_name:
                    opt_ctx = {
                        "service_line": entity_name,
                        "service_line_id": entity_id,
                    }
                    if ctx_metric:
                        opt_ctx["metric"] = ctx_metric
                    if ctx_fy:
                        opt_ctx["financial_year"] = ctx_fy
                    options.append({
                        "label": c_name,
                        "entity_type": "department",
                        "entity_id": c_id,
                        "parent_entity_type": "service_line",
                        "parent_entity_id": entity_id,
                        "capability": capability,
                        "metric": ctx_metric,
                        "inherited_context": opt_ctx
                    })

        return options

    def resolve_followup_input(
        self,
        query: str,
        active_options: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves natural user inputs like 'the first one', '1', 'yes', or an option label against active follow-up options.
        Returns selected option dict, or special clarification dict if input is ambiguous (e.g. 'yes' with multiple options).
        """
        if not query or not active_options:
            return None

        q_clean = query.strip().lower()
        import re

        # Handle 'yes' when multiple options exist
        if q_clean in ["yes", "yeah", "sure", "ok", "okay", "show"]:
            if len(active_options) > 1:
                labels_str = "\n".join([f"{i+1}) {opt['label']}" for i, opt in enumerate(active_options)])
                return {
                    "type": "clarification_needed",
                    "content": f"Which option would you like to see?\n\n{labels_str}",
                    "is_clarification": True,
                    "options": active_options
                }
            elif len(active_options) == 1:
                return active_options[0]

        # Ordinal / index selection ('the first one', '1', 'option 1')
        idx_match = re.search(r'\b(?:option\s*|the\s*)?([1-9])(?:st|nd|rd|th)?(?:\s*one)?\b', q_clean)
        if idx_match:
            idx = int(idx_match.group(1)) - 1
            if 0 <= idx < len(active_options):
                return active_options[idx]

        # Label matching (strip leading action verbs like 'show', 'view', 'get', 'display')
        q_sub = re.sub(r'^(show|view|get|display)\s+', '', q_clean).strip()
        for opt in active_options:
            lbl = str(opt.get("label", "")).lower()
            if lbl and (lbl in q_clean or q_clean in lbl or lbl in q_sub or q_sub in lbl):
                return opt

        return None


conversation_manager = ConversationManager()
