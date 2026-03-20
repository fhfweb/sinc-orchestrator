import psycopg
import os
from pathlib import Path

DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", "9de4c0a8660df01207a8a773e2dcfdb8"),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

SQL_FILE = Path(r"g:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\database\migrations\orchestrator_schema_v2.sql")

def run_migration():
    print(f"Connecting to {DB_CONFIG['dbname']}...")
    try:
        with psycopg.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                print(f"Reading {SQL_FILE}...")
                sql = SQL_FILE.read_text(encoding="utf-8")
                # Split by semicolon but handle potential triggers/functions if any
                # For this schema, simple exec should work
                cur.execute(sql)
                conn.commit()
                print("Migration successful!")
    except Exception as e:
        print(f"Migration failed: {e}")
        exit(1)

if __name__ == "__main__":
    run_migration()
