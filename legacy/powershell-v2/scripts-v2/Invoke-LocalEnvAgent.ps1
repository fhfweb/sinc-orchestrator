<#
.SYNOPSIS
    Captures local environment DNA for the orchestrator runtime.
.DESCRIPTION
    Writes a system snapshot to ai-orchestrator/state/system-dna.json including:
      - hardware hints (cpu/ram/gpu)
      - docker daemon reachability
      - local tool versions (node/python/powershell)
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER IntervalSeconds
    Polling interval when running continuously.
.PARAMETER RunOnce
    Runs one cycle and exits.
.PARAMETER EmitJson
    Emits the generated object to stdout as JSON.
#>
param(
    [string]$ProjectPath = ".",
    [int]$IntervalSeconds = 600,
    [switch]$RunOnce,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2SafeCommandOutput {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    $cmd = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $cmd) {
        return "not-found"
    }

    try {
        $output = & $Command @Arguments 2>&1 | Out-String
        $text = [string]$output
        if ([string]::IsNullOrWhiteSpace($text)) {
            return "ok"
        }
        return $text.Trim()
    }
    catch {
        return "failed"
    }
}

function Get-V2DockerDaemonStatus {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        return [PSCustomObject]@{
            available = $false
            reason = "docker-cli-unavailable"
            detail = "Install Docker Desktop."
        }
    }

    try {
        $probe = & docker info --format "{{.ServerVersion}}" 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            return [PSCustomObject]@{
                available = $true
                reason = "ok"
                detail = ([string]$probe).Trim()
            }
        }
        return [PSCustomObject]@{
            available = $false
            reason = "daemon-unreachable"
            detail = ([string]$probe).Trim()
        }
    }
    catch {
        return [PSCustomObject]@{
            available = $false
            reason = "probe-failed"
            detail = $_.Exception.Message
        }
    }
}

function Get-V2SystemHardwareDNA {
    $osText = ""
    if ($PSVersionTable.PSObject.Properties.Name -contains "OS") {
        $osText = [string]$PSVersionTable.OS
    }
    if ([string]::IsNullOrWhiteSpace($osText)) {
        $osText = [System.Environment]::OSVersion.VersionString
    }

    $dna = [ordered]@{
        cpu = "unknown"
        ram_gb = 0
        gpu = "unknown"
        os = $osText
        timestamp = Get-V2Timestamp
    }

    $platform = [System.Environment]::OSVersion.Platform
    $isWindowsHost = ($platform -eq [System.PlatformID]::Win32NT)
    if (-not $isWindowsHost) {
        return [PSCustomObject]$dna
    }

    try {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        if ($cpu -and -not [string]::IsNullOrWhiteSpace([string]$cpu.Name)) {
            $dna.cpu = [string]$cpu.Name
        }
    }
    catch {
    }

    try {
        $os = Get-CimInstance Win32_OperatingSystem
        if ($os) {
            $ramGb = [Math]::Round(([double]$os.TotalVisibleMemorySize / 1MB), 0)
            $dna.ram_gb = [int]$ramGb
        }
    }
    catch {
    }

    try {
        $gpuList = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)
        if ($gpuList.Count -gt 0) {
            $dna.gpu = ($gpuList -join "; ")
        }
    }
    catch {
    }

    return [PSCustomObject]$dna
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-local-env-agent"

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$envDnaPath = Join-Path $orchestratorRoot "state/system-dna.json"

while ($true) {
    $hardware = Get-V2SystemHardwareDNA
    $dockerStatus = Get-V2DockerDaemonStatus

    $snapshot = [PSCustomObject]@{
        generated_at = Get-V2Timestamp
        project_path = $resolvedProjectPath
        hardware = $hardware
        docker = $dockerStatus
        software = [PSCustomObject]@{
            node = Get-V2SafeCommandOutput -Command "node" -Arguments @("-v")
            python = Get-V2SafeCommandOutput -Command "python" -Arguments @("--version")
            powershell = $PSVersionTable.PSVersion.ToString()
        }
    }

    Save-V2JsonContent -Path $envDnaPath -Value $snapshot
    Write-Output "Environment DNA updated: $envDnaPath"

    if ($EmitJson) {
        $snapshot | ConvertTo-Json -Depth 8
    }

    if ($RunOnce) {
        break
    }

    Start-Sleep -Seconds $IntervalSeconds
}
