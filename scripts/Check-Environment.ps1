<#
.SYNOPSIS
    Validates local environment prerequisites for the orchestrator.
.DESCRIPTION
    Checks required/runtime dependencies:
      - PowerShell version
      - Python availability and optional package availability
      - Docker CLI and daemon
      - Optional local services (Ollama, Qdrant, Neo4j)
      - Optional project layer presence when ProjectPath is provided
.PARAMETER ProjectPath
    Optional project path for project-layer checks.
.PARAMETER Fix
    Attempts to install missing Python packages (requests, qdrant-client, neo4j).
.EXAMPLE
    .\scripts\Check-Environment.ps1
.EXAMPLE
    .\scripts\Check-Environment.ps1 -ProjectPath C:\repo -Fix
#>
param(
    [string]$ProjectPath = "",
    [switch]$Fix
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$pass = 0
$warn = 0
$fail = 0

function Write-Check {
    param(
        [string]$Label,
        [ValidateSet("PASS", "WARN", "FAIL")]
        [string]$Status,
        [string]$Detail = ""
    )

    $icon = switch ($Status) {
        "PASS" { "[OK]" }
        "WARN" { "[WARN]" }
        "FAIL" { "[FAIL]" }
    }

    $message = "  $icon $Label"
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        $message += " - $Detail"
    }
    Write-Host $message
}

function Find-PythonCommand {
    foreach ($candidate in @("python", "python3", "py")) {
        try {
            $output = & $candidate --version 2>&1 | Out-String
            if ($LASTEXITCODE -eq 0 -and $output -match "Python\s+(\d+)\.(\d+)") {
                return [PSCustomObject]@{
                    command = $candidate
                    major   = [int]$matches[1]
                    minor   = [int]$matches[2]
                    text    = $output.Trim()
                }
            }
        }
        catch {
        }
    }
    return $null
}

function Test-PythonImport {
    param(
        [string]$PythonCommand,
        [string]$ModuleName
    )

    try {
        & $PythonCommand -c "import $ModuleName" 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Install-PythonModule {
    param(
        [string]$PythonCommand,
        [string]$ModuleName
    )

    try {
        & $PythonCommand -m pip install $ModuleName --quiet 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Test-HttpEndpoint {
    param(
        [string]$Url,
        [int]$TimeoutSeconds = 3
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSeconds -UseBasicParsing -ErrorAction Stop
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    }
    catch {
        return $false
    }
}

Write-Host ""
Write-Host "========================================================"
Write-Host "  Environment Check - AI Project Orchestrator"
Write-Host "========================================================"
Write-Host ""

Write-Host "[ PowerShell ]"
$psVersion = $PSVersionTable.PSVersion
if ($psVersion.Major -ge 5) {
    Write-Check "PowerShell $($psVersion.ToString())" "PASS" "5.1+ required"
    $pass++
}
else {
    Write-Check "PowerShell $($psVersion.ToString())" "FAIL" "Requires 5.1+"
    $fail++
}

$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -in @("Unrestricted", "RemoteSigned", "Bypass")) {
    Write-Check "Execution policy: $policy" "PASS"
    $pass++
}
else {
    Write-Check "Execution policy: $policy" "WARN" "Use: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
    $warn++
}

Write-Host ""
Write-Host "[ Python ]"
$python = Find-PythonCommand
if ($null -eq $python) {
    Write-Check "Python" "WARN" "Not found. Needed for memory_sync and advanced analysis."
    $warn++
}
else {
    if ($python.major -gt 3 -or ($python.major -eq 3 -and $python.minor -ge 10)) {
        Write-Check $python.text "PASS" "3.10+ preferred"
        $pass++
    }
    elseif ($python.major -eq 3 -and $python.minor -ge 7) {
        Write-Check $python.text "WARN" "3.10+ recommended"
        $warn++
    }
    else {
        Write-Check $python.text "FAIL" "Python 3.7+ required"
        $fail++
    }

    $requiredModules = @("requests", "pytest")
    $optionalModules = @("qdrant_client", "neo4j")

    foreach ($module in $requiredModules) {
        $installed = Test-PythonImport -PythonCommand $python.command -ModuleName $module
        if (-not $installed -and $Fix) {
            $installed = Install-PythonModule -PythonCommand $python.command -ModuleName $module
        }
        if ($installed) {
            Write-Check "python module: $module" "PASS" "required"
            $pass++
        }
        else {
            Write-Check "python module: $module" "FAIL" "Install with: $($python.command) -m pip install $module"
            $fail++
        }
    }

    foreach ($module in $optionalModules) {
        $installed = Test-PythonImport -PythonCommand $python.command -ModuleName $module
        if (-not $installed -and $Fix) {
            $installName = if ($module -eq "qdrant_client") { "qdrant-client" } else { $module }
            $installed = Install-PythonModule -PythonCommand $python.command -ModuleName $installName
        }
        if ($installed) {
            Write-Check "python module: $module" "PASS" "optional"
            $pass++
        }
        else {
            Write-Check "python module: $module" "WARN" "Optional for external memory backends"
            $warn++
        }
    }
}

Write-Host ""
Write-Host "[ Docker ]"
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Check "Docker CLI" "WARN" "Docker Desktop not installed"
    $warn++
}
else {
    Write-Check "Docker CLI" "PASS" "available"
    $pass++

    try {
        & docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Check "Docker daemon" "PASS" "running"
            $pass++
        }
        else {
            # Self-healing check for TCP port 2375
            $tcpClient = New-Object System.Net.Sockets.TcpClient
            $portOpen = $false
            try {
                $asyncResult = $tcpClient.BeginConnect("127.0.0.1", 2375, $null, $null)
                if ($asyncResult.AsyncWaitHandle.WaitOne(500)) {
                    $tcpClient.EndConnect($asyncResult)
                    $portOpen = $true
                }
            }
            catch {} finally { $tcpClient.Close() }

            if ($portOpen) {
                $env:DOCKER_HOST = "tcp://localhost:2375"
                & docker info 2>&1 | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Write-Check "Docker daemon" "PASS" "running (via tcp://localhost:2375)"
                    $pass++
                }
                else {
                    Write-Check "Docker daemon" "WARN" "port 2375 open but connection failed"
                    $warn++
                }
            }
            else {
                Write-Check "Docker daemon" "WARN" "not running"
                $warn++
            }
        }
    }
    catch {
        Write-Check "Docker daemon" "WARN" "not reachable"
        $warn++
    }
}

Write-Host ""
Write-Host "[ Optional Backends ]"
if (Test-HttpEndpoint -Url "http://localhost:11434") {
    Write-Check "Ollama :11434" "PASS" "available"
    $pass++
}
else {
    Write-Check "Ollama :11434" "WARN" "not reachable"
    $warn++
}

if (Test-HttpEndpoint -Url "http://localhost:6333/healthz") {
    Write-Check "Qdrant :6333" "PASS" "available"
    $pass++
}
else {
    Write-Check "Qdrant :6333" "WARN" "not reachable"
    $warn++
}

if (Test-HttpEndpoint -Url "http://localhost:7474") {
    Write-Check "Neo4j :7474" "PASS" "available"
    $pass++
}
else {
    Write-Check "Neo4j :7474" "WARN" "not reachable"
    $warn++
}

if (-not [string]::IsNullOrWhiteSpace($ProjectPath)) {
    Write-Host ""
    Write-Host "[ Project ]"

    $resolvedProjectPath = if (Test-Path -LiteralPath $ProjectPath -PathType Container) {
        (Resolve-Path -LiteralPath $ProjectPath).Path
    }
    else {
        $ProjectPath
    }

    $layerPath = Join-Path $resolvedProjectPath "ai-orchestrator"
    if (-not (Test-Path -LiteralPath $layerPath -PathType Container)) {
        $layerPath = Join-Path $resolvedProjectPath ".ai-orchestrator"
    }

    if (Test-Path -LiteralPath $layerPath -PathType Container) {
        Write-Check "orchestrator layer" "PASS" $layerPath
        $pass++

        $statePath = Join-Path $layerPath "state/project-state.json"
        if (Test-Path -LiteralPath $statePath -PathType Leaf) {
            Write-Check "project-state.json" "PASS" "present"
            $pass++
        }
        else {
            Write-Check "project-state.json" "WARN" "missing"
            $warn++
        }

        $dagPath = Join-Path $layerPath "tasks/task-dag.json"
        if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
            Write-Check "task-dag.json" "PASS" "present"
            $pass++
        }
        else {
            Write-Check "task-dag.json" "WARN" "missing"
            $warn++
        }
    }
    else {
        Write-Check "orchestrator layer" "WARN" "not found. Run v2-submit first."
        $warn++
    }
}

Write-Host ""
Write-Host "========================================================"
Write-Host ("  Results: {0} passed | {1} warnings | {2} failed" -f $pass, $warn, $fail)
Write-Host "========================================================"
Write-Host ""

if ($fail -gt 0) {
    exit 1
}

exit 0
