# Python Control Plane Target Architecture

## Goal

Convergir o orquestrador para uma única arquitetura operacional em Python com separação clara entre API, workers, estado e inteligência.

## Target Topology

### 1. Control Plane

- FastAPI app em `ai-orchestrator/services/streaming`
- somente HTTP/SSE/WebSocket, sem lógica pesada de loop embutida
- falha no startup se dependências críticas não estiverem disponíveis

### 2. State Plane

- Postgres como fonte canônica
- Redis Streams para dispatch/event queue
- arquivos `.json` e `.md` como projeção/export, não como verdade operacional

### 3. Execution Plane

Workers Python dedicados:

- `observer_worker.py`
- `scheduler_worker.py`
- `repair_worker.py`
- `readiness_worker.py`
- `finops_worker.py`
- `mutation_worker.py`
- `deploy_verify_worker.py`
- `pattern_promotion_worker.py`
- `intake_worker.py`

Cada worker deve ser:

- idempotente
- timeout-bounded
- observável
- reentrante
- orientado a eventos

### 4. Intelligence Plane

- Neo4j para knowledge graph / impact analysis
- Qdrant para retrieval vetorial
- Ollama/local LLMs / bridges externos
- lessons learned e pattern memory em DB + object projection

### 5. Presentation Plane

- Dashboard lê apenas APIs/DB reais
- nenhuma métrica mockada
- nenhuma leitura direta de `task-dag.json`

## Required Structural Decisions

### Canonical Trees

- manter: `ai-orchestrator/services`
- manter temporariamente: `scripts/` apenas como wrappers e utilitários de compatibilidade
- congelar: `ai-orchestrator/scripts/v2`

`ai-orchestrator/scripts/v2` hoje amplia a confusão. O plano é:

1. congelar imediatamente
2. copiar o que for necessário para a nova arquitetura Python
3. arquivar no final da transição

### Canonical Root Resolution

Toda resolução de caminho deve derivar de um único `repo_root`.

Problemas atuais como:

- lookup quebrado de `docs/agents/agents-360.registry.json`
- runtime resolvendo `/workspace/ai-orchestrator/...`

precisam ser tratados como blockers de plataforma.

### Canonical Runtime Flow

Fluxo-alvo:

1. ingest / task creation
2. scheduler enqueue
3. agent claim
4. execution
5. validation
6. readiness and incident evaluation
7. repair or completion
8. lesson extraction
9. dashboard / analytics projection

## What Must Stop

- bootstrap parcial silencioso
- dual-write para JSON e DB como se ambos fossem canônicos
- dashboard usando números simulados
- loop central dependente de PowerShell para checks P0

## What Must Be Preserved

- APIs modernas da frente Python
- watchdog / reclaim / dead-letter
- cognitive services
- twin / simulation / analytics
- tenant-safety
- external-agent extensibility
