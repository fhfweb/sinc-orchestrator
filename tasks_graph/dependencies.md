# Task Dependency Graph

Visual and structured representation of task dependencies.
Used by the scheduler for critical path analysis and parallel execution planning.

---

## Dependency Map

```
(task_id) --> (task_id)  means: left must complete before right can start
```

*(Currently empty — add entries as tasks are created)*

Example:
```
PROJ-001 (Infra) --> PROJ-002 (DB Schema)
PROJ-002 (DB Schema) --> PROJ-003 (API Layer)
PROJ-002 (DB Schema) --> PROJ-004 (Admin Panel)  <- parallel with PROJ-003
PROJ-003 (API) --> PROJ-005 (Frontend)
PROJ-004 (Admin) --> PROJ-005 (Frontend)
```

---

## Dependency Rules

1. A task may not start until ALL its dependencies are "done"
2. Circular dependencies are not allowed
3. If a dependency is "cancelled", dependents must be manually reviewed
4. If a dependency is "blocked", all downstream tasks are implicitly blocked

---

## Impact Analysis

Use this section to track what breaks if a task is delayed:

| Task Delayed | Tasks Blocked | Tasks Unblocked When Done |
|-------------|---------------|--------------------------|
| PROJ-001 | PROJ-002, (all downstream) | PROJ-002 |
| PROJ-002 | PROJ-003, PROJ-004 | PROJ-003, PROJ-004 |

---

## Dependency Health Check

Run this analysis before each scheduler cycle:
1. List all "done" tasks
2. For each "pending" task, check if all dependencies are "done"
3. Tasks with all dependencies done -> move to "ready" pool
4. Tasks with blocked dependencies -> mark as "blocked"
5. Assign "ready" tasks to agents via reputation-based matching
