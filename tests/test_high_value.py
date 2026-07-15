import pymysql
import os
from dotenv import load_dotenv

load_dotenv()

conn = pymysql.connect(
    host=os.getenv("DB_HOST"),
    port=int(os.getenv("DB_PORT", 3306)),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PWD"),
    database=os.getenv("DB_NAME")
)

try:
    with conn.cursor() as cursor:
        query = """
        SELECT 
            p.id, 
            c.customer_name as client_name, 
            co.first_name as contact_name, 
            p.total_costs, 
            DATEDIFF(CURDATE(), p.created_at) as age_in_days 
        FROM proposal p 
        LEFT JOIN customers c ON p.client_id = c.id 
        LEFT JOIN contacts co ON p.contact_id = co.id 
        ORDER BY p.total_costs DESC 
        LIMIT 5
        """
        cursor.execute(query)
        print("Top 5 proposals by total_costs:")
        for row in cursor.fetchall():
            print(row)
except Exception as e:
    print(e)
finally:
    conn.close()
