import os
import json
import time
import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any

from mcp.server.fastmcp import FastMCP
import jwt
from redis.asyncio import Redis
import motor.motor_asyncio
from dotenv import load_dotenv

# Import our custom logic
from rag.vector_store_v2 import get_cached_answer, store_vector_cache
from memory import session_manager
from memory import chat_history
from agent.agent import ask_question

load_dotenv()

# Constants
JWT_SECRET = os.getenv("JWT_SECRET", "shhh_change_me")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MONGODB_URI = os.getenv("MONGODB_URI")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
AUDIT_LOG_COL = "mcp_audit_log"
HISTORY_COL = "ai_chat_history"

# Clients
mcp = FastMCP("AskAI", dependencies=["openai", "motor", "redis"])
redis = Redis.from_url(REDIS_URL, decode_responses=True)
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI) if MONGODB_URI else None
db = mongo_client.get_database("dashboard_ai") if mongo_client else None

# Helper functions
async def check_rate_limit(user_id: int) -> bool:
    """Sliding window rate limit: 20 requests per 60 seconds per user."""
    key = f"rate_limit:{user_id}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, 60)
        if current > 20:
            return False
        return True
    except Exception as e:
        print(f"[RateLimit] Redis error — allowing request: {e}")
        return True

async def audit_log(user_id: int, role: str, tool_name: str, params: dict, cache_hit: bool, latency: float):
    """Logs tool call to MongoDB audit log."""
    if not db: return
    doc = {
        "timestamp": datetime.utcnow(),
        "user_id": user_id,
        "role": role,
        "tool_name": tool_name,
        "params": params,
        "cache_hit": cache_hit,
        "latency_ms": latency
    }
    await db[AUDIT_LOG_COL].insert_one(doc)

async def save_session(session_id: str, session_doc: dict):
    if not db: return
    session_doc["last_active"] = datetime.utcnow()
    await db["ai_chat_sessions"].replace_one({"_id": session_id}, session_doc, upsert=True)
    await redis.setex(f"session:{session_id}", 7200, json.dumps(session_doc, default=str))

# Tools
@mcp.tool()
async def ask_ai(question: str, session_id: str, user_id: int, role: str) -> str:
    """Primary entry point for AI queries with semantic caching and RBAC."""
    start_time = time.time()
    
    # 1. Rate Limit
    if not await check_rate_limit(user_id):
        return "⚠️ Rate limit exceeded. Please wait a moment."
        
    # 2. Semantic Cache Check
    cache_hit = False
    cached = await get_cached_answer(question, role)
    if cached:
        cache_hit = True
        answer = cached["answer"]
    else:
        # 3. Resolve Session (Short Memory)
        session = await session_manager.get_session(session_id)
        if not session:
            session = {
                "_id": session_id,
                "user_id": user_id,
                "role": role,
                "hierarchy_level": 5,
                "messages": [],
            }
        
        # Build user context for RBAC
        user_context = {
            "user_id": user_id,
            "role": role,
            "employee_id": user_id,
            "designation_tier": 5,
        }

        # Task 9: Inject entity context from last session into user_context
        entity_ctx = session.get("entity_context") if session else None
        if entity_ctx:
            user_context["last_entity_type"] = entity_ctx.get("type")
            user_context["last_entity_name"] = entity_ctx.get("name")

        # Build history from session messages
        msgs: list = session.get("messages", []) or []
        history = [{"role": m["role"], "content": m["content"]} for m in msgs[-5:]]
        history.append({"role": "user", "content": question})

        # Result tuple from agent (10-tuple):
        # (answer, chart_data, navigate_to, nav_links, export_data, auto_expand, suggested_q, entity_name, entity_type, is_edit_intent)
        res_tuple = ask_question(history, user_context)
        answer = str(res_tuple[0])
        chart_data = res_tuple[1] if len(res_tuple) > 1 else None
        sql_used = res_tuple[2] if len(res_tuple) > 2 else None
        entity_name = res_tuple[7] if len(res_tuple) > 7 else None
        entity_type = res_tuple[8] if len(res_tuple) > 8 else None

        # 4. Store in Vector Cache
        await store_vector_cache(question, answer, chart_data, sql_used or "", role)

        # 5. Update Session Context (rolling window of last 20)
        now_ts = datetime.utcnow().isoformat()
        msgs.append({"role": "user", "content": question, "timestamp": now_ts})
        msgs.append({"role": "assistant", "content": answer, "timestamp": now_ts})
        if isinstance(session, dict):
            session["messages"] = msgs[-20:]
            # Task 9: Persist entity context for multi-turn memory
            if entity_name and entity_type:
                session["entity_context"] = {
                    "type": entity_type,
                    "name": entity_name,
                    "set_at": datetime.utcnow().isoformat(),
                }
        await save_session(session_id, session)

    # 6. Audit Logging & Cleanup
    latency = (time.time() - start_time) * 1000
    await audit_log(user_id, role, "ask_ai", {"question": question}, cache_hit, latency)
    try:
        await chat_history.save_chat_entry({
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "question": question,
            "answer": answer,
            "timestamp": datetime.utcnow()
        })
    except Exception as e:
        print(f"[Audit] Failed to log chat history: {e}")
    
    return answer

@mcp.tool()
async def get_chat_history(user_id: int, role: str, limit: int = 20) -> List[dict]:
    """Returns the user's personal chat history from MongoDB."""
    if not db: return []
    cursor = db[HISTORY_COL].find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    history = await cursor.to_list(length=limit)
    # Convert ObjectIds to strings for MCP transport
    for doc in history: doc["_id"] = str(doc["_id"]); doc["created_at"] = doc["created_at"].isoformat()
    return history

@mcp.tool()
async def clear_session(session_id: str) -> bool:
    """Wipes the current session from Redis and MongoDB TTL."""
    await redis.delete(f"session:{session_id}")
    return True

@mcp.tool()
async def get_suggestions(user_id: int, role: str) -> List[str]:
    """Returns the last 5 unique questions as suggested prompts."""
    if not db: return []
    cursor = db[HISTORY_COL].find({"user_id": user_id}).sort("created_at", -1).limit(50)
    history = await cursor.to_list(length=50)
    
    unique_q = []
    seen = set()
    for doc in history:
        q = doc["question"]
        if q not in seen:
            unique_q.append(q)
            seen.add(q)
            if len(unique_q) == 5: break
    return unique_q

# --- NEW TOOLS (Task 6) — imported from tools_new.py ---
from agent.tools_new import (
    get_dashboard_snapshot as _snapshot_fn,
    get_anomaly_alerts as _alerts_fn,
    compare_periods as _compare_fn,
    get_entity_profile as _entity_fn,
    submit_feedback as _feedback_fn,
    handle_edit_intent as _edit_fn,
)

@mcp.tool()
async def get_dashboard_snapshot(user_id: int, role: str) -> dict:
    """Returns a full role-scoped KPI snapshot: revenue, receivables, pipeline, and projects in one call."""
    return await _snapshot_fn(user_id, role)

@mcp.tool()
async def get_anomaly_alerts(user_id: int, role: str) -> list:
    """Scans the live database for business anomalies: overdue invoices, stalled proposals, cold leads, overdue tasks."""
    return await _alerts_fn(user_id, role)

@mcp.tool()
async def compare_periods(metric: str, period_a_start: str, period_a_end: str, period_b_start: str, period_b_end: str, user_id: int, role: str) -> dict:
    """Compare a metric (revenue/receivables/proposals/leads) across two date ranges. Returns delta, percentage change, and chart data."""
    return await _compare_fn(metric, period_a_start, period_a_end, period_b_start, period_b_end, user_id, role)

@mcp.tool()
async def get_entity_profile(entity_type: str, entity_name: str, user_id: int, role: str) -> dict:
    """Fetch a rich 360-degree profile for a customer, project, or employee by name or code."""
    return await _entity_fn(entity_type, entity_name, user_id, role)

@mcp.tool()
async def submit_feedback(question: str, feedback: str, user_id: int, role: str, comment: str = None) -> dict:
    """Submit positive or negative feedback on an AI answer. Negative feedback bypasses cache on the next similar query."""
    return await _feedback_fn(question, feedback, user_id, role, comment)

@mcp.tool()
async def handle_edit_intent(entity_type: str, entity_name: str, user_id: int, role: str) -> dict:
    """Called when the user expresses intent to edit a CRM record. Returns current data and a confirmation payload for the frontend."""
    return await _edit_fn(entity_type, entity_name, user_id, role)


if __name__ == "__main__":
    import uvicorn
    print(f"Starting MCP Server (SSE) on port {MCP_PORT}...")
    # Get the SSE app from FastMCP and run it with uvicorn on a custom port
    app = mcp.sse_app()
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
