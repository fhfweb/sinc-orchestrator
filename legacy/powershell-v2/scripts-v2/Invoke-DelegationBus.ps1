<#
.SYNOPSIS
    Delegation bus for runtime sub-task requests between agents.
.DESCRIPTION
    Stores delegation requests in ai-orchestrator/state/delegation-bus.json and supports:
      - request: create a new delegation
      - process: mark pending requests as dispatched
      - complete: complete a delegation with result payload
      - list: inspect delegations
#>
param(
    [ValidateSet("request", "process", "complete", "list")]
    [string]$Mode = "list",
    [string]$ProjectPath = ".",
    [string]$DelegationId = "",
    [string]$FromAgent = "",
    [string]$ToAgent = "",
    [string]$ParentTaskId = "",
    [string]$Summary = "",
    [string]$ContextJson = "",
    [string]$ResultJson = "",
    [string]$ProcessorAgent = "",
    [string]$Status = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Read-V2DelegationBus {
    param([string]$Path)
    $doc = Get-V2JsonContent -Path $Path
    if (-not $doc) {
        return [PSCustomObject]@{
            generated_at = Get-V2Timestamp
            requests = @()
        }
    }
    $requests = @((Get-V2OptionalProperty -InputObject $doc -Name "requests" -DefaultValue @()))
    Set-V2DynamicProperty -InputObject $doc -Name "requests" -Value $requests
    return $doc
}

function Save-V2DelegationBus {
    param(
        [string]$Path,
        [object]$Bus
    )
    Set-V2DynamicProperty -InputObject $Bus -Name "generated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $Path -Value $Bus
}

function Request-V2Delegation {
    param(
        [object]$Bus,
        [string]$FromAgent,
        [string]$ToAgent,
        [string]$ParentTaskId,
        [string]$Summary,
        [object]$Context
    )

    if ([string]::IsNullOrWhiteSpace($FromAgent)) { throw "FromAgent is required for request mode." }
    if ([string]::IsNullOrWhiteSpace($ToAgent)) { throw "ToAgent is required for request mode." }
    if ([string]::IsNullOrWhiteSpace($ParentTaskId)) { throw "ParentTaskId is required for request mode." }
    if ([string]::IsNullOrWhiteSpace($Summary)) { throw "Summary is required for request mode." }

    $request = [PSCustomObject]@{
        id             = ("DLG-{0}" -f ((Get-Date).ToString("yyyyMMddHHmmssfff")))
        status         = "pending"
        from_agent     = $FromAgent
        to_agent       = $ToAgent
        parent_task_id = $ParentTaskId
        summary        = $Summary
        context        = $Context
        result         = $null
        created_at     = Get-V2Timestamp
        updated_at     = Get-V2Timestamp
        dispatched_at  = ""
        completed_at   = ""
    }

    $requests = @((Get-V2OptionalProperty -InputObject $Bus -Name "requests" -DefaultValue @()))
    $requests += $request
    Set-V2DynamicProperty -InputObject $Bus -Name "requests" -Value $requests
    return $request
}

function Process-V2DelegationRequests {
    param(
        [object]$Bus,
        [string]$ProcessorAgent
    )

    $requests = @((Get-V2OptionalProperty -InputObject $Bus -Name "requests" -DefaultValue @()))
    $dispatched = New-Object System.Collections.Generic.List[object]
    foreach ($req in $requests) {
        $reqStatus = [string](Get-V2OptionalProperty -InputObject $req -Name "status" -DefaultValue "pending")
        $reqTo = [string](Get-V2OptionalProperty -InputObject $req -Name "to_agent" -DefaultValue "")
        if ($reqStatus -ne "pending") { continue }
        if (-not [string]::IsNullOrWhiteSpace($ProcessorAgent) -and $reqTo -ne $ProcessorAgent) { continue }
        Set-V2DynamicProperty -InputObject $req -Name "status" -Value "dispatched"
        Set-V2DynamicProperty -InputObject $req -Name "dispatched_at" -Value (Get-V2Timestamp)
        Set-V2DynamicProperty -InputObject $req -Name "updated_at" -Value (Get-V2Timestamp)
        [void]$dispatched.Add($req)
    }
    Set-V2DynamicProperty -InputObject $Bus -Name "requests" -Value $requests
    return @($dispatched.ToArray())
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
$orchestratorRoot = Join-Path $projectRoot "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}
$busPath = Join-Path $orchestratorRoot "state/delegation-bus.json"
Initialize-V2Directory -Path (Split-Path -Parent $busPath)
$bus = Read-V2DelegationBus -Path $busPath

switch ($Mode) {
    "request" {
        $context = $null
        if (-not [string]::IsNullOrWhiteSpace($ContextJson)) {
            try {
                $context = $ContextJson | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                throw "ContextJson is not valid JSON."
            }
        }
        $created = Request-V2Delegation -Bus $bus -FromAgent $FromAgent -ToAgent $ToAgent -ParentTaskId $ParentTaskId -Summary $Summary -Context $context
        Save-V2DelegationBus -Path $busPath -Bus $bus
        $result = [PSCustomObject]@{
            success = $true
            mode = $Mode
            delegation = $created
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $busPath)
        }
        if ($EmitJson) { Write-Output ($result | ConvertTo-Json -Depth 10) } else { Write-Output ("delegation-requested id={0}" -f $created.id) }
    }
    "process" {
        $processed = Process-V2DelegationRequests -Bus $bus -ProcessorAgent $ProcessorAgent
        Save-V2DelegationBus -Path $busPath -Bus $bus
        $result = [PSCustomObject]@{
            success = $true
            mode = $Mode
            processor = $ProcessorAgent
            dispatched_count = @($processed).Count
            dispatched = @($processed)
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $busPath)
        }
        if ($EmitJson) { Write-Output ($result | ConvertTo-Json -Depth 10) } else { Write-Output ("delegation-processed count={0}" -f $result.dispatched_count) }
    }
    "complete" {
        if ([string]::IsNullOrWhiteSpace($DelegationId)) {
            throw "DelegationId is required for complete mode."
        }
        $requests = @((Get-V2OptionalProperty -InputObject $bus -Name "requests" -DefaultValue @()))
        $target = @($requests | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $DelegationId } | Select-Object -First 1)
        if ($target.Count -eq 0) {
            throw "DelegationId not found: $DelegationId"
        }
        $entry = $target[0]
        $resultPayload = $null
        if (-not [string]::IsNullOrWhiteSpace($ResultJson)) {
            try {
                $resultPayload = $ResultJson | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                throw "ResultJson is not valid JSON."
            }
        }
        Set-V2DynamicProperty -InputObject $entry -Name "status" -Value "completed"
        Set-V2DynamicProperty -InputObject $entry -Name "result" -Value $resultPayload
        Set-V2DynamicProperty -InputObject $entry -Name "completed_at" -Value (Get-V2Timestamp)
        Set-V2DynamicProperty -InputObject $entry -Name "updated_at" -Value (Get-V2Timestamp)
        Save-V2DelegationBus -Path $busPath -Bus $bus
        $response = [PSCustomObject]@{
            success = $true
            mode = $Mode
            delegation = $entry
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $busPath)
        }
        if ($EmitJson) { Write-Output ($response | ConvertTo-Json -Depth 10) } else { Write-Output ("delegation-completed id={0}" -f $DelegationId) }
    }
    "list" {
        $requests = @((Get-V2OptionalProperty -InputObject $bus -Name "requests" -DefaultValue @()))
        $filtered = $requests
        if (-not [string]::IsNullOrWhiteSpace($Status)) {
            $statusNorm = $Status.ToLowerInvariant()
            $filtered = @($filtered | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq $statusNorm })
        }
        if (-not [string]::IsNullOrWhiteSpace($ProcessorAgent)) {
            $filtered = @($filtered | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "to_agent" -DefaultValue "") -eq $ProcessorAgent })
        }
        $result = [PSCustomObject]@{
            success = $true
            mode = $Mode
            total = @($requests).Count
            returned = @($filtered).Count
            requests = @($filtered)
            path = (Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $busPath)
        }
        if ($EmitJson) { Write-Output ($result | ConvertTo-Json -Depth 10) } else { Write-Output ("delegation-list returned={0}" -f $result.returned) }
    }
}
