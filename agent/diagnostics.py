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
            "1_original_user_query": "",
            "2_planner_output": {
                "selected_business_capabilities": [],
                "confidence": 1.0,
                "presentation_mode": "REPORT",
                "extracted_filters": {},
                "missing_information": []
            },
            "3_entity_resolver_output": {
                "project": None,
                "customer": None,
                "employee": None,
                "service_line": None,
                "department": None,
                "financial_year": None,
                "month": None,
                "date_range": None
            },
            "4_tool_registry_output": [],
            "5_backend_response": [],
            "6_synthesizer_input": [],
            "7_final_response": {}
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
        return self._Timer(self, metric_name)

    # --- Data Collectors ---
    def record_request_context(self, question: str, history_len: int, role: str):
        self.replay["1_original_user_query"] = question

    def record_planner_output(self, execution_plan: Dict[str, Any]):
        caps = execution_plan.get("business_capabilities", [])
        filters = {}
        for c in caps:
            filters.update(c.get("filters", {}))
            filters.update(c.get("context", {}))
        self.replay["2_planner_output"] = {
            "selected_business_capabilities": [c.get("id") for c in caps],
            "confidence": execution_plan.get("confidence_score", 1.0),
            "presentation_mode": execution_plan.get("presentation_mode", "REPORT"),
            "extracted_filters": filters,
            "missing_information": execution_plan.get("missing_information", [])
        }

    def record_entity_resolution(self, resolved_entities: List[Dict[str, Any]], clarifications: List[str]):
        entity_summary = {
            "project": None,
            "customer": None,
            "employee": None,
            "service_line": None,
            "department": None,
            "financial_year": None,
            "month": None,
            "date_range": None
        }
        for ent in resolved_entities:
            e_type = ent.get("entity_type", "").lower()
            val = ent.get("entity_value") or ent.get("query")
            if "project" in e_type: entity_summary["project"] = val
            elif "customer" in e_type: entity_summary["customer"] = val
            elif "employee" in e_type or "emp" in e_type: entity_summary["employee"] = val
            elif "service" in e_type: entity_summary["service_line"] = val
            elif "dept" in e_type or "department" in e_type: entity_summary["department"] = val
            elif "year" in e_type or "fy" in e_type: entity_summary["financial_year"] = val
            elif "month" in e_type: entity_summary["month"] = val
            elif "date" in e_type or "range" in e_type: entity_summary["date_range"] = val
        self.replay["3_entity_resolver_output"] = entity_summary

    def record_validation(self, is_valid: bool, errors: List[str]):
        self.replay["execution_validation"] = {
            "is_valid": is_valid,
            "errors": errors
        }

    def record_registry_selection(self, execution_graph: List[Dict[str, Any]]):
        selections = []
        for node in execution_graph:
            impl = node.get("implementation") or node.get("selected_implementation") or {}
            selections.append({
                "capability_id": node.get("capability_id"),
                "backend_endpoint_selected": impl.get("endpoint") or impl.get("function_call") or node.get("endpoint"),
                "query_parameters": node.get("query_parameters") or node.get("context") or {},
                "request_body": node.get("request_body") or {}
            })
        self.replay["4_tool_registry_output"] = selections

    def record_tool_execution(self, tool_results: List[Dict[str, Any]]):
        safe_results = []
        synthesizer_inputs = []
        for res in tool_results:
            safe_results.append({
                "http_status": res.get("http_status", 200),
                "records_returned": len(res.get("result", [])) if isinstance(res.get("result"), list) else (len(res.get("result", {})) if isinstance(res.get("result"), dict) else 1),
                "payload_summary": str(res.get("result"))[:300]
            })
            synthesizer_inputs.append({
                "capability": res.get("capability"),
                "data": res.get("result")
            })
        self.replay["5_backend_response"] = safe_results
        self.replay["6_synthesizer_input"] = synthesizer_inputs

    def record_synthesis(self, final_response: Dict[str, Any]):
        self.replay["7_final_response"] = {
            "type": final_response.get("type"),
            "content_preview": str(final_response.get("content", ""))[:300],
            "has_chart": bool(final_response.get("chart_data")),
            "is_clarification": final_response.get("is_clarification", False)
        }

    # --- Replay Generation ---
    def dump_trace(self):
        """Prints the full 7-Stage Execution Replay JSON."""
        self.timings["total_ms"] = round((time.time() - self.start_time) * 1000, 2)
        
        trace = {
            "REQUEST_CORRELATION_ID": self.request_id,
            "TIMINGS_MS": self.timings,
            "CONVERSATION_REPLAY": self.replay
        }
        
        print("\n" + "="*80)
        print(f"[DEBUG TRACE] AI Execution Pipeline | ID: {self.request_id}")
        print("="*80)
        print(json.dumps(trace, indent=2, default=str))
        print("="*80 + "\n")
