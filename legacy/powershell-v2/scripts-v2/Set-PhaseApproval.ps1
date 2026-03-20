<#
.SYNOPSIS
    Updates human approval status for orchestration phases.
.DESCRIPTION
    Persists phase approvals in ai-orchestrator/state/project-state.json:
      phase_approvals.context
      phase_approvals.architecture
      phase_approvals.execution
      phase_approvals.release

    This is used by Run-AgentLoop and release pipeline to enforce human checkpoints.
.PARAMETER ProjectPath
    Target project root.
.PARAMETER Phase
    One of: context, architecture, execution, release.
.PARAMETER Status
    One of: approved, rejected, pending.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [ValidateSet("context", "architecture", "execution", "release")]
    [string]$Phase = "context",
    [ValidateSet("approved", "rejected", "pending")]
    [string]$Status = "approved"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$statePath = Join-Path $resolvedProjectPath "ai-orchestrator/state/project-state.json"
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw "project-state.json not found: $statePath"
}

$state = Get-V2JsonContent -Path $statePath
if (-not $state) {
    throw "Invalid project-state.json: $statePath"
}

$phaseApprovals = Get-V2OptionalProperty -InputObject $state -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
if (-not $phaseApprovals) { $phaseApprovals = [PSCustomObject]@{} }

$timestamp = Get-V2Timestamp
$entry = [PSCustomObject]@{
    status      = $Status
    updated_at  = $timestamp
    updated_by  = "human"
}

Set-V2DynamicProperty -InputObject $phaseApprovals -Name $Phase -Value $entry
Set-V2DynamicProperty -InputObject $state -Name "phase_approvals" -Value $phaseApprovals
Set-V2DynamicProperty -InputObject $state -Name "updated_at" -Value $timestamp

Save-V2JsonContent -Path $statePath -Value $state

$result = [PSCustomObject]@{
    success      = $true
    project_path = $resolvedProjectPath
    phase        = $Phase
    status       = $Status
    updated_at   = $timestamp
}

$result | ConvertTo-Json -Depth 8
