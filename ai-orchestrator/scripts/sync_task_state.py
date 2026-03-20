import json
import os
import re
import psycopg
from datetime import datetime

# Database Configuration — use environment variables; fallback for local dev only
DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

BRAIN_PATH = r"C:\Users\Fernando\.gemini\antigravity\brain\c9f6d390-756c-4e04-a4ca-b8a0661d9e6c"
TASK_ID_PATTERN = r"(FEAT-SINC-[A-Z0-9-]+|CORE-[A-Z0-9-]+|REPAIR-[A-Z0-9-]+)"

# Strict Identification Patterns
DONE_MARKER = r"task:done\s+" + TASK_ID_PATTERN
IN_PROGRESS_MARKER = r"task:start\s+" + TASK_ID_PATTERN
FEAT_TAG_PATTERN = r"feat\(" + TASK_ID_PATTERN + r"\)"

def get_db():
    return psycopg.connect(**DB_CONFIG)

def extract_markers(file_path):
    if not os.path.exists(file_path):
        return set(), set()
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    done = set(re.findall(DONE_MARKER, content, re.IGNORECASE))
    start = set(re.findall(IN_PROGRESS_MARKER, content, re.IGNORECASE))
    
    # Also support feat(ID) as in-progress
    feats = set(re.findall(FEAT_TAG_PATTERN, content, re.IGNORECASE))
    start.update(feats)
    
    return done, start

def sync():
    print(f"Starting strict synchronization (PostgreSQL)...")
    
    # Analyze artifacts for markers
    walkthrough_path = os.path.join(BRAIN_PATH, "walkthrough.md")
    plan_path = os.path.join(BRAIN_PATH, "implementation_plan.md")
    
    done_ids = set()
    start_ids = set()
    
    for path in [walkthrough_path, plan_path]:
        d, s = extract_markers(path)
        done_ids.update(d)
        start_ids.update(s)
    
    updates_count = 0
    now = datetime.now()

    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            # Fetch all tasks
            cur.execute("SELECT id, status FROM tasks")
            tasks = cur.fetchall()

            for task in tasks:
                task_id = task['id']
                current_status = task['status']
                
                # Priority 1: Mark as 'done'
                if task_id in done_ids and current_status != 'done':
                    print(f"SYNC [DONE]: {task_id}")
                    cur.execute("""
                        UPDATE tasks 
                        SET status = 'done', updated_at = %s, completed_at = %s 
                        WHERE id = %s
                    """, (now, now, task_id))
                    updates_count += 1
                    
                # Priority 2: Mark as 'in-progress'
                elif task_id in start_ids and current_status == 'pending':
                    print(f"SYNC [START]: {task_id}")
                    cur.execute("""
                        UPDATE tasks 
                        SET status = 'in-progress', updated_at = %s, started_at = %s 
                        WHERE id = %s
                    """, (now, now, task_id))
                    updates_count += 1

            conn.commit()

    if updates_count > 0:
        print(f"Synchronization complete. {updates_count} tasks updated in DB.")
    else:
        print("No strict updates needed.")

if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        print(f"Error during sync: {e}")
