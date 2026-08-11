"""
structured_logger.py — Centralized Structured Logger & Trace Context Manager
=============================================================================
Provides per-request Trace ID propagation via ContextVars and unified
structured stage logging across the Enterprise Hybrid Intelligence Engine.
"""

import os
import time
import random
import string
import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional, List

logger = logging.getLogger("CRM.AI")

# Async context variable for per-request Trace ID
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

def generate_trace_id() -> str:
    """Generates a trace ID in format AI-YYYYMMDD-XXXXXX."""
    date_str = time.strftime("%Y%m%d")
    rand_str = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"AI-{date_str}-{rand_str}"

def set_trace_id(trace_id: Optional[str] = None) -> str:
    """Sets or generates the Trace ID for the current request context."""
    if not trace_id:
        trace_id = generate_trace_id()
    trace_id_var.set(trace_id)
    return trace_id

def get_trace_id() -> str:
    """Retrieves the active Trace ID for the current request context."""
    tid = trace_id_var.get()
    if not tid:
        tid = generate_trace_id()
        trace_id_var.set(tid)
    return tid

def mask_jwt(token: Optional[str]) -> str:
    """Masks sensitive JWT tokens or auth credentials for logging."""
    if not token:
        return "None"
    return "Present"

def _format_kv(kwargs: Dict[str, Any]) -> str:
    """Formats key-value pairs cleanly for single-line structured logging."""
    items = []
    for k, v in kwargs.items():
        if v is None:
            items.append(f"{k}=None")
        elif isinstance(v, bool):
            items.append(f"{k}={v}")
        elif isinstance(v, (int, float)):
            items.append(f"{k}={v}")
        elif isinstance(v, str):
            # Escape newlines or spaces if needed, or wrap in quotes if spaces present
            clean_v = v.replace("\n", " ").strip()
            if " " in clean_v and len(clean_v) > 30:
                clean_v = f"'{clean_v[:30]}...'"
            elif " " in clean_v:
                clean_v = f"'{clean_v}'"
            items.append(f"{k}={clean_v}")
        else:
            items.append(f"{k}={v}")
    return " ".join(items)

def log_stage(target_logger: logging.Logger, stage: str, **kwargs):
    """
    Logs a single-line structured stage event at INFO level.
    Format: [STAGE] TraceId=... key1=val1 key2=val2
    """
    tid = get_trace_id()
    kv_str = _format_kv(kwargs)
    tag = f"[{stage.upper()}]"
    msg = f"{tag:<14} TraceId={tid} {kv_str}"
    target_logger.info(msg)

def log_error(target_logger: logging.Logger, stage: str, error_msg: str, **kwargs):
    """
    Logs a structured error event at ERROR level.
    Format: [ERROR] TraceId=... Stage=... Error='...'
    """
    tid = get_trace_id()
    kwargs["Stage"] = stage.upper()
    kwargs["Error"] = str(error_msg)[:200]
    kv_str = _format_kv(kwargs)
    msg = f"[ERROR]        TraceId={tid} {kv_str}"
    target_logger.error(msg)

def log_summary(target_logger: logging.Logger, **kwargs):
    """
    Logs the final one-line summary event at INFO level.
    Format: [FINAL] TraceId=...
    """
    tid = get_trace_id()
    kv_str = _format_kv(kwargs)
    msg = f"[FINAL]        TraceId={tid} {kv_str}"
    target_logger.info(msg)

def log_debug_payload(target_logger: logging.Logger, tag: str, payload: Any, max_rows: int = 3):
    """
    Logs raw backend payloads ONLY if log level is DEBUG or lower.
    Truncates list/record sets to max_rows.
    """
    if not target_logger.isEnabledFor(logging.DEBUG):
        return

    tid = get_trace_id()
    if isinstance(payload, dict):
        records = payload.get("records") or payload.get("data")
        if isinstance(records, list):
            total_cnt = len(records)
            truncated = records[:max_rows]
            target_logger.debug(f"[DEBUG_PAYLOAD] TraceId={tid} Tag={tag} TotalRows={total_cnt} Showing={len(truncated)} Sample={truncated}")
            return
    target_logger.debug(f"[DEBUG_PAYLOAD] TraceId={tid} Tag={tag} Content={str(payload)[:500]}")
