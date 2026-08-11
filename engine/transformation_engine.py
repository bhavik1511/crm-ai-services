"""
transformation_engine.py — Result Transformation Engine
===========================================================
Generic transformation layer for analytical query operations:
- GROUP BY (customer, service_line, office, project, status, month, employee)
- AGGREGATE (SUM, COUNT, AVG, MIN, MAX)
- SORT (DESC, ASC)
- TOP-N Slicing (limit)
- BREAKDOWN, RANKING, COMPARISON, TREND, AGGREGATE formats

Operates strictly on structured operation metadata and payload envelopes.
Zero capability-specific hardcoded logic.
"""

import logging
from typing import Dict, Any, List, Optional
from engine.query_operation import QueryOperation

logger = logging.getLogger(__name__)

def _parse_numeric(val: Any) -> float:
    """Safely converts string or numeric values to float."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        # Remove currency symbols, commas, spaces
        cleaned = str(val).replace(",", "").replace("$", "").replace("BHD", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0

class ResultTransformationEngine:
    """
    Executes generic data transformations (GROUP BY, SUM, COUNT, AVG, MIN, MAX, SORT, TOP-N)
    on raw backend execution payloads.
    """

    def resolve_dimension_key(self, sample_row: Dict[str, Any], requested_dim: Optional[str]) -> Optional[str]:
        """Dynamically identifies the row dictionary key for the requested dimension."""
        if not sample_row or not isinstance(sample_row, dict):
            return None

        keys = list(sample_row.keys())
        keys_lower = {k.lower(): k for k in keys}

        if requested_dim:
            dim = requested_dim.lower()
            # Direct or pattern match
            patterns = {
                "customer": ["customer_name", "client_name", "customer", "client", "customer_code", "company_name"],
                "service_line": ["service_line_name", "service_line", "service_name", "service", "serviceline"],
                "office": ["office_name", "office", "location", "branch", "branch_name"],
                "project": ["project_name", "project", "job_name", "project_title", "title", "name"],
                "status": ["status", "proposal_status", "stage", "state"],
                "month": ["month", "month_name", "period", "date", "created_date"],
                "year": ["year", "fy", "financial_year"],
                "employee": ["employee_name", "staff_name", "employee", "staff", "resource", "name"]
            }

            matched_patterns = patterns.get(dim, [dim])
            for pat in matched_patterns:
                if pat in keys_lower:
                    return keys_lower[pat]

            # Partial match
            for k in keys:
                if dim in k.lower():
                    return k

        # Fallback: return first string key that is not an ID
        for k, v in sample_row.items():
            if isinstance(v, str) and not k.lower().endswith("id") and not k.startswith("_"):
                return k

        return keys[0] if keys else None

    def resolve_metric_key(self, sample_row: Dict[str, Any], requested_metric: Optional[str]) -> Optional[str]:
        """Dynamically identifies the row dictionary key for the requested metric."""
        if not sample_row or not isinstance(sample_row, dict):
            return None

        keys = list(sample_row.keys())
        keys_lower = {k.lower(): k for k in keys}

        if requested_metric:
            m = requested_metric.lower()
            patterns = {
                "actual_cost": ["actual_cost", "actual_costs", "cost", "costs", "expense"],
                "budget": ["budget", "total_budget", "proposed_fee", "approved_fee"],
                "recoverability": ["recoverability", "realization", "recoverability_rate", "rate", "percentage"],
                "proposals": ["count", "total_proposals", "open_proposals", "won_proposals"],
                "revenue": ["total_net_amount", "net_amount", "amount", "revenue", "billing", "billed_amount", "total_fee", "fee"],
                "count": ["count", "quantity", "total_count", "total_projects"]
            }

            matched_patterns = patterns.get(m, [m])
            for pat in matched_patterns:
                if pat in keys_lower:
                    return keys_lower[pat]

            for k in keys:
                if m in k.lower():
                    return k

        # Fallback: return first numeric key that is not an ID
        for k, v in sample_row.items():
            if isinstance(v, (int, float)) and not k.lower().endswith("id") and not k.startswith("_"):
                return k

        return None

    def transform(
        self, capability_metadata: Dict[str, Any], payload_envelope: Dict[str, Any], query_op: QueryOperation
    ) -> Dict[str, Any]:
        """
        Transforms raw payload envelope into structured, aggregated, sorted, and limited payload envelope.
        """
        if not payload_envelope or payload_envelope.get("status") != "success":
            return payload_envelope

        payload = payload_envelope.get("payload")
        if payload is None:
            return payload_envelope

        # Unpack rows from payload
        rows: List[Dict[str, Any]] = []
        if isinstance(payload, list):
            rows = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            # Look for array keys
            for key in ["records", "data", "rows", "billing", "projects", "statusWiseResults", "details", "list", "results", "summary"]:
                val = payload.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    rows = val
                    break

        if not rows:
            logger.debug(f"[TransformationEngine] No row list found in payload; returning original envelope.")
            payload_envelope["query_operation"] = query_op.to_dict()
            return payload_envelope

        sample_row = rows[0]
        dim_key = self.resolve_dimension_key(sample_row, query_op.dimension)
        metric_key = self.resolve_metric_key(sample_row, query_op.metric)

        logger.debug(f"[TransformationEngine] Resolved dim_key='{dim_key}', metric_key='{metric_key}' for op={query_op.operation}")

        # If dimension is required for ranking/breakdown but no dim_key found, return raw
        if query_op.operation in ["ranking", "breakdown", "trend", "comparison"] and not dim_key:
            payload_envelope["query_operation"] = query_op.to_dict()
            return payload_envelope

        # Group By & Aggregate
        groups: Dict[str, List[float]] = {}
        for row in rows:
            dim_val = str(row.get(dim_key, "Unknown")) if dim_key else "Total"
            
            if query_op.aggregation == "COUNT":
                metric_val = 1.0
            elif metric_key:
                metric_val = _parse_numeric(row.get(metric_key))
            else:
                metric_val = 1.0

            if dim_val not in groups:
                groups[dim_val] = []
            groups[dim_val].append(metric_val)

        # Aggregate values per group
        aggregated_groups: List[Dict[str, Any]] = []
        for dim_val, vals in groups.items():
            if query_op.aggregation == "SUM":
                agg_val = sum(vals)
            elif query_op.aggregation == "COUNT":
                agg_val = float(len(vals))
            elif query_op.aggregation == "AVG":
                agg_val = sum(vals) / len(vals) if vals else 0.0
            elif query_op.aggregation == "MIN":
                agg_val = min(vals) if vals else 0.0
            elif query_op.aggregation == "MAX":
                agg_val = max(vals) if vals else 0.0
            else:
                agg_val = sum(vals)

            aggregated_groups.append({
                "dimension": dim_val,
                "metric_val": agg_val
            })

        # Sort
        reverse_sort = (query_op.sort == "DESC")
        aggregated_groups.sort(key=lambda x: x["metric_val"], reverse=reverse_sort)

        # Limit / Top-N
        if query_op.limit and query_op.limit > 0:
            aggregated_groups = aggregated_groups[:query_op.limit]

        dim_title = (query_op.dimension or dim_key or "Category").replace("_", " ").title()
        metric_title = (query_op.metric or metric_key or "Value").replace("_", " ").title()

        # Build clean transformed payload table
        transformed_records = []
        for item in aggregated_groups:
            transformed_records.append({
                dim_title: item["dimension"],
                metric_title: item["metric_val"]
            })

        transformed_payload = {
            "operation": query_op.operation,
            "dimension": dim_title,
            "metric": metric_title,
            "aggregation": query_op.aggregation,
            "total_records": len(transformed_records),
            "records": transformed_records
        }

        new_envelope = dict(payload_envelope)
        new_envelope["payload"] = transformed_payload
        new_envelope["query_operation"] = query_op.to_dict()

        from utils.structured_logger import log_stage
        log_stage(
            logger, "TRANSFORM",
            Op=query_op.operation,
            Dimension=dim_title,
            Metric=metric_title,
            Agg=query_op.aggregation,
            InRows=len(rows),
            OutRows=len(transformed_records)
        )
        return new_envelope

def get_transformation_engine() -> ResultTransformationEngine:
    return ResultTransformationEngine()
