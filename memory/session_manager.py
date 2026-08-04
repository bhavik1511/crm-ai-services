"""
Session manager — stores user sessions in Redis (hot) + MongoDB (persistent).
Handles TTL by hierarchy level, session lifecycle, and message rolling window.
"""

import json
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

from db.database_mongo import get_sessions_collection
from db.database_redis import get_redis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL mapping by hierarchy level
# ---------------------------------------------------------------------------
_TTL_MAP = {
    1: 28800,   # CEO / Partner       — 8 hours
    2: 28800,   # Partner             — 8 hours (kept separate for clarity)
    3: 21600,   # Manager             — 6 hours
    4: 14400,   # Senior Associate    — 4 hours
}
_DEFAULT_TTL = 7200  # Staff / default   — 2 hours


async def get_ttl_seconds(hierarchy_level: int) -> int:
    """Returns TTL in seconds based on hierarchy level."""
    if hierarchy_level <= 2:
        return 28800
    elif hierarchy_level == 3:
        return 21600
    elif hierarchy_level == 4:
        return 14400
    else:
        return 7200


# ---------------------------------------------------------------------------
# JSON serialization helpers (datetime ↔ string)
# ---------------------------------------------------------------------------
def _serialize_doc(doc: dict) -> str:
    """Convert session document to JSON string, handling datetimes."""
    def _default(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    return json.dumps(doc, default=_default)


def _deserialize_doc(raw: str) -> dict:
    """Parse JSON string back to dict (datetimes remain as ISO strings)."""
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Core session functions
# ---------------------------------------------------------------------------
async def create_session(user_context: dict) -> str:
    """
    Create a new session for a user.
    Invalidates any existing active sessions first (1 active per user).

    user_context must contain:
        user_id, employee_id, role, hierarchy_level,
        department_id, service_line_id
    """
    user_id = user_context["user_id"]
    hierarchy_level = user_context.get("hierarchy_level", 4)

    # Invalidate existing active sessions for this user
    active_sessions = await get_active_sessions_for_user(user_id)
    for old_sid in active_sessions:
        await invalidate_session(old_sid)

    session_id = str(uuid.uuid4())
    ttl = await get_ttl_seconds(hierarchy_level)
    now = datetime.utcnow()

    session_doc = {
        "_id": session_id,
        "user_id": user_id,
        "employee_id": user_context.get("employee_id", user_id),
        "role": user_context.get("role", "Staff"),
        "hierarchy_level": hierarchy_level,
        "department_id": user_context.get("department_id"),
        "service_line_id": user_context.get("service_line_id"),
        "messages": [],
        "created_at": now,
        "last_active": now,
        "ttl_expires": now + timedelta(seconds=ttl),
        "is_expired": False,
    }

    # Write to MongoDB
    col = get_sessions_collection()
    try:
        await col.insert_one(session_doc)
    except Exception as e:
        logger.error(f"[Session] MongoDB insert failed: {e}")
        raise

    # Write to Redis with TTL (non-fatal if Redis is down)
    try:
        redis = get_redis()
        await redis.setex(
            f"session:{session_id}",
            ttl,
            _serialize_doc(session_doc),
        )
    except Exception as e:
        logger.warning(f"[Session] Redis write failed (non-fatal): {e}")

    logger.info(
        f"[Session] Created session {session_id} for user {user_id} "
        f"(role={session_doc['role']}, ttl={ttl}s)"
    )
    return session_id


async def get_session(session_id: str) -> Optional[dict]:
    """
    Retrieve a session. Checks Redis first, then MongoDB fallback.
    Returns None if session is expired or not found.
    """
    redis = get_redis()

    # --- Tier 1: Redis ---
    try:
        cached = await redis.get(f"session:{session_id}")
        if cached:
            doc = _deserialize_doc(cached)
            # Refresh TTL on access
            hierarchy_level = doc.get("hierarchy_level", 4)
            ttl = await get_ttl_seconds(hierarchy_level)
            await redis.expire(f"session:{session_id}", ttl)
            return doc
    except Exception as e:
        logger.warning(f"[Session] Redis read failed (falling back to Mongo): {e}")

    # --- Tier 2: MongoDB ---
    try:
        col = get_sessions_collection()
        doc = await col.find_one({
            "_id": session_id,
            "is_expired": False,
            "ttl_expires": {"$gt": datetime.utcnow()},
        })
        if doc:
            # Re-warm Redis with remaining TTL
            remaining = (doc["ttl_expires"] - datetime.utcnow()).total_seconds()
            remaining = max(int(remaining), 60)  # at least 60s
            try:
                await redis.setex(
                    f"session:{session_id}",
                    remaining,
                    _serialize_doc(doc),
                )
            except Exception as e:
                logger.warning(f"[Session] Redis re-warm failed: {e}")
            return doc
    except Exception as e:
        logger.error(f"[Session] MongoDB read failed: {e}")

    return None


async def append_message(
    session_id: str, role: str, content: str
) -> bool:
    """
    Append a message to the session (rolling window, max 20).
    Updates both Redis and MongoDB.
    role must be 'user' or 'assistant'.
    """
    if role not in ("user", "assistant"):
        logger.error(f"[Session] Invalid message role: {role}")
        return False

    session = await get_session(session_id)
    if not session:
        return False

    messages = session.get("messages", [])
    messages.append({
        "role": role,
        "content": content,
        "timestamp": datetime.utcnow().isoformat(),
    })
    # Cap at 20 messages (rolling window)
    if len(messages) > 20:
        messages = messages[-20:]

    now = datetime.utcnow()
    session["messages"] = messages
    session["last_active"] = now

    # Update MongoDB
    try:
        col = get_sessions_collection()
        await col.update_one(
            {"_id": session_id},
            {"$set": {"messages": messages, "last_active": now}},
        )
    except Exception as e:
        logger.error(f"[Session] MongoDB message update failed: {e}")
        return False

    # Update Redis (re-serialize full doc)
    try:
        redis = get_redis()
        hierarchy_level = session.get("hierarchy_level", 4)
        ttl = await get_ttl_seconds(hierarchy_level)
        await redis.setex(
            f"session:{session_id}",
            ttl,
            _serialize_doc(session),
        )
    except Exception as e:
        logger.warning(f"[Session] Redis message update failed (non-fatal): {e}")

    return True


async def invalidate_session(session_id: str) -> bool:
    """Invalidate a session — delete from Redis, mark expired in Mongo."""
    # Delete from Redis
    try:
        redis = get_redis()
        await redis.delete(f"session:{session_id}")
    except Exception as e:
        logger.warning(f"[Session] Redis delete failed: {e}")

    # Mark expired in MongoDB
    try:
        col = get_sessions_collection()
        await col.update_one(
            {"_id": session_id},
            {"$set": {"is_expired": True}},
        )
    except Exception as e:
        logger.error(f"[Session] MongoDB invalidate failed: {e}")
        return False

    logger.info(f"[Session] Invalidated session {session_id}")
    return True


async def get_active_sessions_for_user(user_id: int) -> list[str]:
    """Return list of active (non-expired) session IDs for a user."""
    try:
        col = get_sessions_collection()
        cursor = col.find(
            {
                "user_id": user_id,
                "is_expired": False,
                "ttl_expires": {"$gt": datetime.utcnow()},
            },
            {"_id": 1},
        )
        docs = await cursor.to_list(length=10)
        return [doc["_id"] for doc in docs]
    except Exception as e:
        logger.error(f"[Session] MongoDB active-session query failed: {e}")
        return []


async def get_session_messages(session_id: str) -> list[dict]:
    """Get the messages array from a session, or empty list if not found."""
    session = await get_session(session_id)
    if session:
        return session.get("messages", [])
    return []
