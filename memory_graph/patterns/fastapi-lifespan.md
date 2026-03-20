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
