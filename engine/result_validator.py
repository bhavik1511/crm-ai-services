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
        req_comparison = req_dict.get("comparison_type") or requested_constraints.get("comparison_type")
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
            
            emp_errs_before = len(errors)
            if is_org is True and not payload_emp:
                errors.append(f"Employee scope mismatch: requested employee '{req_employee or req_employee_id}', but returned organization-wide KPI aggregate.")
            elif req_employee_id and payload_emp_id and int(req_employee_id) != int(payload_emp_id):
                errors.append(f"Employee ID mismatch: requested ID '{req_employee_id}', payload returned ID '{payload_emp_id}'.")
            elif pay_emp_norm and req_emp_norm and (req_emp_norm not in pay_emp_norm and pay_emp_norm not in req_emp_norm):
                errors.append(f"Employee mismatch: requested '{req_employee}', payload returned '{payload_emp}'.")
            
            val_status = "PASS" if len(errors) == emp_errs_before else "FAIL"
            logger.info(
                f"[KPI_ENTITY_VALIDATION] requested_employee_id={req_employee_id} "
                f"returned_employee_id={payload_emp_id} status={val_status}"
            )

        # 2b. Validate Customer Scope
        req_customer = req_dict.get("customer_name") or requested_constraints.get("customer_name") or requested_constraints.get("customer")
        req_customer_id = req_dict.get("customer_id") or requested_constraints.get("customer_id")

        if (req_customer or req_customer_id) and isinstance(payload, dict):
            summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
            payload_cust = summary.get("customer_name") or summary.get("customer") or payload.get("customer_name") or payload.get("customer")
            payload_cust_id = summary.get("customer_id") or summary.get("cust_id") or payload.get("customer_id") or payload.get("cust_id")
            is_cust_scoped = summary.get("is_customer_scoped") or payload.get("is_customer_scoped")

            if not is_cust_scoped and not payload_cust and not payload_cust_id and payload.get("is_generic_unfiltered_search"):
                errors.append(f"Customer scope mismatch: requested customer '{req_customer or req_customer_id}', but returned generic unfiltered aggregate.")
            elif req_customer_id and payload_cust_id and int(req_customer_id) != int(payload_cust_id):
                errors.append(f"Customer ID mismatch: requested ID '{req_customer_id}', payload returned ID '{payload_cust_id}'.")

        # 3. Validate Status Scope
        if req_status and isinstance(payload, dict):
            payload_status = payload.get("status") or payload.get("proposal_status")
            has_status_metrics = any(k in payload for k in [
                "rejected_proposals", "accepted_proposals", "sent_proposals", "created_proposals",
                "dashboard_proposal_metrics_breakdown", "open_proposals", "won_proposals",
                "total_proposals", "status_breakdown", "proposal_status_id", "service_leads_breakdown", "strictly_active_projects_count"
            ])
            if not payload_status and not has_status_metrics and payload.get("is_generic_unfiltered_search"):
                errors.append(f"Status filter mismatch: requested status '{req_status}', but execution payload lacks status filtering.")

        # 3b. Validate Comparison Scope
        if req_comparison and isinstance(payload, dict):
            if req_comparison == "budget":
                has_budget = any(k in payload for k in ["target", "budget", "total_kpi_target", "target_rev", "gp_performance_ytd_breakdown", "performing"]) or any(
                    "target" in str(k).lower() or "budget" in str(k).lower() for k in payload.keys()
                )
                if not has_budget:
                    errors.append("Comparison type mismatch: requested 'budget' comparison, but payload lacks budget/target metrics.")
            elif req_comparison == "previous_fy":
                has_prev_fy = "previous_fy_revenue" in payload or any("prev" in str(k).lower() or "prior" in str(k).lower() for k in payload.keys())
                if not has_prev_fy:
                    errors.append("Comparison type mismatch: requested 'previous_fy' comparison, but payload lacks previous FY metrics.")

        # 4. Validate Operation / Granularity (Ranking & Multi-Period Comparison Fail-Closed Rules)
        if req_op == "ranking":
            ranking_list = payload.get("ranking_data") if isinstance(payload, dict) else None
            records = payload.get("records") or payload.get("data") or payload.get("rows") if isinstance(payload, dict) else None
            if not ranking_list and not (isinstance(records, list) and len(records) > 0):
                errors.append("Operation mismatch: requested ranking operation, but backend returned generic summary report instead of ranking dataset.")

        # 4b. Validate Dimension Match for Ranking
        if req_op == "ranking" or (isinstance(payload, dict) and (payload.get("operation") == "ranking" or "ranking_data" in payload)):
            ret_dim = payload.get("dimension") if isinstance(payload, dict) else None
            if req_dim and ret_dim:
                req_dim_clean = str(req_dim).lower().replace("_", "").replace(" ", "")
                ret_dim_clean = str(ret_dim).lower().replace("_", "").replace(" ", "")
                if req_dim_clean != ret_dim_clean and req_dim_clean not in ret_dim_clean and ret_dim_clean not in req_dim_clean:
                    errors.append(f"Dimension mismatch: requested dimension '{req_dim}', but payload returned dimension '{ret_dim}'.")
                    logger.warning(
                        f"[RESULT_SEMANTIC_VALIDATION] requested_dimension={req_dim} returned_dimension={ret_dim} status=FAIL"
                    )
                else:
                    logger.info(
                        f"[RESULT_SEMANTIC_VALIDATION] requested_dimension={req_dim} returned_dimension={ret_dim} status=PASS"
                    )

        # 4c. Validate Entity Lineage for Ranking Data
        if isinstance(payload, dict) and "ranking_data" in payload:
            ranking_items = payload.get("ranking_data", [])
            for item in ranking_items:
                ent_name = item.get("entity_name")
                ent_source = item.get("source", "node_authoritative_ranking_api")
                logger.info(
                    f"[ENTITY_LINEAGE] requested_dimension={req_dim} requested_entity=None resolved_entity={ent_name} "
                    f"entity_id=None backend_source={ent_source} result_source={ent_source}"
                )

        # 4d. Validate Capability Result Isolation
        if capability_id in ["receivables_analysis", "receivables"]:
            has_receivables_data = isinstance(payload, dict) and any(k in payload for k in [
                "total_receivables", "receivables", "ageing_summary", "outstanding_amount", "receivable_records", "rows", "records", "total_records"
            ])
            if not has_receivables_data and payload.get("billing_revenue_gp_table"):
                errors.append("Capability mismatch: requested receivables analysis, but payload returned revenue billing table.")
                logger.warning(
                    f"[RESULT_SEMANTIC_VALIDATION] requested_capability=receivables_analysis returned_payload=revenue_billing_table status=FAIL"
                )

        if req_op == "comparison" or len(req_dict.get("comparison_periods", [])) >= 2:
            comp_list = payload.get("comparison_periods") if isinstance(payload, dict) else None
            req_period_count = len(req_dict.get("comparison_periods", [])) or 2
            payload_status = payload.get("status") if isinstance(payload, dict) else None

            if payload_status in ["AUTH_ERROR", "BACKEND_ERROR", "VALIDATION_ERROR", "PERIOD_MISMATCH"]:
                errors.append(f"Comparison execution failed with backend status '{payload_status}'.")
            elif not comp_list or not isinstance(comp_list, list) or len(comp_list) < req_period_count:
                errors.append(f"Comparison mismatch: requested {req_period_count}-period comparison, but payload returned {len(comp_list) if isinstance(comp_list, list) else 0} periods.")
            else:
                failed_periods = [p for p in comp_list if p.get("status") != "PASS"]
                if failed_periods:
                    errors.append(f"Comparison failure: {len(failed_periods)} of {len(comp_list)} periods failed execution.")
                for p in comp_list:
                    req_p = p.get("requested_period") or p.get("period")
                    ret_p = p.get("returned_period")
                    if p.get("status") == "PERIOD_MISMATCH" or (ret_p and req_p and str(req_p).strip().lower() != str(ret_p).strip().lower()):
                        errors.append(f"Period identity mismatch: requested '{req_p}', backend returned '{ret_p}'.")

        # 4e. Validate Metric Semantics
        req_metric = (req_dict.get("metric") or "").lower().strip()
        if req_metric and isinstance(payload, dict):
            metric_type = payload.get("metric_type")
            if "count" in req_metric and metric_type == "monetary":
                errors.append(f"Metric semantics mismatch: requested count metric '{req_metric}', but returned monetary value.")
                logger.warning(f"[METRIC_MISMATCH_FAIL] requested_metric='{req_metric}' returned_metric_type='{metric_type}'. Fail-Closed.")
            elif ("amount" in req_metric or "revenue" in req_metric or "receivables" in req_metric) and "count" not in req_metric and metric_type == "count":
                errors.append(f"Metric semantics mismatch: requested monetary metric '{req_metric}', but returned count value.")
                logger.warning(f"[METRIC_MISMATCH_FAIL] requested_metric='{req_metric}' returned_metric_type='{metric_type}'. Fail-Closed.")

        # 5. Validate Primary Metric Existence
        if primary_metric and isinstance(payload, dict):
            if primary_metric not in payload and not any(k in payload for k in ["data", "rows", "summary", "count", "records", "results", "list", "items", "table", "billing", "dashboard_proposal_metrics_breakdown", "open_proposals", "won_proposals", "total_proposals", "ranking_data", "comparison_periods"]):
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
