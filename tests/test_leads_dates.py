from database import get_db_engine
from sqlalchemy import text
from datetime import datetime

def test_pipeline_leads_dates():
    engine = get_db_engine()
    with engine.connect() as conn:
        print("--- Testing Backend Repository Query WITH DATES ---")
        # Same as dashboard: start of fiscal year to today
        start_date = "2025-10-01"
        end_date = datetime.now().strftime("%Y-%m-%d")
        print(f"Using Date Range: {start_date} to {end_date}")
        q2 = f"""
        SELECT ls.id, ls.name, COUNT(sl.id) as totalEntries, SUM(sl.budget_value) as totalBudget
        FROM m_leadstatus ls
        LEFT JOIN saleslead sl ON sl.lead_status_id = ls.id 
                              AND sl.lead_date BETWEEN '{start_date}' AND '{end_date} 23:59:59'
                              AND sl.job_estimation_id IS NULL
        GROUP BY ls.id, ls.name
        ORDER BY ls.id
        """
        res2 = conn.execute(text(q2)).fetchall()
        print("Dashboard Date-Filtered Query Result:")
        for row in res2:
            print(f"ID {row[0]} ({row[1]}): Entries={row[2]}, Budget={row[3]}")
            
if __name__ == "__main__":
    test_pipeline_leads_dates()
