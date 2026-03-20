import os
import psycopg
from pathlib import Path

def build_postgres_dsn():
    host = (os.getenv("ORCHESTRATOR_TASK_DB_HOST") or "").strip()
    if not host: return ""
    port = (os.getenv("ORCHESTRATOR_TASK_DB_PORT") or "5432").strip()
    name = (os.getenv("ORCHESTRATOR_TASK_DB_NAME") or "orchestrator_tasks").strip()
    user = (os.getenv("ORCHESTRATOR_TASK_DB_USER") or "orchestrator").strip()
    password = (os.getenv("ORCHESTRATOR_TASK_DB_PASSWORD") or "").strip()
    sslmode = (os.getenv("ORCHESTRATOR_TASK_DB_SSLMODE") or "disable").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"

def check_schema():
    dsn = build_postgres_dsn()
    if not dsn:
        print("No Postgres DSN found.")
        return
    
    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        print("Checking tables...")
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        for t in cur.fetchall():
            print(f"Table: {t[0]}")
            cur.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{t[0]}'")
            for c in cur.fetchall():
                print(f"  Column: {c[0]} ({c[1]})")
    conn.close()

if __name__ == "__main__":
    check_schema()
