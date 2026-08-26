"""
Diagnose WHY the planner fails for question #5 — "Compare Audit and Tax revenue"
Runs the planner with full exception printing, no swallowing.
"""
import asyncio, sys, io, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(override=True)

import jwt as pyjwt
from datetime import datetime, timedelta

JWT_SECRET = os.getenv("JWT_SECRET_KEY", os.getenv("JWT_SECRET", "fortiuskey"))
payload = {"id":1,"user_id":1,"employee_id":1,"name":"Diag Bot","role":"Super Admin","exp":(datetime.utcnow()+timedelta(hours=8)).timestamp()}
token = pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")

from agent.planner import EnterprisePlanner, RequestContext
from semantic import semantic_layer
from config.role_tier_config import get_tier_for_role

semantic_layer._CRM_AUTH_TOKEN = token
semantic_layer.set_user_context({"employee_id":1,"user_tier":1,"role_name":"Super Admin","department_id":None})

user_context = {"user_id":1,"employee_id":1,"role":"Super Admin","role_name":"Super Admin","hierarchy_level":1,"department":"Management","department_id":None,"service_line_id":None,"user_name":"Diag Bot"}

QUESTIONS = [
    "Compare Audit and Tax revenue",
    "How many proposals are pending?",
    "Show recoverability report",
    "What are total receivables?",
    "Show KPI summary",
    "Which client has the highest revenue?",
]

async def main():
    for q in QUESTIONS:
        print(f"\n{'='*60}")
        print(f"Q: {q}")
        req_ctx = RequestContext(
            question=q, jwt_token=token, session_id="diag-session",
            history=[{"role":"user","content":q}],
            user_context=user_context,
            request_metadata={"is_internal":False},
            feature_flags={"is_stream":False}
        )
        # Directly call _generate_execution_plan to see the raw error
        planner = EnterprisePlanner()
        try:
            plan = await planner._generate_execution_plan(q)
            print(f"  Plan OK: capability={[c.get('id') for c in plan.business_capabilities]}")
            # Now try full turn
            result = await planner.execute_turn(req_ctx)
            ans = result.get("content","")[:200].replace('\n',' ')
            print(f"  Answer: {ans}")
        except Exception as e:
            import traceback
            print(f"  EXCEPTION: {type(e).__name__}: {e}")
            traceback.print_exc()
        await asyncio.sleep(1)

asyncio.run(main())
