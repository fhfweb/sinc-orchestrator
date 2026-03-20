
import psycopg
import os

conninfo = "dbname=orchestrator_tasks user=orchestrator password=9de4c0a8660df01207a8a773e2dcfdb8 host=localhost port=5432"
sql_file = "g:/Fernando/project0/ai-orchestrator/database/migrations/orchestrator_optimization_v1.sql"

try:
    with open(sql_file, "r") as f:
        sql = f.read()
    
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            # We need to execute statements one by one if they don't support multi-command
            # But PostgreSQL usually does. We'll split by ; just in case.
            for statement in sql.split(";"):
                if statement.strip():
                    cur.execute(statement)
        conn.commit()
    print("PostgreSQL optimization indices created successfully.")
except Exception as e:
    print(f"Migration Failed: {e}")
