<#
.SYNOPSIS
    Updates dynamic agent reputation from task outcomes.
.DESCRIPTION
    Reads ai-orchestrator/tasks/task-dag.json and calculates per-agent category scores.
    Writes canonical score state to ai-orchestrator/agents/reputation.json and refreshes
    the "Agent Reputation Scores" table in docs/agents/AGENT_REGISTRY.md.
.PARAMETER ProjectPath
    Project root path.
.PARAMETER EmitJson
    Outputs the updated reputation as JSON.
#>
param(
    [string]$ProjectPath = ".",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path (Join-Path $PSScriptRoot "v2") "Common.ps1")

function Get-TaskCategory {
    param([object]$Task)

    $id = [string](Get-V2OptionalProperty -InputObject $Task -Name "id" -DefaultValue "")
    $type = [string](Get-V2OptionalProperty -InputObject $Task -Name "type" -DefaultValue "")
    $desc = [string](Get-V2OptionalProperty -InputObject $Task -Name "description" -DefaultValue "")
    $title = [string](Get-V2OptionalProperty -InputObject $Task -Name "title" -DefaultValue "")
    $blob = ($id + " " + $type + " " + $title + " " + $desc).ToUpperInvariant()

    if ($blob -match "FRONTEND|UI|UX|COMPONENT|SCREEN|VIEW|CSS|REACT|VUE|ANGULAR") { return "frontend" }
    if ($blob -match "MIGRATION|DB|DATABASE|SQL|INDEX|SCHEMA|TABLE|QUERY") { return "db" }
    if ($blob -match "ARCH|ARCHITECTURE|DESIGN|BOUNDARY|MODULE|ADR") { return "arch" }
    if ($blob -match "TEST|QA|ASSERT|COVERAGE|E2E|INTEGRATION TEST") { return "qa" }
    if ($blob -match "DOCKER|DEPLOY|CI|CD|PIPELINE|K8S|INFRA|DEVOPS") { return "devops" }
    return "backend"
}

function Get-OutcomeWeight {
    param([string]$Status)

    $normalized = if ([string]::IsNullOrWhiteSpace($Status)) { "pending" } else { $Status.ToLowerInvariant() }
    if ($normalized -in @("done", "completed")) { return 0.012 }
    if ($normalized -in @("skipped")) { return 0.004 }
    if ($normalized.StartsWith("blocked")) { return -0.02 }
    return 0.0
}

function Clamp-Score {
    param([double]$Value)
    if ($Value -lt 0.10) { return 0.10 }
    if ($Value -gt 0.99) { return 0.99 }
    return [Math]::Round($Value, 3)
}

function Ensure-AgentRecord {
    param(
        [hashtable]$Index,
        [string]$Agent
    )

    if ([string]::IsNullOrWhiteSpace($Agent)) { return }
    if ($Index.ContainsKey($Agent)) { return }

    $Index[$Agent] = [PSCustomObject]@{
        agent = $Agent
        backend = 0.70
        frontend = 0.70
        db = 0.70
        arch = 0.70
        qa = 0.70
        devops = 0.70
        tasks_total = 0
        tasks_success = 0
        tasks_failure = 0
    }
}

function Update-AgentRegistryTable {
    param(
        [string]$RegistryPath,
        [object[]]$Rows
    )

    if (-not (Test-Path -LiteralPath $RegistryPath -PathType Leaf)) {
        return
    }

    $content = Get-Content -LiteralPath $RegistryPath -Raw
    $table = New-Object System.Collections.Generic.List[string]
    $table.Add("## Agent Reputation Scores")
    $table.Add("<!-- Updated automatically after each task -->")
    $table.Add("")
    $table.Add("| Agent | Backend | Frontend | DB | Arch | QA | DevOps | Total | Success | Fail |")
    $table.Add("|-------|---------|----------|----|------|----|--------|-------|---------|------|")
    foreach ($row in @($Rows)) {
        $table.Add("| $($row.agent) | $($row.backend.ToString('0.000')) | $($row.frontend.ToString('0.000')) | $($row.db.ToString('0.000')) | $($row.arch.ToString('0.000')) | $($row.qa.ToString('0.000')) | $($row.devops.ToString('0.000')) | $($row.tasks_total) | $($row.tasks_success) | $($row.tasks_failure) |")
    }

    $replacement = ($table -join [Environment]::NewLine)
    $pattern = "(?s)## Agent Reputation Scores.*?(?=<!-- To add a new agent, copy a block above and fill in the fields\. -->)"
    if ($content -match $pattern) {
        $updated = [regex]::Replace($content, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $replacement + [Environment]::NewLine + [Environment]::NewLine })
        [System.IO.File]::WriteAllText($RegistryPath, $updated)
        return
    }

    $fallback = $content.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $replacement + [Environment]::NewLine
    [System.IO.File]::WriteAllText($RegistryPath, $fallback)
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$dagPath = Join-Path $resolvedProjectPath "ai-orchestrator/tasks/task-dag.json"
if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
    throw "task-dag.json not found: $dagPath"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$registryPath = Join-Path $repoRoot "docs/agents/AGENT_REGISTRY.md"
$reputationPath = Join-Path $resolvedProjectPath "ai-orchestrator/agents/reputation.json"

$defaults = @(
    [PSCustomObject]@{ agent = "Codex"; backend = 0.90; frontend = 0.70; db = 0.88; arch = 0.65; qa = 0.72; devops = 0.75; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "Claude"; backend = 0.70; frontend = 0.65; db = 0.75; arch = 0.95; qa = 0.80; devops = 0.60; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "Antigravity"; backend = 0.75; frontend = 0.70; db = 0.70; arch = 0.70; qa = 0.90; devops = 0.88; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI Engineer"; backend = 0.75; frontend = 0.70; db = 0.70; arch = 0.70; qa = 0.70; devops = 0.70; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI Architect"; backend = 0.70; frontend = 0.65; db = 0.72; arch = 0.90; qa = 0.75; devops = 0.65; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI DevOps Engineer"; backend = 0.68; frontend = 0.60; db = 0.72; arch = 0.70; qa = 0.70; devops = 0.92; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI Product Manager"; backend = 0.65; frontend = 0.70; db = 0.60; arch = 0.80; qa = 0.72; devops = 0.60; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI CTO"; backend = 0.60; frontend = 0.60; db = 0.60; arch = 0.92; qa = 0.70; devops = 0.60; tasks_total = 0; tasks_success = 0; tasks_failure = 0 },
    [PSCustomObject]@{ agent = "AI Security Engineer"; backend = 0.70; frontend = 0.60; db = 0.72; arch = 0.78; qa = 0.75; devops = 0.72; tasks_total = 0; tasks_success = 0; tasks_failure = 0 }
)

$index = @{}
foreach ($row in $defaults) {
    $index[$row.agent] = $row
}

$existing = Get-V2JsonContent -Path $reputationPath
if ($existing -and ($existing.PSObject.Properties.Name -contains "agents")) {
    foreach ($entry in @($existing.agents)) {
        $agentName = [string](Get-V2OptionalProperty -InputObject $entry -Name "agent" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($agentName)) { continue }
        Ensure-AgentRecord -Index $index -Agent $agentName
        $record = $index[$agentName]
        foreach ($metric in @("backend", "frontend", "db", "arch", "qa", "devops")) {
            $record.$metric = [double](Get-V2OptionalProperty -InputObject $entry -Name $metric -DefaultValue $record.$metric)
        }
    }
}

$dag = Get-Content -LiteralPath $dagPath -Raw | ConvertFrom-Json
$tasks = @($dag.tasks)
foreach ($task in $tasks) {
    $agent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($agent)) { continue }

    Ensure-AgentRecord -Index $index -Agent $agent
    $record = $index[$agent]
    $record.tasks_total = [int]$record.tasks_total + 1

    $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "pending")
    $delta = Get-OutcomeWeight -Status $status
    if ($delta -gt 0) {
        $record.tasks_success = [int]$record.tasks_success + 1
    }
    elseif ($delta -lt 0) {
        $record.tasks_failure = [int]$record.tasks_failure + 1
    }
    if ($delta -eq 0) { continue }

    $category = Get-TaskCategory -Task $task
    $current = [double]$record.$category
    $record.$category = Clamp-Score -Value ($current + $delta)
}

$rows = @(
    @($index.Values) |
    Sort-Object `
        @{ Expression = { [string]$_.agent }; Descending = $false }
)

$payload = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    source_task_dag = $dagPath
    agents = @($rows)
}
Save-V2JsonContent -Path $reputationPath -Value $payload
Update-AgentRegistryTable -RegistryPath $registryPath -Rows @($rows)

if ($EmitJson) {
    $payload | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Agent reputation updated."
    Write-Output "Output: $reputationPath"
}
