<#
.SYNOPSIS
    Root launcher for AI Project Orchestrator actions.
.DESCRIPTION
    Provides a stable entry point from repository root for V2 actions and environment checks.
    This script dispatches to scripts under ./scripts and ./scripts/v2.
.PARAMETER Action
    Action to execute: v2-new, v2-submit, v2-watch, v2-observe, v2-schedule, v2-loop, v2-access, v2-verify,
    v2-clean-project, v2-e2e-round, v2-e2e-batch, v2-agent-dispatch, v2-validate-agents,
    v2-approve-phase, v2-release, v2-sync-tasks, v2-code-read, v2-claim, v2-complete, env-check.
.PARAMETER ProjectPath
    Target project root path for V2 actions.
.PARAMETER ProjectName
    New project name for v2-new.
.PARAMETER DockerConfigMode
    Docker config strategy for V2 actions: isolated (default) or user.
.PARAMETER BatchCases
    Case list for v2-e2e-batch in format <stack>:<database> (for example: python:postgres).
.PARAMETER SkipCoreRegressionGate
    Skips mandatory core regression gate (v2-e2e-batch) for submit/new actions.
.EXAMPLE
    .\orchestrator.ps1 -Action v2-new -ProjectName "med-control"
.EXAMPLE
    .\orchestrator.ps1 -Action v2-submit -ProjectPath "G:\repo\workspace\projects\med-control" -Force
#>
param(
    [ValidateSet("menu", "v2-submit", "v2-new", "v2-watch", "v2-observe", "v2-schedule", "v2-loop", "v2-access", "v2-verify", "v2-clean-project", "v2-e2e-round", "v2-e2e-batch", "v2-agent-dispatch", "v2-validate-agents", "v2-approve-phase", "v2-release", "v2-sync-tasks", "v2-code-read", "v2-claim", "v2-complete", "env-check", "control-plane")]
    [string]$Action = "menu",
    [string]$ProjectPath,
    [string]$ProjectName,
    [string]$ProjectBriefPath,
    [string]$InboxPath,
    [string]$ManagedProjectsRoot,
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "auto",
    [ValidateSet("auto", "postgres", "mysql", "mongodb", "none")]
    [string]$Database = "auto",
    [ValidateSet("unknown", "maintain", "gradual-refactor", "full-refactor")]
    [string]$RefactorPolicy = "unknown",
    [ValidateSet("dedicated-infra", "shared-infra")]
    [string]$InfraMode = "dedicated-infra",
    [ValidateSet("user", "isolated")]
    [string]$DockerConfigMode = "isolated",
    [ValidateSet("auto", "context", "architecture", "execution", "release", "all")]
    [string]$Phase = "context",
    [ValidateSet("approved", "rejected", "pending")]
    [string]$ApprovalStatus = "approved",
    [ValidateSet("staging", "production")]
    [string]$ReleaseEnvironment = "staging",
    [switch]$RollbackOnFailure,
    [string[]]$BatchCases,
    [switch]$SkipCoreRegressionGate,
    [switch]$IncludeRuntimeAgents,
    [switch]$AutoRepairTasks,
    [switch]$FailOnAgentFailure,
    [switch]$FailOnNotReady,
    [int]$PollIntervalSeconds = 10,
    [switch]$IncludeRedis,
    [switch]$IncludeRabbitMq,
    [switch]$IncludeWorker,
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [switch]$SkipMemorySync,
    [switch]$RunOnce,
    [int]$MaxCycles = 0,
    [switch]$Force,
    [switch]$ForceConfirmed,
    [string]$TaskId = "",
    [string]$AgentName = "",
    [string]$Artifacts = "",
    [string]$Notes = "",
    [string]$CompletionPayloadPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-V2 {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\v2\Invoke-UniversalOrchestratorV2.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "V2 orchestrator script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2Loop {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\v2\Invoke-AutonomousLoopV2.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "V2 loop script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-EnvCheck {
    $scriptPath = Join-Path $PSScriptRoot "scripts\Check-Environment.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Environment check script not found: $scriptPath"
    }
    & $scriptPath
}

function Invoke-V2Verify {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\Verify-OrchestratorCore.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Core verification script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2E2ERound {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\v2-e2e-round.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "V2 E2E round script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2E2EBatch {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\v2-e2e-batch.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "V2 E2E batch script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2AgentDispatch {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\Invoke-AgentDispatcherV2.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Agent dispatcher script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2AgentValidation {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\Validate-AgentArtifactsV2.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Agent validation script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2PhaseApproval {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\Set-PhaseApproval.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Phase approval script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2Release {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\Invoke-ReleasePipelineV2.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Release pipeline script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Invoke-V2TaskSync {
    param([string]$TargetProjectPath)

    $taskSyncScript = Join-Path $PSScriptRoot "scripts\\Sync-TaskState.ps1"
    $lockSyncScript = Join-Path $PSScriptRoot "scripts\\Sync-LockState.ps1"
    foreach ($scriptPath in @($taskSyncScript, $lockSyncScript)) {
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
            throw "Sync script not found: $scriptPath"
        }
        & $scriptPath -ProjectPath $TargetProjectPath | Out-Null
    }
}

function Invoke-V2CodeReader {
    param([hashtable]$Arguments)
    $scriptPath = Join-Path $PSScriptRoot "scripts\\v2\\Invoke-CodeReaderAgent.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "CodeReader agent script not found: $scriptPath"
    }
    & $scriptPath @Arguments
}

function Get-V2CoreGateFingerprint {
    param([string[]]$Files)

    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($file in @($Files)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
        $parts.Add(("{0}|{1}" -f $file, $hash))
    }
    if ($parts.Count -eq 0) { return "" }
    $joined = ($parts -join "`n")
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Invoke-V2CoreRegressionGateIfNeeded {
    param(
        [string]$ActionName,
        [string]$InfraModeValue,
        [string]$DockerConfigModeValue
    )

    if ($SkipCoreRegressionGate) {
        # Hard guard: skip is only allowed as emergency bypass.
        if ([string]$env:V2_ALLOW_SKIP_CORE_GATE -ne "1") {
            throw "SkipCoreRegressionGate blocked. Set V2_ALLOW_SKIP_CORE_GATE=1 for emergency-only bypass."
        }
        Write-Host "Core regression gate skipped by emergency override (V2_ALLOW_SKIP_CORE_GATE=1)." -ForegroundColor Yellow
        return
    }

    if ([string]$env:V2_CORE_GATE_IN_PROGRESS -eq "1") {
        return
    }

    $coreFiles = @(
        (Join-Path $PSScriptRoot "orchestrator.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-UniversalOrchestratorV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-ObserverV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-SchedulerV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-RuntimeObservabilityV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-Orchestrator360DecisionEngine.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-AgentDispatcherV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Validate-AgentArtifactsV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Invoke-ReleasePipelineV2.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\Set-PhaseApproval.ps1"),
        (Join-Path $PSScriptRoot "scripts\\Run-AgentLoop.ps1"),
        (Join-Path $PSScriptRoot "scripts\\memory_sync.py"),
        (Join-Path $PSScriptRoot "scripts\\v2\\v2-e2e-round.ps1"),
        (Join-Path $PSScriptRoot "scripts\\v2\\v2-e2e-batch.ps1")
    )
    $fingerprint = Get-V2CoreGateFingerprint -Files $coreFiles
    if ([string]::IsNullOrWhiteSpace($fingerprint)) {
        throw "core-regression-gate-failed: unable to compute core fingerprint."
    }

    $gateStatePath = Join-Path $PSScriptRoot "workspace\\tmp\\_batch-reports\\core-regression-gate.json"
    $gateState = $null
    if (Test-Path -LiteralPath $gateStatePath -PathType Leaf) {
        try { $gateState = Get-Content -LiteralPath $gateStatePath -Raw | ConvertFrom-Json } catch { $gateState = $null }
    }

    $stateFingerprint = if ($gateState) { [string]$gateState.fingerprint } else { "" }
    $stateVerdict = if ($gateState) { [string]$gateState.verdict } else { "" }
    if ($stateFingerprint -eq $fingerprint -and $stateVerdict -eq "READY") {
        return
    }

    Write-Host ("Core regression gate triggered before {0} (fingerprint changed)." -f $ActionName) -ForegroundColor Yellow
    $batchName = "core-gate-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    $batchArgs = @{
        BatchName          = $batchName
        ManagedProjectsRoot = (Join-Path $PSScriptRoot "workspace\\tmp")
        Cases              = @("python:postgres", "node:postgres", "php:mysql")
        ValidationProfile  = "core-smoke"
        InfraMode          = $InfraModeValue
        DockerConfigMode   = $DockerConfigModeValue
        IncludeNeo4j       = $true
        IncludeQdrant      = $true
    }
    if ($IncludeWorker) { $batchArgs.IncludeWorker = $true }
    if ($IncludeRedis) { $batchArgs.IncludeRedis = $true }
    if ($IncludeRabbitMq) { $batchArgs.IncludeRabbitMq = $true }

    $prevGateFlag = [string]$env:V2_CORE_GATE_IN_PROGRESS
    $env:V2_CORE_GATE_IN_PROGRESS = "1"
    try {
        Invoke-V2E2EBatch -Arguments $batchArgs
    }
    finally {
        if ([string]::IsNullOrWhiteSpace($prevGateFlag)) {
            Remove-Item Env:V2_CORE_GATE_IN_PROGRESS -ErrorAction SilentlyContinue
        }
        else {
            $env:V2_CORE_GATE_IN_PROGRESS = $prevGateFlag
        }
    }

    $stateDirectory = Split-Path -Parent $gateStatePath
    if (-not (Test-Path -LiteralPath $stateDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    }
    [PSCustomObject]@{
        checked_at   = (Get-Date).ToString("s")
        action       = $ActionName
        fingerprint  = $fingerprint
        verdict      = "READY"
        batch_name   = $batchName
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $gateStatePath -Encoding UTF8
}

if ($Action -eq "menu") {
    Write-Host "AI Project Orchestrator" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "[1] V2 submit project"
    Write-Host "[2] V2 create new project"
    Write-Host "[3] V2 watch inbox"
    Write-Host "[4] V2 observe project"
    Write-Host "[5] V2 run scheduler once"
    Write-Host "[6] V2 run autonomous loop"
    Write-Host "[7] V2 show access"
    Write-Host "[8] V2 verify core"
    Write-Host "[9] Check environment"
    Write-Host "[10] V2 clean project resources"
    Write-Host "[11] V2 E2E round (new project regression)"
    Write-Host "[12] V2 E2E batch (multi-stack regression)"
    Write-Host "[13] V2 phase approval (context/architecture/execution/release)"
    Write-Host "[14] V2 release pipeline (staging/production)"
    Write-Host "[15] V2 agent dispatch (registry-driven)"
    Write-Host "[16] V2 validate agent artifacts (READY/NOT READY)"
    Write-Host ""
    $choice = Read-Host "Choose an option"
    switch ($choice) {
        "1" { $Action = "v2-submit" }
        "2" { $Action = "v2-new" }
        "3" { $Action = "v2-watch" }
        "4" { $Action = "v2-observe" }
        "5" { $Action = "v2-schedule" }
        "6" { $Action = "v2-loop" }
        "7" { $Action = "v2-access" }
        "8" { $Action = "v2-verify" }
        "9" { $Action = "env-check" }
        "10" { $Action = "v2-clean-project" }
        "11" { $Action = "v2-e2e-round" }
        "12" { $Action = "v2-e2e-batch" }
        "13" { $Action = "v2-approve-phase" }
        "14" { $Action = "v2-release" }
        "15" { $Action = "v2-agent-dispatch" }
        "16" { $Action = "v2-validate-agents" }
        default { throw "Invalid menu option." }
    }
}

switch ($Action) {
    "env-check" {
        Invoke-EnvCheck
        break
    }
    "v2-loop" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-loop."
        }
        $args = @{
            ProjectPath     = $ProjectPath
            IntervalSeconds = if ($PollIntervalSeconds -gt 0) { $PollIntervalSeconds } else { 10 }
        }
        if ($MaxCycles -gt 0) { $args.MaxCycles = $MaxCycles }
        if ($RunOnce) { $args.RunOnce = $true }
        if ($IncludeRuntimeAgents) { $args.IncludeRuntimeAgentsInDispatch = $true }
        if ($SkipMemorySync) { $args.SkipMemorySync = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        Invoke-V2Loop -Arguments $args
        break
    }
    "v2-submit" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-submit."
        }
        if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
            throw "ProjectPath does not exist for v2-submit: $ProjectPath"
        }
        Invoke-V2CoreRegressionGateIfNeeded -ActionName "v2-submit" -InfraModeValue $InfraMode -DockerConfigModeValue $DockerConfigMode
        $args = @{
            Mode         = "submit"
            ProjectPath  = $ProjectPath
            Stack        = $Stack
            Database     = $Database
            RefactorPolicy = $RefactorPolicy
            InfraMode    = $InfraMode
            DockerConfigMode = $DockerConfigMode
        }
        if (-not [string]::IsNullOrWhiteSpace($ProjectBriefPath)) { $args.ProjectBriefPath = $ProjectBriefPath }
        if (-not [string]::IsNullOrWhiteSpace($ManagedProjectsRoot)) { $args.ManagedProjectsRoot = $ManagedProjectsRoot }
        if ($IncludeRedis) { $args.IncludeRedis = $true }
        if ($IncludeRabbitMq) { $args.IncludeRabbitMq = $true }
        if ($IncludeWorker) { $args.IncludeWorker = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        if ($Force) { $args.Force = $true }
        Invoke-V2 -Arguments $args
        break
    }
    "v2-new" {
        if ([string]::IsNullOrWhiteSpace($ProjectName)) {
            throw "ProjectName is required for v2-new."
        }
        Invoke-V2CoreRegressionGateIfNeeded -ActionName "v2-new" -InfraModeValue $InfraMode -DockerConfigModeValue $DockerConfigMode
        $args = @{
            Mode         = "new"
            ProjectName  = $ProjectName
            Stack        = $Stack
            Database     = $Database
            RefactorPolicy = $RefactorPolicy
            InfraMode    = $InfraMode
            DockerConfigMode = $DockerConfigMode
        }
        if (-not [string]::IsNullOrWhiteSpace($ProjectBriefPath)) { $args.ProjectBriefPath = $ProjectBriefPath }
        if (-not [string]::IsNullOrWhiteSpace($ManagedProjectsRoot)) { $args.ManagedProjectsRoot = $ManagedProjectsRoot }
        if ($IncludeRedis) { $args.IncludeRedis = $true }
        if ($IncludeRabbitMq) { $args.IncludeRabbitMq = $true }
        if ($IncludeWorker) { $args.IncludeWorker = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        if ($Force) { $args.Force = $true }
        if ($ForceConfirmed) { $args.ForceConfirmed = $true }
        Invoke-V2 -Arguments $args
        break
    }
    "v2-watch" {
        $args = @{
            Mode = "watch"
            PollIntervalSeconds = if ($PollIntervalSeconds -gt 0) { $PollIntervalSeconds } else { 10 }
            Stack = $Stack
            Database = $Database
            RefactorPolicy = $RefactorPolicy
            InfraMode = $InfraMode
            DockerConfigMode = $DockerConfigMode
        }
        if (-not [string]::IsNullOrWhiteSpace($InboxPath)) { $args.InboxPath = $InboxPath }
        if (-not [string]::IsNullOrWhiteSpace($ManagedProjectsRoot)) { $args.ManagedProjectsRoot = $ManagedProjectsRoot }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        if ($RunOnce) { $args.RunOnce = $true }
        Invoke-V2 -Arguments $args
        break
    }
    "v2-observe" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-observe."
        }
        $args = @{
            Mode        = "observe"
            ProjectPath = $ProjectPath
            DockerConfigMode = $DockerConfigMode
        }
        if ($RunOnce) { $args.RunOnce = $true }
        if ($SkipMemorySync) { $args.SkipMemorySync = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        Invoke-V2 -Arguments $args
        break
    }
    "v2-schedule" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-schedule."
        }
        Invoke-V2 -Arguments @{
            Mode        = "schedule"
            ProjectPath = $ProjectPath
            DockerConfigMode = $DockerConfigMode
        }
        break
    }
    "v2-access" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-access."
        }
        Invoke-V2 -Arguments @{
            Mode        = "access"
            ProjectPath = $ProjectPath
            DockerConfigMode = $DockerConfigMode
        }
        break
    }
    "v2-verify" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-verify."
        }
        Invoke-V2Verify -Arguments @{
            ProjectPath = $ProjectPath
        }
        break
    }
    "v2-clean-project" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-clean-project."
        }
        $args = @{
            Mode       = "clean"
            ProjectPath = $ProjectPath
            InfraMode  = $InfraMode
            DockerConfigMode = $DockerConfigMode
        }
        if ($Force) { $args.Force = $true }
        Invoke-V2 -Arguments $args
        break
    }
    "v2-e2e-round" {
        $args = @{
            ProjectName        = if ([string]::IsNullOrWhiteSpace($ProjectName)) { ("e2e-round-" + (Get-Date -Format "yyyyMMdd-HHmmss")) } else { $ProjectName }
            ManagedProjectsRoot = if ([string]::IsNullOrWhiteSpace($ManagedProjectsRoot)) { (Join-Path $PSScriptRoot "workspace\\tmp") } else { $ManagedProjectsRoot }
            Stack              = $Stack
            Database           = $Database
            InfraMode          = $InfraMode
            DockerConfigMode   = $DockerConfigMode
        }
        if ($IncludeRedis) { $args.IncludeRedis = $true }
        if ($IncludeRabbitMq) { $args.IncludeRabbitMq = $true }
        if ($IncludeWorker) { $args.IncludeWorker = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        if ($Force) { $args.ReuseExistingProject = $true }
        Invoke-V2E2ERound -Arguments $args
        break
    }
    "v2-e2e-batch" {
        $defaultCases = @("python:postgres", "node:postgres", "php:mysql")
        $args = @{
            BatchName          = if ([string]::IsNullOrWhiteSpace($ProjectName)) { ("e2e-batch-" + (Get-Date -Format "yyyyMMdd-HHmmss")) } else { $ProjectName }
            ManagedProjectsRoot = if ([string]::IsNullOrWhiteSpace($ManagedProjectsRoot)) { (Join-Path $PSScriptRoot "workspace\\tmp") } else { $ManagedProjectsRoot }
            InfraMode          = $InfraMode
            DockerConfigMode   = $DockerConfigMode
            Cases              = if ($BatchCases -and $BatchCases.Count -gt 0) { $BatchCases } else { $defaultCases }
        }
        if ($IncludeRedis) { $args.IncludeRedis = $true }
        if ($IncludeRabbitMq) { $args.IncludeRabbitMq = $true }
        if ($IncludeWorker) { $args.IncludeWorker = $true }
        if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { $args.IncludeNeo4j = [bool]$IncludeNeo4j }
        if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { $args.IncludeQdrant = [bool]$IncludeQdrant }
        if ($Force) { $args.ReuseExistingProjects = $true }
        Invoke-V2E2EBatch -Arguments $args
        break
    }
    "v2-agent-dispatch" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-agent-dispatch."
        }
        $args = @{
            ProjectPath = $ProjectPath
            Phase       = $Phase
            EmitJson    = $true
        }
        if ($IncludeRuntimeAgents) { $args.IncludeRuntimeAgents = $true }
        if ($AutoRepairTasks) { $args.AutoRepairTasks = $true }
        if ($FailOnAgentFailure) { $args.FailOnAgentFailure = $true }
        Invoke-V2AgentDispatch -Arguments $args
        break
    }
    "v2-validate-agents" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-validate-agents."
        }
        $args = @{
            ProjectPath = $ProjectPath
            Phase       = $Phase
            EmitJson    = $true
        }
        if ($AutoRepairTasks) { $args.AutoRepairTasks = $true }
        if ($FailOnNotReady) { $args.FailOnNotReady = $true }
        Invoke-V2AgentValidation -Arguments $args
        break
    }
    "v2-approve-phase" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-approve-phase."
        }
        if ($Phase -in @("auto", "all")) {
            throw "Phase for v2-approve-phase must be one of: context, architecture, execution, release."
        }
        Invoke-V2PhaseApproval -Arguments @{
            ProjectPath = $ProjectPath
            Phase       = $Phase
            Status      = $ApprovalStatus
        }
        break
    }
    "v2-release" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-release."
        }
        Invoke-V2CoreRegressionGateIfNeeded -ActionName "v2-release" -InfraModeValue $InfraMode -DockerConfigModeValue $DockerConfigMode
        Invoke-V2Release -Arguments @{
            ProjectPath         = $ProjectPath
            Environment         = $ReleaseEnvironment
            RollbackOnFailure   = [bool]$RollbackOnFailure
            DockerConfigMode    = $DockerConfigMode
        }
        break
    }
    "v2-sync-tasks" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-sync-tasks."
        }
        Invoke-V2TaskSync -TargetProjectPath $ProjectPath
        break
    }
    "v2-code-read" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-code-read."
        }
        $args = @{
            ProjectPath = $ProjectPath
        }
        if ($Force) { $args.Force = $true }
        Invoke-V2CodeReader -Arguments $args
        break
    }
    "v2-claim" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-claim."
        }
        if ([string]::IsNullOrWhiteSpace($TaskId)) {
            throw "TaskId is required for v2-claim."
        }
        if ([string]::IsNullOrWhiteSpace($AgentName)) {
            throw "AgentName is required for v2-claim."
        }
        Invoke-V2 -Arguments @{
            Mode        = "claim"
            ProjectPath = $ProjectPath
            TaskId      = $TaskId
            AgentName   = $AgentName
            DockerConfigMode = $DockerConfigMode
        }
        break
    }
    "v2-complete" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for v2-complete."
        }
        if ([string]::IsNullOrWhiteSpace($TaskId)) {
            throw "TaskId is required for v2-complete."
        }
        if ([string]::IsNullOrWhiteSpace($AgentName)) {
            throw "AgentName is required for v2-complete."
        }
        $args = @{
            Mode        = "complete"
            ProjectPath = $ProjectPath
            TaskId      = $TaskId
            AgentName   = $AgentName
            DockerConfigMode = $DockerConfigMode
        }
        if (-not [string]::IsNullOrWhiteSpace($Artifacts)) { $args.Artifacts = $Artifacts }
        if (-not [string]::IsNullOrWhiteSpace($Notes)) { $args.Notes = $Notes }
        if (-not [string]::IsNullOrWhiteSpace($CompletionPayloadPath)) { $args.CompletionPayloadPath = $CompletionPayloadPath }
        Invoke-V2 -Arguments $args
        break
    }
    "control-plane" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required for control-plane."
        }
        $cpPort    = if ($PSBoundParameters.ContainsKey("PollIntervalSeconds") -and $PollIntervalSeconds -gt 0) { $PollIntervalSeconds } else { 8080 }
        $cpScript  = Join-Path $PSScriptRoot "scripts\Start-ControlPlane.py"
        if (-not (Test-Path -LiteralPath $cpScript -PathType Leaf)) {
            throw "Control plane script not found: $cpScript"
        }
        Write-Host "Starting Control Plane at http://127.0.0.1:$cpPort ..." -ForegroundColor Cyan
        & python $cpScript --project-path $ProjectPath --port $cpPort
        break
    }
    default {
        throw "Unsupported action: $Action"
    }
}
