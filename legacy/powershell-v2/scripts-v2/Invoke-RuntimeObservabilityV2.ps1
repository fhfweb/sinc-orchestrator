<#
.SYNOPSIS
    Evaluates runtime production telemetry and opens repair tasks when thresholds are breached.
.DESCRIPTION
    Reads telemetry from:
      ai-orchestrator/runtime/telemetry/production-metrics.json
    Uses thresholds from:
      ai-orchestrator/runtime/telemetry/thresholds.json

    Checks latency, error rate, and conversion. When `-AutoRepairTasks` is used,
    creates REPAIR tasks in task-dag.json with dedup cooldown protection.
.PARAMETER ProjectPath
    Target project root.
.PARAMETER AutoRepairTasks
    Automatically create REPAIR tasks on threshold breach.
.PARAMETER EmitJson
    Emit machine-readable JSON summary.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [switch]$AutoRepairTasks,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-JsonOrNull {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-MetricValue {
    param(
        [object]$Metrics,
        [string]$Path
    )

    $cursor = $Metrics
    foreach ($token in @($Path -split "\.")) {
        if ($null -eq $cursor) { return $null }
        $cursor = Get-V2OptionalProperty -InputObject $cursor -Name $token -DefaultValue $null
    }
    return $cursor
}

function Add-RuntimeRepairTask {
    param(
        [string]$TaskDagPath,
        [string]$MetricName,
        [string]$Title,
        [string]$Details,
        [string]$Severity,
        [int]$CooldownSeconds
    )

    $taskDoc = Get-V2JsonContent -Path $TaskDagPath
    if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
        return $false
    }

    $now = Get-Date
    $reasonPrefix = "runtime-observability:{0}" -f $MetricName
    $openStatuses = @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")
    $alreadyOpen = @($taskDoc.tasks | Where-Object {
            $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            ($reason -like "$reasonPrefix*") -and ($status -in $openStatuses)
        }).Count -gt 0
    if ($alreadyOpen) {
        return $false
    }

    $recent = @($taskDoc.tasks | Where-Object {
            $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
            if (-not ($reason -like "$reasonPrefix*")) { return $false }
            $updatedAt = [string](Get-V2OptionalProperty -InputObject $_ -Name "updated_at" -DefaultValue "")
            if ([string]::IsNullOrWhiteSpace($updatedAt)) {
                $updatedAt = [string](Get-V2OptionalProperty -InputObject $_ -Name "created_at" -DefaultValue "")
            }
            if ([string]::IsNullOrWhiteSpace($updatedAt)) { return $false }
            try {
                $ageSeconds = ($now.ToUniversalTime() - ([DateTimeOffset]::Parse($updatedAt).UtcDateTime)).TotalSeconds
                return ($ageSeconds -ge 0 -and $ageSeconds -lt $CooldownSeconds)
            }
            catch {
                return $false
            }
        }).Count -gt 0
    if ($recent) {
        return $false
    }

    $timestamp = Get-V2Timestamp
    $taskId = "REPAIR-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 6))
    $priority = if ($Severity -eq "high") { "P0" } else { "P1" }
    $taskDoc.tasks += [PSCustomObject]@{
        id                = $taskId
        description       = $Title
        priority          = $priority
        dependencies      = @()
        preferred_agent   = "AI DevOps Engineer"
        assigned_agent    = ""
        status            = "pending"
        execution_mode    = "external-agent"
        allow_when_blocked = $true
        reason            = "$reasonPrefix :: $Details"
        source            = "runtime-observability"
        files_affected    = @("ai-orchestrator/runtime/telemetry/production-metrics.json", "ai-orchestrator/state/health-report.json")
        created_at        = $timestamp
        updated_at        = $timestamp
    }
    Set-V2DynamicProperty -InputObject $taskDoc -Name "updated_at" -Value $timestamp
    Save-V2JsonContent -Path $TaskDagPath -Value $taskDoc
    return $true
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$telemetryDirectory = Join-Path $orchestratorRoot "runtime/telemetry"
$telemetryPath = Join-Path $telemetryDirectory "production-metrics.json"
$thresholdsPath = Join-Path $telemetryDirectory "thresholds.json"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$reportsDir = Join-Path $orchestratorRoot "reports"
Initialize-V2Directory -Path $telemetryDirectory
Initialize-V2Directory -Path $reportsDir

$thresholds = Get-JsonOrNull -Path $thresholdsPath
if (-not $thresholds) {
    $thresholds = [PSCustomObject]@{
        latency_p95_ms         = 400
        error_rate_percent     = 2
        conversion_rate_percent = 25
        repair_cooldown_seconds = 900
    }
    Save-V2JsonContent -Path $thresholdsPath -Value $thresholds
}

$baselineGenerated = $false
$metricsDoc = Get-JsonOrNull -Path $telemetryPath
if (-not $metricsDoc) {
    $metricsDoc = [PSCustomObject]@{
        generated_at = Get-V2Timestamp
        source       = "baseline-generated"
        metrics      = [PSCustomObject]@{
            api = [PSCustomObject]@{
                latency_p95_ms      = 0
                error_rate_percent  = 0
            }
            business = [PSCustomObject]@{
                conversion_rate_percent = 0
            }
        }
    }
    Save-V2JsonContent -Path $telemetryPath -Value $metricsDoc
    $baselineGenerated = $true
}

$metricsRoot = Get-V2OptionalProperty -InputObject $metricsDoc -Name "metrics" -DefaultValue $metricsDoc
$latencyP95 = [double](Get-MetricValue -Metrics $metricsRoot -Path "api.latency_p95_ms")
if ($latencyP95 -eq 0 -and $null -eq (Get-MetricValue -Metrics $metricsRoot -Path "api.latency_p95_ms")) {
    $latencyP95 = [double](Get-MetricValue -Metrics $metricsRoot -Path "latency_p95_ms")
}

$errorRate = [double](Get-MetricValue -Metrics $metricsRoot -Path "api.error_rate_percent")
if ($errorRate -eq 0 -and $null -eq (Get-MetricValue -Metrics $metricsRoot -Path "api.error_rate_percent")) {
    $errorRate = [double](Get-MetricValue -Metrics $metricsRoot -Path "error_rate_percent")
}

$conversion = [double](Get-MetricValue -Metrics $metricsRoot -Path "business.conversion_rate_percent")
if ($conversion -eq 0 -and $null -eq (Get-MetricValue -Metrics $metricsRoot -Path "business.conversion_rate_percent")) {
    $conversion = [double](Get-MetricValue -Metrics $metricsRoot -Path "conversion_rate_percent")
}

$checks = New-Object System.Collections.Generic.List[object]
$alerts = New-Object System.Collections.Generic.List[object]
$tasksCreated = New-Object System.Collections.Generic.List[string]

$latencyMax = [double](Get-V2OptionalProperty -InputObject $thresholds -Name "latency_p95_ms" -DefaultValue 400)
$errorMax = [double](Get-V2OptionalProperty -InputObject $thresholds -Name "error_rate_percent" -DefaultValue 2)
$conversionMin = [double](Get-V2OptionalProperty -InputObject $thresholds -Name "conversion_rate_percent" -DefaultValue 25)
$cooldownSeconds = [int](Get-V2OptionalProperty -InputObject $thresholds -Name "repair_cooldown_seconds" -DefaultValue 900)
if ($cooldownSeconds -lt 60) { $cooldownSeconds = 60 }

$checks.Add([PSCustomObject]@{
    metric     = "latency_p95_ms"
    value      = $latencyP95
    threshold  = $latencyMax
    comparator = "<="
    status     = if ($latencyP95 -le $latencyMax -or $latencyP95 -eq 0) { "pass" } else { "fail" }
})
if ($latencyP95 -gt $latencyMax) {
    $alerts.Add([PSCustomObject]@{
        metric   = "latency_p95_ms"
        severity = "high"
        title    = "Runtime latency degraded"
        details  = "P95 latency is $latencyP95 ms, above threshold $latencyMax ms."
    })
}

$checks.Add([PSCustomObject]@{
    metric     = "error_rate_percent"
    value      = $errorRate
    threshold  = $errorMax
    comparator = "<="
    status     = if ($errorRate -le $errorMax -or $errorRate -eq 0) { "pass" } else { "fail" }
})
if ($errorRate -gt $errorMax) {
    $alerts.Add([PSCustomObject]@{
        metric   = "error_rate_percent"
        severity = "high"
        title    = "Runtime error rate degraded"
        details  = "Error rate is $errorRate%, above threshold $errorMax%."
    })
}

$checks.Add([PSCustomObject]@{
    metric     = "conversion_rate_percent"
    value      = $conversion
    threshold  = $conversionMin
    comparator = ">="
    status     = if ($conversion -ge $conversionMin -or $conversion -eq 0) { "pass" } else { "fail" }
})
if ($conversion -gt 0 -and $conversion -lt $conversionMin) {
    $alerts.Add([PSCustomObject]@{
        metric   = "conversion_rate_percent"
        severity = "medium"
        title    = "Business conversion dropped"
        details  = "Conversion rate is $conversion%, below threshold $conversionMin%."
    })
}

if ($AutoRepairTasks -and (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    foreach ($alert in @($alerts.ToArray())) {
        $created = Add-RuntimeRepairTask `
            -TaskDagPath $taskDagPath `
            -MetricName ([string]$alert.metric) `
            -Title ([string]$alert.title) `
            -Details ([string]$alert.details) `
            -Severity ([string]$alert.severity) `
            -CooldownSeconds $cooldownSeconds
        if ($created) {
            $tasksCreated.Add([string]$alert.metric)
        }
    }
}

$status = if ($alerts.Count -gt 0) { "degraded" } else { "healthy" }
$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    status       = $status
    baseline_generated = $baselineGenerated
    telemetry_path = $telemetryPath
    thresholds_path = $thresholdsPath
    checks       = @($checks.ToArray())
    alerts       = @($alerts.ToArray())
    tasks_created = @($tasksCreated.ToArray())
}

$reportJsonPath = Join-Path $reportsDir "runtime-observability-report.json"
Save-V2JsonContent -Path $reportJsonPath -Value $report

$mdLines = New-Object System.Collections.Generic.List[string]
$mdLines.Add("# Runtime Observability Report")
$mdLines.Add("")
$mdLines.Add("- Generated At: $($report.generated_at)")
$mdLines.Add("- Status: $status")
$mdLines.Add("- Baseline Generated: $baselineGenerated")
$mdLines.Add("- Telemetry: $telemetryPath")
$mdLines.Add("- Alerts: $($alerts.Count)")
$mdLines.Add("")
$mdLines.Add("## Checks")
foreach ($check in @($checks.ToArray())) {
    $mdLines.Add("- [$([string]$check.status)] $([string]$check.metric): value=$([string]$check.value) threshold=$([string]$check.comparator)$([string]$check.threshold)")
}
if ($alerts.Count -gt 0) {
    $mdLines.Add("")
    $mdLines.Add("## Alerts")
    foreach ($alert in @($alerts.ToArray())) {
        $mdLines.Add("- [$([string]$alert.severity)] $([string]$alert.title): $([string]$alert.details)")
    }
}
if ($tasksCreated.Count -gt 0) {
    $mdLines.Add("")
    $mdLines.Add("## Repair Tasks Created")
    foreach ($metric in @($tasksCreated.ToArray())) {
        $mdLines.Add("- runtime-observability:$metric")
    }
}
$reportMdPath = Join-Path $reportsDir "runtime-observability-report.md"
[System.IO.File]::WriteAllText($reportMdPath, ($mdLines -join [Environment]::NewLine))

$result = [PSCustomObject]@{
    success        = $true
    status         = $status
    baseline_generated = $baselineGenerated
    alerts         = @($alerts.ToArray())
    checks         = @($checks.ToArray())
    tasks_created  = @($tasksCreated.ToArray())
    report_json    = $reportJsonPath
    report_md      = $reportMdPath
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 12
}
else {
    Write-Output ($result | ConvertTo-Json -Depth 12)
}
