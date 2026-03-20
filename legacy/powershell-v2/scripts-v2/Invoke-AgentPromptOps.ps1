<#
.SYNOPSIS
    Unified prompt-driven operations for Codex / Claude Code / Antigravity.
.DESCRIPTION
    Provides one stable CLI for cross-channel agent interaction:
      - status: view actionable tasks for an agent
      - next: auto-claim next task and return context bundle path
      - claim: claim explicit task id
      - complete: complete explicit task id
    Uses Invoke-UniversalOrchestratorV2.ps1 internally to preserve transactional DAG semantics.
#>
param(
    [string]$ProjectPath = ".",
    [ValidateSet("status", "next", "claim", "complete")]
    [string]$Action = "status",
    [string]$AgentName = "Codex",
    [string]$TaskId = "",
    [string]$Artifacts = "",
    [string]$Notes = "",
    [string]$CompletionPayloadPath = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Resolve-V2AgentAlias {
    param(
        [string]$InputName,
        [switch]$DefaultToCodex
    )

    $name = [string]$InputName
    if ([string]::IsNullOrWhiteSpace($name)) {
        if ($DefaultToCodex) { return "Codex" }
        return ""
    }
    $normalized = $name.Trim().ToLowerInvariant()

    switch ($normalized) {
        { $_ -in @("codex", "gpt", "openai") } { return "Codex" }
        { $_ -in @("claude", "claude code", "claudecode", "anthropic") } { return "Claude Code" }
        { $_ -in @("antigravity", "anti gravity", "ag") } { return "Antigravity" }
        default { return $InputName.Trim() }
    }
}

function Get-V2PriorityScore {
    param([string]$Priority)
    switch ([string]$Priority) {
        "P0" { return 0 }
        "P1" { return 1 }
        "P2" { return 2 }
        "P3" { return 3 }
        default { return 9 }
    }
}

function Get-V2TaskStatus {
    param([object]$Task)
    $raw = [string](Get-V2OptionalProperty -InputObject $Task -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($raw)) { return "pending" }
    $normalized = $raw.ToLowerInvariant()
    if ($normalized -eq "open") { return "pending" }
    return $normalized
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$agent = Resolve-V2AgentAlias -InputName $AgentName -DefaultToCodex
$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
$dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$schedulerPath = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
$universalPath = Join-Path $PSScriptRoot "Invoke-UniversalOrchestratorV2.ps1"

if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
    throw "task-dag.json not found: $dagPath"
}
if (-not (Test-Path -LiteralPath $universalPath -PathType Leaf)) {
    throw "Universal orchestrator not found: $universalPath"
}

if ($Action -eq "status") {
    $dag = Get-V2JsonContent -Path $dagPath
    $tasks = @((Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @()))

    $mine = @($tasks | Where-Object {
            (Get-V2TaskStatus -Task $_) -eq "in-progress" -and
            (Resolve-V2AgentAlias -InputName ([string](Get-V2OptionalProperty -InputObject $_ -Name "assigned_agent" -DefaultValue ""))) -eq $agent
        })
    $pending = @($tasks | Where-Object { (Get-V2TaskStatus -Task $_) -eq "pending" })
    $needsRevision = @($tasks | Where-Object { (Get-V2TaskStatus -Task $_) -eq "needs-revision" })

    $topCandidates = @(
        $pending |
        Sort-Object `
            @{ Expression = { Get-V2PriorityScore -Priority ([string](Get-V2OptionalProperty -InputObject $_ -Name "priority" -DefaultValue "P3")) } ; Descending = $false },
            @{ Expression = { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") } ; Descending = $false } |
        Select-Object -First 8
    )

    $payload = [PSCustomObject]@{
        success = $true
        action = "status"
        agent = $agent
        in_progress_count = $mine.Count
        pending_count = $pending.Count
        needs_revision_count = $needsRevision.Count
        in_progress = @($mine | Select-Object id, title, priority, status, assigned_agent)
        top_candidates = @($topCandidates | Select-Object id, title, priority, preferred_agent, status)
    }
    if ($EmitJson) {
        Write-Output ($payload | ConvertTo-Json -Depth 10)
    }
    else {
        Write-Output ("Agent: {0} | in-progress={1} pending={2} needs-revision={3}" -f $agent, $mine.Count, $pending.Count, $needsRevision.Count)
        foreach ($t in @($mine)) {
            Write-Output ("  IN-PROGRESS: {0} [{1}] {2}" -f $t.id, $t.priority, $t.title)
        }
        foreach ($t in @($topCandidates)) {
            Write-Output ("  CANDIDATE:   {0} [{1}] {2}" -f $t.id, $t.priority, $t.title)
        }
    }
    return
}

if ($Action -eq "next") {
    if (Test-Path -LiteralPath $schedulerPath -PathType Leaf) {
        try {
            & $schedulerPath -ProjectPath $projectRoot | Out-Null
        }
        catch {
            # best effort
        }
    }

    $dag = Get-V2JsonContent -Path $dagPath
    $tasks = @((Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @()))

    $alreadyMine = @($tasks | Where-Object {
            (Get-V2TaskStatus -Task $_) -eq "in-progress" -and
            (Resolve-V2AgentAlias -InputName ([string](Get-V2OptionalProperty -InputObject $_ -Name "assigned_agent" -DefaultValue ""))) -eq $agent
        } |
        Sort-Object `
            @{ Expression = { Get-V2PriorityScore -Priority ([string](Get-V2OptionalProperty -InputObject $_ -Name "priority" -DefaultValue "P3")) } ; Descending = $false },
            @{ Expression = { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") } ; Descending = $false })

    $selectedTask = $null
    if ($alreadyMine.Count -gt 0) {
        $selectedTask = $alreadyMine[0]
    }
    else {
        $candidates = @($tasks | Where-Object {
                (Get-V2TaskStatus -Task $_) -eq "pending"
            } |
            Sort-Object `
                @{ Expression = {
                        $pref = [string](Get-V2OptionalProperty -InputObject $_ -Name "preferred_agent" -DefaultValue "")
                        if ([string]::IsNullOrWhiteSpace($pref)) { return 1 }
                        if ((Resolve-V2AgentAlias -InputName $pref) -eq $agent) { return 0 }
                        return 2
                    }; Descending = $false },
                @{ Expression = { Get-V2PriorityScore -Priority ([string](Get-V2OptionalProperty -InputObject $_ -Name "priority" -DefaultValue "P3")) } ; Descending = $false },
                @{ Expression = { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") } ; Descending = $false })

        if ($candidates.Count -gt 0) {
            $selectedTask = $candidates[0]
            $selectedTaskId = [string](Get-V2OptionalProperty -InputObject $selectedTask -Name "id" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($selectedTaskId)) {
                & $universalPath `
                    -Mode claim `
                    -ProjectPath $projectRoot `
                    -TaskId $selectedTaskId `
                    -AgentName $agent `
                    -Notes "prompt-next:auto-claim" | Out-Null
            }
            $dag = Get-V2JsonContent -Path $dagPath
            $tasks = @((Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @()))
            $selectedTask = @($tasks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $selectedTaskId } | Select-Object -First 1)
            if ($selectedTask.Count -gt 0) { $selectedTask = $selectedTask[0] }
        }
    }

    if (-not $selectedTask) {
        $empty = [PSCustomObject]@{
            success = $true
            action = "next"
            agent = $agent
            task_id = ""
            message = "no-task-available"
        }
        if ($EmitJson) { Write-Output ($empty | ConvertTo-Json -Depth 8) } else { Write-Output "No task available." }
        return
    }

    $taskId = [string](Get-V2OptionalProperty -InputObject $selectedTask -Name "id" -DefaultValue "")
    $bundlePath = Join-Path $orchestratorRoot ("tasks/context-bundles/{0}.md" -f $taskId)
    $bundleRelative = if (Test-Path -LiteralPath $bundlePath -PathType Leaf) {
        Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $bundlePath
    }
    else {
        ""
    }

    $result = [PSCustomObject]@{
        success = $true
        action = "next"
        agent = $agent
        task_id = $taskId
        task_title = [string](Get-V2OptionalProperty -InputObject $selectedTask -Name "title" -DefaultValue "")
        priority = [string](Get-V2OptionalProperty -InputObject $selectedTask -Name "priority" -DefaultValue "P3")
        status = [string](Get-V2OptionalProperty -InputObject $selectedTask -Name "status" -DefaultValue "")
        context_bundle = $bundleRelative
        complete_command = ".\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode complete -ProjectPath `"$projectRoot`" -TaskId `"$taskId`" -AgentName `"$agent`" -CompletionPayloadPath `"ai-orchestrator/tasks/completions/<payload>.json`""
    }
    if ($EmitJson) {
        Write-Output ($result | ConvertTo-Json -Depth 10)
    }
    else {
        Write-Output ("NEXT TASK: {0} [{1}] {2}" -f $result.task_id, $result.priority, $result.task_title)
        if (-not [string]::IsNullOrWhiteSpace($bundleRelative)) {
            Write-Output ("CONTEXT BUNDLE: {0}" -f $bundleRelative)
        }
        Write-Output ("COMPLETE TEMPLATE: {0}" -f $result.complete_command)
    }
    return
}

if ($Action -eq "claim") {
    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        throw "TaskId is required for claim."
    }
    & $universalPath `
        -Mode claim `
        -ProjectPath $projectRoot `
        -TaskId $TaskId `
        -AgentName $agent `
        -Notes $Notes
    return
}

if ($Action -eq "complete") {
    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        throw "TaskId is required for complete."
    }
    & $universalPath `
        -Mode complete `
        -ProjectPath $projectRoot `
        -TaskId $TaskId `
        -AgentName $agent `
        -Artifacts $Artifacts `
        -Notes $Notes `
        -CompletionPayloadPath $CompletionPayloadPath
    return
}
