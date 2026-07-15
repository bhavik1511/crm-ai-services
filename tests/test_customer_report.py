"""
Test script for the get_comprehensive_customer_report semantic tool.
Directly invokes the tool to verify it queries the database correctly.
"""
import sys
import json
sys.path.insert(0, '.')

from semantic_layer import get_comprehensive_customer_report

def test_customer_report():
    # Test 1: Search with a common term that should find customers
    print("=" * 60)
    print("TEST 1: Searching for a customer...")
    print("=" * 60)
    
    # First, let's find some customers to work with
    from database import get_db_engine
    from sqlalchemy import text
    engine = get_db_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT customer_name, cust_code FROM customers WHERE is_active = 1 LIMIT 5"))
        rows = result.fetchall()
        print("\nActive customers in DB:")
        for row in rows:
            print(f"  - {row[0]} (Code: {row[1]})")
        
        if not rows:
            print("No active customers found in database!")
            return

        # Use the first customer for testing
        test_name = rows[0][0]
    
    print(f"\n--- Running report for: '{test_name}' ---\n")
    result = get_comprehensive_customer_report.invoke({"search_term": test_name})
    
    try:
        data = json.loads(result)
        
        if "error" in data:
            print(f"ERROR: {data['error']}")
            return
        
        if "multiple_matches" in data:
            print(f"Multiple matches: {data['message']}")
            for m in data['matches']:
                print(f"  - {m['name']} ({m['code']})")
            return
        
        # Print summary
        print(f"Customer: {data.get('customer_name', 'N/A')}")
        print(f"Report Generated: {data.get('report_generated_at', 'N/A')}")
        
        ident = data.get('identification', {})
        print(f"\n--- IDENTIFICATION ---")
        print(f"  Name: {ident.get('customer_name', 'N/A')}")
        print(f"  Code: {ident.get('cust_code', 'N/A')}")
        print(f"  CR No: {ident.get('cust_cr_no', 'N/A')}")
        print(f"  Email: {ident.get('cust_email', 'N/A')}")
        print(f"  Industry: {ident.get('industry', 'N/A')}")
        print(f"  Country: {ident.get('country', 'N/A')}")
        print(f"  Client Relation: {ident.get('client_relation_manager', 'N/A')}")
        
        contacts = data.get('contacts', [])
        print(f"\n--- CONTACTS ({len(contacts)} found) ---")
        for c in contacts[:3]:
            print(f"  - {c.get('contact_name', 'N/A')} | {c.get('email_id', 'N/A')}")
        
        proj = data.get('projects', {})
        print(f"\n--- PROJECTS ---")
        print(f"  Total: {proj.get('total_projects', 0)}")
        print(f"  Running: {proj.get('running', 0)}")
        print(f"  Completed: {proj.get('completed', 0)}")
        print(f"  Total Value: BHD {proj.get('total_value', 0)}")
        
        pipe = data.get('pipeline', {})
        print(f"\n--- PIPELINE ---")
        print(f"  Leads: {pipe.get('total_leads', 0)} (Open: {pipe.get('open_leads', 0)})")
        print(f"  Job Estimations: {pipe.get('total_job_estimations', 0)}")
        print(f"  Proposals: {pipe.get('total_proposals', 0)}")
        
        inv = data.get('invoices_and_receivables', {})
        print(f"\n--- INVOICES & RECEIVABLES ---")
        print(f"  Total Invoices: {inv.get('total_invoices', 0)}")
        print(f"  Total Invoiced: BHD {inv.get('total_invoiced_amount', 0)}")
        print(f"  Total Paid: BHD {inv.get('total_paid_amount', 0)}")
        print(f"  Total Outstanding: BHD {inv.get('total_outstanding', 0)}")
        print(f"  Collection Rate: {inv.get('collection_rate_pct', 0)}%")
        
        aging = data.get('aging_buckets', [])
        print(f"\n--- AGING BUCKETS ({len(aging)} buckets) ---")
        for a in aging:
            print(f"  {a.get('bucket', 'N/A')}: BHD {a.get('outstanding_amount', 0)} ({a.get('invoice_count', 0)} invoices)")
        
        cn = data.get('credit_notes', {})
        print(f"\n--- CREDIT NOTES ---")
        print(f"  Total: {cn.get('total_credit_notes', 0)}")
        print(f"  Total Amount: BHD {cn.get('total_credit_amount', 0)}")
        
        kpi = data.get('kpi_summary', {})
        print(f"\n--- KPI SUMMARY ---")
        for k, v in kpi.items():
            print(f"  {k}: {v}")
        
        print(f"\n{'='*60}")
        print("TEST PASSED ✅ — Report generated successfully!")
        print(f"{'='*60}")
        
    except json.JSONDecodeError:
        print(f"RAW RESULT (not JSON):\n{result}")

if __name__ == "__main__":
    test_customer_report()
