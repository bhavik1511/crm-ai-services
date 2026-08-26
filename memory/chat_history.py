"""
Chat History — permanent per-user chat history in MongoDB.

RBAC RULE: every query MUST filter by user_id.
A user can NEVER see another user's records. No exceptions.
"""

import logging
from datetime import datetime
from typing import Optional

from db.database_mongo import get_chat_history_collection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required fields for validation
# ---------------------------------------------------------------------------
_REQUIRED_FIELDS = {"user_id", "question", "answer", "session_id", "timestamp"}


# ---------------------------------------------------------------------------
# Save a chat entry
# ---------------------------------------------------------------------------
async def save_chat_entry(entry: dict) -> str:
    """
    Validate and insert a chat history document.
    Returns the inserted _id as a string.
    """
    missing = _REQUIRED_FIELDS - set(entry.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    col = get_chat_history_collection()
    result = await col.insert_one(entry)
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Get user history (paginated, searchable, date-filterable)
# ---------------------------------------------------------------------------
async def get_user_history(
    user_id: int,
    limit: int = 50,
    skip: int = 0,
    search_query: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """
    Retrieve paginated chat history for a specific user.
    ALWAYS filters by user_id first.

    Returns: {"entries": [...], "total": int, "has_more": bool}
    """
    # ALWAYS start with user_id — non-negotiable
    query: dict = {"user_id": user_id}

    # Full-text search on question + answer fields
    if search_query:
        query["$text"] = {"$search": search_query}

    # Date range filter
    if date_from or date_to:
        ts_filter = {}
        if date_from:
            ts_filter["$gte"] = date_from
        if date_to:
            ts_filter["$lte"] = date_to
        query["timestamp"] = ts_filter

    col = get_chat_history_collection()

    # Total count with same filter
    total = await col.count_documents(query)

    # Fetch entries — newest first
    cursor = (
        col.find(query)
        .sort("timestamp", -1)
        .skip(skip)
        .limit(limit)
    )
    entries = await cursor.to_list(length=limit)

    # Stringify ObjectIds for JSON transport
    for entry in entries:
        entry["_id"] = str(entry["_id"])
        if isinstance(entry.get("timestamp"), datetime):
            entry["timestamp"] = entry["timestamp"].isoformat()

    has_more = (skip + limit) < total

    return {
        "entries": entries,
        "total": total,
        "has_more": has_more,
    }


# ---------------------------------------------------------------------------
# Get history for a specific session (with user_id guard)
# ---------------------------------------------------------------------------
async def get_session_history(
    session_id: str, user_id: int
) -> list[dict]:
    """
    Retrieve all chat entries for a session, filtered by user_id.
    The user_id check is mandatory — prevents session-ID guessing attacks.
    """
    col = get_chat_history_collection()
    cursor = (
        col.find({"session_id": session_id, "user_id": user_id})
        .sort("timestamp", 1)
    )
    entries = await cursor.to_list(length=200)

    for entry in entries:
        entry["_id"] = str(entry["_id"])
        if isinstance(entry.get("timestamp"), datetime):
            entry["timestamp"] = entry["timestamp"].isoformat()

    return entries


# ---------------------------------------------------------------------------
# Delete user history
# ---------------------------------------------------------------------------
async def delete_user_history(
    user_id: int,
    session_id: Optional[str] = None,
    entry_id: Optional[str] = None,
    clear_all: bool = False
) -> int:
    """
    Delete chat history entries safely.
    - If entry_id provided: delete only that single document by ObjectId.
    - If session_id provided: delete only that specific session's entries for user_id.
    - If clear_all=True: delete ALL records for user_id.
    - If invalid or empty session_id/entry_id and clear_all=False: safety guard aborts and returns 0.
    Returns count of deleted documents.
    """
    # Sanitize inputs against stringified "null", "undefined", "none", or empty strings
    if session_id and str(session_id).strip().lower() in ("none", "null", "undefined", ""):
        session_id = None
    if entry_id and str(entry_id).strip().lower() in ("none", "null", "undefined", ""):
        entry_id = None

    if entry_id:
        from bson import ObjectId
        try:
            query: dict = {"user_id": user_id, "_id": ObjectId(entry_id)}
        except Exception:
            logger.warning(f"[ChatHistory Guard] Invalid ObjectId format for entry_id={entry_id}")
            return 0
    elif session_id:
        query: dict = {"user_id": user_id, "session_id": str(session_id).strip()}
    elif clear_all:
        query: dict = {"user_id": user_id}
    else:
        logger.warning(
            f"[ChatHistory Guard] delete_user_history invoked without valid session_id, entry_id, or clear_all=True for user_id={user_id}. Aborting to prevent full deletion."
        )
        return 0

    col = get_chat_history_collection()
    result = await col.delete_many(query)

    logger.info(
        f"[ChatHistory] Deleted {result.deleted_count} entries for user_id={user_id}"
        + (f", entry_id={entry_id}" if entry_id else (f", session={session_id}" if session_id else " (ALL history)"))
    )
    return result.deleted_count
