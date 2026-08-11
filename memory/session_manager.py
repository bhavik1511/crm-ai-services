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
# Constants
# ---------------------------------------------------------------------------
MAX_MESSAGES = 20  # Rolling message window per session

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

    user_context must contain:
        user_id, employee_id, role, hierarchy_level,
        department_id, service_line_id
    """
    user_id = user_context["user_id"]
    hierarchy_level = user_context.get("hierarchy_level", 4)

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
        # --- Executive Memory Context (Phase 3.1.10) ---
        "executive_memory": {
            "active_topic": None,
            "active_reports": [],
            "active_filters": {},
            "active_entities": [],
            "presentation_mode": None,
            "comparison_baseline": None,
            "discussion_focus": None,
            "last_capability_ids": [],
        },
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


async def restore_session_from_history(
    session_id: str,
    user_id: Optional[int] = None,
    user_context: Optional[dict] = None,
) -> Optional[dict]:
    """
    Auto-restore and reactivate an expired or missing session from permanent MongoDB chat_history.
    Reconstructs rolling message history and upserts into active sessions (MongoDB + Redis).
    Enforces user_id matching if user_id is provided.
    """
    try:
        from db.database_mongo import get_chat_history_collection
        history_col = get_chat_history_collection()
        query = {"session_id": session_id}
        if user_id is not None:
            query["user_id"] = user_id

        cursor = history_col.find(query).sort("timestamp", 1)
        history_entries = await cursor.to_list(length=500)

        if not history_entries:
            return None

        messages = []
        for entry in history_entries:
            q = entry.get("question")
            a = entry.get("answer")
            ts = entry.get("timestamp")
            if q:
                messages.append({"role": "user", "content": q, "timestamp": ts})
            if a:
                messages.append({"role": "assistant", "content": a, "timestamp": ts})

        if len(messages) > MAX_MESSAGES:
            messages = messages[-MAX_MESSAGES:]

        last_entry = history_entries[-1]
        resolved_user_id = user_id if user_id is not None else last_entry.get("user_id", 0)
        user_ctx = user_context or {}
        hierarchy_level = user_ctx.get("hierarchy_level", 4)
        ttl = await get_ttl_seconds(hierarchy_level)
        now = datetime.utcnow()

        session_doc = {
            "_id": session_id,
            "user_id": resolved_user_id,
            "employee_id": user_ctx.get("employee_id", resolved_user_id),
            "role": user_ctx.get("role", "Staff"),
            "hierarchy_level": hierarchy_level,
            "department_id": user_ctx.get("department_id"),
            "service_line_id": user_ctx.get("service_line_id"),
            "messages": messages,
            "created_at": now,
            "last_active": now,
            "ttl_expires": now + timedelta(seconds=ttl),
            "is_expired": False,
            # --- Executive Memory Context (Phase 3.1.10) ---
            "executive_memory": {
                "active_topic": None,
                "active_reports": [],
                "active_filters": {},
                "active_entities": [],
                "presentation_mode": None,
                "comparison_baseline": None,
                "discussion_focus": None,
                "last_capability_ids": [],
            },
        }

        col = get_sessions_collection()
        await col.replace_one({"_id": session_id}, session_doc, upsert=True)

        try:
            redis = get_redis()
            await redis.setex(
                f"session:{session_id}",
                ttl,
                _serialize_doc(session_doc),
            )
        except Exception as e:
            logger.warning(f"[Session] Redis write during restore failed: {e}")

        logger.info(f"[Session] Restored session {session_id} from chat history ({len(messages)} messages restored)")
        return session_doc
    except Exception as e:
        logger.error(f"[Session] Failed to restore session {session_id} from history: {e}")
        return None


async def get_session(
    session_id: str,
    user_id: Optional[int] = None,
    user_context: Optional[dict] = None,
) -> Optional[dict]:
    """
    Retrieve a session. Checks Redis first, then MongoDB fallback.
    If session is missing or expired, attempts auto-restoration from permanent chat_history.
    Returns None if session cannot be retrieved or restored.
    """
    redis = get_redis()

    # --- Tier 1: Redis ---
    try:
        cached = await redis.get(f"session:{session_id}")
        if cached:
            doc = _deserialize_doc(cached)
            if user_id is not None and doc.get("user_id") != user_id:
                logger.warning(f"[Session] User mismatch for session {session_id}")
                return None
            if not doc.get("is_expired", False):
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
            if user_id is not None and doc.get("user_id") != user_id:
                logger.warning(f"[Session] User mismatch for session {session_id}")
                return None

            remaining = (doc["ttl_expires"] - datetime.utcnow()).total_seconds()
            remaining = max(int(remaining), 60)
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

    # --- Tier 3: Auto-restore from chat history ---
    return await restore_session_from_history(session_id, user_id=user_id, user_context=user_context)


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


# ---------------------------------------------------------------------------
# Clarification state management
# ---------------------------------------------------------------------------
async def save_clarification_state(session_id: str, clar_dto: dict) -> bool:
    """Save clarification state DTO in Redis and MongoDB."""
    try:
        redis = get_redis()
        await redis.setex(
            f"clarification:{session_id}",
            3600,  # 1 hour TTL for pending clarification
            _serialize_doc(clar_dto)
        )
    except Exception as e:
        logger.warning(f"[Session] Redis save clarification failed: {e}")

    try:
        col = get_sessions_collection()
        await col.update_one(
            {"_id": session_id},
            {"$set": {"clarification_state": clar_dto}}
        )
        return True
    except Exception as e:
        logger.error(f"[Session] MongoDB save clarification failed: {e}")
        return False


async def get_clarification_state(session_id: str) -> Optional[dict]:
    """Get clarification state DTO for session."""
    try:
        redis = get_redis()
        cached = await redis.get(f"clarification:{session_id}")
        if cached:
            return _deserialize_doc(cached)
    except Exception as e:
        logger.warning(f"[Session] Redis get clarification failed: {e}")

    try:
        col = get_sessions_collection()
        doc = await col.find_one({"_id": session_id})
        if doc:
            return doc.get("clarification_state")
    except Exception as e:
        logger.error(f"[Session] MongoDB get clarification failed: {e}")
    return None


async def clear_clarification_state(session_id: str) -> bool:
    """Clear clarification state DTO for session."""
    try:
        redis = get_redis()
        await redis.delete(f"clarification:{session_id}")
    except Exception as e:
        logger.warning(f"[Session] Redis clear clarification failed: {e}")

    try:
        col = get_sessions_collection()
        await col.update_one(
            {"_id": session_id},
            {"$unset": {"clarification_state": ""}}
        )
        return True
    except Exception as e:
        logger.error(f"[Session] MongoDB clear clarification failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Entity-Aware Cache Functions
# ---------------------------------------------------------------------------
def build_entity_cache_key(
    user_id: int,
    capability: str,
    resolved_entities: list = None,
    financial_year: str = "",
    filters: dict = None
) -> str:
    """
    Build a deterministic cache key for entity-aware queries based on:
    user_id, capability, resolved entities, financial_year, and extra filters.
    """
    import hashlib
    ent_parts = []
    for ent in (resolved_entities or []):
        if isinstance(ent, dict):
            e_type = str(ent.get("entity_type") or ent.get("type") or "").lower()
            e_id = str(ent.get("entity_id") or ent.get("id") or ent.get("value") or "").lower()
            ent_parts.append(f"{e_type}:{e_id}")
    ent_str = "|".join(sorted(ent_parts))

    filter_parts = []
    if filters and isinstance(filters, dict):
        for k, v in sorted(filters.items()):
            if v and k not in ("raw_tool_results", "previous_execution_plan"):
                filter_parts.append(f"{k}:{v}")
    filter_str = "|".join(filter_parts)

    raw_key = f"u:{user_id}|cap:{capability}|fy:{financial_year}|ent:{ent_str}|flt:{filter_str}"
    digest = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
    return f"entity_cache:{digest}"


async def get_entity_cache(cache_key: str) -> Optional[dict]:
    """Retrieve cached response from Redis for an entity-aware query."""
    try:
        redis = get_redis()
        cached = await redis.get(cache_key)
        if cached:
            doc = _deserialize_doc(cached)
            if isinstance(doc, dict):
                content = str(doc.get("content", "")).lower()
                if any(phrase in content for phrase in [
                    "currently unavailable", "please try again later", "failed", 
                    "does not include", "not available", "customer-specific", "bhd 0", "at 0%"
                ]):
                    logger.info(f"[EntityCache] Ignoring stale error/incomplete cached response for key={cache_key}")
                    return None
                if "recoverability" in cache_key and not any(p in content for p in ["actual recoverability", "portfolio recoverability", "recoverability percentage"]):
                    logger.info(f"[EntityCache] Ignoring stale cached response missing recoverability percentage for key={cache_key}")
                    return None
            return doc
    except Exception as e:
        logger.warning(f"[EntityCache] Redis read failed for key={cache_key}: {e}")
    return None


async def set_entity_cache(cache_key: str, response: dict, ttl_seconds: int = 1800) -> bool:
    """Store query response in Redis entity cache (default 30 min TTL)."""
    if not response or not isinstance(response, dict):
        return False
    content = str(response.get("content", "")).lower()
    if "currently unavailable" in content or "please try again later" in content:
        logger.info(f"[EntityCache] Skipping caching of error/unavailable response for key={cache_key}")
        return False
    try:
        redis = get_redis()
        await redis.setex(
            cache_key,
            ttl_seconds,
            _serialize_doc(response)
        )
        return True
    except Exception as e:
        logger.warning(f"[EntityCache] Redis write failed for key={cache_key}: {e}")
        return False


# ---------------------------------------------------------------------------
# Executive Memory Helpers (Phase 3.1.10)
# ---------------------------------------------------------------------------

async def get_session_memory(session_id: str) -> dict:
    """
    Returns the executive_memory dict from the active session.
    Used by the Planner to inherit active filters/entities for follow-up queries.
    Returns an empty dict if session or memory is unavailable.
    """
    session = await get_session(session_id)
    if session and isinstance(session.get("executive_memory"), dict):
        return session["executive_memory"]
    return {}


async def update_session_memory(
    session_id: str,
    execution_plan: dict,
    tool_results: list = None,
) -> bool:
    """
    Persists business context from the current turn into executive_memory.
    The Planner reads this on the next turn to auto-inherit filters/entities
    for follow-up queries like 'only Audit', 'compare with last year', 'why'.
    """
    if not execution_plan or not isinstance(execution_plan, dict):
        return False

    caps = execution_plan.get("business_capabilities", [])
    cap_ids = [c.get("id") for c in caps if c.get("id")]

    merged_filters: dict = {}
    PERSISTENT_KEYS = {"financial_year", "service_line", "department", "office"}
    for cap in caps:
        ctx = cap.get("context") or {}
        for k, v in ctx.items():
            if v and k in PERSISTENT_KEYS:
                merged_filters[k] = v

    for filter_key in PERSISTENT_KEYS:
        val = execution_plan.get(filter_key)
        if val:
            merged_filters[filter_key] = val

    new_memory = {
        "active_topic": execution_plan.get("business_goal") or None,
        "active_reports": cap_ids,
        "active_filters": merged_filters,
        "active_entities": execution_plan.get("resolved_entities", []),
        "presentation_mode": execution_plan.get("presentation_mode"),
        "comparison_baseline": execution_plan.get("comparison"),
        "last_capability_ids": cap_ids,
        "discussion_focus": None,
    }

    try:
        col = get_sessions_collection()
        await col.update_one(
            {"_id": session_id},
            {"$set": {"executive_memory": new_memory}}
        )
    except Exception as e:
        logger.error(f"[ExecutiveMemory] MongoDB update failed: {e}")
        return False

    try:
        session = await get_session(session_id)
        if session:
            session["executive_memory"] = new_memory
            redis = get_redis()
            hierarchy_level = session.get("hierarchy_level", 4)
            ttl = await get_ttl_seconds(hierarchy_level)
            await redis.setex(f"session:{session_id}", ttl, _serialize_doc(session))
    except Exception as e:
        logger.warning(f"[ExecutiveMemory] Redis update failed (non-fatal): {e}")

    logger.info(
        f"[ExecutiveMemory] Updated session {session_id}: "
        f"caps={cap_ids}, filters={list(merged_filters.keys())}"
    )
    return True



