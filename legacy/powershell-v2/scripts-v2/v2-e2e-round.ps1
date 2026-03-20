<#
.SYNOPSIS
    Runs a full V2 end-to-end regression round for a new managed project.
.DESCRIPTION
    Executes the complete orchestrator flow for a fresh project:
      env-check (optional) -> v2-new -> v2-submit -> v2-observe (RunOnce) ->
      v2-schedule -> v2-agent-dispatch -> v2-validate-agents ->
      v2-loop (RunOnce) -> v2-verify.
    Produces a deterministic READY/NOT READY summary based on state, health and DAG.
.PARAMETER ProjectName
    Name for the new E2E project. Defaults to e2e-round-<timestamp>.
.PARAMETER ManagedProjectsRoot
    Root folder where v2-new creates the project.
.PARAMETER Stack
    Target stack passed to orchestrator actions.
.PARAMETER Database
    Target database passed to orchestrator actions.
.PARAMETER InfraMode
    Infrastructure mode passed to orchestrator actions.
.PARAMETER DockerConfigMode
    Docker config mode passed to orchestrator actions.
.PARAMETER SkipEnvCheck
    Skips the initial env-check step.
.PARAMETER ReuseExistingProject
    Allows reusing an existing directory at ManagedProjectsRoot/ProjectName.
.PARAMETER IncludeNeo4j
    Explicitly enables Neo4j in actions.
.PARAMETER IncludeQdrant
    Explicitly enables Qdrant in actions.
.PARAMETER IncludeWorker
    Explicitly enables worker in actions.
.PARAMETER IncludeRedis
    Explicitly enables Redis in actions.
.PARAMETER IncludeRabbitMq
    Explicitly enables RabbitMQ in actions.
.PARAMETER ValidationProfile
    Validation profile used for final READY/NOT READY checks.
    - full: requires health=healthy, CORE-COMPLETE done and zero pending tasks.
    - core-smoke: validates orchestration pipeline integrity without forcing feature backlog closure.
#>
param(
    [string]$ProjectName = ("e2e-round-" + (Get-Date -Format "yyyyMMdd-HHmmss")),
    [string]$ManagedProjectsRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path "workspace\\tmp"),
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "python",
    [ValidateSet("auto", "postgres", "mysql", "mongodb", "none")]
    [string]$Database = "postgres",
    [ValidateSet("dedicated-infra", "shared-infra")]
    [string]$InfraMode = "dedicated-infra",
    [ValidateSet("user", "isolated")]
    [string]$DockerConfigMode = "isolated",
    [switch]$SkipEnvCheck,
    [switch]$ReuseExistingProject,
    [switch]$IncludeNeo4j = $true,
    [switch]$IncludeQdrant = $true,
    [switch]$IncludeWorker,
    [switch]$IncludeRedis,
    [switch]$IncludeRabbitMq,
    [ValidateSet("full", "core-smoke")]
    [string]$ValidationProfile = "full"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Add-Result {
    param(
        [System.Collections.Generic.List[object]]$Results,
        [string]$Step,
        [string]$Status,
        [double]$DurationSeconds,
        [string]$Details = ""
    )
    $Results.Add([PSCustomObject]@{
        step      = $Step
        status    = $Status
        duration  = [Math]::Round($DurationSeconds, 2)
        details   = $Details
        timestamp = (Get-Date).ToString("s")
    })
}

function Invoke-RoundStep {
    param(
        [System.Collections.Generic.List[object]]$Results,
        [string]$Step,
        [scriptblock]$Action
    )

    $start = Get-Date
    try {
        & $Action
        $elapsed = (Get-Date) - $start
        Add-Result -Results $Results -Step $Step -Status "ok" -DurationSeconds $elapsed.TotalSeconds
    }
    catch {
        $elapsed = (Get-Date) - $start
        $message = $_.Exception.Message
        Add-Result -Results $Results -Step $Step -Status "fail" -DurationSeconds $elapsed.TotalSeconds -Details $message
        throw "Step failed [$Step]: $message"
    }
}

function Invoke-OrchestratorAction {
    param(
        [string]$OrchestratorPath,
        [hashtable]$Arguments
    )

    $argList = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", $OrchestratorPath
    )

    foreach ($entry in $Arguments.GetEnumerator()) {
        $name = [string]$entry.Key
        $value = $entry.Value
        if ($value -is [bool]) {
            if ($value) {
                $argList += "-$name"
            }
            continue
        }
        if ($null -eq $value) { continue }
        $argList += "-$name"
        $argList += [string]$value
    }

    $output = @(& powershell.exe @argList 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $message = if ($output) { ($output | Out-String).Trim() } else { "orchestrator-action-failed:$LASTEXITCODE" }
        throw $message
    }
    return $output
}

function Resolve-ProjectPathFromNew {
    param(
        [string]$Root,
        [string]$PreferredName,
        [string[]]$Before
    )

    $preferred = Join-Path $Root $PreferredName
    if (Test-Path -LiteralPath $preferred -PathType Container) {
        return $preferred
    }

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Managed projects root not found after v2-new: $Root"
    }

    $afterDirs = @(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction Stop)
    $newDirs = @($afterDirs | Where-Object { $_.Name -notin $Before })
    if ($newDirs.Count -eq 1) {
        return $newDirs[0].FullName
    }
    if ($newDirs.Count -gt 1) {
        return ($newDirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
    }

    throw "Could not resolve project path created by v2-new for project name '$PreferredName'."
}

function Get-RequiredObject {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object) {
        return $null
    }
    if ($Object.PSObject.Properties.Name -contains $Name) {
        return $Object.$Name
    }
    return $null
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
$orchestratorPath = Join-Path $repoRoot "orchestrator.ps1"
if (-not (Test-Path -LiteralPath $orchestratorPath -PathType Leaf)) {
    throw "Root orchestrator not found: $orchestratorPath"
}

if (-not (Test-Path -LiteralPath $ManagedProjectsRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $ManagedProjectsRoot -Force | Out-Null
}

$projectPath = Join-Path $ManagedProjectsRoot $ProjectName
if ((Test-Path -LiteralPath $projectPath -PathType Container) -and -not $ReuseExistingProject) {
    throw "Project already exists: $projectPath. Use -ReuseExistingProject or change -ProjectName."
}

$stepResults = New-Object System.Collections.Generic.List[object]
$existingDirNames = @()
if (Test-Path -LiteralPath $ManagedProjectsRoot -PathType Container) {
    $existingDirNames = @(Get-ChildItem -LiteralPath $ManagedProjectsRoot -Directory | ForEach-Object { $_.Name })
}

Write-Host "== V2 E2E Round ==" -ForegroundColor Cyan
Write-Host ("ProjectName: {0}" -f $ProjectName)
Write-Host ("ManagedProjectsRoot: {0}" -f $ManagedProjectsRoot)
Write-Host ("Stack/DB: {0}/{1}" -f $Stack, $Database)
Write-Host ("Infra/DockerConfig: {0}/{1}" -f $InfraMode, $DockerConfigMode)
Write-Host ""

if (-not $SkipEnvCheck) {
    Invoke-RoundStep -Results $stepResults -Step "env-check" -Action {
        [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
            Action = "env-check"
        })
    }
}

Invoke-RoundStep -Results $stepResults -Step "v2-new" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-new"
        ProjectName = $ProjectName
        ManagedProjectsRoot = $ManagedProjectsRoot
        Stack = $Stack
        Database = $Database
        InfraMode = $InfraMode
        DockerConfigMode = $DockerConfigMode
        IncludeNeo4j = [bool]$IncludeNeo4j
        IncludeQdrant = [bool]$IncludeQdrant
        IncludeWorker = [bool]$IncludeWorker
        IncludeRedis = [bool]$IncludeRedis
        IncludeRabbitMq = [bool]$IncludeRabbitMq
        Force = $true
        ForceConfirmed = $true
    })
}

if (-not (Test-Path -LiteralPath $projectPath -PathType Container)) {
    $projectPath = Resolve-ProjectPathFromNew -Root $ManagedProjectsRoot -PreferredName $ProjectName -Before $existingDirNames
}

Invoke-RoundStep -Results $stepResults -Step "v2-submit" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-submit"
        ProjectPath = $projectPath
        Stack = $Stack
        Database = $Database
        InfraMode = $InfraMode
        DockerConfigMode = $DockerConfigMode
        IncludeNeo4j = [bool]$IncludeNeo4j
        IncludeQdrant = [bool]$IncludeQdrant
        IncludeWorker = [bool]$IncludeWorker
        IncludeRedis = [bool]$IncludeRedis
        IncludeRabbitMq = [bool]$IncludeRabbitMq
        Force = $true
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-observe" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-observe"
        ProjectPath = $projectPath
        DockerConfigMode = $DockerConfigMode
        IncludeNeo4j = [bool]$IncludeNeo4j
        IncludeQdrant = [bool]$IncludeQdrant
        RunOnce = $true
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-schedule" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-schedule"
        ProjectPath = $projectPath
        DockerConfigMode = $DockerConfigMode
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-agent-dispatch" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-agent-dispatch"
        ProjectPath = $projectPath
        Phase = "auto"
        AutoRepairTasks = $true
        FailOnAgentFailure = $true
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-validate-agents" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-validate-agents"
        ProjectPath = $projectPath
        Phase = "auto"
        AutoRepairTasks = $true
        FailOnNotReady = $true
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-loop" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-loop"
        ProjectPath = $projectPath
        PollIntervalSeconds = 10
        IncludeNeo4j = [bool]$IncludeNeo4j
        IncludeQdrant = [bool]$IncludeQdrant
        RunOnce = $true
    })
}

Invoke-RoundStep -Results $stepResults -Step "v2-verify" -Action {
    [void](Invoke-OrchestratorAction -OrchestratorPath $orchestratorPath -Arguments @{
        Action = "v2-verify"
        ProjectPath = $projectPath
    })
}

$statePath = Join-Path $projectPath "ai-orchestrator\\state\\project-state.json"
$healthPath = Join-Path $projectPath "ai-orchestrator\\state\\health-report.json"
$dagPath = Join-Path $projectPath "ai-orchestrator\\tasks\\task-dag.json"
$historyPath = Join-Path $projectPath "ai-orchestrator\\tasks\\execution-history.md"
$agentValidationReportPath = Join-Path $projectPath "ai-orchestrator\\reports\\agent-artifact-validation-report.json"

if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw "Missing project-state.json: $statePath"
}
if (-not (Test-Path -LiteralPath $healthPath -PathType Leaf)) {
    throw "Missing health-report.json: $healthPath"
}
if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
    throw "Missing task-dag.json: $dagPath"
}
if (-not (Test-Path -LiteralPath $agentValidationReportPath -PathType Leaf)) {
    throw "Missing agent artifact validation report: $agentValidationReportPath"
}

$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
$health = Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json
$dag = Get-Content -LiteralPath $dagPath -Raw | ConvertFrom-Json
$agentValidationReport = Get-Content -LiteralPath $agentValidationReportPath -Raw | ConvertFrom-Json
$historyContent = if (Test-Path -LiteralPath $historyPath -PathType Leaf) {
    Get-Content -LiteralPath $historyPath -Raw
}
else {
    ""
}
$completedEventCount = ([regex]::Matches([string]$historyContent, "- event:\s*task-completed")).Count

$coreTask = @($dag.tasks | Where-Object { $_.id -eq "CORE-COMPLETE-001" } | Select-Object -First 1)
$pendingTasks = @($dag.tasks | Where-Object {
    $status = [string](Get-RequiredObject -Object $_ -Name "status")
    $status -ne "done" -and $status -ne "skipped"
})

$verifyStep = @($stepResults | Where-Object { $_.step -eq "v2-verify" } | Select-Object -First 1)
$taskCount = @($dag.tasks).Count
$databases = Get-RequiredObject -Object $state -Name "databases"
$qdrantDb = Get-RequiredObject -Object $databases -Name "qdrant"
$neo4jDb = Get-RequiredObject -Object $databases -Name "neo4j"
$orchestrator360 = Get-V2OptionalProperty -InputObject $state -Name "orchestrator_360" -DefaultValue ([PSCustomObject]@{})
$orchestrator360Paths = Get-V2OptionalProperty -InputObject $orchestrator360 -Name "paths" -DefaultValue ([PSCustomObject]@{})
$businessContextPath = [string](Get-V2OptionalProperty -InputObject $orchestrator360Paths -Name "business_context_json" -DefaultValue "")
$adrPath = [string](Get-V2OptionalProperty -InputObject $orchestrator360Paths -Name "adr" -DefaultValue "")
$businessContextExists = (-not [string]::IsNullOrWhiteSpace($businessContextPath)) -and (Test-Path -LiteralPath $businessContextPath -PathType Leaf)
$adrExists = (-not [string]::IsNullOrWhiteSpace($adrPath)) -and (Test-Path -LiteralPath $adrPath -PathType Leaf)

$commonChecks = @(
    [PSCustomObject]@{ name = "startup-pack-ready"; ok = ([string](Get-RequiredObject -Object $state -Name "startup_pack_status") -eq "ready"); evidence = [string](Get-RequiredObject -Object $state -Name "startup_pack_status") },
    [PSCustomObject]@{ name = "docker-ready"; ok = ([string](Get-RequiredObject -Object $state -Name "docker_status") -eq "ready"); evidence = [string](Get-RequiredObject -Object $state -Name "docker_status") },
    [PSCustomObject]@{ name = "not-blocked"; ok = (-not ([string](Get-RequiredObject -Object $state -Name "status")).StartsWith("blocked-")); evidence = [string](Get-RequiredObject -Object $state -Name "status") },
    [PSCustomObject]@{ name = "task-dag-populated"; ok = ($taskCount -gt 0); evidence = ("tasks={0}" -f $taskCount) },
    [PSCustomObject]@{ name = "verify-step-ok"; ok = ($verifyStep.Count -eq 1 -and [string](Get-RequiredObject -Object $verifyStep[0] -Name "status") -eq "ok"); evidence = if ($verifyStep.Count -eq 1) { [string](Get-RequiredObject -Object $verifyStep[0] -Name "status") } else { "missing" } },
    [PSCustomObject]@{ name = "agent-validation-ready"; ok = ([string](Get-RequiredObject -Object $agentValidationReport -Name "verdict") -eq "READY"); evidence = [string](Get-RequiredObject -Object $agentValidationReport -Name "verdict") },
    [PSCustomObject]@{ name = "orchestrator-360-generated"; ok = ([string](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "status" -DefaultValue "") -eq "generated"); evidence = [string](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "status" -DefaultValue "missing") },
    [PSCustomObject]@{ name = "business-context-present"; ok = $businessContextExists; evidence = if ($businessContextExists) { $businessContextPath } else { "missing" } },
    [PSCustomObject]@{ name = "architecture-adr-present"; ok = $adrExists; evidence = if ($adrExists) { $adrPath } else { "missing" } },
    [PSCustomObject]@{ name = "qdrant-collection-ready"; ok = ($null -eq $qdrantDb -or -not [bool](Get-RequiredObject -Object $qdrantDb -Name "enabled") -or [string](Get-RequiredObject -Object $qdrantDb -Name "collection_ready") -eq "ready"); evidence = if ($null -eq $qdrantDb) { "n/a" } else { [string](Get-RequiredObject -Object $qdrantDb -Name "collection_ready") } },
    [PSCustomObject]@{ name = "neo4j-uri-present"; ok = ($null -eq $neo4jDb -or -not [bool](Get-RequiredObject -Object $neo4jDb -Name "enabled") -or -not [string]::IsNullOrWhiteSpace([string](Get-RequiredObject -Object $neo4jDb -Name "uri"))); evidence = if ($null -eq $neo4jDb) { "n/a" } else { [string](Get-RequiredObject -Object $neo4jDb -Name "uri") } }
)
$fullOnlyChecks = @(
    [PSCustomObject]@{ name = "health-healthy"; ok = ([string](Get-RequiredObject -Object $health -Name "health_status") -eq "healthy"); evidence = [string](Get-RequiredObject -Object $health -Name "health_status") },
    [PSCustomObject]@{ name = "core-complete-done"; ok = ($coreTask.Count -eq 1 -and [string](Get-RequiredObject -Object $coreTask[0] -Name "status") -eq "done"); evidence = if ($coreTask.Count -eq 1) { [string](Get-RequiredObject -Object $coreTask[0] -Name "status") } else { "missing" } },
    [PSCustomObject]@{ name = "pending-task-count-zero"; ok = ($pendingTasks.Count -eq 0); evidence = ("pending={0}" -f $pendingTasks.Count) }
)
$smokeOnlyChecks = @(
    [PSCustomObject]@{ name = "task-completed-events"; ok = ($completedEventCount -gt 0); evidence = ("completed_events={0}" -f $completedEventCount) }
)
$checks = @($commonChecks)
if ($ValidationProfile -eq "full") {
    $checks += $fullOnlyChecks
}
else {
    $checks += $smokeOnlyChecks
}

$failedChecks = @($checks | Where-Object { -not $_.ok })
$verdict = if ($failedChecks.Count -eq 0) { "READY" } else { "NOT READY" }

$summary = [PSCustomObject]@{
    timestamp            = (Get-Date).ToString("s")
    project_name         = [string](Get-RequiredObject -Object $state -Name "project_name")
    project_slug         = [string](Get-RequiredObject -Object $state -Name "project_slug")
    project_path         = $projectPath
    stack                = $Stack
    database             = $Database
    infra_mode           = $InfraMode
    docker_config_mode   = $DockerConfigMode
    validation_profile   = $ValidationProfile
    verdict              = $verdict
    step_results         = $stepResults
    checks               = $checks
    failed_checks        = $failedChecks
    pending_tasks        = @($pendingTasks | Select-Object id, status, priority, assigned_agent, preferred_agent)
}

$summaryPath = Join-Path $projectPath "ai-orchestrator\\reports\\e2e-round-summary.json"
$summaryDir = Split-Path -Parent $summaryPath
if (-not (Test-Path -LiteralPath $summaryDir -PathType Container)) {
    New-Item -ItemType Directory -Path $summaryDir -Force | Out-Null
}
$summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

Write-Host ""
$verdictColor = if ($verdict -eq "READY") { "Green" } else { "Red" }
Write-Host ("Verdict: {0}" -f $verdict) -ForegroundColor $verdictColor
Write-Host ("ProjectPath: {0}" -f $projectPath)
Write-Host ("Summary: {0}" -f $summaryPath)
Write-Host ""
Write-Host "Step results:"
$stepResults | ForEach-Object {
    Write-Host ("- {0}: {1} ({2}s){3}" -f $_.step, $_.status, $_.duration, $(if ([string]::IsNullOrWhiteSpace($_.details)) { "" } else { " - " + $_.details }))
}
Write-Host ""
Write-Host "Checks:"
$checks | ForEach-Object {
    Write-Host ("- {0}: {1} ({2})" -f $_.name, $(if ($_.ok) { "PASS" } else { "FAIL" }), $_.evidence)
}

if ($verdict -ne "READY") {
    throw ("V2 E2E round failed checks: {0}" -f (($failedChecks | ForEach-Object { $_.name }) -join ", "))
}
