import sys
import os
import json
import asyncio
import time
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Add root directory to sys path so we can import agent modules
sys.path.append(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv()

from agent.planner import EnterprisePlanner, RequestContext
from agent.diagnostics import DiagnosticsTracker

captured_traces = {}

# Monkeypatch dump_trace to capture the trace in memory
def mocked_dump_trace(self):
    captured_traces[self.session_id] = self.replay
    try:
        import logging
        logger = logging.getLogger("agent.diagnostics")
        logger.info(json.dumps(self.replay, indent=2, default=str))
    except Exception:
        pass

DiagnosticsTracker.dump_trace = mocked_dump_trace

async def run_validation_suite():
    print("=" * 60)
    print("Starting EnterprisePlanner Validation Suite")
    print("=" * 60)
    
    matrix_path = os.path.join(os.path.dirname(__file__), 'test_matrix.json')
    with open(matrix_path, 'r', encoding='utf-8') as f:
        test_matrix = json.load(f)
        
    planner = EnterprisePlanner()
    
    results = []
    total_tests = len(test_matrix)
    passed_count = 0
    
    for i, test in enumerate(test_matrix):
        test_id = test.get("test_id")
        query = test.get("query")
        print(f"\n[{i+1}/{total_tests}] Running {test_id}: '{query}'...")
        
        ctx = RequestContext(
            question=query,
            jwt_token="fake_token",
            session_id=test_id,
            history=[],
            user_context={},
            request_metadata={"is_internal": False},
            feature_flags={}
        )
        
        try:
            start_time = time.time()
            response = await planner.execute_turn(ctx)
            execution_time = round((time.time() - start_time) * 1000, 2)
            
            trace = captured_traces.get(test_id, {})
            
            # --- Assertions ---
            passed = True
            reasons = []
            
            planner_output = trace.get("planner_output", {})
            capabilities = planner_output.get("business_capabilities", [])
            actual_capability_id = capabilities[0].get("id") if capabilities else None
            actual_params = capabilities[0].get("context", {}) if capabilities else {}
            
            expected_capability = test.get("expected_capability")
            expected_params = test.get("expected_parameters", [])
            expect_clarification = test.get("expect_clarification", False)
            
            # 1. Capability Assertion
            if expected_capability and actual_capability_id != expected_capability:
                passed = False
                reasons.append(f"Capability mismatch: expected '{expected_capability}', got '{actual_capability_id}'")
                
            # 2. Parameters Assertion
            for param in expected_params:
                if param not in actual_params:
                    # check if the param is one of the abstract keys in CapabilityCallInfo (e.g. time_filter, metric, entity)
                    if not capabilities or not capabilities[0].get(param):
                        passed = False
                        reasons.append(f"Missing expected parameter: '{param}'")
                        
            # 3. Clarification Assertion
            actual_is_clarification = response.get("is_clarification", False)
            if expect_clarification and not actual_is_clarification:
                passed = False
                reasons.append("Expected clarification but execution proceeded.")
            elif not expect_clarification and actual_is_clarification:
                passed = False
                reasons.append(f"Unexpected clarification: {response.get('content')}")
                
            # 4. Navigation Assertion
            expected_nav = test.get("expected_navigation_action")
            if expected_nav:
                nav_target = None
                for res in trace.get("tool_execution_results", []):
                    if res.get("capability") == "ui_navigation":
                        nav_target = res.get("result", {}).get("target")
                if nav_target != expected_nav:
                    passed = False
                    reasons.append(f"Navigation mismatch: expected '{expected_nav}', got '{nav_target}'")
            
            confidence = planner_output.get("confidence_score", 0.0)
            
            if passed:
                passed_count += 1
                print(f"  [PASS] ({execution_time}ms | Conf: {confidence})")
            else:
                print(f"  [FAIL] ({execution_time}ms | Conf: {confidence})")
                for r in reasons:
                    print(f"     - {r}")
                    
            results.append({
                "test_id": test_id,
                "category": test.get("category"),
                "query": query,
                "passed": passed,
                "reasons": reasons,
                "expected_capability": expected_capability,
                "actual_capability": actual_capability_id,
                "confidence": confidence,
                "execution_time_ms": execution_time,
                "trace": trace
            })
            
        except Exception as e:
            import traceback
            print(f"  [ERROR]: {e}")
            traceback.print_exc()
            results.append({
                "test_id": test_id,
                "category": test.get("category"),
                "query": query,
                "passed": False,
                "reasons": [f"Exception: {str(e)}"],
                "expected_capability": test.get("expected_capability"),
                "actual_capability": "Exception",
                "confidence": 0.0,
                "execution_time_ms": 0,
                "trace": {}
            })
            
    # --- Generate Report ---
    generate_markdown_report(results, total_tests, passed_count)
    
    # --- Dump full diagnostics ---
    diag_path = os.path.join(os.path.dirname(__file__), 'validation_diagnostics.json')
    with open(diag_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, default=str)
        
    print("\n" + "=" * 60)
    print(f"[Complete] Validation Complete! {passed_count}/{total_tests} Passed.")
    print("=" * 60)
    
    if passed_count < total_tests:
        sys.exit(1)
        
def generate_markdown_report(results, total_tests, passed_count):
    report_path = os.path.join(os.path.dirname(__file__), 'validation_report.md')
    
    accuracy = round((passed_count / total_tests) * 100, 2) if total_tests > 0 else 0.0
    
    md = [
        "# Enterprise Planner Validation Report",
        f"**Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary Metrics",
        f"- **Total Tests**: {total_tests}",
        f"- **Passed**: {passed_count} ({accuracy}%)",
        f"- **Failed**: {total_tests - passed_count}",
        "",
        "## Detailed Results",
        "| Test ID | Category | Query | Status | Capability | Confidence | Time (ms) |",
        "|---|---|---|---|---|---|---|"
    ]
    
    for r in results:
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        md.append(f"| {r['test_id']} | {r['category']} | {r['query']} | {status} | {r['actual_capability']} | {r['confidence']} | {r['execution_time_ms']} |")
        
    md.append("")
    md.append("## Failure Analysis")
    failures = [r for r in results if not r["passed"]]
    if not failures:
        md.append("🎉 All tests passed!")
    else:
        for f in failures:
            md.append(f"### {f['test_id']} - {f['query']}")
            md.append(f"- **Expected Capability**: {f['expected_capability']}")
            md.append(f"- **Actual Capability**: {f['actual_capability']}")
            md.append("- **Reasons for failure**:")
            for reason in f["reasons"]:
                md.append(f"  - {reason}")
            md.append("")
            
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(md))

if __name__ == "__main__":
    asyncio.run(run_validation_suite())
