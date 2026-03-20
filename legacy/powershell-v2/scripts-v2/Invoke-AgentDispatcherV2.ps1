<#
.SYNOPSIS
    Dispatches Orchestrator 360 agents from canonical registry.
.DESCRIPTION
    Loads docs/agents/agents-360.registry.json, selects enabled agents by phase,
    executes agent handlers (script or runtime-agent), and writes dispatch reports.
    Runtime-agent execution is optional and disabled by default to avoid duplicate
    executor loops.
.PARAMETER ProjectPath
    Target project root containing ai-orchestrator/.
.PARAMETER Phase
    Dispatch phase: auto, context, architecture, execution, release, all.
.PARAMETER IncludeRuntimeAgents
    If set, executes runtime-agent entries through Run-AgentLoop.ps1.
.PARAMETER MaxRuntimeTasksPerAgent
    Max tasks per runtime agent in one dispatch pass.
.PARAMETER RegistryPath
    Optional explicit path to agents registry JSON.
.PARAMETER AutoRepairTasks
    If set, opens REPAIR task when one or more critical agents fail in dispatch.
.PARAMETER FailOnAgentFailure
    If set, throws when one or more agent handlers fail.
.PARAMETER EmitJson
    Emits machine-readable JSON result.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [ValidateSet("auto", "context", "architecture", "execution", "release", "all")]
    [string]$Phase = "auto",
    [switch]$IncludeRuntimeAgents,
    [int]$MaxRuntimeTasksPerAgent = 1,
    [string]$RegistryPath = "",
    [switch]$AutoRepairTasks,
    [switch]$FailOnAgentFailure,
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

function Resolve-DispatchPhase {
    param(
        [string]$RequestedPhase,
        [object]$ProjectState
    )

    if ($RequestedPhase -ne "auto") {
        return $RequestedPhase
    }
    if ($null -eq $ProjectState) {
        return "execution"
    }

    $phaseApprovals = Get-V2OptionalProperty -InputObject $ProjectState -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
    $contextStatus = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $phaseApprovals -Name "context" -DefaultValue ([PSCustomObject]@{})) -Name "status" -DefaultValue "pending")
    $architectureStatus = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $phaseApprovals -Name "architecture" -DefaultValue ([PSCustomObject]@{})) -Name "status" -DefaultValue "pending")
    $executionStatus = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $phaseApprovals -Name "execution" -DefaultValue ([PSCustomObject]@{})) -Name "status" -DefaultValue "pending")
    $releaseStatus = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $phaseApprovals -Name "release" -DefaultValue ([PSCustomObject]@{})) -Name "status" -DefaultValue "pending")

    if ($contextStatus -ne "approved") { return "context" }
    if ($architectureStatus -ne "approved") { return "architecture" }
    if ($executionStatus -ne "approved") { return "execution" }
    if ($releaseStatus -ne "approved") { return "release" }
    return "execution"
}

function Invoke-RegistryScript {
    param(
        [string]$RepoRoot,
        [string]$ProjectRoot,
        [string]$ScriptRelativePath,
        [string[]]$ArgumentTokens
    )

    $normalizedRelPath = $ScriptRelativePath.Replace("/", "\")
    $scriptPath = if ([System.IO.Path]::IsPathRooted($normalizedRelPath)) {
        $normalizedRelPath
    }
    else {
        Join-Path $RepoRoot $normalizedRelPath
    }
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "script-not-found:$ScriptRelativePath"
    }

    $resolvedTokens = New-Object System.Collections.Generic.List[string]
    foreach ($token in @($ArgumentTokens)) {
        $value = [string]$token
        $value = $value.Replace("{project_path}", $ProjectRoot)
        $resolvedTokens.Add($value)
    }
    $resolvedArgs = $resolvedTokens.ToArray()

    if ($scriptPath.ToLowerInvariant().EndsWith(".py")) {
        $output = & python $scriptPath @resolvedArgs 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            throw ("python-script-failed:{0}" -f $output.Trim())
        }
        return $output
    }

    $namedArgs = @{}
    $positionalArgs = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt $resolvedArgs.Length; $index++) {
        $token = [string]$resolvedArgs[$index]
        if ($token.StartsWith("-") -and $token.Length -gt 1) {
            $paramName = $token.TrimStart('-')
            $nextExists = ($index + 1) -lt $resolvedArgs.Length
            if ($nextExists) {
                $nextToken = [string]$resolvedArgs[$index + 1]
                if (-not $nextToken.StartsWith("-")) {
                    $namedArgs[$paramName] = $nextToken
                    $index++
                    continue
                }
            }
            $namedArgs[$paramName] = $true
            continue
        }
        $positionalArgs.Add($token)
    }

    $positionalArray = $positionalArgs.ToArray()
    if ($positionalArray.Length -gt 0) {
        $outputPs = & $scriptPath @namedArgs @positionalArray 2>&1 | Out-String
    }
    else {
        $outputPs = & $scriptPath @namedArgs 2>&1 | Out-String
    }
    return $outputPs
}

function Add-AgentDispatchRepairTask {
    param(
        [string]$TaskDagPath,
        [int]$CriticalFailureCount,
        [string[]]$FailedAgentIds
    )

    $taskDoc = Get-V2JsonContent -Path $TaskDagPath
    if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
        return $false
    }

    $reasonPrefix = "agent-dispatch-failures"
    $openStatuses = @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-phase-approval", "blocked-lock-conflict")
    $hasOpen = @($taskDoc.tasks | Where-Object {
            $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            ($reason -like "$reasonPrefix*") -and ($status -in $openStatuses)
        }).Count -gt 0
    if ($hasOpen) { return $false }

    $timestamp = Get-V2Timestamp
    $taskId = "REPAIR-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 6))
    $taskDoc.tasks += [PSCustomObject]@{
        id                 = $taskId
        description        = "Repair agent dispatch failures (critical agents=$CriticalFailureCount)"
        priority           = "P0"
        dependencies       = @()
        preferred_agent    = "AI CTO"
        assigned_agent     = ""
        status             = "pending"
        execution_mode     = "external-agent"
        allow_when_blocked = $true
        source             = "agent-dispatch"
        reason             = ("{0} :: failed_agents={1}" -f $reasonPrefix, (@($FailedAgentIds) -join ","))
        files_affected     = @("ai-orchestrator/reports/agent-dispatch-report.json", "ai-orchestrator/state/project-state.json")
        created_at         = $timestamp
        updated_at         = $timestamp
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

$repoRoot = Get-V2RepoRoot
$registryResolvedPath = if (-not [string]::IsNullOrWhiteSpace($RegistryPath)) {
    Resolve-V2AbsolutePath -Path $RegistryPath
}
else {
    Join-Path $repoRoot "docs/agents/agents-360.registry.json"
}
if (-not $registryResolvedPath -or -not (Test-Path -LiteralPath $registryResolvedPath -PathType Leaf)) {
    throw "Agents registry not found: $registryResolvedPath"
}

$registry = Get-JsonOrNull -Path $registryResolvedPath
if (-not $registry -or -not ($registry.PSObject.Properties.Name -contains "agents")) {
    throw "Invalid agents registry: $registryResolvedPath"
}

$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$projectState = Get-JsonOrNull -Path $statePath
if ($projectState) {
    $stateChanged = Initialize-V2PhaseApprovals -ProjectState $projectState -UpdatedBy "agent-dispatcher"
    if ($stateChanged) {
        Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value (Get-V2Timestamp)
        Save-V2JsonContent -Path $statePath -Value $projectState
    }
}

$effectivePhase = Resolve-DispatchPhase -RequestedPhase $Phase -ProjectState $projectState
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$reportJsonPath = Join-Path $orchestratorRoot "reports/agent-dispatch-report.json"
$reportMdPath = Join-Path $orchestratorRoot "reports/agent-dispatch-report.md"
Initialize-V2Directory -Path (Split-Path -Parent $reportJsonPath)

$runAgentLoopPath = Join-Path $repoRoot "scripts/Run-AgentLoop.ps1"
$results = New-Object System.Collections.Generic.List[object]
$selectedAgents = @($registry.agents | Where-Object {
        $enabled = [bool](Get-V2OptionalProperty -InputObject $_ -Name "enabled" -DefaultValue $true)
        if (-not $enabled) { return $false }
        if ($effectivePhase -eq "all") { return $true }
        $phases = @((Get-V2OptionalProperty -InputObject $_ -Name "phases" -DefaultValue @()))
        return ($effectivePhase -in $phases)
    })

foreach ($agent in $selectedAgents) {
    $agentId = [string](Get-V2OptionalProperty -InputObject $agent -Name "id" -DefaultValue "")
    $agentName = [string](Get-V2OptionalProperty -InputObject $agent -Name "name" -DefaultValue $agentId)
    $priority = [string](Get-V2OptionalProperty -InputObject $agent -Name "priority" -DefaultValue "medium")
    $execution = Get-V2OptionalProperty -InputObject $agent -Name "execution" -DefaultValue ([PSCustomObject]@{ mode = "none" })
    $mode = [string](Get-V2OptionalProperty -InputObject $execution -Name "mode" -DefaultValue "none")

    $started = Get-Date
    $status = "skipped"
    $details = ""
    $outputTail = ""
    try {
        switch ($mode) {
            "script" {
                $scriptRel = [string](Get-V2OptionalProperty -InputObject $execution -Name "script" -DefaultValue "")
                $args = @((Get-V2OptionalProperty -InputObject $execution -Name "arguments" -DefaultValue @()))
                $raw = Invoke-RegistryScript -RepoRoot $repoRoot -ProjectRoot $resolvedProjectPath -ScriptRelativePath $scriptRel -ArgumentTokens $args
                $status = "executed"
                $outputTail = (([string]$raw).Trim() -split "(`r`n|`n|`r)" | Select-Object -Last 10) -join [Environment]::NewLine
            }
            "runtime-agent" {
                if (-not $IncludeRuntimeAgents) {
                    $status = "skipped"
                    $details = "runtime-agent-disabled"
                    break
                }
                if (-not (Test-Path -LiteralPath $runAgentLoopPath -PathType Leaf)) {
                    throw "run-agent-loop-missing"
                }
                $runtimeAgent = [string](Get-V2OptionalProperty -InputObject $execution -Name "runtime_agent" -DefaultValue "Codex")
                $rawRuntime = & $runAgentLoopPath -ProjectPath $resolvedProjectPath -AgentName $runtimeAgent -RunOnce -MaxTasksPerCycle $MaxRuntimeTasksPerAgent 2>&1 | Out-String
                $status = "executed"
                $outputTail = (([string]$rawRuntime).Trim() -split "(`r`n|`n|`r)" | Select-Object -Last 10) -join [Environment]::NewLine
            }
            default {
                $status = "skipped"
                $details = "mode-none"
            }
        }
    }
    catch {
        $status = "failed"
        $details = $_.Exception.Message
    }

    $elapsed = [Math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    $results.Add([PSCustomObject]@{
            id               = $agentId
            name             = $agentName
            priority         = $priority
            phase            = $effectivePhase
            mode             = $mode
            status           = $status
            details          = $details
            duration_seconds = $elapsed
            output_tail      = $outputTail
        })
}

$failed = @($results | Where-Object { $_.status -eq "failed" })
$executed = @($results | Where-Object { $_.status -eq "executed" })
$skipped = @($results | Where-Object { $_.status -eq "skipped" })
$criticalFailures = @($failed | Where-Object { ([string]$_.priority) -eq "critical" })

if ($AutoRepairTasks -and $criticalFailures.Count -gt 0 -and (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    [void](Add-AgentDispatchRepairTask -TaskDagPath $taskDagPath -CriticalFailureCount $criticalFailures.Count -FailedAgentIds @($criticalFailures | ForEach-Object { [string]$_.id }))
}

$report = [PSCustomObject]@{
    generated_at  = Get-V2Timestamp
    registry_path = $registryResolvedPath
    project_path  = $resolvedProjectPath
    phase         = $effectivePhase
    totals        = [PSCustomObject]@{
        selected        = $selectedAgents.Count
        executed        = $executed.Count
        skipped         = $skipped.Count
        failed          = $failed.Count
        critical_failed = $criticalFailures.Count
    }
    results       = @($results.ToArray())
}
Save-V2JsonContent -Path $reportJsonPath -Value $report

$mdLines = New-Object System.Collections.Generic.List[string]
$mdLines.Add("# Agent Dispatch Report")
$mdLines.Add("")
$mdLines.Add("- Generated At: $($report.generated_at)")
$mdLines.Add("- Phase: $effectivePhase")
$mdLines.Add("- Selected: $($selectedAgents.Count)")
$mdLines.Add("- Executed: $($executed.Count)")
$mdLines.Add("- Skipped: $($skipped.Count)")
$mdLines.Add("- Failed: $($failed.Count)")
$mdLines.Add("- Critical Failed: $($criticalFailures.Count)")
$mdLines.Add("")
$mdLines.Add("## Agent Results")
foreach ($row in @($results.ToArray())) {
    $mdLines.Add("- [$($row.status)] $($row.id) $($row.name) (mode=$($row.mode), $($row.duration_seconds)s) $($row.details)")
}
[System.IO.File]::WriteAllText($reportMdPath, ($mdLines -join [Environment]::NewLine))

if ($projectState) {
    $agents360 = Get-V2OptionalProperty -InputObject $projectState -Name "agents_360" -DefaultValue ([PSCustomObject]@{})
    Set-V2DynamicProperty -InputObject $agents360 -Name "last_dispatch" -Value ([PSCustomObject]@{
            at          = Get-V2Timestamp
            phase       = $effectivePhase
            report_json = $reportJsonPath
            report_md   = $reportMdPath
            selected    = $selectedAgents.Count
            executed    = $executed.Count
            failed      = $failed.Count
        })
    Set-V2DynamicProperty -InputObject $projectState -Name "agents_360" -Value $agents360
    Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $statePath -Value $projectState
}

$result = [PSCustomObject]@{
    success         = ($failed.Count -eq 0)
    phase           = $effectivePhase
    report_json     = $reportJsonPath
    report_md       = $reportMdPath
    selected        = $selectedAgents.Count
    executed        = $executed.Count
    skipped         = $skipped.Count
    failed          = $failed.Count
    critical_failed = $criticalFailures.Count
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 12
}
else {
    Write-Output ($result | ConvertTo-Json -Depth 12)
}

if ($FailOnAgentFailure -and $failed.Count -gt 0) {
    throw ("agent-dispatch-failed: {0}" -f ((@($failed | ForEach-Object { [string]$_.id }) -join ", ")))
}
