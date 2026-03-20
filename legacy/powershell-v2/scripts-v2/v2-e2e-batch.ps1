<#
.SYNOPSIS
    Runs consolidated V2 E2E validation across multiple stack/database combinations.
.DESCRIPTION
    Executes v2-e2e-round for each case in a batch and produces a consolidated
    READY/NOT READY report (JSON + Markdown) with per-case evidence.
.PARAMETER BatchName
    Identifier used in report file names and project name prefixes.
.PARAMETER ManagedProjectsRoot
    Root where round projects are created.
.PARAMETER Cases
    Stack/database cases in the format <stack>:<database> (for example: python:postgres).
.PARAMETER InfraMode
    Infrastructure mode passed to each round.
.PARAMETER DockerConfigMode
    Docker config mode passed to each round.
.PARAMETER SkipEnvCheck
    Skips env-check for all rounds.
.PARAMETER ReuseExistingProjects
    Reuses existing per-case project directories.
.PARAMETER IncludeNeo4j
    Explicitly enables Neo4j in rounds.
.PARAMETER IncludeQdrant
    Explicitly enables Qdrant in rounds.
.PARAMETER IncludeWorker
    Explicitly enables worker service in rounds.
.PARAMETER IncludeRedis
    Explicitly enables Redis in rounds.
.PARAMETER IncludeRabbitMq
    Explicitly enables RabbitMQ in rounds.
.PARAMETER ValidationProfile
    Validation profile passed to each v2-e2e-round execution.
#>
param(
    [string]$BatchName = ("e2e-batch-" + (Get-Date -Format "yyyyMMdd-HHmmss")),
    [string]$ManagedProjectsRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path "workspace\\tmp"),
    [string[]]$Cases = @("python:postgres", "node:postgres", "php:mysql"),
    [ValidateSet("dedicated-infra", "shared-infra")]
    [string]$InfraMode = "dedicated-infra",
    [ValidateSet("user", "isolated")]
    [string]$DockerConfigMode = "isolated",
    [switch]$SkipEnvCheck,
    [switch]$ReuseExistingProjects,
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

$validStacks = @("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")
$validDatabases = @("auto", "postgres", "mysql", "mongodb", "none")

function Parse-Case {
    param([string]$Case)

    $value = [string]$Case
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Invalid empty case entry."
    }

    if ($value -notmatch "^(?<stack>[a-z0-9]+)[:/](?<database>[a-z0-9]+)$") {
        throw "Invalid case format '$value'. Expected <stack>:<database>."
    }

    $stack = [string]$Matches.stack
    $database = [string]$Matches.database
    if ($stack -notin $validStacks) {
        throw "Invalid stack '$stack' in case '$value'."
    }
    if ($database -notin $validDatabases) {
        throw "Invalid database '$database' in case '$value'."
    }

    return [PSCustomObject]@{
        raw      = $value
        stack    = $stack
        database = $database
    }
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
$roundScript = Join-Path $PSScriptRoot "v2-e2e-round.ps1"
if (-not (Test-Path -LiteralPath $roundScript -PathType Leaf)) {
    throw "V2 round script not found: $roundScript"
}

if (-not (Test-Path -LiteralPath $ManagedProjectsRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $ManagedProjectsRoot -Force | Out-Null
}

$parsedCases = @()
foreach ($case in $Cases) {
    $parsedCases += Parse-Case -Case $case
}
if ($parsedCases.Count -eq 0) {
    throw "No cases provided for batch execution."
}

$batchOutputRoot = Join-Path $ManagedProjectsRoot "_batch-reports"
if (-not (Test-Path -LiteralPath $batchOutputRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $batchOutputRoot -Force | Out-Null
}

$results = New-Object System.Collections.Generic.List[object]
$firstRound = $true

Write-Host "== V2 E2E Batch ==" -ForegroundColor Cyan
Write-Host ("BatchName: {0}" -f $BatchName)
Write-Host ("ManagedProjectsRoot: {0}" -f $ManagedProjectsRoot)
Write-Host ("Cases: {0}" -f (($parsedCases | ForEach-Object { $_.raw }) -join ", "))
Write-Host ""

foreach ($case in $parsedCases) {
    $projectName = ("{0}-{1}-{2}" -f $BatchName, $case.stack, $case.database)
    $projectPath = Join-Path $ManagedProjectsRoot $projectName
    $start = Get-Date
    $status = "ok"
    $errorMessage = ""

    $roundArgs = @{
        ProjectName         = $projectName
        ManagedProjectsRoot = $ManagedProjectsRoot
        Stack               = $case.stack
        Database            = $case.database
        InfraMode           = $InfraMode
        DockerConfigMode    = $DockerConfigMode
        ValidationProfile   = $ValidationProfile
        IncludeNeo4j        = [bool]$IncludeNeo4j
        IncludeQdrant       = [bool]$IncludeQdrant
    }
    if ($IncludeWorker) { $roundArgs.IncludeWorker = $true }
    if ($IncludeRedis) { $roundArgs.IncludeRedis = $true }
    if ($IncludeRabbitMq) { $roundArgs.IncludeRabbitMq = $true }
    if ($ReuseExistingProjects) { $roundArgs.ReuseExistingProject = $true }
    if ($SkipEnvCheck -or -not $firstRound) { $roundArgs.SkipEnvCheck = $true }

    Write-Host ("Running case: {0} ({1}/{2})" -f $case.raw, $case.stack, $case.database) -ForegroundColor Yellow
    try {
        & $roundScript @roundArgs
    }
    catch {
        $status = "fail"
        $errorMessage = $_.Exception.Message
        Write-Host ("Case failed: {0}" -f $errorMessage) -ForegroundColor Red
    }

    $summaryPath = Join-Path $projectPath "ai-orchestrator\\reports\\e2e-round-summary.json"
    $roundSummary = $null
    if (Test-Path -LiteralPath $summaryPath -PathType Leaf) {
        $roundSummary = Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json
    }

    $failedChecks = @()
    if ($null -ne $roundSummary) {
        $failedChecks = @((Get-RequiredObject -Object $roundSummary -Name "failed_checks") | ForEach-Object {
                [string](Get-RequiredObject -Object $_ -Name "name")
            } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    $duration = [Math]::Round(((Get-Date) - $start).TotalSeconds, 2)
    $verdict = if ($null -ne $roundSummary) { [string](Get-RequiredObject -Object $roundSummary -Name "verdict") } else { if ($status -eq "ok") { "UNKNOWN" } else { "NOT READY" } }
    if ([string]::IsNullOrWhiteSpace($verdict)) {
        $verdict = if ($status -eq "ok") { "UNKNOWN" } else { "NOT READY" }
    }

    $results.Add([PSCustomObject]@{
            case          = $case.raw
            stack         = $case.stack
            database      = $case.database
            project_name  = $projectName
            project_path  = $projectPath
            status        = $status
            verdict       = $verdict
            duration      = $duration
            failed_checks = $failedChecks
            summary_path  = $summaryPath
            error         = $errorMessage
        })

    $firstRound = $false
    Write-Host ("Case verdict: {0}" -f $verdict) -ForegroundColor $(if ($verdict -eq "READY") { "Green" } else { "Red" })
    Write-Host ""
}

$notReadyCount = @($results | Where-Object { $_.verdict -ne "READY" }).Count
$batchVerdict = if ($notReadyCount -eq 0) { "READY" } else { "NOT READY" }

$batchSummary = [PSCustomObject]@{
    timestamp             = (Get-Date).ToString("s")
    batch_name            = $BatchName
    managed_projects_root = $ManagedProjectsRoot
    validation_profile    = $ValidationProfile
    verdict               = $batchVerdict
    total_cases           = $results.Count
    ready_cases           = @($results | Where-Object { $_.verdict -eq "READY" }).Count
    not_ready_cases       = $notReadyCount
    results               = $results
}

$safeBatchName = ($BatchName -replace "[^a-zA-Z0-9_.-]", "-")
$summaryJsonPath = Join-Path $batchOutputRoot ("{0}-summary.json" -f $safeBatchName)
$summaryMdPath = Join-Path $batchOutputRoot ("{0}-summary.md" -f $safeBatchName)

$batchSummary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryJsonPath -Encoding UTF8

$mdLines = New-Object System.Collections.Generic.List[string]
$mdLines.Add(("# V2 E2E Batch Report - {0}" -f $BatchName))
$mdLines.Add("")
$mdLines.Add(("- Timestamp: {0}" -f $batchSummary.timestamp))
$mdLines.Add(("- Verdict: **{0}**" -f $batchVerdict))
$mdLines.Add(("- Cases: {0}" -f $batchSummary.total_cases))
$mdLines.Add(("- READY: {0}" -f $batchSummary.ready_cases))
$mdLines.Add(("- NOT READY: {0}" -f $batchSummary.not_ready_cases))
$mdLines.Add("")
$mdLines.Add("| Case | Verdict | Status | Duration(s) | Failed checks | Summary |")
$mdLines.Add("|---|---|---|---:|---|---|")
foreach ($item in $results) {
    $failed = if ($item.failed_checks.Count -gt 0) { ($item.failed_checks -join ", ") } else { "-" }
    $summaryRef = [string]$item.summary_path
    $mdLines.Add(("| {0} | {1} | {2} | {3} | {4} | `{5}` |" -f $item.case, $item.verdict, $item.status, $item.duration, $failed, $summaryRef))
}
$mdLines | Set-Content -LiteralPath $summaryMdPath -Encoding UTF8

Write-Host ("Batch verdict: {0}" -f $batchVerdict) -ForegroundColor $(if ($batchVerdict -eq "READY") { "Green" } else { "Red" })
Write-Host ("JSON summary: {0}" -f $summaryJsonPath)
Write-Host ("Markdown summary: {0}" -f $summaryMdPath)

if ($batchVerdict -ne "READY") {
    throw ("V2 E2E batch failed. NOT READY cases: {0}" -f $notReadyCount)
}
