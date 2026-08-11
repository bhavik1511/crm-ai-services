"""
Chat Routes — FastAPI router for the AI chat system.
All endpoints require valid JWT in the Authorization header.
Extracts user_id, role, and other context from the JWT payload.
"""

import os
import time
import logging
from datetime import datetime
from typing import Optional
import json

import jwt
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
from sqlalchemy import text as sql_text

from memory import session_manager
from memory import memory_manager
from memory import chat_history
from memory.serializer import build_clarification_dto, safe_json_dumps
from db.database import get_db_engine
from agent.planner import EnterprisePlanner, RequestContext

load_dotenv(override=True)

USE_ENTERPRISE_PLANNER = os.getenv("USE_ENTERPRISE_PLANNER", "True").lower() in ("true", "1", "yes")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT Configuration
# ---------------------------------------------------------------------------
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", os.getenv("JWT_SECRET", ""))
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

security = HTTPBearer()

router = APIRouter(prefix="/api/ai", tags=["AI Chat"])


# ---------------------------------------------------------------------------
# JWT helper — extracts and validates user context from token
# ---------------------------------------------------------------------------
def _decode_jwt(credentials: HTTPAuthorizationCredentials) -> dict:
    """
    Decode JWT and return user context dict.
    Resolves designation and department from the database since the CRM JWT
    doesn't include these as string names.
    Raises HTTPException(401) on invalid/expired token.
    """
    token = credentials.credentials
    print(f"\n[DEBUG JWT] Token received (first 60 chars): {token[:60]}...")
    try:
        payload = jwt.decode(
            token, 
            JWT_SECRET_KEY, 
            algorithms=[JWT_ALGORITHM],
            options={
                "verify_aud": False, 
                "verify_iss": False, 
                "verify_sub": False
            }
        )
        print(f"[DEBUG JWT] Payload keys: {list(payload.keys())}")
        print(f"[DEBUG JWT] Full payload: {payload}")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    def _to_int(value):
        """Best-effort int parser for JWT claims that may arrive as strings."""
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            v = value.strip()
            if v.isdigit():
                try:
                    return int(v)
                except Exception:
                    return None
        return None

    # Extract base IDs from JWT
    user_id = _to_int(payload.get("id")) or _to_int(payload.get("user_id")) or 0

    employee_id = (
        _to_int(payload.get("employee_id"))
        or _to_int(payload.get("emp_id"))
        or _to_int(payload.get("employeeId"))
    )

    # Employee-login tokens may only carry `id` for the employee row.
    if not employee_id and payload.get("isEmployeeLogin") is True:
        employee_id = _to_int(payload.get("id"))

    user_name = payload.get("name", payload.get("user_name", "Unknown"))

    # Resolve designation name and department name from the database
    # because the CRM JWT does NOT contain these as string fields.
    role_name = payload.get("role", payload.get("designation", "Unknown"))
    department = payload.get("department", "Unknown")
    
    # Resolve from employees table ONLY when we have a reliable employee_id.
    # Falling back to auth user_id can map to another person and produce wrong RBAC role.
    resolved_emp_id = employee_id if employee_id and employee_id > 0 else None

    # Fallback: derive employee_id from token identity fields when employee_id is absent.
    if not resolved_emp_id:
        try:
            from db.database import get_db_engine
            from sqlalchemy import text as sql_text
            engine = get_db_engine()
            with engine.connect() as conn:
                token_code = payload.get("code")
                token_email = payload.get("email") or payload.get("emp_comm_business_email")

                fallback_row = None
                if token_code:
                    fallback_row = conn.execute(sql_text(
                        "SELECT id FROM employees WHERE code = :code LIMIT 1"
                    ), {"code": token_code}).fetchone()

                if (not fallback_row) and token_email:
                    fallback_row = conn.execute(sql_text(
                        "SELECT id FROM employees WHERE emp_comm_business_email = :email LIMIT 1"
                    ), {"email": token_email}).fetchone()

                if fallback_row and fallback_row[0]:
                    resolved_emp_id = int(fallback_row[0])
        except Exception as e:
            logger.warning(f"[JWT] Failed to resolve fallback employee identity: {e}")

    if resolved_emp_id:
        try:
            from db.database import get_db_engine
            from sqlalchemy import text as sql_text
            engine = get_db_engine()
            with engine.connect() as conn:
                # Resolve designation and department in one query from the same employee row.
                row = conn.execute(sql_text(
                    "SELECT des.name AS designation_name, dep.name AS department_name "
                    "FROM employees e "
                    "LEFT JOIN m_designation des ON e.emp_designation_id = des.id "
                    "LEFT JOIN m_department dep ON e.emp_department_id = dep.id "
                    "WHERE e.id = :emp_id"
                ), {"emp_id": resolved_emp_id}).fetchone()
                if row:
                    if row[0]:
                        role_name = row[0]
                    if row[1]:
                        department = row[1]
        except Exception as e:
            logger.warning(f"[JWT] Failed to resolve user context from DB: {e}")

    # Some token variants carry designation/department IDs instead of names.
    # Resolve those IDs only if still unknown.
    if (not role_name or str(role_name).strip().lower() == "unknown") and payload.get("designation_id"):
        try:
            from db.database import get_db_engine
            from sqlalchemy import text as sql_text
            engine = get_db_engine()
            with engine.connect() as conn:
                des_row = conn.execute(sql_text(
                    "SELECT name FROM m_designation WHERE id = :designation_id"
                ), {"designation_id": payload.get("designation_id")}).fetchone()
                if des_row and des_row[0]:
                    role_name = des_row[0]
        except Exception as e:
            logger.warning(f"[JWT] Failed to resolve designation_id: {e}")

    if (not department or str(department).strip().lower() == "unknown") and payload.get("department_id"):
        try:
            from db.database import get_db_engine
            from sqlalchemy import text as sql_text
            engine = get_db_engine()
            with engine.connect() as conn:
                dep_row = conn.execute(sql_text(
                    "SELECT name FROM m_department WHERE id = :department_id"
                ), {"department_id": payload.get("department_id")}).fetchone()
                if dep_row and dep_row[0]:
                    department = dep_row[0]
        except Exception as e:
            logger.warning(f"[JWT] Failed to resolve department_id: {e}")

    # Compute hierarchy level from the resolved role name
    from config.role_tier_config import get_tier_for_role
    hierarchy_level = get_tier_for_role(role_name)
    
    print(f"[DEBUG JWT RESOLVED]")
    print(f"  user_id: {user_id}")
    print(f"  employee_id: {resolved_emp_id or employee_id or user_id}")
    print(f"  role_name: {role_name}")
    print(f"  hierarchy_level (TIER): {hierarchy_level}")
    print(f"  department: {department}")
    print(f"[END DEBUG JWT RESOLVED]\n")

    user_context = {
        "user_id": user_id,
        "employee_id": resolved_emp_id or employee_id or user_id,
        "role": role_name,
        "role_name": role_name,
        "hierarchy_level": hierarchy_level,
        "department_id": payload.get("department_id"),
        "service_line_id": payload.get("service_line_id"),
        "user_name": user_name,
        "department": department,
    }
    logger.info(f"[JWT] Resolved: {user_name} | Role: {role_name} | Tier: {hierarchy_level} | Dept: {department}")
    return user_context


# ---------------------------------------------------------------------------
# Fiscal year helper
# ---------------------------------------------------------------------------
def _current_fiscal_year() -> str:
    """Returns fiscal year string like 'FY2025-2026'."""
    now = datetime.utcnow()
    if now.month >= 10:
        return f"FY{now.year}-{now.year + 1}"
    else:
        return f"FY{now.year - 1}-{now.year}"


# ---------------------------------------------------------------------------
# Receivable filter wizard detection
# ---------------------------------------------------------------------------
def _is_broad_receivable_query(question: str) -> bool:
    """
    Returns True when the user's question is a broad receivable/ageing query
    that should trigger the interactive filter wizard in the frontend.

    Returns False for:
    - 'Generate Receivable Report ...' prompts from the wizard itself (those have filters)
    - Simple total-receivables questions (just fetch data, no wizard needed)
    """
    q = question.strip().lower()

    # Wizard-generated prompts already have filters — don't intercept
    if q.startswith("generate receivable report"):
        return False

    # Simple totals queries — just answer them directly
    TOTALS_KW = ["total receivables", "totalreceivables", "what is total receivable",
                 "how much is receivable", "receivables amount"]
    if any(kw in q for kw in TOTALS_KW):
        return False

    # Broad report / filter intent keywords
    BROAD_KW = [
        "receivable report", "receivables report",
        "ageing report", "aging report",
        "show receivable", "show receivables",
        "receivable overview", "receivables overview",
        "receivable summary", "receivables summary",
        "outstanding report", "show outstanding",
        "overdue report",
        "generate report",
    ]
    if any(kw in q for kw in BROAD_KW):
        return True

    # "receivable" or "receivables" alone (without total/amount context)
    if ("receivable" in q or "receivables" in q) and any(
        kw in q for kw in ["report", "overview", "filter", "summary", "ageing", "aging", "details"]
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    question: str
    session_id: str
    is_internal: Optional[bool] = False
    context: Optional[dict] = None


class SessionResponse(BaseModel):
    session_id: str


class DeleteResponse(BaseModel):
    deleted: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat(
    request: ChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Primary AI chat endpoint.
    Validates session, resolves answer via 3-tier cache, logs history.
    """
    from memory.conversation_memory import memory
    from utils.structured_logger import set_trace_id, log_stage, mask_jwt
    
    set_trace_id()
    user_context = _decode_jwt(credentials)
    user_id = user_context["user_id"]
    question = request.question.strip()
    session_id = request.session_id

    context_prompt = memory.get_context_prompt(session_id)

    log_stage(
        logger, "REQUEST",
        SessionId=session_id,
        UserId=user_id,
        Role=user_context.get("role", "Unknown"),
        Query=question[:80],
        Auth=mask_jwt(credentials.credentials)
    )

    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    # Validate session
    session = await session_manager.get_session(session_id, user_id=user_id, user_context=user_context)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired")

    # Security guard: prevent cross-user session reuse and stale identity leakage.
    session_user_id = session.get("user_id")
    if session_user_id != user_id:
        logger.warning(
            f"[ChatRoute] Session ownership mismatch: session_user_id={session_user_id}, token_user_id={user_id}, session_id={session_id}"
        )
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")

    # Get conversation history from session messages
    session_messages = session.get("messages", [])

    # If session identity metadata differs (e.g., user switched accounts/roles),
    # ignore stale history to stop role contamination in prompt context.
    current_role = (user_context.get("role") or "").strip().lower()
    session_role = (session.get("role") or "").strip().lower()
    current_emp = user_context.get("employee_id")
    session_emp = session.get("employee_id")
    if (session_role and current_role and session_role != current_role) or (
        session_emp and current_emp and session_emp != current_emp
    ):
        logger.warning(
            f"[ChatRoute] Session identity drift detected; dropping history. session_id={session_id}, session_role={session_role}, current_role={current_role}, session_emp={session_emp}, current_emp={current_emp}"
        )
        session_messages = []

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in session_messages
    ]
    if context_prompt:
        history.insert(0, {"role": "system", "content": context_prompt})
        
    # Append current question to history for the agent
    history.append({"role": "user", "content": question})

    # Store auth token globally so semantic_layer tools can use it to call the CRM API
    from semantic import semantic_layer
    semantic_layer._CRM_AUTH_TOKEN = credentials.credentials or ''

    # Set user context for server-side RBAC enforcement in semantic layer tools
    from config.role_tier_config import get_tier_for_role
    resolved_tier = get_tier_for_role(user_context.get('role', 'Unknown'))
    semantic_layer.set_user_context({
        'employee_id': user_context.get('employee_id', 0) or 0,
        'user_tier': resolved_tier,
        'role_name': user_context.get('role', 'Unknown'),
        'department_id': user_context.get('department_id'),
        'jwt_token': credentials.credentials,
    })

    # Merge UI context (like active date picker) into the user_context
    if request.context:
        user_context.update(request.context)

    # NEW: Extract report filters from the query text and merge into user_context globally
    from main import _extract_kpi_filters_from_text
    _text_filters = _extract_kpi_filters_from_text(question)
    for _k, _v in _text_filters.items():
        if _v and str(_v).lower() != "all" and _k not in user_context:
            user_context[_k] = _v


    # Resolve answer via deterministic fast-path or 3-tier cache
    try:
        if USE_ENTERPRISE_PLANNER:
            fast = None
        else:
            from main import deterministic_dashboard_response
            fast = await deterministic_dashboard_response(history, question, user_context, credentials.credentials)
            
        if fast:
            result = fast
            result["was_cached"] = False
            result["cache_tier"] = "fresh"
            result["latency_ms"] = 0
            logger.info(f"[ChatRoute] Matched deterministic route for: {question[:60]}...")
        else:
            if USE_ENTERPRISE_PLANNER:
                from agent.executive_classifier import handle_executive_classification
                _is_conv, _conv_reply = await handle_executive_classification(question, history, user_context)
                if _is_conv and _conv_reply:
                    logger.info(f"[ExecutiveClassifier] Handled conversational query '{question[:40]}' via short-circuit (0 Planner/DB cost)")
                    return {
                        "type": "done",
                        "answer": _conv_reply,
                        "content": _conv_reply,
                        "was_cached": False,
                        "cache_tier": "conversational_short_circuit"
                    }

                # ── Clarification State Injection (zero LLM cost) ──────────────────
                # Load any pending clarification from the session store and inject it
                # into user_context so the Planner can resume without re-planning.
                from memory.session_manager import (
                    get_clarification_state, save_clarification_state, clear_clarification_state
                )
                # [DIAG-1] session_id + conversation metadata
                logger.info(f"[DIAG-1] /chat/query | session_id={session_id} | user_id={user_id} | question='{question[:60]}'")

                # [DIAG-3] Load clarification state
                _clar_state = await get_clarification_state(session_id)

                # [DIAG-4] Log loaded state
                logger.info(
                    f"[DIAG-4] /chat/query | clarification_state loaded | "
                    f"has_state={bool(_clar_state)} | "
                    f"missing_fields={_clar_state.get('missing_fields') if _clar_state else None}"
                )

                clar_history = history
                if _clar_state:
                    user_context["previous_execution_plan"] = _clar_state.get("execution_plan")
                    logger.info(f"[ChatRoute] Injected clarification state for session={session_id}, missing={_clar_state.get('missing_fields')}")
                    # Clarification Fast-Path: Omit full history to eliminate token overhead on clarification follow-ups
                    clar_history = []

                # [DIAG-5/6] Log user_context before RequestContext creation
                logger.info(
                    f"[DIAG-5] /chat/query | user_context keys={list(user_context.keys())} | "
                    f"has_previous_execution_plan={'previous_execution_plan' in user_context} | "
                    f"previous_plan_caps={[c.get('id') for c in user_context.get('previous_execution_plan', {}).get('business_capabilities', [])] if user_context.get('previous_execution_plan') else None}"
                )

                req_ctx = RequestContext(
                    question=question,
                    jwt_token=credentials.credentials,
                    session_id=session_id,
                    history=clar_history,
                    user_context=user_context,
                    request_metadata={"is_internal": request.is_internal},
                    feature_flags={"is_stream": False}
                )

                # --- Phase 3.2.3: Enterprise Hybrid Retrieval Engine Hook ---
                hybrid_result = None
                try:
                    from engine.hybrid_engine import get_hybrid_engine
                    hybrid_engine = get_hybrid_engine()
                    hybrid_result = await hybrid_engine.process_turn(
                        question=question,
                        jwt_token=credentials.credentials,
                        session_id=session_id,
                        user_context=user_context
                    )
                except Exception as h_err:
                    logger.warning(f"[ChatRoute] HybridEngine check failed (non-fatal, falling back to Planner): {h_err}")

                if hybrid_result:
                    result = hybrid_result
                else:
                    planner = EnterprisePlanner()
                    result = await planner.execute_turn(req_ctx)
                result["answer"] = result.get("content", "")

                # ── Clarification State Persistence ───────────────────────────────
                _is_clar = result.get("is_clarification", False)
                _plan_in_result = result.get("execution_plan")
                _missing_in_result = (_plan_in_result or {}).get("missing_information", []) if _plan_in_result else []

                if _is_clar and _plan_in_result and _missing_in_result:
                    # Planner needs more info — persist lightweight snapshot DTO so next turn can resume
                    _clar_dto = build_clarification_dto(
                        session_id=session_id,
                        original_question=question,
                        execution_plan=_plan_in_result,
                        missing_fields=_missing_in_result,
                        resolved_entities=_plan_in_result.get("resolved_entities", []),
                        planner_context=user_context
                    )
                    await save_clarification_state(session_id, _clar_dto)
                else:
                    # Execution succeeded — clear any stale clarification state
                    await clear_clarification_state(session_id)
            else:
                result = await memory_manager.resolve_answer(
                    question=question,
                    session_id=session_id,
                    user_context=user_context,
                    history=history,
                )
        memory.update_context(
            session_id=session_id,
            question=question,
            answer=result.get("answer", ""),
            sql=result.get("sql_executed", "")
        )

        # ── Telemetry Logging to MySQL Database (ai_chatbot_usage) ─────────
        try:
            from db.database import save_token_usage_async
            _emp_id = (user_context or {}).get("employee_id") or user_id or 0
            _telem = result.get("telemetry", {})
            _model = _telem.get("model") or ("fast-path-deterministic" if _telem.get("fast_path") else (os.getenv("PRIMARY_MODEL") or "gemini-2.5-flash"))
            _in_tok = _telem.get("input_tokens") if _telem.get("input_tokens") is not None else _telem.get("planner_tokens", 0)
            _out_tok = _telem.get("output_tokens") if _telem.get("output_tokens") is not None else _telem.get("synthesizer_tokens", 0)
            _tot_tok = _telem.get("total_tokens", _in_tok + _out_tok)
            _exec_path = _telem.get("execution_path") or ("FAST_PATH" if _telem.get("fast_path") else "PLANNER_LLM")
            await save_token_usage_async(
                employee_id=_emp_id,
                session_id=session_id,
                model_name=_model,
                input_tokens=_in_tok,
                output_tokens=_out_tok,
                total_tokens=_tot_tok,
                total_cost_usd=0.0,
                status="success",
                trace_id=_telem.get("trace_id") or f"trc_{(session_id or 'sess')[:8]}_{int(time.time()*1000)}",
                capability_id=_telem.get("capability_id"),
                operation=_telem.get("operation"),
                execution_path=_exec_path,
                planner_tokens=_telem.get("planner_tokens", 0),
                synthesizer_tokens=_telem.get("synthesizer_tokens", 0),
                clarification_required=bool(_telem.get("clarification_required")),
                clarification_reason=_telem.get("clarification_reason"),
                backend_execution_ms=int(_telem.get("backend_ms") or 0),
                total_execution_ms=int(_telem.get("execution_ms") or 0)
            )
        except Exception as _tb_err:
            logger.error(f"[ChatRoute] Failed to save success token usage: {_tb_err}")
    except Exception as e:
        logger.error(f"[ChatRoute] resolve_answer failed: {e}")
        try:
            from db.database import save_token_usage_async
            _emp_id = (user_context or {}).get("employee_id") or user_id or 0
            _model = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "unknown"
            await save_token_usage_async(
                employee_id=_emp_id,
                session_id=session_id,
                model_name=_model,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                total_cost_usd=0.0,
                status="failed",
                error_type=e.__class__.__name__,
                error_message=str(e)[:512]
            )
        except Exception as _db_err:
            logger.error(f"[ChatRoute] Failed to persist exception to DB: {_db_err}")
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    try:
        # ✅ Suppress KPI filter panel if a sufficient text answer was provided by the LLM
        # (e.g. over 80 chars or specific numbers), even if it mapped to kpi_summary initially.
        # Only apply this suppression if the answer came from the Planner/LLM, NOT the deterministic UI fallback.
        if result.get("report_intent") == "kpi_summary" and len(result.get("answer", "").strip()) > 80:
            if result.get("cache_tier") in ["planner", "stream"]:
                result["report_intent"] = "other"

        is_form = False
        ri = result.get("report_intent")
        if ri in {"estimation_sl_picker", "fy_clarification"}:
            is_form = True
        elif ri == "kpi_summary" and not result.get("kpi_payload"):
            is_form = True
        elif ri == "receivable" and not request.is_internal:
            is_form = True

        if not is_form:
            question_to_save = question
            if request.is_internal:
                session_ref = await session_manager.get_session(session_id)
                if session_ref:
                    for m in reversed(session_ref.get("messages", [])):
                        if m.get("role") == "user":
                            question_to_save = m.get("content")
                            break
                            
            await chat_history.save_chat_entry({
                "session_id": session_id,
                "user_id": user_id,
                "employee_id": user_context.get("employee_id", user_id),
                "role": user_context.get("role", "Staff"),
                "hierarchy_level": user_context.get("hierarchy_level", 4),
                "department_id": user_context.get("department_id"),
                "service_line_id": user_context.get("service_line_id"),
                "question": question_to_save,
                "answer": result["answer"],
                "chart_data": result.get("chart_data"),
                "was_cache_hit": result.get("was_cached", False),
                "cache_tier": result.get("cache_tier", "fresh"),
                "sql_executed": result.get("sql_executed"),
                "latency_ms": result.get("latency_ms", 0),
                "timestamp": datetime.utcnow(),
                "fiscal_year": _current_fiscal_year(),
            })
    except Exception as e:
        logger.error(f"[ChatRoute] save_chat_entry failed: {e}")
        # Don't fail the response — the answer was already generated

    # Append messages to session (rolling window)
    await session_manager.append_message(session_id, "user", question)
    await session_manager.append_message(session_id, "assistant", result["answer"])

    # *** DEBUG: Log the response being sent ***
    # ✅ Force report_intent='receivable' for broad receivable queries
    # This ensures the frontend always shows the filter wizard panel.
    if _is_broad_receivable_query(question):
        result["report_intent"] = "receivable"

    from utils.structured_logger import log_summary
    _telem = result.get("telemetry", {})
    _tot_tok = _telem.get("total_tokens", 0)
    _is_fast = _telem.get("fast_path", False)
    log_summary(
        logger,
        SessionId=session_id,
        User=user_context.get("name", "Unknown"),
        Tokens=_tot_tok,
        FastPath=_is_fast,
        Status="SUCCESS"
    )

    def _sanitize_response_dict(obj):
        if isinstance(obj, dict):
            return {k: _sanitize_response_dict(v) for k, v in obj.items() if not k.startswith("_")}
        elif isinstance(obj, (list, tuple)):
            return [_sanitize_response_dict(v) for v in obj]
        elif hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
            try:
                return _sanitize_response_dict(obj.to_dict())
            except Exception:
                return str(obj)
        elif hasattr(obj, "__dataclass_fields__"):
            try:
                d = {}
                for f_name in obj.__dataclass_fields__:
                    val = getattr(obj, f_name, None)
                    d[f_name] = _sanitize_response_dict(val)
                return d
            except Exception:
                return str(obj)
        elif isinstance(obj, (int, float, str, bool, type(None))):
            return obj
        else:
            return str(obj)

    from registry.contract_engine import wrap_presentation_intent
    _cap_id = result.get("report_intent") or (result.get("execution_plan") or {}).get("primary_capability") or "report"
    result = wrap_presentation_intent(result, question, _cap_id)
    return _sanitize_response_dict(result)




@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Streaming AI chat endpoint using Server-Sent Events (SSE).
    Sends the AI response word-by-word for a real-time typing effect.
    Final 'done' event contains metadata (chart_data, navigation, etc.) as JSON.
    """
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse
    from memory.conversation_memory import memory

    user_context = _decode_jwt(credentials)
    
    # Merge UI context (like active date picker) into the user_context
    if hasattr(request, "context") and request.context:
        user_context.update(request.context)

    # NEW: Extract report filters from the query text and merge into user_context globally
    from main import _extract_kpi_filters_from_text
    _text_filters = _extract_kpi_filters_from_text(request.question.strip())
    for _k, _v in _text_filters.items():
        if _v and str(_v).lower() != "all" and _k not in user_context:
            user_context[_k] = _v

    user_id = user_context.get("user_id", 0)
    question = request.question.strip()
    session_id = request.session_id

    context_prompt = memory.get_context_prompt(session_id)

    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")


    session = await session_manager.get_session(session_id, user_id=user_id, user_context=user_context)
    if session is None:
        raise HTTPException(status_code=401, detail="Session expired")
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Session does not belong to authenticated user")

    session_messages = session.get("messages", [])
    current_role = (user_context.get("role") or "").strip().lower()
    session_role = (session.get("role") or "").strip().lower()
    current_emp = user_context.get("employee_id")
    session_emp = session.get("employee_id")
    if (session_role and current_role and session_role != current_role) or (
        session_emp and current_emp and session_emp != current_emp
    ):
        session_messages = []
    history = [{"role": m["role"], "content": m["content"]} for m in session_messages]
    if context_prompt:
        history.insert(0, {"role": "system", "content": context_prompt})
    history.append({"role": "user", "content": question})

    from semantic import semantic_layer
    semantic_layer._CRM_AUTH_TOKEN = credentials.credentials or ''
    from config.role_tier_config import get_tier_for_role
    resolved_tier = get_tier_for_role(user_context.get('role', 'Unknown'))
    semantic_layer.set_user_context({
        'employee_id': user_context.get('employee_id', 0) or 0,
        'user_tier': resolved_tier,
        'role_name': user_context.get('role', 'Unknown'),
        'department_id': user_context.get('department_id'),
    })



    async def event_generator():
        full_answer = ""
        result = {}
        try:
            _already_streamed = False

            # IMMEDIATELY yield so the frontend doesn't freeze while waiting for the LLM
            yield f"data: {json.dumps({'type': 'thinking', 'content': 'Checking your question...'})}\n\n"
            await asyncio.sleep(0)

            if USE_ENTERPRISE_PLANNER:
                _run_hybrid = True
            else:
                # ── FY Guard: fires FIRST before any SQL or deterministic route ─────
                # If a time-sensitive query has no date → show FY picker widget immediately.
                from agent.fy_guard import needs_fy_clarification, build_fy_clarification_response
                from agent.query_parser import _extract_person_name
                
                # Use the smart intent classifier to decide whether to skip the FY guard.
                # This replaces all brittle keyword lists with a single LLM call.
                from agent.intent_classifier import classify_intent as _classify_intent_guard
                _guard_intent = await _classify_intent_guard(question)
                
                from main import _extract_kpi_filters_from_text
                _kpi_filters = _extract_kpi_filters_from_text(question)
                _has_kpi_entity = any(v and str(v).lower() != "all" for k, v in _kpi_filters.items() if k in ["service_line", "department", "employee_name", "customer"])
    
                # Intents that have their own date-free tools or their own filter picker panels
                # should be skipped by the FY Guard to avoid double-prompting or blocking charts.
                _INTENTS_THAT_SKIP_FY_GUARD = {
                    "receivables",   # get_receivables_metrics is date-free (always current outstanding)
                    "other",         # fallback / navigation queries — don't need an FY picker
                }
                # Estimation report messages from the picker always embed date range
                _ESTIMATION_FY_SKIP = [
                    "estimation report", "total estimation", "estimated hours",
                    "hour overrun", "exceeded hours", "estimated project",
                    "service line", "all time",
                ]
                _is_estimation = any(sig in question.lower() for sig in _ESTIMATION_FY_SKIP)
                
                _needs_fy, _resolved_fy = await needs_fy_clarification(question, history)
                
                # 1. Receivables and other date-free intents
                # 2. Estimation reports (picker already sends dates)
                # 3. KPIs with no entity (the KpiFilterPanel handles those)
                # 4. Analytical or specific lookups that the LLM agent handles
                _skip_fy_guard = (
                    _guard_intent in _INTENTS_THAT_SKIP_FY_GUARD
                    or _is_estimation
                    or (_guard_intent == "kpi_summary" and not _has_kpi_entity)
                    or _guard_intent == "analytical"
                )
    
                if _needs_fy and not _skip_fy_guard:
                    _emp_name = _extract_person_name(question)
                    _intent_to_use = _resolved_fy if isinstance(_resolved_fy, str) else "fy_clarification"
                    _r = await build_fy_clarification_response(question, intent=_intent_to_use)
                    full_answer = _r["answer"]
                    for i, word in enumerate(full_answer.split(" ")):
                        yield f"data: {json.dumps({'type': 'token', 'content': word + (' ' if i < len(full_answer.split()) - 1 else '')})}\n\n"
                        await asyncio.sleep(0.004)
                    yield f"data: {json.dumps({'type': 'done', 'content': full_answer, 'report_intent': _r.get('report_intent', 'fy_clarification'), 'show_fy_picker': True, 'entity_name': _emp_name, 'navigate_to': None, 'fy_picker': _r.get('fy_picker'), 'chart_data': None, 'navigation_links': [], 'suggested_questions': _r.get('suggested_questions', []), 'export_data': None, 'auto_expand': False, 'kpi_payload': None, 'is_edit_intent': False})}\n\n"
                    return  # stop — no SQL, no LLM
    
                from main import deterministic_dashboard_response
                fast = await deterministic_dashboard_response(history, question, user_context, credentials.credentials)
                if fast:
                    result = fast
                    result["was_cached"] = False
                    result["cache_tier"] = "fresh"
                    result["latency_ms"] = 0
                    logger.info(f"[StreamRoute] Matched deterministic route for: {question[:60]}...")
                else:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': 'Looking up your data...'})}\n\n"
                    await asyncio.sleep(0)
                    _run_hybrid = False
                
                if not USE_ENTERPRISE_PLANNER:
                    # ── FY Guard: intercept resource utilization queries with no date ───────
                    # Show the FY picker widget BEFORE running any SQL.
                    from agent.fy_guard import needs_fy_clarification, build_fy_clarification_response
                    from agent.query_parser import _extract_person_name
                    _res_util_kws = {
                        "resource utilization", "resource utilisation",
                        "utilization report", "utilisation report",
                        "billable hours", "chargeable hours",
                    }
                    _is_resource_util_chat = any(kw in question.lower() for kw in _res_util_kws)
                    if _is_resource_util_chat:
                        # For resource utilization, always ask for dates (ignore history) unless typed in the question
                        _needs_fy, _resolved_fy = await needs_fy_clarification(question, [])
                        if _needs_fy:
                            _emp_name = _extract_person_name(question)
                            result = await build_fy_clarification_response(question)
                            # Upgrade to resource_utilization intent so the frontend
                            # renders our ResourceUtilFilterPanel instead of a generic picker
                            result["report_intent"] = "resource_utilization"
                            result["show_fy_picker"] = True
                            result["entity_name"] = _emp_name
                            result["navigate_to"] = "/projects/reports/resource-utilization-report"
                            # Done — skip memory_manager entirely
                            full_answer = result["answer"]
                            words = full_answer.split(" ")
                            yield f"data: {json.dumps({'type': 'thinking', 'content': ''})}\n\n"
                            await asyncio.sleep(0)
                            for i, word in enumerate(words):
                                chunk = word + (" " if i < len(words) - 1 else "")
                                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                                await asyncio.sleep(0.004)
                            _already_streamed = True
                        else:
                            _run_hybrid = True
                    else:
                        _run_hybrid = True

            if _run_hybrid:
                import hashlib
                from db.database_redis import get_redis
                from rag import vector_store_v2 as vector_store
                from agent.agent import ask_question_streaming
                from memory.memory_manager import _needs_live_data
                
                role = user_context.get("role", "Staff")
                employee_id = user_context.get("employee_id", 0)
                user_tier = user_context.get("hierarchy_level", 9)
                if user_tier >= 4 and employee_id:
                    scope_key = f"{role}:{employee_id}"
                else:
                    scope_key = role
                    
                q_hash = hashlib.sha256(question.strip().lower().encode()).hexdigest()
                redis_key = f"qa:{q_hash}:{hashlib.sha256(scope_key.encode()).hexdigest()[:12]}"
                
                cached_result = None
                
                # Live data queries bypass cache to avoid stale financial/HR data
                # The Enterprise Planner also bypasses legacy caching to allow dynamic slot filling
                if not _needs_live_data(question) and not USE_ENTERPRISE_PLANNER:
                    # 1. Tier 1: Redis cache
                    try:
                        redis = get_redis()
                        cached = await redis.get(redis_key)
                        if cached:
                            data = json.loads(cached)
                            if data.get("role_scope") == scope_key:
                                data["hit_count"] = data.get("hit_count", 0) + 1
                                await redis.setex(redis_key, 3600, json.dumps(data))
                                cached_result = data
                                cached_result["cache_tier"] = "redis"
                                cached_result["was_cached"] = True
                    except Exception as e:
                        logger.warning(f"Redis cache check failed: {e}")
                        
                    # 2. Tier 2: Vector cache
                    if not cached_result:
                        yield f"data: {json.dumps({'type': 'thinking', 'content': 'Running database query...'})}\n\n"
                        await asyncio.sleep(0)
                        try:
                            vector_hit = await vector_store.get_cached_answer(question, scope_key)
                            if vector_hit:
                                cached_result = vector_hit
                        except Exception as e:
                            logger.warning(f"Vector cache check failed: {e}")
                        
                if cached_result:
                    result = cached_result
                else:
                    # 3. Tier 3: Real streaming / Planner
                    yield f"data: {json.dumps({'type': 'thinking', 'content': 'Analysing results...'})}\n\n"
                    await asyncio.sleep(0)
                    
                    if USE_ENTERPRISE_PLANNER:
                        from agent.executive_classifier import handle_executive_classification
                        _is_conv, _conv_reply = await handle_executive_classification(question, history, user_context)
                        if _is_conv and _conv_reply:
                            logger.info(f"[ExecutiveClassifier Stream] Handled conversational query '{question[:40]}' via short-circuit (0 Planner/DB cost)")
                            yield f"data: {json.dumps({'type': 'token', 'content': _conv_reply})}\n\n"
                            yield f"data: {safe_json_dumps({'type': 'done', 'answer': _conv_reply})}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        _already_streamed = False
                        # ── Clarification State Injection (zero LLM cost) ──────────────────
                        from memory.session_manager import (
                            get_clarification_state, save_clarification_state, clear_clarification_state
                        )
                        # [DIAG-1]
                        logger.info(f"[DIAG-1] /chat/stream | session_id={session_id} | user_id={user_id} | question='{question[:60]}'")

                        # [DIAG-3]
                        _clar_state = await get_clarification_state(session_id)

                        # [DIAG-4]
                        logger.info(
                            f"[DIAG-4] /chat/stream | clarification_state loaded | "
                            f"has_state={bool(_clar_state)} | "
                            f"missing_fields={_clar_state.get('missing_fields') if _clar_state else None}"
                        )

                        if _clar_state:
                            user_context["previous_execution_plan"] = _clar_state.get("execution_plan")
                            logger.info(f"[StreamRoute] Injected clarification state for session={session_id}, missing={_clar_state.get('missing_fields')}")

                        # [DIAG-5/6]
                        logger.info(
                            f"[DIAG-5] /chat/stream | user_context keys={list(user_context.keys())} | "
                            f"has_previous_execution_plan={'previous_execution_plan' in user_context} | "
                            f"previous_plan_caps={[c.get('id') for c in user_context.get('previous_execution_plan', {}).get('business_capabilities', [])] if user_context.get('previous_execution_plan') else None}"
                        )

                        _rcv_jwt = credentials.credentials or ""
                        _masked_rcv_jwt = (_rcv_jwt[:15] + "...") if _rcv_jwt else "None"
                        logger.info(f"[RUNTIME INSTRUMENTATION] [ChatRoute RECEIVED JWT]: Exists={bool(_rcv_jwt)} | Masked={_masked_rcv_jwt}")

                        req_ctx = RequestContext(
                            question=question,
                            jwt_token=credentials.credentials,
                            session_id=session_id,
                            history=history,
                            user_context=user_context,
                            request_metadata={"is_internal": request.is_internal},
                            feature_flags={"is_stream": True}
                        )

                        _passed_jwt = req_ctx.jwt_token or ""
                        _masked_passed_jwt = (_passed_jwt[:15] + "...") if _passed_jwt else "None"
                        logger.info(f"[RUNTIME INSTRUMENTATION] [ChatRoute PASSED JWT TO PLANNER]: Exists={bool(_passed_jwt)} | Masked={_masked_passed_jwt}")

                        # [DIAG-6]
                        logger.info(
                            f"[DIAG-6] /chat/stream | RequestContext.user_context has previous_execution_plan="
                            f"{bool(req_ctx.user_context.get('previous_execution_plan'))}"
                        )

                        yield f"data: {json.dumps({'type': 'thinking', 'content': 'Analyzing query intent & business context...'})}\n\n"
                        await asyncio.sleep(0)

                        planner = EnterprisePlanner()
                        plan_result = await planner.execute_turn(req_ctx)
                        logger.info(f"[RUNTIME INSTRUMENTATION] [ChatRoute PLANNER TURN RETURNED]:\n{json.dumps(plan_result, default=str)[:1000]}")
                        result = plan_result
                        result["answer"] = plan_result.get("content", "Sorry, I could not process that.")
                        result["cache_tier"] = "planner"
                        result["was_cached"] = False
                        full_answer = result["answer"]

                        # ── Clarification State Persistence ───────────────────────────────
                        _is_clar = result.get("is_clarification", False)
                        _plan_in_result = result.get("execution_plan")
                        _missing_in_result = (_plan_in_result or {}).get("missing_information", []) if _plan_in_result else []

                        if _is_clar and _plan_in_result and _missing_in_result:
                            _clar_dto = build_clarification_dto(
                                session_id=session_id,
                                original_question=question,
                                execution_plan=_plan_in_result,
                                missing_fields=_missing_in_result,
                                resolved_entities=_plan_in_result.get("resolved_entities", []),
                                planner_context=user_context
                            )
                            await save_clarification_state(session_id, _clar_dto)
                        else:
                            # Execution succeeded — clear any stale clarification state
                            await clear_clarification_state(session_id)

                    else:
                        _already_streamed = True
                        _cleared_thinking = False
                        async for event in ask_question_streaming(history, user_context):
                            if not _cleared_thinking and event.get("type") == "token":
                                yield f"data: {json.dumps({'type': 'thinking', 'content': ''})}\n\n"
                                await asyncio.sleep(0)
                                _cleared_thinking = True
                            yield f"data: {safe_json_dumps(event)}\n\n"
                            if event.get("type") == "done":
                                result = event
                                result["answer"] = event.get("content", "")
                                result["cache_tier"] = "stream"
                                result["was_cached"] = False
                                full_answer = result["answer"]
                                if "execution_plan" in event:
                                    result["execution_plan"] = event["execution_plan"]

            if not _already_streamed:
                answer_text = result.get("answer", "Sorry, I could not process that.")
                full_answer = answer_text
                # Fake word-split streaming for cache hits
                yield f"data: {json.dumps({'type': 'thinking', 'content': ''})}\n\n"
                await asyncio.sleep(0)
                words = answer_text.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
                    await asyncio.sleep(0.004)

            # ✅ Force report_intent='receivable' for broad receivable queries
            if _is_broad_receivable_query(question):
                result["report_intent"] = "receivable"

            # ✅ Suppress KPI filter panel if a sufficient text answer was provided by the LLM
            # (e.g. over 80 chars or specific numbers), even if it mapped to kpi_summary initially.
            # Only apply this suppression if the answer came from the Planner/LLM, NOT the deterministic UI fallback.
            if result.get("report_intent") == "kpi_summary" and len(full_answer.strip()) > 80:
                if result.get("cache_tier") in ["planner", "stream"]:
                    result["report_intent"] = "other"
                    result["is_form"] = False

            memory.update_context(
                session_id=session_id,
                question=question,
                answer=result.get("answer", ""),
                sql=result.get("sql_executed", "")
            )

        except Exception as e:
            import traceback
            logger.error(f"[StreamEndpoint] Error: {e}\n{traceback.format_exc()}")
            try:
                from db.database import save_token_usage_async
                _emp_id = (user_context or {}).get("employee_id") or user_id or 0
                _model = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "unknown"
                await save_token_usage_async(
                    employee_id=_emp_id,
                    session_id=session_id,
                    model_name=_model,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    total_cost_usd=0.0,
                    status="failed",
                    error_type=e.__class__.__name__,
                    error_message=str(e)[:512]
                )
                logger.info(f"[StreamEndpoint] Saved exception failure record to DB: status=failed, type={e.__class__.__name__}")
            except Exception as _db_err:
                logger.error(f"[StreamEndpoint] Failed to persist exception failure to DB: {_db_err}")

            # Emit a 'done' event so the frontend renders the message in the chat bubble.
            # The frontend only handles 'thinking', 'token', and 'done' — 'error' type is silently ignored.
            error_payload = {
                "type": "done",
                "content": "⚠️ I ran into an issue processing that request. Please try rephrasing your question, or contact support if this keeps happening.",
                "chart_data": None,
                "navigate_to": None,
                "navigation_links": [],
                "export_data": None,
                "auto_expand": False,
                "suggested_questions": ["Show total revenue", "Show open proposals", "What are the pending receivables?"],
                "report_intent": None,
                "kpi_payload": None,
                "entity_name": None,
                "entity_type": None,
                "is_edit_intent": False,
                "show_fy_picker": False,
                "fy_picker": None,
            }
            yield f"data: {safe_json_dumps(error_payload)}\n\n"
            yield "data: [DONE]\n\n"
            return

        from registry.contract_engine import wrap_presentation_intent
        _cap_id = result.get("report_intent") or (result.get("execution_plan") or {}).get("primary_capability") or "report"
        result = wrap_presentation_intent(result, question, _cap_id)

        metadata = {
            "type": "done",
            "content": full_answer,
            "chart_data": result.get("chart_data"),
            "navigate_to": result.get("navigate_to"),
            "navigation_links": result.get("navigation_links"),
            "export_data": result.get("export_data"),
            "presentation_intent": result.get("presentation_intent", "VIEW"),
            "actions": result.get("actions", []),
            "auto_expand": result.get("auto_expand", False),
            "suggested_questions": result.get("suggested_questions"),
            "report_intent": result.get("report_intent"),
            "kpi_payload": result.get("kpi_payload"),
            "entity_name": result.get("entity_name"),
            "entity_type": result.get("entity_type"),
            "is_edit_intent": result.get("is_edit_intent", False),
            "fy_picker": result.get("fy_picker"),
            "execution_plan": result.get("execution_plan"),
            "slot": result.get("slot"),
            "is_slot_request": result.get("type") == "slot_request",
        }
        yield f"data: {safe_json_dumps(metadata)}\n\n"
        yield "data: [DONE]\n\n"

        # ── Token tracking & failure logging ───────────────────────────────────
        # Always log failures (rate limits, model errors) even when token count is 0
        _error_code = result.get("error_code")
        _token_usage = result.get("token_usage", {})
        _total_tokens = _token_usage.get("total_tokens", 0) if isinstance(_token_usage, dict) else 0

        if _error_code and _total_tokens == 0:
            try:
                from db.database import save_token_usage_async
                _emp_id = (user_context or {}).get("employee_id") or user_id or 0
                _model = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "unknown"
                asyncio.create_task(save_token_usage_async(
                    employee_id=_emp_id,
                    session_id=session_id,
                    model_name=_model,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    total_cost_usd=0.0,
                    status="failed",
                    error_type=_error_code,
                    error_message=(result.get("content", "") or "")[:512]
                ))
                logger.info(f"[TokenTracker] Logged failure to DB: status=failed, type={_error_code}")
            except Exception as _te:
                logger.error(f"[TokenTracker] Could not log failure: {_te}")

        # Calculate and track token cost (successful AND fast-path 0-token requests)
        token_usage = result.get("token_usage", {})
        _is_fast_path = isinstance(token_usage, dict) and token_usage.get("model_name") == "lightweight_router_fast_path"
        if token_usage and (token_usage.get("total_tokens", 0) > 0 or _is_fast_path):
            model = token_usage.get("model_name", "llama-3.3-70b-versatile")
            in_tok = token_usage.get("input_tokens", 0)
            out_tok = token_usage.get("output_tokens", 0)
            tot_tok = token_usage.get("total_tokens", 0)
            model_key = (model or "").lower()
            
            # Dynamic pricing dictionary (Cost per 1M tokens)
            pricing_map = {
                # Groq Models
                "llama-3.3-70b-versatile": {"in": 0.59, "out": 0.79},
                "llama-3.1-8b-instant": {"in": 0.05, "out": 0.08},
                "llama3-70b-8192": {"in": 0.59, "out": 0.79},
                "llama3-8b-8192": {"in": 0.05, "out": 0.08},
                "mixtral-8x7b-32768": {"in": 0.24, "out": 0.24},
                "gemma2-9b-it": {"in": 0.20, "out": 0.20},
                
                # OpenAI Models
                "gpt-4o": {"in": 5.00, "out": 15.00},
                "gpt-4o-mini": {"in": 0.15, "out": 0.60},
                "gpt-4-turbo": {"in": 10.00, "out": 30.00},
                "gpt-3.5-turbo": {"in": 0.50, "out": 1.50},
                
                # Anthropic Models
                "claude-3-5-sonnet-20240620": {"in": 3.00, "out": 15.00},
                "claude-3-opus-20240229": {"in": 15.00, "out": 75.00},
                "claude-3-haiku-20240307": {"in": 0.25, "out": 1.25},
            }
            
            rates = pricing_map.get(model_key, {"in": 0.0, "out": 0.0})
            
            # Fallback for dynamic/custom names not strictly matching keys
            if rates["in"] == 0.0 and rates["out"] == 0.0:
                if "70b" in model_key: rates = {"in": 0.59, "out": 0.79}
                elif "8b" in model_key: rates = {"in": 0.05, "out": 0.08}
                elif "gpt-4o-mini" in model_key: rates = {"in": 0.15, "out": 0.60}
                elif "gpt-4o" in model_key: rates = {"in": 5.00, "out": 15.00}
                elif "gpt-4" in model_key: rates = {"in": 10.00, "out": 30.00}
                elif "gpt-3.5" in model_key: rates = {"in": 0.50, "out": 1.50}
                elif "claude-3-5-sonnet" in model_key: rates = {"in": 3.00, "out": 15.00}
                elif "claude-3-haiku" in model_key: rates = {"in": 0.25, "out": 1.25}

            cost = (in_tok / 1000000.0 * rates["in"]) + (out_tok / 1000000.0 * rates["out"])
            try:
                from db.database import save_token_usage_async
                # Determine status from result: if error_code is set, the planner or synthesizer failed
                _error_code = result.get("error_code")
                _db_status = "failed" if _error_code else "success"
                _exec_path = "FAST_PATH" if _is_fast_path else "PLANNER_LLM"
                _task = asyncio.create_task(save_token_usage_async(
                    employee_id=user_context.get("employee_id", user_id) or user_id,
                    session_id=session_id,
                    model_name=model or "unknown",
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=tot_tok,
                    total_cost_usd=cost,
                    status=_db_status,
                    error_type=_error_code,
                    error_message=None,
                    execution_path=_exec_path
                ))
                # Attach error callback so unhandled exceptions don't silently disrupt the event loop
                def _db_task_done(t):
                    if not t.cancelled() and t.exception():
                        logger.error(f"[TokenTracker] Background DB task failed: {t.exception()}")
                _task.add_done_callback(_db_task_done)
            except Exception as e:
                logger.error(f"[StreamEndpoint] Failed to spawn token tracking task: {e}")

        try:
            is_form = False
            ri = result.get("report_intent")
            if ri in {"estimation_sl_picker", "fy_clarification"}:
                is_form = True
            elif ri == "kpi_summary" and not result.get("kpi_payload"):
                is_form = True
            elif ri == "receivable" and not request.is_internal:
                is_form = True

            if not is_form:
                question_to_save = question
                if request.is_internal:
                    session_ref = await session_manager.get_session(session_id)
                    if session_ref:
                        for m in reversed(session_ref.get("messages", [])):
                            if m.get("role") == "user":
                                question_to_save = m.get("content")
                                break
                                
                await chat_history.save_chat_entry({
                    "session_id": session_id,
                    "user_id": user_id,
                    "employee_id": user_context.get("employee_id", user_id),
                    "role": user_context.get("role", "Staff"),
                    "hierarchy_level": user_context.get("hierarchy_level", 4),
                    "department_id": user_context.get("department_id"),
                    "service_line_id": user_context.get("service_line_id"),
                    "question": question_to_save,
                    "answer": full_answer,
                    "chart_data": result.get("chart_data"),
                    "was_cache_hit": result.get("was_cached", False),
                    "cache_tier": result.get("cache_tier", "stream"),
                    "sql_executed": result.get("sql_executed"),
                    "latency_ms": result.get("latency_ms", 0),
                    "timestamp": datetime.utcnow(),
                    "fiscal_year": _current_fiscal_year(),
                })
            await session_manager.append_message(session_id, "user", question)
            await session_manager.append_message(session_id, "assistant", full_answer)
        except Exception as e:
            logger.error(f"[StreamEndpoint] Persist failed: {e}")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/history")
async def get_history(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO format: 2025-01-01"),
    date_to: Optional[str] = Query(None, description="ISO format: 2025-12-31"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get paginated chat history for the authenticated user."""
    user_context = _decode_jwt(credentials)
    user_id = user_context["user_id"]

    try:
        # Parse date strings if provided
        df = datetime.fromisoformat(date_from.replace('Z', '+00:00')) if date_from else None
        dt = datetime.fromisoformat(date_to.replace('Z', '+00:00')) if date_to else None

        result = await chat_history.get_user_history(
            user_id=user_id,
            limit=limit,
            skip=skip,
            search_query=search,
            date_from=df,
            date_to=dt,
        )
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history/{session_id}")
async def get_session_history_route(
    session_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get chat history for a specific session (user_id guard enforced)."""
    user_context = _decode_jwt(credentials)
    user_id = user_context["user_id"]

    entries = await chat_history.get_session_history(session_id, user_id)
    # Re-warm / restore session in session manager so downstream chat queries succeed
    await session_manager.restore_session_from_history(session_id, user_id, user_context)
    return {"messages": entries, "entries": entries}


@router.delete("/history/{session_id}")
async def delete_session_history_by_id(
    session_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Delete a specific chat history session by session_id in URL path.
    """
    user_context = _decode_jwt(credentials)
    user_id = user_context["user_id"]

    clean_sid = str(session_id).strip()
    if not clean_sid or clean_sid.lower() in ("none", "null", "undefined"):
        raise HTTPException(
            status_code=400,
            detail="A valid session_id is required."
        )

    count = await chat_history.delete_user_history(
        user_id=user_id,
        session_id=clean_sid,
    )

    try:
        await session_manager.invalidate_session(clean_sid)
    except Exception as e:
        logger.warning(f"[DeleteHistory] Session invalidation failed for {clean_sid}: {e}")

    return DeleteResponse(deleted=count)


@router.delete("/history")
async def delete_history(
    request: Request,
    session_id: Optional[str] = Query(None),
    sessionId: Optional[str] = Query(None),
    entry_id: Optional[str] = Query(None),
    entryId: Optional[str] = Query(None),
    clear_all: bool = Query(False),
    clearAll: bool = Query(False),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Delete chat history for the authenticated user (Query param or Body based).
    """
    user_context = _decode_jwt(credentials)
    user_id = user_context["user_id"]

    target_session_id = session_id or sessionId
    target_entry_id = entry_id or entryId
    is_clear_all = clear_all or clearAll

    # Parse JSON body if path/query params were not supplied
    if not target_session_id and not target_entry_id and not is_clear_all:
        try:
            body = await request.json()
            if isinstance(body, dict):
                target_session_id = body.get("session_id") or body.get("sessionId")
                target_entry_id = body.get("entry_id") or body.get("entryId") or body.get("id")
                is_clear_all = bool(body.get("clear_all") or body.get("clearAll"))
        except Exception:
            pass

    # Sanitize inputs against stringified "null", "undefined", "none", or empty strings
    if target_session_id and str(target_session_id).strip().lower() in ("none", "null", "undefined", ""):
        target_session_id = None
    if target_entry_id and str(target_entry_id).strip().lower() in ("none", "null", "undefined", ""):
        target_entry_id = None

    # Safety Guard: Require session_id, entry_id, or explicit clear_all flag
    if not target_session_id and not target_entry_id and not is_clear_all:
        raise HTTPException(
            status_code=400,
            detail="A valid session_id or entry_id parameter is required to delete a specific chat item. Pass clear_all=true to delete all history."
        )

    count = await chat_history.delete_user_history(
        user_id=user_id,
        session_id=target_session_id,
        entry_id=target_entry_id,
        clear_all=is_clear_all
    )

    # Invalidate Redis / MongoDB session state if deleting a specific session
    if target_session_id:
        try:
            await session_manager.invalidate_session(target_session_id)
        except Exception as e:
            logger.warning(f"[DeleteHistory] Session invalidation failed for {target_session_id}: {e}")

    return DeleteResponse(deleted=count)


@router.post("/session")
@router.get("/session")
async def create_session(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Create a new AI chat session for the authenticated user."""
    user_context = _decode_jwt(credentials)

    session_id = await session_manager.create_session(user_context)
    return SessionResponse(session_id=session_id)


@router.get("/cache/stats")
async def cache_stats(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Get cache statistics (admin only).
    Only allows hierarchy_level <= 2 (CEO or Partner).
    """
    user_context = _decode_jwt(credentials)
    hierarchy_level = user_context.get("hierarchy_level", 9)

    if hierarchy_level > 2:
        raise HTTPException(
            status_code=403,
            detail="Cache statistics are restricted to CEO and Partner roles.",
        )

    stats = await memory_manager.get_cache_stats()
    return stats


@router.get("/receivable-filters")
async def get_receivable_filters(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Return dropdown options for receivables report filters.
    Values are restricted by the authenticated user's RBAC scope."""
    user_context = _decode_jwt(credentials)
    employee_id = user_context.get("employee_id", 0) or 0
    user_tier = user_context.get("hierarchy_level", 9) or 9

    try:
        from semantic.semantic_layer import _build_ownership_sql
        ownership_sql = _build_ownership_sql(
            employee_id=employee_id,
            user_tier=user_tier,
            table_alias="i",
            check_service_line=False,
            owner_col="project_in_charge_id",
            sl_col="service_line_id",
        )
    except Exception as e:
        logger.warning(f"[Filters] Failed to build ownership SQL, using safe fallback scope: {e}")
        if employee_id and user_tier >= 4:
            safe_emp_id = int(employee_id)
            ownership_sql = f"(i.project_in_charge_id = {safe_emp_id} OR i.created_by = {safe_emp_id})"
        else:
            ownership_sql = "1=1"

    company_q = f"""
        SELECT DISTINCT o.name AS value
        FROM invoice i
        JOIN organization o ON o.id = i.organization_id
        WHERE i.is_active = 1
          AND o.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND o.name IS NOT NULL
          AND TRIM(o.name) <> ''
        ORDER BY o.name
        LIMIT 500
    """

    project_q = f"""
        SELECT DISTINCT p.name AS value
        FROM invoice i
        JOIN projects p ON p.id = i.project_id
        WHERE i.is_active = 1
          AND p.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND p.name IS NOT NULL
          AND TRIM(p.name) <> ''
        ORDER BY p.name
        LIMIT 500
    """

    customer_q = f"""
        SELECT DISTINCT c.customer_name AS value
        FROM invoice i
        JOIN customers c ON c.id = i.client_name_id
        WHERE i.is_active = 1
          AND c.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND c.customer_name IS NOT NULL
          AND TRIM(c.customer_name) <> ''
        ORDER BY c.customer_name
        LIMIT 500
    """

    group_q = f"""
        SELECT DISTINCT g.name AS value
        FROM invoice i
        JOIN customers c ON c.id = i.client_name_id
        JOIN m_group g ON g.id = c.cust_group_id
        WHERE i.is_active = 1
          AND c.is_active = 1
          AND g.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND g.name IS NOT NULL
          AND TRIM(g.name) <> ''
        ORDER BY g.name
        LIMIT 500
    """

    service_line_q = f"""
        SELECT DISTINCT sl.name AS value
        FROM invoice i
        JOIN m_serviceline sl ON sl.id = i.service_line_id
        WHERE i.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND sl.name IS NOT NULL
          AND TRIM(sl.name) <> ''
        ORDER BY sl.name
        LIMIT 500
    """

    project_in_charge_q = f"""
        SELECT DISTINCT e.employee_name AS value
        FROM invoice i
        JOIN employees e ON e.id = i.project_in_charge_id
        WHERE i.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          AND e.employee_name IS NOT NULL
          AND TRIM(e.employee_name) <> ''
        ORDER BY e.employee_name
        LIMIT 500
    """

    def _run_values_query(query: str) -> list[str]:
        try:
            engine = get_db_engine()
            with engine.connect() as conn:
                rows = conn.execute(sql_text(query)).fetchall()
            values: list[str] = []
            for row in rows:
                val = row[0] if row and row[0] is not None else None
                if not val:
                    continue
                text_val = str(val).strip()
                if text_val:
                    values.append(text_val)
            return values
        except Exception as ex:
            logger.warning(f"[Filters] Query failed: {ex}")
            return []

    company_values = _run_values_query(company_q)
    project_values = _run_values_query(project_q)
    customer_values = _run_values_query(customer_q)
    group_values = _run_values_query(group_q)
    service_line_values = _run_values_query(service_line_q)
    project_in_charge_values = _run_values_query(project_in_charge_q)

    logger.info(
        "[Filters] Retrieved data: "
        f"company={len(company_values)}, project={len(project_values)}, customer={len(customer_values)}, "
        f"group={len(group_values)}, service_line={len(service_line_values)}, "
        f"project_in_charge={len(project_in_charge_values)}"
    )

    return {
        "company": company_values,
        "project": project_values,
        "customerName": customer_values,
        "group": group_values,
        "serviceLine": service_line_values,
        "projectInCharge": project_in_charge_values,
    }


@router.get("/kpi-filters")
async def get_kpi_filters(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    service_line: Optional[str] = Query(None, description="Filter departments by service line"),
    department: Optional[str] = Query(None, description="Filter employees by department"),
):
    """
    Cascading KPI filter endpoint.
    - No params            -> returns all service lines
    - ?service_line=X      -> returns departments under that service line
    - ?service_line=X&department=Y -> returns employees under that service line + department
    """
    # Validate token and resolve user context for RBAC
    user_context = _decode_jwt(credentials)
    employee_id = int(user_context.get("employee_id") or 0)
    user_tier = int(user_context.get("hierarchy_level") or 9)

    # Build ownership SQL to scope KPI queries according to server-side RBAC rules
    try:
        from semantic.semantic_layer import _build_ownership_sql
        ownership_sql = _build_ownership_sql(
            employee_id=employee_id,
            user_tier=user_tier,
            table_alias="km",
            check_service_line=True,
            owner_col="employee_id",
            sl_col="service_line_id",
        )
    except Exception as e:
        logger.warning(f"[KPI Filters] Failed to build ownership SQL: {e}")
        # Safe fallback: allow aggregates for management tiers, otherwise deny
        ownership_sql = "1=1" if user_tier <= 4 else "1=0"

    def _run_parameterised(query: str, params: dict) -> list[str]:
        try:
            engine = get_db_engine()
            with engine.connect() as conn:
                rows = conn.execute(sql_text(query), params).fetchall()
            values: list[str] = []
            for row in rows:
                val = row[0] if row and row[0] is not None else None
                if val is None:
                    continue
                text_val = str(val).strip()
                if text_val:
                    values.append(text_val)
            return values
        except Exception as ex:
            logger.warning(f"[KPI Cascade Filters] Query failed: {ex}")
            return []

    # Employees filtered by service line + department
    # Only return employees who actually have KPI records in this service line + department
    if (service_line and department
            and service_line.strip().lower() != "all"
            and department.strip().lower() != "all"):
        emp_q = f"""
            SELECT DISTINCT emp.employee_name AS value
            FROM kpi_master km
            JOIN employees emp ON km.employee_id = emp.id
            JOIN m_serviceline sl ON km.service_line_id = sl.id
            JOIN m_department dep ON km.department_id = dep.id
            WHERE emp.is_active = 1
              AND sl.name = :sl
              AND dep.name = :dep
              AND {ownership_sql}
              AND emp.employee_name IS NOT NULL
              AND TRIM(emp.employee_name) <> ''
            ORDER BY emp.employee_name
            LIMIT 1000
        """
        employees = _run_parameterised(emp_q, {"sl": service_line, "dep": department})
        return {"type": "employeeName", "values": employees}

    # Departments filtered by service line
    if service_line and service_line.strip().lower() != "all":
        dept_q = f"""
            SELECT DISTINCT dep.name AS value
            FROM m_department dep
            JOIN serviceline_department sld ON dep.id = sld.department_id
            JOIN m_serviceline sl ON sld.serviceline_id = sl.id
            JOIN kpi_master km ON km.department_id = dep.id 
              AND km.service_line_id = sl.id
            WHERE dep.is_active = 1
              AND sl.name = :sl
              AND {ownership_sql}
              AND dep.name IS NOT NULL
              AND TRIM(dep.name) <> ''
            ORDER BY dep.name
            LIMIT 500
        """
        departments = _run_parameterised(dept_q, {"sl": service_line})
        return {"type": "department", "values": departments}

    # Default: all service lines
    sl_q = f"""
        SELECT DISTINCT sl.name AS value
        FROM m_serviceline sl
        JOIN kpi_master km ON km.service_line_id = sl.id
        WHERE sl.is_active = 1
          AND sl.name IS NOT NULL
          AND TRIM(sl.name) <> ''
          AND {ownership_sql}
        ORDER BY sl.name
        LIMIT 100
    """
    service_lines = _run_parameterised(sl_q, {})
    return {"type": "serviceLine", "values": service_lines}


@router.get("/staff-billing-filters")
async def get_staff_billing_filters(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """Return dropdown options for Staff Billing report filters."""
    user_context = _decode_jwt(credentials)
    employee_id = user_context.get("employee_id", 0) or 0
    user_tier = user_context.get("hierarchy_level", 9) or 9

    try:
        from semantic.semantic_layer import _build_ownership_sql
        ownership_sql = _build_ownership_sql(
            employee_id=employee_id,
            user_tier=user_tier,
            table_alias="i",
            check_service_line=False,
            owner_col="project_in_charge_id",
            sl_col="service_line_id",
        )
    except Exception as e:
        logger.warning(f"[Filters] Failed to build ownership SQL, using safe fallback scope: {e}")
        if employee_id and user_tier >= 4:
            safe_emp_id = int(employee_id)
            ownership_sql = f"(i.project_in_charge_id = {safe_emp_id} OR i.created_by = {safe_emp_id})"
        else:
            ownership_sql = "1=1"

    def _run_values_query(query: str) -> list[str]:
        try:
            from db.database import get_db_engine
            from sqlalchemy import text as sql_text
            engine = get_db_engine()
            with engine.connect() as conn:
                rows = conn.execute(sql_text(query)).fetchall()
            values = []
            for row in rows:
                val = row[0] if row and row[0] is not None else None
                if not val: continue
                text_val = str(val).strip()
                if text_val: values.append(text_val)
            return values
        except Exception as ex:
            logger.warning(f"[Staff Billing Filters] Query failed: {ex}")
            return []

    date_filter = ""
    if start_date and end_date:
        # Validate dates to prevent SQL injection
        import re
        if re.match(r'^\d{4}-\d{2}-\d{2}$', start_date) and re.match(r'^\d{4}-\d{2}-\d{2}$', end_date):
            date_filter = f" AND DATE(i.created_at) >= '{start_date}' AND DATE(i.created_at) <= '{end_date}' "

    service_line_q = f"""
        SELECT DISTINCT sl.name AS value
        FROM invoice i
        JOIN m_serviceline sl ON sl.id = i.service_line_id
        WHERE i.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          {date_filter}
          AND sl.name IS NOT NULL
          AND TRIM(sl.name) != ''
        ORDER BY sl.name
        LIMIT 500
    """

    project_in_charge_q = f"""
        SELECT DISTINCT e.employee_name AS value
        FROM invoice i
        JOIN employees e ON e.id = i.project_in_charge_id
        WHERE i.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          {date_filter}
          AND e.employee_name IS NOT NULL
          AND TRIM(e.employee_name) != ''
        ORDER BY e.employee_name
        LIMIT 500
    """

    customer_q = f"""
        SELECT DISTINCT c.customer_name AS value
        FROM invoice i
        JOIN customers c ON c.id = i.client_name_id
        WHERE i.is_active = 1
          AND c.is_active = 1
          AND i.payment_status_id NOT IN (2, 4)
          AND {ownership_sql}
          {date_filter}
          AND c.customer_name IS NOT NULL
          AND TRIM(c.customer_name) != ''
        ORDER BY c.customer_name
        LIMIT 500
    """

    # For employee name, we want all active employees so they can filter staff billing by any employee.
    emp_q = f"""
        SELECT DISTINCT employee_name AS value
        FROM employees
        WHERE is_active = 1
          AND employee_name IS NOT NULL
          AND TRIM(employee_name) != ''
        ORDER BY employee_name
        LIMIT 1000
    """

    return {
        "serviceLine": _run_values_query(service_line_q),
        "projectPartner": _run_values_query(project_in_charge_q),
        "customerName": _run_values_query(customer_q),
        "employeeName": _run_values_query(emp_q),
    }
