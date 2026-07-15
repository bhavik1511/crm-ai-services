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
        "id": "customer_resolution",
        "description": "Find a customer by name or code to get their ID. Always required before querying customer-specific data.",
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
            }
        ]
    },
    {
        "id": "customer_360_profile",
        "description": "Get a comprehensive 360-degree profile of a specific customer, including bank details, branches, and contact info.",
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
            }
        ]
    },
    {
        "id": "receivables_analysis",
        "description": "Full ageing breakdown of all overdue invoices. Use when asking about pending or overdue receivables.",
        "required_business_context": {},
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/receivable-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "revenue_analysis",
        "description": "Gets total revenue, revenue by month, and team billing for a specific fiscal year. Use when asking about revenue performance or trends.",
        "required_parameters": [],
        "optional_parameters": {
            "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD), e.g. FY start"},
            "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD), e.g. FY end"}
        },
        "clarification_order": [],
        "implementations": [
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
            },
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_revenue_metrics",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "proposal_search",
        "description": "Search proposals by code, customer, or status. Use when asked to show pending or open proposals.",
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
            }
        ]
    },
    {
        "id": "update_proposal_status",
        "description": "Updates the status of a proposal.",
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
        "description": "Search projects by name, code, or customer.",
        "required_business_context": {
            "search_term": {"type": "string", "description": "Project name or code"}
        },
        "implementations": [
            {
                "priority": PRIORITY_BUSINESS_API,
                "type": "api",
                "endpoint": "GET /api/v1/project?search={search_term}",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": ["search_term"]
            }
        ]
    },
    {
        "id": "create_task",
        "description": "Creates a new task on a specific project.",
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
                "required_parameters": ["task_name"]
            }
        ]
    },
    {
        "id": "kpi_summary",
        "description": "Retrieves the master KPI summary report (budget vs actuals, GP performance).",
        "required_business_context": {},
        "implementations": [
            {
                "priority": PRIORITY_EXISTING_REPORT,
                "type": "report",
                "endpoint": "GET /api/v1/reports/kpi-summary-report",
                "needs_confirmation": False,
                "required_entities": [],
                "required_parameters": []
            }
        ]
    },
    {
        "id": "recoverability_analysis",
        "description": "Gets recoverability metrics, estimated vs actual costs, and recoverability percentage. Use when asked about recoverability.",
        "required_parameters": [],
        "optional_parameters": {
            "start_date": {"type": "string", "format": "date", "description": "Start date (YYYY-MM-DD)"},
            "end_date": {"type": "string", "format": "date", "description": "End date (YYYY-MM-DD)"},
            "financial_year": {"type": "string", "description": "Financial year (e.g. 2026)"}
        },
        "clarification_order": [],
        "implementations": [
            {
                "priority": PRIORITY_SEMANTIC_WRAPPER,
                "type": "wrapper",
                "function_call": "get_project_recoverability_report",
                "needs_confirmation": False,
                "required_entities": ["project"],
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
    }
]

# ---------------------------------------------------------------------------
# Dynamic Registry Extractors
# ---------------------------------------------------------------------------
def get_planner_capabilities_schema() -> list:
    """
    Extracts ONLY the abstract business capabilities.
    The Planner never sees implementations, priorities, endpoints, or SQL.
    """
    schemas = []
    for cap in BUSINESS_CAPABILITIES:
        schemas.append({
            "type": "function",
            "function": {
                "name": cap["id"],
                "description": cap["description"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        **cap.get("required_business_context", {}),
                        **cap.get("optional_parameters", {})
                    },
                    "required": list(cap.get("required_business_context", {}).keys())
                }
            }
        })
    return schemas

def get_capability_metadata(capability_id: str) -> dict:
    """Returns the full metadata for a capability, including implementations."""
    for cap in BUSINESS_CAPABILITIES:
        if cap["id"] == capability_id:
            return cap
    return None
