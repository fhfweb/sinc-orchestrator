---
id: cross-episode-2bb4dddf687a4829
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: project0
source_kind: pattern
source_files: [memory_graph/patterns/repair-test-fail-20260311163333.md]
source_modules: [memory_graph/patterns]
content_hash: 2bb4dddf687a4829e957f81d8ab445f59ffa0e531208b3a8148bd7f3103b09db
---

# Cross-Project Episode: Pattern: Fix failing tests (exit 1)

## Summary
# Pattern: Fix failing tests (exit 1)

**Source task:** REPAIR-TEST-FAIL-20260311163333
**Resolved by:** 
**Recorded at:** 2026-03-11T16:39:40

## Problem
test-failure: exit 1

## Solution
Resolved failing suite by fixing dashboard RBAC test setup and making web layout guest-safe (null user guard). Full php artisan test now passes (26 passed).

## Artifacts
- resources/views/layouts/app.blade.php
- database/factories/UserFactory.php
- tests/Feature/DashboardTest.php
- tests/Feature/RbacSecurityTest.php
---
_Promoted from project: sistema-gestao-psicologos-autonomos on 2026-03-12T12:01:45_
_Confidence score: 0.85_

## Source
- project: project0
- path: memory_graph/patterns/repair-test-fail-20260311163333.md
- imported_at: 2026-03-13T14:28:23