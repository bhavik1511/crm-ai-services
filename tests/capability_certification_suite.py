"""
capability_certification_suite.py — Automated Offline Certification Suite.
Validates capability contracts, response_schema whitelist filtering,
zero metric substitution, clean error masking, and dashboard parity.

This file is maintained completely outside runtime execution.
"""
import unittest
import asyncio
import sys
import os

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from registry.capability_catalog import get_capability_metadata, BUSINESS_CAPABILITIES
from registry.tool_registry import format_capability_envelope
from agent.synthesizer import synthesize_response, trim_report_payload

class TestCapabilityCertificationSuite(unittest.TestCase):

    def test_01_catalog_contracts_exist(self):
        """Verify all catalog entries declare primary_metric, response_schema, and default_error_message."""
        for cap in BUSINESS_CAPABILITIES:
            cap_id = cap.get("id")
            self.assertIn("primary_metric", cap, f"Capability {cap_id} missing primary_metric")
            self.assertIn("response_schema", cap, f"Capability {cap_id} missing response_schema")
            self.assertIn("default_error_message", cap, f"Capability {cap_id} missing default_error_message")

    def test_02_whitelist_filtering(self):
        """Verify format_capability_envelope strips unwhitelisted fields (e.g. Revenue in Active Projects)."""
        raw_payload = {
            "strictly_active_projects_count": 32,
            "total_revenue_ytd": 9999999,  # Unauthorized metric
            "unrelated_field": "leak_test"  # Unauthorized field
        }
        envelope = format_capability_envelope("kpi_summary", raw_payload)
        
        self.assertEqual(envelope["status"], "success")
        payload = envelope["payload"]
        self.assertIn("strictly_active_projects_count", payload)
        self.assertNotIn("total_revenue_ytd", payload, "Revenue leaked into active projects capability payload!")
        self.assertNotIn("unrelated_field", payload, "Unwhitelisted field leaked into payload!")

    def test_03_zero_metric_substitution_on_failure(self):
        """Verify non-success envelope short-circuits synthesizer with default error message."""
        failed_tool_results = [{
            "capability": "receivables_analysis",
            "status": "error",
            "confidence": "unavailable",
            "source": "receivables_analysis",
            "result": {
                "error_message": "Receivables metrics are currently unavailable. Please try again later."
            }
        }]
        
        async def run_synth():
            return await synthesize_response("What are our total receivables?", failed_tool_results, llm_client=None)

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(run_synth())
        loop.close()

        self.assertEqual(res["type"], "done")
        self.assertIn("Receivables metrics are currently unavailable", res["content"])
        self.assertNotIn("total_revenue_ytd", str(res["content"]), "Revenue substituted for receivables on failure!")

    def test_04_clean_error_masking(self):
        """Verify internal technical errors do not leak stack traces or SQL into user responses."""
        error_tool_results = [{
            "capability": "receivables_analysis",
            "status": "error",
            "error": "Receivables metrics are currently unavailable. Please try again later."
        }]

        async def run_synth():
            return await synthesize_response("Top 10 overdue invoices", error_tool_results, llm_client=None)

        loop = asyncio.new_event_loop()
        res = loop.run_until_complete(run_synth())
        loop.close()

        content = res["content"]
        self.assertNotIn("SQL", content)
        self.assertNotIn("Query execution error", content)
        self.assertNotIn("Error Context", content)
        self.assertNotIn("Next Steps", content)

    def test_05_trim_report_payload_scoping(self):
        """Verify trim_report_payload only retains scoped metrics for given capability."""
        raw_data = {
            "total_receivables": 450000.0,
            "ageing_buckets": {"0-30": 100000, "31-60": 350000},
            "unrelated_revenue": 1200000.0
        }
        trimmed = trim_report_payload("receivables_analysis", raw_data)
        self.assertIn("total_receivables", trimmed)
        self.assertNotIn("unrelated_revenue", trimmed)

if __name__ == "__main__":
    unittest.main()
