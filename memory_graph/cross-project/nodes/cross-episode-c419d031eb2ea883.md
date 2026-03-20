---
id: cross-episode-c419d031eb2ea883
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/docker-app-command-review.md]
source_modules: [memory_graph/patterns]
content_hash: c419d031eb2ea883d8ff325b2969a6309b954b53a74d0c25ccde4c3c1db42440
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
- project: project0
- path: memory_graph/patterns/docker-app-command-review.md
- imported_at: 2026-03-13T14:28:23