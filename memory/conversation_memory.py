"""
conversation_memory.py — Session-aware conversation memory for follow-up questions.

Tracks:
- Last query context (tables, filters, results)
- Last SQL executed and its results
- Enables follow-ups like "Compare it with Tax" or "Show that as a chart"

Memory is per-session (in-memory only), no database writes.
"""

import time
from typing import Optional, Dict, List, Any
from collections import OrderedDict

# Max sessions to keep in memory
_MAX_SESSIONS = 100

# Session expiry: 1 hour
_SESSION_TTL = 3600


class ConversationContext:
    """Stores context for a single conversation session."""
    
    def __init__(self):
        self.last_question: str = ""
        self.last_sql: str = ""
        self.last_results: List[Dict] = []
        self.last_chart_data: Optional[Dict] = None
        self.last_tables_used: List[str] = []
        self.last_filters: Dict[str, Any] = {}
        self.last_answer: str = ""
        self.updated_at: float = time.time()
    
    def update(
        self,
        question: str = "",
        sql: str = "",
        results: List[Dict] = None,
        chart_data: Dict = None,
        tables: List[str] = None,
        filters: Dict = None,
        answer: str = "",
    ):
        """Update the context with new interaction data."""
        if question:
            self.last_question = question
        if sql:
            self.last_sql = sql
        if results is not None:
            # Keep only first 20 rows to save memory
            self.last_results = results[:20]
        if chart_data is not None:
            self.last_chart_data = chart_data
        if tables:
            self.last_tables_used = tables
        if filters:
            self.last_filters.update(filters)
        if answer:
            self.last_answer = answer
        self.updated_at = time.time()
    
    def is_expired(self) -> bool:
        return (time.time() - self.updated_at) > _SESSION_TTL
    
    def get_context_prompt(self) -> str:
        """Generate a prompt section describing the conversation context."""
        if not self.last_question:
            return ""
        
        parts = ["## CONVERSATION CONTEXT (from previous question)"]
        parts.append(f"- Previous question: \"{self.last_question}\"")
        
        if self.last_sql:
            parts.append(f"- Previous SQL: `{self.last_sql[:300]}`")
        
        if self.last_tables_used:
            parts.append(f"- Tables used: {', '.join(self.last_tables_used)}")
        
        if self.last_results:
            row_count = len(self.last_results)
            cols = list(self.last_results[0].keys()) if self.last_results else []
            parts.append(f"- Previous results: {row_count} rows, columns: {', '.join(cols)}")
            # Include first 3 rows as preview
            for i, row in enumerate(self.last_results[:3]):
                parts.append(f"  Row {i+1}: {row}")
        
        if self.last_filters:
            parts.append(f"- Active filters: {self.last_filters}")
        
        parts.append("")
        parts.append("Use this context to understand follow-up questions like:")
        parts.append("- 'Compare it with X' → re-run query with additional comparison")
        parts.append("- 'Show that as a chart' → generate chart from previous results")
        parts.append("- 'Filter by X' → add filter to previous query")
        parts.append("- 'What about Y?' → apply same analysis to different entity")
        
        return "\n".join(parts)


class ConversationMemory:
    """Manages conversation contexts across multiple sessions."""
    
    def __init__(self):
        self._sessions: OrderedDict[str, ConversationContext] = OrderedDict()
    
    def get_context(self, session_id: str) -> ConversationContext:
        """Get or create a conversation context for a session."""
        self._cleanup_expired()
        
        if session_id not in self._sessions:
            self._sessions[session_id] = ConversationContext()
        
        return self._sessions[session_id]
    
    def get_context_prompt(self, session_id: str) -> str:
        """Get the context prompt for a session (empty string if no context)."""
        ctx = self.get_context(session_id)
        return ctx.get_context_prompt()
    
    def update_context(
        self,
        session_id: str,
        question: str = "",
        sql: str = "",
        results: List[Dict] = None,
        chart_data: Dict = None,
        tables: List[str] = None,
        filters: Dict = None,
        answer: str = "",
    ):
        """Update the context for a session."""
        ctx = self.get_context(session_id)
        ctx.update(
            question=question,
            sql=sql,
            results=results,
            chart_data=chart_data,
            tables=tables,
            filters=filters,
            answer=answer,
        )
    
    def clear_session(self, session_id: str):
        """Clear a session's context."""
        self._sessions.pop(session_id, None)
    
    def _cleanup_expired(self):
        """Remove expired sessions and enforce max session limit."""
        expired = [sid for sid, ctx in self._sessions.items() if ctx.is_expired()]
        for sid in expired:
            del self._sessions[sid]
        
        # Enforce max sessions (remove oldest)
        while len(self._sessions) > _MAX_SESSIONS:
            self._sessions.popitem(last=False)


# Global singleton
memory = ConversationMemory()
