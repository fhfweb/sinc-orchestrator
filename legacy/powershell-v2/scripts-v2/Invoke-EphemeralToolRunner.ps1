<#
.SYNOPSIS
    Executes an ephemeral tool registered dynamically by an agent at runtime.
.DESCRIPTION
    Loads a tool script from workspace/tmp_tools/<tool_name>.ps1, runs it with
    the supplied arguments JSON, captures output and returns it.
    Ephemeral tools are sandboxed to the project workspace: they cannot write
    outside of it. Tools are discarded (removed) after MaxAgeCycles cycles.
.PARAMETER ProjectPath
    Project root path.
.PARAMETER ToolName
    Name of the ephemeral tool to execute (must match a file in tmp_tools/).
.PARAMETER ToolArgumentsJson
    JSON object with named arguments to pass to the tool script.
.PARAMETER TimeoutSeconds
    Max execution time. Default 60.
.PARAMETER EmitJson
    When set, wraps output in a JSON envelope.
.EXAMPLE
    .\Invoke-EphemeralToolRunner.ps1 -ProjectPath . -ToolName pdf_to_md -ToolArgumentsJson '{"InputPath":"doc.pdf"}'
#>
param(
    [string]$ProjectPath      = ".",
    [string]$ToolName         = "",
    [string]$ToolArgumentsJson = "{}",
    [int]$TimeoutSeconds      = 60,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Test-V2SafeToolName {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $false }
    return ($Name -match "^[A-Za-z0-9_-]{1,80}$")
}

function Get-V2Sha256Hex {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-V2HmacSha256Hex {
    param(
        [string]$Secret,
        [string]$Payload
    )
    $keyBytes = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($Payload)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256($keyBytes)
    try {
        $hash = $hmac.ComputeHash($payloadBytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hmac.Dispose()
    }
}

function ConvertTo-V2PsSingleQuotedLiteral {
    param([string]$Value)
    $escaped = ([string]$Value).Replace("'", "''")
    return "'$escaped'"
}

if ([string]::IsNullOrWhiteSpace($ToolName)) {
    throw "ToolName is required."
}
if (-not (Test-V2SafeToolName -Name $ToolName)) {
    throw "invalid-tool-name"
}

$projectRoot   = Resolve-V2AbsolutePath -Path $ProjectPath
$tmpToolsDir   = Join-Path (Join-Path $projectRoot "workspace") "tmp_tools"
$toolScript    = Join-Path $tmpToolsDir "$ToolName.ps1"
$manifestPath  = Join-Path $tmpToolsDir "$ToolName.json"

if (-not (Test-Path -LiteralPath $toolScript -PathType Leaf)) {
    $err = "ephemeral-tool-not-found:$ToolName"
    if ($EmitJson) { Write-Output (ConvertTo-Json @{success=$false;error=$err} -Compress) }
    else           { throw $err }
    exit 1
}

# Parse arguments
$toolArgs = @{}
try {
    $parsed = $ToolArgumentsJson | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    if ($parsed -is [System.Collections.Hashtable]) { $toolArgs = $parsed }
} catch { <# ignore parse errors; tool gets empty args #> }

$manifest = $null
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -AsHashtable
    }
    catch {
        throw "ephemeral-tool-manifest-invalid"
    }
}
if ($null -eq $manifest) {
    throw "ephemeral-tool-manifest-missing"
}

$status = [string]($manifest["status"])
if (-not [string]::IsNullOrWhiteSpace($status) -and $status.ToLowerInvariant() -ne "active") {
    throw ("ephemeral-tool-status-invalid:{0}" -f $status)
}

$expiresAtText = [string]($manifest["expires_at"])
if (-not [string]::IsNullOrWhiteSpace($expiresAtText)) {
    try {
        $expiresAt = [DateTimeOffset]::Parse($expiresAtText)
        if ($expiresAt.UtcDateTime -lt (Get-Date).ToUniversalTime()) {
            $manifest["status"] = "expired"
            $manifest["expired_at"] = (Get-Date -Format "o")
            $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8 -NoNewline
            throw "ephemeral-tool-expired"
        }
    }
    catch {
        if ($_.Exception.Message -eq "ephemeral-tool-expired") { throw }
        throw "ephemeral-tool-expiry-parse-failed"
    }
}

$scriptContent = Get-Content -LiteralPath $toolScript -Raw -ErrorAction Stop
$actualHash = Get-V2Sha256Hex -Text $scriptContent
$expectedHash = [string]($manifest["script_sha256"])
if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and ($actualHash -ne $expectedHash.ToLowerInvariant())) {
    throw "ephemeral-tool-integrity-failed:hash-mismatch"
}

$requireSignature = ($env:ORCHESTRATOR_TOOL_REQUIRE_SIGNATURE -in @("1","true","yes","on"))
$signingKey = [string]$env:ORCHESTRATOR_TOOL_SIGNING_KEY
$expectedSig = [string]($manifest["signature_hmac_sha256"])
if ($requireSignature -and [string]::IsNullOrWhiteSpace($expectedSig)) {
    throw "ephemeral-tool-signature-missing"
}
if (-not [string]::IsNullOrWhiteSpace($expectedSig)) {
    if ([string]::IsNullOrWhiteSpace($signingKey)) {
        throw "ephemeral-tool-signature-key-missing"
    }
    $sigPayload = "{0}|{1}|{2}|{3}" -f $ToolName, $actualHash, ([string]$manifest["registered_at"]), ([string]$manifest["expires_at"])
    $actualSig = Get-V2HmacSha256Hex -Secret $signingKey -Payload $sigPayload
    if ($actualSig -ne $expectedSig.ToLowerInvariant()) {
        throw "ephemeral-tool-integrity-failed:signature-mismatch"
    }
}

# Build argument list for the PS script
$psArgs = @()
foreach ($kv in $toolArgs.GetEnumerator()) {
    $keyName = [string]$kv.Key
    if ($keyName -notmatch "^[A-Za-z_][A-Za-z0-9_]{0,80}$") {
        throw ("ephemeral-tool-invalid-arg-name:{0}" -f $keyName)
    }
    $psArgs += "-$($kv.Key)"
    $psArgs += (ConvertTo-V2PsSingleQuotedLiteral -Value ([string]$kv.Value))
}

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName    = "powershell.exe"
$startInfo.Arguments   = ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$toolScript`" " + ($psArgs -join " "))
$startInfo.WorkingDirectory         = $projectRoot
$startInfo.RedirectStandardOutput   = $true
$startInfo.RedirectStandardError    = $true
$startInfo.UseShellExecute          = $false
$startInfo.CreateNoWindow           = $true

$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
$null = $process.Start()

if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    try { $process.Kill() } catch {}
    $err = "ephemeral-tool-timeout:$TimeoutSeconds"
    if ($EmitJson) { Write-Output (ConvertTo-Json @{success=$false;error=$err} -Compress) }
    else           { throw $err }
    exit 1
}

$stdout  = $process.StandardOutput.ReadToEnd()
$stderr  = $process.StandardError.ReadToEnd()
$exitCode = $process.ExitCode

# Update usage counter in manifest
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $currentRunCount = 0
        if ($manifest.ContainsKey("run_count") -and $null -ne $manifest["run_count"]) {
            try {
                $currentRunCount = [int]$manifest["run_count"]
            }
            catch {
                $currentRunCount = 0
            }
        }
        $manifest["run_count"] = $currentRunCount + 1
        $manifest["last_run"] = (Get-Date -Format "o")
        $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8 -NoNewline
    } catch { <# non-fatal #> }
}

if ($EmitJson) {
    $result = @{
        success    = ($exitCode -eq 0)
        tool_name  = $ToolName
        exit_code  = $exitCode
        output     = ($stdout.TrimEnd())
        stderr     = ($stderr.TrimEnd())
    }
    Write-Output (ConvertTo-Json $result -Compress -Depth 4)
} else {
    if (-not [string]::IsNullOrWhiteSpace($stdout)) { Write-Output $stdout.TrimEnd() }
    if (-not [string]::IsNullOrWhiteSpace($stderr))  { Write-Output ("STDERR: " + $stderr.TrimEnd()) }
    if ($exitCode -ne 0) { throw "ephemeral-tool-failed:exit-code:$exitCode" }
}
