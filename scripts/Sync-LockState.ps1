<#
.SYNOPSIS
    Sync lock views between ai-orchestrator/locks/locks.json and docs/agents/LOCKS.md.
.DESCRIPTION
    Canonical source is ai-orchestrator/locks/locks.json.
    This script refreshes:
      - docs/agents/LOCKS.md (Active Locks table)
      - docs/agents/LOCKS.json (machine-readable mirror for markdown-only agents)
.PARAMETER ProjectPath
    Project root path.
.EXAMPLE
    .\scripts\Sync-LockState.ps1 -ProjectPath C:\projects\myapp
#>
param([Parameter(Mandatory)][string]$ProjectPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ActiveLocks {
    param([object[]]$Locks)

    $activeStatuses = @("active", "locked", "in-progress")
    return @(
        @($Locks) | Where-Object {
            $status = [string]$_.status
            if ([string]::IsNullOrWhiteSpace($status)) { $status = "active" }
            $activeStatuses -contains $status.ToLowerInvariant()
        }
    )
}

function Build-LockRow {
    param([object]$Lock)

    $taskId = [string]$Lock.task_id
    if ([string]::IsNullOrWhiteSpace($taskId)) { $taskId = "-" }
    $agent = [string]$Lock.agent
    if ([string]::IsNullOrWhiteSpace($agent)) { $agent = "-" }
    $lockedAt = [string]$Lock.locked_at
    if ([string]::IsNullOrWhiteSpace($lockedAt)) { $lockedAt = "-" }
    $ttl = [string]$Lock.ttl_seconds
    if ([string]::IsNullOrWhiteSpace($ttl)) { $ttl = "-" } else { $ttl = "$ttl sec" }
    $path = [string]$Lock.file_path
    if ([string]::IsNullOrWhiteSpace($path)) { $path = "-" }

    return "| $taskId | $agent | $lockedAt | $ttl | $path |"
}

$resolvedProject = if (Test-Path -LiteralPath $ProjectPath) { (Resolve-Path -LiteralPath $ProjectPath).Path } else { $ProjectPath }
if (-not (Test-Path -LiteralPath $resolvedProject -PathType Container)) {
    throw "Project path not found: $ProjectPath"
}

$jsonCandidates = @(
    (Join-Path $resolvedProject "ai-orchestrator/locks/locks.json"),
    (Join-Path $resolvedProject ".ai-orchestrator/locks/locks.json")
)
$locksJsonPath = ""
foreach ($candidate in $jsonCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $locksJsonPath = $candidate
        break
    }
}
if ([string]::IsNullOrWhiteSpace($locksJsonPath)) {
    throw "locks.json not found under ai-orchestrator/locks or .ai-orchestrator/locks."
}

$docsAgentsPath = Join-Path $resolvedProject "docs/agents"
if (-not (Test-Path -LiteralPath $docsAgentsPath -PathType Container)) {
    [void](New-Item -ItemType Directory -Path $docsAgentsPath -Force)
}

$locksMdPath = Join-Path $docsAgentsPath "LOCKS.md"
$locksMirrorPath = Join-Path $docsAgentsPath "LOCKS.json"
if (-not (Test-Path -LiteralPath $locksMdPath -PathType Leaf)) {
    $template = @(
        "# Lock Manager",
        "",
        "## Active Locks",
        "",
        "| TASK-ID | Agent | Locked At | TTL | Files Affected |",
        "|---------|-------|-----------|-----|----------------|",
        "| -       | -     | -         | -   | -              |",
        "",
        "<!-- Sync source: ai-orchestrator/locks/locks.json -->",
        "",
        "## Lock Rules",
        "",
        "1. Lock task files before edits.",
        "2. Release locks when task leaves in-progress.",
        "3. Resolve stale locks before next assignment."
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText($locksMdPath, $template)
}

$doc = Get-Content -LiteralPath $locksJsonPath -Raw | ConvertFrom-Json
$allLocks = @()
if ($doc -and ($doc.PSObject.Properties.Name -contains "locks")) {
    $allLocks = @($doc.locks)
}

$activeLocks = @(Get-ActiveLocks -Locks $allLocks)
$rows = New-Object System.Collections.Generic.List[string]
if ($activeLocks.Count -eq 0) {
    $rows.Add("| -       | -     | -         | -   | -              |")
}
else {
    foreach ($lock in $activeLocks) {
        $rows.Add((Build-LockRow -Lock $lock))
    }
}

$newSection = @(
    "## Active Locks",
    "",
    "| TASK-ID | Agent | Locked At | TTL | Files Affected |",
    "|---------|-------|-----------|-----|----------------|"
) + @($rows.ToArray()) + @(
    "",
    "<!-- Sync source: ai-orchestrator/locks/locks.json -->",
    ""
)

$mdContent = Get-Content -LiteralPath $locksMdPath -Raw
$pattern = "(?s)## Active Locks.*?(?=## Lock Rules)"
$replacement = ($newSection -join [Environment]::NewLine)
if ($mdContent -match $pattern) {
    $updated = [regex]::Replace($mdContent, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement })
}
else {
    $updated = $mdContent.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $replacement + [Environment]::NewLine + [Environment]::NewLine
}
[System.IO.File]::WriteAllText($locksMdPath, $updated)

$mirror = [PSCustomObject]@{
    generated_at = (Get-Date).ToString("s")
    source = (Resolve-Path -LiteralPath $locksJsonPath).Path
    locks = @($activeLocks)
}
[System.IO.File]::WriteAllText($locksMirrorPath, ($mirror | ConvertTo-Json -Depth 16))

Write-Output "Lock sync complete."
Write-Output "Source: $locksJsonPath"
Write-Output "Active locks: $($activeLocks.Count)"
