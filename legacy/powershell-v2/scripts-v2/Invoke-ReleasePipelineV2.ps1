<#
.SYNOPSIS
    Runs staged release pipeline with smoke checks and optional rollback.
.DESCRIPTION
    Executes build -> test -> deploy -> smoke workflow using verified commands and
    optional release config file:
      ai-orchestrator/release/release-config.json

    Enforces release phase approval before deployment.
.PARAMETER ProjectPath
    Target project root.
.PARAMETER Environment
    staging or production.
.PARAMETER RollbackOnFailure
    If set, executes rollback command on deploy/smoke failure when available.
.PARAMETER DockerConfigMode
    Reserved for compatibility with launcher; currently informational.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [ValidateSet("staging", "production")]
    [string]$Environment = "staging",
    [bool]$RollbackOnFailure = $false,
    [ValidateSet("user", "isolated")]
    [string]$DockerConfigMode = "isolated"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Invoke-ReleaseCommand {
    param(
        [string]$Command,
        [string]$WorkingDirectory,
        [int]$TimeoutSeconds = 900
    )

    if ([string]::IsNullOrWhiteSpace($Command)) {
        return [PSCustomObject]@{
            success   = $false
            command   = $Command
            output    = ""
            exit_code = $null
            reason    = "missing-command"
        }
    }

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $cmdArguments = "/d /s /c `"cd /d `"$WorkingDirectory`" && $Command`""
        $process = Start-Process -FilePath $env:ComSpec `
            -ArgumentList $cmdArguments `
            -WorkingDirectory $WorkingDirectory `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -WindowStyle Hidden

        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            try { $process.Kill() } catch {}
            return [PSCustomObject]@{
                success   = $false
                command   = $Command
                output    = "timeout"
                exit_code = $null
                reason    = "timeout"
            }
        }

        $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        $combined = (@($stdout, $stderr) -join [Environment]::NewLine).Trim()
        $exitCode = [int]$process.ExitCode
        return [PSCustomObject]@{
            success   = ($exitCode -eq 0)
            command   = $Command
            output    = $combined
            exit_code = $exitCode
            reason    = if ($exitCode -eq 0) { "" } else { "exit-code-$exitCode" }
        }
    }
    finally {
        foreach ($file in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $file) {
                Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$statePath = Join-Path $resolvedProjectPath "ai-orchestrator/state/project-state.json"
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw "project-state.json not found: $statePath"
}
$state = Get-V2JsonContent -Path $statePath
if (-not $state) {
    throw "Invalid project-state.json: $statePath"
}
$phaseApprovalsChanged = Initialize-V2PhaseApprovals -ProjectState $state -UpdatedBy "release-pipeline-auto"
if ($phaseApprovalsChanged) {
    Set-V2DynamicProperty -InputObject $state -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $statePath -Value $state
}

$phaseApprovals = Get-V2OptionalProperty -InputObject $state -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
$releaseApproval = Get-V2OptionalProperty -InputObject $phaseApprovals -Name "release" -DefaultValue ([PSCustomObject]@{ status = "pending" })
$releaseApprovalStatus = [string](Get-V2OptionalProperty -InputObject $releaseApproval -Name "status" -DefaultValue "pending")
if ($releaseApprovalStatus -ne "approved") {
    throw "Release blocked: phase approval 'release' is '$releaseApprovalStatus'. Approve with -Action v2-approve-phase -Phase release -ApprovalStatus approved."
}

$verified = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
$buildCommand = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verified -Name "build" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "")
$testCommand = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verified -Name "test" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "")

$releaseConfigPath = Join-Path $resolvedProjectPath "ai-orchestrator/release/release-config.json"
$releaseConfig = Get-V2JsonContent -Path $releaseConfigPath
if (-not $releaseConfig) {
    $releaseConfig = [PSCustomObject]@{
        deploy = [PSCustomObject]@{
            staging = [PSCustomObject]@{ command = ""; smoke_command = ""; rollback_command = "" }
            production = [PSCustomObject]@{ command = ""; smoke_command = ""; rollback_command = "" }
        }
    }
    Initialize-V2Directory -Path (Split-Path -Parent $releaseConfigPath)
    Save-V2JsonContent -Path $releaseConfigPath -Value $releaseConfig
}

$deployConfig = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $releaseConfig -Name "deploy" -DefaultValue ([PSCustomObject]@{})) -Name $Environment -DefaultValue ([PSCustomObject]@{})
$deployCommand = [string](Get-V2OptionalProperty -InputObject $deployConfig -Name "command" -DefaultValue "")
$smokeCommand = [string](Get-V2OptionalProperty -InputObject $deployConfig -Name "smoke_command" -DefaultValue "")
$rollbackCommand = [string](Get-V2OptionalProperty -InputObject $deployConfig -Name "rollback_command" -DefaultValue "")

$results = New-Object System.Collections.Generic.List[object]
$pipelineOk = $true
$failedStep = ""

foreach ($step in @(
    [PSCustomObject]@{ name = "build"; command = $buildCommand },
    [PSCustomObject]@{ name = "test"; command = $testCommand },
    [PSCustomObject]@{ name = "deploy"; command = $deployCommand },
    [PSCustomObject]@{ name = "smoke"; command = $smokeCommand }
)) {
    $isRequired = $step.name -in @("build", "test", "deploy", "smoke")
    if ([string]::IsNullOrWhiteSpace([string]$step.command)) {
        $status = if ($isRequired) { "failed" } else { "skipped" }
        if ($isRequired) {
            $pipelineOk = $false
            $failedStep = $step.name
        }
        $results.Add([PSCustomObject]@{
            step     = $step.name
            status   = $status
            command  = [string]$step.command
            reason   = "missing-command"
            output   = ""
        })
        if (-not $pipelineOk) { break }
        continue
    }

    $execution = Invoke-ReleaseCommand -Command ([string]$step.command) -WorkingDirectory $resolvedProjectPath
    $results.Add([PSCustomObject]@{
        step     = $step.name
        status   = if ($execution.success) { "ok" } else { "failed" }
        command  = $execution.command
        reason   = $execution.reason
        output   = $execution.output
    })
    if (-not $execution.success) {
        $pipelineOk = $false
        $failedStep = $step.name
        break
    }
}

$rollbackResult = $null
if (-not $pipelineOk -and $RollbackOnFailure -and -not [string]::IsNullOrWhiteSpace($rollbackCommand)) {
    $rb = Invoke-ReleaseCommand -Command $rollbackCommand -WorkingDirectory $resolvedProjectPath
    $rollbackResult = [PSCustomObject]@{
        status = if ($rb.success) { "ok" } else { "failed" }
        command = $rb.command
        reason = $rb.reason
        output = $rb.output
    }
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = Join-Path $resolvedProjectPath ("ai-orchestrator/reports/release-report-{0}-{1}.json" -f $Environment, $timestamp)
Initialize-V2Directory -Path (Split-Path -Parent $reportPath)

$report = [PSCustomObject]@{
    generated_at       = Get-V2Timestamp
    environment        = $Environment
    docker_config_mode = $DockerConfigMode
    success            = $pipelineOk
    failed_step        = $failedStep
    rollback_requested = [bool]$RollbackOnFailure
    rollback_result    = $rollbackResult
    steps              = @($results.ToArray())
}
Save-V2JsonContent -Path $reportPath -Value $report

$releaseHistory = @(Get-V2OptionalProperty -InputObject $state -Name "release_history" -DefaultValue @())
$releaseHistory += [PSCustomObject]@{
    at           = Get-V2Timestamp
    environment  = $Environment
    success      = $pipelineOk
    failed_step  = $failedStep
    report_path  = $reportPath
}
Set-V2DynamicProperty -InputObject $state -Name "release_history" -Value @($releaseHistory | Select-Object -Last 50)
Set-V2DynamicProperty -InputObject $state -Name "updated_at" -Value (Get-V2Timestamp)
Save-V2JsonContent -Path $statePath -Value $state

$output = [PSCustomObject]@{
    success      = $pipelineOk
    environment  = $Environment
    failed_step  = $failedStep
    report_path  = $reportPath
}
$output | ConvertTo-Json -Depth 12

if (-not $pipelineOk) {
    throw "Release pipeline failed at step: $failedStep (report: $reportPath)"
}
