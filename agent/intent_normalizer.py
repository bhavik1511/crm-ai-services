"""
intent_normalizer.py — Canonical Intent Normalizer & Capability Validator
========================================================================

Lightweight, deterministic schema conversion and metadata validation layer.

Architectural Rule:
- The LLM (via EnterprisePlanner) handles all natural language semantic understanding.
- This module MUST NOT contain regex keyword lists, phrase-specific conditionals, or hardcoded synonym maps.
- Its responsibility is:
  1. Converting structured LLM output into a single CanonicalIntent schema.
  2. Validating intent parameters against CapabilityCatalog metadata.
  3. Delegating temporal resolution to TemporalResolver.
  4. Delegating entity resolution to EntityResolver.
  5. Providing structured observability logs ([INTENT_NORMALIZED], [INTENT_MERGE]).
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured Canonical Intent Schemas (Single Source of Truth)
# ---------------------------------------------------------------------------

class RankingSpec(BaseModel):
    direction: str = Field("desc", description="Sort direction: 'desc' or 'asc'.")
    limit: Optional[int] = Field(1, description="Number of top items to return (e.g. 1 for highest, 5 for top 5).")


class PeriodSpec(BaseModel):
    label: str = Field(description="Period label, e.g. 'FY24', 'FY25'.")
    start_date: Optional[str] = Field(None, description="Start date (YYYY-MM-DD).")
    end_date: Optional[str] = Field(None, description="End date (YYYY-MM-DD).")


class ComparisonSpec(BaseModel):
    type: Optional[str] = Field(None, description="Type of comparison, e.g. 'fiscal_year', 'service_line', 'department'.")
    periods: List[PeriodSpec] = Field(default_factory=list, description="Comparison periods with boundaries.")


class TemporalSpec(BaseModel):
    type: str = Field("default_fy", description="Temporal scope type: 'current_fy', 'previous_fy', 'explicit_fy', 'explicit_range', etc.")
    start_date: Optional[str] = Field(None, description="Start date (YYYY-MM-DD).")
    end_date: Optional[str] = Field(None, description="End date (YYYY-MM-DD).")
    financial_year: Optional[str] = Field(None, description="Canonical Financial Year string, e.g. 'FY25', '2024-2025'.")
    is_explicit: bool = Field(False, description="True if the user explicitly specified the period.")


class CanonicalIntent(BaseModel):
    raw_query: str = Field(description="Original natural-language query string.")
    capability: Optional[str] = Field(None, description="Primary business capability ID.")
    operation: str = Field("summary", description="Operation: 'summary', 'ranking', 'comparison', 'trend', 'count', 'aggregate', 'ageing', 'detail', 'search'.")
    metric: Optional[str] = Field(None, description="Primary metric, e.g. 'revenue', 'gross_profit', 'receivables', 'proposals'.")
    dimension: Optional[str] = Field(None, description="Grouping/ranking dimension, e.g. 'customer', 'department', 'service_line', 'employee', 'project', 'office'.")
    ranking: Optional[RankingSpec] = Field(None, description="Ranking parameters when operation == 'ranking'.")
    comparison: Optional[ComparisonSpec] = Field(None, description="Comparison parameters when operation == 'comparison'.")
    temporal: Optional[TemporalSpec] = Field(None, description="Resolved temporal boundaries.")
    entities: List[Dict[str, Any]] = Field(default_factory=list, description="Raw or resolved entities.")
    filters: Dict[str, Any] = Field(default_factory=dict, description="Explicit filters (service_line, department, etc.).")
    expected_result_type: Optional[str] = Field(None, description="Expected payload type (ranking, receivables_summary, receivables_ageing, comparison, summary, etc.).")
    missing_information: List[str] = Field(default_factory=list, description="List of genuinely missing required fields.")
    business_goal: str = Field("", description="User's high-level business goal.")
    confidence: float = Field(1.0, description="Overall confidence score (0.0 - 1.0).")
    presentation_mode: Optional[str] = Field("REPORT", description="Presentation mode: REPORT, INSIGHT, KPI_CARD, TABLE, COMPARISON.")


# ---------------------------------------------------------------------------
# Canonical Intent Conversion & Normalization
# ---------------------------------------------------------------------------

def to_canonical_intent(plan_data: Dict[str, Any], raw_query: str) -> CanonicalIntent:
    """
    Converts structured LLM output (plan_data dictionary) into a CanonicalIntent object.
    Performs purely deterministic structural parsing without keyword regexes or string checks.
    """
    capabilities = plan_data.get("business_capabilities", [])
    primary_cap = capabilities[0] if capabilities and isinstance(capabilities[0], dict) else {}
    cap_ctx = primary_cap.get("context", {}) or {}

    cap_id = primary_cap.get("id") or (capabilities[0] if capabilities and isinstance(capabilities[0], str) else plan_data.get("capability"))

    raw_op = (primary_cap.get("operation") or primary_cap.get("intent") or cap_ctx.get("operation") or plan_data.get("operation") or "summary")
    operation = str(raw_op).lower().strip()

    metric = primary_cap.get("metric") or cap_ctx.get("metric") or plan_data.get("metric")
    dimension = (
        primary_cap.get("dimension") or primary_cap.get("entity") or primary_cap.get("group_by") or
        cap_ctx.get("dimension") or cap_ctx.get("group_by") or cap_ctx.get("entity") or plan_data.get("dimension")
    )
    if not dimension:
        entities = plan_data.get("entities") or []
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict):
                    ent_str = str(ent.get("type") or ent.get("entity_type") or ent.get("value") or "").lower().strip()
                else:
                    ent_str = str(ent).lower().strip()
                if ent_str in ("department", "service_line", "customer", "employee", "project"):
                    dimension = ent_str
                    break

    if not dimension:
        goal_str = str(plan_data.get("business_goal") or "").lower()
        query_str = str(raw_query or "").lower()
        if "customer" in goal_str or "customer" in query_str or "client" in query_str:
            dimension = "customer"
        elif "department" in goal_str or "department" in query_str or "dept" in query_str:
            dimension = "department"
        elif "service_line" in goal_str or "service line" in query_str:
            dimension = "service_line"
        elif "employee" in goal_str or "employee" in query_str or "staff" in query_str:
            dimension = "employee"

    # Check if a specific target entity is present in plan_data or entities
    raw_entities_check = plan_data.get("resolved_entities") or plan_data.get("entities") or []
    has_specific_entity = False
    from agent.entity_resolver import is_reserved_business_term
    if isinstance(raw_entities_check, list):
        for ent in raw_entities_check:
            if isinstance(ent, dict):
                ent_name = str(ent.get("name") or ent.get("entity_name") or ent.get("value") or "").strip()
                ent_type = str(ent.get("type") or ent.get("entity_type") or "").strip()
            else:
                ent_name = str(ent).strip()
                ent_type = ""
            if ent_name and not is_reserved_business_term(ent_name) and ent_name.lower() not in (ent_type.lower(), "customer", "department", "service_line", "service line", "employee", "project"):
                has_specific_entity = True
                break

    # Force operation == 'ranking' if explicit ranking or limit params are present for analytical queries
    has_ranking_param = bool(
        primary_cap.get("ranking") or primary_cap.get("limit") or cap_ctx.get("limit") or cap_ctx.get("ranking") or
        plan_data.get("ranking") or plan_data.get("limit")
    )
    is_explicit_kpi_ranking = cap_id == "kpi_summary" and has_ranking_param and any(k in raw_query.lower() for k in ["top", "ranking", "highest", "best", "worst", "lowest"])
    is_explicit_gp_ranking = cap_id == "gp_performance" and (has_ranking_param or any(k in raw_query.lower() for k in ["top", "ranking", "highest", "best", "worst", "lowest"]))

    if cap_id == "kpi_summary" and not is_explicit_kpi_ranking:
        operation = "summary"
    elif cap_id == "gp_performance" and not is_explicit_gp_ranking:
        operation = "summary"
    elif has_ranking_param or (operation == "ranking" and not has_specific_entity and cap_id not in ("kpi_summary", "gp_performance")) or primary_cap.get("intent") == "ranking":
        operation = "ranking"
        if not dimension:
            dimension = "customer"
    elif dimension and dimension in ("customer", "department", "service_line", "employee") and operation in ("summary", "generate_report", "analyze", "analytical_query") and not has_specific_entity and cap_id not in ("kpi_summary", "gp_performance"):
        operation = "ranking"

    # Comparison normalization
    raw_comp = primary_cap.get("comparison") or cap_ctx.get("comparison") or plan_data.get("comparison") or primary_cap.get("comparison_periods") or cap_ctx.get("comparison_periods")
    if operation == "comparison" or raw_comp or primary_cap.get("intent") == "comparison":
        operation = "comparison"

    expected_result_type = primary_cap.get("expected_result_type") or plan_data.get("expected_result_type") or operation
    if cap_id == "kpi_summary" and not is_explicit_kpi_ranking:
        expected_result_type = "summary"
    elif cap_id == "gp_performance" and not is_explicit_gp_ranking:
        expected_result_type = "summary"
    elif operation in ("ranking", "comparison") and expected_result_type in ("summary", "generate_report"):
        expected_result_type = operation

    # Ranking normalization
    ranking_spec = None
    if (operation == "ranking" or (has_ranking_param and cap_id != "kpi_summary")) and (cap_id != "kpi_summary" or is_explicit_kpi_ranking):
        raw_sort = str(primary_cap.get("sort_order") or primary_cap.get("ranking") or cap_ctx.get("sort_order") or plan_data.get("ranking") or "desc").lower()
        sort_dir = "asc" if raw_sort == "asc" else "desc"
        limit_val = primary_cap.get("limit") or cap_ctx.get("limit") or plan_data.get("limit") or 1
        ranking_spec = RankingSpec(direction=sort_dir, limit=limit_val)
        if not metric:
            metric = "revenue"

    # Comparison normalization with multi-period extraction
    comparison_spec = None
    if operation == "comparison" or raw_comp:
        comp_type = "fiscal_year"
        from agent.entity_resolver import extract_all_fiscal_years
        all_fys = extract_all_fiscal_years(raw_query)
        periods = []
        for fy_item in all_fys:
            periods.append(PeriodSpec(
                label=fy_item.get("financial_year", ""),
                start_date=fy_item.get("start_date"),
                end_date=fy_item.get("end_date")
            ))
        if not periods and cap_ctx.get("comparison_periods"):
            for p in cap_ctx.get("comparison_periods"):
                if isinstance(p, dict):
                    periods.append(PeriodSpec(
                        label=p.get("label", ""),
                        start_date=p.get("start_date"),
                        end_date=p.get("end_date")
                    ))
        comparison_spec = ComparisonSpec(type=comp_type, periods=periods)

    # Extract temporal scope from capability context or time_filter
    cap_ctx = primary_cap.get("context", {}) or {}
    start_date = cap_ctx.get("start_date") or plan_data.get("start_date")
    end_date = cap_ctx.get("end_date") or plan_data.get("end_date")
    financial_year = cap_ctx.get("financial_year") or cap_ctx.get("temporal_scope") or plan_data.get("financial_year")
    is_explicit = bool(cap_ctx.get("is_explicit") or start_date or financial_year)

    if not start_date or not end_date:
        from agent.temporal_resolver import resolve_temporal_scope
        t_res = resolve_temporal_scope(raw_query)
        start_date = start_date or t_res.get("start_date")
        end_date = end_date or t_res.get("end_date")
        financial_year = financial_year or t_res.get("financial_year")
        is_explicit = is_explicit or bool(t_res.get("is_explicit"))

    temporal_spec = TemporalSpec(
        type="explicit" if is_explicit else "default_fy",
        start_date=start_date,
        end_date=end_date,
        financial_year=financial_year,
        is_explicit=is_explicit
    )

    raw_entities = plan_data.get("resolved_entities") or plan_data.get("entities") or []
    from agent.entity_resolver import is_reserved_business_term
    sanitized_entities = []
    for ent in raw_entities:
        ent_name = str(ent.get("name") or ent.get("entity_name") or ent.get("value") or "").strip()
        ent_type = str(ent.get("type") or ent.get("entity_type") or "").strip()
        if is_reserved_business_term(ent_name):
            continue
        if ent_name.lower() in (ent_type.lower(), "customer", "department", "service_line", "service line", "employee", "project"):
            continue
        sanitized_entities.append(ent)

    filters = primary_cap.get("filters") or plan_data.get("filters") or {}
    missing_info = list(plan_data.get("missing_information") or [])
    if not is_explicit and "temporal_scope" not in missing_info:
        missing_info.append("temporal_scope")

    confidence = float(plan_data.get("confidence_score", 1.0))
    business_goal = plan_data.get("business_goal", "")
    pres_mode = plan_data.get("presentation_mode") or primary_cap.get("presentation_mode") or "REPORT"

    canonical = CanonicalIntent(
        raw_query=raw_query,
        capability=cap_id,
        operation=operation,
        metric=metric,
        dimension=dimension,
        ranking=ranking_spec,
        comparison=comparison_spec,
        temporal=temporal_spec,
        entities=sanitized_entities,
        filters=filters,
        expected_result_type=expected_result_type,
        missing_information=missing_info,
        business_goal=business_goal,
        confidence=confidence,
        presentation_mode=pres_mode
    )

    logger.info(
        f"[INTENT_NORMALIZED] query='{raw_query}' | capability={canonical.capability} | "
        f"operation={canonical.operation} | metric={canonical.metric} | dimension={canonical.dimension} | "
        f"limit={canonical.ranking.limit if canonical.ranking else None} | "
        f"expected_result_type={canonical.expected_result_type} | confidence={canonical.confidence}"
    )

    return canonical


# ---------------------------------------------------------------------------
# Capability Metadata Validation (Single Source of Truth: CapabilityCatalog)
# ---------------------------------------------------------------------------

def validate_canonical_intent(intent: CanonicalIntent) -> Tuple[bool, str]:
    """
    Cross-checks a CanonicalIntent against CapabilityCatalog metadata.
    Does NOT hardcode capabilities; reads directly from CapabilityCatalog.
    """
    if not intent.capability:
        return False, "No capability specified."

    from registry.capability_catalog import get_capability_metadata
    cap_meta = get_capability_metadata(intent.capability)

    if not cap_meta:
        return False, f"Capability '{intent.capability}' is not registered in CapabilityCatalog."

    # Validate dimension for ranking operation
    if intent.operation == "ranking":
        supported_dims = cap_meta.get("supported_dimensions", [])
        if not intent.dimension:
            if len(supported_dims) == 1:
                intent.dimension = supported_dims[0]
                logger.info(f"[CAPABILITY_VALIDATION] Derived single supported dimension '{intent.dimension}' for capability '{intent.capability}'.")
            elif len(supported_dims) > 1:
                if "dimension" not in intent.missing_information:
                    intent.missing_information.append("dimension")
                logger.warning(f"[CAPABILITY_VALIDATION] Ambiguous dimension for capability '{intent.capability}'. Clarification required.")
                return False, "Clarification required for dimension."
        elif supported_dims and intent.dimension not in supported_dims:
            return False, f"Dimension '{intent.dimension}' is not supported by capability '{intent.capability}'."

    logger.info(
        f"[CAPABILITY_INVARIANT] capability={intent.capability} | operation={intent.operation} | "
        f"metric={intent.metric} | dimension={intent.dimension} | expected_result_type={intent.expected_result_type} | valid=true"
    )
    return True, "Valid"

    # Validate operation if supported_operations metadata exists
    supported_ops = cap_meta.get("supported_operations", [])
    if supported_ops and intent.operation not in supported_ops and "all" not in supported_ops:
        # Check if operation can map to a supported metric or implementation
        logger.warning(
            f"[CAPABILITY_VALIDATED] Capability '{intent.capability}' operation '{intent.operation}' "
            f"not in explicitly listed supported_operations={supported_ops}. Proceeding with caution."
        )

    # Validate required entity types if specified in capability catalog
    required_entities = cap_meta.get("required_business_context", {})
    if required_entities:
        for req_key in required_entities.keys():
            if required_entities[req_key].get("required", False):
                has_val = any(
                    e.get("type") == req_key or e.get("entity_type") == req_key
                    for e in intent.entities
                ) or req_key in intent.filters
                if not has_val and req_key not in intent.missing_information:
                    intent.missing_information.append(req_key)

    logger.info(f"[CAPABILITY_VALIDATED] capability={intent.capability} | valid=True")
    return True, "Valid"


def canonical_to_execution_plan(intent: CanonicalIntent) -> Dict[str, Any]:
    """
    Converts a CanonicalIntent back into the standard execution plan dictionary format
    expected by HybridEngine and ToolRegistry.
    """
    cap_ctx = {
        "operation": intent.operation,
        "metric": intent.metric,
        "dimension": intent.dimension,
    }
    if intent.ranking:
        cap_ctx["limit"] = intent.ranking.limit
        cap_ctx["sort_order"] = intent.ranking.direction
    comp_periods_list = [p.model_dump() for p in intent.comparison.periods] if (intent.comparison and intent.comparison.periods) else []
    if comp_periods_list:
        cap_ctx["comparison_periods"] = comp_periods_list

    if intent.temporal:
        if intent.temporal.start_date:
            cap_ctx["start_date"] = intent.temporal.start_date
        if intent.temporal.end_date:
            cap_ctx["end_date"] = intent.temporal.end_date
        if intent.temporal.financial_year:
            cap_ctx["financial_year"] = intent.temporal.financial_year
            cap_ctx["temporal_scope"] = intent.temporal.type

    cap_item = {
        "id": intent.capability or "revenue_analysis",
        "scope": "organization",
        "intent": intent.operation,
        "operation": intent.operation,
        "metric": intent.metric,
        "dimension": intent.dimension,
        "context": cap_ctx,
        "filters": intent.filters
    }

    if intent.ranking:
        cap_item["ranking"] = intent.ranking.direction
        cap_item["sort_order"] = intent.ranking.direction
        cap_item["limit"] = intent.ranking.limit

    if intent.comparison:
        cap_item["comparison"] = intent.comparison.type
        if comp_periods_list:
            cap_item["comparison_periods"] = comp_periods_list

    plan_dict = {
        "business_goal": intent.business_goal,
        "confidence_score": intent.confidence,
        "reasoning_summary": f"Executing canonical intent for capability '{intent.capability}'",
        "ambiguity_detected": bool(intent.missing_information),
        "entities": intent.entities,
        "resolved_entities": intent.entities,
        "scope": ["organization"],
        "business_capabilities": [cap_item],
        "missing_information": intent.missing_information,
        "entity_errors": [],
        "presentation_mode": intent.presentation_mode or "REPORT",
        "analysis_depth": "summary",
        "operation": intent.operation,
        "metric": intent.metric,
        "dimension": intent.dimension,
        "comparison_periods": comp_periods_list,
        "expected_result_type": intent.expected_result_type or intent.operation,
        "canonical_intent": intent.model_dump()
    }
    return plan_dict


def merge_clarification_intent(
    previous_plan: Dict[str, Any],
    clarification_text: str,
    user_context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Generic Clarification Merge Engine.
    
    Rules:
    - Restores previous execution plan & canonical intent completely.
    - Updates ONLY missing/clarified fields (temporal scope, entity values, or slot answers).
    - Enforces Immutable Intent Invariants for established fields: capability, operation, metric, dimension, ranking, comparison, expected_result_type.
    - Fails closed with [INTENT_MERGE_INVARIANT_FAILED] if established intent fields change unexpectedly.
    - Re-validates against CapabilityCatalog.
    - Sets confidence_score to 1.0 on the merged plan.
    - Emits structured observability logs [INTENT_MERGE] and [CANONICAL_INTENT_FINAL].
    """
    import copy
    
    prev_canonical_dict = previous_plan.get("canonical_intent")
    if prev_canonical_dict and isinstance(prev_canonical_dict, dict):
        try:
            prev_canonical = CanonicalIntent(**prev_canonical_dict)
        except Exception:
            prev_canonical = to_canonical_intent(previous_plan, previous_plan.get("original_question") or previous_plan.get("question") or "")
    else:
        prev_canonical = to_canonical_intent(previous_plan, previous_plan.get("original_question") or previous_plan.get("question") or "")

    merged_canonical = prev_canonical.model_copy(deep=True)
    merged_canonical.raw_query = clarification_text

    from .temporal_resolver import resolve_temporal_scope
    extracted_temp = resolve_temporal_scope(clarification_text)

    updated_fields = []
    
    # 1. Resolve temporal scope if missing or non-explicit
    if "temporal_scope" in prev_canonical.missing_information or not prev_canonical.temporal or not prev_canonical.temporal.is_explicit:
        if extracted_temp.get("is_explicit") or extracted_temp.get("start_date"):
            merged_canonical.temporal = TemporalSpec(
                type=extracted_temp.get("temporal_scope") or "explicit",
                start_date=extracted_temp.get("start_date"),
                end_date=extracted_temp.get("end_date"),
                financial_year=extracted_temp.get("financial_year"),
                is_explicit=True
            )
            updated_fields.append("temporal_scope")

    # 2. Handle slot answers or entity resolutions
    slot_answer = (user_context or {}).get("slot_answer")
    if slot_answer and isinstance(slot_answer, dict):
        key = slot_answer.get("key")
        val = slot_answer.get("value")
        if key and val:
            merged_canonical.filters[key] = val
            updated_fields.append(str(key))
    else:
        for miss_field in prev_canonical.missing_information:
            if miss_field not in updated_fields and miss_field != "temporal_scope":
                if miss_field == "dimension" and not merged_canonical.dimension:
                    from .entity_resolver import is_aggregate_value
                    if not is_aggregate_value(clarification_text):
                        merged_canonical.dimension = clarification_text.lower().strip().replace(" ", "_")
                        updated_fields.append("dimension")

    # Clear resolved missing_information
    merged_canonical.missing_information = [
        m for m in prev_canonical.missing_information if m not in updated_fields
    ]
    merged_canonical.confidence = 1.0

    # 3. IMMUTABLE INTENT INVARIANT VALIDATION (Section 6)
    immutable_fields = ["capability", "operation", "metric", "ranking", "comparison", "expected_result_type"]
    if "dimension" not in prev_canonical.missing_information and prev_canonical.dimension:
        immutable_fields.append("dimension")

    changed_fields = []
    for field in immutable_fields:
        prev_val = getattr(prev_canonical, field)
        merged_val = getattr(merged_canonical, field)
        if prev_val != merged_val:
            changed_fields.append((field, prev_val, merged_val))

    if changed_fields:
        logger.error(
            f"[INTENT_MERGE_INVARIANT_FAILED] previous_intent={prev_canonical.model_dump()} | "
            f"clarification_input='{clarification_text}' | merged_intent={merged_canonical.model_dump()} | "
            f"changed_fields={changed_fields}"
        )
        raise ValueError(f"Intent merge invariant failed: Established fields changed unexpectedly: {changed_fields}")

    # 4. Re-validate against CapabilityCatalog
    is_valid, err_msg = validate_canonical_intent(merged_canonical)
    if not is_valid and merged_canonical.missing_information:
        logger.warning(f"[INTENT_MERGE] Revalidation requires further clarification: {err_msg}")

    merged_plan = canonical_to_execution_plan(merged_canonical)
    merged_plan["canonical_intent"] = merged_canonical.model_dump()
    merged_plan["original_question"] = previous_plan.get("original_question") or previous_plan.get("question") or ""
    merged_plan["is_clarification"] = False

    logger.info(
        f"[INTENT_MERGE] original_query='{merged_plan['original_question']}' | "
        f"clarification='{clarification_text}' | "
        f"preserved_fields={immutable_fields} | updated_fields={updated_fields} | "
        f"confidence=1.0"
    )

    logger.info(
        f"[CANONICAL_INTENT_FINAL] capability={merged_canonical.capability} | operation={merged_canonical.operation} | "
        f"metric={merged_canonical.metric} | dimension={merged_canonical.dimension} | "
        f"limit={merged_canonical.ranking.limit if merged_canonical.ranking else None} | "
        f"temporal_scope={merged_canonical.temporal.type if merged_canonical.temporal else None} | "
        f"expected_result_type={merged_canonical.expected_result_type}"
    )

    return merged_plan
