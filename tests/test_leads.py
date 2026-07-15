from database import get_db_engine
from sqlalchemy import text
import pprint

def test_pipeline_leads():
    engine = get_db_engine()
    with engine.connect() as conn:
        print("--- Testing Semantic Layer Query for Open Leads ---")
        q1 = """
        SELECT COUNT(*) as count, ROUND(COALESCE(SUM(sl.budget_value), 0), 2) as value 
        FROM saleslead sl 
        JOIN m_leadstatus ls ON sl.lead_status_id = ls.id 
        WHERE ls.name = 'Open'
        """ # Removed date filter just to see total globally
        res1 = conn.execute(text(q1)).fetchall()
        print("Bot Query Result:")
        print(res1)
        
        print("\n--- Testing Backend Repository Query ---")
        q2 = """
        SELECT ls.id, ls.name, COUNT(sl.id) as totalEntries, SUM(sl.budget_value) as totalBudget
        FROM m_leadstatus ls
        LEFT JOIN saleslead sl ON sl.lead_status_id = ls.id
        GROUP BY ls.id, ls.name
        ORDER BY ls.id
        """
        res2 = conn.execute(text(q2)).fetchall()
        print("Dashboard Query Result (equivalent to statusWiseResults):")
        for row in res2:
            print(f"ID {row[0]} ({row[1]}): Entries={row[2]}, Budget={row[3]}")
            
if __name__ == "__main__":
    test_pipeline_leads()
