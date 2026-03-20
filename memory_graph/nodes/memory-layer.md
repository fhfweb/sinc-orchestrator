---
id: memory-layer
type: module
project_slug: orchestrator-os
tags: [memory, neo4j, qdrant, embeddings, markdown]
---

# Memory Layer

Three-tier memory stack: Markdown (canonical) → Neo4j (graph) → Qdrant (vector).

## Tier 1: Markdown (canonical source)

Location: `memory_graph/nodes/*.md`
Format: YAML front-matter (`id`, `type`, `project_slug`, `tags`) + freetext body + `## Relations` section.
Portable — no infrastructure required, committed to git.

## Tier 2: Neo4j (graph relationships)

- Node label: `MemoryNode`
- Key properties: `project_slug`, `id`, `type`, `title`, `content`
- Relationships: typed edges from `## Relations` sections and `edges/relationships.md`
- Upsert pattern: `MERGE (n:MemoryNode {project_slug, id}) SET n += props`
- CE limitation: single database (`neo4j`), no multi-tenancy

## Tier 3: Qdrant (vector search)

- Collection: `<project-slug>-memory`
- Embedding model: Ollama (`nomic-embed-text` default) → local hash fallback
- Point payload: full node metadata (no secrets)
- Dedup: nodes from all three sources (`markdown_nodes + intake_nodes + world_nodes`)
  are merged via `dedupe_nodes()` before sync — no duplicates in Qdrant

## Security

- Vault passwords are never included in node payloads or embeddings
- `project_slug` is validated before any write — cross-project contamination is blocked at script level
- Vault file is DPAPI-encrypted + hidden on Windows

## When to run memory_sync.py

- Automatically: at the end of every `v2-submit` intake
- Manually: when you add new markdown nodes to `memory_graph/nodes/`
- After: world model update (`extract_world_model.py` reruns)

## Relations
- USED_BY: intake-pipeline
- STORES_FOR: v2-architecture
- INDEXED_BY: orchestrator-core
