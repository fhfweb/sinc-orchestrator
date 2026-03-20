<#
.SYNOPSIS
    External Agent Bridge handler for V2 task execution.
.DESCRIPTION
    Creates dispatch artifacts for external-agent tasks and checks for completion
    artifacts produced by external runtimes (Claude/Antigravity/etc).
    Contract:
      - success=true, deferred=true  => dispatch created/refreshed, wait for external completion
      - success=true, deferred=false => completion artifact found, caller may finalize task
      - success=false                => bridge error (caller may fallback to llm-native once)
#>
param(
    [string]$ProjectPath = ".",
    [string]$TaskId,
    [string]$AgentName = "Codex"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Set-V2BridgeObjectProperty {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$Value
    )
    if ($InputObject.PSObject.Properties.Name -contains $Name) {
        $InputObject.$Name = $Value
    }
    else {
        Add-Member -InputObject $InputObject -MemberType NoteProperty -Name $Name -Value $Value -Force
    }
}

function Resolve-V2BridgeLatestCompletion {
    param(
        [string]$CompletionsDir,
        [string]$TaskId
    )

    if (-not (Test-Path -LiteralPath $CompletionsDir -PathType Container)) {
        return $null
    }

    $patternA = "$TaskId-*.json"
    $patternB = "$TaskId*.json"
    $files = @(
        @(
            Get-ChildItem -LiteralPath $CompletionsDir -Filter $patternA -File -ErrorAction SilentlyContinue
            Get-ChildItem -LiteralPath $CompletionsDir -Filter $patternB -File -ErrorAction SilentlyContinue
        ) | Sort-Object LastWriteTimeUtc -Descending
    )

    if ($files.Count -eq 0) {
        return $null
    }
    return $files[0]
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    throw "task-dag.json not found: $taskDagPath"
}

$taskDoc = Get-V2JsonContent -Path $taskDagPath
if (-not $taskDoc -or -not ($taskDoc.PSObject.Properties.Name -contains "tasks")) {
    throw "Invalid task-dag.json format (missing tasks array)."
}

$task = @($taskDoc.tasks | Where-Object {
        [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $TaskId
    } | Select-Object -First 1)
if ($task.Count -eq 0) {
    throw "Task not found in DAG: $TaskId"
}

$taskObject = $task[0]
$completionsDir = Join-Path $orchestratorRoot "tasks/completions"
$completionFile = Resolve-V2BridgeLatestCompletion -CompletionsDir $completionsDir -TaskId $TaskId
if ($completionFile) {
    $completionRelative = $completionFile.FullName.Substring($resolvedProjectPath.Length).TrimStart('\', '/') -replace "\\", "/"
    $result = [PSCustomObject]@{
        success = $true
        deferred = $false
        reason = "external-completion-detected"
        output = "external-completion-detected:$completionRelative"
        completion_path = $completionRelative
    }
    Write-Output ($result | ConvertTo-Json -Depth 6 -Compress)
    exit 0
}

$dispatchDir = Join-Path $orchestratorRoot "state/external-agent-bridge/dispatches"
Initialize-V2Directory -Path $dispatchDir
$dispatchPath = Join-Path $dispatchDir ("{0}.json" -f $TaskId)

$dispatchCooldownSeconds = 120
try {
    $envCooldown = [int](Get-V2EnvValueOrDefault -Name "ORCHESTRATOR_EXTERNAL_BRIDGE_DISPATCH_COOLDOWN_SECONDS" -DefaultValue "120")
    if ($envCooldown -gt 0) {
        $dispatchCooldownSeconds = $envCooldown
    }
}
catch { }

$nowUtc = [DateTime]::UtcNow
$canWriteDispatch = $true
if (Test-Path -LiteralPath $dispatchPath -PathType Leaf) {
    try {
        $existingDispatch = Get-V2JsonContent -Path $dispatchPath
        $lastDispatchAt = [string](Get-V2OptionalProperty -InputObject $existingDispatch -Name "requested_at" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($lastDispatchAt)) {
            $lastDispatchUtc = [DateTimeOffset]::Parse($lastDispatchAt).UtcDateTime
            $ageSeconds = ($nowUtc - $lastDispatchUtc).TotalSeconds
            if ($ageSeconds -lt [double]$dispatchCooldownSeconds) {
                $canWriteDispatch = $false
            }
        }
    }
    catch { }
}

$taskTitle = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "title" -DefaultValue $TaskId)
$taskDescription = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "description" -DefaultValue "")
$taskReason = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "reason" -DefaultValue "")
$taskPriority = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "priority" -DefaultValue "P3")
$taskFiles = @((Get-V2OptionalProperty -InputObject $taskObject -Name "files_affected" -DefaultValue @()))
$taskDependencies = @((Get-V2OptionalProperty -InputObject $taskObject -Name "dependencies" -DefaultValue @()))
$taskPreflight = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "preflight_path" -DefaultValue "")
$taskPreferredAgent = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "preferred_agent" -DefaultValue "")

$dispatchPayload = [PSCustomObject]@{
    task_id = $TaskId
    title = $taskTitle
    description = $taskDescription
    reason = $taskReason
    priority = $taskPriority
    execution_mode = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "execution_mode" -DefaultValue "external-agent")
    runtime_engine = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "runtime_engine" -DefaultValue "hybrid")
    preferred_agent = $taskPreferredAgent
    assigned_agent = [string](Get-V2OptionalProperty -InputObject $taskObject -Name "assigned_agent" -DefaultValue "")
    requested_by_agent = $AgentName
    requested_at = (Get-V2Timestamp)
    files_affected = @($taskFiles)
    dependencies = @($taskDependencies)
    preflight_path = $taskPreflight
    bridge_version = "v2-external-agent-bridge"
}

if ($canWriteDispatch) {
    Save-V2JsonContent -Path $dispatchPath -Value $dispatchPayload
}

$dispatchRelative = $dispatchPath.Substring($resolvedProjectPath.Length).TrimStart('\', '/') -replace "\\", "/"
$result = [PSCustomObject]@{
    success = $true
    deferred = $true
    reason = "awaiting-external-completion"
    output = "external-agent-dispatched:$dispatchRelative"
    dispatch_path = $dispatchRelative
}

Write-Output ($result | ConvertTo-Json -Depth 6 -Compress)
exit 0
