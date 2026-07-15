"""
Knowledge Base module for CRM AI Assistant.
Loads backend logic documentation and provides context to the LLM agent.

Source: ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md (278KB, 7325 lines)
This is the SINGLE SOURCE OF TRUTH for all CRM business logic, formulas,
table schemas, hardcoded IDs, approval chains, and edge cases.
"""
import os
import re

_DOCS_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# File paths — SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------
MASTER_LOGIC_FILE = os.path.join(_DOCS_DIR, "ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md")

_cached_context = None
_cached_full_doc = None
# Pre-parsed section index for faster search
_cached_sections = None


def _load_file(path: str) -> str:
    """Load a file and return its content, or empty string if not found."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"[KnowledgeBase] WARNING: File not found: {path}")
        return ""


def _build_condensed_modules_summary(full_doc: str) -> str:
    """
    Extract a comprehensive condensed summary from ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.
    Captures:
    - PART headers (# PART X: ...)
    - Module headers (## N. MODULE NAME)
    - Sub-topics (### N.N Topic)
    - APPENDIX headers (## APPENDIX X: ...)
    - Table names from SQL schema (### `table_name`)
    """
    lines = full_doc.split("\n")
    summary_parts = []
    table_names = []

    for line in lines:
        stripped = line.strip()

        # Capture PART headers (e.g. # PART 1: SYSTEM OVERVIEW)
        if stripped.startswith("# PART ") or stripped.startswith("### PART "):
            summary_parts.append("━━━ " + stripped.lstrip("# ").strip())

        # Capture main H2 module headers (e.g. ## 1. SYSTEM ARCHITECTURE)
        elif stripped.startswith("## ") and len(stripped) > 3:
            header_text = stripped[3:].strip()
            # Numbered sections or APPENDIX sections
            if header_text[0:1].isdigit() or header_text.startswith("APPENDIX"):
                summary_parts.append("■ " + header_text)

        # Capture H3 sub-topics (e.g. ### 3.1 User Login Flow)
        elif stripped.startswith("### ") and len(stripped) > 4:
            header_text = stripped[4:].strip()
            if header_text[0:1].isdigit():
                summary_parts.append("    - " + header_text)
            # Capture table names from live DB schema (### `table_name`)
            elif header_text.startswith("`") and header_text.endswith("`"):
                table_names.append(header_text.strip("`"))

    header = "=== ANTIGRAVITY CRM MASTER REFERENCE — COMPLETE OUTLINE ===\n"
    header += "Compiled from: 836 TypeScript files + Live SQL dump (189 tables)\n"
    header += "The AI has access to the following complete documented logic:\n\n"

    summary = header + "\n".join(summary_parts)

    if table_names:
        summary += "\n\n--- LIVE DATABASE TABLES (189 total) ---\n"
        summary += ", ".join(table_names[:50])
        if len(table_names) > 50:
            summary += f"\n  ... and {len(table_names) - 50} more tables"

    return summary


def _parse_sections(full_doc: str) -> list:
    """
    Parse the document into sections for fast keyword search.
    Splits on ## headers and --- dividers.
    Returns list of (title, content) tuples.
    """
    # Split into sections by ## headers
    raw_sections = re.split(r'\n(?=## )', full_doc)

    sections = []
    for section in raw_sections:
        lines = section.strip().split("\n", 1)
        title = lines[0].strip() if lines else ""
        content = section.strip()
        if content:
            sections.append((title, content))

    return sections


def get_knowledge_context() -> str:
    """
    Return the combined knowledge base context string for injection
    into the LLM system prompt. Cached after first load.
    """
    global _cached_context
    if _cached_context is not None:
        return _cached_context

    # Load and condense the master logic doc
    full_doc = _load_file(MASTER_LOGIC_FILE)
    modules_summary = _build_condensed_modules_summary(full_doc) if full_doc else "Master logic file not found."

    context = f"""
═══════════════════════════════════════════════════════════════
CRM BACKEND BUSINESS LOGIC, FORMULAS & RULES (OUTLINE)
═══════════════════════════════════════════════════════════════
The following is a complete outline of ALL business logic documentation.
This covers ALL {189} tables, ALL approval chains, ALL formulas, ALL hardcoded IDs.

If a user asks a detailed question about any of these topics, use the `mcp_search_docs`
or `search_backend_docs` tool to deep-read the exactly matched logic before answering!

CRITICAL HARDCODED IDs (memorize these):
- Direct-to-HR supervisors: employee IDs [3, 31]
- Partner designations: [42, 26]
- HR department_id: 9
- Admin (all-notif) dept: 17
- HR payroll-notify dept: 8
- Trainee designations: [44, 45, 4]
- Accounts dept hardcoded IDs: [51, 15]
- Approved status: 3
- Unpaid leave req_id: 4
- GOSI: 7% of gosi_salary
- Annual leave: 22 days, cycle Oct 1 - Sep 30
- BHD: 3 decimal places (Fils)
- Max failed logins: 5, block: 2 hours

{modules_summary}
"""
    _cached_context = context
    print(f"[KnowledgeBase] Loaded context: {len(context)} characters from ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL.md")
    return context


def search_documentation(query: str) -> str:
    """
    Search the full documentation for specific keywords.
    Returns matching sections (paragraphs containing the keyword).
    Enhanced for the 278KB ANTIGRAVITY_CRM_MASTER_REFERENCE_FINAL document.
    """
    global _cached_full_doc, _cached_sections
    if _cached_full_doc is None:
        _cached_full_doc = _load_file(MASTER_LOGIC_FILE)
        if _cached_full_doc:
            _cached_sections = _parse_sections(_cached_full_doc)

    if not _cached_full_doc:
        return "Documentation files not found."

    query_lower = query.lower()
    keywords = [kw.strip() for kw in query_lower.split() if len(kw.strip()) > 2]

    if not keywords:
        return f"No valid search keywords in: {query}"

    sections = _cached_sections or []

    matching_sections = []
    for title, content in sections:
        content_lower = content.lower()
        title_lower = title.lower()
        # Score by how many keywords match (title matches count double)
        title_score = sum(2 for kw in keywords if kw in title_lower)
        content_score = sum(1 for kw in keywords if kw in content_lower)
        score = title_score + content_score

        if score >= max(1, len(keywords) // 2):
            # Allow larger sections for the comprehensive reference
            display_content = content
            if len(display_content) > 3000:
                display_content = display_content[:3000] + "\n... [truncated — use more specific keywords]"
            matching_sections.append((score, title, display_content))

    if not matching_sections:
        return f"No documentation found matching: {query}"

    # Sort by relevance score and return top 5
    matching_sections.sort(key=lambda x: x[0], reverse=True)
    results = matching_sections[:5]

    output = f"Found {len(results)} relevant documentation sections for '{query}':\n\n"
    for i, (score, title, section) in enumerate(results, 1):
        output += f"--- Section {i} (relevance: {score}, title: {title}) ---\n{section}\n\n"

    return output


if __name__ == "__main__":
    ctx = get_knowledge_context()
    print(f"Context length: {len(ctx)} characters")
    print(ctx[:1000] + "...")
    print("\n--- Testing search ---")
    result = search_documentation("GOSI deduction payroll calculation")
    print(result[:500])
