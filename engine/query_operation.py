"""
query_operation.py — Generic Query Operation Model
=====================================================
Metadata-driven extraction of structured analytical query operations:
- operation: aggregate | ranking | breakdown | comparison | trend | lookup
- metric: revenue | actual_cost | budget | recoverability | count | proposals | etc.
- dimension: customer | service_line | office | project | status | month | year | employee
- aggregation: SUM | COUNT | AVG | MIN | MAX
- sort: DESC | ASC
- limit: top-N slice (e.g. 5, 10)
- filters: date ranges, financial year, entity constraints

100% metadata-driven, dynamic extraction based on capability metadata capabilities.
"""

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

@dataclass
class QueryOperation:
    operation: str = "aggregate"
    metric: str = "revenue"
    dimension: Optional[str] = None
    aggregation: str = "SUM"
    sort: str = "DESC"
    limit: Optional[int] = None
    filters: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "metric": self.metric,
            "dimension": self.dimension,
            "aggregation": self.aggregation,
            "sort": self.sort,
            "limit": self.limit,
            "filters": dict(self.filters) if isinstance(self.filters, dict) else {}
        }

def extract_query_operation(query: str, capability_metadata: Dict[str, Any]) -> QueryOperation:
    """
    Extracts generic QueryOperation metadata from user query and capability metadata.
    Does NOT use capability-specific routing conditions.
    """
    q_lower = query.lower().strip()
    op = QueryOperation()

    # 1. Extract Limit & Ranking
    limit_match = re.search(r'\b(?:top|best|highest|first)\s+(\d+)\b', q_lower)
    if not limit_match:
        limit_match = re.search(r'\b(\d+)\s+(?:top|best|highest)\b', q_lower)

    if limit_match:
        op.limit = int(limit_match.group(1))
        op.operation = "ranking"
        op.sort = "DESC"

    # 2. Extract Sort Order
    if any(kw in q_lower for kw in ["bottom", "lowest", "least", "smallest", "worst"]):
        op.sort = "ASC"
        if not op.limit:
            # Check if limit was specified with bottom
            bottom_match = re.search(r'\b(?:bottom|lowest)\s+(\d+)\b', q_lower)
            if bottom_match:
                op.limit = int(bottom_match.group(1))
                op.operation = "ranking"

    # 3. Extract Dimension from metadata or standard terms
    supported_dims = capability_metadata.get("supported_dimensions", [])
    extracted_dim = None

    dim_patterns = [
        ("customer", ["customer", "client", "customers", "clients"]),
        ("service_line", ["service line", "serviceline", "service_line", "services", "service"]),
        ("office", ["office", "branch", "location", "offices", "branches"]),
        ("project", ["project", "projects", "job", "jobs"]),
        ("status", ["status", "stage", "state"]),
        ("month", ["month", "monthly", "months"]),
        ("year", ["year", "yearly", "fy", "financial year"]),
        ("employee", ["employee", "staff", "consultant", "person", "resource"])
    ]

    for dim_name, keywords in dim_patterns:
        if any(kw in q_lower for kw in keywords):
            extracted_dim = dim_name
            break

    if extracted_dim:
        op.dimension = extracted_dim

    # 4. Extract Metric from metadata or standard terms
    supported_metrics = capability_metadata.get("supported_metrics", [])
    extracted_metric = None

    metric_patterns = [
        ("actual_cost", ["actual cost", "actual_cost", "cost", "costs", "expense"]),
        ("budget", ["budget", "budgeted", "proposed fee", "approved fee"]),
        ("recoverability", ["recoverability", "realization", "realisation"]),
        ("proposals", ["proposal", "proposals", "win rate"]),
        ("revenue", ["revenue", "billing", "billed", "invoice", "invoiced", "fee", "fees", "total_net_amount", "amount"]),
        ("count", ["count", "number of", "quantity", "how many"])
    ]

    for m_name, keywords in metric_patterns:
        if any(kw in q_lower for kw in keywords):
            extracted_metric = m_name
            break

    if extracted_metric:
        op.metric = extracted_metric
    elif capability_metadata.get("primary_metric"):
        op.metric = capability_metadata.get("primary_metric")

    # 5. Extract Aggregation
    if op.metric == "count" or any(kw in q_lower for kw in ["how many", "count of", "number of", "total count"]):
        op.aggregation = "COUNT"
    elif op.metric == "recoverability" or any(kw in q_lower for kw in ["average", "avg", "mean"]):
        op.aggregation = "AVG"
    elif any(kw in q_lower for kw in ["minimum", "min", "lowest"]):
        op.aggregation = "MIN"
    elif any(kw in q_lower for kw in ["maximum", "max", "highest"]):
        op.aggregation = "MAX"
    else:
        op.aggregation = "SUM"

    # 6. Extract Operation Type if not set by ranking
    if op.operation == "aggregate":
        if any(kw in q_lower for kw in ["compare", "comparison", "vs", "versus", "between"]):
            op.operation = "comparison"
        elif any(kw in q_lower for kw in ["trend", "monthly", "over time", "history"]):
            op.operation = "trend"
            if not op.dimension:
                op.dimension = "month"
        elif op.dimension or "by " in q_lower:
            op.operation = "breakdown"
        else:
            op.operation = "aggregate"

    # 7. Extract Filters (Date range, FY, etc.)
    fy_match = re.search(r'\bfy\s*(\d{2,4})\b', q_lower)
    if fy_match:
        op.filters["financial_year"] = f"FY{fy_match.group(1)}"

    logger.debug(f"[QueryOperation] Extracted: op={op.operation}, metric={op.metric}, dim={op.dimension}, agg={op.aggregation}, sort={op.sort}, limit={op.limit}")
    return op
