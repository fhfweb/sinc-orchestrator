<#
.SYNOPSIS
    V2 Observer - monitors project health and generates self-healing tasks on detected issues.
.DESCRIPTION
    Runs health checks on a submitted project by scanning for test failures, build errors,
    missing migrations, stale locks, and other anomalies in the .ai-orchestrator layer.
    Creates REPAIR tasks in task-dag.json when issues are found.
    Can run once (RunOnce) or continuously on an interval.
.PARAMETER ProjectPath
    Path to the project root. Defaults to current directory.
.PARAMETER IntervalSeconds
    Polling interval in continuous mode. Default: 300 (5 minutes).
.PARAMETER RunOnce
    If set, runs one observation cycle and exits.
.PARAMETER SkipMemorySync
    If set, does not trigger memory_sync.py after observation.
.EXAMPLE
    .\scripts\v2\Invoke-ObserverV2.ps1 -ProjectPath C:\projects\myapp -RunOnce
    .\scripts\v2\Invoke-ObserverV2.ps1 -ProjectPath C:\projects\myapp -IntervalSeconds 60
#>param(
    [string]$ProjectPath = ".",
    [int]$IntervalSeconds = 300,
    [switch]$RunOnce,
    [switch]$SkipMemorySync,
    [switch]$SkipHealthChecks,
    [switch]$AllowInferredCommands,
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [switch]$SkipRuntimeObservability,
    [int]$CommandTimeoutSeconds = 900,
    [bool]$EnableFastSlowPath = $true,
    [int]$HeavyCycleCadence = 6,
    [int]$HeavyStageTimeoutSeconds = 900,
    [int]$IncidentDedupCooldownSeconds = 600,
    [string]$AuditFindingPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

. (Join-Path $PSScriptRoot "Common.ps1")

function Test-V2AllowedObservedCommand {
    param([string]$CommandText)

    $normalized = (($CommandText -replace "\s+", " ").Trim().ToLowerInvariant())
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return $false
    }

    $allowedPrefixes = @(
        "npm test",
        "npm run test",
        "npm run build",
        "pnpm test",
        "pnpm run test",
        "pnpm run build",
        "yarn test",
        "yarn build",
        "pytest",
        "python -m pytest",
        "python -m unittest",
        "python -m compileall",
        "go test",
        "go build",
        "cargo test",
        "cargo build",
        "dotnet test",
        "dotnet build",
        "php artisan test",
        "composer test",
        ".\\gradlew.bat test",
        ".\\gradlew.bat build",
        ".\\mvnw test",
        ".\\mvnw package",
        "invoke-pester"
    )

    foreach ($prefix in $allowedPrefixes) {
        if ($normalized.StartsWith($prefix)) {
            return $true
        }
    }

    return $false
}

function Invoke-ObservedCommandV2 {
    param(
        [string]$CommandText,
        [string]$WorkingDirectory,
        [int]$TimeoutSeconds
    )

    if ([string]::IsNullOrWhiteSpace($CommandText) -or $CommandText -eq "unknown") {
        return [PSCustomObject]@{
            command  = $CommandText
            exitCode = $null
            timedOut = $false
            output   = "unknown command"
            status   = "skipped"
        }
    }

    $safeCommand = (($CommandText -replace "\s+#\s+REVIEW_REQUIRED.*$", "").Trim())
    if ([string]::IsNullOrWhiteSpace($safeCommand)) {
        return [PSCustomObject]@{
            command  = $CommandText
            exitCode = $null
            timedOut = $false
            output   = "empty command after normalization"
            status   = "skipped"
        }
    }

    if (-not (Test-V2AllowedObservedCommand -CommandText $safeCommand)) {
        return [PSCustomObject]@{
            command  = $safeCommand
            exitCode = $null
            timedOut = $false
            output   = "command blocked by allowlist"
            status   = "blocked"
        }
    }

    # Windows-safe pytest execution:
    # 1) fallback to "python -m pytest" when pytest.exe is not on PATH
    # 2) force basetemp/cache inside project to avoid permission issues in user temp dirs
    $normalizedSafeCommand = $safeCommand.Trim().ToLowerInvariant()
    $isPytestCommand = $normalizedSafeCommand -eq "pytest" -or
        $normalizedSafeCommand.StartsWith("pytest ") -or
        $normalizedSafeCommand.StartsWith("python -m pytest")
    if ($isPytestCommand) {
        $projectPytestRuntime = Join-Path $WorkingDirectory "ai-orchestrator/runtime/pytest"
        Initialize-V2Directory -Path $projectPytestRuntime
        $baseTempPath = Join-Path $projectPytestRuntime "basetemp"
        $cachePath = Join-Path $projectPytestRuntime "cache"
        Initialize-V2Directory -Path $baseTempPath
        Initialize-V2Directory -Path $cachePath

        $hasPytestExecutable = [bool](Get-Command "pytest" -ErrorAction SilentlyContinue)
        if (-not $hasPytestExecutable -and $normalizedSafeCommand.StartsWith("pytest")) {
            $safeCommand = "python -m pytest"
        }

        if ($safeCommand -notmatch "(^|\s)--basetemp(\s|=)") {
            $safeCommand = "$safeCommand --basetemp `"$baseTempPath`""
        }
        if ($safeCommand -notmatch "(^|\s)-o\s+cache_dir=") {
            $safeCommand = "$safeCommand -o cache_dir=`"$cachePath`""
        }
    }

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $cmdArguments = "/d /s /c `"cd /d `"$WorkingDirectory`" && $safeCommand`""
        $process = Start-Process -FilePath $env:ComSpec `
            -ArgumentList $cmdArguments `
            -WorkingDirectory $WorkingDirectory `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -WindowStyle Hidden

        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            try { $process.Kill() } catch {}
        }
        else {
            $process.WaitForExit()
        }

        $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        $combined = (@($stdout, $stderr) -join [Environment]::NewLine).Trim()

        $exitCode = $null
        if (-not $timedOut) {
            try { $process.Refresh() } catch {}
            try { $exitCode = [int]$process.ExitCode } catch { $exitCode = $null }
        }
        if (-not $timedOut -and $null -eq $exitCode) {
            $normalizedOutput = $combined.ToLowerInvariant()
            if ($normalizedOutput -match "\b\d+\s+passed\b" -and $normalizedOutput -notmatch "\berror\b|\bfailed\b|\btraceback\b") {
                $exitCode = 0
            }
        }

        $status = if ($timedOut) { "timeout" } elseif ($exitCode -eq 0) { "passed" } else { "failed" }
        return [PSCustomObject]@{
            command  = $safeCommand
            exitCode = if ($timedOut) { $null } else { $exitCode }
            timedOut = $timedOut
            output   = $combined
            status   = $status
        }
    }
    finally {
        foreach ($file in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $file) {
                Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            }
        }
    }
}


function Invoke-V2ExternalCommandWithTimeout {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = ".",
        [int]$TimeoutSeconds = 900
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru `
            -WindowStyle Hidden

        $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
        if ($timedOut) {
            try { $process.Kill() } catch {}
        }
        else {
            $process.WaitForExit()
        }

        $stdout = if (Test-Path -LiteralPath $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        $combined = (@($stdout, $stderr) -join [Environment]::NewLine).Trim()
        $exitCode = if ($timedOut) { $null } else { [int]$process.ExitCode }
        return [PSCustomObject]@{
            timed_out = $timedOut
            exit_code = $exitCode
            output = $combined
        }
    }
    finally {
        foreach ($file in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $file) {
                Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            }
        }
    }
}


function Ensure-V2ExecutionBacklogTask {
    param(
        [string]$TaskDagJsonPath,
        [string]$BacklogPath,
        [string]$RoadmapPath,
        [string]$IncidentPath = ""
    )

    if ([string]::IsNullOrWhiteSpace($TaskDagJsonPath) -or -not (Test-Path -LiteralPath $TaskDagJsonPath -PathType Leaf)) {
        return ""
    }

    $pendingMarkers = 0
    $actionableMarkers = 0
    $pendingSections = New-Object System.Collections.Generic.List[string]
    $roadmapActionableItems = New-Object System.Collections.Generic.List[object]
    $roadmapActionableIds = @{}
    if (-not [string]::IsNullOrWhiteSpace($RoadmapPath) -and (Test-Path -LiteralPath $RoadmapPath -PathType Leaf)) {
        try {
            $currentSection = ""
            $roadmapLines = @(Get-Content -LiteralPath $RoadmapPath -ErrorAction Stop)
            foreach ($line in $roadmapLines) {
                $text = [string]$line
                if ($text -match "^\s*##\s+(.+?)\s*$") {
                    $currentSection = [string]$matches[1]
                    continue
                }
                if ($text -match "^\s*-\s*pending\s*$") {
                    $pendingMarkers += 1
                    if (-not [string]::IsNullOrWhiteSpace($currentSection) -and $pendingSections -notcontains $currentSection) {
                        $pendingSections.Add($currentSection)
                    }
                    continue
                }
                if ($text -match "^\s*-\s*((?:FEAT|DEV|TASK|COBERTURA|RECHECK|REPAIR|REFACTOR)-[A-Za-z0-9-]+)\s*:\s*(.+?)\s*$") {
                    $itemId = [string]$matches[1]
                    $itemDescription = [string]$matches[2]
                    if (-not [string]::IsNullOrWhiteSpace($itemId) -and -not $roadmapActionableIds.ContainsKey($itemId)) {
                        $roadmapActionableItems.Add([PSCustomObject]@{
                                id          = $itemId
                                description = $itemDescription.Trim()
                                section     = $currentSection
                            })
                        $roadmapActionableIds[$itemId] = $true
                    }
                }
                if ($text -match "^\s*-\s*(.+?)\s*$") {
                    $markerText = [string]$matches[1]
                    if (-not [string]::IsNullOrWhiteSpace($markerText) -and $markerText.Trim().ToLowerInvariant() -ne "pending") {
                        $actionableMarkers += 1
                    }
                }
            }
        }
        catch {
        }
    }

    $sectionHint = ""
    if ($pendingSections.Count -gt 0) {
        $sectionHint = " Sections: $((@($pendingSections.ToArray()) | Select-Object -First 5) -join ", ")."
    }
    $markerHint = if ($pendingMarkers -gt 0 -and $actionableMarkers -gt 0) {
        "Detected $pendingMarkers pending roadmap marker(s) and $actionableMarkers actionable roadmap item(s)."
    }
    elseif ($pendingMarkers -gt 0) {
        "Detected $pendingMarkers pending roadmap marker(s)."
    }
    elseif ($actionableMarkers -gt 0) {
        "Detected $actionableMarkers actionable roadmap item(s)."
    }
    else {
        "Roadmap has execution items that still need task seeding."
    }
    $roadmapSparse = ($pendingMarkers -gt 0 -and $actionableMarkers -eq 0)

    $seedReason = "execution-backlog-gap-seed: roadmap pending items need execution tasks"
    $denseSeedReason = "execution-backlog-gap-dense-seed: sparse roadmap auto expanded to executable backlog"
    $seedResult = Invoke-V2WithDagMutex -DagPath $TaskDagJsonPath -ScriptBlock {
        $taskDocument = Get-V2JsonContent -Path $TaskDagJsonPath
        if (-not $taskDocument -or -not ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
            return [PSCustomObject]@{
                task_id = ""
                created = $false
                seed_type = "none"
                created_count = 0
            }
        }

        $openStatuses = Get-V2ObserverOpenTaskStatuses
        $existingById = @{}
        foreach ($task in @($taskDocument.tasks)) {
            $existingTaskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($existingTaskId)) {
                $existingById[$existingTaskId] = $true
            }
        }

        if ($roadmapActionableItems.Count -gt 0) {
            $createdRoadmapTasks = 0
            $firstRoadmapTaskId = ""
            $timestampRoadmap = Get-V2Timestamp
            foreach ($item in @($roadmapActionableItems.ToArray())) {
                $itemId = [string](Get-V2OptionalProperty -InputObject $item -Name "id" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($itemId)) { continue }
                if ($existingById.ContainsKey($itemId)) { continue }

                $itemDescription = [string](Get-V2OptionalProperty -InputObject $item -Name "description" -DefaultValue "")
                $itemSection = [string](Get-V2OptionalProperty -InputObject $item -Name "section" -DefaultValue "")
                $itemPriority = if ($itemSection -match "(?i)^next$") { "P2" } else { "P1" }

                $newRoadmapTask = [PSCustomObject]@{
                    id                 = $itemId
                    title              = if ([string]::IsNullOrWhiteSpace($itemDescription)) { "Execute roadmap item $itemId" } else { $itemDescription }
                    description        = if ([string]::IsNullOrWhiteSpace($itemDescription)) { "Auto-generated from roadmap actionable item $itemId." } else { "Auto-generated from roadmap actionable item $itemId. $itemDescription" }
                    reason             = "${seedReason}:roadmap-actionable"
                    reason_fingerprint = "${seedReason}:roadmap-actionable"
                    priority           = $itemPriority
                    dependencies       = @()
                    preferred_agent    = "Codex"
                    assigned_agent     = ""
                    execution_mode     = "llm-native"
                    runtime_engine     = "python"
                    status             = "pending"
                    files_affected     = @()
                    source_incident    = $IncidentPath
                    created_at         = $timestampRoadmap
                    updated_at         = $timestampRoadmap
                }

                $taskDocument.tasks += $newRoadmapTask
                $existingById[$itemId] = $true
                $createdRoadmapTasks += 1
                if ([string]::IsNullOrWhiteSpace($firstRoadmapTaskId)) {
                    $firstRoadmapTaskId = $itemId
                }
            }

            if ($createdRoadmapTasks -gt 0) {
                foreach ($task in @($taskDocument.tasks)) {
                    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                    if ($taskId -notlike "DEV-ROADMAP-AUTO-*") { continue }
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    if ($taskStatus -notin $openStatuses) { continue }
                    Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                    Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestampRoadmap
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestampRoadmap
                    Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: roadmap actionable tasks seeded"
                }

                if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                    $taskDocument.updated_at = $timestampRoadmap
                }
                else {
                    Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestampRoadmap -Force
                }
                Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
                return [PSCustomObject]@{
                    task_id = $firstRoadmapTaskId
                    created = $true
                    seed_type = "roadmap-actionable"
                    created_count = $createdRoadmapTasks
                }
            }

            $allActionablePresent = $true
            foreach ($item in @($roadmapActionableItems.ToArray())) {
                $itemId = [string](Get-V2OptionalProperty -InputObject $item -Name "id" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($itemId)) { continue }
                if (-not $existingById.ContainsKey($itemId)) {
                    $allActionablePresent = $false
                    break
                }
            }
            if ($allActionablePresent) {
                $resolvedLegacySeed = $false
                $timestampResolve = Get-V2Timestamp
                foreach ($task in @($taskDocument.tasks)) {
                    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                    if ($taskId -notlike "DEV-ROADMAP-AUTO-*") { continue }
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    if ($taskStatus -notin $openStatuses) { continue }
                    Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                    Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestampResolve
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestampResolve
                    Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: roadmap items already materialized"
                    $resolvedLegacySeed = $true
                }
                if ($resolvedLegacySeed) {
                    if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                        $taskDocument.updated_at = $timestampResolve
                    }
                    else {
                        Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestampResolve -Force
                    }
                    Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
                }
            }
        }

        $existingExecutionTask = @($taskDocument.tasks | Where-Object {
                $id = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                $isOpen = $status -in $openStatuses
                $isExecution = Test-V2ExecutionTaskId -TaskId $id
                $isOpen -and $isExecution
            } | Select-Object -First 1)
        if ($existingExecutionTask.Count -eq 1) {
            return [PSCustomObject]@{
                task_id = [string](Get-V2OptionalProperty -InputObject $existingExecutionTask[0] -Name "id" -DefaultValue "")
                created = $false
                seed_type = "existing"
                created_count = 0
            }
        }

        if ($roadmapSparse) {
            $existingOpenDenseTask = @($taskDocument.tasks | Where-Object {
                    $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                    $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    ($status -in $openStatuses) -and ($reason -like "$denseSeedReason*")
                } | Select-Object -First 1)
            if ($existingOpenDenseTask.Count -eq 1) {
                return [PSCustomObject]@{
                    task_id = [string](Get-V2OptionalProperty -InputObject $existingOpenDenseTask[0] -Name "id" -DefaultValue "")
                    created = $false
                    seed_type = "existing-dense"
                    created_count = 0
                }
            }

            $latestClosedDenseTask = @($taskDocument.tasks | Where-Object {
                    $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                    $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    ($reason -like "$denseSeedReason*") -and ($status -in @("done", "completed", "skipped"))
                } | Sort-Object {
                    [string](Get-V2OptionalProperty -InputObject $_ -Name "updated_at" -DefaultValue "")
                } -Descending | Select-Object -First 1)
            if ($latestClosedDenseTask.Count -eq 1) {
                $timestamp = Get-V2Timestamp
                $denseTask = $latestClosedDenseTask[0]
                Set-V2DynamicProperty -InputObject $denseTask -Name "status" -Value "pending"
                Set-V2DynamicProperty -InputObject $denseTask -Name "assigned_agent" -Value "Codex"
                Set-V2DynamicProperty -InputObject $denseTask -Name "preferred_agent" -Value "Codex"
                Set-V2DynamicProperty -InputObject $denseTask -Name "execution_mode" -Value "llm-native"
                Set-V2DynamicProperty -InputObject $denseTask -Name "runtime_engine" -Value "python"
                Set-V2DynamicProperty -InputObject $denseTask -Name "blocked_reason" -Value ""
                Set-V2DynamicProperty -InputObject $denseTask -Name "updated_at" -Value $timestamp
                Set-V2DynamicProperty -InputObject $denseTask -Name "completed_at" -Value ""
                Set-V2DynamicProperty -InputObject $denseTask -Name "completion_note" -Value "auto-reopened: sparse roadmap still pending"
                if (-not [string]::IsNullOrWhiteSpace($IncidentPath)) {
                    Set-V2DynamicProperty -InputObject $denseTask -Name "source_incident" -Value $IncidentPath
                }
                if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                    $taskDocument.updated_at = $timestamp
                }
                else {
                    Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
                }
                Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
                return [PSCustomObject]@{
                    task_id = [string](Get-V2OptionalProperty -InputObject $denseTask -Name "id" -DefaultValue "")
                    created = $false
                    seed_type = "reopen-dense"
                    created_count = 0
                }
            }

            $existingById = @{}
            foreach ($task in @($taskDocument.tasks)) {
                $existingTaskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                if (-not [string]::IsNullOrWhiteSpace($existingTaskId)) {
                    $existingById[$existingTaskId] = $true
                }
            }

            $denseTemplates = @(
                [PSCustomObject]@{
                    id = "FEAT-WEB-APPOINTMENTS-QUALITY-004"
                    title = "Harden appointments web UX and edge cases"
                    description = "Improve appointments module UX/validation/loading states and add regression tests for key user flows."
                    priority = "P1"
                    dependencies = @()
                    files_affected = @("resources/views/appointments/index.blade.php", "app/Http/Controllers/Api/AppointmentController.php", "tests/Feature")
                },
                [PSCustomObject]@{
                    id = "FEAT-WEB-RECORDS-QUALITY-004"
                    title = "Harden records web UX and privacy guards"
                    description = "Improve records module UX and enforce safer rendering/permission feedback for sensitive clinical data."
                    priority = "P1"
                    dependencies = @("FEAT-WEB-APPOINTMENTS-QUALITY-004")
                    files_affected = @("resources/views/records/index.blade.php", "app/Http/Controllers/Api/MedicalRecordController.php", "tests/Feature")
                },
                [PSCustomObject]@{
                    id = "FEAT-WEB-FINANCIAL-QUALITY-004"
                    title = "Harden financial web flows and calculations"
                    description = "Refine financial screens, validation, totals consistency and error handling with focused regression tests."
                    priority = "P1"
                    dependencies = @("FEAT-WEB-APPOINTMENTS-QUALITY-004")
                    files_affected = @("resources/views/financial/index.blade.php", "app/Http/Controllers/Api/PaymentController.php", "tests/Feature")
                },
                [PSCustomObject]@{
                    id = "FEAT-WEB-DOCUMENTS-QUALITY-004"
                    title = "Harden documents web upload/download UX"
                    description = "Improve document upload/download UX, empty/error states, and permission feedback with regression coverage."
                    priority = "P1"
                    dependencies = @("FEAT-WEB-APPOINTMENTS-QUALITY-004")
                    files_affected = @("resources/views/documents/index.blade.php", "app/Http/Controllers/Api/DocumentController.php", "tests/Feature")
                },
                [PSCustomObject]@{
                    id = "FEAT-API-ADVANCED-REPORTS-HARDEN-004"
                    title = "Harden advanced reports API consistency"
                    description = "Strengthen advanced report filters, response consistency, and test coverage for edge-date and empty-data scenarios."
                    priority = "P1"
                    dependencies = @("FEAT-WEB-FINANCIAL-QUALITY-004")
                    files_affected = @("app/Http/Controllers/Api/AdvancedReportController.php", "tests/Feature")
                }
            )

            $addedCount = 0
            $firstAddedId = ""
            $timestampDense = Get-V2Timestamp
            foreach ($tpl in $denseTemplates) {
                $tplId = [string](Get-V2OptionalProperty -InputObject $tpl -Name "id" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($tplId)) { continue }
                if ($existingById.ContainsKey($tplId)) { continue }

                $newDenseTask = [PSCustomObject]@{
                    id                 = $tplId
                    title              = [string](Get-V2OptionalProperty -InputObject $tpl -Name "title" -DefaultValue $tplId)
                    description        = [string](Get-V2OptionalProperty -InputObject $tpl -Name "description" -DefaultValue "")
                    reason             = "$denseSeedReason:auto-template"
                    reason_fingerprint = "$denseSeedReason:auto-template"
                    priority           = [string](Get-V2OptionalProperty -InputObject $tpl -Name "priority" -DefaultValue "P1")
                    dependencies       = @((Get-V2OptionalProperty -InputObject $tpl -Name "dependencies" -DefaultValue @()))
                    preferred_agent    = "Codex"
                    assigned_agent     = ""
                    execution_mode     = "llm-native"
                    runtime_engine     = "python"
                    status             = "pending"
                    files_affected     = @((Get-V2OptionalProperty -InputObject $tpl -Name "files_affected" -DefaultValue @()))
                    source_incident    = $IncidentPath
                    created_at         = $timestampDense
                    updated_at         = $timestampDense
                }

                $taskDocument.tasks += $newDenseTask
                $existingById[$tplId] = $true
                $addedCount += 1
                if ([string]::IsNullOrWhiteSpace($firstAddedId)) {
                    $firstAddedId = $tplId
                }
            }

            if ($addedCount -gt 0) {
                if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                    $taskDocument.updated_at = $timestampDense
                }
                else {
                    Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestampDense -Force
                }
                Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
                return [PSCustomObject]@{
                    task_id = $firstAddedId
                    created = $true
                    seed_type = "dense"
                    created_count = $addedCount
                }
            }
        }

        $existingSeedTask = @($taskDocument.tasks | Where-Object {
                $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                ($status -in $openStatuses) -and ($reason -like "$seedReason*")
            } | Select-Object -First 1)
        if ($existingSeedTask.Count -eq 1) {
            return [PSCustomObject]@{
                task_id = [string](Get-V2OptionalProperty -InputObject $existingSeedTask[0] -Name "id" -DefaultValue "")
                created = $false
                seed_type = "existing-single"
                created_count = 0
            }
        }

        $timestamp = Get-V2Timestamp
        $latestClosedSeedTask = @($taskDocument.tasks | Where-Object {
                $reason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                ($reason -like "$seedReason*") -and ($status -in @("done", "completed", "skipped"))
            } | Sort-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "updated_at" -DefaultValue "")
            } -Descending | Select-Object -First 1)
        if ($latestClosedSeedTask.Count -eq 1) {
            $seedTask = $latestClosedSeedTask[0]
            Set-V2DynamicProperty -InputObject $seedTask -Name "status" -Value "pending"
            Set-V2DynamicProperty -InputObject $seedTask -Name "assigned_agent" -Value "Codex"
            Set-V2DynamicProperty -InputObject $seedTask -Name "preferred_agent" -Value "Codex"
            Set-V2DynamicProperty -InputObject $seedTask -Name "execution_mode" -Value "llm-native"
            Set-V2DynamicProperty -InputObject $seedTask -Name "runtime_engine" -Value "python"
            Set-V2DynamicProperty -InputObject $seedTask -Name "blocked_reason" -Value ""
            Set-V2DynamicProperty -InputObject $seedTask -Name "updated_at" -Value $timestamp
            Set-V2DynamicProperty -InputObject $seedTask -Name "completed_at" -Value ""
            Set-V2DynamicProperty -InputObject $seedTask -Name "completion_note" -Value "auto-reopened: roadmap still pending"
            if (-not [string]::IsNullOrWhiteSpace($IncidentPath)) {
                Set-V2DynamicProperty -InputObject $seedTask -Name "source_incident" -Value $IncidentPath
            }
            if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                $taskDocument.updated_at = $timestamp
            }
            else {
                Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
            }
            Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
            return [PSCustomObject]@{
                task_id = [string](Get-V2OptionalProperty -InputObject $seedTask -Name "id" -DefaultValue "")
                created = $false
                seed_type = "reopen-single"
                created_count = 0
            }
        }

        $taskId = "DEV-ROADMAP-AUTO-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 4))
        $newTask = [PSCustomObject]@{
            id                 = $taskId
            title              = "Convert roadmap items into executable tasks"
            description        = ("CORE-COMPLETE-001 is done and roadmap still has actionable items. Create FEAT/DEV/COBERTURA/RECHECK tasks from roadmap priorities and start execution. " + $markerHint + $sectionHint).Trim()
            reason             = $seedReason
            reason_fingerprint = $seedReason
            priority           = "P1"
            dependencies       = @()
            preferred_agent    = "Codex"
            assigned_agent     = "Codex"
            execution_mode     = "llm-native"
            runtime_engine     = "python"
            status             = "pending"
            files_affected     = @(
                "ai-orchestrator/memory/roadmap.md",
                "ai-orchestrator/tasks/task-dag.json",
                "ai-orchestrator/tasks/backlog.md"
            )
            source_incident    = $IncidentPath
            created_at         = $timestamp
            updated_at         = $timestamp
        }
        $taskDocument.tasks += $newTask
        if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
            $taskDocument.updated_at = $timestamp
        }
        else {
            Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
        }
        Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument

        return [PSCustomObject]@{
            task_id = $taskId
            created = $true
            seed_type = "single"
            created_count = 1
        }
    }

    $taskId = [string](Get-V2OptionalProperty -InputObject $seedResult -Name "task_id" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($taskId)) {
        return ""
    }
    $created = [bool](Get-V2OptionalProperty -InputObject $seedResult -Name "created" -DefaultValue $false)
    $seedType = [string](Get-V2OptionalProperty -InputObject $seedResult -Name "seed_type" -DefaultValue "single")
    $createdCount = [int](Get-V2OptionalProperty -InputObject $seedResult -Name "created_count" -DefaultValue 1)
    if (-not $created) {
        return $taskId
    }

    if (-not [string]::IsNullOrWhiteSpace($BacklogPath)) {
        try {
            $alreadyInBacklog = $false
            if (Test-Path -LiteralPath $BacklogPath -PathType Leaf) {
                $backlogContent = Get-Content -LiteralPath $BacklogPath -Raw -ErrorAction SilentlyContinue
                $alreadyInBacklog = $backlogContent -match ("(?im)^-\s*id:\s*" + [regex]::Escape($taskId) + "\s*$")
            }

            if (-not $alreadyInBacklog) {
                if ($seedType -eq "dense") {
                    $backlogTitle = "Seed dense execution backlog from sparse roadmap"
                    $backlogDescription = "Auto-generated dense task seed from observer because roadmap.md has only generic pending markers. Created $createdCount execution task(s)."
                    $backlogReason = $denseSeedReason
                }
                elseif ($seedType -eq "roadmap-actionable") {
                    $backlogTitle = "Seed actionable roadmap items into DAG"
                    $backlogDescription = "Auto-generated from observer by converting actionable roadmap entries into pending FEAT/DEV/TASK/REPAIR/REFACTOR tasks. Created $createdCount task(s)."
                    $backlogReason = "${seedReason}:roadmap-actionable"
                }
                else {
                    $backlogTitle = "Convert roadmap items into executable tasks"
                    $backlogDescription = "Auto-generated from observer because roadmap.md has actionable items but no FEAT/DEV/COBERTURA/RECHECK task is open."
                    $backlogReason = $seedReason
                }
                $backlogMode = "llm-native"
                $backlogEntry = @"

- id: $taskId
  title: $backlogTitle
  description: $backlogDescription
  reason: $backlogReason
  priority: P1
  dependencies: []
  assigned_agent: Codex
  execution_mode: $backlogMode
  status: pending
  source_incident: $IncidentPath
"@
                Add-Content -LiteralPath $BacklogPath -Value $backlogEntry
            }
        }
        catch {
        }
    }

    $schedulerSyncScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
    if (Test-Path -LiteralPath $schedulerSyncScript -PathType Leaf) {
        $projectRootForSync = Split-Path -Parent (Split-Path -Parent $TaskDagJsonPath)
        try {
            & $schedulerSyncScript -ProjectPath $projectRootForSync -MaxAssignmentsPerRun 0 -EmitJson | Out-Null
        }
        catch {
        }
    }

    return $taskId
}

function Get-LessonLearnedHints {
    param(
        [string]$LessonsDirectory,
        [string]$Category,
        [string]$CommandText,
        [string]$OutputText,
        [int]$Limit = 3
    )

    if (-not (Test-Path -LiteralPath $LessonsDirectory -PathType Container)) {
        return @()
    }

    $tokenBag = New-Object System.Collections.Generic.List[string]
    foreach ($token in @(
        $Category,
        $CommandText,
        (($CommandText -split "\s+" | Select-Object -First 6) -join " "),
        (($OutputText -split "(`r`n|`n|`r)" | Select-Object -First 6) -join " ")
    )) {
        $text = [string]$token
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        foreach ($piece in @($text.ToLowerInvariant() -split "[^a-z0-9_]+")) {
            if ($piece.Length -ge 4) {
                $tokenBag.Add($piece)
            }
        }
    }
    $tokens = @($tokenBag | Select-Object -Unique)
    if ($tokens.Count -eq 0) {
        return @()
    }

    $scored = New-Object System.Collections.Generic.List[object]
    foreach ($file in @(Get-ChildItem -LiteralPath $LessonsDirectory -File -Filter "*.md" -ErrorAction SilentlyContinue)) {
        $content = (Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue).ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($content)) { continue }

        $score = 0
        foreach ($token in $tokens) {
            if ($content.Contains($token)) {
                $score += 1
            }
        }
        if ($score -gt 0) {
            $scored.Add([PSCustomObject]@{
                score = $score
                file  = $file.Name
            })
        }
    }

    return @(
        @($scored | Sort-Object @{ Expression = { $_.score }; Descending = $true }, @{ Expression = { $_.file }; Descending = $false } | Select-Object -First $Limit | ForEach-Object { $_.file })
    )
}

function Sync-V2WalkthroughLessons {
    param(
        [string]$ProjectRoot,
        [string]$LessonsDirectory
    )

    if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { return }
    Initialize-V2Directory -Path $LessonsDirectory

    $walkthroughs = Get-ChildItem -LiteralPath $ProjectRoot -Filter "walkthrough*.md" -Recurse -ErrorAction SilentlyContinue
    foreach ($wt in $walkthroughs) {
        $content = Get-Content -LiteralPath $wt.FullName -Raw
        if ($content -match "(?ms)## (?:Lessons Learned|Key Insights)(.*?)(?=##|$)") {
            $lessonContent = $matches[1].Trim()
            if ([string]::IsNullOrWhiteSpace($lessonContent)) { continue }

            $hash = Get-V2Sha1Hex -Text $lessonContent
            $lessonPath = Join-Path $LessonsDirectory "LESSON_WT_$($hash.Substring(0,8)).md"
            
            if (-not (Test-Path -LiteralPath $lessonPath)) {
                $lines = @(
                    "# Lesson Learned: Walkthrough Extraction",
                    "",
                    "- Extracted From: $($wt.FullName)",
                    "- Date: $(Get-V2Timestamp)",
                    "",
                    "## Content",
                    $lessonContent
                )
                [System.IO.File]::WriteAllText($lessonPath, ($lines -join [Environment]::NewLine))
                
                # Record to DB (Postgres/SQLite)
                try {
                    $lessonPayload = [PSCustomObject]@{
                        task_id     = "WT-EXTRACT"
                        category    = "walkthrough"
                        lesson      = $lessonContent
                        source_file = $wt.FullName
                    }
                    $tempLessonJson = [System.IO.Path]::GetTempFileName()
                    try {
                        $lessonPayload | ConvertTo-Json -Depth 16 | Out-File -FilePath $tempLessonJson -Encoding utf8
                        Invoke-V2TaskDb -Mode "record-lesson" -TasksJsonPath $tempLessonJson | Out-Null
                    }
                    finally {
                        if (Test-Path -LiteralPath $tempLessonJson) { Remove-Item -LiteralPath $tempLessonJson -Force }
                    }
                }
                catch {
                    Write-Warning "Failed to record lesson to database: $($_.Exception.Message)"
                }

                Write-Host "[Observer] Extracted new lesson from $($wt.Name)" -ForegroundColor Green
            }
        }
    }
}

function Write-PeriodicReports {
    param(
        [string]$ReportsDirectory,
        [string]$Timestamp,
        [string]$HealthStatus,
        [int]$UnknownCount,
        [int]$IncidentCount,
        [object[]]$Checks
    )

    $dailyPath = Join-Path $ReportsDirectory "daily-report.md"
    $weeklyPath = Join-Path $ReportsDirectory "weekly-report.md"
    $monthlyPath = Join-Path $ReportsDirectory "monthly-report.md"
    $now = Get-Date
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    $weekNumber = $culture.Calendar.GetWeekOfYear($now, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [System.DayOfWeek]::Monday)
    $weekId = "{0}-W{1:d2}" -f $now.Year, $weekNumber
    $monthId = $now.ToString("yyyy-MM")

    $passedChecks = @($Checks | Where-Object { $_.status -eq "passed" }).Count
    $failedChecks = @($Checks | Where-Object { $_.status -eq "failed" -or $_.status -eq "timeout" }).Count

    $baseLines = New-Object System.Collections.Generic.List[string]
    $baseLines.Add("- Generated At: $Timestamp")
    $baseLines.Add("- Health Status: $HealthStatus")
    $baseLines.Add("- Unknown Count: $UnknownCount")
    $baseLines.Add("- Incident Count: $IncidentCount")
    $baseLines.Add("- Check Pass: $passedChecks")
    $baseLines.Add("- Check Fail: $failedChecks")

    Write-V2File -Path $dailyPath -Content ("# Daily Report`r`n`r`n" + ($baseLines -join [Environment]::NewLine)) -Force
    Write-V2File -Path $weeklyPath -Content ("# Weekly Report`r`n`r`n- Window: $weekId`r`n" + ($baseLines -join [Environment]::NewLine)) -Force
    Write-V2File -Path $monthlyPath -Content ("# Monthly Report`r`n`r`n- Window: $monthId`r`n" + ($baseLines -join [Environment]::NewLine)) -Force
}

function Append-ObserverCommunication {
    param(
        [string]$OrchestratorRoot,
        [string]$Timestamp,
        [string]$HealthStatus,
        [int]$IncidentCount
    )

    $messagePath = Join-Path $OrchestratorRoot "communication/messages.md"
    Add-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
        "## $Timestamp",
        "- from: ObserverV2",
        "- to: Orchestrator",
        "- status: $HealthStatus",
        "- incidents: $IncidentCount"
    )
}

function Get-V2UniqueObserverStrings {
    param([object[]]$Items)

    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($Items)) {
        $text = [string]$item
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        $normalized = $text.Trim()
        if ([string]::IsNullOrWhiteSpace($normalized)) { continue }
        if ($seen.Add($normalized)) {
            $result.Add($normalized)
        }
    }
    return @($result.ToArray())
}

function Get-V2ObserverOpenTaskStatuses {
    return @(Get-V2OpenTaskStatuses)
}

function Test-V2ObserverTaskOpen {
    param([object]$Task)

    if (-not $Task) { return $false }
    $taskStatus = [string](Get-V2OptionalProperty -InputObject $Task -Name "status" -DefaultValue "")
    return (Test-V2TaskStatusOpen -Status $taskStatus)
}

function Update-ObserverProjectDna {
    param(
        [string]$ProjectRoot,
        [object]$ProjectState,
        [string]$Timestamp,
        [string]$HealthStatus,
        [string[]]$Unknowns,
        [string[]]$Incidents
    )

    if (-not $ProjectState) { return }
    $relativePath = [string](Get-V2OptionalProperty -InputObject $ProjectState -Name "project_dna_path" -DefaultValue "")
    $projectSlug = [string](Get-V2OptionalProperty -InputObject $ProjectState -Name "project_slug" -DefaultValue "")
    $candidatePaths = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($relativePath)) {
        if ([System.IO.Path]::IsPathRooted($relativePath)) {
            $candidatePaths.Add($relativePath)
        }
        else {
            $candidatePaths.Add((Join-Path $ProjectRoot $relativePath))
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($projectSlug)) {
        $candidatePaths.Add((Join-Path $ProjectRoot ("ai-orchestrator/projects/{0}/project_dna.json" -f $projectSlug)))
    }
    $dnaPath = ""
    foreach ($candidate in @($candidatePaths.ToArray())) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $dnaPath = $candidate
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($dnaPath)) { return }

    $dna = Get-V2JsonContent -Path $dnaPath
    if (-not $dna) { return }

    $identity = Get-V2OptionalProperty -InputObject $dna -Name "project_identity" -DefaultValue ([PSCustomObject]@{})
    $prevHealth = [string](Get-V2OptionalProperty -InputObject $identity -Name "health_status" -DefaultValue "")
    $currentStateStatus = [string](Get-V2OptionalProperty -InputObject $ProjectState -Name "status" -DefaultValue "")
    $runtimeStatus = if ($currentStateStatus -like "blocked*") { "blocked" } elseif ($HealthStatus -in @("healthy")) { "active" } else { "degraded" }

    Set-V2DynamicProperty -InputObject $identity -Name "health_status" -Value $HealthStatus
    Set-V2DynamicProperty -InputObject $identity -Name "status" -Value $runtimeStatus
    Set-V2DynamicProperty -InputObject $identity -Name "updated_at" -Value $Timestamp
    Set-V2DynamicProperty -InputObject $dna -Name "project_identity" -Value $identity

    $evolution = Get-V2OptionalProperty -InputObject $dna -Name "evolution" -DefaultValue ([PSCustomObject]@{ major_changes = @() })
    $majorChanges = New-Object System.Collections.Generic.List[object]
    foreach ($change in @(Get-V2OptionalProperty -InputObject $evolution -Name "major_changes" -DefaultValue @())) {
        $majorChanges.Add($change)
    }
    if (-not [string]::IsNullOrWhiteSpace($prevHealth) -and $prevHealth -ne $HealthStatus) {
        $majorChanges.Add([PSCustomObject]@{
            date        = $Timestamp
            description = "observer health changed: $prevHealth -> $HealthStatus"
        })
    }
    Set-V2DynamicProperty -InputObject $evolution -Name "major_changes" -Value @($majorChanges | Select-Object -Last 120)
    Set-V2DynamicProperty -InputObject $dna -Name "evolution" -Value $evolution

    $knowledge = Get-V2OptionalProperty -InputObject $dna -Name "agent_knowledge" -DefaultValue ([PSCustomObject]@{ known_hotspots = @(); technical_debt = @() })
    $debtItems = New-Object System.Collections.Generic.List[string]
    foreach ($item in @(Get-V2OptionalProperty -InputObject $knowledge -Name "technical_debt" -DefaultValue @())) { $debtItems.Add([string]$item) }
    foreach ($unknown in @($Unknowns)) { $debtItems.Add([string]$unknown) }
    foreach ($incident in @($Incidents)) {
        $name = [System.IO.Path]::GetFileNameWithoutExtension([string]$incident)
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $debtItems.Add("incident:$name")
        }
    }
    Set-V2DynamicProperty -InputObject $knowledge -Name "technical_debt" -Value (Get-V2UniqueObserverStrings -Items @($debtItems.ToArray()) | Select-Object -First 200)
    Set-V2DynamicProperty -InputObject $dna -Name "agent_knowledge" -Value $knowledge

    Save-V2JsonContent -Path $dnaPath -Value $dna
}

function Test-V2MaskedSecretValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $true
    }
    $trimmed = $Value.Trim()
    if ($trimmed -eq "[stored in vault]") {
        return $true
    }
    return $trimmed -match "^\*+$" -or $trimmed -match "^[A-Za-z0-9]{1,3}\*{2,}.*$"
}

function Unprotect-V2ObserverVaultSecretValue {
    param([string]$CipherBase64)

    if ([string]::IsNullOrWhiteSpace($CipherBase64)) {
        return ""
    }

    try {
        $bytes = [Convert]::FromBase64String($CipherBase64)
        $plain = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $bytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [System.Text.Encoding]::UTF8.GetString($plain)
    }
    catch {
        return $CipherBase64
    }
}

function Get-V2ObserverVaultSecret {
    param(
        [string]$ProjectRoot,
        [object]$ProjectState,
        [string]$Domain,
        [string]$Key
    )

    $startupPaths = Get-V2OptionalProperty -InputObject $ProjectState -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})
    $vaultRelative = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "secrets_vault" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
    $vaultPath = if ([System.IO.Path]::IsPathRooted($vaultRelative)) { $vaultRelative } else { Join-Path $ProjectRoot $vaultRelative }
    if (-not (Test-Path -LiteralPath $vaultPath -PathType Leaf)) {
        return ""
    }

    $vault = Get-V2JsonContent -Path $vaultPath
    if (-not $vault) {
        return ""
    }

    $vaultEncrypted = [bool](Get-V2OptionalProperty -InputObject $vault -Name "encrypted" -DefaultValue $false)
    $secretsNode = Get-V2OptionalProperty -InputObject $vault -Name "secrets" -DefaultValue ([PSCustomObject]@{})
    $domainNode = Get-V2OptionalProperty -InputObject $secretsNode -Name $Domain -DefaultValue ([PSCustomObject]@{})
    $rawSecret = [string](Get-V2OptionalProperty -InputObject $domainNode -Name $Key -DefaultValue "")
    if ($vaultEncrypted -and -not [string]::IsNullOrWhiteSpace($rawSecret)) {
        return (Unprotect-V2ObserverVaultSecretValue -CipherBase64 $rawSecret)
    }
    return $rawSecret
}

function Get-V2ObserverGpuPolicy {
    param([string]$ProjectRoot)

    $defaultPolicy = [PSCustomObject]@{
        schema_version                 = "v1"
        enabled                        = $true
        require_gpu_for_embeddings     = $true
        repair_on_cpu_fallback         = $true
        expected_processor             = "gpu"
        unknown_processor_is_failure   = $false
        max_non_ollama_ratio_percent   = 20
        ollama                         = [PSCustomObject]@{
            embed_url      = "http://127.0.0.1:11435/v1/embeddings"
            embed_model    = "mxbai-embed-large:latest"
            embed_model_candidates = @("mxbai-embed-large:latest", "nomic-embed-text:latest", "all-minilm:latest")
            install_missing_models = $true
            max_install_attempts = 1
            keep_alive     = "10m"
            embed_batch_size = 24
            embed_batch_size_auto = $true
            embed_concurrency = 4
            embed_warmup_inputs = 32
            vram_reserve_mb = 3072
            max_loaded_models = 2
            num_parallel = 2
            num_gpu        = "1"
            num_thread     = "4"
            cuda_devices   = "0"
            nvidia_devices = "all"
        }
    }

    $configDir = Join-Path $ProjectRoot "ai-orchestrator/config"
    Initialize-V2Directory -Path $configDir
    $policyPath = Join-Path $configDir "gpu-acceleration-policy.json"
    if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
        Save-V2JsonContent -Path $policyPath -Value $defaultPolicy
        return $defaultPolicy
    }

    $loaded = Get-V2JsonContent -Path $policyPath
    if (-not $loaded) {
        return $defaultPolicy
    }

    $loadedOllama = Get-V2OptionalProperty -InputObject $loaded -Name "ollama" -DefaultValue ([PSCustomObject]@{})
    return [PSCustomObject]@{
        schema_version               = [string](Get-V2OptionalProperty -InputObject $loaded -Name "schema_version" -DefaultValue "v1")
        enabled                      = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "enabled" -DefaultValue $true)
        require_gpu_for_embeddings   = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "require_gpu_for_embeddings" -DefaultValue $true)
        repair_on_cpu_fallback       = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "repair_on_cpu_fallback" -DefaultValue $true)
        expected_processor           = [string](Get-V2OptionalProperty -InputObject $loaded -Name "expected_processor" -DefaultValue "gpu")
        unknown_processor_is_failure = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "unknown_processor_is_failure" -DefaultValue $false)
        max_non_ollama_ratio_percent = [double](Get-V2OptionalProperty -InputObject $loaded -Name "max_non_ollama_ratio_percent" -DefaultValue 20)
        ollama                       = [PSCustomObject]@{
            embed_url      = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_url" -DefaultValue "http://127.0.0.1:11435/v1/embeddings")
            embed_model    = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_model" -DefaultValue "mxbai-embed-large:latest")
            embed_model_candidates = @(Get-V2ObserverStringArray -Value (Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_model_candidates" -DefaultValue @("mxbai-embed-large:latest", "nomic-embed-text:latest", "all-minilm:latest")))
            install_missing_models = [bool](Get-V2OptionalProperty -InputObject $loadedOllama -Name "install_missing_models" -DefaultValue $true)
            max_install_attempts = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "max_install_attempts" -DefaultValue 1)
            keep_alive     = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "keep_alive" -DefaultValue "10m")
            embed_batch_size = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_batch_size" -DefaultValue 24)
            embed_batch_size_auto = [bool](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_batch_size_auto" -DefaultValue $true)
            embed_concurrency = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_concurrency" -DefaultValue 4)
            embed_warmup_inputs = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "embed_warmup_inputs" -DefaultValue 32)
            vram_reserve_mb = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "vram_reserve_mb" -DefaultValue 3072)
            max_loaded_models = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "max_loaded_models" -DefaultValue 2)
            num_parallel = [int](Get-V2OptionalProperty -InputObject $loadedOllama -Name "num_parallel" -DefaultValue 2)
            num_gpu        = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "num_gpu" -DefaultValue "1")
            num_thread     = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "num_thread" -DefaultValue "4")
            cuda_devices   = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "cuda_devices" -DefaultValue "0")
            nvidia_devices = [string](Get-V2OptionalProperty -InputObject $loadedOllama -Name "nvidia_devices" -DefaultValue "all")
        }
    }
}

function Get-V2ObserverStringArray {
    param([object]$Value)

    if ($null -eq $Value) { return @() }
    if ($Value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
        return @($Value.Trim())
    }
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($Value)) {
        $text = [string]$item
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        $list.Add($text.Trim())
    }
    return @($list.ToArray())
}

function Get-V2InstalledOllamaModels {
    $models = New-Object System.Collections.Generic.List[string]
    try {
        $lines = @(ollama list 2>$null)
        foreach ($line in $lines) {
            $text = [string]$line
            if ([string]::IsNullOrWhiteSpace($text)) { continue }
            if ($text.TrimStart().StartsWith("NAME")) { continue }
            $parts = @($text -split "\s+" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($parts.Count -lt 1) { continue }
            $name = [string]$parts[0]
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if ($models -notcontains $name) {
                $models.Add($name)
            }
        }
    }
    catch {
    }
    return @($models.ToArray())
}

function Resolve-V2ObserverOllamaEmbedModel {
    param([object]$OllamaConfig)

    $configuredModel = [string](Get-V2OptionalProperty -InputObject $OllamaConfig -Name "embed_model" -DefaultValue "all-minilm:latest")
    $candidateInput = Get-V2OptionalProperty -InputObject $OllamaConfig -Name "embed_model_candidates" -DefaultValue @()
    $candidateList = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(Get-V2ObserverStringArray -Value $candidateInput)) {
        if ($candidateList -notcontains $candidate) {
            $candidateList.Add($candidate)
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($configuredModel) -and $candidateList -notcontains $configuredModel) {
        $candidateList.Insert(0, $configuredModel)
    }
    if ($candidateList.Count -eq 0) {
        $candidateList.Add("mxbai-embed-large:latest")
        $candidateList.Add("nomic-embed-text:latest")
        $candidateList.Add("all-minilm:latest")
    }

    $installMissing = [bool](Get-V2OptionalProperty -InputObject $OllamaConfig -Name "install_missing_models" -DefaultValue $true)
    $maxInstallAttempts = [int](Get-V2OptionalProperty -InputObject $OllamaConfig -Name "max_install_attempts" -DefaultValue 1)
    if ($maxInstallAttempts -lt 1) { $maxInstallAttempts = 1 }

    $normalizeName = {
        param([string]$ModelName)
        $name = [string]$ModelName
        if ([string]::IsNullOrWhiteSpace($name)) { return "" }
        $normalized = $name.Trim().ToLowerInvariant()
        if ($normalized -notmatch ":[^/]+$") {
            $normalized = "$normalized`:latest"
        }
        return $normalized
    }

    $installed = @(Get-V2InstalledOllamaModels)
    $installedSet = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($model in $installed) {
        $normalized = & $normalizeName $model
        if (-not [string]::IsNullOrWhiteSpace($normalized)) {
            [void]$installedSet.Add($normalized)
        }
    }

    $selected = ""
    foreach ($candidate in @($candidateList.ToArray())) {
        $normalizedCandidate = & $normalizeName $candidate
        if ($installedSet.Contains($normalizedCandidate)) {
            $selected = $candidate
            break
        }
    }

    $installAttempted = $false
    $installSucceeded = $false
    $installError = ""
    $installedModel = ""
    if ([string]::IsNullOrWhiteSpace($selected) -and $installMissing) {
        $attempts = 0
        foreach ($candidate in @($candidateList.ToArray())) {
            if ($attempts -ge $maxInstallAttempts) { break }
            $attempts += 1
            $installAttempted = $true
            try {
                $output = @(ollama pull $candidate 2>&1)
                if ($LASTEXITCODE -eq 0) {
                    $installSucceeded = $true
                    $installedModel = $candidate
                    $selected = $candidate
                    break
                }
                $tail = ($output | Select-Object -Last 12) -join [Environment]::NewLine
                $installError = "ollama pull '$candidate' failed with exit code $LASTEXITCODE. $tail"
            }
            catch {
                $installError = $_.Exception.Message
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($selected)) {
        $selected = $configuredModel
    }

    return [PSCustomObject]@{
        selected_model = $selected
        configured_model = $configuredModel
        candidates = @($candidateList.ToArray())
        installed_models = $installed
        install_attempted = $installAttempted
        install_succeeded = $installSucceeded
        installed_model = $installedModel
        install_error = $installError
    }
}

function Invoke-V2ObserverMemorySync {
    param(
        [string]$ProjectRoot,
        [string]$OrchestratorRoot,
        [object]$ProjectState,
        [object]$Intake,
        [string]$MemorySyncScript,
        [string]$ProjectStatePath,
        [string]$WorldModelJsonPath,
        [int]$TimeoutSeconds = 900,
        [switch]$IncludeNeo4j,
        [switch]$IncludeQdrant
    )

    $statePackRootRelative = [string](Get-V2OptionalProperty -InputObject $ProjectState -Name "project_pack_root" -DefaultValue "")
    $projectPackRoot = Join-Path $OrchestratorRoot ("projects/{0}" -f $intake.project_slug)
    if (-not [string]::IsNullOrWhiteSpace($statePackRootRelative)) {
        $candidatePackRoot = Join-Path $ProjectRoot $statePackRootRelative
        $resolvedCandidatePackRoot = Resolve-V2AbsolutePath -Path $candidatePackRoot
        if (-not [string]::IsNullOrWhiteSpace($resolvedCandidatePackRoot)) {
            $projectPackRoot = $resolvedCandidatePackRoot
        }
    }

    Initialize-V2Directory -Path (Join-Path $projectPackRoot "memory")
    $relationshipsPath = Join-Path $projectPackRoot "memory/relationships.md"
    if (-not (Test-Path -LiteralPath $relationshipsPath)) {
        Write-V2File -Path $relationshipsPath -Content "# Relationships`n" -Force
    }

    $runtimeEventsPath = Join-Path $projectPackRoot "memory/runtime-events"
    Initialize-V2Directory -Path $runtimeEventsPath
    $eventWindowSeconds = [int](Get-V2OptionalProperty -InputObject $ProjectState -Name "observer_runtime_event_window_seconds" -DefaultValue 300)
    if ($eventWindowSeconds -lt 30) {
        $eventWindowSeconds = 30
    }

    $eventStamp = Get-Date -Format "yyyyMMddHHmmssfff"
    $eventTimestamp = Get-V2Timestamp
    $eventNodePath = ""
    $eventMode = "new"

    $recentEventFiles = @(Get-ChildItem -LiteralPath $runtimeEventsPath -File -Filter "observer-*.md" -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
    if ($recentEventFiles.Count -gt 0) {
        $latestEvent = $recentEventFiles[0]
        $ageSeconds = ((Get-Date).ToUniversalTime() - $latestEvent.LastWriteTimeUtc).TotalSeconds
        if ($ageSeconds -ge 0 -and $ageSeconds -lt $eventWindowSeconds) {
            $eventNodePath = $latestEvent.FullName
            $eventMode = "reused-window"
            if ($latestEvent.BaseName -match "^observer-(?<stamp>\d+)$") {
                $eventStamp = $Matches["stamp"]
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($eventNodePath)) {
        $eventNodePath = Join-Path $runtimeEventsPath ("observer-{0}.md" -f $eventStamp)
    }

    $eventNodeContent = @(
        "# Node: runtime.observer.$eventStamp",
        "## Type: Metric",
        "## Created: $eventTimestamp",
        "## Last Updated: $eventTimestamp",
        "## Importance: 3",
        "## Project ID: $($intake.project_slug)",
        "",
        "## Summary",
        "- Observer runtime cycle persisted to memory backends.",
        "",
        "## Details",
        "- project_slug: $($intake.project_slug)",
        "- observer_cycle: $eventTimestamp",
        "- source: Invoke-ObserverV2",
        "- metric_write_mode: $eventMode",
        "- dedup_window_seconds: $eventWindowSeconds",
        "",
        "## Tags",
        "- runtime",
        "- observer",
        "- memory-sync",
        "",
        "## Links",
        "- related_to: [project::$($intake.project_slug)] - observer runtime event"
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText($eventNodePath, $eventNodeContent)

    $eventFiles = @(Get-ChildItem -LiteralPath $runtimeEventsPath -File -Filter "observer-*.md" -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
    if ($eventFiles.Count -gt 200) {
        foreach ($old in $eventFiles[200..($eventFiles.Count - 1)]) {
            Remove-Item -LiteralPath $old.FullName -Force -ErrorAction SilentlyContinue
        }
    }

    $syncArgs = @(
        $MemorySyncScript,
        "--project-slug", $intake.project_slug,
        "--project-root", $ProjectRoot,
        "--memory-dir", (Join-Path $projectPackRoot "memory"),
        "--relationships-path", $relationshipsPath
    )
    $taskDagPath = Join-Path $OrchestratorRoot "tasks/task-dag.json"
    $taskCompletionsDir = Join-Path $OrchestratorRoot "tasks/completions"
    if (Test-Path -LiteralPath $taskDagPath -PathType Leaf) {
        $syncArgs += @("--task-dag-path", $taskDagPath)
    }
    if (Test-Path -LiteralPath $taskCompletionsDir -PathType Container) {
        $syncArgs += @("--task-completions-dir", $taskCompletionsDir)
    }
    $dbConnections = Get-V2OptionalProperty -InputObject $ProjectState -Name "databases" -DefaultValue ([PSCustomObject]@{})
    $neo4jConn = Get-V2OptionalProperty -InputObject $dbConnections -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
    $qdrantConn = Get-V2OptionalProperty -InputObject $dbConnections -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
    $neo4jUri = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "uri" -DefaultValue "")
    $neo4jUser = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "user" -DefaultValue "")
    $neo4jPassword = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "password" -DefaultValue "")
    if (Test-V2MaskedSecretValue -Value $neo4jPassword) {
        $vaultPassword = Get-V2ObserverVaultSecret -ProjectRoot $ProjectRoot -ProjectState $ProjectState -Domain "neo4j" -Key "password"
        if (-not [string]::IsNullOrWhiteSpace($vaultPassword)) {
            $neo4jPassword = $vaultPassword
        }
    }
    $neo4jDatabase = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "database" -DefaultValue "")
    $qdrantHost = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "host" -DefaultValue "")
    $qdrantPort = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "port" -DefaultValue "")
    $qdrantPrefix = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "collection_prefix" -DefaultValue "")
    $gpuPolicy = Get-V2ObserverGpuPolicy -ProjectRoot $ProjectRoot
    $gpuPolicyEnabled = [bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "enabled" -DefaultValue $true)
    $ollamaCfg = Get-V2OptionalProperty -InputObject $gpuPolicy -Name "ollama" -DefaultValue ([PSCustomObject]@{})
    $ollamaEmbedUrl = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_url" -DefaultValue "http://localhost:11434/v1/embeddings")
    $ollamaEmbedModel = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_model" -DefaultValue "all-minilm:latest")
    $ollamaModelSelection = Resolve-V2ObserverOllamaEmbedModel -OllamaConfig $ollamaCfg
    $resolvedOllamaEmbedModel = [string](Get-V2OptionalProperty -InputObject $ollamaModelSelection -Name "selected_model" -DefaultValue $ollamaEmbedModel)
    if (-not [string]::IsNullOrWhiteSpace($resolvedOllamaEmbedModel)) {
        $ollamaEmbedModel = $resolvedOllamaEmbedModel
    }
    $ollamaKeepAlive = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "keep_alive" -DefaultValue "10m")
    $ollamaBatchSize = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_batch_size" -DefaultValue 24)
    $ollamaBatchSizeAuto = [bool](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_batch_size_auto" -DefaultValue $true)
    $ollamaConcurrency = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_concurrency" -DefaultValue 4)
    $ollamaWarmupInputs = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "embed_warmup_inputs" -DefaultValue 32)
    $ollamaVramReserveMb = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "vram_reserve_mb" -DefaultValue 3072)
    if ($ollamaVramReserveMb -lt 512) { $ollamaVramReserveMb = 512 }
    $ollamaGpuOverheadBytes = [int64]$ollamaVramReserveMb * 1MB
    $ollamaMaxLoadedModels = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "max_loaded_models" -DefaultValue 2)
    if ($ollamaMaxLoadedModels -lt 1) { $ollamaMaxLoadedModels = 1 }
    $ollamaNumParallel = [int](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "num_parallel" -DefaultValue 2)
    if ($ollamaNumParallel -lt 1) { $ollamaNumParallel = 1 }
    $ollamaNumGpu = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "num_gpu" -DefaultValue "1")
    $ollamaNumThread = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "num_thread" -DefaultValue "4")
    $cudaDevices = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "cuda_devices" -DefaultValue "0")
    $nvidiaDevices = [string](Get-V2OptionalProperty -InputObject $ollamaCfg -Name "nvidia_devices" -DefaultValue "all")
    if (Test-Path -LiteralPath $ProjectStatePath) {
        $syncArgs += @("--dependency-graph-path", $ProjectStatePath)
    }
    if (Test-Path -LiteralPath $WorldModelJsonPath) {
        $syncArgs += @("--world-model-json-path", $WorldModelJsonPath)
    }
    if (-not [string]::IsNullOrWhiteSpace($neo4jUri)) { $syncArgs += @("--neo4j-uri", $neo4jUri) }
    if (-not [string]::IsNullOrWhiteSpace($neo4jUser)) { $syncArgs += @("--neo4j-user", $neo4jUser) }
    if (-not [string]::IsNullOrWhiteSpace($neo4jPassword)) { $syncArgs += @("--neo4j-password", $neo4jPassword) }
    if (-not [string]::IsNullOrWhiteSpace($neo4jDatabase)) { $syncArgs += @("--neo4j-database", $neo4jDatabase) }
    if (-not [string]::IsNullOrWhiteSpace($qdrantHost)) { $syncArgs += @("--qdrant-host", $qdrantHost) }
    if (-not [string]::IsNullOrWhiteSpace($qdrantPort)) { $syncArgs += @("--qdrant-port", $qdrantPort) }
    if (-not [string]::IsNullOrWhiteSpace($qdrantPrefix)) { $syncArgs += @("--collection-prefix", $qdrantPrefix) }
    if (-not [string]::IsNullOrWhiteSpace($ollamaEmbedUrl)) { $syncArgs += @("--ollama-url", $ollamaEmbedUrl) }
    if (-not [string]::IsNullOrWhiteSpace($ollamaEmbedModel)) { $syncArgs += @("--ollama-model", $ollamaEmbedModel) }
    if (-not [string]::IsNullOrWhiteSpace($ollamaKeepAlive)) { $syncArgs += @("--ollama-keep-alive", $ollamaKeepAlive) }
    if ($ollamaBatchSize -gt 0) { $syncArgs += @("--ollama-embed-batch-size", [string]$ollamaBatchSize) }
    if ($ollamaBatchSizeAuto) { $syncArgs += "--ollama-embed-batch-size-auto" }
    if ($ollamaConcurrency -gt 0) { $syncArgs += @("--ollama-embed-concurrency", [string]$ollamaConcurrency) }
    if ($ollamaWarmupInputs -ge 0) { $syncArgs += @("--ollama-embed-warmup-inputs", [string]$ollamaWarmupInputs) }
    if (-not $IncludeNeo4j) { $syncArgs += "--skip-neo4j" }
    if (-not $IncludeQdrant) { $syncArgs += "--skip-qdrant" }

    $previousProjectPackRoot = [string]$env:PROJECT_PACK_ROOT
    $previousOllamaEmbedUrl = [string]$env:OLLAMA_EMBED_URL
    $previousOllamaEmbedModel = [string]$env:OLLAMA_EMBED_MODEL
    $previousOllamaKeepAlive = [string]$env:OLLAMA_KEEP_ALIVE
    $previousOllamaEmbedBatchSize = [string]$env:OLLAMA_EMBED_BATCH_SIZE
    $previousOllamaEmbedBatchSizeAuto = [string]$env:OLLAMA_EMBED_BATCH_SIZE_AUTO
    $previousOllamaEmbedConcurrency = [string]$env:OLLAMA_EMBED_CONCURRENCY
    $previousOllamaEmbedWarmupInputs = [string]$env:OLLAMA_EMBED_WARMUP_INPUTS
    $previousOllamaNumGpu = [string]$env:OLLAMA_NUM_GPU
    $previousOllamaNumThread = [string]$env:OLLAMA_NUM_THREAD
    $previousOllamaGpuOverhead = [string]$env:OLLAMA_GPU_OVERHEAD
    $previousOllamaNumParallel = [string]$env:OLLAMA_NUM_PARALLEL
    $previousOllamaMaxLoadedModels = [string]$env:OLLAMA_MAX_LOADED_MODELS
    $previousGpuVramReserveMb = [string]$env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB
    $previousCudaVisibleDevices = [string]$env:CUDA_VISIBLE_DEVICES
    $previousNvidiaVisibleDevices = [string]$env:NVIDIA_VISIBLE_DEVICES
    try {
        $env:PROJECT_PACK_ROOT = $projectPackRoot
        if ($gpuPolicyEnabled) {
            if (-not [string]::IsNullOrWhiteSpace($ollamaEmbedUrl)) { $env:OLLAMA_EMBED_URL = $ollamaEmbedUrl }
            if (-not [string]::IsNullOrWhiteSpace($ollamaEmbedModel)) { $env:OLLAMA_EMBED_MODEL = $ollamaEmbedModel }
            if (-not [string]::IsNullOrWhiteSpace($ollamaKeepAlive)) { $env:OLLAMA_KEEP_ALIVE = $ollamaKeepAlive }
            if ($ollamaBatchSize -gt 0) { $env:OLLAMA_EMBED_BATCH_SIZE = [string]$ollamaBatchSize }
            $env:OLLAMA_EMBED_BATCH_SIZE_AUTO = if ($ollamaBatchSizeAuto) { "1" } else { "0" }
            if ($ollamaConcurrency -gt 0) { $env:OLLAMA_EMBED_CONCURRENCY = [string]$ollamaConcurrency }
            if ($ollamaWarmupInputs -ge 0) { $env:OLLAMA_EMBED_WARMUP_INPUTS = [string]$ollamaWarmupInputs }
            if (-not [string]::IsNullOrWhiteSpace($ollamaNumGpu)) { $env:OLLAMA_NUM_GPU = $ollamaNumGpu }
            if (-not [string]::IsNullOrWhiteSpace($ollamaNumThread)) { $env:OLLAMA_NUM_THREAD = $ollamaNumThread }
            $env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB = [string]$ollamaVramReserveMb
            $env:OLLAMA_GPU_OVERHEAD = [string]$ollamaGpuOverheadBytes
            $env:OLLAMA_NUM_PARALLEL = [string]$ollamaNumParallel
            $env:OLLAMA_MAX_LOADED_MODELS = [string]$ollamaMaxLoadedModels
            if (-not [string]::IsNullOrWhiteSpace($cudaDevices)) { $env:CUDA_VISIBLE_DEVICES = $cudaDevices }
            if (-not [string]::IsNullOrWhiteSpace($nvidiaDevices)) { $env:NVIDIA_VISIBLE_DEVICES = $nvidiaDevices }
        }
        $syncExec = Invoke-V2ExternalCommandWithTimeout `
            -FilePath "python" `
            -ArgumentList @($syncArgs) `
            -WorkingDirectory $ProjectRoot `
            -TimeoutSeconds $TimeoutSeconds
        if ([bool](Get-V2OptionalProperty -InputObject $syncExec -Name "timed_out" -DefaultValue $false)) {
            throw "memory_sync.py timeout after ${TimeoutSeconds}s"
        }
        $syncExitCode = [int](Get-V2OptionalProperty -InputObject $syncExec -Name "exit_code" -DefaultValue 1)
        $syncOutputText = [string](Get-V2OptionalProperty -InputObject $syncExec -Name "output" -DefaultValue "")
        if ($syncExitCode -ne 0) {
            $tail = ($syncOutputText -split "(`r`n|`n|`r)" | Select-Object -Last 30) -join [Environment]::NewLine
            throw "memory_sync.py failed with exit code $syncExitCode. $tail"
        }
        $syncJson = $syncOutputText.Trim()
        if ([string]::IsNullOrWhiteSpace($syncJson)) {
            throw "memory_sync.py returned empty output."
        }
        try {
            $syncResult = ($syncJson | ConvertFrom-Json)
            $syncQdrant = Get-V2OptionalProperty -InputObject $syncResult -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "gpu_policy_enabled" -Value $gpuPolicyEnabled
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "gpu_policy_expected_processor" -Value ([string](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "expected_processor" -DefaultValue "gpu"))
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "gpu_policy_require_gpu" -Value ([bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "require_gpu_for_embeddings" -DefaultValue $true))
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_model_selected" -Value $ollamaEmbedModel
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "gpu_vram_reserve_mb" -Value $ollamaVramReserveMb
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_gpu_overhead_bytes" -Value $ollamaGpuOverheadBytes
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_num_parallel" -Value $ollamaNumParallel
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_max_loaded_models" -Value $ollamaMaxLoadedModels
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_model_selection_candidates" -Value (@(Get-V2OptionalProperty -InputObject $ollamaModelSelection -Name "candidates" -DefaultValue @()))
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_model_install_attempted" -Value ([bool](Get-V2OptionalProperty -InputObject $ollamaModelSelection -Name "install_attempted" -DefaultValue $false))
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_model_install_succeeded" -Value ([bool](Get-V2OptionalProperty -InputObject $ollamaModelSelection -Name "install_succeeded" -DefaultValue $false))
            Set-V2DynamicProperty -InputObject $syncQdrant -Name "ollama_model_install_error" -Value ([string](Get-V2OptionalProperty -InputObject $ollamaModelSelection -Name "install_error" -DefaultValue ""))
            Set-V2DynamicProperty -InputObject $syncResult -Name "qdrant" -Value $syncQdrant
            return $syncResult
        }
        catch {
            throw "memory_sync.py returned non-JSON output: $syncJson"
        }
    }
    finally {
        if ([string]::IsNullOrWhiteSpace($previousProjectPackRoot)) {
            Remove-Item -Path Env:PROJECT_PACK_ROOT -ErrorAction SilentlyContinue
        }
        else {
            $env:PROJECT_PACK_ROOT = $previousProjectPackRoot
        }

        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedUrl)) { Remove-Item -Path Env:OLLAMA_EMBED_URL -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_URL = $previousOllamaEmbedUrl }
        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedModel)) { Remove-Item -Path Env:OLLAMA_EMBED_MODEL -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_MODEL = $previousOllamaEmbedModel }
        if ([string]::IsNullOrWhiteSpace($previousOllamaKeepAlive)) { Remove-Item -Path Env:OLLAMA_KEEP_ALIVE -ErrorAction SilentlyContinue } else { $env:OLLAMA_KEEP_ALIVE = $previousOllamaKeepAlive }
        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedBatchSize)) { Remove-Item -Path Env:OLLAMA_EMBED_BATCH_SIZE -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_BATCH_SIZE = $previousOllamaEmbedBatchSize }
        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedBatchSizeAuto)) { Remove-Item -Path Env:OLLAMA_EMBED_BATCH_SIZE_AUTO -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_BATCH_SIZE_AUTO = $previousOllamaEmbedBatchSizeAuto }
        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedConcurrency)) { Remove-Item -Path Env:OLLAMA_EMBED_CONCURRENCY -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_CONCURRENCY = $previousOllamaEmbedConcurrency }
        if ([string]::IsNullOrWhiteSpace($previousOllamaEmbedWarmupInputs)) { Remove-Item -Path Env:OLLAMA_EMBED_WARMUP_INPUTS -ErrorAction SilentlyContinue } else { $env:OLLAMA_EMBED_WARMUP_INPUTS = $previousOllamaEmbedWarmupInputs }
        if ([string]::IsNullOrWhiteSpace($previousOllamaNumGpu)) { Remove-Item -Path Env:OLLAMA_NUM_GPU -ErrorAction SilentlyContinue } else { $env:OLLAMA_NUM_GPU = $previousOllamaNumGpu }
        if ([string]::IsNullOrWhiteSpace($previousOllamaNumThread)) { Remove-Item -Path Env:OLLAMA_NUM_THREAD -ErrorAction SilentlyContinue } else { $env:OLLAMA_NUM_THREAD = $previousOllamaNumThread }
        if ([string]::IsNullOrWhiteSpace($previousOllamaGpuOverhead)) { Remove-Item -Path Env:OLLAMA_GPU_OVERHEAD -ErrorAction SilentlyContinue } else { $env:OLLAMA_GPU_OVERHEAD = $previousOllamaGpuOverhead }
        if ([string]::IsNullOrWhiteSpace($previousOllamaNumParallel)) { Remove-Item -Path Env:OLLAMA_NUM_PARALLEL -ErrorAction SilentlyContinue } else { $env:OLLAMA_NUM_PARALLEL = $previousOllamaNumParallel }
        if ([string]::IsNullOrWhiteSpace($previousOllamaMaxLoadedModels)) { Remove-Item -Path Env:OLLAMA_MAX_LOADED_MODELS -ErrorAction SilentlyContinue } else { $env:OLLAMA_MAX_LOADED_MODELS = $previousOllamaMaxLoadedModels }
        if ([string]::IsNullOrWhiteSpace($previousGpuVramReserveMb)) { Remove-Item -Path Env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB -ErrorAction SilentlyContinue } else { $env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB = $previousGpuVramReserveMb }
        if ([string]::IsNullOrWhiteSpace($previousCudaVisibleDevices)) { Remove-Item -Path Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue } else { $env:CUDA_VISIBLE_DEVICES = $previousCudaVisibleDevices }
        if ([string]::IsNullOrWhiteSpace($previousNvidiaVisibleDevices)) { Remove-Item -Path Env:NVIDIA_VISIBLE_DEVICES -ErrorAction SilentlyContinue } else { $env:NVIDIA_VISIBLE_DEVICES = $previousNvidiaVisibleDevices }
    }
}

function Invoke-V2TaskStateDbSync {
    param(
        [string]$ProjectRoot,
        [string]$TaskDagPath,
        [string]$TaskStateDbScriptPath
    )

    if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
        throw "task-state-db-sync-project-root-required"
    }
    if (-not (Test-Path -LiteralPath $TaskStateDbScriptPath -PathType Leaf)) {
        throw "task-state-db-sync-script-missing"
    }

    $statusRaw = @(python $TaskStateDbScriptPath --project-path $ProjectRoot --mode status --emit-json 2>&1)
    $statusExit = $LASTEXITCODE
    $statusJson = $null
    if ($statusExit -eq 0) {
        $statusPayload = (($statusRaw -join [Environment]::NewLine).Trim())
        if (-not [string]::IsNullOrWhiteSpace($statusPayload)) {
            try {
                $statusJson = ($statusPayload | ConvertFrom-Json -ErrorAction Stop)
            }
            catch {
                $statusJson = $null
            }
        }
    }

    $statusOk = [bool](Get-V2OptionalProperty -InputObject $statusJson -Name "ok" -DefaultValue $false)
    $backendMode = [string](Get-V2OptionalProperty -InputObject $statusJson -Name "backend_mode" -DefaultValue "")

    if (-not (Test-Path -LiteralPath $TaskDagPath -PathType Leaf)) {
        throw "task-state-db-sync-task-dag-missing"
    }

    if ($statusOk -and $backendMode -eq "db-primary-v1") {
        $dagDoc = Get-V2JsonContent -Path $TaskDagPath
        if (-not $dagDoc -or -not ($dagDoc.PSObject.Properties.Name -contains "tasks")) {
            Set-V2DynamicProperty -InputObject $statusJson -Name "sync_mode" -Value "status-only-db-primary-no-task-list"
            return $statusJson
        }

        $orchestratorRoot = Join-Path $ProjectRoot "ai-orchestrator"
        Initialize-V2Directory -Path (Join-Path $orchestratorRoot "state")
        $tasksBufferPath = Join-Path $orchestratorRoot ("state/observer-db-sync-buffer-{0}.json" -f ([Guid]::NewGuid().ToString("N")))
        try {
            Save-V2JsonContent -Path $tasksBufferPath -Value @($dagDoc.tasks)

            $writeOutput = @(python $TaskStateDbScriptPath --project-path $ProjectRoot --mode write-tasks --tasks-json-path $tasksBufferPath --emit-json 2>&1)
            if ($LASTEXITCODE -ne 0) {
                $tail = ($writeOutput | Select-Object -Last 20) -join [Environment]::NewLine
                throw "task-state-db-write-failed: exit=$LASTEXITCODE output=$tail"
            }

            $flushOutput = @(python $TaskStateDbScriptPath --project-path $ProjectRoot --mode flush-dag --emit-json 2>&1)
            if ($LASTEXITCODE -ne 0) {
                $tail = ($flushOutput | Select-Object -Last 20) -join [Environment]::NewLine
                throw "task-state-db-flush-failed: exit=$LASTEXITCODE output=$tail"
            }

            $writeJson = (($writeOutput -join [Environment]::NewLine).Trim())
            if ([string]::IsNullOrWhiteSpace($writeJson)) {
                Set-V2DynamicProperty -InputObject $statusJson -Name "sync_mode" -Value "db-primary-write-empty-output"
                return $statusJson
            }
            try {
                $writeResult = ($writeJson | ConvertFrom-Json -ErrorAction Stop)
                Set-V2DynamicProperty -InputObject $writeResult -Name "sync_mode" -Value "json-to-db-primary-write-tasks"
                return $writeResult
            }
            catch {
                throw "task-state-db-write-non-json-output: $writeJson"
            }
        }
        finally {
            Remove-Item -LiteralPath $tasksBufferPath -Force -ErrorAction SilentlyContinue
        }
    }

    $syncOutput = @(python $TaskStateDbScriptPath --project-path $ProjectRoot --mode sync --emit-json 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $tail = ($syncOutput | Select-Object -Last 20) -join [Environment]::NewLine
        throw "task-state-db-sync-failed: exit=$LASTEXITCODE output=$tail"
    }

    $rawJson = (($syncOutput -join [Environment]::NewLine).Trim())
    if ([string]::IsNullOrWhiteSpace($rawJson)) {
        throw "task-state-db-sync-empty-output"
    }

    try {
        $syncResult = ($rawJson | ConvertFrom-Json -ErrorAction Stop)
        Set-V2DynamicProperty -InputObject $syncResult -Name "sync_mode" -Value "json-to-db"
        return $syncResult
    }
    catch {
        throw "task-state-db-sync-non-json-output: $rawJson"
    }
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-observer"

$initScript = Join-Path $PSScriptRoot "Initialize-AIOrchestratorLayer.ps1"
& $initScript -ProjectPath $resolvedProjectPath | Out-Null

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
$stateDirectory = Join-Path $orchestratorRoot "state"
$reportsDirectory = Join-Path $orchestratorRoot "reports"
$selfHealingPath = Join-Path $orchestratorRoot "analysis/self-healing.md"
$backlogPath = Join-Path $orchestratorRoot "tasks/backlog.md"
$taskDagJsonPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$projectStatePath = Join-Path $stateDirectory "project-state.json"
$observerStatePath = Join-Path $stateDirectory "observer-state.json"
$healthReportJsonPath = Join-Path $stateDirectory "health-report.json"
$healthReportMdPath = Join-Path $stateDirectory "health-report.md"
$intakeReportPath = Join-Path $stateDirectory "intake-report.md"
$openQuestionsPath = Join-Path $stateDirectory "open-questions.md"
$worldModelMdPath = Join-Path $orchestratorRoot "memory/world-model.md"
$worldModelJsonPath = Join-Path $stateDirectory "world-model-auto.json"

$intakeScript = Join-Path $PSScriptRoot "Invoke-UniversalIntakeV2.ps1"
$worldModelScript = Join-Path (Split-Path -Parent $PSScriptRoot) "extract_world_model.py"
$memorySyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "memory_sync.py"
$taskStateDbScript = Join-Path $PSScriptRoot "task_state_db.py"
$dependencyGraphScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Generate-DependencyGraph.py"
$mergeWorldModelScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Merge-WorldModel.ps1"
$compressionScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Invoke-LongTermCompression.ps1"
$securityScanScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Run-OwaspSecurityScan.ps1"
$schedulerScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
$runtimeObservabilityScript = Join-Path $PSScriptRoot "Invoke-RuntimeObservabilityV2.ps1"
$commanderDashboardScript = Join-Path $PSScriptRoot "Invoke-CommanderDashboardV2.ps1"
$policyEnforcerScript = Join-Path $PSScriptRoot "Invoke-PolicyEnforcer.ps1"
$tenantIsolationScript = Join-Path $PSScriptRoot "Check-TenantIsolationPolicy.ps1"
$script:ObserverIncidentDedupPath = Join-Path $stateDirectory "incident-dedup-state.json"
$script:ObserverIncidentDedupCategories = @("commander_dashboard", "runtime_observability")
$script:ObserverIncidentDedupCooldownSeconds = [Math]::Max($IncidentDedupCooldownSeconds, 0)
$HeavyCycleCadence = [Math]::Max($HeavyCycleCadence, 1)
$HeavyStageTimeoutSeconds = [Math]::Max($HeavyStageTimeoutSeconds, 60)

Write-Host "--- Universal Orchestrator V2 Observer ---" -ForegroundColor Cyan
Write-Host "Project path: $resolvedProjectPath"
Write-Host "State path: $stateDirectory"

while ($true) {
    $timestamp = Get-V2Timestamp
    Write-Host "[$timestamp] Checking project drift..." -ForegroundColor Gray

    $projectState = Get-V2JsonContent -Path $projectStatePath
    $refactorPolicy = [string](Get-V2OptionalProperty -InputObject $projectState -Name "refactor_policy" -DefaultValue "unknown")
    $observerIncludeNeo4j = if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) {
        [bool]$IncludeNeo4j
    }
    else {
        [bool](Get-V2OptionalProperty -InputObject $projectState -Name "include_neo4j" -DefaultValue $true)
    }
    $observerIncludeQdrant = if ($PSBoundParameters.ContainsKey("IncludeQdrant")) {
        [bool]$IncludeQdrant
    }
    else {
        [bool](Get-V2OptionalProperty -InputObject $projectState -Name "include_qdrant" -DefaultValue $true)
    }
    $gpuPolicy = Get-V2ObserverGpuPolicy -ProjectRoot $resolvedProjectPath
    $gpuPolicyEnabled = [bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "enabled" -DefaultValue $true)
    $gpuRequireEmbeddings = [bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "require_gpu_for_embeddings" -DefaultValue $true)
    $gpuRepairOnCpuFallback = [bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "repair_on_cpu_fallback" -DefaultValue $true)
    $gpuExpectedProcessor = [string](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "expected_processor" -DefaultValue "gpu")
    $gpuUnknownProcessorFailure = [bool](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "unknown_processor_is_failure" -DefaultValue $false)
    $gpuMaxNonOllamaRatioPercent = [double](Get-V2OptionalProperty -InputObject $gpuPolicy -Name "max_non_ollama_ratio_percent" -DefaultValue 20)

    try {
        $intakeArgs = @("-ProjectPath", $resolvedProjectPath, "-OutputPath", $stateDirectory, "-RefactorPolicy", $refactorPolicy, "-EmitJson")
        if (-not [string]::IsNullOrWhiteSpace($AuditFindingPath)) {
            $intakeArgs += @("-AuditFindingPath", $AuditFindingPath)
        }
        $intakeJson = & $intakeScript @intakeArgs
        $intake = ($intakeJson | Out-String) | ConvertFrom-Json
    }
    catch {
        $incident = New-IncidentReport -ReportDirectory $reportsDirectory -Category "intake" -Title "V2 intake failed" -Details $_.Exception.Message -DedupPath $script:ObserverIncidentDedupPath -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds -DedupCategories $script:ObserverIncidentDedupCategories
        Write-Warning "Intake failed. Incident created: $incident"
        if ($RunOnce) { break }
        Start-Sleep -Seconds $IntervalSeconds
        continue
    }

    $observerState = Get-V2JsonContent -Path $observerStatePath
    $fingerprint = [string]$intake.technical_fingerprint.hash
    $previousCycleIndex = [int](Get-V2OptionalProperty -InputObject $observerState -Name "cycle_index" -DefaultValue 0)
    if ($previousCycleIndex -lt 0) { $previousCycleIndex = 0 }
    $observerCycleIndex = $previousCycleIndex + 1
    $projectHeavyCadence = [int](Get-V2OptionalProperty -InputObject $projectState -Name "observer_heavy_cycle_cadence" -DefaultValue $HeavyCycleCadence)
    if ($projectHeavyCadence -lt 1) { $projectHeavyCadence = 1 }
    $incidents = New-Object System.Collections.Generic.List[string]
    $checkResults = New-Object System.Collections.Generic.List[object]
    $memorySyncResult = $null
    $taskStateDbSyncResult = $null
    $healthStatus = "healthy"

    # ── Loop heartbeat dead-detection ─────────────────────────────────────────────────────────
    try {
        $loopStatePath = Join-Path $stateDirectory "loop-state.json"
        $loopState = Get-V2JsonContent -Path $loopStatePath
        if ($loopState) {
            $loopWasRunning  = [bool](Get-V2OptionalProperty -InputObject $loopState -Name "loop_running" -DefaultValue $false)
            $loopHeartbeatTs = [string](Get-V2OptionalProperty -InputObject $loopState -Name "generated_at" -DefaultValue "")
            $loopInterval    = [int](Get-V2OptionalProperty   -InputObject $loopState -Name "interval_seconds" -DefaultValue 300)
            $loopPid         = [int](Get-V2OptionalProperty   -InputObject $loopState -Name "pid" -DefaultValue 0)

            if ($loopWasRunning -and -not [string]::IsNullOrEmpty($loopHeartbeatTs)) {
                $heartbeatAge = ([DateTime]::UtcNow - [DateTime]::Parse($loopHeartbeatTs).ToUniversalTime()).TotalSeconds
                $deadThreshold = [Math]::Max($loopInterval * 3, 120)

                if ($heartbeatAge -gt $deadThreshold) {
                    $deadDetail = "Loop heartbeat is {0}s old (threshold {1}s). PID was {2}. The autonomous loop has silently died." -f [int]$heartbeatAge, $deadThreshold, $loopPid
                    if ($healthStatus -eq "healthy") { $healthStatus = "degraded" }
                    $incidents.Add((New-IncidentReport `
                        -ReportDirectory $reportsDirectory `
                        -Category "loop-dead" `
                        -Title "Autonomous loop heartbeat expired" `
                        -Details $deadDetail))
                    Write-Warning "[Observer] $deadDetail"
                }
            }
        }
    }
    catch {
        # Non-fatal — heartbeat check is best-effort
    }
    # ─────────────────────────────────────────────────────────────────────────────────────────
    $qdrantFallbackDeferredCycles = 0
    $qdrantFallbackDeferredMaxCycles = [int](Get-V2OptionalProperty -InputObject $projectState -Name "qdrant_fallback_deferred_max_cycles" -DefaultValue 6)
    if ($qdrantFallbackDeferredMaxCycles -lt 1) {
        $qdrantFallbackDeferredMaxCycles = 1
    }
    $fingerprintUnchanged = ($observerState -and $observerState.last_fingerprint -eq $fingerprint -and -not $RunOnce)
    $runHeavyCycle = (
        $RunOnce -or
        -not $EnableFastSlowPath -or
        -not $fingerprintUnchanged -or
        (($observerCycleIndex % $projectHeavyCadence) -eq 0)
    )
    $observerCycleMode = if ($runHeavyCycle) { "heavy" } else { "fast" }
    if ($fingerprintUnchanged) {
        Write-Host "No fingerprint change detected. Skipping drift sync and continuing health checks."
    }
    if ($observerCycleMode -eq "fast") {
        Write-Host ("[Observer] Fast path cycle #{0} (heavy cadence={1})" -f $observerCycleIndex, $projectHeavyCadence) -ForegroundColor DarkGray
    }

    # ── Walkthrough Feedback Loop ─────────────────────────────────────────────────────────────
    if ($runHeavyCycle) {
        Sync-V2WalkthroughLessons -ProjectRoot $resolvedProjectPath -LessonsDirectory (Join-Path $orchestratorRoot "knowledge_base/lessons_learned")
        
        # ── Policy Enforcement & Semantic Security ──────────────────────────────────────────
        if (Test-Path -LiteralPath $policyEnforcerScript -PathType Leaf) {
            try {
                $policyJson = & $policyEnforcerScript -ProjectPath $resolvedProjectPath -EmitJson
                $policyReport = ($policyJson | Out-String) | ConvertFrom-Json
                if ($policyReport.policy_compliance_score -lt 80) {
                    if ($healthStatus -eq "healthy") { $healthStatus = "degraded" }
                    foreach ($finding in $policyReport.findings) {
                        $incidents.Add((New-IncidentReport `
                            -ReportDirectory $reportsDirectory `
                            -Category ("policy-" + $finding.level.ToLower()) `
                            -Title $finding.rule `
                            -Details $finding.evidence `
                            -CommandText "Invoke-PolicyEnforcer.ps1" `
                            -OutputText $finding.recommendation))
                    }
                }
            } catch { }
        }

        if (Test-Path -LiteralPath $tenantIsolationScript -PathType Leaf) {
            try {
                $isolationJson = & $tenantIsolationScript -ProjectPath $resolvedProjectPath -EmitJson
                $isolationReport = ($isolationJson | Out-String) | ConvertFrom-Json
                if ($isolationReport.finding_count -gt 0) {
                    $healthStatus = "unhealthy"
                    foreach ($finding in $isolationReport.findings) {
                        $incidentPath = New-IncidentReport `
                            -ReportDirectory $reportsDirectory `
                            -Category $finding.category `
                            -Title $finding.title `
                            -Details $finding.evidence `
                            -CommandText "Check-TenantIsolationPolicy.ps1" `
                            -OutputText $finding.recommendation
                        $incidents.Add($incidentPath)
                        Add-RepairTask `
                            -SelfHealingPath $selfHealingPath `
                            -BacklogPath $backlogPath `
                            -IncidentPath $incidentPath `
                            -Reason $finding.title `
                            -TaskDagJsonPath $taskDagJsonPath
                    }
                }
            } catch { }
        }
    }

    # ── File-level change detection: which source files changed since last cycle ───────────────
    # Computes per-file SHA256 hashes, diffs against observer-state, then re-evaluates tasks
    # whose affected_files overlap with the changed files.
    if ($runHeavyCycle) {
        try {
        $fileHashStatePath = Join-Path $stateDirectory "file-hashes.json"
        $prevFileHashes = @{}
        if (Test-Path -LiteralPath $fileHashStatePath -PathType Leaf) {
            $prevHashDoc = Get-V2JsonContent -Path $fileHashStatePath
            if ($prevHashDoc) {
                foreach ($prop in $prevHashDoc.PSObject.Properties) {
                    $prevFileHashes[$prop.Name] = [string]$prop.Value
                }
            }
        }

        # Hash all source files (skip binaries, large files, generated dirs)
        $sourceExtMap = @{ py = 1; ts = 1; js = 1; mjs = 1; php = 1; go = 1; cs = 1; java = 1; rb = 1; rs = 1; sql = 1 }
        $excludeDirs  = @("node_modules", "vendor", ".git", "__pycache__", "dist", "build", ".venv", "venv", "ai-orchestrator")
        $sha256       = [System.Security.Cryptography.SHA256]::Create()
        $currentHashes = @{}
        $changedFiles  = New-Object System.Collections.Generic.List[string]

        $sourceFiles = @(Get-ChildItem -LiteralPath $resolvedProjectPath -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $ext = $_.Extension.TrimStart('.').ToLower()
                if (-not $sourceExtMap.ContainsKey($ext)) { return $false }
                if ($_.Length -ge 512KB) { return $false }

                $fullPath = [string]$_.FullName
                foreach ($dirName in $excludeDirs) {
                    $pattern = "[\\/]" + [regex]::Escape([string]$dirName) + "([\\/]|$)"
                    if ($fullPath -match $pattern) {
                        return $false
                    }
                }
                return $true
            } | Select-Object -First 500)

        foreach ($sf in $sourceFiles) {
            try {
                $relPath = $sf.FullName.Substring($resolvedProjectPath.Length).TrimStart('\', '/') -replace '\\', '/'
                $bytes   = [System.IO.File]::ReadAllBytes($sf.FullName)
                $hash    = [Convert]::ToBase64String($sha256.ComputeHash($bytes))
                $currentHashes[$relPath] = $hash
                if (-not $prevFileHashes.ContainsKey($relPath) -or $prevFileHashes[$relPath] -ne $hash) {
                    $changedFiles.Add($relPath)
                }
            }
            catch { }
        }
        $sha256.Dispose()

        # Persist current hashes
        $hashDoc = [PSCustomObject]@{}
        foreach ($kv in $currentHashes.GetEnumerator()) {
            Add-Member -InputObject $hashDoc -MemberType NoteProperty -Name $kv.Key -Value $kv.Value -Force
        }
        Save-V2JsonContent -Path $fileHashStatePath -Value $hashDoc

        if ($changedFiles.Count -gt 0) {
            Write-Host ("[Observer] {0} source file(s) changed since last cycle." -f $changedFiles.Count)

            # Find tasks whose affected_files overlap with changed files → flag as needing recheck
            if (Test-Path -LiteralPath $taskDagJsonPath -PathType Leaf) {
                $dagForChange = Get-V2JsonContent -Path $taskDagJsonPath
                $recheckCount = 0
                foreach ($task in @($dagForChange.tasks)) {
                    $tStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    if ($tStatus -notin @("done", "skipped")) { continue }  # only recheck completed tasks

                    $affectedFiles = @(Get-V2OptionalProperty -InputObject $task -Name "affected_files" -DefaultValue @())
                    if ($affectedFiles.Count -eq 0) { continue }

                    $overlap = @($changedFiles | Where-Object {
                        $cf = $_
                        @($affectedFiles | Where-Object { $cf -like "*$_*" -or $_ -like "*$cf*" }).Count -gt 0
                    })

                    if ($overlap.Count -gt 0) {
                        $tId    = [string](Get-V2OptionalProperty -InputObject $task -Name "id"    -DefaultValue "")
                        $tTitle = [string](Get-V2OptionalProperty -InputObject $task -Name "title" -DefaultValue $tId)
                        $alreadyHasRecheck = @($dagForChange.tasks | Where-Object {
                            [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -like "RECHECK-$tId*"
                        }).Count -gt 0
                        if (-not $alreadyHasRecheck -and -not [string]::IsNullOrWhiteSpace($tId)) {
                            $recheckId = "RECHECK-$tId"
                            $dagForChange.tasks += [PSCustomObject]@{
                                id              = $recheckId
                                title           = "Recheck: $tTitle (files changed)"
                                description     = "Files affecting this task changed: $($overlap -join ', '). Verify task output is still valid."
                                reason          = "file-change-detected"
                                priority        = "P2"
                                dependencies    = @($tId)
                                status          = "pending"
                                affected_files  = @($affectedFiles)
                                created_at      = Get-V2Timestamp
                                updated_at      = Get-V2Timestamp
                            }
                            $recheckCount++
                        }
                    }
                }
                if ($recheckCount -gt 0) {
                    Save-V2JsonContent -Path $taskDagJsonPath -Value $dagForChange
                    Write-Host ("[Observer] Created {0} RECHECK task(s) for changed files." -f $recheckCount)
                }
            }
        }
        }
        catch {
            # Non-fatal — file change detection is best-effort
        }
    }
    # ─────────────────────────────────────────────────────────────────────────────────────────

    if ($runHeavyCycle -and -not $fingerprintUnchanged) {
        try {
            $worldModelExec = Invoke-V2ExternalCommandWithTimeout `
                -FilePath "python" `
                -ArgumentList @($worldModelScript, "--project-path", $resolvedProjectPath, "--project-slug", [string]$intake.project_slug, "--output-path", $worldModelMdPath, "--json-output-path", $worldModelJsonPath) `
                -WorkingDirectory $resolvedProjectPath `
                -TimeoutSeconds $HeavyStageTimeoutSeconds
            if ([bool](Get-V2OptionalProperty -InputObject $worldModelExec -Name "timed_out" -DefaultValue $false)) {
                throw "world-model-timeout-after-${HeavyStageTimeoutSeconds}s"
            }
            $worldModelExit = [int](Get-V2OptionalProperty -InputObject $worldModelExec -Name "exit_code" -DefaultValue 1)
            if ($worldModelExit -ne 0) {
                throw ("world-model-exit-code-{0}" -f $worldModelExit)
            }
        }
        catch {
            $healthStatus = "degraded"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "world-model" -Title "World model update failed" -Details $_.Exception.Message))
        }

        if (Test-Path -LiteralPath $mergeWorldModelScript -PathType Leaf) {
            try {
                & $mergeWorldModelScript -ProjectPath $resolvedProjectPath | Out-Null
            }
            catch {
                $healthStatus = "degraded"
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "world-model-merge" -Title "SYSTEM_MAP merge failed" -Details $_.Exception.Message))
            }
        }

        if (Test-Path -LiteralPath $dependencyGraphScript -PathType Leaf) {
            try {
                $dependencyExec = Invoke-V2ExternalCommandWithTimeout `
                    -FilePath "python" `
                    -ArgumentList @($dependencyGraphScript, "--project-root", $resolvedProjectPath, "--output", "analysis/dependency_graph.json") `
                    -WorkingDirectory $resolvedProjectPath `
                    -TimeoutSeconds $HeavyStageTimeoutSeconds
                if ([bool](Get-V2OptionalProperty -InputObject $dependencyExec -Name "timed_out" -DefaultValue $false)) {
                    throw "dependency-graph-timeout-after-${HeavyStageTimeoutSeconds}s"
                }
                $dependencyExit = [int](Get-V2OptionalProperty -InputObject $dependencyExec -Name "exit_code" -DefaultValue 1)
                if ($dependencyExit -ne 0) {
                    throw ("dependency-graph-exit-code-{0}" -f $dependencyExit)
                }
            }
            catch {
                $healthStatus = "degraded"
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "dependency-graph" -Title "Dependency graph generation failed" -Details $_.Exception.Message))
            }
        }

        if (-not $SkipMemorySync) {
            try {
                $memorySyncResult = Invoke-V2ObserverMemorySync `
                    -ProjectRoot $resolvedProjectPath `
                    -OrchestratorRoot $orchestratorRoot `
                    -ProjectState $projectState `
                    -Intake $intake `
                    -MemorySyncScript $memorySyncScript `
                    -ProjectStatePath $projectStatePath `
                    -WorldModelJsonPath $worldModelJsonPath `
                    -TimeoutSeconds $HeavyStageTimeoutSeconds `
                    -IncludeNeo4j:$observerIncludeNeo4j `
                    -IncludeQdrant:$observerIncludeQdrant
            }
            catch {
                $healthStatus = "degraded"
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync" -Title "Memory sync failed" -Details $_.Exception.Message))
            }
        }
    }
    elseif ($runHeavyCycle -and -not $SkipMemorySync) {
        try {
            $memorySyncResult = Invoke-V2ObserverMemorySync `
                -ProjectRoot $resolvedProjectPath `
                -OrchestratorRoot $orchestratorRoot `
                -ProjectState $projectState `
                -Intake $intake `
                -MemorySyncScript $memorySyncScript `
                -ProjectStatePath $projectStatePath `
                -WorldModelJsonPath $worldModelJsonPath `
                -TimeoutSeconds $HeavyStageTimeoutSeconds `
                -IncludeNeo4j:$observerIncludeNeo4j `
                -IncludeQdrant:$observerIncludeQdrant
        }
        catch {
            $healthStatus = "degraded"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync" -Title "Memory sync failed" -Details $_.Exception.Message))
        }
    }
    else {
        Write-Host "[Observer] Heavy stages skipped this cycle by fast/slow policy." -ForegroundColor DarkGray
    }

    if (-not $SkipMemorySync -and $memorySyncResult) {
        $qdrantState = Get-V2OptionalProperty -InputObject $memorySyncResult -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
        $neo4jState = Get-V2OptionalProperty -InputObject $memorySyncResult -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
        $qdrantEnabled = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "enabled" -DefaultValue $false)
        $neo4jEnabled = [bool](Get-V2OptionalProperty -InputObject $neo4jState -Name "enabled" -DefaultValue $false)
        $qdrantSynced = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "nodes_synced" -DefaultValue 0)
        $neo4jSynced = [int](Get-V2OptionalProperty -InputObject $neo4jState -Name "nodes_synced" -DefaultValue 0)
        $qdrantFallbackHash = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "fallback_embeddings" -DefaultValue 0)
        $qdrantLocal = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "local_embeddings" -DefaultValue 0)
        $qdrantNonOllama = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "non_ollama_embeddings" -DefaultValue ($qdrantFallbackHash + $qdrantLocal))
        $qdrantOllama = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_embeddings" -DefaultValue ([Math]::Max(0, ($qdrantSynced - $qdrantNonOllama))))
        $qdrantEmbeddingRuntime = Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_runtime" -DefaultValue ([PSCustomObject]@{})
        $qdrantEmbeddingBatchSizeRequested = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_size_requested" -DefaultValue 0)
        $qdrantEmbeddingProcessor = [string](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_runtime_processor" -DefaultValue "")
        $qdrantEmbeddingBatchSize = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_size" -DefaultValue 0)
        $qdrantEmbeddingBatchSizeAuto = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_size_auto_enabled" -DefaultValue $false)
        $qdrantEmbeddingBatchSizeGpuMemoryMb = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_size_gpu_memory_mb" -DefaultValue 0)
        $qdrantEmbeddingConcurrency = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_concurrency" -DefaultValue 0)
        $qdrantEmbeddingBatchWorkers = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_workers_used" -DefaultValue 1)
        $qdrantEmbeddingBatchChunkSize = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_chunk_size" -DefaultValue 0)
        $qdrantEmbeddingBatchRequests = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_requests" -DefaultValue 0)
        $qdrantEmbeddingBatchItems = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_batch_items" -DefaultValue 0)
        $qdrantEmbeddingSingleRequests = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_single_requests" -DefaultValue 0)
        $qdrantEmbeddingWarmupInputs = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_warmup_inputs" -DefaultValue 0)
        $qdrantEmbeddingWarmupRan = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_warmup_ran" -DefaultValue $false)
        $qdrantEmbeddingWarmupFailed = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_warmup_failed" -DefaultValue $false)
        $qdrantEmbeddingWorkloadProfile = [string](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_workload_profile" -DefaultValue "")
        $qdrantEmbeddingWorkloadNodeCount = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_workload_node_count" -DefaultValue 0)
        $qdrantEmbeddingWorkloadSmallThreshold = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_workload_small_threshold" -DefaultValue 100)
        $qdrantEmbeddingWarmupOnlyWhenCold = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_warmup_only_when_cold" -DefaultValue $true)
        $qdrantEmbeddingGenerationSeconds = [double](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_generation_seconds" -DefaultValue 0)
        $qdrantEmbeddingVectorsPerSecond = [double](Get-V2OptionalProperty -InputObject $qdrantState -Name "embedding_vectors_per_second" -DefaultValue 0)
        $qdrantOllamaVectorsPerSecond = [double](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_vectors_per_second" -DefaultValue 0)
        $qdrantOllamaModelSelected = [string](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_model_selected" -DefaultValue "")
        $qdrantOllamaInstallAttempted = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_model_install_attempted" -DefaultValue $false)
        $qdrantOllamaInstallSucceeded = [bool](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_model_install_succeeded" -DefaultValue $false)
        $qdrantOllamaInstallError = [string](Get-V2OptionalProperty -InputObject $qdrantState -Name "ollama_model_install_error" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($qdrantEmbeddingProcessor)) {
            $qdrantEmbeddingProcessor = [string](Get-V2OptionalProperty -InputObject $qdrantEmbeddingRuntime -Name "processor" -DefaultValue "unknown")
        }
        $qdrantEmbeddingRuntimeReason = [string](Get-V2OptionalProperty -InputObject $qdrantEmbeddingRuntime -Name "reason" -DefaultValue "")
        $qdrantSchemaSkipped = [int](Get-V2OptionalProperty -InputObject $qdrantState -Name "payload_schema_skipped_points" -DefaultValue 0)
        $qdrantSchemaErrors = @((Get-V2OptionalProperty -InputObject $qdrantState -Name "payload_schema_errors" -DefaultValue @()))
        $qdrantMaintenance = Get-V2OptionalProperty -InputObject $qdrantState -Name "qdrant_maintenance" -DefaultValue ([PSCustomObject]@{})
        $qdrantMaintenanceAlerts = @((Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "alerts" -DefaultValue @()))
        $qdrantMaintenanceRecommendations = @((Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "recommendations" -DefaultValue @()))
        $qdrantFragmentationPercent = [double](Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "fragmentation_percent" -DefaultValue 0)
        $qdrantVectorIndexCoveragePercent = [double](Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "vector_index_coverage_percent" -DefaultValue 0)
        $qdrantSegmentsCount = [int](Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "segments_count" -DefaultValue 0)
        $qdrantDeletedPointsCount = [int](Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "deleted_points_count" -DefaultValue 0)
        $qdrantMaintenanceOk = [bool](Get-V2OptionalProperty -InputObject $qdrantMaintenance -Name "maintenance_ok" -DefaultValue $true)
        $fallbackRatioPercent = if ($qdrantSynced -gt 0) { [math]::Round((100.0 * $qdrantNonOllama) / $qdrantSynced, 2) } else { 0.0 }
        $fallbackMaxRatioPercent = [double](Get-V2OptionalProperty -InputObject $projectState -Name "qdrant_fallback_max_ratio_percent" -DefaultValue 20)
        if ($gpuPolicyEnabled -and $gpuMaxNonOllamaRatioPercent -ge 0) {
            $fallbackMaxRatioPercent = $gpuMaxNonOllamaRatioPercent
        }
        if ($fallbackMaxRatioPercent -lt 0) {
            $fallbackMaxRatioPercent = 0
        }
        $fallbackEnforceAfterCoreComplete = [bool](Get-V2OptionalProperty -InputObject $projectState -Name "qdrant_fallback_enforce_after_core_complete" -DefaultValue $true)
        $coreGateStatus = "missing"
        $coreGateCompleted = $false
        if (Test-Path -LiteralPath $taskDagJsonPath) {
            $taskDocumentForFallback = Get-V2JsonContent -Path $taskDagJsonPath
            if ($taskDocumentForFallback -and ($taskDocumentForFallback.PSObject.Properties.Name -contains "tasks")) {
                $coreTask = @($taskDocumentForFallback.tasks | Where-Object {
                        [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq "CORE-COMPLETE-001"
                    } | Select-Object -First 1)
                if ($coreTask.Count -gt 0 -and $coreTask[0]) {
                    $coreGateStatus = [string](Get-V2OptionalProperty -InputObject $coreTask[0] -Name "status" -DefaultValue "unknown")
                    $coreGateCompleted = ($coreGateStatus -in @("done", "completed"))
                }
            }
        }
        $fallbackThresholdBreached = ($qdrantSynced -gt 0 -and $fallbackRatioPercent -gt $fallbackMaxRatioPercent)
        $fallbackEnforcementDeferred = ($fallbackThresholdBreached -and $fallbackEnforceAfterCoreComplete -and -not $coreGateCompleted)
        $previousDeferredCycles = [int](Get-V2OptionalProperty -InputObject $observerState -Name "qdrant_fallback_deferred_cycles" -DefaultValue 0)
        if ($previousDeferredCycles -lt 0) { $previousDeferredCycles = 0 }
        $qdrantFallbackDeferredCycles = if ($fallbackEnforcementDeferred) { $previousDeferredCycles + 1 } else { 0 }

        $checkResults.Add([PSCustomObject]@{
            name       = "memory-sync"
            command    = "memory_sync.py"
            confidence = "verified"
            status     = "passed"
            exit_code  = 0
            details    = [PSCustomObject]@{
                qdrant_enabled = $qdrantEnabled
                qdrant_nodes   = $qdrantSynced
                qdrant_fallback_embeddings = $qdrantFallbackHash
                qdrant_local_embeddings = $qdrantLocal
                qdrant_non_ollama_embeddings = $qdrantNonOllama
                qdrant_ollama_embeddings = $qdrantOllama
                qdrant_fallback_ratio_percent = $fallbackRatioPercent
                qdrant_fallback_max_ratio_percent = $fallbackMaxRatioPercent
                qdrant_fallback_threshold_breached = $fallbackThresholdBreached
                qdrant_fallback_enforce_after_core_complete = $fallbackEnforceAfterCoreComplete
                qdrant_fallback_core_gate_status = $coreGateStatus
                qdrant_fallback_enforcement_deferred = $fallbackEnforcementDeferred
                qdrant_fallback_deferred_cycles = $qdrantFallbackDeferredCycles
                qdrant_fallback_deferred_max_cycles = $qdrantFallbackDeferredMaxCycles
                qdrant_embedding_runtime_processor = $qdrantEmbeddingProcessor
                qdrant_embedding_runtime_reason = $qdrantEmbeddingRuntimeReason
                qdrant_embedding_batch_size_requested = $qdrantEmbeddingBatchSizeRequested
                qdrant_embedding_batch_size = $qdrantEmbeddingBatchSize
                qdrant_embedding_batch_size_auto_enabled = $qdrantEmbeddingBatchSizeAuto
                qdrant_embedding_batch_size_gpu_memory_mb = $qdrantEmbeddingBatchSizeGpuMemoryMb
                qdrant_embedding_concurrency = $qdrantEmbeddingConcurrency
                qdrant_embedding_batch_workers_used = $qdrantEmbeddingBatchWorkers
                qdrant_embedding_batch_chunk_size = $qdrantEmbeddingBatchChunkSize
                qdrant_embedding_batch_requests = $qdrantEmbeddingBatchRequests
                qdrant_embedding_batch_items = $qdrantEmbeddingBatchItems
                qdrant_embedding_single_requests = $qdrantEmbeddingSingleRequests
                qdrant_embedding_warmup_inputs = $qdrantEmbeddingWarmupInputs
                qdrant_embedding_warmup_ran = $qdrantEmbeddingWarmupRan
                qdrant_embedding_warmup_failed = $qdrantEmbeddingWarmupFailed
                qdrant_embedding_workload_profile = $qdrantEmbeddingWorkloadProfile
                qdrant_embedding_workload_node_count = $qdrantEmbeddingWorkloadNodeCount
                qdrant_embedding_workload_small_threshold = $qdrantEmbeddingWorkloadSmallThreshold
                qdrant_embedding_warmup_only_when_cold = $qdrantEmbeddingWarmupOnlyWhenCold
                qdrant_embedding_generation_seconds = [math]::Round($qdrantEmbeddingGenerationSeconds, 3)
                qdrant_embedding_vectors_per_second = [math]::Round($qdrantEmbeddingVectorsPerSecond, 2)
                qdrant_ollama_vectors_per_second = [math]::Round($qdrantOllamaVectorsPerSecond, 2)
                qdrant_ollama_model_selected = $qdrantOllamaModelSelected
                qdrant_ollama_install_attempted = $qdrantOllamaInstallAttempted
                qdrant_ollama_install_succeeded = $qdrantOllamaInstallSucceeded
                qdrant_ollama_install_error = $qdrantOllamaInstallError
                qdrant_gpu_policy_enabled = $gpuPolicyEnabled
                qdrant_gpu_policy_expected_processor = $gpuExpectedProcessor
                qdrant_gpu_policy_require_gpu = $gpuRequireEmbeddings
                qdrant_payload_schema_skipped_points = $qdrantSchemaSkipped
                qdrant_payload_schema_error_count = @($qdrantSchemaErrors).Count
                qdrant_maintenance_ok = $qdrantMaintenanceOk
                qdrant_maintenance_alert_count = @($qdrantMaintenanceAlerts).Count
                qdrant_maintenance_alerts = @($qdrantMaintenanceAlerts)
                qdrant_maintenance_recommendations = @($qdrantMaintenanceRecommendations)
                qdrant_fragmentation_percent = [math]::Round($qdrantFragmentationPercent, 2)
                qdrant_vector_index_coverage_percent = [math]::Round($qdrantVectorIndexCoveragePercent, 2)
                qdrant_segments_count = $qdrantSegmentsCount
                qdrant_deleted_points_count = $qdrantDeletedPointsCount
                neo4j_enabled  = $neo4jEnabled
                neo4j_nodes    = $neo4jSynced
            }
        })

        if ($observerIncludeQdrant -and (-not $qdrantEnabled -or $qdrantSynced -le 0)) {
            $healthStatus = "degraded"
            $details = "Qdrant sync enabled by project-state, but no nodes were synced. Result: $($qdrantState | ConvertTo-Json -Depth 6)"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-qdrant" -Title "Qdrant sync produced no data" -Details $details))
        }
        if ($observerIncludeNeo4j -and (-not $neo4jEnabled -or $neo4jSynced -le 0)) {
            $healthStatus = "degraded"
            $details = "Neo4j sync enabled by project-state, but no nodes were synced. Result: $($neo4jState | ConvertTo-Json -Depth 6)"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-neo4j" -Title "Neo4j sync produced no data" -Details $details))
        }
        if ($observerIncludeQdrant -and ($qdrantSchemaSkipped -gt 0 -or @($qdrantSchemaErrors).Count -gt 0)) {
            $healthStatus = "degraded"
            $details = "Qdrant payload schema validation failed for one or more nodes. skipped_points=$qdrantSchemaSkipped errors=$(@($qdrantSchemaErrors).Count). Sample: $((@($qdrantSchemaErrors) | Select-Object -First 5) -join '; ')"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-qdrant-schema" -Title "Qdrant payload schema violations detected" -Details $details))
        }
        $gpuProcessorPolicyBreached = $false
        if ($observerIncludeQdrant -and $gpuPolicyEnabled -and $gpuRequireEmbeddings -and $qdrantSynced -gt 0) {
            $processorLower = $qdrantEmbeddingProcessor.ToLowerInvariant()
            $expectedLower = $gpuExpectedProcessor.ToLowerInvariant()
            if ($processorLower -eq "cpu" -and $expectedLower -eq "gpu") {
                $gpuProcessorPolicyBreached = $true
            }
            elseif (($processorLower -eq "unknown") -and $gpuUnknownProcessorFailure) {
                $gpuProcessorPolicyBreached = $true
            }
        }
        if ($gpuProcessorPolicyBreached) {
            $healthStatus = "degraded"
            $details = "GPU embedding policy violated. expected_processor=$gpuExpectedProcessor detected_processor=$qdrantEmbeddingProcessor synced=$qdrantSynced non_ollama=$qdrantNonOllama ratio=$fallbackRatioPercent% runtime_reason=$qdrantEmbeddingRuntimeReason."
            $incidentPath = New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-gpu-policy" -Title "Embedding runtime is not using GPU as required" -Details $details
            $incidents.Add($incidentPath)
            if ($gpuRepairOnCpuFallback) {
                Add-RepairTask `
                    -SelfHealingPath $selfHealingPath `
                    -BacklogPath $backlogPath `
                    -IncidentPath $incidentPath `
                    -Reason ("qdrant-gpu-policy-processor-mismatch: expected={0} detected={1}" -f $gpuExpectedProcessor, $qdrantEmbeddingProcessor) `
                    -TaskDagJsonPath $taskDagJsonPath
            }
        }
        if ($observerIncludeQdrant -and $fallbackThresholdBreached) {
            if ($fallbackEnforcementDeferred) {
                Write-Host "Qdrant fallback threshold breached ($fallbackRatioPercent% > $fallbackMaxRatioPercent%), enforcement deferred until CORE-COMPLETE-001 is done." -ForegroundColor Yellow
                if ($qdrantFallbackDeferredCycles -ge $qdrantFallbackDeferredMaxCycles) {
                    $healthStatus = "degraded"
                    $details = "Qdrant non-ollama embedding ratio remained deferred for too many observer cycles. deferred_cycles=$qdrantFallbackDeferredCycles max_cycles=$qdrantFallbackDeferredMaxCycles ratio=$fallbackRatioPercent% threshold=$fallbackMaxRatioPercent%. Open infra task to restore GPU embeddings."
                    $incidentPath = New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-qdrant-fallback-deferred" -Title "Qdrant fallback deferred cycle limit reached" -Details $details
                    $incidents.Add($incidentPath)

                    $hasOpenDeferredRepair = $false
                    if (Test-Path -LiteralPath $taskDagJsonPath) {
                        $taskDocument = Get-V2JsonContent -Path $taskDagJsonPath
                        if ($taskDocument -and ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
                            $hasOpenDeferredRepair = @($taskDocument.tasks | Where-Object {
                                $taskReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                                $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                                ($taskReason -like "qdrant-fallback-deferred-limit:*") -and (Test-V2TaskStatusOpen -Status $taskStatus)
                            }).Count -gt 0
                        }
                    }

                    if (-not $hasOpenDeferredRepair) {
                        Add-RepairTask `
                            -SelfHealingPath $selfHealingPath `
                            -BacklogPath $backlogPath `
                            -IncidentPath $incidentPath `
                            -Reason ("qdrant-fallback-deferred-limit: cycles={0}/{1}" -f $qdrantFallbackDeferredCycles, $qdrantFallbackDeferredMaxCycles) `
                            -TaskDagJsonPath $taskDagJsonPath
                    }
                }
            }
            else {
                $healthStatus = "degraded"
                $details = "Qdrant non-ollama embedding ratio is above allowed threshold. ratio=$fallbackRatioPercent% threshold=$fallbackMaxRatioPercent% synced=$qdrantSynced non_ollama=$qdrantNonOllama local=$qdrantLocal fallback_hash=$qdrantFallbackHash. Check Ollama GPU runtime and embedding endpoint availability."
                $incidentPath = New-IncidentReport -ReportDirectory $reportsDirectory -Category "memory-sync-qdrant-fallback" -Title "Qdrant fallback ratio above threshold" -Details $details
                $incidents.Add($incidentPath)

                $hasOpenFallbackRepair = $false
                $hasRecentFallbackRepair = $false
                $fallbackAlertCooldownSeconds = [int](Get-V2OptionalProperty -InputObject $projectState -Name "qdrant_fallback_alert_cooldown_seconds" -DefaultValue 1800)
                if ($fallbackAlertCooldownSeconds -lt 60) {
                    $fallbackAlertCooldownSeconds = 60
                }
                if (Test-Path -LiteralPath $taskDagJsonPath) {
                    $taskDocument = Get-V2JsonContent -Path $taskDagJsonPath
                    if ($taskDocument -and ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
                        $fallbackTasks = @($taskDocument.tasks | Where-Object {
                            $taskReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                            ($taskReason -like "qdrant-fallback-ratio-high:*")
                        })

                        $hasOpenFallbackRepair = @($fallbackTasks | Where-Object {
                            $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                            (Test-V2TaskStatusOpen -Status $taskStatus)
                        }).Count -gt 0

                        foreach ($task in $fallbackTasks) {
                            $updatedRaw = [string](Get-V2OptionalProperty -InputObject $task -Name "updated_at" -DefaultValue "")
                            if ([string]::IsNullOrWhiteSpace($updatedRaw)) {
                                $updatedRaw = [string](Get-V2OptionalProperty -InputObject $task -Name "created_at" -DefaultValue "")
                            }
                            if ([string]::IsNullOrWhiteSpace($updatedRaw)) { continue }
                            try {
                                $updatedAt = [DateTimeOffset]::Parse($updatedRaw).UtcDateTime
                            }
                            catch {
                                continue
                            }
                            $ageSeconds = ((Get-Date).ToUniversalTime() - $updatedAt).TotalSeconds
                            if ($ageSeconds -ge 0 -and $ageSeconds -lt $fallbackAlertCooldownSeconds) {
                                $hasRecentFallbackRepair = $true
                                break
                            }
                        }
                    }
                }

                if (-not $hasOpenFallbackRepair -and -not $hasRecentFallbackRepair) {
                    Add-RepairTask `
                        -SelfHealingPath $selfHealingPath `
                        -BacklogPath $backlogPath `
                        -IncidentPath $incidentPath `
                        -Reason ("qdrant-fallback-ratio-high: {0}% > {1}%" -f $fallbackRatioPercent, $fallbackMaxRatioPercent) `
                        -TaskDagJsonPath $taskDagJsonPath
                }
            }
        }
        elseif ($observerIncludeQdrant -and -not $fallbackThresholdBreached -and (Test-Path -LiteralPath $taskDagJsonPath)) {
            # Auto-resolve stale fallback repair tasks once ratio recovers.
            try {
                $taskDocument = Get-V2JsonContent -Path $taskDagJsonPath
                if ($taskDocument -and ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
                    $taskUpdated = $false
                    foreach ($task in @($taskDocument.tasks)) {
                        $taskReason = [string](Get-V2OptionalProperty -InputObject $task -Name "reason" -DefaultValue "")
                        $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                        $isFallbackRepair = ($taskReason -like "qdrant-fallback-ratio-high:*") -or ($taskReason -like "qdrant-fallback-deferred-limit:*")
                        if (-not $isFallbackRepair) { continue }
                        if (-not (Test-V2TaskStatusOpen -Status $taskStatus)) { continue }

                        Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                        Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                        Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
                        Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
                        Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved-qdrant-fallback-recovered"
                        $taskUpdated = $true
                    }
                    if ($taskUpdated) {
                        if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                            $taskDocument.updated_at = $timestamp
                        }
                        else {
                            Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
                        }
                        Save-V2JsonContent -Path $taskDagJsonPath -Value $taskDocument
                    }
                }
            }
            catch {
                # Non-fatal: task auto-resolution should not break observer cycle.
            }
        }
    }

    if (-not $SkipHealthChecks) {
        foreach ($entry in @(
            [PSCustomObject]@{ Name = "build"; Data = $intake.verified_commands.build },
            [PSCustomObject]@{ Name = "test"; Data = $intake.verified_commands.test }
        )) {
            if (-not $entry.Data) {
                $healthStatus = "degraded"
                $checkResults.Add([PSCustomObject]@{
                    name       = $entry.Name
                    command    = "unknown"
                    confidence = "missing"
                    status     = "failed"
                    exit_code  = $null
                })
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category $entry.Name -Title "$($entry.Name) command missing" -Details "verified_commands.$($entry.Name) is missing. CORE-COMPLETE-001 requires verified build/test commands."))
                continue
            }
            $commandValue = [string]$entry.Data.value
            $commandConfidence = [string]$entry.Data.confidence
            if ($commandValue -eq "unknown") {
                $healthStatus = "degraded"
                $checkResults.Add([PSCustomObject]@{
                    name       = $entry.Name
                    command    = "unknown"
                    confidence = $commandConfidence
                    status     = "failed"
                    exit_code  = $null
                })
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category $entry.Name -Title "$($entry.Name) command unresolved" -Details "verified_commands.$($entry.Name).value is unknown. Resolve command to prevent false green status."))
                continue
            }
            if ($commandConfidence -ne "verified" -and -not $AllowInferredCommands) {
                $healthStatus = "degraded"
                $checkResults.Add([PSCustomObject]@{
                    name       = $entry.Name
                    command    = $commandValue
                    confidence = $commandConfidence
                    status     = "failed"
                    exit_code  = $null
                })
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category $entry.Name -Title "$($entry.Name) command not verified" -Details "verified_commands.$($entry.Name).confidence is '$commandConfidence'. Observer requires verified commands unless -AllowInferredCommands is set."))
                continue
            }

            $result = Invoke-ObservedCommandV2 -CommandText $commandValue -WorkingDirectory $resolvedProjectPath -TimeoutSeconds $CommandTimeoutSeconds
            $checkResults.Add([PSCustomObject]@{
                name       = $entry.Name
                command    = $result.command
                confidence = $commandConfidence
                status     = $result.status
                exit_code  = $result.exitCode
            })
            if ($result.status -ne "passed" -and $result.status -ne "skipped") {
                $healthStatus = "unhealthy"
                $incidentPath = New-IncidentReport `
                    -ReportDirectory $reportsDirectory `
                    -Category $entry.Name `
                    -Title "$($entry.Name) check failed" `
                    -Details "Observer command status: $($result.status)" `
                    -CommandText $result.command `
                    -OutputText $result.output
                $incidents.Add($incidentPath)
                $lessonHints = Get-LessonLearnedHints `
                    -LessonsDirectory (Join-Path $orchestratorRoot "knowledge_base/lessons_learned") `
                    -Category $entry.Name `
                    -CommandText $result.command `
                    -OutputText $result.output `
                    -Limit 3
                Add-RepairTask `
                    -SelfHealingPath $selfHealingPath `
                    -BacklogPath $backlogPath `
                    -IncidentPath $incidentPath `
                    -Reason "$($entry.Name) validation failed" `
                    -LessonHints @($lessonHints) `
                    -TaskDagJsonPath $taskDagJsonPath
            }
        }
    }

    if (@($intake.unknowns).Count -gt 0) {
        $healthStatus = "needs-answers"
        $questionLines = New-Object System.Collections.Generic.List[string]
        $questionLines.Add("# Open Questions")
        $questionLines.Add("")
        foreach ($question in @($intake.open_questions)) {
            $questionLines.Add("- $question")
        }
        [System.IO.File]::WriteAllText($openQuestionsPath, ($questionLines -join [Environment]::NewLine))
    }
    elseif (Test-Path -LiteralPath $openQuestionsPath) {
        Remove-Item -LiteralPath $openQuestionsPath -Force -ErrorAction SilentlyContinue
    }

    try {
        & $schedulerScript -ProjectPath $resolvedProjectPath | Out-Null
    }
    catch {
        $healthStatus = "degraded"
        $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "scheduler" -Title "Scheduler run failed" -Details $_.Exception.Message))
    }

    if (-not $SkipRuntimeObservability -and (Test-Path -LiteralPath $runtimeObservabilityScript -PathType Leaf)) {
        try {
            $runtimeJson = & $runtimeObservabilityScript -ProjectPath $resolvedProjectPath -AutoRepairTasks -EmitJson
            $runtimeResult = ($runtimeJson | Out-String) | ConvertFrom-Json
            $runtimeStatus = [string](Get-V2OptionalProperty -InputObject $runtimeResult -Name "status" -DefaultValue "unknown")
            $runtimeAlerts = @(Get-V2OptionalProperty -InputObject $runtimeResult -Name "alerts" -DefaultValue @())
            $checkStatus = switch ($runtimeStatus) {
                "healthy" { "passed" }
                "missing-telemetry" { "skipped" }
                "degraded" { "failed" }
                default { "failed" }
            }
            $checkResults.Add([PSCustomObject]@{
                    name       = "runtime-observability"
                    command    = "Invoke-RuntimeObservabilityV2.ps1"
                    confidence = "verified"
                    status     = $checkStatus
                    exit_code  = if ($checkStatus -eq "failed") { 1 } else { 0 }
                    details    = [PSCustomObject]@{
                        runtime_status = $runtimeStatus
                        alerts_count   = @($runtimeAlerts).Count
                        report_json    = [string](Get-V2OptionalProperty -InputObject $runtimeResult -Name "report_json" -DefaultValue "")
                    }
                })
            if ($runtimeStatus -eq "degraded") {
                if ($healthStatus -eq "healthy") {
                    $healthStatus = "degraded"
                }
                $alertDetails = if (@($runtimeAlerts).Count -gt 0) {
                    (@($runtimeAlerts | ForEach-Object {
                                "{0}: {1}" -f [string](Get-V2OptionalProperty -InputObject $_ -Name "metric" -DefaultValue "metric"), [string](Get-V2OptionalProperty -InputObject $_ -Name "details" -DefaultValue "")
                            }) -join " | ")
                }
                else {
                    "Runtime telemetry breached configured thresholds."
                }
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "runtime-observability" -Title "Runtime metrics degraded" -Details $alertDetails))
            }
        }
        catch {
            $healthStatus = "degraded"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "runtime-observability" -Title "Runtime observability check failed" -Details $_.Exception.Message))
        }
    }

    if (Test-Path -LiteralPath $commanderDashboardScript -PathType Leaf) {
        try {
            $dashboardJson = & $commanderDashboardScript -ProjectPath $resolvedProjectPath -EmitJson
            $dashboardResult = ($dashboardJson | Out-String) | ConvertFrom-Json
            $dashboardStatus = [string](Get-V2OptionalProperty -InputObject $dashboardResult -Name "status" -DefaultValue "unknown")
            $dashboardAlertsCount = [int](Get-V2OptionalProperty -InputObject $dashboardResult -Name "alerts_count" -DefaultValue 0)
            $dashboardCheckStatus = if ($dashboardStatus -eq "healthy") { "passed" } else { "failed" }
            $checkResults.Add([PSCustomObject]@{
                    name       = "commander-dashboard"
                    command    = "Invoke-CommanderDashboardV2.ps1"
                    confidence = "verified"
                    status     = $dashboardCheckStatus
                    exit_code  = if ($dashboardCheckStatus -eq "failed") { 1 } else { 0 }
                    details    = [PSCustomObject]@{
                        dashboard_status = $dashboardStatus
                        alerts_count     = $dashboardAlertsCount
                        report_json      = [string](Get-V2OptionalProperty -InputObject $dashboardResult -Name "report_json" -DefaultValue "")
                        report_md        = [string](Get-V2OptionalProperty -InputObject $dashboardResult -Name "report_md" -DefaultValue "")
                    }
                })
            if ($dashboardStatus -ne "healthy") {
                if ($healthStatus -eq "healthy") {
                    $healthStatus = "degraded"
                }
                $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "commander-dashboard" -Title "Commander dashboard has active alerts" -Details ("status={0} alerts={1}" -f $dashboardStatus, $dashboardAlertsCount)))
            }
        }
        catch {
            $healthStatus = "degraded"
            $checkResults.Add([PSCustomObject]@{
                    name       = "commander-dashboard"
                    command    = "Invoke-CommanderDashboardV2.ps1"
                    confidence = "verified"
                    status     = "failed"
                    exit_code  = 1
                    details    = [PSCustomObject]@{
                        error = $_.Exception.Message
                    }
                })
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "commander-dashboard" -Title "Commander dashboard generation failed" -Details $_.Exception.Message))
        }
    }

    # ── Empty-backlog detection ──────────────────────────────────────────────────
    # If all bootstrap tasks are done/skipped AND there are no pending/in-progress
    # DEV tasks, the system is stalled — emit an alert and create a REPAIR task.
    try {
        $dagDoc = Get-V2JsonContent -Path $taskDagJsonPath
        if ($dagDoc -and ($dagDoc.PSObject.Properties.Name -contains "tasks")) {
            $bootstrapIds = @("V2-INTAKE-001", "V2-PLAN-001", "V2-ANALYSIS-001", "V2-DOCKER-001", "V2-LEGACY-GATE-001")
            $bootstrapAll = @($dagDoc.tasks | Where-Object {
                $bootstrapIds -contains [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
            })
            $bootstrapDone = @($bootstrapAll | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -in @("done", "skipped")
            })
            $observerOpenStatuses = Get-V2ObserverOpenTaskStatuses
            $devRepairOpenTasks = @($dagDoc.tasks | Where-Object {
                $id = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                $isDevOrRepair = ($id -like "DEV-*") -or ($id -like "REPAIR-*")
                $isOpen = Test-V2TaskStatusOpen -Status $status
                $isDevOrRepair -and $isOpen
            })
            $executionOpenTasks = @($dagDoc.tasks | Where-Object {
                $id = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                $isOpen = Test-V2TaskStatusOpen -Status $status
                $isExecution = Test-V2ExecutionTaskId -TaskId $id
                $isExecution -and $isOpen
            })
            $coreTask = @($dagDoc.tasks | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq "CORE-COMPLETE-001"
            } | Select-Object -First 1)
            $coreOpen = $false
            $coreDone = $false
            if ($coreTask.Count -eq 1) {
                $coreStatus = [string](Get-V2OptionalProperty -InputObject $coreTask[0] -Name "status" -DefaultValue "")
                $coreOpen = (Test-V2TaskStatusOpen -Status $coreStatus) -and ($coreStatus -notin @("done", "completed"))
                $coreDone = $coreStatus -in @("done", "completed")
            }
            $isBootstrapComplete = ($bootstrapAll.Count -gt 0 -and $bootstrapDone.Count -eq $bootstrapAll.Count)
            if ($coreDone) {
                $resolvedAnyEmptyBacklogTask = $false
                foreach ($task in @($dagDoc.tasks)) {
                    $taskReason = [string](Get-V2OptionalProperty -InputObject $task -Name "reason" -DefaultValue "")
                    $taskSource = [string](Get-V2OptionalProperty -InputObject $task -Name "source_incident" -DefaultValue "")
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                    $isOpen = Test-V2TaskStatusOpen -Status $taskStatus
                    $isEmptyBacklogRepair = ($taskReason -like "empty-backlog*") -or ($taskSource -match "_empty_backlog\.md$")
                    if (-not $isOpen -or -not $isEmptyBacklogRepair) { continue }

                    Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                    Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
                    Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved-core-complete"
                    $resolvedAnyEmptyBacklogTask = $true
                }
                if ($resolvedAnyEmptyBacklogTask) {
                    if ($dagDoc.PSObject.Properties.Name -contains "updated_at") {
                        $dagDoc.updated_at = $timestamp
                    }
                    else {
                        Add-Member -InputObject $dagDoc -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
                    }
                    Save-V2JsonContent -Path $taskDagJsonPath -Value $dagDoc
                }

                if ($executionOpenTasks.Count -gt 0) {
                    $resolvedExecutionGapTask = $false
                    foreach ($task in @($dagDoc.tasks)) {
                        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                        $taskReason = [string](Get-V2OptionalProperty -InputObject $task -Name "reason" -DefaultValue "")
                        $taskSource = [string](Get-V2OptionalProperty -InputObject $task -Name "source_incident" -DefaultValue "")
                        $taskStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                        $isOpen = Test-V2TaskStatusOpen -Status $taskStatus
                        $isExecutionGap = (($taskReason -like "execution-backlog-gap*") -or ($taskSource -match "_execution_backlog_gap\.md$")) -and ($taskId -like "REPAIR-*")
                        if (-not $isOpen -or -not $isExecutionGap) { continue }

                        Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                        Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                        Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
                        Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
                        Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved: execution backlog created"
                        Set-V2DynamicProperty -InputObject $task -Name "last_error" -Value ""
                        $resolvedExecutionGapTask = $true
                    }
                    if ($resolvedExecutionGapTask) {
                        if ($dagDoc.PSObject.Properties.Name -contains "updated_at") {
                            $dagDoc.updated_at = $timestamp
                        }
                        else {
                            Add-Member -InputObject $dagDoc -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
                        }
                        Save-V2JsonContent -Path $taskDagJsonPath -Value $dagDoc
                    }
                }
            }

            if ($isBootstrapComplete -and $devRepairOpenTasks.Count -eq 0 -and -not $coreOpen -and -not $coreDone) {
                $alreadyFlagged = @($dagDoc.tasks | Where-Object {
                    $taskReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                    $taskSource = [string](Get-V2OptionalProperty -InputObject $_ -Name "source_incident" -DefaultValue "")
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    $isOpen = Test-V2TaskStatusOpen -Status $taskStatus
                    $isOpen -and (($taskReason -like "empty-backlog*") -or ($taskSource -match "_empty_backlog\.md$"))
                }).Count -gt 0
                if (-not $alreadyFlagged) {
                    if ($healthStatus -eq "healthy") { $healthStatus = "degraded" }
                    $incidentPath = New-IncidentReport `
                        -ReportDirectory $reportsDirectory `
                        -Category "empty-backlog" `
                        -Title "Bootstrap complete but no DEV tasks defined" `
                        -Details "All bootstrap tasks are done/skipped and no DEV/REPAIR tasks exist in task-dag.json. Agents have no executable work. Add DEV-* tasks to unblock the autonomous loop." `
                        -DedupPath $script:ObserverIncidentDedupPath `
                        -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds `
                        -DedupCategories $script:ObserverIncidentDedupCategories
                    $incidents.Add($incidentPath)
                    Add-RepairTask `
                        -SelfHealingPath $selfHealingPath `
                        -BacklogPath $backlogPath `
                        -IncidentPath $incidentPath `
                        -Reason "empty-backlog: bootstrap complete but no DEV tasks exist" `
                        -TaskDagJsonPath $taskDagJsonPath
                }
            }

            if ($coreDone) {
                $roadmapPath = Join-Path $orchestratorRoot "memory/roadmap.md"
                $roadmapPendingMarkers = 0
                $roadmapActionableMarkers = 0
                $roadmapNeedsExecutionSeed = $false
                if (Test-Path -LiteralPath $roadmapPath -PathType Leaf) {
                    try {
                        $roadmapContent = Get-Content -LiteralPath $roadmapPath -Raw -ErrorAction Stop
                        $roadmapPendingMarkers = [regex]::Matches($roadmapContent, "(?im)^\s*-\s*pending\s*$").Count
                        $roadmapActionableMarkers = [regex]::Matches($roadmapContent, "(?im)^\s*-\s*(FEAT|DEV|TASK|COBERTURA|RECHECK|REPAIR|REFACTOR)-").Count
                        $roadmapNeedsExecutionSeed = ($roadmapPendingMarkers -gt 0) -or ($roadmapActionableMarkers -gt 0)
                    }
                    catch {
                        $roadmapNeedsExecutionSeed = $false
                    }
                }

                if ($roadmapNeedsExecutionSeed) {
                    $executionSeedTaskId = Ensure-V2ExecutionBacklogTask `
                        -TaskDagJsonPath $taskDagJsonPath `
                        -BacklogPath $backlogPath `
                        -RoadmapPath $roadmapPath

                    if ($executionOpenTasks.Count -eq 0) {
                        $alreadyFlaggedExecutionGap = @($dagDoc.tasks | Where-Object {
                            $taskReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                            $taskSource = [string](Get-V2OptionalProperty -InputObject $_ -Name "source_incident" -DefaultValue "")
                            $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                            $isOpen = Test-V2TaskStatusOpen -Status $taskStatus
                            $isExecutionGap = ($taskReason -like "execution-backlog-gap*") -or ($taskSource -match "_execution_backlog_gap\.md$")
                            $isOpen -and $isExecutionGap
                        }).Count -gt 0

                        if ([string]::IsNullOrWhiteSpace($executionSeedTaskId) -and -not $alreadyFlaggedExecutionGap) {
                            $reopenedExistingGapTask = $false
                            $latestClosedExecutionGap = @($dagDoc.tasks | Where-Object {
                                    $taskReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                                    $taskSource = [string](Get-V2OptionalProperty -InputObject $_ -Name "source_incident" -DefaultValue "")
                                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                                    $isExecutionGap = ($taskReason -like "execution-backlog-gap*") -or ($taskSource -match "_execution_backlog_gap\.md$")
                                    $isClosed = $taskStatus -in @("done", "completed", "skipped")
                                    $isExecutionGap -and $isClosed
                                } | Sort-Object {
                                    [string](Get-V2OptionalProperty -InputObject $_ -Name "updated_at" -DefaultValue "")
                                } -Descending | Select-Object -First 1)

                            if ($latestClosedExecutionGap.Count -eq 1) {
                                $gapTask = $latestClosedExecutionGap[0]
                                Set-V2DynamicProperty -InputObject $gapTask -Name "status" -Value "pending"
                                Set-V2DynamicProperty -InputObject $gapTask -Name "blocked_reason" -Value ""
                                Set-V2DynamicProperty -InputObject $gapTask -Name "execution_mode" -Value "manual"
                                Set-V2DynamicProperty -InputObject $gapTask -Name "assigned_agent" -Value "Codex"
                                Set-V2DynamicProperty -InputObject $gapTask -Name "updated_at" -Value $timestamp
                                Set-V2DynamicProperty -InputObject $gapTask -Name "completed_at" -Value ""
                                Set-V2DynamicProperty -InputObject $gapTask -Name "completion_note" -Value "auto-reopened: execution backlog still missing"
                                Set-V2DynamicProperty -InputObject $gapTask -Name "last_error" -Value ""
                                $reopenedExistingGapTask = $true
                            }

                            if ($reopenedExistingGapTask) {
                                if ($dagDoc.PSObject.Properties.Name -contains "updated_at") {
                                    $dagDoc.updated_at = $timestamp
                                }
                                else {
                                    Add-Member -InputObject $dagDoc -MemberType NoteProperty -Name "updated_at" -Value $timestamp -Force
                                }
                                Save-V2JsonContent -Path $taskDagJsonPath -Value $dagDoc
                            }
                        }

                        if ([string]::IsNullOrWhiteSpace($executionSeedTaskId)) {
                            if ($healthStatus -eq "healthy") { $healthStatus = "degraded" }
                            if (-not $alreadyFlaggedExecutionGap) {
                                $incidentPath = New-IncidentReport `
                                    -ReportDirectory $reportsDirectory `
                                    -Category "execution-backlog-gap" `
                                    -Title "Roadmap has actionable items but no execution tasks are open" `
                                    -Details ("CORE-COMPLETE-001 is done and roadmap.md still contains actionable markers (pending={0}, actionable={1}), but no FEAT/DEV/COBERTURA/RECHECK task is open." -f $roadmapPendingMarkers, $roadmapActionableMarkers) `
                                    -DedupPath $script:ObserverIncidentDedupPath `
                                    -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds `
                                    -DedupCategories $script:ObserverIncidentDedupCategories
                                $incidents.Add($incidentPath)
                                Add-RepairTask `
                                    -SelfHealingPath $selfHealingPath `
                                    -BacklogPath $backlogPath `
                                    -IncidentPath $incidentPath `
                                    -Reason "execution-backlog-gap: roadmap actionable but no execution tasks are open" `
                                    -ExecutionMode "manual" `
                                    -TaskDagJsonPath $taskDagJsonPath

                                [void](Ensure-V2ExecutionBacklogTask `
                                    -TaskDagJsonPath $taskDagJsonPath `
                                    -BacklogPath $backlogPath `
                                    -RoadmapPath $roadmapPath `
                                    -IncidentPath $incidentPath)
                            }
                        }
                    }
                }
            }
        }
    }
    catch {
        # Non-fatal — empty-backlog detection is best-effort
    }

    if (Test-Path -LiteralPath $compressionScript -PathType Leaf) {
        try {
            & $compressionScript -ProjectPath $resolvedProjectPath | Out-Null
        }
        catch {
            $healthStatus = "degraded"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "compression" -Title "Long-term compression failed" -Details $_.Exception.Message -DedupPath $script:ObserverIncidentDedupPath -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds -DedupCategories $script:ObserverIncidentDedupCategories))
        }
    }

    if (Test-Path -LiteralPath $securityScanScript -PathType Leaf) {
        try {
            $scanJson = & $securityScanScript -ProjectPath $resolvedProjectPath -EmitJson
            $scan = ($scanJson | Out-String) | ConvertFrom-Json
            $criticalFindings = [int](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $scan -Name "counts" -DefaultValue ([PSCustomObject]@{})) -Name "critical" -DefaultValue 0)
            $highFindings = [int](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $scan -Name "counts" -DefaultValue ([PSCustomObject]@{})) -Name "high" -DefaultValue 0)
            if ($criticalFindings -gt 0) {
                $healthStatus = "unhealthy"
            }
            elseif ($highFindings -gt 0 -and $healthStatus -eq "healthy") {
                $healthStatus = "degraded"
            }
        }
        catch {
            $healthStatus = "degraded"
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "security-scan" -Title "OWASP scan failed" -Details $_.Exception.Message -DedupPath $script:ObserverIncidentDedupPath -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds -DedupCategories $script:ObserverIncidentDedupCategories))
        }
    }

    # ── Production feedback loop: scan Docker container logs for runtime errors ───────────────
    try {
        $composePath = ""
        $startupPathsForLogs = Get-V2OptionalProperty -InputObject $projectState -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})
        $composeRel  = [string](Get-V2OptionalProperty -InputObject $startupPathsForLogs -Name "docker_compose_file" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($composeRel)) {
            $composePath = if ([System.IO.Path]::IsPathRooted($composeRel)) { $composeRel } else { Join-Path $resolvedProjectPath $composeRel }
        }
        if ([string]::IsNullOrEmpty($composePath)) {
            $composePath = Join-Path $orchestratorRoot "docker/docker-compose.generated.yml"
        }

        if (Test-Path -LiteralPath $composePath -PathType Leaf) {
            $composeEngineLogs = Get-V2DockerComposeEngine
            if (-not $composeEngineLogs) { $composeEngineLogs = "docker compose" }

            # Scan logs from last 100 lines of the app container only (avoid noise from DB containers)
            $appLogOutput = & cmd /c "$composeEngineLogs -f `"$composePath`" logs --tail=100 app 2>&1" | Out-String

            # Error patterns that indicate real runtime problems
            $errorPatterns = @(
                "(?i)(Traceback \(most recent call last\)|CRITICAL|FATAL|panic:|runtime error:|unhandled exception)",
                "(?i)(OOMKilled|out of memory|killed process)",
                "(?i)(connection refused|could not connect to|authentication failed|no such host)",
                "(?i)(disk full|no space left|permission denied|access is denied)"
            )

            $runtimeErrors = New-Object System.Collections.Generic.List[string]
            foreach ($pattern in $errorPatterns) {
                $matches = [regex]::Matches($appLogOutput, $pattern)
                foreach ($m in $matches) {
                    # Get the surrounding line for context
                    $lineStart = [Math]::Max(0, $appLogOutput.LastIndexOf("`n", $m.Index) + 1)
                    $lineEnd   = $appLogOutput.IndexOf("`n", $m.Index)
                    if ($lineEnd -lt 0) { $lineEnd = $appLogOutput.Length }
                    $errorLine = $appLogOutput.Substring($lineStart, $lineEnd - $lineStart).Trim()
                    if ($errorLine -and -not ($runtimeErrors | Where-Object { $_ -eq $errorLine })) {
                        $runtimeErrors.Add($errorLine)
                    }
                    if ($runtimeErrors.Count -ge 5) { break }
                }
                if ($runtimeErrors.Count -ge 5) { break }
            }

            if ($runtimeErrors.Count -gt 0) {
                $errorSummary = ($runtimeErrors | Select-Object -First 3) -join " | "
                if ($healthStatus -eq "healthy") { $healthStatus = "degraded" }

                # Check if REPAIR-PROD-* already open
                $dagForProd = Get-V2JsonContent -Path $taskDagJsonPath
                if ($dagForProd -and $dagForProd.PSObject.Properties.Name -contains "tasks") {
                    $alreadyOpenProd = @($dagForProd.tasks | Where-Object {
                        [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -like "REPAIR-PROD-*" -and
                        (Test-V2TaskStatusOpen -Status ([string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")))
                    }).Count -gt 0

                    if (-not $alreadyOpenProd) {
                        $incidentPath = New-IncidentReport `
                            -ReportDirectory $reportsDirectory `
                            -Category "production-error" `
                            -Title "Runtime errors detected in app container logs" `
                            -Details $errorSummary `
                            -DedupPath $script:ObserverIncidentDedupPath `
                            -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds `
                            -DedupCategories $script:ObserverIncidentDedupCategories
                        $incidents.Add($incidentPath)
                        Add-RepairTask `
                            -SelfHealingPath $selfHealingPath `
                            -BacklogPath $backlogPath `
                            -IncidentPath $incidentPath `
                            -Reason "production-error: $errorSummary" `
                            -TaskDagJsonPath $taskDagJsonPath
                        Write-Warning "[Observer] Production errors detected: $errorSummary"
                    }
                }
            }
        }
    }
    catch {
        # Non-fatal — production log scan is best-effort
    }
    # ─────────────────────────────────────────────────────────────────────────────────────────

    if (Test-Path -LiteralPath $taskStateDbScript -PathType Leaf) {
        try {
            $taskStateDbSyncResult = Invoke-V2TaskStateDbSync `
                -ProjectRoot $resolvedProjectPath `
                -TaskDagPath $taskDagJsonPath `
                -TaskStateDbScriptPath $taskStateDbScript

            $taskStateDbSyncMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "sync_mode" -DefaultValue "")
            $taskStateDbSyncCommandMode = if ($taskStateDbSyncMode -eq "status-only-db-primary") { "status" } else { "sync" }
            $checkResults.Add([PSCustomObject]@{
                    name       = "task-state-db-sync"
                    command    = ("task_state_db.py --mode {0}" -f $taskStateDbSyncCommandMode)
                    confidence = "verified"
                    status     = "passed"
                    exit_code  = 0
                    details    = [PSCustomObject]@{
                        sync_mode                     = $taskStateDbSyncMode
                        backend_mode                  = [string](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "backend_mode" -DefaultValue "")
                        db_path                       = [string](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "db_path" -DefaultValue "")
                        tasks_total                   = [int](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "tasks_total" -DefaultValue 0)
                        open_execution_tasks          = [int](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "open_execution_tasks" -DefaultValue 0)
                        execution_backlog_gap_detected = [bool](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "execution_backlog_gap_detected" -DefaultValue $false)
                        dag_fingerprint               = [string](Get-V2OptionalProperty -InputObject $taskStateDbSyncResult -Name "dag_fingerprint" -DefaultValue "")
                    }
                })
        }
        catch {
            if ($healthStatus -eq "healthy") {
                $healthStatus = "degraded"
            }
            $syncError = [string]$_.Exception.Message
            $checkResults.Add([PSCustomObject]@{
                    name       = "task-state-db-sync"
                    command    = "task_state_db.py --mode sync"
                    confidence = "verified"
                    status     = "failed"
                    exit_code  = 1
                    details    = [PSCustomObject]@{
                        error = $syncError
                    }
                })
            $incidents.Add((New-IncidentReport -ReportDirectory $reportsDirectory -Category "task-state-db-sync" -Title "Task state DB sync failed" -Details $syncError -DedupPath $script:ObserverIncidentDedupPath -DedupCooldownSeconds $script:ObserverIncidentDedupCooldownSeconds -DedupCategories $script:ObserverIncidentDedupCategories))
        }
    }

    $unknownList = New-Object System.Collections.Generic.List[string]
    foreach ($unknown in @($intake.unknowns)) {
        $unknownText = [string]$unknown
        if (-not [string]::IsNullOrWhiteSpace($unknownText)) {
            $unknownList.Add($unknownText)
        }
    }
    $unknownArray = @($unknownList.ToArray())
    $health = [PSCustomObject]@{
        generated_at = $timestamp
        fingerprint  = $fingerprint
        health_status = $healthStatus
        check_results = @($checkResults.ToArray())
        incidents     = @($incidents.ToArray())
        unknowns      = $unknownArray
    }

    Save-V2JsonContent -Path $healthReportJsonPath -Value $health

    $healthMd = New-Object System.Collections.Generic.List[string]
    $healthMd.Add("# Health Report")
    $healthMd.Add("")
    $healthMd.Add("- Generated At: $timestamp")
    $healthMd.Add("- Fingerprint: $fingerprint")
    $healthMd.Add("- Health Status: $healthStatus")
    $healthMd.Add("- Unknown Count: $(@($intake.unknowns).Count)")
    $healthMd.Add("")
    $healthMd.Add("## Checks")
    if ($checkResults.Count -eq 0) {
        $healthMd.Add("- none executed")
    }
    else {
        foreach ($check in $checkResults) {
            $healthMd.Add("- [$($check.status)] $($check.name): $($check.command) ($($check.confidence))")
        }
    }
    if ($incidents.Count -gt 0) {
        $healthMd.Add("")
        $healthMd.Add("## Incidents")
        foreach ($incident in $incidents) {
            $healthMd.Add("- $incident")
        }
    }
    [System.IO.File]::WriteAllText($healthReportMdPath, ($healthMd -join [Environment]::NewLine))

    Write-PeriodicReports `
        -ReportsDirectory $reportsDirectory `
        -Timestamp $timestamp `
        -HealthStatus $healthStatus `
        -UnknownCount $unknownArray.Count `
        -IncidentCount $incidents.Count `
        -Checks @($checkResults.ToArray())

    Append-ObserverCommunication `
        -OrchestratorRoot $orchestratorRoot `
        -Timestamp $timestamp `
        -HealthStatus $healthStatus `
        -IncidentCount $incidents.Count

    if ($incidents.Count -gt 0 -or $unknownArray.Count -gt 0) {
        $alertPath = Join-Path $orchestratorRoot "communication/alerts.md"
        Add-V2MarkdownLog -Path $alertPath -Header "# Alerts" -Lines @(
            "## $timestamp",
            "- health_status: $healthStatus",
            "- unknown_count: $($unknownArray.Count)",
            "- incident_count: $($incidents.Count)"
        )
    }

    $updatedState = [ordered]@{}
    if ($projectState) {
        foreach ($property in $projectState.PSObject.Properties) {
            $updatedState[$property.Name] = $property.Value
        }
    }
    $updatedState["project_type"] = $intake.project_type
    $updatedState["confidence"] = $intake.confidence
    $updatedState["project_slug"] = $intake.project_slug
    if (-not $updatedState.Contains("project_pack_root")) {
        $updatedState["project_pack_root"] = ("ai-orchestrator/projects/{0}" -f $intake.project_slug)
    }
    if (-not $updatedState.Contains("project_dna_path")) {
        $updatedState["project_dna_path"] = ("ai-orchestrator/projects/{0}/project_dna.json" -f $intake.project_slug)
    }
    $startupPackStatus = [string](Get-V2OptionalProperty -InputObject $projectState -Name "startup_pack_status" -DefaultValue "")
    if ($startupPackStatus -eq "blocked") {
        $updatedState["status"] = "blocked-startup"
    }
    else {
        $updatedState["status"] = $intake.status
    }
    $updatedState["technical_fingerprint"] = $intake.technical_fingerprint
    $updatedState["verified_commands"] = $intake.verified_commands
    $preservedUnknowns = @(
        @(Get-V2OptionalProperty -InputObject $projectState -Name "unknowns" -DefaultValue @()) |
        Where-Object { [string]$_ -like "bootstrap::*" }
    )
    $observerUnknowns = @(Get-V2UniqueObserverStrings -Items @(@($intake.unknowns) + @($preservedUnknowns)))
    $updatedState["unknowns"] = [string[]]@($observerUnknowns)
    $updatedState["open_questions"] = [string[]]@(@($intake.open_questions))
    $updatedState["last_observer_run"] = $timestamp
    $updatedState["last_runtime_update"] = $timestamp
    $updatedState["health_status"] = $healthStatus
    $updatedState["memory_sync"] = if ($memorySyncResult) { $memorySyncResult } else { [PSCustomObject]@{ enabled = $false; reason = "skip-or-not-executed" } }
    $updatedState["task_state_db"] = if ($taskStateDbSyncResult) { $taskStateDbSyncResult } else { [PSCustomObject]@{ enabled = $false; reason = "skip-or-not-executed" } }
    $updatedState["updated_at"] = $timestamp
    $updatedStateObject = New-Object PSObject -Property $updatedState
    $phaseApprovalsChanged = Initialize-V2PhaseApprovals -ProjectState $updatedStateObject -UpdatedBy "observer-auto"
    if ($phaseApprovalsChanged) {
        Set-V2DynamicProperty -InputObject $updatedStateObject -Name "updated_at" -Value (Get-V2Timestamp)
    }
    Save-V2JsonContent -Path $projectStatePath -Value $updatedStateObject

    Update-ObserverProjectDna `
        -ProjectRoot $resolvedProjectPath `
        -ProjectState $updatedStateObject `
        -Timestamp $timestamp `
        -HealthStatus $healthStatus `
        -Unknowns @($unknownArray) `
        -Incidents @($incidents.ToArray())

    Save-V2JsonContent -Path $observerStatePath -Value ([PSCustomObject]@{
        last_run_at      = $timestamp
        last_fingerprint = $fingerprint
        cycle_index = $observerCycleIndex
        cycle_mode = $observerCycleMode
        heavy_cycle_cadence = $projectHeavyCadence
        heavy_stage_timeout_seconds = $HeavyStageTimeoutSeconds
        last_heavy_run_at = if ($runHeavyCycle) { $timestamp } else { [string](Get-V2OptionalProperty -InputObject $observerState -Name "last_heavy_run_at" -DefaultValue "") }
        qdrant_fallback_deferred_cycles = $qdrantFallbackDeferredCycles
        qdrant_fallback_deferred_max_cycles = $qdrantFallbackDeferredMaxCycles
    })

    Write-Host "Observer status: $healthStatus" -ForegroundColor Cyan
    if ($RunOnce) { break }
    Start-Sleep -Seconds $IntervalSeconds
}
