<#
.SYNOPSIS
    Regenerates docs/agents/DASHBOARD.md from V2 runtime state.
.PARAMETER ProjectPath
    Optional single project root. If omitted, uses workspace/PROJECT_REGISTRY.json.
.PARAMETER DashboardPath
    Relative output path from repository root.
#>
param(
    [string]$ProjectPath = "",
    [string]$DashboardPath = "docs/agents/DASHBOARD.md"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-SafePropertyValue {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$DefaultValue = ""
    )

    if ($null -eq $InputObject -or [string]::IsNullOrWhiteSpace($Name)) {
        return $DefaultValue
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) {
            return $InputObject[$Name]
        }
        return $DefaultValue
    }

    if ($InputObject.PSObject -and ($InputObject.PSObject.Properties.Name -contains $Name)) {
        return $InputObject.$Name
    }

    return $DefaultValue
}

function Resolve-ProjectRoots {
    param(
        [string]$Root,
        [string]$ProjectPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ProjectPath)) {
        if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
            throw "Project path not found: $ProjectPath"
        }
        return @((Resolve-Path -LiteralPath $ProjectPath).Path)
    }

    $registryPath = Join-Path $Root "workspace/PROJECT_REGISTRY.json"
    if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
        return @($Root)
    }

    $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
    $projectsNode = if ($registry.PSObject.Properties.Name -contains "Projects") { $registry.Projects } else { $registry.projects }
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($projectsNode)) {
        $candidate = [string]$item.WorkingPath
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            $candidate = [string]$item.path
        }
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            $candidate = [string]$item.SourcePath
        }
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            if ($roots -notcontains $resolved) {
                $roots.Add($resolved)
            }
        }
    }

    if ($roots.Count -eq 0) {
        return @($Root)
    }
    return @($roots.ToArray())
}

function Get-ProjectMetrics {
    param([string]$ProjectRoot)

    $slug = Split-Path -Leaf $ProjectRoot
    $dagPath = Join-Path $ProjectRoot "ai-orchestrator/tasks/task-dag.json"
    if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
        $dagPath = Join-Path $ProjectRoot ".ai-orchestrator/tasks/task-dag.json"
    }
    $locksPath = Join-Path $ProjectRoot "ai-orchestrator/locks/locks.json"
    if (-not (Test-Path -LiteralPath $locksPath -PathType Leaf)) {
        $locksPath = Join-Path $ProjectRoot ".ai-orchestrator/locks/locks.json"
    }
    $healthPath = Join-Path $ProjectRoot "ai-orchestrator/state/health-report.json"
    $reputationPath = Join-Path $ProjectRoot "ai-orchestrator/agents/reputation.json"
    $metaCalibrationPath = Join-Path $ProjectRoot "ai-orchestrator/state/meta-calibration.json"

    $counts = [ordered]@{
        pending = 0
        in_progress = 0
        blocked = 0
        done = 0
    }
    $criticalPath = @()

    if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
        $dag = Get-Content -LiteralPath $dagPath -Raw | ConvertFrom-Json
        $tasks = @()
        if ($dag.PSObject.Properties.Name -contains "tasks") {
            $tasks = @($dag.tasks)
        }
        foreach ($task in $tasks) {
            $status = [string](Get-SafePropertyValue -InputObject $task -Name "status" -DefaultValue "pending")
            if ([string]::IsNullOrWhiteSpace($status)) { $status = "pending" }
            $status = $status.ToLowerInvariant()
            if ($status -eq "in-progress") { $counts.in_progress++ }
            elseif ($status -in @("done", "completed", "skipped")) { $counts.done++ }
            elseif ($status.StartsWith("blocked")) { $counts.blocked++ }
            else { $counts.pending++ }
        }

        $criticalPath = @(
            @($tasks | Where-Object {
                    ([string](Get-SafePropertyValue -InputObject $_ -Name "status" -DefaultValue "pending")).ToLowerInvariant() -eq "pending"
                }) |
            Sort-Object `
                @{ Expression = {
                        switch (([string](Get-SafePropertyValue -InputObject $_ -Name "priority" -DefaultValue "")).ToUpperInvariant()) {
                            "P0" { 100 }
                            "P1" { 70 }
                            "P2" { 40 }
                            "P3" { 10 }
                            default { 0 }
                        }
                    }; Descending = $true },
                @{ Expression = { [string](Get-SafePropertyValue -InputObject $_ -Name "id" -DefaultValue "") }; Descending = $false } |
            Select-Object -First 3
        )
    }

    $activeLocks = 0
    if (Test-Path -LiteralPath $locksPath -PathType Leaf) {
        $lockDoc = Get-Content -LiteralPath $locksPath -Raw | ConvertFrom-Json
        if ($lockDoc -and ($lockDoc.PSObject.Properties.Name -contains "locks")) {
            $activeLocks = @(
                @($lockDoc.locks) | Where-Object {
                    $status = [string]$_.status
                    if ([string]::IsNullOrWhiteSpace($status)) { $status = "active" }
                    $status.ToLowerInvariant() -in @("active", "locked", "in-progress")
                }
            ).Count
        }
    }

    $health = "unknown"
    if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
        $healthDoc = Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json
        $health = [string]$healthDoc.health_status
        if ([string]::IsNullOrWhiteSpace($health)) { $health = "unknown" }
    }

    $topAgents = @()
    $reputationUpdatedAt = ""
    if (Test-Path -LiteralPath $reputationPath -PathType Leaf) {
        try {
            $reputationDoc = Get-Content -LiteralPath $reputationPath -Raw | ConvertFrom-Json
            $reputationUpdatedAt = [string](Get-SafePropertyValue -InputObject $reputationDoc -Name "generated_at" -DefaultValue "")
            $agents = @((Get-SafePropertyValue -InputObject $reputationDoc -Name "agents" -DefaultValue @()))
            $topAgents = @(
                @($agents | Sort-Object `
                        @{ Expression = { [double](Get-SafePropertyValue -InputObject $_ -Name "reputation_fit_score" -DefaultValue 0.0) }; Descending = $true },
                        @{ Expression = { [string](Get-SafePropertyValue -InputObject $_ -Name "agent" -DefaultValue "") }; Descending = $false }) |
                Select-Object -First 3
            )
        }
        catch {
            $topAgents = @()
            $reputationUpdatedAt = ""
        }
    }

    $metaCalibrationUpdatedAt = ""
    if (Test-Path -LiteralPath $metaCalibrationPath -PathType Leaf) {
        try {
            $metaDoc = Get-Content -LiteralPath $metaCalibrationPath -Raw | ConvertFrom-Json
            $metaCalibrationUpdatedAt = [string](Get-SafePropertyValue -InputObject $metaDoc -Name "generated_at" -DefaultValue "")
        }
        catch {
            $metaCalibrationUpdatedAt = ""
        }
    }

    return [PSCustomObject]@{
        slug = $slug
        root = $ProjectRoot
        counts = $counts
        active_locks = $activeLocks
        health = $health
        critical_path = @($criticalPath)
        top_agents = @($topAgents)
        reputation_updated_at = $reputationUpdatedAt
        meta_calibration_updated_at = $metaCalibrationUpdatedAt
    }
}

$root = Split-Path -Parent $PSScriptRoot
$dashboardOut = Join-Path $root $DashboardPath
$projectRoots = Resolve-ProjectRoots -Root $root -ProjectPath $ProjectPath
$metrics = @($projectRoots | ForEach-Object { Get-ProjectMetrics -ProjectRoot $_ })

$totalPending = (@($metrics | ForEach-Object { $_.counts.pending }) | Measure-Object -Sum).Sum
$totalInProgress = (@($metrics | ForEach-Object { $_.counts.in_progress }) | Measure-Object -Sum).Sum
$totalBlocked = (@($metrics | ForEach-Object { $_.counts.blocked }) | Measure-Object -Sum).Sum
$totalDone = (@($metrics | ForEach-Object { $_.counts.done }) | Measure-Object -Sum).Sum
$totalLocks = (@($metrics | ForEach-Object { $_.active_locks }) | Measure-Object -Sum).Sum
$crossProjectPatternsPath = Join-Path $root "memory_graph/patterns"
$crossProjectPatternCount = if (Test-Path -LiteralPath $crossProjectPatternsPath -PathType Container) {
    @((Get-ChildItem -LiteralPath $crossProjectPatternsPath -Filter "*.md" -File -ErrorAction SilentlyContinue)).Count
}
else { 0 }

$lines = New-Object System.Collections.Generic.List[string]
$now = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")

$lines.Add("# System Dashboard")
$lines.Add("")
$lines.Add("> Auto-generated by `scripts/Update-Dashboard.ps1` on $now")
$lines.Add("")
$lines.Add("## Global Summary")
$lines.Add("")
$lines.Add("| Metric | Value |")
$lines.Add("|--------|-------|")
$lines.Add("| Projects | $($metrics.Count) |")
$lines.Add("| Pending | $totalPending |")
$lines.Add("| In Progress | $totalInProgress |")
$lines.Add("| Blocked | $totalBlocked |")
$lines.Add("| Done | $totalDone |")
$lines.Add("| Active Locks | $totalLocks |")
$lines.Add("| Cross-Project Patterns | $crossProjectPatternCount |")
$lines.Add("")
$lines.Add("## Per Project")
$lines.Add("")
$lines.Add("| Project | Health | Pending | In Progress | Blocked | Done | Locks |")
$lines.Add("|---------|--------|---------|-------------|---------|------|-------|")
foreach ($item in $metrics) {
    $lines.Add("| $($item.slug) | $($item.health) | $($item.counts.pending) | $($item.counts.in_progress) | $($item.counts.blocked) | $($item.counts.done) | $($item.active_locks) |")
}

$lines.Add("")
$lines.Add("## Agent Reputation (Top 3 per project)")
$lines.Add("")
foreach ($item in $metrics) {
    $lines.Add("### $($item.slug)")
    $repUpdated = if ([string]::IsNullOrWhiteSpace($item.reputation_updated_at)) { "n/a" } else { $item.reputation_updated_at }
    $metaUpdated = if ([string]::IsNullOrWhiteSpace($item.meta_calibration_updated_at)) { "n/a" } else { $item.meta_calibration_updated_at }
    $lines.Add("- Reputation updated: $repUpdated")
    $lines.Add("- Meta-calibration updated: $metaUpdated")
    if (@($item.top_agents).Count -eq 0) {
        $lines.Add("- top agents: none")
    }
    else {
        foreach ($agent in @($item.top_agents)) {
            $agentName = [string](Get-SafePropertyValue -InputObject $agent -Name "agent" -DefaultValue "unknown")
            $fitScore = [double](Get-SafePropertyValue -InputObject $agent -Name "reputation_fit_score" -DefaultValue 0.0)
            $sampleCount = [int](Get-SafePropertyValue -InputObject $agent -Name "runtime_samples" -DefaultValue 0)
            $lines.Add("- $agentName | fit=$([Math]::Round($fitScore, 3)) | samples=$sampleCount")
        }
    }
    $lines.Add("")
}

$lines.Add("## Cross-Project Memory")
$lines.Add("")
$lines.Add("- Global pattern library: memory_graph/patterns")
$lines.Add("- Pattern count: $crossProjectPatternCount")
$lines.Add("")
$lines.Add("## Critical Path (Top Pending)")
$lines.Add("")
foreach ($item in $metrics) {
    $lines.Add("### $($item.slug)")
    if (@($item.critical_path).Count -eq 0) {
        $lines.Add("- none")
    }
    else {
        foreach ($task in @($item.critical_path)) {
            $id = [string](Get-SafePropertyValue -InputObject $task -Name "id" -DefaultValue "")
            $priority = [string](Get-SafePropertyValue -InputObject $task -Name "priority" -DefaultValue "")
            $title = [string](Get-SafePropertyValue -InputObject $task -Name "title" -DefaultValue "")
            if ([string]::IsNullOrWhiteSpace($title)) { $title = [string](Get-SafePropertyValue -InputObject $task -Name "description" -DefaultValue "") }
            if ([string]::IsNullOrWhiteSpace($title)) { $title = "-" }
            $lines.Add("- $id [$priority] $title")
        }
    }
    $lines.Add("")
}

[System.IO.File]::WriteAllText($dashboardOut, ($lines -join [Environment]::NewLine))

# ── dashboard.json: machine-readable live state for agents ───────────────────────────────────
$dashboardJson = [PSCustomObject]@{
    generated_at    = (Get-Date).ToString("o")
    summary = [PSCustomObject]@{
        projects    = $metrics.Count
        pending     = $totalPending
        in_progress = $totalInProgress
        blocked     = $totalBlocked
        done        = $totalDone
        active_locks = $totalLocks
        cross_project_patterns = $crossProjectPatternCount
    }
    projects = @($metrics | ForEach-Object {
        [PSCustomObject]@{
            slug         = $_.slug
            health       = $_.health
            pending      = $_.counts.pending
            in_progress  = $_.counts.in_progress
            blocked      = $_.counts.blocked
            done         = $_.counts.done
            active_locks = $_.active_locks
            reputation_updated_at = $_.reputation_updated_at
            meta_calibration_updated_at = $_.meta_calibration_updated_at
            top_agents = @($_.top_agents | ForEach-Object {
                [PSCustomObject]@{
                    agent = [string](Get-SafePropertyValue -InputObject $_ -Name "agent" -DefaultValue "")
                    reputation_fit_score = [double](Get-SafePropertyValue -InputObject $_ -Name "reputation_fit_score" -DefaultValue 0.0)
                    runtime_samples = [int](Get-SafePropertyValue -InputObject $_ -Name "runtime_samples" -DefaultValue 0)
                    runtime_success_rate = [double](Get-SafePropertyValue -InputObject $_ -Name "runtime_success_rate" -DefaultValue 0.0)
                    runtime_avg_duration_ms = [int](Get-SafePropertyValue -InputObject $_ -Name "runtime_avg_duration_ms" -DefaultValue 0)
                    runtime_timeout_rate = [double](Get-SafePropertyValue -InputObject $_ -Name "runtime_timeout_rate" -DefaultValue 0.0)
                }
            })
            next_tasks   = @($_.critical_path | ForEach-Object {
                $taskTitle = [string](Get-SafePropertyValue -InputObject $_ -Name "title" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($taskTitle)) {
                    $taskTitle = [string](Get-SafePropertyValue -InputObject $_ -Name "description" -DefaultValue "")
                }
                [PSCustomObject]@{
                    id       = [string](Get-SafePropertyValue -InputObject $_ -Name "id" -DefaultValue "")
                    priority = [string](Get-SafePropertyValue -InputObject $_ -Name "priority" -DefaultValue "")
                    title    = $taskTitle
                }
            })
        }
    })
}
$jsonOutPath = Join-Path (Split-Path -Parent $dashboardOut) "dashboard.json"
$dashboardJson | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $jsonOutPath -Encoding UTF8
# ─────────────────────────────────────────────────────────────────────────────────────────────

Write-Output "Dashboard updated: $dashboardOut"
Write-Output "Dashboard JSON:    $jsonOutPath"
