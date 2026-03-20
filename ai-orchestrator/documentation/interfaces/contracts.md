# Interface Contracts

- Generated At: 2026-03-14T17:04:46

## Runtime Contracts
- `task-dag.json` is the canonical task state.
- `locks.json` is the canonical lock state.
- `project-state.json` is the canonical operational state.

## API Contracts (baseline)
- GET /health must return status payload and non-500 response.
- Core domain endpoints must have request validation + explicit error payload shape.

## Event Contracts (baseline)
- Domain task completion events must include: `project_slug`, `task_id`, `status`, `updated_at`.
- Observability incidents must include: `category`, `severity`, `reason`, `evidence_path`.