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
