<#
.SYNOPSIS
    Generates per-round executive READY/NOT READY readiness report.
.DESCRIPTION
    Reads DAG state, health report, mutation score, FinOps status, and open REPAIR tasks.
    Emits a verdict: READY | DEGRADED | NOT_READY.
    Writes ai-orchestrator/reports/readiness-<ts>.json and
    overwrites ai-orchestrator/reports/readiness-latest.json (always-current snapshot).
    Wired into the autonomous loop after MutationPolicyEnforcer — runs every cycle.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER MutationMinScorePct
    Minimum acceptable mutation score. Default: 85.
.PARAMETER EmitJson
    Emit JSON result to stdout.
.EXAMPLE
    .\scripts\v2\Invoke-ReadinessReportV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-ReadinessReportV2.ps1 -ProjectPath C:\projects\myapp -EmitJson
#>
param(
    [string]$ProjectPath        = ".",
    [double]$MutationMinScorePct = 85.0,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$reportsDir       = Join-Path $orchestratorRoot "reports"
$ts               = Get-Date -Format "yyyyMMddHHmmss"
$reportPath       = Join-Path $reportsDir "readiness-$ts.json"
$latestPath       = Join-Path $reportsDir "readiness-latest.json"

Initialize-V2Directory -Path $reportsDir

# ── Data paths ──────────────────────────────────────────────────────────────
$dagPath    = Join-Path $orchestratorRoot "tasks/task-dag.json"
$healthPath = Join-Path $orchestratorRoot "state/health-report.json"
$roadmapPath = Join-Path $orchestratorRoot "memory/roadmap.md"
$taskStateDbScriptPath = Join-Path $PSScriptRoot "task_state_db.py"

# ── Helpers ──────────────────────────────────────────────────────────────────
$checks   = New-Object System.Collections.Generic.List[object]
$blockers = New-Object System.Collections.Generic.List[string]

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $checks.Add([PSCustomObject]@{ name = $Name; passed = $Passed; detail = $Detail })
    if (-not $Passed) { $blockers.Add(("{0}: {1}" -f $Name, $Detail)) }
}

# ── Check 1: Open REPAIR tasks ───────────────────────────────────────────────
$openRepairCount = 0
$pendingCount    = 0
$inProgressCount = 0
$blockedCount    = 0
$openExecutionCount = 0
$taskMetricsSource = "dag"

$taskStateDbStatus = $null
if (Test-Path -LiteralPath $taskStateDbScriptPath -PathType Leaf) {
    try {
        $statusRaw = @(python $taskStateDbScriptPath --project-path $resolvedPath --mode status --emit-json 2>&1)
        if ($LASTEXITCODE -eq 0) {
            $statusPayload = (($statusRaw -join [Environment]::NewLine).Trim())
            if (-not [string]::IsNullOrWhiteSpace($statusPayload)) {
                $taskStateDbStatus = ($statusPayload | ConvertFrom-Json -ErrorAction Stop)
            }
        }
    }
    catch {
        $taskStateDbStatus = $null
    }
}

$taskStateDbOk = [bool](Get-V2OptionalProperty -InputObject $taskStateDbStatus -Name "ok" -DefaultValue $false)
$taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatus -Name "backend_mode" -DefaultValue "")
$taskStateDbOpenRepairs = [int](Get-V2OptionalProperty -InputObject $taskStateDbStatus -Name "open_repairs" -DefaultValue -1)
$taskStateDbOpenExecution = [int](Get-V2OptionalProperty -InputObject $taskStateDbStatus -Name "open_execution_tasks" -DefaultValue -1)
$taskStateDbStatusCounts = Get-V2OptionalProperty -InputObject $taskStateDbStatus -Name "status_counts" -DefaultValue ([PSCustomObject]@{})
$taskStateDbPending = [int](Get-V2OptionalProperty -InputObject $taskStateDbStatusCounts -Name "pending" -DefaultValue 0)
$taskStateDbInProgress = [int](Get-V2OptionalProperty -InputObject $taskStateDbStatusCounts -Name "in-progress" -DefaultValue 0)
$taskStateDbBlocked = 0
foreach ($prop in @($taskStateDbStatusCounts.PSObject.Properties)) {
    if ([string]$prop.Name -like "blocked*") {
        $taskStateDbBlocked += [int]$prop.Value
    }
}

if ($taskStateDbOk -and $taskStateDbOpenExecution -ge 0) {
    $taskMetricsSource = "task_state_db"
    $openExecutionCount = [Math]::Max($taskStateDbOpenExecution, 0)
    if ($taskStateDbOpenRepairs -ge 0) {
        $openRepairCount = [Math]::Max($taskStateDbOpenRepairs, 0)
    }
    $pendingCount = [Math]::Max($taskStateDbPending, 0)
    $inProgressCount = [Math]::Max($taskStateDbInProgress, 0)
    $blockedCount = [Math]::Max($taskStateDbBlocked, 0)
}

if ($taskMetricsSource -eq "dag" -and (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
    try {
        $dag   = Get-V2JsonContent -Path $dagPath
        $tasks = @(Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @())
        foreach ($t in $tasks) {
            $id     = [string](Get-V2OptionalProperty -InputObject $t -Name "id"     -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $t -Name "status" -DefaultValue "")
            if ($id -like "REPAIR-*" -and $status -in @("pending", "in-progress")) { $openRepairCount++ }
            if ($status -eq "pending")     { $pendingCount++ }
            if ($status -eq "in-progress") { $inProgressCount++ }
            if ($status -like "blocked-*") { $blockedCount++ }
            $isExecutionTask = ($id -like "FEAT-*") -or ($id -like "DEV-*") -or ($id -like "COBERTURA-*") -or ($id -like "RECHECK-*")
            if ($isExecutionTask -and $status -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")) {
                $openExecutionCount++
            }
        }
    }
    catch {
        Add-Check -Name "dag-readable" -Passed $false -Detail "Cannot read task-dag.json: $($_.Exception.Message)"
    }
}

$taskStateDetail = if ($taskStateDbOk) {
    "ok backend_mode=$taskStateDbBackendMode source=$taskMetricsSource"
}
else {
    "unavailable source=$taskMetricsSource"
}
Add-Check -Name "task-state-db-status" -Passed $taskStateDbOk -Detail $taskStateDetail

Add-Check -Name "no-open-repair-tasks" `
    -Passed ($openRepairCount -eq 0) `
    -Detail ("$openRepairCount open REPAIR task(s) via $taskMetricsSource")

# ── Check 1b: Roadmap pending requires open execution backlog ───────────────────
$roadmapHasPending = $false
if (Test-Path -LiteralPath $roadmapPath -PathType Leaf) {
    try {
        $roadmapContent = Get-Content -LiteralPath $roadmapPath -Raw -ErrorAction Stop
        $roadmapHasPending = $roadmapContent -match "(?im)^-\s*pending\s*$"
    }
    catch {
        $roadmapHasPending = $false
    }
}
$roadmapBacklogOk = (-not $roadmapHasPending) -or ($openExecutionCount -gt 0)
Add-Check -Name "roadmap-backlog-alignment" `
    -Passed $roadmapBacklogOk `
    -Detail ("roadmap_pending={0}; open_execution_tasks={1}; source={2}" -f $roadmapHasPending, $openExecutionCount, $taskMetricsSource)

# ── Check 2: Health report ───────────────────────────────────────────────────
$healthStatus = "missing"
if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
    try {
        $health       = Get-V2JsonContent -Path $healthPath
        $healthStatus = [string](Get-V2OptionalProperty -InputObject $health -Name "health_status" -DefaultValue "unknown")
    }
    catch { $healthStatus = "unreadable" }
}
$healthOk = $healthStatus -in @("healthy", "ok", "passing")
Add-Check -Name "health-status" -Passed $healthOk -Detail ("health_status=$healthStatus")

# ── Check 3: Latest mutation report ─────────────────────────────────────────
$latestMutationScore    = $null
$latestMutationRun      = 0
$mutationReportPresent  = $false

$mutationFiles = @(Get-ChildItem -LiteralPath $reportsDir -File -Filter "mutation-*.json" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc | Select-Object -Last 1)

if ($mutationFiles.Count -gt 0) {
    try {
        $mr = Get-V2JsonContent -Path $mutationFiles[0].FullName
        $latestMutationScore   = [double](Get-V2OptionalProperty -InputObject $mr -Name "mutation_score_pct" -DefaultValue 0)
        $latestMutationRun     = [int](Get-V2OptionalProperty    -InputObject $mr -Name "mutations_run"       -DefaultValue 0)
        $mutationReportPresent = $true

        if ($latestMutationRun -gt 0) {
            $mutationOk = $latestMutationScore -ge $MutationMinScorePct
            Add-Check -Name "mutation-score" `
                -Passed $mutationOk `
                -Detail ("score={0}% (min={1}%)" -f $latestMutationScore, $MutationMinScorePct)
        }
    }
    catch { $mutationReportPresent = $false }
}

# ── Check 4: FinOps — most recent report ────────────────────────────────────
$finopsFiles = @(Get-ChildItem -LiteralPath $reportsDir -File -Filter "finops-*.json" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc | Select-Object -Last 1)

if ($finopsFiles.Count -gt 0) {
    try {
        $fr             = Get-V2JsonContent -Path $finopsFiles[0].FullName
        $sys            = Get-V2OptionalProperty -InputObject $fr -Name "system" -DefaultValue ([PSCustomObject]@{})
        $ramPct         = [double](Get-V2OptionalProperty -InputObject $sys -Name "ram_used_pct"           -DefaultValue 0)
        $critThreshold  = [int](Get-V2OptionalProperty   -InputObject $fr  -Name "ram_critical_threshold"  -DefaultValue 85)
        $finopsOk       = $ramPct -lt $critThreshold
        Add-Check -Name "finops-ram" `
            -Passed $finopsOk `
            -Detail ("ram={0}% (threshold={1}%)" -f $ramPct, $critThreshold)
    }
    catch {}
}

# ── Check 5: Script validation gate ─────────────────────────────────────────
$scriptValidationPath = Join-Path $reportsDir "script-validation.json"
if (Test-Path -LiteralPath $scriptValidationPath -PathType Leaf) {
    try {
        $sv       = Get-V2JsonContent -Path $scriptValidationPath
        $svOk     = [bool](Get-V2OptionalProperty -InputObject $sv -Name "success"     -DefaultValue $false)
        $errCount = [int](Get-V2OptionalProperty  -InputObject $sv -Name "error_count"  -DefaultValue 1)
        Add-Check -Name "script-validation" -Passed $svOk -Detail ("$errCount parse error(s)")
    }
    catch {}
}

# ── KPI snapshot ─────────────────────────────────────────────────────────────
$passedCount = @($checks | Where-Object { $_.passed }).Count
$totalCount  = $checks.Count

# ── Verdict ──────────────────────────────────────────────────────────────────
$verdict = if ($blockers.Count -eq 0) {
    "READY"
}
elseif ($openRepairCount -gt 0 -or -not $healthOk) {
    "NOT_READY"
}
else {
    "DEGRADED"
}

# ── Report ────────────────────────────────────────────────────────────────────
$report = [PSCustomObject]@{
    schema_version      = "v2-readiness"
    generated_at        = Get-V2Timestamp
    project             = Split-Path -Leaf $resolvedPath
    verdict             = $verdict
    checks              = @($checks.ToArray())
    blockers            = @($blockers.ToArray())
    summary             = [PSCustomObject]@{
        checks_passed   = $passedCount
        checks_total    = $totalCount
        open_repairs    = $openRepairCount
        pending_tasks   = $pendingCount
        in_progress     = $inProgressCount
        blocked_tasks   = $blockedCount
        open_execution_tasks = $openExecutionCount
        task_metrics_source = $taskMetricsSource
        roadmap_pending = $roadmapHasPending
        mutation_score  = $latestMutationScore
        mutation_run    = $latestMutationRun
        health_status   = $healthStatus
    }
}

Save-V2JsonContent -Path $reportPath -Value $report
Save-V2JsonContent -Path $latestPath -Value $report

$symbol = switch ($verdict) { "READY" { "OK" } "DEGRADED" { "WARN" } default { "FAIL" } }
Write-Host ("[ReadinessReport] {0}: {1}/{2} checks passed | verdict: {3} | repairs: {4}" -f $symbol, $passedCount, $totalCount, $verdict, $openRepairCount)

if ($EmitJson) {
    Write-Output ($report | ConvertTo-Json -Depth 8)
}

if ($verdict -eq "NOT_READY") {
    exit 1
}
