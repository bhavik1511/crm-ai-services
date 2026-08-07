"""
Serializer module — safe JSON serialization and DTO builders for session state.
"""
import json
import logging
from datetime import datetime, date
from typing import Any

logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """Fallback handler for non-standard JSON types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "dict") and callable(obj.dict):
        return obj.dict()
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not str(k).startswith("_")}
    return str(obj)


def safe_json_dumps(obj: Any) -> str:
    """Convert any object to a JSON string without raising serialization errors."""
    try:
        return json.dumps(obj, default=_json_default)
    except Exception as e:
        logger.error(f"[Serializer] safe_json_dumps fallback failed: {e}")
        return json.dumps({"error": "Serialization failure", "details": str(e)})


def build_clarification_dto(
    session_id: str,
    original_question: str,
    execution_plan: dict,
    missing_fields: list,
    resolved_entities: list = None,
    planner_context: dict = None
) -> dict:
    """
    Build a lightweight, JSON-primitive DTO for pending clarification states.
    Prevents circular reference / non-serializable object errors in session storage.
    """
    def _sanitize(val: Any) -> Any:
        if isinstance(val, (str, int, float, bool, type(None))):
            return val
        if isinstance(val, (datetime, date)):
            return val.isoformat()
        if isinstance(val, dict):
            return {str(k): _sanitize(v) for k, v in val.items() if not str(k).startswith("_")}
        if isinstance(val, (list, tuple, set)):
            return [_sanitize(x) for x in val]
        return str(val)

    return {
        "session_id": session_id,
        "original_question": original_question,
        "execution_plan": _sanitize(execution_plan or {}),
        "missing_fields": _sanitize(missing_fields or []),
        "resolved_entities": _sanitize(resolved_entities or []),
        "planner_context": _sanitize(planner_context or {}),
        "created_at": datetime.utcnow().isoformat()
    }
