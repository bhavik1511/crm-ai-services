"""
business_knowledge.py — Parses business formulas from the existing .md documentation files.

IMPORTANT: This module reads formulas VERBATIM from the user's existing documentation.
It does NOT invent or add any external formulas. If a formula isn't in the docs,
the AI will say "this formula is not documented in our system."

Source files:
- ALL_BACKEND_LOGIC_AND_FORMULAS.md
- MASTER_BACKEND_LOGIC_FINAL (1).md
- COMPLETE_BACKEND_LOGIC_DOCUMENTED.md
- ULTIMATE_BACKEND_REFERENCE.md
"""

import os
from typing import Optional

_knowledge_cache: Optional[str] = None


def _read_doc_file(filename: str) -> str:
    """Read a documentation file from the ai-service directory."""
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[BusinessKnowledge] Warning: Could not read {filename}: {e}")
        return ""


def _build_condensed_knowledge() -> str:
    """
    Read all documentation files and produce a condensed prompt section
    containing the key formulas and business constants.
    """
    # Read the primary formula file (most structured, 769 lines)
    formulas_doc = _read_doc_file("ALL_BACKEND_LOGIC_AND_FORMULAS.md")
    
    # Read supplementary docs for additional context
    master_doc = _read_doc_file("MASTER_BACKEND_LOGIC_FINAL (1).md")
    complete_doc = _read_doc_file("COMPLETE_BACKEND_LOGIC_DOCUMENTED.md")
    ultimate_doc = _read_doc_file("ULTIMATE_BACKEND_REFERENCE.md")
    
    # Build the condensed knowledge prompt
    sections = []
    
    sections.append("""# CRM BUSINESS FORMULAS & CONSTANTS (from official documentation)
# ═══════════════════════════════════════════════════════════════
# IMPORTANT: These formulas are from the official backend documentation.
# Use ONLY these formulas when calculating metrics. Do NOT invent formulas.
# If a user asks for a formula not listed here, say: "This formula is not documented in our system."
""")

    # ── Key Constants ──
    sections.append("""
## KEY BUSINESS CONSTANTS
- Currency: BHD (Bahraini Dinar), 3 decimal places (fils)
- Fiscal Year: October 1 – September 30
- Annual Leave Entitlement: 22 days/year
- GOSI (Social Insurance): 7% of GOSI salary, rounded to 3 decimal places
- Weekend: Saturday (DAYOFWEEK=6), Sunday (DAYOFWEEK=7)
- Approved Status ID: '3'
- Unpaid Leave Type ID: '4'
- Employee Codes: T% = Trainee, F% = Freelancer, numeric = Regular
- Partner Designation IDs: 42, 26
- HR Department ID: 8
- Locale: en-IN for number formatting
""")

    # ── Leave Formulas ──
    sections.append("""
## LEAVE MANAGEMENT FORMULAS
1. Leave Cycle: Oct 1 → Sep 30
   - If month < October: cycle started LAST year
   - If month >= October: cycle started THIS year

2. Pro-Rated Leave (mid-cycle joiners):
   pro_rated_days = (calendar_days_from_join_to_cycle_end / 365) * 22

3. Final Settlement Balance:
   final_balance = current_balance + (days_worked_in_cycle / 365) * 22

4. Annual Leave Reset (Oct 1):
   new_balance = current_balance + 22
""")

    # ── Payroll Formulas ──
    sections.append("""
## PAYROLL CALCULATION FORMULAS
1. GOSI Deduction:
   gosi = ROUND(gosi_salary * 7 / 100, 3)
   Only when employee.gosi_deduction = 1

2. Total Working Days: Count weekdays (Mon-Fri) excluding holidays_setting

3. Employee Worked Days:
   COUNT(DISTINCT ts_project_date.project_date)
   FROM ts_project_date JOIN timesheet_project
   WHERE status_id = '3' (Approved) AND NOT weekend AND NOT holiday

4. Total Leave Days:
   SUM(leave_plans.leave_days WHERE status_id='3') + SUM(leave_request.total_days WHERE status_id='3')

5. Absent Days = total_working_days - paid_leave_days - worked_days

6. Net Salary:
   hours_factor = (actual_hours / standard_hours)
   base = (hours_factor * gross_salary / working_days) * actual_days
   net = base - GOSI - loans - deductions - advances + allowances + bonus

7. Timesheet Hours (HHMM to decimal):
   decimal_hours = FLOOR(hhmm / 10000) + FLOOR((hhmm % 10000) / 100) / 60
""")

    # ── Financial Formulas ──
    sections.append("""
## FINANCIAL CALCULATIONS
1. Invoice:
   total_amt_ex_vat = SUM(line_items.amount)
   total_vat_amount = total_amt_ex_vat * (vat_percentage / 100)
   discount_amount = total_amt_ex_vat * (discount_percentage / 100)
   total_amount = total_amt_ex_vat + total_vat_amount - discount_amount

2. Receipt Tracking:
   On add: new_paid = current_paid + receipt_amount
   On edit: new_paid = current_paid + (new_amount - old_amount)
   On delete: new_paid = current_paid - receipt_amount
   Remaining = total_amount - paid_amount

3. Credit Note:
   total_net_amount = total_amount - SUM(deductions)
""")

    # ── Dashboard Metrics ──
    sections.append("""
## DASHBOARD METRICS FORMULAS
1. Sales Lead Conversion Rate = (closed_leads / total_leads) * 100
2. Budget Achievement % = (closed_budget / total_budget) * 100
3. Task Completion % = (completed_tasks / total_tasks) * 100
4. Utilization Rate = (total_hours_worked / total_standard_hours) * 100
5. Recoverability Rate = (total_fees / total_costs) * 100
6. Project Overdue % = (overdue_projects / total_projects) * 100
7. Attendance % = (worked_hours / working_hours) * 100
8. Performance Score = (total_score / max_score) * 100
9. Fee Recovery % = (approved_fees / total_actual_cost) * 100
""")

    # Append the full formula doc for detailed reference
    if formulas_doc:
        # Truncate if too large (keep under 6000 chars for prompt budget)
        if len(formulas_doc) > 6000:
            sections.append("\n## DETAILED FORMULA REFERENCE (condensed)\n")
            sections.append(formulas_doc[:6000] + "\n... [truncated for prompt size]")
        else:
            sections.append("\n## DETAILED FORMULA REFERENCE\n")
            sections.append(formulas_doc)

    return "\n".join(sections)


def get_business_knowledge_prompt() -> str:
    """
    Returns the condensed business knowledge as a prompt string.
    Cached after first call (formulas don't change at runtime).
    """
    global _knowledge_cache
    
    if _knowledge_cache is not None:
        return _knowledge_cache
    
    try:
        print("[BusinessKnowledge] Loading business formulas from documentation...")
        _knowledge_cache = _build_condensed_knowledge()
        
        # Save to file for debugging
        debug_path = os.path.join(os.path.dirname(__file__), "business_knowledge_snapshot.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(_knowledge_cache)
        
        print(f"[BusinessKnowledge] Loaded {len(_knowledge_cache)} chars of business knowledge")
        return _knowledge_cache
    except Exception as e:
        print(f"[BusinessKnowledge] ERROR: {e}")
        return "# Business formulas unavailable. Ask the user to refer to the CRM documentation."


def get_formula_for_topic(topic: str) -> str:
    """
    Returns the relevant formula section for a given topic keyword.
    Useful for targeted injection when the full knowledge is too large.
    """
    knowledge = get_business_knowledge_prompt()
    topic_lower = topic.lower()
    
    # Map topics to section headers
    section_map = {
        "leave": "LEAVE MANAGEMENT",
        "payroll": "PAYROLL CALCULATION",
        "salary": "PAYROLL CALCULATION",
        "gosi": "PAYROLL CALCULATION",
        "invoice": "FINANCIAL CALCULATIONS",
        "receipt": "FINANCIAL CALCULATIONS",
        "credit": "FINANCIAL CALCULATIONS",
        "conversion": "DASHBOARD METRICS",
        "utilization": "DASHBOARD METRICS",
        "recoverability": "DASHBOARD METRICS",
        "attendance": "DASHBOARD METRICS",
        "performance": "DASHBOARD METRICS",
    }
    
    for keyword, header in section_map.items():
        if keyword in topic_lower:
            # Extract the relevant section
            start = knowledge.find(f"## {header}")
            if start == -1:
                continue
            next_section = knowledge.find("\n## ", start + 1)
            if next_section == -1:
                return knowledge[start:]
            return knowledge[start:next_section]
    
    return ""
