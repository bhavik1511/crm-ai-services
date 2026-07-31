# CRM MASTER BUSINESS LOGIC AND FORMULAS

## 1. SYSTEM OVERVIEW & CONSTANTS
- Currency: BHD (Bahraini Dinar), rounded to 3 decimal places (fils).
- Fiscal Year: October 1 to September 30.
- Annual Leave Entitlement: 22 days per year.
- GOSI (Social Insurance Deduction): 7% of GOSI salary, rounded to 3 decimal places.
- Weekend Days: Saturday (DAYOFWEEK=6) and Sunday (DAYOFWEEK=7).
- Approved Status ID: '3'.
- Unpaid Leave Type ID: '4'.
- Employee Codes: T% = Trainee, F% = Freelancer, Numeric = Regular Employee.
- Partner Designation IDs: 42, 26.
- HR Department ID: 8.

## 2. LEAVE MANAGEMENT FORMULAS
- Leave Cycle: Oct 1 → Sep 30.
  - If current month < October: cycle started LAST year.
  - If current month >= October: cycle started THIS year.
- Pro-Rated Leave (Mid-Cycle Joiners):
  pro_rated_days = (calendar_days_from_join_to_cycle_end / 365) * 22
- Final Settlement Balance:
  final_balance = current_balance + (days_worked_in_cycle / 365) * 22
- Annual Leave Reset (Oct 1):
  new_balance = current_balance + 22

## 3. PAYROLL CALCULATION FORMULAS
- GOSI Deduction:
  gosi = ROUND(gosi_salary * 7 / 100, 3)
  Applied only when employee.gosi_deduction = 1.
- Total Working Days:
  Count weekdays (Monday through Friday) excluding company holidays setting.
- Employee Worked Days:
  COUNT(DISTINCT ts_project_date.project_date)
  FROM ts_project_date JOIN timesheet_project
  WHERE status_id = '3' (Approved) AND NOT weekend AND NOT holiday.
- Total Leave Days:
  SUM(leave_plans.leave_days WHERE status_id='3') + SUM(leave_request.total_days WHERE status_id='3')
- Absent Days:
  absent_days = total_working_days - paid_leave_days - worked_days
- Net Salary Calculation:
  hours_factor = (actual_hours / standard_hours)
  base_salary = (hours_factor * gross_salary / working_days) * actual_days
  net_salary = base_salary - gosi_deduction - loans - deductions - advances + allowances + bonus
- Timesheet Hours Conversion (HHMM to Decimal):
  decimal_hours = FLOOR(hhmm / 10000) + FLOOR((hhmm % 10000) / 100) / 60

## 4. FINANCIAL & INVOICE CALCULATIONS
- Invoice Totals:
  total_amt_ex_vat = SUM(line_items.amount)
  total_vat_amount = total_amt_ex_vat * (vat_percentage / 100)
  discount_amount = total_amt_ex_vat * (discount_percentage / 100)
  total_amount = total_amt_ex_vat + total_vat_amount - discount_amount
- Receipt & Outstanding Tracking:
  new_paid = current_paid + receipt_amount
  remaining_outstanding = total_amount - paid_amount
- Credit Note Net Amount:
  total_net_amount = total_amount - SUM(deductions)

## 5. DASHBOARD & KPI REPORT FORMULAS
- Sales Lead Conversion Rate:
  conversion_rate = (closed_leads / total_leads) * 100
- Budget Achievement %:
  budget_achievement_pct = (closed_budget / total_budget) * 100
- Task Completion %:
  task_completion_pct = (completed_tasks / total_tasks) * 100
- Staff Utilization Rate:
  utilization_rate = (total_hours_worked / total_standard_hours) * 100
- Recoverability Rate:
  recoverability_rate = (total_fees / total_costs) * 100
- Project Overdue %:
  overdue_pct = (overdue_projects / total_projects) * 100
- Attendance %:
  attendance_pct = (worked_hours / working_hours) * 100
- Fee Recovery %:
  fee_recovery_pct = (approved_fees / total_actual_cost) * 100
- KPI Billing Revenue & Gross Profit Table Calculation:
  total_invoice_amount = SUM(invoiceMap.get(month))
  total_credit_amount = SUM(CreditNotMap.get(month))
  target_value = kpiMap.get(month).value
  target_gp = kpiMap.get(month).gp
  total_rem_amount = SUM(receivableMap.get(month))
