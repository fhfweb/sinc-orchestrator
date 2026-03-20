<#
.SYNOPSIS
    Appends runtime events to stream-events.jsonl for live dashboard/SSE consumers.
#>
param(
    [string]$ProjectPath = ".",
    [string]$EventType = "info",
    [ValidateSet("debug", "info", "warn", "error")]
    [string]$Level = "info",
    [string]$Source = "orchestrator",
    [string]$Message = "",
    [string]$TaskId = "",
    [string]$AgentName = "",
    [string]$PayloadJson = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$eventsPath = Join-Path $orchestratorRoot "state/stream-events.jsonl"
Initialize-V2Directory -Path (Split-Path -Parent $eventsPath)

$payload = $null
if (-not [string]::IsNullOrWhiteSpace($PayloadJson)) {
    try {
        $payload = $PayloadJson | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "PayloadJson is not valid JSON."
    }
}

$event = [PSCustomObject]@{
    timestamp  = Get-V2Timestamp
    event_type = $EventType
    level      = $Level
    source     = $Source
    message    = $Message
    task_id    = $TaskId
    agent_name = $AgentName
    payload    = $payload
}

Add-Content -LiteralPath $eventsPath -Value ($event | ConvertTo-Json -Depth 10 -Compress)

$result = [PSCustomObject]@{
    success = $true
    path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $eventsPath)
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 5)
}
else {
    Write-Output ("stream-event-ok path={0}" -f $result.path)
}
