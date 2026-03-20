<#
.SYNOPSIS
    Manages human-in-the-loop gates per task/phase.
#>
param(
    [ValidateSet("create", "resolve", "check", "list")]
    [string]$Mode = "list",
    [string]$ProjectPath = ".",
    [string]$GateId = "",
    [string]$TaskId = "",
    [string]$Phase = "",
    [string]$RequestedBy = "",
    [string]$Reason = "",
    [ValidateSet("approve", "reject")]
    [string]$Decision = "approve",
    [string]$DecisionBy = "",
    [string]$DecisionRole = "",
    [string]$ApprovalToken = "",
    [switch]$OnlyOpen,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2Sha256Hex {
    param([string]$Text)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Text)
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

function Get-V2HitlPolicy {
    $envMode = ([string]$env:ORCHESTRATOR_ENV).Trim().ToLowerInvariant()
    $requireSignedText = ([string]$env:ORCHESTRATOR_HITL_REQUIRE_SIGNED).Trim().ToLowerInvariant()
    $requireSigned = $false
    if ($requireSignedText -in @("1", "true", "yes", "on")) {
        $requireSigned = $true
    }
    elseif ($requireSignedText -in @("0", "false", "no", "off")) {
        $requireSigned = $false
    }
    elseif ($envMode -in @("prod", "production")) {
        $requireSigned = $true
    }

    $rolesText = ([string]$env:ORCHESTRATOR_HITL_ALLOWED_ROLES).Trim()
    if ([string]::IsNullOrWhiteSpace($rolesText)) {
        $rolesText = "reviewer,admin,human-reviewer"
    }
    $roles = @($rolesText -split "," | ForEach-Object { ([string]$_).Trim().ToLowerInvariant() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)

    return [PSCustomObject]@{
        require_signed = $requireSigned
        signing_key    = ([string]$env:ORCHESTRATOR_HITL_SIGNING_KEY)
        allowed_roles  = @($roles)
    }
}

function Test-V2AllowedHitlRole {
    param(
        [string]$Role,
        [string[]]$AllowedRoles
    )

    $normalized = ([string]$Role).Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $false
    }
    return ($normalized -in @($AllowedRoles))
}

function Get-V2HitlSignaturePayload {
    param(
        [string]$GateId,
        [string]$Decision,
        [string]$DecisionBy,
        [string]$DecisionRole
    )

    return ("{0}|{1}|{2}|{3}" -f $GateId, $Decision.ToLowerInvariant(), $DecisionBy.Trim(), $DecisionRole.Trim().ToLowerInvariant())
}

function Get-V2LastAuditHash {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    $tail = ""
    try {
        $tail = (Get-Content -LiteralPath $Path -Tail 1 -ErrorAction Stop | Out-String).Trim()
    }
    catch {
        $tail = ""
    }
    if ([string]::IsNullOrWhiteSpace($tail)) {
        return ""
    }
    try {
        $parsed = $tail | ConvertFrom-Json -ErrorAction Stop
        return [string](Get-V2OptionalProperty -InputObject $parsed -Name "event_hash" -DefaultValue "")
    }
    catch {
        return ""
    }
}

function Add-V2HitlAuditEvent {
    param(
        [string]$Path,
        [string]$EventType,
        [string]$GateId,
        [string]$TaskId = "",
        [string]$Decision = "",
        [string]$DecisionBy = "",
        [string]$DecisionRole = "",
        [bool]$SignatureValidated = $false,
        [string]$Notes = ""
    )

    Initialize-V2Directory -Path (Split-Path -Parent $Path)
    $previousHash = Get-V2LastAuditHash -Path $Path

    $event = [ordered]@{
        timestamp            = Get-V2Timestamp
        event_type           = $EventType
        gate_id              = $GateId
        task_id              = $TaskId
        decision             = $Decision
        decision_by          = $DecisionBy
        decision_role        = $DecisionRole
        signature_validated  = $SignatureValidated
        notes                = $Notes
        previous_hash        = $previousHash
    }

    $eventJson = ($event | ConvertTo-Json -Depth 8 -Compress)
    $eventHash = Get-V2Sha256Hex -Text ("{0}|{1}" -f $previousHash, $eventJson)
    $event["event_hash"] = $eventHash
    Add-Content -LiteralPath $Path -Value ($event | ConvertTo-Json -Depth 8 -Compress)
}

function Read-V2HitlDoc {
    param([string]$Path)
    $doc = Get-V2JsonContent -Path $Path
    if (-not $doc) {
        return [PSCustomObject]@{
            generated_at = Get-V2Timestamp
            gates = @()
        }
    }
    $gates = @((Get-V2OptionalProperty -InputObject $doc -Name "gates" -DefaultValue @()))
    Set-V2DynamicProperty -InputObject $doc -Name "gates" -Value $gates
    return $doc
}

function Save-V2HitlDoc {
    param([string]$Path, [object]$Doc)
    Set-V2DynamicProperty -InputObject $Doc -Name "generated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $Path -Value $Doc
}

function Set-V2TaskStatusInDag {
    param(
        [string]$TaskDagPath,
        [string]$TaskId,
        [string]$Status,
        [string]$ReasonText = ""
    )

    if ([string]::IsNullOrWhiteSpace($TaskId)) { return $false }
    if (-not (Test-Path -LiteralPath $TaskDagPath -PathType Leaf)) { return $false }

    $dag = Get-V2JsonContent -Path $TaskDagPath
    if (-not $dag) { return $false }
    $tasks = @((Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @()))
    $found = $false
    foreach ($task in $tasks) {
        $id = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if ($id -ne $TaskId) { continue }
        Set-V2DynamicProperty -InputObject $task -Name "status" -Value $Status
        Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        if (-not [string]::IsNullOrWhiteSpace($ReasonText)) {
            Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value $ReasonText
        }
        else {
            Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
        }
        $found = $true
        break
    }
    if (-not $found) { return $false }

    Set-V2DynamicProperty -InputObject $dag -Name "tasks" -Value $tasks
    Set-V2DynamicProperty -InputObject $dag -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $TaskDagPath -Value $dag
    return $true
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$hitlPath = Join-Path $orchestratorRoot "state/hitl-gates.json"
$hitlAuditPath = Join-Path $orchestratorRoot "state/hitl-audit.jsonl"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
Initialize-V2Directory -Path (Split-Path -Parent $hitlPath)
$doc = Read-V2HitlDoc -Path $hitlPath
$policy = Get-V2HitlPolicy

switch ($Mode) {
    "create" {
        if ([string]::IsNullOrWhiteSpace($TaskId)) { throw "TaskId is required for create mode." }
        if ([string]::IsNullOrWhiteSpace($Reason)) { throw "Reason is required for create mode." }
        $newGate = [PSCustomObject]@{
            gate_id      = if ([string]::IsNullOrWhiteSpace($GateId)) { "HITL-$((Get-Date).ToString('yyyyMMddHHmmssfff'))" } else { $GateId }
            task_id      = $TaskId
            phase        = $Phase
            status       = "open"
            reason       = $Reason
            requested_by = $RequestedBy
            requested_at = Get-V2Timestamp
            decided_by   = ""
            decided_at   = ""
            decision     = ""
        }
        $gates = @((Get-V2OptionalProperty -InputObject $doc -Name "gates" -DefaultValue @()))
        $gates += $newGate
        Set-V2DynamicProperty -InputObject $doc -Name "gates" -Value $gates
        Save-V2HitlDoc -Path $hitlPath -Doc $doc
        [void](Set-V2TaskStatusInDag -TaskDagPath $taskDagPath -TaskId $TaskId -Status "waiting-approval" -ReasonText ("hitl-gate-open:" + $newGate.gate_id))
        Add-V2HitlAuditEvent -Path $hitlAuditPath -EventType "gate-created" -GateId ([string]$newGate.gate_id) -TaskId $TaskId -Notes $Reason
        $result = [PSCustomObject]@{
            success = $true
            mode = $Mode
            gate = $newGate
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $hitlPath)
        }
        if ($EmitJson) { Write-Output ($result | ConvertTo-Json -Depth 10) } else { Write-Output ("hitl-gate-created gate={0} task={1}" -f $newGate.gate_id, $TaskId) }
    }
    "resolve" {
        if ([string]::IsNullOrWhiteSpace($GateId)) { throw "GateId is required for resolve mode." }
        $gates = @((Get-V2OptionalProperty -InputObject $doc -Name "gates" -DefaultValue @()))
        $target = @($gates | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "gate_id" -DefaultValue "") -eq $GateId } | Select-Object -First 1)
        if ($target.Count -eq 0) {
            throw "GateId not found: $GateId"
        }
        $gate = $target[0]
        $effectiveDecisionRole = if ([string]::IsNullOrWhiteSpace($DecisionRole)) { "reviewer" } else { $DecisionRole.Trim().ToLowerInvariant() }
        if (-not (Test-V2AllowedHitlRole -Role $effectiveDecisionRole -AllowedRoles @($policy.allowed_roles))) {
            throw ("hitl-role-not-allowed:{0}" -f $effectiveDecisionRole)
        }

        $signatureValidated = $false
        $tokenValue = ([string]$ApprovalToken).Trim()
        $signaturePayload = Get-V2HitlSignaturePayload -GateId $GateId -Decision $Decision -DecisionBy $DecisionBy -DecisionRole $effectiveDecisionRole
        $signingKey = ([string](Get-V2OptionalProperty -InputObject $policy -Name "signing_key" -DefaultValue "")).Trim()
        $requiresSigned = [bool](Get-V2OptionalProperty -InputObject $policy -Name "require_signed" -DefaultValue $false)

        if ($requiresSigned -or -not [string]::IsNullOrWhiteSpace($tokenValue)) {
            if ([string]::IsNullOrWhiteSpace($signingKey)) {
                throw "hitl-signing-key-missing"
            }
            if ([string]::IsNullOrWhiteSpace($tokenValue)) {
                throw "hitl-approval-token-missing"
            }
            $expectedToken = Get-V2HmacSha256Hex -Secret $signingKey -Payload $signaturePayload
            if ($expectedToken -ne $tokenValue.ToLowerInvariant()) {
                throw "hitl-approval-token-invalid"
            }
            $signatureValidated = $true
        }

        Set-V2DynamicProperty -InputObject $gate -Name "status" -Value "closed"
        Set-V2DynamicProperty -InputObject $gate -Name "decision" -Value $Decision
        Set-V2DynamicProperty -InputObject $gate -Name "decided_by" -Value $DecisionBy
        Set-V2DynamicProperty -InputObject $gate -Name "decision_role" -Value $effectiveDecisionRole
        Set-V2DynamicProperty -InputObject $gate -Name "decided_at" -Value (Get-V2Timestamp)
        Set-V2DynamicProperty -InputObject $gate -Name "signature_validated" -Value $signatureValidated
        Save-V2HitlDoc -Path $hitlPath -Doc $doc

        $taskIdForGate = [string](Get-V2OptionalProperty -InputObject $gate -Name "task_id" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($taskIdForGate)) {
            if ($Decision -eq "approve") {
                [void](Set-V2TaskStatusInDag -TaskDagPath $taskDagPath -TaskId $taskIdForGate -Status "in-progress" -ReasonText "")
            }
            else {
                [void](Set-V2TaskStatusInDag -TaskDagPath $taskDagPath -TaskId $taskIdForGate -Status "blocked-phase-approval" -ReasonText ("hitl-gate-rejected:" + $GateId))
            }
        }
        Add-V2HitlAuditEvent `
            -Path $hitlAuditPath `
            -EventType "gate-resolved" `
            -GateId $GateId `
            -TaskId $taskIdForGate `
            -Decision $Decision `
            -DecisionBy $DecisionBy `
            -DecisionRole $effectiveDecisionRole `
            -SignatureValidated $signatureValidated `
            -Notes ""
        $result = [PSCustomObject]@{
            success = $true
            mode = $Mode
            gate = $gate
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $hitlPath)
        }
        if ($EmitJson) { Write-Output ($result | ConvertTo-Json -Depth 10) } else { Write-Output ("hitl-gate-{0} gate={1}" -f $Decision, $GateId) }
    }
    "check" {
        if ([string]::IsNullOrWhiteSpace($TaskId)) { throw "TaskId is required for check mode." }
        $gates = @((Get-V2OptionalProperty -InputObject $doc -Name "gates" -DefaultValue @()))
        $openForTask = @($gates | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "") -eq $TaskId -and
                [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "open"
            })
        $res = [PSCustomObject]@{
            success = $true
            mode = $Mode
            task_id = $TaskId
            allowed = (@($openForTask).Count -eq 0)
            open_gates = @($openForTask)
        }
        if ($EmitJson) { Write-Output ($res | ConvertTo-Json -Depth 10) } else { Write-Output ("hitl-check task={0} allowed={1} open={2}" -f $TaskId, $res.allowed, @($openForTask).Count) }
    }
    "list" {
        $gates = @((Get-V2OptionalProperty -InputObject $doc -Name "gates" -DefaultValue @()))
        $filtered = $gates
        if ($OnlyOpen) {
            $filtered = @($filtered | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "open" })
        }
        if (-not [string]::IsNullOrWhiteSpace($TaskId)) {
            $filtered = @($filtered | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "") -eq $TaskId })
        }
        $res = [PSCustomObject]@{
            success = $true
            mode = $Mode
            total = @($gates).Count
            returned = @($filtered).Count
            gates = @($filtered)
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $hitlPath)
        }
        if ($EmitJson) { Write-Output ($res | ConvertTo-Json -Depth 10) } else { Write-Output ("hitl-gates returned={0}" -f $res.returned) }
    }
}
