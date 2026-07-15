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

with conn.cursor() as cursor:
    cursor.execute("SELECT is_active, COUNT(*), SUM(total_amt_ex_vat) FROM invoice WHERE created_at BETWEEN '2025-10-01' AND '2025-12-31 23:59:59' GROUP BY is_active")
    print("Oct-Dec 2025 invoices by is_active:")
    for row in cursor.fetchall():
        print(f"is_active={row[0]}: Count={row[1]}, Sum={row[2]}")

    cursor.execute("SELECT is_active, COUNT(*), SUM(total_amt_ex_vat) FROM invoice WHERE created_at BETWEEN '2026-01-01' AND '2026-03-31 23:59:59' GROUP BY is_active")
    print("\nJan-Mar 2026 invoices by is_active:")
    for row in cursor.fetchall():
        print(f"is_active={row[0]}: Count={row[1]}, Sum={row[2]}")
