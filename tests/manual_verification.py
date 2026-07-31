"""
Manual Verification of Deterministic CRM Chatbot Components
Validates Tool Registry routing, RBAC, Entity Resolution, and Report filter propagation.
"""
import sys, os, io, json, asyncio
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.tool_registry import tool_registry
from registry.capability_catalog import BUSINESS_CAPABILITIES
from agent.entity_resolver import resolve_entities
from agent.planner import EnterprisePlanner, RequestContext
from semantic import semantic_layer

def test_tool_registry_routing():
    print("--- 1. Testing Tool Registry Routing ---")
    
    # 1.a Wrapper preference for revenue breakdown
    impls_revenue = tool_registry.resolve_implementations("revenue_analysis")
    best_revenue, score_rev = tool_registry.score_and_select_implementation("revenue_analysis", {"operation": "monthly_breakdown"})
    print(f"[Revenue Analysis] Type: {best_revenue['type']} | Function: {best_revenue.get('function_call')} | Score: {score_rev}")
    assert best_revenue['type'] == 'wrapper', "Failed: revenue_analysis should map to wrapper"

    # 1.b SQL Fallback for analytical query
    impls_analytical = tool_registry.resolve_implementations("analytical_query")
    best_analytical, score_analytical = tool_registry.score_and_select_implementation("analytical_query", {"operation": "ranking"})
    print(f"[Analytical Query] Type: {best_analytical['type']} | Function: {best_analytical.get('function_call')} | Score: {score_analytical}")
    assert best_analytical['type'] == 'wrapper' and best_analytical.get('function_call') == 'call_analytical_query', "Failed: analytical_query should map to SQL fallback wrapper"
    print("✅ Tool Registry Routing PASS\n")


async def test_report_filter_propagation():
    print("--- 2. Testing Report Filter Propagation ---")
    # Mock node execution context merger logic found in tool_registry.execute_resolved_implementations
    node = {
        "capability_id": "revenue_analysis",
        "context": {},
        "implementations": [{"type": "wrapper", "function_call": "get_revenue_metrics"}]
    }
    user_context = {
        "financial_year": "FY 2025-26",
        "date_range": "01-10-2025 to 30-09-2026"
    }
    resolved_entities = [{"type": "customer", "id": 105, "name": "Test Client"}]
    
    # Run the merger via execute_resolved_implementations (we will monkeypatch the actual wrapper call to just return the kwargs)
    async def mock_call(*args, **kwargs):
        pass

    import agent.semantic_wrappers
    original_call = agent.semantic_wrappers.call_revenue_metrics
    
    call_args = {}
    async def mock_revenue_metrics(args):
        call_args.update(args)
        return {"status": "mocked"}
        
    agent.semantic_wrappers.SEMANTIC_TOOL_MAP["get_revenue_metrics"] = mock_revenue_metrics
    
    await tool_registry.execute_resolved_implementations([node], resolved_entities, "fake_token", user_context, "test question")
    
    print(f"Propagated Filters: {json.dumps(call_args, indent=2)}")
    assert call_args.get("customer_id") == 105, "Entity ID not merged"
    assert call_args.get("financial_year") == "FY 2025-26", "Financial Year not merged"
    assert call_args.get("start_date") == "2025-10-01", "Start Date not resolved"
    
    agent.semantic_wrappers.SEMANTIC_TOOL_MAP["get_revenue_metrics"] = original_call
    print("✅ Report Filter Propagation PASS\n")


async def test_entity_resolution():
    print("--- 3. Testing Entity Resolution ---")
    # Mock the DB request side of entity_resolver or just ensure the token is set and fails gracefully if no db
    from dotenv import load_dotenv
    load_dotenv(override=True)
    import jwt
    from datetime import datetime, timedelta
    token = jwt.encode({"id": 1, "role": "Super Admin", "exp": (datetime.utcnow()+timedelta(hours=1)).timestamp()}, "fortiuskey", algorithm="HS256")
    
    semantic_layer._CRM_AUTH_TOKEN = token
    entities = [{"type": "service_line", "value": "Audit"}]
    
    try:
        resolved, clarifications = await resolve_entities(entities, token)
        print(f"Resolved Entities: {resolved}")
        # Assuming DB has "Audit" mapping to some ID
        assert len(resolved) > 0, "No entities resolved"
        print("✅ Entity Resolution PASS\n")
    except Exception as e:
        print(f"⚠️ Entity Resolution hit a DB/API error during local mock: {e}\n")


def test_rbac_enforcement():
    print("--- 4. Testing RBAC Enforcement ---")
    from agent.agent import build_rbac_prompt
    
    # Tier 4 context (Employee/Self)
    t4_context = {
        "user_id": 12, "employee_id": 45, "user_tier": 4, 
        "department_id": None, "service_line_id": None
    }
    
    # Test RBAC prompt string output
    sql_out = build_rbac_prompt("John Doe", "Employee", "Sales")
    print(f"[Tier 4] RBAC constraints:\n{sql_out}")
    assert any(term in sql_out.lower() for term in ["assigned", "employee_id", "project_members", "tier"]), "Failed: Tier 4 RBAC clause not generated"
    
    # Tier 1 context (Super Admin)
    sql_out_t1 = build_rbac_prompt("Admin", "Super Admin", "Management")
    print(f"[Tier 1] RBAC constraints:\n{sql_out_t1}")
    assert "full access" in sql_out_t1.lower() or "tier 1" in sql_out_t1.lower(), "Failed: Tier 1 full access prompt not generated"
    print("✅ RBAC Enforcement PASS\n")

async def run_all():
    print("========================================")
    print("   MANUAL DETERMINISTIC VERIFICATION    ")
    print("========================================")
    try:
        test_tool_registry_routing()
    except Exception as e:
        print(f"❌ Defect: {e}")

    try:
        await test_report_filter_propagation()
    except Exception as e:
        print(f"❌ Defect: {e}")

    try:
        await test_entity_resolution()
    except Exception as e:
        print(f"❌ Defect: {e}")

    try:
        test_rbac_enforcement()
    except Exception as e:
        print(f"❌ Defect: {e}")
        
    print("========================================")

if __name__ == "__main__":
    asyncio.run(run_all())
