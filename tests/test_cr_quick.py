import json, traceback
from semantic_layer import get_comprehensive_customer_report

try:
    result = get_comprehensive_customer_report.invoke({"search_term": "3D INTERNATIONAL WLL"})
    data = json.loads(result)
    if "error" in data:
        print(f"ERROR: {data['error']}")
    else:
        print("SUCCESS")
        print(json.dumps(data.get('kpi_summary', {}), indent=2, default=str))
except Exception as e:
    traceback.print_exc()
