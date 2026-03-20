# Module Context: Memory Layer

**Scripts:** `scripts/memory_sync.py`, `scripts/extract_world_model.py`
**Stores:** Neo4j (graph), Qdrant (vector), Markdown (canonical source)

---

## Architecture

```
Markdown nodes (memory_graph/nodes/*.md)
        +
Intake dependency graph (reports/dependency_graph.json)
        +
World model (reports/world_model.json)
        │
        ▼
  dedupe_nodes()  →  all_nodes[]
        │
   ┌────┴────┐
   ▼          ▼
 Neo4j      Qdrant
(graph)    (vector)
```

## memory_sync.py

Entry point: `python scripts/memory_sync.py --project-slug <slug> --memory-dir <path>`

**Node sources (all merged into `all_nodes` before sync):**
- `markdown_nodes` — parsed from `memory_graph/nodes/*.md`
- `intake_nodes` — from `ai-orchestrator/reports/dependency_graph.json`
- `world_nodes` — from `ai-orchestrator/reports/world_model.json`

**Neo4j sync:**
- `MERGE (n:MemoryNode {project_slug, id})` — idempotent upsert
- Relationships from `memory_graph/edges/relationships.md` are also synced

**Qdrant sync:**
- Collection name: `<project-slug>-memory`
- Embedding: Ollama (primary) → local hash projection (fallback)
- Vector size: determined from first successful Ollama embedding
- Fallback ratio monitoring: if >50% embeddings use local hash, logs a warning

**Security:**
- Project slug is validated before any DB operation
- Node `project_slug` must match the CLI argument (cross-project contamination blocked)
- Vault secrets are never included in node payloads

## extract_world_model.py

Extracts 5 semantic dimensions from the project brief + intake report:
1. **Actors** — users, services, external systems
2. **Flows** — key user journeys and data flows
3. **Entities** — domain objects and their relationships
4. **Rules** — business rules and constraints
5. **Risks** — technical and product risks

Output: `ai-orchestrator/reports/world_model.json`

## Memory node format (Markdown)

```markdown
---
id: unique-node-id
type: module | entity | flow | rule | risk | actor
project_slug: my-project
tags: [tag1, tag2]
---

# Node Title

Content describing this memory node.

## Relations
- DEPENDS_ON: other-node-id
- IMPLEMENTS: interface-node-id
```

## Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Qdrant receives 0 nodes | Memory dir empty + no intake/world | Add nodes to `memory_graph/nodes/` |
| Neo4j auth error | Wrong password in vault | Re-run intake to regenerate vault |
| Embedding failures (all local hash) | Ollama not running | Start Ollama: `ollama serve` |
| Duplicate nodes in Qdrant | `dedupe_nodes` key collision | Check node `id` uniqueness |
| Neo4j `CREATE DATABASE` fails | Community Edition limitation | Use `neo4j` as database name |
