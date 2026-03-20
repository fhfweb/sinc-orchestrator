<#
.SYNOPSIS
    Validates Orchestrator 360 required artifacts by phase and emits READY/NOT READY.
.DESCRIPTION
    Loads docs/agents/agents-360.registry.json, resolves effective phase, validates
    required artifacts for enabled agents in scope, writes JSON/Markdown reports,
    updates project-state agents_360.last_validation, and can auto-open REPAIR tasks.
.PARAMETER ProjectPath
    Target project root containing ai-orchestrator/.
.PARAMETER Phase
    Validation phase: auto, context, architecture, execution, release, all.
.PARAMETER RegistryPath
    Optional explicit path to agents registry JSON.
.PARAMETER AutoRepairTasks
    If set, opens REPAIR task when one or more required artifacts are missing.
.PARAMETER FailOnNotReady
    If set, throws when verdict is NOT READY.
.PARAMETER EmitJson
    Emits machine-readable JSON result.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [ValidateSet("auto", "context", "architecture", "execution", "release", "all")]
    [string]$Phase = "auto",
    [string]$RegistryPath = "",
    [switch]$AutoRepairTasks,
    [switch]$FailOnNotReady,
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

function Resolve-ValidationPhase {
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
    return "release"
}

function Get-ValidationScope {
    param([string]$EffectivePhase)
    switch ($EffectivePhase) {
        "all" { return @("context", "architecture", "execution", "release") }
        "context" { return @("context") }
        "architecture" { return @("context", "architecture") }
        "execution" { return @("context", "architecture", "execution") }
        "release" { return @("context", "architecture", "execution", "release") }
        default { return @("context", "architecture", "execution") }
    }
}

function Add-AgentValidationRepairTask {
    param(
        [string]$TaskDagPath,
        [int]$FailureCount,
        [string[]]$FailedAgentIds
    )

    $taskDoc = Get-V2JsonContent -Path $TaskDagPath
    if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
        return $false
    }

    $reasonPrefix = "agent-artifact-validation-failures"
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
        description        = "Repair missing agent artifacts (failed agents=$FailureCount)"
        priority           = "P0"
        dependencies       = @()
        preferred_agent    = "AI CTO"
        assigned_agent     = ""
        status             = "pending"
        execution_mode     = "external-agent"
        allow_when_blocked = $true
        source             = "agent-artifact-validation"
        reason             = ("{0} :: failed_agents={1}" -f $reasonPrefix, (@($FailedAgentIds) -join ","))
        files_affected     = @("ai-orchestrator/reports/agent-artifact-validation-report.json", "ai-orchestrator/state/project-state.json")
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
    $stateChanged = Initialize-V2PhaseApprovals -ProjectState $projectState -UpdatedBy "agent-artifact-validation"
    if ($stateChanged) {
        Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value (Get-V2Timestamp)
        Save-V2JsonContent -Path $statePath -Value $projectState
    }
}

$effectivePhase = Resolve-ValidationPhase -RequestedPhase $Phase -ProjectState $projectState
$scope = @(Get-ValidationScope -EffectivePhase $effectivePhase)
$reportJsonPath = Join-Path $orchestratorRoot "reports/agent-artifact-validation-report.json"
$reportMdPath = Join-Path $orchestratorRoot "reports/agent-artifact-validation-report.md"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
Initialize-V2Directory -Path (Split-Path -Parent $reportJsonPath)

$selectedAgents = @($registry.agents | Where-Object {
    $enabled = [bool](Get-V2OptionalProperty -InputObject $_ -Name "enabled" -DefaultValue $true)
    if (-not $enabled) { return $false }
    if ($effectivePhase -eq "all") { return $true }
    $phases = @((Get-V2OptionalProperty -InputObject $_ -Name "phases" -DefaultValue @()))
    return @($phases | Where-Object { $_ -in $scope }).Count -gt 0
})

$results = New-Object System.Collections.Generic.List[object]
foreach ($agent in $selectedAgents) {
    $agentId = [string](Get-V2OptionalProperty -InputObject $agent -Name "id" -DefaultValue "")
    $agentName = [string](Get-V2OptionalProperty -InputObject $agent -Name "name" -DefaultValue $agentId)
    $priority = [string](Get-V2OptionalProperty -InputObject $agent -Name "priority" -DefaultValue "medium")
    $phases = @((Get-V2OptionalProperty -InputObject $agent -Name "phases" -DefaultValue @()))
    $requiredArtifacts = @((Get-V2OptionalProperty -InputObject $agent -Name "required_artifacts" -DefaultValue @()))

    $presentArtifacts = New-Object System.Collections.Generic.List[string]
    $missingArtifacts = New-Object System.Collections.Generic.List[string]
    foreach ($artifact in $requiredArtifacts) {
        $artifactText = [string]$artifact
        if ([string]::IsNullOrWhiteSpace($artifactText)) { continue }
        $artifactPath = if ([System.IO.Path]::IsPathRooted($artifactText)) { $artifactText } else { Join-Path $resolvedProjectPath $artifactText }
        if (Test-Path -LiteralPath $artifactPath) {
            $presentArtifacts.Add($artifactText)
        }
        else {
            $missingArtifacts.Add($artifactText)
        }
    }

    $status = if ($requiredArtifacts.Count -eq 0) {
        "pass"
    }
    elseif ($missingArtifacts.Count -eq 0) {
        "pass"
    }
    else {
        "fail"
    }

    $details = if ($requiredArtifacts.Count -eq 0) {
        "no-required-artifacts"
    }
    elseif ($missingArtifacts.Count -eq 0) {
        "all-required-artifacts-present"
    }
    else {
        ("missing={0}" -f ($missingArtifacts -join ","))
    }

    $results.Add([PSCustomObject]@{
        id                = $agentId
        name              = $agentName
        priority          = $priority
        phases            = @($phases)
        status            = $status
        required_count    = $requiredArtifacts.Count
        present_count     = $presentArtifacts.Count
        missing_count     = $missingArtifacts.Count
        required_artifacts = @($requiredArtifacts)
        present_artifacts = @($presentArtifacts.ToArray())
        missing_artifacts = @($missingArtifacts.ToArray())
        details           = $details
    })
}

$failed = @($results | Where-Object { $_.status -eq "fail" })
$passed = @($results | Where-Object { $_.status -eq "pass" })
$criticalFailed = @($failed | Where-Object { ([string]$_.priority) -eq "critical" })
$verdict = if ($failed.Count -eq 0) { "READY" } else { "NOT READY" }

if ($AutoRepairTasks -and $failed.Count -gt 0 -and (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    [void](Add-AgentValidationRepairTask -TaskDagPath $taskDagPath -FailureCount $failed.Count -FailedAgentIds @($failed | ForEach-Object { [string]$_.id }))
}

$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    registry_path = $registryResolvedPath
    project_path = $resolvedProjectPath
    phase = $effectivePhase
    scope = @($scope)
    verdict = $verdict
    totals = [PSCustomObject]@{
        selected = $selectedAgents.Count
        pass = $passed.Count
        fail = $failed.Count
        critical_fail = $criticalFailed.Count
    }
    results = @($results.ToArray())
}
Save-V2JsonContent -Path $reportJsonPath -Value $report

$mdLines = New-Object System.Collections.Generic.List[string]
$mdLines.Add("# Agent Artifact Validation Report")
$mdLines.Add("")
$mdLines.Add("- Generated At: $($report.generated_at)")
$mdLines.Add("- Phase: $effectivePhase")
$mdLines.Add("- Scope: $(@($scope) -join ', ')")
$mdLines.Add("- Verdict: $verdict")
$mdLines.Add("- Selected: $($selectedAgents.Count)")
$mdLines.Add("- Pass: $($passed.Count)")
$mdLines.Add("- Fail: $($failed.Count)")
$mdLines.Add("- Critical Fail: $($criticalFailed.Count)")
$mdLines.Add("")
$mdLines.Add("## Agent Results")
foreach ($row in @($results.ToArray())) {
    $mdLines.Add("- [$($row.status)] $($row.id) $($row.name) required=$($row.required_count) missing=$($row.missing_count) $($row.details)")
}
[System.IO.File]::WriteAllText($reportMdPath, ($mdLines -join [Environment]::NewLine))

if ($projectState) {
    $agents360 = Get-V2OptionalProperty -InputObject $projectState -Name "agents_360" -DefaultValue ([PSCustomObject]@{})
    Set-V2DynamicProperty -InputObject $agents360 -Name "last_validation" -Value ([PSCustomObject]@{
        at = Get-V2Timestamp
        phase = $effectivePhase
        scope = @($scope)
        verdict = $verdict
        report_json = $reportJsonPath
        report_md = $reportMdPath
        selected = $selectedAgents.Count
        pass = $passed.Count
        fail = $failed.Count
        critical_fail = $criticalFailed.Count
    })
    Set-V2DynamicProperty -InputObject $projectState -Name "agents_360" -Value $agents360
    Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $statePath -Value $projectState
}

$result = [PSCustomObject]@{
    success = ($failed.Count -eq 0)
    verdict = $verdict
    phase = $effectivePhase
    scope = @($scope)
    report_json = $reportJsonPath
    report_md = $reportMdPath
    selected = $selectedAgents.Count
    pass = $passed.Count
    fail = $failed.Count
    critical_fail = $criticalFailed.Count
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 12
}
else {
    Write-Output ($result | ConvertTo-Json -Depth 12)
}

if ($FailOnNotReady -and $verdict -ne "READY") {
    throw ("agent-artifact-validation-not-ready: failed_agents={0}" -f ((@($failed | ForEach-Object { [string]$_.id }) -join ",")))
}
