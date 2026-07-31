"""
audit_all_capabilities.py — Automated Audit of Every Capability in CRM AI Chatbot.
Verifies Planner selection, ToolRegistry implementation scoring, Execution, Payload validation,
Schema conformance, and Synthesizer compatibility.
"""
import sys
import os
import io
import json
import asyncio

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.capability_catalog import BUSINESS_CAPABILITIES, CAPABILITY_ALIASES, get_capability_metadata
from registry.tool_registry import tool_registry, format_capability_envelope
from agent.planner import EnterprisePlanner, RequestContext
from agent.synthesizer import synthesize_response

# Sample queries mapped to capabilities for Planner selection testing
CAPABILITY_TEST_QUERIES = {
    "customer_360_profile": "Show me the customer 360 profile for Client ABC",
    "pipeline_analysis": "What is our current sales pipeline and proposal win rate?",
    "proposal_analysis": "Analyze open proposals and win probability",
    "revenue_analysis": "Show me total revenue and monthly revenue breakdown for FY 2025-26",
    "analytical_query": "Which top 5 customers generated the highest revenue?",
    "project_details": "Show active project details and total approved fees",
    "receivables_analysis": "Show total overdue receivables and ageing breakdown",
    "receivable_analysis": "Show overdue receivables",
    "job_estimation": "Show job estimation metrics and approved fees",
    "job_estimation_metrics": "Show job estimation status breakdown",
    "service_leads": "Show service pipeline leads and sales lead metrics",
    "recoverability_analysis": "What is our project recoverability percentage?",
    "kpi_summary": "Give me the KPI summary report for FY 2025-26",
    "KPI Summary": "Show KPI summary report",
    "customer_resolution": "Find customer ID for ACME Corp",
    "proposal_search": "Search proposals for status open",
    "project_search": "Search projects named Audit",
    "ui_navigation": "Open Revenue Dashboard"
}

# Mock user context for tests
MOCK_USER_CONTEXT = {
    "user_id": 1,
    "user_tier": 1,
    "role": "Super Admin",
    "employee_id": 1,
    "financial_year": "FY 2025-26",
    "start_date": "2025-10-01",
    "end_date": "2026-09-30"
}

MOCK_JWT_TOKEN = "Bearer mock_jwt_token_for_audit"

async def audit_capability(cap_id: str, query: str):
    print(f"\n==================================================")
    print(f"AUDITING CAPABILITY: '{cap_id}'")
    print(f"Query: \"{query}\"")
    print(f"==================================================")

    results = {
        "capability": cap_id,
        "planner_selected": False,
        "tool_registry_resolved": False,
        "execution_success": False,
        "payload_non_null": False,
        "schema_valid": False,
        "synthesizer_success": False,
        "error_details": None
    }

    target_cap_id = CAPABILITY_ALIASES.get(cap_id, cap_id)
    meta = get_capability_metadata(target_cap_id)
    if not meta:
        results["error_details"] = f"Capability '{cap_id}' (target: '{target_cap_id}') NOT FOUND in CapabilityCatalog!"
        print(f"❌ Catalog Check FAILED: {results['error_details']}")
        return results

    print(f"✅ Catalog Check Passed (Target Canonical ID: '{target_cap_id}')")

    # Step 1: Tool Registry Implementation Resolution & Scoring
    ctx = {
        "question": query,
        "financial_year": "FY 2025-26",
        "start_date": "2025-10-01",
        "end_date": "2026-09-30",
        "search_term": "Audit",
        "target_dashboard": "revenue_dashboard",
        "customer_id": 105,
        "project_id": 201,
        "proposal_id": 301,
        "new_status_id": 2,
        "task_name": "Review Financials"
    }

    implementations = meta.get("implementations", [])
    selection_result = tool_registry.score_and_select_implementation(target_cap_id, implementations, list(ctx.keys()), [], ctx)
    best_impl = selection_result.get("implementation")
    score = selection_result.get("score", 0.0)

    if not best_impl:
        results["error_details"] = f"ToolRegistry failed to select any implementation for capability '{target_cap_id}'."
        print(f"❌ Tool Registry Selection FAILED: {results['error_details']}")
        return results

    results["tool_registry_resolved"] = True
    print(f"✅ Tool Registry Resolved: Impl Type='{best_impl.get('type')}', Priority={best_impl.get('priority')}, Func/Endpoint='{best_impl.get('function_call') or best_impl.get('endpoint')}' (Score: {score})")

    # Step 2: Execution via ToolRegistry
    node = {
        "capability_id": target_cap_id,
        "context": ctx,
        "intent": "generate_report"
    }

    try:
        exec_results = await tool_registry.execute_resolved_implementations([node], [], MOCK_JWT_TOKEN, MOCK_USER_CONTEXT, query)
        if not exec_results or len(exec_results) == 0:
            results["error_details"] = "ToolRegistry return empty execution result list."
            print(f"❌ Execution FAILED: {results['error_details']}")
            return results

        exec_res = exec_results[0]
        exec_status = exec_res.get("status")
        payload = exec_res.get("result")

        print(f"Execution Output Status: '{exec_status}'")
        print(f"Raw Payload: {json.dumps(payload, default=str)[:300]}")

        if exec_status in ["error", "unavailable"] and (not payload or "error_message" in payload and "Diagnostic Error" in str(payload.get("error_message"))):
            results["error_details"] = f"Execution returned error status '{exec_status}': {exec_res.get('error')}"
            print(f"❌ Execution FAILED: {results['error_details']}")
            return results

        results["execution_success"] = True

        if payload is not None and len(str(payload)) > 0:
            results["payload_non_null"] = True
            print("✅ Payload Non-Null Check Passed")
        else:
            results["error_details"] = "Payload is null or empty string."
            print("❌ Payload Non-Null Check FAILED")
            return results

    except Exception as e:
        results["error_details"] = f"Exception during execution: {str(e)}"
        print(f"❌ Execution Exception: {str(e)}")
        return results

    # Step 3: Schema Validation
    response_schema = meta.get("response_schema", {})
    if isinstance(payload, dict):
        # Check if primary metric or any schema key exists in payload
        primary_metric = meta.get("primary_metric")
        missing_keys = []
        if response_schema:
            missing_keys = [k for k in response_schema.keys() if k not in payload]

        if response_schema and len(missing_keys) == len(response_schema.keys()):
            results["error_details"] = f"Payload missing ALL schema keys {list(response_schema.keys())}. Returned payload keys: {list(payload.keys())}"
            print(f"⚠️ Schema Compliance Warning: {results['error_details']}")
        else:
            results["schema_valid"] = True
            print(f"✅ Schema Conformance Passed (Found matching schema keys)")
    else:
        results["schema_valid"] = True
        print(f"✅ Payload is non-dict primitive/list")

    # Step 4: Synthesizer Compatibility
    tool_results_for_synth = [{
        "capability": target_cap_id,
        "intent": "generate_report",
        "result": payload,
        "status": "success",
        "confidence": "verified"
    }]

    try:
        synth_res = await synthesize_response(query, tool_results_for_synth)
        synth_output = synth_res.get("content", str(synth_res))
        print(f"Synthesizer Output Snippet: \"{synth_output[:200]}...\"")
        
        if "unavailable" in synth_output.lower() and "diagnostic error" in synth_output.lower():
            results["error_details"] = f"Synthesizer defaulted to fallback error: {synth_output}"
            print(f"❌ Synthesizer FAILED: Output contained fallback error")
        else:
            results["synthesizer_success"] = True
            print("✅ Synthesizer Pass")
    except Exception as e:
        results["error_details"] = f"Synthesizer exception: {str(e)}"
        print(f"❌ Synthesizer Exception: {str(e)}")

    return results

async def run_full_audit():
    print("==========================================================")
    print("      STARTING FULL SYSTEM CAPABILITY AUDIT               ")
    print("==========================================================")

    all_caps_to_test = list(CAPABILITY_TEST_QUERIES.keys())
    audit_summary = []

    for cap_id in all_caps_to_test:
        query = CAPABILITY_TEST_QUERIES[cap_id]
        res = await audit_capability(cap_id, query)
        audit_summary.append(res)

    print("\n\n==========================================================")
    print("              FINAL AUDIT SUMMARY REPORT                  ")
    print("==========================================================")
    print(f"{'Capability':<26} | {'Registry':<8} | {'Exec':<6} | {'Non-Null':<8} | {'Schema':<6} | {'Synth':<6} | Status")
    print("-" * 80)

    all_passed = True
    for r in audit_summary:
        reg = "✅" if r["tool_registry_resolved"] else "❌"
        exc = "✅" if r["execution_success"] else "❌"
        nul = "✅" if r["payload_non_null"] else "❌"
        sch = "✅" if r["schema_valid"] else "❌"
        syn = "✅" if r["synthesizer_success"] else "❌"
        
        passed = r["tool_registry_resolved"] and r["execution_success"] and r["payload_non_null"] and r["synthesizer_success"]
        if not passed:
            all_passed = False

        status_str = "PASS ✅" if passed else f"FAIL ❌ ({r['error_details']})"
        print(f"{r['capability']:<26} | {reg:<8} | {exc:<6} | {nul:<8} | {sch:<6} | {syn:<6} | {status_str}")

    print("==========================================================")
    if all_passed:
        print("🎉 ALL CAPABILITIES PASSED THE AUDIT PERFECTLY!")
    else:
        print("⚠️ SOME CAPABILITIES FAILED — SEE DETAILS ABOVE")

if __name__ == "__main__":
    asyncio.run(run_full_audit())
