---
id: cross-episode-e0bd4f0c20606411
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/repair-test-fail-20260311162301.md]
source_modules: [memory_graph/patterns]
content_hash: e0bd4f0c20606411333cc23f5a998077ea0b1238cc1f4c232fce041de87d1972
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
- project: project0
- path: memory_graph/patterns/repair-test-fail-20260311162301.md
- imported_at: 2026-03-13T14:28:23