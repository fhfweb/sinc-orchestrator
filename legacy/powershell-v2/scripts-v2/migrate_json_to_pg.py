import json
import psycopg
import os
from datetime import datetime

# Database Configuration — use environment variables; fallback for local dev only
DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

BASE       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAG_PATH   = os.path.join(BASE, "tasks", "task-dag.json")
# Use schema v2 (safe IF NOT EXISTS migrations)
SCHEMA_PATH = os.path.join(BASE, "database", "migrations", "orchestrator_schema_v2.sql")

def migrate():
    print(f"Connecting to database {DB_CONFIG['dbname']}...")
    try:
        with psycopg.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                # 1. Apply Schema
                print("Applying schema...")
                with open(SCHEMA_PATH, 'r') as f:
                    cur.execute(f.read())
                
                # Seed Initial Project
                cur.execute("INSERT INTO projects (id, name) VALUES ('sinc', 'SINC AI Infrastructure') ON CONFLICT (id) DO NOTHING;")
                
                # 2. Read JSON
                print(f"Reading DAG from {DAG_PATH}...")
                with open(DAG_PATH, 'r') as f:
                    dag = json.load(f)
                
                # 3. Migrate Tasks
                print(f"Migrating {len(dag['tasks'])} tasks...")
                for task in dag['tasks']:
                    # Prepare metadata (everything that's not a standard column)
                    standard_cols = ['id', 'project_id', 'status', 'assigned_agent', 'description', 'priority', 'lock_ttl', 'critical_path_priority', 'created_at', 'started_at', 'updated_at', 'completed_at', 'dependencies']
                    metadata = {k: v for k, v in task.items() if k not in standard_cols}
                    
                    cur.execute("""
                        INSERT INTO tasks (
                            id, project_id, status, assigned_agent, description, 
                            priority, lock_ttl, critical_path, created_at, 
                            started_at, updated_at, completed_at, metadata
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = EXCLUDED.status,
                            assigned_agent = EXCLUDED.assigned_agent,
                            updated_at = EXCLUDED.updated_at,
                            metadata = EXCLUDED.metadata;
                    """, (
                        task.get('id'),
                        'sinc', # Default project
                        task.get('status'),
                        task.get('assigned_agent'),
                        task.get('description'),
                        task.get('priority', 'P2'),
                        task.get('lock_ttl', 20),
                        task.get('critical_path_priority', False),
                        task.get('created_at'),
                        task.get('started_at') or None,
                        task.get('updated_at'),
                        task.get('completed_at') or None,
                        json.dumps(metadata)
                    ))
                
                # 4. Migrate Dependencies
                print("Migrating dependencies...")
                for task in dag['tasks']:
                    for dep_id in task.get('dependencies', []):
                        cur.execute("""
                            INSERT INTO dependencies (task_id, dependency_id)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING;
                        """, (task['id'], dep_id))
                
                print("Migration successful.")
                conn.commit()

    except Exception as e:
        print(f"Error during migration: {e}")
        exit(1)

if __name__ == "__main__":
    migrate()
