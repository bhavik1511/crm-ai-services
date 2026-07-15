"""
MongoDB connection module using motor (async driver).
Singleton pattern — one shared client instance across the app.
Works with both localhost (dev) and Atlas (prod) based on env vars.
"""

import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton client + database
# ---------------------------------------------------------------------------
_mongo_client: AsyncIOMotorClient | None = None
_mongo_db = None


def get_mongo_db():
    """
    Returns the MongoDB database instance (singleton).
    Uses MONGODB_URI and MONGODB_DB_NAME from environment.
    Connection pool size adapts to ENVIRONMENT (dev vs prod).
    """
    global _mongo_client, _mongo_db

    if _mongo_db is not None:
        return _mongo_db

    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB_NAME", "crm_ai")
    environment = os.getenv("ENVIRONMENT", "development").lower()

    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. Cannot connect to MongoDB."
        )

    # Pool sizing based on environment
    pool_size = 50 if environment == "production" else 10

    _mongo_client = AsyncIOMotorClient(
        uri,
        maxPoolSize=pool_size,
        minPoolSize=1,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )

    _mongo_db = _mongo_client[db_name]

    logger.info(
        f"[MongoDB] Connected to '{db_name}' | env={environment} | pool={pool_size}"
    )

    return _mongo_db


# ---------------------------------------------------------------------------
# Convenience collection accessors
# ---------------------------------------------------------------------------
def get_sessions_collection():
    """ai_chat_sessions collection."""
    return get_mongo_db().ai_chat_sessions


def get_vector_cache_collection():
    """ai_vector_cache collection."""
    return get_mongo_db().ai_vector_cache


def get_chat_history_collection():
    """ai_chat_history collection."""
    return get_mongo_db().ai_chat_history


def get_audit_log_collection():
    """mcp_audit_log collection."""
    return get_mongo_db().mcp_audit_log


def get_knowledge_collection():
    """ai_knowledge_base collection — used by the RAG pipeline for business logic retrieval."""
    return get_mongo_db().ai_knowledge_base

