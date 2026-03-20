<#
.SYNOPSIS
    FinOps resource monitor: tracks system RAM/CPU and Docker container memory/CPU.
.DESCRIPTION
    Monitors system and container resource consumption every loop cycle:
      1. System RAM - Windows WMI (Get-CimInstance Win32_OperatingSystem)
      2. System CPU - Get-CimInstance Win32_Processor LoadPercentage
      3. Docker container stats - `docker stats --no-stream` for per-container breakdown
      4. Auto-pauses non-critical containers when RAM > $RamCriticalThresholdPct (default 90%)
      5. Resumes paused containers when RAM drops below $RamResumeThresholdPct (default 80%)
      6. Creates REPAIR-RESOURCE-* tasks in task-dag.json when limits are exceeded
      7. Auto-resolves REPAIR-RESOURCE-* tasks when resources recover
    Writes ai-orchestrator/reports/finops-<timestamp>.json.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER RamCriticalThresholdPct
    RAM usage % that triggers container pausing. Default: 90.
.PARAMETER RamResumeThresholdPct
    RAM usage % below which paused containers are resumed. Default: 80.
.PARAMETER CriticalContainers
    Comma-separated container name substrings to NEVER pause (always critical).
    Default includes app/db/redis/neo4j/qdrant/ollama to protect core runtime.
.PARAMETER DryRun
    Report issues without pausing/resuming containers.
.EXAMPLE
    .\scripts\v2\Invoke-FinOpsMonitorV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-FinOpsMonitorV2.ps1 -ProjectPath C:\projects\myapp -DryRun
#>
param(
    [string]$ProjectPath               = ".",
    [int]$RamCriticalThresholdPct      = 90,
    [int]$RamResumeThresholdPct        = 80,
    [string]$CriticalContainers        = "app,db,postgres,mysql,redis,mongo,rabbit,kafka,zookeeper,neo4j,qdrant,ollama",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$dagPath          = Join-Path $orchestratorRoot "tasks/task-dag.json"
$reportsDir       = Join-Path $orchestratorRoot "reports"
$ts               = Get-Date -Format "yyyyMMddHHmmss"
$reportPath       = Join-Path $reportsDir "finops-$ts.json"

Initialize-V2Directory -Path $reportsDir

function Test-V2DockerTcpOpen {
    param(
        [string]$TcpHost = "127.0.0.1",
        [int]$Port = 2375,
        [int]$TimeoutMs = 400
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $iar = $client.BeginConnect($TcpHost, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($iar)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Initialize-V2FinOpsDockerContext {
    param([string]$OrchestratorRoot)

    if (-not (Test-Path Env:DOCKER_HOST) -and (Test-V2DockerTcpOpen -TcpHost "127.0.0.1" -Port 2375 -TimeoutMs 400)) {
        $env:DOCKER_HOST = "tcp://localhost:2375"
    }

    if (-not (Test-Path Env:DOCKER_CONFIG)) {
        $userProfile = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:HOME) { $env:HOME } else { "" }
        $userDockerConfigPath = if ($userProfile) { Join-Path $userProfile ".docker/config.json" } else { "" }
        $useFallbackDockerConfig = $false
        try {
            if ($userDockerConfigPath -and (Test-Path -LiteralPath $userDockerConfigPath -PathType Leaf)) {
                try {
                    [void](Get-Content -LiteralPath $userDockerConfigPath -TotalCount 1 -ErrorAction Stop)
                }
                catch {
                    $useFallbackDockerConfig = $true
                }
            }
        }
        catch {
            $useFallbackDockerConfig = $true
        }

        if ($useFallbackDockerConfig) {
            $fallbackDockerConfigDir = Join-Path $OrchestratorRoot "docker/.docker-config"
            Initialize-V2Directory -Path $fallbackDockerConfigDir
            $fallbackDockerConfigPath = Join-Path $fallbackDockerConfigDir "config.json"
            if (-not (Test-Path -LiteralPath $fallbackDockerConfigPath -PathType Leaf)) {
                $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
                [System.IO.File]::WriteAllText($fallbackDockerConfigPath, '{"auths":{}}', $utf8NoBom)
            }
            $env:DOCKER_CONFIG = $fallbackDockerConfigDir
        }
    }
}

Initialize-V2FinOpsDockerContext -OrchestratorRoot $orchestratorRoot

$criticalKeywords = @($CriticalContainers -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })

function Get-V2SystemMemorySnapshot {
    $result = [PSCustomObject]@{
        success   = $false
        used_pct  = 0.0
        free_gb   = 0.0
        total_gb  = 0.0
        source    = ""
        error     = ""
    }

    # Primary: CIM / Win32_OperatingSystem
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $totalGb = [double]$os.TotalVisibleMemorySize / 1MB
        $freeGb = [double]$os.FreePhysicalMemory / 1MB
        $usedPct = (($totalGb - $freeGb) / [Math]::Max($totalGb, 0.001)) * 100
        $result.success = $true
        $result.used_pct = [Math]::Round($usedPct, 1)
        $result.free_gb = [Math]::Round($freeGb, 2)
        $result.total_gb = [Math]::Round($totalGb, 2)
        $result.source = "cim-win32-operatingsystem"
        return $result
    }
    catch {
    }

    # Fallback: .NET ComputerInfo (no WMI dependency in most environments)
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
        $ci = New-Object Microsoft.VisualBasic.Devices.ComputerInfo
        $totalBytes = [double]$ci.TotalPhysicalMemory
        $freeBytes = [double]$ci.AvailablePhysicalMemory
        $totalGb = $totalBytes / 1GB
        $freeGb = $freeBytes / 1GB
        $usedPct = (($totalGb - $freeGb) / [Math]::Max($totalGb, 0.001)) * 100
        $result.success = $true
        $result.used_pct = [Math]::Round($usedPct, 1)
        $result.free_gb = [Math]::Round($freeGb, 2)
        $result.total_gb = [Math]::Round($totalGb, 2)
        $result.source = "dotnet-computerinfo"
        return $result
    }
    catch {
    }

    # Fallback: Performance counters (requires counter service only)
    try {
        $freeMbCounter = Get-Counter -Counter '\Memory\Available MBytes' -ErrorAction Stop
        $freeMb = [double]$freeMbCounter.CounterSamples[0].CookedValue
        $totalBytes = [double]([System.GC]::GetGCMemoryInfo().TotalAvailableMemoryBytes)
        if ($totalBytes -gt 0) {
            $totalGb = $totalBytes / 1GB
            $freeGb = $freeMb / 1024
            $usedPct = (($totalGb - $freeGb) / [Math]::Max($totalGb, 0.001)) * 100
            $result.success = $true
            $result.used_pct = [Math]::Round($usedPct, 1)
            $result.free_gb = [Math]::Round($freeGb, 2)
            $result.total_gb = [Math]::Round($totalGb, 2)
            $result.source = "performance-counter+gc"
            return $result
        }
    }
    catch {
    }

    $result.error = "memory-metric-unavailable"
    return $result
}

function Get-V2SystemCpuSnapshot {
    $result = [PSCustomObject]@{
        success  = $false
        used_pct = 0.0
        source   = ""
        error    = ""
    }

    # Primary: CIM / Win32_Processor
    try {
        $proc = @(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop)
        $avg = ($proc | Measure-Object -Property LoadPercentage -Average).Average
        if ($null -ne $avg) {
            $result.success = $true
            $result.used_pct = [Math]::Round([double]$avg, 1)
            $result.source = "cim-win32-processor"
            return $result
        }
    }
    catch {
    }

    # Fallback: standard processor counter
    try {
        $counter = Get-Counter -Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 1 -ErrorAction Stop
        $value = [double]$counter.CounterSamples[0].CookedValue
        $result.success = $true
        $result.used_pct = [Math]::Round($value, 1)
        $result.source = "performance-counter-processor-time"
        return $result
    }
    catch {
    }

    # Fallback: processor utility counter (newer Windows)
    try {
        $counter2 = Get-Counter -Counter '\Processor Information(_Total)\% Processor Utility' -SampleInterval 1 -MaxSamples 1 -ErrorAction Stop
        $value2 = [double]$counter2.CounterSamples[0].CookedValue
        $result.success = $true
        $result.used_pct = [Math]::Round($value2, 1)
        $result.source = "performance-counter-processor-utility"
        return $result
    }
    catch {
    }

    # Last fallback: typeperf output parsing
    try {
        $tpOutput = & cmd /c "typeperf `"\Processor(_Total)\% Processor Time`" -sc 1 2>&1"
        $tpText = ($tpOutput -join [Environment]::NewLine)
        $matches = [regex]::Matches($tpText, '"([0-9\.,]+)"')
        if ($matches.Count -gt 0) {
            $last = [string]$matches[$matches.Count - 1].Groups[1].Value
            $normalized = $last.Replace(",", ".")
            $value3 = 0.0
            if ([double]::TryParse($normalized, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$value3)) {
                $result.success = $true
                $result.used_pct = [Math]::Round($value3, 1)
                $result.source = "typeperf"
                return $result
            }
        }
    }
    catch {
    }

    $result.error = "cpu-metric-unavailable"
    return $result
}

function Get-V2GpuSnapshot {
    $result = [PSCustomObject]@{
        success         = $false
        gpu_util_pct    = 0.0
        vram_used_mb    = 0.0
        vram_total_mb   = 0.0
        vram_used_pct   = 0.0
        temperature_c   = 0.0
        source          = ""
        error           = ""
    }

    $commands = @(
        @("nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"),
        @("C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits")
    )

    foreach ($cmd in $commands) {
        try {
            $output = & $cmd[0] $cmd[1] $cmd[2] 2>&1
            if ($LASTEXITCODE -ne 0) { continue }
            $lines = @($output | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($lines.Count -eq 0) { continue }

            $maxUtil = 0.0
            $totalUsed = 0.0
            $totalVram = 0.0
            $maxTemp = 0.0
            foreach ($line in $lines) {
                $parts = @(([string]$line).Split(",") | ForEach-Object { $_.Trim() })
                if ($parts.Count -lt 4) { continue }
                $util = Convert-V2InvariantDouble -Text $parts[0]
                $used = Convert-V2InvariantDouble -Text $parts[1]
                $total = Convert-V2InvariantDouble -Text $parts[2]
                $temp = Convert-V2InvariantDouble -Text $parts[3]
                if ($util -gt $maxUtil) { $maxUtil = $util }
                $totalUsed += $used
                $totalVram += $total
                if ($temp -gt $maxTemp) { $maxTemp = $temp }
            }

            if ($totalVram -gt 0) {
                $result.success = $true
                $result.gpu_util_pct = [Math]::Round($maxUtil, 1)
                $result.vram_used_mb = [Math]::Round($totalUsed, 1)
                $result.vram_total_mb = [Math]::Round($totalVram, 1)
                $result.vram_used_pct = [Math]::Round(($totalUsed / $totalVram) * 100.0, 1)
                $result.temperature_c = [Math]::Round($maxTemp, 1)
                $result.source = [string]$cmd[0]
                return $result
            }
        }
        catch {
        }
    }

    $result.error = "gpu-metric-unavailable"
    return $result
}

function Get-V2LatestMemorySyncMetrics {
    param([string]$ProjectRoot)

    $result = [PSCustomObject]@{
        found                       = $false
        embedding_runtime_processor = ""
        vectors_per_second          = 0.0
        ollama_vectors_per_second   = 0.0
        ollama_embeddings           = 0
        non_ollama_embeddings       = 0
        qdrant_maintenance_ok       = $true
        qdrant_maintenance_alert_count = 0
        qdrant_fragmentation_percent = 0.0
        qdrant_vector_index_coverage_percent = 0.0
        qdrant_segments_count = 0
    }

    $healthPath = Join-Path $ProjectRoot "ai-orchestrator/state/health-report.json"
    if (-not (Test-Path -LiteralPath $healthPath -PathType Leaf)) {
        return $result
    }

    try {
        $health = Get-V2JsonContent -Path $healthPath
        $checks = @(Get-V2OptionalProperty -InputObject $health -Name "check_results" -DefaultValue @())
        $memoryCheck = @($checks | Where-Object {
            [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "memory-sync"
        } | Select-Object -First 1)
        if ($memoryCheck.Count -ne 1) {
            return $result
        }
        $details = Get-V2OptionalProperty -InputObject $memoryCheck[0] -Name "details" -DefaultValue ([PSCustomObject]@{})
        $result.found = $true
        $result.embedding_runtime_processor = [string](Get-V2OptionalProperty -InputObject $details -Name "qdrant_embedding_runtime_processor" -DefaultValue "")
        $result.vectors_per_second = [double](Get-V2OptionalProperty -InputObject $details -Name "qdrant_embedding_vectors_per_second" -DefaultValue 0.0)
        $result.ollama_vectors_per_second = [double](Get-V2OptionalProperty -InputObject $details -Name "qdrant_ollama_vectors_per_second" -DefaultValue 0.0)
        $result.ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $details -Name "qdrant_ollama_embeddings" -DefaultValue 0)
        $result.non_ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $details -Name "qdrant_non_ollama_embeddings" -DefaultValue 0)
        $result.qdrant_maintenance_ok = [bool](Get-V2OptionalProperty -InputObject $details -Name "qdrant_maintenance_ok" -DefaultValue $true)
        $result.qdrant_maintenance_alert_count = [int](Get-V2OptionalProperty -InputObject $details -Name "qdrant_maintenance_alert_count" -DefaultValue 0)
        $result.qdrant_fragmentation_percent = [double](Get-V2OptionalProperty -InputObject $details -Name "qdrant_fragmentation_percent" -DefaultValue 0.0)
        $result.qdrant_vector_index_coverage_percent = [double](Get-V2OptionalProperty -InputObject $details -Name "qdrant_vector_index_coverage_percent" -DefaultValue 0.0)
        $result.qdrant_segments_count = [int](Get-V2OptionalProperty -InputObject $details -Name "qdrant_segments_count" -DefaultValue 0)
    }
    catch {
    }
    return $result
}

function Get-V2LoopLlmMetrics {
    param([string]$ProjectRoot)

    $result = [PSCustomObject]@{
        found = $false
        llm_enabled = $false
        llm_model = ""
        llm_models_used = @()
        tokens_per_second = 0.0
        total_tokens = 0
        llm_calls = 0
        llm_route_fast = 0
        llm_route_heavy = 0
        llm_route_infra_default = 0
        llm_route_routing_disabled = 0
    }

    $loopStatePath = Join-Path $ProjectRoot "ai-orchestrator/state/loop-state.json"
    if (-not (Test-Path -LiteralPath $loopStatePath -PathType Leaf)) {
        return $result
    }

    try {
        $state = Get-V2JsonContent -Path $loopStatePath
        $runtimeMetrics = Get-V2OptionalProperty -InputObject $state -Name "last_python_runtime_metrics" -DefaultValue $null
        if ($null -eq $runtimeMetrics) {
            return $result
        }
        $result.found = $true
        $result.llm_enabled = [bool](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_enabled" -DefaultValue $false)
        $result.llm_model = [string](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_model" -DefaultValue "")
        $result.llm_models_used = @((Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_models_used" -DefaultValue @()))
        $result.tokens_per_second = [double](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_tokens_per_second" -DefaultValue 0.0)
        $result.total_tokens = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_total_tokens" -DefaultValue 0)
        $result.llm_calls = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_calls" -DefaultValue 0)
        $result.llm_route_fast = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_route_fast" -DefaultValue 0)
        $result.llm_route_heavy = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_route_heavy" -DefaultValue 0)
        $result.llm_route_infra_default = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_route_infra_default" -DefaultValue 0)
        $result.llm_route_routing_disabled = [int](Get-V2OptionalProperty -InputObject $runtimeMetrics -Name "llm_route_routing_disabled" -DefaultValue 0)
    }
    catch {
    }

    return $result
}

function Get-V2OllamaProcessorMix {
    $result = [PSCustomObject]@{
        available = $false
        gpu_models = 0
        cpu_models = 0
        unknown_models = 0
        summary = "unavailable"
    }

    try {
        $lines = @(ollama ps 2>&1)
        if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) {
            return $result
        }
        $rows = @($lines | Where-Object {
            $text = [string]$_
            -not [string]::IsNullOrWhiteSpace($text) -and -not $text.TrimStart().StartsWith("NAME")
        })
        if ($rows.Count -eq 0) {
            $result.available = $true
            $result.summary = "no-running-models"
            return $result
        }
        foreach ($row in $rows) {
            $text = ([string]$row).ToUpperInvariant()
            if ($text -match "GPU") {
                $result.gpu_models += 1
            }
            elseif ($text -match "CPU") {
                $result.cpu_models += 1
            }
            else {
                $result.unknown_models += 1
            }
        }
        $result.available = $true
        $result.summary = ("gpu:{0}|cpu:{1}|unknown:{2}" -f $result.gpu_models, $result.cpu_models, $result.unknown_models)
    }
    catch {
    }
    return $result
}

function Remove-V2StaleCoreGateContainers {
    param([switch]$DryRunMode)

    $removed = New-Object System.Collections.Generic.List[string]
    $errors = New-Object System.Collections.Generic.List[string]

    try {
        $lines = @(docker ps -a --filter "name=core-gate-" --format "{{.Names}}`t{{.Status}}" 2>&1)
        if ($LASTEXITCODE -ne 0) {
            return [PSCustomObject]@{
                removed = @()
                errors = @("docker-ps-failed")
            }
        }
        foreach ($line in $lines) {
            $parts = @(([string]$line).Split("`t") | ForEach-Object { $_.Trim() })
            if ($parts.Count -lt 2) { continue }
            $name = $parts[0]
            $statusText = $parts[1]
            if ([string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($statusText)) { continue }
            $isStale = $statusText.StartsWith("Exited", [System.StringComparison]::OrdinalIgnoreCase) -or
                $statusText.StartsWith("Created", [System.StringComparison]::OrdinalIgnoreCase) -or
                $statusText.StartsWith("Dead", [System.StringComparison]::OrdinalIgnoreCase)
            if (-not $isStale) { continue }

            if ($DryRunMode) {
                $removed.Add($name)
                continue
            }

            $rmOut = @(docker rm -f $name 2>&1)
            if ($LASTEXITCODE -eq 0) {
                $removed.Add($name)
            }
            else {
                $errors.Add(($name + ":" + (($rmOut | Out-String).Trim())))
            }
        }
    }
    catch {
        $errors.Add($_.Exception.Message)
    }

    return [PSCustomObject]@{
        removed = @($removed.ToArray())
        errors = @($errors.ToArray())
    }
}

function Remove-V2ZombieFindstrProcesses {
    param(
        [switch]$DryRunMode,
        [int]$MinAgeMinutes = 30,
        [double]$MaxCpuSeconds = 1.0
    )

    $detected = New-Object System.Collections.Generic.List[string]
    $removed = New-Object System.Collections.Generic.List[string]
    $errors = New-Object System.Collections.Generic.List[string]
    $now = Get-Date

    try {
        $candidates = @(Get-Process -Name "findstr" -ErrorAction SilentlyContinue)
        foreach ($proc in $candidates) {
            if (-not $proc) { continue }

            $pid = [int](Get-V2OptionalProperty -InputObject $proc -Name "Id" -DefaultValue 0)
            $cpuSeconds = 0.0
            try { $cpuSeconds = [double](Get-V2OptionalProperty -InputObject $proc -Name "CPU" -DefaultValue 0.0) } catch { $cpuSeconds = 0.0 }
            $startTime = $null
            try { $startTime = $proc.StartTime } catch { $startTime = $null }
            $path = [string](Get-V2OptionalProperty -InputObject $proc -Name "Path" -DefaultValue "")

            if ($pid -le 0) { continue }
            if ($null -eq $startTime) { continue }
            if (-not [string]::IsNullOrWhiteSpace($path) -and -not $path.ToLowerInvariant().EndsWith("\findstr.exe")) { continue }

            $ageMinutes = ($now - $startTime).TotalMinutes
            if ($ageMinutes -lt [double]$MinAgeMinutes) { continue }
            if ($cpuSeconds -gt $MaxCpuSeconds) { continue }

            $entry = ("pid={0} age_min={1} cpu_s={2}" -f $pid, [int]$ageMinutes, [Math]::Round($cpuSeconds, 2))
            $detected.Add($entry)

            if ($DryRunMode) { continue }

            try {
                Stop-Process -Id $pid -Force -ErrorAction Stop
                $removed.Add($entry)
            }
            catch {
                $errors.Add(($entry + " error=" + $_.Exception.Message))
            }
        }
    }
    catch {
        $errors.Add($_.Exception.Message)
    }

    return [PSCustomObject]@{
        detected = @($detected.ToArray())
        removed = @($removed.ToArray())
        errors = @($errors.ToArray())
    }
}

function Convert-V2InvariantDouble {
    param([string]$Text)

    $parsed = 0.0
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return 0.0
    }
    $normalized = ([string]$Text).Trim().Replace("%", "").Replace(",", ".")
    if ([double]::TryParse($normalized, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
        return [Math]::Round($parsed, 1)
    }
    return 0.0
}

# -------------------------------------------------------------------------------
$warnings = New-Object System.Collections.Generic.List[string]
$ramPct = 0.0
$ramFreeGB = 0.0
$ramTotalGB = 0.0
$ramMetricAvailable = $false
$ramMetricSource = ""

$memorySnapshot = Get-V2SystemMemorySnapshot
if ([bool](Get-V2OptionalProperty -InputObject $memorySnapshot -Name "success" -DefaultValue $false)) {
    $ramMetricAvailable = $true
    $ramPct = [double](Get-V2OptionalProperty -InputObject $memorySnapshot -Name "used_pct" -DefaultValue 0.0)
    $ramFreeGB = [double](Get-V2OptionalProperty -InputObject $memorySnapshot -Name "free_gb" -DefaultValue 0.0)
    $ramTotalGB = [double](Get-V2OptionalProperty -InputObject $memorySnapshot -Name "total_gb" -DefaultValue 0.0)
    $ramMetricSource = [string](Get-V2OptionalProperty -InputObject $memorySnapshot -Name "source" -DefaultValue "")
    Write-Host ("[FinOps] RAM: {0}% used ({1} GB free / {2} GB total) source={3}" -f $ramPct, $ramFreeGB, $ramTotalGB, $ramMetricSource)
}
else {
    $warn = "[FinOps] Could not read system RAM (all providers failed)"
    $warnings.Add($warn)
    Write-Warning $warn
}

# -------------------------------------------------------------------------------
$cpuPct = 0.0
$cpuMetricAvailable = $false
$cpuMetricSource = ""
$cpuWarnPending = ""

$cpuSnapshot = Get-V2SystemCpuSnapshot
if ([bool](Get-V2OptionalProperty -InputObject $cpuSnapshot -Name "success" -DefaultValue $false)) {
    $cpuMetricAvailable = $true
    $cpuPct = [double](Get-V2OptionalProperty -InputObject $cpuSnapshot -Name "used_pct" -DefaultValue 0.0)
    $cpuMetricSource = [string](Get-V2OptionalProperty -InputObject $cpuSnapshot -Name "source" -DefaultValue "")
    Write-Host ("[FinOps] CPU: {0}% load source={1}" -f $cpuPct, $cpuMetricSource)
}
else {
    $cpuWarnPending = "[FinOps] Could not read CPU (all providers failed)"
}

# -------------------------------------------------------------------------------
$gpuPct = 0.0
$gpuVramUsedMb = 0.0
$gpuVramTotalMb = 0.0
$gpuVramUsedPct = 0.0
$gpuTempC = 0.0
$gpuMetricAvailable = $false
$gpuMetricSource = ""

$gpuSnapshot = Get-V2GpuSnapshot
if ([bool](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "success" -DefaultValue $false)) {
    $gpuMetricAvailable = $true
    $gpuPct = [double](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "gpu_util_pct" -DefaultValue 0.0)
    $gpuVramUsedMb = [double](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "vram_used_mb" -DefaultValue 0.0)
    $gpuVramTotalMb = [double](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "vram_total_mb" -DefaultValue 0.0)
    $gpuVramUsedPct = [double](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "vram_used_pct" -DefaultValue 0.0)
    $gpuTempC = [double](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "temperature_c" -DefaultValue 0.0)
    $gpuMetricSource = [string](Get-V2OptionalProperty -InputObject $gpuSnapshot -Name "source" -DefaultValue "")
    Write-Host ("[FinOps] GPU: {0}% util | VRAM {1}/{2} MB ({3}%) | Temp {4}C source={5}" -f $gpuPct, $gpuVramUsedMb, $gpuVramTotalMb, $gpuVramUsedPct, $gpuTempC, $gpuMetricSource)
}
else {
    $warnings.Add("[FinOps] Could not read GPU metrics (nvidia-smi unavailable)")
}

$memorySyncMetrics = Get-V2LatestMemorySyncMetrics -ProjectRoot $resolvedPath
$loopLlmMetrics = Get-V2LoopLlmMetrics -ProjectRoot $resolvedPath
$ollamaProcessorMix = Get-V2OllamaProcessorMix

if ([bool](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "found" -DefaultValue $false)) {
    $embeddingProcessor = [string](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "embedding_runtime_processor" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($embeddingProcessor) -and $embeddingProcessor.ToLowerInvariant() -ne "gpu") {
        $warnings.Add(("[FinOps] Offload alert: embeddings running on '{0}' instead of GPU." -f $embeddingProcessor))
    }
    $maintenanceAlertCount = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_maintenance_alert_count" -DefaultValue 0)
    $maintenanceOk = [bool](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_maintenance_ok" -DefaultValue $true)
    if (-not $maintenanceOk -or $maintenanceAlertCount -gt 0) {
        $fragPct = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_fragmentation_percent" -DefaultValue 0.0)
        $coveragePct = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_vector_index_coverage_percent" -DefaultValue 0.0)
        $segmentsCount = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_segments_count" -DefaultValue 0)
        $warnings.Add(("[FinOps] Qdrant maintenance alert: alerts={0} fragmentation={1}% coverage={2}% segments={3}" -f $maintenanceAlertCount, $fragPct, $coveragePct, $segmentsCount))
    }
}
if ([bool](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "available" -DefaultValue $false)) {
    $cpuModels = [int](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "cpu_models" -DefaultValue 0)
    $gpuModels = [int](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "gpu_models" -DefaultValue 0)
    if ($cpuModels -gt 0 -and $gpuModels -eq 0) {
        $warnings.Add("[FinOps] Offload alert: Ollama processor mix indicates CPU-only runtime.")
    }
}

# -------------------------------------------------------------------------------
$containerStats = New-Object System.Collections.Generic.List[object]
$dockerAvailable = $false

try {
    $dockerOutput = & docker stats --no-stream --format "{{.Name}}`t{{.CPUPerc}}`t{{.MemUsage}}`t{{.MemPerc}}" 2>&1
    if ($LASTEXITCODE -eq 0 -and $dockerOutput) {
        $dockerAvailable = $true
        foreach ($line in @($dockerOutput)) {
            $parts = $line -split "`t"
            if ($parts.Count -lt 4) { continue }
            $cName   = $parts[0].Trim()
            $cCpu    = $parts[1].Trim() -replace "%", ""
            $cMem    = $parts[2].Trim()
            $cMemPct = $parts[3].Trim() -replace "%", ""
            $cpuValue = Convert-V2InvariantDouble -Text $cCpu
            $memPctValue = Convert-V2InvariantDouble -Text $cMemPct

            $containerStats.Add([PSCustomObject]@{
                name      = $cName
                cpu_pct   = $cpuValue
                mem_usage = $cMem
                mem_pct   = $memPctValue
            })
        }
        Write-Host ("[FinOps] Docker: {0} containers monitored" -f $containerStats.Count)
    }
    elseif ($LASTEXITCODE -ne 0) {
        $warn = "[FinOps] Docker stats command failed"
        $warnings.Add($warn)
        Write-Warning $warn
    }
}
catch {
    $warn = "[FinOps] Docker stats unavailable: $($_.Exception.Message)"
    $warnings.Add($warn)
    Write-Warning $warn
}

$coreGateCleanup = [PSCustomObject]@{
    removed = @()
    errors = @()
}
if ($dockerAvailable) {
    $coreGateCleanup = Remove-V2StaleCoreGateContainers -DryRunMode:$DryRun
    $removedCoreGate = @((Get-V2OptionalProperty -InputObject $coreGateCleanup -Name "removed" -DefaultValue @()))
    if ($removedCoreGate.Count -gt 0) {
        Write-Host ("[FinOps] Core-gate cleanup removed: {0}" -f ($removedCoreGate -join ", "))
    }
    foreach ($cleanupError in @((Get-V2OptionalProperty -InputObject $coreGateCleanup -Name "errors" -DefaultValue @()))) {
        if (-not [string]::IsNullOrWhiteSpace([string]$cleanupError)) {
            $warnings.Add("[FinOps] core-gate cleanup error: " + [string]$cleanupError)
        }
    }
}

$zombieFindstrCleanup = Remove-V2ZombieFindstrProcesses -DryRunMode:$DryRun -MinAgeMinutes 30 -MaxCpuSeconds 1.0
$zombieDetected = @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "detected" -DefaultValue @()))
$zombieRemoved = @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "removed" -DefaultValue @()))
if ($zombieRemoved.Count -gt 0) {
    Write-Host ("[FinOps] Zombie findstr cleanup removed: {0}" -f ($zombieRemoved -join "; "))
}
foreach ($zombieError in @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "errors" -DefaultValue @()))) {
    if (-not [string]::IsNullOrWhiteSpace([string]$zombieError)) {
        $warnings.Add("[FinOps] zombie findstr cleanup error: " + [string]$zombieError)
    }
}
if ($zombieDetected.Count -gt 0 -and $zombieRemoved.Count -eq 0 -and $DryRun) {
    $warnings.Add("[FinOps] zombie findstr candidates detected in dry-run mode.")
}

if (-not $cpuMetricAvailable -and $containerStats.Count -gt 0) {
    $cpuAggregate = 0.0
    foreach ($c in @($containerStats.ToArray())) {
        try {
            $cpuAggregate += [double](Get-V2OptionalProperty -InputObject $c -Name "cpu_pct" -DefaultValue 0.0)
        }
        catch {
        }
    }
    if ($cpuAggregate -gt 0) {
        # docker stats may exceed 100% on multi-core hosts; keep bounded for dashboard simplicity.
        if ($cpuAggregate -gt 100.0) {
            $cpuAggregate = 100.0
        }
        $cpuPct = [Math]::Round($cpuAggregate, 1)
        $cpuMetricAvailable = $true
        $cpuMetricSource = "docker-container-aggregate"
        Write-Host ("[FinOps] CPU: {0}% load source={1}" -f $cpuPct, $cpuMetricSource)
    }
}

if (-not $cpuMetricAvailable -and -not [string]::IsNullOrWhiteSpace($cpuWarnPending)) {
    $warnings.Add($cpuWarnPending)
    Write-Warning $cpuWarnPending
}
    # -------------------------------------------------------------------------------
$pausedContainers = New-Object System.Collections.Generic.List[string]
$resumedContainers = New-Object System.Collections.Generic.List[string]

function Test-V2WorkerLikeContainer {
    param([string]$ContainerName)

    if ([string]::IsNullOrWhiteSpace($ContainerName)) {
        return $false
    }
    $name = $ContainerName.Trim().ToLowerInvariant()
    return (
        $name -match "worker" -or
        $name -match "queue" -or
        $name -match "job" -or
        $name -match "scheduler" -or
        $name -match "cron" -or
        $name -match "beat"
    )
}

if ($dockerAvailable -and $containerStats.Count -gt 0) {
    if ($ramPct -ge $RamCriticalThresholdPct) {
        Write-Host ("[FinOps] RAM at {0}% - pausing non-critical containers..." -f $ramPct)
        $pauseCandidates = New-Object System.Collections.Generic.List[object]
        foreach ($c in @($containerStats.ToArray())) {
            $isCritical = $false
            foreach ($kw in $criticalKeywords) {
                if ([string]$c.name -like "*$kw*") { $isCritical = $true; break }
            }
            if ($isCritical) { continue }

            # Check if already paused
            $inspectOut = (& docker inspect --format "{{.State.Status}}" "$($c.name)" 2>&1 | Out-String).Trim()
            if ($inspectOut -eq "paused") { continue }
            if ($inspectOut -ne "running") { continue }

            $pauseCandidates.Add($c)
        }

        $workerFirst = @($pauseCandidates | Where-Object { Test-V2WorkerLikeContainer -ContainerName ([string]$_.name) })
        $otherNonCritical = @($pauseCandidates | Where-Object { -not (Test-V2WorkerLikeContainer -ContainerName ([string]$_.name)) })
        $orderedCandidates = @($workerFirst + $otherNonCritical)
        $maxPausesPerRun = [Math]::Max((Get-V2EnvInt -Name "ORCHESTRATOR_FINOPS_MAX_PAUSES_PER_RUN" -DefaultValue 2), 1)

        foreach ($c in $orderedCandidates) {
            if ($pausedContainers.Count -ge $maxPausesPerRun) {
                break
            }

            if (-not $DryRun) {
                $pauseOut = & docker pause "$($c.name)" 2>&1
                if ($LASTEXITCODE -eq 0) {
                    $pausedContainers.Add($c.name)
                    Write-Host ("[FinOps] Paused: {0}" -f $c.name)
                }
            }
            else {
                Write-Host ("[FinOps] [DryRun] Would pause: {0}" -f $c.name)
                $pausedContainers.Add($c.name)
            }
        }
    }
    elseif ($ramPct -le $RamResumeThresholdPct) {
        # Resume any previously auto-paused containers
        $pausedOut = & docker ps --filter status=paused --format "{{.Names}}" 2>&1
        foreach ($name in @($pausedOut | Where-Object { $_ })) {
            $name = $name.Trim()
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if (-not $DryRun) {
                $unpauseOut = & docker unpause "$name" 2>&1
                if ($LASTEXITCODE -eq 0) {
                    $resumedContainers.Add($name)
                    Write-Host ("[FinOps] Resumed: {0}" -f $name)
                }
            }
            else {
                Write-Host ("[FinOps] [DryRun] Would resume: {0}" -f $name)
                $resumedContainers.Add($name)
            }
        }
    }
}
    # -------------------------------------------------------------------------------
$report = [PSCustomObject]@{
    generated_at        = Get-V2Timestamp
    project             = Split-Path -Leaf $resolvedPath
    dry_run             = [bool]$DryRun
    system = [PSCustomObject]@{
        ram_metric_available = $ramMetricAvailable
        ram_metric_source = $ramMetricSource
        ram_used_pct    = $ramPct
        ram_free_gb     = $ramFreeGB
        ram_total_gb    = $ramTotalGB
        cpu_metric_available = $cpuMetricAvailable
        cpu_metric_source = $cpuMetricSource
        cpu_used_pct    = $cpuPct
        gpu_metric_available = $gpuMetricAvailable
        gpu_metric_source = $gpuMetricSource
        gpu_util_pct = $gpuPct
        gpu_vram_used_mb = $gpuVramUsedMb
        gpu_vram_total_mb = $gpuVramTotalMb
        gpu_vram_used_pct = $gpuVramUsedPct
        gpu_temperature_c = $gpuTempC
    }
    embedding_runtime = [PSCustomObject]@{
        found = [bool](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "found" -DefaultValue $false)
        processor = [string](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "embedding_runtime_processor" -DefaultValue "")
        vectors_per_second = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "vectors_per_second" -DefaultValue 0.0)
        ollama_vectors_per_second = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "ollama_vectors_per_second" -DefaultValue 0.0)
        ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "ollama_embeddings" -DefaultValue 0)
        non_ollama_embeddings = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "non_ollama_embeddings" -DefaultValue 0)
        qdrant_maintenance_ok = [bool](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_maintenance_ok" -DefaultValue $true)
        qdrant_maintenance_alert_count = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_maintenance_alert_count" -DefaultValue 0)
        qdrant_fragmentation_percent = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_fragmentation_percent" -DefaultValue 0.0)
        qdrant_vector_index_coverage_percent = [double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_vector_index_coverage_percent" -DefaultValue 0.0)
        qdrant_segments_count = [int](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "qdrant_segments_count" -DefaultValue 0)
    }
    llm_runtime = [PSCustomObject]@{
        found = [bool](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "found" -DefaultValue $false)
        enabled = [bool](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_enabled" -DefaultValue $false)
        model = [string](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_model" -DefaultValue "")
        models_used = @((Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_models_used" -DefaultValue @()))
        tokens_per_second = [double](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "tokens_per_second" -DefaultValue 0.0)
        total_tokens = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "total_tokens" -DefaultValue 0)
        llm_calls = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_calls" -DefaultValue 0)
        route_fast = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_route_fast" -DefaultValue 0)
        route_heavy = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_route_heavy" -DefaultValue 0)
        route_infra_default = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_route_infra_default" -DefaultValue 0)
        route_routing_disabled = [int](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "llm_route_routing_disabled" -DefaultValue 0)
    }
    ollama_runtime = [PSCustomObject]@{
        available = [bool](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "available" -DefaultValue $false)
        gpu_models = [int](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "gpu_models" -DefaultValue 0)
        cpu_models = [int](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "cpu_models" -DefaultValue 0)
        unknown_models = [int](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "unknown_models" -DefaultValue 0)
        processor_mix = [string](Get-V2OptionalProperty -InputObject $ollamaProcessorMix -Name "summary" -DefaultValue "unavailable")
    }
    maintenance = [PSCustomObject]@{
        core_gate_removed = @((Get-V2OptionalProperty -InputObject $coreGateCleanup -Name "removed" -DefaultValue @()))
        core_gate_cleanup_errors = @((Get-V2OptionalProperty -InputObject $coreGateCleanup -Name "errors" -DefaultValue @()))
        zombie_findstr_detected = @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "detected" -DefaultValue @()))
        zombie_findstr_removed = @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "removed" -DefaultValue @()))
        zombie_findstr_errors = @((Get-V2OptionalProperty -InputObject $zombieFindstrCleanup -Name "errors" -DefaultValue @()))
    }
    containers          = @($containerStats.ToArray())
    paused_this_run     = @($pausedContainers.ToArray())
    resumed_this_run    = @($resumedContainers.ToArray())
    warnings            = @($warnings.ToArray())
    ram_critical_threshold = $RamCriticalThresholdPct
    ram_resume_threshold   = $RamResumeThresholdPct
}
Save-V2JsonContent -Path $reportPath -Value $report
    # -------------------------------------------------------------------------------
$resourceOk = $true
if ($ramMetricAvailable) {
    $resourceOk = ($ramPct -lt $RamCriticalThresholdPct)
}

if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
    try {
        $dag = Get-V2JsonContent -Path $dagPath

        if (-not $ramMetricAvailable) {
            # Do not mutate resource repair tasks if RAM metric is unavailable.
        }
        elseif ($resourceOk) {
            $resolved = 0
            foreach ($task in @($dag.tasks)) {
                $tId     = [string](Get-V2OptionalProperty -InputObject $task -Name "id"     -DefaultValue "")
                $tStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                if ($tId -like "REPAIR-RESOURCE-*" -and $tStatus -in @("pending", "in-progress")) {
                    Set-V2DynamicProperty -InputObject $task -Name "status"          -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at"    -Value (Get-V2Timestamp)
                    Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: RAM back to $ramPct%"
                    $resolved++
                }
            }
            if ($resolved -gt 0) {
                Save-V2JsonContent -Path $dagPath -Value $dag
                Write-Host ("[FinOps] Auto-resolved {0} REPAIR-RESOURCE task(s)." -f $resolved)
            }
        }
        else {
            $alreadyOpen = @($dag.tasks | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -like "REPAIR-RESOURCE-*" -and
                [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -in @("pending", "in-progress")
            }).Count -gt 0

            if (-not $alreadyOpen) {
                $repairId  = "REPAIR-RESOURCE-$ts"
                $details   = "RAM: $ramPct% (threshold: $RamCriticalThresholdPct%); CPU: $cpuPct%"
                $dag.tasks += [PSCustomObject]@{
                    id             = $repairId
                    title          = "Investigate resource pressure (RAM $ramPct%)"
                    description    = "FinOps: $details"
                    reason         = "resource-pressure"
                    priority       = "P1"
                    dependencies   = @()
                    status         = "pending"
                    execution_mode = "artifact-validation"
                    source_report  = $reportPath
                    created_at     = Get-V2Timestamp
                    updated_at     = Get-V2Timestamp
                }
                Save-V2JsonContent -Path $dagPath -Value $dag
                Write-Host "[FinOps] Created REPAIR task: $repairId"
            }
        }
    }
    catch {
        Write-Warning "[FinOps] Could not update task-dag: $($_.Exception.Message)"
    }
}

$cpuText = if ($cpuMetricAvailable) { ("{0}%" -f $cpuPct) } else { "n/a" }
$gpuText = if ($gpuMetricAvailable) { ("{0}% util, VRAM {1}% ({2}/{3}MB)" -f $gpuPct, $gpuVramUsedPct, $gpuVramUsedMb, $gpuVramTotalMb) } else { "n/a" }
$vectorsText = if ([bool](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "found" -DefaultValue $false)) { ("{0}/s" -f ([double](Get-V2OptionalProperty -InputObject $memorySyncMetrics -Name "vectors_per_second" -DefaultValue 0.0))) } else { "n/a" }
$tokensText = if ([bool](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "found" -DefaultValue $false)) { ("{0}/s" -f ([double](Get-V2OptionalProperty -InputObject $loopLlmMetrics -Name "tokens_per_second" -DefaultValue 0.0))) } else { "n/a" }
$summary = if (-not $ramMetricAvailable) {
    "UNKNOWN (RAM metric unavailable; CPU: $cpuText; GPU: $gpuText; vectors: $vectorsText; tokens: $tokensText)"
}
elseif ($resourceOk) {
    "OK (RAM: $ramPct%, CPU: $cpuText, GPU: $gpuText, vectors: $vectorsText, tokens: $tokensText)"
}
else {
    "PRESSURE - RAM: $ramPct% exceeds $RamCriticalThresholdPct% threshold (CPU: $cpuText, GPU: $gpuText)"
}
Write-Host "[FinOps] $summary"
Write-Output ($report | ConvertTo-Json -Depth 5)
