<#
.SYNOPSIS
    Role-gated PowerShell command execution tool for native LLM runtime.
.DESCRIPTION
    Executes a restricted set of read/test commands with timeout protection.
    When ORCHESTRATOR_SANDBOX_ENABLED=1 and Docker is running, commands execute
    inside an ephemeral container (guest), leaving the host filesystem untouched.
    Falls back to host execution transparently when Docker is unavailable.
    This script is invoked only via Invoke-AgentToolDispatcher.ps1 policy checks.
    In production mode (ORCHESTRATOR_ENV=prod|production), sandbox is enabled
    and required by default (fail-closed).
.PARAMETER ProjectPath
    Project root path.
.PARAMETER Command
    Command string to execute.
.PARAMETER TimeoutSeconds
    Max execution time before terminating the process.
.PARAMETER SandboxImage
    Docker image for sandbox execution. Default: mcr.microsoft.com/powershell:lts-alpine-3.17
.PARAMETER SandboxRequired
    Fail-closed mode: if enabled and Docker sandbox is unavailable, command execution is blocked.
.PARAMETER SandboxRootless
    Runs container as non-root user (default in production).
.EXAMPLE
    # Enable sandbox mode via environment:
    $env:ORCHESTRATOR_SANDBOX_ENABLED = "1"
    .\Invoke-RunPowerShellCommandTool.ps1 -ProjectPath . -Command "pytest"
#>
param(
    [string]$ProjectPath = ".",
    [string]$Command = "",
    [int]$TimeoutSeconds = 120,
    [string]$SandboxImage = "",
    [switch]$SandboxRequired,
    [switch]$SandboxRootless
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

if ([string]::IsNullOrWhiteSpace($Command)) {
    throw "Command is required."
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$cmd = $Command.Trim()
$lower = $cmd.ToLowerInvariant()

# Block shell chaining/redirection and multi-line payloads.
$forbiddenTokens = @(";", "&&", "||", "|", ">", "<", "`r", "`n")
foreach ($token in $forbiddenTokens) {
    if ($lower.Contains($token)) {
        throw "command-not-allowed:contains-forbidden-token:$token"
    }
}

# Restrict to read + verification commands only.
$allowPrefixes = @(
    "get-content ",
    "type ",
    "cat ",
    "select-string ",
    "get-childitem",
    "ls ",
    "dir ",
    "rg ",
    "pytest",
    "python -m pytest",
    "php artisan test",
    "npm test",
    "npm run test",
    "pnpm test",
    "yarn test",
    "go test",
    "dotnet test",
    "cargo test"
)

$allowed = $false
foreach ($prefix in $allowPrefixes) {
    if ($lower.StartsWith($prefix)) {
        $allowed = $true
        break
    }
}
if (-not $allowed) {
    throw "command-not-allowed:prefix"
}

# ── Sandbox routing ─────────────────────────────────────────────────────────
$envMode = [string]$env:ORCHESTRATOR_ENV
$isProductionMode = $envMode -and ($envMode.Trim().ToLowerInvariant() -in @("prod", "production"))

$sandboxEnabled = if ($null -ne $env:ORCHESTRATOR_SANDBOX_ENABLED -and $env:ORCHESTRATOR_SANDBOX_ENABLED -ne "") {
    ($env:ORCHESTRATOR_SANDBOX_ENABLED -in @("1","true","yes","on"))
} else {
    $isProductionMode
}

$sandboxRequiredEffective = if ($SandboxRequired -or ($env:ORCHESTRATOR_SANDBOX_REQUIRED -in @("1","true","yes","on"))) {
    $true
} else {
    $isProductionMode
}

$sandboxRootlessEffective = if ($SandboxRootless -or ($env:ORCHESTRATOR_SANDBOX_ROOTLESS -in @("1","true","yes","on"))) {
    $true
} elseif ($env:ORCHESTRATOR_SANDBOX_ROOTLESS -in @("0","false","no","off")) {
    $false
} else {
    $isProductionMode
}
$useSandbox = $false

if ($sandboxEnabled) {
    # Check if Docker daemon is reachable (fast probe, no output)
    $null = & docker info 2>&1
    $useSandbox = ($LASTEXITCODE -eq 0)
}

if ($sandboxRequiredEffective -and -not $useSandbox) {
    throw "sandbox-required:docker-unavailable-or-disabled"
}

if ($useSandbox) {
    # ── GUEST execution: ephemeral Docker container ──────────────────────────
    $image = if ($SandboxImage) { $SandboxImage } `
             elseif ($env:ORCHESTRATOR_SANDBOX_IMAGE) { $env:ORCHESTRATOR_SANDBOX_IMAGE } `
             else { "mcr.microsoft.com/powershell:lts-alpine-3.17" }

    # Mount policy:
    # - /project: repository mounted read-only
    # - /project/workspace: mounted read-write for controlled write scope
    # - container rootfs read-only + tmpfs scratch dirs
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../../")).Path.TrimEnd('\','/')
    $workspaceHostPath = Join-Path $projectRoot "workspace"
    Initialize-V2Directory -Path $workspaceHostPath
    $sandboxMemory = if ($env:ORCHESTRATOR_SANDBOX_MEMORY_MB) { $env:ORCHESTRATOR_SANDBOX_MEMORY_MB + "m" } else { "512m" }
    $sandboxCpus   = if ($env:ORCHESTRATOR_SANDBOX_CPUS)      { $env:ORCHESTRATOR_SANDBOX_CPUS }           else { "1" }
    $sandboxPids   = if ($env:ORCHESTRATOR_SANDBOX_PIDS_LIMIT) { [string]$env:ORCHESTRATOR_SANDBOX_PIDS_LIMIT } else { "256" }
    $sandboxUidGid = if ($env:ORCHESTRATOR_SANDBOX_UID_GID) { [string]$env:ORCHESTRATOR_SANDBOX_UID_GID } else { "1000:1000" }
    $dockerArgs = @(
        "run", "--rm",
        "--name", ("orch-sandbox-" + [System.Guid]::NewGuid().ToString("N").Substring(0,8)),
        "--memory", $sandboxMemory,
        "--cpus",  $sandboxCpus,
        "--pids-limit", $sandboxPids,
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m",
        "--tmpfs", "/var/tmp:rw,nosuid,nodev,noexec,size=64m",
        "-v", "${projectRoot}:/project:ro",
        "-v", "${workspaceHostPath}:/project/workspace:rw",
        "-v", "${repoRoot}:/repo:ro",
        "-w", "/project",
        $image,
        "pwsh", "-NoProfile", "-NonInteractive", "-Command", $cmd
    )
    if ($sandboxRootlessEffective) {
        $dockerArgs = @($dockerArgs[0..1] + @("--user", $sandboxUidGid) + $dockerArgs[2..($dockerArgs.Count - 1)])
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "docker"
    $startInfo.Arguments = $dockerArgs -join " "
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true

    Write-Output ("[sandbox:guest] docker:{0} rootless={1} project=ro workspace=rw" -f $image, $sandboxRootlessEffective)
} else {
    # ── HOST execution: direct PowerShell (existing behavior) ────────────────
    if ($sandboxEnabled) {
        Write-Output "[sandbox:fallback-host] docker-unavailable"
    }

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "powershell.exe"
    $startInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $cmd"
    $startInfo.WorkingDirectory = $projectRoot
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
}

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
$null = $process.Start()

if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    try { $process.Kill() } catch {}
    throw "command-timeout:$TimeoutSeconds"
}

$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$exitCode = $process.ExitCode

if (-not [string]::IsNullOrWhiteSpace($stdout)) {
    Write-Output $stdout.TrimEnd()
}
if (-not [string]::IsNullOrWhiteSpace($stderr)) {
    Write-Output ("STDERR: " + $stderr.TrimEnd())
}

if ($exitCode -ne 0) {
    throw ("command-failed:exit-code:{0}" -f $exitCode)
}
