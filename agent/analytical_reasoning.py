"""
agent/analytical_reasoning.py — Generic Analytical Reasoning Engine.

Implements a 7-Level Generic Analytical Reasoning Hierarchy for ANY business capability:
LEVEL 1: Identify observed outcome (authoritative actual vs target).
LEVEL 2: Quantify deviation (exact magnitudes, BHD amounts, percentages).
LEVEL 3: Analyze temporal pattern (persistent vs isolated, worsening/improving, peak shortfall).
LEVEL 4: Investigate underlying drivers (inspect secondary metrics/dimensions).
LEVEL 5: Attribute cause using evidence (direction, magnitude, concentration).
LEVEL 6: Explain business pattern (executive natural language prose).
LEVEL 7: State evidence limitations (explain what cannot be concluded due to missing drivers).

CRITICAL: ZERO hardcoded capability checks (no if capability == "gp_performance").
100% metadata and data-structure driven.
"""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Standard key hints for dynamic schema discovery
_VARIANCE_KEYS = {"variance", "shortfall", "diff", "difference", "delta", "budget_variance"}
_PERFORMING_KEYS = {"performing", "performing_gp", "actual", "actual_amount", "achieved", "realized", "total_amt_ex_vat", "revenue", "amount", "total_revenue_ytd", "total_performing"}
_TARGET_KEYS = {"target", "target_gp", "budget", "target_value", "goal", "benchmark", "target_revenue", "total_target_budget", "total_kpi_target"}
_TEMPORAL_KEYS = {"month", "period", "date", "quarter", "year", "created_at", "target_month", "day", "month_order", "month_name", "date_range"}
_METADATA_KEYS = {"id", "code", "sort_order", "order", "status", "endpoint", "metric_label", "metric_type", "dimension", "returned_metric", "requested_metric", "authoritative", "is_organization_aggregate", "capability"}
_DRIVER_METRIC_KEYS = {"revenue", "cost", "actual_cost", "target_revenue", "agreed_fees", "budget_value", "total_hours", "total_tokens", "total_queries", "total_cost_usd"}
_DRIVER_DIMENSION_KEYS = {"service_line_name", "service_line", "department_name", "department", "customer_name", "client", "project_name", "lead_source", "employee_name"}

class AnalyticalContext:
    """
    Generic analytical context built from authoritative payload & execution plan metadata.
    Does NOT hardcode any capability or metric name.
    """
    def __init__(self, tool_results: List[dict], plan: Optional[dict] = None):
        self.plan = plan or {}
        self.tool_results = tool_results or []
        self.capability = self.plan.get("capability") or self._extract_first_cap_id()
        self.requested_metric = self.plan.get("metric") or "metric"
        self.requested_dimension = self.plan.get("dimension") or "aggregate"
        raw_op = self.plan.get("operation") or "analyze"
        self.operation = "analyze" if raw_op == "analysis" else raw_op
        
        self.rows = self._extract_authoritative_rows()
        self.numeric_fields = self._discover_numeric_fields()
        self.categorical_fields = self._discover_categorical_fields()
        
        # Primary Outcome Keys
        self.variance_key = self._find_matching_key(_VARIANCE_KEYS)
        self.performing_key = self._find_matching_key(_PERFORMING_KEYS)
        self.target_key = self._find_matching_key(_TARGET_KEYS)
        self.temporal_key = self._find_matching_key(_TEMPORAL_KEYS)
        
        # Secondary Driver Discovery
        outcome_metric_keys = {self.variance_key, self.performing_key, self.target_key} - {None}
        self.driver_metric_keys = [
            k for k in self.numeric_fields 
            if k in _DRIVER_METRIC_KEYS or (k not in outcome_metric_keys and k not in _METADATA_KEYS and not k.endswith("_id") and not k.endswith("_order"))
        ]
        self.driver_dimension_keys = [
            k for k in self.categorical_fields 
            if k in _DRIVER_DIMENSION_KEYS or (k != self.temporal_key and k not in _TEMPORAL_KEYS and k not in _METADATA_KEYS and not k.endswith("_order") and not k.endswith("_id"))
        ]
        
        # Computed Analytical Insights
        self.total_rows_count = len(self.rows)
        self.outcome_summary = self._analyze_outcome_and_quantification()
        self.temporal_summary = self._analyze_temporal_pattern()
        self.driver_summary = self._analyze_drivers_and_attribution()

    def _extract_first_cap_id(self) -> str:
        caps = self.plan.get("business_capabilities") or []
        if caps and isinstance(caps[0], dict):
            return caps[0].get("id", "")
        return ""

    def _extract_authoritative_rows(self) -> List[dict]:
        rows = []
        for tr in self.tool_results:
            if not isinstance(tr, dict):
                continue
            inner = tr.get("result") if isinstance(tr.get("result"), dict) else tr
            payload = inner.get("data") if isinstance(inner.get("data"), dict) else inner
            found_rows = False
            for container in (payload, inner, tr):
                if not isinstance(container, dict):
                    continue
                candidates = [
                    container.get("rows"),
                    container.get("data"),
                    container.get("ranking_data"),
                    container.get("breakdown"),
                    container.get("revenue_by_month"),
                    container.get("gp_performance_breakdown"),
                    container.get("monthly_variance"),
                    container.get("items")
                ]
                for cand in candidates:
                    if isinstance(cand, list) and cand and isinstance(cand[0], dict):
                        rows.extend(cand)
                        found_rows = True
                        break
                if found_rows:
                    break
            if not found_rows:
                if isinstance(inner.get("data"), list) and inner["data"] and isinstance(inner["data"][0], dict):
                    rows.extend(inner["data"])
                elif isinstance(tr.get("data"), list) and tr["data"] and isinstance(tr["data"][0], dict):
                    rows.extend(tr["data"])
                elif isinstance(tr, list) and tr and isinstance(tr[0], dict):
                    rows.extend(tr)

        # If no list rows were found, check for top-level scalar summary metrics
        if not rows:
            for tr in self.tool_results:
                if not isinstance(tr, dict):
                    continue
                inner = tr.get("result") if isinstance(tr.get("result"), dict) else tr
                payload = inner.get("data") if isinstance(inner.get("data"), dict) else inner
                res = payload if isinstance(payload, dict) else inner
                if isinstance(res, dict):
                    perf = res.get("total_revenue_ytd") or res.get("total_performing_revenue") or res.get("actual_gp") or res.get("performing")
                    tgt = res.get("total_target_budget") or res.get("total_kpi_target") or res.get("target_gp") or res.get("target")
                    var = res.get("budget_variance") or res.get("variance")
                    period = res.get("month") or res.get("period") or res.get("temporal_scope")
                    if perf is not None or tgt is not None or var is not None:
                        synth_row = {
                            "period": str(period) if period else "Current Period",
                            "performing": float(perf) if perf is not None else 0.0,
                            "target": float(tgt) if tgt is not None else 0.0,
                            "variance": float(var) if var is not None else (float(perf or 0.0) - float(tgt or 0.0))
                        }
                        if res.get("month"):
                            synth_row["month"] = res.get("month")
                        rows.append(synth_row)
                        break
        return rows

    def _discover_numeric_fields(self) -> List[str]:
        if not self.rows:
            return []
        first_row = self.rows[0]
        numeric_keys = []
        for k, v in first_row.items():
            if k.startswith("_"):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numeric_keys.append(k)
        return numeric_keys

    def _discover_categorical_fields(self) -> List[str]:
        if not self.rows:
            return []
        first_row = self.rows[0]
        cat_keys = []
        for k, v in first_row.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str):
                cat_keys.append(k)
        return cat_keys

    def _find_matching_key(self, key_set: set) -> Optional[str]:
        if not self.rows:
            return None
        first_row = self.rows[0]
        for k in first_row.keys():
            if k.lower() in key_set:
                return k
        return None

    def _analyze_outcome_and_quantification(self) -> dict:
        """
        LEVEL 1 & LEVEL 2: Identify outcome & quantify deviations.
        """
        if not self.rows:
            return {
                "status": "NO_DATA",
                "is_negative_variance": False,
                "total_performing": None,
                "total_target": None,
                "total_variance": None,
                "pct_variance": None,
                "shortfall_amount": None,
                "shortfall_pct": None,
                "outcome_label": "NO_DATA_AVAILABLE"
            }

        total_perf = 0.0
        total_tgt = 0.0
        total_var = 0.0
        has_perf = bool(self.performing_key)
        has_tgt = bool(self.target_key)
        has_var = bool(self.variance_key)

        for row in self.rows:
            p_val = float(row.get(self.performing_key, 0.0)) if has_perf else 0.0
            t_val = float(row.get(self.target_key, 0.0)) if has_tgt else 0.0
            if has_var:
                v_val = float(row.get(self.variance_key, 0.0))
            elif has_perf and has_tgt:
                v_val = p_val - t_val
            else:
                v_val = 0.0

            total_perf += p_val
            total_tgt += t_val
            total_var += v_val

        pct = (total_var / total_tgt * 100.0) if total_tgt > 0 else 0.0
        is_negative = total_var < 0
        abs_var = abs(total_var)
        abs_pct = abs(pct)

        return {
            "status": "OUTCOME_IDENTIFIED",
            "is_negative_variance": is_negative,
            "total_performing": round(total_perf, 2),
            "total_target": round(total_tgt, 2),
            "total_variance": round(total_var, 2),
            "pct_variance": round(pct, 2),
            "shortfall_amount": round(abs_var, 2) if is_negative else 0.0,
            "shortfall_pct": round(abs_pct, 2) if is_negative else 0.0,
            "outcome_label": "NEGATIVE_VARIANCE_SHORTFALL" if is_negative else ("SURPLUS" if total_var > 0 else "ON_TRACK")
        }

    def _analyze_temporal_pattern(self) -> dict:
        """
        LEVEL 3: Analyze temporal pattern (persistent vs isolated, worsening/improving, peak shortfall).
        """
        if not self.rows or not self.temporal_key:
            return {"has_temporal_pattern": False, "classification": "UNDETERMINED"}

        negative_periods = []
        positive_periods = []
        period_details = []

        for row in self.rows:
            p_label = str(row.get(self.temporal_key, ""))
            p_val = float(row.get(self.performing_key, 0.0)) if self.performing_key else 0.0
            t_val = float(row.get(self.target_key, 0.0)) if self.target_key else 0.0
            if self.variance_key:
                v_val = float(row.get(self.variance_key, 0.0))
            elif self.performing_key and self.target_key:
                v_val = p_val - t_val
            else:
                v_val = 0.0

            item = {
                "period": p_label,
                "performing": round(p_val, 2),
                "target": round(t_val, 2),
                "variance": round(v_val, 2),
                "is_negative": v_val < 0
            }
            period_details.append(item)
            if v_val < 0:
                negative_periods.append(item)
            else:
                positive_periods.append(item)

        total_p = len(period_details)
        neg_count = len(negative_periods)

        if total_p == 0:
            classification = "NO_PERIODS"
        elif neg_count == total_p or (total_p > 1 and neg_count / total_p >= 0.8):
            classification = "PERSISTENT"
        elif neg_count == 1 and total_p > 1:
            classification = "ISOLATED"
        elif neg_count > 0:
            classification = "MIXED"
        else:
            classification = "FULLY_ON_TRACK"

        # Check trend (WORSENING vs IMPROVING among negative periods)
        trend = "STABLE"
        if len(negative_periods) >= 2:
            var_list = [abs(p["variance"]) for p in negative_periods]
            is_increasing = all(var_list[i] <= var_list[i+1] for i in range(len(var_list)-1))
            is_decreasing = all(var_list[i] >= var_list[i+1] for i in range(len(var_list)-1))
            if is_increasing and not is_decreasing:
                trend = "WORSENING"
            elif is_decreasing and not is_increasing:
                trend = "IMPROVING"

        # Peak shortfall period
        peak_shortfall = min(period_details, key=lambda x: x["variance"]) if period_details else None
        peak_surplus = max(period_details, key=lambda x: x["variance"]) if period_details else None

        return {
            "has_temporal_pattern": True,
            "temporal_key": self.temporal_key,
            "total_periods": total_p,
            "negative_period_count": neg_count,
            "positive_period_count": len(positive_periods),
            "classification": classification,
            "trend": trend,
            "peak_shortfall_period": peak_shortfall,
            "peak_surplus_period": peak_surplus,
            "period_details": period_details
        }

    def _analyze_drivers_and_attribution(self) -> dict:
        """
        LEVEL 4 & LEVEL 5: Investigate drivers & attribute causes using evidence.
        LEVEL 7: Record explicit limitations if driver fields are absent.
        """
        if not self.rows:
            return {"driver_data_available": False, "evidence_limitations": "No data available."}

        # Check if driver metrics or secondary dimensions exist
        has_driver_metrics = len(self.driver_metric_keys) > 0
        has_driver_dimensions = len(self.driver_dimension_keys) > 0

        driver_findings = []
        if has_driver_metrics or has_driver_dimensions:
            for row in self.rows:
                row_findings = {}
                for dk in self.driver_dimension_keys:
                    row_findings[dk] = row.get(dk)
                for mk in self.driver_metric_keys:
                    row_findings[mk] = row.get(mk)
                if row_findings:
                    driver_findings.append(row_findings)

        driver_data_available = len(driver_findings) > 0

        missing_driver_types = []
        if "revenue" not in [k.lower() for k in self.numeric_fields]:
            missing_driver_types.append("revenue breakdown")
        if "cost" not in [k.lower() for k in self.numeric_fields] and "actual_cost" not in [k.lower() for k in self.numeric_fields]:
            missing_driver_types.append("cost breakdown")
        if not has_driver_dimensions:
            missing_driver_types.append("service-line / department / project contributions")

        limitation_note = (
            f"The available dataset confirms the performing vs target metrics, but does NOT expose "
            f"{', '.join(missing_driver_types) if missing_driver_types else 'deeper driver details'} "
            f"needed to conclusively state the underlying root cause."
        ) if not driver_data_available or missing_driver_types else None

        return {
            "driver_data_available": driver_data_available,
            "driver_metric_keys": self.driver_metric_keys,
            "driver_dimension_keys": self.driver_dimension_keys,
            "driver_findings_count": len(driver_findings),
            "evidence_limitations": limitation_note
        }

    def format_as_prompt_instructions(self) -> str:
        """
        Formats computed analytical evidence into a structured system prompt block.
        """
        out = self.outcome_summary
        temp = self.temporal_summary
        drv = self.driver_summary

        # Discover any concrete entity scope from plan / context
        scoped_entity_name = None
        plan_ctx = self.plan.get("context", {}) if isinstance(self.plan.get("context"), dict) else {}
        scoped_entity_name = plan_ctx.get("customer_name") or plan_ctx.get("entity_name") or self.plan.get("customer_name")
        if not scoped_entity_name:
            for ent in self.plan.get("resolved_entities", []):
                if isinstance(ent, dict) and ent.get("resolved_name"):
                    scoped_entity_name = ent["resolved_name"]
                    break

        if not self.rows or out.get("status") == "NO_DATA":
            lines = [
                "================================================================================",
                "AUTHORITATIVE ANALYTICAL EVIDENCE & PRE-COMPUTED METRICS (INTERNAL REASONING CONTEXT)",
                "================================================================================",
                f"• Capability / Metric: {self.capability} | Metric: {self.requested_metric} | Dimension: {self.requested_dimension}",
                f"• Outcome Summary: Status=NO_DATA_AVAILABLE | Records Found=0"
            ]
            if scoped_entity_name:
                lines.append(f"• Requested Entity Scope: Customer '{scoped_entity_name}'.")
                lines.append(f"• Data Status: The CRM database returned NO authoritative records or billing data for customer '{scoped_entity_name}' in the selected period.")
                lines.append(f"• MANDATE: Explicitly state that no authoritative records are available for '{scoped_entity_name}' in the CRM dataset for the selected period, and therefore performance cannot be evaluated against targets. NEVER state 0.00 variance, 0.0% variance, or 'across all customers'. NEVER assume or infer poor performance when no records exist. NEVER use prior organization-wide or GP context.")
            else:
                lines.append("• Data Status: No authoritative records were returned for the requested criteria in the selected period.")
                lines.append("• MANDATE: State clearly that no records are available in the CRM dataset for the requested criteria. Do NOT state 0.00 variance or invent figures.")
            lines.append("================================================================================")
            return "\n".join(lines)

        lines = [
            "================================================================================",
            "AUTHORITATIVE ANALYTICAL EVIDENCE & PRE-COMPUTED METRICS (INTERNAL REASONING CONTEXT)",
            "================================================================================"
        ]
        if scoped_entity_name:
            lines.append(f"• Scoped Entity: {scoped_entity_name}")
        lines.append(f"• Capability / Metric: {self.capability} | Metric: {self.requested_metric} | Dimension: {self.requested_dimension}")

        if out.get("is_negative_variance"):
            lines.append(
                f"• Outcome Summary: Status=NEGATIVE_VARIANCE_SHORTFALL | "
                f"Total Performing=BHD {out.get('total_performing', 0):,.2f} | "
                f"Total Target=BHD {out.get('total_target', 0):,.2f} | "
                f"Total Variance=BHD {out.get('total_variance', 0):,.2f} ({out.get('pct_variance', 0)}%) | "
                f"Authoritative Shortfall=BHD {out.get('shortfall_amount', 0):,.2f} ({out.get('shortfall_pct', 0)}% shortfall)"
            )
        else:
            lines.append(
                f"• Outcome Summary: Status={out.get('outcome_label')} | "
                f"Total Performing=BHD {out.get('total_performing', 0):,.2f} | "
                f"Total Target=BHD {out.get('total_target', 0):,.2f} | "
                f"Total Variance=BHD {out.get('total_variance', 0):,.2f} ({out.get('pct_variance', 0)}%)"
            )

        if temp.get("has_temporal_pattern"):
            lines.append(f"• Temporal Pattern: Classification={temp.get('classification')} | Trend={temp.get('trend')} | Affected Periods={temp.get('negative_period_count')}/{temp.get('total_periods')}")
            ps = temp.get("peak_shortfall_period")
            if ps:
                lines.append(f"  - Peak Shortfall Period: {ps.get('period')} (Performing: BHD {ps.get('performing'):,.2f} | Target: BHD {ps.get('target'):,.2f} | Variance: BHD {ps.get('variance'):,.2f})")

        if drv.get("driver_data_available"):
            lines.append(f"• Secondary Driver Data: AVAILABLE ({drv.get('driver_findings_count')} driver records found). Attribute shortfalls using this evidence.")
        else:
            lines.append(f"• Secondary Driver Data: NOT AVAILABLE in dataset. Evidence Limitation Note: {drv.get('evidence_limitations')}")

        lines.append("================================================================================")
        return "\n".join(lines)


def build_analytical_context(tool_results: List[dict], plan: Optional[dict] = None) -> AnalyticalContext:
    return AnalyticalContext(tool_results, plan)
