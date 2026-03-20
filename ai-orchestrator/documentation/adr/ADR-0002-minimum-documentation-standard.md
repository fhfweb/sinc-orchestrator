# ADR-0002: Minimum Documentation Standard

- Date: 2026-03-14T07:56:03
- Status: Accepted

## Context
As the orchestrator scales, the quality and reliability of project artifacts (context, architecture, contracts) become critical for maintainability and automated governance.

## Decision
All critical AI Orchestrator projects must adhere to the following documentation standards:

1. **Mandatory Artifacts**:
   - `ai-orchestrator/context/business-context.json`
   - `ai-orchestrator/documentation/architecture.md`
   - `ai-orchestrator/documentation/interfaces/contracts.md`
2. **Minimum Content**:
   - Markdown files (`.md`) must contain at least 10 lines of non-whitespace content.
3. **Placeholder Prohibition**:
   - Artifacts must not contain development placeholders like `TODO` or `REVIEW_REQUIRED` once marked as "Complete" or "Accepted".
4. **Governance Rule**:
   - The `Policy Enforcer` will scan these artifacts during each orchestration cycle and report violations.

## Consequences
- Reduced "hallucination" risk for agents reading documentation.
- Higher reliability of the Orchestrator 360 Decision Engine scores.
- Clearer handoff points between human and AI collaborators.
