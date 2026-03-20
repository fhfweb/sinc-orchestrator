<#
.SYNOPSIS
    Calibrates scheduler reputation from real tool usage telemetry.
.DESCRIPTION
    Reads ai-orchestrator/state/tool-usage-log.jsonl and updates:
      1) ai-orchestrator/agents/reputation.json (fit-related fields)
      2) ai-orchestrator/state/meta-calibration.json (runtime telemetry snapshot)
    This keeps agent selection grounded in observed success/latency behavior.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator.
.PARAMETER UsageLogPath
    Optional explicit tool usage log path.
.PARAMETER TimeoutThresholdMs
    Duration considered timeout/degraded for calibration.
.PARAMETER EmitJson
    Emits machine-readable summary.
#>
param(
    [string]$ProjectPath = ".",
    [string]$UsageLogPath = "",
    [int]$TimeoutThresholdMs = 120000,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Clamp-V2Score {
    param([double]$Value)
    if ($Value -lt 0.10) { return 0.10 }
    if ($Value -gt 0.99) { return 0.99 }
    return [Math]::Round($Value, 3)
}

function Get-V2CalibrationKey {
    param([object]$Event)

    $agentName = [string](Get-V2OptionalProperty -InputObject $Event -Name "agent_name" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($agentName)) {
        return $agentName
    }
    $roleName = [string](Get-V2OptionalProperty -InputObject $Event -Name "role" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($roleName)) {
        $roleName = [string](Get-V2OptionalProperty -InputObject $Event -Name "step" -DefaultValue "")
    }
    if ($roleName -match '^AgentRuntime(Py)?\[(.+?)\]$') {
        return [string]$matches[2]
    }
    if (-not [string]::IsNullOrWhiteSpace($roleName)) {
        return $roleName
    }
    return "unknown"
}

function ConvertTo-V2CalibrationEvent {
    param(
        [object]$RawEvent,
        [string]$SourceKind
    )

    if (-not $RawEvent) {
        return $null
    }

    $agentName = [string](Get-V2OptionalProperty -InputObject $RawEvent -Name "agent_name" -DefaultValue "")
    $roleName = [string](Get-V2OptionalProperty -InputObject $RawEvent -Name "role" -DefaultValue "")
    $stepName = [string](Get-V2OptionalProperty -InputObject $RawEvent -Name "step" -DefaultValue "")

    if ([string]::IsNullOrWhiteSpace($roleName) -and -not [string]::IsNullOrWhiteSpace($stepName)) {
        $roleName = $stepName
    }

    if ([string]::IsNullOrWhiteSpace($agentName) -and $stepName -match '^AgentRuntime(Py)?\[(.+?)\]$') {
        $agentName = [string]$matches[2]
    }

    $durationMs = [int](Get-V2OptionalProperty -InputObject $RawEvent -Name "duration_ms" -DefaultValue 0)
    if ($durationMs -lt 0) { $durationMs = 0 }

    $status = [string](Get-V2OptionalProperty -InputObject $RawEvent -Name "status" -DefaultValue "")
    $successDefault = $status -notin @("failed", "timeout")
    $successValue = [bool](Get-V2OptionalProperty -InputObject $RawEvent -Name "success" -DefaultValue $successDefault)
    $errorText = [string](Get-V2OptionalProperty -InputObject $RawEvent -Name "error" -DefaultValue "")

    return [PSCustomObject]@{
        agent_name  = $agentName
        role        = $roleName
        success     = $successValue
        duration_ms = $durationMs
        error       = $errorText
        source_kind = $SourceKind
    }
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$resolvedUsageLog = if ([string]::IsNullOrWhiteSpace($UsageLogPath)) {
    Join-Path $orchestratorRoot "state/tool-usage-log.jsonl"
}
else {
    Resolve-V2AbsolutePath -Path $UsageLogPath
}

$reputationPath = Join-Path $orchestratorRoot "agents/reputation.json"
$metaCalibrationPath = Join-Path $orchestratorRoot "state/meta-calibration.json"

$reputation = Get-V2JsonContent -Path $reputationPath
if (-not $reputation) {
    $reputation = [PSCustomObject]@{
        generated_at = Get-V2Timestamp
        source_task_dag = ""
        agents = @()
    }
}
$agents = @(Get-V2OptionalProperty -InputObject $reputation -Name "agents" -DefaultValue @())

$aggregates = @{}
$eventsRead = 0
$sourceKindsUsed = New-Object System.Collections.Generic.List[string]
$sourceLogsUsed = New-Object System.Collections.Generic.List[string]

$candidateLogs = New-Object System.Collections.Generic.List[object]
$candidateLogs.Add([PSCustomObject]@{
        path = $resolvedUsageLog
        kind = "tool-usage-log"
    })
$candidateLogs.Add([PSCustomObject]@{
        path = (Join-Path $orchestratorRoot "state/loop-step-events.jsonl")
        kind = "loop-step-events"
    })

foreach ($candidate in @($candidateLogs.ToArray())) {
    $candidatePath = [string](Get-V2OptionalProperty -InputObject $candidate -Name "path" -DefaultValue "")
    $candidateKind = [string](Get-V2OptionalProperty -InputObject $candidate -Name "kind" -DefaultValue "unknown")
    if ([string]::IsNullOrWhiteSpace($candidatePath) -or -not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
        continue
    }

    $sourceKindsUsed.Add($candidateKind)
    $sourceLogsUsed.Add($candidatePath)

    foreach ($line in @(Get-Content -LiteralPath $candidatePath -ErrorAction SilentlyContinue)) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        $event = $null
        try {
            $event = $line | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            continue
        }
        $normalized = ConvertTo-V2CalibrationEvent -RawEvent $event -SourceKind $candidateKind
        if (-not $normalized) {
            continue
        }

        $eventsRead += 1
        $key = Get-V2CalibrationKey -Event $normalized
        if (-not $aggregates.ContainsKey($key)) {
            $aggregates[$key] = [PSCustomObject]@{
                key = $key
                total = 0
                success = 0
                timeout = 0
                duration_sum = 0
                avg_duration_ms = 0
                success_rate = 0.0
                timeout_rate = 0.0
                updated_at = Get-V2Timestamp
            }
        }

        $entry = $aggregates[$key]
        $entry.total = [int]$entry.total + 1
        $isSuccess = [bool](Get-V2OptionalProperty -InputObject $normalized -Name "success" -DefaultValue $false)
        if ($isSuccess) {
            $entry.success = [int]$entry.success + 1
        }

        $durationMs = [int](Get-V2OptionalProperty -InputObject $normalized -Name "duration_ms" -DefaultValue 0)
        if ($durationMs -lt 0) { $durationMs = 0 }
        $entry.duration_sum = [int]$entry.duration_sum + $durationMs

        $errorText = [string](Get-V2OptionalProperty -InputObject $normalized -Name "error" -DefaultValue "")
        if ($durationMs -ge $TimeoutThresholdMs -or $errorText -match "(?i)timeout|timed out|deadline exceeded") {
            $entry.timeout = [int]$entry.timeout + 1
        }
    }
}

foreach ($entry in @($aggregates.Values)) {
    if ($entry.total -gt 0) {
        $entry.avg_duration_ms = [int][Math]::Round(([double]$entry.duration_sum / [double]$entry.total), 0)
        $entry.success_rate = [Math]::Round(([double]$entry.success / [double]$entry.total), 4)
        $entry.timeout_rate = [Math]::Round(([double]$entry.timeout / [double]$entry.total), 4)
    }
}

$calibratedAgents = New-Object System.Collections.Generic.List[object]
foreach ($agent in $agents) {
    $agentName = [string](Get-V2OptionalProperty -InputObject $agent -Name "agent" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($agentName)) {
        continue
    }

    $metrics = if ($aggregates.ContainsKey($agentName)) { $aggregates[$agentName] } else { $null }
    if (-not $metrics) {
        $metrics = [PSCustomObject]@{
            key = $agentName
            total = 0
            success = 0
            timeout = 0
            duration_sum = 0
            avg_duration_ms = 0
            success_rate = 0.7
            timeout_rate = 0.0
            updated_at = Get-V2Timestamp
        }
    }

    $durationPenalty = 0.0
    if ($metrics.avg_duration_ms -gt 0 -and $TimeoutThresholdMs -gt 0) {
        $ratio = [double]$metrics.avg_duration_ms / [double]$TimeoutThresholdMs
        if ($ratio -gt 1.0) {
            $durationPenalty = [Math]::Min(0.20, ($ratio - 1.0) * 0.10)
        }
    }

    $fitScore = 0.50 + ([double]$metrics.success_rate * 0.50) - ([double]$metrics.timeout_rate * 0.25) - $durationPenalty
    $fitScore = Clamp-V2Score -Value $fitScore

    $delta = (([double]$metrics.success_rate - 0.75) * 0.04) - (([double]$metrics.timeout_rate) * 0.05)
    foreach ($skill in @("backend", "frontend", "db", "arch", "qa", "devops")) {
        $base = [double](Get-V2OptionalProperty -InputObject $agent -Name $skill -DefaultValue 0.70)
        Set-V2DynamicProperty -InputObject $agent -Name $skill -Value (Clamp-V2Score -Value ($base + $delta))
    }

    Set-V2DynamicProperty -InputObject $agent -Name "runtime_success_rate" -Value ([Math]::Round([double]$metrics.success_rate, 4))
    Set-V2DynamicProperty -InputObject $agent -Name "runtime_avg_duration_ms" -Value ([int]$metrics.avg_duration_ms)
    Set-V2DynamicProperty -InputObject $agent -Name "runtime_timeout_rate" -Value ([Math]::Round([double]$metrics.timeout_rate, 4))
    Set-V2DynamicProperty -InputObject $agent -Name "reputation_fit_score" -Value $fitScore
    Set-V2DynamicProperty -InputObject $agent -Name "runtime_samples" -Value ([int]$metrics.total)
    Set-V2DynamicProperty -InputObject $agent -Name "runtime_metrics_updated_at" -Value (Get-V2Timestamp)

    $calibratedAgents.Add([PSCustomObject]@{
        agent = $agentName
        samples = [int]$metrics.total
        success_rate = [Math]::Round([double]$metrics.success_rate, 4)
        avg_duration_ms = [int]$metrics.avg_duration_ms
        timeout_rate = [Math]::Round([double]$metrics.timeout_rate, 4)
        reputation_fit_score = $fitScore
    })
}

Set-V2DynamicProperty -InputObject $reputation -Name "generated_at" -Value (Get-V2Timestamp)
Set-V2DynamicProperty -InputObject $reputation -Name "agents" -Value @($agents)
$sourceKinds = @($sourceKindsUsed.ToArray() | Sort-Object -Unique)
$sourceLogs = @($sourceLogsUsed.ToArray() | Sort-Object -Unique)
Set-V2DynamicProperty -InputObject $reputation -Name "meta_calibration" -Value ([PSCustomObject]@{
    source = if ($sourceKinds.Count -gt 0) { ($sourceKinds -join ",") } else { "none" }
    source_logs = $sourceLogs
    timeout_threshold_ms = $TimeoutThresholdMs
    updated_at = Get-V2Timestamp
})
Save-V2JsonContent -Path $reputationPath -Value $reputation

$meta = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    project_path = Resolve-V2AbsolutePath -Path $projectRoot
    source_log = if ($sourceLogs.Count -gt 0) { ($sourceLogs -join ";") } else { "" }
    source_kind = if ($sourceKinds.Count -gt 0) { ($sourceKinds -join ",") } else { "none" }
    timeout_threshold_ms = $TimeoutThresholdMs
    events_read = $eventsRead
    agents = @($calibratedAgents.ToArray())
}
Save-V2JsonContent -Path $metaCalibrationPath -Value $meta

$result = [PSCustomObject]@{
    success = $true
    project_path = $projectRoot
    usage_log_path = $resolvedUsageLog
    reputation_path = $reputationPath
    meta_calibration_path = $metaCalibrationPath
    events_read = $eventsRead
    calibrated_agents = @($calibratedAgents.ToArray())
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
else {
    Write-Output ("Meta calibration complete. Events: {0} | Agents: {1}" -f $eventsRead, $calibratedAgents.Count)
}
