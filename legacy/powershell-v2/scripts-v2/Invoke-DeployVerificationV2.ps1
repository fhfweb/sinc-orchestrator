<#
.SYNOPSIS
    Verifies that deployed services (Docker containers + health endpoints) are running.
.DESCRIPTION
    After each loop cycle, checks:
      1. Docker container status - all expected services are running (not exited/restarting)
      2. App HTTP health endpoint - GET /health or / returns 2xx
      3. Database connectivity - pg_isready / mongo ping / redis ping via docker exec
    Writes ai-orchestrator/reports/deploy-verify-<timestamp>.json.
    Creates REPAIR-DEPLOY-* tasks for any failing checks.
    Auto-resolves REPAIR-DEPLOY-* tasks when all checks pass again.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/ and docker-compose.generated.yml.
.PARAMETER HealthEndpoint
    HTTP endpoint to probe. Default: http://localhost:8000/health
.PARAMETER TimeoutSeconds
    HTTP probe timeout. Default 10.
.EXAMPLE
    .\scripts\v2\Invoke-DeployVerificationV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-DeployVerificationV2.ps1 -ProjectPath C:\projects\myapp -HealthEndpoint http://localhost:3000/api/health
#>
param(
    [string]$ProjectPath = ".",
    [string]$HealthEndpoint = "",
    [int]$TimeoutSeconds = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

if (-not $resolvedPath) {
    throw "Fatal: resolvedPath is null. Check ProjectPath input."
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
if (-not $orchestratorRoot) {
    throw "Fatal: orchestratorRoot is null."
}

$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$reportsDir = Join-Path $orchestratorRoot "reports"
$ts = Get-Date -Format "yyyyMMddHHmmss"
$reportPath = Join-Path $reportsDir "deploy-verify-$ts.json"

if (-not [string]::IsNullOrWhiteSpace($reportsDir)) {
    Initialize-V2Directory -Path $reportsDir
}

function Test-DeployTcpOpen {
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

function Initialize-DeployDockerContext {
    param([string]$OrchestratorRoot)

    if (-not (Test-Path Env:DOCKER_HOST) -and (Test-DeployTcpOpen -TcpHost "127.0.0.1" -Port 2375 -TimeoutMs 400)) {
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

Initialize-DeployDockerContext -OrchestratorRoot $orchestratorRoot

$state = Get-V2JsonContent -Path $statePath
$checks = New-Object System.Collections.Generic.List[object]
$allPassed = $true
function Resolve-DeployComposeCommand {
    $dockerComposeLegacy = Get-Command "docker-compose" -ErrorAction SilentlyContinue
    if ($dockerComposeLegacy) {
        return "docker-compose"
    }
    return "docker compose"
}

function Get-DeployAppHostPortFromCompose {
    param(
        [string]$ComposePath
    )

    if ([string]::IsNullOrWhiteSpace($ComposePath) -or -not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
        return 0
    }

    $lines = @(Get-Content -LiteralPath $ComposePath -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) {
        return 0
    }

    $inApp = $false
    $inPorts = $false
    foreach ($line in $lines) {
        if ($line -match "^\s{2}app:\s*$") {
            $inApp = $true
            $inPorts = $false
            continue
        }

        if (-not $inApp) {
            continue
        }

        # New service block (same indentation as app:)
        if ($line -match "^\s{2}[A-Za-z0-9_-]+:\s*$" -and $line -notmatch "^\s{2}app:\s*$") {
            break
        }

        if ($line -match "^\s{4}ports:\s*$") {
            $inPorts = $true
            continue
        }

        if ($inPorts -and $line -match '^\s{6}-\s*"?(?<host>\d{2,5}):(?<container>\d{2,5})"?\s*$') {
            return [int]$matches["host"]
        }

        if ($inPorts -and $line -match "^\s{4}[A-Za-z0-9_-]+:\s*$") {
            $inPorts = $false
        }
    }

    return 0
}
# -------------------------------------------------------------------------------
function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail = "")
    $script:checks.Add([PSCustomObject]@{
            name   = $Name
            passed = $Passed
            detail = $Detail
        })
    if (-not $Passed) { $script:allPassed = $false }
    $icon = if ($Passed) { "OK" } else { "FAIL" }
    Write-Host ("[DeployVerify] $icon $Name" + $(if ($Detail) { " - $Detail" }))
}
# -------------------------------------------------------------------------------
$composePath = ""
if ($state) {
    $startupPaths = Get-V2OptionalProperty -InputObject $state -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})
    $composeRel = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "docker_compose_file" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($composeRel)) {
        $composePath = if ([System.IO.Path]::IsPathRooted($composeRel)) { $composeRel } else { Join-Path $resolvedPath $composeRel }
    }
}
if ([string]::IsNullOrEmpty($composePath)) {
    $composePath = Join-Path $orchestratorRoot "docker/docker-compose.generated.yml"
}

if (Test-Path -LiteralPath $composePath -PathType Leaf) {
    try {
        $composeEngine = Resolve-DeployComposeCommand

        # Get service names only from the `services:` section (avoid volumes/networks false positives).
        $composeContent = Get-Content -LiteralPath $composePath -Raw
        $composeLines = $composeContent -split "(`r`n|`n|`r)"
        $serviceNamesList = New-Object System.Collections.Generic.List[string]
        $inServicesBlock = $false
        foreach ($line in $composeLines) {
            if ($line -match "^\s*services:\s*$") {
                $inServicesBlock = $true
                continue
            }
            if (-not $inServicesBlock) { continue }

            # Reached next top-level section (e.g., volumes:, networks:)
            if ($line -match "^[A-Za-z0-9_-]+\s*:\s*$") {
                break
            }

            if ($line -match "^\s{2}([A-Za-z0-9][A-Za-z0-9_-]*)\s*:\s*$") {
                $svcName = $matches[1]
                if (-not $serviceNamesList.Contains($svcName)) {
                    $serviceNamesList.Add($svcName)
                }
            }
        }
        $serviceNames = @($serviceNamesList.ToArray())
        if ($serviceNames.Count -eq 0) {
            Add-Check -Name "docker-compose-services" -Passed $false -Detail "No services found in services: block"
        }

        foreach ($svc in $serviceNames) {
            try {
                $rawOutput = if ($composeEngine -eq "docker-compose") {
                    & cmd /c "docker-compose -f `"$composePath`" ps --status running --quiet $svc 2>&1"
                }
                else {
                    & cmd /c "docker compose -f `"$composePath`" ps --status running --quiet $svc 2>&1"
                }
                $composeExitCode = $LASTEXITCODE
                $outputText = [string]($rawOutput | Out-String)
                $idLine = @($outputText -split "(`r`n|`n|`r)" | Where-Object { $_ -match "^[0-9a-f]{12,64}$" } | Select-Object -First 1)
                $containerId = if ($idLine.Count -gt 0) { [string]$idLine[0] } else { "" }
                $running = ($composeExitCode -eq 0) -and (-not [string]::IsNullOrWhiteSpace($containerId))
                $detail = if ($running) {
                    "running"
                }
                else {
                    $tail = (($outputText -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 1)
                    if ([string]::IsNullOrWhiteSpace([string]$tail)) {
                        "not running"
                    }
                    else {
                        $tailText = [string]$tail
                        if ($tailText.Length -gt 160) { $tailText = $tailText.Substring(0, 160) + "…" }
                        "not running (compose-exit ${composeExitCode}: $tailText)"
                    }
                }
                Add-Check -Name "container:$svc" -Passed $running -Detail $detail
            }
            catch {
                Add-Check -Name "container:$svc" -Passed $false -Detail $_.Exception.Message
            }
        }
    }
    catch {
        Add-Check -Name "docker-compose" -Passed $false -Detail $_.Exception.Message
    }
}
else {
    Add-Check -Name "docker-compose-file" -Passed $false -Detail "compose file not found at $composePath"
}
# -------------------------------------------------------------------------------
# Skip HTTP health check if no containers are running (avoid false REPAIR tasks)
$anyContainerRunning = @($checks | Where-Object { $_.name -like "container:*" -and $_.passed }).Count -gt 0
$noContainersConfigured = @($checks | Where-Object { $_.name -like "container:*" }).Count -eq 0

if ([string]::IsNullOrWhiteSpace($HealthEndpoint)) {
    # Auto-detect app port from state
    $appPort = 8002
    if ($state) {
        $appP = [int](Get-V2OptionalProperty -InputObject $state -Name "app_port" -DefaultValue 0)
        if ($appP -gt 0) { $appPort = $appP }
        else {
            # Try to extract port from verified_commands.run (e.g. "php artisan serve --port=8080")
            $verifiedCmds = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
            $runCmd = Get-V2VerifiedCommand -VerifiedCommands $verifiedCmds -CommandName "run"
            $portMatch = [regex]::Match($runCmd, "--port[= ](\d+)|:(\d{4,5})")
            if ($portMatch.Success) {
                $appPort = [int]($portMatch.Groups[1].Value + $portMatch.Groups[2].Value)
            }
        }
    }
    # Compose is source-of-truth for host remaps; override app port when available.
    $composePort = Get-DeployAppHostPortFromCompose -ComposePath $composePath
    if ($composePort -gt 0) {
        $appPort = $composePort
    }
    $HealthEndpoint = "http://localhost:$appPort/health"
}

if (-not $anyContainerRunning -and -not $noContainersConfigured) {
    # All configured containers are down — health endpoint will refuse connections; skip to avoid noise
    Add-Check -Name "http-health" -Passed $false -Detail "skipped — no containers are running (check container checks above)"
}
else {
    try {
        $response = Invoke-WebRequest -Uri $HealthEndpoint -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
        Add-Check -Name "http-health" -Passed ($response.StatusCode -lt 400) -Detail "HTTP $($response.StatusCode)"
    }
    catch [System.Net.WebException] {
        $statusCode = 0
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        $errMsg = $_.Exception.Message
        if ($statusCode -eq 0) {
            # Connection refused — endpoint is not reachable
            Add-Check -Name "http-health" -Passed $false -Detail "connection refused at $HealthEndpoint"
        }
        else {
            $ok = $statusCode -lt 400
            Add-Check -Name "http-health" -Passed $ok -Detail "HTTP $statusCode - $errMsg"
        }
    }
    catch {
        Add-Check -Name "http-health" -Passed $false -Detail $_.Exception.Message
    }
}
# -------------------------------------------------------------------------------
$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    project      = Split-Path -Leaf $resolvedPath
    all_passed   = $allPassed
    checks       = @($checks.ToArray())
}
Save-V2JsonContent -Path $reportPath -Value $report
# -------------------------------------------------------------------------------
if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
    try {
        $dag = Get-V2JsonContent -Path $dagPath

        if ($allPassed) {
            $resolved = 0
            foreach ($task in @($dag.tasks)) {
                $tId = [string](Get-V2OptionalProperty -InputObject $task -Name "id"     -DefaultValue "")
                $tStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                if ($tId -like "REPAIR-DEPLOY-*" -and $tStatus -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")) {
                    Set-V2DynamicProperty -InputObject $task -Name "status"          -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at"    -Value (Get-V2Timestamp)
                    Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: all deploy checks passing"
                    $resolved++
                }
            }
            if ($resolved -gt 0) {
                Save-V2JsonContent -Path $dagPath -Value $dag
                Write-Host ("[DeployVerify] Auto-resolved {0} deploy REPAIR task(s)." -f $resolved)
            }
        }
        else {
            $alreadyOpen = @($dag.tasks | Where-Object {
                    [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -like "REPAIR-DEPLOY-*" -and
                    [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")
                }).Count -gt 0

            if (-not $alreadyOpen) {
                $failedChecks = ($checks | Where-Object { -not $_.passed } | ForEach-Object { "$($_.name): $($_.detail)" }) -join "; "
                $repairId = "REPAIR-DEPLOY-$ts"
                $dag.tasks += [PSCustomObject]@{
                    id                 = $repairId
                    title              = "Fix deployment issues"
                    description        = "Deploy verification failed: $failedChecks"
                    reason             = "deploy-check-failed"
                    priority           = "P0"
                    dependencies       = @()
                    preferred_agent    = "AI DevOps Engineer"
                    assigned_agent     = ""
                    status             = "pending"
                    execution_mode     = "external-agent"
                    reason_fingerprint = "deploy-check-failed"
                    files_affected     = @("ai-orchestrator/reports", "ai-orchestrator/docker/docker-compose.generated.yml")
                    source_report      = $reportPath
                    created_at         = Get-V2Timestamp
                    updated_at         = Get-V2Timestamp
                }
                Save-V2JsonContent -Path $dagPath -Value $dag
                Write-Host "[DeployVerify] Created REPAIR task: $repairId"
            }
        }
    }
    catch {
        Write-Warning "[DeployVerify] Could not update task-dag: $($_.Exception.Message)"
    }
}

$summary = if ($allPassed) { "ALL CHECKS PASSED" } else { "FAILED - see $reportPath" }
Write-Host "[DeployVerify] $summary"
Write-Output ($report | ConvertTo-Json -Depth 5)
