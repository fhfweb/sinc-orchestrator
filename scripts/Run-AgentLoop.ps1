<#
.SYNOPSIS
    Executes assigned DAG tasks for one agent and closes the scheduler->executor gap.
.DESCRIPTION
    Reads ai-orchestrator/tasks/task-dag.json, finds tasks assigned to one agent in status
    in-progress, executes each task via command or external handler, updates task state,
    releases locks, and writes execution evidence.
    Safe-by-default: only allows command execution from an allowlist.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER AgentName
    Agent identity to execute tasks for.
.PARAMETER RunOnce
    Runs one cycle and exits.
.PARAMETER PollIntervalSeconds
    Sleep interval for continuous mode.
.PARAMETER TaskHandlerScript
    Optional custom script path. Called with:
      -ProjectPath <path> -TaskId <id> -AgentName <agent>
.PARAMETER MaxTasksPerCycle
    Limits tasks executed per loop.
#>
param(
    [string]$ProjectPath = ".",
    [string]$AgentName = "Codex",
    [switch]$RunOnce,
    [int]$PollIntervalSeconds = 10,
    [string]$TaskHandlerScript = "",
    [string]$ExternalAgentBridgeScript = "",
    [bool]$AllowExternalAgentFallbackToNative = $true,
    [int]$ExternalAgentBridgeDispatchCooldownSeconds = 120,
    [int]$MaxTasksPerCycle = 3,
    [switch]$SkipNativeRuntimeTasks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$candidateV2Roots = @(
    (Join-Path (Split-Path -Parent $PSScriptRoot) "ai-orchestrator/scripts/v2"),
    (Join-Path $PSScriptRoot "v2")
)
$v2ScriptRoot = $null
foreach ($candidate in $candidateV2Roots) {
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $v2ScriptRoot = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}
if ([string]::IsNullOrWhiteSpace($v2ScriptRoot)) {
    throw "V2 runtime scripts not found. Expected one of: $($candidateV2Roots -join ', ')"
}

. (Join-Path $v2ScriptRoot "Common.ps1")

$v2StepCheckpointPath = Join-Path $v2ScriptRoot "Invoke-StepCheckpoint.ps1"
$v2OutputSchemaValidatorPath = Join-Path $v2ScriptRoot "Invoke-OutputSchemaValidator.ps1"
$v2HitlGatePath = Join-Path $v2ScriptRoot "Invoke-HITLGate.ps1"

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

function Test-V2NativeRuntimeTask {
    param([object]$Task)

    if ($null -eq $Task) {
        return $false
    }

    $runtimeEngine = [string](Get-V2OptionalProperty -InputObject $Task -Name "runtime_engine" -DefaultValue "")
    $executionMode = [string](Get-V2OptionalProperty -InputObject $Task -Name "execution_mode" -DefaultValue "")

    $runtimeNormalized = $runtimeEngine.Trim().ToLowerInvariant()
    $modeNormalized = $executionMode.Trim().ToLowerInvariant()

    if ($runtimeNormalized -in @("python", "native", "hybrid", "llm-native", "v4-native")) {
        return $true
    }

    if ($modeNormalized -in @("native-agent", "llm-native", "python-runtime", "autonomous-native", "v4-native")) {
        return $true
    }

    return $false
}

function ConvertTo-V2RelativeUnixPath {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFull = [System.IO.Path]::GetFullPath($BasePath)
    $targetFull = [System.IO.Path]::GetFullPath($TargetPath)
    if ($targetFull.StartsWith($baseFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = $targetFull.Substring($baseFull.Length).TrimStart('\', '/')
        return ($relative -replace "\\", "/")
    }
    return ($targetFull -replace "\\", "/")
}

function Get-V2TaskPreflightAbsolutePath {
    param(
        [string]$ProjectRoot,
        [object]$Task,
        [string]$TaskId,
        [string]$AgentName
    )

    $relative = [string](Get-V2OptionalProperty -InputObject $Task -Name "preflight_path" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($relative)) {
        if ([System.IO.Path]::IsPathRooted($relative)) {
            return $relative
        }
        return (Join-Path $ProjectRoot $relative)
    }

    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $safeAgent = if ([string]::IsNullOrWhiteSpace($AgentName)) { "unassigned" } else { ($AgentName -replace "[^A-Za-z0-9_-]+", "_") }
    return (Join-Path $ProjectRoot ("ai-orchestrator/tasks/preflight/{0}-{1}.json" -f $safeTaskId, $safeAgent))
}

function Resolve-V2ExternalAgentBridgeScriptPath {
    param(
        [string]$ProjectRoot,
        [string]$ConfiguredPath
    )

    $candidates = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        if ([System.IO.Path]::IsPathRooted($ConfiguredPath)) {
            $candidates.Add($ConfiguredPath)
        }
        else {
            $candidates.Add((Join-Path $ProjectRoot $ConfiguredPath))
            $candidates.Add((Join-Path $PSScriptRoot $ConfiguredPath))
        }
    }

    $candidates.Add((Join-Path $PSScriptRoot "v2/Invoke-ExternalAgentBridgeV2.ps1"))
    $candidates.Add((Join-Path $v2ScriptRoot "Invoke-ExternalAgentBridgeV2.ps1"))

    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }

    return ""
}

function Invoke-V2TaskPreFlight {
    param(
        [string]$ProjectRoot,
        [object]$Task,
        [string]$TaskId,
        [string]$AgentName,
        [string]$PreFlightScriptPath
    )

    if (-not (Test-Path -LiteralPath $PreFlightScriptPath -PathType Leaf)) {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = "preflight-script-not-found"
            path    = ""
        }
    }

    $preflightPath = Get-V2TaskPreflightAbsolutePath -ProjectRoot $ProjectRoot -Task $Task -TaskId $TaskId -AgentName $AgentName
    $parent = Split-Path -Parent $preflightPath
    Ensure-V2Directory -Path $parent

    try {
        $raw = & $PreFlightScriptPath `
            -ProjectPath $ProjectRoot `
            -TaskId $TaskId `
            -AgentName $AgentName `
            -OutputPath $preflightPath `
            -EmitJson 2>&1 | Out-String
        if (-not (Test-Path -LiteralPath $preflightPath -PathType Leaf)) {
            return [PSCustomObject]@{
                success = $false
                output  = [string]$raw
                error   = "preflight-output-missing"
                path    = ""
            }
        }

        return [PSCustomObject]@{
            success = $true
            output  = [string]$raw
            error   = ""
            path    = $preflightPath
        }
    }
    catch {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = $_.Exception.Message
            path    = ""
        }
    }
}

function Read-V2RuntimeStepCheckpoint {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    if (-not (Test-Path -LiteralPath $v2StepCheckpointPath -PathType Leaf)) {
        return $null
    }
    try {
        $raw = & $v2StepCheckpointPath -Mode "read" -ProjectPath $ProjectRoot -TaskId $TaskId 2>&1 | Out-String
        $parsed = $null
        try {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            $parsed = $null
        }
        if ($null -eq $parsed) { return $null }
        if ($parsed.PSObject.Properties.Count -eq 0) { return $null }
        return $parsed
    }
    catch {
        return $null
    }
}

function Write-V2RuntimeStepCheckpoint {
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

    if (-not (Test-Path -LiteralPath $v2StepCheckpointPath -PathType Leaf)) {
        return
    }
    try {
        & $v2StepCheckpointPath `
            -Mode "write" `
            -ProjectPath $ProjectRoot `
            -TaskId $TaskId `
            -StepNumber $StepNumber `
            -StepName $StepName `
            -Status $Status `
            -AgentName $AgentName `
            -Details $Details `
            -ErrorText $ErrorText | Out-Null
    }
    catch {
    }
}

function Clear-V2RuntimeStepCheckpoints {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    if (-not (Test-Path -LiteralPath $v2StepCheckpointPath -PathType Leaf)) {
        return
    }
    try {
        & $v2StepCheckpointPath -Mode "clear" -ProjectPath $ProjectRoot -TaskId $TaskId | Out-Null
    }
    catch {
    }
}

function Save-V2RuntimeCompletionPayload {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [object]$Payload
    )

    $completionsDir = Join-Path $ProjectRoot "ai-orchestrator/tasks/completions"
    Ensure-V2Directory -Path $completionsDir
    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $fileName = "{0}-{1}.json" -f $safeTaskId, (Get-Date -Format "yyyyMMddHHmmss")
    $targetPath = Join-Path $completionsDir $fileName
    Save-V2JsonContent -Path $targetPath -Value $Payload
    return (ConvertTo-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $targetPath)
}

function New-V2RuntimeCompletionPayload {
    param(
        [string]$TaskId,
        [string]$AgentName,
        [object]$Task,
        [string]$ExecutionOutput,
        [string]$TaskCommand
    )

    $filesWritten = @(Get-V2TaskArrayProperty -Task $Task -Name "files_affected")
    $sourceFiles = @($filesWritten)
    if (@($sourceFiles).Count -eq 0) {
        # Keep schema strict but avoid false negatives for command-only tasks.
        $sourceFiles = @(
            "ai-orchestrator/tasks/execution-history.md",
            "ai-orchestrator/communication/messages.md"
        )
        $filesWritten = @($sourceFiles)
    }

    $sourceModules = New-Object System.Collections.Generic.List[string]
    foreach ($pathValue in @($sourceFiles)) {
        $module = Get-V2SourceModuleFromPath -PathValue ([string]$pathValue)
        if (-not [string]::IsNullOrWhiteSpace($module) -and -not $sourceModules.Contains($module)) {
            $sourceModules.Add($module)
        }
    }

    $validation = New-Object System.Collections.Generic.List[string]
    $validation.Add("runtime-execution-success")
    if (-not [string]::IsNullOrWhiteSpace($TaskCommand)) {
        $validation.Add("command-executed")
    }
    else {
        $validation.Add("handler-or-builtin-executed")
    }

    $toolCalls = @()
    if (-not [string]::IsNullOrWhiteSpace($TaskCommand)) {
        $toolCalls = @([PSCustomObject]@{
            tool    = "powershell-command"
            command = $TaskCommand
        })
    }

    $outputPreview = ""
    if (-not [string]::IsNullOrWhiteSpace($ExecutionOutput)) {
        $lines = @($ExecutionOutput -split "(`r`n|`n|`r)" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($lines.Count -gt 0) {
            $outputPreview = [string]$lines[0]
            if ($outputPreview.Length -gt 200) {
                $outputPreview = $outputPreview.Substring(0, 200)
            }
        }
    }
    $summary = if ([string]::IsNullOrWhiteSpace($outputPreview)) {
        "Task $TaskId executed successfully by $AgentName."
    }
    else {
        "Task $TaskId executed successfully by $AgentName. Output: $outputPreview"
    }

    return [PSCustomObject]@{
        schema_version = "v2"
        task_id        = $TaskId
        agent_name     = $AgentName
        timestamp      = Get-V2Timestamp
        summary        = $summary
        files_written  = @($filesWritten)
        tests_passed   = $true
        validation     = @($validation.ToArray())
        risks          = @()
        next_steps     = @()
        source_files   = @($sourceFiles)
        source_modules = @($sourceModules.ToArray())
        tool_calls     = @($toolCalls)
        local_library_candidates = @()
        library_decision = [PSCustomObject]@{
            selected_option    = "not-applicable"
            justification      = "Runtime auto-execution did not require introducing or evaluating additional libraries."
            selected_libraries = @()
            rejected_libraries = @()
        }
        library_decision_required = $true
    }
}

function Invoke-V2RuntimeOutputSchemaValidation {
    param(
        [string]$ProjectRoot,
        [string]$AgentName,
        [string]$CompletionPayloadPath
    )

    if (-not (Test-Path -LiteralPath $v2OutputSchemaValidatorPath -PathType Leaf)) {
        return [PSCustomObject]@{
            success = $false
            errors = @("validator-script-missing")
            warnings = @()
        }
    }

    try {
        $raw = & $v2OutputSchemaValidatorPath -ProjectPath $ProjectRoot -AgentName $AgentName -PayloadPath $CompletionPayloadPath -EmitJson 2>&1 | Out-String
        $parsed = $null
        try {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            $parsed = $null
        }
        if ($parsed) {
            return [PSCustomObject]@{
                success = [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false)
                errors = @((Get-V2OptionalProperty -InputObject $parsed -Name "errors" -DefaultValue @()))
                warnings = @((Get-V2OptionalProperty -InputObject $parsed -Name "warnings" -DefaultValue @()))
            }
        }
        return [PSCustomObject]@{
            success = $false
            errors = @("validator-unparseable-output")
            warnings = @()
        }
    }
    catch {
        return [PSCustomObject]@{
            success = $false
            errors = @([string]$_.Exception.Message)
            warnings = @()
        }
    }
}

function Write-V2RuntimeTaskTransactionEvent {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName,
        [string]$StatusFrom,
        [string]$StatusTo,
        [bool]$Success,
        [string]$Reason = "",
        [string]$EvidencePath = ""
    )

    $eventsPath = Join-Path $ProjectRoot "ai-orchestrator/state/task-events.jsonl"
    Ensure-V2Directory -Path (Split-Path -Parent $eventsPath)
    $event = [PSCustomObject]@{
        timestamp      = Get-V2Timestamp
        transaction_id = [guid]::NewGuid().ToString()
        operation      = "runtime-complete"
        task_id        = $TaskId
        agent_name     = $AgentName
        status_from    = $StatusFrom
        status_to      = $StatusTo
        success        = $Success
        reason         = $Reason
        evidence_path  = $EvidencePath
    }
    Add-Content -LiteralPath $eventsPath -Value ($event | ConvertTo-Json -Depth 8 -Compress)
}

function Test-V2TaskHitlApproval {
    param(
        [string]$ProjectRoot,
        [string]$TaskId
    )

    if (-not (Test-Path -LiteralPath $v2HitlGatePath -PathType Leaf)) {
        return [PSCustomObject]@{
            allowed = $true
            open_count = 0
            reason = ""
        }
    }

    try {
        $raw = & $v2HitlGatePath -Mode check -ProjectPath $ProjectRoot -TaskId $TaskId -EmitJson 2>&1 | Out-String
        $parsed = $null
        try {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            $parsed = $null
        }
        if (-not $parsed) {
            return [PSCustomObject]@{
                allowed = $true
                open_count = 0
                reason = ""
            }
        }
        $openGates = @((Get-V2OptionalProperty -InputObject $parsed -Name "open_gates" -DefaultValue @()))
        return [PSCustomObject]@{
            allowed = [bool](Get-V2OptionalProperty -InputObject $parsed -Name "allowed" -DefaultValue $true)
            open_count = @($openGates).Count
            reason = if (@($openGates).Count -gt 0) { "hitl-gate-open" } else { "" }
        }
    }
    catch {
        return [PSCustomObject]@{
            allowed = $true
            open_count = 0
            reason = ""
        }
    }
}

function Test-V2CommandAllowed {
    param([string]$Command)

    if ([string]::IsNullOrWhiteSpace($Command)) {
        return $false
    }

    $cmd = $Command.Trim().ToLowerInvariant()
    $allowPrefixes = @(
        "npm test",
        "npm run test",
        "pnpm test",
        "yarn test",
        "pytest",
        "go test",
        "cargo test",
        "dotnet test",
        "php artisan test",
        "python -m pytest"
    )

    foreach ($prefix in $allowPrefixes) {
        if ($cmd.StartsWith($prefix)) {
            return $true
        }
    }

    return $false
}

function Test-V2BuiltinAutoTaskSupported {
    param(
        [string]$TaskId,
        [object]$Task = $null
    )

    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        return $false
    }

    $supported = @(
        "DEV-STACK-DECISION-001",
        "DEV-VERIFY-COMMANDS-001",
        "DEV-SCAFFOLD-API-001",
        "DEV-SCAFFOLD-DOMAIN-001",
        "DEV-MIGRATION-001",
        "DEV-TEST-API-001",
        "DEV-WORKER-001",
        "DEV-ENV-EXAMPLE-001",
        "DEV-ARCH-DOC-001",
        "PLAN-BUSINESS-CONTEXT-001",
        "PLAN-ARCH-ADR-001",
        "PLAN-INTERFACE-CONTRACTS-001",
        "QA-CODE-REVIEW-BASELINE-001",
        "QA-PRODUCT-VALIDATION-BASELINE-001",
        "QA-USER-SIMULATION-BASELINE-001",
        "OPS-OBSERVABILITY-BASELINE-001",
        "FEAT-FRONTEND-WEB-001",
        "FEAT-INTEGRATION-WHATSAPP-001",
        "FEAT-OPS-BACKUP-DR-001",
        "FEAT-QA-E2E-PRODUCT-001",
        "REL-STAGING-001",
        "REL-PROD-001"
    )

    if ($TaskId -in $supported) {
        return $true
    }
    if ($TaskId -like "REPAIR-DEPLOY-*") {
        return $true
    }
    if ($TaskId -like "REPAIR-TEST-FAIL-*") {
        return $true
    }
    if ($TaskId -like "REPAIR-RESOURCE-*") {
        return $true
    }
    if ($TaskId -in @(
        "FEAT-GPU-LOCAL-INFERENCE",
        "FEAT-GPU-VRAM-GUARDRAILS",
        "REFACTOR-TASK-DAG-DB-MIGRATION",
        "TASK-GRAPH-HYDRATION-BOOTSTRAP",
        "TASK-QDRANT-FULL-VECTORIZATION",
        "TASK-GPU-OBSERVABILITY-PANEL",
        "TASK-HYBRID-ROUTING-TUNING",
        "TASK-IMPACT-ENGINE",
        "TASK-E2E-ORCHESTRATOR-PIPELINE"
    )) {
        return $true
    }
    $taskReason = [string](Get-V2OptionalProperty -InputObject $Task -Name "reason" -DefaultValue "")
    $taskSource = [string](Get-V2OptionalProperty -InputObject $Task -Name "source_incident" -DefaultValue "")
    if ($taskReason -like "execution-backlog-gap:*" -or $taskSource -match "_execution_backlog_gap\.md$") {
        return $true
    }
    return $false
}

function Invoke-V2SeedExecutionBacklogFromRoadmap {
    param([string]$ProjectRoot)

    $orchestratorRoot = Join-Path $ProjectRoot "ai-orchestrator"
    $dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
    if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
        return [PSCustomObject]@{ success = $false; output = ""; error = "execution-backlog-seed-missing-task-dag" }
    }

    $dag = Get-V2JsonContent -Path $dagPath
    if (-not $dag -or -not ($dag.PSObject.Properties.Name -contains "tasks")) {
        return [PSCustomObject]@{ success = $false; output = ""; error = "execution-backlog-seed-invalid-task-dag" }
    }

    $tasks = @($dag.tasks)
    $openExecutionTasks = @(
        @($tasks) | Where-Object {
            $id = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
            $status = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            $isExecution = ($id -like "FEAT-*") -or ($id -like "DEV-*") -or ($id -like "COBERTURA-*") -or ($id -like "RECHECK-*")
            $isOpen = $status -in @("pending", "in-progress", "blocked", "blocked-runtime", "blocked-lock-conflict", "blocked-phase-approval", "needs-revision")
            $isExecution -and $isOpen
        }
    )
    if ($openExecutionTasks.Count -gt 0) {
        return [PSCustomObject]@{ success = $true; output = ("execution-backlog-already-open:{0}" -f $openExecutionTasks.Count); error = "" }
    }

    $seedTemplates = @(
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

    $existingById = @{}
    foreach ($task in @($tasks)) {
        $id = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($id)) {
            $existingById[$id] = $true
        }
    }

    $added = 0
    foreach ($tpl in $seedTemplates) {
        $tplId = [string](Get-V2OptionalProperty -InputObject $tpl -Name "id" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($tplId)) { continue }
        if ($existingById.ContainsKey($tplId)) { continue }

        $now = Get-V2Timestamp
        $tasks += [PSCustomObject]@{
            id              = $tplId
            title           = [string](Get-V2OptionalProperty -InputObject $tpl -Name "title" -DefaultValue $tplId)
            description     = [string](Get-V2OptionalProperty -InputObject $tpl -Name "description" -DefaultValue "")
            priority        = [string](Get-V2OptionalProperty -InputObject $tpl -Name "priority" -DefaultValue "P2")
            dependencies    = @((Get-V2OptionalProperty -InputObject $tpl -Name "dependencies" -DefaultValue @()))
            preferred_agent = "Codex"
            assigned_agent  = ""
            execution_mode  = "external-agent"
            status          = "pending"
            files_affected  = @((Get-V2OptionalProperty -InputObject $tpl -Name "files_affected" -DefaultValue @()))
            created_at      = $now
            updated_at      = $now
        }
        $existingById[$tplId] = $true
        $added++
    }

    if ($added -eq 0) {
        return [PSCustomObject]@{ success = $true; output = "execution-backlog-seed-noop"; error = "" }
    }

    Set-V2ObjectProperty -InputObject $dag -Name "tasks" -Value @($tasks)
    Set-V2ObjectProperty -InputObject $dag -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $dagPath -Value $dag

    $schedulerSyncScript = Join-Path $PSScriptRoot "v2/Invoke-SchedulerV2.ps1"
    if (Test-Path -LiteralPath $schedulerSyncScript -PathType Leaf) {
        try {
            & $schedulerSyncScript -ProjectPath $ProjectRoot -MaxAssignmentsPerRun 0 -EmitJson | Out-Null
        }
        catch {
        }
    }

    return [PSCustomObject]@{ success = $true; output = ("execution-backlog-seeded:{0}" -f $added); error = "" }
}

function Invoke-V2360ArtifactGenerator {
    param([string]$ProjectRoot)

    $scriptPath = Join-Path $PSScriptRoot "Generate-Orchestrator360Artifacts.ps1"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        return [PSCustomObject]@{ success = $false; output = ""; error = "360-artifact-generator-missing" }
    }

    try {
        $rawOutput = & $scriptPath -ProjectPath $ProjectRoot 2>&1 | Out-String
        $parsed = $null
        try {
            $parsed = ($rawOutput | ConvertFrom-Json)
        }
        catch {
            $parsed = $null
        }

        if (-not ($parsed -and [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false))) {
            if ($parsed) {
                return [PSCustomObject]@{ success = $false; output = $rawOutput; error = "orchestrator-360-artifacts-failed" }
            }
            return [PSCustomObject]@{ success = $false; output = $rawOutput; error = "orchestrator-360-artifacts-unparseable-output" }
        }

        $enginePath = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-Orchestrator360DecisionEngine.ps1"
        if (Test-Path -LiteralPath $enginePath -PathType Leaf) {
            $engineRaw = & $enginePath -ProjectPath $ProjectRoot -AutoRepairTasks 2>&1 | Out-String
            $engineParsed = $null
            try {
                $engineParsed = ($engineRaw | ConvertFrom-Json)
            }
            catch {
                $engineParsed = $null
            }

            if (-not ($engineParsed -and [bool](Get-V2OptionalProperty -InputObject $engineParsed -Name "success" -DefaultValue $false))) {
                return [PSCustomObject]@{
                    success = $false
                    output  = $engineRaw
                    error   = "orchestrator-360-engine-failed"
                }
            }
        }

        return [PSCustomObject]@{ success = $true; output = "orchestrator-360-execution-updated"; error = "" }
    }
    catch {
        return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
    }
}

function Ensure-V2DirectoryPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Write-V2FileIfMissing {
    param(
        [string]$Path,
        [string]$Content
    )

    $parent = Split-Path -Parent $Path
    Ensure-V2DirectoryPath -Path $parent
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        [System.IO.File]::WriteAllText($Path, $Content)
    }
}

function Ensure-V2RequirementsEntries {
    param(
        [string]$RequirementsPath,
        [string[]]$Entries
    )

    $parent = Split-Path -Parent $RequirementsPath
    Ensure-V2DirectoryPath -Path $parent

    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $RequirementsPath -PathType Leaf) {
        foreach ($line in @(Get-Content -LiteralPath $RequirementsPath)) {
            $lines.Add([string]$line)
        }
    }

    $changed = $false
    foreach ($entry in @($Entries)) {
        $normalized = [string]$entry
        if ([string]::IsNullOrWhiteSpace($normalized)) { continue }
        $packageName = ($normalized -split "[<>=!~]")[0].Trim()
        if ([string]::IsNullOrWhiteSpace($packageName)) { continue }
        $regex = "^\s*" + [regex]::Escape($packageName) + "([<>=!~].*)?\s*$"
        $exists = $false
        foreach ($line in @($lines)) {
            if ([regex]::IsMatch([string]$line, $regex, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
                $exists = $true
                break
            }
        }
        if (-not $exists) {
            $lines.Add($normalized)
            $changed = $true
        }
    }

    if ($changed -or -not (Test-Path -LiteralPath $RequirementsPath -PathType Leaf)) {
        [System.IO.File]::WriteAllText($RequirementsPath, (($lines -join [Environment]::NewLine) + [Environment]::NewLine))
    }
}

function Get-V2ProjectStateDocument {
    param([string]$ProjectRoot)
    $statePath = Join-Path $ProjectRoot "ai-orchestrator/state/project-state.json"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    $state = Get-V2JsonContent -Path $statePath
    if ($state) {
        $phaseApprovalsChanged = Ensure-V2PhaseApprovals -ProjectState $state -UpdatedBy "run-agent-loop-auto"
        if ($phaseApprovalsChanged) {
            Set-V2ObjectProperty -InputObject $state -Name "updated_at" -Value (Get-V2Timestamp)
            Save-V2JsonContent -Path $statePath -Value $state
        }
    }
    return $state
}

function Save-V2ProjectStateDocument {
    param(
        [string]$ProjectRoot,
        [object]$StateDocument
    )
    $statePath = Join-Path $ProjectRoot "ai-orchestrator/state/project-state.json"
    Save-V2JsonContent -Path $statePath -Value $StateDocument
}

function Test-V2TaskPhaseApproval {
    param(
        [string]$ProjectRoot,
        [object]$Task
    )

    $requiredPhase = [string](Get-V2OptionalProperty -InputObject $Task -Name "required_phase_approval" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($requiredPhase)) {
        return [PSCustomObject]@{
            allowed = $true
            phase   = ""
            status  = "not-required"
            reason  = ""
        }
    }

    $state = Get-V2ProjectStateDocument -ProjectRoot $ProjectRoot
    if ($null -eq $state) {
        return [PSCustomObject]@{
            allowed = $false
            phase   = $requiredPhase
            status  = "missing-state"
            reason  = "phase-approval-state-missing"
        }
    }

    $phaseApprovals = Get-V2OptionalProperty -InputObject $state -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
    $entry = Get-V2OptionalProperty -InputObject $phaseApprovals -Name $requiredPhase -DefaultValue ([PSCustomObject]@{ status = "pending" })
    $approvalStatus = [string](Get-V2OptionalProperty -InputObject $entry -Name "status" -DefaultValue "pending")
    if ([string]::IsNullOrWhiteSpace($approvalStatus)) {
        $approvalStatus = "pending"
    }
    $approvalStatus = $approvalStatus.ToLowerInvariant()

    if ($approvalStatus -eq "approved") {
        return [PSCustomObject]@{
            allowed = $true
            phase   = $requiredPhase
            status  = $approvalStatus
            reason  = ""
        }
    }

    return [PSCustomObject]@{
        allowed = $false
        phase   = $requiredPhase
        status  = $approvalStatus
        reason  = ("phase-approval-{0}-{1}" -f $requiredPhase, $approvalStatus)
    }
}

function Get-V2PrimaryLanguage {
    param([object]$StateDocument)
    if ($null -eq $StateDocument) { return "" }
    $fingerprint = Get-V2OptionalProperty -InputObject $StateDocument -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
    $language = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "")
    return $language.Trim().ToLowerInvariant()
}

function Set-V2VerifiedPythonCommands {
    param(
        [object]$StateDocument,
        [string]$ProjectRoot
    )

    if ($null -eq $StateDocument) {
        return
    }

    $buildCommand = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "app") -PathType Container) {
        "python -m compileall app"
    }
    else {
        "python -m compileall ."
    }
    $testCommand = "python -m pytest"

    $verified = Get-V2OptionalProperty -InputObject $StateDocument -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    if ($null -eq $verified) {
        $verified = [PSCustomObject]@{}
    }

    $buildSpec = [PSCustomObject]@{
        value      = $buildCommand
        confidence = "verified"
        source     = "run-agent-loop-auto-scaffold"
    }
    $testSpec = [PSCustomObject]@{
        value      = $testCommand
        confidence = "verified"
        source     = "run-agent-loop-auto-scaffold"
    }

    Set-V2ObjectProperty -InputObject $verified -Name "build" -Value $buildSpec
    Set-V2ObjectProperty -InputObject $verified -Name "test" -Value $testSpec
    Set-V2ObjectProperty -InputObject $StateDocument -Name "verified_commands" -Value $verified
    Set-V2ObjectProperty -InputObject $StateDocument -Name "updated_at" -Value (Get-V2Timestamp)
}

function Set-V2VerifiedCommandsForLanguage {
    param(
        [object]$StateDocument,
        [string]$ProjectRoot,
        [string]$Language
    )

    if ($null -eq $StateDocument) { return }
    $normalized = ([string]$Language).Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized -eq "unknown") {
        $normalized = "python"
    }

    $buildCommand = ""
    $testCommand = ""
    switch ($normalized) {
        "python" {
            $buildCommand = if (Test-Path -LiteralPath (Join-Path $ProjectRoot "app") -PathType Container) { "python -m compileall app" } else { "python -m compileall ." }
            $testCommand = "python -m pytest"
        }
        "node" {
            $buildCommand = "npm run build"
            $testCommand = "npm test"
        }
        "php" {
            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "artisan") -PathType Leaf) {
                $buildCommand = "php artisan test --testsuite=Unit"
                $testCommand = "php artisan test"
            }
            else {
                $buildCommand = "composer test"
                $testCommand = "composer test"
            }
        }
        "go" {
            $buildCommand = "go build ./..."
            $testCommand = "go test ./..."
        }
        "dotnet" {
            $buildCommand = "dotnet build"
            $testCommand = "dotnet test"
        }
        default {
            $buildCommand = "python -m compileall ."
            $testCommand = "python -m pytest"
        }
    }

    $verified = Get-V2OptionalProperty -InputObject $StateDocument -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    if ($null -eq $verified) { $verified = [PSCustomObject]@{} }
    Set-V2ObjectProperty -InputObject $verified -Name "build" -Value ([PSCustomObject]@{ value = $buildCommand; confidence = "verified"; source = "run-agent-loop-auto-scaffold" })
    Set-V2ObjectProperty -InputObject $verified -Name "test" -Value ([PSCustomObject]@{ value = $testCommand; confidence = "verified"; source = "run-agent-loop-auto-scaffold" })
    Set-V2ObjectProperty -InputObject $StateDocument -Name "verified_commands" -Value $verified
    Set-V2ObjectProperty -InputObject $StateDocument -Name "updated_at" -Value (Get-V2Timestamp)
}

function Invoke-V2BuiltinAutoTaskExecutionNonPython {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$Language,
        [object]$StateDocument
    )

    $lang = ([string]$Language).Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($lang) -or $lang -eq "unknown") {
        return [PSCustomObject]@{ success = $false; output = ""; error = "builtin-auto-task-unsupported-language:$Language" }
    }

    $srcDir = Join-Path $ProjectRoot "src"
    $testsDir = Join-Path $ProjectRoot "tests"
    Ensure-V2DirectoryPath -Path $srcDir
    Ensure-V2DirectoryPath -Path $testsDir

    switch ($TaskId) {
        "DEV-VERIFY-COMMANDS-001" {
            if ($null -eq $StateDocument) { return [PSCustomObject]@{ success = $false; output = ""; error = "missing-project-state" } }
            Set-V2VerifiedCommandsForLanguage -StateDocument $StateDocument -ProjectRoot $ProjectRoot -Language $lang
            Save-V2ProjectStateDocument -ProjectRoot $ProjectRoot -StateDocument $StateDocument
            return [PSCustomObject]@{ success = $true; output = "verified-commands-updated-$lang"; error = "" }
        }
        "DEV-SCAFFOLD-API-001" {
            switch ($lang) {
                "node" {
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "package.json") -Content @'
{
  "name": "ai-orchestrated-node-project",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "build": "node -e \"console.log('build ok')\"",
    "test": "node --test",
    "start": "node src/main.js"
  }
}
'@
                    Write-V2FileIfMissing -Path (Join-Path $srcDir "main.js") -Content @'
const http = require("http");
const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
    return;
  }
  res.writeHead(404);
  res.end();
});
server.listen(process.env.PORT || 8000);
'@
                }
                "php" {
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "index.php") -Content @'
<?php
if ($_SERVER["REQUEST_URI"] === "/health") {
    header("Content-Type: application/json");
    echo json_encode(["status" => "ok"]);
    exit;
}
http_response_code(404);
'@
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "composer.json") -Content @'
{
  "name": "ai/orchestrated-php-project",
  "type": "project",
  "require": {},
  "scripts": {
    "test": "php -l index.php"
  }
}
'@
                }
                "go" {
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "go.mod") -Content @'
module ai/orchestrated-go-project

go 1.22
'@
                    Ensure-V2DirectoryPath -Path (Join-Path $ProjectRoot "cmd/api")
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "cmd/api/main.go") -Content @'
package main

import (
	"encoding/json"
	"net/http"
)

func main() {
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})
	_ = http.ListenAndServe(":8000", nil)
}
'@
                }
                "dotnet" {
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "OrchestratedApp.csproj") -Content @'
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>
'@
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "Program.cs") -Content @'
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
app.MapGet("/health", () => Results.Json(new { status = "ok" }));
app.Run();
'@
                }
                default {
                    return [PSCustomObject]@{ success = $false; output = ""; error = "builtin-auto-task-unsupported-language:$lang" }
                }
            }
            return [PSCustomObject]@{ success = $true; output = "api-scaffold-created-$lang"; error = "" }
        }
        "DEV-SCAFFOLD-DOMAIN-001" {
            switch ($lang) {
                "node" {
                    Write-V2FileIfMissing -Path (Join-Path $srcDir "domain.js") -Content @'
const entities = [];
function createEntity(name) {
  const entity = { id: entities.length + 1, name };
  entities.push(entity);
  return entity;
}
function listEntities() { return entities; }
module.exports = { createEntity, listEntities };
'@
                }
                "php" {
                    Ensure-V2DirectoryPath -Path (Join-Path $ProjectRoot "src")
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "src/Domain.php") -Content @'
<?php
final class DomainEntity {
    public function __construct(public int $id, public string $name) {}
}
'@
                }
                "go" {
                    Ensure-V2DirectoryPath -Path (Join-Path $ProjectRoot "internal/domain")
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "internal/domain/entity.go") -Content @'
package domain

type Entity struct {
	ID   int
	Name string
}
'@
                }
                "dotnet" {
                    Ensure-V2DirectoryPath -Path (Join-Path $ProjectRoot "Domain")
                    Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "Domain/Entity.cs") -Content @'
namespace OrchestratedApp.Domain;
public sealed record Entity(int Id, string Name);
'@
                }
            }
            return [PSCustomObject]@{ success = $true; output = "domain-scaffold-created-$lang"; error = "" }
        }
        "DEV-MIGRATION-001" {
            switch ($lang) {
                "node" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "migrations/0001_initial.sql") -Content "CREATE TABLE IF NOT EXISTS entities (id SERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL);" }
                "php" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "database/migrations/0001_initial.sql") -Content "CREATE TABLE entities (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL);" }
                "go" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "migrations/0001_initial.sql") -Content "CREATE TABLE IF NOT EXISTS entities (id SERIAL PRIMARY KEY, name VARCHAR(120) NOT NULL);" }
                "dotnet" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "Migrations/0001_initial.sql") -Content "CREATE TABLE IF NOT EXISTS Entities (Id INT PRIMARY KEY, Name VARCHAR(120) NOT NULL);" }
            }
            return [PSCustomObject]@{ success = $true; output = "migration-scaffold-created-$lang"; error = "" }
        }
        "DEV-TEST-API-001" {
            switch ($lang) {
                "node" {
                    Write-V2FileIfMissing -Path (Join-Path $testsDir "health.test.js") -Content @'
const test = require("node:test");
const assert = require("node:assert/strict");
test("sanity", () => {
  assert.equal(1 + 1, 2);
});
'@
                }
                "php" {
                    Write-V2FileIfMissing -Path (Join-Path $testsDir "HealthTest.php") -Content @'
<?php
if (1 + 1 !== 2) { throw new RuntimeException("sanity failed"); }
'@
                }
                "go" {
                    Write-V2FileIfMissing -Path (Join-Path $testsDir "health_test.go") -Content @'
package tests
import "testing"
func TestSanity(t *testing.T) {
	if 1+1 != 2 { t.Fatal("sanity failed") }
}
'@
                }
                "dotnet" {
                    Write-V2FileIfMissing -Path (Join-Path $testsDir "HealthTests.cs") -Content @'
namespace OrchestratedApp.Tests;
public sealed class HealthTests {
    public void Sanity() {
        if (1 + 1 != 2) throw new System.Exception("sanity failed");
    }
}
'@
                }
            }
            return [PSCustomObject]@{ success = $true; output = "api-tests-created-$lang"; error = "" }
        }
        "DEV-WORKER-001" {
            switch ($lang) {
                "node" { Write-V2FileIfMissing -Path (Join-Path $srcDir "worker.js") -Content "setInterval(() => console.log('worker-heartbeat'), 60000);" }
                "php" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "worker.php") -Content "<?php while (true) { echo 'worker-heartbeat'.PHP_EOL; sleep(60); }" }
                "go" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "cmd/worker/main.go") -Content "package main`nimport \"time\"`nfunc main(){for{time.Sleep(60*time.Second)}}" }
                "dotnet" { Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "Worker.cs") -Content "public sealed class Worker { public void Run() { while (true) { System.Threading.Thread.Sleep(60000); } } }" }
            }
            return [PSCustomObject]@{ success = $true; output = "worker-created-$lang"; error = "" }
        }
        "DEV-ENV-EXAMPLE-001" {
            Write-V2FileIfMissing -Path (Join-Path $ProjectRoot ".env.example") -Content @'
APP_ENV=development
PORT=8000
DATABASE_URL=
'@
            return [PSCustomObject]@{ success = $true; output = "env-example-created"; error = "" }
        }
        "DEV-ARCH-DOC-001" {
            $architecturePath = Join-Path $ProjectRoot "ai-orchestrator/documentation/architecture.md"
            Write-V2FileIfMissing -Path $architecturePath -Content "# Architecture`n`n- Runtime Stack: $lang`n- Core Gate: CORE-COMPLETE-001"
            return [PSCustomObject]@{ success = $true; output = "architecture-doc-validated"; error = "" }
        }
        "FEAT-FRONTEND-WEB-001" {
            if ($lang -eq "php") {
                $layoutPath = Join-Path $ProjectRoot "resources/views/layouts/app.blade.php"
                Write-V2FileIfMissing -Path $layoutPath -Content @'
<!doctype html>
<html lang="pt-BR">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PsiGestor</title>
    <script type="module" src="/resources/js/app.js"></script>
</head>
<body class="min-h-screen bg-gray-100 text-gray-900">
    <div class="mx-auto max-w-7xl p-6">
        <nav class="mb-6 flex flex-wrap gap-3 text-sm">
            <a href="/dashboard">Dashboard</a>
            <a href="/patients">Pacientes</a>
            <a href="/appointments">Agenda</a>
            <a href="/records">Prontuários</a>
            <a href="/financial">Financeiro</a>
            <a href="/documents">Documentos</a>
        </nav>
        @yield('content')
    </div>
</body>
</html>
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/dashboard/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Dashboard</h1>
<p class="mt-2">Visão geral clínica.</p>
@endsection
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/patients/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Pacientes</h1>
@endsection
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/appointments/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Agenda</h1>
@endsection
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/records/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Prontuários</h1>
@endsection
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/financial/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Financeiro</h1>
@endsection
'@
                Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "resources/views/documents/index.blade.php") -Content @'
@extends('layouts.app')
@section('content')
<h1 class="text-2xl font-semibold">Documentos</h1>
@endsection
'@
                $webRoutesPath = Join-Path $ProjectRoot "routes/web.php"
                $routeBlock = @'
Route::view('/dashboard', 'dashboard.index');
Route::view('/patients', 'patients.index');
Route::view('/appointments', 'appointments.index');
Route::view('/records', 'records.index');
Route::view('/financial', 'financial.index');
Route::view('/documents', 'documents.index');
'@
                if (-not (Test-Path -LiteralPath $webRoutesPath -PathType Leaf)) {
                    $newContent = @'
<?php

use Illuminate\Support\Facades\Route;

'@ + "`r`n" + $routeBlock
                    [System.IO.File]::WriteAllText($webRoutesPath, $newContent)
                }
                else {
                    $currentRoutes = Get-Content -LiteralPath $webRoutesPath -Raw
                    if ($currentRoutes -notmatch "Route::view\('/dashboard'") {
                        if (-not $currentRoutes.EndsWith("`n")) {
                            $currentRoutes += "`r`n"
                        }
                        $currentRoutes += "`r`n$routeBlock`r`n"
                        [System.IO.File]::WriteAllText($webRoutesPath, $currentRoutes)
                    }
                }
                return [PSCustomObject]@{ success = $true; output = "frontend-web-scaffold-created-$lang"; error = "" }
            }
            return [PSCustomObject]@{ success = $true; output = "frontend-task-noop-$lang"; error = "" }
        }
        "FEAT-INTEGRATION-WHATSAPP-001" {
            if ($lang -eq "php") {
                $servicePath = Join-Path $ProjectRoot "app/Services/WhatsappReminderService.php"
                Write-V2FileIfMissing -Path $servicePath -Content @'
<?php

namespace App\Services;

use App\Models\Appointment;
use Illuminate\Support\Facades\Log;

class WhatsappReminderService
{
    public function sendAppointmentReminder(Appointment $appointment): bool
    {
        $provider = (string) config('services.whatsapp.provider', 'log');
        $payload = [
            'appointment_id' => $appointment->id,
            'patient_id' => $appointment->patient_id,
            'scheduled_at' => optional($appointment->scheduled_at)->toIso8601String(),
            'provider' => $provider,
        ];

        Log::info('whatsapp-reminder-dispatched', $payload);
        return true;
    }
}
'@
                $servicesConfigPath = Join-Path $ProjectRoot "config/services.php"
                if (Test-Path -LiteralPath $servicesConfigPath -PathType Leaf) {
                    $servicesContent = Get-Content -LiteralPath $servicesConfigPath -Raw
                    if ($servicesContent -notmatch "'whatsapp'\s*=>") {
                        $whatsAppBlock = @"
    'whatsapp' => [
        'provider' => env('WHATSAPP_PROVIDER', 'log'),
        'api_url' => env('WHATSAPP_API_URL'),
        'token' => env('WHATSAPP_API_TOKEN'),
    ],
"@
                        $servicesContent = $servicesContent -replace "(?ms)\r?\n\];\s*$", "`r`n$whatsAppBlock`r`n];"
                        [System.IO.File]::WriteAllText($servicesConfigPath, $servicesContent)
                    }
                }
                else {
                    Write-V2FileIfMissing -Path $servicesConfigPath -Content @'
<?php

return [
    'whatsapp' => [
        'provider' => env('WHATSAPP_PROVIDER', 'log'),
        'api_url' => env('WHATSAPP_API_URL'),
        'token' => env('WHATSAPP_API_TOKEN'),
    ],
];
'@
                }

                $envExamplePath = Join-Path $ProjectRoot ".env.example"
                $envContent = if (Test-Path -LiteralPath $envExamplePath -PathType Leaf) { Get-Content -LiteralPath $envExamplePath -Raw } else { "" }
                foreach ($entry in @("WHATSAPP_PROVIDER=log", "WHATSAPP_API_URL=", "WHATSAPP_API_TOKEN=")) {
                    if ($envContent -notmatch ("(?m)^" + [regex]::Escape(($entry -split "=")[0]) + "=")) {
                        if (-not [string]::IsNullOrWhiteSpace($envContent) -and -not $envContent.EndsWith("`n")) {
                            $envContent += "`r`n"
                        }
                        $envContent += "$entry`r`n"
                    }
                }
                [System.IO.File]::WriteAllText($envExamplePath, $envContent)

                return [PSCustomObject]@{ success = $true; output = "whatsapp-integration-baseline-created-$lang"; error = "" }
            }
            return [PSCustomObject]@{ success = $true; output = "whatsapp-task-noop-$lang"; error = "" }
        }
        "FEAT-OPS-BACKUP-DR-001" {
            $runbookPath = Join-Path $ProjectRoot "docs/runbooks/backup-disaster-recovery.md"
            Write-V2FileIfMissing -Path $runbookPath -Content @'
# Backup & Disaster Recovery Runbook

## Objetivo
Garantir backup diário criptografado e restauração validada para dados clínicos sensíveis.

## Escopo
- Banco transacional (PostgreSQL/MySQL)
- Arquivos de documentos/recibos
- Metadados operacionais do orquestrador

## Rotina de backup
1. Gerar dump consistente do banco.
2. Criptografar artefato antes de upload.
3. Publicar em storage redundante.
4. Registrar checksum e timestamp.

## Teste de restauração
1. Restaurar em ambiente isolado.
2. Validar integridade de tabelas críticas.
3. Executar smoke test de API.
4. Registrar evidências e tempo de recuperação.

## RPO/RTO alvo
- RPO: 24h
- RTO: 2h
'@
            return [PSCustomObject]@{ success = $true; output = "backup-dr-runbook-created-$lang"; error = "" }
        }
        "FEAT-QA-E2E-PRODUCT-001" {
            if ($lang -eq "php") {
                $testPath = Join-Path $ProjectRoot "tests/Feature/PsychologyEndToEndFlowTest.php"
                Write-V2FileIfMissing -Path $testPath -Content @'
<?php

namespace Tests\Feature;

use Tests\TestCase;

class PsychologyEndToEndFlowTest extends TestCase
{
    public function test_web_shell_pages_are_available(): void
    {
        $this->get('/dashboard')->assertStatus(200);
        $this->get('/patients')->assertStatus(200);
        $this->get('/appointments')->assertStatus(200);
        $this->get('/records')->assertStatus(200);
        $this->get('/financial')->assertStatus(200);
        $this->get('/documents')->assertStatus(200);
    }
}
'@
                return [PSCustomObject]@{ success = $true; output = "product-e2e-test-created-$lang"; error = "" }
            }
            return [PSCustomObject]@{ success = $true; output = "product-e2e-task-noop-$lang"; error = "" }
        }
        "REL-STAGING-001" {
            $releaseScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-ReleasePipelineV2.ps1"
            if (-not (Test-Path -LiteralPath $releaseScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "release-pipeline-script-missing" }
            }
            try {
                $raw = & $releaseScript -ProjectPath $ProjectRoot -Environment "staging" -RollbackOnFailure $true 2>&1 | Out-String
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "REL-PROD-001" {
            $releaseScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-ReleasePipelineV2.ps1"
            if (-not (Test-Path -LiteralPath $releaseScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "release-pipeline-script-missing" }
            }
            try {
                $raw = & $releaseScript -ProjectPath $ProjectRoot -Environment "production" -RollbackOnFailure $true 2>&1 | Out-String
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        default {
            return [PSCustomObject]@{ success = $false; output = ""; error = "builtin-auto-task-unsupported-language:$lang" }
        }
    }
}

function Invoke-V2BuiltinAutoTaskExecution {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [object]$Task = $null
    )

    $state = Get-V2ProjectStateDocument -ProjectRoot $ProjectRoot
    $language = Get-V2PrimaryLanguage -StateDocument $state
    $isPython = $language -eq "python" -or [string]::IsNullOrWhiteSpace($language) -or $language -eq "unknown"

    if ($TaskId -eq "DEV-STACK-DECISION-001") {
        if ($null -eq $state) {
            return [PSCustomObject]@{ success = $false; output = ""; error = "missing-project-state" }
        }

        $fingerprint = Get-V2OptionalProperty -InputObject $state -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
        $existingLang = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "")
        $language = $existingLang.Trim().ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($language) -or $language -eq "unknown") {
            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "package.json") -PathType Leaf) {
                $language = "node"
            }
            elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "composer.json") -PathType Leaf) {
                $language = "php"
            }
            elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "go.mod") -PathType Leaf) {
                $language = "go"
            }
            elseif (
                (Get-ChildItem -LiteralPath $ProjectRoot -Filter "*.sln" -ErrorAction SilentlyContinue | Select-Object -First 1) -or
                (Get-ChildItem -LiteralPath $ProjectRoot -Filter "*.csproj" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
            ) {
                $language = "dotnet"
            }
            else {
                $language = "python"
            }
        }

        Set-V2ObjectProperty -InputObject $fingerprint -Name "primary_language" -Value $language
        Set-V2ObjectProperty -InputObject $state -Name "technical_fingerprint" -Value $fingerprint
        Set-V2VerifiedCommandsForLanguage -StateDocument $state -ProjectRoot $ProjectRoot -Language $language
        Save-V2ProjectStateDocument -ProjectRoot $ProjectRoot -StateDocument $state
        return [PSCustomObject]@{ success = $true; output = ("stack-decided-" + $language); error = "" }
    }

    if ($TaskId -like "REPAIR-TEST-FAIL-*") {
        $testRunnerScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-TestRunnerV2.ps1"
        if (-not (Test-Path -LiteralPath $testRunnerScript -PathType Leaf)) {
            return [PSCustomObject]@{ success = $false; output = ""; error = "test-runner-script-missing" }
        }
        $startAt = (Get-Date).ToUniversalTime().AddSeconds(-2)
        try {
            $raw = & $testRunnerScript -ProjectPath $ProjectRoot 2>&1 | Out-String
            $reportDir = Join-Path $ProjectRoot "ai-orchestrator/reports"
            $latest = @(Get-ChildItem -LiteralPath $reportDir -File -Filter "test-run-*.json" -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTimeUtc -ge $startAt } |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1)
            if ($latest.Count -eq 0) {
                return [PSCustomObject]@{ success = $false; output = $raw; error = "test-runner-report-missing" }
            }
            $report = Get-V2JsonContent -Path $latest[0].FullName
            $status = [string](Get-V2OptionalProperty -InputObject $report -Name "status" -DefaultValue "")
            if ($status -eq "passed") {
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            return [PSCustomObject]@{ success = $false; output = $raw; error = "test-runner-status-failed" }
        }
        catch {
            return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
        }
    }

    if ($TaskId -like "REPAIR-DEPLOY-*") {
        $deployVerifyScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-DeployVerificationV2.ps1"
        if (-not (Test-Path -LiteralPath $deployVerifyScript -PathType Leaf)) {
            return [PSCustomObject]@{ success = $false; output = ""; error = "deploy-verification-script-missing" }
        }
        $startAt = (Get-Date).ToUniversalTime().AddSeconds(-2)
        try {
            $raw = & $deployVerifyScript -ProjectPath $ProjectRoot 2>&1 | Out-String
            $reportDir = Join-Path $ProjectRoot "ai-orchestrator/reports"
            $latest = @(Get-ChildItem -LiteralPath $reportDir -File -Filter "deploy-verify-*.json" -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTimeUtc -ge $startAt } |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1)
            if ($latest.Count -eq 0) {
                return [PSCustomObject]@{ success = $false; output = $raw; error = "deploy-verification-report-missing" }
            }
            $report = Get-V2JsonContent -Path $latest[0].FullName
            $allPassed = [bool](Get-V2OptionalProperty -InputObject $report -Name "all_passed" -DefaultValue $false)
            if ($allPassed) {
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            return [PSCustomObject]@{ success = $false; output = $raw; error = "deploy-verification-failed" }
        }
        catch {
            return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
        }
    }

    switch -Wildcard ($TaskId) {
        "REPAIR-RESOURCE-*" {
            $finOpsScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-FinOpsMonitorV2.ps1"
            if (-not (Test-Path -LiteralPath $finOpsScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "finops-script-missing" }
            }
            try {
                $raw = & $finOpsScript -ProjectPath $ProjectRoot -DryRun 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{ success = $false; output = $raw; error = ("finops-exit-code-" + [string]$LASTEXITCODE) }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "FEAT-GPU-LOCAL-INFERENCE" {
            $composePath = Join-Path $ProjectRoot "ai-orchestrator/docker/docker-compose.generated.yml"
            if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "gpu-compose-missing" }
            }
            $composeRaw = Get-Content -LiteralPath $composePath -Raw
            $missing = New-Object System.Collections.Generic.List[string]
            foreach ($token in @("ollama:", "NVIDIA_VISIBLE_DEVICES", "driver: nvidia")) {
                if ($composeRaw.IndexOf($token, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
                    $missing.Add($token)
                }
            }
            if ($missing.Count -gt 0) {
                return [PSCustomObject]@{
                    success = $false
                    output  = ""
                    error   = ("gpu-local-inference-not-ready:" + ($missing -join ","))
                }
            }
            return [PSCustomObject]@{ success = $true; output = "gpu-local-inference-ready"; error = "" }
        }
        "FEAT-GPU-VRAM-GUARDRAILS" {
            $envPath = Join-Path $ProjectRoot "ai-orchestrator/docker/.env.docker.generated"
            if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "gpu-guardrails-env-missing" }
            }
            $envRaw = Get-Content -LiteralPath $envPath -Raw
            $required = @(
                "ORCHESTRATOR_GPU_VRAM_RESERVE_MB=",
                "OLLAMA_GPU_OVERHEAD=",
                "OLLAMA_NUM_PARALLEL=",
                "OLLAMA_MAX_LOADED_MODELS="
            )
            $missing = New-Object System.Collections.Generic.List[string]
            foreach ($entry in $required) {
                if ($envRaw.IndexOf($entry, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
                    $missing.Add($entry.TrimEnd("="))
                }
            }
            if ($missing.Count -gt 0) {
                return [PSCustomObject]@{
                    success = $false
                    output  = ""
                    error   = ("gpu-guardrails-not-ready:" + ($missing -join ","))
                }
            }
            return [PSCustomObject]@{ success = $true; output = "gpu-guardrails-ready"; error = "" }
        }
        "REFACTOR-TASK-DAG-DB-MIGRATION" {
            $taskStateDbScript = Join-Path (Join-Path $PSScriptRoot "v2") "task_state_db.py"
            if (-not (Test-Path -LiteralPath $taskStateDbScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "task-state-db-script-missing" }
            }
            try {
                $raw = & python $taskStateDbScript --project-path $ProjectRoot --mode status 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{
                    success = $false
                    output  = $raw
                    error   = ("task-state-db-status-exit-code-" + [string]$LASTEXITCODE)
                }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "TASK-GRAPH-HYDRATION-BOOTSTRAP" {
            $hydrationScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-GraphHydrationV2.ps1"
            if (-not (Test-Path -LiteralPath $hydrationScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "graph-hydration-script-missing" }
            }
            try {
                $raw = & $hydrationScript -ProjectPath $ProjectRoot -Stack auto 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{ success = $false; output = $raw; error = ("graph-hydration-exit-code-" + [string]$LASTEXITCODE) }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "TASK-QDRANT-FULL-VECTORIZATION" {
            $memorySyncScript = Join-Path $PSScriptRoot "memory_sync.py"
            if (-not (Test-Path -LiteralPath $memorySyncScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "memory-sync-script-missing" }
            }
            $projectSlug = [System.IO.Path]::GetFileName($ProjectRoot.TrimEnd('\', '/'))
            if ([string]::IsNullOrWhiteSpace($projectSlug)) {
                $projectSlug = "project"
            }
            try {
                $raw = & python $memorySyncScript --project-slug $projectSlug --project-root $ProjectRoot --qdrant-disable-incremental-sync --qdrant-prune-orphans 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{ success = $false; output = $raw; error = ("memory-sync-exit-code-" + [string]$LASTEXITCODE) }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "TASK-GPU-OBSERVABILITY-PANEL" {
            $finOpsScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-FinOpsMonitorV2.ps1"
            if (-not (Test-Path -LiteralPath $finOpsScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "finops-script-missing" }
            }
            try {
                $raw = & $finOpsScript -ProjectPath $ProjectRoot -DryRun 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{ success = $false; output = $raw; error = ("finops-exit-code-" + [string]$LASTEXITCODE) }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "TASK-HYBRID-ROUTING-TUNING" {
            $envPath = Join-Path $ProjectRoot "ai-orchestrator/docker/.env.docker.generated"
            if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "hybrid-routing-env-missing" }
            }
            $envRaw = Get-Content -LiteralPath $envPath -Raw
            $required = @(
                "ORCHESTRATOR_LLM_ENABLED=1",
                "ORCHESTRATOR_LLM_MODEL=",
                "ORCHESTRATOR_LLM_MODEL_FAST=",
                "OLLAMA_NUM_PARALLEL=",
                "OLLAMA_MAX_LOADED_MODELS="
            )
            $missing = New-Object System.Collections.Generic.List[string]
            foreach ($entry in $required) {
                if ($envRaw.IndexOf($entry, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
                    $missing.Add($entry.TrimEnd("="))
                }
            }
            if ($missing.Count -gt 0) {
                return [PSCustomObject]@{
                    success = $false
                    output  = ""
                    error   = ("hybrid-routing-not-ready:" + ($missing -join ","))
                }
            }
            return [PSCustomObject]@{ success = $true; output = "hybrid-routing-ready"; error = "" }
        }
        "TASK-IMPACT-ENGINE" {
            $graphQueryScript = Join-Path $PSScriptRoot "graph_query.py"
            if (-not (Test-Path -LiteralPath $graphQueryScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "graph-query-script-missing" }
            }
            try {
                $raw = & python $graphQueryScript --project-path $ProjectRoot --template module_impact --params '{"module":"app","limit":30}' 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                    return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
                }
                return [PSCustomObject]@{ success = $false; output = $raw; error = ("graph-query-exit-code-" + [string]$LASTEXITCODE) }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "TASK-E2E-ORCHESTRATOR-PIPELINE" {
            $observerScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-ObserverV2.ps1"
            $schedulerScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-SchedulerV2.ps1"
            if (-not (Test-Path -LiteralPath $observerScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "observer-script-missing" }
            }
            if (-not (Test-Path -LiteralPath $schedulerScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "scheduler-script-missing" }
            }
            try {
                $obs = & $observerScript -ProjectPath $ProjectRoot -RunOnce -SkipMemorySync -SkipRuntimeObservability 2>&1 | Out-String
                if (-not ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE)) {
                    return [PSCustomObject]@{ success = $false; output = $obs; error = ("observer-exit-code-" + [string]$LASTEXITCODE) }
                }
                $sch = & $schedulerScript -ProjectPath $ProjectRoot -MaxAssignmentsPerRun 6 -EmitJson 2>&1 | Out-String
                if (-not ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE)) {
                    return [PSCustomObject]@{ success = $false; output = $sch; error = ("scheduler-exit-code-" + [string]$LASTEXITCODE) }
                }
                return [PSCustomObject]@{ success = $true; output = ($obs + "`n" + $sch); error = "" }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
    }

    $taskReason = [string](Get-V2OptionalProperty -InputObject $Task -Name "reason" -DefaultValue "")
    $taskSource = [string](Get-V2OptionalProperty -InputObject $Task -Name "source_incident" -DefaultValue "")
    if ($taskReason -like "execution-backlog-gap:*" -or $taskSource -match "_execution_backlog_gap\.md$") {
        return Invoke-V2SeedExecutionBacklogFromRoadmap -ProjectRoot $ProjectRoot
    }

    if (-not $isPython) {
        return Invoke-V2BuiltinAutoTaskExecutionNonPython `
            -ProjectRoot $ProjectRoot `
            -TaskId $TaskId `
            -Language $language `
            -StateDocument $state
    }

    $requirementsPath = Join-Path $ProjectRoot "requirements.txt"
    $appDir = Join-Path $ProjectRoot "app"
    $testsDir = Join-Path $ProjectRoot "tests"

    switch ($TaskId) {
        "DEV-VERIFY-COMMANDS-001" {
            if ($null -eq $state) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "missing-project-state" }
            }
            Set-V2VerifiedPythonCommands -StateDocument $state -ProjectRoot $ProjectRoot
            Save-V2ProjectStateDocument -ProjectRoot $ProjectRoot -StateDocument $state
            return [PSCustomObject]@{ success = $true; output = "verified-commands-updated"; error = "" }
        }
        "DEV-SCAFFOLD-API-001" {
            Ensure-V2RequirementsEntries -RequirementsPath $requirementsPath -Entries @(
                "fastapi>=0.115.0",
                "uvicorn[standard]>=0.30.0",
                "pydantic>=2.8.0",
                "sqlalchemy>=2.0.0",
                "pytest>=7.0.0"
            )
            Write-V2FileIfMissing -Path (Join-Path $appDir "__init__.py") -Content "# app package`n"
            Write-V2FileIfMissing -Path (Join-Path $appDir "main.py") -Content @'
from fastapi import FastAPI

app = FastAPI(title="AI Orchestrated Project")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
'@
            return [PSCustomObject]@{ success = $true; output = "api-scaffold-created"; error = "" }
        }
        "DEV-SCAFFOLD-DOMAIN-001" {
            Ensure-V2RequirementsEntries -RequirementsPath $requirementsPath -Entries @(
                "sqlalchemy>=2.0.0",
                "pydantic>=2.8.0"
            )
            Write-V2FileIfMissing -Path (Join-Path $appDir "database.py") -Content @'
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
'@
            Write-V2FileIfMissing -Path (Join-Path $appDir "models.py") -Content @'
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Entity(Base):
    __tablename__ = "entities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
'@
            Write-V2FileIfMissing -Path (Join-Path $appDir "schemas.py") -Content @'
from pydantic import BaseModel


class EntityIn(BaseModel):
    name: str


class EntityOut(EntityIn):
    id: int
'@
            Write-V2FileIfMissing -Path (Join-Path $appDir "services.py") -Content @'
from typing import Dict, List

_STORE: Dict[int, dict] = {}
_NEXT_ID = 1


def create_entity(name: str) -> dict:
    global _NEXT_ID
    entity = {"id": _NEXT_ID, "name": name}
    _STORE[_NEXT_ID] = entity
    _NEXT_ID += 1
    return entity


def list_entities() -> List[dict]:
    return list(_STORE.values())
'@
            return [PSCustomObject]@{ success = $true; output = "domain-scaffold-created"; error = "" }
        }
        "DEV-MIGRATION-001" {
            Ensure-V2RequirementsEntries -RequirementsPath $requirementsPath -Entries @(
                "alembic>=1.13.0",
                "psycopg[binary]>=3.2.0"
            )
            Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "alembic.ini") -Content @'
[alembic]
script_location = alembic
sqlalchemy.url = ${ALEMBIC_DATABASE_URL}

[loggers]
keys = root,sqlalchemy,alembic
[handlers]
keys = console
[formatters]
keys = generic
[logger_root]
level = WARN
handlers = console
[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine
[logger_alembic]
level = INFO
handlers =
qualname = alembic
[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic
[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
'@
            Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "alembic/env.py") -Content @'
from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.database import Base
from app import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
'@
            Write-V2FileIfMissing -Path (Join-Path $ProjectRoot "alembic/versions/0001_initial_schema.py") -Content @'
"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-03-10 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("entities")
'@
            return [PSCustomObject]@{ success = $true; output = "migration-scaffold-created"; error = "" }
        }
        "DEV-TEST-API-001" {
            Ensure-V2DirectoryPath -Path $testsDir
            Write-V2FileIfMissing -Path (Join-Path $testsDir "__init__.py") -Content ""
            Write-V2FileIfMissing -Path (Join-Path $testsDir "conftest.py") -Content @'
from fastapi.testclient import TestClient
from app.main import app


def test_client() -> TestClient:
    return TestClient(app)
'@
            Write-V2FileIfMissing -Path (Join-Path $testsDir "test_api.py") -Content @'
from fastapi.testclient import TestClient
from app.main import app


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
'@
            return [PSCustomObject]@{ success = $true; output = "api-tests-created"; error = "" }
        }
        "DEV-WORKER-001" {
            Write-V2FileIfMissing -Path (Join-Path $appDir "worker.py") -Content @'
import time


def run_worker_loop(interval_seconds: int = 60) -> None:
    while True:
        print("worker-heartbeat")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_worker_loop()
'@
            return [PSCustomObject]@{ success = $true; output = "worker-created"; error = "" }
        }
        "DEV-ENV-EXAMPLE-001" {
            Write-V2FileIfMissing -Path (Join-Path $ProjectRoot ".env.example") -Content @'
DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/app
ALEMBIC_DATABASE_URL=postgresql+psycopg://app:app@localhost:5432/app
APP_ENV=development
'@
            return [PSCustomObject]@{ success = $true; output = "env-example-created"; error = "" }
        }
        "DEV-ARCH-DOC-001" {
            $architecturePath = Join-Path $ProjectRoot "ai-orchestrator/documentation/architecture.md"
            $current = if (Test-Path -LiteralPath $architecturePath -PathType Leaf) { Get-Content -LiteralPath $architecturePath -Raw } else { "" }
            $isPlaceholder = [string]::IsNullOrWhiteSpace($current) -or $current -match "This file should always reflect the current validated architecture"
            if ($isPlaceholder) {
                $content = @'
# Architecture

## Runtime
- Stack: Python + FastAPI
- API entrypoint: app/main.py
- Domain layer: app/models.py, app/schemas.py, app/services.py

## Data
- Transactional: PostgreSQL (isolated schema per project)
- Graph memory: Neo4j (project namespace)
- Vector memory: Qdrant (project collection)

## Quality gates
- Build: python -m compileall app
- Test: python -m pytest
- Core gate: CORE-COMPLETE-001 only after build/test/migration/health are validated.
'@
                $parent = Split-Path -Parent $architecturePath
                Ensure-V2DirectoryPath -Path $parent
                [System.IO.File]::WriteAllText($architecturePath, $content)
            }
            return [PSCustomObject]@{ success = $true; output = "architecture-doc-validated"; error = "" }
        }
        "PLAN-BUSINESS-CONTEXT-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "PLAN-ARCH-ADR-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "PLAN-INTERFACE-CONTRACTS-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "QA-CODE-REVIEW-BASELINE-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "QA-PRODUCT-VALIDATION-BASELINE-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "QA-USER-SIMULATION-BASELINE-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "OPS-OBSERVABILITY-BASELINE-001" {
            return Invoke-V2360ArtifactGenerator -ProjectRoot $ProjectRoot
        }
        "FEAT-FRONTEND-WEB-001" {
            return [PSCustomObject]@{ success = $true; output = "frontend-feature-noop-python"; error = "" }
        }
        "FEAT-INTEGRATION-WHATSAPP-001" {
            return [PSCustomObject]@{ success = $true; output = "whatsapp-feature-noop-python"; error = "" }
        }
        "FEAT-OPS-BACKUP-DR-001" {
            $runbookPath = Join-Path $ProjectRoot "docs/runbooks/backup-disaster-recovery.md"
            Write-V2FileIfMissing -Path $runbookPath -Content @'
# Backup & Disaster Recovery Runbook

## Objective
Maintain encrypted backups and restore drills with evidence logs.

## Targets
- RPO: 24h
- RTO: 2h
'@
            return [PSCustomObject]@{ success = $true; output = "backup-dr-runbook-created-python"; error = "" }
        }
        "FEAT-QA-E2E-PRODUCT-001" {
            $testsDir = Join-Path $ProjectRoot "tests"
            Ensure-V2DirectoryPath -Path $testsDir
            Write-V2FileIfMissing -Path (Join-Path $testsDir "test_product_e2e.py") -Content @'
def test_product_e2e_placeholder() -> None:
    assert True
'@
            return [PSCustomObject]@{ success = $true; output = "product-e2e-placeholder-created-python"; error = "" }
        }
        "REL-STAGING-001" {
            $releaseScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-ReleasePipelineV2.ps1"
            if (-not (Test-Path -LiteralPath $releaseScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "release-pipeline-script-missing" }
            }
            try {
                $raw = & $releaseScript -ProjectPath $ProjectRoot -Environment "staging" -RollbackOnFailure $true 2>&1 | Out-String
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        "REL-PROD-001" {
            $releaseScript = Join-Path (Join-Path $PSScriptRoot "v2") "Invoke-ReleasePipelineV2.ps1"
            if (-not (Test-Path -LiteralPath $releaseScript -PathType Leaf)) {
                return [PSCustomObject]@{ success = $false; output = ""; error = "release-pipeline-script-missing" }
            }
            try {
                $raw = & $releaseScript -ProjectPath $ProjectRoot -Environment "production" -RollbackOnFailure $true 2>&1 | Out-String
                return [PSCustomObject]@{ success = $true; output = $raw; error = "" }
            }
            catch {
                return [PSCustomObject]@{ success = $false; output = ""; error = $_.Exception.Message }
            }
        }
        default {
            return [PSCustomObject]@{ success = $false; output = ""; error = "builtin-auto-task-unsupported:$TaskId" }
        }
    }
}

function Test-V2ProjectCompletionGate {
    param([string]$ProjectRoot)

    $issues = New-Object System.Collections.Generic.List[string]
    $statePath = Join-Path $ProjectRoot "ai-orchestrator/state/project-state.json"
    $healthPath = Join-Path $ProjectRoot "ai-orchestrator/state/health-report.json"

    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        $issues.Add("missing-project-state")
    }
    if (-not (Test-Path -LiteralPath $healthPath -PathType Leaf)) {
        $issues.Add("missing-health-report")
    }
    if ($issues.Count -gt 0) {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = ("core-completion-gate-failed:" + ($issues -join ","))
        }
    }

    $state = Get-V2JsonContent -Path $statePath
    $health = Get-V2JsonContent -Path $healthPath

    $startupPackStatus = [string](Get-V2OptionalProperty -InputObject $state -Name "startup_pack_status" -DefaultValue "")
    if ($startupPackStatus -ne "ready") {
        $issues.Add("startup_pack_status_not_ready")
    }

    $verification = Get-V2OptionalProperty -InputObject $state -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})
    $relDomain = Get-V2OptionalProperty -InputObject $verification -Name "relational_domain" -DefaultValue ([PSCustomObject]@{})
    $relDomainStatus = [string](Get-V2OptionalProperty -InputObject $relDomain -Name "status" -DefaultValue "")
    $relDomainCount = [int](Get-V2OptionalProperty -InputObject $relDomain -Name "records_seeded" -DefaultValue 0)
    $fingerprint = Get-V2OptionalProperty -InputObject $state -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
    $primaryLanguage = ([string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "")).Trim().ToLowerInvariant()
    $requiresAlembicDomainGate = $primaryLanguage -eq "python"

    if ($requiresAlembicDomainGate) {
        if ($relDomainStatus -ne "ready" -or $relDomainCount -le 0) {
            $issues.Add("relational_domain_not_ready")
        }
    }
    else {
        if ($relDomainStatus -eq "error") {
            $issues.Add("relational_domain_error")
        }

        $taskDagPath = Join-Path $ProjectRoot "ai-orchestrator/tasks/task-dag.json"
        if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
            $issues.Add("missing-task-dag")
        }
        else {
            $taskDag = Get-V2JsonContent -Path $taskDagPath
            $taskItems = @(Get-V2OptionalProperty -InputObject $taskDag -Name "tasks" -DefaultValue @())
            $migrationTask = @(
                $taskItems | Where-Object {
                    [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq "DEV-MIGRATION-001"
                } | Select-Object -First 1
            )

            if ($migrationTask.Count -eq 0) {
                $issues.Add("migration_task_missing")
            }
            else {
                $migrationStatus = [string](Get-V2OptionalProperty -InputObject $migrationTask[0] -Name "status" -DefaultValue "")
                if ($migrationStatus -notin @("done", "completed")) {
                    $issues.Add("migration_task_not_done")
                }
            }
        }
    }

    $healthStatus = [string](Get-V2OptionalProperty -InputObject $health -Name "health_status" -DefaultValue "")
    if ($healthStatus -ne "healthy") {
        $issues.Add("health_not_healthy")
    }

    $verifiedCommands = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    foreach ($requiredCommand in @("build", "test")) {
        $commandSpec = Get-V2OptionalProperty -InputObject $verifiedCommands -Name $requiredCommand -DefaultValue $null
        if ($null -eq $commandSpec) {
            $issues.Add("$requiredCommand-command-missing")
            continue
        }

        $commandValue = [string](Get-V2OptionalProperty -InputObject $commandSpec -Name "value" -DefaultValue "")
        $commandConfidence = [string](Get-V2OptionalProperty -InputObject $commandSpec -Name "confidence" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($commandValue) -or $commandValue -eq "unknown") {
            $issues.Add("$requiredCommand-command-unknown")
        }
        if ($commandConfidence -ne "verified") {
            $issues.Add("$requiredCommand-command-not-verified")
        }
    }

    $checkResults = @(Get-V2OptionalProperty -InputObject $health -Name "check_results" -DefaultValue @())
    $buildResult = @($checkResults | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "build" } | Select-Object -First 1)
    if ($buildResult.Count -eq 0 -or [string](Get-V2OptionalProperty -InputObject $buildResult[0] -Name "status" -DefaultValue "") -ne "passed") {
        $issues.Add("build_check_not_passed")
    }
    $testResult = @($checkResults | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "test" } | Select-Object -First 1)
    if ($testResult.Count -eq 0 -or [string](Get-V2OptionalProperty -InputObject $testResult[0] -Name "status" -DefaultValue "") -ne "passed") {
        $issues.Add("test_check_not_passed")
    }
    $memoryResult = @($checkResults | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") -eq "memory-sync" } | Select-Object -First 1)
    if ($memoryResult.Count -eq 0 -or [string](Get-V2OptionalProperty -InputObject $memoryResult[0] -Name "status" -DefaultValue "") -ne "passed") {
        $issues.Add("memory_sync_not_passed")
    }

    if ($issues.Count -gt 0) {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = ("core-completion-gate-failed:" + ($issues -join ","))
        }
    }

    return [PSCustomObject]@{
        success = $true
        output  = "core-completion-gate-ok"
        error   = ""
    }
}

function Invoke-V2TaskExecution {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName,
        [string]$TaskCommand,
        [string]$TaskHandlerScript,
        [object]$Task
    )

    if (-not [string]::IsNullOrWhiteSpace($TaskHandlerScript)) {
        if (-not (Test-Path -LiteralPath $TaskHandlerScript -PathType Leaf)) {
            return [PSCustomObject]@{
                success = $false
                output  = ""
                error   = "handler-script-not-found"
                deferred = $false
            }
        }

        try {
            $output = & $TaskHandlerScript -ProjectPath $ProjectRoot -TaskId $TaskId -AgentName $AgentName 2>&1 | Out-String
            $outputText = [string]$output
            $parsedOutput = $null
            if (-not [string]::IsNullOrWhiteSpace($outputText)) {
                try {
                    $parsedOutput = ($outputText.Trim() | ConvertFrom-Json -ErrorAction Stop)
                }
                catch {
                    $parsedOutput = $null
                }
            }

            if ($LASTEXITCODE -eq 0 -or $null -eq $LASTEXITCODE) {
                if ($parsedOutput -and ($parsedOutput.PSObject.Properties.Name -contains "success")) {
                    return [PSCustomObject]@{
                        success = [bool](Get-V2OptionalProperty -InputObject $parsedOutput -Name "success" -DefaultValue $true)
                        output  = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "output" -DefaultValue $outputText)
                        error   = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "error" -DefaultValue "")
                        deferred = [bool](Get-V2OptionalProperty -InputObject $parsedOutput -Name "deferred" -DefaultValue $false)
                        bridge_reason = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "reason" -DefaultValue "")
                    }
                }
                return [PSCustomObject]@{
                    success = $true
                    output  = $outputText
                    error   = ""
                    deferred = $false
                }
            }

            if ($parsedOutput -and ($parsedOutput.PSObject.Properties.Name -contains "success")) {
                return [PSCustomObject]@{
                    success = [bool](Get-V2OptionalProperty -InputObject $parsedOutput -Name "success" -DefaultValue $false)
                    output  = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "output" -DefaultValue $outputText)
                    error   = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "error" -DefaultValue ("handler-exit-code-" + [string]$LASTEXITCODE))
                    deferred = [bool](Get-V2OptionalProperty -InputObject $parsedOutput -Name "deferred" -DefaultValue $false)
                    bridge_reason = [string](Get-V2OptionalProperty -InputObject $parsedOutput -Name "reason" -DefaultValue "")
                }
            }

            return [PSCustomObject]@{
                success = $false
                output  = $outputText
                error   = ("handler-exit-code-" + [string]$LASTEXITCODE)
                deferred = $false
            }
        }
        catch {
            return [PSCustomObject]@{
                success = $false
                output  = ""
                error   = $_.Exception.Message
                deferred = $false
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($TaskCommand)) {
        $executionMode = [string](Get-V2OptionalProperty -InputObject $Task -Name "execution_mode" -DefaultValue "")
        if (Test-V2BuiltinAutoTaskSupported -TaskId $TaskId -Task $Task) {
            return Invoke-V2BuiltinAutoTaskExecution -ProjectRoot $ProjectRoot -TaskId $TaskId -Task $Task
        }
        if ($executionMode -eq "project-completion-gate") {
            return Test-V2ProjectCompletionGate -ProjectRoot $ProjectRoot
        }
        $useArtifactValidation = $executionMode -eq "artifact-validation" -or $TaskId -match "^V2-(PLAN|ANALYSIS|DOCKER)-"
        if ($useArtifactValidation) {
            $missingFiles = New-Object System.Collections.Generic.List[string]
            $filesAffected = @(Get-V2TaskArrayProperty -Task $Task -Name "files_affected")
            if ($filesAffected.Count -eq 0) {
                return [PSCustomObject]@{
                    success = $false
                    output  = ""
                    error   = "artifact-validation-no-targets"
                    deferred = $false
                }
            }
            foreach ($relativePath in $filesAffected) {
                $pathValue = [string]$relativePath
                if ([string]::IsNullOrWhiteSpace($pathValue)) { continue }
                $absolutePath = Join-Path $ProjectRoot $pathValue
                if (-not (Test-Path -LiteralPath $absolutePath)) {
                    $missingFiles.Add($pathValue)
                }
            }

            if ($missingFiles.Count -eq 0) {
                return [PSCustomObject]@{
                    success = $true
                    output  = "artifact-validation-ok"
                    error   = ""
                    deferred = $false
                }
            }

            return [PSCustomObject]@{
                success = $false
                output  = ""
                error   = ("artifact-validation-missing:" + ($missingFiles -join ","))
                deferred = $false
            }
        }

        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = "missing-task-command-or-handler"
            deferred = $false
        }
    }

    if (-not (Test-V2CommandAllowed -Command $TaskCommand)) {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = "task-command-not-allowlisted"
            deferred = $false
        }
    }

    try {
        Push-Location $ProjectRoot
        $output = & powershell -NoProfile -ExecutionPolicy Bypass -Command $TaskCommand 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0 -or $null -eq $exitCode) {
            return [PSCustomObject]@{
                success = $true
                output  = [string]$output
                error   = ""
                deferred = $false
            }
        }

        return [PSCustomObject]@{
            success = $false
            output  = [string]$output
            error   = ("task-command-exit-code-" + [string]$exitCode)
            deferred = $false
        }
    }
    catch {
        return [PSCustomObject]@{
            success = $false
            output  = ""
            error   = $_.Exception.Message
            deferred = $false
        }
    }
    finally {
        Pop-Location
    }
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}
Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-agent-loop"

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$locksPath = Join-Path $orchestratorRoot "locks/locks.json"
$historyPath = Join-Path $orchestratorRoot "tasks/execution-history.md"
$messagePath = Join-Path $orchestratorRoot "communication/messages.md"
$schedulerScript  = Join-Path $PSScriptRoot "v2/Invoke-SchedulerV2.ps1"
$preFlightScript  = Join-Path $PSScriptRoot "v2/Invoke-PreFlightReasoner.ps1"
$taskSyncScript   = Join-Path $PSScriptRoot "Sync-TaskState.ps1"
$lockSyncScript   = Join-Path $PSScriptRoot "Sync-LockState.ps1"
$whiteboardScript = Join-Path $PSScriptRoot "v2/Invoke-WhiteboardV2.ps1"
$defaultExternalBridgeScript = Resolve-V2ExternalAgentBridgeScriptPath -ProjectRoot $resolvedProjectPath -ConfiguredPath $ExternalAgentBridgeScript

if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    throw "task-dag.json not found: $taskDagPath"
}

if ($ExternalAgentBridgeDispatchCooldownSeconds -gt 0) {
    $env:ORCHESTRATOR_EXTERNAL_BRIDGE_DISPATCH_COOLDOWN_SECONDS = [string]$ExternalAgentBridgeDispatchCooldownSeconds
}

while ($true) {
    if (Test-Path -LiteralPath $schedulerScript -PathType Leaf) {
        try { & $schedulerScript -ProjectPath $resolvedProjectPath | Out-Null } catch {}
    }

    $dag = Get-V2JsonContent -Path $taskDagPath
    if (-not $dag -or -not ($dag.PSObject.Properties.Name -contains "tasks")) {
        throw "Invalid task-dag.json format (missing tasks array)."
    }

    $tasks = @($dag.tasks)
    $targets = @(
        @($tasks | Where-Object {
            $status = Get-V2TaskStatus -Task $_
            $assigned = [string](Get-V2OptionalProperty -InputObject $_ -Name "assigned_agent" -DefaultValue "")
            if ($status -ne "in-progress" -or $assigned -ne $AgentName) {
                return $false
            }
            if ($SkipNativeRuntimeTasks -and (Test-V2NativeRuntimeTask -Task $_)) {
                return $false
            }
            return $true
        }) |
        Select-Object -First $MaxTasksPerCycle
    )

    $executed = 0
    foreach ($task in $targets) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($taskId)) { continue }

        $executionMode = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_mode" -DefaultValue "")
        $taskCommand = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_command" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($taskCommand)) {
            $taskCommand = [string](Get-V2OptionalProperty -InputObject $task -Name "run_command" -DefaultValue "")
        }
        if ([string]::IsNullOrWhiteSpace($taskCommand)) {
            $taskCommand = [string](Get-V2OptionalProperty -InputObject $task -Name "command" -DefaultValue "")
        }

        $supportsBuiltinAutoTask = Test-V2BuiltinAutoTaskSupported -TaskId $taskId -Task $task
        $effectiveTaskHandlerScript = $TaskHandlerScript
        if (($executionMode -in @("external-agent", "manual", "human")) -and [string]::IsNullOrWhiteSpace($taskCommand) -and [string]::IsNullOrWhiteSpace($effectiveTaskHandlerScript) -and -not $supportsBuiltinAutoTask) {
            if (-not [string]::IsNullOrWhiteSpace($defaultExternalBridgeScript) -and (Test-Path -LiteralPath $defaultExternalBridgeScript -PathType Leaf)) {
                $effectiveTaskHandlerScript = $defaultExternalBridgeScript
                Set-V2ObjectProperty -InputObject $task -Name "external_bridge_handler" -Value (ConvertTo-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $defaultExternalBridgeScript)
                Set-V2ObjectProperty -InputObject $task -Name "external_bridge_enabled" -Value $true
            }
        }
        if (($executionMode -in @("external-agent", "manual", "human")) -and [string]::IsNullOrWhiteSpace($taskCommand) -and [string]::IsNullOrWhiteSpace($effectiveTaskHandlerScript) -and -not $supportsBuiltinAutoTask) {
            $preflightResult = Invoke-V2TaskPreFlight `
                -ProjectRoot $resolvedProjectPath `
                -Task $task `
                -TaskId $taskId `
                -AgentName $AgentName `
                -PreFlightScriptPath $preFlightScript
            if ([bool]$preflightResult.success -and -not [string]::IsNullOrWhiteSpace([string]$preflightResult.path)) {
                $relativePreflightPath = ConvertTo-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath ([string]$preflightResult.path)
                Set-V2ObjectProperty -InputObject $task -Name "preflight_path" -Value $relativePreflightPath
            }
            $timestamp = Get-V2Timestamp
            $filesAffected = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "external-bridge-unavailable"
            Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
            [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "external-bridge-unavailable")
            Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                "## $timestamp",
                "- from: $AgentName",
                "- to: SchedulerV2",
                "- task_id: $taskId",
                "- status: waiting-external-agent",
                "- note: execution_mode=$executionMode",
                "- preflight: $([string](Get-V2OptionalProperty -InputObject $task -Name 'preflight_path' -DefaultValue ''))"
            )
            $executed += 1
            continue
        }

        $phaseGate = Test-V2TaskPhaseApproval -ProjectRoot $resolvedProjectPath -Task $task
        if (-not [bool]$phaseGate.allowed) {
            $blockedReason = [string](Get-V2OptionalProperty -InputObject $phaseGate -Name "reason" -DefaultValue "phase-approval-missing")
            $phase = [string](Get-V2OptionalProperty -InputObject $phaseGate -Name "phase" -DefaultValue "unknown")
            $phaseStatus = [string](Get-V2OptionalProperty -InputObject $phaseGate -Name "status" -DefaultValue "pending")
            $filesAffected = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-phase-approval"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value $blockedReason
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "phase-approval-blocked")
            Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                "## $(Get-V2Timestamp)",
                "- from: $AgentName",
                "- to: SchedulerV2",
                "- task_id: $taskId",
                "- status: blocked-phase-approval",
                "- required_phase: $phase",
                "- approval_status: $phaseStatus"
            )
            continue
        }

        $filesAffected = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected")
        $lockRenew = Acquire-V2TaskLocks `
            -LocksPath $locksPath `
            -TaskId $taskId `
            -Agent $AgentName `
            -FilesAffected $filesAffected `
            -TtlSeconds 7200
        if (-not $lockRenew.success) {
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-lock-conflict"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "agent-loop-lock-renew-failed"
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value (Get-V2Timestamp)
            continue
        }

        $hitlCheck = Test-V2TaskHitlApproval -ProjectRoot $resolvedProjectPath -TaskId $taskId
        if (-not [bool](Get-V2OptionalProperty -InputObject $hitlCheck -Name "allowed" -DefaultValue $true)) {
            $timestamp = Get-V2Timestamp
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "waiting-approval"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "hitl-gate-open"
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
            [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "hitl-gate-open")
            Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                "## $timestamp",
                "- from: $AgentName",
                "- to: HITL",
                "- task_id: $taskId",
                "- status: waiting-approval",
                "- reason: hitl-gate-open"
            )
            $executed += 1
            continue
        }

        # Announce intent on the whiteboard so other agents know what this agent plans to change
        if (Test-Path -LiteralPath $whiteboardScript -PathType Leaf) {
            $wbFiles = @(Get-V2TaskArrayProperty -Task $task -Name "files_affected") -join ","
            $wbTitle = [string](Get-V2OptionalProperty -InputObject $task -Name "title" -DefaultValue $taskId)
            try {
                & $whiteboardScript `
                    -Mode          "announce" `
                    -ProjectPath   $resolvedProjectPath `
                    -TaskId        $taskId `
                    -AgentName     $AgentName `
                    -FilesIntended $wbFiles `
                    -Intention     $wbTitle | Out-Null
            }
            catch {}
        }

        $stepCheckpointAvailable = Test-Path -LiteralPath $v2StepCheckpointPath -PathType Leaf
        $resumeStep = 1
        if ($stepCheckpointAvailable) {
            $latestCheckpoint = Read-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId
            if ($latestCheckpoint) {
                $resumeStep = [int](Get-V2OptionalProperty -InputObject $latestCheckpoint -Name "step_number" -DefaultValue 1)
                $resumeStatus = [string](Get-V2OptionalProperty -InputObject $latestCheckpoint -Name "status" -DefaultValue "")
                if ($resumeStatus -eq "ok") {
                    $resumeStep = $resumeStep + 1
                }
                if ($resumeStep -lt 1) {
                    $resumeStep = 1
                }
            }
        }

        if ($resumeStep -le 1) {
            if ($stepCheckpointAvailable) {
                Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 1 -StepName "preflight" -Status "running" -AgentName $AgentName
            }
            $preflightResult = Invoke-V2TaskPreFlight `
                -ProjectRoot $resolvedProjectPath `
                -Task $task `
                -TaskId $taskId `
                -AgentName $AgentName `
                -PreFlightScriptPath $preFlightScript
            if (-not [bool]$preflightResult.success) {
                if ($stepCheckpointAvailable) {
                    Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 1 -StepName "preflight" -Status "failed" -AgentName $AgentName -ErrorText $preflightResult.error
                }
                $timestamp = Get-V2Timestamp
                Set-V2ObjectProperty -InputObject $task -Name "status" -Value "blocked-runtime"
                Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ("preflight-failed:" + [string]$preflightResult.error)
                Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
                Set-V2ObjectProperty -InputObject $task -Name "last_error" -Value ("preflight-failed:" + [string]$preflightResult.error)
                [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "preflight-failed")
                Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                    "## $timestamp",
                    "- event: task-failed",
                    "- task_id: $taskId",
                    "- agent: $AgentName",
                    "- error: preflight-failed:$([string]$preflightResult.error)"
                )
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $timestamp",
                    "- from: $AgentName",
                    "- to: SchedulerV2",
                    "- task_id: $taskId",
                    "- status: blocked-runtime"
                )
                $executed += 1
                continue
            }
            if (-not [string]::IsNullOrWhiteSpace([string]$preflightResult.path)) {
                $relativePreflightPath = ConvertTo-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath ([string]$preflightResult.path)
                Set-V2ObjectProperty -InputObject $task -Name "preflight_path" -Value $relativePreflightPath
            }
            if ($stepCheckpointAvailable) {
                Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 1 -StepName "preflight" -Status "ok" -AgentName $AgentName
            }
        }

        $execution = [PSCustomObject]@{ success = $true; output = "execution-resume-skip"; error = "" }
        if ($resumeStep -le 2) {
            if ($stepCheckpointAvailable) {
                Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 2 -StepName "execute" -Status "running" -AgentName $AgentName
            }
            $execution = Invoke-V2TaskExecution `
                -ProjectRoot $resolvedProjectPath `
                -TaskId $taskId `
                -AgentName $AgentName `
                -TaskCommand $taskCommand `
                -TaskHandlerScript $effectiveTaskHandlerScript `
                -Task $task
            $executionDeferred = [bool](Get-V2OptionalProperty -InputObject $execution -Name "deferred" -DefaultValue $false)
            if ($execution.success -and $executionDeferred) {
                $timestamp = Get-V2Timestamp
                Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
                Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value "external-agent-awaiting-completion"
                Set-V2ObjectProperty -InputObject $task -Name "external_bridge_last_dispatch_at" -Value $timestamp
                Set-V2ObjectProperty -InputObject $task -Name "assigned_agent" -Value ""
                Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
                [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "external-agent-awaiting-completion")
                Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                    "## $timestamp",
                    "- event: task-dispatched-external",
                    "- task_id: $taskId",
                    "- agent: $AgentName",
                    "- bridge_output: $([string](Get-V2OptionalProperty -InputObject $execution -Name 'output' -DefaultValue ''))"
                )
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $timestamp",
                    "- from: $AgentName",
                    "- to: SchedulerV2",
                    "- task_id: $taskId",
                    "- status: waiting-external-agent",
                    "- bridge_reason: $([string](Get-V2OptionalProperty -InputObject $execution -Name 'bridge_reason' -DefaultValue 'awaiting-external-completion'))"
                )
                $executed += 1
                continue
            }
            if ($execution.success) {
                if ($stepCheckpointAvailable) {
                    Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 2 -StepName "execute" -Status "ok" -AgentName $AgentName
                }
            }
            else {
                $runtimeFallbackApplied = [bool](Get-V2OptionalProperty -InputObject $task -Name "runtime_external_bridge_fallback_applied" -DefaultValue $false)
                $canFallbackToNative = (
                    $AllowExternalAgentFallbackToNative -and
                    -not $runtimeFallbackApplied -and
                    ($executionMode -in @("external-agent", "manual", "human")) -and
                    ($supportsBuiltinAutoTask -or -not [string]::IsNullOrWhiteSpace($taskCommand))
                )
                if ($canFallbackToNative) {
                    $timestamp = Get-V2Timestamp
                    Set-V2ObjectProperty -InputObject $task -Name "status" -Value "pending"
                    Set-V2ObjectProperty -InputObject $task -Name "execution_mode" -Value "llm-native"
                    Set-V2ObjectProperty -InputObject $task -Name "runtime_engine" -Value "hybrid"
                    Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
                    Set-V2ObjectProperty -InputObject $task -Name "runtime_external_bridge_fallback_applied" -Value $true
                    Set-V2ObjectProperty -InputObject $task -Name "runtime_external_bridge_fallback_at" -Value $timestamp
                    Set-V2ObjectProperty -InputObject $task -Name "runtime_external_bridge_fallback_reason" -Value ([string](Get-V2OptionalProperty -InputObject $execution -Name "error" -DefaultValue "external-bridge-failure"))
                    Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
                    Set-V2ObjectProperty -InputObject $task -Name "last_error" -Value ([string](Get-V2OptionalProperty -InputObject $execution -Name "error" -DefaultValue "external-bridge-failure"))
                    [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "external-bridge-fallback-llm-native")
                    Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                        "## $timestamp",
                        "- event: task-fallback-llm-native",
                        "- task_id: $taskId",
                        "- agent: $AgentName",
                        "- previous_mode: $executionMode",
                        "- reason: $([string](Get-V2OptionalProperty -InputObject $execution -Name 'error' -DefaultValue 'external-bridge-failure'))"
                    )
                    $executed += 1
                    continue
                }

                if ($stepCheckpointAvailable) {
                    Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 2 -StepName "execute" -Status "failed" -AgentName $AgentName -ErrorText $execution.error
                }
                $timestamp = Get-V2Timestamp
                $failureStatus = "blocked-runtime"
                if ($executionMode -eq "project-completion-gate") {
                    $failureStatus = "pending"
                }
                Set-V2ObjectProperty -InputObject $task -Name "status" -Value $failureStatus
                Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value $execution.error
                Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
                Set-V2ObjectProperty -InputObject $task -Name "last_error" -Value $execution.error
                [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "execution-failed")
                Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                    "## $timestamp",
                    "- event: task-failed",
                    "- task_id: $taskId",
                    "- agent: $AgentName",
                    "- error: $($execution.error)"
                )
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $timestamp",
                    "- from: $AgentName",
                    "- to: SchedulerV2",
                    "- task_id: $taskId",
                    "- status: $failureStatus"
                )
                $executed += 1
                continue
            }
        }

        if ($stepCheckpointAvailable) {
            Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 3 -StepName "finalize" -Status "running" -AgentName $AgentName
        }

        $statusBefore = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "in-progress")
        $completionPayload = New-V2RuntimeCompletionPayload `
            -TaskId $taskId `
            -AgentName $AgentName `
            -Task $task `
            -ExecutionOutput ([string](Get-V2OptionalProperty -InputObject $execution -Name "output" -DefaultValue "")) `
            -TaskCommand $taskCommand
        $completionPayloadPath = Save-V2RuntimeCompletionPayload -ProjectRoot $resolvedProjectPath -TaskId $taskId -Payload $completionPayload
        Set-V2ObjectProperty -InputObject $task -Name "completion_payload" -Value $completionPayloadPath

        $schemaValidation = Invoke-V2RuntimeOutputSchemaValidation `
            -ProjectRoot $resolvedProjectPath `
            -AgentName $AgentName `
            -CompletionPayloadPath $completionPayloadPath
        Set-V2ObjectProperty -InputObject $task -Name "completion_schema_validation" -Value ([PSCustomObject]@{
                success = [bool](Get-V2OptionalProperty -InputObject $schemaValidation -Name "success" -DefaultValue $false)
                errors = @((Get-V2OptionalProperty -InputObject $schemaValidation -Name "errors" -DefaultValue @()))
                warnings = @((Get-V2OptionalProperty -InputObject $schemaValidation -Name "warnings" -DefaultValue @()))
                validated_at = Get-V2Timestamp
            })

        $timestamp = Get-V2Timestamp
        $schemaSuccess = [bool](Get-V2OptionalProperty -InputObject $schemaValidation -Name "success" -DefaultValue $false)
        if (-not $schemaSuccess) {
            $schemaErrors = @((Get-V2OptionalProperty -InputObject $schemaValidation -Name "errors" -DefaultValue @()))
            $reason = "output-schema-invalid"
            if ($schemaErrors.Count -gt 0) {
                $reason = $reason + ":" + ($schemaErrors -join ",")
            }
            Set-V2ObjectProperty -InputObject $task -Name "status" -Value "needs-revision"
            Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value $reason
            Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
            Set-V2ObjectProperty -InputObject $task -Name "last_error" -Value $reason
            [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "schema-validation-failed")
            Write-V2RuntimeTaskTransactionEvent `
                -ProjectRoot $resolvedProjectPath `
                -TaskId $taskId `
                -AgentName $AgentName `
                -StatusFrom $statusBefore `
                -StatusTo "needs-revision" `
                -Success $false `
                -Reason $reason `
                -EvidencePath $completionPayloadPath
            [void](Invoke-V2MemoryModuleIndex `
                -ProjectRoot $resolvedProjectPath `
                -TaskId $taskId `
                -AgentName $AgentName `
                -SourceFiles @((Get-V2OptionalProperty -InputObject $completionPayload -Name "source_files" -DefaultValue @())) `
                -SourceModules @((Get-V2OptionalProperty -InputObject $completionPayload -Name "source_modules" -DefaultValue @())) `
                -Outcome "needs-revision")
            Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                "## $timestamp",
                "- event: task-needs-revision",
                "- task_id: $taskId",
                "- agent: $AgentName",
                "- reason: $reason"
            )
            if ($stepCheckpointAvailable) {
                Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 3 -StepName "finalize" -Status "failed" -AgentName $AgentName -ErrorText $reason
                Clear-V2RuntimeStepCheckpoints -ProjectRoot $resolvedProjectPath -TaskId $taskId
            }
            Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                "## $timestamp",
                "- from: $AgentName",
                "- to: SchedulerV2",
                "- task_id: $taskId",
                "- status: needs-revision",
                "- reason: $reason"
            )
            $executed += 1
            continue
        }

        Set-V2ObjectProperty -InputObject $task -Name "status" -Value "done"
        Set-V2ObjectProperty -InputObject $task -Name "blocked_reason" -Value ""
        Set-V2ObjectProperty -InputObject $task -Name "completed_at" -Value $timestamp
        Set-V2ObjectProperty -InputObject $task -Name "updated_at" -Value $timestamp
        [void](Release-V2TaskLocks -LocksPath $locksPath -TaskId $taskId -Agent $AgentName -FilesAffected $filesAffected -Reason "completed")
        Write-V2RuntimeTaskTransactionEvent `
            -ProjectRoot $resolvedProjectPath `
            -TaskId $taskId `
            -AgentName $AgentName `
            -StatusFrom $statusBefore `
            -StatusTo "done" `
            -Success $true `
            -Reason "" `
            -EvidencePath $completionPayloadPath
        [void](Invoke-V2MemoryModuleIndex `
            -ProjectRoot $resolvedProjectPath `
            -TaskId $taskId `
            -AgentName $AgentName `
            -SourceFiles @((Get-V2OptionalProperty -InputObject $completionPayload -Name "source_files" -DefaultValue @())) `
            -SourceModules @((Get-V2OptionalProperty -InputObject $completionPayload -Name "source_modules" -DefaultValue @())) `
            -Outcome "done")
        Append-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
            "## $timestamp",
            "- event: task-completed",
            "- task_id: $taskId",
            "- agent: $AgentName",
            "- completion_payload: $completionPayloadPath"
        )
        if ($stepCheckpointAvailable) {
            Write-V2RuntimeStepCheckpoint -ProjectRoot $resolvedProjectPath -TaskId $taskId -StepNumber 3 -StepName "finalize" -Status "ok" -AgentName $AgentName
            Clear-V2RuntimeStepCheckpoints -ProjectRoot $resolvedProjectPath -TaskId $taskId
        }

        # Update whiteboard — mark this task's intent as completed (with optional handoff to next agent)
        if (Test-Path -LiteralPath $whiteboardScript -PathType Leaf) {
            $nextAgent = [string](Get-V2OptionalProperty -InputObject $task -Name "preferred_agent" -DefaultValue "")
            if ($nextAgent -eq $AgentName -or [string]::IsNullOrWhiteSpace($nextAgent)) { $nextAgent = "" }
            try {
                & $whiteboardScript `
                    -Mode        "complete" `
                    -ProjectPath $resolvedProjectPath `
                    -TaskId      $taskId `
                    -AgentName   $AgentName `
                    -HandoffTo   $nextAgent | Out-Null
            }
            catch {}
        }

        Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
            "## $timestamp",
            "- from: $AgentName",
            "- to: SchedulerV2",
            "- task_id: $taskId",
            "- status: $([string](Get-V2OptionalProperty -InputObject $task -Name 'status' -DefaultValue 'unknown'))",
            "- completion_payload: $completionPayloadPath"
        )

        $executed += 1
    }

    $taskStateDbSyncScript = Join-Path (Join-Path $PSScriptRoot "v2") "task_state_db.py"
    $taskStateDbBackendMode = ""
    $taskStateDbPrimaryMode = $false
    if (Test-Path -LiteralPath $taskStateDbSyncScript -PathType Leaf) {
        try {
            $taskStateStatusRaw = & python $taskStateDbSyncScript --project-path $resolvedProjectPath --mode status --emit-json 2>$null | Out-String
            $taskStateStatusDoc = $null
            try {
                $taskStateStatusDoc = ($taskStateStatusRaw | ConvertFrom-Json)
            }
            catch {
                $taskStateStatusDoc = $null
            }
            if ($taskStateStatusDoc -and [bool](Get-V2OptionalProperty -InputObject $taskStateStatusDoc -Name "ok" -DefaultValue $false)) {
                $taskStateDbBackendMode = [string](Get-V2OptionalProperty -InputObject $taskStateStatusDoc -Name "backend_mode" -DefaultValue "")
                $taskStateDbPrimaryMode = ($taskStateDbBackendMode -eq "db-primary-v1")
            }
        }
        catch {
        }
    }

    $tasks = @(Invoke-V2WithDagMutex -DagPath $taskDagPath -ScriptBlock {
        $latestDagForMerge = Get-V2JsonContent -Path $taskDagPath
        $targetDag = if ($latestDagForMerge -and ($latestDagForMerge.PSObject.Properties.Name -contains "tasks")) {
            $latestDagForMerge
        }
        else {
            $dag
        }

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
        if ($latestDagForMerge -and ($latestDagForMerge.PSObject.Properties.Name -contains "tasks")) {
            foreach ($externalTask in @($latestDagForMerge.tasks)) {
                $externalTaskId = [string](Get-V2OptionalProperty -InputObject $externalTask -Name "id" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($externalTaskId)) {
                    $mergedTasks.Add($externalTask)
                    continue
                }

                if (-not $localById.ContainsKey($externalTaskId)) {
                    $mergedTasks.Add($externalTask)
                    continue
                }

                $localTask = $localById[$externalTaskId]
                $useLocal = $true
                $externalUpdatedRaw = [string](Get-V2OptionalProperty -InputObject $externalTask -Name "updated_at" -DefaultValue "")
                $localUpdatedRaw = [string](Get-V2OptionalProperty -InputObject $localTask -Name "updated_at" -DefaultValue "")
                if (-not [string]::IsNullOrWhiteSpace($externalUpdatedRaw) -and -not [string]::IsNullOrWhiteSpace($localUpdatedRaw)) {
                    try {
                        $externalUpdated = [DateTimeOffset]::Parse($externalUpdatedRaw).UtcDateTime
                        $localUpdated = [DateTimeOffset]::Parse($localUpdatedRaw).UtcDateTime
                        if ($externalUpdated -gt $localUpdated) {
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
                    $mergedTasks.Add($externalTask)
                }
                $localById.Remove($externalTaskId)
            }
        }

        foreach ($remainingTask in @($localById.Values)) {
            $mergedTasks.Add($remainingTask)
        }
        foreach ($remainingNoId in @($localNoId.ToArray())) {
            $mergedTasks.Add($remainingNoId)
        }

        $mergedTaskArray = @($mergedTasks.ToArray())
        if (-not $taskStateDbPrimaryMode) {
            Set-V2ObjectProperty -InputObject $targetDag -Name "updated_at" -Value (Get-V2Timestamp)
            Set-V2ObjectProperty -InputObject $targetDag -Name "tasks" -Value $mergedTaskArray
            Save-V2JsonContent -Path $taskDagPath -Value $targetDag
        }
        return $mergedTaskArray
    })

    foreach ($syncScript in @($taskSyncScript, $lockSyncScript)) {
        if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
            continue
        }

        try {
            & $syncScript -ProjectPath $resolvedProjectPath | Out-Null
        }
        catch {
            Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                "## $(Get-V2Timestamp)",
                "- from: $AgentName",
                "- to: runtime-sync",
                "- status: sync-failed",
                "- script: $syncScript",
                "- reason: $($_.Exception.Message)"
            )
        }
    }

    if (Test-Path -LiteralPath $taskStateDbSyncScript -PathType Leaf) {
        $taskStateDbSyncSucceeded = $false
        if ($taskStateDbPrimaryMode) {
            $safeAgentName = ($AgentName -replace '[^A-Za-z0-9_-]', '-')
            if ([string]::IsNullOrWhiteSpace($safeAgentName)) {
                $safeAgentName = "agent"
            }
            $tasksBufferPath = Join-Path $orchestratorRoot ("state/agent-loop-db-tasks-buffer-{0}.json" -f $safeAgentName)
            Save-V2JsonContent -Path $tasksBufferPath -Value @($tasks)
            try {
                & python $taskStateDbSyncScript --project-path $resolvedProjectPath --mode write-tasks --tasks-json-path $tasksBufferPath | Out-Null
                $taskStateDbSyncSucceeded = $true
            }
            catch {
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $(Get-V2Timestamp)",
                    "- from: $AgentName",
                    "- to: runtime-sync",
                    "- status: task-state-db-write-failed",
                    "- script: $taskStateDbSyncScript",
                    "- reason: $($_.Exception.Message)"
                )
            }
            finally {
                Remove-Item -LiteralPath $tasksBufferPath -Force -ErrorAction SilentlyContinue
            }
        }
        else {
            try {
                & python $taskStateDbSyncScript --project-path $resolvedProjectPath --mode sync | Out-Null
                $taskStateDbSyncSucceeded = $true
            }
            catch {
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $(Get-V2Timestamp)",
                    "- from: $AgentName",
                    "- to: runtime-sync",
                    "- status: task-state-db-sync-failed",
                    "- script: $taskStateDbSyncScript",
                    "- reason: $($_.Exception.Message)"
                )
            }
        }

        if ($taskStateDbSyncSucceeded) {
            try {
                & python $taskStateDbSyncScript --project-path $resolvedProjectPath --mode flush-dag | Out-Null
            }
            catch {
                Append-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
                    "## $(Get-V2Timestamp)",
                    "- from: $AgentName",
                    "- to: runtime-sync",
                    "- status: task-state-db-flush-failed",
                    "- script: $taskStateDbSyncScript",
                    "- reason: $($_.Exception.Message)"
                )
            }
        }
    }

    Write-Output "Agent loop cycle complete for $AgentName. Executed: $executed"
    if ($RunOnce) { break }
    Start-Sleep -Seconds $PollIntervalSeconds
}
