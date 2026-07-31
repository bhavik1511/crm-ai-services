"""
diagnostics.py — Enterprise Observability & Diagnostics
Provides a zero-overhead logging tracker to trace the entire AI Chatbot execution.
Generates unique Correlation IDs and supports complete Conversation Replay.
"""
import os
import time
import json
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Config
AI_DEBUG_MODE = os.getenv("AI_DEBUG_MODE", "False").lower() in ("true", "1", "yes")

class DiagnosticsTracker:
    def __init__(self, session_id: str):
        # 1. Generate Request Correlation ID (e.g., AI-20260710-ab34cd)
        date_str = datetime.utcnow().strftime("%Y%m%d")
        short_uuid = str(uuid.uuid4())[:6]
        self.request_id = f"AI-{date_str}-{short_uuid}"
        
        self.session_id = session_id
        self.start_time = time.time()
        
        # Performance Tracking
        self.timings = {
            "planner_ms": 0,
            "entity_resolver_ms": 0,
            "tool_execution_ms": 0,
            "synthesizer_ms": 0,
            "total_ms": 0
        }
        
        # Conversation Replay Payload
        self.replay = {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "original_question": "",
            "context_summary": {},
            "planner_output": {},
            "entity_resolution": [],
            "execution_validation": {},
            "tool_registry_selection": [],
            "tool_execution_results": [],
            "final_response_metrics": {}
        }

    # --- Timers ---
    class _Timer:
        def __init__(self, tracker, metric_name):
            self.tracker = tracker
            self.metric_name = metric_name
            self.start = 0
            
        def __enter__(self):
            self.start = time.time()
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            elapsed = (time.time() - self.start) * 1000
            self.tracker.timings[self.metric_name] = round(elapsed, 2)

    def track_time(self, metric_name: str):
        """Context manager to easily track block execution time."""
        if not AI_DEBUG_MODE:
            # Dummy context manager for zero-overhead when disabled
            class DummyTimer:
                def __enter__(self): pass
                def __exit__(self, *args): pass
            return DummyTimer()
        return self._Timer(self, metric_name)

    # --- Data Collectors ---
    def record_request_context(self, question: str, history_len: int, role: str):
        if not AI_DEBUG_MODE: return
        self.replay["original_question"] = question
        self.replay["context_summary"] = {
            "history_messages": history_len,
            "user_role": role
        }

    def record_planner_output(self, execution_plan: Dict[str, Any]):
        if not AI_DEBUG_MODE: return
        # Strip sensitive info if any existed, though Planner output should be abstract
        self.replay["planner_output"] = {
            "business_goal": execution_plan.get("business_goal"),
            "confidence_score": execution_plan.get("confidence_score"),
            "business_capabilities": execution_plan.get("business_capabilities", []),
            "missing_information": execution_plan.get("missing_information", [])
        }

    def record_entity_resolution(self, resolved_entities: List[Dict[str, Any]], clarifications: List[str]):
        if not AI_DEBUG_MODE: return
        
        safe_entities = []
        for ent in resolved_entities:
            # Only keep structural metadata, strip raw payload just in case
            safe_entities.append({
                "status": ent.get("status"),
                "entity_type": ent.get("entity_type"),
                "query": ent.get("query"),
                "entity_id": ent.get("entity_id"),
                "confidence": ent.get("confidence")
            })
            
        self.replay["entity_resolution"] = {
            "resolved_entities": safe_entities,
            "clarifications_issued": clarifications
        }

    def record_validation(self, is_valid: bool, errors: List[str]):
        if not AI_DEBUG_MODE: return
        self.replay["execution_validation"] = {
            "is_valid": is_valid,
            "errors": errors
        }

    def record_registry_selection(self, execution_graph: List[Dict[str, Any]]):
        if not AI_DEBUG_MODE: return
        selections = []
        for node in execution_graph:
            impl = node.get("implementation") or node.get("selected_implementation") or {}
            selections.append({
                "capability_id": node.get("capability_id"),
                "implementation_type": impl.get("type") or node.get("implementation_type"),
                "priority": impl.get("priority") if impl.get("priority") is not None else node.get("priority"),
                "target": impl.get("endpoint") or impl.get("function_call") or node.get("endpoint"),
                "backend_endpoint": node.get("backend_endpoint")
            })
        self.replay["tool_registry_selection"] = selections

    def record_tool_execution(self, tool_results: List[Dict[str, Any]]):
        if not AI_DEBUG_MODE: return
        safe_results = []
        for res in tool_results:
            safe_results.append({
                "capability": res.get("capability"),
                "success": res.get("status") == "success" and "error" not in res and not res.get("error"),
                "error_message": res.get("error"),
                "implementation_type": res.get("implementation_type"),
                "endpoint": res.get("function_call"),
                "execution_time_ms": res.get("execution_time_ms"),
                "data_points_returned": len(res.get("result", [])) if isinstance(res.get("result"), list) else (len(res.get("result", {})) if isinstance(res.get("result"), dict) else (1 if res.get("result") else 0))
            })
        self.replay["tool_execution_results"] = safe_results

    def record_synthesis(self, final_response: Dict[str, Any]):
        if not AI_DEBUG_MODE: return
        self.replay["final_response_metrics"] = {
            "type": final_response.get("type"),
            "has_chart": bool(final_response.get("chart_data")),
            "is_clarification": final_response.get("is_clarification", False)
        }

    # --- Replay Generation ---
    def dump_trace(self):
        """Prints the full Conversation Replay JSON if Debug Mode is enabled."""
        if not AI_DEBUG_MODE: return
        
        self.timings["total_ms"] = round((time.time() - self.start_time) * 1000, 2)
        
        trace = {
            "REQUEST_CORRELATION_ID": self.request_id,
            "TIMINGS_MS": self.timings,
            "CONVERSATION_REPLAY": self.replay
        }
        
        # Log purely to stdout for developers
        print("\n" + "="*80)
        print(f"[DEBUG TRACE] AI Execution Pipeline | ID: {self.request_id}")
        print("="*80)
        print(json.dumps(trace, indent=2))
        print("="*80 + "\n")
