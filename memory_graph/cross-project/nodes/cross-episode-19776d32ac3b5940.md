---
id: cross-episode-19776d32ac3b5940
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, lesson]
source_project: consumer-project
source_kind: lesson
source_files: [consumer-project/ai-orchestrator/knowledge_base/lessons_learned/LESSON_REPAIR-20260311214115-35e9e3.md]
source_modules: [consumer-project/ai-orchestrator]
content_hash: 19776d32ac3b5940e4955b1531ac273b9c204cfdd76f33519bb57eb9e5d480dd
---

# Cross-Project Episode: Lesson Learned: REPAIR-20260311214115-35e9e3

## Summary
# Lesson Learned: REPAIR-20260311214115-35e9e3

- Generated At: 2026-03-11T21:51:05
- Task ID: REPAIR-20260311214115-35e9e3
- Agent: Codex
- Completed At: 2026-03-11T21:50:30
- Incident Path: G:\Fernando\project0\workspace\projects\sistema-gestao-psicologos-autonomos\ai-orchestrator\reports\INCIDENT_20260311_214115_memory_sync_qdrant_fallback.md

## Error Signature
- Category: memory-sync-qdrant-fallback
- Title: Qdrant fallback ratio above threshold
- Details: Qdrant non-ollama embedding ratio is above allowed threshold. ratio=100% threshold=20% synced=42 non_ollama=42 local=42 fallback_hash=0. Check Ollama GPU runtime and embedding endpoint availability.

## Fix Pattern
- Task status moved to completed.
- Capture the exact patch/test evidence in this section when available.

## Validation Command
```text
unknown
```

## Reuse Guidance
- Search this lesson first when similar failures appear.
- Re-run the failing command before applying any broad refactor.
- Keep the repair minimal and attach evidence in execution history.

## Source
- project: consumer-project
- path: consumer-project/ai-orchestrator/knowledge_base/lessons_learned/LESSON_REPAIR-20260311214115-35e9e3.md
- imported_at: 2026-03-15T16:51:09