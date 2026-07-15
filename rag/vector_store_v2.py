"""
Vector Store — handles all vectorization and MongoDB vector similarity cache.
Core of the anti-repeat-SQL system: embeds questions and caches answers
per-role using cosine similarity with a configurable threshold.

NOTE: Switched from OpenAI text-embedding-3-small to local SentenceTransformer
(all-MiniLM-L6-v2) since OpenAI key is unavailable. Embeddings are now 100% local.
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

from db.database_mongo import get_vector_cache_collection
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local embedding model (SentenceTransformer — no API key required)
# ---------------------------------------------------------------------------
_local_embedder = None


def _get_embedder():
    """Lazy-load the local SentenceTransformer model (384-dim)."""
    global _local_embedder
    if _local_embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            _local_embedder = SentenceTransformer("all-MiniLM-L6-v2")
            logger.info("[VectorStore] Local embedding model loaded (all-MiniLM-L6-v2)")
        except Exception as exc:
            logger.error(f"[VectorStore] Failed to load local embedder: {exc}")
            raise
    return _local_embedder


# ---------------------------------------------------------------------------
# Embedding helper (sync model wrapped for async callers)
# ---------------------------------------------------------------------------
async def embed_question(question: str) -> list[float]:
    """
    Generate a local 384-dim embedding for a question.
    Runs the SentenceTransformer encode in a thread pool so it doesn’t block the event loop.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    embedder = _get_embedder()
    normalised = question.strip().lower()
    # Run CPU-bound encode in thread pool to avoid blocking async event loop
    embedding = await loop.run_in_executor(None, lambda: embedder.encode(normalised, normalize_embeddings=True).tolist())
    return embedding


# ---------------------------------------------------------------------------
# Cache lookup via Atlas Vector Search
# ---------------------------------------------------------------------------
async def get_cached_answer(
    question: str, role: str
) -> Optional[dict]:
    """
    Search for a semantically similar cached answer.
    Uses MongoDB Atlas Vector Search (index: question_vector_index).
    Returns {answer, chart_data, sql_executed, score, hit_count} or None.
    """
    try:
        vector = await embed_question(question)
    except Exception as e:
        logger.error(f"[VectorStore] Embedding failed — skipping cache: {e}")
        return None

    threshold = float(os.getenv("CACHE_THRESHOLD", "0.92"))
    col = get_vector_cache_collection()

    pipeline = [
        {
            "$vectorSearch": {
                "index": "question_vector_index",
                "path": "question_embedding",
                "queryVector": vector,
                "numCandidates": 50,
                "limit": 3,
            }
        },
        {
            "$addFields": {"score": {"$meta": "vectorSearchScore"}}
        },
        {
            "$match": {
                "score": {"$gte": threshold},
                "role_scope": role,
            }
        },
        {"$limit": 1},
    ]

    try:
        cursor = col.aggregate(pipeline)
        results = await cursor.to_list(length=1)
    except Exception as e:
        logger.error(f"[VectorStore] Atlas vector search failed: {e}")
        return None

    if not results:
        return None

    match = results[0]

    # Task 8: Skip cache entries that were flagged as low-quality by user feedback
    if match.get("bypass_cache"):
        logger.info(f"[VectorStore] Cache entry flagged bypass_cache=True — skipping for fresh LLM call")
        return None

    # Increment hit_count and update last_accessed (background)
    try:
        await col.update_one(
            {"_id": match["_id"]},
            {
                "$inc": {"hit_count": 1},
                "$set": {"last_accessed": datetime.utcnow()},
            },
        )
    except Exception as e:
        logger.warning(f"[VectorStore] Hit-count update failed: {e}")

    return {
        "answer": match.get("answer", ""),
        "chart_data": match.get("chart_data"),
        "sql_executed": match.get("sql_executed"),
        "navigate_to": match.get("navigate_to"),
        "navigation_links": match.get("navigation_links"),
        "export_data": match.get("export_data"),
        "auto_expand": match.get("auto_expand", False),
        "suggested_questions": match.get("suggested_questions"),
        "report_intent": match.get("report_intent"),
        "score": match.get("score", 0.0),
        "hit_count": match.get("hit_count", 0) + 1,
    }



# ---------------------------------------------------------------------------
# Store a new cache entry
# ---------------------------------------------------------------------------
async def store_vector_cache(
    question: str,
    answer: str,
    chart_data: Optional[dict],
    sql_executed: Optional[str],
    role: str,
    navigate_to: Optional[str] = None,
    navigation_links: Optional[list] = None,
    export_data: Optional[dict] = None,
    auto_expand: bool = False,
    suggested_questions: Optional[list] = None,
    report_intent: Optional[str] = None,
) -> str:
    """
    Embed the question and store the full document in ai_vector_cache.
    Returns the inserted _id as a string.
    """
    try:
        embedding = await embed_question(question)
    except Exception as e:
        logger.warning(f"[VectorStore] Skipping cache store because embedding failed: {e}")
        return ""

    doc = {
        "question": question,
        "question_embedding": embedding,
        "answer": answer,
        "chart_data": chart_data,
        "sql_executed": sql_executed,
        "navigate_to": navigate_to,
        "navigation_links": navigation_links,
        "export_data": export_data,
        "auto_expand": auto_expand,
        "suggested_questions": suggested_questions,
        "report_intent": report_intent,
        "role_scope": role,
        "hit_count": 0,
        "last_accessed": datetime.utcnow(),
        "created_at": datetime.utcnow(),
    }

    col = get_vector_cache_collection()
    result = await col.insert_one(doc)
    logger.info(f"[VectorStore] Cached answer for role={role}: {question[:60]}…")
    return str(result.inserted_id)


# ---------------------------------------------------------------------------
# Invalidate cache entries by keywords
# ---------------------------------------------------------------------------
async def invalidate_by_keywords(keywords: list[str]) -> int:
    """
    Delete cached entries whose question contains any of the keywords
    (case-insensitive regex match). Returns total count deleted.

    Use case: invalidate_by_keywords(["invoice", "revenue", "payment"])
    """
    col = get_vector_cache_collection()
    total_deleted = 0

    for keyword in keywords:
        try:
            result = await col.delete_many(
                {"question": {"$regex": keyword, "$options": "i"}}
            )
            total_deleted += result.deleted_count
        except Exception as e:
            logger.error(f"[VectorStore] Keyword invalidation failed for '{keyword}': {e}")

    logger.info(
        f"[VectorStore] Invalidated {total_deleted} entries for keywords: {keywords}"
    )
    return total_deleted
