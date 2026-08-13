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
        try:
            conn.execute(text("ALTER TABLE ai_email_ml_dataset ADD COLUMN review_count INT DEFAULT 1;"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE ai_email_ml_dataset ADD COLUMN discard_count INT DEFAULT 0;"))
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
                        "cost": total_cost_usd
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


import json

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
    confidence_score: int = 90
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
                conn.execute(
                    text("""
                        INSERT INTO ai_email_ml_dataset 
                        (reference_id, sender_email, to_emails, subject, body_clean, thread_count, is_forwarded, forwarded_by_email, predicted_intent, extracted_keywords, confidence_score)
                        VALUES (:ref_id, :sender, :to_e, :subj, :body, :t_cnt, :is_fwd, :fwd_by, :intent, :kw, :conf)
                        ON DUPLICATE KEY UPDATE
                        sender_email=VALUES(sender_email), to_emails=VALUES(to_emails), subject=VALUES(subject),
                        body_clean=VALUES(body_clean), predicted_intent=VALUES(predicted_intent),
                        extracted_keywords=VALUES(extracted_keywords),
                        confidence_score=VALUES(confidence_score)
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
                        "conf": confidence_score
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
                res = conn.execute(
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
                        WHERE reference_id = :ref_id
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

                if res.rowcount == 0:
                    # Row didn't exist for this reference_id — INSERT it directly!
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



