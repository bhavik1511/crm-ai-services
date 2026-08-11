"""
execution_provider.py — Execution Provider Layer
===================================================
Protocol-agnostic execution provider interface for executing capabilities.
Supports REST APIs, Python function wrappers, and SQL queries via ToolRegistry.

Ensures Planner, Retriever, and Formatter remain 100% decoupled from backend execution mechanics.
"""

import logging
from typing import Dict, Any, Optional
from registry.tool_registry import ToolRegistry, format_capability_envelope

logger = logging.getLogger(__name__)

class ExecutionProvider:
    """
    Unified execution provider executing resolved business capabilities.
    """
    def __init__(self):
        self.tool_registry = ToolRegistry()

    async def execute_capability(
        self, capability_id: str, parameters: Dict[str, Any], jwt_token: str = ""
    ) -> Dict[str, Any]:
        """
        Executes capability via ToolRegistry and returns formatted capability envelope.
        """
        from utils.structured_logger import log_stage, log_error
        try:
            raw_result = await self.tool_registry.execute_capability(capability_id, parameters, jwt_token)
            envelope = format_capability_envelope(capability_id, raw_result)
            return envelope
        except Exception as e:
            log_error(logger, "BACKEND", str(e), Capability=capability_id)
            return format_capability_envelope(capability_id, None, error_err=e)

def get_execution_provider() -> ExecutionProvider:
    return ExecutionProvider()
