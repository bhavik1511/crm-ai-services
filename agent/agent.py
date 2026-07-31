import re
import os
import asyncio
import json
import traceback
from typing import List, Dict, Tuple, Optional, Any, Annotated, Union
from datetime import datetime
import hashlib
from dotenv import load_dotenv
from sqlalchemy import text
from openai import AsyncOpenAI
from db.database import get_db_engine

# LangChain / LangGraph Imports
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from config.role_tier_config import build_rbac_prompt
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda, RunnableBranch
from langchain_core.output_parsers import StrOutputParser
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
# MongoDB + Redis Vector Cache Integration (Replacing ChromaDB)
from rag.vector_store_v2 import get_cached_answer, store_vector_cache as store_answer

# Local modules
from semantic import semantic_layer
from semantic.semantic_layer import ALL_SEMANTIC_TOOLS
from rag.knowledge_base import search_documentation
from rag.few_shot_examples import FEW_SHOT_SQL_EXAMPLES
# RAG Knowledge Store — vector search over ai_knowledge_base
from rag.rag_knowledge_store import search_knowledge as _rag_search

from .intent_classifier import classify_intent, should_show_kpi_filters, should_show_revenue_report, should_show_receivables_report, should_show_proposals_report, should_show_projects_report, should_show_resources_report

load_dotenv()

def get_fiscal_info(dt=None):
    if dt is None:
        dt = datetime.now()
    
    # Fiscal year starts Oct 1
    if dt.month >= 10:
        fy_start_year = dt.year
        fy_end_year = dt.year + 1
    else:
        fy_start_year = dt.year - 1
        fy_end_year = dt.year
        
    fy_start = f"{fy_start_year}-10-01"
    fy_end = f"{fy_end_year}-09-30 23:59:59"
    
    # Fiscal Quarters: Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep
    if dt.month in [10, 11, 12]:
        current_q = "Q1"
        q_start = f"{fy_start_year}-10-01"
        q_end = f"{fy_start_year}-12-31 23:59:59"
        last_q = "Q4"
        last_q_start = f"{fy_start_year - 1}-07-01"
        last_q_end = f"{fy_start_year - 1}-09-30 23:59:59"
    elif dt.month in [1, 2, 3]:
        current_q = "Q2"
        q_start = f"{fy_end_year}-01-01"
        q_end = f"{fy_end_year}-03-31 23:59:59"
        last_q = "Q1"
        last_q_start = f"{fy_start_year}-10-01"
        last_q_end = f"{fy_start_year}-12-31 23:59:59"
    elif dt.month in [4, 5, 6]:
        current_q = "Q3"
        q_start = f"{fy_end_year}-04-01"
        q_end = f"{fy_end_year}-06-30 23:59:59"
        last_q = "Q2"
        last_q_start = f"{fy_end_year}-01-01"
        last_q_end = f"{fy_end_year}-03-31 23:59:59"
    else:
        current_q = "Q4"
        q_start = f"{fy_end_year}-07-01"
        q_end = f"{fy_end_year}-09-30 23:59:59"
        last_q = "Q3"
        last_q_start = f"{fy_end_year}-04-01"
        last_q_end = f"{fy_end_year}-06-30 23:59:59"
        
    return {
        "fy_start": fy_start,
        "fy_end": fy_end,
        "current_fy_quarter": current_q,
        "current_fy_q_start": q_start,
        "current_fy_q_end": q_end,
        "last_fy_quarter": last_q,
        "last_fy_q_start": last_q_start,
        "last_fy_q_end": last_q_end,
        "fy_name": f"FY {fy_start_year}-{fy_end_year}" 
    }

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
DANGEROUS_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|EXEC|EXECUTE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

SQL_CONTEXT_HINTS = re.compile(
    r"(\bfrom\b|\binto\b|\bset\b|\bwhere\b|\btable\b|;|--|/\*|\*/)",
    re.IGNORECASE,
)

def _looks_like_sql_write_attempt(user_text: str) -> bool:
    """Detect likely SQL write/injection attempts while avoiding natural-language false positives."""
    if not user_text:
        return False
    has_write_keyword = bool(DANGEROUS_SQL.search(user_text))
    has_sql_context = bool(SQL_CONTEXT_HINTS.search(user_text))
    return has_write_keyword and has_sql_context





_client: AsyncOpenAI | None = None
GROQ_PRIMARY_MODEL = os.getenv("PRIMARY_MODEL") or os.getenv("GROQ_MODEL_PRIMARY", "llama-3.3-70b-versatile")
GROQ_FALLBACK_MODEL = os.getenv("FAST_MODEL") or os.getenv("GROQ_MODEL_FALLBACK", "llama-3.1-8b-instant")
try:
    GROQ_RETRY_ATTEMPTS = max(1, int(os.getenv("GROQ_RETRY_ATTEMPTS", "2")))
except Exception:
    GROQ_RETRY_ATTEMPTS = 2


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err).lower()
    return "429" in msg or "rate" in msg or "quota" in msg or "too many requests" in msg


def _groq_model_candidates() -> list[str]:
    models = [GROQ_PRIMARY_MODEL, GROQ_FALLBACK_MODEL]
    deduped = []
    for m in models:
        if m and m not in deduped:
            deduped.append(m)
    return deduped


from config.llm_factory import get_llm

def _build_llm(model_name: Optional[str] = None, temperature: float = 0.0, max_tokens: Optional[int] = None):
    return get_llm(model_name=model_name, temperature=temperature, max_tokens=max_tokens)

def _get_client():
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


# ---------------------------------------------------------------------------
# Comprehensive SQL prompt ΓÇö built from analysis of every model & repository
# ---------------------------------------------------------------------------
SQL_PROMPT = """You are a MySQL expert for Grant Thornton Bahrain's CRM system.
Given a question, write EXACTLY ONE SELECT query. Return ONLY the raw SQL ΓÇö no markdown, no backticks, no explanation.

ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
FISCAL YEAR (CRITICAL)
ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
Current fiscal year: 2025-10-01 to 2026-09-30 23:59:59
"this year" / "current year" / "FY" = above dates.
Fiscal Quarters: Q1=Oct-Dec, Q2=Jan-Mar, Q3=Apr-Jun, Q4=Jul-Sep.

ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
COMPLETE TABLE & COLUMN REFERENCE
ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

CORE TABLES:
- invoice(id, project_id, organization_id, invoice_type, invoice_no, total_amount, total_vat_amount, total_amt_inc_vat, total_amt_ex_vat, paid_amount, discount_percentage, vat_percentage, discount_amount, total_net_amount, total_amt_out_of_pocket, payment_status_id, project_in_charge_id, service_line_id, service_type_id, project_status_id, agreed_fees, client_name_id, is_active, lock, created_at, created_by, updated_at)
- invoice_details(id, invoice_id, stage_name, invoice_percentage, amount, amount_ex_vat, amount_in_vat, vat_percentage, final_amt_inc_vat, discount, remarks, is_active)
- receipt_details(id, receipt_id, invoice_id, invoice_date, invoice_no, invoice_type, invoice_amount, paid_amount, applied_amount, rem_amount, total_net_amount, service_type_id, service_line_id, project_code, project_name, project_in_charge, status, client_relation, project_partner, agreed_fees, client_id, is_active, created_at)
- receipts(id, receipt_no, receipt_type, organization_id, remarks, payment_type_id, bank_id, cheque_no, value_date, amount, rate, converted_amt, remaining_amt, is_active, lock, received_by, created_at, created_by)
- credit_note(id, organization_id, invoice_id, credit_amount, creditNoteNumber, tax_per, tax_amount, out_of_pocket_amount, total_amount, remarks, credit_note_date, is_active, created_at, created_by)

LEADS & PROPOSALS:
- saleslead(id, lead_date, lead_owner, industry_id, client_type, enquiry_details, code, budget_value, currency_id, lead_status_id, job_estimation_id, serviceline_id, servicetype_id, sub_servicetype_id, lead_source, lead_source_external_id, lead_source_existing_client_id, lead_source_internal_id, customer_id, contact_id, is_active, created_at, created_by)
- m_leadstatus(id, name, is_active)
- job_estimation(id, saleslead_id, proposal_id, from_date, to_date, remarks, total_costs, total_hours, proposed_fees, approved_fees, recoverability, contact_id, customer_id, code, ref_no, status_id, is_active, is_vendor, approved_by, created_at, created_by)
- m_jobestimation_status(id, name, is_active) -- IDs: 1=Pending Approvals, 2=Approved, 3=Reviewed, 4=Rejected, 6=Not Submitted
- proposal(id, job_estimation_id, organization_id, project_id, proposal_template_id, engagement_template_id, proposal_status_id, engagement_status_id, continuous_engagement_status_id, scope, proposal_year, proposed_fees, approved_fees, agreed_fees, recoverability, proposal_date, total_costs, code, ref_no, is_active, contact_id, customer_id, client_id, service_line_id, created_at, created_by)
- m_proposal_status(id, name, is_active, sequence) -- IDs: 1=Proposal Sent, 3=Proposal Accepted (Won), 4=Proposal Rejected (Lost), 7=Proposal Created, 8=Proposal Verify, 9=Project Pending, 10=All Project Created
- m_engagement_status(id, name, is_active, sequence) -- IDs: 1=Engagement Accepted (Won), 2=Engagement Rejected (Lost), 3=Engagement Sent, 4=Engagement Created, 5=Engagement Verify

PROJECTS:
- projects(id, name, code, main_incharge, partner, client, client_relation, start_date, end_date, report_sign_date, audit_year, approved_fees, service_line_id, service_type_id, sub_service_type_id, proposal_id, status_id, is_active, created_at, created_by)
- m_project_status(id, name)
- project_tasks(id, project_id, milestone_id, assignee_id, task_id, name, description, due_date, priority, status, tags, created_at, created_by)
- project_team_members(id, project_id, emp_id)

EMPLOYEES & HR:
- employees(id, employee_name, emp_join_date, emp_designation_id, emp_department_id, emp_contract_type, emp_direct_supervisor_name_id, emp_per_gender, is_active, emp_per_nationality_id, emp_per_dob, emp_basic_salary, emp_gross_salary, code, profile_image, emp_comm_business_email, created_at)
- m_department(id, name, code, is_active)
- m_designation(id, name, code, level, is_active)
- designation_rates(id, designation_id, hourly)

MASTERS:
- customers(id, customer_name, cust_code, cust_cr_no, cust_group_id, cust_curr_id, is_active, cust_client_rel_id, cust_comp_type_id, cust_industry_id, cust_email, cust_tel_number, cust_country_id, risk_rating, created_at)
- contacts(id, first_name, middle_name, last_name, cd_company_name, email, mobile_number, is_active, created_at)
- organization(id, name, short_name, cr_no, is_active, created_at)
- m_serviceline(id, name, code, short_code, is_active)
- m_servicetype(id, name, code, service_line_id, is_active)
- m_sub_servicetype(id, name, code, servicetype_id, is_active)
- m_industry_type(id, name, is_active)

TIMESHEET & KPI:
- timesheet_project(id, project_id, employee_id, client_id, status_id, total_hrs, created_at, created_by)
  NOTE: total_hrs is a TIME column (HH:MM:SS). Use SUM(TIME_TO_SEC(total_hrs))/3600 for hours. status_id=3 means Approved.
- ts_project_date(id, timesheet_id, project_date, task_name, hours, description)
  NOTE: ts_project_date.timesheet_id -> timesheet_project.id. project_date is the actual work date.
- kpi_master(id, service_line_id, department_id, employee_id, target_month, target_value, target_gp, is_active)
- serviceline_department(id, serviceline_id, department_id)
- serviceline_incharge(id, serviceline_id, incharge_id)

OTHER:
- leave_request(id, employee_id, leave_type_id, from_date, to_date, total_days, remarks, status_id, is_active, created_at)
- m_leave_status(id, name, is_active)
- client_survey(id, service_line, project_manager, project_name, client_name, remarks, created_at)
- assign_client_survey_question(id, client_survey_id, questions, answers)

ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
KEY JOINS & RELATIONSHIPS
ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
- invoice.client_name_id -> customers.id
- invoice.project_id -> projects.id
- invoice.project_in_charge_id -> employees.id
- invoice.service_line_id -> m_serviceline.id
- invoice.payment_status_id -> m_invoice_status.id
- receipt_details.invoice_id -> invoice.id
- receipt_details.receipt_id -> receipts.id
- receipt_details.client_id -> customers.id
- credit_note.invoice_id -> invoice.id
- saleslead.lead_status_id -> m_leadstatus.id
- saleslead.serviceline_id -> m_serviceline.id
- saleslead.lead_owner -> employees.id
- saleslead.customer_id -> customers.id
- saleslead.created_by -> employees.id
- job_estimation.saleslead_id -> saleslead.id
- job_estimation.status_id -> m_jobestimation_status.id (NOTE: column is status_id NOT job_estimation_status_id)
- proposal.job_estimation_id -> job_estimation.id
- proposal.proposal_status_id -> m_proposal_status.id
- proposal.engagement_status_id -> m_engagement_status.id
- proposal.project_id -> projects.id (NULL = pending/open)
- proposal.service_line_id -> m_serviceline.id
- projects.status_id -> m_project_status.id (NOTE: column is status_id NOT project_status_id)
- projects.main_incharge -> employees.id
- projects.partner -> employees.id
- projects.client -> customers.id
- projects.service_line_id -> m_serviceline.id
- projects.proposal_id -> proposal.id
- employees.emp_department_id -> m_department.id
- employees.emp_designation_id -> m_designation.id
- project_tasks.assignee_id -> employees.id
- project_tasks.project_id -> projects.id
- project_team_members.emp_id -> employees.id
- timesheet_project.project_id -> projects.id
- timesheet_project.employee_id -> employees.id
- kpi_master.service_line_id -> m_serviceline.id
- kpi_master.department_id -> m_department.id
- serviceline_department.serviceline_id -> m_serviceline.id
- serviceline_department.department_id -> m_department.id

ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
EXACT SQL FOR EVERY DASHBOARD KPI
ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ

--- SERVICE PIPELINE ---
-- Open service leads count:
SELECT COUNT(*) FROM saleslead sl JOIN m_leadstatus ls ON sl.lead_status_id = ls.id WHERE ls.name = 'Open' AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Service pipeline value (total budget of open leads):
SELECT ROUND(COALESCE(SUM(sl.budget_value), 0), 2) FROM saleslead sl JOIN m_leadstatus ls ON sl.lead_status_id = ls.id WHERE ls.name = 'Open' AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Total service leads this FY:
SELECT COUNT(*) FROM saleslead WHERE lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Service leads by status (donut chart):
SELECT ls.name AS status, COUNT(sl.id) AS total FROM m_leadstatus ls LEFT JOIN saleslead sl ON sl.lead_status_id = ls.id AND sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY ls.id, ls.name

--- JOB ESTIMATION PIPELINE ---
-- Job estimation by status (Always filter by je.is_active = 1):
SELECT js.name AS status, COUNT(je.id) AS total FROM m_jobestimation_status js LEFT JOIN job_estimation je ON je.status_id = js.id AND je.is_active = 1 AND je.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY js.id, js.name

-- Total job estimations this FY:
SELECT COUNT(*) FROM job_estimation WHERE is_active = 1 AND created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

--- OPEN PROPOSALS / ENGAGEMENT LETTERS ---
-- Open proposals (status IDs 1,7,8 = proposal statuses, project_id IS NULL means pending):
SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.proposal_status_id IN (1, 7, 8) AND p.project_id IS NULL AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Open engagement letters (engagement_status_id IN 3,4,5, project_id IS NULL):
SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(p.agreed_fees), 0), 2) AS total_budget FROM proposal p WHERE p.engagement_status_id IN (3, 4, 5) AND p.project_id IS NULL AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Proposals by proposal status:
SELECT ps.name, COUNT(p.id) AS total, ROUND(COALESCE(SUM(p.agreed_fees),0),2) AS budget FROM m_proposal_status ps LEFT JOIN proposal p ON p.proposal_status_id = ps.id AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE ps.id IN (1,7,8) GROUP BY ps.id, ps.name ORDER BY ps.sequence

-- Engagement letters by status:
SELECT es.name, COUNT(p.id) AS total, ROUND(COALESCE(SUM(p.agreed_fees),0),2) AS budget FROM m_engagement_status es LEFT JOIN proposal p ON p.engagement_status_id = es.id AND p.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE es.id IN (3,4,5) GROUP BY es.id, es.name ORDER BY es.sequence

--- RECEIVABLES ---
-- Total receivables:
SELECT ROUND(SUM(remaining), 2) AS total_receivables FROM (SELECT i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0) AS remaining FROM invoice i WHERE i.is_active = 1 AND i.payment_status_id NOT IN (2, 4)) sub WHERE remaining != 0

-- Receivables by ageing bucket:
SELECT CASE WHEN DATEDIFF(CURDATE(), i.created_at) < 30 THEN '<30 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 60 THEN '30-60 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days' WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days' ELSE '>365 Days' END AS bucket, ROUND(SUM(i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)), 2) AS amount FROM invoice i WHERE i.is_active = 1 AND i.payment_status_id NOT IN (2, 4) AND (i.total_net_amount - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0) - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0)) != 0 GROUP BY bucket

--- REVENUE ---
-- Total revenue this FY:
SELECT ROUND(SUM(total_amt_ex_vat), 2) AS revenue FROM invoice WHERE is_active = 1 AND created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59'

-- Revenue by month:
SELECT DATE_FORMAT(created_at, '%b-%Y') AS month, ROUND(SUM(total_amt_ex_vat), 2) AS amount FROM invoice WHERE is_active = 1 AND created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY DATE_FORMAT(created_at, '%b-%Y') ORDER BY MIN(created_at)

--- SERVICE LINE PERFORMANCE ---
-- Invoice amount by service line (performing):
SELECT sl.name AS service_line, sl.short_code, ROUND(SUM(i.total_amt_ex_vat), 2) AS performing FROM m_serviceline sl JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code

--- GP PERFORMANCE (target vs performing) ---
SELECT sl.name, sl.short_code, ROUND(COALESCE(SUM(i.total_amt_ex_vat), 0), 2) AS performing, COALESCE((SELECT ROUND(SUM(km.target_value), 2) FROM kpi_master km JOIN serviceline_department sd ON km.department_id = sd.department_id WHERE sd.serviceline_id = sl.id), 0) AS target FROM m_serviceline sl LEFT JOIN invoice i ON i.service_line_id = sl.id AND i.is_active = 1 AND i.created_at BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' WHERE sl.is_active = 1 GROUP BY sl.id, sl.name, sl.short_code HAVING performing > 0 OR target > 0

--- PROJECTS ---
-- Projects by status category (Active/WIP/Completed):
SELECT CASE WHEN ps.id IN (1, 2) THEN 'Active' WHEN ps.id = 5 THEN 'WIP' WHEN ps.id IN (6,7,8,9,10) THEN 'Completed' END AS category, COUNT(p.id) AS total FROM m_project_status ps LEFT JOIN projects p ON p.status_id = ps.id GROUP BY category HAVING category IS NOT NULL

-- Total active projects:
SELECT COUNT(*) FROM projects WHERE status_id IN (1, 2) AND is_active = 1

-- Project details with client and service line:
SELECT p.name, p.code, c.customer_name AS client, sl.name AS service_line, ps.name AS status, e.employee_name AS incharge FROM projects p JOIN customers c ON p.client = c.id JOIN m_serviceline sl ON p.service_line_id = sl.id JOIN m_project_status ps ON p.status_id = ps.id JOIN employees e ON p.main_incharge = e.id WHERE p.is_active = 1 ORDER BY p.created_at DESC LIMIT 50

--- LEAD SOURCE ---
-- Lead source breakdown (Internal/Existing Client/External):
SELECT sl.lead_source, COUNT(*) AS total FROM saleslead sl WHERE sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY sl.lead_source

-- Lead source by value:
SELECT sl.lead_source, ROUND(COALESCE(SUM(sl.budget_value), 0), 2) AS total_value FROM saleslead sl WHERE sl.lead_date BETWEEN '2025-10-01' AND '2026-09-30 23:59:59' GROUP BY sl.lead_source

--- EMPLOYEES ---
-- Employee count by department:
SELECT d.name AS department, COUNT(e.id) AS count FROM employees e JOIN m_department d ON e.emp_department_id = d.id WHERE e.is_active = 1 GROUP BY d.id, d.name

-- Employee details:
SELECT e.employee_name, e.code, d.name AS department, des.name AS designation FROM employees e LEFT JOIN m_department d ON e.emp_department_id = d.id LEFT JOIN m_designation des ON e.emp_designation_id = des.id WHERE e.is_active = 1 ORDER BY e.employee_name LIMIT 50

--- CUSTOMERS ---
-- Total active customers:
SELECT COUNT(*) FROM customers WHERE is_active = 1

-- Customer list:
SELECT customer_name, cust_code, cust_email FROM customers WHERE is_active = 1 ORDER BY customer_name LIMIT 50

--- TASKS ---
-- Tasks by status:
SELECT status, COUNT(*) AS total FROM project_tasks GROUP BY status

-- Overdue tasks:
SELECT pt.name AS task, p.name AS project, e.employee_name AS assignee, pt.due_date, pt.priority FROM project_tasks pt JOIN projects p ON pt.project_id = p.id JOIN employees e ON pt.assignee_id = e.id WHERE pt.status != 'Finished' AND pt.due_date < CURDATE() ORDER BY pt.due_date LIMIT 50

ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
GENERAL RULES
ΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉΓòÉ
- NO MULTIPLE QUERIES: You must return exactly ONE single query. Never return two queries separated by a semicolon.
  - If asked for two numbers (e.g. "approved and pending"), combine them using conditional aggregation:
    `SELECT SUM(CASE WHEN status_id=1 THEN 1 ELSE 0 END) as approved, SUM(CASE WHEN status_id=2 THEN 1 ELSE 0 END) as pending FROM table...`
  - Or use UNION ALL: `SELECT 'Approved', COUNT(*) FROM... UNION ALL SELECT 'Pending', COUNT(*) FROM...`
- ROUND all decimals to 2 places
- LIMIT to 1000 for receivable report queries (needed for full export), LIMIT 50 for all others
- Currency is BHD (Bahraini Dinar)
- Use lead_date for saleslead filtering (NOT created_at)
- Use created_at for invoice/proposal/project/job_estimation
- projects.status_id links to m_project_status (NOT project_status_id)
- job_estimation.status_id links to m_jobestimation_status
- For "open" proposals: project_id IS NULL

--- FILTERED RECEIVABLE REPORT TEMPLATE ---
-- Use this pattern when the question is "Generate Receivable Report for dates X to Y [, Ageing: Z] [, Payment Status: W]"
-- Apply date filter on i.created_at, ageing bucket filter in HAVING, payment status filter on mis.name
SELECT
  DATE_FORMAT(i.created_at, '%d-%m-%Y') AS invoice_date,
  i.invoice_no AS reference_no,
  sl.name AS service_line,
  e.employee_name AS project_in_charge,
  c.customer_name AS customer_name,
  ROUND(i.total_net_amount, 2) AS invoice_amount,
  ROUND(COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0), 2) AS paid_amount,
  ROUND(i.total_net_amount
        - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
        - COALESCE((SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id), 0), 2) AS remaining_amount,
  CASE
    WHEN DATEDIFF(CURDATE(), i.created_at) < 30  THEN '<30 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 60  THEN '30-60 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 120 THEN '60-120 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 180 THEN '120-180 Days'
    WHEN DATEDIFF(CURDATE(), i.created_at) < 365 THEN '180-365 Days'
    ELSE '>365 Days'
  END AS ageing_bucket,
  mis.name AS payment_status
FROM invoice i
LEFT JOIN customers c       ON i.client_name_id      = c.id
LEFT JOIN m_serviceline sl  ON i.service_line_id     = sl.id
LEFT JOIN employees e       ON i.project_in_charge_id = e.id
LEFT JOIN m_invoice_status mis ON i.payment_status_id = mis.id
WHERE i.is_active = 1
  AND i.payment_status_id NOT IN (2, 4)
  AND (i.total_net_amount
       - COALESCE((SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id), 0)
       - COALESCE((SELECT SUM(cn.total_amount)   FROM credit_note cn   WHERE cn.invoice_id = i.id), 0)) > 0
  /* ADD DATE FILTER HERE  e.g.: AND i.created_at BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD 23:59:59' */
  /* ADD STATUS FILTER HERE e.g.: AND mis.name = 'Unpaid' */
HAVING 1=1
  /* ADD AGEING FILTER HERE e.g.: AND ageing_bucket = '30-60 Days' */
ORDER BY i.created_at DESC
LIMIT 1000

QUESTION: {question}
SQL:"""

# Dynamically inject the current fiscal year so the prompt stays correct every Oct 1
_fy = get_fiscal_info()
SQL_PROMPT = SQL_PROMPT.replace(
    "2025-10-01 to 2026-09-30 23:59:59",
    f"{_fy['fy_start']} to {_fy['fy_end']}"
)


FORMAT_PROMPT = """You are a CRM data analyst assistant for Grant Thornton Bahrain.
Given a question and SQL data, provide a clear, professional answer.

Rules:
- Format BHD currency with commas: "BHD 10,600.00"
- Use bullet points for multiple items
- Be direct ΓÇö give the number/answer first, then brief context
- Never show SQL
- If no data: say "No data found for this query"
- Keep answers concise but complete

Question: {question}
Data: {result}
Answer:"""


# ---------------------------------------------------------------------------
# LangChain Orchestrator
# ---------------------------------------------------------------------------

@tool
async def ad_hoc_sql_query(question: str) -> str:
    """Use this tool ONLY if the user's question cannot be answered by the other predefined metric tools. 
    This tool writes custom SQL to query the database.
    Pass the user's exact detailed question to this tool.
    """
    if DANGEROUS_SQL.search(question):
        return "⚠️ I can only run read-only queries."

    try:
        from db.database import get_db_engine
        from sqlalchemy import text
        from config.schema_index import get_schema_for_question, SQL_RULES, FISCAL_YEAR_HEADER
        import asyncio
        import json
        import re

        fy_info = get_fiscal_info()

        # ── Step 0: Query Parser — get verified SQL or context hints ─────────
        # If the entity (employee, customer) is confirmed in DB, returns pre-built SQL.
        # Otherwise returns a context_hint to guide the LLM to the right tables/filters.
        from .query_parser import get_grounded_sql
        verified_sql, context_hint, _intent = await get_grounded_sql(question)
        # Store intent metadata for the streaming response to use
        _ad_hoc_intent_navigate_to   = _intent.navigate_to
        _ad_hoc_show_fy_picker       = (
            _intent.metric_type == "resource_utilization"
            and not _intent.date_was_specified
        )
        _ad_hoc_entity_name          = _intent.entity_name
        _ad_hoc_metric_type          = _intent.metric_type

        if verified_sql:
            # DB-verified SQL — skip LLM generation entirely, zero hallucination
            raw_sql = verified_sql
        else:
            # ── Step 1: Dynamic Schema Retrieval (<1ms, zero LLM call) ───────
            compact_schema = get_schema_for_question(question)
            fy_header = FISCAL_YEAR_HEADER.format(
                fy_start=fy_info['fy_start'],
                fy_end=fy_info['fy_end']
            )

            # ── Step 2: SQL Prompt with context hint from parser ──────────────
            # Resolve user context FIRST — needed inside the f-string below
            ctx = semantic_layer._CURRENT_USER_CONTEXT.get()
            ctx_tier = ctx.get('user_tier')
            ctx_emp_id = ctx.get('employee_id')

            system_prompt = f"""You are a MySQL expert for Grant Thornton Bahrain's CRM.
Given a question, write EXACTLY ONE SELECT query. Return ONLY raw SQL — no markdown, no backticks, no explanation.

{fy_header}

{compact_schema}

{SQL_RULES}

QUERY CONTEXT (from intent parser — USE THIS to pick the right tables and WHERE filters):
{context_hint}

USER CONTEXT:
The logged-in user asking this question has employee_id={ctx_emp_id} and role_tier={ctx_tier}.
If the user uses personal pronouns like "my", "mine", or "assigned to me", MUST filter by this employee_id.
For task queries, use: WHERE (pt.assignee_id = {ctx_emp_id} OR pt.created_by = {ctx_emp_id}) and DO NOT add date filters unless the user explicitly asked for a date range.

QUESTION: {question}
SQL:"""

            # ── Step 3: RBAC — inject ownership filters if user is restricted ──
            try:
                if isinstance(ctx_tier, dict):
                    ctx_tier = int(ctx_tier.get('id', 9))
                else:
                    ctx_tier = int(ctx_tier) if ctx_tier is not None else 9
            except (ValueError, TypeError):
                ctx_tier = 9

            if ctx_tier >= 4 and ctx_emp_id:
                inv_rbac = semantic_layer._build_ownership_sql(ctx_emp_id, ctx_tier, "i")
                proj_rbac = semantic_layer._build_ownership_sql(ctx_emp_id, ctx_tier, "p")
                sl_rbac = semantic_layer._build_ownership_sql(ctx_emp_id, ctx_tier, "sl")
                system_prompt += (
                    f"\n\nMANDATORY RBAC SQL FILTER:\n"
                    f"User is Tier {ctx_tier} (employee_id={ctx_emp_id}). MUST apply:\n"
                    f"- invoice queries: AND {inv_rbac}\n"
                    f"- projects queries: AND {proj_rbac}\n"
                    f"- saleslead/proposal queries: AND {sl_rbac}\n"
                    f"Never return company-wide aggregates for this user.\n"
                )

            system_prompt += "\n\nReturn ONLY the raw SQL string. No markdown."

            # ── Step 4: SQL Generation ─────
            sql_llm = _build_llm(model_name=GROQ_PRIMARY_MODEL, temperature=0, max_tokens=400)
            sql_resp = await sql_llm.ainvoke([{"role": "system", "content": system_prompt}])

            raw_sql = sql_resp.content.strip()
            # Clean up any accidental markdown wrapping
            raw_sql = re.sub(r"^```(?:sql|mysql)?\s*", "", raw_sql, flags=re.IGNORECASE)
            raw_sql = re.sub(r"\s*```$", "", raw_sql).strip()
            raw_sql = raw_sql.rstrip(";")

        # ── Step 5: Execute SQL (via thread to avoid blocking event loop) ────
        def _execute_sql_sync():
            engine = get_db_engine()
            with open("sql_debug.log", "a", encoding="utf-8") as f:
                f.write(f"\n--- NEW SQL QUERY (Sequential-RAG) ---\n"
                        f"Question: {question}\n"
                        f"SQL: {raw_sql}\n")

            with engine.connect() as conn:
                if DANGEROUS_SQL.search(raw_sql):
                    return "Error: Blocked dangerous SQL."
                try:
                    result = conn.execute(text(raw_sql))
                    rows = result.fetchall()
                    columns = list(result.keys())

                    if not rows:
                        result_str = "No results found."
                    elif len(rows) == 1 and len(columns) == 1:
                        result_str = str(rows[0][0])
                    else:
                        full_data = {
                            "__type": "ad_hoc_table",
                            "columns": columns,
                            "rows": [list(row) for row in rows]
                        }
                        with open("sql_debug.log", "a", encoding="utf-8") as f:
                            f.write(f"Result: {len(rows)} rows\n")
                        return json.dumps(full_data, default=str)

                    with open("sql_debug.log", "a", encoding="utf-8") as f:
                        f.write(f"Result:\n{result_str}\n")
                    return result_str
                except Exception as e:
                    err = str(e)
                    with open("sql_debug.log", "a", encoding="utf-8") as f:
                        f.write(f"SQL Error: {err}\n")
                    return f"SQL execution error: {err}"

        return await asyncio.to_thread(_execute_sql_sync)

    except Exception as e:
        err = str(e)
        with open("sql_debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- SQL GENERATION ERROR ---\nException: {err}\n")
        return f"Database query failed: {err}"


@tool
async def search_backend_docs(query: str) -> str:
    """Search the CRM backend documentation for specific business logic, formulas, validation rules, or workflow details.
    Use this tool when the user asks about HOW something works, what formula is used, what validation rules apply,
    or any question about the CRM system's business logic that cannot be answered by the database query tools.
    Pass the relevant keywords to search for.
    """
    # Determine the user's tier from the current semantic layer context for RBAC-filtered retrieval
    try:
        ctx = semantic_layer._CURRENT_USER_CONTEXT
        user_tier = ctx.get("user_tier", 1) if ctx else 1
    except Exception:
        user_tier = 1

    # Try the Vector DB RAG pipeline first
    try:
        rag_result = await _rag_search(query, user_tier=user_tier)
        # If the RAG returned useful results (not the fallback "No relevant" message)
        if rag_result and "No relevant documentation found" not in rag_result and "unavailable" not in rag_result.lower():
            return rag_result
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(f"[RAG] Vector search failed, falling back to keyword search: {exc}")

    # Fallback to keyword-based static file search (keeps the system working even
    # if the MongoDB vector index is not yet set up)
    return search_documentation(query)



def get_agent_executor(user_context: dict = None, model_name: Optional[str] = None):
    llm = _build_llm(model_name or GROQ_PRIMARY_MODEL, temperature=0)
    
    # ---------------------------------------------------------
    # 1. Specialized Prompts for the Semantic Router
    # ---------------------------------------------------------
    current_year = datetime.now().year
    current_date = datetime.now().strftime('%Y-%m-%d')

    # NOTE: We no longer inject the full 278KB static knowledge context into the prompt.
    # Business logic is now retrieved dynamically via the `search_backend_docs` RAG tool.
    # This reduces token cost and prevents the LLM from being overwhelmed with irrelevant context.

    # --- Inject RBAC prompt if user context is available and role is known ---
    rbac_injection = ""
    # Support BOTH 'role_name' (from /ask-ai) and 'role' (from /api/ai/chat JWT) keys
    role_name = (
        (user_context or {}).get("role_name")
        or (user_context or {}).get("role")
        or "Unknown"
    ) if user_context else "Unknown"
    # Tier 1 roles (super admin/admin) get full access — everyone else gets RBAC injected
    _admin_roles = ("super admin", "administrator", "admin")
    if user_context and role_name and role_name.lower() not in ("", "none", "unknown") + _admin_roles:
        rbac_injection = build_rbac_prompt(
            user_name=user_context.get("user_name", "Unknown"),
            role_name=role_name,
            department=user_context.get("department", "Unknown"),
        )
        print(f"[RBAC] Injected RBAC prompt for: {role_name} (Tier resolution confirmed)")
    else:
        print(f"[RBAC] Skipped RBAC injection — role_name resolved as: '{role_name}'")

    # BASE BEHAVIOR (Shared by all)
    CRM_ROUTE_MAP = """
NAVIGATION ROUTES — use these EXACT paths in navigate_to JSON field:

[Dashboards & Core CRM]
- Dashboard / Home: /
- CRM Dashboard: /crm-dashboard
- Service Leads: /service-lead
- Job Estimations: /job-estimation
- Proposals: /proposal
- Customers / Clients: /customer (CRITICAL: if you have a specific customer_id or client_id, append it like /customer/edit?id=123 so the frontend opens the exact customer)
- Contacts: /contact
- Employees: /employee

[Projects & Tasks]
- Projects List: /projects-list
- Project Portfolio: /project-portfolio
- Individual Project / Project details: /projects/individual-project (CRITICAL: if you have a specific project_id, append it like /projects/individual-project?id=123 so the frontend opens the exact project)
- Tasks / Task Overview / My Tasks: /projects/tasks
- Tasks Main Board: /projects/tasks-main
- Resource Allocation: /projects/resource-allocation
- Time Sheet / Timesheet: /projects/timesheet
- Timesheet Approval: /projects/timesheet-approval
- Milestones: /projects/milestone
- Gantt Charts: /projects/gantt-and-reports
- Client Connect: /projects/client-connect

[Billing & Finance]
- Billing (main): /billing
- Invoices: /billing/invoice
- Credit Notes: /billing/credit-note
- Receipts: /billing/receipt

[Self Services & HR]
- Self Services Main: /self-services
- General Queries: /self-services/general-queries
- Leave Requests: /self-services/leave-request
- Leave Plans: /self-services/leave-plans
- Travel Requests: /self-services/travel-request
- Cash Advance Requests: /self-services/cash-advance
- HR Payroll: /hr/payroll
- Final Settlements: /hr/final-settlement-list

[Settings & Masters]
- Global Settings: /global-setting
- Setting Masters (main): /setting/master
- Organization Master: /setting/master/organization
- Department Master: /setting/master/department
- Service Line Master: /setting/master/service-line
- City Master: /setting/master/city
- Office Master: /setting/master/office
- Space Inventory: /setting/master/space-inventory
- Task Master / Task Setup: /setting/master/task
- KPI Budget Master: /setting/master/kpi-budget-master
- Templates / Library: /setting/templates
- Proposal Templates: /setting/proposal-templates
- User Management: /setting/admin-panel/user-management
- User Permissions: /setting/admin-panel/user-permissions

[Reports]
- Project Reports (Main): /projects/reports
- KPI Summary Report: /projects/reports/kpi-summary-report
- Project Status Report: /projects/reports/project-status-report
- Project Ageing Report: /projects/reports/project-ageing-report
- Staff Billing Report: /projects/reports/staff-billing-report
- Resource Utilization Report: /projects/reports/resource-utilization-report
- Billing Reports (Main): /billing/reports
- Receivable Report: /billing/reports/receivable-report
- Invoice Summary Report: /billing/reports/invoice-summary-report
- CRM Reports (Main): /crm/reports
- Proposal Status Report: /crm/reports/proposal-status-report
- Service Lead Report: /crm/reports/service-lead-report

[Miscellaneous]
- Meetings / Calendar: /meetings
- Client Satisfaction / Survey: /client-satisfaction

🚨 CRITICAL NAVIGATION INSTRUCTION 🚨
NEVER navigate to a page that is NOT explicitly listed above. If the user asks for a report or page that does not exist in this list (for example, "Total Estimation Report"), you MUST set `navigate_to` to the closest parent category (like "/projects/reports") or "/". NEVER invent, guess, or hallucinate URLs. If you output a URL that is not on this list, the application will crash.
"""

    # Use true employee_id from context, fallback to user_id (if 0 or missing, it's 0)
    employee_id = (user_context or {}).get("employee_id") or (user_context or {}).get("user_id", 0)
    employee_id_str = str(employee_id) if employee_id else "0"

    from config.role_tier_config import get_tier_for_role
    user_tier = get_tier_for_role(role_name)

    # Set server-side user context on semantic_layer for RBAC enforcement
    semantic_layer.set_user_context({
        'employee_id': int(employee_id_str) if employee_id_str != '0' else None,
        'department_id': (user_context or {}).get("department_id"),
        'user_tier': user_tier,
        'role_name': role_name,
    })
    
    if user_tier <= 4:
        tool_instruction = f"""
\u26A0\uFE0F  DATA SCOPING INSTRUCTION:
When calling tools (get_revenue_metrics, get_receivables_metrics, get_pipeline_and_proposals, get_active_projects_metrics):
- By default, OMIT the `employee_id` argument to fetch COMPANY-WIDE aggregate dashboard data (which is what users expect when asking for 'total receivables', 'revenue', etc.).
- ONLY pass `employee_id={employee_id_str}` if the user EXPLICITLY asks for their personal data (e.g., "my assigned tasks", "my projects", "my revenue").
"""
    else:
        tool_instruction = f"""
\u26A0\uFE0F  DATA SCOPING INSTRUCTION (MANDATORY SECURITY ENFORCEMENT):
- Because you are Tier {user_tier}, you are strictly PROHIBITED from accessing company-wide financial totals.
- By default, OMIT the `employee_id` argument to fetch BRANCH-WIDE aggregate dashboard data (the SQL layer will securely scope this to the user's specific Service Line).
- ONLY pass `employee_id={employee_id_str}` if the user EXPLICITLY asks for their personal, individual data (e.g., "my assigned tasks", "my projects", "my invoices").
- NEVER mention that you are filtering the data unless asked about your access level. Simply provide the data as it applies to the user's branch.
"""

    base_instructions = f"""You are **ANTIGRAVITY**, an elite CRM Data Analyst.
Current Year: {current_year} | Current Date: {current_date}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
LOGGED-IN USER (CRITICAL \u2014 READ CAREFULLY)
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
- Employee ID: {employee_id_str}
- Name: {(user_context or {}).get('user_name', 'Unknown')}
- Role: {role_name}
- Department: {(user_context or {}).get('department', 'Unknown')}

{tool_instruction}

BEHAVIORAL GUIDELINES:
1. Be Precise: Use EXACT numbers returned by your tools. NEVER invent or hallucinate metrics, percentages, or rates (e.g., 'Overall Completion', 'Recoverability', etc.) if they are not present in your tool data. If data is unavailable, say so and navigate the user to the relevant dashboard.
2. Be Visual: Use tables and emojis.
3. Be Concise: Answer directly, avoid filler.
4. Currency: ALL monetary values MUST be displayed in BHD (Bahraini Dinar). NEVER use $, USD, or any other currency symbol. Always format as "BHD X,XXX.XX".

{rbac_injection}

{CRM_ROUTE_MAP}

EVERY response MUST end with a ```json block containing:
- navigate_to: the single most relevant route from the CRM NAVIGATION ROUTES above. NEVER make up or hallucinate a URL. If none apply, use "/".
   * STRICT NAVIGATION RULE: If the user asked about a specific PROJECT (by typing its code or name), you MUST find that exact project's ID in your data and output EXACTLY: "/projects/individual-project?id=<the_id>"!
   * STRICT NAVIGATION RULE: If the user asked about a CUSTOMER and you got a single match, use the Customer ID and output EXACTLY: "/customer/edit?id=<the_id>"!
   * MULTIPLE MATCHES RULE: If the tool returned a "multiple_matches" response, set navigate_to to "/customer" and populate navigation_links using the "navigate_url" field from each match item. Do NOT leave navigation_links empty when multiple matches exist.
- navigation_links: array of objects with label and url for related pages (max 5 when multiple matches exist, 3 otherwise)
- entity_name: string (if you found a specific customer, project, or employee name) else null
- entity_type: string ("customer" or "project" or "employee" if applicable) else null
- is_edit_intent: boolean (set to true ONLY if the user explicitly used words like "edit", "update", or "change" in their prompt)
- suggested_questions: array of 3-4 string questions tailored specifically to the data you just provided. **CRITICAL RULES**: 1. Must be highly relevant follow-ups to your current answer. 2. NEVER repeat a question the user has already asked. 3. Every suggestion MUST start with "What" or describe a specific metric (never "Can I").
- chart_data: null (unless chart is appropriate)
- export_data: null
- auto_expand: false

\u26A0\uFE0F **STRICT RESPONSE RULE**:
- NEVER repeat your access rules, role name, or tier level in your response unless specifically asked "What is my role?". 
- NEVER say "As a Partner..." or "Your role is...". 
- Jump directly to the data and the answer.
"""

    # ROUTER PROMPT
    router_prompt_template = """As an AI intent classifier, analyze the user's message and categorize it into exactly one label.

Labels:
- CUSTOMER_REPORT: Mentions a specific company name, client, customer code (e.g., G00377), or CR number.
- DASHBOARD_METRICS: Asks for high-level KPIs, firm-wide totals, revenue, receivables, or pipeline stats.
- AD_HOC: Specific custom data queries (e.g., "resource utilization", "employee billable hours", "list of tasks"), project details/codes, system logic questions ("how does X work?"), or anything else.

Response MUST be exactly one of the three labels, nothing else.

User Message: {input}
Label:"""

    # CUSTOMER REPORT PROMPT
    customer_report_prompt = base_instructions + """
You are acting as the CUSTOMER REPORT AGENT. 
Use the `get_comprehensive_customer_report` tool to fetch the 360-degree data.

🚨 ANTI-HALLUCINATION GUARD: If the tool returns an error or says "No customer found", you MUST NOT invent or hallucinate data! Reply EXACTLY with: "I couldn't find a customer matching that name or ID in the database. Please verify the customer name, code, or project reference." and append the JSON block with `navigate_to: "/"` and NO `export_data` or `chart_data`.

## 📋 COMPREHENSIVE CUSTOMER REPORT FORMAT
Use this EXACT strict structure:
# 🏢 [CUSTOMER NAME] — Complete 360° Analysis
## 📌 Customer Overview
(Table of Customer Details)
## 👥 Key Contacts
(Mini-cards for contacts)
## 📊 Projects Summary
(Running/Completed counts, Overall Completion %, and Actual/Estimated Recoverability if available)
## 📈 Sales Pipeline
(Leads and Proposals metrics)
## 💰 Financial Summary
(Invoices, Aging, Credit Notes)
## 💡 Recommendations
(1-3 strategic next steps)

CRITICAL: Append the JSON block at the very end with `auto_expand: true`, `export_data` (containing matching sheets), and `navigation_links`.
"""

    # DASHBOARD METRICS PROMPT
    dashboard_prompt = base_instructions + """
You are acting as the DASHBOARD METRICS AGENT.
You must use the predefined metric tools: `get_revenue_metrics`, `get_receivables_metrics`, `get_pipeline_and_proposals`, `get_active_projects_metrics`.

FORMAT RULES FOR DASHBOARD:
- Answer normally like a polite chat assistant. Do NOT use the massive Customer Report layout.
- Use simple text, bullet points, or single small tables. 
- NEVER generate large markdown tables (limit to 10 rows max). Large tables cause token limits to truncate the JSON block!
- Set `auto_expand: false` and `export_data: null` in your JSON block.
- Set `navigate_to` to the relevant CRM page if the dashboard answers the question directly.
"""

    # AD-HOC PROMPT
    ad_hoc_prompt = base_instructions + """
You are acting as the AD-HOC SQL & KNOWLEDGE AGENT.
Use `ad_hoc_sql_query` for custom data questions.
Use `search_backend_docs` if the user asks how a formula or rule works.

FORMAT RULES:
- Never show raw SQL to the user.
- DO NOT generate a markdown table. Provide a brief 1-2 sentence summary of the findings. The system will automatically generate a dynamic data table for the user below your text.
- Append the standard JSON block with relevant `navigation_links`.
- If the user asks for resource utilization, explicitly add this exact suggested question in the `suggested_questions` JSON array: "Can you show me the project breakdown for this utilization?"
"""

    # ---------------------------------------------------------
    # 2. Build the FLATTENED routing pipeline (no ReAct loop)
    # ---------------------------------------------------------
    # Instead of create_react_agent (which adds Thought/Action/Observation loops),
    # we call tools DIRECTLY and format the result in a single LLM pass.
    # This reduces 4 serial LLM calls to just 2 (classify + format).

    # Keep ReAct ONLY for customer reports (multi-step, complex formatting)
    customer_agent = create_react_agent(llm, tools=[semantic_layer.get_comprehensive_customer_report], prompt=customer_report_prompt)

    # Fast keyword-based classification (avoids LLM call for obvious intents)
    _DASHBOARD_KEYWORDS = {
        "revenue", "receivable", "receivables", "billing", "invoice", "collection",
        "pipeline", "proposal", "proposals", "lead", "leads", "project", "projects",
        "task", "tasks", "overdue", "kpi", "dashboard", "win rate", "conversion",
        "active projects", "high value", "total revenue", "total receivables",
        "outstanding", "paid", "unpaid", "aging", "ageing", "service line", "budget",
        # Service line / GP performance
        "service line performance", "service line revenue", "serviceline performance",
        "gp performance", "gross profit performance", "gp by service line",
        "gp target", "performing vs target", "team billing",
        # Department utilization
        "department utilization", "dept utilization", "department utilisation",
        "utilization by department", "department hours",
        # KPI Report specific terms
        "gross profit", "gp report",
        "secured business", "balance to achieve", "project in hand",
        "budget vs actual", "variance", "direct cost", "staff cost",
        "referral fee", "gti expenses", "direct consultancy", "bad debts",
        "billing revenue", "revenue vs budget", "monthly revenue",
        "receivable summary", "receivable aging", "receivable ageing",
        "kpi summary", "kpi report",
        "target gp", "budget target", "actual revenue",
    }
    _CUSTOMER_KEYWORDS = {
        "customer report", "client report", "360", "comprehensive",
    }
    # Pattern for customer codes like G00377, K00123, etc. (excludes project codes with dashes)
    _CUSTOMER_CODE_PATTERN = re.compile(r'\b[A-Z]\d{4,6}\b(?!\-)')

    # Ad-hoc bypass: these terms signal a dynamic query, not a pre-built dashboard metric.
    # NOTE: Do NOT add generic words like 'list', 'name', 'details' here — they
    # would incorrectly bypass legitimate dashboard queries like 'list of proposals'.
    _ADHOC_KEYWORDS = {
        "who", "owner", "created by", "ago", "joined", "years old",
        "utilization", "utilisation", "resource", "timesheet", "leave balance",
        "estimation report", "total estimation", "estimated hours", "actual hours",
        "hour overrun", "exceeded hours", "exceeded approved", "approved hours",
        "over budget hours", "over estimated", "recoverability",
    }

    # CRITICAL: Queries that ask for a LIST of individual items (tasks, projects,
    # employees, etc.) MUST go to AD_HOC SQL — the dashboard tools only return
    # aggregate totals/counts and the LLM WILL hallucinate fake rows otherwise.
    _LIST_DETAIL_PATTERNS = [
        "list the", "list my", "list all", "list of", "show me the",
        "show all", "show my", "pending task", "pending tasks",
        "my tasks", "my pending", "overdue task", "overdue tasks",
        "assigned task", "assigned tasks", "incomplete task", "incomplete tasks",
        "open tasks", "list task", "list tasks",
    ]

    # When a query is about CUSTOMERS as the primary entity (e.g. "top 5 customers by revenue"),
    # the dashboard metric tools cannot answer it — they only have service-line aggregates.
    # These phrases force AD_HOC so a proper SQL JOIN against the `customers` table runs.
    _CUSTOMER_ENTITY_SIGNALS = {
        "customer", "client", "clients", "customers",
    }
    # Metric keywords that, when paired with a customer signal, must go AD_HOC
    _METRIC_KEYWORDS_NEEDING_CUSTOMER_JOIN = {
        "revenue", "billing", "invoice", "invoices", "collection",
        "paid", "unpaid", "outstanding", "receivable", "receivables",
        "aging", "ageing", "overdue", "top", "highest", "most",
        "inactive", "active", "list", "all"
    }

    def _fast_classify(question: str) -> str:
        """Keyword-based fast classification — avoids an LLM call for ~80% of queries."""
        import re as _fcre
        q_lower = question.lower().strip()

        # ── Typo normalization so mistyped queries also match keywords below ──
        _FC_TYPO = [
            (_fcre.compile(r'\bservice\s+li[a-z]{0,3}\b', _fcre.IGNORECASE), 'service line'),
            (_fcre.compile(r'\bperform[a-z]{0,5}\b',      _fcre.IGNORECASE), 'performance'),
            (_fcre.compile(r'\bgp\s+perf[a-z]{0,7}\b',   _fcre.IGNORECASE), 'gp performance'),
            (_fcre.compile(r'\bgross\s+prof[a-z]{0,2}\b', _fcre.IGNORECASE), 'gross profit'),
            (_fcre.compile(r'\bdep[a-z]{0,6}\s+util[a-z]{0,6}\b', _fcre.IGNORECASE), 'department utilization'),
            (_fcre.compile(r'\butili[zs]a[a-z]{0,4}\b',  _fcre.IGNORECASE), 'utilization'),
            (_fcre.compile(r'\brecei[a-z]{0,6}\b',        _fcre.IGNORECASE), 'receivables'),
            (_fcre.compile(r'\brevenu[a-z]{0,2}\b',        _fcre.IGNORECASE), 'revenue'),
        ]
        q_norm = q_lower
        for _p, _r in _FC_TYPO:
            q_norm = _p.sub(_r, q_norm)
        # ── End typo normalization ───────────────────────────────────

        # CRITICAL: Detect filtered receivable report requests from the chat filter form.
        # These always start with "generate receivable report" and contain specific filter params.
        # They MUST go to AD_HOC so a filtered SQL query is executed, not the broad summary tool.
        _FILTERED_REPORT_SIGNALS = ["for dates ", "ageing:", "payment status:", "date range", "from date", "to date"]
        if "generate receivable report" in q_norm and any(sig in q_norm for sig in _FILTERED_REPORT_SIGNALS):
            return "AD_HOC"

        # CRITICAL FIX: Analytical/Specific queries MUST go to AD_HOC SQL agent.
        # This prevents them from being trapped in the Dashboard logic.
        _ANALYTICAL_MARKERS_STRONG = [
            "recent", "latest", "lowest", "highest", "top", "bottom", 
            "compare", "specific", "a proposal", "an invoice", "the project", "detail"
        ]
        if any(marker in q_norm for marker in _ANALYTICAL_MARKERS_STRONG):
            return "AD_HOC"

        # CRITICAL FIX: If the question is asking about CUSTOMERS as an entity combined with
        # a metric (revenue, billing, invoices, etc.), force AD_HOC.
        has_customer_signal = any(kw in q_norm for kw in _CUSTOMER_ENTITY_SIGNALS)
        has_metric_signal = any(kw in q_norm for kw in _METRIC_KEYWORDS_NEEDING_CUSTOMER_JOIN)
        if has_customer_signal and has_metric_signal:
            return "AD_HOC"

        # CRITICAL FIX: Estimation queries MUST bypass LLM classifier and go directly to AD_HOC
        _ESTIMATION_SIGNALS = [
            "estimation report", "total estimation", "estimated hours", "actual hours",
            "hour overrun", "exceeded hours", "exceeded approved", "approved hours",
            "over budget hours", "over estimated", "estimated project", "exceede",
        ]
        if any(sig in q_norm for sig in _ESTIMATION_SIGNALS):
            return "AD_HOC"

        # CRITICAL: List/detail queries requesting individual rows MUST go AD_HOC
        # to prevent LLM hallucination of fake data (e.g. Task ID 12345, John Doe)
        if any(sig in q_norm for sig in _LIST_DETAIL_PATTERNS):
            return "AD_HOC"

        _RECOVERABILITY_SIGNALS = [
            "recoverability report"
        ]
        if any(sig in q_norm for sig in _RECOVERABILITY_SIGNALS):
            return "AD_HOC"

        # CRITICAL: Staff billing queries must go to AD_HOC for the deterministic handler
        _STAFF_BILLING_SIGNALS = [
            "staff billing", "staff cost", "billing for employee", "employee billing",
            "partner billing", "partner project billing",
        ]
        if any(sig in q_norm for sig in _STAFF_BILLING_SIGNALS) or ("staff" in q_norm and "billing" in q_norm):
            return "AD_HOC"

        # Check for ad-hoc specific patterns first to prevent dashboard trapping
        for kw in _ADHOC_KEYWORDS:
            if kw in q_norm:
                return "UNKNOWN"  # Fall back to LLM, which will classify as AD_HOC

        # Check for customer report patterns
        for kw in _CUSTOMER_KEYWORDS:
            if kw in q_norm:
                return "CUSTOMER_REPORT"
        if _CUSTOMER_CODE_PATTERN.search(question):
            return "CUSTOMER_REPORT"

        # Check for dashboard metrics patterns
        for kw in _DASHBOARD_KEYWORDS:
            if kw in q_norm:
                return "DASHBOARD_METRICS"

        # Ambiguous — must fall back to LLM classification
        return "UNKNOWN"

    category_prompt = PromptTemplate.from_template(router_prompt_template)
    classification_chain = category_prompt | llm | StrOutputParser()

    async def _pick_and_call_dashboard_tool(question: str) -> str:
        """Directly select and call the right semantic layer tool based on keywords."""
        q_lower = question.lower()

        # Import the underlying async functions directly from semantic_layer
        from semantic.semantic_layer import (
            get_revenue_metrics, get_receivables_metrics,
            get_pipeline_and_proposals, get_active_projects_metrics
        )

        # Map question keywords to the correct semantic tool (direct async call)
        # Use intelligent intent classification instead of hardcoded keywords
        intent = await classify_intent(question)
        
        if should_show_revenue_report(intent):
            return await get_revenue_metrics.ainvoke({})
        elif should_show_receivables_report(intent):
            # Distinguish between total/summary (return data) vs detailed/breakdown (show filters)
            is_total_request = any(kw in q_lower for kw in ["total", "overall", "summary"])
            is_detailed_request = any(kw in q_lower for kw in ["detailed", "breakdown", "filter", "filtered", "specific"])
            
            if is_detailed_request or (any(kw in q_lower for kw in ["report", "overview", "receivables"]) and not is_total_request):
                # User asks for detailed/filtered view → show filter panel
                return "[SYSTEM_HINT: BROAD_REPORT_REQUEST] The user wants to see the receivable report filters. DO NOT show any financial numbers. Ask whether they want an overall report, a group report, a pipeline report, or a customized report."
            else:
                # User asks for total/summary → return actual data
                return await get_receivables_metrics.ainvoke({})

        elif should_show_proposals_report(intent):
            return await get_pipeline_and_proposals.ainvoke({})
        elif should_show_projects_report(intent):
            return await get_active_projects_metrics.ainvoke({})
        elif should_show_resources_report(intent):
            return "[SYSTEM_HINT: ADHOC_REQUIRED] This is a dynamic resource/employee query. Please use the ad_hoc_sql_query tool to fetch this data from the employees and timesheet_project tables."
        else:
            # Broad question — call revenue + pipeline concurrently
            import asyncio
            rev_task = asyncio.create_task(get_revenue_metrics.ainvoke({}))
            pipe_task = asyncio.create_task(get_pipeline_and_proposals.ainvoke({}))
            rev, pipe = await asyncio.gather(rev_task, pipe_task)
            return f"Revenue Data:\n{rev}\n\nPipeline Data:\n{pipe}"
    # ---------------------------------------------------------------------------
    # Anti-hallucination helpers
    # ---------------------------------------------------------------------------
    import re as _re

    def _is_sql_error(raw: str) -> bool:
        """Return True when the tool returned an error, not real data."""
        if not raw:
            return True
        raw_stripped = raw.strip().lower()
        if raw_stripped in ("[]", "{}", "", "null", "none"):
            return True
        prefixes = ("sql execution error:", "error retrieving", "database query failed:",
                    "error:", "blocked dangerous sql", "no results found.", "no projects exist", "critical: no projects exist")
        if raw_stripped.startswith(prefixes):
            return True
        if '{"error":' in raw_stripped or '"error":' in raw_stripped:
            return True
        return False

    def _no_data_response(question: str) -> str:
        """Honest answer when the DB returned nothing / an error."""
        return (
            f"I couldn't find any data in the CRM database for that query. "
            f"The data may not exist yet, or the filters may need adjustment. "
            f"\n\nPlease verify on the relevant CRM page or refine your question.\n\n"
            f"```json\n"
            f'{{"navigate_to": "/", "navigation_links": [], "suggested_questions": '
            f'["Show total revenue this year", "List all active projects", "Show open leads"], '
            f'"chart_data": null, "export_data": null, "report_intent": null, "auto_expand": false}}\n```'
        )

    def _ground_check(answer: str, raw_data: str) -> str:
        """
        Post-response grounding check.
        If the raw data was empty/no-results but the LLM answer contains
        currency amounts (hallucinated numbers), replace with honest response.
        """
        no_data_signals = ["no results found", "no data", "0 rows", "empty result", "no projects exist", "0 projects"]
        raw_lower = (raw_data or "").strip().lower()
        if not any(sig in raw_lower for sig in no_data_signals):
            return answer  # data was present, trust the answer
        # Raw data indicates no results — check if LLM hallucinated amounts or names
        if _re.search(r'BHD\s*[\d,]+\.\d+', answer or "") or "Project A" in answer or "PRJ" in answer:
            return (
                "No data was found in the CRM database for that query.\n\n"
                "```json\n"
                '{"navigate_to": "/", "navigation_links": [], "suggested_questions": '
                '["Show total revenue this year", "List all active projects", "Show open leads"], '
                '"chart_data": null, "export_data": null, "report_intent": null, "auto_expand": false}\n```'
            )
        return answer

    # Lean format prompt — only what's needed for formatting, not the full system prompt
    _LEAN_FORMAT_PROMPT = """You are ANTIGRAVITY, a CRM Data Analyst for Grant Thornton Bahrain.

════════════════════════════════════════════
⛔ STRICT GROUNDING RULES — NEVER VIOLATE
════════════════════════════════════════════
1. You may ONLY state numbers, names, and facts that appear VERBATIM in the "Raw CRM data" below.
2. NEVER calculate, infer, or estimate any value not explicitly in the raw data.
   - If recoverability % or actual_recoverability_percentage is in the data → you MUST display it prominently. If NOT in data → do NOT mention it.
   - If completion % is not in the data → do NOT mention completion rate.
   - If a name is not in the data → do NOT invent a name.
   - If the user asks for a LIST of items but the data only contains aggregate counts/totals, respond with the aggregates you DO have and say "I have summary data showing X total items. For a detailed list, please check the relevant page." NEVER fabricate individual items, task IDs, names, or assignees.
3. If the raw data says "No results found" or is empty → reply ONLY: "No data found for this query in the CRM database." Do NOT invent numbers.
4. NEVER use placeholder values like "X", "N/A (not in data)", or made-up amounts.
   NEVER use generic names like "John Doe", "Jane Smith", "Project Meeting" — these are obvious hallucinations.
5. If the data is partial, present only what is there and say "additional data not available".

RULES:
- Currency: BHD (Bahraini Dinar). Format as "BHD X,XXX.XX". NEVER use $ or USD.
- Be concise and direct. Give numbers first, then brief context.
- Use bullet points or small tables for clarity. Use emojis sparingly.
- NEVER show SQL or formulas to the user.
- PRIVACY: NEVER mention the user's role, tier, or internal permissions.
- CRITICAL FORMATTING: Do NOT output "Report Intent:", "Navigate To:", "Navigation Links:", or "Suggested Questions:" in your conversational text. These MUST ONLY exist inside the final JSON block.

⚠️ RESPONSE TYPE RULES — READ CAREFULLY:
- If the raw data contains PROPOSAL or PIPELINE data (proposals, leads, win rate, agreed fees): Answer as a concise pipeline/proposal summary. Do NOT use KPI report layout. Navigate to /proposal.
- If the raw data contains PROJECT or TASK data (projects, tasks, milestones): Answer as a concise project summary. You MUST include Overall Completion and Actual/Estimated Recoverability metrics if they exist in the raw data. Navigate to /projects-list.
- If the raw data contains REVENUE or INVOICE data (but NOT a KPI report): Answer as a concise revenue summary. Navigate to /billing/invoice.
- If the raw data contains KPI report fields (billing_revenue_gp_table, variance, gross profit, secured business): THEN apply KPI formatting and navigate to /projects/reports/kpi-summary-report.
- If the raw data contains SERVICE LINE fields (service_line, actual_revenue, target_revenue, short_code, performing, target, achievement_pct): Present as a Service Line Performance table. Navigate to /crm-dashboard.
- If the raw data contains DEPARTMENT UTILIZATION fields (department, approved_hours, eligible_hours, utilization_pct): Present as a Department Utilization table. Navigate to /projects/reports/resource-utilization-report.
- If the raw data contains TOTAL ESTIMATION REPORT fields: DO NOT output a markdown table for the rows! An interactive table will be displayed automatically. ONLY provide the summary stats (total projects, over-budget count, total hours difference) in your text. Navigate to /projects-list.
- If the raw data contains PROJECT RECOVERABILITY REPORT fields: DO NOT output a markdown table for the rows! An interactive table will be displayed automatically. ONLY provide the summary stats (total projects, total estimated cost, total actual cost, total actual recoverability) in your text. You MUST mention the specific Month/Period (e.g., "- February 2026") at the very top of your summary. Navigate to /projects/reports/project-recoverability-report.
- CRITICAL INSTRUCTION FOR ALL AD-HOC REPORTS: DO NOT generate markdown tables containing data rows. The UI will automatically render the full table from the JSON payload. Only provide high-level summary statistics in your conversational text.
SERVICE LINE PERFORMANCE FORMATTING (use when raw data has service_line + actual_revenue or performing):
- Present as a markdown table with columns: Service Line | Actual Revenue (BHD) | Target (BHD) | Achievement %
- Format all BHD amounts with commas. Show achievement_pct as "X.XX%".
- Summarise the total actual revenue at the top.
- Do NOT show service lines with 0 actual revenue AND 0 target.

DEPARTMENT UTILIZATION FORMATTING (use when raw data has department + utilization_pct):
- Present as a markdown table with columns: Department | Approved Hours | Eligible Hours | Utilization %
- Sort by Utilization % descending (highest first).
- Show overall average utilization at the top.
- Utilization % = approved_hours / eligible_hours × 100 — use the value from the data, do NOT recalculate.

KPI-ONLY FORMULAS (only use when raw data is from a KPI report):
  - Variance = Actual Revenue − Budget Target Revenue
  - Gross Profit = Actual Revenue − Credit Notes − GTI Expenses − Staff Cost − Referral Fees − Direct Consultancy Fees − Bad Debts
  - Secured Business = Actual Revenue (net) + Project Approved Fees
  - Balance to Achieve = Total Annual Target − Secured Business

KPI FORMATTING (only when responding to KPI data):
- Negative variances: "shortfall of BHD X" 🔴
- Positive variances: "surplus of BHD X" 🟢
- Show "-" for months with zero data — do NOT show "BHD 0.000".

EVERY response MUST end with a ```json block:
{"navigate_to": "/relevant-page", "navigation_links": [{"label": "...", "url": "..."}], "suggested_questions": ["...", "...", "..."], "chart_data": null, "export_data": null, "report_intent": null, "auto_expand": false}

🚨 STRICT NAVIGATION ALLOWLIST — YOU MAY ONLY USE THESE EXACT URLs FOR navigate_to. ANY OTHER URL WILL CRASH THE APPLICATION:
/ | /crm-dashboard | /proposal | /projects-list | /projects/tasks | /projects/reports | /projects/reports/kpi-summary-report | /projects/reports/project-status-report | /projects/reports/project-ageing-report | /projects/reports/staff-billing-report | /projects/reports/resource-utilization-report | /billing/invoice | /billing/reports | /billing/reports/receivable-report | /billing/reports/invoice-summary-report | /service-lead | /customer | /setting | /meetings | /client-satisfaction | /crm/reports | /crm/reports/proposal-status-report | /crm/reports/service-lead-report | /self-services/leave-request

NAVIGATION ASSIGNMENT (pick EXACTLY from list above — NEVER invent or modify):
- Service line / GP performance data → /crm-dashboard
- Department utilization data → /projects/reports/resource-utilization-report
- KPI / budget vs actual data → /projects/reports/kpi-summary-report
- Total estimation / approved hours data → /projects-list
- Proposal / pipeline / win rate data → /proposal
- Invoice / revenue / billing data → /billing/invoice
- Receivable / ageing / outstanding data → /billing/reports/receivable-report
- Project or task data → /projects-list
- Leave data → /self-services/leave-request
- No match → /

- KPI RULE: If the user asks KPI summary/KPI report, set `report_intent` to "kpi_summary" and `navigate_to` to "/projects/reports/kpi-summary-report".
- Use `report_intent: "receivable"` ONLY if the user asks for a receivable report/overview/ageing WITHOUT specific filters.
- FILTERED RECEIVABLE REPORT: If the user's question starts with "Generate Receivable Report" AND contains filter parameters (dates, ageing, payment status), you MUST:
  1. Display a clear summary table of the invoice-level results from the raw data.
  2. Populate `export_data` with filename: "Receivable-Report-<today's date>" and a sheet named "Receivable Report" containing ALL rows from the raw data with these headers: ["Invoice Date", "Reference No", "Service Line", "Project In Charge", "Customer Name", "Invoice Amount (BHD)", "Paid Amount (BHD)", "Remaining Amount (BHD)", "Ageing Bucket", "Payment Status"]. Extract every row from the raw SQL result and include it in `rows`. Do NOT summarize — include every single data row.
  3. Set `navigate_to` to "/billing/reports/receivable-report".
- Use `export_data` to return a tabular representation of the data (with filename, sheets: [{name, headers, rows}]) if the response contains report data."""

    async def _format_tool_output(question: str, raw_data: str, prompt_context: str) -> str:
        """Format raw tool data into a polished user-facing answer.
        Includes SQL error detection and post-response grounding check."""
        # ── Guard 1: SQL / tool error detected — return honest message immediately
        if _is_sql_error(raw_data):
            print(f"[AntiHallucination] SQL error detected, skipping format LLM. raw={raw_data[:120]}")
            return _no_data_response(question)

        format_msg = f"""{_LEAN_FORMAT_PROMPT}

{rbac_injection}

The user asked: "{question}"

Raw CRM data:
{raw_data}

Provide a clear, professional answer based ONLY on the raw data above."""

        resp = await llm.ainvoke([{"role": "system", "content": format_msg}])
        answer = resp.content.strip()

        # ── Guard 2: Post-response grounding check
        answer = _ground_check(answer, raw_data)
        return answer

    async def final_routing_logic(inputs):
        """Flattened router: Classify → Direct Tool Call → Single Format Pass."""
        import time
        t0 = time.time()

        history = inputs["messages"]
        last_msg_content = history[-1].content if hasattr(history[-1], 'content') else history[-1]["content"]

        # Step 1: Fast classification (keyword heuristic first, LLM fallback)
        category = _fast_classify(last_msg_content)
        if category == "UNKNOWN":
            category = await classification_chain.ainvoke({"input": last_msg_content})
            category = category.strip().upper()
        print(f"[Router] Classified as: {category} in {time.time()-t0:.2f}s")

        # Step 2: Route
        if "CUSTOMER_REPORT" in category:
            # Customer reports are complex multi-tool workflows → keep ReAct
            return await customer_agent.ainvoke({"messages": history})

        elif "DASHBOARD_METRICS" in category:
            # FLATTENED: Direct tool call + single format pass
            t1 = time.time()
            raw_data = await _pick_and_call_dashboard_tool(last_msg_content)
            print(f"[Router] Dashboard tool returned in {time.time()-t1:.2f}s")

            t2 = time.time()
            formatted = await _format_tool_output(last_msg_content, raw_data, dashboard_prompt)
            print(f"[Router] Format pass completed in {time.time()-t2:.2f}s")

            return {"messages": [type("Msg", (), {"content": formatted})()]}

        else:
            # AD_HOC: Check if this is a Total Estimation Report question first
            _ESTIMATION_SIGNALS = [
                "estimation report", "total estimation", "estimated hours",
                "actual hours", "hour overrun", "exceeded hours", "exceeded approved",
                "over budget hours", "over estimated", "estimated project", "exceede",
            ]
            full_context = "\\n".join([m.content if hasattr(m, 'content') else m.get('content', '') for m in history[-3:]])
            q_lower_adhoc = full_context.lower()
            is_estimation_query = any(sig in q_lower_adhoc for sig in _ESTIMATION_SIGNALS)

            _RECOVERABILITY_SIGNALS = [
                "recoverability report", "project recoverability", "actual recoverability", "estimated recoverability", "recoverability", "recoverablity"
            ]
            is_recoverability_query = any(sig in q_lower_adhoc for sig in _RECOVERABILITY_SIGNALS)

            _STAFF_BILLING_SIGNALS_ADHOC = [
                "staff billing", "staff cost", "billing for employee", "employee billing",
                "partner billing", "partner project billing",
            ]
            is_staff_billing_query = (
                any(sig in q_lower_adhoc for sig in _STAFF_BILLING_SIGNALS_ADHOC)
                or ("staff" in q_lower_adhoc and "billing" in q_lower_adhoc)
            )

            ad_hoc_export_data = None
            
            if is_estimation_query:
                # Use the specialized estimation tool for accurate data
                t1 = time.time()
                
                # Provide entire history to check_sl_prompt so we have context
                check_sl_prompt = f"Extract the service line from this conversation if present (e.g. Audit, Tax, Advisory, BPO). If no service line is mentioned, reply EXACTLY with 'NONE'.\\nConversation:\\n{full_context}"
                sl_resp = await llm.ainvoke([{"role": "user", "content": check_sl_prompt}])
                extracted_sl = sl_resp.content.strip()
                
                if extracted_sl == "NONE" or "NONE" in extracted_sl.upper():
                    direct_msg = 'Please select a service line to generate the Total Estimation Report.\\n\\n```json\\n{"report_intent": "estimation_sl_picker", "navigate_to": null, "chart_data": null, "export_data": null, "auto_expand": false}\\n```'
                    return {"messages": [type("Msg", (), {"content": direct_msg})()]}
                else:
                    from semantic.semantic_layer import get_total_estimation_report
                    from .query_parser import _extract_date_range
                    date_from, date_to, _ = _extract_date_range(last_msg_content)
                    sl_val = extracted_sl.replace("'", "").replace('"', "")
                    raw_data = await get_total_estimation_report.ainvoke({
                        "start_date": date_from,
                        "end_date": date_to,
                        "service_line": sl_val
                    })
                    
                    try:
                        parsed = json.loads(raw_data)
                        if "projects" in parsed:
                            projects = parsed["projects"]
                            if projects:
                                columns = list(projects[0].keys())
                                all_rows = [[r.get(c, "") for c in columns] for r in projects]
                                ad_hoc_export_data = {
                                    "filename": "Total_Estimation_Report",
                                    "sheets": [{"name": "Data", "headers": columns, "rows": all_rows}]
                                }
                            # Truncate raw_data for LLM
                            raw_data = json.dumps({"summary": parsed.get("summary", {})})
                    except:
                        pass
                        
                print(f"[Router] Estimation report tool returned in {time.time()-t1:.2f}s")
            elif is_recoverability_query:
                # ─── DETERMINISTIC RECOVERABILITY HANDLER ────────────────────
                # CRITICAL: We format the response entirely in Python.
                # We NEVER send recoverability data to the LLM because it
                # hallucinates fake project names (Project A, B, C) from totals.
                # ─────────────────────────────────────────────────────────────
                t1 = time.time()
                from semantic.semantic_layer import get_project_recoverability_report
                from .query_parser import _extract_date_range
                from main import _extract_kpi_filters_from_text
                date_from, date_to, _ = _extract_date_range(last_msg_content)
                _filters = _extract_kpi_filters_from_text(last_msg_content)
                
                rec_args = {
                    "start_date": date_from,
                    "end_date": date_to
                }
                
                _sl = _filters.get("service_line")
                _emp = _filters.get("employee_name")
                
                if _sl and _sl.lower() != 'all':
                    rec_args["service_line"] = _sl
                if _emp and _emp.lower() != 'all':
                    rec_args["incharge_employee"] = _emp
                    
                raw_data = await get_project_recoverability_report.ainvoke(rec_args)
                print(f"[Router] Recoverability report tool returned in {time.time()-t1:.2f}s")

                # Build a deterministic response — no LLM involved
                try:
                    parsed = json.loads(raw_data)
                except Exception:
                    parsed = {}

                summary = parsed.get("summary", {})
                projects = parsed.get("projects", [])
                dr = parsed.get("date_range", {})
                total_projects = summary.get("total_projects", len(projects))

                # Build period label from dates
                sd_str = dr.get("start") or date_from or ""
                ed_str = dr.get("end") or date_to or ""
                try:
                    from datetime import datetime as _dt
                    sd_p = _dt.strptime(str(sd_str), "%Y-%m-%d")
                    ed_p = _dt.strptime(str(ed_str), "%Y-%m-%d")
                    if sd_p.year == ed_p.year and sd_p.month == ed_p.month:
                        period_label = sd_p.strftime("%B %Y")
                    else:
                        period_label = f"{sd_p.strftime('%d %b')} – {ed_p.strftime('%d %b %Y')}"
                except Exception:
                    period_label = f"{sd_str} to {ed_str}" if sd_str and ed_str else "All Time"

                # Build export data for the Excel button
                ad_hoc_export_data = None
                if projects:
                    columns = list(projects[0].keys())
                    all_rows = [[r.get(c, "") for c in columns] for r in projects]
                    
                    try:
                        from datetime import datetime as _dt_now
                        gen_date = _dt_now.now().strftime('%d %b %Y')
                    except Exception:
                        gen_date = "Today"
                        
                    meta = [
                        "Project Recoverability Report",
                        f"Generated on: {gen_date}",
                        f"Period: {period_label}"
                    ]
                    if _sl and _sl.lower() != 'all':
                        meta.append(f"Service Line: {_sl}")
                    if _emp and _emp.lower() != 'all':
                        meta.append(f"In-Charge Employee: {_emp}")
                        
                    ad_hoc_export_data = {
                        "filename": "Project_Recoverability_Report",
                        "sheets": [{"name": "Data", "headers": columns, "rows": all_rows, "metadata": meta}]
                    }

                # Format the answer text deterministically
                answer_lines = [
                    "### Project Recoverability Report",
                    f"- **Period:** {period_label}",
                ]
                if _sl and _sl.lower() != 'all':
                    answer_lines.append(f"- **Service Line:** {_sl}")
                if _emp and _emp.lower() != 'all':
                    answer_lines.append(f"- **In-Charge Employee:** {_emp}")
                    
                answer_lines.append(f"- **Total Active Projects:** {total_projects}")

                est_cost = summary.get("total_estimated_cost")
                if est_cost not in (None, "", "N/A"):
                    answer_lines.append(f"- **Total Estimated Cost:** BHD {float(est_cost):,.2f}")

                act_cost = summary.get("total_actual_cost")
                if act_cost not in (None, "", "N/A"):
                    answer_lines.append(f"- **Total Actual Cost:** BHD {float(act_cost):,.2f}")

                act_rec = summary.get("total_actual_recoverability_percentage")
                if act_rec not in (None, "", "N/A") and str(act_rec).strip().lower() != "nan":
                    answer_lines.append(f"- **Actual Recoverability:** {act_rec}%")

                source = summary.get("source", "")
                if "API" in source:
                    answer_lines.append("\n*Data sourced from CRM Recoverability Report API.*")
                elif "SQL" in source:
                    answer_lines.append("\n*Data sourced from CRM database (SQL fallback).*")

                if total_projects == 0:
                    msg = summary.get("message", "")
                    answer_lines.append(f"\n⚠️ No projects matched the criteria for {period_label}.")

                final_answer = "\n".join(answer_lines).strip()
                json_block = json.dumps({
                    "navigate_to": "/projects/reports/project-recoverability-report",
                    "navigation_links": [
                        {"label": "Recoverability Report", "url": "/projects/reports/project-recoverability-report"},
                        {"label": "Projects List", "url": "/projects-list"}
                    ],
                    "suggested_questions": [
                        "Show recoverability for last month",
                        "Show active vs completed projects",
                        "What is the total revenue?"
                    ],
                    "chart_data": None,
                    "export_data": ad_hoc_export_data,
                    "report_intent": None,
                    "auto_expand": False
                })
                deterministic_response = f"{final_answer}\n\n```json\n{json_block}\n```"
                return {"messages": [type("Msg", (), {"content": deterministic_response})()]}
            elif is_staff_billing_query:
                # ─── DETERMINISTIC STAFF BILLING HANDLER ────────────────────
                # CRITICAL: We format the response entirely in Python.
                # Never send staff billing data to the LLM — it will hallucinate.
                # ─────────────────────────────────────────────────────────────
                t1 = time.time()
                from semantic.semantic_layer import get_staff_billing_report
                from .query_parser import _extract_date_range
                from main import _extract_kpi_filters_from_text
                date_from, date_to, _ = _extract_date_range(last_msg_content)
                _filters = _extract_kpi_filters_from_text(last_msg_content)

                sb_args = {"start_date": date_from, "end_date": date_to}
                _sl = _filters.get("service_line")
                _emp = _filters.get("employee_name")
                _cust = _filters.get("customer_name")
                if _sl and _sl.lower() != 'all': sb_args["service_line"] = _sl
                if _emp and _emp.lower() != 'all': sb_args["employee_name"] = _emp
                if _cust and _cust.lower() != 'all': sb_args["customer_name"] = _cust

                import re as _re_sb
                partner_match = _re_sb.search(r'(?:partner\s+is|partner:|partner)\s+([a-zA-Z\s]+)', last_msg_content, _re_sb.IGNORECASE)
                if partner_match:
                    sb_args["project_partner"] = partner_match.group(1).strip()

                raw_sb = await get_staff_billing_report.ainvoke(sb_args)
                print(f"[Router] Staff Billing report tool returned in {time.time()-t1:.2f}s")

                try:
                    parsed_sb = json.loads(raw_sb)
                except Exception:
                    parsed_sb = {}

                summary_sb = parsed_sb.get("summary", {})
                projects_sb = parsed_sb.get("projects", [])
                dr_sb = parsed_sb.get("date_range", {})
                total_projects_sb = summary_sb.get("total_projects", len(projects_sb))

                sd_sb = dr_sb.get("start") or date_from or ""
                ed_sb = dr_sb.get("end") or date_to or ""
                try:
                    from datetime import datetime as _dt_sb
                    sd_p_sb = _dt_sb.strptime(str(sd_sb), "%Y-%m-%d")
                    ed_p_sb = _dt_sb.strptime(str(ed_sb), "%Y-%m-%d")
                    if sd_p_sb.year == ed_p_sb.year and sd_p_sb.month == ed_p_sb.month:
                        period_label_sb = sd_p_sb.strftime("%B %Y")
                    else:
                        period_label_sb = f"{sd_p_sb.strftime('%d %b')} – {ed_p_sb.strftime('%d %b %Y')}"
                except Exception:
                    period_label_sb = f"{sd_sb} to {ed_sb}" if sd_sb and ed_sb else "All Time"

                answer_lines_sb = [
                    "### Staff Billing Report",
                    f"- **Period:** {period_label_sb}",
                ]
                if _sl and _sl.lower() != 'all': answer_lines_sb.append(f"- **Service Line:** {_sl}")
                if _emp and _emp.lower() != 'all': answer_lines_sb.append(f"- **Employee:** {_emp}")
                if _cust and _cust.lower() != 'all': answer_lines_sb.append(f"- **Customer:** {_cust}")
                if sb_args.get("project_partner"): answer_lines_sb.append(f"- **Project Partner:** {sb_args.get('project_partner')}")

                answer_lines_sb.append(f"- **Total Projects:** {total_projects_sb}")

                staff_cost_sb = summary_sb.get("total_staff_cost")
                if staff_cost_sb not in (None, "", "N/A"):
                    answer_lines_sb.append(f"- **Total Staff Billing:** BHD {float(staff_cost_sb):,.2f}")

                app_fees_sb = summary_sb.get("total_approved_fees")
                if app_fees_sb not in (None, "", "N/A"):
                    answer_lines_sb.append(f"- **Total Approved Fees:** BHD {float(app_fees_sb):,.2f}")

                invoiced_sb = summary_sb.get("total_invoiced")
                if invoiced_sb not in (None, "", "N/A"):
                    answer_lines_sb.append(f"- **Total Invoiced:** BHD {float(invoiced_sb):,.2f}")

                if total_projects_sb == 0:
                    answer_lines_sb.append(f"\n⚠️ No records matched the criteria for {period_label_sb}.")

                # Build export data
                ad_hoc_export_data = None
                if projects_sb:
                    columns_sb = list(projects_sb[0].keys())
                    all_rows_sb = [[r.get(c, "") for c in columns_sb] for r in projects_sb]
                    try:
                        gen_date_sb = _dt_sb.now().strftime('%d %b %Y')
                    except Exception:
                        gen_date_sb = "Today"
                    meta_sb = ["Staff Billing Report", f"Generated on: {gen_date_sb}", f"Period: {period_label_sb}"]
                    if _sl and _sl.lower() != 'all': meta_sb.append(f"Service Line: {_sl}")
                    if _emp and _emp.lower() != 'all': meta_sb.append(f"Employee: {_emp}")
                    if _cust and _cust.lower() != 'all': meta_sb.append(f"Customer: {_cust}")
                    if sb_args.get("project_partner"): meta_sb.append(f"Project Partner: {sb_args.get('project_partner')}")
                    ad_hoc_export_data = {
                        "filename": "Staff_Billing_Report",
                        "sheets": [{"name": "Data", "headers": columns_sb, "rows": all_rows_sb, "metadata": meta_sb}]
                    }

                final_answer_sb = "\n".join(answer_lines_sb).strip()
                json_block_sb = json.dumps({
                    "navigate_to": "/projects/reports/staff-billing-report",
                    "navigation_links": [
                        {"label": "Staff Billing Report", "url": "/projects/reports/staff-billing-report"},
                        {"label": "Projects List", "url": "/projects-list"}
                    ],
                    "suggested_questions": [
                        "Show staff billing for Audit service line",
                        "Show active vs completed projects",
                        "What is the total revenue?"
                    ],
                    "chart_data": None,
                    "export_data": ad_hoc_export_data,
                    "report_intent": None,
                    "auto_expand": False
                })
                deterministic_response_sb = f"{final_answer_sb}\n\n```json\n{json_block_sb}\n```"
                return {"messages": [type("Msg", (), {"content": deterministic_response_sb})()]}
            else:
                # Generic AD_HOC: Direct SQL tool call
                t1 = time.time()
                raw_data = await ad_hoc_sql_query.ainvoke({"question": last_msg_content})
                print(f"[Router] Ad-hoc SQL tool returned in {time.time()-t1:.2f}s")
                
                try:
                    parsed = json.loads(raw_data)
                    if isinstance(parsed, dict) and parsed.get("__type") == "ad_hoc_table":
                        columns = parsed["data"]["columns"]
                        all_rows = parsed["data"]["rows"]
                        # Provide top 5 rows to LLM for context
                        header = " | ".join(columns)
                        data_rows = [" | ".join(str(v) for v in row) for row in all_rows[:5]]
                        raw_data = f"Found {len(all_rows)} rows. Top 5 rows shown below:\n" + header + "\n" + "\n".join(data_rows)
                        
                        ad_hoc_export_data = {
                            "filename": "AdHoc_Report",
                            "sheets": [{
                                "name": "Data",
                                "headers": columns,
                                "rows": all_rows
                            }]
                        }
                except:
                    pass

            t2 = time.time()
            formatted = await _format_tool_output(last_msg_content, raw_data, ad_hoc_prompt)
            print(f"[Router] Format pass completed in {time.time()-t2:.2f}s")
            
            # Inject ad_hoc_export_data into the JSON block if present
            if ad_hoc_export_data:
                import re, json
                match = re.search(r'```json\s*(\{.*?\})\s*```', formatted, re.DOTALL)
                if match:
                    try:
                        json_obj = json.loads(match.group(1))
                        json_obj["export_data"] = ad_hoc_export_data
                        new_json = "```json\n" + json.dumps(json_obj) + "\n```"
                        formatted = formatted.replace(match.group(0), new_json)
                    except Exception as e:
                        print("Failed to inject export_data:", e)

            return {"messages": [type("Msg", (), {"content": formatted})()]}

    _agent_executor = RunnableLambda(final_routing_logic)

    return _agent_executor

async def ask_question(history: List[Dict], user_context=None) -> Tuple[str, Optional[Dict], Optional[str], Optional[List[Dict]], Optional[Dict], bool, Optional[List[str]], Optional[str], Optional[str], bool, Optional[str]]:
    """
    Main entry point for the AI agent.
    Returns: (answer, chart_data, navigate_to, navigation_links, export_data, auto_expand, suggested_questions, entity_name, entity_type, is_edit_intent, report_intent)
    """
    import logging
    logger = logging.getLogger(__name__)
    
    latest_question = history[-1]['content'] if history else ""
    role = user_context.get("role", "Staff") if user_context else "Staff"
    employee_id = user_context.get("employee_id", 0) if user_context else 0

    from config.role_tier_config import get_tier_for_role
    user_tier = get_tier_for_role(role)
    if user_tier >= 4 and employee_id:
        scope_key = f"{role}:{employee_id}"
    else:
        scope_key = role
    
    # *** CRITICAL DEBUG: Log resolved role ***
    user_id = user_context.get("user_id", "unknown") if user_context else "unknown"
    department = user_context.get("department", "unknown") if user_context else "unknown"
    logger.info(
        f"[Agent] RECEIVED: user_id={user_id}, employee_id={employee_id}, "
        f"role='{role}', dept='{department}', scope='{scope_key}', q_excerpt='{latest_question[:80]}...'"
    )
    
    # Intercept simple greetings
    q_stripped = latest_question.strip().lower()
    if q_stripped in ["hi", "hello", "hey", "hii", "heya", "howdy", "good morning", "good afternoon", "good evening", "welcome", "hola"]:
        name = user_context.get("user_name", "") if user_context else ""
        name_str = f"{name}" if name and name != "Unknown" else "there"
        return f"Hello {name_str}! How can I help you today?", None, None, None, None, False, ["What is the total revenue?", "What are the pending proposals?"], None, None, False, None

    if _looks_like_sql_write_attempt(latest_question):
        return "⚠️ I can only answer read-only questions about dashboard data.", None, None, None, None, False, None, None, None, False, None

    try:
        # --- NEW: Semantic Cache Check ---
        import asyncio
        
        cached = await get_cached_answer(latest_question, scope_key)

        if cached:
            cached_nav = cached.get("navigate_to")
            if cached_nav and cached_nav.lower().strip() in ["/proposal_win_rate", "/proposal-win-rate", "/proposals", "proposal_win_rate", "proposal"]:
                cached_nav = "/proposal"
            
            return (
                cached.get("answer", ""),
                cached.get("chart_data"),
                cached_nav,          # Index 2 mapping correctly 
                cached.get("navigation_links"),     # Index 3
                cached.get("export_data"),          # Index 4
                cached.get("auto_expand", False),   # auto_expand
                cached.get("suggested_questions"),  # suggested_questions
                None, None, False, cached.get("report_intent")             # entity_name, entity_type, is_edit_intent, report_intent
            )
        # --- End Cache Check ---

        # Convert history array to LangGraph messages state
        # NOTE: Use 'msg_role' to avoid overwriting the outer 'role' (user's actual role for cache)
        langchain_history = []
        for msg in history:
            msg_role = "user" if msg['role'] == "user" else "assistant"
            langchain_history.append({"role": msg_role, "content": msg['content']})

        # The routed agent returns the identical state format as create_react_agent
        response = None
        last_rate_error = None
        for model_name in _groq_model_candidates():
            for attempt in range(GROQ_RETRY_ATTEMPTS):
                try:
                    executor = get_agent_executor(user_context, model_name=model_name)
                    response = await executor.ainvoke({"messages": langchain_history})
                    break
                except Exception as e:
                    if _is_rate_limit_error(e):
                        last_rate_error = e
                        logger.warning(
                            f"[Agent] Groq rate limit on model={model_name} attempt={attempt + 1}/{GROQ_RETRY_ATTEMPTS}; retrying/falling back"
                        )
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise
            if response is not None:
                break

        if response is None and last_rate_error is not None:
            raise last_rate_error
        
        # Safely extract final answer — different agents return different shapes
        if isinstance(response, dict) and "messages" in response:
            last_msg = response["messages"][-1]
            final_answer = last_msg.content.strip() if hasattr(last_msg, 'content') else last_msg["content"].strip()
        elif isinstance(response, dict) and "sql" in response:
            # ad_hoc self-correcting graph stores result in messages
            last_msg = response.get("messages", [{}])[-1]
            final_answer = last_msg.content.strip() if hasattr(last_msg, 'content') else str(last_msg)
        elif isinstance(response, str):
            final_answer = response.strip()
        else:
            final_answer = str(response)
        chart_data = None
        sql_used = None
        navigate_to = None
        navigation_links = None
        suggested_questions = None
        export_data = None
        auto_expand = False
        report_intent = None
        
        # ═══════════════════════════════════════════════════════════════════
        # CRITICAL: Strip all formula-related content from output
        # ═══════════════════════════════════════════════════════════════════
        formula_patterns = [
            r"(?i)Win\s+Rate\s+Formula\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
            r"(?i)Calculation\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
            r"(?i)Formula\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
            r"(?i)Derivation\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
            r"(?i)Working\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
            r"(?i)Result\s*:.*?(?=\n\n|\n[A-Z]|\Z)",
        ]
        import re
        for pattern in formula_patterns:
            final_answer = re.sub(pattern, "", final_answer, flags=re.DOTALL)
        
        latex_patterns = [
            r"\[\\text.*?\]",
            r"\[\\frac.*?\]",
            r"\\\w+\{[^}]+\}",
            r"\\\w+",
        ]
        for pattern in latex_patterns:
            final_answer = re.sub(pattern, "", final_answer)
        
        lines = final_answer.split("\n")
        filtered_lines = []
        for line in lines:
            if ("\\" in line and ("text" in line or "frac" in line or "left" in line or "right" in line)) or \
               (line.count("=") > 0 and ("\\" in line or "(" in line and "/" in line)):
                continue
            filtered_lines.append(line)
        
        final_answer = "\n".join(filtered_lines).strip()
        final_answer = re.sub(r"\n\n\n+", "\n\n", final_answer)

        # Extract JSON block(s) from the response — robust brace-balanced finder
        def _find_json_blocks(text: str) -> list:
            """Extract all top-level JSON objects containing 'navigate_to', handling nesting."""
            results = []
            # Step 1: fenced ```json ... ``` blocks (highest confidence)
            fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if fenced:
                return fenced
            # Step 2: brace-balancing scan for bare JSON blocks
            i = 0
            while i < len(text):
                if text[i] == '{':
                    depth, in_str, esc = 0, False, False
                    for j in range(i, len(text)):
                        ch = text[j]
                        if esc:         esc = False;  continue
                        if ch == '\\' and in_str: esc = True; continue
                        if ch == '"':   in_str = not in_str; continue
                        if in_str:      continue
                        if ch == '{':   depth += 1
                        elif ch == '}': depth -= 1
                        if depth == 0:
                            candidate = text[i:j+1]
                            if '"navigate_to"' in candidate:
                                results.append(candidate)
                            i = j + 1
                            break
                    else:
                        i += 1
                else:
                    i += 1
            return results

        json_matches = _find_json_blocks(final_answer)


        for json_str in json_matches:
            try:
                parsed_json = json.loads(json_str)
                if "chart" in parsed_json:
                    chart_data = parsed_json["chart"]
                if "navigate_to" in parsed_json:
                    navigate_to = parsed_json["navigate_to"]
                if "navigation_links" in parsed_json:
                    navigation_links = parsed_json["navigation_links"]
                if "suggested_questions" in parsed_json:
                    suggested_questions = parsed_json["suggested_questions"]
                if "export_data" in parsed_json:
                    export_data = parsed_json["export_data"]
                if "auto_expand" in parsed_json:
                    auto_expand = bool(parsed_json["auto_expand"])
                if "entity_name" in parsed_json:
                    entity_name = parsed_json.get("entity_name")
                if "entity_type" in parsed_json:
                    entity_type = parsed_json.get("entity_type")
                if "is_edit_intent" in parsed_json:
                    is_edit_intent = bool(parsed_json.get("is_edit_intent"))
                if "report_intent" in parsed_json:
                    report_intent = parsed_json.get("report_intent")
            except Exception as e:
                print(f"[AI JSON Parse Error]: {e}")
        
        # Fallback suggestions if AI failed to provide them
        if not suggested_questions or len(suggested_questions) == 0:
            if "tax" in q_stripped or "vat" in q_stripped:
                suggested_questions = ["Show me the VAT summary", "What are the pending tax payments?"]
            elif "receivable" in q_stripped or "billing" in q_stripped or "payment" in q_stripped:
                suggested_questions = ["What are the overdue receivables?", "Show me the collection report"]
            elif "project" in q_stripped or "task" in q_stripped:
                suggested_questions = ["Show me my active projects", "What are my upcoming deadlines?"]
            else:
                suggested_questions = ["What is the total revenue?", "Show me the service pipeline"]

        # Catch LLM hallucinations for proposal win rate routing
        if navigate_to and navigate_to.lower().strip() in ["/proposal_win_rate", "/proposal-win-rate", "/proposals", "proposal_win_rate", "proposal"]:
            navigate_to = "/proposal"

        # ── HARD ALLOWLIST ENFORCEMENT (Python code, not LLM prompt) ──────────────
        # No matter what the LLM outputs, navigate_to MUST be one of these exact routes.
        # If the LLM hallucinated a URL (e.g. /service_line_dashboard), correct it here.
        _VALID_ROUTES = {
            "/", "/crm-dashboard", "/proposal", "/projects-list", "/projects/tasks",
            "/projects/reports", "/projects/reports/kpi-summary-report",
            "/projects/reports/project-status-report",
            "/projects/reports/project-ageing-report",
            "/projects/reports/staff-billing-report",
            "/projects/reports/resource-utilization-report",
            "/projects/reports/project-recoverability-report",
            "/billing/invoice", "/billing/reports",
            "/billing/reports/receivable-report",
            "/billing/reports/invoice-summary-report",
            "/service-lead", "/customer", "/setting", "/meetings",
            "/client-satisfaction", "/crm/reports",
            "/crm/reports/proposal-status-report",
            "/crm/reports/service-lead-report",
            "/self-services/leave-request",
        }
        if navigate_to and navigate_to not in _VALID_ROUTES:
            # Try to correct it intelligently based on keywords in the bad URL
            _bad = (navigate_to or "").lower()
            if "service" in _bad or "dashboard" in _bad:
                navigate_to = "/crm-dashboard"
            elif "kpi" in _bad or "summary" in _bad:
                navigate_to = "/projects/reports/kpi-summary-report"
            elif "utiliz" in _bad or "resource" in _bad:
                navigate_to = "/projects/reports/resource-utilization-report"
            elif "receiv" in _bad or "aging" in _bad or "ageing" in _bad:
                navigate_to = "/billing/reports/receivable-report"
            elif "invoice" in _bad or "billing" in _bad:
                navigate_to = "/billing/invoice"
            elif "proposal" in _bad or "pipeline" in _bad:
                navigate_to = "/proposal"
            elif "project" in _bad or "task" in _bad or "estimat" in _bad:
                navigate_to = "/projects-list"
            elif "lead" in _bad:
                navigate_to = "/service-lead"
            else:
                navigate_to = "/"
            print(f"[NavGuard] Corrected hallucinated route '{_bad}' → '{navigate_to}'")
        # ── END HARD ALLOWLIST ENFORCEMENT ────────────────────────────────────────

        # --- NEW: Forced Navigation Interception ---
        # If the semantic layer injected an explicit metadata instruction, we forcefully parse it
        # and override whatever the LLM decided for navigate_to.
        override_match = re.search(r'CRITICAL: set your final navigate_to field EXACTLY to:\s*([^\n\"]+)', final_answer)
        if override_match:
            forced_route = override_match.group(1).strip()
            # Clean off any trailing json commas or brackets just in case
            navigate_to = re.sub(r'[\"\},]', '', forced_route).strip()
            # Remove the instruction from the human-visible text
            final_answer = re.sub(r'metadata_instructions.*?:.*?CRITICAL: set your final navigate_to field EXACTLY to:\s*[^\n]+', '', final_answer, flags=re.IGNORECASE)

        # Remove all JSON blocks from visible answer — brace-balanced stripping
        def _strip_json_blocks(text: str) -> str:
            """Remove all top-level JSON objects/arrays containing system fields from text."""
            # Pass 1: Remove fenced ```json...``` blocks
            text = re.sub(r"```(?:json)?\s*[\{\[].*?[\}\]]\s*```", "", text, flags=re.DOTALL)
            # Pass 2: Brace-balanced removal of bare JSON blocks
            out, i = [], 0
            while i < len(text):
                if text[i] in '{[':
                    open_ch = text[i]
                    close_ch = '}' if open_ch == '{' else ']'
                    depth, in_str, esc, start = 0, False, False, i
                    found = False
                    for j in range(i, len(text)):
                        ch = text[j]
                        if esc:         esc = False;  continue
                        if ch == '\\' and in_str: esc = True; continue
                        if ch == '"':   in_str = not in_str; continue
                        if in_str:      continue
                        if ch == open_ch:   depth += 1
                        elif ch == close_ch: depth -= 1
                        if depth == 0:
                            candidate = text[start:j+1]
                            sys_keys = ['"navigate_to"', '"navigation_links"', '"export_data"', '"kpi_payload"', '"url"']
                            if not any(k in candidate for k in sys_keys):
                                out.append(candidate)  # keep non-system blocks
                            i = j + 1
                            found = True
                            break
                    if not found:
                        out.append(text[i])
                        i += 1
                else:
                    out.append(text[i])
                    i += 1
            return re.sub(r'\n{3,}', '\n\n', ''.join(out)).strip()

        final_answer = _strip_json_blocks(final_answer)

        # Recover export payload when model emits it as plain text section
        if not export_data:
            export_match = re.search(
                r'\{\s*\"filename\"\s*:\s*\"[^\"]+\"\s*,\s*\"sheets\"\s*:\s*\[[\s\S]*?\]\s*\}',
                final_answer,
            )
            if export_match:
                try:
                    export_data = json.loads(export_match.group(0))
                except Exception:
                    pass

        final_answer = _sanitize_user_visible_answer(final_answer)

        # KPI hard override: keep navigation deterministic for KPI summary/report prompts.
        if (await classify_intent(latest_question)) == "kpi_summary" or report_intent == "kpi_summary":
            report_intent = "kpi_summary"
            navigate_to = "/projects/reports/kpi-summary-report"
            if not navigation_links:
                navigation_links = [
                    {
                        "label": "KPI Summary Report",
                        "url": "/projects/reports/kpi-summary-report",
                    }
                ]
                
        # Resource Utilization override
        _res_util_kws = {"resource utilization", "resource utilisation", "utilization report",
                         "utilisation report", "billable hours", "chargeable hours"}
        if any(kw in latest_question.lower() for kw in _res_util_kws) or report_intent == "resource_utilization":
            report_intent = "resource_utilization"
            navigate_to = "/projects/reports/resource-utilization-report"

        # --- NEW: Store in Semantic Cache on Miss ---
        if not cached:
            try:
                from rag.vector_store_v2 import store_vector_cache as store_ans
                # Trigger storage without blocking the response
                asyncio.create_task(store_ans(latest_question.strip().lower(), final_answer, chart_data, sql_used or "", scope_key, navigate_to, navigation_links, export_data, auto_expand, suggested_questions, report_intent))
            except Exception as e:
                print(f"[Cache Store Error]: {e}")

        # Ensure new fields fallbacks
        entity_name = locals().get("entity_name", None)
        entity_type = locals().get("entity_type", None)
        is_edit_intent = locals().get("is_edit_intent", False)

        return final_answer, chart_data, navigate_to, navigation_links, export_data, auto_expand, suggested_questions, entity_name, entity_type, is_edit_intent, report_intent

    except Exception as e:
        error_msg = str(e)
        if _is_rate_limit_error(e):
            print("ACTUAL RATE LIMIT ERROR:", repr(error_msg))
            return "⚠️ API rate limit reached. Please wait a moment and try again.", None, None, None, None, False, None, None, None, False, None
        print(f"[AI ERROR] {traceback.format_exc()}")
        return f"Sorry, I encountered an error: {error_msg}", None, None, None, None, False, None, None, None, False, None


async def ask_question_async(history: List[Dict], user_context=None) -> Tuple[str, Optional[Dict], Optional[str], Optional[List[Dict]], Optional[Dict], bool, Optional[List[str]], Optional[str], Optional[str], bool, Optional[str]]:
    """Wrapper that returns exactly 11 values for the /ask-ai and /ask-ai-stream endpoints."""
    return await ask_question(history, user_context)


# ---------------------------------------------------------------------------
# Helper: parse JSON metadata blocks out of LLM response text
# Used by both ask_question() and ask_question_streaming()
# ---------------------------------------------------------------------------
def _parse_llm_json_blocks(text: str) -> dict:
    """Extract navigate_to, chart_data, navigation_links, suggested_questions,
    export_data, auto_expand, entity_name, entity_type, is_edit_intent
    from a raw LLM response string. Returns a dict of parsed values."""
    result = {
        "chart_data": None,
        "navigate_to": None,
        "navigation_links": None,
        "suggested_questions": None,
        "export_data": None,
        "auto_expand": False,
        "entity_name": None,
        "entity_type": None,
        "is_edit_intent": False,
        "report_intent": None,
    }
    import re, json

    json_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not json_matches:
        fallback = re.search(r'(\{\s*\"navigate_to\"[\s\S]*?\})\s*$', text)
        if fallback:
            json_matches = [fallback.group(1)]

    for json_str in json_matches:
        try:
            parsed = json.loads(json_str)
            if "chart" in parsed:
                result["chart_data"] = parsed["chart"]
            if "navigate_to" in parsed:
                result["navigate_to"] = parsed["navigate_to"]
            if "navigation_links" in parsed:
                result["navigation_links"] = parsed["navigation_links"]
            if "suggested_questions" in parsed:
                result["suggested_questions"] = parsed["suggested_questions"]
            if "export_data" in parsed:
                result["export_data"] = parsed["export_data"]
            if "auto_expand" in parsed:
                result["auto_expand"] = bool(parsed["auto_expand"])
            if "entity_name" in parsed:
                result["entity_name"] = parsed.get("entity_name")
            if "entity_type" in parsed:
                result["entity_type"] = parsed.get("entity_type")
            if "is_edit_intent" in parsed:
                result["is_edit_intent"] = bool(parsed.get("is_edit_intent"))
            if "report_intent" in parsed:
                result["report_intent"] = parsed.get("report_intent")
        except Exception as e:
            print(f"[AI JSON Parse Error in _parse_llm_json_blocks]: {e}")

    return result


def _sanitize_user_visible_answer(text: str) -> str:
    """Remove technical payload leaks (SQL and raw export JSON) from final visible text."""
    if not text:
        return text

    cleaned = text

    # Remove fenced SQL blocks
    cleaned = re.sub(r"```sql[\s\S]*?```", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```[\s\S]*?\b(SELECT|WITH)\b[\s\S]*?```", "", cleaned, flags=re.IGNORECASE)

    # Remove plain export sections
    cleaned = re.sub(r"(?is)(?:^|\n)#+\s*export\s+data\s*\n+\s*\{[\s\S]*?(?=\n{2,}|\Z)", "\n", cleaned)
    cleaned = re.sub(r"(?is)(?:^|\n)export\s+data\s*\n+\s*\{[\s\S]*?(?=\n{2,}|\Z)", "\n", cleaned)

    # Remove standalone export payload object
    cleaned = re.sub(
        r"(?is)\{\s*\"filename\"\s*:\s*\"[^\"]+\"\s*,\s*\"sheets\"\s*:\s*\[[\s\S]*?\]\s*\}",
        "",
        cleaned,
    )

    # Remove plain SQL queries that leaked without fences
    filtered_lines = []
    in_sql = False
    for line in cleaned.splitlines():
        if not in_sql and re.match(r"^\s*(SELECT|WITH)\b", line, flags=re.IGNORECASE):
            in_sql = True
            if ";" in line:
                in_sql = False
            continue
        if in_sql:
            if ";" in line:
                in_sql = False
            continue
        filtered_lines.append(line)

    cleaned = "\n".join(filtered_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# ask_question_streaming -- REAL token streaming via OpenAI stream=True
# Yields SSE token events for real-time typewriter streaming in the frontend.
# ---------------------------------------------------------------------------
async def ask_question_streaming(history, user_context=None):
    """
    Real token streaming generator using OpenAI stream=True.
    Tokens arrive from the API as they are generated — no fake delays.

    Yields dicts:
      {"type": "token",  "content": "partial text"}   -- live tokens from OpenAI
      {"type": "done",   "chart_data": ..., ...}       -- final metadata
      {"type": "error",  "content": "..."}             -- on failure
    """
    import asyncio
    try:
        # Build user context for RBAC and prompting
        user_ctx = user_context or {}
        role_name = user_ctx.get("role_name") or user_ctx.get("role") or "Unknown"

        from config.role_tier_config import get_tier_for_role
        user_tier = get_tier_for_role(role_name)
        employee_id = user_ctx.get("employee_id") or user_ctx.get("user_id", 0)
        employee_id_str = str(employee_id) if employee_id else "0"

        from config.role_tier_config import build_rbac_prompt

        current_year = datetime.now().year
        current_date = datetime.now().strftime('%Y-%m-%d')

        _admin_roles = ("super admin", "administrator", "admin")
        rbac_injection = ""
        if role_name and role_name.lower() not in ("", "none", "unknown") + _admin_roles:
            rbac_injection = build_rbac_prompt(
                user_name=user_ctx.get("user_name", "Unknown"),
                role_name=role_name,
                department=user_ctx.get("department", "Unknown"),
            )

        if user_tier <= 4:
            tool_instruction = (
                f"\u26a0\ufe0f  DATA SCOPING: By default, fetch company-wide aggregates. "
                f"Only scope to employee_id={employee_id_str} if the user explicitly asks "
                f"for personal data."
            )
        else:
            tool_instruction = (
                f"\u26a0\ufe0f  DATA SCOPING (MANDATORY SECURITY): You are Tier {user_tier}. "
                f"ALWAYS pass employee_id={employee_id_str} to metric tools. "
                f"NEVER return company-wide totals."
            )

        # ── STREAMING ROUTER: classify + pre-fetch DB data before streaming ──
        # The streaming path must mirror the non-streaming routing logic:
        # classify the question first, fetch real DB data, then stream the formatted answer.
        latest_q = history[-1].get("content", "") if history else ""
        q_lower = latest_q.lower().strip()

        # Set RBAC context on semantic_layer so tools respect user scope
        from semantic import semantic_layer as _sl
        _sl.set_user_context({
            'employee_id': int(employee_id_str) if employee_id_str != '0' else None,
            'user_tier': user_tier,
            'role_name': role_name,
        })

        # Detect customer+metric queries — these MUST go to SQL, not LLM hallucination
        _customer_signals = {"customer", "client", "clients", "customers"}
        _metric_signals = {
            "revenue", "billing", "invoice", "invoices", "collection",
            "paid", "unpaid", "outstanding", "receivable", "receivables",
            "aging", "ageing", "overdue", "top", "highest", "most",
        }
        _dashboard_signals = {
            "receivable", "receivables", "billing", "pipeline", "proposal",
            "proposals", "lead", "leads", "project", "projects", "task", "tasks",
            "overdue", "kpi", "dashboard", "win rate", "conversion", "outstanding",
            "paid", "unpaid", "aging", "ageing", "service line", "budget",
            "gross profit", "secured business", "kpi summary", "kpi report",
            "service line performance", "gp performance", "department utilization",
            "gp by service line", "team billing", "revenue by service line",
        }

        has_customer_signal = any(kw in q_lower for kw in _customer_signals)
        has_metric_signal = any(kw in q_lower for kw in _metric_signals)
        has_revenue_kw = "revenue" in q_lower

        # ── EARLY EXIT: Resource Utilization with no date → show FY picker first ──
        # If user asks for utilization without specifying a year/month,
        # return immediately with show_fy_picker=True so the frontend shows the
        # calendar widget BEFORE running any SQL. SQL runs only after the user
        # selects a date range and clicks "Show Utilization Data →".
        _res_util_kws = {
            "resource utilization", "resource utilisation", "utilization report",
            "utilisation report", "billable hours", "chargeable hours",
        }
        _is_resource_util_q = any(kw in q_lower for kw in _res_util_kws)
        if _is_resource_util_q:
            from .query_parser import _extract_date_range, _extract_person_name
            _, _, _date_was_specified_early = _extract_date_range(latest_q)
            if not _date_was_specified_early:
                _emp_name_early = _extract_person_name(latest_q)
                _intro = (
                    f"I found **{_emp_name_early}** in the system. "
                    if _emp_name_early else ""
                )
                yield {
                    "type": "token",
                    "content": (
                        f"{_intro}Please select the **Financial Year** and **period** "
                        f"you'd like to view the resource utilization for."
                    ),
                }
                yield {
                    "type":               "done",
                    "content":            (
                        f"{_intro}Please select the Financial Year and period you'd "
                        f"like to view the resource utilization for."
                    ),
                    "report_intent":      "resource_utilization",
                    "show_fy_picker":     True,
                    "entity_name":        _emp_name_early,
                    "navigate_to":        "/projects/reports/resource-utilization-report",
                    "chart_data":         None,
                    "navigation_links":   [],
                    "suggested_questions":[],
                    "export_data":        None,
                    "auto_expand":        False,
                }
                return   # stop here — do NOT call the LLM

        prefetched_data = ""

        _ESTIMATION_SIGNALS = [
            "estimation report", "total estimation", "estimated hours", "actual hours",
            "hour overrun", "exceeded hours", "exceeded approved", "approved hours",
            "over budget hours", "over estimated", "estimated project", "exceede",
            "total estimation report",  # explicit match for ServiceLinePickerPanel output
        ]

        # Extract dates globally for all tools to use
        from .query_parser import _extract_date_range
        try:
            ext_start, ext_end, dws = _extract_date_range(latest_q)
        except Exception:
            ext_start, ext_end, dws = None, None, False
        
        if not dws and user_ctx and user_ctx.get("start_date") and user_ctx.get("end_date"):
            global_date_from = user_ctx.get("start_date")
            global_date_to = user_ctx.get("end_date")
        else:
            global_date_from = ext_start
            global_date_to = ext_end
            
        dashboard_args = {}
        if global_date_from: dashboard_args["start_date"] = global_date_from
        if global_date_to: dashboard_args["end_date"] = global_date_to


        _ANALYTICAL_MARKERS_STRONG = [
            "recent", "latest", "lowest", "highest", "top", "bottom",
            "compare", "specific", "a proposal", "an invoice", "the project", "detail",
            "last", "first", "which", "who made", "when was", "when did", "single",
            "most recent", "what was the last", "which was the last",
        ]
        is_analytical = any(marker in q_lower for marker in _ANALYTICAL_MARKERS_STRONG)

        if (has_customer_signal and has_metric_signal) or is_analytical:
            # Dynamic/Specific query: run real SQL and inject result into context
            try:
                prefetched_data = await ad_hoc_sql_query.ainvoke({"question": latest_q})
            except Exception as _e:
                prefetched_data = f"(DB query failed: {_e})"
        elif any(sig in q_lower for sig in _ESTIMATION_SIGNALS):
            # First try to extract service line directly from the message text
            # (new ServiceLinePickerPanel embeds "service line X" in the message)
            import re as _re_est
            _sl_direct_match = _re_est.search(
                r"(?:service line|serviceline)\s+([A-Za-z &]+?)\s+(?:for|$)",
                latest_q, _re_est.IGNORECASE
            )
            if _sl_direct_match:
                extracted_sl = _sl_direct_match.group(1).strip()
            else:
                # Fallback: use LLM to extract from conversation context
                full_context = "\n".join([m.get('content', '') for m in history[-3:]])
                check_sl_prompt = (
                    f"Extract the service line from this conversation if present "
                    f"(e.g. Audit, Tax, Advisory, BPO). If no service line is mentioned, "
                    f"reply EXACTLY with 'NONE'.\nConversation:\n{full_context}"
                )
                sl_llm = _build_llm(model_name=GROQ_PRIMARY_MODEL, temperature=0, max_tokens=15)
                sl_resp = await sl_llm.ainvoke([{"role": "user", "content": check_sl_prompt}])
                extracted_sl = sl_resp.content.strip()
            
            if not extracted_sl or "NONE" in extracted_sl.upper():
                yield {
                    "type": "token",
                    "content": "Please select a service line to generate the Total Estimation Report.",
                }
                yield {
                    "type": "done",
                    "content": "Please select a service line to generate the Total Estimation Report.",
                    "report_intent": "estimation_sl_picker",
                    "navigate_to": None,
                    "chart_data": None,
                    "navigation_links": [],
                    "export_data": None,
                    "auto_expand": False,
                }
                return
            else:
                try:
                    import json
                    from semantic.semantic_layer import get_total_estimation_report
                    # "all time" means no date filter — pass None to fetch all records
                    if "all time" in q_lower:
                        date_from, date_to = None, None
                    else:
                        date_from, date_to = global_date_from, global_date_to
                    sl_val = extracted_sl.replace("'", "").replace('"', "").strip()
                    prefetched_data = await get_total_estimation_report.ainvoke({
                        "start_date": date_from,
                        "end_date": date_to,
                        "service_line": sl_val
                    })
                    
                    if isinstance(prefetched_data, str) and prefetched_data.startswith("{"):
                        est_data = json.loads(prefetched_data)
                        
                        # Build Markdown Table
                        lines = [
                            f"### Total Estimation Report for **{sl_val}**",
                            "",
                            "| Project Code | Hours | Actual Hours | Hours Difference |",
                            "|---|---:|---:|---:|"
                        ]
                        export_rows = []
                        import base64
                        for p in est_data.get("projects", []):
                            diff = float(p.get('hours_difference', 0) or 0)
                            diff_str = f"+{diff} 🔴" if diff > 0 else f"{diff} 🟢" if diff < 0 else "0"
                            
                            # Build the encoded state for frontend routing
                            p_id = p.get('project_id')
                            p_code = p.get('project_code', '-')
                            p_name = p.get('project_name', '-')
                            state_dict = {"id": p_id, "ProjectName": p_name, "ProjectCode": p_code, "tab": "4"}
                            encoded_state = base64.b64encode(json.dumps(state_dict).encode('utf-8')).decode('utf-8')
                            url = f"/projects/individual-project?state={encoded_state}"
                            
                            link_md = f"[{p_code}]({url})" if p_id else p_code
                            
                            lines.append(f"| {link_md} | {p.get('estimated_hours',0)} | {p.get('actual_hours',0)} | {diff_str} |")
                            export_rows.append([
                                p_code, p_name, 
                                p.get('estimated_hours',0), p.get('actual_hours',0), diff
                            ])
                        
                        export_data = {
                            "filename": f"Estimation_Report_{sl_val}.xlsx",
                            "sheets": [{
                                "name": "Estimation",
                                "headers": ["Project Code", "Project Name", "Estimated Hours", "Actual Hours", "Hours Difference"],
                                "rows": export_rows
                            }]
                        }
                        
                        answer_text = "\n".join(lines)
                        
                        # Stream the table instantly
                        for i, word in enumerate(answer_text.split(" ")):
                            yield {"type": "token", "content": word + (" " if i < len(answer_text.split()) - 1 else "")}
                            import asyncio
                            await asyncio.sleep(0.005)
                            
                        yield {
                            "type": "done",
                            "content": answer_text,
                            "chart_data": None,
                            "export_data": export_data,
                            "report_intent": "estimation",
                            "navigate_to": "/projects-list",
                            "navigation_links": [{"label": sl_val, "url": "/projects-list"}]
                        }
                        return
                    else:
                        prefetched_data = f"(Estimation fetch failed: {prefetched_data})"
                except Exception as _e:
                    prefetched_data = f"(Estimation fetch failed: {_e})"
        elif has_revenue_kw and not has_customer_signal:
            # Pure revenue/dashboard query: fetch service-line aggregate data
            try:
                from semantic.semantic_layer import get_revenue_metrics
                prefetched_data = await get_revenue_metrics.ainvoke(dashboard_args)
            except Exception as _e:
                prefetched_data = f"(Revenue fetch failed: {_e})"
        elif any(kw in q_lower for kw in ["receivable", "outstanding", "overdue payment"]):
            try:
                from semantic.semantic_layer import get_receivables_metrics
                prefetched_data = await get_receivables_metrics.ainvoke(dashboard_args)
            except Exception as _e:
                prefetched_data = f"(Receivables fetch failed: {_e})"
        elif any(kw in q_lower for kw in ["pipeline", "proposal", "proposals", "lead", "leads"]):
            try:
                from semantic.semantic_layer import get_pipeline_and_proposals
                prefetched_data = await get_pipeline_and_proposals.ainvoke(dashboard_args)
            except Exception as _e:
                prefetched_data = f"(Pipeline fetch failed: {_e})"
        elif any(kw in q_lower for kw in ["project", "projects", "task", "tasks", "active project"]):
            # CRITICAL: If the user asks for a LIST of individual items,
            # use ad_hoc_sql_query instead of get_active_projects_metrics (which only returns aggregate counts).
            # Without this guard, the LLM fabricates fake task/project rows (hallucination).
            _LIST_DETAIL_STREAM = [
                "list the", "list my", "list all", "list of", "show me the",
                "show all", "show my", "pending task", "pending tasks",
                "my tasks", "my pending", "overdue task", "overdue tasks",
                "assigned task", "assigned tasks", "incomplete task", "incomplete tasks",
                "open tasks", "list task", "list tasks",
            ]
            _is_list_query = any(sig in q_lower for sig in _LIST_DETAIL_STREAM)
            if _is_list_query:
                try:
                    prefetched_data = await ad_hoc_sql_query.ainvoke({"question": latest_q})
                except Exception as _e:
                    prefetched_data = f"(Task list query failed: {_e})"
            else:
                try:
                    from semantic.semantic_layer import get_active_projects_metrics
                    prefetched_data = await get_active_projects_metrics.ainvoke(dashboard_args)
                except Exception as _e:
                    prefetched_data = f"(Projects fetch failed: {_e})"
        data_context_block = ""
        if prefetched_data:
            data_context_block = (
                f"\n\n=== RETRIEVED CRM DATA (use ONLY this data to answer) ===\n"
                f"{prefetched_data}\n"
                f"=== END OF CRM DATA ===\n"
                f"CRITICAL: Answer ONLY from the data above. "
                f"If the data contains customer names, list THOSE customers — never substitute service lines. "
                f"NEVER fabricate individual items, task IDs, employee names, or project names that are not in the data above. "
                f"If the user asks for a list of items but the data only has aggregate counts/totals, say so honestly and navigate them to the relevant CRM page."
            )
        else:
            data_context_block = (
                f"\n\nCRITICAL WARNING: No CRM database records were retrieved for this query. "
                f"If the user is asking for specific data (like a list of projects, customers, invoices, or performance metrics), "
                f"you MUST honestly state that you do not have any data matching their request for that period. "
                f"NEVER fabricate, guess, or hallucinate any numbers, dates, items, names, or rows under any circumstances."
            )

        system_content = (
            f"You are ANTIGRAVITY, an elite CRM Data Analyst for Grant Thornton Bahrain.\n"
            f"Current Year: {current_year} | Current Date: {current_date}\n\n"
            f"{tool_instruction}\n\n"
            f"{rbac_injection}\n\n"
            f"{data_context_block}\n\n"
            "EVERY response MUST end with a ```json block containing navigate_to, navigation_links, "
            "entity_name, entity_type, is_edit_intent, report_intent, suggested_questions, chart_data, export_data, auto_expand.\n"
            "CRITICAL: navigation_links MUST be an array of objects: [{\"label\": \"Link Name\", \"url\": \"/path\"}]. NEVER an array of strings.\n\n"
            f"Currency: BHD. Format as \"BHD X,XXX.XX\". NEVER use $ or USD.\n"
            "Be concise and direct. Use bullet points or small tables. Use emojis sparingly. NEVER show SQL.\n"
            "PRIVACY: NEVER mention user roles (Superadmin) or tiers in the answer.\n"
        )
        system_content += (
            "INTENT: If the user asks for a 'receivable report' or 'ageing summary' without filters, SET `report_intent: 'receivable'` and ask which scope they want: overall, group, pipeline, or customized."
        )

        # Convert history to OpenAI message format
        messages = [{"role": "system", "content": system_content}]
        for msg in history:
            msg_role = "user" if msg.get("role") == "user" else "assistant"
            messages.append({"role": msg_role, "content": msg.get("content", "")})

        full_answer = ""
        streamed_ok = False
        last_rate_error = None
        
        token_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "model_name": None}

        for model_name in _groq_model_candidates():
            for attempt in range(GROQ_RETRY_ATTEMPTS):
                answer_parts = []
                try:
                    llm = _build_llm(model_name=model_name, temperature=0.1, max_tokens=2000)
                    async for chunk in llm.astream(messages):
                        # Extract token usage metadata if available
                        if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                            token_usage["input_tokens"] = chunk.usage_metadata.get('input_tokens', token_usage["input_tokens"])
                            token_usage["output_tokens"] = chunk.usage_metadata.get('output_tokens', token_usage["output_tokens"])
                            token_usage["total_tokens"] = chunk.usage_metadata.get('total_tokens', token_usage["total_tokens"])
                        elif hasattr(chunk, 'response_metadata') and chunk.response_metadata and 'token_usage' in chunk.response_metadata:
                            tu = chunk.response_metadata['token_usage']
                            token_usage["input_tokens"] = tu.get('prompt_tokens', token_usage["input_tokens"])
                            token_usage["output_tokens"] = tu.get('completion_tokens', token_usage["output_tokens"])
                            token_usage["total_tokens"] = tu.get('total_tokens', token_usage["total_tokens"])

                        if chunk.content:
                            answer_parts.append(chunk.content)
                            yield {"type": "token", "content": chunk.content}

                    full_answer = "".join(answer_parts)
                    streamed_ok = True
                    token_usage["model_name"] = model_name
                    
                    # Fallback token approximation if API doesn't return metadata
                    if token_usage["total_tokens"] == 0:
                        est_in = len(system_content.split()) + len(latest_q.split())
                        est_out = len(full_answer.split())
                        token_usage["input_tokens"] = int(est_in * 1.3)
                        token_usage["output_tokens"] = int(est_out * 1.3)
                        token_usage["total_tokens"] = token_usage["input_tokens"] + token_usage["output_tokens"]

                    break
                except Exception as e:
                    if _is_rate_limit_error(e):
                        last_rate_error = e
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise
            if streamed_ok:
                break

        if not streamed_ok and last_rate_error is not None:
            raise last_rate_error

        # Parse metadata JSON blocks from the full streamed answer
        parsed = _parse_llm_json_blocks(full_answer)

        # Strip JSON blocks from the visible answer text
        import re
        clean_answer = re.sub(r"```(?:json)?\s*\{[\s\S]*?\}\s*```", "", full_answer, flags=re.DOTALL).strip()
        clean_answer = re.sub(r'\{[\s\S]*"navigate_to"[\s\S]*\}\s*$', "", clean_answer).strip()
        clean_answer = _sanitize_user_visible_answer(clean_answer)

        if not parsed.get("export_data"):
            export_match = re.search(
                r'\{\s*\"filename\"\s*:\s*\"[^\"]+\"\s*,\s*\"sheets\"\s*:\s*\[[\s\S]*?\]\s*\}',
                full_answer,
            )
            if export_match:
                try:
                    parsed["export_data"] = json.loads(export_match.group(0))
                except Exception:
                    pass

        # ── Determine navigate_to and report_intent for the done event ──────
        _res_util_kws = {"resource utilization", "resource utilisation", "utilization report",
                         "utilisation report", "billable hours", "chargeable hours"}
        _is_resource_util = any(kw in q_lower for kw in _res_util_kws)

        from .query_parser import _extract_date_range
        _, _, _date_was_specified = _extract_date_range(latest_q)
        _show_fy_picker = _is_resource_util and not _date_was_specified

        # Detect specific employee name was mentioned
        from .query_parser import _extract_person_name
        _entity_name = _extract_person_name(latest_q) if _is_resource_util else ""

        _is_kpi = parsed.get("report_intent") == "kpi_summary"
        
        # If the agent actually generated a real answer, do NOT trigger the KPI filter panel,
        # otherwise the frontend will hide the agent's text and just show the filters!
        if _is_kpi and clean_answer and len(clean_answer) > 30:
            _is_kpi = False
            parsed["report_intent"] = "other"

        if _is_kpi:
            _final_navigate_to   = "/projects/reports/kpi-summary-report"
            _final_report_intent = "kpi_summary"
        elif _is_resource_util:
            _final_navigate_to   = "/projects/reports/resource-utilization-report"
            _final_report_intent = "resource_utilization"
        else:
            _raw_nav = parsed["navigate_to"]
            _VALID_STREAM = {
                "/", "/crm-dashboard", "/proposal", "/projects-list", "/projects/tasks",
                "/projects/reports", "/projects/reports/kpi-summary-report",
                "/projects/reports/project-status-report",
                "/projects/reports/project-ageing-report",
                "/projects/reports/staff-billing-report",
                "/projects/reports/resource-utilization-report",
                "/billing/invoice", "/billing/reports",
                "/billing/reports/receivable-report",
                "/billing/reports/invoice-summary-report",
                "/service-lead", "/customer", "/setting", "/meetings",
                "/client-satisfaction", "/crm/reports",
                "/crm/reports/proposal-status-report",
                "/crm/reports/service-lead-report",
                "/self-services/leave-request",
            }
            if _raw_nav not in _VALID_STREAM:
                _b = (_raw_nav or "").lower()
                if "service" in _b or "dashboard" in _b:  _raw_nav = "/crm-dashboard"
                elif "kpi" in _b or "summary" in _b:      _raw_nav = "/projects/reports/kpi-summary-report"
                elif "estimat" in _b or "project" in _b:  _raw_nav = "/projects-list"
                elif "receiv" in _b:                       _raw_nav = "/billing/reports/receivable-report"
                elif "proposal" in _b or "pipeline" in _b: _raw_nav = "/proposal"
                elif "lead" in _b:                         _raw_nav = "/service-lead"
                else:                                      _raw_nav = "/"
                print(f"[StreamNavGuard] Corrected '{parsed['navigate_to']}' → '{_raw_nav}'")
            _final_navigate_to   = _raw_nav
            _final_report_intent = parsed["report_intent"]

        yield {
            "type":               "done",
            "content":            clean_answer or full_answer,
            "chart_data":         parsed["chart_data"],
            "navigate_to":        _final_navigate_to,
            "navigation_links":   parsed["navigation_links"] or [],
            "suggested_questions":parsed["suggested_questions"] or [],
            "export_data":        parsed["export_data"],
            "auto_expand":        parsed["auto_expand"],
            "report_intent":      _final_report_intent,
            "show_fy_picker":     _show_fy_picker,
            "entity_name":        _entity_name,
            "token_usage":        token_usage,
        }

    except Exception as e:
        print(f"[AskQuestionStreaming] Error: {e}")
        yield {
            "type": "done",
            "content": f"⚠️ I encountered an error streaming your response: {str(e)}",
            "error_code": "streaming_error",
        }
        yield {"type": "done", "content": "[DONE]\n\n"}
