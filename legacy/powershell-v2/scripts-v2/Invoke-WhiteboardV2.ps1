<#
.SYNOPSIS
    Operational agent whiteboard for intent tracking, change announcements, and handoffs.
.DESCRIPTION
    Manages ai-orchestrator/state/whiteboard.json.

    The whiteboard complements locks.json:
      locks.json    = EXCLUSIVE ACCESS   (prevents simultaneous writes to the same file)
      whiteboard    = DECLARED INTENT    (lets agents see what others plan to change before locks are acquired)

    Modes:
      announce    Agent announces intent to change specific files before execution.
                  Creates a whiteboard entry visible to all agents.
      complete    Agent marks its whiteboard entry done and optionally names a handoff target.
                  Appends a handoff event to ai-orchestrator/state/handoff-log.jsonl.
      status      Prints current whiteboard (active announcements).
      clear-stale Removes entries older than TtlMinutes (default 120). Called by the loop each cycle.

.PARAMETER Mode
    announce | complete | status | clear-stale

.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.

.PARAMETER TaskId
    Task identifier (required for announce / complete).

.PARAMETER AgentName
    Agent name (required for announce).

.PARAMETER FilesIntended
    Comma-separated file paths the agent intends to modify (announce).

.PARAMETER Intention
    One-line description of what the agent plans to do (announce).

.PARAMETER HandoffTo
    Agent name to hand off to after completion (complete, optional).

.PARAMETER TtlMinutes
    Entry lifetime in minutes before clear-stale removes it. Default: 120.

.PARAMETER EmitJson
    Emit JSON result to stdout.

.EXAMPLE
    # Agent announces intent before acquiring locks
    .\Invoke-WhiteboardV2.ps1 -Mode announce -ProjectPath . -TaskId DEV-001 -AgentName Codex `
        -FilesIntended "app/auth.py,tests/test_auth.py" -Intention "Fix JWT expiry bug"

    # Agent completes and hands off
    .\Invoke-WhiteboardV2.ps1 -Mode complete -ProjectPath . -TaskId DEV-001 -AgentName Codex `
        -HandoffTo "AI QA"

    # Loop maintenance
    .\Invoke-WhiteboardV2.ps1 -Mode clear-stale -ProjectPath .
#>
param(
    [ValidateSet("announce", "complete", "status", "clear-stale")]
    [string]$Mode       = "status",
    [string]$ProjectPath = ".",
    [string]$TaskId      = "",
    [string]$AgentName   = "",
    [string]$FilesIntended = "",
    [string]$Intention   = "",
    [string]$HandoffTo   = "",
    [int]$TtlMinutes     = 120,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$stateDir       = Join-Path $resolvedPath "ai-orchestrator/state"
$whiteboardPath = Join-Path $stateDir "whiteboard.json"
$handoffLogPath = Join-Path $stateDir "handoff-log.jsonl"

Initialize-V2Directory -Path $stateDir

# ── Helpers ──────────────────────────────────────────────────────────────────
function Read-Whiteboard {
    if (-not (Test-Path -LiteralPath $whiteboardPath -PathType Leaf)) {
        return [PSCustomObject]@{ entries = @(); updated_at = "" }
    }
    $wb = Get-V2JsonContent -Path $whiteboardPath
    if ($null -eq $wb) {
        return [PSCustomObject]@{ entries = @(); updated_at = "" }
    }
    return $wb
}

function Save-Whiteboard {
    param([object]$Whiteboard)
    Save-V2JsonContent -Path $whiteboardPath -Value $Whiteboard
}

function Get-ActiveEntries {
    param([object]$Whiteboard)
    $entries = @(Get-V2OptionalProperty -InputObject $Whiteboard -Name "entries" -DefaultValue @())
    return @($entries | Where-Object {
        [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "announced"
    })
}

function Remove-StaleEntries {
    param([object[]]$Entries, [int]$TtlMins)
    $cutoff = [DateTime]::UtcNow.AddMinutes(-$TtlMins)
    return @($Entries | Where-Object {
        $a = [string](Get-V2OptionalProperty -InputObject $_ -Name "announced_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($a)) { return $false }
        try {
            return [DateTime]::Parse($a).ToUniversalTime() -ge $cutoff
        }
        catch { return $false }
    })
}

# ── Mode dispatch ─────────────────────────────────────────────────────────────
switch ($Mode) {

    "announce" {
        if ([string]::IsNullOrWhiteSpace($TaskId)) {
            throw "TaskId is required for announce mode."
        }
        if ([string]::IsNullOrWhiteSpace($AgentName)) {
            throw "AgentName is required for announce mode."
        }

        $wb      = Read-Whiteboard
        $entries = [System.Collections.Generic.List[object]]::new()

        # Replace any existing entry for the same task (idempotent announce)
        foreach ($e in @(Get-V2OptionalProperty -InputObject $wb -Name "entries" -DefaultValue @())) {
            $eId = [string](Get-V2OptionalProperty -InputObject $e -Name "task_id" -DefaultValue "")
            if ($eId -eq $TaskId) { continue }
            $entries.Add($e)
        }

        $filesList = @($FilesIntended -split "," |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) })

        $newEntry = [PSCustomObject]@{
            task_id        = $TaskId
            agent          = $AgentName
            intention      = $Intention
            files_intended = $filesList
            status         = "announced"
            announced_at   = Get-V2Timestamp
            completed_at   = ""
            handoff_to     = ""
        }
        $entries.Add($newEntry)

        Set-V2DynamicProperty -InputObject $wb -Name "entries"    -Value @($entries.ToArray())
        Set-V2DynamicProperty -InputObject $wb -Name "updated_at" -Value (Get-V2Timestamp)
        Save-Whiteboard -Whiteboard $wb

        Write-Host ("[Whiteboard] ANNOUNCED  {0} | {1} | files: {2}" -f $AgentName, $TaskId, ($filesList -join ", "))

        if ($EmitJson) {
            Write-Output ($newEntry | ConvertTo-Json -Depth 5)
        }
    }

    "complete" {
        if ([string]::IsNullOrWhiteSpace($TaskId)) {
            throw "TaskId is required for complete mode."
        }

        $wb      = Read-Whiteboard
        $entries = @(Get-V2OptionalProperty -InputObject $wb -Name "entries" -DefaultValue @())
        $found   = $false

        foreach ($e in $entries) {
            if ([string](Get-V2OptionalProperty -InputObject $e -Name "task_id" -DefaultValue "") -ne $TaskId) {
                continue
            }

            $completedAt = Get-V2Timestamp
            Set-V2DynamicProperty -InputObject $e -Name "status"       -Value "completed"
            Set-V2DynamicProperty -InputObject $e -Name "completed_at" -Value $completedAt

            if (-not [string]::IsNullOrWhiteSpace($HandoffTo)) {
                Set-V2DynamicProperty -InputObject $e -Name "handoff_to" -Value $HandoffTo

                # Append handoff event to append-only log
                $handoffEvent = [PSCustomObject]@{
                    timestamp   = $completedAt
                    task_id     = $TaskId
                    from_agent  = [string](Get-V2OptionalProperty -InputObject $e -Name "agent" -DefaultValue "")
                    to_agent    = $HandoffTo
                    intention   = [string](Get-V2OptionalProperty -InputObject $e -Name "intention" -DefaultValue "")
                    files       = @(Get-V2OptionalProperty -InputObject $e -Name "files_intended" -DefaultValue @())
                }
                Add-Content -LiteralPath $handoffLogPath -Value ($handoffEvent | ConvertTo-Json -Depth 5 -Compress)
                Write-Host ("[Whiteboard] HANDOFF    {0} -> {1} | {2}" -f $handoffEvent.from_agent, $HandoffTo, $TaskId)
            }
            else {
                Write-Host ("[Whiteboard] COMPLETED  {0} | {1}" -f ([string](Get-V2OptionalProperty -InputObject $e -Name "agent" -DefaultValue "?")), $TaskId)
            }

            $found = $true
            break
        }

        if (-not $found) {
            Write-Host ("[Whiteboard] COMPLETE: task $TaskId not found on whiteboard (nothing to update).")
        }

        Set-V2DynamicProperty -InputObject $wb -Name "entries"    -Value $entries
        Set-V2DynamicProperty -InputObject $wb -Name "updated_at" -Value (Get-V2Timestamp)
        Save-Whiteboard -Whiteboard $wb

        if ($EmitJson) {
            Write-Output ([PSCustomObject]@{ task_id = $TaskId; found = $found } | ConvertTo-Json -Depth 3)
        }
    }

    "clear-stale" {
        $wb      = Read-Whiteboard
        $entries = @(Get-V2OptionalProperty -InputObject $wb -Name "entries" -DefaultValue @())
        $kept    = @(Remove-StaleEntries -Entries $entries -TtlMins $TtlMinutes)
        $removed = $entries.Count - $kept.Count

        Set-V2DynamicProperty -InputObject $wb -Name "entries"    -Value $kept
        Set-V2DynamicProperty -InputObject $wb -Name "updated_at" -Value (Get-V2Timestamp)
        Save-Whiteboard -Whiteboard $wb

        Write-Host ("[Whiteboard] CLEAR-STALE: kept=$($kept.Count) removed=$removed (TTL=${TtlMinutes}m)")

        if ($EmitJson) {
            Write-Output ([PSCustomObject]@{ kept = $kept.Count; removed = $removed } | ConvertTo-Json -Depth 3)
        }
    }

    "status" {
        $wb      = Read-Whiteboard
        $active  = @(Get-ActiveEntries -Whiteboard $wb)
        $entries = @(Get-V2OptionalProperty -InputObject $wb -Name "entries" -DefaultValue @())
        Write-Host ("[Whiteboard] STATUS: {0} active announcements / {1} total entries" -f $active.Count, $entries.Count)

        if ($EmitJson) {
            Write-Output ($wb | ConvertTo-Json -Depth 8)
        }
        else {
            foreach ($e in $active) {
                $agent     = [string](Get-V2OptionalProperty -InputObject $e -Name "agent"       -DefaultValue "?")
                $task      = [string](Get-V2OptionalProperty -InputObject $e -Name "task_id"     -DefaultValue "?")
                $intent    = [string](Get-V2OptionalProperty -InputObject $e -Name "intention"   -DefaultValue "")
                $announced = [string](Get-V2OptionalProperty -InputObject $e -Name "announced_at" -DefaultValue "")
                Write-Host ("  [{0}] {1} -> {2}: {3}" -f $announced, $agent, $task, $intent)
            }
        }
    }
}
