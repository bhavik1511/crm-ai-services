"""
metadata_registry.py — Enterprise Metadata Registry
=====================================================
Thread-safe, explicit self-registration registry for capabilities, reports,
REST API endpoints, response contracts, and presentation renderers.

Zero file-system scanning. Zero hardcoded capability conditionals.
Provides single source of truth for runtime discovery across the AI pipeline.
"""

import threading
import logging
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)

class MetadataRegistry:
    """
    Central, thread-safe registry holding metadata for all capabilities,
    reports, APIs, contracts, and renderers.
    """
    _instance = None
    _lock = threading.RLock()

    def __init__(self):
        self._capabilities: Dict[str, Dict[str, Any]] = {}
        self._reports: Dict[str, Dict[str, Any]] = {}
        self._endpoints: Dict[str, Dict[str, Any]] = {}
        self._contracts: Dict[str, Dict[str, Any]] = {}
        self._renderers: Dict[str, Callable] = {}
        self._execution_providers: Dict[str, Any] = {}
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "MetadataRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    cls._instance._bootstrap_default_catalog()
        return cls._instance

    def _bootstrap_default_catalog(self):
        """Auto-registers capabilities from capability_catalog.py on first initialization."""
        if self._initialized:
            return
        try:
            from registry.capability_catalog import BUSINESS_CAPABILITIES
            for cap in BUSINESS_CAPABILITIES:
                self.register_capability(cap)
            self._initialized = True
            logger.info(f"[MetadataRegistry] Bootstrapped {len(self._capabilities)} capabilities from catalog.")
        except Exception as e:
            logger.error(f"[MetadataRegistry] Error bootstrapping capability catalog: {e}")

    def register_capability(self, metadata: Dict[str, Any]) -> None:
        """Register a business capability metadata entry."""
        cap_id = metadata.get("id")
        if not cap_id:
            raise ValueError("Capability metadata must contain an 'id' field.")
        with self._lock:
            self._capabilities[cap_id] = metadata
            if "response_contract" in metadata:
                self._contracts[cap_id] = metadata["response_contract"]
            logger.debug(f"[MetadataRegistry] Registered capability: {cap_id}")

    def register_report(self, report_metadata: Dict[str, Any]) -> None:
        """Register a backend report descriptor."""
        report_id = report_metadata.get("id") or report_metadata.get("name")
        if not report_id:
            raise ValueError("Report metadata must contain an 'id' or 'name' field.")
        with self._lock:
            self._reports[report_id] = report_metadata
            logger.debug(f"[MetadataRegistry] Registered report: {report_id}")

    def register_endpoint(self, endpoint_metadata: Dict[str, Any]) -> None:
        """Register a REST API endpoint descriptor."""
        endpoint_key = endpoint_metadata.get("endpoint") or endpoint_metadata.get("path")
        if not endpoint_key:
            raise ValueError("Endpoint metadata must contain an 'endpoint' or 'path' field.")
        with self._lock:
            self._endpoints[endpoint_key] = endpoint_metadata
            logger.debug(f"[MetadataRegistry] Registered endpoint: {endpoint_key}")

    def register_contract(self, name: str, contract: Dict[str, Any]) -> None:
        """Register a response contract or schema."""
        with self._lock:
            self._contracts[name] = contract
            logger.debug(f"[MetadataRegistry] Registered contract: {name}")

    def register_renderer(self, name: str, renderer_fn: Callable) -> None:
        """Register a deterministic presentation renderer."""
        with self._lock:
            self._renderers[name] = renderer_fn
            logger.debug(f"[MetadataRegistry] Registered renderer: {name}")

    def register_execution_provider(self, provider_type: str, provider_instance: Any) -> None:
        """Register a protocol-agnostic execution provider."""
        with self._lock:
            self._execution_providers[provider_type] = provider_instance
            logger.debug(f"[MetadataRegistry] Registered execution provider: {provider_type}")

    def get_context_requirements(self, capability_id: str) -> Dict[str, Any]:
        """
        Returns metadata-declared context requirements for a capability:
        {
            "required_context": List[str],
            "clarifiable_context": List[str],
            "inheritable_context": List[str],
            "defaultable_context": List[str],
            "optional_context": List[str]
        }
        """
        cap = self.get_capability(capability_id)
        if not cap:
            return {
                "required_context": [],
                "clarifiable_context": [],
                "inheritable_context": ["temporal_scope", "start_date", "end_date", "financial_year", "employee_id", "customer_id", "project_id"],
                "defaultable_context": [],
                "optional_context": []
            }
        return {
            "required_context": cap.get("required_context", []),
            "clarifiable_context": cap.get("clarifiable_context", cap.get("required_context", [])),
            "inheritable_context": cap.get("inheritable_context", ["temporal_scope", "start_date", "end_date", "financial_year", "employee_id", "customer_id", "project_id"]),
            "defaultable_context": cap.get("defaultable_context", []),
            "optional_context": cap.get("optional_context", [])
        }

    # --- Discovery & Retrieval Methods ---

    def get_capability(self, capability_id: str) -> Optional[Dict[str, Any]]:
        return self._capabilities.get(capability_id)

    def list_capabilities(self) -> List[Dict[str, Any]]:
        return list(self._capabilities.values())

    def get_contract(self, capability_id: str) -> Optional[Dict[str, Any]]:
        return self._contracts.get(capability_id)

    def get_renderer(self, name: str) -> Optional[Callable]:
        return self._renderers.get(name)

    def get_execution_provider(self, provider_type: str) -> Optional[Any]:
        return self._execution_providers.get(provider_type)


# --- Module-Level Decorators & Utility Functions ---

def register_capability(metadata: Dict[str, Any]) -> None:
    MetadataRegistry.get_instance().register_capability(metadata)

def register_report(metadata: Dict[str, Any]) -> None:
    MetadataRegistry.get_instance().register_report(metadata)

def register_endpoint(metadata: Dict[str, Any]) -> None:
    MetadataRegistry.get_instance().register_endpoint(metadata)

def register_contract(name: str, contract: Dict[str, Any]) -> None:
    MetadataRegistry.get_instance().register_contract(name, contract)

def register_renderer(name: str):
    """Decorator to register a deterministic renderer function."""
    def decorator(fn: Callable):
        MetadataRegistry.get_instance().register_renderer(name, fn)
        return fn
    return decorator

def get_registry() -> MetadataRegistry:
    return MetadataRegistry.get_instance()
