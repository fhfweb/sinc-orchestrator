# Code Review Checklist

- Generated At: 2026-03-14T17:04:46

## Technical
- Verify architecture decisions are respected (ADR + contracts).
- Reject unknown build/test commands for completion gating.
- Check migration reversibility and backward compatibility.

## Security
- Reject hardcoded secrets and plaintext credential exposure.
- Validate authorization and input validation in critical endpoints.

## Business
- Confirm implementation maps to Definition of Done criteria.
- Open corrective tasks when requirement traceability is missing.