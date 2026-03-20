---
id: cross-episode-7036781ce665ee45
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: orchestrator-root
source_kind: pattern
source_files: [orchestrator-root/memory_graph/patterns/neo4j-community-edition.md]
source_modules: [orchestrator-root/memory_graph]
content_hash: 7036781ce665ee457d4f95f9f5e942432cf484c54ba2bc67adea49fa8edd650d
---

# Cross-Project Episode: Pattern: Neo4j Community Edition — database name restriction

## Summary
# Pattern: Neo4j Community Edition — database name restriction

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
- project: orchestrator-root
- path: orchestrator-root/memory_graph/patterns/neo4j-community-edition.md
- imported_at: 2026-03-14T17:05:19