# Access Credentials

- Generated At: 2026-03-12T17:52:24
- Project: SINC (sinc)

## Relational Database
- enabled: True
- engine: mysql
- host: localhost
- port: 3307
- database: sinc
- user: sinc
- password: [stored in vault]

## Neo4j
- enabled: True
- bolt_uri: bolt://localhost:7688
- browser_url: http://localhost:7475/browser/
- database: neo4j
- user: neo4j
- password: [stored in vault]

## Qdrant
- enabled: True
- url: http://localhost:6334
- collection: sinc-memory

## Bootstrap Verification
- relational: status=skipped records=0 details=relational-seed-not-supported-for-engine:mysql
- neo4j: status=ready records=1 details=neo4j-bootstrap-state-upserted
- qdrant: status=ready records=2458 details=qdrant-bootstrap-point-upserted

## Bootstrap Notes
- orchestrator-core::orchestrator-core-ready: container=orchestrator-core network=orchestrator-core-net
- container-isolation::project-container-isolation-ok: all container_name values use 'sinc-*'
- port-remap: service=db 3306 -> 3307
- port-remap: service=neo4j 7474 -> 7475
- port-remap: service=neo4j 7687 -> 7688
- port-remap: service=qdrant 6333 -> 6334
- port-remap: service=ollama 11435 -> 11436
- port-remap: service=redis 6379 -> 6380
- neo4j-provision: A ligação subjacente foi fechada: A ligação terminou inesperadamente.
- qdrant-provision: qdrant-collection-exists
- relational-verify-seed: status=skipped records=0 detail=relational-seed-not-supported-for-engine:mysql
- neo4j-verify-seed: status=ready records=1 detail=neo4j-bootstrap-state-upserted
- qdrant-verify-seed: status=ready records=2458 detail=qdrant-bootstrap-point-upserted
- relational-domain-verify-seed: status=skipped records=0 detail=domain-migration-not-supported-for-engine:mysql
- infra-mode: dedicated-infra

## Secret Vault
- path: ai-orchestrator/database/.secrets/vault.json
- policy: credentials are persisted only in vault and masked in state/output

## How To Connect
1. Open Neo4j Browser at the browser_url above.
2. Use bolt_uri + user + password loaded from secret vault.
3. If connection fails, check remapped ports in ai-orchestrator/project-pack/DOCKER_PORTS.json.