# ADR-0001: Context-Driven Architecture Baseline

- Date: 2026-03-14T17:04:46
- Status: Accepted (baseline)

## Context
The orchestrator must execute software delivery with business context as first-class input, not only technical fingerprint.

## Decision
- Persist business context in i-orchestrator/context/business-context.json.
- Keep architecture decisions and contracts versioned in i-orchestrator/documentation/.
- Gate completion through CORE-COMPLETE-001 requiring verified build/test + healthy runtime + migration evidence.

## Consequences
- Better backlog quality for greenfield projects.
- Reduced false-green states by connecting technical checks to business acceptance criteria.
- Clear handoff across agents with stable artifacts.