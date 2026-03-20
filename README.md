# 🧠 SINC AI Orchestrator: The Cognitive Control Plane
> **Production-Hardened | Multi-Tenant | Autonomous Software Delivery**

SINC is not just a runner; it is a **Cognitive Operating System** designed for high-concurrency, autonomous engineering at scale. It leverages a three-pillar architecture to transform raw LLM capabilities into a reliable, learning engineering swarm.

---

## 🏛️ The Three Pillars

### ⚡ Pillar I: Verified Execution
A production-hardened runtime environment with native support for top-tier backends:
- **Anthropic Sonnet 3.5**: Primary cognitive driver with built-in token-budgeting.
- **Ollama (Qwen2.5-Coder)**: Local-first execution with intelligent GPU VRAM scheduling.
- **Codex CLI**: Full-auto legacy support for CLI-driven workflows.
- **Hardware Isolation**: Browser-based tool-use via a lazy-loaded Playwright pool.

### 🧠 Pillar II: Cognitive Memory Hierarchy
A 5-layer persistent memory stack ensures the system learns from every task execution:
- **L0: Deterministic Guardrails** - Hard-coded rules and safety constraints.
- **L1: Elastic Cache** - Redis-backed LRU cache for instant deterministic hits.
- **L2: Semantic Memory** - Qdrant-powered vector search for contextually related history.
- **L3: Graph Reasoning** - Neo4j-driven relationship mapping and architectural alignment.
- **L4: Durable Events** - PostgreSQL event-store for full auditability and reputation tracking.

### 🎲 Pillar III: Strategic Planning (MCTS)
Beyond linear prompting. SINC uses **Monte Carlo Tree Search (MCTS)** to:
- Simulate multiple execution paths before committing tokens.
- Blend **Agent Reputation Scores** into decision-making.
- Use **Thompson Sampling** for optimal exploration/exploitation of agent swarms.

---

## 🐝 The 21-Agent Swarm
The orchestrator manages a specialized ecosystem across 6 operational groups:

| Group | Agents |
|---|---|
| **Estratégia** | Business Analyst, AI Architect, AI Product Manager |
| **Construção** | AI Engineer, AI Engineer Frontend, AI DevOps, Database Agent, Integration Agent |
| **Qualidade** | Code Reviewer, AI Security Engineer, Performance Agent, QA Agent |
| **Operações** | DevOps Agent, User Simulation, Observability, Incident Response |
| **Inteligência** | Memory Agent, Learning Agent, Estimation Agent |
| **Coordenação** | AI CTO, Documentation Agent |

---

## 🛡️ Production Standards

As of **March 2026**, the system has been hardened against top-tier architectural risks:
- [x] **Cost Controls**: Per-task token budgets prevent financial runaway.
- [x] **Memory Stability**: All memory layers implemented with pool-aware singleton logic.
- [x] **Availability**: Lazy-import drivers prevent startup crashes on minimal hosts.
- [x] **Reputation Integrity**: Atomic source-of-truth updates for agent scores.

---

## 🚀 Quick Start

### Provider Node
```bash
cd ai-orchestrator/docker
docker compose -f docker-compose.orchestrator.yml up -d
```
Dashboard available at: `http://localhost:8765/dashboard`

### Engineering Workers
```bash
# Connect local project to the swarm
cd ai-orchestrator/docker
docker compose -f docker-compose.client.yml up -d
```

---
> **Project Status**: **Baseline Synchronized (fhfweb/sinc-orchestrator)**  
> **Hardening Coverage**: 🔴 critical (100%) | 🟠 high-risk (100%)
