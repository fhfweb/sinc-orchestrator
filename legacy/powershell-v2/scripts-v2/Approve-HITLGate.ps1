<#
.SYNOPSIS
    Convenience CLI for listing and approving/rejecting HITL gates.
#>
param(
    [ValidateSet("list", "approve", "reject")]
    [string]$Action = "list",
    [string]$ProjectPath = ".",
    [string]$GateId = "",
    [string]$TaskId = "",
    [string]$DecisionBy = "human-reviewer",
    [string]$DecisionRole = "reviewer",
    [string]$ApprovalToken = "",
    [switch]$AutoSign,
    [switch]$OnlyOpen,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$hitlScript = Join-Path $PSScriptRoot "Invoke-HITLGate.ps1"
if (-not (Test-Path -LiteralPath $hitlScript -PathType Leaf)) {
    throw "Invoke-HITLGate.ps1 not found: $hitlScript"
}

function Get-V2HmacSha256Hex {
    param(
        [string]$Secret,
        [string]$Payload
    )

    $secretBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Secret)
    $payloadBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Payload)
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($secretBytes)
    try {
        $hash = $hmac.ComputeHash($payloadBytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hmac.Dispose()
    }
}

function Get-V2HitlDecisionToken {
    param(
        [string]$GateId,
        [string]$Decision,
        [string]$DecisionBy,
        [string]$DecisionRole,
        [string]$ExistingToken
    )

    if (-not [string]::IsNullOrWhiteSpace($ExistingToken)) {
        return $ExistingToken.Trim()
    }

    $envMode = ([string]$env:ORCHESTRATOR_ENV).Trim().ToLowerInvariant()
    $requireSignedText = ([string]$env:ORCHESTRATOR_HITL_REQUIRE_SIGNED).Trim().ToLowerInvariant()
    $requiresSigned = $false
    if ($requireSignedText -in @("1", "true", "yes", "on")) {
        $requiresSigned = $true
    }
    elseif ($requireSignedText -in @("0", "false", "no", "off")) {
        $requiresSigned = $false
    }
    elseif ($envMode -in @("prod", "production")) {
        $requiresSigned = $true
    }

    $signingKey = ([string]$env:ORCHESTRATOR_HITL_SIGNING_KEY).Trim()
    $shouldAutoSign = $AutoSign -or $requiresSigned
    if (-not $shouldAutoSign) {
        return ""
    }
    if ([string]::IsNullOrWhiteSpace($signingKey)) {
        throw "ORCHESTRATOR_HITL_SIGNING_KEY is required to auto-sign HITL decisions."
    }

    $payload = ("{0}|{1}|{2}|{3}" -f $GateId, $Decision.ToLowerInvariant(), $DecisionBy.Trim(), $DecisionRole.Trim().ToLowerInvariant())
    return (Get-V2HmacSha256Hex -Secret $signingKey -Payload $payload)
}

switch ($Action) {
    "list" {
        & $hitlScript -Mode list -ProjectPath $ProjectPath -TaskId $TaskId -OnlyOpen:$OnlyOpen -EmitJson:$EmitJson
    }
    "approve" {
        if ([string]::IsNullOrWhiteSpace($GateId)) {
            throw "GateId is required for Action=approve."
        }
        $token = Get-V2HitlDecisionToken -GateId $GateId -Decision "approve" -DecisionBy $DecisionBy -DecisionRole $DecisionRole -ExistingToken $ApprovalToken
        & $hitlScript -Mode resolve -ProjectPath $ProjectPath -GateId $GateId -Decision approve -DecisionBy $DecisionBy -DecisionRole $DecisionRole -ApprovalToken $token -EmitJson:$EmitJson
    }
    "reject" {
        if ([string]::IsNullOrWhiteSpace($GateId)) {
            throw "GateId is required for Action=reject."
        }
        $token = Get-V2HitlDecisionToken -GateId $GateId -Decision "reject" -DecisionBy $DecisionBy -DecisionRole $DecisionRole -ExistingToken $ApprovalToken
        & $hitlScript -Mode resolve -ProjectPath $ProjectPath -GateId $GateId -Decision reject -DecisionBy $DecisionBy -DecisionRole $DecisionRole -ApprovalToken $token -EmitJson:$EmitJson
    }
}
