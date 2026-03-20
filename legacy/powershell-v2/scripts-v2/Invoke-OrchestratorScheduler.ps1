# SINC Orchestrator Automated Scheduler (Orchestrator Loop)
# Version: 1.0.0
#
# Runs as a continuous loop, invoking Python scripts at configured intervals.
# Each script failure is isolated — one failure never stops the loop.
#
# Usage:
#   .\Invoke-OrchestratorScheduler.ps1
#   .\Invoke-OrchestratorScheduler.ps1 -IntervalSeconds 30 -LogPath "C:\custom\scheduler.log"

param (
    [int]$IntervalSeconds = 60,
    [string]$LogPath = ""
)

# ─────────────────────────────────────────────────────────────────────────────
# PATH RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$OrchestratorRoot = (Resolve-Path (Join-Path $ScriptDir "../../")).Path.TrimEnd('\').TrimEnd('/')

$V2Dir        = Join-Path $OrchestratorRoot "scripts\v2"
$SchedulerDir = Join-Path $OrchestratorRoot "runtime\agent_scheduler"
$LogsDir      = Join-Path $OrchestratorRoot "logs"

if (-not $LogPath) {
    $LogPath = Join-Path $LogsDir "scheduler.log"
}

# ─────────────────────────────────────────────────────────────────────────────
# PYTHON EXECUTABLE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

$PythonExe = $null
foreach ($candidate in @("python", "python3")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $candidate
            break
        }
    } catch { }
}

if (-not $PythonExe) {
    Write-Error "Neither 'python' nor 'python3' found in PATH. Aborting."
    exit 1
}

# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIAL INJECTION
# ─────────────────────────────────────────────────────────────────────────────

# DB password read from environment — never hard-coded
$env:ORCH_DB_PASSWORD = if ($env:ORCH_DB_PASSWORD) { $env:ORCH_DB_PASSWORD } else { "" }

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULE DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────
# Each entry:
#   Name          - display label
#   Script        - full path to .py file
#   Args          - argument string
#   IntervalSecs  - how often to run (seconds)
#   DailyAt       - if set, run once daily at this HH:MM time (overrides IntervalSecs)

$Scripts = @(
    [PSCustomObject]@{
        Name         = "scheduler"
        Script       = Join-Path $SchedulerDir "scheduler.py"
        Args         = "--max-assign 5"
        IntervalSecs = 300          # every 5 minutes
        DailyAt      = $null
    },
    [PSCustomObject]@{
        Name         = "heartbeat_monitor"
        Script       = Join-Path $V2Dir "heartbeat_monitor.py"
        Args         = ""
        IntervalSecs = 60           # every 60 seconds
        DailyAt      = $null
    },
    [PSCustomObject]@{
        Name         = "whiteboard_enforcer"
        Script       = Join-Path $V2Dir "whiteboard_enforcer.py"
        Args         = "--mode enforce"
        IntervalSecs = 600          # every 10 minutes
        DailyAt      = $null
    },
    [PSCustomObject]@{
        Name         = "completion_validator"
        Script       = Join-Path $V2Dir "completion_validator.py"
        Args         = "--scan --fix"
        IntervalSecs = 300          # every 5 minutes
        DailyAt      = $null
    },
    [PSCustomObject]@{
        Name         = "state_rotator"
        Script       = Join-Path $V2Dir "state_rotator.py"
        Args         = ""
        IntervalSecs = 86400        # daily (guarded by DailyAt below)
        DailyAt      = "03:00"      # run once at 03:00
    },
    [PSCustomObject]@{
        Name         = "preflight_enricher"
        Script       = Join-Path $V2Dir "preflight_enricher.py"
        Args         = "--all"
        IntervalSecs = 900          # every 15 minutes
        DailyAt      = $null
    }
)

# ─────────────────────────────────────────────────────────────────────────────
# STATE TRACKING  (last-run timestamps + daily-run tracking)
# ─────────────────────────────────────────────────────────────────────────────

$LastRun     = @{}   # Name -> [DateTime] of last successful invocation start
$DailyRanOn  = @{}   # Name -> date string "yyyy-MM-dd" when last ran (for DailyAt jobs)

foreach ($s in $Scripts) {
    $LastRun[$s.Name]    = [DateTime]::MinValue
    $DailyRanOn[$s.Name] = ""
}

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING HELPER
# ─────────────────────────────────────────────────────────────────────────────

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] [$Level] $Message"
    Write-Host $line -ForegroundColor $(switch ($Level) {
        "ERROR"   { "Red"       }
        "WARN"    { "Yellow"    }
        "SUCCESS" { "Green"     }
        "TICK"    { "Cyan"      }
        default   { "White"     }
    })
    try {
        $logDir = Split-Path -Parent $LogPath
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    } catch {
        Write-Host "[WARN] Could not write to log file: $_" -ForegroundColor Yellow
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# SCRIPT INVOCATION
# ─────────────────────────────────────────────────────────────────────────────

function Invoke-Script {
    param([PSCustomObject]$ScriptDef)

    $name   = $ScriptDef.Name
    $script = $ScriptDef.Script
    $args   = $ScriptDef.Args

    if (-not (Test-Path $script)) {
        Write-Log "Script not found: $script — skipping $name" "WARN"
        return
    }

    Write-Log "Running: $name  ($PythonExe $script $args)"

    try {
        $argList = @($script)
        if ($args -and $args.Trim() -ne "") {
            # Split respecting quoted tokens
            $argList += ($args -split '\s+')
        }

        $proc = Start-Process -FilePath $PythonExe `
                              -ArgumentList $argList `
                              -NoNewWindow `
                              -Wait `
                              -PassThru `
                              -RedirectStandardOutput "$env:TEMP\orch_sched_stdout_$name.txt" `
                              -RedirectStandardError  "$env:TEMP\orch_sched_stderr_$name.txt"

        $exitCode = $proc.ExitCode

        # Capture and surface output
        $stdout = if (Test-Path "$env:TEMP\orch_sched_stdout_$name.txt") {
            Get-Content "$env:TEMP\orch_sched_stdout_$name.txt" -Raw -ErrorAction SilentlyContinue
        } else { "" }
        $stderr = if (Test-Path "$env:TEMP\orch_sched_stderr_$name.txt") {
            Get-Content "$env:TEMP\orch_sched_stderr_$name.txt" -Raw -ErrorAction SilentlyContinue
        } else { "" }

        if ($stdout -and $stdout.Trim()) {
            foreach ($line in ($stdout -split "`n")) {
                if ($line.Trim()) { Write-Log "  [stdout] $($line.TrimEnd())" "INFO" }
            }
        }
        if ($stderr -and $stderr.Trim()) {
            foreach ($line in ($stderr -split "`n")) {
                if ($line.Trim()) { Write-Log "  [stderr] $($line.TrimEnd())" "WARN" }
            }
        }

        if ($exitCode -eq 0) {
            Write-Log "Completed: $name  (exit=0)" "SUCCESS"
        } else {
            Write-Log "Non-zero exit: $name  (exit=$exitCode)" "WARN"
        }

        # Update last-run time regardless of exit code so we respect intervals
        $LastRun[$name] = Get-Date

    } catch {
        Write-Log "EXCEPTION running $name : $_" "ERROR"
        # Still update last-run to avoid tight failure loop
        $LastRun[$name] = Get-Date
    } finally {
        Remove-Item "$env:TEMP\orch_sched_stdout_$name.txt" -ErrorAction SilentlyContinue
        Remove-Item "$env:TEMP\orch_sched_stderr_$name.txt" -ErrorAction SilentlyContinue
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# NEXT-RUN CALCULATION HELPER
# ─────────────────────────────────────────────────────────────────────────────

function Get-NextRunTime {
    param([PSCustomObject]$ScriptDef)
    $name = $ScriptDef.Name

    if ($ScriptDef.DailyAt) {
        # Next daily window
        $now      = Get-Date
        $todayStr = $now.ToString("yyyy-MM-dd")
        $runTime  = [DateTime]::ParseExact("$todayStr $($ScriptDef.DailyAt)", "yyyy-MM-dd HH:mm", $null)
        if ($DailyRanOn[$name] -eq $todayStr) {
            # Already ran today — next run is tomorrow
            return $runTime.AddDays(1)
        }
        if ($now -ge $runTime) {
            return $runTime   # overdue / window passed but not run yet
        }
        return $runTime
    }

    $last = $LastRun[$name]
    if ($last -eq [DateTime]::MinValue) { return [DateTime]::Now }
    return $last.AddSeconds($ScriptDef.IntervalSecs)
}

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULE EVALUATION — returns true if script should run now
# ─────────────────────────────────────────────────────────────────────────────

function Should-Run {
    param([PSCustomObject]$ScriptDef)
    $name = $ScriptDef.Name
    $now  = Get-Date

    if ($ScriptDef.DailyAt) {
        $todayStr = $now.ToString("yyyy-MM-dd")
        if ($DailyRanOn[$name] -eq $todayStr) { return $false }   # already ran today
        $runTime = [DateTime]::ParseExact("$todayStr $($ScriptDef.DailyAt)", "yyyy-MM-dd HH:mm", $null)
        return ($now -ge $runTime)
    }

    $last = $LastRun[$name]
    if ($last -eq [DateTime]::MinValue) { return $true }          # never run — run immediately
    return (($now - $last).TotalSeconds -ge $ScriptDef.IntervalSecs)
}

# ─────────────────────────────────────────────────────────────────────────────
# STATUS SUMMARY PRINTER
# ─────────────────────────────────────────────────────────────────────────────

function Print-StatusSummary {
    $now = Get-Date
    Write-Host ""
    Write-Host "──────────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "  Orchestrator Scheduler  |  Tick @ $($now.ToString('yyyy-MM-dd HH:mm:ss'))" -ForegroundColor Cyan
    Write-Host "──────────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host ("  {0,-28} {1,-22} {2}" -f "SCRIPT", "LAST RUN", "NEXT RUN") -ForegroundColor DarkGray

    foreach ($s in $Scripts) {
        $last = $LastRun[$s.Name]
        $next = Get-NextRunTime -ScriptDef $s

        $lastStr = if ($last -eq [DateTime]::MinValue) { "never" } else { $last.ToString("HH:mm:ss") }
        $nextStr = $next.ToString("HH:mm:ss")

        $dueIn   = ($next - $now).TotalSeconds
        $color   = if ($dueIn -le 0) { "Yellow" } else { "White" }
        $dueMark = if ($dueIn -le 0) { " [DUE]" } else { "" }

        Write-Host ("  {0,-28} {1,-22} {2}{3}" -f $s.Name, $lastStr, $nextStr, $dueMark) -ForegroundColor $color
    }
    Write-Host "──────────────────────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host ""
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

Write-Log "SINC Orchestrator Scheduler starting  (tick=${IntervalSeconds}s, python=$PythonExe)" "INFO"
Write-Log "Log file : $LogPath" "INFO"
Write-Log "Root     : $OrchestratorRoot" "INFO"
Write-Log "DB password env : $(if ($env:ORCH_DB_PASSWORD) { 'set (*****)' } else { 'NOT SET — check ORCH_DB_PASSWORD' })" "INFO"
Write-Log "Press Ctrl+C to stop." "INFO"

$TickCount = 0

while ($true) {
    $TickCount++
    Print-StatusSummary

    foreach ($s in $Scripts) {
        if (Should-Run -ScriptDef $s) {
            Write-Log "─── Triggering: $($s.Name) ───" "TICK"
            Invoke-Script -ScriptDef $s

            # Mark daily-at jobs
            if ($s.DailyAt) {
                $DailyRanOn[$s.Name] = (Get-Date).ToString("yyyy-MM-dd")
            }
        }
    }

    Write-Log "Tick #$TickCount complete. Sleeping ${IntervalSeconds}s..." "INFO"
    Start-Sleep -Seconds $IntervalSeconds
}
