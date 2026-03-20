<#
.SYNOPSIS
    V2 autonomous loop for observe -> schedule -> execute cycles.
.DESCRIPTION
    Runs the orchestration lifecycle in order:
      1) Observer health pass (optional)
      2) Scheduler assignment pass (optional)
      3) Agent dispatch pass (optional)
      4) Agent execution pass (optional)
      5) Agent artifact validation pass (optional)
    Writes loop heartbeat to ai-orchestrator/state/loop-state.json.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER IntervalSeconds
    Wait interval between cycles.
.PARAMETER MaxCycles
    Maximum cycles. 0 means run until interrupted.
.PARAMETER RunOnce
    Shorthand for one cycle.
.PARAMETER SkipObserver
    Skip observer step.
.PARAMETER SkipScheduler
    Skip scheduler step.
.PARAMETER SkipAgentRuntime
    Skip executor step.
.PARAMETER SkipAgentDispatch
    Skip registry-driven agent dispatch step.
.PARAMETER SkipAgentValidation
    Skip agent artifact validation step.
.PARAMETER DispatchPhase
    Phase used by agent dispatcher (auto by default).
.PARAMETER IncludeRuntimeAgentsInDispatch
    Allows dispatcher to trigger runtime-agent handlers.
.PARAMETER FailOnValidationNotReady
    Stops loop cycle when artifact validation verdict is NOT READY.
.PARAMETER SkipMemorySync
    Skip observer memory sync step. Default is false (memory sync enabled).
.PARAMETER IncludeNeo4j
    Force Neo4j sync on/off for observer step. If omitted, uses project-state include_neo4j.
.PARAMETER IncludeQdrant
    Force Qdrant sync on/off for observer step. If omitted, uses project-state include_qdrant.
.PARAMETER AgentName
    Agent identity used by Run-AgentLoop.ps1.
.PARAMETER MaxTasksPerCycle
    Maximum tasks the agent runtime executes per cycle.
.PARAMETER IdlePendingTakeoverMinutes
    Minutes before a pending assigned task can be reassigned to an idle agent.
.PARAMETER IdleInProgressTakeoverMinutes
    Minutes before an in-progress stale task can be requeued for takeover.
.PARAMETER AgentRuntimeEngine
    Runtime execution engine:
      powershell - only Run-AgentLoop.ps1
      python     - native Python cognitive runtime with PowerShell fallback
      hybrid     - Python for native tasks + PowerShell for external/manual tasks
.PARAMETER PythonExecutable
    Python binary used for native runtime (default: python).
.PARAMETER VerboseOutput
    Print child script output.
#>
param(
    [string]$ProjectPath = ".",
    [int]$IntervalSeconds = 300,
    [int]$MaxCycles = 0,
    [switch]$RunOnce,
    [switch]$SkipObserver,
    [switch]$SkipScheduler,
    [switch]$SkipAgentRuntime,
    [switch]$SkipAgentDispatch,
    [switch]$SkipAgentValidation,
    [ValidateSet("auto", "context", "architecture", "execution", "release", "all")]
    [string]$DispatchPhase = "auto",
    [switch]$IncludeRuntimeAgentsInDispatch,
    [switch]$FailOnValidationNotReady,
    [switch]$SkipMemorySync,
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [string]$AgentName = "Codex",
    [bool]$RunAllAssignedAgents = $true,
    [int]$MaxTasksPerCycle = 3,
    [int]$IdlePendingTakeoverMinutes = 10,
    [int]$IdleInProgressTakeoverMinutes = 30,
    [int]$MutationCadenceCycles = 12,
    [bool]$MutationRunOnFingerprintChange = $true,
    [switch]$AllowConcurrentLoopOverride,
    [bool]$BootstrapLocalLlmGpu = $true,
    [ValidateSet("powershell", "python", "hybrid")]
    [string]$AgentRuntimeEngine = "hybrid",
    [string]$PythonExecutable = "python",
    [switch]$VerboseOutput
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-loop"

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$observerPath       = Join-Path $PSScriptRoot "Invoke-ObserverV2.ps1"
$policyEnforcerPath = Join-Path $PSScriptRoot "Invoke-PolicyEnforcer.ps1"
$schedulerPath      = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
$agentLoopPath      = Join-Path (Split-Path -Parent $PSScriptRoot) "Run-AgentLoop.ps1"
$pythonAgentLoopPath = Join-Path $PSScriptRoot "core_llm/Run-AgentLoop.py"
$agentDispatchPath  = Join-Path $PSScriptRoot "Invoke-AgentDispatcherV2.ps1"
$agentValidationPath = Join-Path $PSScriptRoot "Validate-AgentArtifactsV2.ps1"
$scriptValidationPath = Join-Path $PSScriptRoot "Invoke-ScriptValidationGate.ps1"
$promotePatternPath  = Join-Path $PSScriptRoot "Invoke-PromotePatterns.ps1"
$testRunnerPath      = Join-Path $PSScriptRoot "Invoke-TestRunnerV2.ps1"
$deployVerifyPath    = Join-Path $PSScriptRoot "Invoke-DeployVerificationV2.ps1"
$codeReaderPath      = Join-Path $PSScriptRoot "Invoke-CodeReaderAgent.ps1"
$finOpsPath          = Join-Path $PSScriptRoot "Invoke-FinOpsMonitorV2.ps1"
$mutationTestPath    = Join-Path $PSScriptRoot "Invoke-MutationTestingV2.ps1"
$dashboardPath       = Join-Path (Split-Path -Parent $PSScriptRoot) "Update-Dashboard.ps1"
$delegationBusPath        = Join-Path $PSScriptRoot "Invoke-DelegationBus.ps1"
$localAgentExecutorPath   = Join-Path $PSScriptRoot "Invoke-LocalAgentExecutorV2.ps1"
$streamBroadcastPath = Join-Path $PSScriptRoot "Invoke-StreamingBroadcast.ps1"
$crossProjectMemorySyncPath = Join-Path $PSScriptRoot "Invoke-CrossProjectMemorySync.ps1"
$metaCalibrationPath = Join-Path $PSScriptRoot "Invoke-MetaSchedulerCalibration.ps1"
$runtimeObservabilityPath = Join-Path $PSScriptRoot "Invoke-RuntimeObservabilityV2.ps1"
$whiteboardPath           = Join-Path $PSScriptRoot "Invoke-WhiteboardV2.ps1"
$mutationPolicyPath       = Join-Path $PSScriptRoot "Invoke-MutationPolicyEnforcerV2.ps1"
$projectAggregationPath   = Join-Path $PSScriptRoot "Invoke-ProjectAggregation.ps1"
$readinessReportPath      = Join-Path $PSScriptRoot "Invoke-ReadinessReportV2.ps1"
$taskProjectionPath       = Join-Path $PSScriptRoot "sync_ai_system_projection.py"

$taskDagPath        = Join-Path $orchestratorRoot "tasks/task-dag.json"
$loopStatePath      = Join-Path $orchestratorRoot "state/loop-state.json"
$loopStepEventsPath = Join-Path $orchestratorRoot "state/loop-step-events.jsonl"
$toolUsageLogPath   = Join-Path $orchestratorRoot "state/tool-usage-log.jsonl"
$projectStatePath   = Join-Path $orchestratorRoot "state/project-state.json"
$architectureDocPath = Join-Path $orchestratorRoot "documentation/architecture.md"

if (-not (Test-Path -LiteralPath $observerPath -PathType Leaf)) {
    throw "Observer script not found: $observerPath"
}
if (-not (Test-Path -LiteralPath $schedulerPath -PathType Leaf)) {
    throw "Scheduler script not found: $schedulerPath"
}
if (-not (Test-Path -LiteralPath $agentLoopPath -PathType Leaf)) {
    throw "Agent runtime script not found: $agentLoopPath"
}
if ($AgentRuntimeEngine -in @("python", "hybrid")) {
    if (-not (Test-Path -LiteralPath $pythonAgentLoopPath -PathType Leaf)) {
        Write-Warning "Python runtime script not found: $pythonAgentLoopPath. Falling back to PowerShell runtime."
        $AgentRuntimeEngine = "powershell"
    }
}
if (-not $SkipAgentDispatch -and -not (Test-Path -LiteralPath $agentDispatchPath -PathType Leaf)) {
    throw "Agent dispatcher script not found: $agentDispatchPath"
}
if (-not $SkipAgentValidation -and -not (Test-Path -LiteralPath $agentValidationPath -PathType Leaf)) {
    throw "Agent validation script not found: $agentValidationPath"
}

if ($RunOnce) {
    $MaxCycles = 1
}

function Get-V2LoopEnvInt {
    param(
        [string]$Name,
        [int]$DefaultValue,
        [int]$MinValue = 0,
        [int]$MaxValue = [int]::MaxValue
    )

    $envEntry = Get-Item -Path ("Env:{0}" -f $Name) -ErrorAction SilentlyContinue
    if (-not $envEntry -or [string]::IsNullOrWhiteSpace($envEntry.Value)) {
        return $DefaultValue
    }

    $parsed = 0
    if (-not [int]::TryParse([string]$envEntry.Value, [ref]$parsed)) {
        return $DefaultValue
    }

    if ($parsed -lt $MinValue) {
        return $MinValue
    }
    if ($parsed -gt $MaxValue) {
        return $MaxValue
    }
    return $parsed
}

$envLoopIntervalSeconds = Get-V2LoopEnvInt -Name "ORCHESTRATOR_LOOP_INTERVAL_SECONDS" -DefaultValue 0 -MinValue 0
if (-not $PSBoundParameters.ContainsKey("IntervalSeconds") -and $envLoopIntervalSeconds -gt 0) {
    $IntervalSeconds = $envLoopIntervalSeconds
}

if (-not $RunOnce -and -not $PSBoundParameters.ContainsKey("MaxCycles")) {
    $envMaxCycles = Get-V2LoopEnvInt -Name "ORCHESTRATOR_LOOP_MAX_CYCLES" -DefaultValue 0 -MinValue 0
    $MaxCycles = $envMaxCycles
}

$observerEveryCycles = Get-V2LoopEnvInt -Name "ORCHESTRATOR_OBSERVER_EVERY_CYCLES" -DefaultValue 1 -MinValue 1
$observerMemorySyncEveryCycles = Get-V2LoopEnvInt -Name "ORCHESTRATOR_OBSERVER_MEMORY_SYNC_EVERY_CYCLES" -DefaultValue 1 -MinValue 1
$observerCommandTimeoutSeconds = Get-V2LoopEnvInt -Name "ORCHESTRATOR_OBSERVER_COMMAND_TIMEOUT_SECONDS" -DefaultValue 300 -MinValue 30
$aiSystemProjectionEveryCycles = Get-V2LoopEnvInt -Name "ORCHESTRATOR_AI_SYSTEM_PROJECTION_EVERY_CYCLES" -DefaultValue 1 -MinValue 1

$stateForBackends = Get-V2JsonContent -Path $projectStatePath
$loopIncludeNeo4j = if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) {
    [bool]$IncludeNeo4j
}
else {
    [bool](Get-V2OptionalProperty -InputObject $stateForBackends -Name "include_neo4j" -DefaultValue $true)
}
$loopIncludeQdrant = if ($PSBoundParameters.ContainsKey("IncludeQdrant")) {
    [bool]$IncludeQdrant
}
else {
    [bool](Get-V2OptionalProperty -InputObject $stateForBackends -Name "include_qdrant" -DefaultValue $true)
}

$policyEnforcerEveryCycles  = Get-V2LoopEnvInt -Name "ORCHESTRATOR_POLICY_ENFORCER_EVERY_CYCLES" -DefaultValue 1 -MinValue 1
$aggregationEveryCycles     = Get-V2LoopEnvInt -Name "ORCHESTRATOR_AGGREGATION_EVERY_CYCLES" -DefaultValue 5 -MinValue 1

function Get-V2LoopMutationFingerprint {
    param([string]$ProjectRoot)

    $healthPath = Join-Path $ProjectRoot "ai-orchestrator/state/health-report.json"
    if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
        try {
            $health = Get-V2JsonContent -Path $healthPath
            $fingerprint = [string](Get-V2OptionalProperty -InputObject $health -Name "fingerprint" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($fingerprint)) {
                return $fingerprint.Trim()
            }
        }
        catch {}
    }

    $projectState = Join-Path $ProjectRoot "ai-orchestrator/state/project-state.json"
    if (Test-Path -LiteralPath $projectState -PathType Leaf) {
        try {
            $state = Get-V2JsonContent -Path $projectState
            $fallbackFingerprint = [string](Get-V2OptionalProperty -InputObject $state -Name "fingerprint" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($fallbackFingerprint)) {
                return $fallbackFingerprint.Trim()
            }
        }
        catch {}
    }

    return ""
}

function Ensure-V2LocalLlmRuntime {
    param(
        [string]$PythonBin,
        [string]$ProjectRoot = ""
    )

    $result = [PSCustomObject]@{
        configured = $false
        litellm_installed = $false
        model = ""
        api_base = ""
        gpu_vram_reserve_mb = 0
        ollama_gpu_overhead = ""
        error = ""
    }

    try {
        $projectEnvMap = @{}
        if (-not [string]::IsNullOrWhiteSpace($ProjectRoot)) {
            $dockerEnvPath = Join-Path $ProjectRoot "ai-orchestrator/docker/.env.docker.generated"
            if (Test-Path -LiteralPath $dockerEnvPath -PathType Leaf) {
                foreach ($line in Get-Content -LiteralPath $dockerEnvPath -ErrorAction SilentlyContinue) {
                    $text = [string]$line
                    if ([string]::IsNullOrWhiteSpace($text)) { continue }
                    if ($text.TrimStart().StartsWith("#")) { continue }
                    $parts = $text.Split("=", 2)
                    if ($parts.Count -ne 2) { continue }
                    $key = $parts[0].Trim()
                    if ([string]::IsNullOrWhiteSpace($key)) { continue }
                    $projectEnvMap[$key] = $parts[1].Trim()
                }
            }
        }

        $projectApiBase = ""
        foreach ($k in @("ORCHESTRATOR_LLM_API_BASE", "OLLAMA_API_BASE")) {
            if ($projectEnvMap.ContainsKey($k) -and -not [string]::IsNullOrWhiteSpace([string]$projectEnvMap[$k])) {
                $projectApiBase = [string]$projectEnvMap[$k]
                break
            }
        }
        if ([string]::IsNullOrWhiteSpace($projectApiBase) -and $projectEnvMap.ContainsKey("OLLAMA_HOST_PORT")) {
            $parsedPort = 0
            if ([int]::TryParse([string]$projectEnvMap["OLLAMA_HOST_PORT"], [ref]$parsedPort) -and $parsedPort -gt 0) {
                $projectApiBase = "http://127.0.0.1:$parsedPort"
            }
        }

        $projectModel = ""
        foreach ($k in @("ORCHESTRATOR_LLM_MODEL", "ORCHESTRATOR_LLM_MODEL_FAST")) {
            if ($projectEnvMap.ContainsKey($k) -and -not [string]::IsNullOrWhiteSpace([string]$projectEnvMap[$k])) {
                $projectModel = [string]$projectEnvMap[$k]
                break
            }
        }

        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_ENABLED)) {
            $env:ORCHESTRATOR_LLM_ENABLED = "1"
        }
        if (-not [string]::IsNullOrWhiteSpace($projectModel)) {
            # Project-local model settings take precedence to avoid cross-project drift.
            $env:ORCHESTRATOR_LLM_MODEL = $projectModel
        }
        elseif ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_MODEL)) {
            $env:ORCHESTRATOR_LLM_MODEL = "ollama/llama3:8b"
        }
        if (-not [string]::IsNullOrWhiteSpace($projectApiBase)) {
            # Always pin API base from the current project environment when available.
            $env:ORCHESTRATOR_LLM_API_BASE = $projectApiBase
        }
        elseif ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_API_BASE)) {
            $env:ORCHESTRATOR_LLM_API_BASE = "http://127.0.0.1:11435"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_API_KEY)) {
            $env:ORCHESTRATOR_LLM_API_KEY = "ollama"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_ROUTING_ENABLED)) {
            $env:ORCHESTRATOR_LLM_ROUTING_ENABLED = "1"
        }
        if ([string]::IsNullOrWhiteSpace($env:LITELLM_LOCAL_MODEL_COST_MAP)) {
            # Keep LiteLLM fully offline-safe: no remote model_cost_map fetch during loop.
            $env:LITELLM_LOCAL_MODEL_COST_MAP = "true"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_ROUTING_COMPLEXITY_THRESHOLD)) {
            # Higher threshold keeps more work on the low-latency local fast model.
            $env:ORCHESTRATOR_LLM_ROUTING_COMPLEXITY_THRESHOLD = "12"
        }
        if (-not [string]::IsNullOrWhiteSpace($projectModel)) {
            $env:ORCHESTRATOR_LLM_MODEL_FAST = $projectModel
        }
        elseif ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_LLM_MODEL_FAST)) {
            $env:ORCHESTRATOR_LLM_MODEL_FAST = $env:ORCHESTRATOR_LLM_MODEL
        }

        # Hardware-aware scheduler profile tuned for RTX 5070:
        # aggressively prefer native-GPU runtime for medium-complexity tasks.
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_GPU_ROUTING_ENABLED)) {
            $env:ORCHESTRATOR_GPU_ROUTING_ENABLED = "1"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_GPU_ROUTING_COMPLEXITY_THRESHOLD)) {
            $env:ORCHESTRATOR_GPU_ROUTING_COMPLEXITY_THRESHOLD = "15"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_GPU_ROUTING_MAX_FILES)) {
            $env:ORCHESTRATOR_GPU_ROUTING_MAX_FILES = "30"
        }
        if ([string]::IsNullOrWhiteSpace($env:ORCHESTRATOR_GPU_ROUTING_MAX_DEPENDENCIES)) {
            $env:ORCHESTRATOR_GPU_ROUTING_MAX_DEPENDENCIES = "10"
        }
        $env:OLLAMA_API_BASE = $env:ORCHESTRATOR_LLM_API_BASE
        if ([string]::IsNullOrWhiteSpace($env:OLLAMA_API_KEY)) {
            $env:OLLAMA_API_KEY = $env:ORCHESTRATOR_LLM_API_KEY
        }

        $gpuGuard = Set-V2OllamaGpuGuardEnv
        $result.gpu_vram_reserve_mb = [int](Get-V2OptionalProperty -InputObject $gpuGuard -Name "reserve_mb" -DefaultValue 0)
        $result.ollama_gpu_overhead = [string](Get-V2OptionalProperty -InputObject $gpuGuard -Name "gpu_overhead_bytes" -DefaultValue "")

        $result.model = [string]$env:ORCHESTRATOR_LLM_MODEL
        $result.api_base = [string]$env:ORCHESTRATOR_LLM_API_BASE
        $result.configured = $true
    }
    catch {
        $result.error = "env-bootstrap-failed: $($_.Exception.Message)"
        return $result
    }

    function Invoke-V2PythonQuiet {
        param(
            [string]$Exe,
            [string[]]$Arguments
        )

        $exitCode = 1
        $errorPref = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $Exe @Arguments *> $null
            $exitCode = [int]$LASTEXITCODE
        }
        catch {
            $exitCode = 1
        }
        finally {
            $ErrorActionPreference = $errorPref
        }
        return $exitCode
    }

    $checkImportArgs = @("-c", "import litellm")
    $importExit = Invoke-V2PythonQuiet -Exe $PythonBin -Arguments $checkImportArgs
    if ($importExit -eq 0) {
        $result.litellm_installed = $true
        return $result
    }

    try {
        $installExit = Invoke-V2PythonQuiet -Exe $PythonBin -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "litellm")
        if ($installExit -ne 0) {
            # Debian/Ubuntu containers with PEP 668 may require explicit opt-in for system package installs.
            $installExit = Invoke-V2PythonQuiet -Exe $PythonBin -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "--break-system-packages", "litellm")
        }
        if ($installExit -eq 0) {
            $recheckExit = Invoke-V2PythonQuiet -Exe $PythonBin -Arguments $checkImportArgs
            if ($recheckExit -eq 0) {
                $result.litellm_installed = $true
                return $result
            }
        }
        $result.error = "litellm-install-failed: pip-exit-$installExit"
    }
    catch {
        $result.error = "litellm-install-failed: $($_.Exception.Message)"
    }

    return $result
}

function Get-V2LoopMutexName {
    param([string]$ProjectRoot)

    $normalized = [string]$ProjectRoot
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return "Global\OrchestratorV2Loop_default"
    }

    $lower = $normalized.ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($lower)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
        $hash = ([BitConverter]::ToString($hashBytes)).Replace("-", "").Substring(0, 24)
        return ("Global\OrchestratorV2Loop_{0}" -f $hash)
    }
    finally {
        $sha.Dispose()
    }
}

function Invoke-V2LoopStep {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [hashtable]$Arguments
    )

    $stepStartedAt = Get-Date
    Write-V2LoopStepState -StepName $Name -StepStatus "running" -StepStartedAt $stepStartedAt
    Write-V2StreamEvent -EventType "loop-step-running" -Message ("Step {0} started." -f $Name) -Payload ([PSCustomObject]@{
            cycle = $script:loopCurrentCycle
            step = $Name
            started_at = $stepStartedAt.ToString("o")
        })
    Write-Host ("  -> {0}..." -f $Name) -NoNewline
    try {
        $stepTimeoutSeconds = 0
        if ($Arguments -and $Arguments.ContainsKey("CommandTimeoutSeconds")) {
            try {
                $stepTimeoutSeconds = [int]$Arguments["CommandTimeoutSeconds"]
            }
            catch {
                $stepTimeoutSeconds = 0
            }
        }

        $output = $null
        if ($stepTimeoutSeconds -gt 0) {
            $job = Start-Job -ScriptBlock {
                param(
                    [string]$InnerScriptPath,
                    [hashtable]$InnerArguments
                )
                & $InnerScriptPath @InnerArguments 2>&1
            } -ArgumentList $ScriptPath, $Arguments

            try {
                $completed = Wait-Job -Job $job -Timeout $stepTimeoutSeconds
                if (-not $completed) {
                    Stop-Job -Job $job -Force -ErrorAction SilentlyContinue
                    throw ("step-timeout-{0}s" -f $stepTimeoutSeconds)
                }
                $output = Receive-Job -Job $job -ErrorAction Stop
            }
            finally {
                Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
            }
        }
        else {
            $output = & $ScriptPath @Arguments 2>&1
        }
        if ($VerboseOutput -and $output) {
            Write-Host ""
            foreach ($line in @($output)) {
                Write-Host ("     {0}" -f $line)
            }
        }
        else {
            Write-Host " ok"
        }
        $stepFinishedAt = Get-Date
        Write-V2LoopStepEvent -StepName $Name -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2LoopStepState -StepName $Name -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2StreamEvent -EventType "loop-step-ok" -Message ("Step {0} completed." -f $Name) -Payload ([PSCustomObject]@{
                cycle = $script:loopCurrentCycle
                step = $Name
                finished_at = $stepFinishedAt.ToString("o")
            })
        return $true
    }
    catch {
        Write-Host (" failed ({0})" -f $_.Exception.Message)
        $stepFinishedAt = Get-Date
        Write-V2LoopStepEvent -StepName $Name -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $_.Exception.Message
        Write-V2LoopStepState -StepName $Name -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $_.Exception.Message
        Write-V2StreamEvent -EventType "loop-step-failed" -Level "error" -Message ("Step {0} failed: {1}" -f $Name, $_.Exception.Message) -Payload ([PSCustomObject]@{
                cycle = $script:loopCurrentCycle
                step = $Name
                error = $_.Exception.Message
                finished_at = $stepFinishedAt.ToString("o")
            })
        return $false
    }
}

function ConvertFrom-V2JsonOutput {
    param([string]$RawOutput)

    if ([string]::IsNullOrWhiteSpace($RawOutput)) {
        return $null
    }

    $lines = @($RawOutput -split "(`r`n|`n|`r)" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -eq 0) {
        return $null
    }

    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        $line = [string]$lines[$i]
        try {
            return ($line | ConvertFrom-Json -ErrorAction Stop)
        }
        catch {
        }
    }

    return $null
}

function Invoke-V2PythonAgentRuntimeStep {
    param(
        [string]$AgentForRuntime
    )

    $stepName = ("AgentRuntimePy[{0}]" -f $AgentForRuntime)
    $stepStartedAt = Get-Date
    Write-V2LoopStepState -StepName $stepName -StepStatus "running" -StepStartedAt $stepStartedAt
    Write-V2StreamEvent -EventType "loop-step-running" -Message ("Step {0} started." -f $stepName) -Payload ([PSCustomObject]@{
            cycle = $script:loopCurrentCycle
            step = $stepName
            started_at = $stepStartedAt.ToString("o")
        })
    Write-Host ("  -> {0}..." -f $stepName) -NoNewline

    $rawOutput = ""
    $exitCode = 0
    $errorPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $args = @(
            $pythonAgentLoopPath,
            "--project_path", $resolvedProjectPath,
            "--agent_name", $AgentForRuntime,
            "--max_tasks_per_cycle", [string]$MaxTasksPerCycle,
            "--python_executable", $PythonExecutable,
            "--emit_json"
        )
        $rawOutput = (& $PythonExecutable @args 2>&1 | Out-String)
        $exitCode = [int]$LASTEXITCODE
    }
    catch {
        $exitCode = 1
        $rawOutput = $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $errorPref
    }

    if ($VerboseOutput -and -not [string]::IsNullOrWhiteSpace($rawOutput)) {
        Write-Host ""
        foreach ($line in @($rawOutput -split "(`r`n|`n|`r)")) {
            if (-not [string]::IsNullOrWhiteSpace($line)) {
                Write-Host ("     {0}" -f $line)
            }
        }
    }

    $parsed = ConvertFrom-V2JsonOutput -RawOutput $rawOutput
    $succeeded = ($exitCode -eq 0)
    if ($parsed -and ($parsed.PSObject.Properties.Name -contains "success")) {
        $succeeded = $succeeded -and [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false)
    }

    if ($succeeded) {
        Write-Host " ok"
        $stepFinishedAt = Get-Date
        Write-V2LoopStepEvent -StepName $stepName -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2LoopStepState -StepName $stepName -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2StreamEvent -EventType "loop-step-ok" -Message ("Step {0} completed." -f $stepName) -Payload ([PSCustomObject]@{
                cycle = $script:loopCurrentCycle
                step = $stepName
                finished_at = $stepFinishedAt.ToString("o")
            })
        
        $executedCount = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "executed_tasks" -DefaultValue 0) } else { 0 }
        if ($executedCount -gt 0) {
            Write-V2StreamEvent -EventType "task_completed" -Message ("Agent {0} completed {1} task(s)." -f $AgentForRuntime, $executedCount) -AgentForEvent $AgentForRuntime -Payload $parsed
        }
    }
    else {
        $errorText = if ($parsed) {
            [string](Get-V2OptionalProperty -InputObject $parsed -Name "error" -DefaultValue $rawOutput)
        }
        else {
            [string]$rawOutput
        }
        if ($errorText.Length -gt 400) {
            $errorText = $errorText.Substring(0, 400) + "..."
        }
        Write-Host (" failed ({0})" -f $errorText)
        $stepFinishedAt = Get-Date
        Write-V2LoopStepEvent -StepName $stepName -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $errorText
        Write-V2LoopStepState -StepName $stepName -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $errorText
        Write-V2StreamEvent -EventType "loop-step-failed" -Level "error" -Message ("Step {0} failed: {1}" -f $stepName, $errorText) -Payload ([PSCustomObject]@{
                cycle = $script:loopCurrentCycle
                step = $stepName
                error = $errorText
                finished_at = $stepFinishedAt.ToString("o")
            })
    }

    return [PSCustomObject]@{
        success = $succeeded
        exit_code = $exitCode
        executed_tasks = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "executed_tasks" -DefaultValue 0) } else { 0 }
        skipped_non_native = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "skipped_non_native" -DefaultValue 0) } else { 0 }
        skipped_no_llm = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "skipped_no_llm" -DefaultValue 0) } else { 0 }
        llm_enabled = if ($parsed) { [bool](Get-V2OptionalProperty -InputObject $parsed -Name "llm_enabled" -DefaultValue $false) } else { $false }
        llm_model = if ($parsed) { [string](Get-V2OptionalProperty -InputObject $parsed -Name "llm_model" -DefaultValue "") } else { "" }
        llm_calls = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_calls" -DefaultValue 0) } else { 0 }
        llm_total_tokens = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_total_tokens" -DefaultValue 0) } else { 0 }
        llm_tokens_per_second = if ($parsed) { [double](Get-V2OptionalProperty -InputObject $parsed -Name "llm_tokens_per_second" -DefaultValue 0.0) } else { 0.0 }
        llm_models_used = if ($parsed) { @((Get-V2OptionalProperty -InputObject $parsed -Name "llm_models_used" -DefaultValue @())) } else { @() }
        llm_route_fast = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_route_fast" -DefaultValue 0) } else { 0 }
        llm_route_heavy = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_route_heavy" -DefaultValue 0) } else { 0 }
        llm_route_infra_default = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_route_infra_default" -DefaultValue 0) } else { 0 }
        llm_route_routing_disabled = if ($parsed) { [int](Get-V2OptionalProperty -InputObject $parsed -Name "llm_route_routing_disabled" -DefaultValue 0) } else { 0 }
        raw_output = [string]$rawOutput
    }
}

function Invoke-V2PythonUtilityStep {
    param(
        [string]$StepName,
        [string]$PythonScriptPath,
        [string[]]$PythonArguments = @()
    )

    if (-not (Test-Path -LiteralPath $PythonScriptPath -PathType Leaf)) {
        return $false
    }

    $stepStartedAt = Get-Date
    Write-V2LoopStepState -StepName $StepName -StepStatus "running" -StepStartedAt $stepStartedAt
    Write-V2StreamEvent -EventType "loop-step-running" -Message ("Step {0} started." -f $StepName) -Payload ([PSCustomObject]@{
            cycle = $script:loopCurrentCycle
            step = $StepName
            started_at = $stepStartedAt.ToString("o")
        })
    Write-Host ("  -> {0}..." -f $StepName) -NoNewline

    $rawOutput = ""
    $exitCode = 0
    $errorPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $rawOutput = (& $PythonExecutable $PythonScriptPath @PythonArguments 2>&1 | Out-String)
        $exitCode = [int]$LASTEXITCODE
    }
    catch {
        $exitCode = 1
        $rawOutput = $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $errorPref
    }

    $parsed = ConvertFrom-V2JsonOutput -RawOutput $rawOutput
    $succeeded = ($exitCode -eq 0)
    if ($parsed -and ($parsed.PSObject.Properties.Name -contains "success")) {
        $succeeded = $succeeded -and [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false)
    }

    if ($succeeded) {
        Write-Host " ok"
        $stepFinishedAt = Get-Date
        Write-V2LoopStepEvent -StepName $StepName -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2LoopStepState -StepName $StepName -StepStatus "ok" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt
        Write-V2StreamEvent -EventType "loop-step-ok" -Message ("Step {0} completed." -f $StepName) -Payload ([PSCustomObject]@{
                cycle = $script:loopCurrentCycle
                step = $StepName
                finished_at = $stepFinishedAt.ToString("o")
            })
        return $true
    }

    $errorText = if ($parsed) {
        [string](Get-V2OptionalProperty -InputObject $parsed -Name "error" -DefaultValue $rawOutput)
    }
    else {
        [string]$rawOutput
    }
    if ($errorText.Length -gt 400) {
        $errorText = $errorText.Substring(0, 400) + "..."
    }
    Write-Host (" failed ({0})" -f $errorText)
    $stepFinishedAt = Get-Date
    Write-V2LoopStepEvent -StepName $StepName -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $errorText
    Write-V2LoopStepState -StepName $StepName -StepStatus "failed" -StepStartedAt $stepStartedAt -StepFinishedAt $stepFinishedAt -StepError $errorText
    Write-V2StreamEvent -EventType "loop-step-failed" -Level "error" -Message ("Step {0} failed: {1}" -f $StepName, $errorText) -Payload ([PSCustomObject]@{
            cycle = $script:loopCurrentCycle
            step = $StepName
            error = $errorText
            finished_at = $stepFinishedAt.ToString("o")
        })
    return $false
}

function Write-V2StreamEvent {
    param(
        [string]$EventType,
        [string]$Message,
        [ValidateSet("debug", "info", "warn", "error")]
        [string]$Level = "info",
        [string]$TaskId = "",
        [string]$AgentForEvent = "",
        [object]$Payload = $null
    )

    if (-not (Test-Path -LiteralPath $streamBroadcastPath -PathType Leaf)) {
        return
    }

    $payloadJson = ""
    if ($null -ne $Payload) {
        try {
            $payloadJson = ($Payload | ConvertTo-Json -Depth 10 -Compress)
        }
        catch {
            $payloadJson = ""
        }
    }

    try {
        & $streamBroadcastPath `
            -ProjectPath $resolvedProjectPath `
            -EventType $EventType `
            -Level $Level `
            -Source "autonomous-loop" `
            -Message $Message `
            -TaskId $TaskId `
            -AgentName $AgentForEvent `
            -PayloadJson $payloadJson | Out-Null
    }
    catch {
    }
}

function Write-V2LoopStepState {
    param(
        [string]$StepName,
        [ValidateSet("running", "ok", "failed")]
        [string]$StepStatus,
        [datetime]$StepStartedAt,
        [Nullable[datetime]]$StepFinishedAt = $null,
        [string]$StepError = ""
    )

    $lastStep = [PSCustomObject]@{
        name        = $StepName
        status      = $StepStatus
        started_at  = $StepStartedAt.ToString("o")
        finished_at = if ($null -ne $StepFinishedAt) { $StepFinishedAt.ToString("o") } else { "" }
        error       = $StepError
    }

    $checkpointSummary = Get-V2LatestTaskCheckpointSummary -ProjectRoot $resolvedProjectPath

    $snapshot = [PSCustomObject]@{
        generated_at      = Get-V2Timestamp
        loop_running      = $true
        pid               = $script:loopCurrentPid
        cycle             = $script:loopCurrentCycle
        cumulative_cycles = $script:loopCumulativeCyclesBase + $script:loopCurrentCycle
        run_count         = $script:loopRunCount
        interval_seconds  = $IntervalSeconds
        max_cycles        = $MaxCycles
        agent_name        = $AgentName
        agent_runtime_engine = $AgentRuntimeEngine
        started_at        = $script:loopStartedAt.ToString("o")
        mode              = "observe-schedule-execute"
        mutation_cadence_cycles = $MutationCadenceCycles
        mutation_run_on_fingerprint_change = [bool]$MutationRunOnFingerprintChange
        mutation_last_cycle = $script:lastMutationCycle
        mutation_last_fingerprint = $script:lastMutationFingerprint
        mutation_last_reason = $script:lastMutationReason
        llm_runtime = [PSCustomObject]@{
            configured = [bool]$script:llmRuntimeConfigured
            litellm_installed = [bool]$script:llmRuntimeLiteLlmInstalled
            model = [string]$script:llmRuntimeModel
            api_base = [string]$script:llmRuntimeApiBase
            bootstrap_error = [string]$script:llmRuntimeBootstrapError
        }
        last_python_runtime_metrics = $script:lastPythonRuntimeMetrics
        last_step         = $lastStep
        last_checkpoint_step = $checkpointSummary
    }
    Save-V2JsonContent -Path $loopStatePath -Value $snapshot
}

function Get-V2LoopAgentFromStepName {
    param([string]$StepName)

    $normalized = [string]$StepName
    if ($normalized -match '^AgentRuntimePy\[(.+?)\]$') {
        return [string]$matches[1]
    }
    if ($normalized -match '^AgentRuntime\[(.+?)\]$') {
        return [string]$matches[1]
    }
    return "orchestrator-loop"
}

function Write-V2ToolUsageEvent {
    param(
        [string]$StepName,
        [ValidateSet("ok", "failed")]
        [string]$StepStatus,
        [datetime]$StepStartedAt,
        [datetime]$StepFinishedAt,
        [string]$StepError = ""
    )

    try {
        $logDir = Split-Path -Parent $toolUsageLogPath
        if (-not [string]::IsNullOrWhiteSpace($logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }

        $agentForEvent = Get-V2LoopAgentFromStepName -StepName $StepName
        $durationMs = [int][Math]::Max(0, [Math]::Round((($StepFinishedAt - $StepStartedAt).TotalMilliseconds), 0))
        $event = [PSCustomObject]@{
            timestamp   = Get-V2Timestamp
            source      = "loop-step-events"
            agent_name  = $agentForEvent
            role        = $StepName
            tool        = $StepName
            success     = ($StepStatus -eq "ok")
            duration_ms = $durationMs
            error       = [string]$StepError
            cycle       = $script:loopCurrentCycle
            run_count   = $script:loopRunCount
        }
        Add-Content -LiteralPath $toolUsageLogPath -Value (($event | ConvertTo-Json -Depth 8 -Compress))
    }
    catch {
    }
}

function Write-V2LoopStepEvent {
    param(
        [string]$StepName,
        [ValidateSet("ok", "failed")]
        [string]$StepStatus,
        [datetime]$StepStartedAt,
        [datetime]$StepFinishedAt,
        [string]$StepError = ""
    )

    $durationMs = [Math]::Round((($StepFinishedAt - $StepStartedAt).TotalMilliseconds), 1)
    $stepEvent = [PSCustomObject]@{
        timestamp        = Get-V2Timestamp
        pid              = $script:loopCurrentPid
        run_count        = $script:loopRunCount
        cycle            = $script:loopCurrentCycle
        step             = $StepName
        status           = $StepStatus
        duration_ms      = $durationMs
        started_at       = $StepStartedAt.ToString("o")
        finished_at      = $StepFinishedAt.ToString("o")
        error            = $StepError
    }
    $line = ($stepEvent | ConvertTo-Json -Depth 5 -Compress)
    Add-Content -LiteralPath $loopStepEventsPath -Value $line
    Write-V2ToolUsageEvent -StepName $StepName -StepStatus $StepStatus -StepStartedAt $StepStartedAt -StepFinishedAt $StepFinishedAt -StepError $StepError
}

function Get-V2LatestTaskCheckpointSummary {
    param([string]$ProjectRoot)

    $checkpointDir = Join-Path $ProjectRoot "ai-orchestrator/tasks/checkpoints"
    if (-not (Test-Path -LiteralPath $checkpointDir -PathType Container)) {
        return $null
    }

    # Only surface checkpoint state for currently active execution tasks.
    # This avoids stale "failed" checkpoint pointers after all tasks are done.
    $dagPath = Join-Path $ProjectRoot "ai-orchestrator/tasks/task-dag.json"
    $dagDoc = Get-V2JsonContent -Path $dagPath
    if (-not $dagDoc -or -not ($dagDoc.PSObject.Properties.Name -contains "tasks")) {
        return $null
    }

    $activeTaskIds = @{}
    foreach ($task in @($dagDoc.tasks)) {
        $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
        if ($taskStatus -ne "in-progress") {
            continue
        }

        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($taskId)) {
            $activeTaskIds[$taskId] = $true
        }
    }

    if ($activeTaskIds.Count -eq 0) {
        return $null
    }

    $checkpointFiles = @(Get-ChildItem -LiteralPath $checkpointDir -File -Filter "step-*.json" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc, Name -Descending)
    foreach ($file in $checkpointFiles) {
        $doc = Get-V2JsonContent -Path $file.FullName
        if (-not $doc) {
            continue
        }

        $checkpointTaskId = [string](Get-V2OptionalProperty -InputObject $doc -Name "task_id" -DefaultValue "")
        if (-not $activeTaskIds.ContainsKey($checkpointTaskId)) {
            continue
        }

        return [PSCustomObject]@{
            task_id     = $checkpointTaskId
            step_number = [int](Get-V2OptionalProperty -InputObject $doc -Name "step_number" -DefaultValue 0)
            step_name   = [string](Get-V2OptionalProperty -InputObject $doc -Name "step_name" -DefaultValue "")
            status      = [string](Get-V2OptionalProperty -InputObject $doc -Name "status" -DefaultValue "")
            updated_at  = [string](Get-V2OptionalProperty -InputObject $doc -Name "updated_at" -DefaultValue "")
            file        = [string]$file.FullName
        }
    }

    return $null
}

function Test-V2ArchitectureStub {
    param([string]$ArchitecturePath)

    if (-not (Test-Path -LiteralPath $ArchitecturePath -PathType Leaf)) {
        return $true
    }

    $content = Get-Content -LiteralPath $ArchitecturePath -Raw -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($content)) {
        return $true
    }

    $lineCount = @(($content -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
    if ($lineCount -le 25) {
        return $true
    }

    return ($content -match "## Runtime Contracts" -and $content -match "`task-dag.json` is canonical task state")
}

$cycle        = 0
$startedAt    = Get-Date
$currentPid   = [System.Diagnostics.Process]::GetCurrentProcess().Id
$loopMutexName = Get-V2LoopMutexName -ProjectRoot $resolvedProjectPath
$loopMutex = $null
$loopMutexHeld = $false

# -------------------------------------------------------------------------------
$priorState       = Get-V2JsonContent -Path $loopStatePath
$cumulativeCycles = 0
$runCount         = 1
$lastMutationCycle = 0
$lastMutationFingerprint = ""
$lastMutationReason = ""
if ($priorState) {
    $cumulativeCycles = [int](Get-V2OptionalProperty -InputObject $priorState -Name "cumulative_cycles" -DefaultValue 0)
    $runCount         = [int](Get-V2OptionalProperty -InputObject $priorState -Name "run_count"         -DefaultValue 0) + 1
    $lastMutationCycle = [int](Get-V2OptionalProperty -InputObject $priorState -Name "mutation_last_cycle" -DefaultValue 0)
    $lastMutationFingerprint = [string](Get-V2OptionalProperty -InputObject $priorState -Name "mutation_last_fingerprint" -DefaultValue "")
    $lastMutationReason = [string](Get-V2OptionalProperty -InputObject $priorState -Name "mutation_last_reason" -DefaultValue "")

    # Detect potentially running duplicate instance
    $priorRunning   = [bool](Get-V2OptionalProperty -InputObject $priorState -Name "loop_running"    -DefaultValue $false)
    $priorPid       = [int](Get-V2OptionalProperty  -InputObject $priorState -Name "pid"             -DefaultValue 0)
    $priorHeartbeat = [string](Get-V2OptionalProperty -InputObject $priorState -Name "generated_at"  -DefaultValue "")
    if ($priorRunning -and $priorPid -gt 0 -and $priorPid -ne $currentPid) {
        $staleThresholdSeconds = [Math]::Max($IntervalSeconds * 3, 120)
        $priorPidAlive = $false
        try {
            $priorPidAlive = $null -ne (Get-Process -Id $priorPid -ErrorAction SilentlyContinue)
        }
        catch {
            $priorPidAlive = $false
        }
        $heartbeatAge = 99999
        if (-not [string]::IsNullOrEmpty($priorHeartbeat)) {
            try {
                $heartbeatAge = ([DateTime]::UtcNow - [DateTime]::Parse($priorHeartbeat).ToUniversalTime()).TotalSeconds
            }
            catch {
                $heartbeatAge = 99999
            }
        }

        if (-not $priorPidAlive) {
            Write-Host ("[LOOP] Stale loop-state from PID {0} (process not found). Treating as dead. Resuming." -f $priorPid)
        }
        elseif ($heartbeatAge -lt $staleThresholdSeconds -and -not $AllowConcurrentLoopOverride) {
            throw ("loop-duplicate-instance-detected: pid={0} heartbeat_age_s={1}. Use -AllowConcurrentLoopOverride to bypass intentionally." -f $priorPid, [int]$heartbeatAge)
        }
        elseif ($heartbeatAge -lt $staleThresholdSeconds -and $AllowConcurrentLoopOverride) {
            Write-Warning ("[LOOP] Duplicate instance override enabled. Another active loop PID={0} heartbeat_age_s={1}." -f $priorPid, [int]$heartbeatAge)
        }
        elseif ($heartbeatAge -ge $staleThresholdSeconds) {
            Write-Host ("[LOOP] Stale loop-state from PID {0} (heartbeat {1}s ago - exceeded {2}s threshold). Treating as dead. Resuming." -f $priorPid, [int]$heartbeatAge, $staleThresholdSeconds)
        }
    }
}

try {
    $loopMutex = New-Object System.Threading.Mutex($false, $loopMutexName)
    $loopMutexHeld = $loopMutex.WaitOne([TimeSpan]::FromSeconds(0))
}
catch {
    $loopMutexHeld = $false
}

if (-not $loopMutexHeld -and -not $AllowConcurrentLoopOverride) {
    throw ("loop-singleton-lock-active: mutex '{0}' is already held by another instance. Use -AllowConcurrentLoopOverride to bypass intentionally." -f $loopMutexName)
}
if (-not $loopMutexHeld -and $AllowConcurrentLoopOverride) {
    Write-Warning ("[LOOP] Concurrent loop override enabled. Could not acquire mutex '{0}'." -f $loopMutexName)
}

$script:loopStartedAt = $startedAt
$script:loopCurrentPid = $currentPid
$script:loopCurrentCycle = 0
$script:loopRunCount = $runCount
$script:loopCumulativeCyclesBase = $cumulativeCycles
$script:lastMutationCycle = $lastMutationCycle
$script:lastMutationFingerprint = $lastMutationFingerprint
$script:lastMutationReason = $lastMutationReason
$script:lastPythonRuntimeMetrics = $null
$script:llmRuntimeConfigured = $false
$script:llmRuntimeLiteLlmInstalled = $false
$script:llmRuntimeModel = ""
$script:llmRuntimeApiBase = ""
$script:llmRuntimeBootstrapError = ""

if ($BootstrapLocalLlmGpu) {
    $llmBootstrap = Ensure-V2LocalLlmRuntime -PythonBin $PythonExecutable -ProjectRoot $resolvedProjectPath
    $script:llmRuntimeConfigured = [bool](Get-V2OptionalProperty -InputObject $llmBootstrap -Name "configured" -DefaultValue $false)
    $script:llmRuntimeLiteLlmInstalled = [bool](Get-V2OptionalProperty -InputObject $llmBootstrap -Name "litellm_installed" -DefaultValue $false)
    $script:llmRuntimeModel = [string](Get-V2OptionalProperty -InputObject $llmBootstrap -Name "model" -DefaultValue "")
    $script:llmRuntimeApiBase = [string](Get-V2OptionalProperty -InputObject $llmBootstrap -Name "api_base" -DefaultValue "")
    $script:llmRuntimeBootstrapError = [string](Get-V2OptionalProperty -InputObject $llmBootstrap -Name "error" -DefaultValue "")
}

Write-Host ""
Write-Host "========================================"
Write-Host (" Autonomous Loop - {0}" -f (Split-Path -Leaf $resolvedProjectPath))
Write-Host (" Interval: {0}s | MaxCycles: {1} | Run #{2}" -f $IntervalSeconds, ($(if ($MaxCycles -eq 0) { "infinite" } else { [string]$MaxCycles })), $runCount)
Write-Host (" Observer cadence: every {0} cycle(s) | memory-sync every {1} | command-timeout={2}s" -f $observerEveryCycles, $observerMemorySyncEveryCycles, $observerCommandTimeoutSeconds)
Write-Host (" Runtime Engine: {0} (python={1})" -f $AgentRuntimeEngine, $PythonExecutable)
Write-Host (" Mutation cadence: every {0} cycles | fingerprint-change={1}" -f $MutationCadenceCycles, $MutationRunOnFingerprintChange)
Write-Host (" Singleton lock: {0} | held={1}" -f $loopMutexName, $loopMutexHeld)
if ($BootstrapLocalLlmGpu) {
    Write-Host (" LLM GPU bootstrap: configured={0} litellm_installed={1} model={2}" -f $script:llmRuntimeConfigured, $script:llmRuntimeLiteLlmInstalled, $script:llmRuntimeModel)
    if (-not [string]::IsNullOrWhiteSpace($script:llmRuntimeBootstrapError)) {
        Write-Warning ("[LOOP] LLM bootstrap warning: {0}" -f $script:llmRuntimeBootstrapError)
    }
}
Write-Host (" Started:  {0} | PID: {1}" -f $startedAt.ToString("yyyy-MM-dd HH:mm:ss"), $currentPid)
Write-Host (" Prior cumulative cycles: {0}" -f $cumulativeCycles)
Write-Host "========================================"

$script:lastHandledCommandTs = ""

while ($true) {
    $cycle += 1
    $script:loopCurrentCycle = $cycle
    Write-Host ""
    Write-Host ("[{0}] Cycle {1}" -f (Get-Date).ToString("HH:mm:ss"), $cycle)

    $systemControlPath = Join-Path $orchestratorRoot "state/system-control.json"
    if (Test-Path -LiteralPath $systemControlPath -PathType Leaf) {
        try {
            $sysCtrl = Get-V2JsonContent -Path $systemControlPath
            $sysMode = [string](Get-V2OptionalProperty -InputObject $sysCtrl -Name "system_mode" -DefaultValue "")
            if ($sysMode -eq "kill") {
                Write-Host ""
                Write-Host "===========================" -ForegroundColor Red
                Write-Host "☠ KILL MODE ATIVADO ☠" -ForegroundColor Red
                Write-Host "Orquestrador encerrado via Dashboard." -ForegroundColor Red
                Write-Host "===========================" -ForegroundColor Red
                
                $sysCtrl.system_mode = "paused"
                Save-V2JsonContent -Path $systemControlPath -Value $sysCtrl
                break
            }
            
            $confThreshold = [int](Get-V2OptionalProperty -InputObject $sysCtrl -Name "confidence_threshold" -DefaultValue 72)
            $env:ORCHESTRATOR_LLM_ROUTING_COMPLEXITY_THRESHOLD = [Math]::Max(1, [Math]::Round(((100 - $confThreshold) / 5), 0))
            
            $lastCmd = [string](Get-V2OptionalProperty -InputObject $sysCtrl -Name "last_command" -DefaultValue "")
            $lastCmdTs = [string](Get-V2OptionalProperty -InputObject $sysCtrl -Name "timestamp" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($lastCmd) -and $lastCmdTs -ne $script:lastHandledCommandTs) {
                Write-Host ("[LOOP] Comando Recebido do Dashboard: {0}" -f $lastCmd) -ForegroundColor Magenta
                $script:lastHandledCommandTs = $lastCmdTs
                
                switch -Regex ($lastCmd.Trim()) {
                    '^/reclaim\s+task\-(.+)$' {
                        $tId = $matches[1]
                        Write-Host "  -> Reclamando tarefa $tId..." -ForegroundColor Yellow
                        $dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
                        if (Test-Path -LiteralPath $dagPath) {
                            try {
                                $dag = Get-V2JsonContent -Path $dagPath
                                $found = $false
                                if ($dag -and $dag.tasks) {
                                    foreach ($t in $dag.tasks) {
                                        # Handle either id or task_id mappings
                                        $currId = if ($t.id) { $t.id } else { $t.task_id }
                                        if ([string]$currId -eq $tId -and [string]$t.status -in @("in-progress", "blocked")) {
                                            $t.status = "pending"
                                            $t.assigned_agent = ""
                                            $t.blocked_reason = ""
                                            $t.updated_at = (Get-Date).ToUniversalTime().ToString("o")
                                            $found = $true
                                            break
                                        }
                                    }
                                }
                                if ($found) {
                                    Save-V2JsonContent -Path $dagPath -Value $dag
                                    Write-V2StreamEvent -EventType "command-result" -Message "Task $tId reclaimed with success." -Level "info"
                                } else {
                                    Write-V2StreamEvent -EventType "command-result" -Message "Task $tId not found or already idle." -Level "warn"
                                }
                            } catch {
                                Write-Host "  -> Falha ao reclamar tarefa: $($_.Exception.Message)" -ForegroundColor Red
                            }
                        }
                    }
                    '^/kill\s+agent\s+(.+)$' {
                        $aName = $matches[1]
                        Write-Host "  -> Procurando processos nativos e Python de agente: $aName..." -ForegroundColor Red
                        try {
                            Get-WmiObject Win32_Process -Filter "CommandLine LIKE '%$aName%'" | ForEach-Object {
                                if ($_.ProcessId -ne $PID) {
                                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                                    Write-Host "  -> Processo $($_.ProcessId) morto." -ForegroundColor DarkRed
                                }
                            }
                            Write-V2StreamEvent -EventType "command-result" -Message "Agent $aName term signal issued." -Level "warn"
                        } catch {
                            Write-Host "  -> Falha ao obter lista de processos: $($_.Exception.Message)" -ForegroundColor Red
                        }
                    }
                    '^/snapshot\s+stack$' {
                        Write-Host "  -> Gerando snapshot completo do estado e tasks..." -ForegroundColor Green
                        $snapDir = Join-Path $orchestratorRoot "state/snapshots"
                        if (-not (Test-Path -LiteralPath $snapDir)) {
                            New-Item -ItemType Directory -Path $snapDir -Force | Out-Null
                        }
                        $tsName = (Get-Date).ToString("yyyyMMdd_HHmmss")
                        $zipPath = Join-Path $snapDir "snapshot_$tsName.zip"
                        $sourceDirs = @(
                            (Join-Path $orchestratorRoot "state"),
                            (Join-Path $orchestratorRoot "tasks")
                        )
                        try {
                            Compress-Archive -Path $sourceDirs -DestinationPath $zipPath -Force
                            Write-V2StreamEvent -EventType "command-result" -Message "Snapshot ready at $zipPath" -Level "info"
                        } catch {
                            Write-V2StreamEvent -EventType "command-result" -Message "Failed to create snapshot: $($_.Exception.Message)" -Level "error"
                        }
                    }
                    '^/list\s+zombies$' {
                        Write-Host "  -> Procurando processos em loop orfandados..." -ForegroundColor Yellow
                        $zombies = @()
                        try {
                            Get-WmiObject Win32_Process -Filter "CommandLine LIKE '%Run-Agent%'" | ForEach-Object {
                                if ($_.CreationDate) {
                                    $dur = (Get-Date) - $_.ConvertToDateTime($_.CreationDate)
                                    if ($dur.TotalHours -gt 2 -and $_.ProcessId -ne $PID) {
                                        $zombies += "PID $($_.ProcessId) ($([Math]::Round($dur.TotalHours,1))h)"
                                    }
                                }
                            }
                        } catch {}
                        if ($zombies.Count -gt 0) {
                            $msg = "Zombies encontrados: " + ($zombies -join ", ")
                            Write-V2StreamEvent -EventType "command-result" -Message $msg -Level "warn"
                            Write-Host "  -> $msg" -ForegroundColor Red
                        } else {
                            Write-V2StreamEvent -EventType "command-result" -Message "Nenhum zombie (processo > 2h) encontrado." -Level "info"
                        }
                    }
                    '^/logs\s+tail\s+(.+)$' {
                        $worker = $matches[1]
                        Write-Host "  -> Extraindo ultimos logs de $worker..." -ForegroundColor Yellow
                        $logFile = Join-Path $orchestratorRoot "state/loop-step-events.jsonl"
                        if (Test-Path -LiteralPath $logFile) {
                            $lines = @(Get-Content -LiteralPath $logFile -Tail 500 -ErrorAction SilentlyContinue | Where-Object { $_ -match $worker } | Select-Object -Last 3)
                            if ($lines.Count -gt 0) {
                                Write-V2StreamEvent -EventType "command-result" -Message "Recent logs for $worker found (check console)." -Level "info"
                                foreach ($l in $lines) { Write-Host "     $l" -ForegroundColor DarkGray }
                            } else {
                                Write-V2StreamEvent -EventType "command-result" -Message "No recent logs found for $worker." -Level "warn"
                            }
                        }
                    }
                    '^/reset\s+confidence$' {
                        Write-Host "  -> Restabelecendo confiança base de 72%..." -ForegroundColor Yellow
                        $sysCtrl.confidence_threshold = 72
                        Save-V2JsonContent -Path $systemControlPath -Value $sysCtrl
                        $env:ORCHESTRATOR_LLM_ROUTING_COMPLEXITY_THRESHOLD = [Math]::Max(1, [Math]::Round(((100 - 72) / 5), 0))
                        Write-V2StreamEvent -EventType "command-result" -Message "Confidence threshold reset to 72%." -Level "info"
                    }
                    default {
                        Write-Host "  -> Comando nao reconhecido pelo engine nativo: $lastCmd" -ForegroundColor DarkGray
                        Write-V2StreamEvent -EventType "command-result" -Message "Command '$lastCmd' unrecognized by runtime loop." -Level "debug"
                    }
                }
            }
        } catch {}
    }

    # Whiteboard maintenance — clear stale intent announcements at the top of each cycle
    if (Test-Path -LiteralPath $whiteboardPath -PathType Leaf) {
        try { & $whiteboardPath -Mode "clear-stale" -ProjectPath $resolvedProjectPath | Out-Null } catch {}
    }

    Write-V2StreamEvent -EventType "loop-cycle-start" -Message ("Cycle {0} started." -f $cycle) -Payload ([PSCustomObject]@{
            cycle = $cycle
            run_count = $runCount
            started_at = (Get-Date).ToString("o")
        })

    try {
        Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-loop"
    }
    catch {
        Write-Host ("[LOOP] Stopped by coordination mode: {0}" -f $_.Exception.Message)
        break
    }

    if (Test-Path -LiteralPath $scriptValidationPath -PathType Leaf) {
        $scriptValidationOk = Invoke-V2LoopStep -Name "ScriptValidation" -ScriptPath $scriptValidationPath -Arguments @{
            ProjectPath = $resolvedProjectPath
            EmitJson = $true
        }
        if (-not $scriptValidationOk) {
            Write-Host "[LOOP] Script validation gate failed. Stopping loop for safety."
            break
        }
    }

    $runObserverThisCycle = (-not $SkipObserver) -and (($cycle -eq 1) -or (($cycle % $observerEveryCycles) -eq 0))
    if ($runObserverThisCycle) {
        $skipObserverMemorySyncThisCycle = [bool]$SkipMemorySync
        if (-not $skipObserverMemorySyncThisCycle -and $observerMemorySyncEveryCycles -gt 1) {
            if (($cycle % $observerMemorySyncEveryCycles) -ne 0) {
                $skipObserverMemorySyncThisCycle = $true
            }
        }
        [void](Invoke-V2LoopStep -Name "Observer" -ScriptPath $observerPath -Arguments @{
                ProjectPath = $resolvedProjectPath
                RunOnce = $true
                SkipMemorySync = $skipObserverMemorySyncThisCycle
                IncludeNeo4j = $loopIncludeNeo4j
                IncludeQdrant = $loopIncludeQdrant
                CommandTimeoutSeconds = $observerCommandTimeoutSeconds
            })
    }
    elseif (-not $SkipObserver) {
        Write-Host ("  -> Observer skipped by cadence (every {0} cycle(s))" -f $observerEveryCycles)
    }

    $runPolicyEnforcerThisCycle = (Test-Path -LiteralPath $policyEnforcerPath -PathType Leaf) -and (($cycle -eq 1) -or (($cycle % $policyEnforcerEveryCycles) -eq 0))
    if ($runPolicyEnforcerThisCycle) {
        [void](Invoke-V2LoopStep -Name "PolicyEnforcer" -ScriptPath $policyEnforcerPath -Arguments @{
                ProjectPath = $resolvedProjectPath
                EmitJson = $true
            })
    }

    $runAggregationThisCycle = (Test-Path -LiteralPath $projectAggregationPath -PathType Leaf) -and (($cycle -eq 1) -or (($cycle % $aggregationEveryCycles) -eq 0))
    if ($runAggregationThisCycle) {
        $_parent1 = Split-Path -Parent $resolvedProjectPath
        $_parent2 = if ($_parent1) { Split-Path -Parent $_parent1 } else { "" }
        $workspacePath = if ($_parent2) { $_parent2 } else { $_parent1 }
        if ($workspacePath) {
            [void](Invoke-V2LoopStep -Name "ProjectAggregation" -ScriptPath $projectAggregationPath -Arguments @{
                    WorkspacePath = $workspacePath
                })
        }
    }

    if (-not $SkipScheduler) {
        [void](Invoke-V2LoopStep -Name "Scheduler" -ScriptPath $schedulerPath -Arguments @{
                ProjectPath = $resolvedProjectPath
                MaxAssignmentsPerRun = 6
                IdlePendingTakeoverMinutes = $IdlePendingTakeoverMinutes
                IdleInProgressTakeoverMinutes = $IdleInProgressTakeoverMinutes
            })
    }

    if (-not $SkipAgentDispatch) {
        $dispatchArgs = @{
            ProjectPath = $resolvedProjectPath
            Phase = $DispatchPhase
            AutoRepairTasks = $true
            EmitJson = $true
        }
        if ($IncludeRuntimeAgentsInDispatch) { $dispatchArgs.IncludeRuntimeAgents = $true }
        [void](Invoke-V2LoopStep -Name "AgentDispatch" -ScriptPath $agentDispatchPath -Arguments $dispatchArgs)
    }

    if (Test-Path -LiteralPath $delegationBusPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "DelegationBusProcessor" -ScriptPath $delegationBusPath -Arguments @{
                Mode = "process"
                ProjectPath = $resolvedProjectPath
                EmitJson = $true
            })
    }

    # Execute pending dispatch tasks using local CLI agents (codex, python/anthropic, ollama)
    # before falling back to waiting for external agents to consume dispatch files.
    if (Test-Path -LiteralPath $localAgentExecutorPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "LocalAgentExecutor" -ScriptPath $localAgentExecutorPath -Arguments @{
                ProjectPath = $resolvedProjectPath
                MaxTasks    = 3
            })
    }

    if (-not $SkipAgentRuntime) {
        $targetAgents = New-Object System.Collections.Generic.List[string]
        if ($RunAllAssignedAgents -and (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
            try {
                $dag = Get-V2JsonContent -Path $taskDagPath
                $tasks = @(Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @())
                foreach ($task in $tasks) {
                    $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    $assigned = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
                    if ($status -eq "in-progress" -and -not [string]::IsNullOrWhiteSpace($assigned)) {
                        if (-not $targetAgents.Contains($assigned)) {
                            $targetAgents.Add($assigned)
                        }
                    }
                }
            }
            catch {
                Write-Host ("  -> AgentRuntime discovery failed ({0})" -f $_.Exception.Message)
            }
        }

        if ($targetAgents.Count -eq 0) {
            $targetAgents.Add($AgentName)
        }

        foreach ($targetAgent in $targetAgents) {
            $pythonResult = $null
            $pythonSucceeded = $false
            $pythonAvailable = ($AgentRuntimeEngine -in @("python", "hybrid")) -and (Test-Path -LiteralPath $pythonAgentLoopPath -PathType Leaf)

            if ($pythonAvailable) {
                $pythonResult = Invoke-V2PythonAgentRuntimeStep -AgentForRuntime $targetAgent
                $pythonSucceeded = [bool](Get-V2OptionalProperty -InputObject $pythonResult -Name "success" -DefaultValue $false)
                $pythonExecutedTasks = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "executed_tasks" -DefaultValue 0)
                $pythonSkippedNoLlm = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "skipped_no_llm" -DefaultValue 0)
                $script:lastPythonRuntimeMetrics = [PSCustomObject]@{
                    captured_at = Get-V2Timestamp
                    cycle = $cycle
                    agent = $targetAgent
                    success = $pythonSucceeded
                    executed_tasks = $pythonExecutedTasks
                    skipped_no_llm = $pythonSkippedNoLlm
                    llm_enabled = [bool](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_enabled" -DefaultValue $false)
                    llm_model = [string](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_model" -DefaultValue "")
                    llm_models_used = @((Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_models_used" -DefaultValue @()))
                    llm_calls = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_calls" -DefaultValue 0)
                    llm_total_tokens = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_total_tokens" -DefaultValue 0)
                    llm_tokens_per_second = [double](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_tokens_per_second" -DefaultValue 0.0)
                    llm_route_fast = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_route_fast" -DefaultValue 0)
                    llm_route_heavy = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_route_heavy" -DefaultValue 0)
                    llm_route_infra_default = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_route_infra_default" -DefaultValue 0)
                    llm_route_routing_disabled = [int](Get-V2OptionalProperty -InputObject $pythonResult -Name "llm_route_routing_disabled" -DefaultValue 0)
                }
                if ($pythonSucceeded -and $pythonExecutedTasks -eq 0 -and $pythonSkippedNoLlm -gt 0) {
                    # If the Python cognitive runtime is not configured with an LLM provider,
                    # force PowerShell full fallback to avoid starving native tasks.
                    $pythonSucceeded = $false
                }
            }

            $runPowerShell = $false
            $skipNativeForPowerShell = $false

            if ($AgentRuntimeEngine -eq "powershell") {
                $runPowerShell = $true
                $skipNativeForPowerShell = $false
            }
            elseif ($AgentRuntimeEngine -eq "python") {
                $runPowerShell = $true
                if ($pythonAvailable -and $pythonSucceeded) {
                    $skipNativeForPowerShell = $true
                }
                else {
                    # Python failed or unavailable: fallback to full PowerShell runtime.
                    $skipNativeForPowerShell = $false
                }
            }
            elseif ($AgentRuntimeEngine -eq "hybrid") {
                # Hybrid always runs PowerShell for external/manual tasks.
                $runPowerShell = $true
                if ($pythonAvailable -and $pythonSucceeded) {
                    $skipNativeForPowerShell = $true
                }
                else {
                    # Python path unavailable/failed: avoid starving native tasks.
                    $skipNativeForPowerShell = $false
                }
            }

            if ($runPowerShell) {
                $runtimeArgs = @{
                    ProjectPath = $resolvedProjectPath
                    AgentName = $targetAgent
                    RunOnce = $true
                    MaxTasksPerCycle = $MaxTasksPerCycle
                }
                if ($skipNativeForPowerShell) {
                    $runtimeArgs.SkipNativeRuntimeTasks = $true
                }

                [void](Invoke-V2LoopStep -Name ("AgentRuntime[{0}]" -f $targetAgent) -ScriptPath $agentLoopPath -Arguments $runtimeArgs)
            }
        }
    }

    if (-not $SkipAgentValidation) {
        $validationArgs = @{
            ProjectPath = $resolvedProjectPath
            Phase = "auto"
            AutoRepairTasks = $true
            EmitJson = $true
        }
        if ($FailOnValidationNotReady) { $validationArgs.FailOnNotReady = $true }
        [void](Invoke-V2LoopStep -Name "AgentValidation" -ScriptPath $agentValidationPath -Arguments $validationArgs)
    }
    # -------------------------------------------------------------------------------
    # Run test suite and create/resolve REPAIR-TEST-FAIL tasks automatically
    if (Test-Path -LiteralPath $testRunnerPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "TestRunner" -ScriptPath $testRunnerPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Verify deployed services are running and healthy
    if (Test-Path -LiteralPath $deployVerifyPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "DeployVerify" -ScriptPath $deployVerifyPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Evaluate production telemetry and create REPAIR tasks when thresholds are breached
    if (Test-Path -LiteralPath $runtimeObservabilityPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "RuntimeObservability" -ScriptPath $runtimeObservabilityPath -Arguments @{
            ProjectPath     = $resolvedProjectPath
            AutoRepairTasks = $true
            EmitJson        = $true
        })
    }

    # Promote resolved REPAIR patterns to the global cross-project library
    if (Test-Path -LiteralPath $promotePatternPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "PromotePatterns" -ScriptPath $promotePatternPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    if (Test-Path -LiteralPath $metaCalibrationPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "MetaCalibration" -ScriptPath $metaCalibrationPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    if (Test-Path -LiteralPath $taskProjectionPath -PathType Leaf) {
        $runProjectionThisCycle = ($cycle -eq 1) -or (($cycle % $aiSystemProjectionEveryCycles) -eq 0)
        if ($runProjectionThisCycle) {
            [void](Invoke-V2PythonUtilityStep -StepName "AiSystemProjection" -PythonScriptPath $taskProjectionPath -PythonArguments @(
                    "--project-path", $resolvedProjectPath,
                    "--emit-json"
                ))
        }
        else {
            Write-Host ("  -> AiSystemProjection skipped by cadence (every {0} cycle(s))" -f $aiSystemProjectionEveryCycles)
        }
    }

    if (Test-Path -LiteralPath $crossProjectMemorySyncPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "CrossProjectMemorySync" -ScriptPath $crossProjectMemorySyncPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Refresh live dashboard
    if (Test-Path -LiteralPath $dashboardPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "Dashboard" -ScriptPath $dashboardPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Keep architecture documentation grounded in real code when still stubbed.
    if ((Test-Path -LiteralPath $codeReaderPath -PathType Leaf) -and (Test-V2ArchitectureStub -ArchitecturePath $architectureDocPath)) {
        [void](Invoke-V2LoopStep -Name "CodeReader" -ScriptPath $codeReaderPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Monitor system RAM/CPU and Docker container resource usage; pause overloaded containers.
    if (Test-Path -LiteralPath $finOpsPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "FinOps" -ScriptPath $finOpsPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        })
    }

    # Run mutation testing during idle cycles with cadence/fingerprint gating.
    $hasActiveWork = $false
    if (Test-Path -LiteralPath $taskDagPath -PathType Leaf) {
        try {
            $dagForMutation = Get-V2JsonContent -Path $taskDagPath
            $activeTasks = @($dagForMutation.tasks | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "in-progress"
            })
            $hasActiveWork = $activeTasks.Count -gt 0
        }
        catch { $hasActiveWork = $true }
    }
    $ranMutationThisCycle = $false
    $currentMutationFingerprint = Get-V2LoopMutationFingerprint -ProjectRoot $resolvedProjectPath
    $globalCycleNumber = $script:loopCumulativeCyclesBase + $cycle
    $cyclesSinceMutation = if ($script:lastMutationCycle -gt 0) { $globalCycleNumber - $script:lastMutationCycle } else { 999999 }
    $fingerprintChanged = (-not [string]::IsNullOrWhiteSpace($currentMutationFingerprint)) -and ($currentMutationFingerprint -ne $script:lastMutationFingerprint)
    $cadenceReached = $cyclesSinceMutation -ge ([Math]::Max($MutationCadenceCycles, 1))
    $shouldRunMutation = $false
    $mutationReason = ""

    if (-not $hasActiveWork) {
        if ($MutationRunOnFingerprintChange -and $fingerprintChanged) {
            $shouldRunMutation = $true
            $mutationReason = "fingerprint-change"
        }
        elseif ($cadenceReached) {
            $shouldRunMutation = $true
            $mutationReason = "cadence"
        }
    }

    if ($shouldRunMutation -and (Test-Path -LiteralPath $mutationTestPath -PathType Leaf)) {
        Write-Host ("[LOOP] MutationTest enabled ({0}) - cycles_since_last={1}" -f $mutationReason, $cyclesSinceMutation)
        $mutationStepOk = Invoke-V2LoopStep -Name "MutationTest" -ScriptPath $mutationTestPath -Arguments @{
            ProjectPath = $resolvedProjectPath
        }
        if ($mutationStepOk) {
            $ranMutationThisCycle = $true
            $script:lastMutationCycle = $globalCycleNumber
            if (-not [string]::IsNullOrWhiteSpace($currentMutationFingerprint)) {
                $script:lastMutationFingerprint = $currentMutationFingerprint
            }
            $script:lastMutationReason = $mutationReason
        }
    }
    elseif (-not $hasActiveWork) {
        Write-Host ("[LOOP] MutationTest skipped - cycles_since_last={0}, fingerprint_changed={1}" -f $cyclesSinceMutation, $fingerprintChanged)
    }

    # Enforce mutation score thresholds when mutation has just executed.
    if ($ranMutationThisCycle -and (Test-Path -LiteralPath $mutationPolicyPath -PathType Leaf)) {
        [void](Invoke-V2LoopStep -Name "MutationPolicyEnforcer" -ScriptPath $mutationPolicyPath -Arguments @{
            ProjectPath = $resolvedProjectPath
            EmitJson    = $true
        })
    }

    # Executive readiness report — READY / DEGRADED / NOT_READY verdict for this cycle
    if (Test-Path -LiteralPath $readinessReportPath -PathType Leaf) {
        [void](Invoke-V2LoopStep -Name "ReadinessReport" -ScriptPath $readinessReportPath -Arguments @{
            ProjectPath = $resolvedProjectPath
            EmitJson    = $true
        })
    }
    # -------------------------------------------------------------------------------

    Write-V2LoopStepState -StepName "CycleComplete" -StepStatus "ok" -StepStartedAt (Get-Date) -StepFinishedAt (Get-Date)
    Write-V2StreamEvent -EventType "loop-cycle-complete" -Message ("Cycle {0} completed." -f $cycle) -Payload ([PSCustomObject]@{
            cycle = $cycle
            completed_at = (Get-Date).ToString("o")
        })

    if ($MaxCycles -gt 0 -and $cycle -ge $MaxCycles) {
        Write-Host ("[LOOP] Reached MaxCycles ({0})." -f $MaxCycles)
        break
    }

    Write-Host ("[{0}] Sleeping {1}s until next cycle..." -f (Get-Date).ToString("HH:mm:ss"), $IntervalSeconds)
    Start-Sleep -Seconds $IntervalSeconds
}

$elapsedMinutes = [Math]::Round(((Get-Date) - $startedAt).TotalMinutes, 1)
$finalCheckpointSummary = Get-V2LatestTaskCheckpointSummary -ProjectRoot $resolvedProjectPath
Save-V2JsonContent -Path $loopStatePath -Value ([PSCustomObject]@{
        generated_at      = Get-V2Timestamp
        loop_running      = $false
        pid               = $currentPid
        cycle             = $cycle
        cumulative_cycles = $cumulativeCycles + $cycle
        run_count         = $runCount
        total_cycles      = $cycle
        elapsed_minutes   = $elapsedMinutes
        interval_seconds  = $IntervalSeconds
        max_cycles        = $MaxCycles
        agent_name        = $AgentName
        agent_runtime_engine = $AgentRuntimeEngine
        mutation_cadence_cycles = $MutationCadenceCycles
        mutation_run_on_fingerprint_change = [bool]$MutationRunOnFingerprintChange
        mutation_last_cycle = $script:lastMutationCycle
        mutation_last_fingerprint = $script:lastMutationFingerprint
        mutation_last_reason = $script:lastMutationReason
        llm_runtime = [PSCustomObject]@{
            configured = [bool]$script:llmRuntimeConfigured
            litellm_installed = [bool]$script:llmRuntimeLiteLlmInstalled
            model = [string]$script:llmRuntimeModel
            api_base = [string]$script:llmRuntimeApiBase
            bootstrap_error = [string]$script:llmRuntimeBootstrapError
        }
        last_python_runtime_metrics = $script:lastPythonRuntimeMetrics
        started_at        = $startedAt.ToString("o")
        stopped_at        = (Get-Date).ToString("o")
        mode              = "observe-schedule-execute"
        last_step         = [PSCustomObject]@{
            name        = "LoopStopped"
            status      = "ok"
            started_at  = $startedAt.ToString("o")
            finished_at = (Get-Date).ToString("o")
            error       = ""
        }
        last_checkpoint_step = $finalCheckpointSummary
    })

if ($loopMutexHeld -and $loopMutex) {
    try { $loopMutex.ReleaseMutex() } catch {}
}
if ($loopMutex) {
    try { $loopMutex.Dispose() } catch {}
}

Write-Host ""
Write-Host "========================================"
Write-Host (" Loop finished. Cycles: {0} | Elapsed: {1}m" -f $cycle, $elapsedMinutes)
Write-Host "========================================"
