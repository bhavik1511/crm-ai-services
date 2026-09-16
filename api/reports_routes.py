"""
reports_routes.py — FastAPI router for AI Usage Analytics Reports.
Provides endpoints for AI Email Parsing Usage and AI Chatbot Usage metrics.
"""

import logging
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, date

from fastapi import APIRouter, HTTPException, Depends, Query, Request
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


def _build_base_report_filter(
    request: Request,
    table_alias: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_id: Optional[int] = None,
    search: Optional[str] = None,
    search_columns: Optional[List[str]] = None,
) -> Tuple[List[str], Dict[str, Any], Dict[str, str]]:
    """
    Centralized extractor for date ranges, employee scoping, searchQuery JSON,
    and global search conditions across report endpoints.
    """
    conditions = ["1=1"]
    params: Dict[str, Any] = {}
    query_params = dict(request.query_params)

    # 1. Date Range Handling
    df_val = date_from or start_date or query_params.get("start_date") or query_params.get("dateFrom") or query_params.get("date_from")
    dt_val = date_to or end_date or query_params.get("end_date") or query_params.get("dateTo") or query_params.get("date_to")
    parsed_from = _parse_date(df_val)
    if parsed_from:
        conditions.append(f"{table_alias}.created_at >= :df")
        params["df"] = parsed_from

    parsed_to = _parse_date(dt_val)
    if parsed_to:
        if len(str(dt_val).strip()) <= 10:
            parsed_to = parsed_to.replace(hour=23, minute=59, second=59)
        conditions.append(f"{table_alias}.created_at <= :dt")
        params["dt"] = parsed_to

    # 2. Employee Scope
    emp_id_val = employee_id or query_params.get("employee_id") or query_params.get("employeeId")
    if emp_id_val:
        try:
            emp_int = int(emp_id_val)
            if emp_int > 0:
                conditions.append(f"{table_alias}.employee_id = :emp_id")
                params["emp_id"] = emp_int
        except Exception:
            pass

    # 3. Frontend SearchQuery JSON & Query Params
    field_filters: Dict[str, str] = {}
    sq_raw = query_params.get("searchQuery")
    if sq_raw and str(sq_raw).strip().startswith("{"):
        try:
            import json
            sq_json = json.loads(sq_raw)
            if isinstance(sq_json, dict):
                for k, v in sq_json.items():
                    if v and str(v).strip():
                        field_filters[k] = str(v).strip()
        except Exception:
            pass

    for k, v in query_params.items():
        if v and str(v).strip() and k not in field_filters:
            field_filters[k] = str(v).strip()

    # 4. Common Employee Name Filter
    if "employee_name" in field_filters:
        val = field_filters["employee_name"]
        conditions.append(f"LOWER(COALESCE(e.employee_name, CASE WHEN {table_alias}.employee_id > 0 THEN CONCAT('Employee #', {table_alias}.employee_id) ELSE 'System' END)) LIKE :flt_emp_name")
        params["flt_emp_name"] = f"%{val.lower()}%"

    # 5. Global Search
    if search and str(search).strip():
        s_pat = f"%{str(search).strip().lower()}%"
        search_clauses = [f"LOWER(COALESCE({col}, '')) LIKE :g_search" for col in (search_columns or [])]
        search_clauses.append(f"LOWER(COALESCE(e.employee_name, CASE WHEN {table_alias}.employee_id > 0 THEN CONCAT('Employee #', {table_alias}.employee_id) ELSE 'System' END)) LIKE :g_search")
        conditions.append(f"({' OR '.join(search_clauses)})")
        params["g_search"] = s_pat

    return conditions, params, field_filters


@router.get("/ai-email-usage")
async def get_ai_email_usage_report(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    employee_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
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
            conditions, params, field_filters = _build_base_report_filter(
                request=request,
                table_alias="p",
                date_from=date_from,
                date_to=date_to,
                start_date=start_date,
                end_date=end_date,
                employee_id=employee_id,
                search=search,
                search_columns=["p.document_type", "p.processing_status"],
            )

            if "document_type" in field_filters:
                val = field_filters["document_type"]
                conditions.append("LOWER(COALESCE(p.document_type, '')) LIKE :flt_doc_type")
                params["flt_doc_type"] = f"%{val.lower()}%"

            if "processing_status" in field_filters or "status" in field_filters:
                val = field_filters.get("processing_status") or field_filters.get("status")
                conditions.append("LOWER(COALESCE(p.processing_status, '')) LIKE :flt_status")
                params["flt_status"] = f"%{val.lower()}%"

            where_clause = " AND ".join(conditions)

            # 1. KPI Aggregates
            kpi_sql = f"""
                SELECT 
                    COUNT(*) AS total_emails_parsed,
                    SUM(CASE WHEN p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'COMPLETED') THEN 1 ELSE 0 END) AS total_tasks_created,
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
                WHERE {where_clause} AND p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'COMPLETED') AND p.employee_id IS NOT NULL AND p.employee_id > 0
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
                    SUM(CASE WHEN p.document_type = 'email_task' AND COALESCE(p.processing_status, '') IN ('CONVERTED', 'COMPLETED') THEN 1 ELSE 0 END) AS tasks_created,
                    COALESCE(SUM(p.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(p.output_tokens), 0) AS output_tokens,
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
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    employee_id: Optional[int] = Query(None),
    metric: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    """
    Returns aggregated KPIs, User Leaderboard, Execution Path & Model Breakdown,
    and paginated telemetry logs from `ai_chatbot_usage` joined with `employees`.
    """
    user_context = _decode_jwt(credentials)
    caller_emp_id = user_context.get("employee_id") or user_context.get("user_id")

    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            # Build WHERE conditions
            conditions, params, field_filters = _build_base_report_filter(
                request=request,
                table_alias="c",
                date_from=date_from,
                date_to=date_to,
                start_date=start_date,
                end_date=end_date,
                employee_id=employee_id,
                search=search,
                search_columns=[
                    "c.session_id",
                    "c.model_name",
                    "c.execution_path",
                    "c.capability_id",
                    "c.operation",
                    "c.status",
                ],
            )

            if "session_id" in field_filters:
                val = field_filters["session_id"]
                conditions.append("LOWER(COALESCE(c.session_id, '')) LIKE :flt_sess")
                params["flt_sess"] = f"%{val.lower()}%"

            if "model_name" in field_filters:
                val = field_filters["model_name"]
                conditions.append("LOWER(COALESCE(c.model_name, '')) LIKE :flt_model")
                params["flt_model"] = f"%{val.lower()}%"

            if "execution_path" in field_filters:
                val = field_filters["execution_path"]
                conditions.append("LOWER(COALESCE(c.execution_path, '')) LIKE :flt_path")
                params["flt_path"] = f"%{val.lower()}%"

            if "capability_id" in field_filters:
                val = field_filters["capability_id"]
                if val.lower() not in ("ai_chatbot_usage", "ai-chatbot-usage-report", "ai_email_usage", "ai-email-usage-report"):
                    conditions.append("LOWER(COALESCE(c.capability_id, '')) LIKE :flt_cap")
                    params["flt_cap"] = f"%{val.lower()}%"

            if "operation" in field_filters:
                val = field_filters["operation"]
                if val.lower() not in ("ranking", "summary", "report", "group_by", "filter", "comparison", "analytics"):
                    conditions.append("LOWER(COALESCE(c.operation, '')) LIKE :flt_op")
                    params["flt_op"] = f"%{val.lower()}%"

            if "status" in field_filters:
                val = field_filters["status"]
                conditions.append("LOWER(COALESCE(c.status, '')) LIKE :flt_status")
                params["flt_status"] = f"%{val.lower()}%"

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
                LEFT JOIN employees e ON c.employee_id = e.id
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

            # 5. User Leaderboard (Ordered by requested metric, defaulting to total_tokens DESC)
            req_metric = str(metric or field_filters.get("metric") or field_filters.get("sort_by") or request.query_params.get("metric") or request.query_params.get("sort_by") or "").lower().strip()
            if req_metric in ("total_queries", "queries", "query_count", "count"):
                order_clause = "total_queries DESC, total_tokens DESC"
                sort_field = "total_queries"
            elif req_metric in ("total_cost", "total_cost_usd", "cost", "amount", "revenue"):
                order_clause = "total_cost_usd DESC, total_tokens DESC"
                sort_field = "total_cost_usd"
            else:
                order_clause = "total_tokens DESC, total_queries DESC"
                sort_field = "total_tokens"

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
                ORDER BY {order_clause}
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
                "metric": sort_field,
                "ranking_field": sort_field,
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
