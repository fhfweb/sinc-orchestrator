<#
.SYNOPSIS
    Reads and reports the current coordination mode and active locks for a project.
.DESCRIPTION
    Reads coordination-mode.json and locks.json from .ai-orchestrator/ and outputs
    a human-readable status summary showing: current mode, active locks, TTLs, and
    any blocked agents. Used by agents to check before claiming a task.
.PARAMETER ProjectPath
    Path to the project root containing .ai-orchestrator/. Defaults to current directory.
.PARAMETER EmitJson
    If set, outputs status as JSON instead of human-readable text.
.EXAMPLE
    .\scripts\v2\Invoke-CoordinationStatus.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-CoordinationStatus.ps1 -ProjectPath C:\projects\myapp -EmitJson
#>param(
    [string]$ProjectPath = ".",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$coordinationPath = Join-Path $orchestratorRoot "state/coordination-mode.json"
$canonicalLocksPath = Join-Path $orchestratorRoot "locks/locks.json"
$alternativeLocksPath = Join-Path $resolvedProjectPath "docs/agents/LOCKS.json"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"

$unverified = New-Object System.Collections.Generic.List[string]

$projectState = Get-V2JsonContent -Path $statePath
if (-not $projectState) {
    $unverified.Add("missing-project-state")
}

$coordination = Get-V2JsonContent -Path $coordinationPath
if (-not $coordination) {
    $coordination = [PSCustomObject]@{
        mode                    = "planning-only"
        resume_policy           = "manual-command"
        manual_release_required = $true
        release_trigger_command = "liberado para implementar"
        reason                  = "coordination-file-missing-default-applied"
        owner                   = "unknown"
        updated_at              = Get-V2Timestamp
    }
    $unverified.Add("missing-coordination-mode-defaulted")
}

$canonicalLocks = Get-V2JsonContent -Path $canonicalLocksPath
if (-not $canonicalLocks) {
    $canonicalLocks = [PSCustomObject]@{ locks = @() }
    $unverified.Add("missing-canonical-locks-defaulted")
}

$activeStatuses = @("active", "locked", "in-progress")
$activeLocks = @(
    @($canonicalLocks.locks | Where-Object {
            $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "active")
            $activeStatuses -contains $status.ToLowerInvariant()
        })
)

$alternativeLocks = Get-V2JsonContent -Path $alternativeLocksPath
if (-not $alternativeLocks) {
    $unverified.Add("missing-alternative-locks")
    $alternativeLocks = [PSCustomObject]@{}
}

$alternativeLockCount = 0
$alternativeProperties = @($alternativeLocks.PSObject.Properties)
$hasLocksProperty = (@($alternativeProperties | Where-Object { $_.Name -eq "locks" }).Count -gt 0)
if ($alternativeLocks -and $hasLocksProperty) {
    $alternativeLockCount = @($alternativeLocks.locks).Count
}
elseif ($alternativeLocks -and $alternativeProperties.Count -gt 0) {
    $alternativeLockCount = $alternativeProperties.Count
}

if ($activeLocks.Count -gt 0 -and $alternativeLockCount -eq 0) {
    $unverified.Add("lock-dual-source-drift")
}

$taskDag = Get-V2JsonContent -Path $taskDagPath
if (-not $taskDag) {
    $unverified.Add("missing-task-dag")
    $taskDag = [PSCustomObject]@{ tasks = @() }
}

$blockedStatus = [string](Get-V2OptionalProperty -InputObject $projectState -Name "status" -DefaultValue "unknown")
$isBlocked = $blockedStatus -like "blocked-*"
$mode = [string](Get-V2OptionalProperty -InputObject $coordination -Name "mode" -DefaultValue "planning-only")
$canPatch = ($mode -eq "execution-enabled" -and -not $isBlocked -and $activeLocks.Count -eq 0 -and $unverified.Count -eq 0)

$result = [PSCustomObject]@{
    project_path             = $resolvedProjectPath
    coordination_mode        = $mode
    resume_policy            = [string](Get-V2OptionalProperty -InputObject $coordination -Name "resume_policy" -DefaultValue "manual-command")
    release_trigger_command  = [string](Get-V2OptionalProperty -InputObject $coordination -Name "release_trigger_command" -DefaultValue "liberado para implementar")
    project_status           = $blockedStatus
    project_is_blocked       = $isBlocked
    active_lock_count        = $activeLocks.Count
    alternative_lock_count   = $alternativeLockCount
    dag_task_count           = @($taskDag.tasks).Count
    can_patch                = $canPatch
    unverified               = @($unverified.ToArray())
    checked_at               = Get-V2Timestamp
    sources                  = [PSCustomObject]@{
        project_state   = $statePath
        coordination    = $coordinationPath
        canonical_locks = $canonicalLocksPath
        alt_locks       = $alternativeLocksPath
        task_dag        = $taskDagPath
    }
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Coordination status"
    Write-Output "Project: $resolvedProjectPath"
    Write-Output "Mode: $($result.coordination_mode)"
    Write-Output "Project status: $($result.project_status)"
    Write-Output "Active locks: $($result.active_lock_count)"
    Write-Output "Can patch: $($result.can_patch)"
    if (@($result.unverified).Count -gt 0) {
        Write-Output "UNVERIFIED:"
        foreach ($item in @($result.unverified)) {
            Write-Output "- $item"
        }
    }
}

