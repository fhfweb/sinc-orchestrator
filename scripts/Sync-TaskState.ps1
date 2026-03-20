<#
.SYNOPSIS
    Syncs markdown task boards from ai-orchestrator/tasks/task-dag.json.
.DESCRIPTION
    Canonical execution state comes from task-dag.json (tasks[]).
    Regenerates:
      - ai-orchestrator/tasks/task-dag.md
      - ai-orchestrator/tasks/backlog.md
      - ai-orchestrator/tasks/in-progress.md
      - ai-orchestrator/tasks/completed.md
      - docs/agents/TASK_BOARD.md
      - docs/agents/ACTIVE_TASKS.md
.PARAMETER ProjectPath
    Project root path.
#>
param([Parameter(Mandatory)][string]$ProjectPath)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PriorityWeight {
    param([string]$Priority)
    $value = if ($null -eq $Priority) { "" } else { [string]$Priority }
    switch ($value.ToUpperInvariant()) {
        "P0" { return 100 }
        "P1" { return 70 }
        "P2" { return 40 }
        "P3" { return 10 }
        default { return 0 }
    }
}

function As-Array {
    param([object]$Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
        return @($Value)
    }
    return @($Value)
}

function Get-Prop {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$DefaultValue = $null
    )

    if ($null -eq $InputObject) { return $DefaultValue }
    if ($InputObject.PSObject.Properties.Name -contains $Name) {
        return $InputObject.$Name
    }
    return $DefaultValue
}

$resolvedProject = if (Test-Path -LiteralPath $ProjectPath) { (Resolve-Path -LiteralPath $ProjectPath).Path } else { $ProjectPath }
if (-not (Test-Path -LiteralPath $resolvedProject -PathType Container)) {
    throw "Project path not found: $ProjectPath"
}

$dagCandidates = @(
    (Join-Path $resolvedProject "ai-orchestrator/tasks/task-dag.json"),
    (Join-Path $resolvedProject ".ai-orchestrator/tasks/task-dag.json")
)
$dagPath = ""
foreach ($candidate in $dagCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $dagPath = $candidate
        break
    }
}
if ([string]::IsNullOrWhiteSpace($dagPath)) {
    throw "task-dag.json not found under ai-orchestrator/tasks."
}

$docsAgentsPath = Join-Path $resolvedProject "docs/agents"
if (-not (Test-Path -LiteralPath $docsAgentsPath -PathType Container)) {
    [void](New-Item -ItemType Directory -Path $docsAgentsPath -Force)
}

$taskBoardPath = Join-Path $docsAgentsPath "TASK_BOARD.md"
$activeTasksPath = Join-Path $docsAgentsPath "ACTIVE_TASKS.md"

$dag = Get-Content -LiteralPath $dagPath -Raw | ConvertFrom-Json
if (-not $dag -or -not ($dag.PSObject.Properties.Name -contains "tasks")) {
    throw "Invalid task-dag.json: missing tasks array."
}

$tasks = @($dag.tasks | Sort-Object `
    @{ Expression = { Get-PriorityWeight -Priority ([string]$_.priority) }; Descending = $true },
    @{ Expression = { [string]$_.id }; Descending = $false }
)

$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$projectName = Split-Path -Leaf $resolvedProject
$orchestratorTasksPath = Split-Path -Parent $dagPath
$taskDagMdPath = Join-Path $orchestratorTasksPath "task-dag.md"
$backlogPath = Join-Path $orchestratorTasksPath "backlog.md"
$inProgressPath = Join-Path $orchestratorTasksPath "in-progress.md"
$completedPath = Join-Path $orchestratorTasksPath "completed.md"

$rows = New-Object System.Collections.Generic.List[string]
foreach ($task in $tasks) {
    $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "")
    $title = [string](Get-Prop -InputObject $task -Name "title" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($title)) { $title = [string](Get-Prop -InputObject $task -Name "description" -DefaultValue "") }
    if ([string]::IsNullOrWhiteSpace($title)) { $title = "-" }
    $status = [string](Get-Prop -InputObject $task -Name "status" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($status)) { $status = "pending" }
    $priority = [string](Get-Prop -InputObject $task -Name "priority" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($priority)) { $priority = "P3" }
    $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($agent)) { $agent = "-" }
    $deps = @((As-Array -Value (Get-Prop -InputObject $task -Name "dependencies" -DefaultValue @())) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $depText = if ($deps.Count -eq 0) { "-" } else { $deps -join ", " }
    $rows.Add("| $id | $title | $status | $priority | $agent | $depText |")
}

$taskDagLines = New-Object System.Collections.Generic.List[string]
$taskDagLines.Add("# Task DAG")
$taskDagLines.Add("")
$taskDagLines.Add("~~~yaml")
$taskDagLines.Add("tasks:")
foreach ($task in $tasks) {
    $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "unknown")
    $description = [string](Get-Prop -InputObject $task -Name "description" -DefaultValue "")
    $status = [string](Get-Prop -InputObject $task -Name "status" -DefaultValue "pending")
    $priority = [string](Get-Prop -InputObject $task -Name "priority" -DefaultValue "P3")
    $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
    $deps = @((As-Array -Value (Get-Prop -InputObject $task -Name "dependencies" -DefaultValue @())) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $depText = if ($deps.Count -eq 0) { "[]" } else { "[{0}]" -f ($deps -join ", ") }
    $files = @((As-Array -Value (Get-Prop -InputObject $task -Name "files_affected" -DefaultValue @())) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })

    $taskDagLines.Add("  - id: $id")
    $taskDagLines.Add("    description: $description")
    $taskDagLines.Add("    priority: $priority")
    $taskDagLines.Add("    dependencies: $depText")
    $taskDagLines.Add("    assigned_agent: $agent")
    $taskDagLines.Add("    status: $status")
    $taskDagLines.Add("    files_affected:")
    if ($files.Count -eq 0) {
        $taskDagLines.Add("      - none")
    }
    else {
        foreach ($file in $files) {
            $taskDagLines.Add("      - $file")
        }
    }
    $blockedReason = [string](Get-Prop -InputObject $task -Name "blocked_reason" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($blockedReason)) {
        $taskDagLines.Add("    blocked_reason: $blockedReason")
    }
    $taskDagLines.Add("")
}
$taskDagLines.Add("~~~")
[System.IO.File]::WriteAllText($taskDagMdPath, ($taskDagLines -join [Environment]::NewLine))

$backlogLines = New-Object System.Collections.Generic.List[string]
$backlogLines.Add("# Backlog")
$backlogLines.Add("")
$backlogTasks = @($tasks | Where-Object {
    $status = [string](Get-Prop -InputObject $_ -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($status)) { $status = "pending" }
    $status -notin @("done", "completed", "skipped", "in-progress")
})
if ($backlogTasks.Count -eq 0) {
    $backlogLines.Add("- none")
}
else {
    foreach ($task in $backlogTasks) {
        $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "unknown")
        $description = [string](Get-Prop -InputObject $task -Name "description" -DefaultValue "")
        $priority = [string](Get-Prop -InputObject $task -Name "priority" -DefaultValue "P3")
        $status = [string](Get-Prop -InputObject $task -Name "status" -DefaultValue "pending")
        $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
        $deps = @((As-Array -Value (Get-Prop -InputObject $task -Name "dependencies" -DefaultValue @())) | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $depText = if ($deps.Count -eq 0) { "[]" } else { "[{0}]" -f ($deps -join ", ") }
        $backlogLines.Add("- id: $id")
        $backlogLines.Add("  description: $description")
        $backlogLines.Add("  priority: $priority")
        $backlogLines.Add("  dependencies: $depText")
        $backlogLines.Add("  assigned_agent: $agent")
        $backlogLines.Add("  status: $status")
        $blockedReason = [string](Get-Prop -InputObject $task -Name "blocked_reason" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($blockedReason)) {
            $backlogLines.Add("  blocked_reason: $blockedReason")
        }
        $backlogLines.Add("")
    }
}
[System.IO.File]::WriteAllText($backlogPath, ($backlogLines -join [Environment]::NewLine))

$inProgressLines = New-Object System.Collections.Generic.List[string]
$inProgressLines.Add("# In Progress")
$inProgressLines.Add("")
$inProgressLines.Add("## Active Tasks")
$inProgressTasks = @($tasks | Where-Object {
    $status = [string](Get-Prop -InputObject $_ -Name "status" -DefaultValue "")
    $status -eq "in-progress"
})
if ($inProgressTasks.Count -eq 0) {
    $inProgressLines.Add("- none")
}
else {
    foreach ($task in $inProgressTasks) {
        $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "unknown")
        $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
        $startedAt = [string](Get-Prop -InputObject $task -Name "started_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($startedAt)) { $startedAt = "-" }
        $inProgressLines.Add("- id: $id | agent: $agent | started_at: $startedAt")
    }
}
[System.IO.File]::WriteAllText($inProgressPath, ($inProgressLines -join [Environment]::NewLine))

$completedLines = New-Object System.Collections.Generic.List[string]
$completedLines.Add("# Completed")
$completedLines.Add("")
$completedTasks = @($tasks | Where-Object {
    $status = [string](Get-Prop -InputObject $_ -Name "status" -DefaultValue "")
    $status -in @("done", "completed", "skipped")
})
if ($completedTasks.Count -eq 0) {
    $completedLines.Add("- none")
}
else {
    foreach ($task in $completedTasks) {
        $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "unknown")
        $status = [string](Get-Prop -InputObject $task -Name "status" -DefaultValue "done")
        $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
        $completedAt = [string](Get-Prop -InputObject $task -Name "completed_at" -DefaultValue "")
        $completedLines.Add("- id: $id | status: $status | agent: $agent | completed_at: $completedAt")
    }
}
[System.IO.File]::WriteAllText($completedPath, ($completedLines -join [Environment]::NewLine))

$board = New-Object System.Collections.Generic.List[string]
$board.Add("# Task Board - $projectName")
$board.Add("")
$board.Add(('> Synced from `task-dag.json` on {0} by `scripts/Sync-TaskState.ps1`.' -f $now))
$board.Add("")
$board.Add("| Task ID | Title | Status | Priority | Agent | Dependencies |")
$board.Add("|---------|-------|--------|----------|-------|--------------|")
if ($rows.Count -eq 0) {
    $board.Add("| - | No tasks | - | - | - | - |")
}
else {
    foreach ($row in @($rows.ToArray())) { $board.Add($row) }
}
[System.IO.File]::WriteAllText($taskBoardPath, ($board -join [Environment]::NewLine))

$activeRows = New-Object System.Collections.Generic.List[string]
foreach ($task in $tasks) {
    $status = [string](Get-Prop -InputObject $task -Name "status" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($status)) { $status = "pending" }
    if ($status -notin @("open", "pending", "in-progress", "blocked", "blocked-waiting-answers", "blocked-lock-conflict", "blocked-runtime", "handoff", "blocked-no-agent", "blocked-startup", "blocked-strategic-mode")) {
        continue
    }
    $id = [string](Get-Prop -InputObject $task -Name "id" -DefaultValue "")
    $title = [string](Get-Prop -InputObject $task -Name "title" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($title)) { $title = [string](Get-Prop -InputObject $task -Name "description" -DefaultValue "") }
    if ([string]::IsNullOrWhiteSpace($title)) { $title = "-" }
    $priority = [string](Get-Prop -InputObject $task -Name "priority" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($priority)) { $priority = "P3" }
    $agent = [string](Get-Prop -InputObject $task -Name "assigned_agent" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($agent)) { $agent = "-" }
    $activeRows.Add("| $id | $title | $status | $priority | $agent |")
}

$active = New-Object System.Collections.Generic.List[string]
$active.Add("# Active Tasks - $projectName")
$active.Add("")
$active.Add("> L2 context view. Synced on $now.")
$active.Add("")
$active.Add("| Task ID | Title | Status | Priority | Agent |")
$active.Add("|---------|-------|--------|----------|-------|")
if ($activeRows.Count -eq 0) {
    $active.Add("| - | No active tasks | - | - | - |")
}
else {
    foreach ($row in @($activeRows.ToArray())) { $active.Add($row) }
}
[System.IO.File]::WriteAllText($activeTasksPath, ($active -join [Environment]::NewLine))

Write-Output "Task sync complete."
Write-Output "Source: $dagPath"
Write-Output "Total tasks: $($tasks.Count)"
