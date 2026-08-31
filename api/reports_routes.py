"""
reports_routes.py — FastAPI router for AI Usage Analytics Reports.
Provides endpoints for AI Email Parsing Usage and AI Chatbot Usage metrics.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, date

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text as sql_text

from db.database import get_db_engine
from api.chat_routes import _decode_jwt, security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["AI Reports"])


def _parse_date(d_str: Optional[str]) -> Optional[datetime]:
    """Helper to parse ISO or date strings cleanly."""
    if not d_str:
        return None
    try:
        clean = d_str.strip().replace("Z", "+00:00")
        if "T" in clean:
            return datetime.fromisoformat(clean).replace(tzinfo=None)
        return datetime.strptime(clean[:10], "%Y-%m-%d")
    except Exception as e:
        logger.warning(f"[Reports] Could not parse date '{d_str}': {e}")
        return None


@router.get("/ai-email-usage")
async def get_ai_email_usage_report(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    employee_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    # Boundary normalization: Map incoming external start_date/end_date to internal date_from/date_to
    date_from = start_date or date_from
    date_to = end_date or date_to
    """
    Returns aggregated KPIs, User Leaderboard, Document Type Breakdown,
    and paginated telemetry logs from `ai_email_parsing` joined with `employees`.
    """
    user_context = _decode_jwt(credentials)
    caller_emp_id = user_context.get("employee_id") or user_context.get("user_id")

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            # Build WHERE conditions
            conditions = ["1=1"]
            params: Dict[str, Any] = {}

            parsed_from = _parse_date(date_from)
            if parsed_from:
                conditions.append("p.created_at >= :df")
                params["df"] = parsed_from

            parsed_to = _parse_date(date_to)
            if parsed_to:
                # Add end-of-day time if date only
                if len(date_to.strip()) <= 10:
                    parsed_to = parsed_to.replace(hour=23, minute=59, second=59)
                conditions.append("p.created_at <= :dt")
                params["dt"] = parsed_to

            if employee_id and employee_id > 0:
                conditions.append("p.employee_id = :emp_id")
                params["emp_id"] = employee_id

            if search and search.strip():
                s_pat = f"%{search.strip().lower()}%"
                conditions.append("""(
                    LOWER(COALESCE(p.reference_id, '')) LIKE :search 
                    OR LOWER(COALESCE(p.document_type, '')) LIKE :search 
                    OR LOWER(COALESCE(p.model_name, '')) LIKE :search 
                    OR LOWER(COALESCE(p.processing_status, '')) LIKE :search 
                    OR LOWER(COALESCE(e.employee_name, CASE WHEN p.employee_id > 0 THEN CONCAT('Employee #', p.employee_id) ELSE 'System' END)) LIKE :search
                )""")
                params["search"] = s_pat

            where_clause = " AND ".join(conditions)

            # 1. KPI Aggregates
            kpi_sql = f"""
                SELECT 
                    COUNT(*) AS total_emails_parsed,
                    SUM(CASE WHEN p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'SUCCESS', 'COMPLETED') THEN 1 ELSE 0 END) AS total_tasks_created,
                    COALESCE(SUM(p.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(p.total_cost_usd), 0.0) AS total_cost_usd,
                    COALESCE(AVG(p.confidence_score), 0) AS avg_confidence_score
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause}
            """
            kpi_row = conn.execute(sql_text(kpi_sql), params).mappings().fetchone()
            kpis = dict(kpi_row) if kpi_row else {
                "total_emails_parsed": 0,
                "total_tasks_created": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_confidence_score": 0
            }
            kpis["total_cost_usd"] = float(kpis.get("total_cost_usd") or 0.0)
            kpis["avg_confidence_score"] = float(round(kpis.get("avg_confidence_score") or 0.0, 1))

            # 2. Top User Parsed & Top Task Creator
            top_user_sql = f"""
                SELECT p.employee_id, COALESCE(e.employee_name, CONCAT('Employee #', p.employee_id)) AS name, COUNT(*) AS count
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause} AND p.employee_id IS NOT NULL AND p.employee_id > 0
                GROUP BY p.employee_id, e.employee_name
                ORDER BY count DESC LIMIT 1
            """
            top_user_row = conn.execute(sql_text(top_user_sql), params).mappings().fetchone()

            top_creator_sql = f"""
                SELECT p.employee_id, COALESCE(e.employee_name, CONCAT('Employee #', p.employee_id)) AS name, COUNT(*) AS count
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause} AND p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'SUCCESS', 'COMPLETED') AND p.employee_id IS NOT NULL AND p.employee_id > 0
                GROUP BY p.employee_id, e.employee_name
                ORDER BY count DESC LIMIT 1
            """
            top_creator_row = conn.execute(sql_text(top_creator_sql), params).mappings().fetchone()

            kpis["top_user_parsed"] = dict(top_user_row) if top_user_row else {"employee_id": 0, "name": "N/A", "count": 0}
            kpis["top_task_creator"] = dict(top_creator_row) if top_creator_row else {"employee_id": 0, "name": "N/A", "count": 0}

            # 3. Document Type Breakdown
            doc_type_sql = f"""
                SELECT 
                    p.document_type,
                    COUNT(*) AS count,
                    COALESCE(SUM(p.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(p.total_cost_usd), 0.0) AS total_cost_usd
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause}
                GROUP BY p.document_type
                ORDER BY count DESC
            """
            doc_rows = conn.execute(sql_text(doc_type_sql), params).mappings().all()
            doc_breakdown = []
            for r in doc_rows:
                d_item = dict(r)
                d_item["total_cost_usd"] = float(d_item.get("total_cost_usd") or 0.0)
                doc_breakdown.append(d_item)

            # 4. User Leaderboard
            leaderboard_sql = f"""
                SELECT 
                    p.employee_id,
                    COALESCE(e.employee_name, CASE WHEN p.employee_id > 0 THEN CONCAT('Employee #', p.employee_id) ELSE 'System/Automation' END) AS employee_name,
                    COUNT(*) AS emails_parsed,
                    SUM(CASE WHEN p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'SUCCESS', 'COMPLETED') THEN 1 ELSE 0 END) AS tasks_created,
                    COALESCE(SUM(p.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(p.total_cost_usd), 0.0) AS total_cost_usd
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause}
                GROUP BY p.employee_id, e.employee_name
                ORDER BY emails_parsed DESC
            """
            lb_rows = conn.execute(sql_text(leaderboard_sql), params).mappings().all()
            leaderboard = []
            for r in lb_rows:
                lb_item = dict(r)
                lb_item["total_cost_usd"] = float(lb_item.get("total_cost_usd") or 0.0)
                leaderboard.append(lb_item)

            # 5. Paginated Telemetry Logs
            offset = (page - 1) * limit
            logs_count_sql = f"SELECT COUNT(*) FROM ai_email_parsing p LEFT JOIN employees e ON p.employee_id = e.id WHERE {where_clause}"
            total_logs_count = conn.execute(sql_text(logs_count_sql), params).scalar() or 0

            logs_sql = f"""
                SELECT 
                    p.id,
                    p.employee_id,
                    COALESCE(e.employee_name, CASE WHEN p.employee_id > 0 THEN CONCAT('Employee #', p.employee_id) ELSE 'System' END) AS employee_name,
                    p.document_type,
                    p.reference_id,
                    p.model_name,
                    p.has_attachment,
                    p.file_extension,
                    p.input_tokens,
                    p.output_tokens,
                    p.total_tokens,
                    p.total_cost_usd,
                    p.confidence_score,
                    p.confidence_level,
                    p.processing_status,
                    p.processing_time_ms,
                    p.created_at
                FROM ai_email_parsing p
                LEFT JOIN employees e ON p.employee_id = e.id
                WHERE {where_clause}
                ORDER BY p.id DESC
                LIMIT :limit OFFSET :offset
            """
            params_logs = {**params, "limit": limit, "offset": offset}
            log_rows = conn.execute(sql_text(logs_sql), params_logs).mappings().all()
            logs = []
            for r in log_rows:
                l_item = dict(r)
                if l_item.get("created_at") and hasattr(l_item["created_at"], "isoformat"):
                    l_item["created_at"] = l_item["created_at"].isoformat()
                l_item["total_cost_usd"] = float(l_item.get("total_cost_usd") or 0.0)
                logs.append(l_item)

            return {
                "status": "success",
                "kpis": kpis,
                "document_breakdown": doc_breakdown,
                "leaderboard": leaderboard,
                "pagination": {
                    "total": total_logs_count,
                    "page": page,
                    "limit": limit,
                    "total_pages": (total_logs_count + limit - 1) // limit if limit > 0 else 1
                },
                "logs": logs
            }
    except Exception as e:
        logger.error(f"[AIReports] Failed to generate AI Email Usage Report: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate AI Email Usage Report: {str(e)}")


@router.get("/ai-chatbot-usage")
async def get_ai_chatbot_usage_report(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    employee_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    # Boundary normalization: Map incoming external start_date/end_date to internal date_from/date_to
    date_from = start_date or date_from
    date_to = end_date or date_to
    """
    Returns aggregated KPIs, User Leaderboard, Execution Path & Model Breakdown,
    and paginated telemetry logs from `ai_chatbot_usage` joined with `employees`.
    """
    user_context = _decode_jwt(credentials)
    caller_emp_id = user_context.get("employee_id") or user_context.get("user_id")

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            conditions = ["1=1"]
            params: Dict[str, Any] = {}

            parsed_from = _parse_date(date_from)
            if parsed_from:
                conditions.append("c.created_at >= :df")
                params["df"] = parsed_from

            parsed_to = _parse_date(date_to)
            if parsed_to:
                if len(date_to.strip()) <= 10:
                    parsed_to = parsed_to.replace(hour=23, minute=59, second=59)
                conditions.append("c.created_at <= :dt")
                params["dt"] = parsed_to

            if employee_id and employee_id > 0:
                conditions.append("c.employee_id = :emp_id")
                params["emp_id"] = employee_id

            if search and search.strip():
                s_pat = f"%{search.strip().lower()}%"
                conditions.append("""(
                    LOWER(COALESCE(c.session_id, '')) LIKE :search 
                    OR LOWER(COALESCE(c.model_name, '')) LIKE :search 
                    OR LOWER(COALESCE(c.execution_path, '')) LIKE :search 
                    OR LOWER(COALESCE(c.capability_id, '')) LIKE :search 
                    OR LOWER(COALESCE(c.operation, '')) LIKE :search 
                    OR LOWER(COALESCE(c.status, '')) LIKE :search 
                    OR LOWER(COALESCE(e.employee_name, CASE WHEN c.employee_id > 0 THEN CONCAT('Employee #', c.employee_id) ELSE 'System' END)) LIKE :search
                )""")
                params["search"] = s_pat

            where_clause = " AND ".join(conditions)

            # 1. KPI Aggregates
            kpi_sql = f"""
                SELECT 
                    COUNT(*) AS total_queries,
                    COUNT(DISTINCT c.session_id) AS total_sessions,
                    COALESCE(SUM(c.input_tokens), 0) AS total_input_tokens,
                    COALESCE(SUM(c.output_tokens), 0) AS total_output_tokens,
                    COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(c.total_cost_usd), 0.0) AS total_cost_usd,
                    COALESCE(AVG(c.backend_execution_ms), 0) AS avg_execution_ms
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause}
            """
            kpi_row = conn.execute(sql_text(kpi_sql), params).mappings().fetchone()
            kpis = dict(kpi_row) if kpi_row else {
                "total_queries": 0,
                "total_sessions": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_execution_ms": 0
            }
            kpis["total_cost_usd"] = float(kpis.get("total_cost_usd") or 0.0)
            kpis["avg_execution_ms"] = int(round(kpis.get("avg_execution_ms") or 0))

            # 2. Top Spender User & Top Token User
            top_spender_sql = f"""
                SELECT c.employee_id, COALESCE(e.employee_name, CONCAT('Employee #', c.employee_id)) AS name, SUM(c.total_cost_usd) AS cost
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause} AND c.employee_id IS NOT NULL AND c.employee_id > 0
                GROUP BY c.employee_id, e.employee_name
                ORDER BY cost DESC LIMIT 1
            """
            top_spender_row = conn.execute(sql_text(top_spender_sql), params).mappings().fetchone()

            top_tokens_sql = f"""
                SELECT c.employee_id, COALESCE(e.employee_name, CONCAT('Employee #', c.employee_id)) AS name, SUM(c.total_tokens) AS tokens
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause} AND c.employee_id IS NOT NULL AND c.employee_id > 0
                GROUP BY c.employee_id, e.employee_name
                ORDER BY tokens DESC LIMIT 1
            """
            top_tokens_row = conn.execute(sql_text(top_tokens_sql), params).mappings().fetchone()

            top_model_sql = f"""
                SELECT c.model_name AS name, COUNT(*) AS count
                FROM ai_chatbot_usage c
                WHERE {where_clause} AND c.model_name IS NOT NULL
                GROUP BY c.model_name
                ORDER BY count DESC LIMIT 1
            """
            top_model_row = conn.execute(sql_text(top_model_sql), params).mappings().fetchone()

            kpis["top_spending_user"] = {
                "employee_id": top_spender_row["employee_id"] if top_spender_row else 0,
                "name": top_spender_row["name"] if top_spender_row else "N/A",
                "cost": float(top_spender_row["cost"]) if top_spender_row and top_spender_row["cost"] else 0.0
            }
            kpis["top_token_user"] = {
                "employee_id": top_tokens_row["employee_id"] if top_tokens_row else 0,
                "name": top_tokens_row["name"] if top_tokens_row else "N/A",
                "tokens": int(top_tokens_row["tokens"]) if top_tokens_row and top_tokens_row["tokens"] else 0
            }
            kpis["most_used_model"] = top_model_row["name"] if top_model_row and top_model_row["name"] else "qwen/qwen3.6-27b"

            # 3. Model Breakdown
            model_sql = f"""
                SELECT 
                    COALESCE(c.model_name, 'unknown') AS model_name,
                    COUNT(*) AS count,
                    COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(c.total_cost_usd), 0.0) AS total_cost_usd
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause}
                GROUP BY c.model_name
                ORDER BY count DESC
            """
            model_rows = conn.execute(sql_text(model_sql), params).mappings().all()
            model_breakdown = []
            for r in model_rows:
                m_item = dict(r)
                m_item["total_cost_usd"] = float(m_item.get("total_cost_usd") or 0.0)
                model_breakdown.append(m_item)

            # 4. Execution Path Breakdown (fast_path vs llm_stream)
            path_sql = f"""
                SELECT 
                    COALESCE(c.execution_path, 'fast_path') AS execution_path,
                    COUNT(*) AS count,
                    COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(c.total_cost_usd), 0.0) AS total_cost_usd
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause}
                GROUP BY c.execution_path
                ORDER BY count DESC
            """
            path_rows = conn.execute(sql_text(path_sql), params).mappings().all()
            execution_path_breakdown = []
            for r in path_rows:
                p_item = dict(r)
                p_item["total_cost_usd"] = float(p_item.get("total_cost_usd") or 0.0)
                execution_path_breakdown.append(p_item)

            # 5. User Leaderboard
            leaderboard_sql = f"""
                SELECT 
                    c.employee_id,
                    COALESCE(e.employee_name, CASE WHEN c.employee_id > 0 THEN CONCAT('Employee #', c.employee_id) ELSE 'System' END) AS employee_name,
                    COUNT(*) AS total_queries,
                    COUNT(DISTINCT c.session_id) AS total_sessions,
                    COALESCE(SUM(c.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(c.output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(c.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(c.total_cost_usd), 0.0) AS total_cost_usd
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause}
                GROUP BY c.employee_id, e.employee_name
                ORDER BY total_cost_usd DESC, total_tokens DESC
            """
            lb_rows = conn.execute(sql_text(leaderboard_sql), params).mappings().all()
            leaderboard = []
            for r in lb_rows:
                lb_item = dict(r)
                lb_item["total_cost_usd"] = float(lb_item.get("total_cost_usd") or 0.0)
                leaderboard.append(lb_item)

            # 6. Paginated Telemetry Logs
            offset = (page - 1) * limit
            logs_count_sql = f"SELECT COUNT(*) FROM ai_chatbot_usage c LEFT JOIN employees e ON c.employee_id = e.id WHERE {where_clause}"
            total_logs_count = conn.execute(sql_text(logs_count_sql), params).scalar() or 0

            logs_sql = f"""
                SELECT 
                    c.id,
                    c.employee_id,
                    COALESCE(e.employee_name, CASE WHEN c.employee_id > 0 THEN CONCAT('Employee #', c.employee_id) ELSE 'System' END) AS employee_name,
                    c.session_id,
                    c.model_name,
                    c.input_tokens,
                    c.output_tokens,
                    c.total_tokens,
                    c.total_cost_usd,
                    c.status,
                    c.execution_path,
                    c.capability_id,
                    c.operation,
                    c.backend_execution_ms,
                    c.created_at
                FROM ai_chatbot_usage c
                LEFT JOIN employees e ON c.employee_id = e.id
                WHERE {where_clause}
                ORDER BY c.id DESC
                LIMIT :limit OFFSET :offset
            """
            params_logs = {**params, "limit": limit, "offset": offset}
            log_rows = conn.execute(sql_text(logs_sql), params_logs).mappings().all()
            logs = []
            for r in log_rows:
                l_item = dict(r)
                if l_item.get("created_at") and hasattr(l_item["created_at"], "isoformat"):
                    l_item["created_at"] = l_item["created_at"].isoformat()
                l_item["total_cost_usd"] = float(l_item.get("total_cost_usd") or 0.0)
                logs.append(l_item)

            return {
                "status": "success",
                "kpis": kpis,
                "model_breakdown": model_breakdown,
                "execution_path_breakdown": execution_path_breakdown,
                "leaderboard": leaderboard,
                "pagination": {
                    "total": total_logs_count,
                    "page": page,
                    "limit": limit,
                    "total_pages": (total_logs_count + limit - 1) // limit if limit > 0 else 1
                },
                "logs": logs
            }
    except Exception as e:
        logger.error(f"[AIReports] Failed to generate AI Chatbot Usage Report: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate AI Chatbot Usage Report: {str(e)}")
