<#
.SYNOPSIS
    Step-level checkpoint helpers for task execution resume.
.DESCRIPTION
    Writes/reads/clears per-task step checkpoints under:
      ai-orchestrator/tasks/checkpoints/step-<task>-<step>.json
    Intended to be dot-sourced by Run-AgentLoop.ps1, but can also be invoked directly.
.PARAMETER Mode
    Optional direct mode: write | read | clear | noop.
#>
param(
    [ValidateSet("write", "read", "clear", "noop")]
    [string]$Mode = "noop",
    [string]$ProjectPath = ".",
    [string]$TaskId = "",
    [int]$StepNumber = 0,
    [string]$StepName = "",
    [ValidateSet("", "running", "ok", "failed", "skipped")]
    [string]$Status = "",
    [string]$AgentName = "",
    [string]$Details = "",
    [string]$ErrorText = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2StepCheckpointDirectory {
    param([string]$ProjectRoot)
    return (Join-Path $ProjectRoot "ai-orchestrator/tasks/checkpoints")
}

function Get-V2TaskCheckpointFiles {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        return @()
    }
    $checkpointDir = Get-V2StepCheckpointDirectory -ProjectRoot $ProjectRoot
    if (-not (Test-Path -LiteralPath $checkpointDir -PathType Container)) {
        return @()
    }
    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    return @(
        Get-ChildItem -LiteralPath $checkpointDir -File -Filter ("step-{0}-*.json" -f $safeTaskId) -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc, Name
    )
}

function Read-V2StepCheckpoint {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    $files = @(Get-V2TaskCheckpointFiles -ProjectRoot $ProjectRoot -TaskId $TaskId)
    if ($files.Count -eq 0) {
        return $null
    }

    $snapshots = New-Object System.Collections.Generic.List[object]
    foreach ($file in $files) {
        $doc = Get-V2JsonContent -Path $file.FullName
        if (-not $doc) { continue }
        $stepNumber = [int](Get-V2OptionalProperty -InputObject $doc -Name "step_number" -DefaultValue 0)
        if ($stepNumber -le 0) {
            $nameMatch = [regex]::Match($file.Name, "-(\d+)\.json$")
            if ($nameMatch.Success) {
                $stepNumber = [int]$nameMatch.Groups[1].Value
            }
        }
        $snapshots.Add([PSCustomObject]@{
                file         = $file.FullName
                step_number  = $stepNumber
                step_name    = [string](Get-V2OptionalProperty -InputObject $doc -Name "step_name" -DefaultValue "")
                status       = [string](Get-V2OptionalProperty -InputObject $doc -Name "status" -DefaultValue "")
                task_id      = [string](Get-V2OptionalProperty -InputObject $doc -Name "task_id" -DefaultValue $TaskId)
                agent_name   = [string](Get-V2OptionalProperty -InputObject $doc -Name "agent_name" -DefaultValue "")
                details      = [string](Get-V2OptionalProperty -InputObject $doc -Name "details" -DefaultValue "")
                error        = [string](Get-V2OptionalProperty -InputObject $doc -Name "error" -DefaultValue "")
                updated_at   = [string](Get-V2OptionalProperty -InputObject $doc -Name "updated_at" -DefaultValue "")
                generated_at = [string](Get-V2OptionalProperty -InputObject $doc -Name "generated_at" -DefaultValue "")
            })
    }
    if ($snapshots.Count -eq 0) {
        return $null
    }

    return @($snapshots.ToArray() | Sort-Object step_number, generated_at | Select-Object -Last 1)[0]
}

function Write-V2StepCheckpoint {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [int]$StepNumber,
        [string]$StepName,
        [string]$Status,
        [string]$AgentName = "",
        [string]$Details = "",
        [string]$ErrorText = ""
    )

    if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
        throw "ProjectRoot is required."
    }
    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        throw "TaskId is required."
    }
    if ([string]::IsNullOrWhiteSpace($StepName)) {
        throw "StepName is required."
    }
    if ($StepNumber -le 0) {
        $latest = Read-V2StepCheckpoint -ProjectRoot $ProjectRoot -TaskId $TaskId
        $StepNumber = if ($latest) { [int](Get-V2OptionalProperty -InputObject $latest -Name "step_number" -DefaultValue 0) + 1 } else { 1 }
    }

    $checkpointDir = Get-V2StepCheckpointDirectory -ProjectRoot $ProjectRoot
    Initialize-V2Directory -Path $checkpointDir
    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $checkpointPath = Join-Path $checkpointDir ("step-{0}-{1}.json" -f $safeTaskId, $StepNumber)

    $detailsText = [string]$Details
    if ($detailsText.Length -gt 1000) {
        $detailsText = $detailsText.Substring(0, 1000) + "..."
    }
    $errorValue = [string]$ErrorText
    if ($errorValue.Length -gt 1000) {
        $errorValue = $errorValue.Substring(0, 1000) + "..."
    }

    $payload = [PSCustomObject]@{
        generated_at = Get-V2Timestamp
        updated_at   = Get-V2Timestamp
        task_id      = $TaskId
        step_number  = $StepNumber
        step_name    = $StepName
        status       = $Status
        agent_name   = $AgentName
        details      = $detailsText
        error        = $errorValue
    }
    Save-V2JsonContent -Path $checkpointPath -Value $payload

    return [PSCustomObject]@{
        path        = $checkpointPath
        task_id     = $TaskId
        step_number = $StepNumber
        step_name   = $StepName
        status      = $Status
    }
}

function Clear-V2StepCheckpoints {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    $files = @(Get-V2TaskCheckpointFiles -ProjectRoot $ProjectRoot -TaskId $TaskId)
    $removed = 0
    foreach ($file in $files) {
        try {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
            $removed++
        }
        catch {
            # Keep non-fatal: checkpoints are recoverability helpers.
        }
    }
    return $removed
}

if ($Mode -eq "write") {
    $projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
    $result = Write-V2StepCheckpoint -ProjectRoot $projectRoot -TaskId $TaskId -StepNumber $StepNumber -StepName $StepName -Status $Status -AgentName $AgentName -Details $Details -ErrorText $ErrorText
    Write-Output ($result | ConvertTo-Json -Depth 5)
}
elseif ($Mode -eq "read") {
    $projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
    $result = Read-V2StepCheckpoint -ProjectRoot $projectRoot -TaskId $TaskId
    if ($null -eq $result) {
        Write-Output "{}"
    }
    else {
        Write-Output ($result | ConvertTo-Json -Depth 5)
    }
}
elseif ($Mode -eq "clear") {
    $projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
    $removed = Clear-V2StepCheckpoints -ProjectRoot $projectRoot -TaskId $TaskId
    Write-Output ([PSCustomObject]@{ removed = $removed } | ConvertTo-Json -Depth 3)
}
