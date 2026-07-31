import asyncio, json, time, uuid, sys, os, re, io
from datetime import datetime, timedelta
from typing import Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AI_DEBUG_MODE"] = "True"

from dotenv import load_dotenv
load_dotenv(override=True)

import jwt as pyjwt

# ── Config ─────────────────────────────────────────────────────────────────
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "validation_results_v2.json")
MAX_BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
JWT_SECRET = os.getenv("JWT_SECRET_KEY", os.getenv("JWT_SECRET", "fortiuskey"))


def make_token(role: str = "Super Admin", emp_id: int = 1) -> str:
    payload = {
        "id": 1, "user_id": 1, "employee_id": emp_id,
        "name": "Validation Bot", "role": role,
        "isEmployeeLogin": False,
        "exp": (datetime.utcnow() + timedelta(hours=8)).timestamp(),
    }
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


# ── 100 test questions ─────────────────────────────────────────────────────
QUESTIONS = [
    # Revenue (1-15)
    (1,  "Revenue",      "What is the total revenue for this fiscal year?"),
    (2,  "Revenue",      "Show me revenue by month for FY 2025-26"),
    (3,  "Revenue",      "Which client has the highest revenue?"),
    (4,  "Revenue",      "Show top 5 customers by revenue"),
    (5,  "Revenue",      "Compare Audit and Tax revenue"),
    (6,  "Revenue",      "What is the revenue for October 2025?"),
    (7,  "Revenue",      "Show revenue trend for the last 6 months"),
    (8,  "Revenue",      "What is the total invoiced amount this month?"),
    (9,  "Revenue",      "Which service line generates the most revenue?"),
    (10, "Revenue",      "Show revenue for Advisory service line"),
    (11, "Revenue",      "What percentage of revenue comes from Audit?"),
    (12, "Revenue",      "Show revenue comparison between this year and last year"),
    (13, "Revenue",      "What is the average monthly revenue for FY 2025-26?"),
    (14, "Revenue",      "Show me the bottom 3 service lines by revenue"),
    (15, "Revenue",      "Total revenue billed in Q2 of this fiscal year"),
    # Service Leads (16-25)
    (16, "ServiceLeads", "How many service leads were created this month?"),
    (17, "ServiceLeads", "Show open service leads"),
    (18, "ServiceLeads", "How many leads are in pipeline?"),
    (19, "ServiceLeads", "What is the total pipeline value of open leads?"),
    (20, "ServiceLeads", "Show service leads created this fiscal year by status"),
    (21, "ServiceLeads", "Which employee owns the most service leads?"),
    (22, "ServiceLeads", "How many leads were converted to proposals?"),
    (23, "ServiceLeads", "Show leads by service line"),
    (24, "ServiceLeads", "What is the total budget value of all leads this FY?"),
    (25, "ServiceLeads", "Show leads created in the last 30 days"),
    # Customers (26-35)
    (26, "Customers",    "How many active customers do we have?"),
    (27, "Customers",    "Which customers have overdue invoices?"),
    (28, "Customers",    "Show all customers in the Construction industry"),
    (29, "Customers",    "Which customer has the most projects?"),
    (30, "Customers",    "Show customers with no invoices this year"),
    (31, "Customers",    "List the top 3 customers by number of proposals"),
    (32, "Customers",    "Which customer has been with us the longest?"),
    (33, "Customers",    "Show me all customers with pending proposals"),
    (34, "Customers",    "How many new customers were onboarded this fiscal year?"),
    (35, "Customers",    "Which customers have the highest outstanding receivables?"),
    # Projects (36-45)
    (36, "Projects",     "How many active projects do we have?"),
    (37, "Projects",     "Show projects by status"),
    (38, "Projects",     "Which project has the highest approved fees?"),
    (39, "Projects",     "Show all Audit projects"),
    (40, "Projects",     "How many projects are overdue?"),
    (41, "Projects",     "Show projects ending this month"),
    (42, "Projects",     "Which projects are assigned to Audit service line?"),
    (43, "Projects",     "Show me projects with no timesheet entries"),
    (44, "Projects",     "List the top 5 projects by approved fees"),
    (45, "Projects",     "How many projects were completed this fiscal year?"),
    # Recoverability (46-52)
    (46, "Recoverability","Show recoverability report"),
    (47, "Recoverability","What is the average project recoverability?"),
    (48, "Recoverability","Show recoverability for Audit service line"),
    (49, "Recoverability","Which project has the lowest recoverability?"),
    (50, "Recoverability","Show recoverability trend for this fiscal year"),
    (51, "Recoverability","Compare estimated vs actual cost for all projects"),
    (52, "Recoverability","Which projects are over budget based on actual costs?"),
    # Receivables (53-60)
    (53, "Receivables",  "What are total receivables?"),
    (54, "Receivables",  "Show receivables aging breakdown"),
    (55, "Receivables",  "Which clients owe us money past 90 days?"),
    (56, "Receivables",  "What is the total overdue amount beyond 120 days?"),
    (57, "Receivables",  "Show top 5 customers by outstanding receivables"),
    (58, "Receivables",  "What percentage of invoices are unpaid?"),
    (59, "Receivables",  "Show invoices overdue more than 180 days"),
    (60, "Receivables",  "What is the total receivables for Audit service line?"),
    # Proposals (61-70)
    (61, "Proposals",    "How many proposals are pending?"),
    (62, "Proposals",    "Show open proposals"),
    (63, "Proposals",    "What is the total value of pending proposals?"),
    (64, "Proposals",    "How many proposals were approved this fiscal year?"),
    (65, "Proposals",    "Show proposals by status"),
    (66, "Proposals",    "Which service line has the most proposals?"),
    (67, "Proposals",    "Show proposals created in the last 3 months"),
    (68, "Proposals",    "What is the win rate for proposals?"),
    (69, "Proposals",    "Which proposals have been pending for more than 60 days?"),
    (70, "Proposals",    "Compare proposal approvals between Audit and Tax"),
    # Resource Utilization (71-77)
    (71, "Resources",    "Show resource utilization report for FY 2025-26"),
    (72, "Resources",    "What is the overall utilization rate?"),
    (73, "Resources",    "Which department has the lowest utilization?"),
    (74, "Resources",    "Show department utilization breakdown"),
    (75, "Resources",    "How many billable hours were logged this month?"),
    (76, "Resources",    "Which employees have zero billable hours this month?"),
    (77, "Resources",    "Show utilization trend by month for this fiscal year"),
    # KPI (78-83)
    (78, "KPI",          "Show KPI summary"),
    (79, "KPI",          "What is the budget vs actual revenue?"),
    (80, "KPI",          "How are we performing vs target?"),
    (81, "KPI",          "Show GP performance by service line"),
    (82, "KPI",          "What is the balance to achieve for this year?"),
    (83, "KPI",          "Show me service line performance breakdown"),
    # Pipeline (84-88)
    (84, "Pipeline",     "Show the sales pipeline"),
    (85, "Pipeline",     "What is the total pipeline value?"),
    (86, "Pipeline",     "How many deals are in the pipeline?"),
    (87, "Pipeline",     "Show pipeline by stage"),
    (88, "Pipeline",     "Which service line has the highest pipeline value?"),
    # Multi-filter (89-93)
    (89, "MultiFilter",  "Show revenue for Audit service line for FY 2025-26"),
    (90, "MultiFilter",  "How many projects in Tax service line are active?"),
    (91, "MultiFilter",  "Show proposals for Advisory created in January 2026"),
    (92, "MultiFilter",  "What is the receivable amount for Audit service line?"),
    (93, "MultiFilter",  "Show leads from internal source in this fiscal year"),
    # Ranking (94-96)
    (94, "Ranking",      "Show top 10 projects by actual hours logged"),
    (95, "Ranking",      "Which employee has the most approved timesheets?"),
    (96, "Ranking",      "Show the 3 service lines with highest open proposal value"),
    # Comparison (97-98)
    (97, "Comparison",   "Compare revenue this year vs last year"),
    (98, "Comparison",   "How does Audit compare to Advisory in terms of projects?"),
    # Trend (99-100)
    (99, "Trend",        "Show monthly revenue trend for the last 12 months"),
    (100,"Trend",        "What is the trend in new leads over the last 6 months?"),
]

# ── SQL log state ──────────────────────────────────────────────────────────
def _count_sql_blocks() -> int:
    try:
        with open("sql_debug.log", "r", encoding="utf-8", errors="ignore") as f:
            return f.read().count("--- NEW SQL QUERY")
    except Exception:
        return 0


def _last_sql_block() -> Optional[str]:
    try:
        with open("sql_debug.log", "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        blocks = content.split("--- NEW SQL QUERY")
        last = blocks[-1] if len(blocks) > 1 else ""
        m = re.search(r"SQL:\s*(.*?)(?:Result:|$)", last, re.DOTALL)
        return m.group(1).strip()[:600] if m else None
    except Exception:
        return None


# ── Rate-limit-aware retry helper ──────────────────────────────────────────
def _parse_retry_seconds(err_str: str) -> int:
    """Extract wait time in seconds from a Groq 429 error string."""
    m = re.search(r"try again in ([0-9]+)m([0-9.]+)s", err_str)
    if m:
        return int(m.group(1)) * 60 + int(float(m.group(2))) + 5
    m2 = re.search(r"try again in ([0-9.]+) ?seconds?", err_str, re.IGNORECASE)
    if m2:
        return int(float(m2.group(1))) + 5
    return 65  # safe default


# ── Core executor ──────────────────────────────────────────────────────────
async def run_question(idx: int, category: str, question: str,
                       token: str, session_id: str,
                       user_context: dict) -> dict:
    from agent.planner import EnterprisePlanner, RequestContext
    from semantic import semantic_layer
    from config.role_tier_config import get_tier_for_role

    semantic_layer._CRM_AUTH_TOKEN = token
    semantic_layer.set_user_context({
        "employee_id": 1, "user_tier": 1,
        "role_name": "Super Admin", "department_id": None,
    })

    history = [{"role": "user", "content": question}]
    req_ctx = RequestContext(
        question=question, jwt_token=token, session_id=session_id,
        history=history, user_context=user_context,
        request_metadata={"is_internal": False},
        feature_flags={"is_stream": False},
    )

    sql_before = _count_sql_blocks()
    t0 = time.time()

    # ── capture diagnostics output from AI_DEBUG_MODE ──────────────────────
    planner_output = {}
    capability_id = "unknown"
    registry_decision = "unknown"
    selected_impl = "unknown"
    sql_used = False
    generated_sql = None
    error_code = None
    retry_after = None

    try:
        planner = EnterprisePlanner()
        result = await planner.execute_turn(req_ctx)
        elapsed = round((time.time() - t0) * 1000)

        answer = result.get("content", "") or result.get("answer", "")
        error_code = result.get("error_code")
        retry_after = result.get("retry_after")

        # Detect SQL usage
        sql_after = _count_sql_blocks()
        if sql_after > sql_before:
            sql_used = True
            generated_sql = _last_sql_block()
            selected_impl = "sql_fallback"
        else:
            selected_impl = "api_or_wrapper"

        # Determine PASS / FAIL
        is_rate_limit = error_code == "rate_limit_exceeded" or "at capacity" in answer
        is_empty = not answer or len(answer.strip()) < 20
        is_error = (
            "encountered an error" in answer.lower()
            or "something went wrong" in answer.lower()
        ) and not is_rate_limit
        is_hallucination = any(
            k in answer for k in ["PRJ001", "Customer XYZ", "John Doe Revenue", "Project Alpha"]
        )

        if is_rate_limit:
            status = "RATE_LIMITED"
            passed = None  # Indeterminate — not a logic failure
            root_cause = f"Groq TPD/TPM rate limit. Retry in {retry_after or 'unknown'}"
        elif is_empty:
            status = "FAIL"
            passed = False
            root_cause = "Empty or too-short response"
        elif is_error:
            status = "FAIL"
            passed = False
            root_cause = "Error message in response"
        elif is_hallucination:
            status = "FAIL"
            passed = False
            root_cause = "Possible hallucinated entity"
        else:
            status = "PASS"
            passed = True
            root_cause = None

        print(f"[{idx:>3}] {status:<12} [{category:<14}] sql={str(sql_used):<5} {elapsed:>6}ms | {question[:55]}")
        if answer and status in ("PASS", "FAIL"):
            print(f"       {answer[:100].replace(chr(10), ' ')}")

        return {
            "test_id": idx, "category": category, "question": question,
            "planner_output": planner_output,
            "capability_id": capability_id,
            "registry_decision": registry_decision,
            "selected_impl": selected_impl,
            "sql_used": sql_used,
            "generated_sql": generated_sql,
            "elapsed_ms": elapsed,
            "answer": answer[:500],
            "status": status,
            "passed": passed,
            "root_cause": root_cause,
            "error_code": error_code,
            "retry_after": retry_after,
        }

    except Exception as e:
        elapsed = round((time.time() - t0) * 1000)
        err_str = str(e)
        is_429 = "429" in err_str or "rate_limit" in err_str.lower()
        if is_429:
            wait_s = _parse_retry_seconds(err_str)
            print(f"[{idx:>3}] RATE_LIMITED  [{category:<14}] 429 hit. Waiting {wait_s}s then retrying...")
            await asyncio.sleep(wait_s)
            # Retry once
            return await run_question(idx, category, question, token, session_id, user_context)

        print(f"[{idx:>3}] ERR           [{category:<14}] EXCEPTION: {err_str[:80]}")
        return {
            "test_id": idx, "category": category, "question": question,
            "planner_output": {}, "capability_id": "error",
            "registry_decision": "error", "selected_impl": "error",
            "sql_used": False, "generated_sql": None,
            "elapsed_ms": elapsed,
            "answer": f"EXCEPTION: {err_str[:300]}",
            "status": "ERROR", "passed": False,
            "root_cause": f"Exception: {err_str[:200]}",
            "error_code": "exception", "retry_after": None,
        }

# ── State Management ───────────────────────────────────────────────────────
def load_checkpoint() -> list[dict]:
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("results", [])
        except Exception:
            return []
    return []

def save_checkpoint(results: list[dict]):
    total = len(results)
    executed = [r for r in results if r["status"] != "RATE_LIMITED" and r["status"] != "ERROR"]
    passed = [r for r in executed if r["passed"]]
    failed = [r for r in executed if not r["passed"]]
    
    summary = {
        "run_at": datetime.now().isoformat(),
        "total_planned": 100,
        "total_executed": len(executed),
        "rate_limited": len([r for r in results if r["status"] == "RATE_LIMITED"]),
        "passed": len(passed),
        "failed": len(failed),
        "accuracy_pct": round(len(passed) / len(executed) * 100, 1) if executed else 0,
        "avg_ms": round(sum(r["elapsed_ms"] for r in executed) / len(executed)) if executed else 0,
        "sql_pct": round(len([r for r in executed if r["sql_used"]]) / len(executed) * 100) if executed else 0,
        "api_pct": round(len([r for r in executed if not r["sql_used"]]) / len(executed) * 100) if executed else 0,
        "results": results
    }
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    return summary


# ── Main ───────────────────────────────────────────────────────────────────
async def main():
    token = make_token("Super Admin", 1)
    session_id = str(uuid.uuid4())
    user_context = {
        "user_id": 1, "employee_id": 1,
        "role": "Super Admin", "role_name": "Super Admin",
        "hierarchy_level": 1, "department": "Management",
        "department_id": None, "service_line_id": None,
        "user_name": "Validation Bot",
    }

    print("=" * 72)
    print("  CRM AI - Resumable Validation Suite")
    print(f"  Max Batch Size: {MAX_BATCH_SIZE} questions")
    print("=" * 72)

    results = load_checkpoint()
    completed_ids = {r["test_id"] for r in results if r["status"] not in ("RATE_LIMITED", "ERROR")}
    
    pending_questions = [(idx, cat, q) for (idx, cat, q) in QUESTIONS if idx not in completed_ids]
    
    if not pending_questions:
        print("🎉 All 100 questions have been successfully executed!")
        return

    print(f"  Completed: {len(completed_ids)}/100 | Pending: {len(pending_questions)}")
    print("-" * 72)

    batch = pending_questions[:MAX_BATCH_SIZE]
    
    for (idx, category, question) in batch:
        r = await run_question(idx, category, question, token, session_id, user_context)
        
        # Remove any previous failed/rate-limited attempt for this ID before appending the new one
        results = [x for x in results if x["test_id"] != idx]
        results.append(r)
        
        # Always save immediately so we don't lose data
        save_checkpoint(results)

        if r.get("status") == "RATE_LIMITED":
            print(f"  [Rate limit hit] Quota exhausted. Stopping batch execution early.")
            break
            
        await asyncio.sleep(1.2)  # ~50 req/min cap buffer

    summary = save_checkpoint(results)
    
    print("\n" + "=" * 72)
    print("  VALIDATION PROGRESS")
    print("=" * 72)
    print(f"  Total Executed Overall : {summary['total_executed']}/100")
    print(f"  Passed                 : {summary['passed']}  [OK]")
    print(f"  Failed                 : {summary['failed']}  [FAIL]")
    print(f"  Accuracy               : {summary['accuracy_pct']}%")
    print(f"  Results saved -> {RESULTS_FILE}")
    print("=" * 72)

if __name__ == "__main__":
    asyncio.run(main())
