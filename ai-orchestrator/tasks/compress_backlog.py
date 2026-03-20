import json
import os
from datetime import datetime

dag_path = r"g:\Fernando\project0\ai-orchestrator\tasks\task-dag.json"
archive_dir = r"g:\Fernando\project0\ai-orchestrator\tasks\archive"
archive_path = os.path.join(archive_dir, f"backlog_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

if not os.path.exists(dag_path):
    print(f"DAG not found at {dag_path}")
    exit(1)

with open(dag_path, 'r', encoding='utf-8') as f:
    dag = json.load(f)

# The DAG structure is usually a list of tasks or an object with a 'tasks' key.
# From previous knowledge, it's often a list or a dict with string keys.
tasks = dag if isinstance(dag, list) else dag.get('tasks', [])
if not isinstance(tasks, list):
    # Handle dict-based tasks {id: {data}}
    orig_tasks = tasks
    tasks = []
    for tid, tdata in orig_tasks.items():
        tdata['id'] = tid
        tasks.append(tdata)

print(f"Total tasks in DAG: {len(tasks)}")

# Filter: Move tasks that are 'blocked-waiting-answers' or redundant P0s
to_archive = []
to_keep = []

for task in tasks:
    status = task.get('status', '')
    prio = task.get('priority', 'P1')
    
    # We keep 'todo', 'in-progress', or very recent/important ones.
    # We archive 'blocked-waiting-answers' if they have been there for a while
    if status == 'blocked-waiting-answers' or (status == 'completed' and prio == 'P0'):
        to_archive.append(task)
    else:
        to_keep.append(task)

print(f"Archiving {len(to_archive)} tasks.")
print(f"Keeping {len(to_keep)} tasks.")

# Save archive
with open(archive_path, 'w', encoding='utf-8') as f:
    json.dump(to_archive, f, indent=2)

# Update original DAG
new_dag = to_keep if isinstance(dag, list) else {'tasks': to_keep}
with open(dag_path, 'w', encoding='utf-8') as f:
    json.dump(new_dag, f, indent=2)

print(f"Backlog compression complete. Archive saved to {archive_path}")
