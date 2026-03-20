---
id: cross-episode-75cfcaf9cbdd88cb
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, pattern]
source_project: sistema-gestao-psicologos-autonomos
source_kind: pattern
source_files: [ai-orchestrator/patterns/repair-test-fail-20260311163333.md]
source_modules: [ai-orchestrator/patterns]
content_hash: 75cfcaf9cbdd88cb872c76034a3e7f14faf327a96231a0add881c79c7c01389c
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

## Source
- project: sistema-gestao-psicologos-autonomos
- path: ai-orchestrator/patterns/repair-test-fail-20260311163333.md
- imported_at: 2026-03-13T14:28:23