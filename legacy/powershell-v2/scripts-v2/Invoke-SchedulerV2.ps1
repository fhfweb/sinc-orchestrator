<#
.SYNOPSIS
    V2 Task Scheduler - assigns pending tasks to agents using priority scoring and lock management.
.DESCRIPTION
    Reads task-dag.json and locks.json from a project .ai-orchestrator layer.
    Calculates priority score for each pending task (weight + aging + dependency bonus - conflict penalty).
    Assigns tasks to agents based on reputation scores. Respects operational mode from STRATEGIC_PLANNING_ENGINE.md.
.PARAMETER ProjectPath
    Path to the project root containing .ai-orchestrator/. Defaults to current directory.
.PARAMETER LockTtlSeconds
    Lock TTL in seconds. Default: 7200 (2 hours).
.PARAMETER MaxAssignmentsPerRun
    Maximum number of task assignments per scheduler cycle. Default: 6.
.PARAMETER EmitJson
    If set, outputs assignment result as JSON.
.EXAMPLE
    .\scripts\v2\Invoke-SchedulerV2.ps1 -ProjectPath C:\projects\myapp
#>param(
    [string]$ProjectPath = ".",
    [int]$LockTtlSeconds = 7200,
    [int]$IdlePendingTakeoverMinutes = 10,
    [int]$IdleInProgressTakeoverMinutes = 30,
    [int]$RepairOrphanTakeoverMinutes = 12,
    [int]$LockConflictRetryMinutes = 8,
    [int]$MaxAssignmentsPerRun = 6,
    [bool]$UseTaskStateDb = $true,
    [int]$TaskStateDbFlushCooldownSeconds = 45,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Polyfill for PowerShell < 6.0
if (-not (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue)) {
    $IsWindows = $env:OS -like "*Windows*"
}
if (-not (Get-Variable -Name IsLinux -ErrorAction SilentlyContinue)) {
    $IsLinux = $false
}
if (-not (Get-Variable -Name IsMacOS -ErrorAction SilentlyContinue)) {
    $IsMacOS = $false
}

. (Join-Path $PSScriptRoot "Common.ps1")

function Set-V2ObjectProperty {
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

function Compress-V2TaskPayload {
    param([object]$Task)

    if (-not $Task) { return $false }
    $changed = $false

    if ($Task.PSObject.Properties.Name -contains "completion_payload") {
        [void]$Task.PSObject.Properties.Remove("completion_payload")
        $changed = $true
    }

    $fieldLimits = @{
        description                = 1200
        reason                     = 600
        completion_note            = 1200
        completion_payload_summary = 1200
        output_snippet             = 800
        original_line              = 300
        mutated_line               = 300
    }
    foreach ($fieldName in $fieldLimits.Keys) {
        if (-not ($Task.PSObject.Properties.Name -contains $fieldName)) { continue }
        $value = [string](Get-V2OptionalProperty -InputObject $Task -Name $fieldName -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        $maxLen = [int]$fieldLimits[$fieldName]
        if ($value.Length -le $maxLen) { continue }
        Set-V2ObjectProperty -InputObject $Task -Name $fieldName -Value ($value.Substring(0, $maxLen) + "…")
        $changed = $true
    }

    if ($Task.PSObject.Properties.Name -contains "completion_payload_changes") {
        $rawChanges = @(Get-V2TaskArrayProperty -Task $Task -Name "completion_payload_changes")
        $normalizedChanges = New-Object System.Collections.Generic.List[string]
        foreach ($entry in $rawChanges) {
            if ($normalizedChanges.Count -ge 20) { break }
            $text = [string]$entry
            if ([string]::IsNullOrWhiteSpace($text)) { continue }
            $trimmed = $text.Trim()
            if ($trimmed.Length -gt 300) {
                $trimmed = $trimmed.Substring(0, 300) + "…"
            }
            $normalizedChanges.Add($trimmed)
        }
        if ($normalizedChanges.Count -ne $rawChanges.Count) {
            Set-V2ObjectProperty -InputObject $Task -Name "completion_payload_changes" -Value @($normalizedChanges.ToArray())
            $changed = $true
        }
    }

    return $changed
}

function Get-V2TaskStatus {
    param([object]$Task)

    $raw = [string](Get-V2OptionalProperty -InputObject $Task -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return "pending"
    }

    $normalized = $raw.ToLowerInvariant()
    if ($normalized -eq "open") {
        return "pending"
    }

    return $normalized
}

function Get-V2TaskArrayProperty {
    param(
        [object]$Task,
        [string]$Name
    )

    $value = Get-V2OptionalProperty -InputObject $Task -Name $Name -DefaultValue @()
    if ($null -eq $value) {
        return @()
    }

    if ($value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($value)) { return @() }
        return @($value)
    }

    return @($value)
}

function Get-V2EnvBool {
    param(
        [string]$Name,
        [bool]$DefaultValue = $false
    )

    $raw = [string][System.Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $DefaultValue
    }

    $normalized = $raw.Trim().ToLowerInvariant()
    if ($normalized -in @("1", "true", "yes", "on")) {
        return $true
    }
    if ($normalized -in @("0", "false", "no", "off")) {
        return $false
    }

    return $DefaultValue
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

function Invoke-V2TaskStateDbCommand {
    param(
        [string]$TaskStateDbScriptPath,
        [string]$ProjectRoot,
        [string]$Mode,
        [hashtable]$ExtraArgs = @{}
    )

    if (-not (Test-Path -LiteralPath $TaskStateDbScriptPath -PathType Leaf)) {
        throw "task-state-db-script-missing"
    }

    $argList = @($TaskStateDbScriptPath, "--project-path", $ProjectRoot, "--mode", $Mode, "--emit-json")
    foreach ($entry in $ExtraArgs.GetEnumerator()) {
        $key = [string]$entry.Key
        $value = [string]$entry.Value
        if ([string]::IsNullOrWhiteSpace($key) -or [string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        $argList += @("--$key", $value)
    }

    $output = @(python @argList 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $tail = ($output | Select-Object -Last 20) -join [Environment]::NewLine
        throw ("task-state-db-command-failed mode={0} exit={1} output={2}" -f $Mode, $LASTEXITCODE, $tail)
    }

    $rawJson = (($output -join [Environment]::NewLine).Trim())
    if ([string]::IsNullOrWhiteSpace($rawJson)) {
        throw ("task-state-db-command-empty-output mode={0}" -f $Mode)
    }

    try {
        return ($rawJson | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw ("task-state-db-command-non-json mode={0} output={1}" -f $Mode, $rawJson)
    }
}

function Get-V2SchedulerDbState {
    param([string]$Path)

    $state = Get-V2JsonContent -Path $Path
    if (-not $state) {
        return [PSCustomObject]@{
            last_flush_at     = ""
            last_flush_reason = ""
            backend_mode      = ""
        }
    }
    return $state
}

function Save-V2SchedulerDbState {
    param(
        [string]$Path,
        [string]$LastFlushAt,
        [string]$LastFlushReason,
        [string]$BackendMode
    )

    $doc = [PSCustomObject]@{
        last_flush_at     = $LastFlushAt
        last_flush_reason = $LastFlushReason
        backend_mode      = $BackendMode
        updated_at        = Get-V2Timestamp
    }
    Save-V2JsonContent -Path $Path -Value $doc
}

function Get-V2TaskDagTextFingerprint {
    param([string]$DagPath)

    if ([string]::IsNullOrWhiteSpace($DagPath)) {
        return ""
    }
    if (-not (Test-Path -LiteralPath $DagPath -PathType Leaf)) {
        return ""
    }

    try {
        try {
            $utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
            $raw = [System.IO.File]::ReadAllText($DagPath, $utf8Strict)
        }
        catch {
            $raw = [System.IO.File]::ReadAllText($DagPath, [System.Text.Encoding]::UTF8)
        }

        if ([string]::IsNullOrWhiteSpace($raw)) {
            return ""
        }
        return (Get-V2Sha256Text -InputText $raw)
    }
    catch {
        return ""
    }
}

function Test-V2TaskDagManualDrift {
    param(
        [string]$DagPath,
        [object]$TaskStateStatus
    )

    if (-not (Test-Path -LiteralPath $DagPath -PathType Leaf)) {
        return $false
    }

    $dbFingerprint = [string](Get-V2OptionalProperty -InputObject $TaskStateStatus -Name "dag_fingerprint" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($dbFingerprint)) {
        return $false
    }

    $dagFingerprint = Get-V2TaskDagTextFingerprint -DagPath $DagPath
    if ([string]::IsNullOrWhiteSpace($dagFingerprint)) {
        return $false
    }
    if ($dagFingerprint -eq $dbFingerprint) {
        return $false
    }

    $lastFlushAtRaw = [string](Get-V2OptionalProperty -InputObject $TaskStateStatus -Name "last_dag_flush_at" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($lastFlushAtRaw)) {
        return $true
    }

    try {
        $lastFlushUtc = ([DateTimeOffset]::Parse($lastFlushAtRaw)).UtcDateTime
        $dagWriteUtc = (Get-Item -LiteralPath $DagPath -ErrorAction Stop).LastWriteTimeUtc
        return ($dagWriteUtc -gt $lastFlushUtc.AddSeconds(1))
    }
    catch {
        return $true
    }
}

function Resolve-V2TaskExecutionProfile {
    param(
        [object]$Task,
        [bool]$RoutingEnabled,
        [int]$NativeComplexityThreshold,
        [int]$NativeMaxFiles,
        [int]$NativeMaxDependencies
    )

    $result = [PSCustomObject]@{
        profile          = "external-agent"
        execution_mode   = "external-agent"
        runtime_engine   = "hybrid"
        reason           = "routing-default-external"
        complexity_score = 0
    }

    if (-not $Task) {
        return $result
    }

    $currentMode = [string](Get-V2OptionalProperty -InputObject $Task -Name "execution_mode" -DefaultValue "")
    $currentModeNormalized = $currentMode.Trim().ToLowerInvariant()
    $taskId = [string](Get-V2OptionalProperty -InputObject $Task -Name "id" -DefaultValue "")
    $taskIdUpper = $taskId.ToUpperInvariant()
    $taskType = [string](Get-V2OptionalProperty -InputObject $Task -Name "type" -DefaultValue "")
    $fallbackApplied = [bool](Get-V2OptionalProperty -InputObject $Task -Name "runtime_missing_handler_fallback_applied" -DefaultValue $false)

    if ($fallbackApplied) {
        $result.reason = "fallback-missing-handler-external"
        return $result
    }

    if ($currentModeNormalized -in @("project-completion-gate", "artifact-validation")) {
        $result.reason = "explicit-special-mode"
        return $result
    }

    if ($currentModeNormalized -in @("native-agent", "llm-native", "python-runtime", "autonomous-native", "v4-native")) {
        $result.profile = "native-gpu"
        $result.execution_mode = "llm-native"
        $result.runtime_engine = "python"
        $result.reason = "explicit-native-mode"
        return $result
    }

    if (-not $RoutingEnabled) {
        $result.reason = "routing-disabled"
        return $result
    }

    if ($taskIdUpper.StartsWith("FEAT-") -or $taskIdUpper.StartsWith("FEATURE-")) {
        $result.reason = "forced-external-product-feat"
        return $result
    }

    if (
        $taskIdUpper.StartsWith("REPAIR-DEPLOY-") -or
        $taskIdUpper.StartsWith("REPAIR-TEST-FAIL-") -or
        $taskIdUpper.StartsWith("COBERTURA-FALHA-") -or
        $taskIdUpper.StartsWith("REL-")
    ) {
        $result.reason = "forced-external-critical-id"
        return $result
    }

    $filesAffected = @(Get-V2TaskArrayProperty -Task $Task -Name "files_affected")
    if (@($filesAffected).Count -eq 0) {
        $filesAffected = @(Get-V2TaskArrayProperty -Task $Task -Name "affected_files")
    }
    $fileCount = @($filesAffected).Count

    $infraMarkers = @(
        "ai-orchestrator/",
        "scripts/v2/",
        "scripts/run-agentloop.ps1",
        "scripts/run-autonomousloop.ps1",
        "ai-orchestrator/docker",
        "docker-compose",
        "k8s/",
        "kubernetes/",
        "infra/",
        ".github/workflows",
        "database/migrations",
        ".env"
    )
    $infraSurfaceHit = $false
    foreach ($pathValue in @($filesAffected)) {
        $normalizedPath = ([string]$pathValue).Replace("\", "/").Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($normalizedPath)) { continue }
        foreach ($marker in $infraMarkers) {
            if ($normalizedPath.Contains($marker)) {
                $infraSurfaceHit = $true
                break
            }
        }
        if ($infraSurfaceHit) { break }
    }

    $isInfraTaskFamily = $taskIdUpper.StartsWith("TASK-") -or $taskIdUpper.StartsWith("REPAIR-")
    $taskTypeNormalized = $taskType.Trim().ToLowerInvariant()
    $isInfraTypedTask = $taskTypeNormalized.Contains("infra") -or $taskTypeNormalized.Contains("orchestrator")
    if ($isInfraTaskFamily -and ($infraSurfaceHit -or $isInfraTypedTask)) {
        $result.profile = "native-gpu"
        $result.execution_mode = "llm-native"
        $result.runtime_engine = "python"
        $result.reason = "forced-native-infra-task"
        return $result
    }

    $dependencies = @(Get-V2TaskArrayProperty -Task $Task -Name "dependencies")
    if (@($dependencies).Count -eq 0) {
        $dependencies = @(Get-V2TaskArrayProperty -Task $Task -Name "depends_on")
    }
    $depCount = @($dependencies).Count

    $priority = [string](Get-V2OptionalProperty -InputObject $Task -Name "priority" -DefaultValue "P3")
    $priorityScore = switch ($priority.ToUpperInvariant()) {
        "P0" { 4 }
        "P1" { 3 }
        "P2" { 2 }
        default { 1 }
    }

    $description = [string](Get-V2OptionalProperty -InputObject $Task -Name "description" -DefaultValue "")
    $descScore = [Math]::Min(4, [int][Math]::Floor($description.Length / 500.0))
    $complexity = $priorityScore + [Math]::Min(6, $fileCount) + [Math]::Min(4, $depCount) + $descScore
    $result.complexity_score = [int]$complexity

    if ($fileCount -gt $NativeMaxFiles) {
        $result.reason = "forced-external-many-files"
        return $result
    }
    if ($depCount -gt $NativeMaxDependencies) {
        $result.reason = "forced-external-many-dependencies"
        return $result
    }

    if ($complexity -le $NativeComplexityThreshold) {
        $result.profile = "native-gpu"
        $result.execution_mode = "llm-native"
        $result.runtime_engine = "python"
        $result.reason = "auto-native-low-complexity"
        return $result
    }

    $result.reason = "auto-external-high-complexity"
    return $result
}

function ConvertTo-V2UtcDateTime {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    try {
        return ([DateTime]::Parse($Value)).ToUniversalTime()
    }
    catch {
        return $null
    }
}

function Get-V2TaskIdleMinutes {
    param(
        [object]$Task,
        [DateTime]$NowUtc
    )

    if (-not $Task) {
        return -1.0
    }

    $updatedAtRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "updated_at" -DefaultValue "")
    $startedAtRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "started_at" -DefaultValue "")
    $assignedAtRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "assigned_at" -DefaultValue "")

    $lastActivity = ConvertTo-V2UtcDateTime -Value $updatedAtRaw
    if (-not $lastActivity) {
        $lastActivity = ConvertTo-V2UtcDateTime -Value $startedAtRaw
    }
    if (-not $lastActivity) {
        $lastActivity = ConvertTo-V2UtcDateTime -Value $assignedAtRaw
    }
    if (-not $lastActivity) {
        return -1.0
    }

    return [Math]::Round(([TimeSpan]($NowUtc - $lastActivity)).TotalMinutes, 2)
}

function Test-V2RecoveryTask {
    param([object]$Task)

    if (-not $Task) { return $false }

    $taskId = [string](Get-V2OptionalProperty -InputObject $Task -Name "id" -DefaultValue "")
    $taskType = [string](Get-V2OptionalProperty -InputObject $Task -Name "type" -DefaultValue "")
    $allowWhenBlocked = [bool](Get-V2OptionalProperty -InputObject $Task -Name "allow_when_blocked" -DefaultValue $false)

    if ($taskId.StartsWith("REPAIR-", [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($allowWhenBlocked) { return $true }
    if ($taskType -match "^(recovery|bootstrap|infra-fix)$") { return $true }

    return $false
}

function Get-V2PriorityWeight {
    param([string]$Priority)

    $priorityValue = if ($null -eq $Priority) { "" } else { [string]$Priority }
    switch ($priorityValue.ToUpperInvariant()) {
        "P0" { return 100 }
        "P1" { return 70 }
        "P2" { return 40 }
        "P3" { return 10 }
        default { return 0 }
    }
}

function Get-V2StrategicMode {
    param([string]$ProjectRoot)

    $planningPath = Join-Path $ProjectRoot "docs/agents/STRATEGIC_PLANNING_ENGINE.md"
    if (-not (Test-Path -LiteralPath $planningPath -PathType Leaf)) {
        return "NORMAL"
    }

    try {
        $content = Get-Content -LiteralPath $planningPath -Raw
        $match = [regex]::Match($content, "(?im)^\s*##\s*Current Mode:\s*\[(?<mode>[A-Z]+)\]")
        if (-not $match.Success) {
            $match = [regex]::Match($content, "(?im)^Current Mode:\s*\*\*\[(?<mode>[A-Z]+)\]\*\*")
        }
        if ($match.Success) {
            $mode = [string]$match.Groups["mode"].Value
            if ($mode -in @("STABILIZE", "ACCELERATE", "CONSOLIDATE", "NORMAL")) {
                return $mode
            }
        }
    }
    catch {
    }

    return "NORMAL"
}

function Get-V2TaskModeCategory {
    param([object]$Task)

    if (-not $Task) { return "OTHER" }

    $taskId = [string](Get-V2OptionalProperty -InputObject $Task -Name "id" -DefaultValue "")
    $taskType = [string](Get-V2OptionalProperty -InputObject $Task -Name "type" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($taskType)) {
        $taskType = [string](Get-V2OptionalProperty -InputObject $Task -Name "task_type" -DefaultValue "")
    }
    if ([string]::IsNullOrWhiteSpace($taskType)) {
        $taskType = [string](Get-V2OptionalProperty -InputObject $Task -Name "category" -DefaultValue "")
    }
    $description = [string](Get-V2OptionalProperty -InputObject $Task -Name "description" -DefaultValue "")
    $title = [string](Get-V2OptionalProperty -InputObject $Task -Name "title" -DefaultValue "")
    $blob = ($taskId + " " + $taskType + " " + $title + " " + $description).ToUpperInvariant()

    if ($blob -match "REPAIR|BUG|HOTFIX|INCIDENT|SELF-HEAL") { return "BUGFIX" }
    if ($blob -match "TEST|QA|ASSERT|COVERAGE") { return "TEST" }
    if ($blob -match "REFACTOR|CLEANUP|DECOUPLE|MODERNIZ") { return "REFACTOR" }
    if ($blob -match "DOC|DOCUMENT|READ(ME)?|GUIDE|ADR") { return "DOCS" }
    if ($blob -match "DEBT|PERF|OPTIMIZ|HARDEN|SECURITY") { return "DEBT" }
    if ($blob -match "FEATURE|UI|UX|FLOW|SCREEN|API|ENDPOINT|CREATE|BUILD") { return "FEATURE" }

    return "OTHER"
}

function Get-V2ModePriorityBonus {
    param(
        [string]$StrategicMode,
        [object]$Task
    )

    $category = Get-V2TaskModeCategory -Task $Task
    switch ($StrategicMode) {
        "STABILIZE" {
            if ($category -eq "BUGFIX") { return 35 }
            if ($category -eq "TEST") { return 20 }
            if ($category -eq "FEATURE") { return -200 }
            return 0
        }
        "ACCELERATE" {
            if ($category -eq "FEATURE") { return 25 }
            if ($category -eq "DEBT") { return -20 }
            return 0
        }
        "CONSOLIDATE" {
            if ($category -in @("REFACTOR", "DOCS", "TEST", "DEBT")) { return 20 }
            if ($category -eq "FEATURE") { return -200 }
            return 0
        }
        default { return 0 }
    }
}

function Test-V2TaskAllowedByMode {
    param(
        [string]$StrategicMode,
        [object]$Task
    )

    $category = Get-V2TaskModeCategory -Task $Task
    switch ($StrategicMode) {
        "STABILIZE" { return ($category -ne "FEATURE") }
        "CONSOLIDATE" { return ($category -ne "FEATURE") }
        default { return $true }
    }
}

function Test-V2DependenciesResolved {
    param(
        [object]$Task,
        [hashtable]$TaskIndex
    )

    $dependencies = @(Get-V2TaskArrayProperty -Task $Task -Name "dependencies")
    foreach ($dependencyId in $dependencies) {
        $depIdText = [string]$dependencyId
        if ([string]::IsNullOrWhiteSpace($depIdText)) {
            continue
        }
        if (-not $TaskIndex.ContainsKey($depIdText)) {
            return $false
        }

        $depStatus = Get-V2TaskStatus -Task $TaskIndex[$depIdText]
        if ($depStatus -notin @("done", "completed", "skipped")) {
            return $false
        }
    }

    return $true
}

function Get-V2PhaseApprovalStatus {
    param(
        [object]$ProjectState,
        [string]$PhaseName
    )

    if ([string]::IsNullOrWhiteSpace($PhaseName)) {
        return "approved"
    }

    $phaseApprovals = Get-V2OptionalProperty -InputObject $ProjectState -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
    $phaseEntry = Get-V2OptionalProperty -InputObject $phaseApprovals -Name $PhaseName -DefaultValue ([PSCustomObject]@{ status = "pending" })
    $status = [string](Get-V2OptionalProperty -InputObject $phaseEntry -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($status)) {
        $status = "pending"
    }
    return $status.ToLowerInvariant()
}

function Test-V2TaskLockCoverage {
    param(
        [object]$LocksDoc,
        [string]$TaskId,
        [string]$Agent,
        [object[]]$FilesAffected
    )

    if ([string]::IsNullOrWhiteSpace($TaskId) -or [string]::IsNullOrWhiteSpace($Agent)) {
        return $false
    }

    $expectedFiles = @(
        @($FilesAffected | ForEach-Object { Get-V2NormalizedPath -Path ([string]$_) }) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
    )
    if ($expectedFiles.Count -eq 0) {
        return $true
    }

    if (-not $LocksDoc -or -not ($LocksDoc.PSObject.Properties.Name -contains "locks")) {
        return $false
    }

    $activeStatuses = @("active", "locked", "in-progress")
    $ownedFiles = @(
        @($LocksDoc.locks | Where-Object {
                $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "active")
                if ([string]::IsNullOrWhiteSpace($status)) { $status = "active" }
                if ($status.ToLowerInvariant() -notin $activeStatuses) { return $false }

                $lockTaskId = [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "")
                $lockAgent = [string](Get-V2OptionalProperty -InputObject $_ -Name "agent" -DefaultValue "")
                if ($lockTaskId -ne $TaskId -or $lockAgent -ne $Agent) { return $false }
                return $true
            }) | ForEach-Object {
            Get-V2NormalizedPath -Path ([string](Get-V2OptionalProperty -InputObject $_ -Name "file_path" -DefaultValue ""))
        } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Sort-Object -Unique
    )

    foreach ($filePath in $expectedFiles) {
        if ($filePath -notin $ownedFiles) {
            return $false
        }
    }

    return $true
}

function New-V2DefaultWorkload {
    return [PSCustomObject]@{
        agents = @(
            [PSCustomObject]@{ name = "Codex"; role = "engineering"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "Claude"; role = "architecture"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "Antigravity"; role = "product"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI CTO"; role = "strategy"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI Architect"; role = "architecture"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI DevOps Engineer"; role = "devops"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI Security Engineer"; role = "security"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI Product Manager"; role = "product"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
            [PSCustomObject]@{ name = "AI Engineer"; role = "engineering"; active_tasks = 0; completed_tasks = 0; last_assigned = "" }
        )
    }
}

function Ensure-V2Workload {
    param([string]$WorkloadPath)

    $workload = Get-V2JsonContent -Path $WorkloadPath
    if (-not $workload -or -not ($workload.PSObject.Properties.Name -contains "agents")) {
        $workload = New-V2DefaultWorkload
        Save-V2JsonContent -Path $WorkloadPath -Value $workload
    }

    return $workload
}

function Get-V2AgentIndex {
    param([object]$Workload)

    $index = @{}
    foreach ($agent in @($Workload.agents)) {
        if (-not $agent) { continue }
        $name = [string](Get-V2OptionalProperty -InputObject $agent -Name "name" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $index[$name] = $agent
    }
    return $index
}

function Get-V2CandidateAgents {
    param(
        [string]$PreferredAgent,
        [hashtable]$AgentIndex
    )

    $groups = @{
        # ── Legacy / original agents ───────────────────────────────────────────
        # AI Architect fallback: Claude first, then CTO for escalation
        "AI Architect"            = @("AI Architect", "Claude", "AI CTO")
        "Claude"                  = @("Claude", "AI Architect")
        "AI Product Manager"      = @("AI Product Manager", "Antigravity")
        "Antigravity"             = @("Antigravity", "AI Product Manager")
        "AI Engineer"             = @("AI Engineer", "Codex")
        "AI Developer"            = @("AI Engineer", "Codex")
        "AI Frontend Engineer"    = @("Codex", "AI Engineer")
        "AI Integration Engineer" = @("Codex", "AI Engineer")
        "AI QA"                   = @("Codex", "AI Engineer")
        "Codex"                   = @("Codex", "AI Engineer")
        "AI DevOps Engineer"      = @("Codex", "AI Engineer", "AI DevOps Engineer")
        "AI Security Engineer"    = @("AI Security Engineer")
        "AI CTO"                  = @("AI CTO")

        # ── 21-Agent Ecosystem ─────────────────────────────────────────────────
        # ESTRATEGIA
        "Business Analyst"        = @("Business Analyst", "AI Product Manager")

        # CONSTRUCAO
        "AI Engineer Frontend"    = @("AI Engineer Frontend", "AI Engineer", "Codex")
        "Database Agent"          = @("Database Agent", "AI Engineer")
        "Integration Agent"       = @("Integration Agent", "AI Engineer", "Codex")

        # QUALIDADE
        "Code Review Agent"       = @("Code Review Agent", "AI Architect", "AI Engineer")
        "Performance Agent"       = @("Performance Agent", "AI Engineer", "Database Agent")
        "QA Agent"                = @("QA Agent", "AI Engineer", "Codex")

        # OPERACOES
        "DevOps Agent"            = @("DevOps Agent", "AI DevOps Engineer")
        "User Simulation Agent"   = @("User Simulation Agent", "QA Agent", "AI Engineer")
        "Observability Agent"     = @("Observability Agent", "AI DevOps Engineer")
        "Incident Response Agent" = @("Incident Response Agent", "AI CTO", "AI DevOps Engineer")

        # INTELIGENCIA
        "Memory Agent"            = @("Memory Agent", "AI Architect")
        "Learning Agent"          = @("Learning Agent", "AI Architect")
        "Estimation Agent"        = @("Estimation Agent", "AI Product Manager", "Business Analyst")

        # COORDENACAO
        "Documentation Agent"     = @("Documentation Agent", "AI Engineer", "AI Architect")
    }

    $preferred = [string]$PreferredAgent
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($preferred) -and $groups.ContainsKey($preferred)) {
        $candidates = @($groups[$preferred])
    }
    elseif (-not [string]::IsNullOrWhiteSpace($preferred)) {
        $candidates = @($preferred)
    }
    else {
        $candidates = @($AgentIndex.Keys)
    }

    $resolved = New-Object System.Collections.Generic.List[object]
    foreach ($candidateName in $candidates) {
        if ($AgentIndex.ContainsKey($candidateName)) {
            $resolved.Add($AgentIndex[$candidateName])
        }
    }

    return @($resolved.ToArray())
}

function Select-V2BestAgent {
    param(
        [object[]]$Candidates,
        [object]$Task,
        [hashtable]$ReputationIndex,
        [hashtable]$CalibrationIndex
    )

    if (@($Candidates).Count -eq 0) {
        return $null
    }

    $priorityScore = (Get-V2PriorityWeight -Priority ([string](Get-V2OptionalProperty -InputObject $Task -Name "priority" -DefaultValue "P3"))) + 1
    $scored = New-Object System.Collections.Generic.List[object]
    foreach ($candidate in @($Candidates)) {
        $agentName = [string](Get-V2OptionalProperty -InputObject $candidate -Name "name" -DefaultValue "")
        $activeTasks = [int](Get-V2OptionalProperty -InputObject $candidate -Name "active_tasks" -DefaultValue 0)
        $completedTasks = [int](Get-V2OptionalProperty -InputObject $candidate -Name "completed_tasks" -DefaultValue 0)

        $fitScore = 0.70
        if ($ReputationIndex.ContainsKey($agentName)) {
            $repEntry = $ReputationIndex[$agentName]
            $fitFromRep = [double](Get-V2OptionalProperty -InputObject $repEntry -Name "reputation_fit_score" -DefaultValue 0.70)
            if ($fitFromRep -gt 0) {
                $fitScore = $fitFromRep
            }
        }

        $timeoutPenalty = 0.0
        if ($CalibrationIndex.ContainsKey($agentName)) {
            $calEntry = $CalibrationIndex[$agentName]
            $timeoutPenalty = [double](Get-V2OptionalProperty -InputObject $calEntry -Name "timeout_rate" -DefaultValue 0.0) * 20.0
            $avgDuration = [double](Get-V2OptionalProperty -InputObject $calEntry -Name "avg_duration_ms" -DefaultValue 0.0)
            if ($avgDuration -gt 120000.0) {
                $timeoutPenalty += [Math]::Min(10.0, (($avgDuration - 120000.0) / 120000.0) * 5.0)
            }
        }

        $adjustedScore = ($priorityScore * $fitScore) - $timeoutPenalty - ($activeTasks * 5.0)
        if ($completedTasks -gt 0) {
            $adjustedScore += [Math]::Min(2.0, $completedTasks * 0.1)
        }

        $scored.Add([PSCustomObject]@{
                agent                = $candidate
                adjusted_score       = [Math]::Round($adjustedScore, 3)
                reputation_fit_score = [Math]::Round($fitScore, 3)
                timeout_penalty      = [Math]::Round($timeoutPenalty, 3)
                active_tasks         = $activeTasks
                completed_tasks      = $completedTasks
            })
    }

    return @(
        @($scored.ToArray()) |
        Sort-Object `
        @{ Expression = { [double](Get-V2OptionalProperty -InputObject $_ -Name "adjusted_score" -DefaultValue 0.0) }; Descending = $true },
        @{ Expression = { [int](Get-V2OptionalProperty -InputObject $_ -Name "active_tasks" -DefaultValue 0) }; Descending = $false },
        @{ Expression = { [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $_ -Name "agent" -DefaultValue ([PSCustomObject]@{})) -Name "name" -DefaultValue "") }; Descending = $false }
    )[0]
}

function Reset-V2WorkloadFromTasks {
    param(
        [object]$Workload,
        [object[]]$Tasks
    )

    foreach ($agent in @($Workload.agents)) {
        if (-not $agent) { continue }
        Set-V2ObjectProperty -InputObject $agent -Name "active_tasks" -Value 0
        Set-V2ObjectProperty -InputObject $agent -Name "completed_tasks" -Value 0
    }

    $agentIndex = Get-V2AgentIndex -Workload $Workload
    foreach ($task in @($Tasks)) {
        $assignedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($assignedAgent)) { continue }
        if (-not $agentIndex.ContainsKey($assignedAgent)) { continue }

        $status = Get-V2TaskStatus -Task $task
        $entry = $agentIndex[$assignedAgent]
        if ($status -eq "in-progress") {
            $entry.active_tasks = [int](Get-V2OptionalProperty -InputObject $entry -Name "active_tasks" -DefaultValue 0) + 1
        }
        elseif ($status -in @("done", "completed", "skipped")) {
            $entry.completed_tasks = [int](Get-V2OptionalProperty -InputObject $entry -Name "completed_tasks" -DefaultValue 0) + 1
        }
    }
}

function Write-V2TaskBoardsFromJson {
    param(
        [string]$OrchestratorRoot,
        [object[]]$Tasks
    )

    $taskDagPath = Join-Path $OrchestratorRoot "tasks/task-dag.md"
    $backlogPath = Join-Path $OrchestratorRoot "tasks/backlog.md"
    $inProgressPath = Join-Path $OrchestratorRoot "tasks/in-progress.md"
    $completedPath = Join-Path $OrchestratorRoot "tasks/completed.md"

    $sortedTasks = @(
        @($Tasks) |
        Sort-Object `
        @{ Expression = { Get-V2PriorityWeight -Priority ([string](Get-V2OptionalProperty -InputObject $_ -Name "priority" -DefaultValue "P3")) }; Descending = $true },
        @{ Expression = { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") }; Descending = $false }
    )

    $dagLines = New-Object System.Collections.Generic.List[string]
    $dagLines.Add("# Task DAG")
    $dagLines.Add("")
    $dagLines.Add("~~~yaml")
    $dagLines.Add("tasks:")
    foreach ($task in $sortedTasks) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "unknown")
        $description = [string](Get-V2OptionalProperty -InputObject $task -Name "description" -DefaultValue "")
        $priority = [string](Get-V2OptionalProperty -InputObject $task -Name "priority" -DefaultValue "P3")
        $assignedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
        $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "pending")
        $blockedReason = [string](Get-V2OptionalProperty -InputObject $task -Name "blocked_reason" -DefaultValue "")
        $dependencies = @(Get-V2TaskArrayProperty -Task $task -Name "dependencies")
        $files = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")

        $depText = if ($dependencies.Count -eq 0) { "[]" } else { "[{0}]" -f (($dependencies | ForEach-Object { [string]$_ }) -join ", ") }

        $dagLines.Add("  - id: $taskId")
        $dagLines.Add("    description: $description")
        $dagLines.Add("    priority: $priority")
        $dagLines.Add("    dependencies: $depText")
        $dagLines.Add("    assigned_agent: $assignedAgent")
        $dagLines.Add("    status: $status")
        $dagLines.Add("    files_affected:")
        if ($files.Count -eq 0) {
            $dagLines.Add("      - none")
        }
        else {
            foreach ($file in $files) {
                $dagLines.Add("      - $([string]$file)")
            }
        }
        if (-not [string]::IsNullOrWhiteSpace($blockedReason)) {
            $dagLines.Add("    blocked_reason: $blockedReason")
        }
        $dagLines.Add("")
    }
    $dagLines.Add("~~~")
    $dagLines.Add("")
    $dagLines.Add("## Scheduler Rule")
    $dagLines.Add("- deny assignment when any files_affected entry overlaps active lock entries in /ai-orchestrator/locks/locks.json")
    $dagLines.Add("- deny assignment when dependencies are not done")
    Write-V2File -Path $taskDagPath -Content ($dagLines -join [Environment]::NewLine) -Force

    $backlogLines = New-Object System.Collections.Generic.List[string]
    $backlogLines.Add("# Backlog")
    $backlogLines.Add("")
    $backlogTasks = @($sortedTasks | Where-Object {
            $status = (Get-V2TaskStatus -Task $_)
            $status -notin @("done", "completed", "skipped", "in-progress")
        })
    if ($backlogTasks.Count -eq 0) {
        $backlogLines.Add("- none")
    }
    else {
        foreach ($task in $backlogTasks) {
            $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "unknown")
            $description = [string](Get-V2OptionalProperty -InputObject $task -Name "description" -DefaultValue "")
            $priority = [string](Get-V2OptionalProperty -InputObject $task -Name "priority" -DefaultValue "P3")
            $assignedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "pending")
            $dependencies = @(Get-V2TaskArrayProperty -Task $task -Name "dependencies")
            $blockedReason = [string](Get-V2OptionalProperty -InputObject $task -Name "blocked_reason" -DefaultValue "")

            $depText = if ($dependencies.Count -eq 0) { "[]" } else { "[{0}]" -f (($dependencies | ForEach-Object { [string]$_ }) -join ", ") }
            $backlogLines.Add("- id: $taskId")
            $backlogLines.Add("  description: $description")
            $backlogLines.Add("  priority: $priority")
            $backlogLines.Add("  dependencies: $depText")
            $backlogLines.Add("  assigned_agent: $assignedAgent")
            $backlogLines.Add("  status: $status")
            if (-not [string]::IsNullOrWhiteSpace($blockedReason)) {
                $backlogLines.Add("  blocked_reason: $blockedReason")
            }
            $backlogLines.Add("")
        }
    }
    Write-V2File -Path $backlogPath -Content ($backlogLines -join [Environment]::NewLine) -Force

    $inProgressLines = New-Object System.Collections.Generic.List[string]
    $inProgressLines.Add("# In Progress")
    $inProgressLines.Add("")
    $inProgressLines.Add("## Active Tasks")
    $inProgressTasks = @($sortedTasks | Where-Object { (Get-V2TaskStatus -Task $_) -eq "in-progress" })
    if ($inProgressTasks.Count -eq 0) {
        $inProgressLines.Add("- none")
    }
    else {
        foreach ($task in $inProgressTasks) {
            $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "unknown")
            $assignedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
            $startedAt = [string](Get-V2OptionalProperty -InputObject $task -Name "started_at" -DefaultValue "")
            $inProgressLines.Add("- id: $taskId | agent: $assignedAgent | started_at: $startedAt")
        }
    }
    Write-V2File -Path $inProgressPath -Content ($inProgressLines -join [Environment]::NewLine) -Force

    $completedLines = New-Object System.Collections.Generic.List[string]
    $completedLines.Add("# Completed")
    $completedLines.Add("")
    $completedTasks = @($sortedTasks | Where-Object { (Get-V2TaskStatus -Task $_) -in @("done", "completed", "skipped") })
    if ($completedTasks.Count -eq 0) {
        $completedLines.Add("- none")
    }
    else {
        foreach ($task in $completedTasks) {
            $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "unknown")
            $status = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "done")
            $assignedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
            $completedAt = [string](Get-V2OptionalProperty -InputObject $task -Name "completed_at" -DefaultValue "")
            $completedLines.Add("- id: $taskId | status: $status | agent: $assignedAgent | completed_at: $completedAt")
        }
    }
    Write-V2File -Path $completedPath -Content ($completedLines -join [Environment]::NewLine) -Force
}

function Get-V2Sha256Text {
    param([string]$InputText)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($InputText)
        $hash = $sha.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Add-V2LockConflictWhiteboardNote {
    param(
        [string]$WhiteboardPath,
        [string]$TaskId,
        [string]$PreferredAgent,
        [string[]]$FilesAffected,
        [object[]]$ConflictingLocks
    )

    $fileSet = @(
        @($FilesAffected | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) |
        Sort-Object -Unique
    )
    $lockSet = @(
        @($ConflictingLocks | ForEach-Object {
                $lockAgent = [string](Get-V2OptionalProperty -InputObject $_ -Name "agent" -DefaultValue "unknown")
                $lockTask = [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "unknown")
                $lockPath = [string](Get-V2OptionalProperty -InputObject $_ -Name "file_path" -DefaultValue "unknown")
                "$lockAgent|$lockTask|$lockPath"
            }) |
        Sort-Object -Unique
    )

    $signatureSource = "$TaskId`n$($fileSet -join "`n")`n$($lockSet -join "`n")"
    $signature = Get-V2Sha256Text -InputText $signatureSource
    $signatureMarker = "- signature: $signature"
    if (Test-Path -LiteralPath $WhiteboardPath) {
        $existing = Get-Content -LiteralPath $WhiteboardPath -Raw
        if ($existing -match [regex]::Escape($signatureMarker)) {
            return
        }
    }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("## $(Get-V2Timestamp)")
    $lines.Add("- type: lock-conflict")
    $lines.Add("- task_id: $TaskId")
    $lines.Add("- preferred_agent: $PreferredAgent")
    $lines.Add("- signature: $signature")
    $lines.Add("- files_affected:")
    if ($fileSet.Count -eq 0) {
        $lines.Add("  - none")
    }
    else {
        foreach ($file in $fileSet) {
            $lines.Add("  - $file")
        }
    }
    $lines.Add("- blocking_locks:")
    if ($lockSet.Count -eq 0) {
        $lines.Add("  - none")
    }
    else {
        foreach ($entry in $lockSet) {
            $parts = $entry.Split("|")
            $entryAgent = if ($parts.Count -gt 0) { $parts[0] } else { "unknown" }
            $entryTask = if ($parts.Count -gt 1) { $parts[1] } else { "unknown" }
            $entryPath = if ($parts.Count -gt 2) { $parts[2] } else { "unknown" }
            $lines.Add("  - agent: $entryAgent | task: $entryTask | path: $entryPath")
        }
    }
    $lines.Add("- request: coordinate lock release or split file ownership.")

    Add-V2MarkdownLog -Path $WhiteboardPath -Header "# Whiteboard" -Lines @($lines.ToArray())
}

function Test-V2LockConflict {
    param(
        [string[]]$FilesAffected,
        [object[]]$ActiveLocks
    )

    $result = [PSCustomObject]@{
        has_conflict      = $false
        conflicting_locks = @()
    }

    if (@($FilesAffected).Count -eq 0) {
        return $result
    }

    if (@($ActiveLocks).Count -eq 0) {
        return $result
    }

    $conflicts = New-Object System.Collections.Generic.List[object]
    foreach ($taskFile in @($FilesAffected)) {
        $taskPath = Get-V2NormalizedPath -Path $taskFile
        if ([string]::IsNullOrWhiteSpace($taskPath)) { continue }

        foreach ($lock in $ActiveLocks) {
            $lockPathRaw = if ($lock.PSObject.Properties.Name -contains "file_path") { [string]$lock.file_path } else { "" }
            $lockPath = Get-V2NormalizedPath -Path $lockPathRaw
            if ([string]::IsNullOrWhiteSpace($lockPath)) { continue }

            $isOverlap = $false
            if ($taskPath -eq $lockPath) {
                $isOverlap = $true
            }
            elseif ($taskPath.StartsWith("$lockPath/")) {
                $isOverlap = $true
            }
            elseif ($lockPath.StartsWith("$taskPath/")) {
                $isOverlap = $true
            }

            if ($isOverlap) {
                $conflicts.Add($lock)
            }
        }
    }

    if ($conflicts.Count -gt 0) {
        $result = [PSCustomObject]@{
            has_conflict      = $true
            conflicting_locks = @($conflicts.ToArray())
        }
    }

    return $result
}

function Resolve-V2ProjectRelativePath {
    param(
        [string]$ProjectRoot,
        [string]$AnyPath
    )

    if ([string]::IsNullOrWhiteSpace($AnyPath)) {
        return ""
    }

    if ([System.IO.Path]::IsPathRooted($AnyPath)) {
        return [System.IO.Path]::GetFullPath($AnyPath)
    }

    $projectRelative = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $AnyPath))
    if (Test-Path -LiteralPath $projectRelative) {
        return $projectRelative
    }

    if (Test-Path -LiteralPath $AnyPath) {
        return [System.IO.Path]::GetFullPath($AnyPath)
    }

    return $projectRelative
}

function New-V2RepairLessonLearned {
    param(
        [string]$ProjectRoot,
        [string]$OrchestratorRoot,
        [object]$Task
    )

    $taskId = [string](Get-V2OptionalProperty -InputObject $Task -Name "id" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($taskId)) {
        return [PSCustomObject]@{ generated = $false; reason = "missing-task-id" }
    }

    $safeTaskId = ($taskId -replace "[^A-Za-z0-9_-]+", "_")
    $lessonsRoot = Join-Path $OrchestratorRoot "knowledge_base/lessons_learned"
    Initialize-V2Directory -Path $lessonsRoot
    $lessonPath = Join-Path $lessonsRoot "LESSON_${safeTaskId}.md"
    if (Test-Path -LiteralPath $lessonPath) {
        return [PSCustomObject]@{
            generated       = $false
            reason          = "already-exists"
            lesson_path     = $lessonPath
            lesson_relative = (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $lessonPath)
        }
    }

    $incidentRaw = [string](Get-V2OptionalProperty -InputObject $Task -Name "source_incident" -DefaultValue "")
    $incidentPath = Resolve-V2ProjectRelativePath -ProjectRoot $ProjectRoot -AnyPath $incidentRaw
    $incidentTitle = "unknown"
    $incidentDetails = "unknown"
    $incidentCommand = "unknown"
    $incidentCategory = "unknown"

    if (-not [string]::IsNullOrWhiteSpace($incidentPath) -and (Test-Path -LiteralPath $incidentPath -PathType Leaf)) {
        $incidentContent = Get-Content -LiteralPath $incidentPath -Raw

        $titleMatch = [regex]::Match($incidentContent, '(?ms)^##\s*Title\s*\r?\n(.+?)(?:\r?\n##|\Z)')
        if ($titleMatch.Success) {
            $incidentTitle = $titleMatch.Groups[1].Value.Trim()
        }

        $detailsMatch = [regex]::Match($incidentContent, '(?ms)^##\s*Details\s*\r?\n(.+?)(?:\r?\n##|\Z)')
        if ($detailsMatch.Success) {
            $incidentDetails = $detailsMatch.Groups[1].Value.Trim()
        }

        $commandMatch = [regex]::Match($incidentContent, '(?ms)^##\s*Command\s*\r?\n```text\s*\r?\n(.+?)\r?\n```')
        if ($commandMatch.Success) {
            $incidentCommand = $commandMatch.Groups[1].Value.Trim()
        }

        $categoryMatch = [regex]::Match($incidentContent, '(?mi)^- Category:\s*(.+)$')
        if ($categoryMatch.Success) {
            $incidentCategory = $categoryMatch.Groups[1].Value.Trim()
        }
    }

    $completedAt = [string](Get-V2OptionalProperty -InputObject $Task -Name "completed_at" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($completedAt)) {
        $completedAt = Get-V2Timestamp
    }
    $assignedAgent = [string](Get-V2OptionalProperty -InputObject $Task -Name "assigned_agent" -DefaultValue "unknown")

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Lesson Learned: $taskId")
    $lines.Add("")
    $lines.Add("- Generated At: $(Get-V2Timestamp)")
    $lines.Add("- Task ID: $taskId")
    $lines.Add("- Agent: $assignedAgent")
    $lines.Add("- Completed At: $completedAt")
    $lines.Add("- Incident Path: $incidentRaw")
    $lines.Add("")
    $lines.Add("## Error Signature")
    $lines.Add("- Category: $incidentCategory")
    $lines.Add("- Title: $incidentTitle")
    $lines.Add("- Details: $incidentDetails")
    $lines.Add("")
    $lines.Add("## Fix Pattern")
    $lines.Add("- Task status moved to completed.")
    $lines.Add("- Capture the exact patch/test evidence in this section when available.")
    $lines.Add("")
    $lines.Add("## Validation Command")
    $lines.Add('```text')
    $lines.Add($incidentCommand)
    $lines.Add('```')
    $lines.Add("")
    $lines.Add("## Reuse Guidance")
    $lines.Add("- Search this lesson first when similar failures appear.")
    $lines.Add("- Re-run the failing command before applying any broad refactor.")
    $lines.Add("- Keep the repair minimal and attach evidence in execution history.")

    [System.IO.File]::WriteAllText($lessonPath, ($lines -join [Environment]::NewLine))
    return [PSCustomObject]@{
        generated       = $true
        reason          = "created"
        lesson_path     = $lessonPath
        lesson_relative = (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $lessonPath)
    }
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

# Auto-cleanup of orphan repairs/transients before scheduling (Phase 4: Self-Healing)
if ($UseTaskStateDb -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot "Invoke-RepairOrphanCleanupV2.ps1") -PathType Leaf)) {
    try {
        & (Join-Path $PSScriptRoot "Invoke-RepairOrphanCleanupV2.ps1") -ProjectPath $resolvedProjectPath -UseTaskStateDb $true > $null
    }
    catch { }
}

Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-scheduler"

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$taskDagJsonPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$locksPath = Join-Path $orchestratorRoot "locks/locks.json"
$workloadPath = Join-Path $orchestratorRoot "agents/workload.json"
$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$historyPath = Join-Path $orchestratorRoot "tasks/execution-history.md"
$messagePath = Join-Path $orchestratorRoot "communication/messages.md"
$whiteboardPath = Join-Path $orchestratorRoot "communication/whiteboard.md"
$decisionsPath = Join-Path $orchestratorRoot "communication/decisions.md"
$reputationPath = Join-Path $orchestratorRoot "agents/reputation.json"
$metaCalibrationPath = Join-Path $orchestratorRoot "state/meta-calibration.json"
$taskSyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Sync-TaskState.ps1"
$lockSyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Sync-LockState.ps1"
$taskStateDbScriptPath = Join-Path $PSScriptRoot "task_state_db.py"
$taskStateDbPath = Join-Path $orchestratorRoot "state/task-state-v3.db"
$schedulerDbStatePath = Join-Path $orchestratorRoot "state/scheduler-db-state.json"

$taskDocument = $null
$tasks = @()
$taskStateDbActive = $false
$taskStateDbBootstrapSync = $false
$taskStateDbBootstrapReason = ""
$taskStateDbStatusResult = $null
$taskStateDbBackendMode = ""
$taskStateDbDriftSync = $false
if ($UseTaskStateDb -and (Test-Path -LiteralPath $taskStateDbScriptPath -PathType Leaf)) {
    try {
        $taskStateDbStatusResult = Invoke-V2TaskStateDbCommand `
            -TaskStateDbScriptPath $taskStateDbScriptPath `
            -ProjectRoot $resolvedProjectPath `
            -Mode "status"

        $statusOk = [bool](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "ok" -DefaultValue $false)
        if ($statusOk) {
            $taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "backend_mode" -DefaultValue "")
            $taskStateDbPath = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "db_path" -DefaultValue $taskStateDbPath)
            $taskStateDbActive = $true
            if (Test-V2TaskDagManualDrift -DagPath $taskDagJsonPath -TaskStateStatus $taskStateDbStatusResult) {
                [void](Invoke-V2TaskStateDbCommand -TaskStateDbScriptPath $taskStateDbScriptPath -ProjectRoot $resolvedProjectPath -Mode "sync")
                $taskStateDbStatusResult = Invoke-V2TaskStateDbCommand `
                    -TaskStateDbScriptPath $taskStateDbScriptPath `
                    -ProjectRoot $resolvedProjectPath `
                    -Mode "status"
                $taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "backend_mode" -DefaultValue "")
                $taskStateDbPath = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "db_path" -DefaultValue $taskStateDbPath)
                $taskStateDbActive = [bool](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "ok" -DefaultValue $false)
                if ($taskStateDbActive) {
                    $taskStateDbDriftSync = $true
                    $taskStateDbBootstrapSync = $true
                    $taskStateDbBootstrapReason = "manual-dag-drift-sync"
                }
            }
        }
        else {
            $taskStateDbBootstrapSync = $true
            $taskStateDbBootstrapReason = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "error" -DefaultValue "status-probe-failed")
        }

        if ($taskStateDbBootstrapSync -and (Test-Path -LiteralPath $taskDagJsonPath -PathType Leaf)) {
            [void](Invoke-V2TaskStateDbCommand -TaskStateDbScriptPath $taskStateDbScriptPath -ProjectRoot $resolvedProjectPath -Mode "sync")
            $taskStateDbStatusResult = Invoke-V2TaskStateDbCommand `
                -TaskStateDbScriptPath $taskStateDbScriptPath `
                -ProjectRoot $resolvedProjectPath `
                -Mode "status"
            $taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "backend_mode" -DefaultValue "")
            $taskStateDbPath = [string](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "db_path" -DefaultValue $taskStateDbPath)
            $taskStateDbActive = [bool](Get-V2OptionalProperty -InputObject $taskStateDbStatusResult -Name "ok" -DefaultValue $false)
            if ($taskStateDbActive) {
                $taskStateDbBootstrapReason = "initial-sync-from-task-dag"
            }
        }

        if ($taskStateDbActive) {
            $allTasksResult = Invoke-V2TaskStateDbCommand `
                -TaskStateDbScriptPath $taskStateDbScriptPath `
                -ProjectRoot $resolvedProjectPath `
                -Mode "query" `
                -ExtraArgs @{ query = "all-tasks"; limit = "10000" }
            $rows = @((Get-V2OptionalProperty -InputObject $allTasksResult -Name "rows" -DefaultValue @()))
            $tasks = @($rows)
            $taskDocument = [PSCustomObject]@{
                tasks      = @($tasks)
                updated_at = Get-V2Timestamp
            }
        }
    }
    catch {
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: scheduler-task-state-db-fallback-json",
            "- reason: $($_.Exception.Message)"
        )
        $taskStateDbActive = $false
    }
}

if (-not $taskStateDbActive) {
    $taskDocument = Get-V2JsonContent -Path $taskDagJsonPath
    if (-not $taskDocument -or -not ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
        $result = [PSCustomObject]@{
            project_path = $resolvedProjectPath
            scheduled    = 0
            status       = "no-task-dag-json"
        }
        if ($EmitJson) {
            $result | ConvertTo-Json -Depth 8
            return
        }
        Write-Output "Scheduler skipped: task-dag.json not found."
        return
    }
    $tasks = @($taskDocument.tasks)
}

$tasksSnapshotBefore = @($tasks | ConvertTo-Json -Depth 100 -Compress)
$workload = Ensure-V2Workload -WorkloadPath $workloadPath
$agentIndex = Get-V2AgentIndex -Workload $workload
$reputationIndex = @{}
$reputationDoc = Get-V2JsonContent -Path $reputationPath
if ($reputationDoc -and ($reputationDoc.PSObject.Properties.Name -contains "agents")) {
    foreach ($entry in @($reputationDoc.agents)) {
        $agentName = [string](Get-V2OptionalProperty -InputObject $entry -Name "agent" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($agentName)) { continue }
        $reputationIndex[$agentName] = $entry
    }
}
$calibrationIndex = @{}
$metaCalibrationDoc = Get-V2JsonContent -Path $metaCalibrationPath
if ($metaCalibrationDoc -and ($metaCalibrationDoc.PSObject.Properties.Name -contains "agents")) {
    $calibPath = [string](Get-V2OptionalProperty -InputObject $metaCalibrationDoc -Name "project_path" -DefaultValue "")
    if ($calibPath -like "/*" -and $IsWindows) {
        # Self-heal: re-resolve Unix-style path on Windows
        $calibPath = Resolve-V2AbsolutePath -Path $calibPath
        Set-V2DynamicProperty -InputObject $metaCalibrationDoc -Name "project_path" -Value $calibPath
    }
    foreach ($entry in @($metaCalibrationDoc.agents)) {
        $agentName = [string](Get-V2OptionalProperty -InputObject $entry -Name "agent" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($agentName)) { continue }
        $calibrationIndex[$agentName] = $entry
    }
}
$projectState = Get-V2JsonContent -Path $statePath
if ($projectState) {
    $phaseApprovalsChanged = Initialize-V2PhaseApprovals -ProjectState $projectState -UpdatedBy "scheduler-auto"
    if ($phaseApprovalsChanged) {
        Set-V2DynamicProperty -InputObject $projectState -Name "updated_at" -Value (Get-V2Timestamp)
        Save-V2JsonContent -Path $statePath -Value $projectState
    }
}
$projectStatus = [string](Get-V2OptionalProperty -InputObject $projectState -Name "status" -DefaultValue "unknown")
$strategicMode = Get-V2StrategicMode -ProjectRoot $resolvedProjectPath
$blockedByProjectState = ($projectStatus -like "blocked-*")
$blockedByUncertainty = ($projectStatus -eq "blocked-waiting-answers")
$nowUtc = (Get-Date).ToUniversalTime()
$staleTakeovers = New-Object System.Collections.Generic.List[object]
$gpuRoutingEnabled = Get-V2EnvBool -Name "ORCHESTRATOR_GPU_ROUTING_ENABLED" -DefaultValue $true
$gpuRoutingNativeComplexityThreshold = [Math]::Max((Get-V2EnvInt -Name "ORCHESTRATOR_GPU_ROUTING_COMPLEXITY_THRESHOLD" -DefaultValue 15), 1)
$gpuRoutingNativeMaxFiles = [Math]::Max((Get-V2EnvInt -Name "ORCHESTRATOR_GPU_ROUTING_MAX_FILES" -DefaultValue 30), 1)
$gpuRoutingNativeMaxDependencies = [Math]::Max((Get-V2EnvInt -Name "ORCHESTRATOR_GPU_ROUTING_MAX_DEPENDENCIES" -DefaultValue 10), 0)
$executionProfileStats = [ordered]@{
    native_gpu     = 0
    external_agent = 0
    changed        = 0
    unchanged      = 0
}
$RepairOrphanTakeoverMinutes = [Math]::Max($RepairOrphanTakeoverMinutes, 0)
$lockConflictRetryMinutes = [Math]::Max($LockConflictRetryMinutes, 0)
$locksDoc = Get-V2JsonContent -Path $locksPath
$activeLocks = if ($locksDoc) { @(Get-V2ActiveLocks -LocksPath $locksPath) } else { @() } # Optimization: Get-V2ActiveLocks still uses LocksPath but we will pre-cache later if needed

$runnableTaskStatuses = @("pending", "blocked-lock-conflict", "blocked-no-agent", "blocked-waiting-answers", "blocked-phase-approval")

foreach ($task in @($tasks)) {
    $payloadCompacted = Compress-V2TaskPayload -Task $task
    $taskStatus = Get-V2TaskStatus -Task $task
    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
    $executionMode = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_mode" -DefaultValue "")
    $needsExternalExecution = ($taskId -like "REPAIR-DEPLOY-*") -or ($taskId -like "REPAIR-TEST-FAIL-*") -or ($taskId -like "COBERTURA-FALHA-*")
    if ($needsExternalExecution -and $executionMode -eq "artifact-validation") {
        Set-V2ObjectProperty -InputObject $task -Name "execution_mode" -Value "external-agent"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        $executionMode = "external-agent"
    }
    elseif ($payloadCompacted) {
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
    }

    if ($taskStatus -notin @("done", "completed", "skipped", "cancelled")) {
        $executionProfile = Resolve-V2TaskExecutionProfile `
            -Task $task `
            -RoutingEnabled $gpuRoutingEnabled `
            -NativeComplexityThreshold $gpuRoutingNativeComplexityThreshold `
            -NativeMaxFiles $gpuRoutingNativeMaxFiles `
            -NativeMaxDependencies $gpuRoutingNativeMaxDependencies

        $targetExecutionMode = [string](Get-V2OptionalProperty -InputObject $executionProfile -Name "execution_mode" -DefaultValue "external-agent")
        $targetRuntimeEngine = [string](Get-V2OptionalProperty -InputObject $executionProfile -Name "runtime_engine" -DefaultValue "hybrid")
        $profileReason = [string](Get-V2OptionalProperty -InputObject $executionProfile -Name "reason" -DefaultValue "")
        $complexityScore = [int](Get-V2OptionalProperty -InputObject $executionProfile -Name "complexity_score" -DefaultValue 0)
        $profileName = [string](Get-V2OptionalProperty -InputObject $executionProfile -Name "profile" -DefaultValue "external-agent")

        $modeChanged = ($executionMode -ne $targetExecutionMode)
        $engineCurrent = [string](Get-V2OptionalProperty -InputObject $task -Name "runtime_engine" -DefaultValue "")
        $engineChanged = ($engineCurrent -ne $targetRuntimeEngine)
        if ($modeChanged -or $engineChanged) {
            $executionProfileStats.changed = [int]$executionProfileStats.changed + 1
            Set-V2ObjectProperty -InputObject $task -Name "execution_mode" -Value $targetExecutionMode
            Set-V2ObjectProperty -InputObject $task -Name "runtime_engine" -Value $targetRuntimeEngine
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            $executionMode = $targetExecutionMode
        }
        else {
            $executionProfileStats.unchanged = [int]$executionProfileStats.unchanged + 1
        }

        Set-V2ObjectProperty -InputObject $task -Name "execution_profile_reason" -Value $profileReason
        Set-V2ObjectProperty -InputObject $task -Name "execution_profile_complexity" -Value $complexityScore
        if ($profileName -eq "native-gpu") {
            $executionProfileStats.native_gpu = [int]$executionProfileStats.native_gpu + 1
        }
        else {
            $executionProfileStats.external_agent = [int]$executionProfileStats.external_agent + 1
        }
    }

    $taskAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
    $taskFiles = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")
    $taskIdleMinutes = Get-V2TaskIdleMinutes -Task $task -NowUtc $nowUtc
    if ($taskStatus -notin @("in-progress", "active")) {
        if (-not [string]::IsNullOrWhiteSpace($taskId) -and -not [string]::IsNullOrWhiteSpace($taskAgent)) {
            [void](Remove-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles -Reason ("status-" + $taskStatus))
        }
    }
    elseif (-not (Test-V2TaskLockCoverage -LocksDoc $locksDoc -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles)) {
        $isRepairTask = $taskId.StartsWith("REPAIR-", [System.StringComparison]::OrdinalIgnoreCase)
        $takeoverReason = $(if ($isRepairTask) { "repair-orphan-missing-active-lock" } else { "missing-active-lock" })
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value $takeoverReason
        Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
        if (-not [string]::IsNullOrWhiteSpace($taskAgent)) {
            Set-V2ObjectProperty -InputObject $task -Name "takeover_excluded_agent" -Value $taskAgent
            Set-V2ObjectProperty -InputObject $task -Name "last_takeover_from_agent" -Value $taskAgent
            Set-V2ObjectProperty -InputObject $task -Name "last_takeover_reason" -Value $takeoverReason
            $staleTakeovers.Add([PSCustomObject]@{
                    task_id      = $taskId
                    from_agent   = $taskAgent
                    reason       = $takeoverReason
                    idle_minutes = $taskIdleMinutes
                })
        }
        Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }
    $isRepairTask = $taskId.StartsWith("REPAIR-", [System.StringComparison]::OrdinalIgnoreCase)
    $effectiveInProgressTakeoverMinutes = [double]$IdleInProgressTakeoverMinutes
    if (
        $isRepairTask -and
        $RepairOrphanTakeoverMinutes -gt 0 -and
        (
            $effectiveInProgressTakeoverMinutes -le 0 -or
            [double]$RepairOrphanTakeoverMinutes -lt $effectiveInProgressTakeoverMinutes
        )
    ) {
        $effectiveInProgressTakeoverMinutes = [double]$RepairOrphanTakeoverMinutes
    }

    if (
        $effectiveInProgressTakeoverMinutes -gt 0 -and
        -not [string]::IsNullOrWhiteSpace($taskAgent) -and
        $taskIdleMinutes -ge $effectiveInProgressTakeoverMinutes
    ) {
        $takeoverReason = $(if ($isRepairTask) { "repair-orphan-idle-timeout" } else { "in-progress-idle-timeout" })
        [void](Remove-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles -Reason "stale-in-progress-takeover")
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
        Set-V2ObjectProperty -InputObject $task -Name "takeover_excluded_agent" -Value $taskAgent
        Set-V2ObjectProperty -InputObject $task -Name "last_takeover_from_agent" -Value $taskAgent
        Set-V2ObjectProperty -InputObject $task -Name "last_takeover_reason" -Value $takeoverReason
        Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        $staleTakeovers.Add([PSCustomObject]@{
                task_id      = $taskId
                from_agent   = $taskAgent
                reason       = $takeoverReason
                idle_minutes = $taskIdleMinutes
            })
        $taskStatus = "pending"
        $taskAgent = ""
    }

    if (
        $IdlePendingTakeoverMinutes -gt 0 -and
        $taskStatus -in @("pending", "blocked-no-agent", "blocked-lock-conflict") -and
        -not [string]::IsNullOrWhiteSpace($taskAgent) -and
        $taskIdleMinutes -ge [double]$IdlePendingTakeoverMinutes
    ) {
        [void](Remove-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $taskAgent -FilesAffected $taskFiles -Reason "stale-pending-takeover")
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
        Set-V2ObjectProperty -InputObject $task -Name "takeover_excluded_agent" -Value $taskAgent
        Set-V2ObjectProperty -InputObject $task -Name "last_takeover_from_agent" -Value $taskAgent
        Set-V2ObjectProperty -InputObject $task -Name "last_takeover_reason" -Value "pending-idle-timeout"
        Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        $staleTakeovers.Add([PSCustomObject]@{
                task_id      = $taskId
                from_agent   = $taskAgent
                reason       = "pending-idle-timeout"
                idle_minutes = $taskIdleMinutes
            })
        $taskStatus = "pending"
        $taskAgent = ""
    }

    if (
        $lockConflictRetryMinutes -gt 0 -and
        $taskStatus -eq "blocked-lock-conflict" -and
        [string](Get-V2OptionalProperty -InputObject $task -Name "blocked_reason" -DefaultValue "") -eq "active-lock-overlap"
    ) {
        $conflictSinceRaw = [string](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_since_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($conflictSinceRaw)) {
            $conflictSinceRaw = [string](Get-V2OptionalProperty -InputObject $task -Name "updated_at" -DefaultValue "")
            if ([string]::IsNullOrWhiteSpace($conflictSinceRaw)) {
                $conflictSinceRaw = Get-V2Timestamp
            }
            Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_since_at" -Value $conflictSinceRaw
        }

        $conflictAgeMinutes = -1.0
        try {
            $conflictAgeMinutes = [Math]::Round(((Get-Date).ToUniversalTime() - ([DateTimeOffset]::Parse($conflictSinceRaw)).UtcDateTime).TotalMinutes, 2)
        }
        catch {
            $conflictAgeMinutes = -1.0
        }

        if ($conflictAgeMinutes -ge [double]$lockConflictRetryMinutes) {
            $previousAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
            Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "last_takeover_reason" -Value "lock-conflict-timeout-retry"
            Set-V2ObjectProperty -InputObject $task -Name "last_lock_conflict_retry_at" -Value (Get-V2Timestamp)
            Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_retry_count" -Value ([int](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_retry_count" -DefaultValue 0) + 1)
            if (-not [string]::IsNullOrWhiteSpace($previousAgent)) {
                Set-V2ObjectProperty -InputObject $task -Name "takeover_excluded_agent" -Value $previousAgent
            }
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            $taskStatus = "pending"
            $taskAgent = ""
        }
    }

    if (-not $blockedByProjectState -and $taskStatus -eq "blocked-waiting-answers") {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
    }

    if (-not $blockedByProjectState -and $taskStatus -eq "blocked-startup") {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
    }

    if ($taskStatus -eq "blocked-runtime") {
        $blockedReasonRaw = [string](Get-V2OptionalProperty -InputObject $task -Name "blocked_reason" -DefaultValue "")
        $blockedReasonNormalized = $blockedReasonRaw.ToLowerInvariant()
        $fallbackApplied = [bool](Get-V2OptionalProperty -InputObject $task -Name "runtime_missing_handler_fallback_applied" -DefaultValue $false)
        $currentExecutionMode = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_mode" -DefaultValue "")
        if (
            -not $fallbackApplied -and
            -not [string]::IsNullOrWhiteSpace($blockedReasonRaw) -and
            $blockedReasonNormalized.Contains("missing-task-command-or-handler")
        ) {
            $targetExecutionMode = $(if ($currentExecutionMode -in @("external-agent", "manual", "human")) { "llm-native" } else { "external-agent" })
            $targetRuntimeEngine = $(if ($targetExecutionMode -eq "llm-native") { "hybrid" } else { "hybrid" })
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
            Set-V2ObjectProperty -InputObject $task -Name "execution_mode" -Value $targetExecutionMode
            Set-V2ObjectProperty -InputObject $task -Name "runtime_engine" -Value $targetRuntimeEngine
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "runtime_missing_handler_fallback_applied" -Value $true
            Set-V2ObjectProperty -InputObject $task -Name "runtime_missing_handler_fallback_at" -Value (Get-V2Timestamp)
            Set-V2ObjectProperty -InputObject $task -Name "runtime_missing_handler_fallback_reason" -Value "missing-task-command-or-handler"
            Set-V2ObjectProperty -InputObject $task -Name "runtime_missing_handler_fallback_target" -Value $targetExecutionMode
            Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            continue
        }

        $isKnownTransientRuntimeError = (
            -not [string]::IsNullOrWhiteSpace($blockedReasonRaw) -and
            (
                $blockedReasonNormalized.Contains("property 'count' cannot be found") -or
                $blockedReasonNormalized.Contains("object reference not set") -or
                $blockedReasonNormalized.Contains("timeout") -or
                $blockedReasonNormalized.Contains("external-bridge")
            )
        )
        if ($isKnownTransientRuntimeError) {
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $true
            Set-V2ObjectProperty -InputObject $task -Name "runtime_transient_requeue_count" -Value ([int](Get-V2OptionalProperty -InputObject $task -Name "runtime_transient_requeue_count" -DefaultValue 0) + 1)
            Set-V2ObjectProperty -InputObject $task -Name "runtime_transient_requeue_at" -Value (Get-V2Timestamp)
            Set-V2ObjectProperty -InputObject $task -Name "runtime_transient_requeue_reason" -Value $blockedReasonRaw
            Set-V2ObjectProperty -InputObject $task -Name "last_takeover_reason" -Value "blocked-runtime-transient-requeue"
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            continue
        }

        if ($executionMode -eq "project-completion-gate") {
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        }
    }
}

Reset-V2WorkloadFromTasks -Workload $workload -Tasks $tasks
$agentIndex = Get-V2AgentIndex -Workload $workload

$taskIndex = @{}
foreach ($task in @($tasks)) {
    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($taskId)) {
        $taskIndex[$taskId] = $task
    }
}

$coreGateDone = $false
if ($taskIndex.ContainsKey("CORE-COMPLETE-001")) {
    $coreStatus = Get-V2TaskStatus -Task $taskIndex["CORE-COMPLETE-001"]
    $coreGateDone = $coreStatus -in @("done", "completed")
}

$runnableTasks = @($tasks | Where-Object { (Get-V2TaskStatus -Task $_) -in $runnableTaskStatuses })
if ($runnableTasks.Count -gt 1) {
    $runnableTasks = @($runnableTasks | Sort-Object `
        @{ Expression = { (Get-V2PriorityWeight -Priority ([string](Get-V2OptionalProperty -InputObject $_ -Name "priority" -DefaultValue "P3"))) + (Get-V2ModePriorityBonus -StrategicMode $strategicMode -Task $_) }; Descending = $true },
        @{ Expression = { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") }; Descending = $false }
    )
}

$assignments = New-Object System.Collections.Generic.List[object]
$assignmentCount = 0
foreach ($task in $runnableTasks) {
    if ($assignmentCount -ge $MaxAssignmentsPerRun) { break }

    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($taskId)) { continue }

    if ($taskId.StartsWith("FEATURE-", [System.StringComparison]::OrdinalIgnoreCase) -and -not $coreGateDone) {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-dependency"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "core-complete-gate-not-done"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    if (-not (Test-V2TaskAllowedByMode -StrategicMode $strategicMode -Task $task) -and -not (Test-V2RecoveryTask -Task $task)) {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-strategic-mode"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ("strategic-mode-" + $strategicMode.ToLowerInvariant())
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    if ($blockedByProjectState -and -not (Test-V2RecoveryTask -Task $task)) {
        $blockedStatus = $(if ($projectStatus -eq "blocked-waiting-answers") { "blocked-waiting-answers" } else { "blocked-startup" })
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value $blockedStatus
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ("project-state-" + $projectStatus)
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    if ($blockedByUncertainty -and $taskId -ne "V2-LEGACY-GATE-001") {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-waiting-answers"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "project-has-open-questions"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    if (-not (Test-V2DependenciesResolved -Task $task -TaskIndex $taskIndex)) {
        continue
    }

    $requiredPhase = [string](Get-V2OptionalProperty -InputObject $task -Name "required_phase_approval" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($requiredPhase)) {
        $phaseStatus = Get-V2PhaseApprovalStatus -ProjectState $projectState -PhaseName $requiredPhase
        if ($phaseStatus -ne "approved") {
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-phase-approval"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ("phase-approval-{0}-{1}" -f $requiredPhase, $phaseStatus)
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            continue
        }
    }

    $preferredAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "preferred_agent" -DefaultValue "")
    $allowAgentTakeover = [bool](Get-V2OptionalProperty -InputObject $task -Name "allow_agent_takeover" -DefaultValue $false)
    $takeoverExcludedAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "takeover_excluded_agent" -DefaultValue "")
    $filesAffected = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")
    $lockConflict = Test-V2LockConflict -FilesAffected $filesAffected -ActiveLocks $activeLocks
    if ($lockConflict.has_conflict) {
        $conflictNow = Get-V2Timestamp
        $currentConflictSince = [string](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_since_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($currentConflictSince)) {
            Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_since_at" -Value $conflictNow
        }
        Set-V2ObjectProperty -InputObject $task -Name "last_lock_conflict_at" -Value $conflictNow
        Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_count" -Value ([int](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_count" -DefaultValue 0) + 1)
        $conflictingTaskIds = @(
            @($lockConflict.conflicting_locks | ForEach-Object {
                    [string](Get-V2OptionalProperty -InputObject $_ -Name "task_id" -DefaultValue "")
                }) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique
        )
        Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_task_ids" -Value $conflictingTaskIds
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-lock-conflict"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "active-lock-overlap"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $conflictNow
        Add-V2LockConflictWhiteboardNote `
            -WhiteboardPath $whiteboardPath `
            -TaskId $taskId `
            -PreferredAgent $preferredAgent `
            -FilesAffected $filesAffected `
            -ConflictingLocks @($lockConflict.conflicting_locks)
        continue
    }

    $candidatePreferredAgent = $(if ($allowAgentTakeover) { "" } else { $preferredAgent })
    $candidates = @(Get-V2CandidateAgents -PreferredAgent $candidatePreferredAgent -AgentIndex $agentIndex)
    if ($allowAgentTakeover -and -not [string]::IsNullOrWhiteSpace($takeoverExcludedAgent)) {
        $candidates = @(
            @($candidates) |
            Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -ne $takeoverExcludedAgent
            }
        )
    }
    $selectedCandidate = Select-V2BestAgent -Candidates $candidates -Task $task -ReputationIndex $reputationIndex -CalibrationIndex $calibrationIndex
    if (-not $selectedCandidate) {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-no-agent"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "no-available-agent"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    $selectedAgent = Get-V2OptionalProperty -InputObject $selectedCandidate -Name "agent" -DefaultValue $null
    if (-not $selectedAgent) {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-no-agent"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "invalid-agent-selection"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    $selectedAgentName = [string](Get-V2OptionalProperty -InputObject $selectedAgent -Name "name" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($selectedAgentName)) {
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-no-agent"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "invalid-agent-selection"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        continue
    }

    $lockResult = New-V2TaskLocks `
        -LocksPath $locksPath `
        -TaskId $taskId `
        -Agent $selectedAgentName `
        -FilesAffected $filesAffected `
        -TtlSeconds $LockTtlSeconds

    if (-not $lockResult.success) {
        $lockFailNow = Get-V2Timestamp
        $currentConflictSince = [string](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_since_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($currentConflictSince)) {
            Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_since_at" -Value $lockFailNow
        }
        Set-V2ObjectProperty -InputObject $task -Name "last_lock_conflict_at" -Value $lockFailNow
        Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_count" -Value ([int](Get-V2OptionalProperty -InputObject $task -Name "lock_conflict_count" -DefaultValue 0) + 1)
        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-lock-conflict"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "lock-acquire-failed"
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $lockFailNow
        continue
    }

    $now = Get-V2Timestamp
    Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value $selectedAgentName
    Set-V2ObjectProperty -InputObject $task -Name "status" -Value "in-progress"
    Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
    Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_since_at" -Value ""
    Set-V2ObjectProperty -InputObject $task -Name "lock_conflict_task_ids" -Value @()
    Set-V2ObjectProperty -InputObject $task -Name "allow_agent_takeover" -Value $false
    Set-V2ObjectProperty -InputObject $task -Name "takeover_excluded_agent" -Value ""
    Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $now
    Set-V2ObjectProperty -InputObject $task -Name "assignment_score" -Value ([double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "adjusted_score" -DefaultValue 0.0))
    Set-V2ObjectProperty -InputObject $task -Name "reputation_fit_score" -Value ([double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "reputation_fit_score" -DefaultValue 0.0))
    Set-V2ObjectProperty -InputObject $task -Name "timeout_penalty" -Value ([double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "timeout_penalty" -DefaultValue 0.0))
    if ([string]::IsNullOrWhiteSpace([string](Get-V2OptionalProperty -InputObject $task -Name "started_at" -DefaultValue ""))) {
        Set-V2ObjectProperty -InputObject $task -Name "started_at" -Value $now
    }

    $selectedAgent.active_tasks = [int](Get-V2OptionalProperty -InputObject $selectedAgent -Name "active_tasks" -DefaultValue 0) + 1
    $selectedAgent.last_assigned = $now

    $assignments.Add([PSCustomObject]@{
            task_id                  = $taskId
            agent                    = $selectedAgentName
            priority                 = [string](Get-V2OptionalProperty -InputObject $task -Name "priority" -DefaultValue "P3")
            execution_mode           = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_mode" -DefaultValue "")
            runtime_engine           = [string](Get-V2OptionalProperty -InputObject $task -Name "runtime_engine" -DefaultValue "")
            execution_profile_reason = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_profile_reason" -DefaultValue "")
            adjusted_score           = [double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "adjusted_score" -DefaultValue 0.0)
            reputation_fit_score     = [double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "reputation_fit_score" -DefaultValue 0.0)
            timeout_penalty          = [double](Get-V2OptionalProperty -InputObject $selectedCandidate -Name "timeout_penalty" -DefaultValue 0.0)
            takeover                 = [bool]$allowAgentTakeover
        })
    $assignmentCount += 1

    # ── Generate context bundle so the agent has everything it needs in one file ─────────────
    try {
        $bundleDir = Join-Path $orchestratorRoot "tasks/context-bundles"
        if (-not (Test-Path -LiteralPath $bundleDir -PathType Container)) {
            New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null
        }
        $bundlePath = Join-Path $bundleDir "$taskId.md"
        $taskTitle = [string](Get-V2OptionalProperty -InputObject $task -Name "title"       -DefaultValue $taskId)
        $taskDesc = [string](Get-V2OptionalProperty -InputObject $task -Name "description" -DefaultValue "")
        $taskGoal = [string](Get-V2OptionalProperty -InputObject $task -Name "goal"        -DefaultValue "")
        $taskPriority = [string](Get-V2OptionalProperty -InputObject $task -Name "priority"    -DefaultValue "P3")
        $taskDeps = @(Get-V2OptionalProperty -InputObject $task -Name "depends_on" -DefaultValue @())
        $taskFiles = @(Get-V2OptionalProperty -InputObject $task -Name "affected_files" -DefaultValue @())
        $taskReason = [string](Get-V2OptionalProperty -InputObject $task -Name "reason"      -DefaultValue "")

        # Resolved dependency titles
        $depLines = New-Object System.Collections.Generic.List[string]
        foreach ($depId in @($taskDeps)) {
            $depTask = @($tasks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $depId }) | Select-Object -First 1
            $depStatus = $(if ($depTask) { [string](Get-V2OptionalProperty -InputObject $depTask -Name "status" -DefaultValue "?") } else { "?" })
            $depTitle = $(if ($depTask) { [string](Get-V2OptionalProperty -InputObject $depTask -Name "title"  -DefaultValue $depId) } else { $depId })
            $depLines.Add("- [$depStatus] **$depId** - $depTitle")
        }

        # Relevant patterns from prior REPAIR resolutions
        $patternDir = Join-Path $orchestratorRoot "patterns"
        $patternLines = New-Object System.Collections.Generic.List[string]
        if (Test-Path -LiteralPath $patternDir -PathType Container) {
            $patternFiles = @(Get-ChildItem -LiteralPath $patternDir -Filter "*.md" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 5)
            foreach ($pf in $patternFiles) {
                $patternLines.Add("- [$($pf.BaseName)]($($pf.FullName))")
            }
        }

        # Semantic search
        $semanticLines = New-Object System.Collections.Generic.List[string]
        try {
            $queryLessonsScript = Join-Path (Split-Path -Parent $PSScriptRoot) "scripts/query_lessons.py"
            if (-not (Test-Path -LiteralPath $queryLessonsScript -PathType Leaf)) {
                $queryLessonsScript = Join-Path (Split-Path -Parent $PSScriptRoot) "query_lessons.py"
            }
            $searchQuery = "$taskTitle $taskDesc $taskReason".Trim()
            if ($searchQuery.Length -gt 10 -and (Test-Path -LiteralPath $queryLessonsScript -PathType Leaf)) {
                $queryOutput = & python $queryLessonsScript --project-path $resolvedProjectPath --query $searchQuery --top-k 3 2>$null
                if ($queryOutput) {
                    $queryResult = $queryOutput | ConvertFrom-Json -ErrorAction SilentlyContinue
                    if ($queryResult -and $queryResult.results -and $queryResult.results.Count -gt 0) {
                        foreach ($hit in $queryResult.results) {
                            $scoreStr = $(if ($hit.score) { " (score: $($hit.score))" } else { "" })
                            $semanticLines.Add("- **$($hit.title)**$scoreStr - $($hit.path)")
                            if ($hit.snippet) {
                                $snip = [string]$hit.snippet
                                if ($snip.Length -gt 120) { $snip = $snip.Substring(0, 120) + "..." }
                                $semanticLines.Add("  > $snip")
                            }
                        }
                    }
                }
            }
        } catch { }

        $bundleLines = New-Object System.Collections.Generic.List[string]
        $bundleLines.Add("# Context Bundle: $taskId")
        $bundleLines.Add("")
        $bundleLines.Add("**Agent:** $selectedAgentName  |  **Priority:** $taskPriority  |  **Assigned:** $now")
        $bundleLines.Add("")
        $bundleLines.Add("## Task")
        $bundleLines.Add("**Title:** $taskTitle")
        if (-not [string]::IsNullOrWhiteSpace($taskDesc)) { $bundleLines.Add("**Description:** $taskDesc") }
        if (-not [string]::IsNullOrWhiteSpace($taskGoal)) { $bundleLines.Add("**Goal:** $taskGoal") }
        if (-not [string]::IsNullOrWhiteSpace($taskReason)) { $bundleLines.Add("**Reason:** $taskReason") }
        $bundleLines.Add("")
        $bundleLines.Add("## Dependencies (what was done before this task)")
        if ($depLines.Count -gt 0) { foreach ($dl in $depLines) { $bundleLines.Add($dl) } }
        else { $bundleLines.Add("- none") }
        $bundleLines.Add("")
        $bundleLines.Add("## Files to work on")
        if ($taskFiles.Count -gt 0) { foreach ($f in $taskFiles) { $bundleLines.Add("- $f") } }
        else { $bundleLines.Add("- (no specific files listed - check architecture.md)") }
        $bundleLines.Add("")
        $bundleLines.Add("## Architecture reference")
        $archPath = Join-Path $orchestratorRoot "documentation/architecture.md"
        if (Test-Path -LiteralPath $archPath -PathType Leaf) {
            $bundleLines.Add("See: ai-orchestrator/documentation/architecture.md")
        } else {
            $bundleLines.Add("- architecture.md not yet available")
        }
        $bundleLines.Add("")
        $bundleLines.Add("## Relevant patterns from prior REPAIR tasks")
        if ($patternLines.Count -gt 0) { foreach ($pl in $patternLines) { $bundleLines.Add($pl) } }
        else { $bundleLines.Add("- no patterns yet") }
        $bundleLines.Add("")
        if ($semanticLines.Count -gt 0) {
            $bundleLines.Add("## Semantically similar past fixes (query_lessons)")
            foreach ($sl in $semanticLines) { $bundleLines.Add($sl) }
            $bundleLines.Add("")
        }

        $safeTaskId = [string]$taskId -replace '[^A-Za-z0-9_-]+', '_'
        $safeAgentForPreflight = $(if ([string]::IsNullOrWhiteSpace($selectedAgentName)) { "unassigned" } else { ($selectedAgentName -replace "[^A-Za-z0-9_-]+", "_") })
        $preflightRelativePath = "ai-orchestrator/tasks/preflight/$safeTaskId-$safeAgentForPreflight.json"
        $completionRelativePath = "ai-orchestrator/tasks/completions/$safeTaskId-<timestamp>.json"
        $bundleLines.Add("## Pre-Flight (mandatory before edits)")
        $bundleLines.Add(("Update this file before changing code: {0}" -f $preflightRelativePath))
        $bundleLines.Add('```json')
        $bundleLines.Add("{")
        $bundleLines.Add('  "schema_version": "v2-preflight",')
        $bundleLines.Add('  "task_id": "' + $taskId + '",')
        $bundleLines.Add('  "agent_name": "' + $selectedAgentName + '",')
        $bundleLines.Add('  "objective": "",')
        $bundleLines.Add('  "thought": "",')
        $bundleLines.Add('  "action_plan": [],')
        $bundleLines.Add('  "risks": [],')
        $bundleLines.Add('  "validation_plan": [],')
        $bundleLines.Add('  "dependencies_needed": [],')
        $bundleLines.Add('  "requires_human_approval": false,')
        $bundleLines.Add('  "library_first_policy": {"enabled": true, "priority": "P0", "local_only": true, "options": ["use-existing-library","hybrid","custom-code-justified","not-applicable"]},')
        $bundleLines.Add('  "local_library_candidates": [],')
        $bundleLines.Add('  "build_vs_buy_recommendation": {"recommended_option": "", "confidence": "", "reason": ""},')
        $bundleLines.Add('  "library_decision_required": true')
        $bundleLines.Add("}")
        $bundleLines.Add('```')
        $bundleLines.Add("")
        $bundleLines.Add("## How to report completion")
        $bundleLines.Add('``````powershell')
        $bundleLines.Add(".\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode complete -ProjectPath <path> -TaskId $taskId -AgentName '$selectedAgentName' -Artifacts 'file1,file2'")
        $bundleLines.Add('  -Notes ''{"schema_version":"v2","summary":"what was done","files_written":["file1","file2"],"tests_passed":true,"validation":["tests run"],"source_files":["file1","file2"],"source_modules":["app/controllers"],"local_library_candidates":["laravel/sanctum"],"library_decision":{"selected_option":"use-existing-library","justification":"Reused local package to reduce delivery time and risk.","selected_libraries":["laravel/sanctum"],"rejected_libraries":[]},"risks":[],"next_steps":[],"tool_calls":[]}''')
        $bundleLines.Add('  # if no library applies, use: "local_library_candidates":[] + "library_decision":{"selected_option":"not-applicable","justification":"No local library reuse required.","selected_libraries":[],"rejected_libraries":[]}')
        $bundleLines.Add("  # optional: -CompletionPayloadPath 'ai-orchestrator/tasks/completions/<prepared>.json'")
        $bundleLines.Add('``````')
        $bundleLines.Add(("Completion payloads are persisted under: {0}" -f $completionRelativePath))

        [System.IO.File]::WriteAllText($bundlePath, ($bundleLines -join [Environment]::NewLine), [System.Text.Encoding]::UTF8)
    } catch { }
    # ─────────────────────────────────────────────────────────────────────────────────────────
}

$lessonsGenerated = New-Object System.Collections.Generic.List[object]
foreach ($task in @($tasks)) {
    $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
    if (-not $taskId.StartsWith("REPAIR-")) { continue }

    $taskStatus = Get-V2TaskStatus -Task $task
    if ($taskStatus -notin @("done", "completed")) { continue }

    $existingLesson = [string](Get-V2OptionalProperty -InputObject $task -Name "lesson_learned_path" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($existingLesson)) {
        $existingLessonPath = Resolve-V2ProjectRelativePath -ProjectRoot $resolvedProjectPath -AnyPath $existingLesson
        if (Test-Path -LiteralPath $existingLessonPath -PathType Leaf) {
            continue
        }
    }

    $lessonResult = New-V2RepairLessonLearned -ProjectRoot $resolvedProjectPath -OrchestratorRoot $orchestratorRoot -Task $task
    if ($lessonResult.generated) {
        Set-V2ObjectProperty -InputObject $task -Name "lesson_learned_path" -Value $lessonResult.lesson_relative
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
        $lessonsGenerated.Add([PSCustomObject]@{
                task_id     = $taskId
                lesson_path = $lessonResult.lesson_relative
            })
    }
}

$tasksSnapshotAfter = @($tasks | ConvertTo-Json -Depth 100 -Compress)
$tasksChanged = ($tasksSnapshotAfter -ne $tasksSnapshotBefore)
$taskStateDbWriteResult = $null
$taskStateDbFlushResult = $null
$dagFlushed = $false
$dagFlushReason = ""

if ($taskStateDbActive) {
    if ($tasksChanged) {
        $tasksBufferPath = Join-Path $orchestratorRoot "state/scheduler-db-tasks-buffer.json"
        Save-V2JsonContent -Path $tasksBufferPath -Value @($tasks)
        try {
            $taskStateDbWriteResult = Invoke-V2TaskStateDbCommand `
                -TaskStateDbScriptPath $taskStateDbScriptPath `
                -ProjectRoot $resolvedProjectPath `
                -Mode "write-tasks" `
                -ExtraArgs @{ "tasks-json-path" = $tasksBufferPath }
            $taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateDbWriteResult -Name "backend_mode" -DefaultValue $taskStateDbBackendMode)
        }
        finally {
            Remove-Item -LiteralPath $tasksBufferPath -Force -ErrorAction SilentlyContinue
        }
    }

    $taskStateDbFlushResult = Invoke-V2TaskStateDbCommand `
        -TaskStateDbScriptPath $taskStateDbScriptPath `
        -ProjectRoot $resolvedProjectPath `
        -Mode "flush-dag"
    $dagFlushed = $true
    if ($tasksChanged) {
        $dagFlushReason = "end-of-cycle-after-write"
    }
    elseif ($taskStateDbDriftSync) {
        $dagFlushReason = "end-of-cycle-after-manual-sync"
    }
    else {
        $dagFlushReason = "end-of-cycle"
    }
    $effectiveBackendMode = $(if ([string]::IsNullOrWhiteSpace($taskStateDbBackendMode)) { "db-primary-v1" } else { $taskStateDbBackendMode })
    Save-V2SchedulerDbState `
        -Path $schedulerDbStatePath `
        -LastFlushAt (Get-V2Timestamp) `
        -LastFlushReason $dagFlushReason `
        -BackendMode $effectiveBackendMode
}
else {
    $tasks = @(Invoke-V2WithDagMutex -DagPath $taskDagJsonPath -ScriptBlock {
            $latestTaskDocument = Get-V2JsonContent -Path $taskDagJsonPath
            $documentToPersist = $(if ($latestTaskDocument -and ($latestTaskDocument.PSObject.Properties.Name -contains "tasks")) {
                $latestTaskDocument
            }
            else {
                $taskDocument
            })

            $localById = @{}
            $localNoId = New-Object System.Collections.Generic.List[object]
            foreach ($localTask in @($tasks)) {
                $localTaskId = [string](Get-V2OptionalProperty -InputObject $localTask -Name "id" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($localTaskId)) {
                    $localNoId.Add($localTask)
                    continue
                }
                $localById[$localTaskId] = $localTask
            }

            $mergedTasks = New-Object System.Collections.Generic.List[object]
            if ($latestTaskDocument -and ($latestTaskDocument.PSObject.Properties.Name -contains "tasks")) {
                foreach ($latestTask in @($latestTaskDocument.tasks)) {
                    $latestTaskId = [string](Get-V2OptionalProperty -InputObject $latestTask -Name "id" -DefaultValue "")
                    if ([string]::IsNullOrWhiteSpace($latestTaskId)) {
                        $mergedTasks.Add($latestTask)
                        continue
                    }

                    if (-not $localById.ContainsKey($latestTaskId)) {
                        $mergedTasks.Add($latestTask)
                        continue
                    }

                    $localTask = $localById[$latestTaskId]
                    $useLocal = $true
                    $latestUpdatedRaw = [string](Get-V2OptionalProperty -InputObject $latestTask -Name "updated_at" -DefaultValue "")
                    $localUpdatedRaw = [string](Get-V2OptionalProperty -InputObject $localTask -Name "updated_at" -DefaultValue "")
                    if (-not [string]::IsNullOrWhiteSpace($latestUpdatedRaw) -and -not [string]::IsNullOrWhiteSpace($localUpdatedRaw)) {
                        try {
                            $latestUpdated = [DateTimeOffset]::Parse($latestUpdatedRaw).UtcDateTime
                            $localUpdated = [DateTimeOffset]::Parse($localUpdatedRaw).UtcDateTime
                            if ($latestUpdated -gt $localUpdated) {
                                $useLocal = $false
                            }
                        }
                        catch {
                            $useLocal = $true
                        }
                    }

                    if ($useLocal) {
                        $mergedTasks.Add($localTask)
                    }
                    else {
                        $mergedTasks.Add($latestTask)
                    }
                    $localById.Remove($latestTaskId)
                }
            }

            foreach ($remainingTask in @($localById.Values)) {
                $mergedTasks.Add($remainingTask)
            }
            foreach ($remainingNoId in @($localNoId.ToArray())) {
                $mergedTasks.Add($remainingNoId)
            }

            $mergedTaskArray = @($mergedTasks.ToArray())
            Set-V2ObjectProperty -InputObject $documentToPersist -Name "updated_at" -Value (Get-V2Timestamp)
            Set-V2ObjectProperty -InputObject $documentToPersist -Name "tasks" -Value $mergedTaskArray
            Save-V2JsonContent -Path $taskDagJsonPath -Value $documentToPersist
            return $mergedTaskArray
        })
}

Save-V2JsonContent -Path $workloadPath -Value $workload
Write-V2TaskBoardsFromJson -OrchestratorRoot $orchestratorRoot -Tasks $tasks

$reputationScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Update-AgentReputation.ps1"
if (Test-Path -LiteralPath $reputationScript -PathType Leaf) {
    try {
        & $reputationScript -ProjectPath $resolvedProjectPath | Out-Null
    }
    catch {
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: reputation-update-failed",
            "- reason: $($_.Exception.Message)"
        )
    }
}

$metaCalibrationScript = Join-Path $PSScriptRoot "Invoke-MetaSchedulerCalibration.ps1"
if (Test-Path -LiteralPath $metaCalibrationScript -PathType Leaf) {
    try {
        & $metaCalibrationScript -ProjectPath $resolvedProjectPath | Out-Null
    }
    catch {
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: meta-calibration-failed",
            "- reason: $($_.Exception.Message)"
        )
    }
}

if ($assignments.Count -gt 0) {
    foreach ($assignment in $assignments) {
        Add-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
            "## $(Get-V2Timestamp)",
            "- event: scheduler-assignment",
            "- task_id: $($assignment.task_id)",
            "- agent: $($assignment.agent)",
            "- priority: $($assignment.priority)",
            "- adjusted_score: $($assignment.adjusted_score)",
            "- reputation_fit_score: $($assignment.reputation_fit_score)",
            "- timeout_penalty: $($assignment.timeout_penalty)"
        )
    }

    $assignmentSummary = @($assignments | ForEach-Object { "$($_.task_id)->$($_.agent)" }) -join ", "
    Add-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
        "## $(Get-V2Timestamp)",
        "- from: SchedulerV2",
        "- to: multi-agent-system",
        "- assignments: $assignmentSummary"
    )
}

if ($staleTakeovers.Count -gt 0) {
    foreach ($takeover in $staleTakeovers) {
        Add-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
            "## $(Get-V2Timestamp)",
            "- event: scheduler-task-takeover",
            "- task_id: $($takeover.task_id)",
            "- from_agent: $($takeover.from_agent)",
            "- reason: $($takeover.reason)",
            "- idle_minutes: $($takeover.idle_minutes)"
        )
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: task-requeued-for-idle-takeover",
            "- task_id: $($takeover.task_id)",
            "- from_agent: $($takeover.from_agent)",
            "- reason: $($takeover.reason)",
            "- idle_minutes: $($takeover.idle_minutes)"
        )
    }
}

if ($lessonsGenerated.Count -gt 0) {
    foreach ($lesson in $lessonsGenerated) {
        Add-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
            "## $(Get-V2Timestamp)",
            "- event: lesson-learned-created",
            "- task_id: $($lesson.task_id)",
            "- lesson_path: $($lesson.lesson_path)"
        )
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: repair-knowledge-captured",
            "- task_id: $($lesson.task_id)",
            "- artifact: $($lesson.lesson_path)"
        )
    }
}

foreach ($syncScript in @($taskSyncScript, $lockSyncScript)) {
    if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
        continue
    }

    try {
        & $syncScript -ProjectPath $resolvedProjectPath | Out-Null
    }
    catch {
        Add-V2MarkdownLog -Path $decisionsPath -Header "# Decision Log" -Lines @(
            "## $(Get-V2Timestamp)",
            "- decision: sync-script-failed",
            "- script: $syncScript",
            "- reason: $($_.Exception.Message)"
        )
    }
}

    $result = [PSCustomObject]@{
        project_path                         = $resolvedProjectPath
        orchestrator_root                    = $orchestratorRoot
        project_status                       = $projectStatus
        strategic_mode                       = $strategicMode
        execution_routing                    = [PSCustomObject]@{
            gpu_routing_enabled         = $gpuRoutingEnabled
            native_complexity_threshold = $gpuRoutingNativeComplexityThreshold
            native_max_files            = $gpuRoutingNativeMaxFiles
            native_max_dependencies     = $gpuRoutingNativeMaxDependencies
            stats                       = [PSCustomObject]$executionProfileStats
        }
        scheduled                            = $assignmentCount
        assignments                          = @($assignments.ToArray())
        takeovers                            = @($staleTakeovers.ToArray())
        lessons_generated                    = @($lessonsGenerated.ToArray())
        task_backend_mode                    = $(if ($taskStateDbActive) { "task-state-db" } else { "task-dag-json" })
        task_state_db_active                 = $taskStateDbActive
        task_state_db_path                   = $taskStateDbPath
        task_state_db_backend_mode           = $taskStateDbBackendMode
        task_state_db_bootstrap_sync         = $taskStateDbBootstrapSync
        task_state_db_bootstrap_reason       = $taskStateDbBootstrapReason
        task_state_db_flush_performed        = $dagFlushed
        task_state_db_flush_reason           = $dagFlushReason
        task_state_db_flush_cooldown_seconds = $TaskStateDbFlushCooldownSeconds
        task_state_db_status                 = $taskStateDbStatusResult
    }

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Scheduler run complete for $resolvedProjectPath"
    Write-Output "Assigned tasks: $assignmentCount"
}
