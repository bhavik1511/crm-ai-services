"""
secure_log_sanitizer.py — Centralized Application Log Sanitizer.
===================================================================
Ensures raw CRM financial values, employee/customer PII, and sensitive secrets
are NEVER emitted into application logs, exception traces, or debug replays.

Fulfills strict fail-closed log sanitization while preserving observability metadata.
"""

import re
import copy
import json
import logging
from typing import Any, Dict, List, Set, Union

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Declarative Config: Redaction Constants
# ---------------------------------------------------------------------------
REDACTED_FINANCIAL = "[REDACTED_FINANCIAL_VALUE]"
REDACTED_PII = "[REDACTED_PII]"
REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_FAIL_CLOSED = "[LOG_SANITIZATION_FAILED_FAIL_CLOSED]"

# ---------------------------------------------------------------------------
# Declarative Config: Preserved Telemetry / Metadata Keys (Exact Case-Insensitive)
# ---------------------------------------------------------------------------
TELEMETRY_KEYS: Set[str] = {
    "request_id", "session_id", "capability", "capability_id", "endpoint",
    "backend_endpoint_selected", "http_status", "status", "operation", "metric",
    "dimension", "records_returned", "row_count", "rows_count", "rows_returned",
    "fields", "validation_status", "execution_time", "planner_ms",
    "entity_resolver_ms", "tool_execution_ms", "synthesizer_ms", "total_ms",
    "llm_calls", "llm_call_count", "planner_tokens", "synth_tokens",
    "total_tokens", "authoritative", "confidence", "presentation_mode",
    "is_valid", "errors", "blocked_reason", "source", "report_type",
    "has_chart", "is_clarification", "action", "reason", "export_available",
    "count", "strictly_active_projects_count", "total_projects_all_statuses_combined",
    "request_correlation_id", "timings_ms", "conversation_replay", "timestamp",
    "1_original_user_query", "2_planner_output", "3_entity_resolver_output",
    "4_tool_registry_output", "5_backend_response", "6_synthesizer_input",
    "7_final_response", "selected_business_capabilities", "extracted_filters",
    "missing_information", "query_parameters", "request_body", "execution_validation",
    "status_name", "short_code", "name", "capability_id", "id", "type",
    "temporal_scope", "financial_year", "start_date", "end_date", "dimension",
    "content_preview", "was_cached", "cache_tier", "is_edit_intent",
    "show_fy_picker", "navigate_to", "suggested_questions", "auto_expand"
}

# ---------------------------------------------------------------------------
# Declarative Config: Sensitive Field Key Patterns (Case-Insensitive Substring Match)
# ---------------------------------------------------------------------------
FINANCIAL_KEY_PATTERNS = [
    "revenue", "profit", "invoice", "credit", "billing", "salary", "cost",
    "staff_cost", "budget", "secured_business", "balance_to_achieve",
    "project_in_hand", "open_proposals", "paid_amount", "unpaid_amount",
    "receivables", "outstanding", "overdue", "unbilled", "collected",
    "fee", "pricing", "target", "performing", "variance", "amount"
]

PII_KEY_PATTERNS = [
    "employee_name", "emp_name", "customer_name", "client_name", "contact_name",
    "person_name", "user_name", "username", "email", "phone", "mobile",
    "address", "ssn", "pan", "account_number", "bank_account", "passport",
    "tax_id", "canonical_name", "employee_id", "customer_id", "client_id"
]

SECRET_KEY_PATTERNS = [
    "jwt_token", "authorization", "auth_token", "bearer", "access_token",
    "refresh_token", "secret", "password", "api_key", "apikey", "private_key",
    "token", "jwt"
]

# Regex patterns for string value scanning
EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
JWT_BEARER_REGEX = re.compile(r'ey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._-]+', re.IGNORECASE)


def _classify_key(key: str) -> str:
    """Classifies a dictionary key as 'TELEMETRY', 'SECRET', 'PII', 'FINANCIAL', or 'UNKNOWN'."""
    k_lower = str(key).lower().strip()
    
    # 1. Exact telemetry key match (highest priority for preservation)
    if k_lower in TELEMETRY_KEYS:
        return "TELEMETRY"
    
    # 2. Check secret patterns
    for p in SECRET_KEY_PATTERNS:
        if p in k_lower:
            return "SECRET"
            
    # 3. Check PII patterns
    for p in PII_KEY_PATTERNS:
        if p in k_lower:
            return "PII"
            
    # 4. Check financial patterns
    for p in FINANCIAL_KEY_PATTERNS:
        if p in k_lower:
            return "FINANCIAL"
            
    return "UNKNOWN"


def _sanitize_string(val: str) -> str:
    """Scans and redacts embedded secrets and emails from raw string values."""
    if not val:
        return val
        
    # Check for JWT / Bearer secret tokens
    if JWT_BEARER_REGEX.search(val):
        val = JWT_BEARER_REGEX.sub(REDACTED_SECRET, val)
        
    # Check for email addresses
    if EMAIL_REGEX.search(val):
        val = EMAIL_REGEX.sub(REDACTED_PII, val)
        
    return val


def _sanitize_recursive(val: Any, parent_key: str = None) -> Any:
    """Recursively processes values and replaces confidential values with redaction placeholders."""
    if val is None or isinstance(val, bool):
        return val
        
    if parent_key:
        category = _classify_key(parent_key)
        if category == "FINANCIAL":
            return REDACTED_FINANCIAL
        elif category == "PII":
            return REDACTED_PII
        elif category == "SECRET":
            return REDACTED_SECRET
            
    if isinstance(val, dict):
        sanitized_dict = {}
        for k, v in val.items():
            cat = _classify_key(k)
            if cat == "FINANCIAL":
                sanitized_dict[k] = REDACTED_FINANCIAL
            elif cat == "PII":
                sanitized_dict[k] = REDACTED_PII
            elif cat == "SECRET":
                sanitized_dict[k] = REDACTED_SECRET
            else:
                sanitized_dict[k] = _sanitize_recursive(v, parent_key=k)
        return sanitized_dict

    elif isinstance(val, (list, tuple, set)):
        items = [_sanitize_recursive(item, parent_key=parent_key) for item in val]
        if isinstance(val, tuple):
            return tuple(items)
        if isinstance(val, set):
            return set(items)
        return items

    elif isinstance(val, str):
        # Attempt to parse as JSON if it looks like serialized JSON
        val_trimmed = val.strip()
        if (val_trimmed.startswith('{') and val_trimmed.endswith('}')) or (val_trimmed.startswith('[') and val_trimmed.endswith(']')):
            try:
                parsed = json.loads(val_trimmed)
                sanitized_parsed = _sanitize_recursive(parsed, parent_key=parent_key)
                return json.dumps(sanitized_parsed)
            except Exception:
                pass
        return _sanitize_string(val)

    elif isinstance(val, (int, float)):
        if parent_key:
            cat = _classify_key(parent_key)
            if cat == "FINANCIAL":
                return REDACTED_FINANCIAL
            elif cat == "PII":
                return REDACTED_PII
            elif cat == "SECRET":
                return REDACTED_SECRET
        return val

    else:
        # Fallback for custom objects, Pydantic models, or Exception objects
        try:
            if hasattr(val, "__dict__"):
                return _sanitize_recursive(val.__dict__, parent_key=parent_key)
            else:
                return _sanitize_string(str(val))
        except Exception:
            return REDACTED_FAIL_CLOSED


def sanitize_for_log(value: Any) -> Any:
    """
    Centralized log sanitizer entrypoint.
    
    1. Deep-copies the input value to ensure the original runtime execution object is never mutated.
    2. Recursively sanitizes sensitive financial amounts, employee/customer PII, and secret tokens.
    3. Emits [LOG_SANITIZATION] status=PASS on success, or status=FAIL and returns fail-closed placeholder on error.
    """
    try:
        val_copy = copy.deepcopy(value)
        sanitized = _sanitize_recursive(val_copy)
        logger.debug("[LOG_SANITIZATION] status=PASS")
        return sanitized
    except Exception as e:
        logger.error(f"[LOG_SANITIZATION] status=FAIL error={e}")
        return REDACTED_FAIL_CLOSED
