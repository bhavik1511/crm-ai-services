"""
tool_registry.py — The Dynamic Implementation Resolver.
This module bridges the abstract Business Analyst Planner with the physical execution layer.
It reads the Business Capabilities requested by the Planner, dynamically resolves 
the highest-priority implementation from the capability_catalog, and returns executable closures.
"""
import logging
from typing import Dict, Any, List

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.capability_catalog import get_capability_metadata
from agent.semantic_wrappers import SEMANTIC_TOOL_MAP

logger = logging.getLogger(__name__)

class ToolRegistry:
    def __init__(self):
        pass
        
    def resolve_implementations(self, capabilities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Takes a list of abstract capabilities selected by the Planner.
        Passes all implementations to the execution phase where dynamic selection occurs
        based on available resolved entities.
        """
        execution_graph = []
        
        for cap in capabilities:
            cap_id = cap.get("id")
            context = cap.get("context", {})
            
            metadata = get_capability_metadata(cap_id)
            if not metadata:
                logger.error(f"Capability '{cap_id}' not found in catalog.")
                execution_graph.append({"error": f"Capability {cap_id} is missing."})
                continue
                
            # Sort implementations by priority (lowest number = highest priority)
            implementations = metadata.get("implementations", [])
            implementations = sorted(implementations, key=lambda x: x.get("priority", 99))
            
            if not implementations:
                logger.error(f"No implementations registered for '{cap_id}'.")
                execution_graph.append({"error": f"No implementations for {cap_id}."})
                continue
                
            executable_node = {
                "capability_id": cap_id,
                "implementations": implementations, # Pass all implementations
                "context": context
            }
            execution_graph.append(executable_node)
            
        return execution_graph

    def score_and_select_implementation(self, capability_id: str, implementations: List[Dict[str, Any]], context_keys: List[str], resolved_entities: List[Dict[str, Any]]) -> Dict[str, Any]:
        import os
        debug_mode = os.getenv("AI_DEBUG_MODE", "false").lower() == "true"
        
        # Build available context keys (context params + resolved entity IDs)
        available_keys = set(context_keys)
        for ent in resolved_entities:
            ent_type = ent.get("type", "").lower()
            if ent_type:
                available_keys.add(f"{ent_type}_id")
                
        if debug_mode:
            logger.info(f"\n[AI_DEBUG_MODE] --- EVALUATING CAPABILITY: {capability_id} ---")
            logger.info(f"[AI_DEBUG_MODE] Resolved Entities: {resolved_entities}")
            logger.info(f"[AI_DEBUG_MODE] Available Context Keys: {available_keys}")
            
        best_impl = None
        highest_score = -999999
        missing_params_for_best = []
        
        for impl in implementations:
            req_entities = impl.get("required_entities", [])
            req_params = impl.get("required_parameters", [])
            
            # 1. HARD GATE: Entity Match
            disqualified = False
            for ent in req_entities:
                if f"{ent}_id" not in available_keys:
                    disqualified = True
                    break
                    
            if disqualified:
                if debug_mode:
                    logger.info(f"[AI_DEBUG_MODE] CANDIDATE: {impl.get('function_call') or impl.get('endpoint')} -> DISQUALIFIED (Missing required entity: {req_entities})")
                continue
                
            # 2. SCORE CALCULATION
            entity_score = len(req_entities)
            param_score = sum(1 for p in req_params if p in available_keys)
            priority_score = 10 - impl.get("priority", 5)
            
            total_score = (entity_score * 1000) + (param_score * 100) + priority_score
            
            if debug_mode:
                logger.info(f"[AI_DEBUG_MODE] CANDIDATE: {impl.get('function_call') or impl.get('endpoint')} | Entity: {entity_score*1000} | Param: {param_score*100} | Prio: {priority_score} | TOTAL: {total_score}")
                
            if total_score > highest_score:
                highest_score = total_score
                best_impl = impl
                missing_params_for_best = [p for p in req_params if p not in available_keys]
                
        if debug_mode:
            if best_impl:
                logger.info(f"[AI_DEBUG_MODE] >>> SELECTED: {best_impl.get('function_call') or best_impl.get('endpoint')} (Score: {highest_score})")
                if missing_params_for_best:
                    logger.info(f"[AI_DEBUG_MODE] >>> MISSING PARAMS: {missing_params_for_best}")
            else:
                logger.info(f"[AI_DEBUG_MODE] >>> NO VALID IMPLEMENTATION FOUND")
                
        return {
            "implementation": best_impl,
            "missing_parameters": missing_params_for_best
        }

    async def execute_resolved_implementations(self, execution_graph: List[Dict[str, Any]], resolved_entities: List[Dict[str, Any]], jwt_token: str) -> List[Dict[str, Any]]:
        """
        Executes the resolved implementations (Wrappers, APIs, Reports).
        Note: Actual concurrent execution mapping.
        """
        results = []
        import asyncio
        
        async def execute_node(node):
            import os
            debug_mode = os.getenv("AI_DEBUG_MODE", "false").lower() == "true"
            
            if "error" in node:
                return {"capability": node.get("capability_id", "Unknown"), "error": node["error"]}
                
            ctx = node["context"]
            cap_id = node["capability_id"]
            
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
                        
            # 2. Dynamically select the correct implementation based on context
            implementations = node.get("implementations", [])
            if "implementation" in node:
                implementations = [node["implementation"]] # Fallback
                
            selection_result = self.score_and_select_implementation(cap_id, implementations, list(ctx.keys()), resolved_entities)
            best_impl = selection_result["implementation"]
            missing_params = selection_result["missing_parameters"]
                    
            if not best_impl:
                return {"capability": cap_id, "error": "No valid implementation found for available context and entities."}
                
            if missing_params:
                # Execution Validator should have caught this, but just in case
                return {"capability": cap_id, "error": f"Missing required parameters: {missing_params}"}

            if debug_mode:
                logger.info(f"[AI_DEBUG_MODE] Parameters injected into {cap_id}: {ctx}")

            impl_type = best_impl.get("type")
            
            if debug_mode:
                if impl_type == "wrapper":
                    logger.info(f"[AI_DEBUG_MODE] Final wrapper executed: {best_impl.get('function_call')}")
                elif impl_type in ["api", "report"]:
                    logger.info(f"[AI_DEBUG_MODE] Final endpoint executed: {best_impl.get('endpoint')}")
                    
            impl = best_impl
            
            # Route 1: Python Semantic Wrapper
            if impl_type == "wrapper":
                func_name = impl.get("function_call")
                wrapper_func = SEMANTIC_TOOL_MAP.get(func_name)
                
                if wrapper_func:
                    res = await wrapper_func(ctx)
                    return {"capability": cap_id, "result": res}
                else:
                    return {"capability": cap_id, "error": f"Semantic wrapper {func_name} missing."}
                    
            # Route 2: Backend API or Report
            elif impl_type in ["api", "report"]:
                if cap_id == "customer_resolution":
                    return {"capability": cap_id, "result": resolved_entities}
                
                method = impl.get("method", "GET").upper()
                raw_endpoint = impl.get("endpoint", "")
                
                # If endpoint contains method, extract it
                if " " in raw_endpoint:
                    method_str, path_str = raw_endpoint.split(" ", 1)
                    if method_str.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                        method = method_str.upper()
                        raw_endpoint = path_str
                
                # Format URL parameters (e.g., {customer_id})
                endpoint_url = raw_endpoint
                used_keys = set()
                
                # Inject default dates for reports that strictly require them but might not be extracted
                if cap_id in ["kpi_summary", "revenue_analysis"]:
                    if "start_date" not in ctx:
                        ctx["start_date"] = "2025-01-01"
                    if "end_date" not in ctx:
                        ctx["end_date"] = "2025-12-31"
                        
                for k, v in ctx.items():
                    if f"{{{k}}}" in endpoint_url:
                        endpoint_url = endpoint_url.replace(f"{{{k}}}", str(v))
                        used_keys.add(k)
                        
                if method == "GET":
                    import urllib.parse
                    unused_params = {k: v for k, v in ctx.items() if k not in used_keys}
                    if unused_params:
                        qs = urllib.parse.urlencode(unused_params)
                        if "?" in endpoint_url:
                            endpoint_url += f"&{qs}"
                        else:
                            endpoint_url += f"?{qs}"
                            
                if not endpoint_url.startswith("http"):
                    from agent.entity_resolver import CRM_API_BASE
                    if endpoint_url.startswith("/api/v1"):
                        endpoint_url = endpoint_url[7:]
                    endpoint_url = CRM_API_BASE.rstrip('/') + '/' + endpoint_url.lstrip('/')
                    
                print(f"DEBUG URL: {endpoint_url}")

                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        auth_header = jwt_token if jwt_token.startswith("Bearer ") else f"Bearer {jwt_token}"
                        headers = {
                            "Authorization": auth_header,
                            "Content-Type": "application/json",
                            "Accept-Language": "en"
                        }
                        
                        # Data for POST/PUT (only include items not used in URL)
                        json_data = None
                        if method in ["POST", "PUT", "PATCH"] and ctx:
                            json_data = {k: v for k, v in ctx.items() if f"{{{k}}}" not in raw_endpoint}
                            
                        async with session.request(method, endpoint_url, headers=headers, json=json_data, timeout=15.0) as response:
                            if 200 <= response.status < 300:
                                try:
                                    res_data = await response.json()
                                    return {"capability": cap_id, "result": res_data}
                                except Exception:
                                    text_data = await response.text()
                                    return {"capability": cap_id, "result": {"text": text_data}}
                            else:
                                text_data = await response.text()
                                return {"capability": cap_id, "error": f"HTTP {response.status}: {text_data}"}
                except Exception as e:
                    return {"capability": cap_id, "error": f"API Request Failed: {str(e)}"}
                
            return {"capability": cap_id, "error": "Unknown implementation type."}

        tasks = [execute_node(n) for n in execution_graph]
        results = await asyncio.gather(*tasks)
        return list(results)

# Singleton instance
tool_registry = ToolRegistry()
