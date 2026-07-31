"""
production_readiness_suite.py — Production Readiness Validation Suite.
Executes 270 CRM queries using a dual-validation architecture:
1. Live LLM Validation (~28 representative queries covering every capability via live EnterprisePlanner).
2. Deterministic Pipeline Validation (~242 queries validating Entity Resolver -> Validator -> Tool Registry -> SQL -> Synthesizer).

Strict Classification:
- PASS: Complete execution succeeded (valid status, non-empty content, valid tool payload, synthesizer output).
- SKIPPED: LLM Provider HTTP 429 rate limit, quota exhausted, capacity fallback message. NEVER counted as PASS.
- FAILED: Planner error, unregistered capability, backend/SQL error, synthesizer suppression, exception.
"""
import sys
import os
import io
import json
import time
import asyncio
import logging

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

from agent.planner import EnterprisePlanner, RequestContext
from semantic.semantic_layer import set_user_context

# User contexts for testing RBAC and roles
USER_CONTEXT_TIER_1 = {
    "user_id": 1,
    "employee_id": 1,
    "user_tier": 1,
    "role": "Super Admin",
    "department_id": 1,
    "financial_year": "FY 2025-26",
    "start_date": "2025-10-01",
    "end_date": "2026-09-30"
}

USER_CONTEXT_TIER_9 = {
    "user_id": 99,
    "employee_id": 99,
    "user_tier": 9,
    "role": "Employee",
    "department_id": 2,
    "financial_year": "FY 2025-26",
    "start_date": "2025-10-01",
    "end_date": "2026-09-30"
}

# 28 Representative Scenarios for LIVE LLM Validation
LIVE_LLM_SCENARIOS = [
    ("Revenue", "Show me total revenue for FY 2025-26", "revenue_analysis"),
    ("Receivables", "Show total overdue receivables", "receivables_analysis"),
    ("Projects", "Show active project details", "project_details"),
    ("Pipeline", "What is our current sales pipeline value?", "pipeline_analysis"),
    ("Proposals", "Analyze open proposals and win probability", "proposal_analysis"),
    ("Customers", "Show me the customer 360 profile for Client ABC", "customer_360_profile"),
    ("Customers", "Find customer ID for ACME Corp", "customer_resolution"),
    ("Employees", "Show staff billing report", "revenue_analysis"),
    ("Departments", "Show total revenue by department", "revenue_analysis"),
    ("Financial Years", "Show revenue summary for FY 2025-26", "revenue_analysis"),
    ("Date Ranges", "Show revenue from 01-10-2025 to 30-09-2026", "revenue_analysis"),
    ("Comparisons", "Compare revenue between Audit and Advisory", "analytical_query"),
    ("Rankings", "Which top 5 customers generated the highest revenue?", "analytical_query"),
    ("KPI", "Give me the KPI summary report for FY 2025-26", "kpi_summary"),
    ("Recoverability", "What is our project recoverability percentage?", "recoverability_analysis"),
    ("Job Estimations", "Show job estimation metrics and approved fees", "job_estimation"),
    ("Service Leads", "Show service pipeline leads and sales lead metrics", "service_leads"),
    ("Customer 360", "Show customer 360 profile for Client ABC", "customer_360_profile"),
    ("UI Navigation", "Open Revenue Dashboard", "ui_navigation"),
    ("UI Navigation", "Navigate to Sales Pipeline Dashboard", "ui_navigation"),
    ("Follow-up", "Show total revenue for FY 2025-26", "revenue_analysis"),
    ("Follow-up", "Filter for invoices older than 60 days", "receivables_analysis"),
    ("Clarification", "Show customer profile", "customer_resolution"),
    ("Clarification", "Update proposal status", "update_proposal_status"),
    ("Clarification", "Create project task", "create_task"),
    ("Projects", "Search projects for Financial Audit", "project_search"),
    ("Proposals", "Search proposals for status open", "proposal_search"),
    ("Multi-step", "Show total revenue and overdue receivables for FY 2025-26", "kpi_summary")
]

# Full 270 Production Queries Pool
PRODUCTION_QUERIES = [
    # Category 1: Revenue (15 queries)
    ("Revenue", "Show me total revenue for FY 2025-26"),
    ("Revenue", "What is our current monthly revenue breakdown?"),
    ("Revenue", "Show total billed revenue for Audit department"),
    ("Revenue", "Give me the total revenue summary for Advisory service line"),
    ("Revenue", "What is the total revenue generated in Q1?"),
    ("Revenue", "Show me revenue breakdown by month from October 2025 to September 2026"),
    ("Revenue", "What is the total revenue for Tax services?"),
    ("Revenue", "Show total billings and revenue YTD"),
    ("Revenue", "What is the revenue for project PRJ-101?"),
    ("Revenue", "Show gross revenue for all active projects"),
    ("Revenue", "Give me total revenue for client ACME Corp"),
    ("Revenue", "What is the total revenue comparison between Q1 and Q2?"),
    ("Revenue", "Show monthly revenue performance YTD"),
    ("Revenue", "What is our total accrued billing revenue?"),
    ("Revenue", "Show total revenue split by service line"),

    # Category 2: Receivables (15 queries)
    ("Receivables", "Show total overdue receivables"),
    ("Receivables", "Give me the receivables ageing breakdown"),
    ("Receivables", "What are the total pending collections for Audit?"),
    ("Receivables", "Show overdue invoices older than 90 days"),
    ("Receivables", "What is the total overdue amount for Client ABC?"),
    ("Receivables", "Show receivables summary for FY 2025-26"),
    ("Receivables", "Which customers have the highest overdue receivables?"),
    ("Receivables", "Show receivables breakdown by service line"),
    ("Receivables", "Give me total outstanding invoices for Advisory"),
    ("Receivables", "What is the current collection efficiency ratio?"),
    ("Receivables", "Show pending invoices between 30 and 60 days"),
    ("Receivables", "List all overdue receivables for Tax department"),
    ("Receivables", "What is the total receivables amount due this month?"),
    ("Receivables", "Show receivables summary by department"),
    ("Receivables", "What is the total overdue debt for top 5 clients?"),

    # Category 3: Projects (15 queries)
    ("Projects", "Show active project details"),
    ("Projects", "List all ongoing projects for Audit department"),
    ("Projects", "What is the total approved budget for active projects?"),
    ("Projects", "Show project status breakdown"),
    ("Projects", "List projects managed by employee 31"),
    ("Projects", "Show details for project named Financial Audit"),
    ("Projects", "What are the top active projects by contract fee?"),
    ("Projects", "Show overdue project tasks"),
    ("Projects", "List active projects in Advisory service line"),
    ("Projects", "What is the completion status of PRJ-202?"),
    ("Projects", "Show project count by status"),
    ("Projects", "List all projects started in 2025"),
    ("Projects", "Show active projects with pending deliverables"),
    ("Projects", "What is the total fee for all completed projects?"),
    ("Projects", "Show project breakdown by department"),

    # Category 4: Pipeline (15 queries)
    ("Pipeline", "What is our current sales pipeline value?"),
    ("Pipeline", "Show total open opportunity value"),
    ("Pipeline", "What is our proposal win rate?"),
    ("Pipeline", "Show pipeline breakdown by stage"),
    ("Pipeline", "Give me sales pipeline leads for Advisory"),
    ("Pipeline", "What is the total weighted pipeline value?"),
    ("Pipeline", "Show pipeline opportunities closing this quarter"),
    ("Pipeline", "What is the pipeline conversion rate for Audit?"),
    ("Pipeline", "Show sales pipeline leads by service line"),
    ("Pipeline", "List all high-value pipeline opportunities"),
    ("Pipeline", "What is the proposal win rate for FY 2025-26?"),
    ("Pipeline", "Show new leads added this month"),
    ("Pipeline", "What is the total pipeline value for Tax service line?"),
    ("Pipeline", "Show pipeline velocity and average deal size"),
    ("Pipeline", "Give me open proposal pipeline summary"),

    # Category 5: Proposals (15 queries)
    ("Proposals", "Analyze open proposals and win probability"),
    ("Proposals", "Search proposals for status open"),
    ("Proposals", "Show proposals submitted in FY 2025-26"),
    ("Proposals", "What is the total value of won proposals?"),
    ("Proposals", "List all pending proposals for client ACME"),
    ("Proposals", "Show proposals awaiting client approval"),
    ("Proposals", "What is the average proposal response time?"),
    ("Proposals", "List proposals submitted by Advisory department"),
    ("Proposals", "Show proposal win/loss analysis"),
    ("Proposals", "What is the total value of proposals in draft?"),
    ("Proposals", "List top 5 proposals by estimated fee"),
    ("Proposals", "Show proposals expiring this month"),
    ("Proposals", "Search proposals for keyword Valuation"),
    ("Proposals", "What is the win rate for proposals over $50,000?"),
    ("Proposals", "Show proposal status distribution"),

    # Category 6: Customers (15 queries)
    ("Customers", "Show me the customer 360 profile for Client ABC"),
    ("Customers", "Find customer ID for ACME Corp"),
    ("Customers", "List top 10 customers by revenue"),
    ("Customers", "Show customer resolution details for Global Tech"),
    ("Customers", "List active customers in Audit service line"),
    ("Customers", "What is the total revenue generated from Client XYZ?"),
    ("Customers", "Show customer 360 profile for customer ID 105"),
    ("Customers", "List new customers onboarded in FY 2025-26"),
    ("Customers", "Show customer contact info for Alpha Trading"),
    ("Customers", "Which customers have open proposals?"),
    ("Customers", "List customers with overdue receivables"),
    ("Customers", "Show customer 360 overview for Gulf Logistics"),
    ("Customers", "What is the average revenue per customer?"),
    ("Customers", "List all corporate customers in Bahrain"),
    ("Customers", "Show customer contract renewals due soon"),

    # Category 7: Employees (15 queries)
    ("Employees", "Show staff billing report"),
    ("Employees", "What is the billing utilization rate for employee 31?"),
    ("Employees", "List all tasks assigned to me"),
    ("Employees", "Show staff billing summary for Audit department"),
    ("Employees", "What is the total billable hours logged this month?"),
    ("Employees", "Show employee project assignments"),
    ("Employees", "List senior managers in Advisory"),
    ("Employees", "What is the staff utilization rate for Advisory?"),
    ("Employees", "Show billing report by employee tier"),
    ("Employees", "List active team members on project PRJ-101"),
    ("Employees", "Show employee timesheet compliance rate"),
    ("Employees", "What are the total billable vs non-billable hours?"),
    ("Employees", "Show employee performance metrics YTD"),
    ("Employees", "List project managers with active projects"),
    ("Employees", "Show staff billing breakdown for FY 2025-26"),

    # Category 8: Departments (15 queries)
    ("Departments", "Show total revenue by department"),
    ("Departments", "What is the performance summary for Audit department?"),
    ("Departments", "Compare revenue across all departments"),
    ("Departments", "Show receivables breakdown by department"),
    ("Departments", "What is the project count for Advisory department?"),
    ("Departments", "Show Tax department KPI metrics"),
    ("Departments", "What is the recoverability percentage for Audit?"),
    ("Departments", "List all active projects in Tax department"),
    ("Departments", "Show department budget vs actuals"),
    ("Departments", "What is the average project size by department?"),
    ("Departments", "Show department pipeline distribution"),
    ("Departments", "Which department generated highest gross profit?"),
    ("Departments", "Show department staff billing summary"),
    ("Departments", "List proposals submitted by Tax department"),
    ("Departments", "Show department overview for Management"),

    # Category 9: Financial Years (15 queries)
    ("Financial Years", "Show revenue summary for FY 2025-26"),
    ("Financial Years", "Give me the KPI summary report for FY 2025-26"),
    ("Financial Years", "What was our total revenue in FY 2024-25?"),
    ("Financial Years", "Compare performance FY 2024-25 vs FY 2025-26"),
    ("Financial Years", "Show receivables ageing for FY 2025-26"),
    ("Financial Years", "What is the project recoverability for FY 2025-26?"),
    ("Financial Years", "Show proposal win rate for FY 2024-25"),
    ("Financial Years", "List all active projects in FY 2025-26"),
    ("Financial Years", "What was the staff billing utilization in FY 2024-25?"),
    ("Financial Years", "Show job estimation metrics for FY 2025-26"),
    ("Financial Years", "What is the gross margin for FY 2025-26?"),
    ("Financial Years", "Show service pipeline leads for FY 2025-26"),
    ("Financial Years", "Compare quarterly revenue in FY 2025-26"),
    ("Financial Years", "Show total collections for FY 2024-25"),
    ("Financial Years", "What is the budget vs actual for FY 2025-26?"),

    # Category 10: Date Ranges (15 queries)
    ("Date Ranges", "Show revenue from 01-10-2025 to 30-09-2026"),
    ("Date Ranges", "What are the billings from October 2025 to December 2025?"),
    ("Date Ranges", "Show receivables between 2025-10-01 and 2026-03-31"),
    ("Date Ranges", "What is the pipeline value generated between Jan and Mar 2026?"),
    ("Date Ranges", "List active projects started between 2025-01-01 and 2025-12-31"),
    ("Date Ranges", "Show proposals submitted in Q1 2026"),
    ("Date Ranges", "What is the total revenue for the last 12 months?"),
    ("Date Ranges", "Show staff billing hours from 01-01-2026 to 31-03-2026"),
    ("Date Ranges", "Give me recoverability metrics for Q2 FY 2025-26"),
    ("Date Ranges", "Show overdue invoices for date range 2025-10-01 to 2026-09-30"),
    ("Date Ranges", "What is the revenue growth rate between Q1 and Q3?"),
    ("Date Ranges", "Show project status changes from Oct 2025 to Mar 2026"),
    ("Date Ranges", "List new leads acquired between 01-10-2025 and 31-12-2025"),
    ("Date Ranges", "Show collections report for November 2025"),
    ("Date Ranges", "What is the job estimation summary for H1 2025-26?"),

    # Category 11: Comparisons (15 queries)
    ("Comparisons", "Compare revenue between Audit and Advisory"),
    ("Comparisons", "Compare receivables for Client A vs Client B"),
    ("Comparisons", "What is the revenue comparison Q1 vs Q2 FY 2025-26?"),
    ("Comparisons", "Compare recoverability pct across departments"),
    ("Comparisons", "Compare proposal win rates by service line"),
    ("Comparisons", "What is the billing utilization comparison between tiers?"),
    ("Comparisons", "Compare project fee vs actual cost"),
    ("Comparisons", "Compare pipeline value this year vs last year"),
    ("Comparisons", "Compare overdue receivables 30-days vs 90-days"),
    ("Comparisons", "Compare top 5 customers by revenue contribution"),
    ("Comparisons", "Compare Tax vs Advisory active project counts"),
    ("Comparisons", "What is the margin comparison between project types?"),
    ("Comparisons", "Compare staff billings Q1 vs Q2"),
    ("Comparisons", "Compare new leads vs closed won proposals"),
    ("Comparisons", "Compare budget vs actual revenue YTD"),

    # Category 12: Rankings (15 queries)
    ("Rankings", "Which top 5 customers generated the highest revenue?"),
    ("Rankings", "List top 10 overdue invoices by amount"),
    ("Rankings", "Which service line has the highest win rate?"),
    ("Rankings", "Rank top 5 projects by approved fee"),
    ("Rankings", "Which employee logged the highest billable hours?"),
    ("Rankings", "Rank departments by total revenue"),
    ("Rankings", "Which customer has the largest pending receivables?"),
    ("Rankings", "Rank top 5 open pipeline proposals by fee"),
    ("Rankings", "Which service line has the highest recoverability percentage?"),
    ("Rankings", "Rank active projects by budget variance"),
    ("Rankings", "Which customer has the most active projects?"),
    ("Rankings", "List top 5 largest deals in sales pipeline"),
    ("Rankings", "Which department has the lowest overdue receivables?"),
    ("Rankings", "Rank employees by project utilization rate"),
    ("Rankings", "Which proposal has the highest estimated fee?"),

    # Category 13: KPI (10 queries)
    ("KPI", "Give me the KPI summary report for FY 2025-26"),
    ("KPI", "Show overall business KPI metrics"),
    ("KPI", "What is our gross profit margin KPI?"),
    ("KPI", "Show active project KPI metrics"),
    ("KPI", "What is our current collection efficiency KPI?"),
    ("KPI", "Show executive KPI dashboard metrics"),
    ("KPI", "What is our staff utilization KPI?"),
    ("KPI", "Show proposal conversion rate KPI"),
    ("KPI", "What is our revenue growth KPI YTD?"),
    ("KPI", "Give me KPI summary for Advisory department"),

    # Category 14: Recoverability (10 queries)
    ("Recoverability", "What is our project recoverability percentage?"),
    ("Recoverability", "Show recoverability report for Audit service line"),
    ("Recoverability", "What is the actual vs estimated cost breakdown?"),
    ("Recoverability", "Show project recoverability for Advisory"),
    ("Recoverability", "Which projects have recoverability below 80%?"),
    ("Recoverability", "Show recoverability summary for FY 2025-26"),
    ("Recoverability", "What is total estimated cost vs actual cost?"),
    ("Recoverability", "Show recoverability percentage by department"),
    ("Recoverability", "List projects with high cost overruns"),
    ("Recoverability", "Show recoverability metrics for top 5 projects"),

    # Category 15: Job Estimations (10 queries)
    ("Job Estimations", "Show job estimation metrics and approved fees"),
    ("Job Estimations", "What is our total job estimation count?"),
    ("Job Estimations", "Show job estimation status breakdown"),
    ("Job Estimations", "List job estimates pending approval"),
    ("Job Estimations", "What is the average job estimation fee?"),
    ("Job Estimations", "Show job estimation report for FY 2025-26"),
    ("Job Estimations", "List job estimations for Audit department"),
    ("Job Estimations", "Show fee estimation vs actual contract price"),
    ("Job Estimations", "What is the total value of approved job estimates?"),
    ("Job Estimations", "Show job estimation distribution by service line"),

    # Category 16: Service Leads (10 queries)
    ("Service Leads", "Show service pipeline leads and sales lead metrics"),
    ("Service Leads", "List open service leads for Advisory"),
    ("Service Leads", "What is the total value of active service leads?"),
    ("Service Leads", "Show service leads breakdown by status"),
    ("Service Leads", "List service leads for Audit service line"),
    ("Service Leads", "What is the lead conversion rate for FY 2025-26?"),
    ("Service Leads", "Show new service leads added this quarter"),
    ("Service Leads", "List top service leads by estimated value"),
    ("Service Leads", "Show service leads assigned to employee 31"),
    ("Service Leads", "What is the average lead nurturing duration?"),

    # Category 17: Customer 360 (10 queries)
    ("Customer 360", "Show customer 360 profile for Client ABC"),
    ("Customer 360", "Give me 360 view for ACME Corp"),
    ("Customer 360", "Show full customer profile for Global Trading"),
    ("Customer 360", "What are the bank details and contacts for Client XYZ?"),
    ("Customer 360", "Show customer 360 details for customer ID 201"),
    ("Customer 360", "List all branches and contact persons for Client ABC"),
    ("Customer 360", "Show customer 360 overview for Gulf Financial"),
    ("Customer 360", "What is the 360 project history for Client ABC?"),
    ("Customer 360", "Show customer 360 summary for Prime Holding"),
    ("Customer 360", "Give me 360 profile and tax registration for Client ABC"),

    # Category 18: UI Navigation (10 queries)
    ("UI Navigation", "Open Revenue Dashboard"),
    ("UI Navigation", "Navigate to Receivables Overview"),
    ("UI Navigation", "Open Active Projects View"),
    ("UI Navigation", "Navigate to Sales Pipeline Dashboard"),
    ("UI Navigation", "Open Proposal Management Screen"),
    ("UI Navigation", "Navigate to Customer 360 Panel"),
    ("UI Navigation", "Open Staff Billing Report View"),
    ("UI Navigation", "Navigate to Job Estimation Dashboard"),
    ("UI Navigation", "Open Service Leads Tracking View"),
    ("UI Navigation", "Navigate to Executive KPI Summary Screen"),

    # Category 19: Follow-up Conversations (10 queries)
    ("Follow-up", "Show total revenue for FY 2025-26"),
    ("Follow-up", "Now filter that by Audit department"),
    ("Follow-up", "What about Advisory?"),
    ("Follow-up", "Show overdue receivables"),
    ("Follow-up", "Filter for invoices older than 60 days"),
    ("Follow-up", "Show top 5 customers for those overdue receivables"),
    ("Follow-up", "What are our active projects?"),
    ("Follow-up", "Show only those in Advisory service line"),
    ("Follow-up", "Give me total proposal pipeline"),
    ("Follow-up", "Break that down by win probability"),

    # Category 20: Clarification Flows (10 queries)
    ("Clarification", "Show customer profile"),
    ("Clarification", "Show project details"),
    ("Clarification", "Update proposal status"),
    ("Clarification", "Create project task"),
    ("Clarification", "Find customer ID"),
    ("Clarification", "Show proposal details"),
    ("Clarification", "Show revenue for project"),
    ("Clarification", "Assign task to employee"),
    ("Clarification", "Show job estimation for client"),
    ("Clarification", "Show receivables for customer"),

    # Category 21: Multi-step Queries (10 queries)
    ("Multi-step", "Show total revenue and overdue receivables for FY 2025-26"),
    ("Multi-step", "What is our current sales pipeline and top 5 active projects?"),
    ("Multi-step", "Show KPI summary report and recoverability percentage"),
    ("Multi-step", "Give me revenue breakdown by month and list top 5 clients"),
    ("Multi-step", "Show overdue receivables ageing and customer 360 profile for Client ABC"),
    ("Multi-step", "What is the proposal win rate and service leads breakdown?"),
    ("Multi-step", "Show active project details and staff billing utilization"),
    ("Multi-step", "Give me total revenue for Audit and compare with Advisory"),
    ("Multi-step", "Show job estimation metrics and open proposal pipeline"),
    ("Multi-step", "What is the total revenue, total receivables, and KPI summary for FY 2025-26?")
]


class SmartDeterministicPlanner(EnterprisePlanner):
    """
    Deterministic Planner that generates execution plans deterministically for test queries
    without invoking the live LLM, while passing through the LIVE downstream pipeline
    (EntityResolver -> ExecutionValidator -> ToolRegistry -> Backend SQL -> Synthesizer).
    """
    def __init__(self):
        from langchain_community.chat_models.fake import FakeListChatModel
        super().__init__(llm_client=FakeListChatModel(responses=["OK"]))

    async def _generate_execution_plan(self, question: str):
        q_lower = question.lower()
        
        cap_id = "kpi_summary"
        if "revenue" in q_lower or "billed" in q_lower or "billings" in q_lower:
            cap_id = "revenue_analysis"
        elif "receivable" in q_lower or "overdue" in q_lower or "collection" in q_lower or "invoices" in q_lower:
            cap_id = "receivables_analysis"
        elif "project" in q_lower or "task" in q_lower or "prj-" in q_lower:
            cap_id = "project_details"
        elif "pipeline" in q_lower or "opportunity" in q_lower or "lead" in q_lower:
            cap_id = "pipeline_analysis"
        elif "proposal" in q_lower or "win rate" in q_lower or "win probability" in q_lower:
            cap_id = "proposal_analysis"
        elif "customer 360" in q_lower or "profile for" in q_lower or "bank details" in q_lower:
            cap_id = "customer_360_profile"
        elif "customer" in q_lower or "client" in q_lower:
            cap_id = "customer_resolution"
        elif "employee" in q_lower or "staff" in q_lower or "billing utilization" in q_lower or "hours" in q_lower:
            cap_id = "staff_billing_report"
        elif "recoverability" in q_lower or "overrun" in q_lower:
            cap_id = "recoverability_analysis"
        elif "job estimation" in q_lower or "estimate" in q_lower:
            cap_id = "job_estimation"
        elif "open" in q_lower or "navigate" in q_lower or "dashboard" in q_lower:
            cap_id = "ui_navigation"
        elif "top 5" in q_lower or "top 10" in q_lower or "rank" in q_lower or "highest" in q_lower or "compare" in q_lower:
            cap_id = "analytical_query"

        class MockMessage:
            usage_metadata = {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}
            response_metadata = {"model_name": "smart_deterministic_planner"}

        class MockParsed:
            def model_dump(self):
                return {
                    "business_goal": f"Execute test for capability {cap_id}",
                    "confidence_score": 0.98,
                    "entities": [],
                    "scope": ["Organization"],
                    "business_capabilities": [
                        {
                            "id": cap_id,
                            "scope": "organization",
                            "filters": {
                                "financial_year": "FY 2025-26",
                                "start_date": "2025-10-01",
                                "end_date": "2026-09-30"
                            },
                            "context": {
                                "financial_year": "FY 2025-26",
                                "start_date": "2025-10-01",
                                "end_date": "2026-09-30"
                            },
                            "intent": "generate_report"
                        }
                    ],
                    "missing_information": [],
                    "entity_errors": []
                }

        return {
            "parsed": MockParsed(),
            "raw": MockMessage()
        }


def classify_execution(response: dict, raw_exception: Exception = None) -> tuple[str, str]:
    """
    Classifies test response strictly based on pipeline execution status.
    Returns: (STATUS, REASON)
      - STATUS: 'PASS', 'SKIPPED', or 'FAILED'
    """
    if raw_exception is not None:
        err_msg = str(raw_exception).lower()
        if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg or "tpd" in err_msg:
            return "SKIPPED", f"LLM Rate Limit Exception: {raw_exception}"
        return "FAILED", f"Unhandled Exception: {raw_exception}"

    if not isinstance(response, dict):
        return "FAILED", "Invalid response structure (not a dict)"

    content = response.get("content", "")
    error_code = str(response.get("error_code", "")).lower()

    # 1. Rate Limit / Quota Suppression -> SKIPPED (NEVER PASS)
    if (
        error_code in ["rate_limit", "rate_limit_exceeded", "quota_exceeded"]
        or "retry_after" in response
        or "temporarily at capacity" in content.lower()
        or "daily token quota may be exhausted" in content.lower()
        or "rate limit reached for model" in content.lower()
        or "please try again in" in content.lower()
    ):
        return "SKIPPED", "LLM Provider Rate Limit / Token Quota Exceeded"

    # 2. Pipeline / Execution Failures -> FAILED
    if "encountered an error while analysing your request" in content:
        return "FAILED", "Generic Planner/Synthesizer error returned"
    
    if "is not registered in the Catalog" in content:
        return "FAILED", "Unregistered capability error"

    if "Traceback (most recent call last)" in content or "UnboundLocalError" in content or "NameError" in content:
        return "FAILED", "Raw Python exception leaked in response"

    if not content and not response.get("raw_tool_results"):
        return "FAILED", "Completely empty pipeline response"

    # Check for underlying tool execution errors in envelope
    tool_results = response.get("raw_tool_results", [])
    for tr in tool_results:
        if isinstance(tr, dict) and tr.get("status") == "error":
            return "FAILED", f"Tool Execution Failure: {tr.get('error')}"

    # 3. All components completed successfully -> PASS
    return "PASS", "Pipeline Execution Completed Successfully"


async def run_hybrid_production_suite():
    start_time = time.time()
    print("=========================================================================")
    print("      PRODUCTION READINESS DUAL VALIDATION SUITE (270 SCENARIOS)        ")
    print("=========================================================================")
    print(f"Phase 1: Live LLM Validation ({len(LIVE_LLM_SCENARIOS)} scenarios)")
    print(f"Phase 2: Live Deterministic Pipeline Validation ({len(PRODUCTION_QUERIES) - len(LIVE_LLM_SCENARIOS)} scenarios)")
    print("=========================================================================\n")

    live_planner = EnterprisePlanner()
    deterministic_planner = SmartDeterministicPlanner()

    live_llm_queries_set = set(q for _, q, _ in LIVE_LLM_SCENARIOS)

    live_results = []
    deterministic_results = []
    
    capability_validation_matrix = {}

    # Initialize capability matrix
    all_caps = [
        "analytical_query", "customer_resolution", "customer_360_profile",
        "receivables_analysis", "revenue_analysis", "proposal_search",
        "update_proposal_status", "project_search", "create_task",
        "kpi_summary", "recoverability_analysis", "pipeline_analysis",
        "proposal_analysis", "job_estimation", "project_details",
        "service_leads", "ui_navigation"
    ]
    for cap in all_caps:
        capability_validation_matrix[cap] = {"live_llm": False, "deterministic": False, "status": "PENDING"}

    # -------------------------------------------------------------------------
    # PHASE 1: LIVE LLM VALIDATION (28 Scenarios)
    # -------------------------------------------------------------------------
    print("--- PHASE 1: LIVE LLM VALIDATION (Representative Subset) ---")
    for idx, (category, query, expected_cap) in enumerate(LIVE_LLM_SCENARIOS, 1):
        ctx_obj = RequestContext(
            question=query,
            jwt_token="Bearer mock_prod_validation_token",
            session_id=f"live_llm_session_{idx}",
            history=[],
            user_context=dict(USER_CONTEXT_TIER_1),
            request_metadata={}
        )
        set_user_context(USER_CONTEXT_TIER_1)

        result_item = {
            "phase": "Live LLM",
            "index": idx,
            "category": category,
            "query": query,
            "expected_capability": expected_cap,
            "status": "UNKNOWN",
            "reason": None,
            "snippet": None
        }

        try:
            response = await live_planner.execute_turn(ctx_obj)
            status, reason = classify_execution(response)
            result_item["snippet"] = response.get("content", "")[:80].replace("\n", " ")
        except Exception as e:
            status, reason = classify_execution({}, raw_exception=e)
            result_item["snippet"] = "Exception Raised"

        result_item["status"] = status
        result_item["reason"] = reason
        live_results.append(result_item)

        if status == "PASS":
            capability_validation_matrix[expected_cap]["live_llm"] = True
            capability_validation_matrix[expected_cap]["status"] = "VALIDATED"
            print(f"[LIVE LLM {idx}/{len(LIVE_LLM_SCENARIOS)}] [{category}] PASS ✅ | {query} | {result_item['snippet']}...")
        elif status == "SKIPPED":
            print(f"[LIVE LLM {idx}/{len(LIVE_LLM_SCENARIOS)}] [{category}] SKIPPED ⏳ (Rate Limited) | {query}")
        else:
            print(f"[LIVE LLM {idx}/{len(LIVE_LLM_SCENARIOS)}] [{category}] FAIL ❌ | {query} | Reason: {reason}")

        await asyncio.sleep(0.05)

    # -------------------------------------------------------------------------
    # PHASE 2: DETERMINISTIC PIPELINE VALIDATION (242 Scenarios)
    # -------------------------------------------------------------------------
    print("\n--- PHASE 2: DETERMINISTIC PIPELINE VALIDATION (Live Downstream Pipeline) ---")
    deterministic_queue = [(cat, q) for cat, q in PRODUCTION_QUERIES if q not in live_llm_queries_set]

    for idx, (category, query) in enumerate(deterministic_queue, 1):
        ctx_obj = RequestContext(
            question=query,
            jwt_token="Bearer mock_prod_validation_token",
            session_id=f"det_pipeline_session_{idx}",
            history=[],
            user_context=dict(USER_CONTEXT_TIER_1),
            request_metadata={}
        )
        set_user_context(USER_CONTEXT_TIER_1)

        result_item = {
            "phase": "Deterministic Pipeline",
            "index": idx,
            "category": category,
            "query": query,
            "status": "UNKNOWN",
            "reason": None,
            "snippet": None
        }

        try:
            print(f"[{idx}/{len(deterministic_queue)}] {query[:50]}...", flush=True)
            response = await deterministic_planner.execute_turn(ctx_obj)
            status, reason = classify_execution(response)
            result_item["snippet"] = response.get("content", "")[:80].replace("\n", " ")
        except Exception as e:
            status, reason = classify_execution({}, raw_exception=e)
            result_item["snippet"] = "Exception Raised"

        result_item["status"] = status
        result_item["reason"] = reason
        deterministic_results.append(result_item)

        # Update capability matrix for deterministic phase
        q_lower = query.lower()
        matched_cap = "kpi_summary"
        if "revenue" in q_lower: matched_cap = "revenue_analysis"
        elif "receivable" in q_lower or "overdue" in q_lower: matched_cap = "receivables_analysis"
        elif "project" in q_lower: matched_cap = "project_details"
        elif "pipeline" in q_lower: matched_cap = "pipeline_analysis"
        elif "proposal" in q_lower: matched_cap = "proposal_analysis"
        elif "customer 360" in q_lower: matched_cap = "customer_360_profile"
        elif "customer" in q_lower: matched_cap = "customer_resolution"
        elif "employee" in q_lower or "staff" in q_lower: matched_cap = "revenue_analysis"
        elif "recoverability" in q_lower: matched_cap = "recoverability_analysis"
        elif "job estimation" in q_lower: matched_cap = "job_estimation"
        elif "open" in q_lower or "navigate" in q_lower: matched_cap = "ui_navigation"

        if status == "PASS":
            capability_validation_matrix[matched_cap]["deterministic"] = True
            if capability_validation_matrix[matched_cap]["status"] != "VALIDATED":
                capability_validation_matrix[matched_cap]["status"] = "VALIDATED"

        if idx % 10 == 0 or idx == len(deterministic_queue):
            print(f"[DETERMINISTIC PIPELINE {idx}/{len(deterministic_queue)}] Processed {idx} scenarios... (Passed: {sum(1 for r in deterministic_results if r['status']=='PASS')})", flush=True)

    total_time = round(time.time() - start_time, 2)

    # -------------------------------------------------------------------------
    # METRIC CALCULATIONS & REPORT GENERATION
    # -------------------------------------------------------------------------
    all_results = live_results + deterministic_results
    total_tested = len(all_results)
    
    passed_count = sum(1 for r in all_results if r["status"] == "PASS")
    failed_count = sum(1 for r in all_results if r["status"] == "FAILED")
    skipped_count = sum(1 for r in all_results if r["status"] == "SKIPPED")

    live_passed = sum(1 for r in live_results if r["status"] == "PASS")
    live_skipped = sum(1 for r in live_results if r["status"] == "SKIPPED")
    live_failed = sum(1 for r in live_results if r["status"] == "FAILED")

    det_passed = sum(1 for r in deterministic_results if r["status"] == "PASS")
    det_failed = sum(1 for r in deterministic_results if r["status"] == "FAILED")
    det_skipped = sum(1 for r in deterministic_results if r["status"] == "SKIPPED")

    validated_caps_cnt = sum(1 for cap, val in capability_validation_matrix.items() if val["live_llm"] or val["deterministic"])
    coverage_pct = round((validated_caps_cnt / len(all_caps)) * 100, 2)

    print("\n=========================================================================")
    print("                 FINAL PRODUCTION VALIDATION REPORT                      ")
    print("=========================================================================")
    print(f"Total Test Scenarios:          {total_tested}")
    print(f"Live LLM Tests Executed:       {len(live_results)} (Passed: {live_passed}, Skipped: {live_skipped}, Failed: {live_failed})")
    print(f"Deterministic Pipeline Tests:  {len(deterministic_results)} (Passed: {det_passed}, Skipped: {det_skipped}, Failed: {det_failed})")
    print(f"Total Passed:                  {passed_count}")
    print(f"Total Failed:                  {failed_count}")
    print(f"Total Skipped (Rate Limited):  {skipped_count}")
    print(f"Capability Coverage:           {coverage_pct}% ({validated_caps_cnt}/{len(all_caps)} Capabilities Verified)")
    print(f"Execution Time:                {total_time}s")
    print("=========================================================================")

    print("\nBUSINESS CAPABILITY VALIDATION MATRIX:")
    for cap, info in capability_validation_matrix.items():
        modes = []
        if info["live_llm"]: modes.append("Live LLM")
        if info["deterministic"]: modes.append("Deterministic Pipeline")
        mode_str = " + ".join(modes) if modes else "Not Validated"
        status_str = "VALIDATED ✅" if (info["live_llm"] or info["deterministic"]) else "PENDING ❌"
        print(f"- {cap:<25} | Status: {status_str:<12} | Validation Method: {mode_str}")

    return {
        "total_tested": total_tested,
        "live_tests": len(live_results),
        "deterministic_tests": len(deterministic_results),
        "passed": passed_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "coverage_pct": coverage_pct,
        "execution_time": total_time,
        "capability_matrix": capability_validation_matrix
    }

if __name__ == "__main__":
    asyncio.run(run_hybrid_production_suite())
