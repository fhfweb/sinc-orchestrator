<#
.SYNOPSIS
    Generates baseline 360 orchestration artifacts (business context, architecture, review, product, simulation, observability).
.DESCRIPTION
    Produces deterministic artifacts under ai-orchestrator/ so the V2 pipeline can reason about
    business intent before/while executing technical tasks.

    This script is safe to run multiple times: files are rewritten with current evidence from
    PROJECT_REQUEST.md and ai-orchestrator/state/project-state.json.
.PARAMETER ProjectPath
    Target project root that contains ai-orchestrator/.
.PARAMETER FailOnMissingProjectRequest
    If set, fails when PROJECT_REQUEST.md is missing. Default behavior is graceful fallback.
.EXAMPLE
    .\scripts\Generate-Orchestrator360Artifacts.ps1 -ProjectPath ".\workspace\projects\my-project"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [switch]$FailOnMissingProjectRequest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path (Join-Path $PSScriptRoot "v2") "Common.ps1")

function Get-SectionValue {
    param(
        [string]$Content,
        [string]$SectionName
    )

    if ([string]::IsNullOrWhiteSpace($Content) -or [string]::IsNullOrWhiteSpace($SectionName)) {
        return ""
    }

    $escaped = [regex]::Escape($SectionName)
    $pattern = "(?ms)^\s*##\s*$escaped\s*\r?\n(.*?)(?=^\s*##\s+|\z)"
    $match = [regex]::Match($Content, $pattern)
    if (-not $match.Success) {
        return ""
    }

    return [string]$match.Groups[1].Value.Trim()
}

function Get-BulletItems {
    param([string]$Text)

    $items = New-Object System.Collections.Generic.List[string]
    foreach ($line in @($Text -split "(`r`n|`n|`r)")) {
        $clean = [string]$line
        if ([string]::IsNullOrWhiteSpace($clean)) { continue }
        $clean = $clean.Trim()
        if ($clean.StartsWith("-")) {
            $clean = $clean.Substring(1).Trim()
        }
        if ($clean.StartsWith("*")) {
            $clean = $clean.Substring(1).Trim()
        }
        if ([string]::IsNullOrWhiteSpace($clean)) { continue }
        $items.Add($clean)
    }
    return @($items.ToArray())
}

function Write-TextFile {
    param(
        [string]$Path,
        [string]$Content
    )

    $parent = Split-Path -Parent $Path
    Ensure-V2Directory -Path $parent
    [System.IO.File]::WriteAllText($Path, $Content)
}

function Write-JsonFileIfMissing {
    param(
        [string]$Path,
        [object]$Value
    )

    $parent = Split-Path -Parent $Path
    Ensure-V2Directory -Path $parent
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Save-V2JsonContent -Path $Path -Value $Value
    }
}

function Get-FirstNonEmpty {
    param([string[]]$Values)
    foreach ($value in @($Values)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$value)) {
            return [string]$value
        }
    }
    return ""
}

function New-V2AutoIntakeContent {
    param(
        [string]$ProjectName,
        [string]$ProjectSlug,
        [string]$Owner,
        [string]$Description,
        [string]$Goal,
        [string]$ConstraintsText,
        [string]$ExpectedOutcome,
        [string]$PrimaryLanguage,
        [string]$DatabaseEngine
    )

    $coreProblem = Get-FirstNonEmpty -Values @($Description, $Goal, "Define core problem in PROJECT_REQUEST.md")
    $who = "Primary user for $ProjectName"
    $uniqueValue = Get-FirstNonEmpty -Values @($Goal, "Deliver measurable value for primary user with low operational overhead")
    $northStar = Get-FirstNonEmpty -Values @($ExpectedOutcome, "Activated users completing critical flow")
    $constraints = @($ConstraintsText -split "(`r`n|`n|`r)" | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    $flow1 = Get-FirstNonEmpty -Values @($Goal, "User completes main workflow successfully")
    $flow2 = "Operator validates system health and receives feedback"
    $flow1Acceptance = "Primary user can complete the intended workflow without manual support."
    $flow2Acceptance = "Operator can monitor completion and resolve failures quickly."

    $included = if ($constraints.Count -gt 0) { ($constraints | Select-Object -First 3) -join "\n- " } else { "Core API\nAutomated tests\nOperational observability" }

    return @"
PROJECT_NAME=$ProjectName
PROJECT_ID=$ProjectSlug
OWNER=$Owner
CORE_PROBLEM=$coreProblem
WHO_HAS_THIS_PROBLEM=$who
CURRENT_ALTERNATIVE=manual_process
WHY_ALTERNATIVE_FAILS=low_visibility_and_inconsistent_execution
UNIQUE_VALUE=$uniqueValue
REVENUE_MODEL=subscription
NORTH_STAR_METRIC=$northStar
PERSONA_1_NAME=Primary Operator
PERSONA_1_ROLE=System Operator
PERSONA_1_IS_PAYER=true
PERSONA_1_TECHNICAL_LEVEL=medium
PERSONA_1_MAIN_GOAL=Deliver expected business outcome.
PERSONA_1_MAIN_FRUSTRATION=Lack of end-to-end visibility.
CRITICAL_FLOW_1=$flow1
CRITICAL_FLOW_2=$flow2
FLOW_1_ACCEPTANCE=$flow1Acceptance
FLOW_2_ACCEPTANCE=$flow2Acceptance
LAUNCH_BLOCKER_FLOWS=1,2
MVP_INCLUDED=- $included
MVP_EXCLUDED=- Optional integrations not required for launch
POST_LAUNCH_BACKLOG=- UX refinements\n- Secondary automation paths
MAX_RESPONSE_TIME_MS=500
MAX_RESPONSE_TIME_CRITICAL_MS=200
TARGET_UPTIME_PERCENT=99.9
USERS_AT_LAUNCH=100
USERS_AT_MONTH_6=1000
USERS_AT_MONTH_12=5000
CONCURRENT_USERS_PEAK=100
DATA_SENSITIVITY=internal
PII_PRESENT=true
MFA_REQUIRED=false
RTO_MINUTES=60
RPO_MINUTES=15
COMPLIANCE_LGPD=true
DATA_RETENTION_DAYS=365
RIGHT_TO_ERASURE=true
DATA_EXPORT_REQUIRED=true
STACK_LANGUAGE_BACKEND=$PrimaryLanguage
STACK_DATABASE_PRIMARY=$DatabaseEngine
ARCHITECTURAL_PATTERN=modular_monolith
MULTI_TENANCY_REQUIRED=false
MULTI_TENANCY_STRATEGY=none
LAUNCH_SUCCESS_CRITERIA=- Build and test pass with verified commands\n- Core flow runs without blockers
MONTH_3_SUCCESS_CRITERIA=- Increased active usage and reduced support incidents
MONTH_12_SUCCESS_CRITERIA=- Stable growth with reliable operations
MAIN_DEPLOYMENT_STRATEGY=rolling
ROLLBACK_STRATEGY=automatic
ON_CALL_OWNER=$Owner
"@
}

function Invoke-V2BusinessContextEngine {
    param(
        [string]$ProjectRoot,
        [string]$OrchestratorRoot,
        [string]$ProjectSlug,
        [string]$ProjectName,
        [string]$RequestContent,
        [string]$PrimaryLanguage,
        [string]$DatabaseEngine
    )

    $enginePath = Join-Path (Split-Path -Parent $PSScriptRoot) "scripts/business_context_engine.py"
    if (-not (Test-Path -LiteralPath $enginePath -PathType Leaf)) {
        return [PSCustomObject]@{ success = $false; reason = "engine-missing" }
    }

    $contextDir = Join-Path $OrchestratorRoot "context"
    Ensure-V2Directory -Path $contextDir
    $templatePath = Join-Path (Split-Path -Parent $PSScriptRoot) "docs/context/intake_template.env"
    $schemaTemplatePath = Join-Path (Split-Path -Parent $PSScriptRoot) "docs/context/world_model.schema.json"
    $intakePath = Join-Path $contextDir "intake.env"
    $schemaPath = Join-Path $contextDir "world_model.schema.json"

    if (-not (Test-Path -LiteralPath $schemaPath -PathType Leaf) -and (Test-Path -LiteralPath $schemaTemplatePath -PathType Leaf)) {
        Copy-Item -LiteralPath $schemaTemplatePath -Destination $schemaPath -Force
    }

    if (-not (Test-Path -LiteralPath $intakePath -PathType Leaf)) {
        if (Test-Path -LiteralPath $templatePath -PathType Leaf) {
            Copy-Item -LiteralPath $templatePath -Destination $intakePath -Force
        }

        $owner = [Environment]::UserName
        $description = Get-SectionValue -Content $RequestContent -SectionName "Description"
        $goal = Get-SectionValue -Content $RequestContent -SectionName "Goal"
        $constraintsText = Get-SectionValue -Content $RequestContent -SectionName "Constraints"
        $expectedOutcome = Get-SectionValue -Content $RequestContent -SectionName "Expected Outcome"
        $autoContent = New-V2AutoIntakeContent `
            -ProjectName $ProjectName `
            -ProjectSlug $ProjectSlug `
            -Owner $owner `
            -Description $description `
            -Goal $goal `
            -ConstraintsText $constraintsText `
            -ExpectedOutcome $expectedOutcome `
            -PrimaryLanguage $PrimaryLanguage `
            -DatabaseEngine $DatabaseEngine
        [System.IO.File]::WriteAllText($intakePath, $autoContent)
    }

    try {
        Push-Location $ProjectRoot
        $args = @(
            $enginePath,
            "--intake", $intakePath,
            "--project-id", $ProjectSlug,
            "--output-dir", $contextDir,
            "--schema", $schemaPath,
            "--update",
            "--sync-qdrant"
        )
        $raw = & python @args 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            return [PSCustomObject]@{ success = $false; reason = "engine-failed"; output = $raw }
        }
        $parsed = $null
        try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null }
        if ($parsed -and [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false)) {
            return [PSCustomObject]@{
                success = $true
                intake_path = $intakePath
                world_model_path = [string](Get-V2OptionalProperty -InputObject $parsed -Name "world_model_path" -DefaultValue (Join-Path $contextDir "world-model.json"))
            }
        }
        return [PSCustomObject]@{ success = $false; reason = "engine-invalid-output"; output = $raw }
    }
    catch {
        return [PSCustomObject]@{ success = $false; reason = "engine-exception"; output = $_.Exception.Message }
    }
    finally {
        Pop-Location
    }
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$requestPath = Join-Path $resolvedProjectPath "PROJECT_REQUEST.md"

$state = Get-V2JsonContent -Path $statePath
if (-not $state) {
    throw "project-state.json not found or invalid: $statePath"
}
$phaseApprovalsChanged = Ensure-V2PhaseApprovals -ProjectState $state -UpdatedBy "orchestrator-360-artifacts"
if ($phaseApprovalsChanged) {
    Set-V2DynamicProperty -InputObject $state -Name "updated_at" -Value (Get-V2Timestamp)
    Save-V2JsonContent -Path $statePath -Value $state
}

$requestContent = ""
if (Test-Path -LiteralPath $requestPath -PathType Leaf) {
    $requestContent = Get-Content -LiteralPath $requestPath -Raw
}
elseif ($FailOnMissingProjectRequest) {
    throw "PROJECT_REQUEST.md not found: $requestPath"
}

$projectName = [string](Get-V2OptionalProperty -InputObject $state -Name "project_name" -DefaultValue (Split-Path -Leaf $resolvedProjectPath))
$projectSlug = [string](Get-V2OptionalProperty -InputObject $state -Name "project_slug" -DefaultValue (Get-V2ProjectSlug -Name $projectName))
$projectType = [string](Get-V2OptionalProperty -InputObject $state -Name "project_type" -DefaultValue "unknown")
$fingerprint = Get-V2OptionalProperty -InputObject $state -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
$verified = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
$analysis = Get-V2OptionalProperty -InputObject $state -Name "analysis" -DefaultValue ([PSCustomObject]@{})

$description = Get-SectionValue -Content $requestContent -SectionName "Description"
$goal = Get-SectionValue -Content $requestContent -SectionName "Goal"
$constraintsText = Get-SectionValue -Content $requestContent -SectionName "Constraints"
$expectedOutcome = Get-SectionValue -Content $requestContent -SectionName "Expected Outcome"
$valueProposal = if (-not [string]::IsNullOrWhiteSpace($goal)) { $goal } elseif (-not [string]::IsNullOrWhiteSpace($description)) { $description } else { "Define a clear business value proposition in PROJECT_REQUEST.md." }

$constraints = Get-BulletItems -Text $constraintsText
if (@($constraints).Count -eq 0) {
    $constraints = @(
        "Keep MVP scope focused on one primary user journey.",
        "Require verified build/test commands before phase completion.",
        "Preserve project isolation for Postgres schema, Neo4j namespace, and Qdrant collection."
    )
}

$primaryLanguage = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "unknown")
$databaseEngine = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $fingerprint -Name "database" -DefaultValue ([PSCustomObject]@{})) -Name "engine" -DefaultValue "unknown")
$architecturePattern = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "architecture_pattern" -DefaultValue "unknown")
$frameworks = @(Get-V2OptionalProperty -InputObject $fingerprint -Name "frameworks" -DefaultValue @())
$unknowns = @(Get-V2OptionalProperty -InputObject $state -Name "unknowns" -DefaultValue @())

$buildCommand = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verified -Name "build" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "unknown")
$testCommand = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verified -Name "test" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "unknown")

$businessContextPath = Join-Path $orchestratorRoot "context/business-context.json"
$businessContextMarkdownPath = Join-Path $orchestratorRoot "context/business-context.md"
$adrPath = Join-Path $orchestratorRoot "documentation/adr/ADR-0001-context-driven-architecture.md"
$contractsPath = Join-Path $orchestratorRoot "documentation/interfaces/contracts.md"
$codeReviewPath = Join-Path $orchestratorRoot "reports/code-review-checklist.md"
$productValidationPath = Join-Path $orchestratorRoot "reports/product-validation-checklist.md"
$simulationPath = Join-Path $orchestratorRoot "reports/user-simulation-plan.md"
$observabilityPath = Join-Path $orchestratorRoot "reports/production-observability-plan.md"
$runtimeTelemetryPath = Join-Path $orchestratorRoot "runtime/telemetry/production-metrics.json"
$runtimeThresholdsPath = Join-Path $orchestratorRoot "runtime/telemetry/thresholds.json"
$releaseConfigPath = Join-Path $orchestratorRoot "release/release-config.json"

$engineResult = Invoke-V2BusinessContextEngine `
    -ProjectRoot $resolvedProjectPath `
    -OrchestratorRoot $orchestratorRoot `
    -ProjectSlug $projectSlug `
    -ProjectName $projectName `
    -RequestContent $requestContent `
    -PrimaryLanguage $primaryLanguage `
    -DatabaseEngine $databaseEngine
if (-not [bool](Get-V2OptionalProperty -InputObject $engineResult -Name "success" -DefaultValue $false)) {
    Write-Warning ("Business context engine fallback: {0}" -f [string](Get-V2OptionalProperty -InputObject $engineResult -Name "reason" -DefaultValue "unknown"))
}

$timestamp = Get-V2Timestamp
$businessContext = [PSCustomObject]@{
    generated_at = $timestamp
    project      = [PSCustomObject]@{
        name = $projectName
        slug = $projectSlug
        type = $projectType
    }
    domain       = [PSCustomObject]@{
        problem_statement = if (-not [string]::IsNullOrWhiteSpace($description)) { $description } else { "Missing description in PROJECT_REQUEST.md" }
        value_proposition = $valueProposal
        expected_outcome  = if (-not [string]::IsNullOrWhiteSpace($expectedOutcome)) { $expectedOutcome } else { "Deliver MVP with validated core workflow and production-ready baseline." }
    }
    non_functional_requirements = [PSCustomObject]@{
        reliability = "Health checks must stay >= healthy and CORE-COMPLETE-001 must pass."
        security    = "Secrets masked in outputs and stored only in protected vault."
        performance = "P95 API latency target <= 400ms for critical path in initial production."
        observability = "Critical endpoints and failed flows must open repair tasks automatically."
    }
    architecture = [PSCustomObject]@{
        primary_language     = if (-not [string]::IsNullOrWhiteSpace($primaryLanguage)) { $primaryLanguage } else { "unknown" }
        frameworks           = @($frameworks)
        architecture_pattern = if (-not [string]::IsNullOrWhiteSpace($architecturePattern)) { $architecturePattern } else { "unknown" }
        database_engine      = if (-not [string]::IsNullOrWhiteSpace($databaseEngine)) { $databaseEngine } else { "unknown" }
    }
    definition_of_done = @(
        "Business context and ADR approved by human checkpoint.",
        "Build and test commands verified and passing.",
        "Domain migration scaffold applied and validated.",
        "User simulation happy-path and adversarial-path executed with report.",
        "Production observability plan active with automatic repair-task policy."
    )
    personas = @(
        [PSCustomObject]@{
            name   = "Primary Operator"
            goal   = "Run the core business workflow with minimal friction."
            pain   = "Manual process and fragmented tooling."
            source = "inferred-from-project-request"
        },
        [PSCustomObject]@{
            name   = "Business Owner"
            goal   = "See measurable business outcome and operational health."
            pain   = "Low visibility on quality and delivery risk."
            source = "orchestrator-default"
        }
    )
    constraints = @($constraints)
    unknowns    = @($unknowns)
    verified_commands = [PSCustomObject]@{
        build = $buildCommand
        test  = $testCommand
    }
}

Save-V2JsonContent -Path $businessContextPath -Value $businessContext

$contextLines = New-Object System.Collections.Generic.List[string]
$contextLines.Add("# Business Context")
$contextLines.Add("")
$contextLines.Add("- Generated At: $timestamp")
$contextLines.Add("- Project: $projectName ($projectSlug)")
$contextLines.Add("- Type: $projectType")
$contextLines.Add("")
$contextLines.Add("## Problem")
if (-not [string]::IsNullOrWhiteSpace($description)) {
    $contextLines.Add($description)
}
else {
    $contextLines.Add("Missing description in PROJECT_REQUEST.md.")
}
$contextLines.Add("")
$contextLines.Add("## Value Proposition")
$contextLines.Add($valueProposal)
$contextLines.Add("")
$contextLines.Add("## Constraints")
if (@($constraints).Count -eq 0) {
    $contextLines.Add("- none")
}
else {
    foreach ($constraint in @($constraints)) {
        $contextLines.Add("- $constraint")
    }
}
$contextLines.Add("")
$contextLines.Add("## Definition Of Done")
$contextLines.Add("- Business context and ADR approved by human checkpoint.")
$contextLines.Add("- Build and test commands verified and passing.")
$contextLines.Add("- Domain migration scaffold applied and validated.")
$contextLines.Add("- User simulation and production observability reports generated.")

$contextMd = ($contextLines -join [Environment]::NewLine)
Write-TextFile -Path $businessContextMarkdownPath -Content $contextMd

$adrContent = @(
    "# ADR-0001: Context-Driven Architecture Baseline",
    "",
    "- Date: $timestamp",
    "- Status: Accepted (baseline)",
    "",
    "## Context",
    "The orchestrator must execute software delivery with business context as first-class input, not only technical fingerprint.",
    "",
    "## Decision",
    "- Persist business context in `ai-orchestrator/context/business-context.json`.",
    "- Keep architecture decisions and contracts versioned in `ai-orchestrator/documentation/`.",
    "- Gate completion through `CORE-COMPLETE-001` requiring verified build/test + healthy runtime + migration evidence.",
    "",
    "## Consequences",
    "- Better backlog quality for greenfield projects.",
    "- Reduced false-green states by connecting technical checks to business acceptance criteria.",
    "- Clear handoff across agents with stable artifacts."
) -join [Environment]::NewLine
Write-TextFile -Path $adrPath -Content $adrContent

$contractsContent = @(
    "# Interface Contracts",
    "",
    "- Generated At: $timestamp",
    "",
    "## Runtime Contracts",
    "- ``task-dag.json`` is the canonical task state.",
    "- ``locks.json`` is the canonical lock state.",
    "- ``project-state.json`` is the canonical operational state.",
    "",
    "## API Contracts (baseline)",
    "- `GET /health` must return status payload and non-500 response.",
    "- Core domain endpoints must have request validation + explicit error payload shape.",
    "",
    "## Event Contracts (baseline)",
    "- Domain task completion events must include: ``project_slug``, ``task_id``, ``status``, ``updated_at``.",
    "- Observability incidents must include: ``category``, ``severity``, ``reason``, ``evidence_path``."
) -join [Environment]::NewLine
Write-TextFile -Path $contractsPath -Content $contractsContent

$codeReviewContent = @(
    "# Code Review Checklist",
    "",
    "- Generated At: $timestamp",
    "",
    "## Technical",
    "- Verify architecture decisions are respected (ADR + contracts).",
    "- Reject unknown build/test commands for completion gating.",
    "- Check migration reversibility and backward compatibility.",
    "",
    "## Security",
    "- Reject hardcoded secrets and plaintext credential exposure.",
    "- Validate authorization and input validation in critical endpoints.",
    "",
    "## Business",
    "- Confirm implementation maps to Definition of Done criteria.",
    "- Open corrective tasks when requirement traceability is missing."
) -join [Environment]::NewLine
Write-TextFile -Path $codeReviewPath -Content $codeReviewContent

$productValidationContent = @(
    "# Product Validation Checklist",
    "",
    "- Generated At: $timestamp",
    "",
    "## Requirement Traceability",
    "- Map each implemented feature to a business requirement from PROJECT_REQUEST/context.",
    "- Flag implemented scope that has no corresponding requirement.",
    "",
    "## Flow Completeness",
    "- Validate primary business flow end-to-end from input to expected outcome.",
    "- Validate failure paths produce actionable feedback.",
    "",
    "## Release Risk",
    "- Provide READY/NOT READY decision with evidence paths.",
    "- Document missing items and operational impact."
) -join [Environment]::NewLine
Write-TextFile -Path $productValidationPath -Content $productValidationContent

$simulationContent = @(
    "# User Simulation Plan",
    "",
    "- Generated At: $timestamp",
    "",
    "## Happy Path",
    "- Execute primary persona flow from first action to successful completion.",
    "- Record latency and UX friction points.",
    "",
    "## Adversarial Path",
    "- Invalid inputs, duplicate operations, expired session, service degradation.",
    "- Verify graceful error handling and no data corruption.",
    "",
    "## Exit Criteria",
    "- No blocker in happy path.",
    "- No silent failure in adversarial path."
) -join [Environment]::NewLine
Write-TextFile -Path $simulationPath -Content $simulationContent

$observabilityContent = @(
    "# Production Observability Plan",
    "",
    "- Generated At: $timestamp",
    "",
    "## Critical Signals",
    "- API latency and error rate on critical business endpoints.",
    "- Task throughput and repair-task creation rate.",
    "- Memory sync health (Qdrant/Neo4j) with project_slug isolation checks.",
    "",
    "## Automatic Actions",
    "- Open REPAIR task when thresholds are breached.",
    "- Escalate to incident when breach persists across multiple cycles.",
    "",
    "## Feedback Loop",
    "- Persist production findings back into business context and architecture decisions.",
    "- Re-prioritize backlog based on real user impact."
) -join [Environment]::NewLine
Write-TextFile -Path $observabilityPath -Content $observabilityContent

$runtimeMetricsBaseline = [PSCustomObject]@{
    generated_at = $timestamp
    source       = "orchestrator-360-baseline"
    metrics      = [PSCustomObject]@{
        api = [PSCustomObject]@{
            latency_p95_ms = 0
            error_rate_percent = 0
        }
        business = [PSCustomObject]@{
            conversion_rate_percent = 0
        }
    }
}
Write-JsonFileIfMissing -Path $runtimeTelemetryPath -Value $runtimeMetricsBaseline

$runtimeThresholdsBaseline = [PSCustomObject]@{
    latency_p95_ms = 400
    error_rate_percent = 2
    conversion_rate_percent = 25
    repair_cooldown_seconds = 900
}
Write-JsonFileIfMissing -Path $runtimeThresholdsPath -Value $runtimeThresholdsBaseline

$releaseConfigBaseline = [PSCustomObject]@{
    deploy = [PSCustomObject]@{
        staging = [PSCustomObject]@{
            command = "docker compose -f ai-orchestrator/docker/docker-compose.generated.yml up -d --build"
            smoke_command = "python -m pytest -q"
            rollback_command = "docker compose -f ai-orchestrator/docker/docker-compose.generated.yml down"
        }
        production = [PSCustomObject]@{
            command = "docker compose -f ai-orchestrator/docker/docker-compose.generated.yml up -d --build"
            smoke_command = "python -m pytest -q"
            rollback_command = "docker compose -f ai-orchestrator/docker/docker-compose.generated.yml down"
        }
    }
}
Write-JsonFileIfMissing -Path $releaseConfigPath -Value $releaseConfigBaseline

$result = [PSCustomObject]@{
    success      = $true
    generated_at = $timestamp
    project      = $projectSlug
    paths        = [PSCustomObject]@{
        business_context_json = $businessContextPath
        business_context_md   = $businessContextMarkdownPath
        adr                   = $adrPath
        contracts             = $contractsPath
        code_review           = $codeReviewPath
        product_validation    = $productValidationPath
        user_simulation       = $simulationPath
        observability         = $observabilityPath
        runtime_telemetry     = $runtimeTelemetryPath
        release_config        = $releaseConfigPath
    }
}

$result | ConvertTo-Json -Depth 20
