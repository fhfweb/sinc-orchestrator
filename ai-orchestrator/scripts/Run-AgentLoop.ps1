<#
.SYNOPSIS
    PowerShell agent runtime stub — delegates to Python runtime (core_llm/Run-AgentLoop.py).
.DESCRIPTION
    Invoked by Invoke-AutonomousLoopV2.ps1 when AgentRuntimeEngine = "powershell".
    In the standalone orchestrator setup, the Python runtime (Run-AgentLoop.py) handles
    all agent execution via the HTTP API. This stub exits cleanly so the path-check
    in Invoke-AutonomousLoopV2.ps1 passes when AgentRuntimeEngine = "python" or "hybrid".
#>
param(
    [string]$ProjectPath  = ".",
    [string]$OrchestratorUrl  = $env:ORCHESTRATOR_URL,
    [string]$ApiKey           = $env:ORCHESTRATOR_API_KEY,
    [string]$AgentName        = "agent-worker",
    [int]$MaxTasks            = 1
)

Write-Host "[Run-AgentLoop] PowerShell runtime stub — no-op (Python runtime active)."
exit 0
