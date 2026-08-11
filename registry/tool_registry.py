"""
tool_registry.py — The Dynamic Implementation Resolver.
This module bridges the abstract Business Analyst Planner with the physical execution layer.
It reads the Business Capabilities requested by the Planner, dynamically resolves 
the highest-priority implementation from the capability_catalog, and returns executable closures.
"""
import logging
import time
import json
from typing import Dict, Any, List

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from utils.structured_logger import log_stage, log_debug_payload, log_error, mask_jwt

from registry.capability_catalog import get_capability_metadata, CAPABILITY_ALIASES, BUSINESS_CAPABILITIES
from agent.semantic_wrappers import SEMANTIC_TOOL_MAP

logger = logging.getLogger(__name__)

CRM_API_BASE = os.getenv("CRM_API_BASE", "http://localhost:3001/api/v1").rstrip("/")

def format_capability_envelope(capability_id: str, result_data: Any, error_err: Any = None) -> Dict[str, Any]:
    cap_meta = get_capability_metadata(capability_id) or {}
    default_msg = cap_meta.get("default_error_message", "The requested information is currently unavailable. Please try again later.")
    response_schema = cap_meta.get("response_schema", {})
    primary_metric = cap_meta.get("primary_metric")

    if error_err or not result_data:
        if error_err:
            logger.error(f"[Capability Error] cap_id={capability_id} | internal_err={error_err}")
        return {
            "status": "error",
            "confidence": "unavailable",
            "source": capability_id,
            "payload": {
                "error_message": default_msg
            }
        }

    # Extract payload if already wrapped or raw dict
    payload = result_data
    if isinstance(result_data, dict) and "payload" in result_data and "status" in result_data:
        status_val = result_data.get("status", "success")
        if status_val != "success":
            return {
                "status": status_val,
                "confidence": result_data.get("confidence", "unavailable"),
                "source": capability_id,
                "payload": {"error_message": default_msg}
            }
        payload = result_data.get("payload", {})

    # In-line Whitelist Filtering against response_schema
    sanitized_payload = {}
    if isinstance(payload, dict):
        if response_schema:
            for field in response_schema.keys():
                if field in payload:
                    sanitized_payload[field] = payload[field]
            # If whitelist filtering stripped all keys, but original payload contains valid report/data fields, preserve original payload
            if not sanitized_payload and payload:
                if any(k in payload for k in ["data", "rows", "summary", "count", "records", "results", "list", "items", "table", "billing", "strictly_active_projects_count"]):
                    sanitized_payload = payload
                elif not any(k in payload for k in ["error", "error_message"]):
                    sanitized_payload = payload
        else:
            sanitized_payload = payload
    else:
        sanitized_payload = payload

    has_primary = True
    if primary_metric and isinstance(sanitized_payload, dict):
        if not sanitized_payload:
            has_primary = False
        # If payload is non-empty, do not fail primary metric check for report responses
        elif isinstance(payload, dict) and any(k in payload for k in ["data", "rows", "summary", "records", "results", "list", "items", "projects_by_status"]):
            has_primary = True

    if not sanitized_payload or not has_primary:
        return {
            "status": "unavailable",
            "confidence": "unavailable",
            "source": capability_id,
            "payload": {
                "error_message": default_msg
            }
        }

    return {
        "status": "success",
        "confidence": "verified",
        "source": capability_id,
        "payload": sanitized_payload
    }

class ToolResolutionException(Exception):
    """Raised when a Planner capability fails to resolve to a valid executable implementation."""
    pass

class ToolRegistry:
    def __init__(self):
        pass
        
    def resolve_implementations(self, capabilities: Any, resolved_entities: List[Dict[str, Any]] = None, user_context: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Takes abstract capabilities selected by the Planner.
        Traces resolution through 5 stages:
          Stage 1: Capability Catalog Lookup
          Stage 2: Available Implementations Extraction
          Stage 3: Filtering & Entity / Operations Matching
          Stage 4: Priority & Score Resolution
          Stage 5: Backend Endpoint Selection

        Raises ToolResolutionException on any resolution failure or empty tool selection.
        """
        if not capabilities:
            logger.error("[ToolRegistry Stage 1 FAILED] No capabilities provided by Planner in BusinessExecutionPlan.")
            raise ToolResolutionException("ToolResolutionException: No capabilities provided by Planner in BusinessExecutionPlan.")

        # Normalize capabilities to a list of dicts
        if isinstance(capabilities, str):
            capabilities = [{"id": capabilities}]
        elif isinstance(capabilities, dict):
            capabilities = [capabilities]
        elif not isinstance(capabilities, list):
            raise ToolResolutionException(f"ToolResolutionException: Invalid capabilities type '{type(capabilities).__name__}'. Expected list, dict, or str.")

        resolved_entities = resolved_entities or []
        user_context = user_context or {}
        execution_graph = []
        
        for cap in capabilities:
            if isinstance(cap, str):
                cap = {"id": cap}
            cap_id = cap.get("id")
            if not cap_id:
                raise ToolResolutionException("ToolResolutionException: Capability specification is missing the required 'id' field.")

            target_cap_id = CAPABILITY_ALIASES.get(str(cap_id).lower(), CAPABILITY_ALIASES.get(cap_id, cap_id))
            context = cap.get("context", {}) or {}

            # STAGE 1: Capability Catalog Lookup
            metadata = get_capability_metadata(target_cap_id)
            if not metadata:
                logger.error(f"[ToolRegistry] Stage 1 FAILED: Capability '{cap_id}' (target: '{target_cap_id}') not registered in Capability Catalog.")
                raise ToolResolutionException(f"ToolResolutionException: Capability '{cap_id}' (target: '{target_cap_id}') is not registered in the Capability Catalog.")
            
            logger.info(f"[ToolRegistry] STAGE 1 (Catalog Lookup): capability_id='{cap_id}' -> catalog_id='{target_cap_id}' | Status: FOUND")

            # STAGE 2: Available Implementations
            available_impls = metadata.get("implementations", [])
            avail_summary = [impl.get("function_call") or impl.get("endpoint") or impl.get("type") for impl in available_impls]
            if not available_impls:
                logger.error(f"[ToolRegistry] Stage 2 FAILED: No implementations registered in catalog for capability '{target_cap_id}'.")
                raise ToolResolutionException(f"ToolResolutionException: No implementations registered in Capability Catalog for capability '{target_cap_id}'.")
            
            logger.info(f"[ToolRegistry] STAGE 2 (Available Implementations): capability_id='{target_cap_id}' | Candidate count={len(available_impls)} | Implementations={avail_summary}")

            # STAGE 3 & STAGE 4: Filtering & Priority Resolution
            selection_result = self.score_and_select_implementation(
                target_cap_id, 
                available_impls, 
                list(context.keys()), 
                resolved_entities, 
                context, 
                raise_on_failure=True
            )
            
            best_impl = selection_result.get("implementation")
            filtered_impls = selection_result.get("filtered_implementations", [])
            rejection_reasons = selection_result.get("rejection_reasons", [])
            best_score = selection_result.get("score", 0)

            filtered_summary = [impl.get("function_call") or impl.get("endpoint") or impl.get("type") for impl in filtered_impls]
            logger.info(f"[ToolRegistry] STAGE 3 (Filtering): capability_id='{target_cap_id}' | Candidates={len(available_impls)} | Passed={len(filtered_impls)} ({filtered_summary}) | Rejections={rejection_reasons or 'None'}")

            if not best_impl:
                logger.error(f"[ToolRegistry] Stage 4 FAILED: Business capability '{target_cap_id}' failed implementation selection. Rejection reasons: {rejection_reasons}")
                raise ToolResolutionException(f"ToolResolutionException: Business capability '{target_cap_id}' failed implementation selection. Available implementations: {avail_summary}. Rejection reasons: {rejection_reasons}")

            selected_target = best_impl.get("function_call") or best_impl.get("endpoint") or best_impl.get("type")
            logger.info(f"[ToolRegistry] STAGE 4 (Priority Resolution): capability_id='{target_cap_id}' -> Selected implementation: type='{best_impl.get('type')}', priority={best_impl.get('priority')}, target='{selected_target}' (Score: {best_score})")

            # STAGE 5: Backend Endpoint Selection
            impl_type = best_impl.get("type")
            if impl_type == "wrapper":
                func_name = best_impl.get("function_call")
                if func_name not in SEMANTIC_TOOL_MAP:
                    logger.error(f"[ToolRegistry] Stage 5 FAILED: Wrapper function '{func_name}' for capability '{target_cap_id}' missing from SEMANTIC_TOOL_MAP.")
                    raise ToolResolutionException(f"ToolResolutionException: Semantic wrapper function '{func_name}' for capability '{target_cap_id}' is missing from SEMANTIC_TOOL_MAP.")
                final_backend_target = f"wrapper: {func_name}"
            elif impl_type in ["report", "api"]:
                raw_ep = best_impl.get("endpoint", "")
                if raw_ep.upper().startswith(("GET ", "POST ", "PUT ", "DELETE ", "PATCH ")):
                    final_backend_target = raw_ep
                else:
                    final_backend_target = f"{best_impl.get('method', 'GET')} {raw_ep}"
            else:
                final_backend_target = f"custom: {selected_target}"

            logger.info(f"[ToolRegistry] STAGE 5 (Backend Endpoint Selection): capability_id='{target_cap_id}' -> Final backend target: '{final_backend_target}'")

            executable_node = {
                "capability_id": target_cap_id,
                "capability": target_cap_id,
                "implementation": best_impl,
                "selected_implementation": best_impl,
                "available_implementations": available_impls,
                "filtered_implementations": filtered_impls,
                "rejection_reasons": rejection_reasons,
                "context": context,
                "intent": cap.get("intent"),
                "endpoint": best_impl.get("endpoint") or best_impl.get("function_call"),
                "backend_endpoint": final_backend_target,
                "function_call": best_impl.get("function_call"),
                "implementation_type": impl_type,
                "priority": best_impl.get("priority"),
                "score": best_score,
                "full_capability_spec": cap
            }
            execution_graph.append(executable_node)

        if not execution_graph:
            raise ToolResolutionException("ToolResolutionException: Tool Registry failed to resolve any executable implementation node.")

        return execution_graph

    def score_and_select_implementation(
        self, 
        capability_id: str, 
        implementations: List[Dict[str, Any]], 
        context_keys: List[str], 
        resolved_entities: List[Dict[str, Any]], 
        context: Dict[str, Any] = None,
        raise_on_failure: bool = False
    ) -> Dict[str, Any]:
        """
        Evaluates candidate implementations for a capability against entities, context, and operational requirements.
        Traces filtering, priority resolution, and selection score.
        """
        context = context or {}
        
        # Build available context keys (context params + resolved entity IDs)
        available_keys = set(context_keys)
        available_entity_types = set()
        for ent in (resolved_entities or []):
            ent_type = (ent.get("type") or ent.get("entity_type") or "").lower()
            if ent_type:
                available_keys.add(f"{ent_type}_id")
                available_entity_types.add(ent_type)

        rejection_reasons = []
        filtered_candidates = []
        avail_summary = [impl.get("function_call") or impl.get("endpoint") or impl.get("type") for impl in implementations]

        for impl in implementations:
            impl_name = impl.get("function_call") or impl.get("endpoint") or impl.get("type")
            req_entities = impl.get("required_entities", [])
            req_params = impl.get("required_parameters", [])
            
            # 1. HARD GATE: Entity Match
            missing_entities = [ent for ent in req_entities if f"{ent}_id" not in available_keys and ent not in available_keys and ent not in available_entity_types]
            if missing_entities:
                reason = f"Candidate '{impl_name}': Missing required entity '{missing_entities[0]}' (Available entity keys: {list(available_keys)})"
                rejection_reasons.append(reason)
                logger.info(f"[ToolRegistry Filter] Disqualified '{impl_name}' -> {reason}")
                continue

            # 1.2. HARD GATE: Required Parameters
            # If an implementation lists required_parameters, ALL of them must be present in context
            # or available_keys. This allows REST endpoints to be skipped when their mandatory
            # query params (like department_id) are not resolved, falling back to wrappers.
            missing_required_params = [p for p in req_params if p not in available_keys]
            if missing_required_params:
                reason = f"Candidate '{impl_name}': Missing required parameter(s) {missing_required_params}"
                rejection_reasons.append(reason)
                logger.info(f"[ToolRegistry Filter] Disqualified '{impl_name}' -> {reason}")
                continue

            # 1.5. HARD GATE: Analytical Operations Support
            requested_ops = set()
            for op_key in ["ranking", "comparison", "group_by", "trend", "limit", "sort_order"]:
                val = context.get(op_key)
                if val and str(val).lower() not in ["none", "null", "false", ""]:
                    requested_ops.add(op_key)

            op_val = context.get("operation")
            if op_val and str(op_val).lower() not in ["none", "null", "false", ""]:
                requested_ops.add(str(op_val).lower())

            supported_ops = set(impl.get("supported_operations", ["filter", "summary", "ranking", "comparison", "group_by", "trend", "count", "sum", "average", "sort_order", "limit"]))
            missing_ops = requested_ops - supported_ops
            if missing_ops:
                reason = f"Candidate '{impl_name}': Missing required operations {missing_ops} (Supported: {supported_ops})"
                rejection_reasons.append(reason)
                logger.info(f"[ToolRegistry Filter] Disqualified '{impl_name}' -> {reason}")
                continue

            # 2. SCORE CALCULATION
            entity_score = len(req_entities)
            param_score = sum(1 for p in req_params if p in available_keys)
            priority_score = 10 - impl.get("priority", 5)
            
            total_score = (entity_score * 1000) + (param_score * 100) + priority_score
            missing_params = [p for p in req_params if p not in available_keys]
            filtered_candidates.append((total_score, impl, missing_params))
            logger.info(f"[ToolRegistry Candidate] '{impl_name}' passed filtering | Score={total_score} (EntityScore={entity_score*1000}, ParamScore={param_score*100}, PriorityScore={priority_score})")

        if filtered_candidates:
            # Sort by total_score descending
            filtered_candidates.sort(key=lambda x: x[0], reverse=True)
            highest_score, best_impl, missing_params_for_best = filtered_candidates[0]
        else:
            best_impl = None
            missing_params_for_best = []
            highest_score = -999999
            if raise_on_failure:
                raise ToolResolutionException(f"ToolResolutionException: Capability '{capability_id}' could not be resolved to any executable implementation. Available implementations: {avail_summary}. Rejection reasons: {rejection_reasons}")

        return {
            "implementation": best_impl,
            "filtered_implementations": [item[1] for item in filtered_candidates],
            "rejection_reasons": rejection_reasons,
            "missing_parameters": missing_params_for_best,
            "score": highest_score if best_impl else 0
        }

    async def execute_capability(
        self, capability_id: str, parameters: Dict[str, Any] = None, jwt_token: str = ""
    ) -> Dict[str, Any]:
        """
        Convenience method to resolve and execute a single capability by ID.
        """
        parameters = parameters or {}
        nodes = self.resolve_implementations([{"id": capability_id, "context": parameters}], [], parameters)
        results = await self.execute_resolved_implementations(nodes, [], jwt_token, parameters)
        if results and isinstance(results, list):
            res_dict = results[0]
            return res_dict.get("result", res_dict)
        return {"error": f"No execution result returned for capability '{capability_id}'."}

    async def execute_resolved_implementations(self, execution_graph: List[Dict[str, Any]], resolved_entities: List[Dict[str, Any]], jwt_token: str, user_context: Dict[str, Any] = None, question: str = "") -> List[Dict[str, Any]]:
        """
        Executes the resolved implementations (Wrappers, APIs, Reports).
        Note: Actual concurrent execution mapping.
        """
        import time, datetime, json, traceback
        logger.info("=" * 80)
        logger.info("ENTERED execute_resolved_implementations()")
        logger.info("=" * 80)
        logger.info(f"Execution Graph          : {json.dumps(execution_graph, default=str)}")
        logger.info(f"Number of Implementations: {len(execution_graph)}")
        logger.info(f"Capability IDs           : {[n.get('capability_id') or n.get('capability') or n.get('id') for n in execution_graph]}")
        logger.info(f"Selected Implementations : {[n.get('selected_implementation') for n in execution_graph]}")

        if not execution_graph:
            logger.info("[TOOL REGISTRY RETURN] Returning because execution graph is empty.")
            return []

        results = []
        import asyncio
        
        async def execute_node(node):
            start_time = time.time()
            import os
            debug_mode = os.getenv("AI_DEBUG_MODE", "false").lower() == "true"
            
            if "error" in node:
                logger.info(f"[TOOL REGISTRY RETURN] Returning node because error was found in node: {node['error']}")
                return {"capability": node.get("capability_id", "Unknown"), "error": node["error"]}
                
            ctx = node.get("context", {})
            cap_id = node.get("capability_id") or node.get("capability") or node.get("id") or "unknown_capability"
            target_cap_id = CAPABILITY_ALIASES.get(cap_id, cap_id)
            node_intent = node.get("intent")
            
            # 1. Dynamically inject ALL resolved entities into context
            for ent in resolved_entities:
                ent_type = (ent.get("type") or ent.get("entity_type") or "").lower()
                ent_id = ent.get("id") or ent.get("entity_id")
                ent_name = ent.get("name") or ent.get("entity_name")
                if ent_type and ent_id:
                    key_name = f"{ent_type}_id"
                    if key_name not in ctx:
                        ctx[key_name] = ent_id
                    if ent_name and f"{ent_type}_name" not in ctx:
                        ctx[f"{ent_type}_name"] = ent_name

            # NEW: Inject global report filters from user_context
            if user_context:
                import re

                # 1. Merge filters that don't need resolution (like period, dates, scope)
                for filter_key in ["financial_year", "date_range", "start_date", "end_date", "period", "service_line", "office", "department", "partner", "client", "manager", "industry", "country", "status"]:
                    val = user_context.get(filter_key)
                    if val and str(val).lower() != "all" and filter_key not in ctx:
                        ctx[filter_key] = val

            # Auto-parse temporal scope filters (FY, Quarters, Months, Date Ranges)
            tf = ctx.get("time_filter") or ctx.get("period") or ctx.get("date_range") or ctx.get("financial_year") or (user_context.get("time_filter") if user_context else None)
            if tf:
                from agent.entity_resolver import parse_scope_time_filter
                parsed_time = parse_scope_time_filter(str(tf))
                for k, v in parsed_time.items():
                    if k not in ctx or not ctx[k]:
                        ctx[k] = v

            # MANDATE: Centralized FiscalYearResolver enforcement for all capabilities
            fy_val = ctx.get("financial_year") or ctx.get("time_filter") or ctx.get("period")
            from agent.entity_resolver import is_fiscal_year_expression, resolve_fiscal_year
            if fy_val and is_fiscal_year_expression(str(fy_val)):
                fy_res = resolve_fiscal_year(str(fy_val))
                ctx["financial_year"] = fy_res["financial_year"]
                if "start_date" not in ctx or not ctx["start_date"]:
                    ctx["start_date"] = fy_res["start_date"]
                if "end_date" not in ctx or not ctx["end_date"]:
                    ctx["end_date"] = fy_res["end_date"]

            # 2. Dynamically select the correct implementation based on context
            if "question" not in ctx and question:
                ctx["question"] = question
                
            implementations = node.get("implementations", [])
            if not implementations:
                metadata = get_capability_metadata(target_cap_id)
                implementations = metadata.get("implementations", []) if metadata else []

            selection_result = self.score_and_select_implementation(target_cap_id, implementations, list(ctx.keys()), resolved_entities, ctx)
            best_impl = selection_result.get("implementation")
            score = selection_result.get("score", 0.0)
            missing_params = selection_result.get("missing_parameters", [])
                    
            if not best_impl:
                raise ToolResolutionException(f"ToolResolutionException: No valid implementation found for capability '{target_cap_id}'.")

            if debug_mode:
                logger.info(f"[AI_DEBUG_MODE] Parameters injected into {cap_id}: {ctx}")

            impl_type = best_impl.get("type")
            func_name = best_impl.get("function_call", best_impl.get("endpoint", "N/A"))
            priority = best_impl.get("priority", "N/A")
            
            logger.info(f"[ToolRegistry Start] Executing Capability '{target_cap_id}' via {impl_type} '{func_name}' (Priority: {priority}, Score: {score})")
            
            http_status = 200
            raw_backend_response = None
            exception_obj = None
            is_rest_attempt = (impl_type in ["api", "report"])

            # Route 1: Python Semantic Wrapper
            if impl_type == "wrapper":
                wrapper_func = SEMANTIC_TOOL_MAP.get(func_name)
                
                if wrapper_func:
                    try:
                        raw_backend_response = await wrapper_func(ctx)
                    except Exception as exc:
                        exception_obj = exc
                        logger.error(f"[ToolRegistry Wrapper Exception] Cap: {target_cap_id} | Func: {func_name} | Exception: {exc}")
                else:
                    exception_obj = RuntimeError(f"Semantic wrapper function '{func_name}' missing from SEMANTIC_TOOL_MAP.")
                    
            # Route 2: Backend API or Report
            elif impl_type in ["api", "report"]:
                if target_cap_id == "customer_resolution":
                    raw_backend_response = resolved_entities
                else:
                    method = best_impl.get("method", "GET").upper()
                    raw_endpoint = best_impl.get("endpoint", "")
                    
                    if " " in raw_endpoint:
                        method_str, path_str = raw_endpoint.split(" ", 1)
                        if method_str.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                            method = method_str.upper()
                            raw_endpoint = path_str
                    
                    endpoint_url = raw_endpoint
                    used_keys = set()

                    # Generic: inject any catalog-defined default_context values not already present
                    cap_meta_defaults = get_capability_metadata(target_cap_id) or {}
                    for def_key, def_val in cap_meta_defaults.get("default_context", {}).items():
                        if def_key not in ctx or not ctx[def_key]:
                            ctx[def_key] = def_val
                            
                    for k, v in ctx.items():
                        if f"{{{k}}}" in endpoint_url:
                            endpoint_url = endpoint_url.replace(f"{{{k}}}", str(v))
                            used_keys.add(k)
                            
                    if method == "GET":
                        import urllib.parse
                        NON_API_KEYS = {
                            "business_goal", "question", "intent", "scope", "metric", 
                            "operation", "aggregation", "ranking", "comparison", "group_by", 
                            "sort_order", "limit", "presentation_mode", "requires_report", 
                            "requires_summary", "requires_chart", "requires_table", 
                            "requires_comparison", "requires_export", "analysis_depth", 
                            "canonical_fy", "all_fiscal_years", "missing_information", 
                            "raw_tool_results", "previous_execution_plan", "user_context", 
                            "resolved_entities", "entity", "entity_type", "entity_id", "entity_name"
                        }
                        unused_params = {k: v for k, v in ctx.items() if k not in used_keys and k not in NON_API_KEYS}
                        
                        if "/api/v1/reports/project-recoverability-report" in endpoint_url or "/reports/project-recoverability-report" in endpoint_url:
                            if "page" not in unused_params:
                                unused_params["page"] = 1
                            if "pageSize" not in unused_params:
                                unused_params["pageSize"] = 10000
                            if "statusFilter" not in unused_params:
                                unused_params["statusFilter"] = -1

                        if "/api/v1/projects" in endpoint_url or "/api/v1/project?" in endpoint_url:
                            sq = {}
                            flat_params = {}
                            for k, v in unused_params.items():
                                if k in ["page", "pageSize", "sortDirection", "start_date", "end_date", "status", "is_active"]:
                                    flat_params[k] = v
                                else:
                                    sq[k] = v
                            if sq:
                                flat_params["searchQuery"] = json.dumps(sq)
                            unused_params = flat_params

                        if unused_params:
                            qs = urllib.parse.urlencode(unused_params)
                            if "?" in endpoint_url:
                                endpoint_url += f"&{qs}"
                            else:
                                endpoint_url += f"?{qs}"
                                
                    if not endpoint_url.startswith("http"):
                        if endpoint_url.startswith("/api/v1"):
                            endpoint_url = endpoint_url[7:]
                        endpoint_url = CRM_API_BASE.rstrip('/') + '/' + endpoint_url.lstrip('/')
                        
                    try:
                        async with aiohttp.ClientSession() as session:
                            auth_header = jwt_token if jwt_token.startswith("Bearer ") else f"Bearer {jwt_token}"
                            headers = {
                                "Authorization": auth_header,
                                "Content-Type": "application/json",
                                "Accept-Language": "en"
                            }
                            
                            json_data = None
                            if method in ["POST", "PUT", "PATCH"] and ctx:
                                json_data = {k: v for k, v in ctx.items() if f"{{{k}}}" not in raw_endpoint}
                            req_start_time = time.time()
                            log_stage(logger, "BACKEND_REQ", Method=method, Endpoint=func_name, Auth="Present" if jwt_token else "None")

                            async with session.request(method, endpoint_url, headers=headers, json=json_data, timeout=aiohttp.ClientTimeout(total=2.5)) as response:
                                http_status = response.status
                                body_text = await response.text()
                                req_exec_time = round((time.time() - req_start_time) * 1000, 2)
                                
                                if 200 <= response.status < 300:
                                    try:
                                        import json as _json
                                        raw_backend_response = _json.loads(body_text)
                                    except Exception:
                                        raw_backend_response = {"text": body_text}
                                else:
                                    exception_obj = RuntimeError(f"HTTP {response.status}: {body_text[:200]}")
                    except Exception as e:
                        exception_obj = e
                        log_error(logger, "BACKEND", str(e), Endpoint=func_name)

                    # Universal Fallback: If API attempt failed or returned error/empty payload, try semantic wrappers
                    is_invalid_payload = False
                    if not raw_backend_response:
                        is_invalid_payload = True
                    elif isinstance(raw_backend_response, dict):
                        # Check top-level error indicators
                        if raw_backend_response.get("status") in ["error", "failure", 400, 401, 403, 404, 500]:
                            is_invalid_payload = True
                        elif raw_backend_response.get("success") is False:
                            is_invalid_payload = True
                        elif "error_message" in raw_backend_response:
                            is_invalid_payload = True
                        elif "error" in raw_backend_response and not isinstance(raw_backend_response.get("error"), bool):
                            is_invalid_payload = True
                        else:
                            # Deep-check: Node backend wraps response in 'data' key
                            inner_data = raw_backend_response.get("data", raw_backend_response)
                            if isinstance(inner_data, dict):
                                if "error_message" in inner_data:
                                    is_invalid_payload = True
                                elif inner_data.get("status") in ["error", "failure"]:
                                    is_invalid_payload = True
                                elif inner_data.get("success") is False:
                                    is_invalid_payload = True

                    if exception_obj is not None or is_invalid_payload:
                        log_stage(logger, "BACKEND_FAIL", Capability=target_cap_id, Action="FALLBACK_WRAPPER", Reason=str(exception_obj) if exception_obj else "Invalid payload")
                        for fallback_impl in sorted(implementations, key=lambda x: x.get("priority", 99)):
                            if fallback_impl.get("type") == "wrapper":
                                f_func_name = fallback_impl.get("function_call")
                                f_wrapper = SEMANTIC_TOOL_MAP.get(f_func_name)
                                if f_wrapper:
                                    try:
                                        fallback_res = await f_wrapper(ctx)
                                        if fallback_res:
                                            raw_backend_response = fallback_res
                                            exception_obj = None
                                            impl_type = "wrapper (fallback)"
                                            func_name = f_func_name
                                            log_stage(logger, "BACKEND_FALLBACK", Capability=target_cap_id, Wrapper=f_func_name, Status="SUCCESS")
                                            break
                                    except Exception as fallback_exc:
                                        log_error(logger, "BACKEND_FALLBACK", str(fallback_exc), Wrapper=f_func_name)
            else:
                exception_obj = RuntimeError(f"Unknown implementation type '{impl_type}'.")

            exec_time_ms = round((time.time() - start_time) * 1000, 2)

            # Format capability envelope
            env = format_capability_envelope(target_cap_id, raw_backend_response, error_err=str(exception_obj) if exception_obj else None)
            
            # Secondary Failover: If envelope formatting resolved to an error/unavailable status and fallback hasn't executed yet, force semantic wrapper fallback
            if env.get("status") != "success" and impl_type != "wrapper (fallback)":
                for fallback_impl in sorted(implementations, key=lambda x: x.get("priority", 99)):
                    if fallback_impl.get("type") == "wrapper":
                        f_func_name = fallback_impl.get("function_call")
                        f_wrapper = SEMANTIC_TOOL_MAP.get(f_func_name)
                        if f_wrapper:
                            try:
                                fallback_res = await f_wrapper(ctx)
                                if fallback_res:
                                    raw_backend_response = fallback_res
                                    exception_obj = None
                                    impl_type = "wrapper (fallback)"
                                    func_name = f_func_name
                                    env = format_capability_envelope(target_cap_id, raw_backend_response)
                                    break
                            except Exception as fallback_exc:
                                pass
            
            fallback_used = (impl_type == "wrapper (fallback)")

            # Calculate record count cleanly for structured log
            rec_cnt = 0
            if isinstance(raw_backend_response, dict):
                rec_data = raw_backend_response.get("records") or raw_backend_response.get("data")
                if isinstance(rec_data, list):
                    rec_cnt = len(rec_data)
                elif isinstance(rec_data, dict):
                    inner_recs = rec_data.get("records") or rec_data.get("data")
                    rec_cnt = len(inner_recs) if isinstance(inner_recs, list) else 1
                else:
                    rec_cnt = 1
            elif isinstance(raw_backend_response, list):
                rec_cnt = len(raw_backend_response)

            log_stage(
                logger, "BACKEND",
                Status=http_status if http_status else 200,
                Endpoint=func_name,
                Rows=rec_cnt,
                LatencyMs=exec_time_ms,
                Fallback=fallback_used
            )
            log_debug_payload(logger, "BACKEND", raw_backend_response, max_rows=3)
            log_stage(logger, "EXECUTE", Capability=target_cap_id, Status=env.get("status", "success"))
            return {
                "capability": target_cap_id,
                "intent": node_intent,
                "result": env["payload"],
                "status": env["status"],
                "confidence": env["confidence"],
                "source": env["source"],
                "implementation_type": impl_type,
                "priority": priority,
                "function_call": func_name,
                "execution_time_ms": exec_time_ms,
                "http_status": http_status,
                "error": env["payload"].get("error_message") if env["status"] != "success" else None
            }

        tasks = [execute_node(n) for n in execution_graph]
        results = await asyncio.gather(*tasks)
        logger.info(f"[TOOL REGISTRY RETURN] Completed execution of {len(results)} nodes. Returning results.")
        return list(results)

# Singleton instance
tool_registry = ToolRegistry()
