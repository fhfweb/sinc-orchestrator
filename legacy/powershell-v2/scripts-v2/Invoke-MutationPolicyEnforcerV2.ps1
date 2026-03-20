<#
.SYNOPSIS
    Enforces minimum mutation testing score policy per project domain.
.DESCRIPTION
    Reads the latest mutation testing report from ai-orchestrator/reports/mutation-*.json.
    Compares the overall mutation_score_pct against policy thresholds defined in:
      ai-orchestrator/config/mutation-policy.json  (optional, created on first run if absent)

    Policy schema (mutation-policy.json):
      {
        "global_min_score_pct": 85,
        "domain_policies": [
          { "pattern": "auth|security|payment", "min_score_pct": 95, "priority": "P0" },
          { "pattern": "api|controller|route",  "min_score_pct": 90, "priority": "P1" }
        ]
      }

    When the mutation score falls below the threshold for a domain (checked against
    survived_details[].file), a COBERTURA-SCORE-FAIL-<ts>-<domain> task is created in
    task-dag.json with the configured priority (default P1).

    A global COBERTURA-SCORE-FAIL-<ts>-global task is created when overall score < global_min.

    Tasks are deduplicated: if an open COBERTURA-SCORE-FAIL task already exists for the
    same domain, a new one is not created.

.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.

.PARAMETER EmitJson
    Emit JSON result to stdout.

.EXAMPLE
    .\scripts\v2\Invoke-MutationPolicyEnforcerV2.ps1 -ProjectPath C:\projects\myapp
#>
param(
    [string]$ProjectPath = ".",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$reportsDir       = Join-Path $orchestratorRoot "reports"
$configDir        = Join-Path $orchestratorRoot "config"
$policyPath       = Join-Path $configDir "mutation-policy.json"
$dagPath          = Join-Path $orchestratorRoot "tasks/task-dag.json"
$ts               = Get-Date -Format "yyyyMMddHHmmss"

Initialize-V2Directory -Path $configDir

# ── Ensure default policy file exists ────────────────────────────────────────
if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
    $defaultPolicy = [PSCustomObject]@{
        schema_version    = "v1"
        global_min_score_pct = 85
        domain_policies   = @(
            [PSCustomObject]@{ pattern = "auth|security|payment|lgpd|privacy"; min_score_pct = 95; priority = "P0" },
            [PSCustomObject]@{ pattern = "api|controller|route|view|endpoint";  min_score_pct = 90; priority = "P1" },
            [PSCustomObject]@{ pattern = "service|repository|model|domain";     min_score_pct = 85; priority = "P1" }
        )
    }
    Save-V2JsonContent -Path $policyPath -Value $defaultPolicy
    Write-Host "[MutationPolicy] Created default policy: $policyPath"
}

$policy         = Get-V2JsonContent -Path $policyPath
$globalMin      = [double](Get-V2OptionalProperty -InputObject $policy -Name "global_min_score_pct" -DefaultValue 85)
$domainPolicies = @(Get-V2OptionalProperty -InputObject $policy -Name "domain_policies" -DefaultValue @())

# ── Find latest mutation report ───────────────────────────────────────────────
$mutationFiles = @(Get-ChildItem -LiteralPath $reportsDir -File -Filter "mutation-*.json" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc | Select-Object -Last 1)

if ($mutationFiles.Count -eq 0) {
    Write-Host "[MutationPolicy] No mutation report found. Skipping."
    if ($EmitJson) {
        Write-Output ([PSCustomObject]@{ skipped = $true; reason = "no-mutation-report" } | ConvertTo-Json -Depth 3)
    }
    exit 0
}

$mr             = Get-V2JsonContent -Path $mutationFiles[0].FullName
$overallScore   = [double](Get-V2OptionalProperty -InputObject $mr -Name "mutation_score_pct" -DefaultValue 0)
$mutationsRun   = [int](Get-V2OptionalProperty    -InputObject $mr -Name "mutations_run"       -DefaultValue 0)
$survived       = @(Get-V2OptionalProperty        -InputObject $mr -Name "survived_details"    -DefaultValue @())

if ($mutationsRun -eq 0) {
    Write-Host "[MutationPolicy] Zero mutations run. Nothing to enforce."
    if ($EmitJson) {
        Write-Output ([PSCustomObject]@{ skipped = $true; reason = "zero-mutations-run" } | ConvertTo-Json -Depth 3)
    }
    exit 0
}

Write-Host ("[MutationPolicy] Latest score: {0}% (min: {1}%) | survived: {2}" -f $overallScore, $globalMin, $survived.Count)

# ── Load DAG (only if it exists) ─────────────────────────────────────────────
$dagAvailable = Test-Path -LiteralPath $dagPath -PathType Leaf
$dag          = $null
if ($dagAvailable) {
    try { $dag = Get-V2JsonContent -Path $dagPath }
    catch { $dagAvailable = $false }
}

# ── Helper: check if a COBERTURA-SCORE-FAIL task already open for domain ─────
function Test-OpenScoreFailTask {
    param([string]$DomainKey)
    if (-not $dagAvailable -or $null -eq $dag) { return $false }
    $openTasks = @($dag.tasks | Where-Object {
        $id     = [string](Get-V2OptionalProperty -InputObject $_ -Name "id"     -DefaultValue "")
        $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
        $id -like "COBERTURA-SCORE-FAIL-*-$DomainKey" -and $status -in @("pending", "in-progress")
    })
    return $openTasks.Count -gt 0
}

function Add-ScoreFailTask {
    param(
        [string]$DomainKey,
        [string]$Priority,
        [double]$ActualScore,
        [double]$MinScore,
        [string]$Description
    )

    if (-not $dagAvailable -or $null -eq $dag) { return }
    if (Test-OpenScoreFailTask -DomainKey $DomainKey) {
        Write-Host ("[MutationPolicy] COBERTURA-SCORE-FAIL already open for domain '{0}'. Skipping." -f $DomainKey)
        return
    }

    $taskId = "COBERTURA-SCORE-FAIL-{0}-{1}" -f $ts, $DomainKey

    $dag.tasks += [PSCustomObject]@{
        id             = $taskId
        title          = ("Mutation score below threshold ({0}%/{1}%) in domain: {2}" -f $ActualScore, $MinScore, $DomainKey)
        description    = $Description
        reason         = "mutation-score-policy"
        priority       = $Priority
        dependencies   = @()
        status         = "pending"
        execution_mode = "artifact-validation"
        source_report  = $mutationFiles[0].FullName
        mutation_score = $ActualScore
        min_score      = $MinScore
        domain         = $DomainKey
        created_at     = Get-V2Timestamp
        updated_at     = Get-V2Timestamp
    }

    Write-Host ("[MutationPolicy] Created {0} task: {1} (score={2}% min={3}%)" -f $Priority, $taskId, $ActualScore, $MinScore)
}

# ── Evaluate global threshold ─────────────────────────────────────────────────
$violations = New-Object System.Collections.Generic.List[object]

if ($overallScore -lt $globalMin) {
    Add-ScoreFailTask `
        -DomainKey   "global" `
        -Priority    "P1" `
        -ActualScore $overallScore `
        -MinScore    $globalMin `
        -Description ("Overall mutation score {0}% is below the global minimum of {1}%. Add tests to cover the {2} survived mutation(s)." -f $overallScore, $globalMin, $survived.Count)

    $violations.Add([PSCustomObject]@{ domain = "global"; score = $overallScore; min = $globalMin; priority = "P1" })
}

# ── Evaluate per-domain policies against survived mutations ───────────────────
foreach ($dp in $domainPolicies) {
    $pattern    = [string](Get-V2OptionalProperty -InputObject $dp -Name "pattern"       -DefaultValue "")
    $minScore   = [double](Get-V2OptionalProperty -InputObject $dp -Name "min_score_pct" -DefaultValue $globalMin)
    $dpPriority = [string](Get-V2OptionalProperty -InputObject $dp -Name "priority"      -DefaultValue "P1")
    $domainKey  = ($pattern -replace "[^A-Za-z0-9]", "-").Trim("-")

    if ([string]::IsNullOrWhiteSpace($pattern)) { continue }

    # Find survived mutations that hit files matching this domain pattern
    $domainSurvived = @($survived | Where-Object {
        $file = [string](Get-V2OptionalProperty -InputObject $_ -Name "file" -DefaultValue "")
        $file -match $pattern
    })

    if ($domainSurvived.Count -eq 0) { continue }

    # Compute domain score: mutations that survived / total attempted on domain files
    # Since we only have survived details, use: domain_score = 1 - (survived/total_run)*weight
    # Approximation: if domain has survived mutations, flag as degraded
    $domainScore = if ($domainSurvived.Count -gt 0 -and $mutationsRun -gt 0) {
        [Math]::Round((1.0 - ($domainSurvived.Count / [Math]::Max($mutationsRun, 1))) * 100, 1)
    }
    else { 100.0 }

    if ($domainScore -lt $minScore) {
        $fileList = @($domainSurvived | ForEach-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "file" -DefaultValue "") }) -join ", "
        Add-ScoreFailTask `
            -DomainKey   $domainKey `
            -Priority    $dpPriority `
            -ActualScore $domainScore `
            -MinScore    $minScore `
            -Description ("Domain '{0}' mutation score {1}% < {2}% threshold. {3} survived mutation(s) in: {4}" -f $domainKey, $domainScore, $minScore, $domainSurvived.Count, $fileList)

        $violations.Add([PSCustomObject]@{ domain = $domainKey; score = $domainScore; min = $minScore; priority = $dpPriority })
    }
}

# ── Save updated DAG ──────────────────────────────────────────────────────────
if ($dagAvailable -and $violations.Count -gt 0 -and $null -ne $dag) {
    try {
        Save-V2JsonContent -Path $dagPath -Value $dag
    }
    catch {
        Write-Warning "[MutationPolicy] Could not save task-dag: $($_.Exception.Message)"
    }
}

$result = [PSCustomObject]@{
    generated_at    = Get-V2Timestamp
    project         = Split-Path -Leaf $resolvedPath
    overall_score   = $overallScore
    global_min      = $globalMin
    mutations_run   = $mutationsRun
    survived_count  = $survived.Count
    violations      = @($violations.ToArray())
    violations_count = $violations.Count
}

if ($violations.Count -eq 0) {
    Write-Host ("[MutationPolicy] OK: all domain thresholds met (score={0}%, min={1}%)" -f $overallScore, $globalMin)
}
else {
    Write-Host ("[MutationPolicy] FAIL: {0} domain threshold violation(s)" -f $violations.Count)
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
