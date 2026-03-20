import sys
import json
import os
import psycopg
import argparse

# Database Configuration — use environment variables; fallback for local dev only
DB_CONFIG = {
    "dbname": os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":   os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":   os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":   os.environ.get("ORCH_DB_PORT",     "5434"),
}

def get_db():
    return psycopg.connect(**DB_CONFIG)

def list_tasks():
    with get_db() as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute("SELECT * FROM tasks")
            tasks = cur.fetchall()
            
            # Fetch dependencies
            cur.execute("SELECT * FROM dependencies")
            deps = cur.fetchall()
            
            # Attach dependencies and format dates
            for t in tasks:
                t['dependencies'] = [d['dependency_id'] for d in deps if d['task_id'] == t['id']]
                # Post-process dates and JSONB
                for key in ['created_at', 'started_at', 'updated_at', 'completed_at']:
                    if t[key]:
                        t[key] = t[key].isoformat()
                if t['metadata']:
                    t.update(t['metadata'])
                    del t['metadata']
            
            return tasks

def update_task(task_id, status=None, agent=None, reason=None):
    with get_db() as conn:
        with conn.cursor() as cur:
            updates = []
            params = []
            if status is not None:
                updates.append("status = %s")
                params.append(status)
            if agent is not None:
                updates.append("assigned_agent = %s")
                params.append(agent)
            
            updates.append("updated_at = CURRENT_TIMESTAMP")
            
            params.append(task_id)
            query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = %s"
            cur.execute(query, params)

            if reason:
                cur.execute(
                    "INSERT INTO agent_events (task_id, agent_name, event_type, payload) "
                    "VALUES (%s, %s, 'update', %s) ON CONFLICT DO NOTHING",
                    (task_id, agent or "", json.dumps({"reason": reason, "status": status}))
                )

            conn.commit()
            return True

def unlock_all():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET status = 'pending', assigned_agent = '' WHERE status IN ('in-progress', 'blocked-lock-conflict') OR assigned_agent != ''")
            count = cur.rowcount
            conn.commit()
            return count

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["list", "update", "unlock-all", "info"])
    parser.add_argument("--task-id", help="Task ID for update/info")
    parser.add_argument("--status", nargs='?', const='', default=None, help="New status")
    parser.add_argument("--agent", nargs='?', const='', default=None, help="New agent")
    
    args = parser.parse_args()
    
    try:
        if args.action == "list":
            tasks = list_tasks()
            print(json.dumps({"tasks": tasks}, indent=2))
        elif args.action == "update":
            if not args.task_id:
                print("Error: --task-id required for update")
                sys.exit(1)
            update_task(args.task_id, args.status, args.agent)
            print("OK")
        elif args.action == "unlock-all":
            count = unlock_all()
            print(f"Unlocked {count} tasks")
        elif args.action == "info":
            if not args.task_id:
                print("Error: --task-id required for info")
                sys.exit(1)
            tasks = list_tasks()
            task = next((t for t in tasks if t['id'] == args.task_id), None)
            if task:
                print(json.dumps(task, indent=2))
            else:
                print("Not found")
                sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
