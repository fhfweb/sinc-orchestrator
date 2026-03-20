---
id: cross-episode-4eb6c4d1dca7c9df
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: orchestrator-root
source_kind: pattern
source_files: [orchestrator-root/memory_graph/patterns/docker-app-command-review.md]
source_modules: [orchestrator-root/memory_graph]
content_hash: 4eb6c4d1dca7c9dfdcfcbceda9813773a7a8387aa0e82100144ee4cc5b4c239c
---

# Cross-Project Episode: Pattern: Docker Compose app command REVIEW_REQUIRED

## Summary
# Pattern: Docker Compose app command REVIEW_REQUIRED

**Stack:** any
**Recorded at:** 2026-03-11

## Problem

The Docker factory generates a compose file with `REVIEW_REQUIRED` as the app command
when it cannot detect the application entrypoint:

```yaml
command: sh -lc "python -m app # REVIEW_REQUIRED"
```

This causes the container to start and immediately exit or run incorrectly.

## Solution by stack

**FastAPI (Python):**
```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Or with reload for dev:
```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Flask (Python):**
```yaml
command: flask run --host 0.0.0.0 --port 5000
```

**Django (Python):**
```yaml
command: python manage.py runserver 0.0.0.0:8000
```

**Node/Express:**
```yaml
command: node dist/index.js
```

**Laravel (PHP):**
```yaml
command: php artisan serve --host=0.0.0.0 --port=8000
```

## Detection

Grep for `REVIEW_REQUIRED` in `docker-compose.generated.yml`.

## Prevention

After fixing, update `APP_COMMAND_CONFIDENCE` in `.env.docker.generated` from `low` to `confirmed`.

## Source
- project: orchestrator-root
- path: orchestrator-root/memory_graph/patterns/docker-app-command-review.md
- imported_at: 2026-03-14T17:05:18