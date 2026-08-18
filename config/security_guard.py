"""
security_guard.py — Central Security & Confidentiality Guardrail

Provides fuzzy pattern matching with typo tolerance (e.g. 'databse schema', 'db schema',
'confidential info', 'credentials') to prevent internal system or database schema disclosure.
"""
import re
from typing import Optional, Dict, Any

# Robust pattern for confidential queries, internal database schemas, credentials, and source code.
# Includes typo tolerance (e.g., 'databse', 'schem', 'passwrd').
CONFIDENTIAL_PATTERN = r"""(?xi)
    \b(
        data\s*ba?se?\s*sche?ma |
        db\s*sche?ma |
        sche?ma\s*details |
        table\s*struc?ture |
        show\s*tables |
        list\s*tables |
        show\s*data\s*ba?se? |
        db\s*struc?ture |
        list\s*columns |
        sys?tem\s*prompts? |
        env\s*file |
        \.env |
        api\s*keys? |
        secre?t\s*keys? |
        creden?tials |
        source\s*code |
        back?end\s*code |
        db\s*passwords? |
        data\s*ba?se?\s*passwords? |
        confiden?tial\s*(info|information|data|things?|details?|credentials?|passwords?|keys?)? |
        internal\s*(info|information|data|things?|details?|credentials?|passwords?|keys?)
    )\b
    | ^sche?ma\s*$
    | ^tables?\s*$
"""

STANDARD_SECURITY_NOTICE = (
    "Internal database schemas, system configurations, source code, and administrative "
    "security credentials are restricted and cannot be displayed. However, I can help you "
    "analyze customer accounts, revenue trends, project statuses, proposals, and CRM reports! "
    "How can I assist you with your business data?"
)

def check_security_guardrail(question: str) -> Optional[Dict[str, Any]]:
    """
    Checks if a user question requests confidential data, database schemas,
    system credentials, source code, or internal instructions.
    
    Returns a standardized, professional response dict if matched, or None if safe.
    """
    if not question:
        return None
        
    q = question.strip()
    if re.search(CONFIDENTIAL_PATTERN, q):
        return {
            "answer": STANDARD_SECURITY_NOTICE,
            "content": STANDARD_SECURITY_NOTICE,
            "navigate_to": None,
            "navigation_links": [],
            "suggested_questions": [
                "Show top revenue generating customers",
                "What is the status of recent proposals?",
                "Show active projects summary",
                "Show KPI summary report"
            ],
            "chart_data": None,
            "export_data": None,
            "auto_expand": False,
            "kpi_payload": None,
            "is_security_block": True,
            "type": "done"
        }
        
    return None
