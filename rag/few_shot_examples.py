from typing import List, Dict

# Examples of highly accurate SQL queries tailored specifically for the Grant Thornton CRM schema
# Each example teaches the LLM a specific pattern (e.g., date filtering, complex joins, metric definitions)

FEW_SHOT_SQL_EXAMPLES: List[Dict[str, str]] = [
    {
        "input": "How many open leads do we have in the service pipeline this year?",
        "query": "SELECT COUNT(*) FROM saleslead sl JOIN m_leadstatus ls ON sl.lead_status_id = ls.id WHERE ls.name = 'Open' AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "What is the total value of our service pipeline right now?",
        "query": "SELECT ROUND(COALESCE(SUM(sl.budget_value), 0), 2) FROM saleslead sl JOIN m_leadstatus ls ON sl.lead_status_id = ls.id WHERE ls.name = 'Open' AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "Can you give me the breakdown of service leads by their status?",
        "query": "SELECT ls.name AS status, COUNT(sl.id) AS total FROM m_leadstatus ls LEFT JOIN saleslead sl ON sl.lead_status_id = ls.id AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY ls.id, ls.name"
    },
    {
        "input": "How many job estimations are there grouped by status?",
        "query": "SELECT js.name AS status, COUNT(je.id) AS total FROM m_jobestimation_status js LEFT JOIN job_estimation je ON je.status_id = js.id AND je.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY js.id, js.name"
    },
    {
        "input": "How many open proposals do we have and what is their total budget?",
        "query": "SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.proposal_status_id IN (1, 7, 8) AND p.project_id IS NULL AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "What are the high-value proposals by customer?",
        "query": "SELECT p.id AS proposal_id, COALESCE(c.customer_name, co.cd_company_name, co.first_name, 'N/A') AS client_name, p.total_costs AS budget_value, DATEDIFF(CURDATE(), p.created_at) AS age_in_days FROM proposal p LEFT JOIN customers c ON p.client_id = c.id LEFT JOIN contacts co ON p.contact_id = co.id WHERE p.is_active = 1 AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' ORDER BY p.total_costs DESC, p.created_at DESC LIMIT 5"
    },
    {
        "input": "Show me the count and total value of open engagement letters.",
        "query": "SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.engagement_status_id IN (3, 4, 5) AND p.project_id IS NULL AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "List the total number and budget of proposals by proposal status.",
        "query": "SELECT ps.name, COUNT(p.id) AS total, ROUND(COALESCE(SUM(p.agreed_fees),0),2) AS budget FROM m_proposal_status ps LEFT JOIN proposal p ON p.proposal_status_id = ps.id AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE ps.id IN (1,7,8) GROUP BY ps.id, ps.name ORDER BY ps.sequence"
    },
    {
        "input": "What is our total outstanding receivables amount?",
        "query": "SELECT ROUND(SUM(remaining), 2) AS total_receivables FROM (SELECT i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0) AS remaining FROM invoice i WHERE i.is_active = 1 AND i.payment_status_id NOT IN (2, 4)) sub WHERE remaining != 0"
    },
    {
        "input": "Give me the receivables broken down by ageing bucket (0-30 days, 30-60, etc.)",
        "query": "SELECT CASE WHEN DATEDIFF(CURDATE(), i.created_at) < 30 THEN '<30 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 60 THEN '30-60 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days' ELSE '>365 Days' END AS bucket, ROUND(SUM(i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)), 2) AS amount FROM invoice i WHERE i.is_active = 1 AND i.payment_status_id NOT IN (2, 4) AND (i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)) != 0 GROUP BY bucket"
    },
    {
        "input": "What is our total revenue for the current fiscal year?",
        "query": "SELECT ROUND(SUM(total_amt_ex_vat), 2) AS revenue FROM invoice WHERE is_active = 1 AND created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "Show me revenue grouped by month.",
        "query": "SELECT DATE_FORMAT(created_at, '%b-%Y') AS month, ROUND(SUM(total_amt_ex_vat), 2) AS amount FROM invoice WHERE is_active = 1 AND created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY DATE_FORMAT(created_at, '%b-%Y') ORDER BY MIN(created_at)"
    },
    {
        "input": "How is each service line performing based on invoice amounts?",
        "query": "SELECT sl.name AS service_line, sl.short_code, ROUND(SUM(i.total_amt_ex_vat), 2) AS performing FROM m_serviceline sl JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code"
    },
    {
        "input": "Compare the GP performance (target vs actual performing) by service line.",
        "query": "SELECT sl.name, sl.short_code, ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS performing, COALESCE((SELECT ROUND(SUM(km.target_value), 2) FROM kpi_master km JOIN serviceline_department sd ON km.department_id = sd.department_id WHERE sd.serviceline_id = sl.id), 0) AS target FROM m_serviceline sl LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code HAVING performing > 0 OR target > 0"
    },
    {
        "input": "How many projects do we have categorized by Active, WIP, and Completed?",
        "query": "SELECT CASE WHEN ps.id IN (1, 2) THEN 'Active' WHEN ps.id = 5 THEN 'WIP' WHEN ps.id IN (6,7,8,9,10) THEN 'Completed' END AS category, COUNT(p.id) AS total FROM m_project_status ps LEFT JOIN projects p ON p.status_id = ps.id GROUP BY category HAVING category IS NOT NULL"
    },
    {
        "input": "What is the total count of active projects?",
        "query": "SELECT COUNT(*) FROM projects WHERE status_id IN (1, 2) AND is_active = 1"
    },
    {
        "input": "List the 50 most recent projects with their client, service line, status, and incharge.",
        "query": "SELECT p.name, p.code, c.customer_name AS client, sl.name AS service_line, ps.name AS status, e.employee_name AS incharge FROM projects p JOIN customers c ON p.client = c.id JOIN m_serviceline sl ON p.service_line_id = sl.id JOIN m_project_status ps ON p.status_id = ps.id JOIN employees e ON p.incharge = e.id WHERE p.is_active = 1 ORDER BY p.created_at DESC LIMIT 50"
    },
    {
        "input": "How many leads came from each lead source (Internal vs External)?",
        "query": "SELECT sl.lead_source, COUNT(*) AS total FROM saleslead sl WHERE sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY sl.lead_source"
    },
    {
        "input": "What is the total value of leads coming from each lead source?",
        "query": "SELECT sl.lead_source, ROUND(COALESCE(SUM(sl.budget_value), 0), 2) AS total_value FROM saleslead sl WHERE sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY sl.lead_source"
    },
    {
        "input": "How many employees are in each department?",
        "query": "SELECT d.name AS department, COUNT(e.id) AS count FROM employees e JOIN m_department d ON e.emp_department_id = d.id WHERE e.is_active = 1 GROUP BY d.id, d.name"
    },
    {
        "input": "List up to 50 active employees with their department and designation.",
        "query": "SELECT e.employee_name, e.code, d.name AS department, des.name AS designation FROM employees e LEFT JOIN m_department d ON e.emp_department_id = d.id LEFT JOIN m_designation des ON e.emp_designation_id = des.id WHERE e.is_active = 1 ORDER BY e.employee_name LIMIT 50"
    },
    {
        "input": "How many active customers do we currently have?",
        "query": "SELECT COUNT(*) FROM customers WHERE is_active = 1"
    },
    {
        "input": "Show me a list of 50 active customers with their code and email.",
        "query": "SELECT customer_name, cust_code, cust_email FROM customers WHERE is_active = 1 ORDER BY customer_name LIMIT 50"
    },
    {
        "input": "Give me a breakdown of project tasks by their status.",
        "query": "SELECT status, COUNT(*) AS total FROM project_tasks GROUP BY status"
    },
    {
        "input": "List up to 50 tasks that are overdue, including the project name and assignee.",
        "query": "SELECT pt.name AS task, p.name AS project, e.employee_name AS assignee, pt.due_date, pt.priority FROM project_tasks pt JOIN projects p ON pt.project_id = p.id JOIN employees e ON pt.assignee_id = e.id WHERE pt.status != 'Finished' AND pt.due_date < CURDATE() ORDER BY pt.due_date LIMIT 50"
    },
    {
        "input": "Which employee has the most project assignments?",
        "query": "SELECT e.employee_name, COUNT(p.id) AS project_count FROM employees e JOIN projects p ON p.incharge = e.id WHERE p.is_active = 1 AND e.is_active = 1 GROUP BY e.id, e.employee_name ORDER BY project_count DESC LIMIT 10"
    },
    {
        "input": "Which customer has the highest total invoice value?",
        "query": "SELECT c.customer_name, ROUND(SUM(i.total_amt_ex_vat), 2) AS total_invoiced FROM customers c JOIN invoice i ON i.client_id = c.id WHERE i.is_active = 1 GROUP BY c.id, c.customer_name ORDER BY total_invoiced DESC LIMIT 10"
    },
    {
        "input": "Show top 5 customers by revenue",
        "query": "SELECT c.customer_name, ROUND(SUM(i.total_amt_ex_vat), 2) AS revenue FROM customers c JOIN invoice i ON i.client_id = c.id WHERE i.is_active = 1 GROUP BY c.id, c.customer_name ORDER BY revenue DESC LIMIT 5"
    },
    {
        "input": "Show me details for proposal 33620",
        "query": "SELECT p.id, p.doc_no, p.name AS proposal_name, c.customer_name AS client, sl.name AS service_line, ps.name AS status, ROUND(p.agreed_fees, 2) AS agreed_fees, p.created_at FROM proposal p LEFT JOIN customers c ON p.client_id = c.id LEFT JOIN m_serviceline sl ON p.serviceline_id = sl.id LEFT JOIN m_proposal_status ps ON p.proposal_status_id = ps.id WHERE p.doc_no = '33620' OR p.id = 33620 LIMIT 1"
    },
    {
        "input": "How many employees joined in 2024?",
        "query": "SELECT COUNT(*) AS count FROM employees WHERE emp_join_date >= '2024-01-01' AND emp_join_date < '2025-01-01' AND is_active = 1"
    },
    {
        "input": "Show me employees who joined this year with their department",
        "query": "SELECT e.employee_name, e.code, d.name AS department, e.emp_join_date FROM employees e LEFT JOIN m_department d ON e.emp_department_id = d.id WHERE e.emp_join_date >= '2025-10-01' AND e.is_active = 1 ORDER BY e.emp_join_date DESC LIMIT 50"
    },
    {
        "input": "What is the average proposal value by service line?",
        "query": "SELECT sl.name AS service_line, COUNT(p.id) AS proposal_count, ROUND(AVG(p.agreed_fees), 2) AS avg_value, ROUND(SUM(p.agreed_fees), 2) AS total_value FROM m_serviceline sl JOIN proposal p ON p.serviceline_id = sl.id WHERE sl.is_active = 1 GROUP BY sl.id, sl.name ORDER BY avg_value DESC"
    },
    {
        "input": "Show the proposal win rate by service line",
        "query": "SELECT sl.name AS service_line, COUNT(p.id) AS total_proposals, SUM(CASE WHEN p.proposal_status_id = 2 THEN 1 ELSE 0 END) AS won, ROUND(SUM(CASE WHEN p.proposal_status_id = 2 THEN 1 ELSE 0 END) * 100.0 / COUNT(p.id), 1) AS win_rate_pct FROM m_serviceline sl JOIN proposal p ON p.serviceline_id = sl.id WHERE sl.is_active = 1 GROUP BY sl.id, sl.name HAVING total_proposals > 0 ORDER BY win_rate_pct DESC"
    },
    {
        "input": "What is the GOSI deduction for employee ID 5?",
        "query": "SELECT e.employee_name, e.gosi_salary, e.gosi_deduction, CASE WHEN e.gosi_deduction = 1 THEN ROUND(e.gosi_salary * 7 / 100, 3) ELSE 0 END AS gosi_amount FROM employees e WHERE e.id = 5"
    },
    {
        "input": "Show me all invoices for a specific customer Al Salam Bank",
        "query": "SELECT i.id, i.invoice_no, i.total_amt_ex_vat, i.total_amt_inc_vat, i.payment_status_id, i.created_at FROM invoice i JOIN customers c ON i.client_id = c.id WHERE c.customer_name LIKE '%Al Salam%' AND i.is_active = 1 ORDER BY i.created_at DESC LIMIT 50"
    },
    {
        "input": "How many total records in the business development report this year?",
        "query": "SELECT COUNT(p.id) FROM projects p LEFT JOIN proposal prop ON p.proposal_id = prop.id WHERE p.is_active = 1 AND p.created_at >= '2025-10-01'"
    },
    {
        "input": "Show details in the business development report",
        "query": "SELECT p.code AS project_code, p.name AS project_name, e_incharge.employee_name AS incharge, sl.name AS service_line, prop.agreed_fees AS approved_fees FROM projects p LEFT JOIN proposal prop ON p.proposal_id = prop.id LEFT JOIN m_serviceline sl ON p.serviceline_id = sl.id LEFT JOIN employees e_incharge ON p.incharge = e_incharge.id WHERE p.is_active = 1 ORDER BY p.created_at DESC LIMIT 50"
    },
    {
        "input": "How many credit notes were issued this fiscal year?",
        "query": "SELECT COUNT(*) AS count, ROUND(SUM(total_amount), 2) AS total_value FROM credit_note WHERE created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'"
    },
    {
        "input": "Show total hours logged by each employee this month",
        "query": "SELECT e.employee_name, ROUND(SUM(TIME_TO_SEC(tpd.hours)) / 3600, 2) AS total_hours FROM timesheet_project tp JOIN ts_project_date tpd ON tp.id = tpd.timesheet_id JOIN employees e ON tp.employee_id = e.id WHERE tp.status_id = 3 AND tpd.project_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01') GROUP BY e.id, e.employee_name ORDER BY total_hours DESC LIMIT 50"
    },
    {
        "input": "Which service line has the best recoverability?",
        "query": "SELECT sl.name AS service_line, ROUND(SUM(i.total_amt_ex_vat), 2) AS total_fees, ROUND(SUM(p.total_cost), 2) AS total_costs, CASE WHEN SUM(p.total_cost) > 0 THEN ROUND(SUM(i.total_amt_ex_vat) / SUM(p.total_cost) * 100, 1) ELSE 0 END AS recoverability_pct FROM m_serviceline sl JOIN projects p ON p.service_line_id = sl.id LEFT JOIN invoice i ON i.project_id = p.id AND i.is_active = 1 WHERE sl.is_active = 1 GROUP BY sl.id, sl.name ORDER BY recoverability_pct DESC"
    },
    {
        "input": "What are the top 5 highest value invoices this year?",
        "query": "SELECT i.invoice_no, c.customer_name, sl.name AS service_line, ROUND(i.total_amt_ex_vat, 2) AS amount, i.created_at FROM invoice i JOIN customers c ON i.client_id = c.id LEFT JOIN m_serviceline sl ON i.service_line_id = sl.id WHERE i.is_active = 1 AND i.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' ORDER BY i.total_amt_ex_vat DESC LIMIT 5"
    },
    {
        "input": "List all projects for customer Al Salam Bank",
        "query": "SELECT p.name AS project_name, p.code, sl.name AS service_line, ps.name AS status, e.employee_name AS incharge, p.created_at FROM projects p JOIN customers c ON p.client = c.id LEFT JOIN m_serviceline sl ON p.service_line_id = sl.id LEFT JOIN m_project_status ps ON p.status_id = ps.id LEFT JOIN employees e ON p.incharge = e.id WHERE c.customer_name LIKE '%Al Salam%' AND p.is_active = 1 ORDER BY p.created_at DESC LIMIT 50"
    },
    {
        "input": "Show me the leave balance for employee Ahmad",
        "query": "SELECT e.employee_name, e.code, COALESCE(e.leave_balance, 0) AS leave_balance FROM employees e WHERE e.employee_name LIKE '%Ahmad%' AND e.is_active = 1"
    },
    {
        "input": "What is the resource utilization for Sulaiman Thowfeek?",
        "query": "SELECT p.name AS project_name, ROUND(SUM(TIME_TO_SEC(tpd.hours)) / 3600, 2) AS total_billable_hours FROM timesheet_project tp JOIN ts_project_date tpd ON tp.id = tpd.timesheet_id JOIN employees e ON tp.employee_id = e.id JOIN projects p ON tp.project_id = p.id WHERE tp.status_id = 3 AND e.employee_name LIKE '%Sulaiman%Thowfeek%' GROUP BY p.id, p.name ORDER BY total_billable_hours DESC"
    }
]

