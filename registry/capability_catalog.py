"""
capability_catalog.py — Abstract Business Capability & Implementation Metadata.
This file is the single source of truth for the Tool Registry.
Adding a new chatbot capability simply requires adding a new dictionary entry here.
No code changes in the Planner or Registry are needed.
"""

# Priorities (Lower number = Higher Priority)
PRIORITY_EXISTING_REPORT = 1
PRIORITY_BUSINESS_API = 2
PRIORITY_AGGREGATE_API = 3
PRIORITY_SEMANTIC_WRAPPER = 4
PRIORITY_AD_HOC_SQL = 5

# ---------------------------------------------------------------------------
# Business Capabilities Metadata
# The Planner ONLY sees the 'id', 'description', and 'required_business_context'.
# It never sees the implementations list.
# ---------------------------------------------------------------------------
BUSINESS_CAPABILITIES = [
    {
        "id": "analytical_query",
        "business_domain": "analytics",
        "fast_path_eligible": False,
        "intent_keywords": ['top performing projects', 'top performing project', 'top projects', 'best performing projects', 'highest revenue projects', 'top clients'],
        "description": (
            "Performs ad-hoc analysis, counts, aggregations, comparisons, or ranking across any CRM "
            "business entity (e.g. service leads, proposals, customers, projects, invoices, tasks). "
            "Use this for: top-N rankings by any metric, cross-entity comparisons, trend queries, "
            "count queries, and any analytical question that is NOT a simple revenue-by-month lookup. "
            "ALWAYS use this for 'top N customers by revenue', 'which client has highest revenue', "
            "and similar customer-revenue ranking questions."
        ),
        "supported_metrics": ["count", "sum", "average", "ranking", "comparison"],
        "supported_operations": ["count", "sum", "average", "group_by", "ranking", "comparison", "trend", "filter", "sort_order", "limit", "aggregation"],
        "priority": PRIORITY_AD_HOC_SQL,
        "dependencies": [],
        "response_contract": {
            "supports_report": False,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": False,
            "supports_export": False,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "INSIGHT"
        },
        "primary_metric": "analytical_result",
        "response_schema": {
            "query": "string",
            "results": "array",
            "total_count": "number",
            "summary": "object",
            "headers": "array"
        },
        "default_error_message": "I couldn't complete the requested analytical query at the moment. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "employee_id", "customer_id"],
        "defaultable_context": [],
        "required_business_context": {},
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": False,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like to use?",
                "smart_default": "current_fy"
            }
        },
        "implementations": [
            {
                "priority": PRIORITY_AD_HOC_SQL,
                "type": "wrapper",
                "function_call": "call_analytical_query",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": [],
                "supported_operations": [
                    "count", "sum", "average", "group_by",
                    "ranking", "comparison", "trend", "filter",
                    "sort_order", "limit", "aggregation"
                ]
            }
        ]
    },

    {
        "id": "customer_resolution",
        "business_domain": "customer",
        "fast_path_eligible": True,
        "intent_keywords": ['customer resolution', 'find customer', 'customer search'],
        "description": "Find a customer by name or code to get their ID. Always required before querying customer-specific data.",
        "supported_metrics": ["customer_id", "customer_name"],
        "supported_operations": ["filter", "search"],
        "priority": PRIORITY_BUSINESS_API,
        "dependencies": [],
        "response_contract": {
            "supports_report": False,
            "supports_summary": False,
            "supports_analysis": False,
            "supports_chart": False,
            "supports_export": False,
            "supports_filters": False,
            "supports_followup": False,
            "supports_comparison": False,
            "supports_drilldown": False,
            "default_presentation": "INSIGHT"
        },
        "primary_metric": "customer_id",
        "response_schema": {
            "customer_id": "number",
            "customer_name": "string",
            "customer_code": "string"
        },
        "default_error_message": "I couldn't locate the specified customer. Please check the customer name and try again.",
        "required_business_context": {
            "search_term": {"type": "string", "description": "The customer name or code"}
        },
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "GET /api/v1/customer?search={search_term}",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["search_term"]
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "call_customer_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "customer_360_profile",
        "business_domain": "customer",
        "fast_path_eligible": True,
        "intent_keywords": ['customer 360', 'customer profile', 'customer details'],
        "description": "Get a comprehensive 360-degree profile of a specific customer, including bank details, branches, and contact info.",
        "supported_metrics": ["customer_profile", "branches", "contacts"],
        "supported_operations": ["filter", "summary", "drilldown"],
        "priority": PRIORITY_BUSINESS_API,
        "dependencies": ["customer_resolution"],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": False,
            "supports_chart": False,
            "supports_export": False,
            "supports_filters": False,
            "supports_followup": True,
            "supports_comparison": False,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "customer",
        "response_schema": {
            "customer": "object",
            "contactDetails": "array",
            "bankDetails": "array",
            "branchDetails": "array",
            "attachment": "array"
        },
        "default_error_message": "Customer profile information is currently unavailable.",
        "required_parameters": [],
        "optional_parameters": {
            "customer_id": {"type": "integer", "description": "Resolved ID of the customer"}
        },
        "clarification_order": [],
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "GET /api/v1/customer/single/{customer_id}",
                "needs_confirmation": False,
                "required_entities": ["customer"],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "call_customer_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "receivables_analysis",
        "business_domain": "finance",
        "fast_path_eligible": True,
        "intent_keywords": ['receivables', 'receivable', 'overdue invoices', 'ageing report', 'ageing bucket', 'view by ageing', 'view by service line', 'receivables by service line', 'ageing summary'],
        "description": "Full ageing breakdown of all overdue invoices. Use when asking about pending or overdue receivables.",
        "supported_metrics": ["total_receivables", "overdue_invoices", "ageing_buckets", "total_overdue_count"],
        "supported_operations": ["sum", "filter", "group_by", "comparison", "trend"],
        "priority": PRIORITY_EXISTING_REPORT,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "total_receivables",
        "response_schema": {
            "total_receivables": "number",
            "ageing_buckets": "object",
            "overdue_invoices": "array",
            "total_overdue_count": "number",
            "filter_service_line": "string"
        },
        "default_error_message": "Receivables metrics are currently unavailable. Please try again later.",
        "required_business_context": {},
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": True,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like me to use?",
                "smart_default": "current_fy"
            },
            "service_line": {
                "type": "string",
                "required": True,
                "ui_component": "service_line_dropdown",
                "label": "Service Line",
                "prompt": "Would you like to filter by Service Line, or search across all?",
                "smart_default": None
            }
        },
        "ui_action": "navigate",
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/receivable-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "call_receivables_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "revenue_analysis",
        "business_domain": "finance",
        "fast_path_eligible": True,
        "intent_keywords": ['revenue', 'revenue summary', 'monthly revenue', 'total revenue', 'top 5 customers', 'top customers by revenue', 'revenue by service line', 'highest revenue', 'gp performance', 'gp performance by service line', 'gross profit', 'gp', 'gp breakdown', 'monthly revenue trend', 'revenue trend', 'revenue comparison', 'revenue comparison with previous fy', 'previous fy', 'revenue analysis', 'show revenue', 'revenue by office'],
        "description": (
            "Gets overall revenue totals, revenue by month, customer revenue rankings, revenue by service line, or team billing for a specific fiscal year or date range."
        ),
        "supported_metrics": ["total_revenue_ytd", "revenue_by_month", "gp_performance", "team_billing", "revenue", "count", "net_amount"],
        "supported_operations": ["aggregate", "ranking", "breakdown", "comparison", "trend", "filter", "sum", "count", "avg"],
        "supported_dimensions": ["customer", "service_line", "office", "month", "year", "employee"],
        "supported_aggregations": ["SUM", "COUNT", "AVG", "MIN", "MAX"],
        "priority": PRIORITY_EXISTING_REPORT,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "total_revenue_ytd",
        "response_schema": {
            "total_revenue_ytd": "number",
            "previous_fy_revenue": "number",
            "top_5_customers": "array",
            "gp_performance_ytd_breakdown": "array",
            "current_team_billing_period_total": "number",
            "revenue_by_month": "array",
            "gp_performance_breakdown": "array",
            "team_billing_breakdown": "array"
        },
        "default_error_message": "Revenue metrics are currently unavailable. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "customer_id", "service_line_id"],
        "defaultable_context": [],
        "required_parameters": [],
        "optional_parameters": {
            "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD), e.g. FY start"},
            "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD), e.g. FY end"}
        },
        "clarification_order": [],
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": True,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like me to use?",
                "smart_default": "current_fy"
            },
            "service_line": {
                "type": "string",
                "required": True,
                "ui_component": "service_line_dropdown",
                "label": "Service Line",
                "prompt": "Would you like to filter by Service Line, or search across all?",
                "smart_default": None
            },
            "customer_id": {
                "type": "integer",
                "required": False,
                "ui_component": "customer_autocomplete",
                "label": "Customer",
                "prompt": "Please select or type the name of the customer.",
                "smart_default": None
            }
        },
        "chart_config": {
            "type": "bar",
            "data_key": "revenue_by_month",
            "x_field": "month",
            "y_field": "amount",
            "label": "Revenue"
        },
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "wrapper",
                "function_call": "get_revenue_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/revenue-billing-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "api",
                "endpoint": "GET /api/v1/reports/revenue-billing-report?searchQuery={\"client_id\":{customer_id}}",
                "needs_confirmation": False,
                "required_entities": ["customer"],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "api",
                "endpoint": "GET /api/v1/project/{project_id}",
                "needs_confirmation": False,
                "required_entities": ["project"],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "proposal_search",
        "business_domain": "pipeline",
        "fast_path_eligible": True,
        "intent_keywords": ['proposals', 'proposal', 'open proposals', 'pending proposals', 'proposal win rate', 'win rate', 'winrate', 'proposal winrate', 'rejected proposals', 'accepted proposals'],
        "description": "Search proposals by code, customer, or status. Use when asked to show pending or open proposals.",
        "supported_metrics": ["proposals_list", "total_count", "open_proposals"],
        "supported_operations": ["filter", "search", "count", "summary"],
        "priority": PRIORITY_BUSINESS_API,
        "dependencies": [],
        "response_contract": {
            "supports_report": False,
            "supports_summary": True,
            "supports_analysis": False,
            "supports_chart": False,
            "supports_export": False,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": False,
            "supports_drilldown": True,
            "default_presentation": "KPI_CARD"
        },
        "primary_metric": "proposals_list",
        "response_schema": {
            "proposals": "array",
            "total_count": "number",
            "search_term": "string",
            "accepted_proposals": "object",
            "rejected_proposals": "object",
            "sent_proposals": "object",
            "created_proposals": "object",
            "open_proposals": "object",
            "won_proposals": "object",
            "total_proposals": "object",
            "proposal_win_rate": "number"
        },
        "default_error_message": "Proposal search results are currently unavailable. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "status", "start_date", "end_date", "financial_year"],
        "defaultable_context": [],
        "required_business_context": {
            "search_term": {"type": "string", "description": "Proposal code, reference, or 'open'/'pending'"}
        },
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "GET /api/v1/proposal?search={search_term}",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["search_term"]
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_pipeline_and_proposals",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "update_proposal_status",
        "fast_path_eligible": False,
        "intent_keywords": [],
        "description": "Updates the status of a proposal.",
        "primary_metric": "status_update",
        "response_schema": {
            "proposal_id": "number",
            "new_status_id": "number",
            "success": "boolean",
            "message": "string"
        },
        "default_error_message": "Could not update proposal status at this time.",
        "required_parameters": ["new_status_id"],
        "optional_parameters": {
            "proposal_id": {"type": "integer"},
            "new_status_id": {"type": "integer"}
        },
        "clarification_order": ["new_status_id"],
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "PUT /api/v1/proposal/status/{proposal_id}/{new_status_id}",
                "needs_confirmation": True,
                "required_entities": ["proposal"],
                "required_parameters": ["new_status_id"]
            }
        ]
    },
    {
        "id": "project_search",
        "fast_path_eligible": True,
        "intent_keywords": ["search projects", "find project", "project search", "status of project", "status of this project", "project status"],
        "description": "Search specific projects by name, code, or customer to get status, fees, and dates.",
        "primary_metric": "projects_list",
        "response_schema": {
            "projects": "array",
            "total_count": "number"
        },
        "default_error_message": "Project search data is currently unavailable.",
        "required_context": [],
        "clarifiable_context": [],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "status", "customer_id"],
        "defaultable_context": [],
        "required_business_context": {
            "search_term": {"type": "string", "description": "Project name or code"}
        },
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "GET /api/v1/projects?search={search_term}",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["search_term"]
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_comprehensive_customer_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_active_projects_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "create_task",
        "fast_path_eligible": False,
        "intent_keywords": [],
        "description": "Creates a new task on a specific project.",
        "primary_metric": "task_creation",
        "response_schema": {
            "task_id": "number",
            "task_name": "string",
            "status": "string"
        },
        "default_error_message": "Could not create project task at this time.",
        "required_parameters": ["task_name"],
        "optional_parameters": {
            "project_id": {"type": "integer", "description": "Resolved Project ID"},
            "task_name": {"type": "string", "description": "Name or subject of the task"}
        },
        "clarification_order": ["task_name"],
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "POST /api/v1/project-task/",
                "needs_confirmation": True,
                "required_entities": ["project"],
                "required_parameters": ["task_name"],
                "supported_operations": ["filter"]
            }
        ]
    },
    {
        "id": "kpi_summary",
        "business_domain": "kpi",
        "fast_path_eligible": True,
        "intent_keywords": ['kpi summary', 'executive kpi', 'organization kpi', 'kpi report', 'generate kpi report', 'kpi dashboard', 'show kpi'],
        "description": "Retrieves the master KPI summary report (budget vs actuals, GP performance).",
        "supported_metrics": ["strictly_active_projects_count", "overdue_tasks", "overdue_projects", "projects_by_status"],
        "supported_operations": ["summary", "filter", "comparison", "trend", "count", "aggregate", "generate"],
        "priority": PRIORITY_EXISTING_REPORT,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "strictly_active_projects_count",
        "response_schema": {
            "employee_id": "number",
            "employee_name": "string",
            "is_organization_aggregate": "boolean",
            "total_kpi_target": "number",
            "target_gp": "number",
            "secured_business": "number",
            "balance_to_achieve": "number",
            "total_proposals": "number",
            "total_proposal_value": "number",
            "total_projects": "number",
            "strictly_active_projects_count": "number",
            "total_projects_all_statuses_combined": "number",
            "total_performing_revenue": "number",
            "variance": "number",
            "projects_by_status": "object",
            "gp_performance": "object",
            "summary": "object",
            "date_range": "object",
            "overdue_tasks": "number",
            "overdue_projects": "number"
        },
        "default_error_message": "KPI summary and active project metrics are currently unavailable.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "employee_id", "employee_name"],
        "defaultable_context": [],
        "required_business_context": {},
        "optional_parameters": {
            "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD), default FY start '2025-04-01'"},
            "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD), default FY end '2026-03-31'"}
        },
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": True,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like me to use?",
                "smart_default": "current_fy"
            },
            "service_line": {
                "type": "string",
                "required": True,
                "ui_component": "service_line_dropdown",
                "label": "Service Line",
                "prompt": "Would you like to filter by Service Line, or search across all?",
                "smart_default": None
            }
        },
        "ui_action": "navigate",
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/kpi-summary-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_kpi_summary_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "recoverability_analysis",
        "business_domain": "finance",
        "fast_path_eligible": True,
        "intent_keywords": ['recoverability', 'project recoverability', 'recoverability report'],
        "description": "Gets recoverability metrics, estimated vs actual costs, and recoverability percentage. Use when asked about recoverability.",
        "supported_metrics": ["actual_recoverability_pct", "total_estimated_cost", "total_actual_cost"],
        "supported_operations": ["sum", "average", "filter", "comparison", "trend", "ranking"],
        "priority": PRIORITY_EXISTING_REPORT,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "actual_recoverability_pct",
        "response_schema": {
            "total_estimated_cost": "number",
            "total_actual_cost": "number",
            "actual_recoverability_pct": "number",
            "summary": "object",
            "key_projects_sample": "array"
        },
        "default_error_message": "Project recoverability metrics are currently unavailable. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "project_id"],
        "defaultable_context": [],
        "required_parameters": [],
        "optional_parameters": {
            "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD)"},
            "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD)"},
            "financial_year": {"type": "string", "description": "Financial year (e.g. 2026)"}
        },
        "clarification_order": [],
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": True,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like me to use?",
                "smart_default": "current_fy"
            },
            "service_line": {
                "type": "string",
                "required": True,
                "ui_component": "service_line_dropdown",
                "label": "Service Line",
                "prompt": "Would you like to filter by Service Line, or search across all?",
                "smart_default": None
            }
        },
        "ui_action": "navigate",
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/project-recoverability-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_project_recoverability_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "pipeline_analysis",
        "business_domain": "pipeline",
        "fast_path_eligible": True,
        "intent_keywords": ['pipeline', 'sales pipeline', 'opportunities', 'proposals', 'proposal status', 'rejected proposals', 'accepted proposals', 'won proposals', 'proposal win rate', 'proposal metrics'],
        "description": "Gets sales pipeline metrics, proposal status breakdown (accepted, rejected, sent, created), proposal win rate, and sales lead metrics.",
        "supported_metrics": ["open_proposals", "won_proposals", "proposal_win_rate", "service_pipeline_leads_summary", "dashboard_proposal_metrics_breakdown"],
        "supported_operations": ["sum", "count", "filter", "group_by", "comparison", "trend", "summary"],
        "priority": PRIORITY_SEMANTIC_WRAPPER,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "open_proposals",
        "response_schema": {
            "open_proposals": "object",
            "won_proposals": "object",
            "total_proposals": "object",
            "proposal_win_rate": "number",
            "service_pipeline_leads_summary": "object",
            "service_leads_breakdown": "array",
            "dashboard_proposal_metrics_breakdown": "array",
            "dashboard_engagement_metrics_breakdown": "array",
            "dashboard_continuous_engagement_metrics_breakdown": "array"
        },
        "default_error_message": "Pipeline and proposal metrics are currently unavailable. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "status"],
        "defaultable_context": [],
        "required_parameters": [],
        "optional_parameters": {},
        "parameter_metadata": {
            "financial_year": {
                "type": "string",
                "required": True,
                "ui_component": "fy_dropdown",
                "label": "Financial Year",
                "prompt": "Which Financial Year would you like me to use?",
                "smart_default": "current_fy"
            },
            "service_line": {
                "type": "string",
                "required": True,
                "ui_component": "service_line_dropdown",
                "label": "Service Line",
                "prompt": "Would you like to filter by Service Line, or search across all?",
                "smart_default": None
            }
        },
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/proposal/statuswise_budget",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_pipeline_and_proposals",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": [],
                "supported_operations": ["filter", "summary"]
            }
        ]
    },
    {
        "id": "job_estimation",
        "fast_path_eligible": True,
        "intent_keywords": ['job estimation', 'job estimations'],
        "description": "Gets job estimation metrics, status breakdown (approved, pending, reviewed, rejected), proposed fees, and approved fees.",
        "primary_metric": "summary",
        "response_schema": {
            "status_breakdown": "array",
            "summary": "object"
        },
        "default_error_message": "Job estimation metrics are currently unavailable. Please try again later.",
        "required_parameters": [],
        "optional_parameters": {},
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_job_estimation_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": [],
                "supported_operations": ["filter", "summary"]
            }
        ]
    },
    {
        "id": "project_details",
        "fast_path_eligible": True,
        "intent_keywords": ['projects', 'active projects', 'project details'],
        "description": "Gets detailed metrics and status for active or specific projects.",
        "primary_metric": "strictly_active_projects_count",
        "response_schema": {
            "strictly_active_projects_count": "number",
            "total_projects_all_statuses_combined": "number",
            "total_approved_fees": "number",
            "total_actual_cost": "number",
            "actual_recoverability_percentage": "number",
            "projects_by_status": "array"
        },
        "default_error_message": "Project details are currently unavailable.",
        "required_parameters": [],
        "optional_parameters": {},
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_active_projects_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": [],
                "supported_operations": ["filter", "summary", "count", "aggregate"]
            }
        ]
    },
    {
        "id": "service_leads",
        "fast_path_eligible": True,
        "intent_keywords": ['service leads', 'pipeline leads'],
        "description": "Gets service pipeline leads and sales lead metrics.",
        "primary_metric": "service_pipeline_leads_summary",
        "response_schema": {
            "service_pipeline_leads_summary": "object",
            "service_leads_breakdown": "array"
        },
        "default_error_message": "Service leads information is currently unavailable.",
        "required_parameters": [],
        "optional_parameters": {},
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_pipeline_and_proposals",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": [],
                "supported_operations": ["filter", "summary"]
            }
        ]
    },
    {
        "id": "staff_billing",
        "business_domain": "billing",
        "fast_path_eligible": True,
        "intent_keywords": ["staff billing", "billing report", "employee billing", "team billing"],
        "description": "Gets the staff billing report showing employee time, billing amounts, and project-level cost recovery.",
        "supported_metrics": ["billing_summary", "employee_billing", "project_billing"],
        "supported_operations": ["sum", "filter", "group_by", "comparison", "trend", "ranking"],
        "priority": PRIORITY_EXISTING_REPORT,
        "dependencies": [],
        "response_contract": {
            "supports_report": True,
            "supports_summary": True,
            "supports_analysis": True,
            "supports_chart": True,
            "supports_export": True,
            "supports_filters": True,
            "supports_followup": True,
            "supports_comparison": True,
            "supports_drilldown": True,
            "default_presentation": "REPORT"
        },
        "primary_metric": "summary",
        "response_schema": {
            "summary": "object",
            "projects": "array"
        },
        "default_error_message": "Staff billing metrics are currently unavailable. Please try again later.",
        "required_context": ["temporal_scope"],
        "clarifiable_context": ["temporal_scope"],
        "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "department_id", "employee_id"],
        "defaultable_context": [],
        "required_parameters": [],
        "optional_parameters": {
            "financial_year": {"type": "string", "description": "Financial year"},
            "service_line": {"type": "string", "description": "Service line filter"},
            "employee_id": {"type": "integer", "description": "Filter by specific employee"}
        },
        "ui_action": "navigate",
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_staff_billing_report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            },
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/revenue-billing-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["department_id"]
            }
        ]
    },
    {
        "id": "ui_navigation",
        "fast_path_eligible": True,
        "intent_keywords": ['open dashboard', 'go to dashboard', 'navigate to'],
        "description": "Use ONLY when the user explicitly asks to open, go to, or navigate to a specific dashboard, page, or UI screen (e.g., 'Open Revenue Dashboard', 'Go to Proposal Dashboard', 'Navigate to KPI Summary').",
        "primary_metric": "target",
        "response_schema": {
            "target": "string",
            "action": "string"
        },
        "default_error_message": "Could not complete navigation request.",
        "required_business_context": {
            "target_dashboard": {"type": "string", "description": "The semantic name of the dashboard to open (e.g., 'proposal_dashboard', 'revenue_dashboard', 'kpi_summary')"}
        },
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "call_ui_navigation",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["target_dashboard"]
            }
        ]
    }
]

# Capability Alias Mapping for robust matching
CAPABILITY_ALIASES = {
    "receivable_analysis": "receivables_analysis",
    "receivable_report": "receivables_analysis",
    "receivables": "receivables_analysis",
    "proposal_analysis": "pipeline_analysis",
    "proposal_pipeline": "pipeline_analysis",
    "active_projects": "project_details",
    "job_estimation_metrics": "job_estimation",
    "comprehensive_customer_report": "customer_360_profile",
    "kpi_summary_report": "kpi_summary",
    "kpi summary": "kpi_summary",
    "customer_360": "customer_360_profile",
    "revenue": "revenue_analysis",
    "revenue_report": "revenue_analysis",
    "pipeline": "pipeline_analysis",
    "staff_billing_report": "staff_billing",
    "staff_billing": "staff_billing"
}

# ---------------------------------------------------------------------------
# Dynamic Registry Extractors
# ---------------------------------------------------------------------------
def get_planner_capabilities_schema() -> list:
    """
    Extracts ONLY the compact abstract business capabilities for Planner prompt.
    Optimized for minimal token consumption.
    """
    schemas = []
    for cap in BUSINESS_CAPABILITIES:
        schemas.append({
            "id": cap["id"],
            "domain": cap.get("business_domain", "general"),
            "description": cap["description"]
        })
    return schemas

def get_capability_metadata(capability_id: str) -> dict:
    """Returns the full metadata for a capability, including implementations, dependencies, and scope metadata."""
    if not capability_id:
        return None
    norm_id = str(capability_id).lower().replace(" ", "_").strip()
    target_id = CAPABILITY_ALIASES.get(norm_id, CAPABILITY_ALIASES.get(capability_id, norm_id))
    for cap in BUSINESS_CAPABILITIES:
        if cap["id"] == target_id:
            # Explicit capability dependencies and governance defaults
            if "depends_on_entities" not in cap:
                if cap["id"] in ("customer_360_profile", "customer_resolution"):
                    cap["depends_on_entities"] = ["customer"]
                    cap["supports_organization_scope"] = False
                elif cap["id"] in ("update_proposal_status",):
                    cap["depends_on_entities"] = ["proposal"]
                    cap["supports_organization_scope"] = False
                elif cap["id"] in ("create_task",):
                    cap["depends_on_entities"] = ["project"]
                    cap["supports_organization_scope"] = False
                else:
                    cap["depends_on_entities"] = []
                    cap["supports_organization_scope"] = True

            cap.setdefault("supports_organization_scope", True)
            cap.setdefault("supports_entity_scope", True)
            cap.setdefault("presentation_modes", ["REPORT", "INSIGHT", "REPORT_AND_INSIGHT"])
            cap.setdefault("optional_filters", ["financial_year", "service_line", "department", "customer_id", "office"])
            return cap
    return None


def get_capability_entity_requirements(capability_id: str) -> tuple:
    """
    Returns (requires_entities: bool, required_entity_types: list) for a capability.
    Enforces capability metadata rules for entity resolution.
    """
    meta = get_capability_metadata(capability_id)
    if not meta:
        return False, []

    requires_entities = meta.get("requires_entities")
    required_types = meta.get("required_entity_types", [])

    if requires_entities is None:
        depends_on = meta.get("depends_on_entities", [])
        requires_entities = len(depends_on) > 0
        required_types = list(depends_on)

    return bool(requires_entities), list(required_types)


# ---------------------------------------------------------------------------
# Deterministic Capability Resolver (Phase 3.1.10)
# ---------------------------------------------------------------------------

def resolve_capabilities_from_requirements(
    required_domains=None,
    required_metrics=None,
    required_operations=None,
    required_entities=None,
    presentation_mode=None,
    max_results=10,
):
    """
    Deterministically matches Planner business requirements against Capability Catalog metadata.
    NEVER uses report names, capability IDs, keyword lists, hardcoded combinations, or if/else chains.
    Adding a new capability to BUSINESS_CAPABILITIES makes it automatically discoverable here.
    """
    req_domains = [d.lower() for d in (required_domains or [])]
    req_metrics = [m.lower() for m in (required_metrics or [])]
    req_operations = [o.lower() for o in (required_operations or [])]

    scored = []
    for cap in BUSINESS_CAPABILITIES:
        score = 0.0
        cap_domain = (cap.get("business_domain") or "").lower()
        cap_metrics = [m.lower() for m in (cap.get("supported_metrics") or [])]
        cap_operations = [o.lower() for o in (cap.get("supported_operations") or [])]
        cap_priority = cap.get("priority", PRIORITY_SEMANTIC_WRAPPER)
        contract = cap.get("response_contract") or {}

        if req_domains and cap_domain in req_domains:
            score += 10.0
        if req_metrics:
            score += sum(5.0 for m in req_metrics if any(m in cm for cm in cap_metrics))
        if req_operations:
            score += sum(2.0 for o in req_operations if o in cap_operations)
        if presentation_mode:
            pm = presentation_mode.upper()
            if pm == "REPORT" and contract.get("supports_report"):
                score += 3.0
            elif pm in ("INSIGHT", "EXECUTIVE_BRIEF") and contract.get("supports_analysis"):
                score += 3.0
            elif pm == "COMPARISON" and contract.get("supports_comparison"):
                score += 3.0
        score += max(0.0, (PRIORITY_AD_HOC_SQL - cap_priority) * 0.5)
        if score > 0:
            scored.append((score, cap))

    scored.sort(key=lambda x: (-x[0], x[1].get("priority", PRIORITY_SEMANTIC_WRAPPER)))
    return [cap for _, cap in scored[:max_results]]


def get_capabilities_for_executive_brief():
    """
    Returns all capabilities that support executive-level analysis.
    Used by the Planner for broad queries like 'How is our company performing?'.
    Metadata-driven -- zero hardcoded capability ID lists.
    """
    return [
        cap for cap in BUSINESS_CAPABILITIES
        if (cap.get("response_contract") or {}).get("supports_analysis", False)
        and cap.get("business_domain") not in ("customer", "analytics")
        and cap.get("fast_path_eligible", False)
    ]
