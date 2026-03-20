<#
.SYNOPSIS
    V1 Change-Aware Observer — monitors a project and refreshes world model, memory, and health.
.DESCRIPTION
    Watches a submitted project for changes (file modifications, new commits, failed tests)
    and triggers: world model refresh, memory sync, health check update, and self-healing
    incident generation when issues are detected.
    This is the V1 observer. For V2 projects use scripts/v2/Invoke-ObserverV2.ps1 instead.
    Can run continuously or as a one-shot scan.
.PARAMETER ProjectPath
    Path to the project to observe. Defaults to current directory.
.PARAMETER IntervalSeconds
    Polling interval for continuous mode. Default: 300 (5 minutes).
.PARAMETER RunOnce
    If set, performs one observation pass and exits.
.EXAMPLE
    .\scripts\Observe-Project.ps1 -ProjectPath C:\projects\myapp -RunOnce
    .\scripts\Observe-Project.ps1 -ProjectPath C:\projects\myapp -IntervalSeconds 120
#>param(
    [string]$ProjectPath = ".",
    [string]$ProjectSlug,
    [string]$StatePath,
    [int]$IntervalSeconds = 300,
    [switch]$SkipMemorySync,
    [switch]$SkipHealthChecks,
    [switch]$AllowInferredCommands,
    [switch]$RunOnce,
    [int]$CommandTimeoutSeconds = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

function Get-ProjectSlug {
    param([string]$Name)

    $slug = $Name.ToLowerInvariant() -replace "[^a-z0-9]+", "-"
    $slug = $slug.Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) {
        return "project"
    }

    return $slug
}

function Ensure-Directory {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-JsonContent {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-OptionalValue {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$DefaultValue = $null
    )

    if ($null -eq $InputObject) {
        return $DefaultValue
    }
    try {
        if ($InputObject -is [System.Collections.IDictionary]) {
            if ($InputObject.Contains($Name)) {
                return $InputObject[$Name]
            }
            return $DefaultValue
        }
        $prop = $InputObject.PSObject.Properties[$Name]
        if ($null -ne $prop) {
            return $prop.Value
        }
    }
    catch {
    }
    return $DefaultValue
}

function Save-JsonContent {
    param(
        [string]$Path,
        [object]$Value
    )

    $directory = Split-Path -Parent $Path
    if ($directory) {
        Ensure-Directory -Path $directory
    }

    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12))
}

function Get-ObserverGpuPolicy {
    param([string]$ProjectRoot)

    $defaultPolicy = [PSCustomObject]@{
        enabled = $true
        ollama  = [PSCustomObject]@{
            embed_url      = "http://localhost:11434/v1/embeddings"
            embed_model    = "all-minilm:latest"
            embed_model_candidates = @("mxbai-embed-large:latest", "nomic-embed-text:latest", "all-minilm:latest")
            install_missing_models = $true
            max_install_attempts = 1
            keep_alive     = "10m"
            embed_batch_size = 24
            embed_batch_size_auto = $true
            embed_concurrency = 4
            embed_warmup_inputs = 32
            num_gpu        = "1"
            num_thread     = "4"
            cuda_devices   = "0"
            nvidia_devices = "all"
        }
    }

    $configDir = Join-Path $ProjectRoot "ai-orchestrator/config"
    Ensure-Directory -Path $configDir
    $policyPath = Join-Path $configDir "gpu-acceleration-policy.json"
    if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
        Save-JsonContent -Path $policyPath -Value $defaultPolicy
        return $defaultPolicy
    }

    $loaded = Get-JsonContent -Path $policyPath
    if (-not $loaded) {
        return $defaultPolicy
    }
    return $loaded
}

function Get-InstalledOllamaModels {
    $models = New-Object System.Collections.Generic.List[string]
    try {
        $lines = @(ollama list 2>$null)
        foreach ($line in $lines) {
            $text = [string]$line
            if ([string]::IsNullOrWhiteSpace($text)) { continue }
            if ($text.TrimStart().StartsWith("NAME")) { continue }
            $parts = @($text -split "\s+" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($parts.Count -lt 1) { continue }
            $name = [string]$parts[0]
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if ($models -notcontains $name) { $models.Add($name) }
        }
    }
    catch {
    }
    return @($models.ToArray())
}

function Resolve-ObserverOllamaEmbedModel {
    param([object]$OllamaConfig)

    $configuredModel = [string](Get-OptionalValue -InputObject $OllamaConfig -Name "embed_model" -DefaultValue "all-minilm:latest")
    $candidateInput = Get-OptionalValue -InputObject $OllamaConfig -Name "embed_model_candidates" -DefaultValue @()
    $candidateList = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($candidateInput)) {
        $name = [string]$item
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($candidateList -notcontains $name) { $candidateList.Add($name.Trim()) }
    }
    if (-not [string]::IsNullOrWhiteSpace($configuredModel) -and $candidateList -notcontains $configuredModel) {
        $candidateList.Insert(0, $configuredModel)
    }
    if ($candidateList.Count -eq 0) {
        $candidateList.Add("mxbai-embed-large:latest")
        $candidateList.Add("nomic-embed-text:latest")
        $candidateList.Add("all-minilm:latest")
    }

    $normalizeName = {
        param([string]$ModelName)
        $name = [string]$ModelName
        if ([string]::IsNullOrWhiteSpace($name)) { return "" }
        $normalized = $name.Trim().ToLowerInvariant()
        if ($normalized -notmatch ":[^/]+$") { $normalized = "$normalized`:latest" }
        return $normalized
    }

    $installMissing = [bool](Get-OptionalValue -InputObject $OllamaConfig -Name "install_missing_models" -DefaultValue $true)
    $maxInstallAttempts = [int](Get-OptionalValue -InputObject $OllamaConfig -Name "max_install_attempts" -DefaultValue 1)
    if ($maxInstallAttempts -lt 1) { $maxInstallAttempts = 1 }

    $installed = @(Get-InstalledOllamaModels)
    $installedSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($model in $installed) {
        $normalized = & $normalizeName $model
        if (-not [string]::IsNullOrWhiteSpace($normalized)) {
            [void]$installedSet.Add($normalized)
        }
    }

    $selected = ""
    foreach ($candidate in @($candidateList.ToArray())) {
        if ($installedSet.Contains((& $normalizeName $candidate))) {
            $selected = $candidate
            break
        }
    }

    if ([string]::IsNullOrWhiteSpace($selected) -and $installMissing) {
        $attempts = 0
        foreach ($candidate in @($candidateList.ToArray())) {
            if ($attempts -ge $maxInstallAttempts) { break }
            $attempts += 1
            try {
                & ollama pull $candidate | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    $selected = $candidate
                    break
                }
            }
            catch {
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = $configuredModel
    }
    return $selected
}

function Get-RelativeUnixPath {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath)
    $targetFullPath = [System.IO.Path]::GetFullPath($TargetPath)

    if (-not $baseFullPath.EndsWith("\")) {
        $baseFullPath = "$baseFullPath\"
    }

    if (Test-Path -LiteralPath $TargetPath -PathType Container) {
        if (-not $targetFullPath.EndsWith("\")) {
            $targetFullPath = "$targetFullPath\"
        }
    }

    $baseUri = [System.Uri]::new($baseFullPath)
    $targetUri = [System.Uri]::new($targetFullPath)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString())
}

function Get-ProjectFingerprint {
    param([string]$RootPath)

    $excludedPatterns = @(
        '(^|/)workspace/state/',
        '(^|/)self_healing/',
        '(^|/)\.git/',
        '(^|/)node_modules/',
        '(^|/)vendor/',
        '(^|/)dist/',
        '(^|/)build/',
        '(^|/)\.next/',
        '(^|/)target/',
        '(^|/)bin/',
        '(^|/)obj/',
        '(^|/)coverage/',
        '(^|/)infra/.+/docker/'
    )

    $items = New-Object System.Collections.Generic.List[string]
    $files = Get-ChildItem -LiteralPath $RootPath -Recurse -File -Force
    foreach ($file in $files) {
        $relativePath = Get-RelativeUnixPath -BasePath $RootPath -TargetPath $file.FullName
        $shouldSkip = $false
        foreach ($pattern in $excludedPatterns) {
            if ($relativePath -match $pattern) {
                $shouldSkip = $true
                break
            }
        }

        if ($shouldSkip) {
            continue
        }

        $items.Add("$relativePath|$($file.Length)|$($file.LastWriteTimeUtc.Ticks)")
    }

    $hashSource = ($items | Sort-Object) -join "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($hashSource)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }

    return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

function Get-SafeCommandText {
    param([string]$CommandText)

    if ([string]::IsNullOrWhiteSpace($CommandText)) {
        return ""
    }

    return (($CommandText -replace "\s+#\s+REVIEW_REQUIRED.*$", "").Trim())
}

function Invoke-ObservedCommand {
    param(
        [string]$CommandText,
        [string]$WorkingDirectory,
        [int]$TimeoutSeconds
    )

    $safeCommand = Get-SafeCommandText -CommandText $CommandText
    if ([string]::IsNullOrWhiteSpace($safeCommand)) {
        return [PSCustomObject]@{
            Command  = $CommandText
            ExitCode = $null
            TimedOut = $false
            Output   = "Command was empty after normalization."
        }
    }

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $cmdArguments = "/d /s /c `"cd /d `"$WorkingDirectory`" && $safeCommand`""
        $process = Start-Process -FilePath $env:ComSpec `
            -ArgumentList $cmdArguments `
            -WorkingDirectory $WorkingDirectory `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -WindowStyle Hidden

        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            try {
                $process.Kill()
            }
            catch {
            }
        }
        else {
            $process.WaitForExit()
        }

        $stdoutText = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderrText = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        $combinedOutput = @($stdoutText, $stderrText) -join [Environment]::NewLine

        return [PSCustomObject]@{
            Command  = $safeCommand
            ExitCode = if ($timedOut) { $null } else { $process.ExitCode }
            TimedOut = $timedOut
            Output   = $combinedOutput.Trim()
        }
    }
    finally {
        foreach ($tempPath in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $tempPath) {
                Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Get-OutputTail {
    param(
        [string]$Text,
        [int]$MaxLines = 80
    )

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return "No output captured."
    }

    $lines = $Text -split "(`r`n|`n|`r)"
    $selected = @($lines | Select-Object -Last $MaxLines)
    return ($selected -join [Environment]::NewLine).Trim()
}

function Write-Incident {
    param(
        [string]$SelfHealingDir,
        [string]$ProjectSlug,
        [string]$Category,
        [string]$Title,
        [string]$Details,
        [string]$CommandText = "",
        [string]$CommandOutput = ""
    )

    Ensure-Directory -Path $SelfHealingDir
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $safeCategory = ($Category.ToLowerInvariant() -replace "[^a-z0-9]+", "_").Trim("_")
    $incidentPath = Join-Path $SelfHealingDir "INCIDENT_${timestamp}_${safeCategory}.md"

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Incident")
    $lines.Add("")
    $lines.Add("- Project: $ProjectSlug")
    $lines.Add("- Category: $Category")
    $lines.Add("- Created At: $(Get-Date -Format s)")
    $lines.Add("")
    $lines.Add("## Title")
    $lines.Add($Title)
    $lines.Add("")
    $lines.Add("## Details")
    $lines.Add($Details)
    if (-not [string]::IsNullOrWhiteSpace($CommandText)) {
        $lines.Add("")
        $lines.Add("## Command")
        $lines.Add('```text')
        $lines.Add($CommandText)
        $lines.Add('```')
    }
    if (-not [string]::IsNullOrWhiteSpace($CommandOutput)) {
        $lines.Add("")
        $lines.Add("## Output Tail")
        $lines.Add('```text')
        $lines.Add((Get-OutputTail -Text $CommandOutput))
        $lines.Add('```')
    }

    [System.IO.File]::WriteAllText($incidentPath, ($lines -join [Environment]::NewLine))
    return $incidentPath
}

function Get-HealthCheckPlan {
    param(
        [object]$Intake,
        [switch]$AllowInferred
    )

    $checks = New-Object System.Collections.Generic.List[object]

    foreach ($candidate in @(
        [PSCustomObject]@{ Name = "build"; Data = $Intake.Commands.Build },
        [PSCustomObject]@{ Name = "test"; Data = $Intake.Commands.Test }
    )) {
        if (-not $candidate.Data) {
            continue
        }

        if ($candidate.Data.Value -eq "unknown") {
            continue
        }

        if ($candidate.Data.Confidence -eq "verified" -or $AllowInferred) {
            $checks.Add([PSCustomObject]@{
                Name       = $candidate.Name
                Command    = $candidate.Data.Value
                Confidence = $candidate.Data.Confidence
            })
        }
    }

    return @($checks)
}

function New-HealthMarkdown {
    param([object]$Summary)

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Health Report")
    $lines.Add("")
    $lines.Add("- Project: $($Summary.ProjectSlug)")
    $lines.Add("- Generated At: $($Summary.GeneratedAt)")
    $lines.Add("- Fingerprint: $($Summary.Fingerprint)")
    $lines.Add("- Overall Status: $($Summary.OverallStatus)")
    $lines.Add("- Open Questions: $($Summary.OpenQuestionCount)")
    $lines.Add("- World Model Status: $($Summary.WorldModelStatus)")
    $lines.Add("- Memory Sync Status: $($Summary.MemorySyncStatus)")
    $lines.Add("")
    $lines.Add("## Checks")
    if (@($Summary.Checks).Count -eq 0) {
        $lines.Add("- none executed")
    }
    else {
        foreach ($check in $Summary.Checks) {
            $lines.Add("- [$($check.Status)] $($check.Name): $($check.Command) ($($check.Confidence))")
        }
    }

    if (@($Summary.IncidentPaths).Count -gt 0) {
        $lines.Add("")
        $lines.Add("## Incidents")
        foreach ($incidentPath in $Summary.IncidentPaths) {
            $lines.Add("- $incidentPath")
        }
    }

    return ($lines -join [Environment]::NewLine)
}

$resolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
if ([string]::IsNullOrWhiteSpace($ProjectSlug)) {
    $ProjectSlug = Get-ProjectSlug -Name (Split-Path -Leaf $resolvedProjectPath)
}

if ([string]::IsNullOrWhiteSpace($StatePath)) {
    $StatePath = Join-Path $resolvedProjectPath "workspace\state\$ProjectSlug"
}

Ensure-Directory -Path $StatePath

$projectStateFile = Join-Path $StatePath "PROJECT_STATE.json"
$observerStateFile = Join-Path $StatePath "OBSERVER_STATE.json"
$healthReportJsonPath = Join-Path $StatePath "HEALTH_REPORT.json"
$healthReportMdPath = Join-Path $StatePath "HEALTH_REPORT.md"
$intakeReportPath = Join-Path $StatePath "INTAKE_REPORT.md"
$intakeScript = Join-Path $PSScriptRoot "Invoke-ProjectIntake.ps1"
$memorySyncScript = Join-Path $PSScriptRoot "memory_sync.py"
$worldModelScript = Join-Path $PSScriptRoot "extract_world_model.py"
$selfHealingDir = Join-Path $resolvedProjectPath "self_healing"
$worldModelOutputPath = Join-Path $StatePath "WORLD_MODEL_AUTO.md"
$worldModelJsonPath = Join-Path $StatePath "WORLD_MODEL_AUTO.json"

Ensure-Directory -Path $selfHealingDir

Write-Host "--- AI Project Observer: Self-Healing & Learning Loop ---" -ForegroundColor Cyan
Write-Host "Monitoring project path: $resolvedProjectPath"
Write-Host "Using project slug: $ProjectSlug"
Write-Host "Using state path: $StatePath"

while ($true) {
    $scanTimestamp = (Get-Date).ToString("s")
    Write-Host "[$scanTimestamp] Scanning project health..." -ForegroundColor Gray

    $fingerprint = Get-ProjectFingerprint -RootPath $resolvedProjectPath
    $observerState = Get-JsonContent -Path $observerStateFile
    if ($observerState -and $observerState.LastFingerprint -eq $fingerprint) {
        Write-Host "No relevant changes detected. Skipping deep refresh." -ForegroundColor DarkGray
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    $incidentPaths = New-Object System.Collections.Generic.List[string]
    $checks = New-Object System.Collections.Generic.List[object]
    $overallStatus = "healthy"
    $worldModelStatus = "generated"
    $memorySyncStatus = "skipped"
    $openQuestionCount = 0
    $projectState = Get-JsonContent -Path $projectStateFile

    try {
        $intakeJsonText = & $intakeScript -ProjectPath $resolvedProjectPath -OutputPath $intakeReportPath -EmitJson
        $intake = ($intakeJsonText | Out-String) | ConvertFrom-Json
        $openQuestionCount = @($intake.Questions).Count
    }
    catch {
        $intake = $null
        $overallStatus = "unhealthy"
        $incidentPaths.Add((Write-Incident -SelfHealingDir $selfHealingDir -ProjectSlug $ProjectSlug -Category "intake" -Title "Intake refresh failed" -Details $_.Exception.Message))
    }

    if ($intake -and $openQuestionCount -gt 0 -and $overallStatus -eq "healthy") {
        $overallStatus = "needs-answers"
        Write-Host "Open questions still exist. Observer will not claim full alignment." -ForegroundColor Yellow
    }

    try {
        python $worldModelScript --project-path $resolvedProjectPath --project-slug $ProjectSlug --output-path $worldModelOutputPath --json-output-path $worldModelJsonPath | Out-Null
    }
    catch {
        $worldModelStatus = "failed"
        $overallStatus = "unhealthy"
        $incidentPaths.Add((Write-Incident -SelfHealingDir $selfHealingDir -ProjectSlug $ProjectSlug -Category "world-model" -Title "World model extraction failed" -Details $_.Exception.Message))
    }

    if (-not $SkipMemorySync) {
        $gpuPolicy = Get-ObserverGpuPolicy -ProjectRoot $resolvedProjectPath
        $gpuEnabled = [bool](Get-OptionalValue -InputObject $gpuPolicy -Name "enabled" -DefaultValue $true)
        $ollamaCfg = Get-OptionalValue -InputObject $gpuPolicy -Name "ollama" -DefaultValue ([PSCustomObject]@{})
        $memoryArguments = @($memorySyncScript, "--project-slug", $ProjectSlug)
        if (-not $projectState -or -not $projectState.IncludeNeo4j) {
            $memoryArguments += "--skip-neo4j"
        }
        if (-not $projectState -or -not $projectState.IncludeQdrant) {
            $memoryArguments += "--skip-qdrant"
        }
        if ($gpuEnabled -and $ollamaCfg) {
            $embedUrl = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_url" -DefaultValue "")
            $embedModel = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_model" -DefaultValue "")
            $resolvedModel = Resolve-ObserverOllamaEmbedModel -OllamaConfig $ollamaCfg
            if (-not [string]::IsNullOrWhiteSpace($resolvedModel)) { $embedModel = $resolvedModel }
            $keepAlive = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "keep_alive" -DefaultValue "")
            $embedBatchSize = [int](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_batch_size" -DefaultValue 24)
            $embedBatchSizeAuto = [bool](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_batch_size_auto" -DefaultValue $true)
            $embedConcurrency = [int](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_concurrency" -DefaultValue 4)
            $embedWarmupInputs = [int](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_warmup_inputs" -DefaultValue 32)
            if (-not [string]::IsNullOrWhiteSpace($embedUrl)) { $memoryArguments += @("--ollama-url", $embedUrl) }
            if (-not [string]::IsNullOrWhiteSpace($embedModel)) { $memoryArguments += @("--ollama-model", $embedModel) }
            if (-not [string]::IsNullOrWhiteSpace($keepAlive)) { $memoryArguments += @("--ollama-keep-alive", $keepAlive) }
            if ($embedBatchSize -gt 0) { $memoryArguments += @("--ollama-embed-batch-size", [string]$embedBatchSize) }
            if ($embedBatchSizeAuto) { $memoryArguments += "--ollama-embed-batch-size-auto" }
            if ($embedConcurrency -gt 0) { $memoryArguments += @("--ollama-embed-concurrency", [string]$embedConcurrency) }
            if ($embedWarmupInputs -ge 0) { $memoryArguments += @("--ollama-embed-warmup-inputs", [string]$embedWarmupInputs) }
        }

        $prevOllamaEmbedUrl = [string]$env:OLLAMA_EMBED_URL
        $prevOllamaEmbedModel = [string]$env:OLLAMA_EMBED_MODEL
        $prevOllamaKeepAlive = [string]$env:OLLAMA_KEEP_ALIVE
        $prevOllamaEmbedBatchSize = [string]$env:OLLAMA_EMBED_BATCH_SIZE
        $prevOllamaEmbedBatchSizeAuto = [string]$env:OLLAMA_EMBED_BATCH_SIZE_AUTO
        $prevOllamaEmbedConcurrency = [string]$env:OLLAMA_EMBED_CONCURRENCY
        $prevOllamaEmbedWarmupInputs = [string]$env:OLLAMA_EMBED_WARMUP_INPUTS
        $prevOllamaNumGpu = [string]$env:OLLAMA_NUM_GPU
        $prevOllamaNumThread = [string]$env:OLLAMA_NUM_THREAD
        $prevCudaVisibleDevices = [string]$env:CUDA_VISIBLE_DEVICES
        $prevNvidiaVisibleDevices = [string]$env:NVIDIA_VISIBLE_DEVICES
        try {
            if ($gpuEnabled -and $ollamaCfg) {
                $cfgEmbedUrl = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_url" -DefaultValue "")
                $cfgEmbedModel = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_model" -DefaultValue "")
                $cfgKeepAlive = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "keep_alive" -DefaultValue "")
                $cfgEmbedBatchSize = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_batch_size" -DefaultValue "")
                $cfgEmbedBatchSizeAuto = [bool](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_batch_size_auto" -DefaultValue $true)
                $cfgEmbedConcurrency = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_concurrency" -DefaultValue "")
                $cfgEmbedWarmupInputs = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "embed_warmup_inputs" -DefaultValue "")
                $cfgNumGpu = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "num_gpu" -DefaultValue "")
                $cfgNumThread = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "num_thread" -DefaultValue "")
                $cfgCudaDevices = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "cuda_devices" -DefaultValue "")
                $cfgNvidiaDevices = [string](Get-OptionalValue -InputObject $ollamaCfg -Name "nvidia_devices" -DefaultValue "")
                if ($cfgEmbedUrl) { $env:OLLAMA_EMBED_URL = $cfgEmbedUrl }
                if ($cfgEmbedModel) { $env:OLLAMA_EMBED_MODEL = $cfgEmbedModel }
                if ($cfgKeepAlive) { $env:OLLAMA_KEEP_ALIVE = $cfgKeepAlive }
                if ($cfgEmbedBatchSize) { $env:OLLAMA_EMBED_BATCH_SIZE = $cfgEmbedBatchSize }
                $env:OLLAMA_EMBED_BATCH_SIZE_AUTO = if ($cfgEmbedBatchSizeAuto) { "1" } else { "0" }
                if ($cfgEmbedConcurrency) { $env:OLLAMA_EMBED_CONCURRENCY = $cfgEmbedConcurrency }
                if ($cfgEmbedWarmupInputs) { $env:OLLAMA_EMBED_WARMUP_INPUTS = $cfgEmbedWarmupInputs }
                if ($cfgNumGpu) { $env:OLLAMA_NUM_GPU = $cfgNumGpu }
                if ($cfgNumThread) { $env:OLLAMA_NUM_THREAD = $cfgNumThread }
                if ($cfgCudaDevices) { $env:CUDA_VISIBLE_DEVICES = $cfgCudaDevices }
                if ($cfgNvidiaDevices) { $env:NVIDIA_VISIBLE_DEVICES = $cfgNvidiaDevices }
            }
            python @memoryArguments | Out-Null
            $memorySyncStatus = "synced"
        }
        catch {
            $memorySyncStatus = "failed"
            $overallStatus = "unhealthy"
            $incidentPaths.Add((Write-Incident -SelfHealingDir $selfHealingDir -ProjectSlug $ProjectSlug -Category "memory-sync" -Title "Memory sync failed" -Details $_.Exception.Message))
        }
        finally {
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedUrl)) { Remove-Item Env:OLLAMA_EMBED_URL -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_URL = $prevOllamaEmbedUrl }
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedModel)) { Remove-Item Env:OLLAMA_EMBED_MODEL -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_MODEL = $prevOllamaEmbedModel }
            if ([string]::IsNullOrWhiteSpace($prevOllamaKeepAlive)) { Remove-Item Env:OLLAMA_KEEP_ALIVE -ErrorAction SilentlyContinue } else { $env:OLLAMA_KEEP_ALIVE = $prevOllamaKeepAlive }
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedBatchSize)) { Remove-Item Env:OLLAMA_EMBED_BATCH_SIZE -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_BATCH_SIZE = $prevOllamaEmbedBatchSize }
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedBatchSizeAuto)) { Remove-Item Env:OLLAMA_EMBED_BATCH_SIZE_AUTO -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_BATCH_SIZE_AUTO = $prevOllamaEmbedBatchSizeAuto }
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedConcurrency)) { Remove-Item Env:OLLAMA_EMBED_CONCURRENCY -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_CONCURRENCY = $prevOllamaEmbedConcurrency }
            if ([string]::IsNullOrWhiteSpace($prevOllamaEmbedWarmupInputs)) { Remove-Item Env:OLLAMA_EMBED_WARMUP_INPUTS -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_WARMUP_INPUTS = $prevOllamaEmbedWarmupInputs }
            if ([string]::IsNullOrWhiteSpace($prevOllamaNumGpu)) { Remove-Item Env:OLLAMA_NUM_GPU -ErrorAction SilentlyContinue } else { $env:OLLAMA_NUM_GPU = $prevOllamaNumGpu }
            if ([string]::IsNullOrWhiteSpace($prevOllamaNumThread)) { Remove-Item Env:OLLAMA_NUM_THREAD -ErrorAction SilentlyContinue } else { $env:OLLAMA_NUM_THREAD = $prevOllamaNumThread }
            if ([string]::IsNullOrWhiteSpace($prevCudaVisibleDevices)) { Remove-Item Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue } else { $env:CUDA_VISIBLE_DEVICES = $prevCudaVisibleDevices }
            if ([string]::IsNullOrWhiteSpace($prevNvidiaVisibleDevices)) { Remove-Item Env:NVIDIA_VISIBLE_DEVICES -ErrorAction SilentlyContinue } else { $env:NVIDIA_VISIBLE_DEVICES = $prevNvidiaVisibleDevices }
        }
    }

    if (-not $SkipHealthChecks -and $intake) {
        $plannedChecks = @(Get-HealthCheckPlan -Intake $intake -AllowInferred:$AllowInferredCommands)
        foreach ($plannedCheck in $plannedChecks) {
            Write-Host "Running $($plannedCheck.Name) check: $($plannedCheck.Command)" -ForegroundColor DarkGray
            $result = Invoke-ObservedCommand -CommandText $plannedCheck.Command -WorkingDirectory $resolvedProjectPath -TimeoutSeconds $CommandTimeoutSeconds
            $status = if ($result.TimedOut) {
                "timeout"
            }
            elseif ($result.ExitCode -eq 0) {
                "passed"
            }
            else {
                "failed"
            }

            $checks.Add([PSCustomObject]@{
                Name       = $plannedCheck.Name
                Command    = $result.Command
                Confidence = $plannedCheck.Confidence
                Status     = $status
                ExitCode   = $result.ExitCode
            })

            if ($status -ne "passed") {
                $overallStatus = "unhealthy"
                $incidentPaths.Add((Write-Incident -SelfHealingDir $selfHealingDir -ProjectSlug $ProjectSlug -Category $plannedCheck.Name -Title "$($plannedCheck.Name) check failed" -Details "Observer command returned status '$status'." -CommandText $result.Command -CommandOutput $result.Output))
            }
        }
    }

    $checksArray = @($checks.ToArray())
    $incidentArray = @($incidentPaths.ToArray())
    $summary = New-Object PSObject -Property ([ordered]@{
        ProjectSlug       = $ProjectSlug
        GeneratedAt       = $scanTimestamp
        Fingerprint       = $fingerprint
        OverallStatus     = $overallStatus
        OpenQuestionCount = $openQuestionCount
        WorldModelStatus  = $worldModelStatus
        MemorySyncStatus  = $memorySyncStatus
        Checks            = $checksArray
        IncidentPaths     = $incidentArray
    })

    Save-JsonContent -Path $healthReportJsonPath -Value $summary
    [System.IO.File]::WriteAllText($healthReportMdPath, (New-HealthMarkdown -Summary $summary))
    Save-JsonContent -Path $observerStateFile -Value ([PSCustomObject]@{
        LastRunAt      = $scanTimestamp
        LastFingerprint = $fingerprint
    })

    $projectStateMap = [ordered]@{}
    if ($projectState) {
        foreach ($property in $projectState.PSObject.Properties) {
            $projectStateMap[$property.Name] = $property.Value
        }
    }
    $projectStateMap["ObserverStatus"] = $overallStatus
    $projectStateMap["ObserverLastRunAt"] = $scanTimestamp
    $projectStateMap["HealthReportPath"] = $healthReportMdPath
    $projectStateMap["HealthCheckCount"] = $checksArray.Count
    $projectStateMap["OpenQuestionCount"] = $openQuestionCount
    $projectStateMap["WorldModelStatus"] = $worldModelStatus
    Save-JsonContent -Path $projectStateFile -Value (New-Object PSObject -Property $projectStateMap)

    Write-Host "Observer status: $overallStatus" -ForegroundColor Cyan
    if ($RunOnce) {
        break
    }
    Write-Host "Sleeping for $IntervalSeconds seconds..."
    Start-Sleep -Seconds $IntervalSeconds
}

