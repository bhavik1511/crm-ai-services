import json
import traceback
from semantic_layer import get_revenue_metrics

try:
    print("--- TESTING REVENUE METRICS 12-MONTH OUTPUT ---")
    # This calls the tool directly to see the exact JSON output
    # Since it's a LangChain tool, we have to invoke it or call its function
    result_str = get_revenue_metrics.invoke({"start_date": "2025-10-01", "end_date": "2026-09-30 23:59:59"})
    data = json.loads(result_str)
    
    months = data.get("revenue_by_month", [])
    print(f"\nTotal Months Returned: {len(months)}")
    for m in months:
        print(f"{m['month']}: {m['amount']}")
        
except Exception as e:
    print(traceback.format_exc())
