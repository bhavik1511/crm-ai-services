from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from dotenv import load_dotenv
from urllib.parse import quote_plus
from typing import Optional, Any, Union
import os

load_dotenv(override=True)
import time
unique_id = int(time.time())
print(f"[DEBUG ENV] Server unique ID: {unique_id}")
print(f"[DEBUG ENV] Loaded .env from: {os.path.abspath('.env')}")
print(f"[DEBUG ENV] LLM_PROVIDER: {os.getenv('LLM_PROVIDER')}")

_engine = None


def get_db_engine() -> Engine:
    """Create and return a cached SQLAlchemy engine for the CRM MySQL database."""
    global _engine
    if _engine is not None:
        return _engine

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "3306")
    user = os.getenv("DB_USER", "root")
    password = quote_plus(os.getenv("DB_PWD", ""))
    db_name = os.getenv("DB_NAME", "dashboard_ai")

    connection_string = (
        f"mysql+pymysql://{user}:{password}@{host}:{port}/{db_name}"
    )

    _engine = create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=10,
    )

    # Disable ONLY_FULL_GROUP_BY on every connection
    @event.listens_for(_engine, "connect")
    def _set_sql_mode(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute(
            "SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,"
            "NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'"
        )
        cursor.close()

    # Automatically initialize the AI telemetry tables (ai_chatbot_usage & ai_email_parsing)
    with _engine.begin() as conn:

        # Automatically initialize the standardized chatbot telemetry table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_chatbot_usage (
                id INT AUTO_INCREMENT PRIMARY KEY,
                employee_id INT NOT NULL,
                session_id VARCHAR(255) NULL,
                model_name VARCHAR(100),
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                total_tokens INT DEFAULT 0,
                total_cost_usd DECIMAL(10, 6) DEFAULT 0.000000,
                status VARCHAR(50) DEFAULT 'success',
                error_type VARCHAR(50) NULL,
                error_message VARCHAR(512) NULL,
                execution_path VARCHAR(50) DEFAULT 'fast_path',
                capability_id VARCHAR(100) DEFAULT 'general_query',
                operation VARCHAR(100) DEFAULT 'chat_response',
                backend_execution_ms INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # Automatically initialize the standardized email parsing telemetry table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_email_parsing (
                id INT AUTO_INCREMENT PRIMARY KEY,
                employee_id INT NULL,
                document_type VARCHAR(50) NOT NULL,
                reference_id VARCHAR(255) NULL,
                model_name VARCHAR(100),
                has_attachment BOOLEAN DEFAULT FALSE,
                file_extension VARCHAR(50) NULL,
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                total_tokens INT DEFAULT 0,
                total_cost_usd DECIMAL(10, 6) DEFAULT 0.000000,
                confidence_score INT NULL,
                confidence_level VARCHAR(20) NULL,
                processing_status VARCHAR(50) NULL,
                processing_time_ms INT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

    # Safe column migration: add new columns if they don't exist yet (compatible with all MySQL versions)
    _col_migrations = [
        ("status",                 "ALTER TABLE ai_chatbot_usage ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'success'"),
        ("error_type",             "ALTER TABLE ai_chatbot_usage ADD COLUMN error_type VARCHAR(50) NULL"),
        ("error_message",          "ALTER TABLE ai_chatbot_usage ADD COLUMN error_message VARCHAR(512) NULL"),
        ("trace_id",               "ALTER TABLE ai_chatbot_usage ADD COLUMN trace_id VARCHAR(255) NULL"),
        ("capability_id",          "ALTER TABLE ai_chatbot_usage ADD COLUMN capability_id VARCHAR(100) NULL"),
        ("operation",              "ALTER TABLE ai_chatbot_usage ADD COLUMN operation VARCHAR(50) NULL"),
        ("execution_path",          "ALTER TABLE ai_chatbot_usage ADD COLUMN execution_path VARCHAR(50) NULL"),
        ("planner_tokens",         "ALTER TABLE ai_chatbot_usage ADD COLUMN planner_tokens INT DEFAULT 0"),
        ("synthesizer_tokens",     "ALTER TABLE ai_chatbot_usage ADD COLUMN synthesizer_tokens INT DEFAULT 0"),
        ("clarification_required", "ALTER TABLE ai_chatbot_usage ADD COLUMN clarification_required BOOLEAN DEFAULT FALSE"),
        ("clarification_reason",   "ALTER TABLE ai_chatbot_usage ADD COLUMN clarification_reason VARCHAR(255) NULL"),
        ("backend_execution_ms",   "ALTER TABLE ai_chatbot_usage ADD COLUMN backend_execution_ms INT DEFAULT 0"),
        ("total_execution_ms",     "ALTER TABLE ai_chatbot_usage ADD COLUMN total_execution_ms INT DEFAULT 0"),
        ("user_query",             "ALTER TABLE ai_chatbot_usage ADD COLUMN user_query TEXT NULL"),
    ]
    for col_name, col_sql in _col_migrations:
        with _engine.begin() as _conn:
            exists = _conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_chatbot_usage' AND COLUMN_NAME = '{col_name}'"
            )).scalar()
            if not exists:
                try:
                    _conn.execute(text(col_sql))
                except Exception as _e:
                    logger.warning(f"[DB Migration] Could not add column {col_name}: {_e}")

    try:
        engine = get_db_engine()
        with engine.begin() as conn:
            # Automatically initialize the unified ML dataset & human feedback table
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ai_email_ml_dataset (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reference_id VARCHAR(255) UNIQUE NULL,
                    sender_email VARCHAR(255) NULL,
                    to_emails TEXT NULL,
                    subject VARCHAR(500) NULL,
                    body_clean LONGTEXT NULL,
                    thread_count INT DEFAULT 1,
                    is_forwarded BOOLEAN DEFAULT FALSE,
                    forwarded_by_email VARCHAR(255) NULL,
                    predicted_intent VARCHAR(100) NULL,
                    extracted_keywords TEXT NULL,
                    confidence_score INT DEFAULT 90,
                    action_status VARCHAR(50) NULL,
                    is_task_required BOOLEAN DEFAULT TRUE,
                    was_edited BOOLEAN DEFAULT FALSE,
                    intent_edited BOOLEAN DEFAULT FALSE,
                    customer_edited BOOLEAN DEFAULT FALSE,
                    assignee_edited BOOLEAN DEFAULT FALSE,
                    due_date_edited BOOLEAN DEFAULT FALSE,
                    is_hard_example BOOLEAN DEFAULT FALSE,
                    time_to_action_ms INT NULL,
                    approved_intent VARCHAR(100) NULL,
                    approved_task_name VARCHAR(500) NULL,
                    approved_customer_name VARCHAR(255) NULL,
                    approved_customer_id VARCHAR(100) NULL,
                    approved_priority VARCHAR(50) NULL,
                    approved_due_date VARCHAR(50) NULL,
                    approved_assignee_id INT NULL,
                    approved_contact_phone VARCHAR(100) NULL,
                    approved_task_description TEXT NULL,
                    reviewed_by_user_id INT NULL,
                    reviewed_by_user_name VARCHAR(255) NULL,
                    reviewed_by_user_email VARCHAR(255) NULL,
                    review_count INT DEFAULT 1,
                    discard_count INT DEFAULT 0,
                    include_in_training BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                );
            """))

            # Alter table migrations for existing installations
            for col_stmt in [
                "ALTER TABLE ai_chatbot_usage ADD COLUMN status VARCHAR(50) DEFAULT 'success';",
                "ALTER TABLE ai_chatbot_usage ADD COLUMN execution_path VARCHAR(50) DEFAULT 'fast_path';",
                "ALTER TABLE ai_chatbot_usage ADD COLUMN capability_id VARCHAR(100) DEFAULT 'general_query';",
                "ALTER TABLE ai_chatbot_usage ADD COLUMN operation VARCHAR(100) DEFAULT 'chat_response';",
                "ALTER TABLE ai_chatbot_usage ADD COLUMN backend_execution_ms INT DEFAULT 0;",
                "ALTER TABLE ai_email_ml_dataset ADD COLUMN review_count INT DEFAULT 1;",
                "ALTER TABLE ai_email_ml_dataset ADD COLUMN discard_count INT DEFAULT 0;"
            ]:
                try:
                    conn.execute(text(col_stmt))
                except Exception:
                    pass

            # Drop legacy bloated unreadable JSON columns if they exist
            try:
                conn.execute(text("ALTER TABLE ai_email_ml_dataset DROP COLUMN llm_predictions;"))
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE ai_email_ml_dataset DROP COLUMN human_approved_values;"))
            except Exception:
                pass
    except Exception as ex:
        logger.warning(f"[DB Migration] Exception in table setup: {ex}")

    return _engine


import asyncio
import logging
logger = logging.getLogger(__name__)

async def save_token_usage_async(
    employee_id: int, 
    session_id: str, 
    model_name: str, 
    input_tokens: int, 
    output_tokens: int, 
    total_tokens: int, 
    total_cost_usd: float,
    status: str = "success",
    error_type: str = None,
    error_message: str = None,
    trace_id: str = None,
    capability_id: str = "general_query",
    operation: str = "chat_response",
    execution_path: str = "fast_path",
    planner_tokens: int = 0,
    synthesizer_tokens: int = 0,
    clarification_required: bool = False,
    clarification_reason: str = None,
    backend_execution_ms: int = 0,
    total_execution_ms: int = 0,
    user_query: str = None
):
    """
    Asynchronously saves the authoritative token usage & telemetry record for chatbot to MySQL (ai_chatbot_usage).
    """
    def _insert_sync():
        try:
            engine = get_db_engine()
            safe_emp_id = 0
            if employee_id is not None:
                try:
                    s_emp = str(employee_id).strip()
                    if ":" in s_emp:
                        s_emp = s_emp.split(":")[-1]
                    safe_emp_id = int(s_emp)
                except Exception:
                    safe_emp_id = 0

            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO ai_chatbot_usage 
                        (employee_id, session_id, model_name, input_tokens, output_tokens, total_tokens, total_cost_usd, status, error_type, error_message, trace_id, capability_id, operation, execution_path, planner_tokens, synthesizer_tokens, clarification_required, clarification_reason, backend_execution_ms, total_execution_ms, user_query)
                        VALUES (:emp_id, :sess_id, :model, :in_tok, :out_tok, :tot_tok, :cost, :status, :error_type, :error_message, :trace_id, :cap_id, :op, :exec_path, :p_tok, :s_tok, :clar_req, :clar_reason, :b_ms, :t_ms, :u_query)
                    """),
                    {
                        "emp_id": safe_emp_id,
                        "sess_id": str(session_id or "default_session")[:255],
                        "model": str(model_name or "qwen/qwen3.6-27b")[:100],
                        "in_tok": int(input_tokens or 0),
                        "out_tok": int(output_tokens or 0),
                        "tot_tok": int(total_tokens or 0),
                        "cost": float(total_cost_usd or 0.0),
                        "status": str(status or "success")[:50],
                        "error_type": error_type,
                        "error_message": (error_message or "")[:512] if error_message else None,
                        "trace_id": trace_id,
                        "cap_id": str(capability_id or "general_query")[:100],
                        "op": str(operation or "chat_response")[:50],
                        "exec_path": str(execution_path or "fast_path")[:100],
                        "p_tok": int(planner_tokens or 0),
                        "s_tok": int(synthesizer_tokens or 0),
                        "clar_req": bool(clarification_required),
                        "clar_reason": clarification_reason,
                        "b_ms": int(backend_execution_ms or 0),
                        "t_ms": int(total_execution_ms or 0),
                        "u_query": None
                    }
                )
            logger.info(f"[TokenTracker] Successfully saved usage record to ai_chatbot_usage table: emp_id={safe_emp_id}, session={session_id}, path={execution_path}")
        except Exception as e:
            logger.error(f"[TokenTracker] Failed to save chatbot token usage: {e}")

    await asyncio.to_thread(_insert_sync)

async def save_parsing_token_usage_async(
    employee_id: int, 
    document_type: str, 
    reference_id: str,
    input_tokens: int, 
    output_tokens: int, 
    total_tokens: int, 
    total_cost_usd: float,
    model_name: str = None, 
    has_attachment: bool = False,
    file_extension: str = None
):
    """
    Asynchronously saves the token usage record for document parsing (email, pdf, screenshot) to MySQL.
    """
    if not model_name or model_name == "unknown":
        model_name = (
            os.getenv("LLM_MODEL") or 
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            "qwen/qwen3.6-27b"
        )

    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
                if reference_id and str(reference_id).strip():
                    existing = conn.execute(
                        text("SELECT id FROM ai_email_parsing WHERE reference_id = :ref LIMIT 1"),
                        {"ref": str(reference_id).strip()}
                    ).fetchone()
                    if existing:
                        conn.execute(
                            text("""
                                UPDATE ai_email_parsing
                                SET employee_id = :emp_id, document_type = :doc_type, model_name = :model,
                                    input_tokens = :in_tok, output_tokens = :out_tok, total_tokens = :tot_tok,
                                    total_cost_usd = :cost, has_attachment = :has_att, file_extension = :ext,
                                    created_at = CURRENT_TIMESTAMP
                                WHERE id = :row_id
                            """),
                            {
                                "row_id": existing[0],
                                "emp_id": employee_id,
                                "doc_type": document_type,
                                "model": model_name,
                                "in_tok": input_tokens,
                                "out_tok": output_tokens,
                                "tot_tok": total_tokens,
                                "cost": total_cost_usd,
                                "has_att": has_attachment,
                                "ext": file_extension
                            }
                        )
                        return

                conn.execute(
                    text("""
                        INSERT INTO ai_email_parsing 
                        (employee_id, document_type, reference_id, model_name, input_tokens, output_tokens, total_tokens, total_cost_usd, has_attachment, file_extension)
                        VALUES (:emp_id, :doc_type, :ref_id, :model, :in_tok, :out_tok, :tot_tok, :cost, :has_att, :ext)
                    """),
                    {
                        "emp_id": employee_id,
                        "doc_type": document_type,
                        "ref_id": reference_id,
                        "model": model_name,
                        "in_tok": input_tokens,
                        "out_tok": output_tokens,
                        "tot_tok": total_tokens,
                        "cost": total_cost_usd,
                        "has_att": has_attachment,
                        "ext": file_extension
                    }
                )
        except Exception as e:
            logger.error(f"[TokenTracker] Failed to save parsing token usage: {e}")

    await asyncio.to_thread(_insert_sync)

def save_parsing_token_usage(
    employee_id: int, 
    document_type: str, 
    reference_id: str,
    input_tokens: int, 
    output_tokens: int, 
    total_tokens: int, 
    total_cost_usd: float,
    model_name: str = None, 
    has_attachment: bool = False,
    file_extension: str = None
):
    """
    Synchronously saves the token usage record for document parsing (email, pdf, screenshot) to MySQL.
    """
    if not model_name or model_name == "unknown":
        model_name = (
            os.getenv("LLM_MODEL") or 
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            "qwen/qwen3.6-27b"
        )
        
    try:
        engine = get_db_engine()
        with engine.begin() as conn:
            if reference_id and str(reference_id).strip():
                existing = conn.execute(
                    text("SELECT id FROM ai_email_parsing WHERE reference_id = :ref LIMIT 1"),
                    {"ref": str(reference_id).strip()}
                ).fetchone()
                if existing:
                    conn.execute(
                        text("""
                            UPDATE ai_email_parsing
                            SET employee_id = :emp_id, document_type = :doc_type, model_name = :model,
                                input_tokens = :in_tok, output_tokens = :out_tok, total_tokens = :tot_tok,
                                total_cost_usd = :cost, has_attachment = :has_att, file_extension = :ext,
                                created_at = CURRENT_TIMESTAMP
                            WHERE id = :row_id
                        """),
                        {
                            "row_id": existing[0],
                            "emp_id": employee_id,
                            "doc_type": document_type,
                            "model": model_name,
                            "in_tok": input_tokens,
                            "out_tok": output_tokens,
                            "tot_tok": total_tokens,
                            "cost": total_cost_usd,
                            "has_att": has_attachment,
                            "ext": file_extension
                        }
                    )
                    return

            conn.execute(
                text("""
                    INSERT INTO ai_email_parsing 
                    (employee_id, document_type, reference_id, model_name, input_tokens, output_tokens, total_tokens, total_cost_usd, has_attachment, file_extension)
                    VALUES (:emp_id, :doc_type, :ref_id, :model, :in_tok, :out_tok, :tot_tok, :cost, :has_att, :ext)
                """),
                {
                    "emp_id": employee_id,
                    "doc_type": document_type,
                    "ref_id": reference_id,
                    "model": model_name,
                    "in_tok": input_tokens,
                    "out_tok": output_tokens,
                    "tot_tok": total_tokens,
                    "cost": total_cost_usd,
                    "has_att": has_attachment,
                    "ext": file_extension
                }
            )
    except Exception as e:
        logger.error(f"[TokenTracker] Failed to save parsing token usage (sync): {e}")


async def save_ai_email_parsing_async(
    employee_id: Optional[int], 
    document_type: str, 
    reference_id: str,
    input_tokens: int, 
    output_tokens: int, 
    total_tokens: int, 
    total_cost_usd: float,
    model_name: str = None, 
    has_attachment: bool = False,
    file_extension: str = None,
    confidence_score: Optional[int] = None,
    confidence_level: Optional[str] = None,
    processing_status: Optional[str] = None,
    processing_time_ms: Optional[int] = None
):
    """
    Asynchronously saves the telemetry log for email/lead parsing to the ai_email_parsing MySQL table.
    """
    if not model_name or model_name == "unknown":
        model_name = (
            os.getenv("LLM_MODEL") or 
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("OPENROUTER_PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            "qwen/qwen3.6-27b"
        )

    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
                if reference_id and str(reference_id).strip():
                    existing = conn.execute(
                        text("SELECT id FROM ai_email_parsing WHERE reference_id = :ref LIMIT 1"),
                        {"ref": str(reference_id).strip()}
                    ).fetchone()
                    if existing:
                        conn.execute(
                            text("""
                                UPDATE ai_email_parsing
                                SET employee_id = :emp_id, document_type = :doc_type, model_name = :model,
                                    input_tokens = :in_tok, output_tokens = :out_tok, total_tokens = :tot_tok,
                                    total_cost_usd = :cost, has_attachment = :has_att, file_extension = :ext,
                                    confidence_score = :conf_score, confidence_level = :conf_level,
                                    processing_status = :proc_status, processing_time_ms = :proc_time,
                                    created_at = CURRENT_TIMESTAMP
                                WHERE id = :row_id
                            """),
                            {
                                "row_id": existing[0],
                                "emp_id": employee_id,
                                "doc_type": document_type,
                                "model": model_name,
                                "in_tok": input_tokens,
                                "out_tok": output_tokens,
                                "tot_tok": total_tokens,
                                "cost": total_cost_usd,
                                "has_att": has_attachment,
                                "ext": file_extension,
                                "conf_score": confidence_score,
                                "conf_level": confidence_level,
                                "proc_status": processing_status,
                                "proc_time": processing_time_ms
                            }
                        )
                        return

                conn.execute(
                    text("""
                        INSERT INTO ai_email_parsing 
                        (employee_id, document_type, reference_id, model_name, input_tokens, output_tokens, total_tokens, total_cost_usd, has_attachment, file_extension, confidence_score, confidence_level, processing_status, processing_time_ms)
                        VALUES (:emp_id, :doc_type, :ref_id, :model, :in_tok, :out_tok, :tot_tok, :cost, :has_att, :ext, :conf_score, :conf_level, :proc_status, :proc_time)
                    """),
                    {
                        "emp_id": employee_id,
                        "doc_type": document_type,
                        "ref_id": reference_id,
                        "model": model_name,
                        "in_tok": input_tokens,
                        "out_tok": output_tokens,
                        "tot_tok": total_tokens,
                        "cost": total_cost_usd,
                        "has_att": has_attachment,
                        "ext": file_extension,
                        "conf_score": confidence_score,
                        "conf_level": confidence_level,
                        "proc_status": processing_status,
                        "proc_time": processing_time_ms
                    }
                )
        except Exception as e:
            logger.error(f"[TokenTracker] Failed to save ai_email_parsing log: {e}")

    await asyncio.to_thread(_insert_sync)


def calculate_llm_cost(model_name: str = None, input_tokens: int = 0, output_tokens: int = 0) -> float:
    """
    Calculates the USD cost of an LLM call based on model pricing per 1M tokens.
    Guarantees non-zero estimation for active models.
    """
    if not model_name:
        model_name = os.getenv("LLM_MODEL") or os.getenv("PRIMARY_MODEL") or "openai/gpt-oss-20b"
    
    model_key = str(model_name).lower().strip()
    
    pricing_map = {
        # Groq & OpenRouter Models
        "qwen/qwen3.6-27b": {"in": 0.27, "out": 0.40},
        "openai/gpt-oss-20b": {"in": 0.10, "out": 0.15},
        "openai/gpt-oss-120b": {"in": 0.50, "out": 0.75},
        "groq/compound": {"in": 0.25, "out": 0.40},
        "groq/compound-mini": {"in": 0.10, "out": 0.15},
        "llama-3.3-70b-versatile": {"in": 0.59, "out": 0.79},
        "llama-3.3-70b": {"in": 0.59, "out": 0.79},
        "llama-3.1-8b-instant": {"in": 0.05, "out": 0.08},
        "llama-3.1-8b": {"in": 0.05, "out": 0.08},
        "llama-3.2-90b": {"in": 0.90, "out": 0.90},
        "llama3-70b-8192": {"in": 0.59, "out": 0.79},
        "llama3-8b-8192": {"in": 0.05, "out": 0.08},
        "mixtral-8x7b-32768": {"in": 0.24, "out": 0.24},
        "gemma2-9b-it": {"in": 0.20, "out": 0.20},
        
        # OpenAI Models
        "gpt-4o": {"in": 2.50, "out": 10.00},
        "gpt-4o-mini": {"in": 0.15, "out": 0.60},
        "gpt-4-turbo": {"in": 10.00, "out": 30.00},
        "gpt-3.5-turbo": {"in": 0.50, "out": 1.50},
        
        # Anthropic Models
        "claude-3-5-sonnet-20240620": {"in": 3.00, "out": 15.00},
        "claude-3-opus-20240229": {"in": 15.00, "out": 75.00},
        "claude-3-haiku-20240307": {"in": 0.25, "out": 1.25},
    }
    
    rates = pricing_map.get(model_key)
    if not rates:
        if "70b" in model_key: rates = {"in": 0.59, "out": 0.79}
        elif "90b" in model_key: rates = {"in": 0.90, "out": 0.90}
        elif "8b" in model_key: rates = {"in": 0.05, "out": 0.08}
        elif "gpt-4o-mini" in model_key: rates = {"in": 0.15, "out": 0.60}
        elif "gpt-4o" in model_key: rates = {"in": 2.50, "out": 10.00}
        elif "gpt-4" in model_key: rates = {"in": 10.00, "out": 30.00}
        elif "gpt-3.5" in model_key: rates = {"in": 0.50, "out": 1.50}
        elif "claude-3-5-sonnet" in model_key: rates = {"in": 3.00, "out": 15.00}
        elif "claude-3-haiku" in model_key: rates = {"in": 0.25, "out": 1.25}
        elif "qwen" in model_key: rates = {"in": 0.27, "out": 0.40}
        elif "mixtral" in model_key: rates = {"in": 0.24, "out": 0.24}
        else: rates = {"in": 0.50, "out": 0.50}

    cost = (input_tokens / 1_000_000.0 * rates["in"]) + (output_tokens / 1_000_000.0 * rates["out"])
    return round(cost, 6)



import json

def check_duplicate_message_id(reference_id: str) -> Optional[dict]:
    """
    Checks if an email Message ID / reference_id has already been processed in MySQL DB.
    Returns existing parsed result dict if found, skipping redundant LLM invocations.
    """
    if not reference_id or str(reference_id).startswith("draft_") or len(str(reference_id).strip()) < 5:
        return None

    ref_clean = str(reference_id).strip()
    INVALID_PHRASES = ["information for your", "contact information", "further take up"]
    
    try:
        engine = get_db_engine()
        with engine.connect() as conn:
            # Search ai_email_parsing for existing telemetry record
            p_row = conn.execute(
                text("SELECT confidence_score, confidence_level, document_type FROM ai_email_parsing WHERE reference_id = :ref ORDER BY id DESC LIMIT 1"),
                {"ref": ref_clean}
            ).fetchone()
            
            if p_row:
                # Retrieve extracted entities from ai_email_ml_dataset if present
                ml_row = conn.execute(
                    text("SELECT predicted_intent, extracted_keywords, subject, body_clean FROM ai_email_ml_dataset WHERE reference_id = :ref ORDER BY id DESC LIMIT 1"),
                    {"ref": ref_clean}
                ).fetchone()

                if ml_row and len(ml_row) >= 4:
                    keywords_str = str(ml_row[1] or "").lower()
                    subj_str = str(ml_row[2] or "").lower()
                    body_str = str(ml_row[3] or "").lower()
                    # Invalidate cache if stale row contains invalid filler string
                    if any(phrase in keywords_str or phrase in subj_str for phrase in INVALID_PHRASES):
                        logger.info(f"[check_duplicate_message_id] Stale invalid phrase detected in cached record for {ref_clean}. Bypassing cache for fresh re-parse.")
                        return None

                intent_val = ml_row[0] if (ml_row and len(ml_row) > 0 and ml_row[0]) else "General Task"
                return {
                    "reference_id": ref_clean,
                    "intent": intent_val,
                    "confidence_score": p_row[0] if (p_row and len(p_row) > 0 and p_row[0] is not None) else 90,
                    "confidence_level": p_row[1] if (p_row and len(p_row) > 1 and p_row[1]) else "high",
                    "is_duplicate": True,
                    "duplicate_notice": "Email Message ID was previously processed. Cached result returned without calling LLM.",
                    "requires_manual_review": False,
                    "task_description": f"Cached task extraction for Message ID: {ref_clean}"
                }
    except Exception as e:
        logger.error(f"[check_duplicate_message_id] Error checking duplicate reference_id: {e}")
    return None


async def save_email_ml_dataset_async(
    reference_id: Optional[str],
    sender_email: Optional[str],
    to_emails: Optional[str],
    subject: Optional[str],
    body_clean: Optional[str],
    thread_count: int,
    is_forwarded: bool,
    forwarded_by_email: Optional[str],
    predicted_intent: Optional[str],
    extracted_keywords: Optional[list],
    extracted_entities: Optional[dict],
    confidence_score: int = 90,
    employee_id: Optional[int] = None
):
    """
    Asynchronously saves initial ML training dataset entry to MySQL table (ai_email_ml_dataset)
    AND appends JSONL sample to logs/email_ml_dataset.jsonl for AI fine-tuning.
    """
    keywords_str = json.dumps(extracted_keywords or [])
    entities_json = json.dumps(extracted_entities or {})

    def _save_sync():
        # 1. Insert/Update initial parse in MySQL DB table
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
                existing = None
                if reference_id and str(reference_id).strip():
                    existing = conn.execute(
                        text("SELECT id FROM ai_email_ml_dataset WHERE reference_id = :ref AND (reviewed_by_user_id = :emp_id OR reviewed_by_user_id IS NULL) LIMIT 1"),
                        {"ref": str(reference_id).strip(), "emp_id": employee_id}
                    ).fetchone()
                
                if not existing and subject and str(subject).strip():
                    existing = conn.execute(
                        text("SELECT id FROM ai_email_ml_dataset WHERE subject = :subj AND (sender_email = :send OR :send IS NULL) AND (reviewed_by_user_id = :emp_id OR reviewed_by_user_id IS NULL) LIMIT 1"),
                        {"subj": str(subject).strip(), "send": sender_email, "emp_id": employee_id}
                    ).fetchone()

                if existing:
                    conn.execute(
                        text("""
                            UPDATE ai_email_ml_dataset
                            SET reference_id = COALESCE(:ref_id, reference_id),
                                sender_email = COALESCE(:sender, sender_email),
                                to_emails = COALESCE(:to_e, to_emails),
                                subject = COALESCE(:subj, subject),
                                body_clean = COALESCE(:body, body_clean),
                                thread_count = :t_cnt,
                                is_forwarded = :is_fwd,
                                forwarded_by_email = COALESCE(:fwd_by, forwarded_by_email),
                                predicted_intent = COALESCE(:intent, predicted_intent),
                                extracted_keywords = COALESCE(:kw, extracted_keywords),
                                confidence_score = :conf
                            WHERE id = :row_id
                        """),
                        {
                            "row_id": existing[0],
                            "ref_id": reference_id,
                            "sender": sender_email,
                            "to_e": to_emails,
                            "subj": subject,
                            "body": body_clean,
                            "t_cnt": thread_count,
                            "is_fwd": is_forwarded,
                            "fwd_by": forwarded_by_email,
                            "intent": predicted_intent,
                            "kw": keywords_str,
                            "conf": confidence_score
                        }
                    )
                else:
                    conn.execute(
                        text("""
                            INSERT INTO ai_email_ml_dataset 
                            (reference_id, sender_email, to_emails, subject, body_clean, thread_count, is_forwarded, forwarded_by_email, predicted_intent, extracted_keywords, confidence_score, reviewed_by_user_id)
                            VALUES (:ref_id, :sender, :to_e, :subj, :body, :t_cnt, :is_fwd, :fwd_by, :intent, :kw, :conf, :emp_id)
                        """),
                        {
                            "ref_id": reference_id,
                            "sender": sender_email,
                            "to_e": to_emails,
                            "subj": subject,
                            "body": body_clean,
                            "t_cnt": thread_count,
                            "is_fwd": is_forwarded,
                            "fwd_by": forwarded_by_email,
                            "intent": predicted_intent,
                            "kw": keywords_str,
                            "conf": confidence_score,
                            "emp_id": employee_id
                        }
                    )
        except Exception as e:
            logger.error(f"[MLDataset] Failed to insert DB ML dataset row: {e}")

        # 2. Append to local JSONL dataset file for LLM fine-tuning
        try:
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            jsonl_path = os.path.join(log_dir, "email_ml_dataset.jsonl")

            sample = {
                "timestamp": time.time(),
                "reference_id": reference_id,
                "input": {
                    "sender_email": sender_email,
                    "to_emails": to_emails,
                    "subject": subject,
                    "body_clean": body_clean,
                    "thread_count": thread_count,
                    "is_forwarded": is_forwarded,
                    "forwarded_by_email": forwarded_by_email
                },
                "extracted_keywords": extracted_keywords or [],
                "target_output": {
                    "predicted_intent": predicted_intent,
                    "extracted_entities": extracted_entities or {},
                    "confidence_score": confidence_score
                }
            }

            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        except Exception as fe:
            logger.error(f"[MLDataset] Failed to write JSONL dataset file: {fe}")

    await asyncio.to_thread(_save_sync)


async def update_email_ml_dataset_feedback_async(
    reference_id: str,
    action_status: str,
    is_task_required: bool = True,
    was_edited: bool = False,
    intent_edited: bool = False,
    customer_edited: bool = False,
    assignee_edited: bool = False,
    due_date_edited: bool = False,
    is_hard_example: bool = False,
    time_to_action_ms: Optional[int] = None,
    human_approved_values: Optional[dict] = None,
    reviewed_by_user_id: Optional[int] = None,
    reviewed_by_user_name: Optional[str] = None,
    reviewed_by_user_email: Optional[str] = None,
    include_in_training: bool = True
):
    """
    Asynchronously updates the exact same record in ai_email_ml_dataset when a user acts in the UI modal
    AND logs the supervised training pair to logs/email_ml_feedback_pairs.jsonl.
    """
    vals = human_approved_values or {}
    approved_json = json.dumps(vals)

    def _update_sync():
        # 1. Update existing row in MySQL DB table
        try:
            engine = get_db_engine()
            is_disc = 1 if action_status == 'DISCARDED' else 0
            with engine.begin() as conn:
                existing = None
                if reference_id and str(reference_id).strip():
                    ref_str = str(reference_id).strip()
                    existing = conn.execute(
                        text("SELECT id FROM ai_email_ml_dataset WHERE reference_id = :ref LIMIT 1"),
                        {"ref": ref_str}
                    ).fetchone()
                    if not existing and "_" in ref_str:
                        # Try partial match on raw email ID or subject component
                        subj_part = ref_str.split("_")[-1]
                        if len(subj_part) > 3:
                            existing = conn.execute(
                                text("SELECT id FROM ai_email_ml_dataset WHERE reference_id LIKE :pat OR subject LIKE :pat LIMIT 1"),
                                {"pat": f"%{subj_part}%"}
                            ).fetchone()

                if existing:
                    conn.execute(
                        text("""
                            UPDATE ai_email_ml_dataset
                            SET action_status = :act_stat,
                                is_task_required = :is_task,
                                was_edited = :was_ed,
                                intent_edited = :int_ed,
                                customer_edited = :cust_ed,
                                assignee_edited = :ass_ed,
                                due_date_edited = :due_ed,
                                is_hard_example = :hard_ex,
                                time_to_action_ms = :t_action,
                                approved_intent = :app_intent,
                                approved_task_name = :app_tname,
                                approved_customer_name = :app_cname,
                                approved_customer_id = :app_cid,
                                approved_priority = :app_prio,
                                approved_due_date = :app_ddate,
                                approved_assignee_id = :app_ass,
                                approved_contact_phone = :app_phone,
                                approved_task_description = :app_desc,
                                reviewed_by_user_id = :rev_id,
                                reviewed_by_user_name = :rev_name,
                                reviewed_by_user_email = :rev_email,
                                include_in_training = :inc_train,
                                review_count = review_count + 1,
                                discard_count = discard_count + :is_disc
                            WHERE id = :row_id
                        """),
                        {
                            "row_id": existing[0],
                            "act_stat": action_status,
                            "is_task": is_task_required,
                            "was_ed": was_edited,
                            "int_ed": intent_edited,
                            "cust_ed": customer_edited,
                            "ass_ed": assignee_edited,
                            "due_ed": due_date_edited,
                            "hard_ex": is_hard_example or was_edited,
                            "t_action": time_to_action_ms,
                            "app_intent": vals.get("intent"),
                            "app_tname": vals.get("task_name"),
                            "app_cname": vals.get("customer_name"),
                            "app_cid": str(vals.get("customer_id")) if vals.get("customer_id") is not None else None,
                            "app_prio": vals.get("priority"),
                            "app_ddate": str(vals.get("due_date")) if vals.get("due_date") is not None else None,
                            "app_ass": int(vals["assigned_to"]) if vals.get("assigned_to") and str(vals["assigned_to"]).isdigit() else None,
                            "app_phone": vals.get("contact_phone"),
                            "app_desc": vals.get("task_description"),
                            "rev_id": reviewed_by_user_id,
                            "rev_name": reviewed_by_user_name,
                            "rev_email": reviewed_by_user_email,
                            "inc_train": include_in_training,
                            "is_disc": is_disc
                        }
                    )
                else:
                    conn.execute(
                        text("""
                            INSERT INTO ai_email_ml_dataset
                            (reference_id, action_status, is_task_required, was_edited, intent_edited, customer_edited, 
                             assignee_edited, due_date_edited, is_hard_example, time_to_action_ms, approved_intent, 
                             approved_task_name, approved_customer_name, approved_customer_id, approved_priority, 
                             approved_due_date, approved_assignee_id, approved_contact_phone, approved_task_description, 
                             reviewed_by_user_id, reviewed_by_user_name, reviewed_by_user_email, include_in_training, review_count, discard_count)
                            VALUES
                            (:ref_id, :act_stat, :is_task, :was_ed, :int_ed, :cust_ed, :ass_ed, :due_ed, :hard_ex, :t_action, 
                             :app_intent, :app_tname, :app_cname, :app_cid, :app_prio, :app_ddate, :app_ass, :app_phone, 
                             :app_desc, :rev_id, :rev_name, :rev_email, :inc_train, 1, :is_disc)
                        """),
                        {
                            "ref_id": reference_id,
                            "act_stat": action_status,
                            "is_task": is_task_required,
                            "was_ed": was_edited,
                            "int_ed": intent_edited,
                            "cust_ed": customer_edited,
                            "ass_ed": assignee_edited,
                            "due_ed": due_date_edited,
                            "hard_ex": is_hard_example or was_edited,
                            "t_action": time_to_action_ms,
                            "app_intent": vals.get("intent"),
                            "app_tname": vals.get("task_name"),
                            "app_cname": vals.get("customer_name"),
                            "app_cid": str(vals.get("customer_id")) if vals.get("customer_id") is not None else None,
                            "app_prio": vals.get("priority"),
                            "app_ddate": str(vals.get("due_date")) if vals.get("due_date") is not None else None,
                            "app_ass": int(vals["assigned_to"]) if vals.get("assigned_to") and str(vals["assigned_to"]).isdigit() else None,
                            "app_phone": vals.get("contact_phone"),
                            "app_desc": vals.get("task_description"),
                            "rev_id": reviewed_by_user_id,
                            "rev_name": reviewed_by_user_name,
                            "rev_email": reviewed_by_user_email,
                            "inc_train": include_in_training,
                            "is_disc": is_disc
                        }
                    )
        except Exception as e:
            logger.error(f"[MLDataset] Failed to update feedback in DB: {e}")

        # 2. Append ground-truth feedback pair to local JSONL for ML fine-tuning
        try:
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            feedback_jsonl_path = os.path.join(log_dir, "email_ml_feedback_pairs.jsonl")

            pair_sample = {
                "timestamp": time.time(),
                "reference_id": reference_id,
                "action_status": action_status,
                "include_in_training": include_in_training,
                "was_edited": was_edited,
                "is_hard_example": is_hard_example or was_edited,
                "field_edits": {
                    "intent": intent_edited,
                    "customer": customer_edited,
                    "assignee": assignee_edited,
                    "due_date": due_date_edited
                },
                "time_to_action_ms": time_to_action_ms,
                "human_approved_values": human_approved_values or {}
            }

            with open(feedback_jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(pair_sample, ensure_ascii=False) + "\n")
        except Exception as fe:
            logger.error(f"[MLDataset] Failed to write feedback JSONL file: {fe}")

    await asyncio.to_thread(_update_sync)



