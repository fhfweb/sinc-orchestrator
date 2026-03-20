---
id: cross-episode-c22f3dcc36bc2ecd
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: sistema-gestao-psicologos-autonomos
source_kind: pattern
source_files: [ai-orchestrator/patterns/repair-test-fail-20260311170155.md]
source_modules: [ai-orchestrator/patterns]
content_hash: c22f3dcc36bc2ecd19387a62e0485d163354433e7590666a26412e4de07e1b4d
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

## Source
- project: sistema-gestao-psicologos-autonomos
- path: ai-orchestrator/patterns/repair-test-fail-20260311170155.md
- imported_at: 2026-03-13T14:28:23