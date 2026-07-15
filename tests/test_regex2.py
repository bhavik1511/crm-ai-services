import asyncio
import re

# Test regex logic directly
q_lower = "AAJ Holding".lower().strip()
customer_patterns = [
    r"^tell me about\s+(.+)$",
    r"^details?\s+(?:of|about|for)\s+(.+)$",
    r"^(?:show|get|give|provide|fetch)\s+(?:me\s+)?(?:details?|info|information|data|report)\s+(?:of|about|for|on)\s+(.+)$",
    r"^what\s+(?:do\s+)?(?:we\s+)?know\s+about\s+(.+)$",
    r"^(?:customer|client|company)\s+(?:details?|info|report|data)?\s*(?:of|for|about|:)?\s*(.+)$",
    r"^(.+?)\s+(?:company|customer|client)\s+(?:details?|info|report)$",
    r"^(.+?)\s+(?:details?|report|info|information)$",
    r"^who\s+is\s+(.+)$",
    r"^(?:info|information)\s+(?:on|about|of)\s+(.+)$",
    r"^(.+)$"  # Fallback
]
metric_words = {'revenue', 'receivable', 'project', 'invoice', 'pipeline', 'proposal',
                'dashboard', 'kpi', 'lead', 'employee', 'timesheet', 'total', 'active',
                'fiscal', 'quarter', 'payment', 'receipt', 'credit', 'note', 'aging'}

search_term = ""
for pattern in customer_patterns:
    match = re.search(pattern, q_lower, re.IGNORECASE)
    if match:
        extracted = match.group(1).strip().rstrip('?.,!')
        if extracted.lower() not in metric_words and len(extracted) > 2:
            search_term = extracted
            break
print(f"Extracted search term: '{search_term}'")
