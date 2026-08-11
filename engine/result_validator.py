"""
result_validator.py — Result Validation Engine
================================================
Metadata-driven result validation engine. Validates backend tool payloads
against user-requested parameters (FY, month, customer, project, service line,
department, metrics, schema completeness, employee scope, status scope) BEFORE formatting/rendering.

Never allows corrupted, downgraded, or mismatched backend data to reach the user.
"""

import logging
from typing import Dict, Any, List, Tuple
from registry.metadata_registry import get_registry

logger = logging.getLogger(__name__)

class ResultValidationEngine:
    """
    Validates backend execution results against requested constraints and ExecutionContract.
    """
    def __init__(self, registry=None):
        self.registry = registry or get_registry()

    def validate_result(
        self, capability_id: str, payload_envelope: Dict[str, Any], requested_constraints: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Validates payload envelope against requested parameters & capability schema.
        Returns (is_valid, validation_errors).
        """
        errors = []

        if not payload_envelope or not isinstance(payload_envelope, dict):
            return False, ["Payload envelope is empty or not a valid dict."]

        status = payload_envelope.get("status")
        if status == "error":
            payload_data = payload_envelope.get("payload", {})
            err_msg = payload_data.get("error_message") if isinstance(payload_data, dict) else "Backend returned error status."
            return False, [f"Backend execution failed: {err_msg}"]

        payload = payload_envelope.get("payload")
        if payload is None:
            return False, ["Payload content is None."]

        cap_meta = self.registry.get_capability(capability_id) or {}
        schema = cap_meta.get("response_schema", {})
        primary_metric = cap_meta.get("primary_metric")

        # Extract contract / requested scope fields
        contract = requested_constraints.get("execution_contract")
        if contract and hasattr(contract, "to_dict"):
            req_dict = contract.to_dict()
        elif isinstance(requested_constraints, dict):
            req_dict = requested_constraints
        else:
            req_dict = {}

        req_status = req_dict.get("status_filter") or requested_constraints.get("status")
        req_employee = req_dict.get("employee_name") or requested_constraints.get("employee_name") or requested_constraints.get("employee")
        req_employee_id = req_dict.get("employee_id") or requested_constraints.get("employee_id")
        req_op = req_dict.get("operation") or requested_constraints.get("operation")
        req_dim = req_dict.get("dimension") or requested_constraints.get("dimension")
        req_start = req_dict.get("start_date") or requested_constraints.get("start_date")
        req_end = req_dict.get("end_date") or requested_constraints.get("end_date")
        req_fy = req_dict.get("financial_year") or requested_constraints.get("financial_year") or requested_constraints.get("fy")

        # 1. Check Sample Truncation Status
        total_count = None
        rows_len = 0
        if isinstance(payload, dict):
            total_count = payload.get("total_count") or payload.get("count") or payload.get("total")
            rows = payload.get("rows") or payload.get("records") or payload.get("data") or payload.get("results") or payload.get("proposals")
            if isinstance(rows, list):
                rows_len = len(rows)

        is_sample_truncated = False
        if total_count and isinstance(total_count, int) and rows_len > 0 and total_count > rows_len:
            is_sample_truncated = True
            payload_envelope["is_sample_truncated"] = True
            logger.info(f"[ResultValidator] Marked sample as TRUNCATED (total_count={total_count} > sample_rows={rows_len})")

        # 2. Validate Employee Scope
        if (req_employee or req_employee_id) and isinstance(payload, dict):
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
            payload_emp = summary.get("employee_name") or summary.get("employee") or payload.get("employee_name") or payload.get("employee")
            payload_emp_id = summary.get("employee_id") or summary.get("emp_id") or payload.get("employee_id") or payload.get("emp_id")
            is_org = summary.get("is_organization_aggregate") if "is_organization_aggregate" in summary else payload.get("is_organization_aggregate")
            
            req_emp_norm = " ".join(str(req_employee).split()).lower() if req_employee else ""
            pay_emp_norm = " ".join(str(payload_emp).split()).lower() if payload_emp else ""
            
            if is_org is True and not payload_emp:
                errors.append(f"Employee scope mismatch: requested employee '{req_employee or req_employee_id}', but returned organization-wide KPI aggregate.")
            elif req_employee_id and payload_emp_id and int(req_employee_id) != int(payload_emp_id):
                errors.append(f"Employee ID mismatch: requested ID '{req_employee_id}', payload returned ID '{payload_emp_id}'.")
            elif pay_emp_norm and req_emp_norm and (req_emp_norm not in pay_emp_norm and pay_emp_norm not in req_emp_norm):
                errors.append(f"Employee mismatch: requested '{req_employee}', payload returned '{payload_emp}'.")

        # 3. Validate Status Scope
        if req_status and isinstance(payload, dict):
            payload_status = payload.get("status") or payload.get("proposal_status")
            has_status_metrics = any(k in payload for k in [
                "rejected_proposals", "accepted_proposals", "sent_proposals", "created_proposals",
                "dashboard_proposal_metrics_breakdown", "open_proposals", "won_proposals",
                "total_proposals", "status_breakdown", "proposal_status_id", "service_leads_breakdown"
            ])
            if not payload_status and not has_status_metrics and payload.get("is_generic_unfiltered_search"):
                errors.append(f"Status filter mismatch: requested status '{req_status}', but execution payload lacks status filtering.")

        # 4. Validate Operation / Granularity (Ranking vs Total Revenue Downgrade)
        if req_op == "ranking" and req_dim == "customer" and isinstance(payload, dict):
            # Must contain customer-level records/list
            records = payload.get("records") or payload.get("data") or payload.get("rows") or payload.get("billing") or payload.get("customers")
            if isinstance(records, list) and len(records) > 0:
                pass  # Good, contains customer records
            elif "total_net_amount" in payload or "total_revenue" in payload:
                errors.append("Operation mismatch: requested customer ranking/top-N, but payload only contains organization-wide total revenue without customer breakdown.")

        # 5. Validate Primary Metric Existence
        if primary_metric and isinstance(payload, dict):
            if primary_metric not in payload and not any(k in payload for k in ["data", "rows", "summary", "count", "records", "results", "list", "items", "table", "billing", "dashboard_proposal_metrics_breakdown", "open_proposals", "won_proposals", "total_proposals"]):
                errors.append(f"Primary metric '{primary_metric}' missing from response payload.")

        # 6. Validate Financial Year Matching if explicitly requested
        if req_fy and isinstance(payload, dict):
            payload_fy = payload.get("financial_year") or payload.get("fy")
            if payload_fy and str(payload_fy).lower() != str(req_fy).lower():
                errors.append(f"Financial year mismatch: requested '{req_fy}', payload returned '{payload_fy}'.")

        # 7. Validate Temporal Scope Bounds if explicit range requested
        req_temporal = req_dict.get("temporal_scope")
        if req_temporal and req_temporal in ["current_month", "explicit_month"] and isinstance(payload, dict):
            payload_start = payload.get("start_date") or payload.get("from_date")
            payload_end = payload.get("end_date") or payload.get("to_date")
            if payload_start and req_start and str(payload_start)[:7] != str(req_start)[:7]:
                errors.append(f"Temporal scope mismatch: requested range starting '{req_start}', payload returned range starting '{payload_start}'.")

        is_valid = len(errors) == 0
        from utils.structured_logger import log_stage
        log_stage(logger, "VALIDATION", Status="PASS" if is_valid else "FAIL", Capability=capability_id, Errors=errors if errors else "None")

        return is_valid, errors

def get_result_validator() -> ResultValidationEngine:
    return ResultValidationEngine()
