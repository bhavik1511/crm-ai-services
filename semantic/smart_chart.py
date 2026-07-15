"""
smart_chart.py — Auto-generates ApexCharts-compatible chart_data from SQL query results.

Rules:
- Category + single value (≤10 items) → donut
- Category + single value (>10 items) → bar
- Time/date + value → area (line chart)
- Category + 2+ values → grouped bar
- Single number → NO chart
- Record details (single row, many columns) → NO chart
- List of records → NO chart
"""

import re
from typing import Optional, Dict, List, Any


# Common date/time column name patterns
_DATE_PATTERNS = re.compile(
    r'(date|month|year|quarter|period|week|day|time|created_at|updated_at|fy|fiscal)',
    re.IGNORECASE
)

# Common category column name patterns
_CATEGORY_PATTERNS = re.compile(
    r'(name|type|status|category|department|service|line|designation|group|bucket|role|label)',
    re.IGNORECASE
)

# Common numeric value column name patterns
_VALUE_PATTERNS = re.compile(
    r'(count|total|sum|amount|value|revenue|budget|cost|fee|salary|rate|percentage|avg|average|hours|days|score)',
    re.IGNORECASE
)


def _is_numeric(val: Any) -> bool:
    """Check if a value is numeric (int or float)."""
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str):
        try:
            float(val.replace(',', ''))
            return True
        except ValueError:
            return False
    return False


def _classify_columns(rows: List[Dict]) -> Dict[str, List[str]]:
    """
    Classify columns into categories: date_cols, category_cols, value_cols.
    Uses both naming patterns and actual data types.
    """
    if not rows:
        return {"date_cols": [], "category_cols": [], "value_cols": []}
    
    first_row = rows[0]
    date_cols = []
    category_cols = []
    value_cols = []
    
    for col_name, val in first_row.items():
        # Skip ID columns
        if col_name.lower() == 'id' or col_name.lower().endswith('_id'):
            continue
        
        # Check by name pattern first
        if _DATE_PATTERNS.search(col_name):
            date_cols.append(col_name)
        elif _VALUE_PATTERNS.search(col_name):
            value_cols.append(col_name)
        elif _CATEGORY_PATTERNS.search(col_name):
            category_cols.append(col_name)
        # Fallback: check by data type
        elif _is_numeric(val):
            # Check if ALL values in this column are numeric
            all_numeric = all(_is_numeric(r.get(col_name)) for r in rows)
            if all_numeric:
                value_cols.append(col_name)
            else:
                category_cols.append(col_name)
        else:
            category_cols.append(col_name)
    
    return {
        "date_cols": date_cols,
        "category_cols": category_cols,
        "value_cols": value_cols,
    }


def generate_chart_data(
    rows: List[Dict],
    query_text: str = "",
    force_type: Optional[str] = None
) -> Optional[Dict]:
    """
    Analyze SQL query results and generate ApexCharts-compatible chart_data.
    
    Returns None if the data doesn't warrant a chart.
    Returns a dict like:
    {
        "type": "bar" | "donut" | "pie" | "area",
        "title": "...",
        "categories": [...],
        "series": [...],  # For bar/area: [{name, data}], for pie/donut: [values]
    }
    """
    if not rows:
        return None
    
    # Rule: Single row with many columns = record detail → NO chart
    if len(rows) == 1 and len(rows[0]) > 3:
        return None
    
    # Rule: Single number result → NO chart
    if len(rows) == 1 and len(rows[0]) <= 2:
        values = list(rows[0].values())
        if all(_is_numeric(v) for v in values):
            return None
    
    # Rule: Too many rows without clear structure → NO chart  
    if len(rows) > 30:
        return None
    
    classified = _classify_columns(rows)
    date_cols = classified["date_cols"]
    category_cols = classified["category_cols"]
    value_cols = classified["value_cols"]
    
    # Need at least one value column for any chart
    if not value_cols:
        return None
    
    # ── Time series chart (area) ──
    if date_cols and value_cols and len(rows) >= 2:
        date_col = date_cols[0]
        val_col = value_cols[0]
        title = _generate_title(val_col, date_col, query_text)
        
        return {
            "type": "area",
            "title": title,
            "categories": [str(r.get(date_col, "")) for r in rows],
            "series": [
                {
                    "name": _humanize(val_col),
                    "data": [_safe_float(r.get(val_col, 0)) for r in rows]
                }
            ],
        }
    
    # ── Category + values ──
    if category_cols and value_cols and len(rows) >= 2:
        cat_col = category_cols[0]
        
        # Grouped bar (2+ value columns)
        if len(value_cols) >= 2 and force_type != "donut":
            title = _generate_title(None, cat_col, query_text)
            return {
                "type": "bar",
                "title": title,
                "categories": [str(r.get(cat_col, "")) for r in rows],
                "series": [
                    {
                        "name": _humanize(vc),
                        "data": [_safe_float(r.get(vc, 0)) for r in rows]
                    }
                    for vc in value_cols[:4]  # Max 4 series
                ],
            }
        
        # Single value column
        val_col = value_cols[0]
        title = _generate_title(val_col, cat_col, query_text)
        
        if len(rows) <= 8:
            # Donut for small datasets
            return {
                "type": "donut",
                "title": title,
                "categories": [str(r.get(cat_col, "")) for r in rows],
                "series": [_safe_float(r.get(val_col, 0)) for r in rows],
            }
        else:
            # Bar for larger datasets
            return {
                "type": "bar",
                "title": title,
                "categories": [str(r.get(cat_col, "")) for r in rows],
                "series": [
                    {
                        "name": _humanize(val_col),
                        "data": [_safe_float(r.get(val_col, 0)) for r in rows]
                    }
                ],
            }
    
    return None


def _safe_float(val: Any) -> float:
    """Safely convert a value to float."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return 0.0


def _humanize(col_name: str) -> str:
    """Convert column_name to Human Name."""
    return col_name.replace('_', ' ').title()


def _generate_title(val_col: Optional[str], group_col: str, query_text: str) -> str:
    """Generate a human-readable chart title."""
    if val_col:
        return f"{_humanize(val_col)} by {_humanize(group_col)}"
    return f"Comparison by {_humanize(group_col)}"
