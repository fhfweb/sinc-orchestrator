---
id: cross-episode-528eb61e7f1ecc89
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/fastapi-lifespan.md]
source_modules: [memory_graph/patterns]
content_hash: 528eb61e7f1ecc89a82700279ccbf07a20e83c474272d69aac696311d57f1146
---

# Cross-Project Episode: Pattern: FastAPI lifespan context manager (replaces deprecated on_event)

## Summary
# Pattern: FastAPI lifespan context manager (replaces deprecated on_event)

**Stack:** python + FastAPI >= 0.103
**Recorded at:** 2026-03-11

## Problem

`@app.on_event("startup")` and `@app.on_event("shutdown")` are deprecated since
FastAPI 0.103 and removed in newer versions. The warning is:

```
DeprecationWarning: on_event is deprecated, use lifespan event handlers instead.
```

## Solution

Replace with the `lifespan` context manager pattern:

```python
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # startup logic here
    Base.metadata.create_all(bind=engine)
    yield
    # shutdown logic here (optional)

app = FastAPI(title=settings.app_name, lifespan=lifespan)
```

Key points:
- Use `_app` (underscore) to avoid IDE "unused parameter" warnings
- Code before `yield` runs on startup
- Code after `yield` runs on shutdown
- Import `asynccontextmanager` from `contextlib`, not from `fastapi`

## Detection

Search for `@app.on_event` in Python files.

## Source
- project: project0
- path: memory_graph/patterns/fastapi-lifespan.md
- imported_at: 2026-03-13T14:28:23