## What does this PR do?

<!-- One paragraph. Link the issue or audit item if applicable. -->

---

## Mandatory checklist

Answer all four questions before requesting review. A PR without answers will not be merged.

### 1. Does this change touch the auth or rate-limiting path?

- [ ] No
- [ ] Yes — I have tested the change with an invalid API key, an expired key, and a key over quota.

### 2. Does this change touch any DB schema or query?

- [ ] No
- [ ] Yes — a migration file is included in `database/migrations/` and in `docker/init-db/`.
- [ ] Yes — I verified the query plan with `EXPLAIN ANALYZE` on the relevant table.

### 3. Does this change accept user-supplied file paths or execute shell commands?

- [ ] No
- [ ] Yes — `safe_project_path()` is used on every user-supplied path before any file system operation.

### 4. Does this change affect a shared external dependency (Redis, Neo4j, Qdrant, PostgreSQL)?

- [ ] No
- [ ] Yes — the circuit breaker / retry policy is in place.
- [ ] Yes — the failure mode (503 / fail-safe) has been verified manually or via test.

---

## Tests

<!-- What tests cover this change? Paste the relevant pytest node IDs. -->

```
pytest services/streaming/tests/test_<your_module>.py -v
```

---

## Observability

<!-- Does this PR add or change log lines, metrics, or health check output? List them. -->

- [ ] No new log lines or metrics.
- [ ] New log key: `<key>=<value>` — used for …
- [ ] New Prometheus metric: `orchestrator_<name>{…}` — tracks …
