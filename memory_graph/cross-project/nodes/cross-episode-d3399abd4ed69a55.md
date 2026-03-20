---
id: cross-episode-d3399abd4ed69a55
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: sistema-gestao-psicologos-autonomos
source_kind: pattern
source_files: [ai-orchestrator/patterns/repair-test-fail-20260311162301.md]
source_modules: [ai-orchestrator/patterns]
content_hash: d3399abd4ed69a55ebbf113dbf1e17821dc73d40cb270a2921bd623f22e8579c
---

# Cross-Project Episode: Pattern: Fix failing tests (exit 1)

## Summary
# Pattern: Fix failing tests (exit 1)

**Source task:** REPAIR-TEST-FAIL-20260311162301
**Resolved by:** 
**Recorded at:** 2026-03-11T16:25:05

## Problem
test-failure: exit 1

## Solution
Fixed flaky web-shell feature test by disabling Vite dependency in test context with withoutVite(); full suite now passes (19/19).

## Artifacts
- tests/Feature/PsychologyEndToEndFlowTest.php
- ai-orchestrator/reports/test-run-20260311162301.json

## Source
- project: sistema-gestao-psicologos-autonomos
- path: ai-orchestrator/patterns/repair-test-fail-20260311162301.md
- imported_at: 2026-03-13T14:28:23