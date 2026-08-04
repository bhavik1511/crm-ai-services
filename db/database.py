from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from dotenv import load_dotenv
from urllib.parse import quote_plus
from typing import Optional
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
                status VARCHAR(20) NOT NULL DEFAULT 'success',
                error_type VARCHAR(50) NULL,
                error_message VARCHAR(512) NULL,
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
        ("status",        "ALTER TABLE ai_chatbot_usage ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'success'"),
        ("error_type",    "ALTER TABLE ai_chatbot_usage ADD COLUMN error_type VARCHAR(50) NULL"),
        ("error_message", "ALTER TABLE ai_chatbot_usage ADD COLUMN error_message VARCHAR(512) NULL"),
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
    error_message: str = None
):
    """
    Asynchronously saves the token usage record for chatbot to MySQL (ai_chatbot_usage).
    """
    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO ai_chatbot_usage 
                        (employee_id, session_id, model_name, input_tokens, output_tokens, total_tokens, total_cost_usd)
                        VALUES (:emp_id, :sess_id, :model, :in_tok, :out_tok, :tot_tok, :cost)
                    """),
                    {
                        "emp_id": employee_id,
                        "sess_id": session_id,
                        "model": model_name,
                        "in_tok": input_tokens,
                        "out_tok": output_tokens,
                        "tot_tok": total_tokens,
                        "cost": total_cost_usd,
                        "status": status,
                        "error_type": error_type,
                        "error_message": (error_message or "")[:512] if error_message else None
                    }
                )
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
    if model_name is None:
        model_name = (
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            os.getenv("LLM_PROVIDER") or 
            "unknown"
        )

    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
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
    if model_name is None:
        model_name = (
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            os.getenv("LLM_PROVIDER") or 
            "unknown"
        )
        
    try:
        engine = get_db_engine()
        with engine.begin() as conn:
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
            os.getenv("PRIMARY_MODEL") or 
            os.getenv("OPENROUTER_PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            os.getenv("LLM_PROVIDER") or 
            "llama-3.3-70b-versatile"
        )

    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
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

