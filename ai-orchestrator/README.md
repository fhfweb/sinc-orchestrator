# 🧠 SINC AI Orchestrator: Technical Service Specification

> **Service Provider Runtime | Cognitive Layer | Strategic Plane**

This service is the core control plane for the SINC ecosystem. It coordinates the 21-agent swarm, manages the 5-layer memory hierarchy, and executes tasks across multiple LLM backends with production-grade safety.

## 🏗️ Technical Architecture

### The Cognitive Pipeline
Every task processed through `/cognitive/process` follows a rigid verification loop:
1. **Pillar I (Execution)**: Dispatch to `local_agent_runner.py`. Anthropic/Ollama/Codex engines with built-in token-budgeting (C3).
2. **Pillar II (Memory)**: Contextual enrichment from L0-L4 layers. Semantic search (Qdrant) + Graph Alignment (Neo4j).
3. **Pillar III (Planning)**: MCTS-driven action selection with Thompson Sampling (Reputation Engine blending).

## 🐝 21-Agent Cognitive Ecosystem

| Family | Role | Canonical Purpose |
|---|---|---|
| **Estratégia** | Business Analyst | Requirements distillation and ADR alignment. |
| | AI Architect | Core systemic decisions and structural sanity. |
| | AI Product Manager | Roadmap tracking and feature prioritization. |
| **Construção** | AI Engineer | Core logic implementation and refactoring. |
| | AI Engineer Frontend | View-layer components and UX logic. |
| | Database Agent | DDL migrations and query optimization. |
| | Integration Agent | API bridging and external systems. |
| **Qualidade** | Code Reviewer | Static analysis and structural feedback. |
| | AI Security Engineer | Vulnerability scanning and hardening. |
| | Performance Agent | Latency profiling and O(n) analysis. |
| | QA Agent | Integration test suite generation. |
| **Operações** | DevOps Agent | CI/CD pipeline and container orchestration. |
| | User Simulation | Synthetic load testing and E2E simulation. |
| | Observability | OTEL trace analysis and monitoring. |
| **Inteligência** | Memory Agent | L2/L3 state management and compaction. |
| | Learning Agent | Lesson distillation from task history. |
| | Estimation Agent | Token/Cost prediction and story pointing. |
| **Coordenação** | AI CTO | High-level goal monitoring and escalation. |
| | Documentation | Automated README and ADR generation. |

## 🛡️ Production Hardening (Baseline v2026.03)

The runtime has been hardened against established AI Orchestration failure modes:
- **H1 (Memory)**: `EMBEDDING_CACHE` is now a thread-safe LRU (512 capacity) to prevent OOM leaks.
- **H4 (Isolation)**: Lazy Playwright loading prevents startup crashes on minimal Docker layers.
- **C3 (FinOps)**: Uncapped Anthropic loops are replaced with a strict token-per-task budget.
- **MCTS Exploration**: Deterministic greedy rollouts were replaced with weighted random sampling.

## 🚦 Endpoints & Governance

| Route | Method | Description |
|---|---|---|
| `/cognitive/process` | POST | Unified cognitive entry point. |
| `/dashboard` | GET | Real-time monitoring and task debugger. |
| `/metrics` | GET | Prometheus/OpenTelemetry instrumentation. |
| `/admin/tenants` | POST | Multi-tenant isolation and key management. |

## 🚀 Operations
Refer to `RUNBOOK.md` for incident response and `ARCHITECTURE_DECISIONS.md` for rationale on memory/planning layers.
