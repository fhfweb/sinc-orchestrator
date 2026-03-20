<#
.SYNOPSIS
    Aggregates health and governance reports from all managed projects in the workspace.
#>
param(
    [string]$WorkspacePath = "g:\Fernando\project0\workspace",
    [string]$OutputPath = "g:\Fernando\project0\reports\global-workspace-status.md",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$commonPath = Join-Path $PSScriptRoot "Common.ps1"
if (Test-Path -LiteralPath $commonPath) {
    . $commonPath
}

function Get-JsonData {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw
        if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch { return $null }
}

$projectsDir = Join-Path $WorkspacePath "projects"
if (-not (Test-Path -LiteralPath $projectsDir -PathType Container)) {
    Write-Error "Projects directory not found at $projectsDir"
    return
}

$projectFolders = Get-ChildItem -Path $projectsDir -Directory
$aggregatedData = New-Object System.Collections.Generic.List[object]

foreach ($folder in $projectFolders) {
    $orchestratorDir = Join-Path $folder.FullName "ai-orchestrator"
    if (-not (Test-Path -LiteralPath $orchestratorDir -PathType Container)) {
        continue
    }

    $policyPath = Join-Path $orchestratorDir "reports/latest-policy-report.json"
    $decisionPath = Join-Path $orchestratorDir "reports/orchestrator-360-decision-report.json"
    
    $pData = Get-JsonData -Path $policyPath
    $dData = Get-JsonData -Path $decisionPath
    
    $gScore = 0
    $rScore = 0
    $statusStr = "missing"
    $updateTime = ""
    $findCount = 0

    if ($null -ne $dData) {
        $statusStr = [string]$dData.status
        if ([string]::IsNullOrWhiteSpace($statusStr)) { $statusStr = "unknown" }
        
        if ($null -ne $dData.scores) {
            $rScore = [int]$dData.scores.overall
            $gScore = [int]$dData.scores.quality
        }
        $updateTime = [string]$dData.generated_at
    }

    if ($null -ne $pData) {
        if ($null -ne $pData.findings) {
            $findCount = @($pData.findings).Count
        }
    }
    
    $aggregatedData.Add([PSCustomObject]@{
        name = $folder.Name
        status = $statusStr
        readiness = $rScore
        governance = $gScore
        findings = $findCount
        updated = $updateTime
    })
}

$ts = (Get-Date).ToString("s")
$md = New-Object System.Collections.Generic.List[string]
$md.Add("# Workspace Global Status Dashboard")
$md.Add("")
$md.Add("- Generated At: $ts")
$md.Add("- Total Projects: $($aggregatedData.Count)")
$md.Add("")
$md.Add("| Project | Status | Readiness | Governance | Findings | Last Update |")
$md.Add("| :--- | :---: | :---: | :---: | :---: | :--- |")

foreach ($item in $aggregatedData) {
    $icon = "OK"
    if ($item.status -eq "degraded") { $icon = "!!" }
    elseif ($item.status -eq "missing") { $icon = "--" }

    $md.Add("| $($item.name) | $icon $($item.status) | $($item.readiness)% | $($item.governance)% | $($item.findings) | $($item.updated) |")
}

$outDir = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

[System.IO.File]::WriteAllText($OutputPath, ($md -join "`r`n"))

if ($EmitJson) {
    $aggregatedData | ConvertTo-Json -Depth 10
}
else {
    Write-Host "Dashboard generated: $OutputPath"
}
