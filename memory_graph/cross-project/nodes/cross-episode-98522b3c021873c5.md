---
id: cross-episode-98522b3c021873c5
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/neo4j-community-edition.md]
source_modules: [memory_graph/patterns]
content_hash: 98522b3c021873c5346bb1587f58fd498cac24d10db433cfff42f55e8a97dad6
---

# Cross-Project Episode: Pattern: Neo4j Community Edition â€” database name restriction

## Summary
# Pattern: Neo4j Community Edition â€” database name restriction

**Stack:** any
**Recorded at:** 2026-03-11

## Problem

Neo4j Community Edition (CE) does not support `CREATE DATABASE <name>` or connecting
to a database other than the default `neo4j`. When the orchestrator generates a
`NEO4J_DATABASE` env var with a custom name (e.g. `controle_medicamentos_idosos`),
the Bolt connection fails with:

```
Database '<name>' does not exist.
The default database name in Neo4j CE is 'neo4j'.
```

## Solution

1. Set `NEO4J_DATABASE=neo4j` in `.env.docker.generated` and `.env`
2. In `memory_sync.py`, use `--neo4j-database neo4j`
3. In application connection strings, use `neo4j` as the database name
4. Only use custom database names if you have Neo4j Enterprise Edition

## Detection

Look for `NEO4J_DATABASE` set to anything other than `neo4j` in generated env files,
combined with a Neo4j CE docker image tag (e.g. `neo4j:5` without `-enterprise`).

## Prevention

The Docker factory should default `NEO4J_DATABASE=neo4j` for CE images.

## Source
- project: project0
- path: memory_graph/patterns/neo4j-community-edition.md
- imported_at: 2026-03-13T14:28:23