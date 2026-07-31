import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import json
from semantic.semantic_layer import get_pipeline_and_proposals, set_user_context

def test_user(emp, tier, dept=5):
    set_user_context({'employee_id': emp, 'user_tier': tier, 'role_name': 'Test', 'department_id': dept})
    res = asyncio.run(get_pipeline_and_proposals.ainvoke({}))
    data = json.loads(res) if isinstance(res, str) else res
    print(f"emp={emp}, tier={tier}, dept={dept} => win_rate={data.get('proposal_win_rate')}, total={data.get('total_proposals')}, won={data.get('won_proposals')}")

print("--- Testing get_pipeline_and_proposals ---")
for emp, tier, dept in [(None, 1, 17), (51, 1, 17), (50, 4, 5), (50, 5, 5), (10, 5, 2), (999, 5, 2)]:
    test_user(emp, tier, dept)
