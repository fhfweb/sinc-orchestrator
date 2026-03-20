<#
.SYNOPSIS
    Validates PowerShell script syntax for orchestration core scripts.
.DESCRIPTION
    Parses scripts under /scripts and /scripts/v2 using the PowerShell parser.
    Fails hard when parser errors are found so loop cannot progress with broken scripts.
.PARAMETER ProjectPath
    Project root path.
.PARAMETER EmitJson
    Emit JSON result to stdout.
#>
param(
    [string]$ProjectPath = ".",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function ConvertTo-V2RelativeUnixPath {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    if ($targetFull.StartsWith($baseFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = $targetFull.Substring($baseFull.Length).TrimStart('\', '/')
        return ($relative -replace "\\", "/")
    }
    return ($targetFull -replace "\\", "/")
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$validationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$scriptsRoot = Join-Path $validationRoot "scripts"
if (-not (Test-Path -LiteralPath $scriptsRoot -PathType Container)) {
    throw "scripts directory not found: $scriptsRoot"
}

$filesByPath = @{}
$coreCandidates = @(
    (Join-Path $scriptsRoot "Run-AgentLoop.ps1"),
    (Join-Path $scriptsRoot "Sync-TaskState.ps1"),
    (Join-Path $scriptsRoot "Sync-LockState.ps1"),
    (Join-Path $scriptsRoot "Update-Dashboard.ps1")
)
foreach ($candidate in $coreCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $filesByPath[$candidate.ToLowerInvariant()] = Get-Item -LiteralPath $candidate
    }
}

$v2Root = Join-Path $scriptsRoot "v2"
if (Test-Path -LiteralPath $v2Root -PathType Container) {
    foreach ($file in @(Get-ChildItem -LiteralPath $v2Root -Recurse -File -Filter "*.ps1" -ErrorAction SilentlyContinue)) {
        $filesByPath[$file.FullName.ToLowerInvariant()] = $file
    }
}

$files = @($filesByPath.Values | Sort-Object FullName)

$issues = New-Object System.Collections.Generic.List[object]
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    foreach ($parseError in @($parseErrors)) {
        if ($null -eq $parseError) { continue }
        $extent = $parseError.Extent
        $line = if ($extent) { [int]$extent.StartLineNumber } else { 0 }
        $column = if ($extent) { [int]$extent.StartColumnNumber } else { 0 }
        $issues.Add([PSCustomObject]@{
                file    = (ConvertTo-V2RelativeUnixPath -BasePath $validationRoot -TargetPath $file.FullName)
                line    = $line
                column  = $column
                message = [string]$parseError.Message
            })
    }
}

$reportDir = Join-Path $projectRoot "ai-orchestrator/reports"
Initialize-V2Directory -Path $reportDir
$reportPath = Join-Path $reportDir "script-validation.json"

$result = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    success      = $issues.Count -eq 0
    checked_files = $files.Count
    error_count  = $issues.Count
    errors       = @($issues.ToArray())
}
Save-V2JsonContent -Path $reportPath -Value $result

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
else {
    Write-Output ("Script validation checked {0} files with {1} errors." -f $files.Count, $issues.Count)
}

if ($issues.Count -gt 0) {
    throw ("script-validation-failed:{0}" -f $issues.Count)
}
