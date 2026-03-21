# SINC Architecture Overview

O SINC Orchestrator (SINC: Systems Intelligence & Neural Context) é um Control Plane agêntico projetado para autonomia em larga escala e compreensão profunda de código.

## Tech Stack (Canonical Runtime)

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Runtime** | Python 3.12+ | Linguagem canônica para lógica e orquestração. |
| **API Gateway** | FastAPI | Interface REST e SSE de alta performance. |
| **Orchestration** | Redis Streams | Barramento de eventos para logs e despacho de tarefas. |
| **Memory L1/L2** | Qdrant | Vetorização semântica de código e padrões. |
| **Logic Graph (L3)** | Neo4j | Mapeamento de dependências e análise de impacto (AST). |
| **Persistence** | PostgreSQL | Estado persistente de tenants, projetos e histórico. |

## Core Components

- **Streaming Server (`services/streaming/`)**: O coração do sistema. Gerencia rotas, autenticação, billing e broadcasting de eventos.
- **Agent Worker (`services/agent_worker.py`)**: Consumidor de tarefas que executa o "pensamento" dos agentes Core-5.
- **Ingest Pipeline (`services/ingest_pipeline.py`)**: Pipeline de ingestão assíncrona que alimenta o Neo4j e o Qdrant.
- **Memory Layers (`services/memory_layers.py`)**: Abstração da hierarquia de memória (L0 a L4).

## Data Flow

1. **Input**: Requisição via API ou despacho de stream.
2. **Context**: `ContextRetriever` busca no Neo4j (grafo) e Qdrant (semântica).
3. **Execution**: `AgentWorker` processa a tarefa usando o `LocalAgentRunner`.
4. **Output**: Resultados são gravados no DB e emitidos via SSE/Redis Streams.

---
**Status**: Versão 2.0 (Narrativa 12.0 - Stabilized)