# Quick Start — AI Project Orchestrator OS

Get a project running under AI agent control in 5 minutes.

---

## Prerequisites

- Windows 10/11
- PowerShell 5.1+ (built-in)
- Python 3.10+ (optional — for memory sync and AST scanning)
- Docker Desktop (optional — for project isolation)

---

## Step 1 — Submit a Project

Point the orchestrator at any project folder:

```powershell
.\orchestrator.ps1 -Action v2-submit -ProjectPath C:\projects\myapp
```

This will:
- Create `.ai-orchestrator/` inside the project folder
- Classify the project (new / existing / legacy)
- Seed the initial task DAG
- Generate Docker assets (if stack is detected)

Output files: `.ai-orchestrator/state/project-state.json`, `.ai-orchestrator/tasks/task-dag.json`

---

## Step 2 — Run the Autonomous Loop

```powershell
.\orchestrator.ps1 -Action v2-loop -ProjectPath C:\projects\myapp
```

The loop runs indefinitely:
1. **Observer** — scans project health, creates REPAIR tasks on issues found
2. **Scheduler** — assigns pending tasks to agents by priority score
3. **Wait** — 5 minutes (default), then repeat

To run one cycle only:
```powershell
.\orchestrator.ps1 -Action v2-loop -ProjectPath C:\projects\myapp -RunOnce
```

---

## Step 3 — Watch What's Happening

Check current status:
```powershell
.\orchestrator.ps1 -Action v2-status -ProjectPath C:\projects\myapp
```

View the task board:
```
.ai-orchestrator/tasks/task-dag.json
```

View agent messages:
```
.ai-orchestrator/communication/messages.md
```

View memory X-Ray report:
```powershell
.\scripts\Visualize-Memory.ps1 -ProjectPath C:\projects\myapp -EmitHtml
```

---

## Common Commands

| Goal | Command |
|------|---------|
| Submit project | `.\orchestrator.ps1 -Action v2-submit -ProjectPath <path>` |
| Start loop | `.\orchestrator.ps1 -Action v2-loop -ProjectPath <path>` |
| Single cycle | `.\orchestrator.ps1 -Action v2-loop -ProjectPath <path> -RunOnce` |
| Check status | `.\orchestrator.ps1 -Action v2-status -ProjectPath <path>` |
| Freeze agents | `.\orchestrator.ps1 -Action v2-freeze -ProjectPath <path>` |
| Unfreeze agents | `.\orchestrator.ps1 -Action v2-release -ProjectPath <path>` |

---

## Next Steps

- Read [docs/ONBOARDING.md](docs/ONBOARDING.md) for the full agent setup guide
- Read [docs/agents/SYSTEM_SUMMARY.md](docs/agents/SYSTEM_SUMMARY.md) — load this every agent session
- Read [PROMPT_INDEX.md](PROMPT_INDEX.md) — which prompt to use for each task
