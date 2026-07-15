import sys
import re

history = [{"role": "user", "content": "AAJ Holding"}]
latest_question = history[-1]['content'] if history else ""

print(f"Question: '{latest_question}'")
q_lower = latest_question.lower().strip()
force_customer_report = False
search_term = latest_question.strip()

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
    r"^(.+)$"  # Fallback for just the raw name
]

metric_words = {'revenue', 'receivable', 'project', 'invoice', 'pipeline', 'proposal',
                'dashboard', 'kpi', 'lead', 'employee', 'timesheet', 'total', 'active',
                'fiscal', 'quarter', 'payment', 'receipt', 'credit', 'note', 'aging'}

for pattern in customer_patterns:
    match = re.search(pattern, q_lower, re.IGNORECASE)
    if match:
        extracted = match.group(1).strip().rstrip('?.,!')
        print(f"Extracted: '{extracted}' from pattern {pattern}")
        if extracted.lower() not in metric_words and len(extracted) > 2:
            search_term = extracted
            force_customer_report = True
            break
        else:
            print(f"Skipped because '{extracted}' is too short or in metric_words")

print(f"search_term: '{search_term}', force: {force_customer_report}")
