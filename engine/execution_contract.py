"""
execution_contract.py — Unified Immutable Execution Contract
============================================================

Defines the single source of truth for query scope, filters, and execution boundaries
passed from QueryParser down to ExecutionProvider, ResultValidator, TransformationEngine,
and Renderer.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

@dataclass
class ExecutionContract:
    capability_id: str
    operation: str = "summary"
    metric: Optional[str] = None
    dimension: Optional[str] = None
    status_filter: Optional[str] = None
    comparison_type: Optional[str] = None
    comparison_periods: List[Dict[str, Any]] = field(default_factory=list)
    employee_id: Optional[int] = None
    employee_name: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    project_id: Optional[int] = None
    service_line_id: Optional[int] = None
    service_line: Optional[str] = None
    temporal_scope: str = "default_fy"
    start_date: str = ""
    end_date: str = ""
    financial_year: Optional[str] = None
    limit: Optional[int] = None
    sort_order: Optional[str] = None
    is_sample_truncated: bool = False
    authoritative_count: Optional[int] = None
    raw_question: str = ""
    extra_filters: Dict[str, Any] = field(default_factory=dict)
    clarification_required: bool = False
    missing_context: List[str] = field(default_factory=list)
    clarification_reason: Optional[str] = None
    is_explicit: bool = False
    presentation_intent: str = "VIEW"
    expected_result_type: str = "summary"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "operation": self.operation,
            "metric": self.metric,
            "dimension": self.dimension,
            "status_filter": self.status_filter,
            "comparison_type": self.comparison_type,
            "comparison_periods": self.comparison_periods,
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "project_id": self.project_id,
            "service_line_id": self.service_line_id,
            "service_line": self.service_line,
            "temporal_scope": self.temporal_scope,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "financial_year": self.financial_year,
            "limit": self.limit,
            "sort_order": self.sort_order,
            "is_sample_truncated": self.is_sample_truncated,
            "authoritative_count": self.authoritative_count,
            "raw_question": self.raw_question,
            "extra_filters": self.extra_filters,
            "clarification_required": self.clarification_required,
            "missing_context": self.missing_context,
            "clarification_reason": self.clarification_reason,
            "is_explicit": self.is_explicit,
            "presentation_intent": self.presentation_intent,
            "expected_result_type": self.expected_result_type,
        }


def create_execution_contract(
    capability_id: str,
    question: str,
    override_params: Optional[Dict[str, Any]] = None
) -> ExecutionContract:
    """
    Constructs a normalized, canonical ExecutionContract from validated intent parameters and capability.
    Evaluates metadata-declared context requirements dynamically without second-stage query string parsing.
    """
    from agent.temporal_resolver import resolve_temporal_scope
    from registry.metadata_registry import get_registry

    params = override_params or {}
    pending_contract = params.get("pending_contract") or params.get("contract") or {}
    canon = params.get("canonical_intent") or {}
    if isinstance(canon, str):
        try:
            import json
            canon = json.loads(canon)
        except Exception:
            canon = {}

    temp = resolve_temporal_scope(question)
    status = params.get("status") or params.get("status_filter") or pending_contract.get("status_filter")
    comp_type = params.get("comparison_type") or pending_contract.get("comparison_type") or (canon.get("comparison") or {}).get("type")
    op = params.get("operation") or canon.get("operation") or pending_contract.get("operation") or "summary"
    lim = params.get("limit") or (canon.get("ranking") or {}).get("limit") or pending_contract.get("limit")
    exp_res_type = params.get("expected_result_type") or canon.get("expected_result_type") or pending_contract.get("expected_result_type") or op

    # Temporal Scope precedence
    if temp.get("is_explicit"):
        start_date = temp["start_date"]
        end_date = temp["end_date"]
        temporal_scope = temp["temporal_scope"]
        financial_year = temp["financial_year"]
        is_explicit = True
    else:
        start_date = params.get("start_date") or temp["start_date"]
        end_date = params.get("end_date") or temp["end_date"]
        temporal_scope = params.get("temporal_scope") or temp["temporal_scope"]
        financial_year = params.get("financial_year") or temp["financial_year"]
        is_explicit = bool(params.get("start_date") and params.get("is_explicit")) or (temporal_scope != "default_fy" and temporal_scope != "all_time")

    # Dynamic Metadata-Driven Context Requirements Evaluation
    reg = get_registry()
    ctx_reqs = reg.get_context_requirements(capability_id)
    req_context = ctx_reqs.get("required_context", [])
    clar_context = ctx_reqs.get("clarifiable_context", req_context)

    clarification_required = False
    missing_context = []
    clarification_reason = None

    if "temporal_scope" in clar_context or "temporal_scope" in req_context:
        if not start_date and not end_date and not is_explicit and not params.get("is_explicit") and op not in ["ranking", "comparison"]:
            clarification_required = True
            missing_context.append("temporal_scope")
            clarification_reason = "missing_temporal_scope"

    from registry.contract_engine import resolve_presentation_intent
    presentation_intent = params.get("presentation_intent") or resolve_presentation_intent(canon, capability_id=capability_id)

    # Target Employee Entity Resolution
    from agent.query_parser import _extract_person_name, _lookup_employee_by_name, _extract_company_name, _lookup_customer_by_name

    target_emp_id = pending_contract.get("employee_id") or params.get("employee_id")
    target_emp_name = pending_contract.get("employee_name") or params.get("employee_name")

    from agent.entity_resolver import has_employee_trigger, is_reserved_business_term
    has_emp_trig = has_employee_trigger(question)
    requires_emp_capability = capability_id in ["kpi_summary", "employee_performance", "staff_billing"]

    extracted_emp_name = _extract_person_name(question) if (not target_emp_name and (has_emp_trig or requires_emp_capability) and capability_id != "gp_performance") else None
    if extracted_emp_name:
        emp_match = _lookup_employee_by_name(extracted_emp_name)
        if emp_match:
            target_emp_id, target_emp_name = emp_match
            logger.info(f"[ENTITY_RESOLUTION] Requested employee '{extracted_emp_name}' -> CRM employee_id={target_emp_id} ('{target_emp_name}')")
        else:
            target_emp_name = extracted_emp_name
            target_emp_id = None
            logger.info(f"[ENTITY_RESOLUTION] Requested employee '{extracted_emp_name}' -> Not found in CRM DB master data")
    elif not target_emp_id and not target_emp_name:
        if params.get("target_employee_id") or params.get("requested_employee_id"):
            target_emp_id = params.get("target_employee_id") or params.get("requested_employee_id")
            target_emp_name = params.get("target_employee_name") or params.get("requested_employee_name")

    if capability_id == "gp_performance":
        is_my_gp = any(w in question.lower().split() for w in ["my", "mine", "me"])
        if not is_my_gp and not has_emp_trig and not extracted_emp_name and not params.get("target_employee_id") and not params.get("requested_employee_id"):
            target_emp_id = None
            target_emp_name = None

    # Target Customer Entity Resolution
    target_cust_id = params.get("customer_id") or pending_contract.get("customer_id")
    target_cust_name = params.get("customer_name") or pending_contract.get("customer_name")

    skip_cust_extraction = capability_id in ["gp_performance", "kpi_summary", "employee_performance", "staff_billing"] or bool(target_emp_id) or bool(target_emp_name)
    extracted_cust_name = _extract_company_name(question) if (not target_cust_name and not skip_cust_extraction) else None
    if extracted_cust_name:
        if (target_emp_name and extracted_cust_name.lower() == target_emp_name.lower()) or _lookup_employee_by_name(extracted_cust_name):
            extracted_cust_name = ""

    if extracted_cust_name and not target_cust_id:
        cust_match = _lookup_customer_by_name(extracted_cust_name)
        if cust_match:
            target_cust_id, target_cust_name = cust_match
            logger.info(f"[ENTITY_RESOLUTION] Requested customer '{extracted_cust_name}' -> CRM customer_id={target_cust_id} ('{target_cust_name}')")
        else:
            target_cust_name = extracted_cust_name
            target_cust_id = None
            logger.info(f"[ENTITY_RESOLUTION] Requested customer '{extracted_cust_name}' -> Not found in CRM DB master data")
            clarification_required = True
            missing_context.append("customer")
            clarification_reason = f"customer_not_found:{extracted_cust_name}"

    comp_periods = params.get("comparison_periods") or pending_contract.get("comparison_periods") or []
    if not comp_periods and canon.get("comparison"):
        canon_comp = canon.get("comparison")
        if isinstance(canon_comp, dict):
            comp_periods = canon_comp.get("periods") or canon.get("comparison_periods") or []
        elif hasattr(canon_comp, "periods"):
            comp_periods = [p.model_dump() if hasattr(p, "model_dump") else (p.dict() if hasattr(p, "dict") else dict(p)) for p in (getattr(canon_comp, "periods") or [])]
        elif isinstance(canon.get("comparison_periods"), list):
            comp_periods = canon.get("comparison_periods")
    extracted_dim = params.get("dimension") or canon.get("dimension") or pending_contract.get("dimension")
    extracted_metric = params.get("metric") or canon.get("metric") or pending_contract.get("metric")

    # Fail-Closed Rule: Ranking operations REQUIRE an explicit dimension
    if (op == "ranking" or exp_res_type == "ranking") and not extracted_dim:
        logger.warning("[EXECUTION_CONTRACT] Ranking operation requested without explicit dimension. Triggering Fail-Closed clarification.")
        clarification_required = True
        if "dimension" not in missing_context:
            missing_context.append("dimension")
        clarification_reason = "missing_ranking_dimension"

    target_sl_id = params.get("service_line_id") or pending_contract.get("service_line_id")
    target_sl_name = params.get("service_line") or pending_contract.get("service_line")
    raw_input_val = params.get("raw_input_alias") or params.get("input_value") or params.get("query")

    # ── Canonical Entity Integrity Invariant Check ────────────────────────────
    if target_sl_id is not None or target_sl_name is not None:
        if raw_input_val and target_sl_name and target_sl_name.strip().lower() == str(raw_input_val).strip().lower() and params.get("match_field") == "short_code":
            logger.error(f"[ENTITY_CANONICAL_INVARIANT] status=FAIL reason=identity_mismatch service_line_id={target_sl_id} service_line=\"{target_sl_name}\"")
            clarification_required = True
            clarification_reason = "identity_mismatch"
        else:
            logger.info(f"[ENTITY_CANONICAL_INVARIANT] status=PASS service_line_id={target_sl_id} service_line=\"{target_sl_name}\"")

    if target_emp_id is not None or target_emp_name is not None:
        is_emp_mismatch = False
        if target_emp_id is not None and target_emp_name:
            emp_name_clean = target_emp_name.strip().lower()
            if emp_name_clean in ["tech", "brs", "a&a", "bps", "legal", "tax"] or is_reserved_business_term(emp_name_clean):
                is_emp_mismatch = True
            elif raw_input_val and emp_name_clean == str(raw_input_val).strip().lower() and len(str(raw_input_val).strip().split()) == 1 and len(target_emp_name.strip().split()) > 1:
                is_emp_mismatch = True

        if is_emp_mismatch:
            logger.error(f"[ENTITY_CANONICAL_INVARIANT] status=FAIL reason=identity_mismatch employee_id={target_emp_id} employee_name=\"{target_emp_name}\"")
            clarification_required = True
            clarification_reason = "identity_mismatch"
        else:
            logger.info(f"[ENTITY_CANONICAL_INVARIANT] status=PASS employee_id={target_emp_id} employee_name=\"{target_emp_name}\"")

    contract_obj = ExecutionContract(
        capability_id=capability_id,
        operation=op,
        metric=extracted_metric,
        dimension=extracted_dim,
        status_filter=status,
        comparison_type=comp_type,
        comparison_periods=comp_periods,
        employee_id=target_emp_id,
        employee_name=target_emp_name,
        customer_id=target_cust_id,
        customer_name=target_cust_name,
        project_id=params.get("project_id") or pending_contract.get("project_id"),
        service_line_id=target_sl_id,
        service_line=target_sl_name,
        temporal_scope=temporal_scope,
        start_date=start_date,
        end_date=end_date,
        financial_year=financial_year,
        limit=lim,
        sort_order=params.get("sort_order") or pending_contract.get("sort_order") or ("desc" if op == "ranking" else None),
        raw_question=question,
        extra_filters=params,
        clarification_required=clarification_required,
        missing_context=missing_context,
        clarification_reason=clarification_reason,
        is_explicit=is_explicit,
        presentation_intent=presentation_intent,
        expected_result_type=exp_res_type,
    )
    # Authoritative Capability Catalog Contract Validation
    from registry.capability_catalog import get_capability_metadata
    cap_meta = get_capability_metadata(capability_id) or {}
    auth_ep = cap_meta.get("authoritative_endpoint", "unknown")
    contract_status = "PASS" if not clarification_required else "FAIL"

    logger.info(
        f"[EXECUTION_CONTRACT] "
        f"capability={capability_id} "
        f"operation={op} "
        f"metric={extracted_metric or 'default'} "
        f"dimension={extracted_dim or 'none'} "
        f"endpoint={auth_ep} "
        f"parameters={{'start_date': '{start_date}', 'end_date': '{end_date}', 'employee_id': {target_emp_id}, 'customer_id': {target_cust_id}}} "
        f"status={contract_status}"
    )

    return contract_obj
