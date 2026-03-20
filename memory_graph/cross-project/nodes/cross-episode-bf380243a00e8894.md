---
id: cross-episode-bf380243a00e8894
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: orchestrator-root
source_kind: pattern
source_files: [orchestrator-root/memory_graph/patterns/repair-test-fail-20260311162301.md]
source_modules: [orchestrator-root/memory_graph]
content_hash: bf380243a00e889497d9bcba1eb31966d06ee504924cced7dfc6b8e6649f7fd1
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
---
_Promoted from project: sistema-gestao-psicologos-autonomos on 2026-03-12T12:01:45_
_Confidence score: 0.85_

## Source
- project: orchestrator-root
- path: orchestrator-root/memory_graph/patterns/repair-test-fail-20260311162301.md
- imported_at: 2026-03-14T17:05:19