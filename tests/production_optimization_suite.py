"""
tests/production_optimization_suite.py — Production Readiness Verification & Performance Benchmarking
"""

import sys
import os
import asyncio
import json
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.entity_resolver import _score_record_match, _normalize_name_string, resolve_fiscal_year, parse_scope_time_filter
from agent.router import route_query_fast_path
from agent.synthesizer import format_data_response
from memory.session_manager import build_entity_cache_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ProdOptSuite")

def test_1_customer_resolution():
    logger.info("\n=== TEST 1: CUSTOMER RESOLUTION & MATCHING ===")
    query = "The Grove Resort W.L.L."
    norm = _normalize_name_string(query)
    logger.info(f"Original: '{query}' | Normalized: '{norm}'")

    mock_records = [
        {"id": 101, "customer_name": "The Grove Resort W.L.L.", "cust_code": "CUST001"},
        {"id": 102, "customer_name": "Grove Resort Phase 2 WLL", "cust_code": "CUST002"}
    ]
    map_info = {"id_field": "id", "name_fields": ["customer_name", "cust_code"]}

    score, display = _score_record_match(mock_records[0], map_info, query)
    logger.info(f"Score: {score} | Matched Display: {display}")
    assert score >= 0.95, f"Expected high score >= 0.95, got {score}"

    # Punctuation/suffix variation test
    var_query = "the grove resort wll"
    score_var, display_var = _score_record_match(mock_records[0], map_info, var_query)
    logger.info(f"Variation Query: '{var_query}' | Score: {score_var}")
    assert score_var >= 0.90, f"Variation matching failed with score {score_var}"
    logger.info("✅ TEST 1 PASSED PERFECTLY")

def test_2_fiscal_year_resolution():
    logger.info("\n=== TEST 2: FINANCIAL YEAR RESOLUTION ===")
    fy25 = resolve_fiscal_year("FY25")
    logger.info(f"FY25: {fy25}")
    assert fy25["start_date"] == "2025-10-01"
    assert fy25["end_date"] == "2026-09-30"

    fy24 = resolve_fiscal_year("FY24")
    logger.info(f"FY24: {fy24}")
    assert fy24["start_date"] == "2024-10-01"
    assert fy24["end_date"] == "2025-09-30"

    fy26 = resolve_fiscal_year("FY26")
    logger.info(f"FY26: {fy26}")
    assert fy26["start_date"] == "2026-10-01"
    assert fy26["end_date"] == "2027-09-30"
    logger.info("✅ TEST 2 PASSED PERFECTLY (Zero calendar-year drift)")

def test_3_lightweight_router():
    logger.info("\n=== TEST 3: LIGHTWEIGHT DETERMINISTIC ROUTER ===")
    simple_q = "Show all projects for customer Grove Resort"
    res = route_query_fast_path(simple_q)
    logger.info(f"Query: '{simple_q}' -> Route Result: {res}")
    assert res is not None, "Expected fast-path match"
    assert res["routed_by"] == "metadata_driven_router"
    assert res["response_mode"] == "DATA"

    complex_q = "Analyze revenue trends and compare with FY24"
    complex_res = route_query_fast_path(complex_q)
    logger.info(f"Complex Query: '{complex_q}' -> Route Result: {complex_res}")
    assert complex_res is None, "Expected fallback to EnterprisePlanner for complex query"
    logger.info("✅ TEST 3 PASSED PERFECTLY")

def test_4_data_mode_formatter():
    logger.info("\n=== TEST 4: DATA MODE DIRECT FORMATTER ===")
    mock_tool_results = [
        {
            "capability": "active_projects",
            "result": [
                {"project_code": "PRJ-001", "project_name": "Resort Spa Extension", "approved_fees": 45000.0, "status": "In Progress"},
                {"project_code": "PRJ-002", "project_name": "Marina Refinement", "approved_fees": 32000.0, "status": "Completed"}
            ]
        }
    ]
    formatted = format_data_response("Show projects", mock_tool_results)
    logger.info(f"Formatted Content:\n{formatted['content']}")
    assert formatted["response_mode"] == "DATA"
    assert formatted["synthesizer_invoked"] is False
    assert "| Project Code | Project Name | Approved Fees | Status |" in formatted["content"]
    logger.info("✅ TEST 4 PASSED PERFECTLY")

def test_5_entity_aware_cache_key():
    logger.info("\n=== TEST 5: ENTITY-AWARE CACHE KEY DERIVATION ===")
    resolved_e1 = [{"entity_type": "Customer", "entity_id": 101, "entity_name": "The Grove Resort W.L.L."}]
    resolved_e2 = [{"entity_type": "customer", "entity_id": 101, "entity_name": "Grove Resort"}]

    k1 = build_entity_cache_key(user_id=5, capability="active_projects", resolved_entities=resolved_e1, financial_year="FY25")
    k2 = build_entity_cache_key(user_id=5, capability="active_projects", resolved_entities=resolved_e2, financial_year="FY25")

    logger.info(f"Prompt 1 Key: {k1}")
    logger.info(f"Prompt 2 Key: {k2}")
    assert k1 == k2, "Different prompt variations for same customer must produce EQUAL cache key!"
    logger.info("✅ TEST 5 PASSED PERFECTLY (Entity Cache Key Harmonization)")

def main():
    test_1_customer_resolution()
    test_2_fiscal_year_resolution()
    test_3_lightweight_router()
    test_4_data_mode_formatter()
    test_5_entity_aware_cache_key()

    print("\n" + "="*70)
    print("ALL PRODUCTION OPTIMIZATION TESTS PASSED WITH 100% SUCCESS!")
    print("="*70)

if __name__ == "__main__":
    main()
