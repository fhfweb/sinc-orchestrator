# SINC Orchestrator Auto-Repair Watchdog (PostgreSQL Edition)
# Version: 2.1.0

param (
    [int]$WarningThresholdMinutes = 15,
    [int]$StaleThresholdMinutes = 30
)

$PythonBridge    = "G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\scripts\v2\orchestrator_db_bridge.py"
$SchedulerScript = "G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\runtime\agent_scheduler\scheduler.py"
$LogPath         = "G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\logs\auto_repair_log.md"
$HealthReportPath= "G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\state\health-report.json"

# Exponential backoff config
$BackoffBaseSeconds = 30
$BackoffMaxSeconds  = 3600
$BackoffMultiplier  = 2.0

function Get-BackoffSeconds {
    param([int]$ConflictCount)
    $delay = [Math]::Min($BackoffBaseSeconds * [Math]::Pow($BackoffMultiplier, $ConflictCount), $BackoffMaxSeconds)
    return [int]$delay
}

Write-Host "Starting Orchestrator Watchdog v2 (PostgreSQL + Backoff)..." -ForegroundColor Cyan

$Now = Get-Date
$Dag = python $PythonBridge list | ConvertFrom-Json
$RepairsMade = 0
$WarningsIssued = 0
$LogEntries = @()

if ($Dag.tasks) {
    foreach ($Task in $Dag.tasks) {
        if ($Task.status -eq "in-progress" -and $Task.updated_at) {
            $UpdatedAt = [DateTime]$Task.updated_at
            $IdleTime  = $Now - $UpdatedAt
            $TotalIdle = $IdleTime.TotalMinutes

            # Use task-specific TTL or global default
            $TaskWarning = $WarningThresholdMinutes
            $TaskTimeout = $StaleThresholdMinutes
            if ($Task.lock_ttl) {
                $TaskTimeout = [int]$Task.lock_ttl
                $TaskWarning = [Math]::Floor($TaskTimeout * 0.75)
            }

            # Level 2: Critical Timeout — apply exponential backoff before resetting
            if ($TotalIdle -gt $TaskTimeout) {
                $ConflictCount = if ($Task.lock_conflict_count) { [int]$Task.lock_conflict_count } else { 0 }
                $BackoffSec    = Get-BackoffSeconds -ConflictCount $ConflictCount
                $BackoffUntil  = (Get-Date).AddSeconds($BackoffSec).ToString("o")

                Write-Host "CRITICAL: Stale task $($Task.id) (Idle=$TotalIdle min, conflicts=$ConflictCount, backoff=${BackoffSec}s)" -ForegroundColor Red

                $LogEntries += "- **Repaired**: $($Task.id)`n  - Idle: $TotalIdle min | Conflicts: $ConflictCount | Backoff: ${BackoffSec}s`n  - Agent: $($Task.assigned_agent)"

                # Reset task with backoff via scheduler lock-conflict handler
                if (Test-Path $SchedulerScript) {
                    python $SchedulerScript --lock-conflict $Task.id 2>$null
                } else {
                    python $PythonBridge update --task-id $Task.id --status "pending" --agent ""
                }
                $RepairsMade++
            }
            # Level 1: Warning
            elseif ($TotalIdle -gt $TaskWarning) {
                Write-Host "WARNING: Task $($Task.id) becoming stale ($TotalIdle min, warning at $TaskWarning)" -ForegroundColor Yellow
                $WarningsIssued++
            }
        }

        # Skip tasks still in backoff window (prevent re-assignment loops)
        if ($Task.status -eq "pending" -and $Task.lock_backoff_until) {
            $BackoffUntil = [DateTime]$Task.lock_backoff_until
            if ($Now -lt $BackoffUntil) {
                $SecsRemaining = [int]($BackoffUntil - $Now).TotalSeconds
                Write-Host "BACKOFF: $($Task.id) — ${SecsRemaining}s remaining (conflicts=$($Task.lock_conflict_count))" -ForegroundColor DarkYellow
            }
        }
    }
}

if ($RepairsMade -gt 0 -or $WarningsIssued -gt 0) {
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $Header    = "### Watchdog Run: $Timestamp`n"
    $Summary   = "- Repairs: $RepairsMade`n- Warnings: $WarningsIssued`n"
    $FullLog   = $Header + $Summary + ($LogEntries -join "`n") + "`n`n"
    $FullLog + (Get-Content $LogPath -ErrorAction SilentlyContinue) | Set-Content $LogPath

    # Update health report with watchdog summary
    if (Test-Path $HealthReportPath) {
        try {
            $HealthReport = Get-Content $HealthReportPath -Raw | ConvertFrom-Json
            $HealthReport | Add-Member -Force -NotePropertyName "last_watchdog_run" -NotePropertyValue $Timestamp
            $HealthReport | Add-Member -Force -NotePropertyName "last_watchdog_repairs" -NotePropertyValue $RepairsMade
            $HealthReport | Add-Member -Force -NotePropertyName "last_watchdog_warnings" -NotePropertyValue $WarningsIssued
            $HealthReport | ConvertTo-Json -Depth 10 | Set-Content $HealthReportPath
        } catch {
            Write-Host "WARNING: Could not update health report: $_" -ForegroundColor Yellow
        }
    }
}

Write-Host "Orchestrator health is good. Repairs: $RepairsMade, Warnings: $WarningsIssued" -ForegroundColor Green
