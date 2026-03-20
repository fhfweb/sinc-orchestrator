<#
.SYNOPSIS
    Sets the coordination mode for a project (freeze / release / normal).
.DESCRIPTION
    Updates the coordination-mode.json file inside .ai-orchestrator/ and optionally
    writes a freeze/release record to communication/whiteboard.md.
    Used by the Master Orchestrator to prevent conflicting agent execution during
    high-risk operations (migrations, deployments, major refactors).
    Modes: freeze (all agents blocked) | release (unblocks all) | normal (standard operation)
.PARAMETER ProjectPath
    Path to the project root containing .ai-orchestrator/. Defaults to current directory.
.PARAMETER Mode
    Coordination mode: freeze | release | normal
.PARAMETER Reason
    Optional reason string logged to the whiteboard.
.EXAMPLE
    .\scripts\v2\Set-CoordinationMode.ps1 -ProjectPath C:\projects\myapp -Mode freeze -Reason 'Running migration'
    .\scripts\v2\Set-CoordinationMode.ps1 -ProjectPath C:\projects\myapp -Mode release
#>param(
    [string]$ProjectPath = ".",
    [ValidateSet("planning-only", "execution-enabled")]
    [string]$Mode = "planning-only",
    [string]$Reason = "",
    [string]$ConfirmPhrase = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

if ($Mode -eq "execution-enabled" -and $ConfirmPhrase -ne "liberado para implementar") {
    throw "To release execution mode you must pass -ConfirmPhrase 'liberado para implementar'."
}

$stateDir = Join-Path $resolvedProjectPath "ai-orchestrator/state"
Initialize-V2Directory -Path $stateDir

$coordinationPath = Join-Path $stateDir "coordination-mode.json"
$existing = Get-V2JsonContent -Path $coordinationPath
$resumePolicy = "manual-command"
$manualReleaseRequired = $true
$triggerCommand = "liberado para implementar"
$owner = if ($env:USERNAME) { [string]$env:USERNAME } else { "unknown" }
$now = Get-V2Timestamp

$payload = [PSCustomObject]@{
    mode                    = $Mode
    resume_policy           = $resumePolicy
    manual_release_required = $manualReleaseRequired
    release_trigger_command = $triggerCommand
    reason                  = $Reason
    owner                   = $owner
    updated_at              = $now
    previous_mode           = [string](Get-V2OptionalProperty -InputObject $existing -Name "mode" -DefaultValue "unknown")
}

if (-not $existing -or -not (Get-V2OptionalProperty -InputObject $existing -Name "created_at" -DefaultValue "")) {
    Add-Member -InputObject $payload -MemberType NoteProperty -Name "created_at" -Value $now -Force
}
else {
    Add-Member -InputObject $payload -MemberType NoteProperty -Name "created_at" -Value ([string]$existing.created_at) -Force
}

Save-V2JsonContent -Path $coordinationPath -Value $payload

Write-Output "Coordination mode updated."
Write-Output "Project: $resolvedProjectPath"
Write-Output "Mode: $Mode"
Write-Output "State: $coordinationPath"

