<#
.SYNOPSIS
    Cleans orphan/stale REPAIR tasks and requeues transient runtime failures.
.DESCRIPTION
    Detects open REPAIR tasks that are orphaned (no active lock coverage) or stale,
    and reclassifies them to pending with takeover metadata.
    Also requeues transient blocked-runtime REPAIR failures.
    Optionally syncs task_state_db after changes.
#>
param(
    [string]$ProjectPath = ".",
    [int]$InProgressStaleMinutes = 15,
    [int]$BlockedRuntimeStaleMinutes = 10,
    [bool]$UseTaskStateDb = $true,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Set-V2CleanupObjectProperty {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$Value
    )
    if ($InputObject.PSObject.Properties.Name -contains $Name) {
        $InputObject.$Name = $Value
    }
    else {
        Add-Member -InputObject $InputObject -MemberType NoteProperty -Name $Name -Value $Value -Force
    }
}

function Get-V2CleanupTaskStatus {
    param([object]$Task)

    $raw = [string](Get-V2OptionalProperty -InputObject $Task -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($raw)) { return "pending" }
    return $raw.ToLowerInvariant()
}

function Get-V2CleanupTaskArrayProperty {
    param(
        [object]$Task,
        [string]$Name
    )
    $value = Get-V2OptionalProperty -InputObject $Task -Name $Name -DefaultValue @()
    if ($null -eq $value) { return @() }
    if ($value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($value)) { return @() }
        return @($value)
    }
    return @($value)
}

function Get-V2CleanupTaskIdleMinutes {
    param([object]$Task)

    $updatedAtRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "updated_at" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($updatedAtRaw)) {
        $updatedAtRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "created_at" -DefaultValue "")
    }
    if ([string]::IsNullOrWhiteSpace($updatedAtRaw)) { return 0.0 }
    try {
        $updatedAtUtc = ([DateTimeOffset]::Parse($updatedAtRaw)).UtcDateTime
        return [Math]::Round(((Get-Date).ToUniversalTime() - $updatedAtUtc).TotalMinutes, 2)
    }
    catch {
        return 0.0
    }
}

function Test-V2CleanupTaskLockCoverage {
    param(
        [string]$LocksPath,
        [string]$TaskId,
        [string]$Agent,
        [string[]]$FilesAffected
    )

    if ([string]::IsNullOrWhiteSpace($TaskId) -or [string]::IsNullOrWhiteSpace($Agent)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $LocksPath -PathType Leaf)) {
        return $false
    }

    $locksDoc = Get-V2JsonContent -Path $LocksPath
    if (-not $locksDoc -or -not ($locksDoc.PSObject.Properties.Name -contains "locks")) {
        return $false
    }

    $activeStatuses = @("active", "locked", "in-progress")
    $taskLocks = @($locksDoc.locks | Where-Object {
            $lockTaskId = [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "")
            $lockAgent = [string](Get-V2OptionalProperty -InputObject $_ -Name "agent" -DefaultValue "")
            $lockStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "active")
            ($lockTaskId -eq $TaskId) -and ($lockAgent -eq $Agent) -and ($activeStatuses -contains $lockStatus)
        })
    if ($taskLocks.Count -eq 0) {
        return $false
    }

    $files = @($FilesAffected | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($files.Count -eq 0) {
        return $true
    }

    $lockedPaths = @($taskLocks | ForEach-Object {
            [string](Get-V2OptionalProperty -InputObject $_ -Name "file_path" -DefaultValue "")
        } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lockedPaths.Count -eq 0) {
        return $false
    }

    foreach ($file in $files) {
        $hasCoverage = $false
        foreach ($path in $lockedPaths) {
            if ($path -eq $file -or $path -eq "*" -or $path -like "*$file*" -or $file -like "*$path*") {
                $hasCoverage = $true
                break
            }
        }
        if (-not $hasCoverage) {
            return $false
        }
    }

    return $true
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$locksPath = Join-Path $orchestratorRoot "locks/locks.json"
$taskStateDbScriptPath = Join-Path $PSScriptRoot "task_state_db.py"

if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    throw "task-dag.json not found: $taskDagPath"
}

$InProgressStaleMinutes = [Math]::Max($InProgressStaleMinutes, 1)
$BlockedRuntimeStaleMinutes = [Math]::Max($BlockedRuntimeStaleMinutes, 1)

$changes = New-Object System.Collections.Generic.List[object]

$cleanupResult = Invoke-V2WithDagMutex -DagPath $taskDagPath -ScriptBlock {
    $doc = Get-V2JsonContent -Path $taskDagPath
    if (-not $doc -or -not ($doc.PSObject.Properties.Name -contains "tasks")) {
        throw "Invalid task-dag.json format (missing tasks array)."
    }

    $localChanges = New-Object System.Collections.Generic.List[object]
    $now = Get-V2Timestamp
    $taskList = @($doc.tasks)

    foreach ($task in $taskList) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if (-not $taskId.StartsWith("REPAIR-", [System.StringComparison]::OrdinalIgnoreCase)) { continue }

        $status = Get-V2CleanupTaskStatus -Task $task
        if ($status -in @("done", "completed", "skipped", "cancelled")) { continue }

        $taskAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
        $taskFiles = @(Get-V2CleanupTaskArrayProperty -Task $task -Name "files_affected")
        $taskIdleMinutes = Get-V2CleanupTaskIdleMinutes -Task $task
        $blockedReason = [string](Get-V2OptionalProperty -InputObject $task -Name "blocked_reason" -DefaultValue "")
        $blockedReasonNormalized = $blockedReason.ToLowerInvariant()
        $shouldRequeue = $false
        $requeueReason = ""

        if ($status -eq "in-progress") {
            $lockCoverage = Test-V2CleanupTaskLockCoverage -LocksPath $locksPath -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles
            if (-not $lockCoverage) {
                $shouldRequeue = $true
                $requeueReason = "repair-orphan-missing-active-lock"
            }
            elseif ($taskIdleMinutes -ge [double]$InProgressStaleMinutes) {
                $shouldRequeue = $true
                $requeueReason = "repair-orphan-idle-timeout"
            }
        }
        elseif ($status -eq "blocked-runtime") {
            $isTransient = (
                $blockedReasonNormalized.Contains("missing-task-command-or-handler") -or
                $blockedReasonNormalized.Contains("property 'count' cannot be found") -or
                $blockedReasonNormalized.Contains("cannot bind argument to parameter 'path' because it is null") -or
                $blockedReasonNormalized.Contains("timeout")
            )
            if ($isTransient -and $taskIdleMinutes -ge [double]$BlockedRuntimeStaleMinutes) {
                $shouldRequeue = $true
                $requeueReason = "repair-runtime-transient-requeue"
            }
        }
        elseif ($status -eq "blocked-lock-conflict") {
            if ([string]::IsNullOrWhiteSpace($taskAgent) -and $taskIdleMinutes -ge [double]$InProgressStaleMinutes) {
                $shouldRequeue = $true
                $requeueReason = "repair-lock-conflict-timeout-requeue"
            }
        }

        if (-not $shouldRequeue) { continue }

        if (-not [string]::IsNullOrWhiteSpace($taskAgent)) {
            [void](Remove-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles -Reason "repair-orphan-cleanup")
        }

        Set-V2CleanupObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2CleanupObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2CleanupObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
        Set-V2CleanupObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
        Set-V2CleanupObjectProperty -InputObject $task -Name "last_takeover_reason" -Value $requeueReason
        Set-V2CleanupObjectProperty -InputObject $task -Name "runtime_orphan_cleanup_applied" -Value $true
        Set-V2CleanupObjectProperty -InputObject $task -Name "runtime_orphan_cleanup_at" -Value $now
        Set-V2CleanupObjectProperty -InputObject $task -Name "updated_at" -Value $now

        $localChanges.Add([PSCustomObject]@{
            task_id = $taskId
            from_status = $status
            to_status = "pending"
            reason = $requeueReason
            idle_minutes = $taskIdleMinutes
        })
    }

    if ($localChanges.Count -gt 0) {
        Set-V2CleanupObjectProperty -InputObject $doc -Name "updated_at" -Value $now
        Save-V2JsonContent -Path $taskDagPath -Value $doc
    }

    return [PSCustomObject]@{
        tasks = @($doc.tasks)
        changes = @($localChanges.ToArray())
    }
}

$cleanupChanges = @()
if ($cleanupResult -and ($cleanupResult.PSObject.Properties.Name -contains "changes")) {
    $cleanupChanges = @((Get-V2OptionalProperty -InputObject $cleanupResult -Name "changes" -DefaultValue @()))
}
elseif ($cleanupResult -is [System.Array]) {
    foreach ($entry in @($cleanupResult)) {
        if ($entry -and ($entry.PSObject.Properties.Name -contains "changes")) {
            $cleanupChanges += @((Get-V2OptionalProperty -InputObject $entry -Name "changes" -DefaultValue @()))
        }
    }
}
foreach ($change in @($cleanupChanges)) {
    $changes.Add($change)
}

$taskStateDbSync = $null
if ($UseTaskStateDb -and (Test-Path -LiteralPath $taskStateDbScriptPath -PathType Leaf)) {
    try {
        $syncRaw = @(python $taskStateDbScriptPath --project-path $resolvedProjectPath --mode sync --emit-json 2>&1)
        if ($LASTEXITCODE -eq 0) {
            $syncJson = (($syncRaw -join [Environment]::NewLine).Trim())
            if (-not [string]::IsNullOrWhiteSpace($syncJson)) {
                $taskStateDbSync = ($syncJson | ConvertFrom-Json)
            }
        }
    }
    catch { }
}

$result = [PSCustomObject]@{
    ok = $true
    project_path = $resolvedProjectPath
    changes_count = $changes.Count
    changes = @($changes.ToArray())
    task_state_db_sync = $taskStateDbSync
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 10)
}
else {
    Write-Host ("[RepairOrphanCleanup] changes={0}" -f $changes.Count) -ForegroundColor Cyan
}
