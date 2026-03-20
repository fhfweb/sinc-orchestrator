<#
.SYNOPSIS
    Runs the project's test suite and records results as a health check.
.DESCRIPTION
    Reads the verified test command from project-state.json (set during intake),
    executes it, captures stdout/stderr, and writes a structured test report to
    ai-orchestrator/reports/test-run-<timestamp>.json.

    If tests fail:
      - Sets a REPAIR-TEST-FAIL-<timestamp> task in task-dag.json
      - Creates an incident report

    If tests pass:
      - Marks any open REPAIR-TEST-FAIL-* tasks as done
      - Writes a test-passed event to memory

    Safe to run repeatedly — idempotent when tests are already passing.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/ and source code.
.PARAMETER TimeoutSeconds
    Maximum seconds to wait for the test command. Default 300.
.EXAMPLE
    .\scripts\v2\Invoke-TestRunnerV2.ps1 -ProjectPath C:\projects\myapp
#>
param(
    [string]$ProjectPath    = ".",
    [int]$TimeoutSeconds    = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2SanitizedSnippet {
    param(
        [string]$Text,
        [int]$MaxLength = 2000
    )

    if ($null -eq $Text) {
        return ""
    }

    $safe = [string]$Text
    $safe = $safe -replace "[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", " "
    $safe = $safe -replace "`r`n?", "`n"
    $safe = $safe -replace "`n{3,}", "`n`n"
    if ($safe.Length -gt $MaxLength) {
        $safe = $safe.Substring(0, $MaxLength) + "…"
    }
    return $safe.Trim()
}

function Get-V2Sha256Text {
    param([string]$InputText)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$InputText)
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-V2EnvInt {
    param(
        [string]$Name,
        [int]$DefaultValue
    )

    $raw = [string][System.Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }

    $parsed = 0
    if ([int]::TryParse($raw.Trim(), [ref]$parsed)) {
        return $parsed
    }

    return $DefaultValue
}

function ConvertTo-V2UtcDateTime {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    try {
        return ([DateTimeOffset]::Parse($Value)).UtcDateTime
    }
    catch {
        return $null
    }
}

function Invoke-V2TaskStateDbCommandLite {
    param(
        [string]$TaskStateDbScriptPath,
        [string]$ProjectRoot,
        [string]$Mode,
        [hashtable]$ExtraArgs = @{}
    )

    $args = @($TaskStateDbScriptPath, "--project-path", $ProjectRoot, "--mode", $Mode, "--emit-json")
    foreach ($kv in $ExtraArgs.GetEnumerator()) {
        if ([string]::IsNullOrWhiteSpace([string]$kv.Key)) { continue }
        $args += @("--$($kv.Key)", [string]$kv.Value)
    }

    $raw = @(python @args 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $tail = ($raw | Select-Object -Last 30) -join [Environment]::NewLine
        throw "task-state-db-$Mode-failed: exit=$LASTEXITCODE output=$tail"
    }

    $payload = (($raw -join [Environment]::NewLine).Trim())
    if ([string]::IsNullOrWhiteSpace($payload)) {
        return $null
    }

    try {
        return ($payload | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "task-state-db-$Mode-non-json-output: $payload"
    }
}

function Save-V2TestRunnerTaskDag {
    param(
        [string]$DagPath,
        [object]$DagDocument,
        [string]$TaskStateDbScriptPath,
        [string]$ProjectRoot,
        [string]$OrchestratorRoot,
        [bool]$UseTaskStateDbPrimary
    )

    if (-not $DagDocument) {
        return [PSCustomObject]@{ used_db = $false; reason = "empty-dag-document" }
    }

    $tasks = @()
    if ($DagDocument.PSObject.Properties.Name -contains "tasks") {
        $tasks = @($DagDocument.tasks)
    }

    if ($UseTaskStateDbPrimary -and (Test-Path -LiteralPath $TaskStateDbScriptPath -PathType Leaf) -and @($tasks).Count -gt 0) {
        $bufferPath = Join-Path $OrchestratorRoot ("state/test-runner-db-write-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
        try {
            Save-V2JsonContent -Path $bufferPath -Value @($tasks)
            [void](Invoke-V2TaskStateDbCommandLite -TaskStateDbScriptPath $TaskStateDbScriptPath -ProjectRoot $ProjectRoot -Mode "write-tasks" -ExtraArgs @{ "tasks-json-path" = $bufferPath })
            [void](Invoke-V2TaskStateDbCommandLite -TaskStateDbScriptPath $TaskStateDbScriptPath -ProjectRoot $ProjectRoot -Mode "flush-dag")
            return [PSCustomObject]@{ used_db = $true; reason = "db-primary-write-tasks+flush-dag" }
        }
        catch {
            Write-Warning ("[TestRunner] task_state_db persistence failed, fallback to DAG write: {0}" -f $_.Exception.Message)
        }
        finally {
            Remove-Item -LiteralPath $bufferPath -Force -ErrorAction SilentlyContinue
        }
    }

    Save-V2JsonContent -Path $DagPath -Value $DagDocument
    return [PSCustomObject]@{ used_db = $false; reason = "dag-write-fallback" }
}

$resolvedPath     = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$statePath        = Join-Path $orchestratorRoot "state/project-state.json"
$dagPath          = Join-Path $orchestratorRoot "tasks/task-dag.json"
$taskStateDbScriptPath = Join-Path $PSScriptRoot "task_state_db.py"
$reportsDir       = Join-Path $orchestratorRoot "reports"
$ts               = Get-Date -Format "yyyyMMddHHmmss"
$reportPath       = Join-Path $reportsDir "test-run-$ts.json"

$taskStateDbPrimary = $false
if (Test-Path -LiteralPath $taskStateDbScriptPath -PathType Leaf) {
    try {
        $statusResult = Invoke-V2TaskStateDbCommandLite -TaskStateDbScriptPath $taskStateDbScriptPath -ProjectRoot $resolvedPath -Mode "status"
        $backendMode = [string](Get-V2OptionalProperty -InputObject $statusResult -Name "backend_mode" -DefaultValue "")
        $taskStateDbPrimary = ($backendMode -eq "db-primary-v1")
    }
    catch {
        $taskStateDbPrimary = $false
    }
}

Initialize-V2Directory -Path $reportsDir

# ── Read test command from project state ─────────────────────────────────────────────────────
$state       = Get-V2JsonContent -Path $statePath
$testCommand = ""
if ($state) {
    $verifiedCmds = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    $testCommand = Get-V2VerifiedCommand -VerifiedCommands $verifiedCmds -CommandName "test"
}

if ([string]::IsNullOrWhiteSpace($testCommand)) {
    # Auto-detect from stack
    $stack = if ($state) { [string](Get-V2OptionalProperty -InputObject $state -Name "stack" -DefaultValue "") } else { "" }
    $testCommand = switch ($stack) {
        "python"  { "pytest --tb=short -q" }
        "node"    { "npm test" }
        "php"     { "php artisan test" }
        "go"      { "go test ./..." }
        "dotnet"  { "dotnet test" }
        "java"    { "mvn test -q" }
        "ruby"    { "bundle exec rspec" }
        "rust"    { "cargo test" }
        default   { "" }
    }
}

if ([string]::IsNullOrWhiteSpace($testCommand)) {
    Write-Host "[TestRunner] No test command found in project-state.json and stack not recognized. Skipping."
    exit 0
}

Write-Host "[TestRunner] Running: $testCommand"

# ── Execute tests ─────────────────────────────────────────────────────────────────────────────
$exitCode  = -1
$output    = ""
$startTime = Get-Date
$tempDir = if ($env:TEMP) { $env:TEMP } elseif ($env:TMPDIR) { $env:TMPDIR } else { "/tmp" }
$stdoutFile = Join-Path $tempDir "testrunner_stdout_$ts.txt"
$stderrFile = Join-Path $tempDir "testrunner_stderr_$ts.txt"

try {
    $onWindows = [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)
    if ($onWindows) {
        $shell = "cmd.exe"
        $shellArgs = "/c `"cd /d `"$resolvedPath`" && $testCommand`""
    } else {
        $shell = "sh"
        $shellArgs = "-c `"cd '$resolvedPath' && $testCommand`""
    }

    $proc = Start-Process -FilePath $shell `
        -ArgumentList $shellArgs `
        -WorkingDirectory $resolvedPath `
        -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError  $stderrFile `
        -NoNewWindow -PassThru -Wait -ErrorAction Stop

    if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
        $proc.Kill()
        throw "Test command timed out after ${TimeoutSeconds}s"
    }
    $exitCode = $proc.ExitCode
    $stdout = if (Test-Path $stdoutFile) { Get-Content $stdoutFile -Raw } else { "" }
    $stderr = if (Test-Path $stderrFile) { Get-Content $stderrFile -Raw } else { "" }
    $output = ($stdout + "`n" + $stderr).Trim()
}
catch {
    $exitCode = -1
    $output   = $_.Exception.Message
}
finally {
    Remove-Item $stdoutFile -ErrorAction SilentlyContinue
    Remove-Item $stderrFile -ErrorAction SilentlyContinue
}

$elapsed  = [Math]::Round(((Get-Date) - $startTime).TotalSeconds, 1)
$passed   = $exitCode -eq 0
$status   = if ($passed) { "passed" } else { "failed" }

# ── Write test report ─────────────────────────────────────────────────────────────────────────
$report = [PSCustomObject]@{
    generated_at   = Get-V2Timestamp
    project        = Split-Path -Leaf $resolvedPath
    test_command   = $testCommand
    exit_code      = $exitCode
    status         = $status
    elapsed_seconds = $elapsed
    output_snippet = Get-V2SanitizedSnippet -Text $output -MaxLength 2000
}
Save-V2JsonContent -Path $reportPath -Value $report

Write-Host ("[TestRunner] {0} (exit {1}, {2}s)" -f $status.ToUpper(), $exitCode, $elapsed)
$repairCooldownMinutes = [Math]::Max((Get-V2EnvInt -Name "ORCHESTRATOR_TEST_REPAIR_COOLDOWN_MINUTES" -DefaultValue 20), 0)

# ── Update task-dag based on result ──────────────────────────────────────────────────────────
if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
    try {
        $dagToPersist = $null
        $resolvedRepairCount = 0
        $createdRepairTaskId = ""

        Invoke-V2WithDagMutex -DagPath $dagPath -ScriptBlock {
            $dag = Get-V2JsonContent -Path $dagPath
            if (-not $dag -or -not ($dag.PSObject.Properties.Name -contains "tasks")) {
                return
            }

            if ($passed) {
                # Resolve any open test-failure REPAIR tasks
                $resolved = 0
                foreach ($task in @($dag.tasks)) {
                    $tId     = [string](Get-V2OptionalProperty -InputObject $task -Name "id"     -DefaultValue "")
                    $tStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    if ($tId -like "REPAIR-TEST-FAIL-*" -and $tStatus -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")) {
                        Set-V2DynamicProperty -InputObject $task -Name "status"          -Value "done"
                        Set-V2DynamicProperty -InputObject $task -Name "completed_at"    -Value (Get-V2Timestamp)
                        Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: tests now passing"
                        $resolved++
                    }
                }
                if ($resolved -gt 0) {
                    $dagToPersist = $dag
                    $resolvedRepairCount = $resolved
                }
            }
            else {
                # Create a REPAIR task for test failures (dedupe by fingerprint + cooldown).
                $openStatuses = @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval")
                $outputSnip = Get-V2SanitizedSnippet -Text $output -MaxLength 500
                $failureFingerprint = Get-V2Sha256Text -InputText ("{0}|{1}|{2}" -f $testCommand, $exitCode, $outputSnip)
                $nowUtc = (Get-Date).ToUniversalTime()
                $openSameFingerprintCount = 0
                $recentSameFingerprintCount = 0

                foreach ($existingTask in @($dag.tasks)) {
                    $existingId = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "id" -DefaultValue "")
                    if ($existingId -notlike "REPAIR-TEST-FAIL-*") { continue }

                    $existingFingerprint = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "reason_fingerprint" -DefaultValue "")
                    if ($existingFingerprint -ne $failureFingerprint -and $existingFingerprint -ne "test-failure") { continue }

                    $existingStatus = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "status" -DefaultValue "")
                    if ($existingStatus -in $openStatuses) {
                        $openSameFingerprintCount++
                    }

                    if ($repairCooldownMinutes -gt 0) {
                        $activityRaw = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "updated_at" -DefaultValue "")
                        if ([string]::IsNullOrWhiteSpace($activityRaw)) {
                            $activityRaw = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "created_at" -DefaultValue "")
                        }
                        if ([string]::IsNullOrWhiteSpace($activityRaw)) {
                            $activityRaw = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "completed_at" -DefaultValue "")
                        }
                        $activityUtc = ConvertTo-V2UtcDateTime -Value $activityRaw
                        if ($activityUtc) {
                            $ageMinutes = ($nowUtc - $activityUtc).TotalMinutes
                            if ($ageMinutes -lt [double]$repairCooldownMinutes) {
                                $recentSameFingerprintCount++
                            }
                        }
                    }
                }

                if ($openSameFingerprintCount -eq 0 -and $recentSameFingerprintCount -eq 0) {
                    $repairId    = "REPAIR-TEST-FAIL-$ts"
                    $dag.tasks  += [PSCustomObject]@{
                        id              = $repairId
                        title           = "Fix failing tests (exit $exitCode)"
                        description     = "Test suite failed. Command: $testCommand"
                        reason          = "test-failure: exit $exitCode"
                        priority        = "P1"
                        dependencies    = @()
                        preferred_agent = "AI QA"
                        assigned_agent  = ""
                        status          = "pending"
                        execution_mode  = "external-agent"
                        reason_fingerprint = $failureFingerprint
                        failure_signature = "test-failure:$failureFingerprint"
                        repair_cooldown_minutes = $repairCooldownMinutes
                        files_affected  = @("ai-orchestrator/reports", "tests")
                        source_report   = $reportPath
                        output_snippet  = $outputSnip
                        created_at      = Get-V2Timestamp
                        updated_at      = Get-V2Timestamp
                    }
                    $dagToPersist = $dag
                    $createdRepairTaskId = $repairId
                }
                elseif ($openSameFingerprintCount -gt 0) {
                    Write-Host ("[TestRunner] Skipped REPAIR creation: open task with same fingerprint ({0})." -f $failureFingerprint)
                }
                else {
                    Write-Host ("[TestRunner] Skipped REPAIR creation: cooldown active ({0} min) for fingerprint {1}." -f $repairCooldownMinutes, $failureFingerprint)
                }
            }
        } | Out-Null

        if ($dagToPersist) {
            [void](Save-V2TestRunnerTaskDag `
                -DagPath $dagPath `
                -DagDocument $dagToPersist `
                -TaskStateDbScriptPath $taskStateDbScriptPath `
                -ProjectRoot $resolvedPath `
                -OrchestratorRoot $orchestratorRoot `
                -UseTaskStateDbPrimary:$taskStateDbPrimary)
        }
        if ($resolvedRepairCount -gt 0) {
            Write-Host ("[TestRunner] Auto-resolved {0} test-failure REPAIR task(s)." -f $resolvedRepairCount)
        }
        if (-not [string]::IsNullOrWhiteSpace($createdRepairTaskId)) {
            Write-Host "[TestRunner] Created REPAIR task: $createdRepairTaskId"
        }
    }
    catch {
        Write-Warning "[TestRunner] Could not update task-dag: $($_.Exception.Message)"
    }
}

Write-Output ($report | ConvertTo-Json -Depth 4)
