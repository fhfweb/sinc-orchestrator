<#
.SYNOPSIS
    Executes role-scoped tools from registry with audit logging.
.DESCRIPTION
    Enforces role -> allowed_tools from docs/agents/agent-tools-registry.json,
    dispatches scripts, and writes usage events to ai-orchestrator/state/tool-usage-log.jsonl.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator.
.PARAMETER AgentName
    Agent/role requesting the tool.
.PARAMETER ToolName
    Registered tool id.
.PARAMETER ToolArgumentsJson
    Optional JSON object with tool args.
.PARAMETER EmitJson
    Emits machine-readable result.
#>
param(
    [string]$ProjectPath = ".",
    [string]$AgentName = "",
    [string]$ToolName = "",
    [string]$ToolArgumentsJson = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2RoleToolPolicy {
    param(
        [object]$Registry,
        [string]$RoleName
    )

    $roles = Get-V2OptionalProperty -InputObject $Registry -Name "roles" -DefaultValue ([PSCustomObject]@{})
    $defaultRole = [string](Get-V2OptionalProperty -InputObject $Registry -Name "default_role" -DefaultValue "default")
    $role = if ([string]::IsNullOrWhiteSpace($RoleName)) { $defaultRole } else { $RoleName.Trim().ToLowerInvariant() }
    $roleCfg = Get-V2OptionalProperty -InputObject $roles -Name $role -DefaultValue $null
    $defaultCfg = Get-V2OptionalProperty -InputObject $roles -Name $defaultRole -DefaultValue ([PSCustomObject]@{})
    if (-not $roleCfg) {
        $roleCfg = $defaultCfg
    }
    $extends = [string](Get-V2OptionalProperty -InputObject $roleCfg -Name "extends" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($extends)) {
        $parent = Get-V2OptionalProperty -InputObject $roles -Name $extends.ToLowerInvariant() -DefaultValue $null
        if ($parent) {
            $mergedAllowed = New-Object System.Collections.Generic.List[string]
            foreach ($tool in @(Get-V2OptionalProperty -InputObject $parent -Name "allowed_tools" -DefaultValue @())) {
                if (-not [string]::IsNullOrWhiteSpace([string]$tool)) { $mergedAllowed.Add(([string]$tool).Trim()) }
            }
            foreach ($tool in @(Get-V2OptionalProperty -InputObject $roleCfg -Name "allowed_tools" -DefaultValue @())) {
                $text = ([string]$tool).Trim()
                if (-not [string]::IsNullOrWhiteSpace($text) -and -not $mergedAllowed.Contains($text)) {
                    $mergedAllowed.Add($text)
                }
            }
            $roleCfg = [PSCustomObject]@{ allowed_tools = @($mergedAllowed.ToArray()) }
        }
    }
    return [PSCustomObject]@{
        role_name = $role
        allowed_tools = @((Get-V2OptionalProperty -InputObject $roleCfg -Name "allowed_tools" -DefaultValue @()))
    }
}

if ([string]::IsNullOrWhiteSpace($ToolName)) {
    throw "ToolName is required."
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$repoRoot = Get-V2RepoRoot
$registryPath = Join-Path $repoRoot "docs/agents/agent-tools-registry.json"
if (-not (Test-Path -LiteralPath $registryPath -PathType Leaf)) {
    throw "Tool registry not found: $registryPath"
}

$registry = Get-V2JsonContent -Path $registryPath
if (-not $registry) {
    throw "Invalid tool registry JSON: $registryPath"
}

$toolDef = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $registry -Name "tools" -DefaultValue ([PSCustomObject]@{})) -Name $ToolName -DefaultValue $null
if (-not $toolDef) {
    throw "tool-not-registered:$ToolName"
}

$policy = Get-V2RoleToolPolicy -Registry $registry -RoleName $AgentName
$allowedTools = @($policy.allowed_tools | ForEach-Object { [string]$_ })
if (-not ($ToolName -in $allowedTools)) {
    throw ("tool-not-allowed:role={0}:tool={1}" -f $policy.role_name, $ToolName)
}

$toolScriptRel = [string](Get-V2OptionalProperty -InputObject $toolDef -Name "script" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($toolScriptRel)) {
    throw "tool-script-missing:$ToolName"
}
$toolScriptPath = if ([System.IO.Path]::IsPathRooted($toolScriptRel)) { $toolScriptRel } else { Join-Path $repoRoot $toolScriptRel }
if (-not (Test-Path -LiteralPath $toolScriptPath -PathType Leaf)) {
    throw "tool-script-not-found:$toolScriptPath"
}

$toolArgs = [ordered]@{}
$toolArgs["ProjectPath"] = $projectRoot
$fixedMode = [string](Get-V2OptionalProperty -InputObject $toolDef -Name "fixed_mode" -DefaultValue "")
if (-not [string]::IsNullOrWhiteSpace($fixedMode)) {
    $toolArgs["Mode"] = $fixedMode
}
if (-not [string]::IsNullOrWhiteSpace($ToolArgumentsJson)) {
    $parsedArgs = $ToolArgumentsJson | ConvertFrom-Json
    foreach ($name in @($parsedArgs.PSObject.Properties.Name)) {
        $toolArgs[$name] = $parsedArgs.$name
    }
}

$startedAt = Get-Date
$success = $false
$errorText = ""
$rawOutput = ""

try {
    $rawOutput = (& $toolScriptPath @toolArgs 2>&1 | Out-String)
    $success = $true
}
catch {
    $success = $false
    $errorText = $_.Exception.Message
}

$finishedAt = Get-Date
$durationMs = [int][Math]::Round((($finishedAt - $startedAt).TotalMilliseconds), 0)

$usageLogDir = Join-Path $projectRoot "ai-orchestrator/state"
Initialize-V2Directory -Path $usageLogDir
$usageLogPath = Join-Path $usageLogDir "tool-usage-log.jsonl"

$event = [PSCustomObject]@{
    timestamp   = Get-V2Timestamp
    role        = $policy.role_name
    agent_name  = $AgentName
    tool        = $ToolName
    success     = $success
    duration_ms = $durationMs
    error       = $errorText
}
Add-Content -LiteralPath $usageLogPath -Value ($event | ConvertTo-Json -Depth 5 -Compress)

$result = [PSCustomObject]@{
    success      = $success
    tool         = $ToolName
    role         = $policy.role_name
    duration_ms  = $durationMs
    output       = $rawOutput
    error        = $errorText
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
else {
    Write-Output ("Tool dispatch: tool={0} role={1} success={2}" -f $ToolName, $policy.role_name, $success)
}

if (-not $success) {
    throw ("tool-dispatch-failed:{0}" -f $errorText)
}
