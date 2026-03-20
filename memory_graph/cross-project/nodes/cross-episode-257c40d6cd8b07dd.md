---
id: cross-episode-257c40d6cd8b07dd
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/repair-test-fail-20260311170155.md]
source_modules: [memory_graph/patterns]
content_hash: 257c40d6cd8b07dddf995b50820b4a76e01498885bddcc3a71274aa3c589ddc4
---

# Cross-Project Episode: Pattern: Fix failing tests (exit 1)

## Summary
# Pattern: Fix failing tests (exit 1)

**Source task:** REPAIR-TEST-FAIL-20260311170155
**Resolved by:** 
**Recorded at:** 2026-03-11T17:05:05

## Problem
test-failure: exit 1

## Solution
Resolved transient suite failure caused by clinic_id tenant constraints in test setup. Added tenant-safe defaults and adjusted tests; full suite now passes (34 tests, 176 assertions).

## Artifacts
- tests/Feature/LgpdEncryptionTest.php
- tests/Feature/AuditControllerTest.php
- tests/Feature/TeleconsultaTest.php
- app/Traits/BelongsToClinic.php
---
_Promoted from project: sistema-gestao-psicologos-autonomos on 2026-03-12T12:01:45_
_Confidence score: 0.85_

## Source
- project: project0
- path: memory_graph/patterns/repair-test-fail-20260311170155.md
- imported_at: 2026-03-13T14:28:23