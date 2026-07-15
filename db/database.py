from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from dotenv import load_dotenv
from urllib.parse import quote_plus
import os

load_dotenv(override=True)
import time
unique_id = int(time.time())
print(f"[DEBUG ENV] Server unique ID: {unique_id}")
print(f"[DEBUG ENV] Loaded .env from: {os.path.abspath('.env')}")
print(f"[DEBUG ENV] OPENROUTER_PRIMARY_MODEL: {os.getenv('OPENROUTER_PRIMARY_MODEL')}")
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

    # Automatically initialize the AI token tracking table
    with _engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_token_usage (
                id INT AUTO_INCREMENT PRIMARY KEY,
                employee_id INT NOT NULL,
                session_id VARCHAR(255) NULL,
                model_name VARCHAR(100),
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                total_tokens INT DEFAULT 0,
                total_cost_usd DECIMAL(10, 6) DEFAULT 0.000000,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # Automatically initialize the document parsing token tracking table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_parsing_token_usage (
                id INT AUTO_INCREMENT PRIMARY KEY,
                employee_id INT NOT NULL,
                document_type VARCHAR(50) NOT NULL,
                reference_id VARCHAR(255) NULL,
                model_name VARCHAR(100),
                has_attachment BOOLEAN DEFAULT FALSE,
                file_extension VARCHAR(50) NULL,
                input_tokens INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                total_tokens INT DEFAULT 0,
                total_cost_usd DECIMAL(10, 6) DEFAULT 0.000000,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

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
    total_cost_usd: float
):
    """
    Asynchronously saves the token usage record to MySQL to avoid blocking chat responses.
    """
    def _insert_sync():
        try:
            engine = get_db_engine()
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO ai_token_usage 
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
                        "cost": total_cost_usd
                    }
                )
        except Exception as e:
            logger.error(f"[TokenTracker] Failed to save token usage: {e}")

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
            os.getenv("OPENROUTER_PRIMARY_MODEL") or 
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
                        INSERT INTO ai_parsing_token_usage 
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
            os.getenv("OPENROUTER_PRIMARY_MODEL") or 
            os.getenv("GROQ_MODEL") or 
            os.getenv("LLM_PROVIDER") or 
            "unknown"
        )
        
    try:
        engine = get_db_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO ai_parsing_token_usage 
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
