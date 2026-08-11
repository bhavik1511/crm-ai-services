"""
execution_contract.py — Unified Immutable Execution Contract
============================================================

Defines the single source of truth for query scope, filters, and execution boundaries
passed from QueryParser down to ExecutionProvider, ResultValidator, TransformationEngine,
and Renderer.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

@dataclass
class ExecutionContract:
    capability_id: str
    operation: str = "summary"
    metric: Optional[str] = None
    dimension: Optional[str] = None
    status_filter: Optional[str] = None
    employee_id: Optional[int] = None
    employee_name: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    project_id: Optional[int] = None
    service_line_id: Optional[int] = None
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "operation": self.operation,
            "metric": self.metric,
            "dimension": self.dimension,
            "status_filter": self.status_filter,
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "project_id": self.project_id,
            "service_line_id": self.service_line_id,
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
        }


def extract_status_filter(query: str) -> Optional[str]:
    """Extract canonical status filter from user query text."""
    q_lower = query.lower()
    if any(w in q_lower for w in ['accepted', 'won', 'approved']):
        return 'accepted'
    if any(w in q_lower for w in ['rejected', 'lost', 'declined']):
        return 'rejected'
    if any(w in q_lower for w in ['sent', 'submitted', 'open', 'pending']):
        return 'open'
    if any(w in q_lower for w in ['active', 'ongoing']):
        return 'active'
    if any(w in q_lower for w in ['completed', 'closed', 'finished']):
        return 'completed'
    return None


def extract_operation(query: str) -> str:
    """Extract operational intent (ranking, breakdown, count, summary)."""
    q_lower = query.lower()
    if any(w in q_lower for w in ['top', 'highest', 'best', 'ranking', 'largest', 'bottom', 'lowest']):
        return 'ranking'
    if any(w in q_lower for w in ['breakdown', 'by status', 'by service line', 'by department', 'distribution']):
        return 'breakdown'
    if any(w in q_lower for w in ['how many', 'count', 'total number']):
        return 'count'
    return 'summary'


def extract_limit(query: str) -> Optional[int]:
    """Extract limit (e.g. top 5 -> 5)."""
    m = re.search(r'\btop\s+(\d+)\b', query, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def create_execution_contract(
    capability_id: str,
    question: str,
    override_params: Optional[Dict[str, Any]] = None
) -> ExecutionContract:
    """
    Constructs a normalized, canonical ExecutionContract from question text and capability.
    Evaluates metadata-declared context requirements dynamically.
    """
    from agent.temporal_resolver import resolve_temporal_scope
    from registry.metadata_registry import get_registry

    params = override_params or {}
    pending_contract = params.get("pending_contract") or params.get("contract") or {}

    temp = resolve_temporal_scope(question)
    status = extract_status_filter(question) or pending_contract.get("status_filter")
    op = extract_operation(question)
    if op == "summary" and pending_contract.get("operation"):
        op = pending_contract.get("operation")
    lim = extract_limit(question) or pending_contract.get("limit")

    # MANDATE: If query explicitly specified a temporal scope (e.g., "this month", "current month", "last month", "FY25"),
    # the resolved temporal scope and date bounds MUST take precedence over any inherited background params.
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
        if not is_explicit and not params.get("is_explicit"):
            clarification_required = True
            missing_context.append("temporal_scope")
            clarification_reason = "missing_temporal_scope"

    from registry.contract_engine import resolve_presentation_intent
    presentation_intent = params.get("presentation_intent") or resolve_presentation_intent(question)

    return ExecutionContract(
        capability_id=capability_id,
        operation=params.get("operation") or op,
        metric=params.get("metric") or pending_contract.get("metric"),
        dimension=params.get("dimension") or pending_contract.get("dimension") or ("customer" if op == "ranking" else None),
        status_filter=params.get("status") or params.get("status_filter") or status,
        employee_id=params.get("employee_id") or pending_contract.get("employee_id"),
        employee_name=params.get("employee_name") or pending_contract.get("employee_name"),
        customer_id=params.get("customer_id") or pending_contract.get("customer_id"),
        customer_name=params.get("customer_name") or pending_contract.get("customer_name"),
        project_id=params.get("project_id") or pending_contract.get("project_id"),
        service_line_id=params.get("service_line_id") or pending_contract.get("service_line_id"),
        temporal_scope=temporal_scope,
        start_date=start_date,
        end_date=end_date,
        financial_year=financial_year,
        limit=params.get("limit") or lim,
        sort_order=params.get("sort_order") or pending_contract.get("sort_order") or ("DESC" if op == "ranking" else None),
        raw_question=question,
        extra_filters=params,
        clarification_required=clarification_required,
        missing_context=missing_context,
        clarification_reason=clarification_reason,
        is_explicit=is_explicit,
        presentation_intent=presentation_intent,
    )
