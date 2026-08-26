"""
RAG Knowledge Store (Local ChromaDB + HuggingFace Embeddings)
=============================================================
Provides vector-similarity search over a local ChromaDB collection.
No paid API required — embeddings run 100% locally on this machine.

Model used: all-MiniLM-L6-v2 (384-dim, fast, accurate for English business text)
Database  : ChromaDB (persistent on local disk at ./chroma_db)

Flow when the agent calls `search_backend_docs(query)`:
  1. Embed the query locally using SentenceTransformer.
  2. Query ChromaDB collection for the top-k most similar chunks.
  3. Apply RBAC filter: hide chunks whose min_tier > user_tier.
  4. Return the most relevant chunks as formatted text ready for the LLM prompt.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_RAG_DIR = Path(__file__).resolve().parent
_CHROMA_DIR = os.getenv("CHROMA_DB_DIR", str(_RAG_DIR / "chroma_db"))
_COLLECTION_NAME = "crm_knowledge_base"
_EMBED_MODEL = "all-MiniLM-L6-v2"
_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.30"))   # ChromaDB returns distance (lower=closer); 0.30 is a good cutoff


# ---------------------------------------------------------------------------
# Lazy singletons — load only when first needed (saves memory at startup)
# ---------------------------------------------------------------------------
_embedder = None
_chroma_client = None
_chroma_collection = None


def _get_embedder():
    """Load the local SentenceTransformer model (cached after first call)."""
    global _embedder
    if _embedder is None:
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("[RAG] Loading local embedding model: %s", _EMBED_MODEL)
            _embedder = SentenceTransformer(_EMBED_MODEL)
            logger.info("[RAG] Embedding model loaded successfully.")
        except Exception as exc:
            logger.error("[RAG] Failed to load embedding model: %s", exc)
            raise
    return _embedder


def _get_collection():
    """Return the ChromaDB collection (create/load on first call)."""
    global _chroma_client, _chroma_collection
    if _chroma_collection is None:
        try:
            import chromadb
            logger.info("[RAG] Connecting to ChromaDB at: %s", _CHROMA_DIR)
            _chroma_client = chromadb.PersistentClient(path=_CHROMA_DIR)
            _chroma_collection = _chroma_client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},  # cosine similarity
            )
            doc_count = _chroma_collection.count()
            logger.info("[RAG] ChromaDB collection '%s' loaded — %d chunks.", _COLLECTION_NAME, doc_count)
        except Exception as exc:
            logger.error("[RAG] Failed to connect to ChromaDB: %s", exc)
            raise
    return _chroma_collection


# ---------------------------------------------------------------------------
# Core search function (called by agent.py search_backend_docs tool)
# ---------------------------------------------------------------------------
async def search_knowledge(
    query: str,
    user_tier: int = 1,
    top_k: int = _TOP_K,
) -> str:
    """
    Perform a vector similarity search over the local ChromaDB knowledge base.

    Parameters
    ----------
    query     : The user's natural-language question or search keywords.
    user_tier : RBAC tier of the logged-in user.
                Chunks whose stored `min_tier` > user_tier are filtered out
                BEFORE returning results (access control enforcement).
    top_k     : Max number of chunks to include in the returned context.

    Returns
    -------
    A formatted string of the most relevant documentation chunks ready to be
    injected as context into the LLM prompt. Returns a fallback message if the
    collection is empty or no relevant results are found.
    """
    # -- Guard: collection must have data --
    try:
        collection = _get_collection()
    except Exception as exc:
        logger.warning("[RAG] ChromaDB unavailable: %s", exc)
        return "Knowledge base temporarily unavailable."

    doc_count = collection.count()
    if doc_count == 0:
        logger.warning("[RAG] Knowledge base is empty. Run `python ingest_knowledge.py` first.")
        return (
            "Knowledge base is not yet populated. "
            "Please run `python ingest_knowledge.py` to ingest business logic documentation."
        )

    # -- Embed the query locally --
    try:
        embedder = _get_embedder()
        query_vector = embedder.encode(query, normalize_embeddings=True).tolist()
    except Exception as exc:
        logger.error("[RAG] Local embedding failed: %s", exc)
        return "Knowledge base temporarily unavailable — embedding model error."

    # -- Query ChromaDB --
    # We fetch extra results then apply our RBAC + score filters
    fetch_k = min(top_k * 3, doc_count)
    try:
        results = collection.query(
            query_embeddings=[query_vector],
            n_results=fetch_k,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        logger.error("[RAG] ChromaDB query failed: %s", exc)
        return "Knowledge base search failed. ChromaDB query error."

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    if not documents:
        return f"No relevant documentation found for: '{query}'"

    # -- Filter by RBAC tier and relevance score --
    filtered: list[dict] = []
    for doc, meta, dist in zip(documents, metadatas, distances):
        # ChromaDB cosine distance: 0=identical, 2=opposite. Convert to similarity score.
        similarity = 1.0 - (dist / 2.0)

        # Skip if below relevance threshold
        if similarity < _MIN_SCORE:
            continue

        # RBAC: chunk min_tier must be <= user_tier (e.g., tier 1 is the most privileged)
        chunk_min_tier = meta.get("min_tier", 1)
        if chunk_min_tier < user_tier:   # chunk requires higher privilege than user has
            continue

        filtered.append({
            "text": doc,
            "module": meta.get("source_module", "General"),
            "section": meta.get("section_title", ""),
            "score": similarity,
        })

        if len(filtered) >= top_k:
            break

    if not filtered:
        return f"No relevant documentation found for: '{query}'"

    # -- Build a clean context block for the LLM --
    lines: list[str] = [
        f"=== RETRIEVED KNOWLEDGE ({len(filtered)} relevant sections for: '{query}') ===\n"
    ]
    for i, chunk in enumerate(filtered, 1):
        header = f"[{i}] {chunk['module']}"
        if chunk["section"]:
            header += f" › {chunk['section']}"
        header += f"  (relevance: {chunk['score']:.0%})"
        lines.append(header)
        lines.append(chunk["text"])
        lines.append("")

    lines.append("=== END OF RETRIEVED KNOWLEDGE ===")

    logger.info("[RAG] Returned %d chunks for query: '%s'", len(filtered), query[:60])
    return "\n".join(lines)
