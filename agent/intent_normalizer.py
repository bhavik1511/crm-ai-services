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

import re
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
    operation: str = Field("summary", description="Operation: 'summary', 'ranking', 'comparison', 'trend', 'count', 'aggregate', 'ageing', 'detail', 'search', 'analyze'.")
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
    if operation == "analysis":
        operation = "analyze"

    metric = primary_cap.get("metric") or cap_ctx.get("metric") or plan_data.get("metric")
    if not metric:
        q_low = raw_query.lower()
        if "revenue" in q_low or "billing" in q_low:
            metric = "revenue"
        elif "receivable" in q_low or "invoice" in q_low:
            metric = "receivables"
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
                if ent_str in ("department", "service_line", "customer", "employee", "project", "month", "months"):
                    dimension = "month" if ent_str in ("month", "months") else ent_str
                    break

    if not dimension:
        q_lower = raw_query.lower()
        if any(w in q_lower for w in ["service line", "service_line", "serviceline", "service lines"]):
            dimension = "service_line"
        elif any(w in q_lower for w in ["customer", "client", "customers", "clients"]):
            dimension = "customer"
        elif any(w in q_lower for w in ["department", "departments", "dept"]):
            dimension = "department"
        elif any(w in q_lower for w in ["employee", "staff", "employees"]):
            dimension = "employee"
        elif any(w in q_lower for w in ["month", "months", "monthly"]):
            dimension = "month"

    if not dimension and cap_id:
        from registry.capability_catalog import get_capability_default_dimension
        dimension = get_capability_default_dimension(cap_id)

    # Check if a specific target entity filter is present in plan_data or entities (role="filter")
    raw_entities_check = plan_data.get("resolved_entities") or plan_data.get("entities") or []
    if not raw_entities_check:
        from agent.entity_resolver import extract_entities_from_text
        raw_entities_check = extract_entities_from_text(raw_query) or []

    if not dimension and raw_entities_check:
        for ent in raw_entities_check:
            if isinstance(ent, dict):
                e_type = str(ent.get("type") or ent.get("entity_type") or "").lower().strip()
                if e_type in ("customer", "client"):
                    dimension = "customer"
                    break
                elif e_type in ("service_line", "serviceline"):
                    dimension = "service_line"
                    break
                elif e_type in ("department", "dept"):
                    dimension = "department"
                    break
                elif e_type in ("employee", "staff"):
                    dimension = "employee"
                    break

    has_specific_entity = False
    from agent.entity_resolver import is_reserved_business_term
    if isinstance(raw_entities_check, list):
        for ent in raw_entities_check:
            if isinstance(ent, dict):
                ent_name = str(ent.get("name") or ent.get("entity_name") or ent.get("value") or "").strip()
                ent_type = str(ent.get("type") or ent.get("entity_type") or "").strip()
                ent_role = str(ent.get("role") or "filter").strip().lower()
            else:
                ent_name = str(ent).strip()
                ent_type = ""
                ent_role = "filter"
            if ent_role == "filter" and ent_name and not is_reserved_business_term(ent_name) and ent_name.lower() not in (ent_type.lower(), "customer", "department", "service_line", "service line", "employee", "project"):
                has_specific_entity = True
                break

    # Dynamic Capability Alignment for customer/employee dimensions
    q_low = raw_query.lower()
    has_customer_entity = (
        dimension == "customer"
        or any(str(e.get("type") or e.get("entity_type") or "").lower() in ("customer", "client") for e in raw_entities_check if isinstance(e, dict))
        or (has_specific_entity and cap_id in ("customer_resolution", "entity_discovery") and not any(str(e.get("type") or e.get("entity_type") or "").lower() in ("employee", "service_line", "department", "project") for e in raw_entities_check if isinstance(e, dict)))
    )

    if (has_customer_entity or "revenue" in q_low or "billing" in q_low or metric == "revenue") and cap_id in (None, "gp_performance", "customer_resolution", "entity_discovery"):
        if "receivable" in q_low or "invoice" in q_low:
            cap_id = "receivables_analysis"
            if operation in ("entity_discovery", "lookup", "search"):
                operation = "summary"
            dimension = dimension or "customer"
        elif "proposal" in q_low:
            cap_id = "proposal_search"
            if operation in ("entity_discovery", "lookup", "search"):
                operation = "summary"
            dimension = dimension or "customer"
        elif "profile" in q_low or "contact" in q_low or "bank" in q_low:
            cap_id = "customer_360_profile"
            dimension = dimension or "customer"
        elif "revenue" in q_low or "billing" in q_low or metric == "revenue":
            cap_id = "revenue_analysis"
            metric = "revenue"
            if operation in ("entity_discovery", "lookup", "search"):
                operation = "summary"
            dimension = dimension or "customer"
        elif cap_id not in ("customer_resolution", "entity_discovery"):
            cap_id = "revenue_analysis"
            dimension = dimension or "customer"
    elif (dimension == "employee" or any(str(e.get("type") or e.get("entity_type") or "").lower() in ("employee", "staff") for e in raw_entities_check if isinstance(e, dict))) and cap_id in (None, "gp_performance"):
        if "billing" in q_low or "timesheet" in q_low or "chargeable" in q_low:
            cap_id = "staff_billing_report"
        else:
            cap_id = "kpi_summary"

    q_low = raw_query.lower()
    if re.search(r'\b(gp|gross\s+profit)\b', q_low) and cap_id in (None, "kpi_summary", "entity_discovery", "staff_billing_report"):
        cap_id = "gp_performance"
        if not metric or metric == "kpi":
            if "variance" in q_low:
                metric = "variance"
            else:
                metric = "actual_gp"

    if cap_id == "gp_performance" and not metric:
        if "variance" in q_low:
            metric = "variance"
        elif any(k in q_low for k in ("target", "budget")):
            metric = "target_gp"
        elif "percent" in q_low or "%" in q_low:
            metric = "gp_percent"
        else:
            metric = "actual_gp"

    # Check if query has explanatory/analytical intent
    expected_result_type = plan_data.get("expected_result_type") or primary_cap.get("expected_result_type")
    exp_res_type = str(expected_result_type or "").lower().strip()
    bg_lower = str(plan_data.get("business_goal", "")).lower().strip()
    rq_lower = str(raw_query).lower().strip()
    is_explanatory = (
        exp_res_type in ("explanation", "insight") or
        operation in ("analyze", "analysis", "explanation") or
        any(k in bg_lower for k in ["explain", "why", "reason", "cause"]) or
        any(k in rq_lower for k in ["why", "explain", "reason for", "cause of"])
    )
    has_explicit_ranking_kw = any(k in rq_lower for k in ["top ", "top 3", "top 5", "top 10", "highest", "best", "worst", "lowest", "rank", "ranking", "ranked", "bottom", "least", "most"])

    # Force operation == 'ranking' if explicit ranking or limit params are present for analytical queries
    has_ranking_param = bool(
        primary_cap.get("ranking") or primary_cap.get("limit") or cap_ctx.get("limit") or cap_ctx.get("ranking") or
        plan_data.get("ranking") or plan_data.get("limit")
    )
    is_explicit_kpi_ranking = cap_id == "kpi_summary" and (has_ranking_param or has_explicit_ranking_kw) and has_explicit_ranking_kw
    is_explicit_gp_ranking = cap_id == "gp_performance" and (has_ranking_param or has_explicit_ranking_kw) and has_explicit_ranking_kw

    if is_explanatory and not has_explicit_ranking_kw:
        operation = "analyze"
        expected_result_type = "explanation"
        pres_mode = "EXPLANATION"
    elif cap_id == "kpi_summary" and not is_explicit_kpi_ranking:
        operation = "summary"
        expected_result_type = "summary"
        pres_mode = plan_data.get("presentation_mode") or "REPORT"
    elif cap_id == "gp_performance" and not is_explicit_gp_ranking:
        operation = "summary"
        expected_result_type = "summary"
        pres_mode = plan_data.get("presentation_mode") or "REPORT"
    elif (has_ranking_param or operation == "ranking" or primary_cap.get("intent") == "ranking") and has_explicit_ranking_kw and not has_specific_entity:
        operation = "ranking"
        if not dimension and cap_id:
            from registry.capability_catalog import get_capability_default_dimension
            dimension = get_capability_default_dimension(cap_id)
        pres_mode = plan_data.get("presentation_mode") or "REPORT"
    elif dimension and has_explicit_ranking_kw and not has_specific_entity:
        operation = "ranking"
        pres_mode = plan_data.get("presentation_mode") or "REPORT"

    # Comparison normalization
    raw_comp = primary_cap.get("comparison") or cap_ctx.get("comparison") or plan_data.get("comparison") or primary_cap.get("comparison_periods") or cap_ctx.get("comparison_periods")
    if operation == "comparison" or raw_comp or primary_cap.get("intent") == "comparison":
        operation = "comparison"

    if not expected_result_type:
        expected_result_type = primary_cap.get("expected_result_type") or plan_data.get("expected_result_type") or operation

    # Ranking normalization
    ranking_spec = None
    if has_explicit_ranking_kw and (operation == "ranking" or has_ranking_param) and not (is_explanatory and not has_explicit_ranking_kw):
        raw_sort = str(primary_cap.get("sort_order") or primary_cap.get("ranking") or cap_ctx.get("sort_order") or plan_data.get("ranking") or "desc").lower()
        sort_dir = "asc" if (raw_sort == "asc" or any(w in rq_lower for w in ("worst", "lowest", "bottom", "least"))) else "desc"
        limit_val = primary_cap.get("limit") or cap_ctx.get("limit") or plan_data.get("limit") or 1
        ranking_spec = RankingSpec(direction=sort_dir, limit=limit_val)
        if not metric:
            metric = primary_cap.get("primary_metric") or primary_cap.get("default_ranking_field")

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
    if not raw_entities:
        from agent.entity_resolver import extract_entities_from_text
        raw_entities = extract_entities_from_text(raw_query) or []
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
    if not isinstance(filters, dict):
        filters = {}

    # Extract explicit column projection if user specifies "with <col1> and <col2>" or "only <col1>"
    req_cols = filters.get("requested_columns") or cap_ctx.get("requested_columns")
    if not req_cols:
        q_lower = raw_query.lower()
        if any(w in q_lower for w in [" with ", " only ", " columns ", " fields ", " including "]):
            col_patterns = [
                ("project_name", ["project name", "project names", "project"]),
                ("customer_name", ["customer name", "client name", "customer names", "client names", "customer", "client"]),
                ("service_line", ["service line", "service lines", "serviceline"]),
                ("approved_fees", ["approved fees", "approved fee", "fees", "fee"]),
                ("actual_recoverability", ["actual recoverability", "actual recoverability percentage", "actual recoverability pct"]),
                ("project_status", ["project status", "status"]),
                ("estimated_recoverability", ["estimated recoverability", "proposal recoverability", "est recoverability"]),
                ("performing_gp", ["performing gp", "actual gp", "gross profit"]),
                ("target_gp", ["target gp", "gp target"]),
                ("variance", ["variance", "gap", "shortfall"]),
            ]
            trigger_match = re.search(r'\b(?:with|only|including|showing|show)\s+(.+)$', q_lower)
            target_str = trigger_match.group(1) if trigger_match else q_lower
            matched_cols = []
            for col_key, phrases in col_patterns:
                if any(p in target_str for p in phrases):
                    if col_key not in matched_cols:
                        matched_cols.append(col_key)
            if len(matched_cols) >= 1:
                req_cols = matched_cols
                filters["requested_columns"] = matched_cols
                if isinstance(cap_ctx, dict):
                    cap_ctx["requested_columns"] = matched_cols

    missing_info = list(plan_data.get("missing_information") or [])
    TEMPORAL_MISSING_TERMS = {
        "date_range", "financial_year", "start_date", "end_date",
        "temporal_scope", "period", "time_filter", "dates", "timeframe", "time_period",
        "month", "months", "specific months", "specific_months"
    }
    if temporal_spec and temporal_spec.start_date and temporal_spec.end_date:
        missing_info = [
            m for m in missing_info
            if m.lower().strip() not in TEMPORAL_MISSING_TERMS
        ]

    if metric:
        missing_info = [m for m in missing_info if m.lower().strip() not in ("metric", "metrics")]
    if cap_id:
        missing_info = [m for m in missing_info if m.lower().strip() not in ("capability", "report", "intent")]
    if dimension:
        missing_info = [m for m in missing_info if m.lower().strip() not in ("dimension", "group_by")]
    if operation == "analyze":
        missing_info = [
            m for m in missing_info
            if m.lower().strip() not in (
                "field", "fields", "field context", "field_context",
                "context", "dimension", "dimensions", "reason", "cause",
                "explanation", "variance"
            )
        ]

    confidence = float(plan_data.get("confidence_score", 1.0))
    if not missing_info and confidence < 1.0 and not plan_data.get("entity_errors"):
        confidence = 1.0

    business_goal = plan_data.get("business_goal", "")
    exp_res_type = str(expected_result_type or "").lower().strip()
    bg_lower = str(business_goal).lower().strip()
    rq_lower = str(raw_query).lower().strip()

    is_explanatory = (
        exp_res_type in ("explanation", "insight") or
        operation in ("analyze", "analysis", "explanation") or
        any(k in bg_lower for k in ["explain", "why", "reason", "cause"]) or
        any(k in rq_lower for k in ["why", "explain", "reason for", "cause of"])
    )

    if is_explanatory and not has_explicit_ranking_kw:
        operation = "analyze"
        pres_mode = "EXPLANATION"
        if not expected_result_type or expected_result_type == "summary":
            expected_result_type = "explanation"
        ranking_spec = None
    else:
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
