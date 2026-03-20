<#
.SYNOPSIS
    Configures host-aware WSL2 RAM policy for orchestrator graph workloads.
.DESCRIPTION
    Generates ai-orchestrator/config/host-resource-policy.json and
    ai-orchestrator/config/.wslconfig.generated tuned for Neo4j + Qdrant.
    Optionally applies generated .wslconfig to the current Windows user profile.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER ProfileName
    Host profile label stored in policy metadata.
.PARAMETER WslMemoryGb
    WSL2 memory limit (GB). Default: 16.
.PARAMETER WslProcessors
    WSL2 vCPU allocation. Default: 8.
.PARAMETER WslSwapGb
    WSL2 swap size (GB). Default: 8.
.PARAMETER ApplyToUserProfile
    If set, copies generated .wslconfig into %USERPROFILE%\.wslconfig.
.PARAMETER EmitJson
    If set, emits machine-readable JSON result.
#>
param(
    [string]$ProjectPath = ".",
    [string]$ProfileName = "rtx5070-neo4j-qdrant",
    [int]$WslMemoryGb = 16,
    [int]$WslProcessors = 8,
    [int]$WslSwapGb = 8,
    [switch]$ApplyToUserProfile,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

if ($WslMemoryGb -lt 4) { throw "WslMemoryGb must be >= 4." }
if ($WslProcessors -lt 1) { throw "WslProcessors must be >= 1." }
if ($WslSwapGb -lt 0) { throw "WslSwapGb must be >= 0." }

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$configDir = Join-Path $orchestratorRoot "config"
$stateDir = Join-Path $orchestratorRoot "state"
Initialize-V2Directory -Path $configDir
Initialize-V2Directory -Path $stateDir

$policyPath = Join-Path $configDir "host-resource-policy.json"
$generatedWslConfigPath = Join-Path $configDir ".wslconfig.generated"
$projectStatePath = Join-Path $stateDir "project-state.json"

$totalMb = $WslMemoryGb * 1024
$neo4jHeapMb = [int][Math]::Floor($totalMb * 0.40)
$neo4jPageCacheMb = [int][Math]::Floor($totalMb * 0.25)
$qdrantMemMb = [int][Math]::Floor($totalMb * 0.25)
$safetyReserveMb = [Math]::Max(1024, $totalMb - ($neo4jHeapMb + $neo4jPageCacheMb + $qdrantMemMb))

$timestamp = Get-V2Timestamp

$policy = [PSCustomObject]@{
    generated_at = $timestamp
    profile = [PSCustomObject]@{
        name = $ProfileName
        hardware = "RTX 5070 / 48GB RAM / Ryzen 7 class"
        objective = "Dedicate 16GB WSL2 envelope for Neo4j + Qdrant graph/vector workloads."
    }
    wsl2 = [PSCustomObject]@{
        memory_gb = $WslMemoryGb
        processors = $WslProcessors
        swap_gb = $WslSwapGb
        localhost_forwarding = $true
        generated_file = $generatedWslConfigPath
    }
    workload = [PSCustomObject]@{
        neo4j = [PSCustomObject]@{
            heap_mb = $neo4jHeapMb
            pagecache_mb = $neo4jPageCacheMb
            notes = "Use as upper bound for container env (NEO4J_server_memory_*)."
        }
        qdrant = [PSCustomObject]@{
            memory_budget_mb = $qdrantMemMb
            notes = "Cap Qdrant service memory close to this value when running with other services."
        }
        safety_reserve_mb = $safetyReserveMb
    }
    apply = [PSCustomObject]@{
        manual_steps = @(
            "Copy ai-orchestrator/config/.wslconfig.generated to %USERPROFILE%\\.wslconfig",
            "Run: wsl --shutdown",
            "Restart Docker Desktop / WSL distro"
        )
    }
}

Save-V2JsonContent -Path $policyPath -Value $policy

$wslConfigLines = @(
    "[wsl2]",
    ("memory={0}GB" -f $WslMemoryGb),
    ("processors={0}" -f $WslProcessors),
    ("swap={0}GB" -f $WslSwapGb),
    "localhostForwarding=true"
)
Write-V2File -Path $generatedWslConfigPath -Content ($wslConfigLines -join [Environment]::NewLine) -Force

$projectState = Get-V2JsonContent -Path $projectStatePath
if (-not $projectState) {
    $projectState = [PSCustomObject]@{}
}
Set-V2DynamicProperty -InputObject $projectState -Name "host_profile" -Value ([PSCustomObject]@{
    name = $ProfileName
    category = "gpu-workstation"
    updated_at = $timestamp
})
Set-V2DynamicProperty -InputObject $projectState -Name "wsl2_resource_policy" -Value ([PSCustomObject]@{
    memory_gb = $WslMemoryGb
    processors = $WslProcessors
    swap_gb = $WslSwapGb
    generated_path = $generatedWslConfigPath
    policy_path = $policyPath
    updated_at = $timestamp
})
Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value $timestamp
Save-V2JsonContent -Path $projectStatePath -Value $projectState

$appliedPath = ""
if ($ApplyToUserProfile) {
    if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        throw "USERPROFILE is not available; cannot apply .wslconfig."
    }
    $targetWslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
    Copy-Item -LiteralPath $generatedWslConfigPath -Destination $targetWslConfigPath -Force
    $appliedPath = $targetWslConfigPath
}

$result = [PSCustomObject]@{
    ok = $true
    generated_at = $timestamp
    project_path = $resolvedProjectPath
    profile_name = $ProfileName
    policy_path = $policyPath
    generated_wslconfig_path = $generatedWslConfigPath
    applied_wslconfig_path = $appliedPath
    memory_gb = $WslMemoryGb
    processors = $WslProcessors
    swap_gb = $WslSwapGb
    neo4j_heap_mb = $neo4jHeapMb
    neo4j_pagecache_mb = $neo4jPageCacheMb
    qdrant_memory_budget_mb = $qdrantMemMb
    safety_reserve_mb = $safetyReserveMb
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 10
}
else {
    Write-Host ("WSL2 policy generated: {0}" -f $policyPath)
    Write-Host ("Generated .wslconfig: {0}" -f $generatedWslConfigPath)
    if (-not [string]::IsNullOrWhiteSpace($appliedPath)) {
        Write-Host ("Applied to: {0}" -f $appliedPath)
    }
    Write-Host "Next step: run 'wsl --shutdown' to apply new limits."
}
