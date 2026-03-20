# Pattern: Missing .env.example for local PostgreSQL development

**Stack:** any + PostgreSQL
**Recorded at:** 2026-03-11

## Problem

Projects using PostgreSQL through Docker Compose have `.env.docker.generated`
for container use, but no `.env.example` for developers running the app locally
(outside Docker). Developers see errors like:

```
sqlalchemy.exc.OperationalError: could not connect to server
django.db.utils.OperationalError: FATAL: password authentication failed
```

## Solution

Create `.env.example` in the project root with safe placeholder values:

```env
# Local development environment — copy to .env and fill in your values
# Do NOT commit .env — only commit .env.example

# Option A: full DATABASE_URL
DATABASE_URL=postgresql://postgres:password@localhost:5432/mydb

# Option B: individual vars (used if DATABASE_URL is not set)
DB_HOST=localhost
DB_PORT=5432
DB_NAME=mydb
DB_USER=postgres
DB_PASSWORD=password

# Optional services (comment out if not running locally)
# NEO4J_URI=bolt://localhost:7687
# NEO4J_USER=neo4j
# NEO4J_PASSWORD=password
# QDRANT_HOST=localhost
# QDRANT_PORT=6333

# App settings
APP_ENV=development
DEBUG=true
SECRET_KEY=dev-secret-key-change-in-production
```

Then add `.env` to `.gitignore` (keep `.env.example` tracked).

## Detection

Check if `.env.example` exists in the project root when PostgreSQL is in the stack.
