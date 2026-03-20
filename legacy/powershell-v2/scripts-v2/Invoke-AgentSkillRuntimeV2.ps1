<#
.SYNOPSIS
    Executes deterministic skill runtimes for Orchestrator 360 agents.
.DESCRIPTION
    Provides concrete runtime execution for AG-11, AG-12, AG-13, AG-16, AG-17, and AG-18.
    Writes normalized reports so dispatcher and artifact validation can treat these
    agents as first-class operational units.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER AgentId
    Agent identifier (AG-11, AG-12, AG-13, AG-16, AG-17, AG-18).
.PARAMETER EmitJson
    Emits machine-readable JSON output.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [Parameter(Mandatory = $true)]
    [ValidateSet("AG-11", "AG-12", "AG-13", "AG-16", "AG-17", "AG-18")]
    [string]$AgentId,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2ScriptRepoRoot {
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
}

function Add-V2StepResult {
    param(
        [System.Collections.Generic.List[object]]$Steps,
        [string]$Name,
        [bool]$Success,
        [string]$Details
    )

    $Steps.Add([PSCustomObject]@{
        name    = $Name
        success = $Success
        details = $Details
    })
}

function Invoke-V2ScriptJson {
    param(
        [string]$ScriptPath,
        [hashtable]$Arguments
    )

    if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
        throw "script-not-found:$ScriptPath"
    }
    $raw = & $ScriptPath @Arguments 2>&1 | Out-String
    $parsed = $null
    try {
        $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        $parsed = $null
    }
    return [PSCustomObject]@{
        raw    = $raw
        parsed = $parsed
    }
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$reportsDir = Join-Path $orchestratorRoot "reports"
$stateDir = Join-Path $orchestratorRoot "state"
$analysisDir = Join-Path $orchestratorRoot "analysis"
$skillReportsDir = Join-Path $reportsDir "agent-skills"
Initialize-V2Directory -Path $reportsDir
Initialize-V2Directory -Path $stateDir
Initialize-V2Directory -Path $analysisDir
Initialize-V2Directory -Path $skillReportsDir

$repoRoot = Get-V2ScriptRepoRoot
$steps = New-Object System.Collections.Generic.List[object]
$warnings = New-Object System.Collections.Generic.List[string]
$errors = New-Object System.Collections.Generic.List[string]
$artifacts = New-Object System.Collections.Generic.List[string]
$success = $true
$agentReportPath = ""

try {
    switch ($AgentId) {
        "AG-11" {
            $scanScript = Join-Path $repoRoot "scripts/Run-OwaspSecurityScan.ps1"
            $scan = Invoke-V2ScriptJson -ScriptPath $scanScript -Arguments @{
                ProjectPath = $projectRoot
                EmitJson = $true
            }
            Add-V2StepResult -Steps $steps -Name "owasp-scan" -Success $true -Details "Security scan executed."

            $securityState = "ai-orchestrator/state/security-scan.json"
            $securityReport = "ai-orchestrator/analysis/security-report.md"
            if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $securityState) -PathType Leaf)) {
                $warnings.Add("security-scan-state-missing")
                $success = $false
            }
            if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $securityReport) -PathType Leaf)) {
                $warnings.Add("security-scan-report-missing")
                $success = $false
            }
            $artifacts.Add($securityState)
            $artifacts.Add($securityReport)

            $agentReportPath = Join-Path $reportsDir "security-agent-runtime.json"
            $report = [PSCustomObject]@{
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $success
                warnings = @($warnings.ToArray())
                scan = $scan.parsed
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
        }
        "AG-12" {
            $healthPath = Join-Path $stateDir "health-report.json"
            $obsPath = Join-Path $reportsDir "runtime-observability-report.json"
            $readinessPath = Join-Path $reportsDir "readiness-latest.json"

            $health = if (Test-Path -LiteralPath $healthPath -PathType Leaf) { Get-V2JsonContent -Path $healthPath } else { $null }
            $obs = if (Test-Path -LiteralPath $obsPath -PathType Leaf) { Get-V2JsonContent -Path $obsPath } else { $null }
            $readiness = if (Test-Path -LiteralPath $readinessPath -PathType Leaf) { Get-V2JsonContent -Path $readinessPath } else { $null }

            if (-not $health) { $warnings.Add("health-report-missing") }
            if (-not $obs) { $warnings.Add("runtime-observability-report-missing") }
            if (-not $readiness) { $warnings.Add("readiness-latest-missing") }

            Add-V2StepResult -Steps $steps -Name "performance-snapshot" -Success $true -Details "Collected performance-adjacent telemetry from health, observability, and readiness."

            $agentReportPath = Join-Path $reportsDir "performance-agent-report.json"
            $report = [PSCustomObject]@{
                schema_version = "v1-performance-agent"
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $true
                source_reports = [PSCustomObject]@{
                    health = if ($health) { "ai-orchestrator/state/health-report.json" } else { "" }
                    observability = if ($obs) { "ai-orchestrator/reports/runtime-observability-report.json" } else { "" }
                    readiness = if ($readiness) { "ai-orchestrator/reports/readiness-latest.json" } else { "" }
                }
                snapshot = [PSCustomObject]@{
                    health_status = [string](Get-V2OptionalProperty -InputObject $health -Name "health_status" -DefaultValue "unknown")
                    readiness_verdict = [string](Get-V2OptionalProperty -InputObject $readiness -Name "verdict" -DefaultValue "unknown")
                    open_repairs = [int](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $readiness -Name "summary" -DefaultValue ([PSCustomObject]@{})) -Name "open_repairs" -DefaultValue 0)
                    incidents = @((Get-V2OptionalProperty -InputObject $obs -Name "incidents" -DefaultValue @())).Count
                }
                warnings = @($warnings.ToArray())
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
        }
        "AG-13" {
            $testRunnerPath = Join-Path $repoRoot "scripts/v2/Invoke-TestRunnerV2.ps1"
            $runnerSuccess = $true
            $runner = $null
            try {
                $runner = Invoke-V2ScriptJson -ScriptPath $testRunnerPath -Arguments @{
                    ProjectPath = $projectRoot
                    EmitJson = $true
                }
            }
            catch {
                $runnerSuccess = $false
                $warnings.Add(("test-runner-failed:{0}" -f $_.Exception.Message))
            }

            Add-V2StepResult -Steps $steps -Name "qa-test-runner" -Success $runnerSuccess -Details "Executed QA test runner."

            $agentReportPath = Join-Path $reportsDir "qa-agent-report.json"
            $report = [PSCustomObject]@{
                schema_version = "v1-qa-agent"
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $runnerSuccess
                warnings = @($warnings.ToArray())
                test_runner = if ($runner) { $runner.parsed } else { $null }
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
        }
        "AG-16" {
            $incidentsDir = Join-Path $reportsDir "incidents"
            $incidentFiles = @()
            if (Test-Path -LiteralPath $incidentsDir -PathType Container) {
                $incidentFiles = @(Get-ChildItem -LiteralPath $incidentsDir -Filter "*.json" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
            }

            $items = New-Object System.Collections.Generic.List[object]
            foreach ($file in @($incidentFiles | Select-Object -First 25)) {
                $doc = $null
                try {
                    $doc = Get-V2JsonContent -Path $file.FullName
                }
                catch {
                    continue
                }
                $items.Add([PSCustomObject]@{
                    file = Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $file.FullName
                    category = [string](Get-V2OptionalProperty -InputObject $doc -Name "category" -DefaultValue "")
                    severity = [string](Get-V2OptionalProperty -InputObject $doc -Name "severity" -DefaultValue "")
                    created_at = [string](Get-V2OptionalProperty -InputObject $doc -Name "created_at" -DefaultValue "")
                    status = [string](Get-V2OptionalProperty -InputObject $doc -Name "status" -DefaultValue "open")
                })
            }

            Add-V2StepResult -Steps $steps -Name "incident-summary" -Success $true -Details ("Summarized {0} incident report(s)." -f $items.Count)

            $agentReportPath = Join-Path $reportsDir "incident-response-summary.json"
            $report = [PSCustomObject]@{
                schema_version = "v1-incident-response"
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $true
                incidents_total = $items.Count
                incidents = @($items.ToArray())
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
        }
        "AG-17" {
            $crossProjectScript = Join-Path $repoRoot "scripts/v2/Invoke-CrossProjectMemorySync.ps1"
            $sync = Invoke-V2ScriptJson -ScriptPath $crossProjectScript -Arguments @{
                ProjectPath = $projectRoot
                EmitJson = $true
            }

            Add-V2StepResult -Steps $steps -Name "cross-project-memory-sync" -Success $true -Details "Cross-project memory sync executed."

            $agentReportPath = Join-Path $reportsDir "memory-agent-report.json"
            $report = [PSCustomObject]@{
                schema_version = "v1-memory-agent"
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $true
                sync_summary = $sync.parsed
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
            $artifacts.Add("ai-orchestrator/memory/world-model.md")
        }
        "AG-18" {
            $promoteScript = Join-Path $repoRoot "scripts/v2/Invoke-PromotePatterns.ps1"
            $promotion = Invoke-V2ScriptJson -ScriptPath $promoteScript -Arguments @{
                ProjectPath = $projectRoot
            }

            Add-V2StepResult -Steps $steps -Name "promote-patterns" -Success $true -Details "Pattern promotion scan executed."

            $agentReportPath = Join-Path $reportsDir "learning-agent-report.json"
            $report = [PSCustomObject]@{
                schema_version = "v1-learning-agent"
                generated_at = Get-V2Timestamp
                agent_id = $AgentId
                success = $true
                promotion = $promotion.parsed
            }
            Save-V2JsonContent -Path $agentReportPath -Value $report
            $artifacts.Add((Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $agentReportPath))
        }
    }
}
catch {
    $success = $false
    $errors.Add($_.Exception.Message)
    Add-V2StepResult -Steps $steps -Name "execution" -Success $false -Details $_.Exception.Message
}

$summaryPath = Join-Path $skillReportsDir ("{0}.json" -f $AgentId.ToLowerInvariant())
$summary = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    agent_id = $AgentId
    success = $success
    steps = @($steps.ToArray())
    artifacts = @($artifacts.ToArray())
    warnings = @($warnings.ToArray())
    errors = @($errors.ToArray())
}
Save-V2JsonContent -Path $summaryPath -Value $summary

$result = [PSCustomObject]@{
    success = $success
    agent_id = $AgentId
    report = Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $summaryPath
    artifacts = @($artifacts.ToArray())
    warnings = @($warnings.ToArray())
    errors = @($errors.ToArray())
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 12)
}
else {
    Write-Output ("Agent skill runtime complete: agent={0} success={1}" -f $AgentId, $success)
}
