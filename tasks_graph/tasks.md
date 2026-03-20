# Task Graph — Task Registry

All tasks in DAG format.
The scheduler resolves dependencies from this file before assigning work.
For human-readable task details, see /docs/agents/TASK_BOARD.md.

---

## Format

```yaml
task_id: [ID]
title: [Title]
category: [BACKEND | FRONTEND | DATABASE | INFRA | AI | SECURITY | QA | DOCS]
priority: [P0 | P1 | P2 | P3]
status: [pending | in-progress | done | blocked | handoff | cancelled]
depends_on: [list of task IDs or empty]
assigned_to: [agent name or unassigned]
files_affected: [list of file paths]
estimated_hours: [number]
tags: [list of tags]
```

---

## Tasks

```yaml
- task_id: PROJ-001
  title: Setup Core Infrastructure
  category: INFRA
  priority: P0
  status: pending
  depends_on: []
  assigned_to: unassigned
  files_affected:
    - docker-compose.yml
    - .env.example
  estimated_hours: 4
  tags: []

- task_id: PROJ-002
  title: Database Schema
  category: DATABASE
  priority: P0
  status: blocked
  depends_on: [PROJ-001]
  assigned_to: unassigned
  files_affected:
    - database/migrations/*
  estimated_hours: 8
  tags: []
```

---

## Parallel Execution Groups

Groups of tasks that can run simultaneously (no shared file conflicts):

```
Group 1 (run in parallel):
  - [TASK-ID]
  - [TASK-ID]

Group 2 (run after Group 1):
  - [TASK-ID]
  - [TASK-ID]
```

---

## Critical Path

```
[Current critical path — updated by scheduler]

PROJ-001 -> PROJ-002 -> [downstream tasks]
```
