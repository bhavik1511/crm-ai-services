"""
entity_resolver.py — Production-grade Entity Resolver.
Identifies entities from the LLM, validates them against existing Node.js APIs, 
and returns standardized JSON structures with Exact Matches, Clarifications, or Failures.
"""
import logging
import json
import urllib.request
import urllib.parse
import asyncio
from typing import Dict, Any, Tuple, List

logger = logging.getLogger(__name__)

import os

CRM_API_BASE = os.getenv('CRM_API_BASE', 'http://localhost:3001/api/v1').rstrip('/')


# Entity to API Mapping
ENTITY_API_MAP = {
    "customer": {"endpoint": "/customer", "id_field": "id", "name_fields": ["customer_name", "cust_code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "project": {"endpoint": "/projects", "id_field": "id", "name_fields": ["name", "code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "proposal": {"endpoint": "/proposal", "id_field": "id", "name_fields": ["subject", "proposal_code"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "employee": {"endpoint": "/employee", "id_field": "id", "name_fields": ["employee_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "invoice": {"endpoint": "/invoice", "id_field": "id", "name_fields": ["invoice_no"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "task": {"endpoint": "/project-task", "id_field": "id", "name_fields": ["task_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "lead": {"endpoint": "/saleslead", "id_field": "id", "name_fields": ["company_name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "department": {"endpoint": "/master/department", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "office": {"endpoint": "/master/offices", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5},
    "service_line": {"endpoint": "/master/service-line", "id_field": "id", "name_fields": ["name"], "search_parameter": "searchQuery", "search_payload_format": "json", "search_key": "search", "default_limit": 5}
}

async def fetch_entity_from_api(entity_type: str, search_query: str, jwt_token: str) -> Dict[str, Any]:
    """
    Makes the actual HTTP GET call to the CRM backend.
    Runs in a thread to prevent blocking the async loop.
    """
    map_info = ENTITY_API_MAP.get(entity_type.lower())
    if not map_info:
        return {"error": f"Unknown entity type: {entity_type}"}

    search_param = map_info.get("search_parameter", "search")
    search_format = map_info.get("search_payload_format", "string")
    search_key = map_info.get("search_key", "search")
    limit = map_info.get("default_limit", 5)

    if search_format == "json":
        payload = {search_key: search_query}
        payload_str = json.dumps(payload)
        safe_query = urllib.parse.quote(payload_str)
    else:
        safe_query = urllib.parse.quote(search_query)

    url = f"{CRM_API_BASE}{map_info['endpoint']}?{search_param}={safe_query}&pageSize={limit}"
    
    logger.info(f"[EntityResolver DEBUG] Outgoing URL: GET {url}")
    logger.info(f"[EntityResolver DEBUG] Outgoing Params: {search_param}={safe_query}&pageSize={limit}")

    def _sync_fetch():
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {jwt_token}'})
        try:
            with urllib.request.urlopen(req) as response:
                status = response.status
                body = response.read().decode('utf-8')
                logger.info(f"[EntityResolver DEBUG] Response Status: {status}")
                logger.info(f"[EntityResolver DEBUG] Response Body: {body[:500]}...") # truncate for safety
                return json.loads(body)
        except urllib.error.HTTPError as e:
            logger.error(f"[EntityResolver DEBUG] Response Status: {e.code}")
            try:
                err_body = e.read().decode('utf-8')
                logger.error(f"[EntityResolver DEBUG] Response Body: {err_body}")
            except:
                pass
            logger.error(f"[EntityResolver] Backend HTTP {e.code}: {e.reason}")
            return {"error": f"API Error: {e.code}"}
        except Exception as e:
            logger.error(f"[EntityResolver] Backend Request Failed: {str(e)}")
            return {"error": f"Connection Failed: {str(e)}"}

    return await asyncio.to_thread(_sync_fetch)

def _build_display_name(record: Dict[str, Any], name_fields: List[str]) -> str:
    """Builds a human-readable name from the API record using the mapped name fields."""
    parts = []
    for f in name_fields:
        val = record.get(f)
        if val:
            parts.append(str(val))
    return " - ".join(parts) if parts else "Unknown"

async def resolve_entities(extracted_entities: List[Dict[str, str]], jwt_token: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Resolves multiple entities concurrently and standardizes the output.
    Returns a tuple: (list of resolved entities, list of clarification strings).
    """
    if not extracted_entities:
        return [], []

    logger.info(f"Resolving {len(extracted_entities)} entities...")
    
    # Process multiple entities concurrently
    tasks = []
    for entity in extracted_entities:
        e_type = entity.get("type", "").lower()
        e_value = entity.get("value", "")
        tasks.append(_resolve_single_entity(e_type, e_value, jwt_token))
        
    results = await asyncio.gather(*tasks)
    
    resolved = []
    clarifications = []
    
    for res in results:
        status = res.get("status")
        e_type = res.get("entity_type", "Entity")
        query = res.get("query", "Unknown")
        
        if status == "clarification_required":
            matches = res.get("matches", [])
            match_names = [str(m.get("entity_name")) for m in matches]
            clarifications.append(f"I found multiple matches for {e_type} '{query}': **{', '.join(match_names)}**. Which one did you mean?")
        elif status == "not_found":
            clarifications.append(f"I could not find a {e_type} matching '{query}'. Please verify the name and try again.")
        elif status == "error":
            clarifications.append(f"I encountered an error looking up {e_type} '{query}': {res.get('message')}")
        else:
            resolved.append(res)
            
    return resolved, clarifications

async def _resolve_single_entity(e_type: str, e_value: str, jwt_token: str) -> Dict[str, Any]:
    """Resolves a single entity and returns the standardized JSON format."""
    # 1. Validation
    if not e_value:
        return {
            "status": "error",
            "entity_type": e_type,
            "message": "Empty search query provided."
        }
        
    map_info = ENTITY_API_MAP.get(e_type)
    if not map_info:
        # Pass-through generic entities that don't need API validation (like 'FinancialYear')
        return {
            "status": "pass_through",
            "entity_type": e_type,
            "entity_value": e_value
        }

    # 2. API Call
    response = await fetch_entity_from_api(e_type, e_value, jwt_token)
    
    # 3. Handle API Failure
    if "error" in response:
        return {
            "status": "error",
            "entity_type": e_type.capitalize(),
            "query": e_value,
            "message": response["error"]
        }
        
    # 4. Parse Results
    if isinstance(response, dict):
        if "rows" in response:
            data = response["rows"]
        elif "data" in response:
            data = response["data"]
        else:
            # Maybe the object itself is the single entity or a wrapper
            data = [response] if response else []
    elif isinstance(response, list):
        data = response
    else:
        data = []
    
    # 5. Handle No Match
    if not data or len(data) == 0:
        return {
            "status": "not_found",
            "entity_type": e_type.capitalize(),
            "query": e_value
        }
        
    # 6. Handle Exact Match (Assuming exact if only 1 returned, or if top match is very confident)
    # Note: A real implementation might do string similarity scoring here. We assume API sorting puts best match first.
    if len(data) == 1:
        record = data[0]
        display_name = _build_display_name(record, map_info["name_fields"])
        return {
            "status": "resolved",
            "entity_type": e_type.capitalize(),
            "entity_name": display_name,
            "entity_id": record.get(map_info["id_field"]),
            "confidence": 0.98,
            "raw_record": record # Store raw for advanced planner logic if needed
        }
        
    # 7. Handle Multiple Matches
    matches_list = []
    for record in data[:3]: # Limit to top 3 for clarification
        matches_list.append({
            "entity_id": record.get(map_info["id_field"]),
            "entity_name": _build_display_name(record, map_info["name_fields"])
        })
        
    return {
        "status": "clarification_required",
        "entity_type": e_type.capitalize(),
        "query": e_value,
        "matches": matches_list
    }
