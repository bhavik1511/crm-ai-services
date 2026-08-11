"""
retrieval_engine.py — Semantic Capability Retrieval Engine
============================================================
High-precision semantic vector retrieval engine for capabilities, reports,
entities, and response contracts.

Builds embeddings dynamically from MetadataRegistry descriptors.
Zero hardcoded routing rules, zero regex dicts, zero switch statements.
"""

import math
import re
import logging
from typing import Dict, Any, List, Tuple, Optional
from registry.metadata_registry import get_registry

logger = logging.getLogger(__name__)

def _tokenize(text: str) -> List[str]:
    """Lightweight text tokenizer for metadata feature extraction."""
    if not text or not isinstance(text, str):
        return []
    words = re.findall(r'\b[a-zA-Z0-9]+\b', text.lower())
    # Simple stopword removal
    stopwords = {"a", "an", "the", "in", "on", "of", "for", "to", "and", "or", "is", "are", "with", "by", "at", "it", "this", "that", "me", "show", "give", "get"}
    return [w for w in words if w not in stopwords]

class SemanticRetrievalEngine:
    """
    Metadata-driven vector retrieval engine.
    Ranks capabilities by semantic similarity between user query and capability metadata.
    """
    def __init__(self, registry=None):
        self.registry = registry or get_registry()
        self._doc_vectors: Dict[str, Dict[str, float]] = {}
        self._idf: Dict[str, float] = {}
        self._capabilities_cache: List[Dict[str, Any]] = []
        self._build_index()

    def _build_index(self):
        """Builds TF-IDF / Vector index from registered capability metadata."""
        capabilities = self.registry.list_capabilities()
        self._capabilities_cache = capabilities
        doc_count = len(capabilities)
        if doc_count == 0:
            return

        doc_freqs: Dict[str, int] = {}
        raw_doc_tokens: Dict[str, List[str]] = {}

        for cap in capabilities:
            cap_id = cap["id"]
            # Consolidate metadata features into text document representation
            desc = cap.get("description", "")
            domain = cap.get("business_domain", "")
            intent_kws = " ".join(cap.get("intent_keywords", []))
            metrics = " ".join(cap.get("supported_metrics", []))
            ops = " ".join(cap.get("supported_operations", []))
            primary = cap.get("primary_metric", "")

            doc_text = f"{cap_id} {domain} {desc} {intent_kws} {metrics} {ops} {primary}"
            tokens = _tokenize(doc_text)
            raw_doc_tokens[cap_id] = tokens

            unique_tokens = set(tokens)
            for token in unique_tokens:
                doc_freqs[token] = doc_freqs.get(token, 0) + 1

        # Compute IDF
        self._idf = {}
        for token, df in doc_freqs.items():
            self._idf[token] = math.log((doc_count + 1) / (df + 1)) + 1.0

        # Compute L2-normalized TF-IDF vector for each capability
        self._doc_vectors = {}
        for cap_id, tokens in raw_doc_tokens.items():
            tf: Dict[str, float] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            
            tf_idf = {}
            norm_sq = 0.0
            for t, count in tf.items():
                val = count * self._idf.get(t, 1.0)
                tf_idf[t] = val
                norm_sq += val * val
            
            norm = math.sqrt(norm_sq) if norm_sq > 0 else 1.0
            self._doc_vectors[cap_id] = {t: val / norm for t, val in tf_idf.items()}

        logger.debug(f"[SemanticRetrievalEngine] Indexed {len(self._doc_vectors)} capabilities.")

    def retrieve_top_k(self, query: str, top_k: int = 3) -> List[Tuple[Dict[str, Any], float]]:
        """
        Retrieves Top-K candidate capabilities matching the query.
        Returns list of (capability_metadata, similarity_score) tuples.
        """
        if not self._doc_vectors:
            self._build_index()

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        # Build query vector
        q_tf: Dict[str, float] = {}
        for t in q_tokens:
            q_tf[t] = q_tf.get(t, 0.0) + 1.0

        q_vec = {}
        norm_sq = 0.0
        for t, count in q_tf.items():
            val = count * self._idf.get(t, 1.0)
            q_vec[t] = val
            norm_sq += val * val
        
        norm = math.sqrt(norm_sq) if norm_sq > 0 else 1.0
        q_norm_vec = {t: val / norm for t, val in q_vec.items()}

        # Compute Cosine Similarity against indexed document vectors
        scores: List[Tuple[Dict[str, Any], float]] = []
        for cap in self._capabilities_cache:
            cap_id = cap["id"]
            doc_vec = self._doc_vectors.get(cap_id, {})
            
            score = 0.0
            for t, weight in q_norm_vec.items():
                if t in doc_vec:
                    score += weight * doc_vec[t]
            
            # Boost score if query exact matches capability intent keywords or ID
            q_lower = query.lower()
            intent_kws = cap.get("intent_keywords", [])
            if any(kw.lower() in q_lower for kw in intent_kws):
                score = max(score, 0.96)
            elif cap_id.replace("_", " ") in q_lower:
                score = max(score, 0.95)

            scores.append((cap, round(min(score, 1.0), 4)))

        # Sort by similarity score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        top_results = scores[:top_k]

        from utils.structured_logger import log_stage
        if top_results:
            top_cap, top_score = top_results[0]
            log_stage(logger, "RETRIEVAL", Capability=top_cap["id"], Similarity=top_score, CandidatesCount=len(top_results))
        else:
            log_stage(logger, "RETRIEVAL", Capability="None", Similarity=0.0, CandidatesCount=0)

        return top_results

def get_retrieval_engine() -> SemanticRetrievalEngine:
    return SemanticRetrievalEngine()
