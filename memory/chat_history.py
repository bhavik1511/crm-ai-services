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
    user_id: int, session_id: Optional[str] = None
) -> int:
    """
    Delete chat history entries.
    - If session_id provided: delete only that session's entries for user_id.
    - If no session_id: delete ALL records for user_id.
    Returns count of deleted documents.
    """
    query: dict = {"user_id": user_id}
    if session_id:
        query["session_id"] = session_id

    col = get_chat_history_collection()
    result = await col.delete_many(query)

    logger.info(
        f"[ChatHistory] Deleted {result.deleted_count} entries for "
        f"user_id={user_id}" + (f", session={session_id}" if session_id else "")
    )
    return result.deleted_count
