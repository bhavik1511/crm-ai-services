"""
ingest_knowledge.py  (Local ChromaDB + HuggingFace Edition)
============================================================
Ingests business logic documentation into a local ChromaDB vector database.
100% free — no OpenAI key required.

Usage:
    python ingest_knowledge.py                          # ingest default master reference
    python ingest_knowledge.py --file custom_rules.md   # ingest a specific file
    python ingest_knowledge.py --module "HR Policy" --min-tier 3 --file hr.md
    python ingest_knowledge.py --clear                  # wipe collection and re-ingest

What it does:
    1. Reads the markdown source file(s).
    2. Splits them into semantic chunks by section headers.
    3. Embeds each chunk locally using sentence-transformers (all-MiniLM-L6-v2).
    4. Upserts into the local ChromaDB collection (./chroma_db).

After running this, the RAG pipeline in rag_knowledge_store.py will automatically
use these chunks when the agent calls search_backend_docs().
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("ingest_knowledge")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_AI_SERVICE_DIR = Path(__file__).parent
_CHROMA_DIR = str(_AI_SERVICE_DIR / "chroma_db")
_COLLECTION_NAME = "crm_knowledge_base"
_EMBED_MODEL = "all-MiniLM-L6-v2"
CHUNK_MAX_CHARS = 1200       # target max chars per chunk
CHUNK_OVERLAP_LINES = 2      # lines of overlap when splitting large sections
BATCH_SIZE = 64              # how many chunks to embed at once

DEFAULT_SOURCES: list[dict] = [
    {
        "path": str(_AI_SERVICE_DIR / "ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md"),
        "module": "CRM Master Reference",
        "min_tier": 1,   # 1 = all roles can access (most permissive)
    },
    # Add more files here as your knowledge base grows:
    # {"path": "hr_policy.md", "module": "HR Policy", "min_tier": 3},
    # {"path": "partner_remuneration.md", "module": "Partner Payout", "min_tier": 2},
]


# ---------------------------------------------------------------------------
# Step 1: Smart Markdown Chunker
# ---------------------------------------------------------------------------
def _header_level(line: str) -> Optional[int]:
    m = re.match(r"^(#{1,4})\s", line)
    return len(m.group(1)) if m else None


def chunk_markdown(text: str, source_module: str, min_tier: int) -> list[dict]:
    """
    Split markdown into semantic chunks bounded by section headers.
    Chunks that exceed CHUNK_MAX_CHARS are further split by blank lines.
    """
    lines = text.splitlines()
    chunks: list[dict] = []
    breadcrumb: list[str] = [""] * 5
    current_section = ""
    current_lines: list[str] = []

    def _add_chunk(section_title: str, body: str):
        body = body.strip()
        if not body or len(body) < 20:
            return
        chunk_id = hashlib.md5(
            (source_module + section_title + body).encode()
        ).hexdigest()
        chunks.append({
            "id": chunk_id,
            "text": body,
            "metadata": {
                "source_module": source_module,
                "section_title": section_title,
                "min_tier": min_tier,
            },
        })

    def _flush(section_title: str, body_lines: list[str]):
        body = "\n".join(body_lines).strip()
        if not body:
            return
        if len(body) > CHUNK_MAX_CHARS:
            paragraphs = re.split(r"\n{2,}", body)
            buffer = ""
            for para in paragraphs:
                if buffer and len(buffer) + len(para) > CHUNK_MAX_CHARS:
                    _add_chunk(section_title, buffer)
                    buffer = para
                else:
                    buffer = (buffer + "\n\n" + para).strip() if buffer else para
            if buffer:
                _add_chunk(section_title, buffer)
        else:
            _add_chunk(section_title, body)

    for line in lines:
        lvl = _header_level(line)
        if lvl:
            if current_lines:
                _flush(current_section, current_lines)
            breadcrumb[lvl - 1] = line.lstrip("#").strip()
            for d in range(lvl, 5):
                breadcrumb[d] = ""
            current_section = " › ".join(b for b in breadcrumb if b)
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        _flush(current_section, current_lines)

    logger.info("  → Chunked '%s' into %d chunks", source_module, len(chunks))
    return chunks


# ---------------------------------------------------------------------------
# Step 2: Embed and upsert into ChromaDB
# ---------------------------------------------------------------------------
def run_ingestion(sources: list[dict], clear: bool = False):
    from sentence_transformers import SentenceTransformer
    import chromadb

    logger.info("Loading local embedding model: %s", _EMBED_MODEL)
    embedder = SentenceTransformer(_EMBED_MODEL)
    logger.info("Embedding model loaded.")

    logger.info("Connecting to ChromaDB at: %s", _CHROMA_DIR)
    client = chromadb.PersistentClient(path=_CHROMA_DIR)

    if clear:
        try:
            client.delete_collection(_COLLECTION_NAME)
            logger.info("  → Cleared existing collection '%s'", _COLLECTION_NAME)
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info("Collection '%s' ready — currently has %d docs.", _COLLECTION_NAME, collection.count())

    # Collect all chunks from all sources
    all_chunks: list[dict] = []
    for source in sources:
        path = Path(source["path"])
        if not path.exists():
            logger.warning("  ⚠ File not found, skipping: %s", path)
            continue
        logger.info("Processing: %s (module='%s')", path.name, source["module"])
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_markdown(text, source["module"], source.get("min_tier", 1))
        all_chunks.extend(chunks)

    if not all_chunks:
        logger.warning("No chunks produced — nothing to ingest.")
        return

    logger.info("Total chunks to embed: %d", len(all_chunks))
    t0 = time.time()

    # Embed in batches
    for start in range(0, len(all_chunks), BATCH_SIZE):
        batch = all_chunks[start: start + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        embeddings = embedder.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()

        collection.upsert(
            ids=[c["id"] for c in batch],
            embeddings=embeddings,
            documents=texts,
            metadatas=[c["metadata"] for c in batch],
        )
        done = min(start + BATCH_SIZE, len(all_chunks))
        logger.info("  Embedded and upserted %d / %d chunks…", done, len(all_chunks))

    elapsed = time.time() - t0
    logger.info("✅ Ingestion complete in %.1fs — %d total chunks in ChromaDB.", elapsed, collection.count())
    logger.info("")
    logger.info("You can now start the AI service and the RAG pipeline is live!")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest knowledge base into local ChromaDB for RAG")
    parser.add_argument("--file", type=str, help="Path to a specific markdown file to ingest")
    parser.add_argument("--module", type=str, default="Custom", help="Module name for the file")
    parser.add_argument("--min-tier", type=int, default=1, help="Minimum user tier to access these chunks")
    parser.add_argument("--clear", action="store_true", help="Clear the collection before ingesting")
    args = parser.parse_args()

    sources = DEFAULT_SOURCES
    if args.file:
        sources = [{"path": args.file, "module": args.module, "min_tier": args.min_tier}]

    run_ingestion(sources, clear=args.clear)
