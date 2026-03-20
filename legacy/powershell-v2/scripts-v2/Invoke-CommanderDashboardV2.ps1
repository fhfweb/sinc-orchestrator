<#
.SYNOPSIS
    Builds a consolidated command dashboard for orchestrator operations.
.DESCRIPTION
    Aggregates runtime health, FinOps GPU/LLM metrics, graph hydration freshness,
    DAG task state and task-state DB sync into a single JSON/Markdown report.
.PARAMETER ProjectPath
    Target project root.
.PARAMETER MaxGraphAgeMinutes
    Marks graph hydration as stale when older than this threshold.
.PARAMETER MaxFinOpsAgeMinutes
    Marks FinOps snapshot as stale when older than this threshold.
.PARAMETER EmitJson
    Emit machine-readable JSON summary.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [int]$MaxGraphAgeMinutes = 180,
    [int]$MaxFinOpsAgeMinutes = 30,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2DashboardJsonOrNull {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-V2DashboardLatestFile {
    param(
        [string]$Directory,
        [string]$Filter
    )
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return $null }
    return @(Get-ChildItem -LiteralPath $Directory -Filter $Filter -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1)[0]
}

function Get-V2DashboardAgeMinutes {
    param([object]$TimestampValue)
    if ($null -eq $TimestampValue) { return -1.0 }
    $raw = [string]$TimestampValue
    if ([string]::IsNullOrWhiteSpace($raw)) { return -1.0 }
    try {
        $parsed = [DateTimeOffset]::Parse($raw)
        return [Math]::Round(((Get-Date).ToUniversalTime() - $parsed.UtcDateTime).TotalMinutes, 2)
    }
    catch {
        return -1.0
    }
}

function Get-V2DashboardEnvDouble {
    param(
        [string]$Name,
        [double]$DefaultValue
    )

    $raw = [string][System.Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }
    $parsed = 0.0
    if ([double]::TryParse($raw.Trim().Replace(",", "."), [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
        return $parsed
    }
    return $DefaultValue
}

function Get-V2DashboardPercentile {
    param(
        [double[]]$Values,
        [double]$Percentile = 95.0
    )

    $numbers = @($Values | Where-Object { $_ -ge 0 } | Sort-Object)
    if ($numbers.Count -eq 0) {
        return 0.0
    }

    $p = [Math]::Min([Math]::Max($Percentile, 0.0), 100.0) / 100.0
    $index = [Math]::Ceiling($numbers.Count * $p) - 1
    if ($index -lt 0) { $index = 0 }
    if ($index -ge $numbers.Count) { $index = $numbers.Count - 1 }
    return [double]$numbers[$index]
}

function Get-V2DashboardLoopCycleMetrics {
    param([string]$LoopStepEventsPath)

    $result = [PSCustomObject]@{
        samples = 0
        cycle_duration_p95_ms = 0.0
        cycle_duration_avg_ms = 0.0
    }
    if (-not (Test-Path -LiteralPath $LoopStepEventsPath -PathType Leaf)) {
        return $result
    }

    $durationsByCycle = @{}
    try {
        $lines = @(Get-Content -LiteralPath $LoopStepEventsPath -ErrorAction Stop | Select-Object -Last 4000)
    }
    catch {
        return $result
    }

    foreach ($line in $lines) {
        $raw = [string]$line
        if ([string]::IsNullOrWhiteSpace($raw)) { continue }
        $entry = $null
        try {
            $entry = $raw | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            continue
        }
        if (-not $entry) { continue }
        $cycle = [string](Get-V2OptionalProperty -InputObject $entry -Name "cycle" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($cycle)) { continue }
        $duration = [double](Get-V2OptionalProperty -InputObject $entry -Name "duration_ms" -DefaultValue 0.0)
        if ($duration -lt 0) { continue }
        if (-not $durationsByCycle.ContainsKey($cycle)) {
            $durationsByCycle[$cycle] = 0.0
        }
        $durationsByCycle[$cycle] = [double]$durationsByCycle[$cycle] + $duration
    }

    $cycleDurations = New-Object System.Collections.Generic.List[double]
    foreach ($value in $durationsByCycle.Values) {
        $cycleDurations.Add([double]$value)
    }
    if ($cycleDurations.Count -eq 0) {
        return $result
    }

    $avg = ($cycleDurations | Measure-Object -Average).Average
    $p95 = Get-V2DashboardPercentile -Values @($cycleDurations.ToArray()) -Percentile 95
    $result.samples = $cycleDurations.Count
    $result.cycle_duration_avg_ms = [Math]::Round([double]$avg, 1)
    $result.cycle_duration_p95_ms = [Math]::Round([double]$p95, 1)
    return $result
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$reportsDir = Join-Path $orchestratorRoot "reports"
$stateDir = Join-Path $orchestratorRoot "state"
$tasksDir = Join-Path $orchestratorRoot "tasks"
Initialize-V2Directory -Path $reportsDir
Initialize-V2Directory -Path $stateDir
Initialize-V2Directory -Path $tasksDir

$healthReportPath = Join-Path $stateDir "health-report.json"
$runtimeReportPath = Join-Path $reportsDir "runtime-observability-report.json"
$graphHydrationPath = Join-Path $reportsDir "graph-hydration-latest.json"
$taskDagPath = Join-Path $tasksDir "task-dag.json"

$finopsFile = Get-V2DashboardLatestFile -Directory $reportsDir -Filter "finops-*.json"
$finopsReport = if ($finopsFile) { Get-V2DashboardJsonOrNull -Path $finopsFile.FullName } else { $null }
$healthReport = Get-V2DashboardJsonOrNull -Path $healthReportPath
$runtimeReport = Get-V2DashboardJsonOrNull -Path $runtimeReportPath
$graphHydration = Get-V2DashboardJsonOrNull -Path $graphHydrationPath
$taskDag = Get-V2DashboardJsonOrNull -Path $taskDagPath

$alerts = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

$gpuSystem = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $finopsReport -Name "system" -DefaultValue ([PSCustomObject]@{})) -Name "gpu_util_pct" -DefaultValue $null
$gpuVramUsed = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $finopsReport -Name "system" -DefaultValue ([PSCustomObject]@{})) -Name "gpu_vram_used_mb" -DefaultValue $null
$gpuVramTotal = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $finopsReport -Name "system" -DefaultValue ([PSCustomObject]@{})) -Name "gpu_vram_total_mb" -DefaultValue $null
$gpuTemp = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $finopsReport -Name "system" -DefaultValue ([PSCustomObject]@{})) -Name "gpu_temperature_c" -DefaultValue $null
$ollamaMix = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $finopsReport -Name "ollama_runtime" -DefaultValue ([PSCustomObject]@{})) -Name "processor_mix" -DefaultValue "")

$llmRuntime = Get-V2OptionalProperty -InputObject $finopsReport -Name "llm_runtime" -DefaultValue ([PSCustomObject]@{})
$embeddingRuntime = Get-V2OptionalProperty -InputObject $finopsReport -Name "embedding_runtime" -DefaultValue ([PSCustomObject]@{})

$healthChecks = @((Get-V2OptionalProperty -InputObject $healthReport -Name "check_results" -DefaultValue @()))
$memorySyncCheck = @($healthChecks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "memory-sync" } | Select-Object -First 1)
$taskStateCheck = @($healthChecks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "task-state-db-sync" } | Select-Object -First 1)

$memorySyncDetails = if ($memorySyncCheck.Count -gt 0) { Get-V2OptionalProperty -InputObject $memorySyncCheck[0] -Name "details" -DefaultValue ([PSCustomObject]@{}) } else { [PSCustomObject]@{} }
$taskStateDetails = if ($taskStateCheck.Count -gt 0) { Get-V2OptionalProperty -InputObject $taskStateCheck[0] -Name "details" -DefaultValue ([PSCustomObject]@{}) } else { [PSCustomObject]@{} }
$taskStateDbScript = Join-Path $PSScriptRoot "task_state_db.py"
if (Test-Path -LiteralPath $taskStateDbScript -PathType Leaf) {
    try {
        $taskStateRaw = & python $taskStateDbScript --project-path $resolvedProjectPath --mode status --emit-json 2>$null | Out-String
        $taskStateParsed = $null
        try {
            $taskStateParsed = ($taskStateRaw | ConvertFrom-Json)
        }
        catch {
            $taskStateParsed = $null
        }
        if ($taskStateParsed -and [bool](Get-V2OptionalProperty -InputObject $taskStateParsed -Name "ok" -DefaultValue $false)) {
            $taskStateDetails = $taskStateParsed
        }
    }
    catch {
    }
}

$graphGeneratedAt = [string](Get-V2OptionalProperty -InputObject $graphHydration -Name "generated_at" -DefaultValue "")
$graphAgeMinutes = Get-V2DashboardAgeMinutes -TimestampValue $graphGeneratedAt
$graphStale = ($graphAgeMinutes -lt 0 -or $graphAgeMinutes -gt $MaxGraphAgeMinutes)
if ($graphStale) {
    $alerts.Add("graph-hydration-stale")
}

$finopsGeneratedAt = [string](Get-V2OptionalProperty -InputObject $finopsReport -Name "generated_at" -DefaultValue "")
$finopsAgeMinutes = Get-V2DashboardAgeMinutes -TimestampValue $finopsGeneratedAt
$finopsStale = ($finopsAgeMinutes -lt 0 -or $finopsAgeMinutes -gt $MaxFinOpsAgeMinutes)
if ($finopsStale) {
    $warnings.Add("finops-snapshot-stale")
}

if ($null -eq $gpuSystem -or $null -eq $gpuVramUsed) {
    $alerts.Add("gpu-metrics-missing")
}

$taskStatusSummary = @{}
if ($taskDag -and ($taskDag.PSObject.Properties.Name -contains "tasks")) {
    foreach ($grp in @($taskDag.tasks | Group-Object status)) {
        $taskStatusSummary[$grp.Name] = $grp.Count
    }
}

$executionBacklogGapDetected = [bool](Get-V2OptionalProperty -InputObject $taskStateDetails -Name "execution_backlog_gap_detected" -DefaultValue $false)
if ($executionBacklogGapDetected) {
    $alerts.Add("execution-backlog-gap-detected")
}

$healthStatus = [string](Get-V2OptionalProperty -InputObject $healthReport -Name "health_status" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($healthStatus)) {
    $healthStatus = "unknown"
}

$healthFailedChecks = @()
if ($healthReport -and ($healthReport.PSObject.Properties.Name -contains "check_results")) {
    foreach ($check in @($healthReport.check_results)) {
        if (-not $check) {
            continue
        }
        $checkStatus = [string](Get-V2OptionalProperty -InputObject $check -Name "status" -DefaultValue "")
        if ($checkStatus -ne "failed") {
            continue
        }
        $checkName = [string](Get-V2OptionalProperty -InputObject $check -Name "name" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($checkName)) {
            $healthFailedChecks += $checkName
        }
    }
}
$healthFailedChecks = @($healthFailedChecks | Sort-Object -Unique)
$healthFailedNonDashboard = @($healthFailedChecks | Where-Object { $_ -ne "commander-dashboard" })

if ($healthStatus -ne "healthy") {
    if (@($healthFailedNonDashboard).Count -gt 0) {
        $alerts.Add("health-report-not-healthy")
    }
    elseif (@($healthFailedChecks).Count -gt 0) {
        $warnings.Add("health-report-degraded-dashboard-only")
    }
    else {
        $alerts.Add("health-report-not-healthy")
    }
}

$tasksAll = @()
if ($taskDag -and ($taskDag.PSObject.Properties.Name -contains "tasks")) {
    $tasksAll = @($taskDag.tasks)
}
$totalTasks = @($tasksAll).Count
$blockedTasks = @($tasksAll | Where-Object {
        $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
        $status.ToLowerInvariant().StartsWith("blocked")
    }).Count
$retryTasks = @($tasksAll | Where-Object {
        [int](Get-V2OptionalProperty -InputObject $_ -Name "retries" -DefaultValue 0) -gt 0
    }).Count
$blockedPct = if ($totalTasks -gt 0) { [Math]::Round(($blockedTasks * 100.0) / $totalTasks, 2) } else { 0.0 }
$retryPct = if ($totalTasks -gt 0) { [Math]::Round(($retryTasks * 100.0) / $totalTasks, 2) } else { 0.0 }

$loopStepEventsPath = Join-Path $stateDir "loop-step-events.jsonl"
$cycleMetrics = Get-V2DashboardLoopCycleMetrics -LoopStepEventsPath $loopStepEventsPath
$cycleP95Ms = [double](Get-V2OptionalProperty -InputObject $cycleMetrics -Name "cycle_duration_p95_ms" -DefaultValue 0.0)
$cycleSamples = [int](Get-V2OptionalProperty -InputObject $cycleMetrics -Name "samples" -DefaultValue 0)
$tokensPerSecond = [double](Get-V2OptionalProperty -InputObject $llmRuntime -Name "tokens_per_second" -DefaultValue 0.0)
$vectorsPerSecond = [double](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "vectors_per_second" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_embedding_vectors_per_second" -DefaultValue 0.0))

$incidentCount = @((Get-V2OptionalProperty -InputObject $healthReport -Name "incidents" -DefaultValue @())).Count
$falseIncidentCount = 0
if ($healthStatus -ne "healthy" -and @($healthFailedNonDashboard).Count -eq 0) {
    $falseIncidentCount = $incidentCount
}
$falseIncidentPct = if ($incidentCount -gt 0) { [Math]::Round(($falseIncidentCount * 100.0) / $incidentCount, 2) } else { 0.0 }

$sloThresholds = [PSCustomObject]@{
    cycle_p95_ms_max = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_CYCLE_P95_MS_MAX" -DefaultValue 180000
    blocked_tasks_pct_max = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_BLOCKED_TASKS_PCT_MAX" -DefaultValue 30
    retry_tasks_pct_max = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_RETRY_TASKS_PCT_MAX" -DefaultValue 20
    tokens_per_second_min = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_TOKENS_PER_SECOND_MIN" -DefaultValue 0.5
    vectors_per_second_min = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_VECTORS_PER_SECOND_MIN" -DefaultValue 5.0
    false_incidents_pct_max = Get-V2DashboardEnvDouble -Name "ORCHESTRATOR_SLO_FALSE_INCIDENTS_PCT_MAX" -DefaultValue 25
}

$sloBreaches = New-Object System.Collections.Generic.List[string]
if ($cycleSamples -gt 0 -and $cycleP95Ms -gt [double]$sloThresholds.cycle_p95_ms_max) {
    $sloBreaches.Add("cycle-p95")
}
if ($blockedPct -gt [double]$sloThresholds.blocked_tasks_pct_max) {
    $sloBreaches.Add("blocked-rate")
}
if ($retryPct -gt [double]$sloThresholds.retry_tasks_pct_max) {
    $sloBreaches.Add("retry-rate")
}
if ($tokensPerSecond -gt 0 -and $tokensPerSecond -lt [double]$sloThresholds.tokens_per_second_min) {
    $sloBreaches.Add("tokens-per-second")
}
if ($vectorsPerSecond -gt 0 -and $vectorsPerSecond -lt [double]$sloThresholds.vectors_per_second_min) {
    $sloBreaches.Add("vectors-per-second")
}
if ($falseIncidentPct -gt [double]$sloThresholds.false_incidents_pct_max) {
    $sloBreaches.Add("false-incidents")
}
if ($sloBreaches.Count -gt 0) {
    foreach ($breach in @($sloBreaches.ToArray())) {
        $alerts.Add(("slo-breach-" + $breach))
    }
}

$slo = [PSCustomObject]@{
    enabled = $true
    thresholds = $sloThresholds
    metrics = [PSCustomObject]@{
        cycle_p95_ms = $cycleP95Ms
        cycle_samples = $cycleSamples
        blocked_tasks_percent = $blockedPct
        retry_tasks_percent = $retryPct
        tokens_per_second = $tokensPerSecond
        vectors_per_second = $vectorsPerSecond
        incident_count = $incidentCount
        false_incident_count = $falseIncidentCount
        false_incidents_percent = $falseIncidentPct
    }
    breaches = @($sloBreaches.ToArray())
}

$graphAst = Get-V2OptionalProperty -InputObject $graphHydration -Name "ast" -DefaultValue ([PSCustomObject]@{})
$graphMemorySync = Get-V2OptionalProperty -InputObject $graphHydration -Name "memory_sync" -DefaultValue ([PSCustomObject]@{})
$graphNeo4j = Get-V2OptionalProperty -InputObject $graphMemorySync -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
$graphQdrant = Get-V2OptionalProperty -InputObject $graphMemorySync -Name "qdrant" -DefaultValue ([PSCustomObject]@{})

$dashboardStatus = if (@($alerts.ToArray()).Count -gt 0) { "degraded" } else { "healthy" }
$timestamp = Get-V2Timestamp

$dashboard = [PSCustomObject]@{
    generated_at = $timestamp
    status = $dashboardStatus
    project = [PSCustomObject]@{
        path = $resolvedProjectPath
        orchestrator_root = $orchestratorRoot
    }
    gpu = [PSCustomObject]@{
        util_pct = $gpuSystem
        vram_used_mb = $gpuVramUsed
        vram_total_mb = $gpuVramTotal
        temperature_c = $gpuTemp
        processor_mix = $ollamaMix
    }
    llm = [PSCustomObject]@{
        enabled = [bool](Get-V2OptionalProperty -InputObject $llmRuntime -Name "enabled" -DefaultValue $false)
        model = [string](Get-V2OptionalProperty -InputObject $llmRuntime -Name "model" -DefaultValue "")
        tokens_per_second = [double](Get-V2OptionalProperty -InputObject $llmRuntime -Name "tokens_per_second" -DefaultValue 0)
        llm_calls = [int](Get-V2OptionalProperty -InputObject $llmRuntime -Name "llm_calls" -DefaultValue 0)
    }
    embeddings = [PSCustomObject]@{
        processor = [string](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "processor" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_embedding_runtime_processor" -DefaultValue "unknown"))
        vectors_per_second = [double](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "vectors_per_second" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_embedding_vectors_per_second" -DefaultValue 0))
        ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "ollama_embeddings" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_ollama_embeddings" -DefaultValue 0))
        non_ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "non_ollama_embeddings" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_non_ollama_embeddings" -DefaultValue 0))
        qdrant_maintenance_ok = [bool](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "qdrant_maintenance_ok" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_maintenance_ok" -DefaultValue $true))
        qdrant_maintenance_alert_count = [int](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "qdrant_maintenance_alert_count" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_maintenance_alert_count" -DefaultValue 0))
        qdrant_fragmentation_percent = [double](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "qdrant_fragmentation_percent" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_fragmentation_percent" -DefaultValue 0))
        qdrant_vector_index_coverage_percent = [double](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "qdrant_vector_index_coverage_percent" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_vector_index_coverage_percent" -DefaultValue 0))
        qdrant_segments_count = [int](Get-V2OptionalProperty -InputObject $embeddingRuntime -Name "qdrant_segments_count" -DefaultValue (Get-V2OptionalProperty -InputObject $memorySyncDetails -Name "qdrant_segments_count" -DefaultValue 0))
    }
    graph = [PSCustomObject]@{
        generated_at = $graphGeneratedAt
        age_minutes = $graphAgeMinutes
        stale = $graphStale
        ast_nodes = [int](Get-V2OptionalProperty -InputObject $graphAst -Name "nodes" -DefaultValue 0)
        neo4j_nodes = [int](Get-V2OptionalProperty -InputObject $graphNeo4j -Name "nodes_synced" -DefaultValue 0)
        qdrant_nodes = [int](Get-V2OptionalProperty -InputObject $graphQdrant -Name "nodes_synced" -DefaultValue 0)
    }
    task_state_db = [PSCustomObject]@{
        db_path = [string](Get-V2OptionalProperty -InputObject $taskStateDetails -Name "db_path" -DefaultValue "")
        tasks_total = [int](Get-V2OptionalProperty -InputObject $taskStateDetails -Name "tasks_total" -DefaultValue 0)
        open_execution_tasks = [int](Get-V2OptionalProperty -InputObject $taskStateDetails -Name "open_execution_tasks" -DefaultValue 0)
        execution_backlog_gap_detected = $executionBacklogGapDetected
    }
    dag = [PSCustomObject]@{
        task_status_counts = $taskStatusSummary
    }
    slo = $slo
    sources = [PSCustomObject]@{
        health_report = $healthReportPath
        runtime_report = $runtimeReportPath
        graph_hydration = $graphHydrationPath
        finops_report = if ($finopsFile) { $finopsFile.FullName } else { "" }
        task_dag = $taskDagPath
    }
    alerts = @($alerts.ToArray())
    warnings = @($warnings.ToArray())
}

$dashboardJsonPath = Join-Path $reportsDir "commander-dashboard.json"
$dashboardMdPath = Join-Path $reportsDir "commander-dashboard.md"
Save-V2JsonContent -Path $dashboardJsonPath -Value $dashboard

$md = @()
$md += "# Commander Dashboard"
$md += ""
$md += "- generated_at: $timestamp"
$md += "- status: $dashboardStatus"
$md += "- gpu: util=$gpuSystem% vram=$gpuVramUsed/$gpuVramTotal MB temp=${gpuTemp}C mix=$ollamaMix"
$md += "- llm: enabled=$($dashboard.llm.enabled) model=$($dashboard.llm.model) tps=$($dashboard.llm.tokens_per_second)"
$md += "- embeddings: processor=$($dashboard.embeddings.processor) vectors/s=$($dashboard.embeddings.vectors_per_second)"
$md += "- qdrant-maintenance: ok=$($dashboard.embeddings.qdrant_maintenance_ok) alerts=$($dashboard.embeddings.qdrant_maintenance_alert_count) frag_pct=$($dashboard.embeddings.qdrant_fragmentation_percent) coverage_pct=$($dashboard.embeddings.qdrant_vector_index_coverage_percent) segments=$($dashboard.embeddings.qdrant_segments_count)"
$md += "- slo: cycle_p95_ms=$($dashboard.slo.metrics.cycle_p95_ms) blocked_pct=$($dashboard.slo.metrics.blocked_tasks_percent)% retry_pct=$($dashboard.slo.metrics.retry_tasks_percent)% false_incidents_pct=$($dashboard.slo.metrics.false_incidents_percent)% breaches=$((@($dashboard.slo.breaches) -join ','))"
$md += "- graph: stale=$graphStale age_min=$graphAgeMinutes ast_nodes=$($dashboard.graph.ast_nodes) neo4j_nodes=$($dashboard.graph.neo4j_nodes) qdrant_nodes=$($dashboard.graph.qdrant_nodes)"
$md += "- task_state_db: tasks_total=$($dashboard.task_state_db.tasks_total) open_execution=$($dashboard.task_state_db.open_execution_tasks) backlog_gap=$executionBacklogGapDetected"
$md += ""
$md += "## Alerts"
if (@($alerts.ToArray()).Count -eq 0) {
    $md += "- none"
}
else {
    foreach ($alert in @($alerts.ToArray())) {
        $md += "- $alert"
    }
}
$md += ""
$md += "## Warnings"
if (@($warnings.ToArray()).Count -eq 0) {
    $md += "- none"
}
else {
    foreach ($warning in @($warnings.ToArray())) {
        $md += "- $warning"
    }
}

Set-Content -LiteralPath $dashboardMdPath -Value ($md -join "`r`n") -Encoding UTF8

$result = [PSCustomObject]@{
    generated_at = $timestamp
    status = $dashboardStatus
    alerts_count = @($alerts.ToArray()).Count
    warnings_count = @($warnings.ToArray()).Count
    report_json = $dashboardJsonPath
    report_md = $dashboardMdPath
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 10
}
else {
    Write-Host ("Commander dashboard generated: {0}" -f $dashboardJsonPath)
    Write-Host ("Status: {0} | Alerts: {1}" -f $dashboardStatus, @($alerts.ToArray()).Count)
}
