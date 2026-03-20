# 🧠 SINC AI Orchestrator: Technical Service Specification

> **Service Provider Runtime | Cognitive Layer | Strategic Plane**

This service coordinates the 21-agent swarm, manages the 5-layer memory hierarchy, and executes tasks across multiple LLM backends with **production-grade infrastructure (actively hardened)**.

## 🏗️ Technical Architecture

### The Cognitive Pipeline
Every task processed through `/cognitive/process` follows a structured verification loop:
1. **Pillar I (Execution)**: Dispatch to hardened `local_agent_runner.py` with cost-circuit-breakers (C3).
2. **Pillar II (Memory)**: Contextual enrichment from pooled and singleton-aware memory layers. Semantic search (Qdrant) + Graph Alignment (Neo4j).
3. **Pillar III (Planning)**: Early-stage MCTS-driven action selection with reputation-aware evaluation (Thompson Sampling).

## 🐝 Specialized Agent Swarm
Expandable architecture of specialized agents grouped by functional domains:

| Family | Role | Canonical Purpose |
|---|---|---|
| **Estratégia** | Business Analyst | Requirements distillation and ADR alignment. |
| | AI Architect | Core systemic decisions and structural sanity. |
| | AI Product Manager | Roadmap tracking and feature prioritization. |
| **Construção** | AI Engineer | Core logic implementation and refactoring. |
| | AI Engineer Frontend | View-layer components and UX logic. |
| | Database Agent | DDL migrations and query optimization. |
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

## 🛡️ Production Hardening Baseline

The runtime is actively hardened against established failure modes:
- **Memory Management**: Pooled connection drivers and thread-safe **Redis-backed** LRU cache (H1-H4).
- **FinOps**: Per-task token budgets to mitigate recursive financial exposure (C3).
- **Isolation**: Strategic driver isolation to prevent startup failures on minimal hosts.

## 🚦 Endpoints & Governance

| Route | Method | Description |
|---|---|---|
| `/cognitive/process` | POST | Unified cognitive entry point. |
| `/dashboard` | GET | Real-time monitoring and task debugger. |
| `/metrics` | GET | Prometheus/OpenTelemetry instrumentation. |

## 🚀 Operations
Refer to `ARCHITECTURE_DECISIONS.md` for deep-dive rationale on memory and planning implementations.
