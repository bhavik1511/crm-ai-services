import re

questions = [
    "tell me about AAJ Holding",
    "AAJ Holding details",
    "show info for AAJ Holding",
    "what do we know about AAJ Holding",
    "who is AAJ Holding",
    "info on AAJ Holding",
    "AAJ Holding",
    "fetch data for AAJ Holding",
]

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
    r"^(.+)$" # Fallback - just the name
]

metric_words = {'revenue', 'receivable', 'project', 'invoice', 'pipeline', 'proposal',
                'dashboard', 'kpi', 'lead', 'employee', 'timesheet', 'total', 'active',
                'fiscal', 'quarter', 'payment', 'receipt', 'credit', 'note', 'aging'}

for q in questions:
    q_lower = q.lower().strip()
    match_found = False
    for i, pattern in enumerate(customer_patterns):
        match = re.search(pattern, q_lower, re.IGNORECASE)
        if match:
            extracted = match.group(1).strip().rstrip('?.,!')
            if extracted.lower() not in metric_words and len(extracted) > 2:
                print(f"'{q}' -> Matched pattern {i}: Extracted '{extracted}'")
                match_found = True
                break
    if not match_found:
         print(f"'{q}' -> NO MATCH")

