<#
.SYNOPSIS
    Executes the 360 decision engine with evidence scoring and automatic remediation tasks.
.DESCRIPTION
    Converts orchestration artifacts into actionable governance signals:
      - Architecture score
      - Quality score
      - Operations score
      - Overall readiness score

    Writes JSON/Markdown report and optionally opens REPAIR tasks in task-dag.json.
.PARAMETER ProjectPath
    Target project root.
.PARAMETER AutoRepairTasks
    Create remediation tasks when score thresholds are not met.
.PARAMETER EmitJson
    Emit machine-readable output.
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
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Test-ArtifactSignal {
    param(
        [string]$Path,
        [int]$MinLines = 8,
        [string[]]$PlaceholderPatterns = @("TODO", "REVIEW_REQUIRED", "This file should always reflect")
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [PSCustomObject]@{
            exists = $false
            lines  = 0
            quality = "missing"
            score = 0
            note = "missing"
        }
    }

    $raw = Get-Content -LiteralPath $Path -Raw
    $lineCount = @(($raw -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
    $placeholderHit = $false
    foreach ($pattern in @($PlaceholderPatterns)) {
        if (-not [string]::IsNullOrWhiteSpace($pattern) -and $raw -match [regex]::Escape($pattern)) {
            $placeholderHit = $true
            break
        }
    }

    if ($lineCount -lt $MinLines) {
        return [PSCustomObject]@{
            exists = $true
            lines  = $lineCount
            quality = "too-short"
            score = 40
            note = "insufficient-content"
        }
    }

    if ($placeholderHit) {
        return [PSCustomObject]@{
            exists = $true
            lines  = $lineCount
            quality = "placeholder"
            score = 55
            note = "contains-placeholder-pattern"
        }
    }

    return [PSCustomObject]@{
        exists = $true
        lines  = $lineCount
        quality = "good"
        score = 100
        note = "ok"
    }
}

function Add-ScoreRepairTask {
    param(
        [string]$TaskDagPath,
        [string]$Category,
        [int]$Score,
        [string]$Details
    )

    $taskDoc = Get-V2JsonContent -Path $TaskDagPath
    if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
        return $false
    }

    $reasonPrefix = "orchestrator-360-score:{0}" -f $Category
    $openStatuses = @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-phase-approval", "blocked-lock-conflict")
    $hasOpen = @($taskDoc.tasks | Where-Object {
            $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            ($reason -like "$reasonPrefix*") -and ($status -in $openStatuses)
        }).Count -gt 0
    if ($hasOpen) { return $false }

    $timestamp = Get-V2Timestamp
    $taskId = "REPAIR-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 6))
    $priority = if ($Category -eq "overall" -or $Score -lt 60) { "P0" } else { "P1" }

    $taskDoc.tasks += [PSCustomObject]@{
        id                 = $taskId
        description        = "Raise orchestrator-360 score for '$Category' (current: $Score)"
        priority           = $priority
        dependencies       = @()
        preferred_agent    = "AI Architect"
        assigned_agent     = ""
        status             = "pending"
        execution_mode     = "external-agent"
        allow_when_blocked = $true
        source             = "orchestrator-360-decision-engine"
        reason             = "$reasonPrefix :: $Details"
        files_affected     = @(
            "ai-orchestrator/context/business-context.json",
            "ai-orchestrator/documentation/adr/ADR-0001-context-driven-architecture.md",
            "ai-orchestrator/reports/orchestrator-360-decision-report.json"
        )
        created_at         = $timestamp
        updated_at         = $timestamp
    }
    Set-V2DynamicProperty -InputObject $taskDoc -Name "updated_at" -Value $timestamp
    Save-V2JsonContent -Path $TaskDagPath -Value $taskDoc
    return $true
}

function Resolve-ScoreRepairTasks {
    param(
        [string]$TaskDagPath,
        [string]$Category,
        [string]$CompletionNote
    )

    $taskDoc = Get-V2JsonContent -Path $TaskDagPath
    if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
        return 0
    }

    $reasonPrefix = "orchestrator-360-score:{0}" -f $Category
    $openStatuses = @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-phase-approval", "blocked-lock-conflict")
    $resolvedCount = 0
    $timestamp = Get-V2Timestamp

    foreach ($task in @($taskDoc.tasks)) {
        $reason = [string](Get-V2OptionalProperty -InputObject $task -Name "reason" -DefaultValue "")
        $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
        if (-not ($reason -like "$reasonPrefix*")) { continue }
        if ($status -notin $openStatuses) { continue }

        Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
        Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
        Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
        Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value $CompletionNote
        Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
        $resolvedCount += 1
    }

    if ($resolvedCount -gt 0) {
        Set-V2DynamicProperty -InputObject $taskDoc -Name "updated_at" -Value $timestamp
        Save-V2JsonContent -Path $TaskDagPath -Value $taskDoc
    }

    return $resolvedCount
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$artifacts = [ordered]@{
    business_context_json = Join-Path $orchestratorRoot "context/business-context.json"
    business_context_md = Join-Path $orchestratorRoot "context/business-context.md"
    adr = Join-Path $orchestratorRoot "documentation/adr/ADR-0001-context-driven-architecture.md"
    interfaces = Join-Path $orchestratorRoot "documentation/interfaces/contracts.md"
    code_review = Join-Path $orchestratorRoot "reports/code-review-checklist.md"
    product_validation = Join-Path $orchestratorRoot "reports/product-validation-checklist.md"
    user_simulation = Join-Path $orchestratorRoot "reports/user-simulation-plan.md"
    observability_plan = Join-Path $orchestratorRoot "reports/production-observability-plan.md"
    runtime_observability_report = Join-Path $orchestratorRoot "reports/runtime-observability-report.json"
    release_config = Join-Path $orchestratorRoot "release/release-config.json"
    architecture_doc = Join-Path $orchestratorRoot "documentation/architecture.md"
}

$signals = [ordered]@{}
foreach ($key in @($artifacts.Keys)) {
    $signals[$key] = Test-ArtifactSignal -Path ([string]$artifacts[$key])
}

$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$taskDoc = Get-JsonOrNull -Path $taskDagPath
$taskStats = [PSCustomObject]@{ total = 0; done = 0; open = 0 }
if ($taskDoc -and ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
    $tasks = @($taskDoc.tasks)
    $taskStats.total = $tasks.Count
    $taskStats.done = @($tasks | Where-Object {
            [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -in @("done", "completed", "skipped")
        }).Count
    $taskStats.open = @($tasks | Where-Object {
            [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-phase-approval", "blocked-lock-conflict")
        }).Count
}

$healthPath = Join-Path $orchestratorRoot "state/health-report.json"
$health = Get-JsonOrNull -Path $healthPath
$healthStatus = if ($health) { [string](Get-V2OptionalProperty -InputObject $health -Name "health_status" -DefaultValue "unknown") } else { "missing" }
$healthScore = if ($healthStatus -eq "healthy") { 100 } elseif ($healthStatus -eq "degraded") { 50 } else { 30 }

$architectureScore = [int][Math]::Round((($signals.business_context_json.score + $signals.business_context_md.score + $signals.adr.score + $signals.interfaces.score + $signals.architecture_doc.score) / 5.0), 0)
$qualityScore = [int][Math]::Round((($signals.code_review.score + $signals.product_validation.score + $signals.user_simulation.score + $healthScore) / 4.0), 0)
$operationsScore = [int][Math]::Round((($signals.observability_plan.score + $signals.runtime_observability_report.score + $signals.release_config.score + $healthScore) / 4.0), 0)
$executionThroughputScore = if ($taskStats.total -le 0) { 40 } else { [int][Math]::Round(([double]$taskStats.done / [double]$taskStats.total) * 100, 0) }

$overallScore = [int][Math]::Round((($architectureScore + $qualityScore + $operationsScore + $executionThroughputScore) / 4.0), 0)
$status = if ($overallScore -ge 80 -and $architectureScore -ge 70 -and $qualityScore -ge 70 -and $operationsScore -ge 70) { "healthy" } else { "degraded" }

$repairCreated = New-Object System.Collections.Generic.List[string]
$repairResolved = New-Object System.Collections.Generic.List[string]
if ($AutoRepairTasks -and (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    foreach ($bucket in @(
            [PSCustomObject]@{ name = "architecture"; score = $architectureScore; threshold = 70; details = "business context / ADR / contracts not fully mature." },
            [PSCustomObject]@{ name = "quality"; score = $qualityScore; threshold = 70; details = "review/product/simulation baseline not fully validated." },
            [PSCustomObject]@{ name = "operations"; score = $operationsScore; threshold = 70; details = "observability/release path not yet production-grade." },
            [PSCustomObject]@{ name = "overall"; score = $overallScore; threshold = 80; details = "overall readiness below orchestrator target." }
        )) {
        if ([int]$bucket.score -lt [int]$bucket.threshold) {
            $created = Add-ScoreRepairTask -TaskDagPath $taskDagPath -Category ([string]$bucket.name) -Score ([int]$bucket.score) -Details ([string]$bucket.details)
            if ($created) {
                $repairCreated.Add([string]$bucket.name)
            }
        }
        else {
            $resolvedCount = Resolve-ScoreRepairTasks `
                -TaskDagPath $taskDagPath `
                -Category ([string]$bucket.name) `
                -CompletionNote ("auto-resolved: {0} score recovered to {1}" -f [string]$bucket.name, [int]$bucket.score)
            if ($resolvedCount -gt 0) {
                $repairResolved.Add(("{0}:{1}" -f [string]$bucket.name, [int]$resolvedCount))
            }
        }
    }
}

$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    status       = $status
    scores       = [PSCustomObject]@{
        overall      = $overallScore
        architecture = $architectureScore
        quality      = $qualityScore
        operations   = $operationsScore
        execution    = $executionThroughputScore
    }
    health_status = $healthStatus
    task_stats    = $taskStats
    artifacts     = [PSCustomObject]$signals
    repairs_created = @($repairCreated.ToArray())
    repairs_resolved = @($repairResolved.ToArray())
}

$reportJsonPath = Join-Path $orchestratorRoot "reports/orchestrator-360-decision-report.json"
Save-V2JsonContent -Path $reportJsonPath -Value $report

$md = New-Object System.Collections.Generic.List[string]
$md.Add("# Orchestrator 360 Decision Report")
$md.Add("")
$md.Add("- Generated At: $($report.generated_at)")
$md.Add("- Status: $status")
$md.Add("- Overall Score: $overallScore")
$md.Add("")
$md.Add("## Score Breakdown")
$md.Add("- Architecture: $architectureScore")
$md.Add("- Quality: $qualityScore")
$md.Add("- Operations: $operationsScore")
$md.Add("- Execution Throughput: $executionThroughputScore")
$md.Add("")
$md.Add("## Artifact Signals")
foreach ($key in @($signals.Keys)) {
    $sig = $signals[$key]
    $md.Add("- ${key}: quality=$([string]$sig.quality), lines=$([string]$sig.lines), score=$([string]$sig.score)")
}
if ($repairCreated.Count -gt 0) {
    $md.Add("")
    $md.Add("## Repair Tasks Created")
    foreach ($entry in @($repairCreated)) {
        $md.Add("- $entry")
    }
}
if ($repairResolved.Count -gt 0) {
    $md.Add("")
    $md.Add("## Repair Tasks Resolved")
    foreach ($entry in @($repairResolved)) {
        $md.Add("- $entry")
    }
}
$reportMdPath = Join-Path $orchestratorRoot "reports/orchestrator-360-decision-report.md"
[System.IO.File]::WriteAllText($reportMdPath, ($md -join [Environment]::NewLine))

$result = [PSCustomObject]@{
    success       = $true
    status        = $status
    overall_score = $overallScore
    report_json   = $reportJsonPath
    report_md     = $reportMdPath
    repairs_created = @($repairCreated.ToArray())
    repairs_resolved = @($repairResolved.ToArray())
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 12
}
else {
    Write-Output ($result | ConvertTo-Json -Depth 12)
}
