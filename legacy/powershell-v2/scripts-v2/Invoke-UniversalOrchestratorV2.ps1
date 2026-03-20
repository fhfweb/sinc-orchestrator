<#
.SYNOPSIS
    V2 Universal Orchestrator - main entry point for all V2 orchestration modes.
.DESCRIPTION
    Dispatches to the appropriate V2 sub-script based on the selected Mode:
      submit   - Runs intake and initializes .ai-orchestrator layer for an existing project
      new      - Initializes a greenfield project from scratch
      watch    - Continuously monitors an inbox folder for new projects
      observe  - Runs the V2 Observer on a submitted project
      schedule - Runs the V2 Scheduler to assign tasks
      access   - Prints connection/access data from current project state
      prompt   - Unified prompt operations (status|next|claim|complete) for Codex/Claude/Antigravity
    Enforces startup-pack bootstrap (Docker port validation, Neo4j/Qdrant startup) on submit/new.
.PARAMETER Mode
    Orchestration mode: submit | new | watch | observe | schedule | access | clean | claim | complete | prompt
.PARAMETER ProjectPath
    Path to the target project (required for submit, observe, schedule modes).
.PARAMETER ProjectName
    Optional project name override.
.PARAMETER IncludeNeo4j
    Default behavior is enabled. Use -IncludeNeo4j:$false to disable Neo4j for this execution.
.PARAMETER IncludeQdrant
    Default behavior is enabled. Use -IncludeQdrant:$false to disable Qdrant for this execution.
.PARAMETER DockerConfigMode
    Docker config strategy: isolated (default) uses a temp isolated config context; user uses user profile defaults.
.PARAMETER ForceConfirmed
    Required alongside -Force in new mode when replacing an existing project directory.
.EXAMPLE
    .\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode submit -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode new -ProjectName MyNewApp
    .\scripts\v2\Invoke-UniversalOrchestratorV2.ps1 -Mode watch
#>param(
    [ValidateSet("submit", "new", "watch", "observe", "schedule", "access", "clean", "claim", "complete", "prompt")]
    [string]$Mode = "submit",
    [string]$ProjectPath,
    [string]$ProjectName,
    [string]$ProjectBriefPath,
    [string]$InboxPath = ".\incoming",
    [string]$ManagedProjectsRoot = ".\workspace\projects",
    [int]$PollIntervalSeconds = 10,
    [switch]$GenerateDocker,
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "auto",
    [ValidateSet("auto", "postgres", "mysql", "mongodb", "none")]
    [string]$Database = "auto",
    [ValidateSet("unknown", "maintain", "gradual-refactor", "full-refactor")]
    [string]$RefactorPolicy = "unknown",
    [ValidateSet("dedicated-infra", "shared-infra")]
    [string]$InfraMode = "dedicated-infra",
    [switch]$IncludeRedis,
    [switch]$IncludeRabbitMq,
    [switch]$IncludeWorker,
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [ValidateSet("user", "isolated")]
    [string]$DockerConfigMode = "isolated",
    [switch]$SkipMemorySync,
    [switch]$RunOnce,
    [switch]$Force,
    [switch]$ForceConfirmed,
    [int]$ClaimTakeoverIdleMinutes = 45,
    # claim / complete / prompt modes
    [string]$TaskId     = "",
    [string]$AgentName  = "",
    [string]$Artifacts  = "",
    [string]$Notes      = "",
    [string]$CompletionPayloadPath = "",
    [ValidateSet("status", "next", "claim", "complete")]
    [string]$PromptAction = "status",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

# Default policy:
# - submit/new/watch bootstrap with Neo4j + Qdrant enabled by default
# - explicit CLI value still wins (e.g. -IncludeNeo4j:$false)
$effectiveIncludeNeo4j = if ($PSBoundParameters.ContainsKey("IncludeNeo4j")) { [bool]$IncludeNeo4j } else { $true }
$effectiveIncludeQdrant = if ($PSBoundParameters.ContainsKey("IncludeQdrant")) { [bool]$IncludeQdrant } else { $true }
$script:V2DockerConfigMode = [string]$DockerConfigMode

function New-OpenQuestionsMarkdown {
    param([string[]]$Questions)

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Open Questions")
    $lines.Add("")
    if (@($Questions).Count -eq 0) {
        $lines.Add("- none")
    }
    else {
        foreach ($question in $Questions) {
            $lines.Add("- $question")
        }
    }

    return ($lines -join [Environment]::NewLine)
}

function Mask-V2SecretValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $trimmed = $Value.Trim()
    if ($trimmed.Length -le 4) {
        return ("*" * $trimmed.Length)
    }

    return ("{0}{1}{2}" -f $trimmed.Substring(0, 2), ("*" * ($trimmed.Length - 4)), $trimmed.Substring($trimmed.Length - 2))
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
    param([object]$Task)

    if (-not $Task) {
        return -1.0
    }

    $nowUtc = (Get-Date).ToUniversalTime()
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

    return [Math]::Round(([TimeSpan]($nowUtc - $lastActivity)).TotalMinutes, 2)
}

function Protect-V2Secret {
    <#
    .SYNOPSIS Encrypts a plaintext string using Windows DPAPI (CurrentUser scope).
    Returns a Base64-encoded ciphertext, or $null if encryption fails.
    #>
    param([string]$Plaintext)

    if ([string]::IsNullOrEmpty($Plaintext)) { return "" }
    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
        $bytes    = [System.Text.Encoding]::UTF8.GetBytes($Plaintext)
        $cipher   = [System.Security.Cryptography.ProtectedData]::Protect(
                        $bytes, $null,
                        [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [Convert]::ToBase64String($cipher)
    }
    catch {
        Write-Warning "DPAPI encryption unavailable — secret will be stored unencrypted: $($_.Exception.Message)"
        return $null
    }
}

function Unprotect-V2Secret {
    <#
    .SYNOPSIS Decrypts a Base64-encoded DPAPI ciphertext back to plaintext.
    Returns the plaintext, or the original value if decryption fails (backwards-compat).
    #>
    param([string]$CipherBase64)

    if ([string]::IsNullOrEmpty($CipherBase64)) { return "" }
    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
        $cipher = [Convert]::FromBase64String($CipherBase64)
        $bytes  = [System.Security.Cryptography.ProtectedData]::Unprotect(
                      $cipher, $null,
                      [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [System.Text.Encoding]::UTF8.GetString($bytes)
    }
    catch {
        $looksLikeDpapiCipher = $CipherBase64 -match "^[A-Za-z0-9+/=]+$" -and $CipherBase64.StartsWith("AQAAANCM") -and $CipherBase64.Length -ge 128
        if ($looksLikeDpapiCipher) {
            # Vault entry is encrypted but unreadable in this context; callers should fallback to .env or regenerated secrets.
            Write-Verbose "DPAPI decryption failed for encrypted vault value. Using runtime fallback."
            return ""
        }

        # Vault may be plaintext from older versions.
        return $CipherBase64
    }
}

function ConvertTo-V2PublicConnection {
    param(
        [object]$Connection,
        [string]$PasswordRef = ""
    )

    $publicConnection = if ($null -eq $Connection) { [PSCustomObject]@{} } else { ($Connection | ConvertTo-Json -Depth 20 | ConvertFrom-Json) }
    $passwordValue = [string](Get-V2OptionalProperty -InputObject $publicConnection -Name "password" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($passwordValue)) {
        Set-V2DynamicProperty -InputObject $publicConnection -Name "password" -Value (Mask-V2SecretValue -Value $passwordValue)
        Set-V2DynamicProperty -InputObject $publicConnection -Name "password_masked" -Value $true
        if (-not [string]::IsNullOrWhiteSpace($PasswordRef)) {
            Set-V2DynamicProperty -InputObject $publicConnection -Name "password_ref" -Value $PasswordRef
        }
    }

    return $publicConnection
}

function Write-V2SecretsVault {
    param(
        [string]$OrchestratorRoot,
        [string]$ProjectSlug,
        [object]$DbConnection,
        [object]$Neo4jConnection
    )

    $vaultRelativePath = "ai-orchestrator/database/.secrets/vault.json"
    $vaultPath = Join-Path $OrchestratorRoot "database/.secrets/vault.json"
    Initialize-V2Directory -Path (Split-Path -Parent $vaultPath)

    $rawRelPass  = [string](Get-V2OptionalProperty -InputObject $DbConnection    -Name "password" -DefaultValue "")
    $rawNeoPass  = [string](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name "password" -DefaultValue "")
    $encRelPass  = Protect-V2Secret -Plaintext $rawRelPass
    $encNeoPass  = Protect-V2Secret -Plaintext $rawNeoPass
    $isEncrypted = ($null -ne $encRelPass -and $null -ne $encNeoPass)
    $hasAnySecret = (-not [string]::IsNullOrWhiteSpace($rawRelPass)) -or (-not [string]::IsNullOrWhiteSpace($rawNeoPass))
    $allowPlaintextVault = ([string]$env:V2_ALLOW_PLAINTEXT_VAULT -eq "1")
    if ($hasAnySecret -and -not $isEncrypted -and -not $allowPlaintextVault) {
        throw "DPAPI encryption required for secrets vault. Set V2_ALLOW_PLAINTEXT_VAULT=1 only for temporary emergency fallback."
    }

    $vaultDocument = [PSCustomObject]@{
        generated_at = Get-V2Timestamp
        project_slug = $ProjectSlug
        encrypted    = $isEncrypted
        encryption   = if ($isEncrypted) { "dpapi-currentuser" } else { "none" }
        secrets      = [PSCustomObject]@{
            relational = [PSCustomObject]@{
                password = if ($isEncrypted) { $encRelPass } else { $rawRelPass }
            }
            neo4j      = [PSCustomObject]@{
                password = if ($isEncrypted) { $encNeoPass } else { $rawNeoPass }
            }
        }
    }
    try {
        Save-V2JsonContent -Path $vaultPath -Value $vaultDocument
    }
    catch {
        try {
            if (Test-Path -LiteralPath $vaultPath -PathType Leaf) {
                Remove-Item -LiteralPath $vaultPath -Force -ErrorAction SilentlyContinue
            }
            $vaultJson = $vaultDocument | ConvertTo-Json -Depth 20
            Set-Content -LiteralPath $vaultPath -Value $vaultJson -Encoding UTF8 -Force
        }
        catch {
            throw "Failed to persist secrets vault at '$vaultPath': $($_.Exception.Message)"
        }
    }

    try {
        $vaultItem = Get-Item -LiteralPath $vaultPath -ErrorAction Stop
        $vaultItem.Attributes = ($vaultItem.Attributes -bor [System.IO.FileAttributes]::Hidden)
    }
    catch {
        Write-Warning "Could not hide secrets vault file: $($_.Exception.Message)"
    }

    return [PSCustomObject]@{
        path          = $vaultPath
        relative_path = $vaultRelativePath
    }
}

function Write-V2AnalysisArtifacts {
    param(
        [string]$OrchestratorRoot,
        [object]$Intake
    )

    $architecturePath = Join-Path $OrchestratorRoot "analysis/architecture-report.md"
    $dependencyPath = Join-Path $OrchestratorRoot "analysis/dependency-graph.md"
    $qualityPath = Join-Path $OrchestratorRoot "analysis/code-quality.md"
    $servicesMapPath = Join-Path $OrchestratorRoot "services/map.md"
    $documentationArchitecturePath = Join-Path $OrchestratorRoot "documentation/architecture.md"

    $depGraph = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $Intake -Name "analysis") -Name "dependency_graph" -DefaultValue $null
    $quality = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $Intake -Name "analysis") -Name "code_quality" -DefaultValue $null
    $fingerprint = Get-V2OptionalProperty -InputObject $Intake -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})

    $architectureLines = New-Object System.Collections.Generic.List[string]
    $architectureLines.Add("# Architecture Report")
    $architectureLines.Add("")
    $architectureLines.Add("- Generated At: $(Get-V2Timestamp)")
    $architectureLines.Add("- Project Type: $(Get-V2OptionalProperty -InputObject $Intake -Name 'project_type' -DefaultValue 'unknown')")
    $architectureLines.Add("- Confidence: $(Get-V2OptionalProperty -InputObject $Intake -Name 'confidence' -DefaultValue 'unknown')")
    $architectureLines.Add("- Architecture Pattern: $(Get-V2OptionalProperty -InputObject $fingerprint -Name 'architecture_pattern' -DefaultValue 'unknown')")
    $architectureLines.Add("- Primary Language: $(Get-V2OptionalProperty -InputObject $fingerprint -Name 'primary_language' -DefaultValue 'unknown')")
    $architectureLines.Add("- API Patterns: $((@(Get-V2OptionalProperty -InputObject $fingerprint -Name 'api_patterns' -DefaultValue @()) -join ', '))")
    $architectureLines.Add("")
    $architectureLines.Add("## Improvement Suggestions")
    $architectureLines.Add("- Validate service boundaries before splitting microservices.")
    $architectureLines.Add("- Enforce test command verification before architecture confirmation.")
    $architectureLines.Add("- Keep refactor policy explicit for legacy migrations.")
    Write-V2File -Path $architecturePath -Content ($architectureLines -join [Environment]::NewLine) -Force

    $docArchitecture = New-Object System.Collections.Generic.List[string]
    $docArchitecture.Add("# Architecture")
    $docArchitecture.Add("")
    $docArchitecture.Add("- Generated At: $(Get-V2Timestamp)")
    $docArchitecture.Add("- Project Type: $(Get-V2OptionalProperty -InputObject $Intake -Name 'project_type' -DefaultValue 'unknown')")
    $docArchitecture.Add("- Runtime Stack: $(Get-V2OptionalProperty -InputObject $fingerprint -Name 'primary_language' -DefaultValue 'unknown')")
    $docArchitecture.Add("- Frameworks: $((@(Get-V2OptionalProperty -InputObject $fingerprint -Name 'frameworks' -DefaultValue @()) -join ', '))")
    $docArchitecture.Add("- Database: $(Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $fingerprint -Name 'database' -DefaultValue ([PSCustomObject]@{})) -Name 'engine' -DefaultValue 'unknown')")
    $docArchitecture.Add("- Architecture Pattern: $(Get-V2OptionalProperty -InputObject $fingerprint -Name 'architecture_pattern' -DefaultValue 'unknown')")
    $docArchitecture.Add("")
    $docArchitecture.Add("## Modules")
    $modules = @(Get-V2OptionalProperty -InputObject $depGraph -Name "modules" -DefaultValue @())
    if ($modules.Count -eq 0) {
        $docArchitecture.Add("- none detected")
    }
    else {
        foreach ($module in @($modules | Select-Object -First 80)) {
            $docArchitecture.Add("- $module")
        }
    }
    $docArchitecture.Add("")
    $docArchitecture.Add("## Core Flows")
    $docArchitecture.Add("- Intake -> Analysis -> Scheduler -> Agent Runtime -> Reports")
    $docArchitecture.Add("- Observer runs health checks and opens REPAIR tasks when checks fail")
    $docArchitecture.Add("- Memory sync persists structural + semantic context into project memory backends")
    $docArchitecture.Add("")
    $docArchitecture.Add("## Runtime Contracts")
    $docArchitecture.Add('- `task-dag.json` is canonical task state')
    $docArchitecture.Add('- `locks.json` is canonical lock state')
    $docArchitecture.Add("- Markdown task/lock boards are generated views and must be kept in sync")
    # Only write the auto-generated doc when current content is empty/placeholder/auto-generated.
    # Preserve curated architecture docs when they are substantial (>30 non-empty lines)
    # and not placeholder text.
    $existingDocArch = if (Test-Path -LiteralPath $documentationArchitecturePath -PathType Leaf) {
        Get-Content -LiteralPath $documentationArchitecturePath -Raw
    } else { "" }
    $existingDocArchLines = @(($existingDocArch -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $existingDocArchLineCount = $existingDocArchLines.Count
    $docArchLooksAutoGenerated = $existingDocArch -match "## Runtime Contracts" -and $existingDocArch -match "`task-dag.json` is canonical task state"
    $docArchHasPlaceholder = $existingDocArch -match "This file should always reflect the current validated architecture"
    $docArchHasSubstantialCuratedContent = ($existingDocArchLineCount -gt 30) -and -not $docArchHasPlaceholder -and -not $docArchLooksAutoGenerated
    $docArchShouldWrite = [string]::IsNullOrWhiteSpace($existingDocArch) `
        -or $docArchHasPlaceholder `
        -or $docArchLooksAutoGenerated `
        -or (-not $docArchHasSubstantialCuratedContent)
    if ($docArchShouldWrite) {
        Write-V2File -Path $documentationArchitecturePath -Content ($docArchitecture -join [Environment]::NewLine) -Force
    }

    $dependencyLines = New-Object System.Collections.Generic.List[string]
    $dependencyLines.Add("# Dependency Graph")
    $dependencyLines.Add("")
    $dependencyLines.Add("- Generated At: $(Get-V2Timestamp)")
    $dependencyLines.Add("- Detection Mode: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'detection_mode' -DefaultValue 'heuristic')")
    $dependencyLines.Add("- Detection Reason: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'detection_reason' -DefaultValue 'n/a')")
    $dependencyLines.Add("- Heuristic Edge Count: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'heuristic_edge_count' -DefaultValue 0)")
    $dependencyLines.Add("- Semantic Edge Count: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'semantic_edge_count' -DefaultValue 0)")
    $dependencyLines.Add("- Module Count: $(@(Get-V2OptionalProperty -InputObject $depGraph -Name 'modules' -DefaultValue @()).Count)")
    $dependencyLines.Add("- Edge Count: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'edge_count' -DefaultValue 0)")
    $dependencyLines.Add("- Cycle Detected: $(Get-V2OptionalProperty -InputObject $depGraph -Name 'cycle_detected' -DefaultValue $false)")
    $dependencyLines.Add("")
    $dependencyLines.Add("## Edges")
    $edgeEntries = @(Get-V2OptionalProperty -InputObject $depGraph -Name "edges" -DefaultValue @())
    if ($edgeEntries.Count -eq 0) {
        $dependencyLines.Add("- none")
    }
    else {
        foreach ($edge in @($edgeEntries | Select-Object -First 300)) {
            $dependencyLines.Add("- $($edge.source) -> $($edge.target)")
        }
    }
    Write-V2File -Path $dependencyPath -Content ($dependencyLines -join [Environment]::NewLine) -Force
    Write-V2File -Path $servicesMapPath -Content ($dependencyLines -join [Environment]::NewLine) -Force

    $qualityLines = New-Object System.Collections.Generic.List[string]
    $qualityLines.Add("# Code Quality")
    $qualityLines.Add("")
    $qualityLines.Add("- Generated At: $(Get-V2Timestamp)")
    $qualityLines.Add("- Code Files: $(Get-V2OptionalProperty -InputObject $quality -Name 'code_file_count' -DefaultValue 0)")
    $qualityLines.Add("- Complexity Proxy: $(Get-V2OptionalProperty -InputObject $quality -Name 'complexity_proxy' -DefaultValue 0)")
    $qualityLines.Add("- Complexity Rating: $(Get-V2OptionalProperty -InputObject $quality -Name 'complexity_rating' -DefaultValue 'unknown')")
    $qualityLines.Add("- Large Files: $(Get-V2OptionalProperty -InputObject $quality -Name 'large_file_count' -DefaultValue 0)")
    $qualityLines.Add("- Duplicate Groups: $(Get-V2OptionalProperty -InputObject $quality -Name 'duplicate_group_count' -DefaultValue 0)")
    $qualityLines.Add("- Dead Code Candidates: $(Get-V2OptionalProperty -InputObject $quality -Name 'dead_code_count' -DefaultValue 0)")
    $qualityLines.Add("- Vulnerability Signals: $(Get-V2OptionalProperty -InputObject $quality -Name 'vulnerability_count' -DefaultValue 0)")
    $qualityLines.Add("")
    $qualityLines.Add("## Vulnerability Signals")
    $vulnerabilitySignals = @(Get-V2OptionalProperty -InputObject $quality -Name "vulnerability_signals" -DefaultValue @())
    if ($vulnerabilitySignals.Count -eq 0) {
        $qualityLines.Add("- none")
    }
    else {
        foreach ($signal in @($vulnerabilitySignals | Select-Object -First 120)) {
            $qualityLines.Add("- $signal")
        }
    }
    Write-V2File -Path $qualityPath -Content ($qualityLines -join [Environment]::NewLine) -Force
}

function Append-V2SubmissionLogs {
    param(
        [string]$OrchestratorRoot,
        [object]$Intake,
        [string]$DockerStatus
    )

    $executionHistoryPath = Join-Path $OrchestratorRoot "tasks/execution-history.md"
    Add-V2MarkdownLog -Path $executionHistoryPath -Header "# Execution History" -Lines @(
        "## $(Get-V2Timestamp)",
        "- Event: submit",
        "- Type: $(Get-V2OptionalProperty -InputObject $Intake -Name 'project_type' -DefaultValue 'unknown')",
        "- Status: $(Get-V2OptionalProperty -InputObject $Intake -Name 'status' -DefaultValue 'unknown')",
        "- Docker: $DockerStatus"
    )

    $messagePath = Join-Path $OrchestratorRoot "communication/messages.md"
    Add-V2MarkdownLog -Path $messagePath -Header "# Agent Messages" -Lines @(
        "## $(Get-V2Timestamp)",
        "- from: UniversalOrchestratorV2",
        "- to: multi-agent-system",
        "- message: intake completed and orchestration state updated"
    )
}

function Invoke-V2360ArtifactsGeneration {
    param([string]$ProjectPath)

    $generatorScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Generate-Orchestrator360Artifacts.ps1"
    if (-not (Test-Path -LiteralPath $generatorScript -PathType Leaf)) {
        return [PSCustomObject]@{
            success = $false
            status  = "missing-script"
            details = "Generate-Orchestrator360Artifacts.ps1 not found"
            paths   = [PSCustomObject]@{}
        }
    }

    try {
        $raw = & $generatorScript -ProjectPath $ProjectPath 2>&1 | Out-String
        $parsed = $null
        try {
            $parsed = ($raw | ConvertFrom-Json)
        }
        catch {
            $parsed = $null
        }

        if ($parsed -and [bool](Get-V2OptionalProperty -InputObject $parsed -Name "success" -DefaultValue $false)) {
            return [PSCustomObject]@{
                success = $true
                status  = "generated"
                details = ""
                paths   = Get-V2OptionalProperty -InputObject $parsed -Name "paths" -DefaultValue ([PSCustomObject]@{})
            }
        }

        return [PSCustomObject]@{
            success = $false
            status  = "generation-failed"
            details = if ([string]::IsNullOrWhiteSpace($raw)) { "unknown-error" } else { ($raw.Trim() -replace "\r?\n", " | ") }
            paths   = [PSCustomObject]@{}
        }
    }
    catch {
        return [PSCustomObject]@{
            success = $false
            status  = "generation-failed"
            details = $_.Exception.Message
            paths   = [PSCustomObject]@{}
        }
    }
}

function Set-V2TaskSeeds {
    param(
        [string]$OrchestratorRoot,
        [object]$Intake,
        [bool]$DockerRequested,
        [string]$RequestedStack = ""
    )

    $taskDagPath = Join-Path $OrchestratorRoot "tasks/task-dag.md"
    $taskDagJsonPath = Join-Path $OrchestratorRoot "tasks/task-dag.json"
    $backlogPath = Join-Path $OrchestratorRoot "tasks/backlog.md"
    $inProgressPath = Join-Path $OrchestratorRoot "tasks/in-progress.md"
    $completedPath = Join-Path $OrchestratorRoot "tasks/completed.md"

    $locksPath = Join-Path $OrchestratorRoot "locks/locks.json"
    $planTaskFiles = @(
        "ai-orchestrator/documentation/architecture.md",
        "ai-orchestrator/tasks/backlog.md"
    )
    $dockerTaskFiles = @(
        "ai-orchestrator/docker/docker-compose.generated.yml",
        "ai-orchestrator/docker/app.Dockerfile.generated"
    )
    $analysisTaskFiles = @(
        "ai-orchestrator/analysis/architecture-report.md",
        "ai-orchestrator/analysis/dependency-graph.md",
        "ai-orchestrator/analysis/code-quality.md"
    )
    $legacyGateFiles = @(
        "ai-orchestrator/state/project-state.json",
        "ai-orchestrator/state/open-questions.md"
    )

    $planTaskStatus = "pending"
    if ((Test-V2LockConflict -FilesAffected $planTaskFiles -LocksPath $locksPath).has_conflict) {
        $planTaskStatus = "blocked-lock-conflict"
    }

    $dockerTaskStatus = if ($DockerRequested) { "pending" } else { "skipped" }
    if ($dockerTaskStatus -eq "pending" -and (Test-V2LockConflict -FilesAffected $dockerTaskFiles -LocksPath $locksPath).has_conflict) {
        $dockerTaskStatus = "blocked-lock-conflict"
    }

    $analysisTaskStatus = "pending"
    if ((Test-V2LockConflict -FilesAffected $analysisTaskFiles -LocksPath $locksPath).has_conflict) {
        $analysisTaskStatus = "blocked-lock-conflict"
    }

    $legacyGateStatus = if ($Intake.project_type -eq "legacy" -and $Intake.refactor_policy -eq "unknown") {
        "blocked"
    }
    elseif ($Intake.project_type -eq "legacy") {
        "pending"
    }
    else {
        "skipped"
    }
    if ($legacyGateStatus -eq "pending" -and (Test-V2LockConflict -FilesAffected $legacyGateFiles -LocksPath $locksPath).has_conflict) {
        $legacyGateStatus = "blocked-lock-conflict"
    }

    $timestamp = Get-V2Timestamp
    $taskItems = @(
        [PSCustomObject]@{
            id              = "V2-INTAKE-001"
            description     = "Validate technical fingerprint and unresolved unknowns"
            priority        = "P0"
            dependencies    = @()
            preferred_agent = "AI Architect"
            assigned_agent  = "AI Architect"
            status          = "done"
            files_affected  = @("ai-orchestrator/state/intake-report.md")
            created_at      = $timestamp
            updated_at      = $timestamp
            completed_at    = $timestamp
        }
        [PSCustomObject]@{
            id              = "V2-PLAN-001"
            description     = "Build module and architecture execution plan"
            priority        = "P0"
            dependencies    = @("V2-INTAKE-001")
            preferred_agent = "AI Product Manager"
            assigned_agent  = "AI Product Manager"
            status          = $planTaskStatus
            execution_mode  = "artifact-validation"
            files_affected  = @("ai-orchestrator/documentation/architecture.md", "ai-orchestrator/tasks/backlog.md")
            created_at      = $timestamp
            updated_at      = $timestamp
        }
        [PSCustomObject]@{
            id              = "V2-ANALYSIS-001"
            description     = "Validate dependency graph and code quality outputs"
            priority        = "P0"
            dependencies    = @("V2-INTAKE-001")
            preferred_agent = "AI Architect"
            assigned_agent  = "AI Architect"
            status          = $analysisTaskStatus
            execution_mode  = "artifact-validation"
            files_affected  = @(
                "ai-orchestrator/analysis/architecture-report.md",
                "ai-orchestrator/analysis/dependency-graph.md",
                "ai-orchestrator/analysis/code-quality.md"
            )
            created_at      = $timestamp
            updated_at      = $timestamp
        }
        [PSCustomObject]@{
            id              = "V2-DOCKER-001"
            description     = "Generate isolated runtime infrastructure"
            priority        = "P1"
            dependencies    = @("V2-INTAKE-001")
            preferred_agent = "AI DevOps Engineer"
            assigned_agent  = "AI DevOps Engineer"
            status          = $dockerTaskStatus
            execution_mode  = "artifact-validation"
            files_affected  = @("ai-orchestrator/docker/docker-compose.generated.yml", "ai-orchestrator/docker/app.Dockerfile.generated")
            created_at      = $timestamp
            updated_at      = $timestamp
        }
        [PSCustomObject]@{
            id              = "V2-LEGACY-GATE-001"
            description     = "Confirm refactor policy before structural refactor"
            priority        = "P0"
            dependencies    = @("V2-INTAKE-001")
            preferred_agent = "AI CTO"
            assigned_agent  = "AI CTO"
            status          = $legacyGateStatus
            files_affected  = @("ai-orchestrator/state/project-state.json", "ai-orchestrator/state/open-questions.md")
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    )

    $projectRoot = Split-Path -Parent $OrchestratorRoot
    $fingerprint = Get-V2OptionalProperty -InputObject $Intake -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
    $primaryLanguage = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "")
    $projectType = [string](Get-V2OptionalProperty -InputObject $Intake -Name "project_type" -DefaultValue "")
    $isGreenfield = $projectType -in @("greenfield", "new")
    $allowFunctionalFeatureSeeding = $projectType -in @("greenfield", "new", "existing")

    $normalizedRequestedStack = ([string]$RequestedStack).Trim().ToLowerInvariant()
    $effectiveLanguage = $primaryLanguage.Trim().ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($effectiveLanguage) -or $effectiveLanguage -eq "unknown") {
        if (-not [string]::IsNullOrWhiteSpace($normalizedRequestedStack) -and $normalizedRequestedStack -notin @("auto", "unknown")) {
            $effectiveLanguage = $normalizedRequestedStack
        }
    }
    $isPython = $effectiveLanguage -eq "python"
    $stackUnknown = $isGreenfield -and ([string]::IsNullOrWhiteSpace($effectiveLanguage) -or $effectiveLanguage -eq "unknown")

    $docArchitecturePath = Join-Path $OrchestratorRoot "documentation/architecture.md"
    $docArchitectureContent = if (Test-Path -LiteralPath $docArchitecturePath -PathType Leaf) { Get-Content -LiteralPath $docArchitecturePath -Raw } else { "" }
    $docNeedsFill = [string]::IsNullOrWhiteSpace($docArchitectureContent) -or $docArchitectureContent -match "This file should always reflect the current validated architecture"

    $businessContextJsonPath = Join-Path $OrchestratorRoot "context/business-context.json"
    $businessContextMarkdownPath = Join-Path $OrchestratorRoot "context/business-context.md"
    $architectureAdrPath = Join-Path $OrchestratorRoot "documentation/adr/ADR-0001-context-driven-architecture.md"
    $interfaceContractsPath = Join-Path $OrchestratorRoot "documentation/interfaces/contracts.md"
    $codeReviewChecklistPath = Join-Path $OrchestratorRoot "reports/code-review-checklist.md"
    $productValidationChecklistPath = Join-Path $OrchestratorRoot "reports/product-validation-checklist.md"
    $userSimulationPlanPath = Join-Path $OrchestratorRoot "reports/user-simulation-plan.md"
    $observabilityPlanPath = Join-Path $OrchestratorRoot "reports/production-observability-plan.md"

    $missingBusinessContext = -not (
        (Test-Path -LiteralPath $businessContextJsonPath -PathType Leaf) -and
        (Test-Path -LiteralPath $businessContextMarkdownPath -PathType Leaf)
    )
    $missingArchitectureAdr = -not (Test-Path -LiteralPath $architectureAdrPath -PathType Leaf)
    $missingInterfaceContracts = -not (Test-Path -LiteralPath $interfaceContractsPath -PathType Leaf)
    $missingCodeReviewChecklist = -not (Test-Path -LiteralPath $codeReviewChecklistPath -PathType Leaf)
    $missingProductValidationChecklist = -not (Test-Path -LiteralPath $productValidationChecklistPath -PathType Leaf)
    $missingUserSimulationPlan = -not (Test-Path -LiteralPath $userSimulationPlanPath -PathType Leaf)
    $missingObservabilityPlan = -not (Test-Path -LiteralPath $observabilityPlanPath -PathType Leaf)

    $webRoutesPath = Join-Path $projectRoot "routes/web.php"
    $webRoutesContent = if (Test-Path -LiteralPath $webRoutesPath -PathType Leaf) { Get-Content -LiteralPath $webRoutesPath -Raw } else { "" }
    $webRoutesStillWelcomeOnly = $webRoutesContent -match "return\s+view\('welcome'\)"

    $frontendPagesPath = Join-Path $projectRoot "resources/js/Pages"
    $frontendPagesCount = if (Test-Path -LiteralPath $frontendPagesPath -PathType Container) {
        @(Get-ChildItem -LiteralPath $frontendPagesPath -File -Recurse -ErrorAction SilentlyContinue).Count
    }
    else { 0 }

    $viewsPath = Join-Path $projectRoot "resources/views"
    $frontendBladeModuleCount = 0
    if (Test-Path -LiteralPath $viewsPath -PathType Container) {
        $frontendBladeModuleCount = @(
            Get-ChildItem -LiteralPath $viewsPath -File -Recurse -Filter "*.blade.php" -ErrorAction SilentlyContinue |
            Where-Object {
                $normalized = ([string]$_.FullName).Replace("\", "/").ToLowerInvariant()
                $normalized -notlike "*/resources/views/welcome.blade.php" -and
                $normalized -notlike "*/resources/views/receipts/*" -and
                $normalized -notlike "*/resources/views/emails/*"
            }
        ).Count
    }

    $requiredWebModuleViews = @(
        (Join-Path $viewsPath "dashboard/index.blade.php"),
        (Join-Path $viewsPath "patients/index.blade.php"),
        (Join-Path $viewsPath "appointments/index.blade.php"),
        (Join-Path $viewsPath "records/index.blade.php"),
        (Join-Path $viewsPath "financial/index.blade.php"),
        (Join-Path $viewsPath "documents/index.blade.php")
    )
    $frontendModuleViewQualityOk = $true
    foreach ($moduleView in $requiredWebModuleViews) {
        if (-not (Test-Path -LiteralPath $moduleView -PathType Leaf)) {
            $frontendModuleViewQualityOk = $false
            break
        }
        $moduleContent = Get-Content -LiteralPath $moduleView -Raw -ErrorAction SilentlyContinue
        $moduleLines = @(($moduleContent -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($moduleLines.Count -lt 6) {
            $frontendModuleViewQualityOk = $false
            break
        }
    }

    $frontendDelivered = (($frontendPagesCount -gt 0) -or ($frontendBladeModuleCount -gt 0)) -and $frontendModuleViewQualityOk
    $missingFrontendProductDelivery = $allowFunctionalFeatureSeeding -and ($webRoutesStillWelcomeOnly -or -not $frontendDelivered)

    $whatsappServicePath = Join-Path $projectRoot "app/Services/WhatsappReminderService.php"
    $servicesConfigPath = Join-Path $projectRoot "config/services.php"
    $servicesConfigContent = if (Test-Path -LiteralPath $servicesConfigPath -PathType Leaf) { Get-Content -LiteralPath $servicesConfigPath -Raw } else { "" }
    $whatsappServiceContent = if (Test-Path -LiteralPath $whatsappServicePath -PathType Leaf) { Get-Content -LiteralPath $whatsappServicePath -Raw } else { "" }
    $hasWhatsappDispatchLog = $whatsappServiceContent -match 'Log::info\((["'']?)whatsapp-reminder-dispatched\1'
    $returnsAlwaysTrue = $whatsappServiceContent -match 'return\s+true\s*;'
    $whatsappLooksMock = $hasWhatsappDispatchLog -and $returnsAlwaysTrue
    $whatsappConfigured = (Test-Path -LiteralPath $whatsappServicePath -PathType Leaf) -and ($servicesConfigContent -match "'whatsapp'\s*=>") -and -not $whatsappLooksMock
    $missingWhatsAppIntegration = $allowFunctionalFeatureSeeding -and (-not $whatsappConfigured)

    $backupRunbookPath = Join-Path $projectRoot "docs/runbooks/backup-disaster-recovery.md"
    $backupRunbookQualityOk = $false
    if (Test-Path -LiteralPath $backupRunbookPath -PathType Leaf) {
        $backupRunbookContent = Get-Content -LiteralPath $backupRunbookPath -Raw -ErrorAction SilentlyContinue
        $backupRunbookLines = @(($backupRunbookContent -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $backupRunbookQualityOk = ($backupRunbookLines.Count -ge 15) -and ($backupRunbookContent -match "(?i)rpo") -and ($backupRunbookContent -match "(?i)rto") -and ($backupRunbookContent -match "(?i)(restore|restaur|recovery)")
    }
    $missingBackupRunbook = $allowFunctionalFeatureSeeding -and (-not $backupRunbookQualityOk)

    $e2eRegressionTestPath = Join-Path $projectRoot "tests/Feature/PsychologyEndToEndFlowTest.php"
    $e2eRegressionQualityOk = $false
    if (Test-Path -LiteralPath $e2eRegressionTestPath -PathType Leaf) {
        $e2eRegressionTestContent = Get-Content -LiteralPath $e2eRegressionTestPath -Raw -ErrorAction SilentlyContinue
        $e2eRegressionLines = @(($e2eRegressionTestContent -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $assertCount = ([regex]::Matches($e2eRegressionTestContent, "assert[A-Za-z]+")).Count
        $coversAuth = $e2eRegressionTestContent -match "(?i)auth|login|register|sanctum"
        $coversCoreDomain = $e2eRegressionTestContent -match "(?i)patients|appointments|medical|payments|documents"
        $e2eRegressionQualityOk = ($e2eRegressionLines.Count -ge 30) -and ($assertCount -ge 8) -and $coversAuth -and $coversCoreDomain
    }
    $missingProductE2ERegression = $allowFunctionalFeatureSeeding -and (-not $e2eRegressionQualityOk)

    $composeGeneratedPath = Join-Path $OrchestratorRoot "docker/docker-compose.generated.yml"
    $composeNeedsFix = $false
    if (Test-Path -LiteralPath $composeGeneratedPath -PathType Leaf) {
        $composeContent = Get-Content -LiteralPath $composeGeneratedPath -Raw
        $composeNeedsFix = $composeContent -match "REVIEW_REQUIRED"
    }

    $envExamplePath = Join-Path $projectRoot ".env.example"
    $missingEnvExample = -not (Test-Path -LiteralPath $envExamplePath -PathType Leaf)

    $verifiedCommands = Get-V2OptionalProperty -InputObject $Intake -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    $buildCommandValue = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verifiedCommands -Name "build" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "unknown")
    $testCommandValue = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verifiedCommands -Name "test" -DefaultValue ([PSCustomObject]@{})) -Name "value" -DefaultValue "unknown")
    $buildConfidence = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verifiedCommands -Name "build" -DefaultValue ([PSCustomObject]@{})) -Name "confidence" -DefaultValue "")
    $testConfidence = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $verifiedCommands -Name "test" -DefaultValue ([PSCustomObject]@{})) -Name "confidence" -DefaultValue "")
    $missingVerifiedCommands = [string]::IsNullOrWhiteSpace($buildCommandValue) -or $buildCommandValue -eq "unknown" -or `
        [string]::IsNullOrWhiteSpace($testCommandValue) -or $testCommandValue -eq "unknown" -or `
        $buildConfidence -ne "verified" -or $testConfidence -ne "verified"

    $missingMigrationScaffold = $isPython -and -not (Test-Path -LiteralPath (Join-Path $projectRoot "alembic.ini") -PathType Leaf)
    $missingApiTests = $isPython -and -not (Test-Path -LiteralPath (Join-Path $projectRoot "tests/test_api.py") -PathType Leaf)
    $missingWorker = $isPython -and -not (Test-Path -LiteralPath (Join-Path $projectRoot "app/worker.py") -PathType Leaf)
    $missingApiScaffold = $isPython -and -not (Test-Path -LiteralPath (Join-Path $projectRoot "app/main.py") -PathType Leaf)
    $missingDomainScaffold = $isPython -and (
        -not (Test-Path -LiteralPath (Join-Path $projectRoot "app/models.py") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $projectRoot "app/schemas.py") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $projectRoot "app/services.py") -PathType Leaf)
    )

    if ($stackUnknown) {
        $taskFiles = @("PROJECT_REQUEST.md", "README.md", "ai-orchestrator/state/project-state.json")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-STACK-DECISION-001"
            description     = "Define runtime stack/framework and update verified build/test commands for scaffold generation"
            priority        = "P0"
            dependencies    = @("V2-PLAN-001")
            preferred_agent = "AI CTO"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingVerifiedCommands) {
        $taskFiles = @("ai-orchestrator/state/project-state.json", "PROJECT_REQUEST.md")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $dependencies = @("V2-INTAKE-001")
        if ($stackUnknown) {
            $dependencies += "DEV-STACK-DECISION-001"
        }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-VERIFY-COMMANDS-001"
            description     = "Resolve and verify mandatory build/test commands (no unknown allowed for CORE-COMPLETE-001)"
            priority        = "P0"
            dependencies    = @($dependencies)
            preferred_agent = "AI Architect"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($isGreenfield -and $isPython -and $missingApiScaffold) {
        $taskFiles = @("app/main.py", "app/__init__.py", "requirements.txt")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $dependencies = @("V2-PLAN-001", "V2-ANALYSIS-001")
        if ($missingVerifiedCommands) { $dependencies += "DEV-VERIFY-COMMANDS-001" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-SCAFFOLD-API-001"
            description     = "Scaffold runnable API entrypoint and base package for greenfield project"
            priority        = "P0"
            dependencies    = @($dependencies)
            preferred_agent = "Codex"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($isGreenfield -and $isPython -and $missingDomainScaffold) {
        $taskFiles = @("app/models.py", "app/schemas.py", "app/services.py")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $dependencies = @("V2-ANALYSIS-001")
        if ($missingApiScaffold) { $dependencies += "DEV-SCAFFOLD-API-001" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-SCAFFOLD-DOMAIN-001"
            description     = "Create core domain layer (models, schemas, services) for greenfield baseline"
            priority        = "P0"
            dependencies    = @($dependencies)
            preferred_agent = "Codex"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingMigrationScaffold) {
        $taskFiles = @("alembic.ini", "alembic/env.py", "alembic/versions/0001_initial_schema.py", "requirements.txt")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-MIGRATION-001"
            description     = "Add Alembic migrations and initial schema for SQLAlchemy models"
            priority        = "P0"
            dependencies    = @("V2-PLAN-001", "V2-ANALYSIS-001")
            preferred_agent = "Codex"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingApiTests) {
        $taskFiles = @("tests/test_api.py", "tests/conftest.py")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $deps = @("V2-ANALYSIS-001")
        if ($missingMigrationScaffold) { $deps += "DEV-MIGRATION-001" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-TEST-API-001"
            description     = "Add API integration tests for CRUD and domain endpoints"
            priority        = "P1"
            dependencies    = $deps
            preferred_agent = "Antigravity"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingWorker) {
        $taskFiles = @("app/worker.py", "ai-orchestrator/docker/docker-compose.generated.yml")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $deps = @("V2-ANALYSIS-001")
        if ($missingMigrationScaffold) { $deps += "DEV-MIGRATION-001" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-WORKER-001"
            description     = "Add periodic worker to sync schedules and escalate missed doses"
            priority        = "P1"
            dependencies    = $deps
            preferred_agent = "Codex"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($isGreenfield -and -not $isPython) {
        $genericApiFiles = @("src/main", "src/api", "README.md")
        $genericApiStatus = if ((Test-V2LockConflict -FilesAffected $genericApiFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $genericApiDeps = @("V2-PLAN-001", "V2-ANALYSIS-001")
        if ($stackUnknown) { $genericApiDeps += "DEV-STACK-DECISION-001" }
        if ($missingVerifiedCommands) { $genericApiDeps += "DEV-VERIFY-COMMANDS-001" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-SCAFFOLD-API-001"
            description     = "Scaffold runtime API/service entrypoint for selected stack (greenfield baseline)"
            priority        = "P0"
            dependencies    = @($genericApiDeps)
            preferred_agent = "AI Developer"
            assigned_agent  = ""
            status          = $genericApiStatus
            execution_mode  = "external-agent"
            files_affected  = $genericApiFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }

        $genericMigrationFiles = @("migrations/", "db/", "README.md")
        $genericMigrationStatus = if ((Test-V2LockConflict -FilesAffected $genericMigrationFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-MIGRATION-001"
            description     = "Create migration scaffold and baseline schema for selected stack"
            priority        = "P0"
            dependencies    = @("DEV-SCAFFOLD-API-001")
            preferred_agent = "AI Developer"
            assigned_agent  = ""
            status          = $genericMigrationStatus
            execution_mode  = "external-agent"
            files_affected  = $genericMigrationFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }

        $genericTestFiles = @("tests/", "test/", "README.md")
        $genericTestStatus = if ((Test-V2LockConflict -FilesAffected $genericTestFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-TEST-API-001"
            description     = "Create API integration tests for baseline CRUD/health routes"
            priority        = "P1"
            dependencies    = @("DEV-SCAFFOLD-API-001", "DEV-MIGRATION-001")
            preferred_agent = "AI QA"
            assigned_agent  = ""
            status          = $genericTestStatus
            execution_mode  = "external-agent"
            files_affected  = $genericTestFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }

        $genericWorkerFiles = @("worker/", "scheduler/", "README.md")
        $genericWorkerStatus = if ((Test-V2LockConflict -FilesAffected $genericWorkerFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-WORKER-001"
            description     = "Implement worker/scheduler loop for periodic jobs and notifications"
            priority        = "P1"
            dependencies    = @("DEV-SCAFFOLD-API-001")
            preferred_agent = "AI Developer"
            assigned_agent  = ""
            status          = $genericWorkerStatus
            execution_mode  = "external-agent"
            files_affected  = $genericWorkerFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($composeNeedsFix) {
        $taskFiles = @("ai-orchestrator/docker/docker-compose.generated.yml")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-DOCKER-FIX-001"
            description     = "Fix generated docker compose command placeholders to runnable commands"
            priority        = "P0"
            dependencies    = @("V2-DOCKER-001")
            preferred_agent = "AI DevOps Engineer"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingEnvExample) {
        $taskFiles = @(".env.example")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-ENV-EXAMPLE-001"
            description     = "Create .env.example with local runtime and database variables"
            priority        = "P2"
            dependencies    = @("V2-DOCKER-001")
            preferred_agent = "Antigravity"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($docNeedsFill) {
        $taskFiles = @("ai-orchestrator/documentation/architecture.md")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "DEV-ARCH-DOC-001"
            description     = "Populate architecture documentation from intake and dependency evidence"
            priority        = "P2"
            dependencies    = @("V2-PLAN-001", "V2-ANALYSIS-001")
            preferred_agent = "AI Architect"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    $taskFiles = @("ai-orchestrator/context/business-context.json", "ai-orchestrator/context/business-context.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "PLAN-BUSINESS-CONTEXT-001"
        description     = "Generate business context model (problem, personas, constraints, definition-of-done)"
        priority        = "P0"
        dependencies    = @("V2-INTAKE-001", "V2-PLAN-001")
        preferred_agent = "AI Product Manager"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/documentation/adr/ADR-0001-context-driven-architecture.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "PLAN-ARCH-ADR-001"
        description     = "Write architecture decision record from business context and technical fingerprint"
        priority        = "P0"
        dependencies    = @("PLAN-BUSINESS-CONTEXT-001", "V2-ANALYSIS-001")
        preferred_agent = "AI Architect"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/documentation/interfaces/contracts.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "PLAN-INTERFACE-CONTRACTS-001"
        description     = "Define API/event contracts for cross-agent implementation consistency"
        priority        = "P1"
        dependencies    = @("PLAN-ARCH-ADR-001")
        preferred_agent = "AI Architect"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/reports/code-review-checklist.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "QA-CODE-REVIEW-BASELINE-001"
        description     = "Create code-review baseline (technical + security + business checks)"
        priority        = "P1"
        dependencies    = @("PLAN-ARCH-ADR-001", "PLAN-INTERFACE-CONTRACTS-001")
        preferred_agent = "AI QA"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/reports/product-validation-checklist.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "QA-PRODUCT-VALIDATION-BASELINE-001"
        description     = "Create product validation baseline with requirement traceability and release risk"
        priority        = "P1"
        dependencies    = @("PLAN-BUSINESS-CONTEXT-001")
        preferred_agent = "AI Product Manager"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/reports/user-simulation-plan.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "QA-USER-SIMULATION-BASELINE-001"
        description     = "Define happy-path and adversarial user simulation plan"
        priority        = "P1"
        dependencies    = @("QA-PRODUCT-VALIDATION-BASELINE-001")
        preferred_agent = "AI QA"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $taskFiles = @("ai-orchestrator/reports/production-observability-plan.md")
    $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "OPS-OBSERVABILITY-BASELINE-001"
        description     = "Define production observability plan and automatic repair-task policy"
        priority        = "P1"
        dependencies    = @("PLAN-BUSINESS-CONTEXT-001", "PLAN-INTERFACE-CONTRACTS-001")
        preferred_agent = "AI DevOps Engineer"
        assigned_agent  = ""
        status          = $taskStatus
        execution_mode  = "external-agent"
        files_affected  = $taskFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    # Core completion gate: project is only considered complete when baseline quality/runtime is healthy.
    # Feature tasks should be scheduled only after this task is done.
    $coreGateFiles = @(
        "ai-orchestrator/state/project-state.json",
        "ai-orchestrator/state/health-report.json",
        "ai-orchestrator/documentation/architecture.md"
    )
    $coreGateStatus = if ((Test-V2LockConflict -FilesAffected $coreGateFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $coreDependencies = @(
        @($taskItems | ForEach-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") }) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and ($_ -match "^(V2-|DEV-|PLAN-|QA-|OPS-)") }
    )
    $taskItems += [PSCustomObject]@{
        id              = "CORE-COMPLETE-001"
        description     = "Validate project baseline completion (runtime, migrations, tests, health) before feature phase"
        priority        = "P0"
        dependencies    = @($coreDependencies)
        preferred_agent = "AI CTO"
        assigned_agent  = ""
        status          = $coreGateStatus
        execution_mode  = "project-completion-gate"
        files_affected  = $coreGateFiles
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    $releaseTaskFiles = @(
        "ai-orchestrator/release/release-config.json",
        "ai-orchestrator/reports"
    )
    $releaseTaskStatus = if ((Test-V2LockConflict -FilesAffected $releaseTaskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
    $taskItems += [PSCustomObject]@{
        id              = "REL-STAGING-001"
        description     = "Execute staged release pipeline with smoke validation"
        priority        = "P1"
        dependencies    = @("CORE-COMPLETE-001")
        preferred_agent = "AI DevOps Engineer"
        assigned_agent  = ""
        status          = $releaseTaskStatus
        execution_mode  = "external-agent"
        files_affected  = @("ai-orchestrator/release/release-config.json", "ai-orchestrator/reports")
        created_at      = $timestamp
        updated_at      = $timestamp
    }
    $taskItems += [PSCustomObject]@{
        id              = "REL-PROD-001"
        description     = "Execute production release pipeline with smoke validation and rollback readiness"
        priority        = "P1"
        dependencies    = @("REL-STAGING-001")
        preferred_agent = "AI DevOps Engineer"
        assigned_agent  = ""
        status          = $releaseTaskStatus
        execution_mode  = "external-agent"
        files_affected  = @("ai-orchestrator/release/release-config.json", "ai-orchestrator/reports")
        created_at      = $timestamp
        updated_at      = $timestamp
    }

    if ($missingFrontendProductDelivery) {
        $taskFiles = @("routes/web.php", "resources/views", "resources/js/Pages")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "FEAT-FRONTEND-WEB-001"
            description     = "Deliver production web UI for dashboard, patients, agenda, records and financeiro modules"
            priority        = "P0"
            dependencies    = @("CORE-COMPLETE-001")
            preferred_agent = "AI Frontend Engineer"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingWhatsAppIntegration) {
        $taskFiles = @("app/Services/WhatsappReminderService.php", "config/services.php", ".env.example")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "FEAT-INTEGRATION-WHATSAPP-001"
            description     = "Implement WhatsApp reminder channel integration with provider abstraction and fallback"
            priority        = "P1"
            dependencies    = @("CORE-COMPLETE-001")
            preferred_agent = "AI Integration Engineer"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingBackupRunbook) {
        $taskFiles = @("docs/runbooks/backup-disaster-recovery.md", "ai-orchestrator/reports/runtime-observability-report.json")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "FEAT-OPS-BACKUP-DR-001"
            description     = "Document and validate encrypted backup/disaster-recovery runbook with restore checklist"
            priority        = "P1"
            dependencies    = @("REL-PROD-001")
            preferred_agent = "AI DevOps Engineer"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    if ($missingProductE2ERegression) {
        $taskFiles = @("tests/Feature/PsychologyEndToEndFlowTest.php")
        $taskStatus = if ((Test-V2LockConflict -FilesAffected $taskFiles -LocksPath $locksPath).has_conflict) { "blocked-lock-conflict" } else { "pending" }
        $taskItems += [PSCustomObject]@{
            id              = "FEAT-QA-E2E-PRODUCT-001"
            description     = "Add end-to-end regression test covering complete product flow (auth, agenda, prontuario, financeiro, recibo)"
            priority        = "P1"
            dependencies    = @("FEAT-FRONTEND-WEB-001", "FEAT-INTEGRATION-WHATSAPP-001")
            preferred_agent = "AI QA"
            assigned_agent  = ""
            status          = $taskStatus
            execution_mode  = "external-agent"
            files_affected  = $taskFiles
            created_at      = $timestamp
            updated_at      = $timestamp
        }
    }

    # Merge with existing task-dag: preserve status/timestamps of tasks already completed or in-progress.
    # This prevents re-submit from resetting done tasks back to pending.
    $existingDag = Get-V2JsonContent -Path $taskDagJsonPath
    if ($existingDag -and ($existingDag.PSObject.Properties.Name -contains "tasks")) {
        $existingById = @{}
        foreach ($existingTask in @($existingDag.tasks)) {
            $existingId = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "id" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($existingId)) {
                $existingById[$existingId] = $existingTask
            }
        }
        $mergedItems = New-Object System.Collections.Generic.List[object]
        foreach ($newTask in @($taskItems)) {
            $newId = [string](Get-V2OptionalProperty -InputObject $newTask -Name "id" -DefaultValue "")
            if ($existingById.ContainsKey($newId)) {
                $prev = $existingById[$newId]
                $prevStatus = [string](Get-V2OptionalProperty -InputObject $prev -Name "status" -DefaultValue "")
                # Preserve non-pending task lifecycle state from existing DAG.
                # This avoids resetting tasks during re-submit.
                if (-not [string]::IsNullOrWhiteSpace($prevStatus) -and $prevStatus -ne "pending") {
                    Set-V2DynamicProperty -InputObject $newTask -Name "status"       -Value $prevStatus
                    Set-V2DynamicProperty -InputObject $newTask -Name "assigned_agent" -Value ([string](Get-V2OptionalProperty -InputObject $prev -Name "assigned_agent" -DefaultValue ""))
                    $completedAt = [string](Get-V2OptionalProperty -InputObject $prev -Name "completed_at" -DefaultValue "")
                    $startedAt   = [string](Get-V2OptionalProperty -InputObject $prev -Name "started_at"   -DefaultValue "")
                    if (-not [string]::IsNullOrWhiteSpace($completedAt)) {
                        Set-V2DynamicProperty -InputObject $newTask -Name "completed_at" -Value $completedAt
                    }
                    if (-not [string]::IsNullOrWhiteSpace($startedAt)) {
                        Set-V2DynamicProperty -InputObject $newTask -Name "started_at" -Value $startedAt
                    }
                }
                $existingById.Remove($newId) | Out-Null
            }
            $mergedItems.Add($newTask)
        }
        # Carry forward any tasks in the existing dag that are NOT bootstrap tasks and not in the new seed list
        # (e.g. REPAIR tasks, custom DEV tasks added by agents or observers)
        $bootstrapIds = @("V2-INTAKE-001", "V2-PLAN-001", "V2-ANALYSIS-001", "V2-DOCKER-001", "V2-LEGACY-GATE-001")
        foreach ($orphanId in @($existingById.Keys)) {
            if ($orphanId -notin $bootstrapIds) {
                $mergedItems.Add($existingById[$orphanId])
            }
        }
        $taskItems = @($mergedItems.ToArray())
    }

    foreach ($task in @($taskItems)) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($taskId)) { continue }
        $phase = ""
        $normalizedTaskId = $taskId.ToUpperInvariant()
        switch -Regex ($normalizedTaskId) {
            "^V2-INTAKE-" { $phase = "context"; break }
            "^V2-PLAN-" { $phase = "context"; break }
            "^V2-ANALYSIS-" { $phase = "context"; break }
            "^PLAN-BUSINESS-CONTEXT-" { $phase = "context"; break }
            "^PLAN-ARCH-" { $phase = "architecture"; break }
            "^PLAN-INTERFACE-" { $phase = "architecture"; break }
            "^QA-" { $phase = "architecture"; break }
            "^OPS-OBSERVABILITY-" { $phase = "architecture"; break }
            "^DEV-" { $phase = "execution"; break }
            "^FEAT-" { $phase = "execution"; break }
            "^CORE-COMPLETE-" { $phase = "execution"; break }
            "^REL-" { $phase = "release"; break }
            default { $phase = "" }
        }
        if (-not [string]::IsNullOrWhiteSpace($phase)) {
            Set-V2DynamicProperty -InputObject $task -Name "required_phase_approval" -Value $phase
        }
    }

    # Auto-close known DEV tasks when their artifact/condition is already satisfied.
    $autoResolvedByCondition = @{
        "DEV-STACK-DECISION-001"   = (-not $stackUnknown)
        "DEV-VERIFY-COMMANDS-001"  = (-not $missingVerifiedCommands)
        "DEV-SCAFFOLD-API-001"     = (-not $missingApiScaffold)
        "DEV-SCAFFOLD-DOMAIN-001"  = (-not $missingDomainScaffold)
        "DEV-MIGRATION-001"        = (-not $missingMigrationScaffold)
        "DEV-TEST-API-001"         = (-not $missingApiTests)
        "DEV-WORKER-001"           = (-not $missingWorker)
        "DEV-DOCKER-FIX-001"       = (-not $composeNeedsFix)
        "DEV-ENV-EXAMPLE-001"      = (-not $missingEnvExample)
        "DEV-ARCH-DOC-001"         = (
            (Test-Path -LiteralPath $docArchitecturePath -PathType Leaf) -and
            ((@(($docArchitectureContent -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count) -ge 8)
        )
        "PLAN-BUSINESS-CONTEXT-001" = (-not $missingBusinessContext)
        "PLAN-ARCH-ADR-001" = (-not $missingArchitectureAdr)
        "PLAN-INTERFACE-CONTRACTS-001" = (-not $missingInterfaceContracts)
        "QA-CODE-REVIEW-BASELINE-001" = (-not $missingCodeReviewChecklist)
        "QA-PRODUCT-VALIDATION-BASELINE-001" = (-not $missingProductValidationChecklist)
        "QA-USER-SIMULATION-BASELINE-001" = (-not $missingUserSimulationPlan)
        "OPS-OBSERVABILITY-BASELINE-001" = (-not $missingObservabilityPlan)
        "FEAT-FRONTEND-WEB-001" = (-not $missingFrontendProductDelivery)
        "FEAT-INTEGRATION-WHATSAPP-001" = (-not $missingWhatsAppIntegration)
        "FEAT-OPS-BACKUP-DR-001" = (-not $missingBackupRunbook)
        "FEAT-QA-E2E-PRODUCT-001" = (-not $missingProductE2ERegression)
    }
    foreach ($task in @($taskItems)) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if (-not $autoResolvedByCondition.ContainsKey($taskId)) { continue }
        if (-not [bool]$autoResolvedByCondition[$taskId]) { continue }

        $currentStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "pending")
        if ($currentStatus -notin @("done", "completed", "skipped")) {
            Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
            Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
            Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
            Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
            Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value "auto-resolved-by-artifact-check"
        }
    }

    # Auto-close core gate when baseline is fully validated.
    $taskIndexById = @{}
    foreach ($task in @($taskItems)) {
        $taskId = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($taskId)) {
            $taskIndexById[$taskId] = $task
        }
    }
    if ($taskIndexById.ContainsKey("CORE-COMPLETE-001")) {
        $coreTask = $taskIndexById["CORE-COMPLETE-001"]
        $dependenciesReady = $true
        foreach ($depId in @(Get-V2OptionalProperty -InputObject $coreTask -Name "dependencies" -DefaultValue @())) {
            $depIdText = [string]$depId
            if ([string]::IsNullOrWhiteSpace($depIdText)) { continue }
            if (-not $taskIndexById.ContainsKey($depIdText)) {
                $dependenciesReady = $false
                break
            }
            $depStatus = [string](Get-V2OptionalProperty -InputObject $taskIndexById[$depIdText] -Name "status" -DefaultValue "")
            if ($depStatus -notin @("done", "completed", "skipped")) {
                $dependenciesReady = $false
                break
            }
        }

        $projectStatePath = Join-Path $OrchestratorRoot "state/project-state.json"
        $projectStateDoc = Get-V2JsonContent -Path $projectStatePath
        $healthStatus = [string](Get-V2OptionalProperty -InputObject $projectStateDoc -Name "health_status" -DefaultValue "")
        $startupPackStatus = [string](Get-V2OptionalProperty -InputObject $projectStateDoc -Name "startup_pack_status" -DefaultValue "")
        $dockerStatus = [string](Get-V2OptionalProperty -InputObject $projectStateDoc -Name "docker_status" -DefaultValue "")
        $domainVerification = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $projectStateDoc -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})) -Name "relational_domain" -DefaultValue ([PSCustomObject]@{})
        $domainStatus = [string](Get-V2OptionalProperty -InputObject $domainVerification -Name "status" -DefaultValue "unknown")
        $domainReady = $domainStatus -in @("ready", "skipped")

        $coreReady = $dependenciesReady -and (-not $missingVerifiedCommands) -and `
            ($healthStatus -eq "healthy") -and ($startupPackStatus -eq "ready") -and ($dockerStatus -eq "ready") -and $domainReady

        if ($coreReady) {
            Set-V2DynamicProperty -InputObject $coreTask -Name "status" -Value "done"
            Set-V2DynamicProperty -InputObject $coreTask -Name "blocked_reason" -Value ""
            Set-V2DynamicProperty -InputObject $coreTask -Name "updated_at" -Value $timestamp
            Set-V2DynamicProperty -InputObject $coreTask -Name "completed_at" -Value $timestamp
            Set-V2DynamicProperty -InputObject $coreTask -Name "completion_note" -Value "auto-validated-core-baseline"
        }
        else {
            $coreCurrentStatus = [string](Get-V2OptionalProperty -InputObject $coreTask -Name "status" -DefaultValue "")
            if ($coreCurrentStatus -in @("done", "completed")) {
                Set-V2DynamicProperty -InputObject $coreTask -Name "status" -Value "pending"
                Set-V2DynamicProperty -InputObject $coreTask -Name "blocked_reason" -Value "core-prerequisites-not-ready"
                Set-V2DynamicProperty -InputObject $coreTask -Name "updated_at" -Value $timestamp
                Set-V2DynamicProperty -InputObject $coreTask -Name "completion_note" -Value "core-gate-reopened-after-regression"
            }
        }
    }

    $taskDocument = [PSCustomObject]@{
        generated_at = $timestamp
        updated_at   = $timestamp
        tasks        = $taskItems
    }
    Save-V2JsonContent -Path $taskDagJsonPath -Value $taskDocument

    $taskSyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Sync-TaskState.ps1"
    if (Test-Path -LiteralPath $taskSyncScript -PathType Leaf) {
        try {
            & $taskSyncScript -ProjectPath $projectRoot | Out-Null
        }
        catch {
            # fallback: keep canonical JSON saved even if markdown sync fails
            Write-Warning "Task markdown sync failed during seeding: $($_.Exception.Message)"
        }
    }
}

function Get-V2ProjectNameFromRequest {
    param([string]$ProjectRoot)

    $requestPath = Join-Path $ProjectRoot "PROJECT_REQUEST.md"
    if (-not (Test-Path -LiteralPath $requestPath -PathType Leaf)) {
        return ""
    }

    $content = Get-Content -LiteralPath $requestPath -Raw -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($content)) {
        return ""
    }

    $match = [regex]::Match($content, "(?ms)^##\s*Name\s*\r?\n(.+?)(?:\r?\n##|\Z)")
    if (-not $match.Success) {
        return ""
    }

    $name = $match.Groups[1].Value.Trim()
    if ([string]::IsNullOrWhiteSpace($name)) {
        return ""
    }
    if ($name -match "^\[.+\]$") {
        return ""
    }

    return $name
}

function Resolve-V2ProjectName {
    param(
        [string]$ProjectRoot,
        [string]$StatePath,
        [string]$ExplicitName = "",
        [switch]$PromptIfMissing
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitName)) {
        return $ExplicitName.Trim()
    }

    $state = Get-V2JsonContent -Path $StatePath
    if ($state) {
        $stateName = [string](Get-V2OptionalProperty -InputObject $state -Name "project_name" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($stateName)) {
            return $stateName.Trim()
        }
    }

    $requestName = Get-V2ProjectNameFromRequest -ProjectRoot $ProjectRoot
    if (-not [string]::IsNullOrWhiteSpace($requestName)) {
        return $requestName.Trim()
    }

    $fallbackName = [string](Split-Path -Leaf $ProjectRoot)
    if ($PromptIfMissing) {
        $inputName = Read-Host "Project name not defined. Inform project name (Enter keeps '$fallbackName')"
        if (-not [string]::IsNullOrWhiteSpace($inputName)) {
            return $inputName.Trim()
        }
    }

    return $fallbackName
}

function Get-V2DockerComposeEngine {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        $dockerConfigContext = $null
        try {
            $dockerConfigContext = Enter-V2DockerConfigFallbackContext
            $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                $composeProbe = @(& docker --config $dockerConfigDir compose version 2>&1)
            }
            else {
                $composeProbe = @(& docker compose version 2>&1)
            }
            $composeProbeText = (($composeProbe | Out-String).Trim())
            if ($LASTEXITCODE -eq 0) {
                return "docker-compose-v2"
            }
            if ($composeProbeText -and $composeProbeText -notmatch "(?i)is not a docker command|unknown command|no such command") {
                return "docker-compose-v2"
            }
        }
        catch {
        }
        finally {
            Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
        }
    }

    $dockerComposeCmd = Get-Command docker-compose -ErrorAction SilentlyContinue
    if ($dockerComposeCmd) {
        try {
            & docker-compose version *> $null
            if ($LASTEXITCODE -eq 0) {
                return "docker-compose-v1"
            }
        }
        catch {
        }
    }

    return "unavailable"
}

function Enter-V2DockerConfigFallbackContext {
    $dockerConfigMode = [string]$script:V2DockerConfigMode
    if ([string]::IsNullOrWhiteSpace($dockerConfigMode)) {
        $dockerConfigMode = "isolated"
    }
    $dockerConfigMode = $dockerConfigMode.Trim().ToLowerInvariant()

    $context = [PSCustomObject]@{
        fallback_used       = $false
        previous_exists     = $false
        previous_value      = ""
        previous_home_exists = $false
        previous_home_value  = ""
        previous_userprofile_exists = $false
        previous_userprofile_value  = ""
        selected_config_dir = ""
        selected_config_file = ""
        selected_home_dir   = ""
        reason              = ""
    }

    $context.previous_exists = Test-Path Env:DOCKER_CONFIG
    if ($context.previous_exists) {
        $context.previous_value = [string]$env:DOCKER_CONFIG
    }

    if ($dockerConfigMode -eq "user") {
        return $context
    }

    $configDir = if ($context.previous_exists) {
        [string]$env:DOCKER_CONFIG
    }
    else {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) { "" } else { Join-Path $env:USERPROFILE ".docker" }
    }

    $configFile = ""
    if (-not [string]::IsNullOrWhiteSpace($configDir)) {
        $configFile = Join-Path $configDir "config.json"
        $context.selected_config_dir = $configDir
        $context.selected_config_file = $configFile
    }
    $needsFallback = $false
    $forceIsolatedConfig = ($dockerConfigMode -eq "isolated")
    if ($forceIsolatedConfig) {
        # Isolated mode always runs docker commands with a temp config/home context.
        $needsFallback = $true
        $context.reason = "docker-config-isolated-context"
    }

    if (-not [string]::IsNullOrWhiteSpace($configFile)) {
        $configExists = $false
        try {
            $configExists = [System.IO.File]::Exists($configFile)
        }
        catch {
            $configExists = $false
        }
        if ($configExists) {
            $stream = $null
            try {
                $stream = [System.IO.File]::Open($configFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            }
            catch {
                $errorText = [string]$_.Exception.Message
                if ($errorText -match "(?i)access is denied|permission denied") {
                    $needsFallback = $true
                    $context.reason = "docker-config-access-denied"
                }
            }
            finally {
                if ($stream) { $stream.Dispose() }
            }
        }
        else {
            # If config file is missing but directory itself is inaccessible, force fallback.
            $dirProbe = $null
            try {
                $dirProbe = [System.IO.Directory]::GetFileSystemEntries($configDir)
            }
            catch {
                $errorText = [string]$_.Exception.Message
                if ($errorText -match "(?i)access is denied|permission denied") {
                    $needsFallback = $true
                    $context.reason = "docker-config-dir-access-denied"
                }
            }
            finally {
                $null = $dirProbe
            }
        }
    }

    if (-not $needsFallback) {
        return $context
    }

    $fallbackDir = Join-Path ([System.IO.Path]::GetTempPath()) "ai-orchestrator-docker-config"
    $fallbackHome = Join-Path ([System.IO.Path]::GetTempPath()) "ai-orchestrator-docker-home"
    $fallbackDockerDir = Join-Path $fallbackHome ".docker"
    Initialize-V2Directory -Path $fallbackDir
    Initialize-V2Directory -Path $fallbackDockerDir
    $fallbackFile = Join-Path $fallbackDir "config.json"
    $fallbackHomeConfigFile = Join-Path $fallbackDockerDir "config.json"
    if (-not [System.IO.File]::Exists($fallbackFile)) {
        [System.IO.File]::WriteAllText($fallbackFile, "{}")
    }
    if (-not [System.IO.File]::Exists($fallbackHomeConfigFile)) {
        [System.IO.File]::WriteAllText($fallbackHomeConfigFile, "{}")
    }

    $context.previous_home_exists = Test-Path Env:HOME
    if ($context.previous_home_exists) {
        $context.previous_home_value = [string]$env:HOME
    }
    $context.previous_userprofile_exists = Test-Path Env:USERPROFILE
    if ($context.previous_userprofile_exists) {
        $context.previous_userprofile_value = [string]$env:USERPROFILE
    }

    $env:DOCKER_CONFIG = $fallbackDir
    $env:HOME = $fallbackHome
    $env:USERPROFILE = $fallbackHome
    $context.fallback_used = $true
    $context.selected_config_dir = $fallbackDir
    $context.selected_config_file = $fallbackFile
    $context.selected_home_dir = $fallbackHome

    return $context
}

function Exit-V2DockerConfigFallbackContext {
    param([object]$Context)

    if ($null -eq $Context) {
        return
    }
    if (-not [bool](Get-V2OptionalProperty -InputObject $Context -Name "fallback_used" -DefaultValue $false)) {
        return
    }

    $previousExists = [bool](Get-V2OptionalProperty -InputObject $Context -Name "previous_exists" -DefaultValue $false)
    $previousValue = [string](Get-V2OptionalProperty -InputObject $Context -Name "previous_value" -DefaultValue "")
    $previousHomeExists = [bool](Get-V2OptionalProperty -InputObject $Context -Name "previous_home_exists" -DefaultValue $false)
    $previousHomeValue = [string](Get-V2OptionalProperty -InputObject $Context -Name "previous_home_value" -DefaultValue "")
    $previousUserProfileExists = [bool](Get-V2OptionalProperty -InputObject $Context -Name "previous_userprofile_exists" -DefaultValue $false)
    $previousUserProfileValue = [string](Get-V2OptionalProperty -InputObject $Context -Name "previous_userprofile_value" -DefaultValue "")
    if ($previousExists) {
        $env:DOCKER_CONFIG = $previousValue
    }
    else {
        Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
    }
    if ($previousHomeExists) {
        $env:HOME = $previousHomeValue
    }
    else {
        Remove-Item Env:HOME -ErrorAction SilentlyContinue
    }
    if ($previousUserProfileExists) {
        $env:USERPROFILE = $previousUserProfileValue
    }
    else {
        Remove-Item Env:USERPROFILE -ErrorAction SilentlyContinue
    }
}

function Get-V2DockerDaemonStatus {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        return [PSCustomObject]@{
            available   = $false
            reason      = "docker-cli-unavailable"
            detail      = "docker command not found in PATH"
            suggest_fix = "Install Docker Desktop and ensure 'docker' is in your PATH."
        }
    }

    $suggestFix = New-Object System.Collections.Generic.List[string]
    $daemonAvailable = $false
    $reason = "ok"
    $dockerConfigContext = $null

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $dockerConfigContext = Enter-V2DockerConfigFallbackContext
        $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
            $probeOutput = @(& docker --config $dockerConfigDir info --format "{{.ServerVersion}}" 2>&1)
        }
        else {
            $probeOutput = @(& docker info --format "{{.ServerVersion}}" 2>&1)
        }
        $exitCode = $LASTEXITCODE
        $probeText = (($probeOutput | Out-String).Trim())
        
        if ($exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($probeText)) {
            $daemonAvailable = $true
        }
        else {
            $reason = "docker-daemon-unreachable"
            if ($probeText -match "(?i)access is denied|permission denied") {
                $reason = "docker-daemon-access-denied"
                
                # Self-healing: Check if the user enabled the TCP socket (Port 2375)
                # If so, we can use it to bypass the named pipe permission issue.
                $tcpClient = New-Object System.Net.Sockets.TcpClient
                try {
                    $asyncResult = $tcpClient.BeginConnect("127.0.0.1", 2375, $null, $null)
                    if ($asyncResult.AsyncWaitHandle.WaitOne(500)) {
                        $tcpClient.EndConnect($asyncResult)
                        $suggestFix.Add("Docker TCP socket is OPEN. Attempting session fallback to tcp://localhost:2375.")
                        $env:DOCKER_HOST = "tcp://localhost:2375"
                        
                        # Retry probe with TCP host
                        if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                            $retryOutput = @(& docker --config $dockerConfigDir info --format "{{.ServerVersion}}" 2>&1)
                        }
                        else {
                            $retryOutput = @(& docker info --format "{{.ServerVersion}}" 2>&1)
                        }
                        $retryText = (($retryOutput | Out-String).Trim())
                        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($retryText)) {
                            $daemonAvailable = $true
                            $reason = "ok"
                            $probeText = $retryText
                        }
                    }
                }
                catch {} finally { $tcpClient.Close() }

                if (-not $daemonAvailable) {
                    $suggestFix.Add("Check Docker Desktop -> Settings -> General -> 'Expose daemon on tcp://localhost:2375 without TLS'.")
                }
            }
            
            # Diagnostic: Is Docker Desktop running?
            try {
                $proc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
                if (-not $proc) {
                    $suggestFix.Add("Docker Desktop is not running. Please start it.")
                }
            }
            catch {
            }

            # Diagnostic: WSL Check (on Windows) - best effort only.
            $runningOnWindows = $false
            try {
                $runningOnWindows = (($env:OS -eq "Windows_NT") -or ($PSVersionTable.Platform -eq "Win32NT"))
            }
            catch {
                $runningOnWindows = ($env:OS -eq "Windows_NT")
            }
            if ($runningOnWindows) {
                try {
                    $wslStatus = @(& wsl --list --verbose 2>&1 | Out-String)
                    if ($wslStatus -notmatch "docker-desktop") {
                        $suggestFix.Add("Docker WSL integration might be disabled. Check Docker Desktop -> Settings -> Resources -> WSL Integration.")
                    }
                }
                catch {
                }
            }

            # Diagnostic: Context check (best effort).
            try {
                if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                    $context = @(& docker --config $dockerConfigDir context ls --format "{{.Name}} {{.Current}}" 2>&1 | Out-String)
                }
                else {
                    $context = @(& docker context ls --format "{{.Name}} {{.Current}}" 2>&1 | Out-String)
                }
                if ($context -match "default\s+false" -and $context -notmatch "default\s+true") {
                    $suggestFix.Add("Current Docker context is not 'default'. Run: docker context use default")
                }
            }
            catch {
            }
        }
    }
    catch {
        $reason = "diagnostic-execution-failed"
        $probeText = $_.Exception.Message
    }
    finally {
        Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($dockerConfigContext -and [bool](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "fallback_used" -DefaultValue $false)) {
        $suggestFix.Add("Using fallback DOCKER_CONFIG due to access denied on user docker config.")
    }

    return [PSCustomObject]@{
        available   = $daemonAvailable
        reason      = $reason
        detail      = if ([string]::IsNullOrWhiteSpace($probeText)) { "docker info probe failed" } else { $probeText }
        suggest_fix = if ($daemonAvailable) { "" } else { ($suggestFix -join " | ") }
    }
}

function Get-V2ComposeTopology {
    param([string]$ComposePath)

    if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
        return [PSCustomObject]@{
            services = @()
            ports    = @()
        }
    }

    $lines = Get-Content -LiteralPath $ComposePath
    $services = New-Object System.Collections.Generic.List[string]
    $ports = New-Object System.Collections.Generic.List[object]
    $inServices = $false
    $currentService = ""

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ($line -match "^\s*services:\s*$") {
            $inServices = $true
            continue
        }

        if (-not $inServices) {
            continue
        }

        if ($line -match "^[^\s]" -and -not ($line -match "^\s*services:\s*$")) {
            break
        }

        $serviceMatch = [regex]::Match($line, "^\s{2}([A-Za-z0-9_-]+):\s*$")
        if ($serviceMatch.Success) {
            $currentService = $serviceMatch.Groups[1].Value
            if (-not ($services -contains $currentService)) {
                $services.Add($currentService)
            }
            continue
        }

        if ([string]::IsNullOrWhiteSpace($currentService)) {
            continue
        }

        $portMatch = [regex]::Match($line, "^(?<indent>\s*)-\s*`"?(?<host>\d+):(?<container>\d+)`"?\s*$")
        if ($portMatch.Success) {
            $ports.Add([PSCustomObject]@{
                    service        = $currentService
                    host_port      = [int]$portMatch.Groups["host"].Value
                    container_port = [int]$portMatch.Groups["container"].Value
                    line_index     = $i
                    indent         = [string]$portMatch.Groups["indent"].Value
                    quoted         = ($line -match "`"")
                })
        }
    }

    return [PSCustomObject]@{
        services = @($services.ToArray())
        ports    = @($ports.ToArray())
    }
}

function Get-V2UsedHostPorts {
    $used = [System.Collections.Generic.HashSet[int]]::new()

    try {
        foreach ($port in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort)) {
            if ($null -ne $port) {
                [void]$used.Add([int]$port)
            }
        }
    }
    catch {
    }

    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCmd) {
        $dockerConfigContext = $null
        try {
            $dockerConfigContext = Enter-V2DockerConfigFallbackContext
            $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
            $dockerPsOutput = @()
            if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                $dockerPsOutput = @(& docker --config $dockerConfigDir ps --format "{{.Ports}}" 2>$null)
            }
            else {
                $dockerPsOutput = @(& docker ps --format "{{.Ports}}" 2>$null)
            }
            foreach ($line in $dockerPsOutput) {
                foreach ($match in [regex]::Matches([string]$line, "(?:0\.0\.0\.0:|:::)?(?<host>\d+)->")) {
                    [void]$used.Add([int]$match.Groups["host"].Value)
                }
            }
        }
        catch {
        }
        finally {
            Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
        }
    }

    return , $used
}

function Find-V2AvailablePort {
    param(
        [int]$PreferredPort,
        [System.Collections.Generic.HashSet[int]]$UsedPorts
    )

    $start = if ($PreferredPort -lt 1025) { 1025 } else { $PreferredPort }
    for ($port = $start; $port -lt ($start + 4000); $port++) {
        if (-not $UsedPorts.Contains($port)) {
            # Double-check with live probe so we avoid selecting ports that are in use
            # when docker/netstat discovery is partial in restricted environments.
            if (-not (Test-V2PortOpen -Port $port -TimeoutMs 250)) {
                return $port
            }
            [void]$UsedPorts.Add($port)
        }
    }

    return -1
}

function Set-V2ComposePortRemaps {
    param(
        [string]$ComposePath,
        [object[]]$Remaps
    )

    if (@($Remaps).Count -eq 0) {
        return
    }

    $lines = Get-Content -LiteralPath $ComposePath
    foreach ($remap in @($Remaps)) {
        $index = [int](Get-V2OptionalProperty -InputObject $remap -Name "line_index" -DefaultValue -1)
        if ($index -lt 0 -or $index -ge $lines.Count) {
            continue
        }
        $indent = [string](Get-V2OptionalProperty -InputObject $remap -Name "indent" -DefaultValue "      ")
        $containerPort = [int](Get-V2OptionalProperty -InputObject $remap -Name "container_port" -DefaultValue 0)
        $newHostPort = [int](Get-V2OptionalProperty -InputObject $remap -Name "new_host_port" -DefaultValue 0)
        $quoted = [bool](Get-V2OptionalProperty -InputObject $remap -Name "quoted" -DefaultValue $true)
        if ($containerPort -le 0 -or $newHostPort -le 0) {
            continue
        }
        if ($quoted) {
            $lines[$index] = "$indent- `"${newHostPort}:${containerPort}`""
        }
        else {
            $lines[$index] = "$indent- ${newHostPort}:${containerPort}"
        }
    }

    [System.IO.File]::WriteAllText($ComposePath, ($lines -join [Environment]::NewLine))
}

function Invoke-V2ComposeUp {
    param(
        [string]$ComposeEngine,
        [string]$ComposePath,
        [string[]]$Services
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    $dockerConfigContext = $null
    $dockerHostProactive = ""
    $dockerHostApplied = $false
    $previousDockerHostExists = $false
    $previousDockerHostValue = ""
    try {
        $ErrorActionPreference = "Continue"
        $dockerConfigContext = Enter-V2DockerConfigFallbackContext
        $dockerHostProactive = Get-V2PreferredDockerHost
        if (-not [string]::IsNullOrWhiteSpace($dockerHostProactive) -and -not (Test-Path Env:DOCKER_HOST)) {
            $previousDockerHostExists = $false
            $env:DOCKER_HOST = $dockerHostProactive
            $dockerHostApplied = $true
        }
        elseif (Test-Path Env:DOCKER_HOST) {
            $previousDockerHostExists = $true
            $previousDockerHostValue = [string]$env:DOCKER_HOST
        }

        try {
            $hasDockerCli = ($null -ne (Get-Command docker -ErrorAction SilentlyContinue))
            $hasDockerComposeV1 = ($null -ne (Get-Command docker-compose -ErrorAction SilentlyContinue))
            $preferComposeV2 = ($ComposeEngine -eq "docker-compose-v2") -or $hasDockerCli
            $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
            if ($preferComposeV2) {
                if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                    $output = @(& docker --config $dockerConfigDir compose -f $ComposePath up -d @($Services) 2>&1)
                }
                else {
                    $output = @(& docker compose -f $ComposePath up -d @($Services) 2>&1)
                }
                $exitCode = $LASTEXITCODE
                $outputText = (($output | Out-String).Trim())
                if ($exitCode -ne 0 -and $hasDockerComposeV1 -and $outputText -match "(?i)is not a docker command|unknown command|no such command|unknown shorthand flag:\s*'f'\s*in\s*-f") {
                    $output = @(& docker-compose -f $ComposePath up -d @($Services) 2>&1)
                    $exitCode = $LASTEXITCODE
                }
            }
            else {
                $output = @(& docker-compose -f $ComposePath up -d @($Services) 2>&1)
                $exitCode = $LASTEXITCODE
            }
        }
        catch {
            # Preserve command error text so caller can still parse conflicts and auto-remap.
            $errorText = [string]$_.Exception.Message
            if (-not [string]::IsNullOrWhiteSpace($errorText)) {
                $output = @($output + $errorText)
            }
            if ($LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) {
                $exitCode = [int]$LASTEXITCODE
            }
            else {
                $exitCode = 1
            }
        }
    }
    finally {
        if ($dockerHostApplied) {
            if ($previousDockerHostExists) {
                $env:DOCKER_HOST = $previousDockerHostValue
            }
            else {
                Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue
            }
        }
        Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
        $ErrorActionPreference = $previousErrorActionPreference
    }

    return [PSCustomObject]@{
        exit_code                 = $exitCode
        output                    = @($output)
        text                      = (($output | Out-String).Trim())
        docker_host_proactive     = $dockerHostProactive
        docker_host_proactive_used = $dockerHostApplied
        docker_config_fallback_used = [bool](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "fallback_used" -DefaultValue $false)
    }
}

function Ensure-V2OrchestratorCoreComposeFile {
    param(
        [string]$CoordinationRoot
    )

    $coreRoot = Join-Path $CoordinationRoot "workspace/orchestrator-core"
    $coreDockerRoot = Join-Path $coreRoot "docker"
    Initialize-V2Directory -Path $coreDockerRoot
    $composePath = Join-Path $coreDockerRoot "docker-compose.core.yml"

    $composeContent = @"
name: orchestrator-core

services:
  orchestrator-core:
    container_name: orchestrator-core
    image: alpine:3.20
    command: ["sh", "-lc", "while true; do sleep 3600; done"]
    restart: unless-stopped
    labels:
      ai.orchestrator.scope: "global"
      ai.orchestrator.role: "core"
    networks:
      - orchestrator-core-net

networks:
  orchestrator-core-net:
    name: orchestrator-core-net
"@

    [System.IO.File]::WriteAllText($composePath, $composeContent.Trim() + [Environment]::NewLine)

    return [PSCustomObject]@{
        root_path    = $coreRoot
        docker_root  = $coreDockerRoot
        compose_path = $composePath
        container    = "orchestrator-core"
        network      = "orchestrator-core-net"
    }
}

function Get-V2ContainerRunningStatus {
    param(
        [string]$ContainerName
    )

    $isRunning = $false
    $details = ""
    $dockerConfigContext = $null

    try {
        $dockerConfigContext = Enter-V2DockerConfigFallbackContext
        $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
        $output = @()
        if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
            $output = @(& docker --config $dockerConfigDir ps --filter "name=^${ContainerName}$" --filter "status=running" --format "{{.Names}}" 2>&1)
        }
        else {
            $output = @(& docker ps --filter "name=^${ContainerName}$" --filter "status=running" --format "{{.Names}}" 2>&1)
        }
        $details = (($output | Out-String).Trim())
        if ($LASTEXITCODE -eq 0) {
            $names = @(
                $output |
                ForEach-Object { [string]$_ } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                ForEach-Object { $_.Trim() }
            )
            if ($names -contains $ContainerName) {
                $isRunning = $true
            }
        }
    }
    catch {
        $details = $_.Exception.Message
    }
    finally {
        Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
    }

    return [PSCustomObject]@{
        running = $isRunning
        details = $details
    }
}

function Invoke-V2OrchestratorCoreBootstrap {
    param(
        [string]$CoordinationRoot
    )

    $notes = New-Object System.Collections.Generic.List[string]
    $errors = New-Object System.Collections.Generic.List[string]
    $coreInfo = Ensure-V2OrchestratorCoreComposeFile -CoordinationRoot $CoordinationRoot
    $composePath = [string](Get-V2OptionalProperty -InputObject $coreInfo -Name "compose_path" -DefaultValue "")
    $containerName = [string](Get-V2OptionalProperty -InputObject $coreInfo -Name "container" -DefaultValue "orchestrator-core")
    $networkName = [string](Get-V2OptionalProperty -InputObject $coreInfo -Name "network" -DefaultValue "orchestrator-core-net")

    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        $errors.Add("orchestrator-core-compose-missing: $composePath")
    }

    $composeEngine = Get-V2DockerComposeEngine
    if ($composeEngine -eq "unavailable") {
        $errors.Add("orchestrator-core-compose-engine-unavailable")
    }

    $dockerDaemonStatus = Get-V2DockerDaemonStatus
    if (-not [bool](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "available" -DefaultValue $false)) {
        $errors.Add("orchestrator-core-daemon-unavailable: $([string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name 'reason' -DefaultValue 'unknown'))")
    }

    if ($errors.Count -eq 0) {
        $upResult = Invoke-V2ComposeUp -ComposeEngine $composeEngine -ComposePath $composePath -Services @("orchestrator-core")
        if ([int](Get-V2OptionalProperty -InputObject $upResult -Name "exit_code" -DefaultValue 1) -ne 0) {
            $errors.Add("orchestrator-core-up-failed: $([string](Get-V2OptionalProperty -InputObject $upResult -Name 'text' -DefaultValue 'unknown'))")
        }
    }

    $runtimeStatus = Get-V2ContainerRunningStatus -ContainerName $containerName
    if (-not [bool](Get-V2OptionalProperty -InputObject $runtimeStatus -Name "running" -DefaultValue $false)) {
        $errors.Add("orchestrator-core-not-running")
        if (-not [string]::IsNullOrWhiteSpace([string](Get-V2OptionalProperty -InputObject $runtimeStatus -Name "details" -DefaultValue ""))) {
            $notes.Add("orchestrator-core-runtime-detail: $([string](Get-V2OptionalProperty -InputObject $runtimeStatus -Name 'details' -DefaultValue ''))")
        }
    }

    if ($errors.Count -eq 0) {
        $notes.Add("orchestrator-core-ready: container=$containerName network=$networkName")
    }

    return [PSCustomObject]@{
        success       = ($errors.Count -eq 0)
        status        = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
        compose_path  = $composePath
        root_path     = [string](Get-V2OptionalProperty -InputObject $coreInfo -Name "root_path" -DefaultValue "")
        docker_root   = [string](Get-V2OptionalProperty -InputObject $coreInfo -Name "docker_root" -DefaultValue "")
        compose_engine = $composeEngine
        container     = $containerName
        network       = $networkName
        daemon_status = $dockerDaemonStatus
        notes         = @($notes.ToArray())
        errors        = @($errors.ToArray())
    }
}

function Test-V2DedicatedProjectContainerIsolation {
    param(
        [string]$ComposePath,
        [string]$ProjectSlug
    )

    $notes = New-Object System.Collections.Generic.List[string]
    $errors = New-Object System.Collections.Generic.List[string]
    $containerNames = New-Object System.Collections.Generic.List[string]
    $expectedPrefix = "$ProjectSlug-"

    if (-not (Test-Path -LiteralPath $ComposePath -PathType Leaf)) {
        $errors.Add("compose-file-missing: $ComposePath")
        return [PSCustomObject]@{
            success = $false
            status  = "error"
            notes   = @($notes.ToArray())
            errors  = @($errors.ToArray())
            containers = @()
        }
    }

    foreach ($line in @(Get-Content -LiteralPath $ComposePath)) {
        $match = [regex]::Match([string]$line, "^\s{4}container_name:\s*(?<name>[A-Za-z0-9_.-]+)\s*$")
        if ($match.Success) {
            $name = [string]$match.Groups["name"].Value
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $containerNames.Add($name)
            }
        }
    }

    if ($containerNames.Count -eq 0) {
        $errors.Add("no-container-name-entries-found")
    }
    else {
        foreach ($containerName in @($containerNames.ToArray())) {
            if (-not $containerName.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                $errors.Add("container-prefix-mismatch: $containerName (expected prefix '$expectedPrefix')")
            }
        }
    }

    if ($errors.Count -eq 0) {
        $notes.Add("project-container-isolation-ok: all container_name values use '$expectedPrefix*'")
    }

    return [PSCustomObject]@{
        success    = ($errors.Count -eq 0)
        status     = if ($errors.Count -eq 0) { "ready" } else { "error" }
        notes      = @($notes.ToArray())
        errors     = @($errors.ToArray())
        containers = @($containerNames.ToArray())
    }
}

function Get-V2ComposeConflictHostPorts {
    param([string]$ComposeOutputText)

    $ports = [System.Collections.Generic.HashSet[int]]::new()
    if ([string]::IsNullOrWhiteSpace($ComposeOutputText)) {
        return @()
    }

    $patterns = @(
        "(?i)Bind for\s+0\.0\.0\.0:(?<host>\d{2,5})\s+failed:\s+port is already allocated",
        "(?i)Bind for\s+\[::\]:(?<host>\d{2,5})\s+failed:\s+port is already allocated",
        "(?i)0\.0\.0\.0:(?<host>\d{2,5})\s+failed:\s+port is already allocated",
        "(?i)\[::\]:(?<host>\d{2,5})\s+failed:\s+port is already allocated",
        "(?i)listen tcp 0\.0\.0\.0:(?<host>\d{2,5}):\s*bind",
        "(?i)listen tcp \[::\]:(?<host>\d{2,5}):\s*bind",
        "(?i)Ports are not available:.*0\.0\.0\.0:(?<host>\d{2,5})",
        "(?i)Ports are not available:.*\[\:\:\]:(?<host>\d{2,5})"
    )

    foreach ($pattern in $patterns) {
        foreach ($match in [regex]::Matches($ComposeOutputText, $pattern)) {
            $hostValue = [string]$match.Groups["host"].Value
            if (-not [string]::IsNullOrWhiteSpace($hostValue)) {
                [void]$ports.Add([int]$hostValue)
            }
        }
    }

    # Fallback parser for variants that still contain bind/allocation semantics
    # but do not match the strict patterns above.
    foreach ($line in @($ComposeOutputText -split "(`r`n|`n|`r)")) {
        $text = [string]$line
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        if ($text -notmatch "(?i)already allocated|address already in use|bind") { continue }
        foreach ($portMatch in [regex]::Matches($text, ":(?<host>\d{2,5})")) {
            $hostValue = [string]$portMatch.Groups["host"].Value
            if (-not [string]::IsNullOrWhiteSpace($hostValue)) {
                [void]$ports.Add([int]$hostValue)
            }
        }
    }

    return @($ports | ForEach-Object { [int]$_ } | Sort-Object -Unique)
}

function New-V2PortRemapsForConflicts {
    param(
        [object]$Topology,
        [int[]]$ConflictPorts,
        [System.Collections.Generic.HashSet[int]]$UsedPorts
    )

    $remaps = New-Object System.Collections.Generic.List[object]
    $targetPorts = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($port in @($ConflictPorts)) {
        if ($null -ne $port -and [int]$port -gt 0) {
            [void]$targetPorts.Add([int]$port)
        }
    }

    foreach ($mapping in @($Topology.ports | Sort-Object line_index)) {
        $hostPort = [int](Get-V2OptionalProperty -InputObject $mapping -Name "host_port" -DefaultValue 0)
        if ($hostPort -le 0) {
            continue
        }
        if (-not $targetPorts.Contains($hostPort)) {
            continue
        }

        $nextPort = Find-V2AvailablePort -PreferredPort ($hostPort + 1) -UsedPorts $UsedPorts
        if ($nextPort -gt 0) {
            [void]$UsedPorts.Add($nextPort)
            $remaps.Add([PSCustomObject]@{
                    service        = [string](Get-V2OptionalProperty -InputObject $mapping -Name "service" -DefaultValue "")
                    host_port      = $hostPort
                    new_host_port  = $nextPort
                    container_port = [int](Get-V2OptionalProperty -InputObject $mapping -Name "container_port" -DefaultValue 0)
                    line_index     = [int](Get-V2OptionalProperty -InputObject $mapping -Name "line_index" -DefaultValue -1)
                    indent         = [string](Get-V2OptionalProperty -InputObject $mapping -Name "indent" -DefaultValue "      ")
                    quoted         = [bool](Get-V2OptionalProperty -InputObject $mapping -Name "quoted" -DefaultValue $true)
                })
        }
    }

    return @($remaps.ToArray())
}

function Test-V2PortOpen {
    param(
        [int]$Port,
        [int]$TimeoutMs = 2000
    )

    if ($Port -le 0) {
        return $false
    }

    $client = New-Object System.Net.Sockets.TcpClient
    $waitHandle = $null
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $waitHandle = $asyncResult.AsyncWaitHandle
        if (-not $waitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($asyncResult) | Out-Null
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($waitHandle) { $waitHandle.Dispose() }
        $client.Close()
        $client.Dispose()
    }
}

function Get-V2PreferredDockerHost {
    $runningOnWindows = $false
    try {
        $runningOnWindows = (($env:OS -eq "Windows_NT") -or ($PSVersionTable.Platform -eq "Win32NT"))
    }
    catch {
        $runningOnWindows = ($env:OS -eq "Windows_NT")
    }

    if (-not $runningOnWindows) {
        return ""
    }

    if (Test-V2PortOpen -Port 2375 -TimeoutMs 700) {
        return "tcp://localhost:2375"
    }

    return ""
}

function Get-V2EnvMap {
    param([string]$EnvPath)

    $map = @{}
    if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
        return $map
    }

    foreach ($line in @(Get-Content -LiteralPath $EnvPath)) {
        $trimmed = [string]$line
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        if (-not [string]::IsNullOrWhiteSpace($key)) {
            $map[$key] = $value
        }
    }

    return $map
}

function Ensure-V2ScopedProjectPack {
    param(
        [string]$OrchestratorRoot,
        [string]$ProjectName,
        [string]$ProjectSlug
    )

    $scopedRoot = Join-Path $OrchestratorRoot ("projects/{0}" -f $ProjectSlug)
    $scopedDirs = @(
        "pack",
        "docker",
        "memory",
        "graph",
        "vector",
        "tasks",
        "agents",
        "analysis",
        "architecture",
        "logs",
        "state"
    )

    Initialize-V2Directory -Path $scopedRoot
    foreach ($dir in $scopedDirs) {
        Initialize-V2Directory -Path (Join-Path $scopedRoot $dir)
    }

    Write-V2File -Path (Join-Path $scopedRoot "pack/README.md") -Content @"
# Project Pack ($ProjectName)

Self-contained project operational pack.
All runtime state, memory, graph, vector, tasks, and configuration for this project lives under this scope.
"@ -Force
    Write-V2File -Path (Join-Path $scopedRoot "memory/README.md") -Content "# Memory Layer`n`nScoped memory for this project." -Force
    Write-V2File -Path (Join-Path $scopedRoot "graph/README.md") -Content "# Graph Layer`n`nScoped knowledge graph state for this project." -Force
    Write-V2File -Path (Join-Path $scopedRoot "vector/README.md") -Content "# Vector Layer`n`nScoped semantic memory state for this project." -Force

    return $scopedRoot
}

function Get-V2UniqueStringArray {
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

function Get-V2ServiceMapFromEdges {
    param([object[]]$Edges)

    $map = [ordered]@{}
    foreach ($edge in @($Edges)) {
        $source = [string](Get-V2OptionalProperty -InputObject $edge -Name "source" -DefaultValue "")
        $target = [string](Get-V2OptionalProperty -InputObject $edge -Name "target" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($source) -or [string]::IsNullOrWhiteSpace($target)) {
            continue
        }

        if (-not $map.Contains($source)) {
            $map[$source] = New-Object System.Collections.Generic.List[string]
        }
        if (-not ($map[$source] -contains $target)) {
            $map[$source].Add($target)
        }
    }

    $result = [ordered]@{}
    foreach ($key in $map.Keys) {
        $result[$key] = [PSCustomObject]@{
            depends_on = @($map[$key].ToArray())
        }
    }

    return ([PSCustomObject]$result)
}

function Update-V2ProjectDna {
    param(
        [string]$ScopedProjectRoot,
        [string]$ProjectName,
        [string]$ProjectSlug,
        [object]$Intake,
        [object]$Bootstrap,
        [string]$EffectiveStatus,
        [string[]]$Unknowns,
        [string[]]$OpenQuestions
    )

    $dnaPath = Join-Path $ScopedProjectRoot "project_dna.json"
    $existingDna = Get-V2JsonContent -Path $dnaPath
    $timestamp = Get-V2Timestamp

    $tf = Get-V2OptionalProperty -InputObject $Intake -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
    $analysis = Get-V2OptionalProperty -InputObject $Intake -Name "analysis" -DefaultValue ([PSCustomObject]@{})
    $quality = Get-V2OptionalProperty -InputObject $analysis -Name "code_quality" -DefaultValue ([PSCustomObject]@{})
    $dependencyGraph = Get-V2OptionalProperty -InputObject $analysis -Name "dependency_graph" -DefaultValue ([PSCustomObject]@{})
    $edges = @(Get-V2OptionalProperty -InputObject $dependencyGraph -Name "edges" -DefaultValue @())

    $languageRows = @(Get-V2OptionalProperty -InputObject $tf -Name "languages" -DefaultValue @())
    $languageNames = @()
    foreach ($row in $languageRows) {
        $language = [string](Get-V2OptionalProperty -InputObject $row -Name "language" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($language)) {
            $languageNames += $language
        }
    }
    $languageNames = Get-V2UniqueStringArray -Items $languageNames

    $bootstrapServices = @(
        @(Get-V2OptionalProperty -InputObject $Bootstrap -Name "services" -DefaultValue @()) |
        Where-Object { $_ -notin @("db", "neo4j", "qdrant") }
    )
    $serviceStructure = @(Get-V2OptionalProperty -InputObject $tf -Name "service_structure" -DefaultValue @())
    $serviceNames = Get-V2UniqueStringArray -Items @($bootstrapServices + $serviceStructure)

    $connectionPack = Get-V2OptionalProperty -InputObject $Bootstrap -Name "connection_pack" -DefaultValue ([PSCustomObject]@{})
    $connections = Get-V2OptionalProperty -InputObject $connectionPack -Name "connections" -DefaultValue ([PSCustomObject]@{})
    $relational = Get-V2OptionalProperty -InputObject $connections -Name "transactional_db" -DefaultValue ([PSCustomObject]@{})
    $neo4j = Get-V2OptionalProperty -InputObject $connections -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
    $qdrant = Get-V2OptionalProperty -InputObject $connections -Name "qdrant" -DefaultValue ([PSCustomObject]@{})

    $detectedDb = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $tf -Name "database" -DefaultValue ([PSCustomObject]@{})) -Name "engine" -DefaultValue "unknown")
    $databaseNames = @()
    $relationalEngine = [string](Get-V2OptionalProperty -InputObject $relational -Name "engine" -DefaultValue "unknown")
    if (-not [string]::IsNullOrWhiteSpace($relationalEngine) -and $relationalEngine -ne "unknown") { $databaseNames += $relationalEngine }
    elseif (-not [string]::IsNullOrWhiteSpace($detectedDb) -and $detectedDb -ne "unknown") { $databaseNames += $detectedDb }
    if ([bool](Get-V2OptionalProperty -InputObject $neo4j -Name "enabled" -DefaultValue $false)) { $databaseNames += "neo4j" }
    if ([bool](Get-V2OptionalProperty -InputObject $qdrant -Name "enabled" -DefaultValue $false)) { $databaseNames += "qdrant" }
    $databaseNames = Get-V2UniqueStringArray -Items $databaseNames

    $architectureStyle = [string](Get-V2OptionalProperty -InputObject $tf -Name "architecture_pattern" -DefaultValue "unknown")
    if ([string]::IsNullOrWhiteSpace($architectureStyle)) { $architectureStyle = "unknown" }

    $existingArchitecturePatterns = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "patterns" -DefaultValue ([PSCustomObject]@{})) `
            -Name "architecture_patterns" `
            -DefaultValue @()
    )
    $architecturePatterns = Get-V2UniqueStringArray -Items @($existingArchitecturePatterns + @($architectureStyle))

    $existingCodePatterns = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "patterns" -DefaultValue ([PSCustomObject]@{})) `
            -Name "code_patterns" `
            -DefaultValue @()
    )
    $inferredCodePatterns = @()
    if (@($serviceNames).Count -gt 0) { $inferredCodePatterns += "service_layer" }
    if ([bool](Get-V2OptionalProperty -InputObject $relational -Name "enabled" -DefaultValue $false)) { $inferredCodePatterns += "repository_pattern" }
    $codePatterns = Get-V2UniqueStringArray -Items @($existingCodePatterns + $inferredCodePatterns)

    $largeFiles = @(Get-V2OptionalProperty -InputObject $quality -Name "large_files" -DefaultValue @())
    $deadCode = @(Get-V2OptionalProperty -InputObject $quality -Name "dead_code_candidates" -DefaultValue @())
    $vulnerabilitySignals = @(Get-V2OptionalProperty -InputObject $quality -Name "vulnerability_signals" -DefaultValue @())

    $hotspots = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @($largeFiles + $deadCode)) {
        if ($entry -is [string]) {
            if (-not [string]::IsNullOrWhiteSpace($entry)) { $hotspots.Add([string]$entry) }
            continue
        }
        $candidate = [string](Get-V2OptionalProperty -InputObject $entry -Name "path" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            $candidate = [string](Get-V2OptionalProperty -InputObject $entry -Name "file" -DefaultValue "")
        }
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $hotspots.Add($candidate)
        }
    }

    $existingHotspots = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "agent_knowledge" -DefaultValue ([PSCustomObject]@{})) `
            -Name "known_hotspots" `
            -DefaultValue @()
    )
    $knownHotspots = Get-V2UniqueStringArray -Items @($existingHotspots + @($hotspots.ToArray()))

    $existingDebt = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "agent_knowledge" -DefaultValue ([PSCustomObject]@{})) `
            -Name "technical_debt" `
            -DefaultValue @()
    )
    $technicalDebt = Get-V2UniqueStringArray -Items @($existingDebt + $Unknowns + $OpenQuestions + $vulnerabilitySignals)

    $serviceMap = Get-V2ServiceMapFromEdges -Edges $edges

    $existingIdentity = Get-V2OptionalProperty -InputObject $existingDna -Name "project_identity" -DefaultValue ([PSCustomObject]@{})
    $createdAt = [string](Get-V2OptionalProperty -InputObject $existingIdentity -Name "created_at" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($createdAt)) {
        $createdAt = (Get-Date).ToString("yyyy-MM-dd")
    }

    $previousStyle = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "architecture" -DefaultValue ([PSCustomObject]@{})) -Name "style" -DefaultValue "")
    $previousServices = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "architecture" -DefaultValue ([PSCustomObject]@{})) `
            -Name "services" `
            -DefaultValue @()
    )
    $previousDatabases = @(
        Get-V2OptionalProperty `
            -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "architecture" -DefaultValue ([PSCustomObject]@{})) `
            -Name "databases" `
            -DefaultValue @()
    )

    $majorChanges = New-Object System.Collections.Generic.List[object]
    foreach ($change in @(Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $existingDna -Name "evolution" -DefaultValue ([PSCustomObject]@{})) -Name "major_changes" -DefaultValue @())) {
        $majorChanges.Add($change)
    }
    if (-not $existingDna) {
        $majorChanges.Add([PSCustomObject]@{
                date        = $timestamp
                description = "bootstrap initialized project dna"
            })
    }
    if (-not [string]::IsNullOrWhiteSpace($previousStyle) -and $previousStyle -ne $architectureStyle) {
        $majorChanges.Add([PSCustomObject]@{
                date        = $timestamp
                description = "architecture style changed: $previousStyle -> $architectureStyle"
            })
    }

    $addedServices = @($serviceNames | Where-Object { $_ -notin $previousServices })
    foreach ($service in $addedServices) {
        $majorChanges.Add([PSCustomObject]@{
                date        = $timestamp
                description = "service added: $service"
            })
    }

    $addedDatabases = @($databaseNames | Where-Object { $_ -notin $previousDatabases })
    foreach ($db in $addedDatabases) {
        $majorChanges.Add([PSCustomObject]@{
                date        = $timestamp
                description = "database added: $db"
            })
    }

    $projectRuntimeStatus = if ($EffectiveStatus -like "blocked*") { "blocked" } else { "active" }

    $dna = [PSCustomObject]@{
        project_identity = [PSCustomObject]@{
            name       = $ProjectName
            created_at = $createdAt
            type       = [string](Get-V2OptionalProperty -InputObject $Intake -Name "project_type" -DefaultValue "unknown")
            status     = $projectRuntimeStatus
            updated_at = $timestamp
        }
        architecture     = [PSCustomObject]@{
            style     = $architectureStyle
            services  = @($serviceNames)
            languages = @($languageNames)
            databases = @($databaseNames)
        }
        service_map      = $serviceMap
        patterns         = [PSCustomObject]@{
            architecture_patterns = @($architecturePatterns)
            code_patterns         = @($codePatterns)
        }
        tech_stack       = [PSCustomObject]@{
            backend       = [string](Get-V2OptionalProperty -InputObject $tf -Name "primary_language" -DefaultValue "unknown")
            database      = if (-not [string]::IsNullOrWhiteSpace($relationalEngine) -and $relationalEngine -ne "unknown") { $relationalEngine } else { "unknown" }
            vector_memory = if ([bool](Get-V2OptionalProperty -InputObject $qdrant -Name "enabled" -DefaultValue $false)) { "qdrant" } else { "unknown" }
            graph_memory  = if ([bool](Get-V2OptionalProperty -InputObject $neo4j -Name "enabled" -DefaultValue $false)) { "neo4j" } else { "unknown" }
        }
        evolution        = [PSCustomObject]@{
            major_changes = @($majorChanges | Select-Object -Last 120)
        }
        agent_knowledge  = [PSCustomObject]@{
            known_hotspots = @($knownHotspots | Select-Object -First 200)
            technical_debt = @($technicalDebt | Select-Object -First 200)
        }
    }

    Save-V2JsonContent -Path $dnaPath -Value $dna
    return $dnaPath
}

function Get-V2Neo4jDatabaseName {
    param([string]$ProjectSlug)

    $slug = [string]$ProjectSlug
    if ([string]::IsNullOrWhiteSpace($slug)) {
        $slug = "project"
    }

    $normalized = ($slug.ToLowerInvariant() -replace "[^a-z0-9_]", "_")
    if ($normalized.Length -eq 0) {
        $normalized = "project"
    }
    if ($normalized[0] -notmatch "[a-z]") {
        $normalized = "p_$normalized"
    }
    if ($normalized.Length -gt 48) {
        $normalized = $normalized.Substring(0, 48)
    }

    return $normalized
}

function Get-V2ProjectSchemaName {
    param([string]$ProjectSlug)

    $slug = [string]$ProjectSlug
    if ([string]::IsNullOrWhiteSpace($slug)) {
        $slug = "project"
    }

    $normalized = ($slug.ToLowerInvariant() -replace "[^a-z0-9_]", "_")
    if ($normalized.Length -eq 0) {
        $normalized = "project"
    }
    if ($normalized[0] -notmatch "[a-z]") {
        $normalized = "p_$normalized"
    }
    if ($normalized.Length -gt 48) {
        $normalized = $normalized.Substring(0, 48)
    }
    if ($normalized -eq "public") {
        $normalized = "project_public"
    }

    return "p_{0}" -f $normalized
}

function New-V2BasicAuthHeader {
    param(
        [string]$Username,
        [string]$Password
    )

    $pair = "{0}:{1}" -f $Username, $Password
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($pair)
    return "Basic {0}" -f [Convert]::ToBase64String($bytes)
}

function Invoke-V2Neo4jHttpCommit {
    param(
        [string]$BaseUrl,
        [string]$Database,
        [hashtable]$Headers,
        [object[]]$Statements
    )

    $payload = @{
        statements = @($Statements)
    } | ConvertTo-Json -Depth 12

    $uri = "{0}/db/{1}/tx/commit" -f $BaseUrl.TrimEnd("/"), $Database
    $response = Invoke-RestMethod -Uri $uri -Method Post -Headers $Headers -Body $payload -TimeoutSec 12
    $errors = @(Get-V2OptionalProperty -InputObject $response -Name "errors" -DefaultValue @())
    if ($errors.Count -gt 0) {
        $first = [string](Get-V2OptionalProperty -InputObject $errors[0] -Name "message" -DefaultValue "neo4j-http-error")
        throw "neo4j-query-failed ($Database): $first"
    }

    return $response
}

function Ensure-V2Neo4jProjectNamespace {
    param(
        [object]$Connection,
        [string]$ProjectSlug
    )

    $disabledResult = [PSCustomObject]@{
        status            = "skipped"
        database          = "neo4j"
        project_namespace = $ProjectSlug
        details           = "neo4j-disabled"
    }
    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return $disabledResult
    }

    $serverHost = [string](Get-V2OptionalProperty -InputObject $Connection -Name "host" -DefaultValue "localhost")
    $httpPort = [int](Get-V2OptionalProperty -InputObject $Connection -Name "http_port" -DefaultValue 0)
    $username = [string](Get-V2OptionalProperty -InputObject $Connection -Name "user" -DefaultValue "neo4j")
    $password = [string](Get-V2OptionalProperty -InputObject $Connection -Name "password" -DefaultValue "")
    if ($httpPort -le 0) {
        return [PSCustomObject]@{
            status            = "error"
            database          = "neo4j"
            project_namespace = $ProjectSlug
            details           = "neo4j-http-port-missing"
        }
    }
    if ([string]::IsNullOrWhiteSpace($password)) {
        return [PSCustomObject]@{
            status            = "error"
            database          = "neo4j"
            project_namespace = $ProjectSlug
            details           = "neo4j-password-missing"
        }
    }

    $baseUrl = "http://{0}:{1}" -f $serverHost, $httpPort
    $headers = @{
        Authorization = (New-V2BasicAuthHeader -Username $username -Password $password)
        "Content-Type" = "application/json"
    }
    $targetDb = Get-V2Neo4jDatabaseName -ProjectSlug $ProjectSlug
    $activeDb = "neo4j"
    $createDbState = "skipped"
    $createDbDetails = ""

    $createDbStatement = @{
        statement = ("CREATE DATABASE `{0}` IF NOT EXISTS" -f $targetDb)
    }

    $attempts = 5
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database "system" -Headers $headers -Statements @($createDbStatement) | Out-Null
            $activeDb = $targetDb
            $createDbState = "created-or-exists"
            $createDbDetails = "neo4j-database-ready"
            break
        }
        catch {
            $createDbState = "fallback"
            $createDbDetails = [string]$_.Exception.Message
            if ($attempt -lt $attempts) {
                Start-Sleep -Seconds 2
            }
        }
    }

    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            # Neo4j does not allow schema changes and writes in the same transaction.
            Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database $activeDb -Headers $headers -Statements @(
                @{
                    statement = "CREATE CONSTRAINT memory_node_identity IF NOT EXISTS FOR (n:MemoryNode) REQUIRE (n.project_slug, n.id) IS UNIQUE"
                }
            ) | Out-Null
            Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database $activeDb -Headers $headers -Statements @(
                @{
                    statement  = "MERGE (:Project {slug: `$project_slug})"
                    parameters = @{ project_slug = $ProjectSlug }
                }
            ) | Out-Null
            return [PSCustomObject]@{
                status            = if ($createDbState -eq "fallback") { "ready-with-fallback" } else { "ready" }
                database          = $activeDb
                project_namespace = $ProjectSlug
                details           = if ([string]::IsNullOrWhiteSpace($createDbDetails)) { "neo4j-namespace-provisioned" } else { $createDbDetails }
            }
        }
        catch {
            if ($attempt -ge $attempts) {
                return [PSCustomObject]@{
                    status            = "error"
                    database          = $activeDb
                    project_namespace = $ProjectSlug
                    details           = [string]$_.Exception.Message
                }
            }
            Start-Sleep -Seconds 2
        }
    }

    return [PSCustomObject]@{
        status            = "error"
        database          = $activeDb
        project_namespace = $ProjectSlug
        details           = "neo4j-provision-unknown-error"
    }
}

function Ensure-V2QdrantProjectCollection {
    param(
        [object]$Connection,
        [string]$ProjectSlug
    )

    $disabledResult = [PSCustomObject]@{
        status     = "skipped"
        collection = ""
        details    = "qdrant-disabled"
    }
    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return $disabledResult
    }

    $url = [string](Get-V2OptionalProperty -InputObject $Connection -Name "url" -DefaultValue "")
    $prefix = [string](Get-V2OptionalProperty -InputObject $Connection -Name "collection_prefix" -DefaultValue "")
    $vectorSize = [int](Get-V2OptionalProperty -InputObject $Connection -Name "vector_size" -DefaultValue 768)
    if ($vectorSize -le 0) {
        $vectorSize = 768
    }
    if ([string]::IsNullOrWhiteSpace($url)) {
        return [PSCustomObject]@{
            status     = "error"
            collection = ""
            details    = "qdrant-url-missing"
        }
    }

    $normalizedPrefix = $prefix.Trim().Trim("-_")
    if (-not [string]::IsNullOrWhiteSpace($normalizedPrefix) -and $normalizedPrefix.ToLowerInvariant() -ne $ProjectSlug.ToLowerInvariant()) {
        $collectionName = "{0}-{1}-memory" -f $normalizedPrefix, $ProjectSlug
    }
    else {
        $collectionName = "{0}-memory" -f $ProjectSlug
    }
    $baseUrl = $url.TrimEnd("/")
    $listUri = "{0}/collections" -f $baseUrl
    $createUri = "{0}/collections/{1}" -f $baseUrl, $collectionName
    $createBody = @{
        vectors = @{
            size     = $vectorSize
            distance = "Cosine"
        }
    } | ConvertTo-Json -Depth 8

    $attempts = 5
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            $listResponse = Invoke-RestMethod -Uri $listUri -Method Get -TimeoutSec 10
            $collections = @(Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $listResponse -Name "result" -DefaultValue ([PSCustomObject]@{})) -Name "collections" -DefaultValue @())
            $collectionNames = @($collections | ForEach-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "name" -DefaultValue "") })
            if ($collectionNames -contains $collectionName) {
                return [PSCustomObject]@{
                    status     = "ready"
                    collection = $collectionName
                    details    = "qdrant-collection-exists"
                }
            }

            Invoke-RestMethod -Uri $createUri -Method Put -Body $createBody -ContentType "application/json" -TimeoutSec 10 | Out-Null
            return [PSCustomObject]@{
                status     = "ready"
                collection = $collectionName
                details    = "qdrant-collection-created"
            }
        }
        catch {
            if ($attempt -ge $attempts) {
                return [PSCustomObject]@{
                    status     = "error"
                    collection = $collectionName
                    details    = [string]$_.Exception.Message
                }
            }
            Start-Sleep -Seconds 2
        }
    }

    return [PSCustomObject]@{
        status     = "error"
        collection = $collectionName
        details    = "qdrant-provision-unknown-error"
    }
}

function ConvertTo-V2SqlLiteral {
    param([string]$Value)

    if ($null -eq $Value) {
        return "NULL"
    }

    return "'" + ($Value -replace "'", "''") + "'"
}

function Get-V2DeterministicGuid {
    param([string]$Seed)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Seed)
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $hash = $md5.ComputeHash($bytes)
    }
    finally {
        $md5.Dispose()
    }
    return New-Object System.Guid (,$hash)
}

function Invoke-V2ComposeExec {
    param(
        [string]$ComposeEngine,
        [string]$ComposePath,
        [string]$Service,
        [string[]]$Arguments,
        [hashtable]$Environment = @{}
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    $dockerConfigContext = $null
    $fallbackUsed = $false
    $fallbackAttempted = $false
    $dockerHostProactive = ""
    $dockerHostProactiveUsed = $false
    $previousDockerHostExists = $false
    $previousDockerHostValue = ""
    try {
        $ErrorActionPreference = "Continue"
        $dockerConfigContext = Enter-V2DockerConfigFallbackContext
        $dockerHostProactive = Get-V2PreferredDockerHost
        if (Test-Path Env:DOCKER_HOST) {
            $previousDockerHostExists = $true
            $previousDockerHostValue = [string]$env:DOCKER_HOST
        }
        if (-not [string]::IsNullOrWhiteSpace($dockerHostProactive) -and -not $previousDockerHostExists) {
            $env:DOCKER_HOST = $dockerHostProactive
            $dockerHostProactiveUsed = $true
        }

        $envArgs = @()
        foreach ($key in @($Environment.Keys | Sort-Object)) {
            $envArgs += @("-e", ("{0}={1}" -f $key, [string]$Environment[$key]))
        }

        $hasDockerCli = ($null -ne (Get-Command docker -ErrorAction SilentlyContinue))
        $hasDockerComposeV1 = ($null -ne (Get-Command docker-compose -ErrorAction SilentlyContinue))
        $preferComposeV2 = ($ComposeEngine -eq "docker-compose-v2") -or $hasDockerCli
        $dockerConfigDir = [string](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "selected_config_dir" -DefaultValue "")
        if ($preferComposeV2) {
            if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                $output = @(& docker --config $dockerConfigDir compose -f $ComposePath exec -T @($envArgs) $Service @($Arguments) 2>&1)
            }
            else {
                $output = @(& docker compose -f $ComposePath exec -T @($envArgs) $Service @($Arguments) 2>&1)
            }
            $exitCode = $LASTEXITCODE
            $outputTextV2 = (($output | Out-String).Trim())
            if ($exitCode -ne 0 -and $hasDockerComposeV1 -and $outputTextV2 -match "(?i)is not a docker command|unknown command|no such command|unknown shorthand flag:\s*'f'\s*in\s*-f") {
                $cmdArgs = @("-f", $ComposePath, "exec", "-T") + $envArgs + @($Service) + @($Arguments)
                $output = @(& docker-compose @cmdArgs 2>&1)
                $exitCode = $LASTEXITCODE
            }
        }
        else {
            $cmdArgs = @("-f", $ComposePath, "exec", "-T") + $envArgs + @($Service) + @($Arguments)
            $output = @(& docker-compose @cmdArgs 2>&1)
            $exitCode = $LASTEXITCODE
        }

        $outputText = (($output | Out-String).Trim())
        $needsDockerHostFallback = ($exitCode -ne 0) -and ($outputText -match "(?i)access is denied|permission denied|open //\./pipe/docker_engine")
        if ($needsDockerHostFallback) {
            $fallbackAttempted = $true
            if (Test-V2PortOpen -Port 2375 -TimeoutMs 800) {
                $fallbackPreviousExists = Test-Path Env:DOCKER_HOST
                $fallbackPreviousValue = if ($fallbackPreviousExists) { [string]$env:DOCKER_HOST } else { "" }
                try {
                    $env:DOCKER_HOST = "tcp://localhost:2375"
                    if ($preferComposeV2) {
                        if (-not [string]::IsNullOrWhiteSpace($dockerConfigDir)) {
                            $retryOutput = @(& docker --config $dockerConfigDir compose -f $ComposePath exec -T @($envArgs) $Service @($Arguments) 2>&1)
                        }
                        else {
                            $retryOutput = @(& docker compose -f $ComposePath exec -T @($envArgs) $Service @($Arguments) 2>&1)
                        }
                        $retryExitCode = $LASTEXITCODE
                        $retryTextV2 = (($retryOutput | Out-String).Trim())
                        if ($retryExitCode -ne 0 -and $hasDockerComposeV1 -and $retryTextV2 -match "(?i)is not a docker command|unknown command|no such command|unknown shorthand flag:\s*'f'\s*in\s*-f") {
                            $cmdArgs = @("-f", $ComposePath, "exec", "-T") + $envArgs + @($Service) + @($Arguments)
                            $retryOutput = @(& docker-compose @cmdArgs 2>&1)
                            $retryExitCode = $LASTEXITCODE
                        }
                    }
                    else {
                        $cmdArgs = @("-f", $ComposePath, "exec", "-T") + $envArgs + @($Service) + @($Arguments)
                        $retryOutput = @(& docker-compose @cmdArgs 2>&1)
                        $retryExitCode = $LASTEXITCODE
                    }
                    if ($retryExitCode -eq 0) {
                        $output = @($retryOutput)
                        $exitCode = $retryExitCode
                        $fallbackUsed = $true
                    }
                    else {
                        $retryText = (($retryOutput | Out-String).Trim())
                        $output = @($output + @("docker-host-fallback-attempt-failed: $retryText"))
                    }
                }
                finally {
                    if ($fallbackPreviousExists) {
                        $env:DOCKER_HOST = $fallbackPreviousValue
                    }
                    else {
                        Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue
                    }
                }
            }
            else {
                $output = @($output + @("docker-host-fallback-unavailable: tcp://localhost:2375 not reachable"))
            }
        }
    }
    finally {
        if ($dockerHostProactiveUsed) {
            if ($previousDockerHostExists) {
                $env:DOCKER_HOST = $previousDockerHostValue
            }
            else {
                Remove-Item Env:DOCKER_HOST -ErrorAction SilentlyContinue
            }
        }
        Exit-V2DockerConfigFallbackContext -Context $dockerConfigContext
        $ErrorActionPreference = $previousErrorActionPreference
    }

    return [PSCustomObject]@{
        success            = ($exitCode -eq 0)
        exit_code          = $exitCode
        output             = @($output)
        text               = (($output | Out-String).Trim())
        docker_host_proactive      = $dockerHostProactive
        docker_host_proactive_used = $dockerHostProactiveUsed
        docker_config_fallback_used = [bool](Get-V2OptionalProperty -InputObject $dockerConfigContext -Name "fallback_used" -DefaultValue $false)
        docker_host_fallback_attempted = $fallbackAttempted
        docker_host_fallback_used      = $fallbackUsed
    }
}

function Get-V2CanonicalRelationalEngine {
    param([string]$Engine)

    $value = ([string]$Engine).Trim().ToLowerInvariant()
    switch ($value) {
        "postgresql" { return "postgres" }
        "pgsql" { return "postgres" }
        "postgres" { return "postgres" }
        default { return $value }
    }
}

function Ensure-V2RelationalProjectSeed {
    param(
        [string]$ComposeEngine,
        [string]$ComposePath,
        [object]$Connection,
        [string]$ProjectSlug,
        [string]$ProjectName,
        [string]$ProjectSchema = ""
    )

    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return [PSCustomObject]@{
            status          = "skipped"
            backend         = "relational"
            records_seeded  = 0
            details         = "relational-disabled"
        }
    }

    $engine = Get-V2CanonicalRelationalEngine -Engine ([string](Get-V2OptionalProperty -InputObject $Connection -Name "engine" -DefaultValue "unknown"))
    if ($engine -ne "postgres") {
        return [PSCustomObject]@{
            status          = "skipped"
            backend         = "relational"
            records_seeded  = 0
            details         = "relational-seed-not-supported-for-engine:$engine"
        }
    }

    $dbName = [string](Get-V2OptionalProperty -InputObject $Connection -Name "database" -DefaultValue "")
    $dbUser = [string](Get-V2OptionalProperty -InputObject $Connection -Name "user" -DefaultValue "")
    $dbPassword = [string](Get-V2OptionalProperty -InputObject $Connection -Name "password" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($dbName) -or [string]::IsNullOrWhiteSpace($dbUser)) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational"
            records_seeded  = 0
            details         = "relational-seed-missing-db-credentials"
        }
    }
    if ([string]::IsNullOrWhiteSpace($ProjectSchema)) {
        $ProjectSchema = Get-V2ProjectSchemaName -ProjectSlug $ProjectSlug
    }

    $slugLiteral = ConvertTo-V2SqlLiteral -Value $ProjectSlug
    $nameLiteral = ConvertTo-V2SqlLiteral -Value $ProjectName
    $sourceLiteral = ConvertTo-V2SqlLiteral -Value "v2-submit"
    $schemaQuoted = '"' + ($ProjectSchema -replace '"', '""') + '"'
    $schemaLiteral = ConvertTo-V2SqlLiteral -Value $ProjectSchema
    $sql = @"
CREATE SCHEMA IF NOT EXISTS ai_orchestrator;
CREATE SCHEMA IF NOT EXISTS $schemaQuoted;
CREATE TABLE IF NOT EXISTS ai_orchestrator.project_registry (
  project_slug TEXT PRIMARY KEY,
  project_name TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO ai_orchestrator.project_registry (project_slug, project_name, source, created_at, updated_at)
VALUES ($slugLiteral, $nameLiteral, $sourceLiteral, NOW(), NOW())
ON CONFLICT (project_slug) DO UPDATE
SET project_name = EXCLUDED.project_name,
    source = EXCLUDED.source,
    updated_at = NOW();
SELECT COUNT(*)::BIGINT
FROM ai_orchestrator.project_registry
WHERE project_slug = $slugLiteral;
SELECT COUNT(*)::BIGINT
FROM information_schema.schemata
WHERE schema_name = $schemaLiteral;
"@
    $command = @"
cat <<'SQL' | psql -h 127.0.0.1 -U "$dbUser" -d "$dbName" -v ON_ERROR_STOP=1 -tA
$sql
SQL
"@
    $exec = Invoke-V2ComposeExec `
        -ComposeEngine $ComposeEngine `
        -ComposePath $ComposePath `
        -Service "db" `
        -Arguments @("sh", "-lc", $command) `
        -Environment @{ PGPASSWORD = $dbPassword }

    if (-not $exec.success) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational"
            records_seeded  = 0
            details         = "relational-seed-command-failed: $([string]$exec.text)"
        }
    }

    $seededCount = 0
    $schemaExists = $false
    $countMatches = [regex]::Matches([string]$exec.text, "(?<count>\d+)\s*$", [System.Text.RegularExpressions.RegexOptions]::Multiline)
    if ($countMatches.Count -ge 1) {
        $seededCount = [int]$countMatches[0].Groups["count"].Value
    }
    if ($countMatches.Count -ge 2) {
        $schemaExists = ([int]$countMatches[1].Groups["count"].Value) -gt 0
    }

    return [PSCustomObject]@{
        status          = "ready"
        backend         = "relational"
        records_seeded  = $seededCount
        schema          = $ProjectSchema
        schema_ready    = $schemaExists
        details         = "relational-project-registry-upserted"
    }
}

function Ensure-V2RelationalDomainMigrations {
    param(
        [string]$ComposeEngine,
        [string]$ComposePath,
        [object]$Connection,
        [string]$ProjectPath
    )

    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return [PSCustomObject]@{
            status          = "skipped"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "relational-disabled"
        }
    }

    $engine = Get-V2CanonicalRelationalEngine -Engine ([string](Get-V2OptionalProperty -InputObject $Connection -Name "engine" -DefaultValue "unknown"))
    if ($engine -ne "postgres") {
        return [PSCustomObject]@{
            status          = "skipped"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-not-supported-for-engine:$engine"
        }
    }

    if ([string]::IsNullOrWhiteSpace($ProjectPath) -or -not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-project-path-not-found"
        }
    }

    $alembicIniPath = Join-Path $ProjectPath "alembic.ini"
    $alembicDirPath = Join-Path $ProjectPath "alembic"
    if (-not (Test-Path -LiteralPath $alembicIniPath -PathType Leaf) -or -not (Test-Path -LiteralPath $alembicDirPath -PathType Container)) {
        return [PSCustomObject]@{
            status          = "skipped"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-skipped-no-alembic-scaffold"
        }
    }

    $dbName = [string](Get-V2OptionalProperty -InputObject $Connection -Name "database" -DefaultValue "")
    $dbUser = [string](Get-V2OptionalProperty -InputObject $Connection -Name "user" -DefaultValue "")
    $dbPassword = [string](Get-V2OptionalProperty -InputObject $Connection -Name "password" -DefaultValue "")
    $dbHost = [string](Get-V2OptionalProperty -InputObject $Connection -Name "host" -DefaultValue "localhost")
    $dbPort = [int](Get-V2OptionalProperty -InputObject $Connection -Name "port" -DefaultValue 5432)
    if ([string]::IsNullOrWhiteSpace($dbName) -or [string]::IsNullOrWhiteSpace($dbUser)) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-missing-db-credentials"
        }
    }

    $userEscaped = [System.Uri]::EscapeDataString($dbUser)
    $passwordEscaped = [System.Uri]::EscapeDataString($dbPassword)
    $databaseUrl = "postgresql+psycopg://{0}:{1}@{2}:{3}/{4}" -f $userEscaped, $passwordEscaped, $dbHost, $dbPort, $dbName

    $migrationOutput = @()
    $migrationExitCode = 1
    $previousDatabaseUrlExists = $false
    $previousDatabaseUrl = ""
    $previousAlembicDatabaseUrlExists = $false
    $previousAlembicDatabaseUrl = ""
    if (Test-Path Env:DATABASE_URL) {
        $previousDatabaseUrlExists = $true
        $previousDatabaseUrl = $env:DATABASE_URL
    }
    if (Test-Path Env:ALEMBIC_DATABASE_URL) {
        $previousAlembicDatabaseUrlExists = $true
        $previousAlembicDatabaseUrl = $env:ALEMBIC_DATABASE_URL
    }

    try {
        Push-Location $ProjectPath
        $env:DATABASE_URL = $databaseUrl
        $env:ALEMBIC_DATABASE_URL = $databaseUrl
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $alembicEnvPath = Join-Path $ProjectPath "alembic/env.py"
            $useConnectionInjectedAlembic = $false
            if (Test-Path -LiteralPath $alembicEnvPath -PathType Leaf) {
                $alembicEnvContent = Get-Content -LiteralPath $alembicEnvPath -Raw -ErrorAction SilentlyContinue
                if (-not [string]::IsNullOrWhiteSpace($alembicEnvContent) -and $alembicEnvContent -match "No SQLAlchemy connection configured for migrations") {
                    $useConnectionInjectedAlembic = $true
                }
            }

            if ($useConnectionInjectedAlembic) {
                $inlinePython = @'
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
import os

url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url:
    raise RuntimeError("No SQLAlchemy connection configured for migrations.")

config = Config("alembic.ini")
config.set_main_option("sqlalchemy.url", url)
engine = create_engine(url)
try:
    config.attributes["connection"] = engine
    command.upgrade(config, "head")
finally:
    engine.dispose()
'@
                $migrationOutput = @($inlinePython | python - 2>&1)
                $migrationExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
            }
            else {
                $migrationOutput = @(& python -m alembic upgrade head 2>&1)
                $migrationExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
            }
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
    catch {
        $migrationOutput = @($_.Exception.Message)
        $migrationExitCode = 1
    }
    finally {
        if ($previousDatabaseUrlExists) {
            $env:DATABASE_URL = $previousDatabaseUrl
        }
        else {
            Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
        }
        if ($previousAlembicDatabaseUrlExists) {
            $env:ALEMBIC_DATABASE_URL = $previousAlembicDatabaseUrl
        }
        else {
            Remove-Item Env:ALEMBIC_DATABASE_URL -ErrorAction SilentlyContinue
        }
        Pop-Location
    }

    if ($migrationExitCode -ne 0) {
        $tail = (($migrationOutput | Out-String).Trim())
        if ($tail.Length -gt 900) {
            $tail = $tail.Substring($tail.Length - 900)
        }
        if ($tail -match "ModuleNotFoundError:\s+No module named ['""]app['""]" -or $tail -match "No module named ['""]app['""]") {
            return [PSCustomObject]@{
                status          = "skipped"
                backend         = "relational-domain"
                records_seeded  = 0
                details         = "domain-migration-deferred-scaffold-missing"
            }
        }
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-failed: $tail"
        }
    }

    $domainTableSql = "SELECT COUNT(*)::BIGINT FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema','ai_orchestrator');"
    $domainCountCommand = @"
cat <<'SQL' | psql -h 127.0.0.1 -U "$dbUser" -d "$dbName" -v ON_ERROR_STOP=1 -tA
$domainTableSql
SQL
"@
    $domainExec = Invoke-V2ComposeExec `
        -ComposeEngine $ComposeEngine `
        -ComposePath $ComposePath `
        -Service "db" `
        -Arguments @("sh", "-lc", $domainCountCommand) `
        -Environment @{ PGPASSWORD = $dbPassword }

    if (-not $domainExec.success) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-verify-failed: $([string]$domainExec.text)"
        }
    }

    $domainTableCount = 0
    $domainMatch = [regex]::Match([string]$domainExec.text, "(?<count>\d+)\s*$")
    if ($domainMatch.Success) {
        $domainTableCount = [int]$domainMatch.Groups["count"].Value
    }

    if ($domainTableCount -le 0) {
        return [PSCustomObject]@{
            status          = "error"
            backend         = "relational-domain"
            records_seeded  = 0
            details         = "domain-migration-applied-but-no-domain-tables-found"
        }
    }

    return [PSCustomObject]@{
        status          = "ready"
        backend         = "relational-domain"
        records_seeded  = $domainTableCount
        details         = "alembic-upgrade-head-applied"
    }
}

function Ensure-V2Neo4jBootstrapSeed {
    param(
        [object]$Connection,
        [string]$ProjectSlug
    )

    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return [PSCustomObject]@{
            status         = "skipped"
            backend        = "neo4j"
            records_seeded = 0
            details        = "neo4j-disabled"
        }
    }

    $serverHost = [string](Get-V2OptionalProperty -InputObject $Connection -Name "host" -DefaultValue "localhost")
    $httpPort = [int](Get-V2OptionalProperty -InputObject $Connection -Name "http_port" -DefaultValue 0)
    $username = [string](Get-V2OptionalProperty -InputObject $Connection -Name "user" -DefaultValue "neo4j")
    $password = [string](Get-V2OptionalProperty -InputObject $Connection -Name "password" -DefaultValue "")
    $database = [string](Get-V2OptionalProperty -InputObject $Connection -Name "database" -DefaultValue "neo4j")
    if ($httpPort -le 0 -or [string]::IsNullOrWhiteSpace($password)) {
        return [PSCustomObject]@{
            status         = "error"
            backend        = "neo4j"
            records_seeded = 0
            details        = "neo4j-seed-missing-connection-parameters"
        }
    }

    $baseUrl = "http://{0}:{1}" -f $serverHost, $httpPort
    $headers = @{
        Authorization = (New-V2BasicAuthHeader -Username $username -Password $password)
        "Content-Type" = "application/json"
    }

    try {
        Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database $database -Headers $headers -Statements @(
            @{
                statement = @"
MERGE (p:Project {slug: `$project_slug})
SET p.last_bootstrap_at = datetime()
MERGE (b:BootstrapState {project_slug: `$project_slug, kind: 'v2-submit'})
ON CREATE SET b.created_at = datetime()
SET b.updated_at = datetime(), b.status = 'ready'
MERGE (p)-[:HAS_BOOTSTRAP]->(b)
"@
                parameters = @{ project_slug = $ProjectSlug }
            }
        ) | Out-Null

        $countResponse = Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database $database -Headers $headers -Statements @(
            @{
                statement = "MATCH (p:Project {slug: `$project_slug}) RETURN count(p) AS total"
                parameters = @{ project_slug = $ProjectSlug }
            }
        )

        $projectCount = 0
        $results = @(Get-V2OptionalProperty -InputObject $countResponse -Name "results" -DefaultValue @())
        if ($results.Count -gt 0) {
            $dataRows = @(Get-V2OptionalProperty -InputObject $results[0] -Name "data" -DefaultValue @())
            if ($dataRows.Count -gt 0) {
                $row = @(Get-V2OptionalProperty -InputObject $dataRows[0] -Name "row" -DefaultValue @())
                if ($row.Count -gt 0) {
                    $projectCount = [int]$row[0]
                }
            }
        }

        return [PSCustomObject]@{
            status         = "ready"
            backend        = "neo4j"
            records_seeded = $projectCount
            details        = "neo4j-bootstrap-state-upserted"
        }
    }
    catch {
        return [PSCustomObject]@{
            status         = "error"
            backend        = "neo4j"
            records_seeded = 0
            details        = [string]$_.Exception.Message
        }
    }
}

function Ensure-V2QdrantBootstrapSeed {
    param(
        [object]$Connection,
        [string]$ProjectSlug
    )

    if (-not [bool](Get-V2OptionalProperty -InputObject $Connection -Name "enabled" -DefaultValue $false)) {
        return [PSCustomObject]@{
            status         = "skipped"
            backend        = "qdrant"
            records_seeded = 0
            details        = "qdrant-disabled"
        }
    }

    $url = [string](Get-V2OptionalProperty -InputObject $Connection -Name "url" -DefaultValue "")
    $collection = [string](Get-V2OptionalProperty -InputObject $Connection -Name "collection" -DefaultValue "")
    $vectorSize = [int](Get-V2OptionalProperty -InputObject $Connection -Name "vector_size" -DefaultValue 768)
    if ($vectorSize -le 0) {
        $vectorSize = 768
    }
    if ([string]::IsNullOrWhiteSpace($url) -or [string]::IsNullOrWhiteSpace($collection)) {
        return [PSCustomObject]@{
            status         = "error"
            backend        = "qdrant"
            records_seeded = 0
            details        = "qdrant-seed-missing-connection-parameters"
        }
    }

    $baseUrl = $url.TrimEnd("/")
    $vector = New-Object System.Collections.Generic.List[double]
    for ($index = 0; $index -lt $vectorSize; $index++) {
        $vector.Add(0.0)
    }

    $pointId = (Get-V2DeterministicGuid -Seed ("bootstrap:{0}" -f $ProjectSlug)).ToString()
    $upsertBody = @{
        points = @(
            @{
                id      = $pointId
                vector  = @($vector.ToArray())
                payload = @{
                    project_slug = $ProjectSlug
                    node_type    = "bootstrap"
                    summary      = "Bootstrap marker for project memory collection."
                    details      = "Created by v2-submit bootstrap validation."
                    kind         = "bootstrap"
                    source       = "v2-submit"
                    embedding_source = "bootstrap_seed"
                    generated_at = Get-V2Timestamp
                }
            }
        )
    } | ConvertTo-Json -Depth 16

    try {
        $upsertUri = "{0}/collections/{1}/points?wait=true" -f $baseUrl, $collection
        Invoke-RestMethod -Uri $upsertUri -Method Put -Body $upsertBody -ContentType "application/json" -TimeoutSec 15 | Out-Null

        $countUri = "{0}/collections/{1}/points/count" -f $baseUrl, $collection
        $countResponse = Invoke-RestMethod -Uri $countUri -Method Post -Body (@{ exact = $true } | ConvertTo-Json) -ContentType "application/json" -TimeoutSec 10
        $pointCount = [int](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $countResponse -Name "result" -DefaultValue ([PSCustomObject]@{})) -Name "count" -DefaultValue 0)

        return [PSCustomObject]@{
            status         = "ready"
            backend        = "qdrant"
            records_seeded = $pointCount
            details        = "qdrant-bootstrap-point-upserted"
        }
    }
    catch {
        return [PSCustomObject]@{
            status         = "error"
            backend        = "qdrant"
            records_seeded = 0
            details        = [string]$_.Exception.Message
        }
    }
}

function Invoke-V2BootstrapVerification {
    param(
        [string]$ComposeEngine,
        [string]$ComposePath,
        [object]$DbConnection,
        [object]$Neo4jConnection,
        [object]$QdrantConnection,
        [string]$ProjectPath,
        [string]$ProjectSlug,
        [string]$ProjectName,
        [string]$ProjectSchema,
        [string]$InfraMode = "dedicated-infra"
    )

    $relational = Ensure-V2RelationalProjectSeed `
        -ComposeEngine $ComposeEngine `
        -ComposePath $ComposePath `
        -Connection $DbConnection `
        -ProjectSlug $ProjectSlug `
        -ProjectName $ProjectName `
        -ProjectSchema $ProjectSchema
    $neo4j = Ensure-V2Neo4jBootstrapSeed -Connection $Neo4jConnection -ProjectSlug $ProjectSlug
    $qdrant = Ensure-V2QdrantBootstrapSeed -Connection $QdrantConnection -ProjectSlug $ProjectSlug
    $relationalDomain = Ensure-V2RelationalDomainMigrations `
        -ComposeEngine $ComposeEngine `
        -ComposePath $ComposePath `
        -Connection $DbConnection `
        -ProjectPath $ProjectPath

    $errors = New-Object System.Collections.Generic.List[string]
    $notes = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($relational, $neo4j, $qdrant, $relationalDomain)) {
        $status = [string](Get-V2OptionalProperty -InputObject $item -Name "status" -DefaultValue "unknown")
        $backend = [string](Get-V2OptionalProperty -InputObject $item -Name "backend" -DefaultValue "unknown")
        $details = [string](Get-V2OptionalProperty -InputObject $item -Name "details" -DefaultValue "")
        $recordsSeeded = [int](Get-V2OptionalProperty -InputObject $item -Name "records_seeded" -DefaultValue 0)
        if ($status -like "error*") {
            $errors.Add([string]::Format("{0}-verify-seed-failed: {1}", $backend, $details))
        }
        else {
            $notes.Add([string]::Format("{0}-verify-seed: status={1} records={2} detail={3}", $backend, $status, $recordsSeeded, $details))
        }
    }
    $notes.Add("infra-mode: $InfraMode")

    return [PSCustomObject]@{
        status      = if ($errors.Count -eq 0) { "ready" } else { "error" }
        verification = [PSCustomObject]@{
            relational        = $relational
            neo4j             = $neo4j
            qdrant            = $qdrant
            relational_domain = $relationalDomain
        }
        errors      = @($errors.ToArray())
        notes       = @($notes.ToArray())
    }
}

function Write-V2AccessGuide {
    param(
        [string]$OrchestratorRoot,
        [string]$ScopedProjectRoot,
        [string]$ProjectName,
        [string]$ProjectSlug,
        [object]$DbConnection,
        [object]$Neo4jConnection,
        [object]$QdrantConnection,
        [string]$SecretsVaultRelativePath = "",
        [object]$BootstrapVerification,
        [string[]]$BootstrapNotes,
        [string[]]$BootstrapErrors
    )

    $databaseDir = Join-Path $OrchestratorRoot "database"
    Initialize-V2Directory -Path $databaseDir
    $accessGuidePath = Join-Path $databaseDir "access.md"

    $neo4jHttpPort = [int](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name "http_port" -DefaultValue 0)
    $neo4jHttpUrl = if ($neo4jHttpPort -gt 0) { "http://localhost:$neo4jHttpPort" } else { "" }
    $neo4jBrowserUrl = if ([string]::IsNullOrWhiteSpace($neo4jHttpUrl)) { "" } else { "$neo4jHttpUrl/browser/" }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Access Credentials")
    $lines.Add("")
    $lines.Add("- Generated At: $(Get-V2Timestamp)")
    $lines.Add("- Project: $ProjectName ($ProjectSlug)")
    $lines.Add("")
    $lines.Add("## Relational Database")
    $lines.Add("- enabled: $([bool](Get-V2OptionalProperty -InputObject $DbConnection -Name 'enabled' -DefaultValue $false))")
    $lines.Add("- engine: $([string](Get-V2OptionalProperty -InputObject $DbConnection -Name 'engine' -DefaultValue 'unknown'))")
    $lines.Add("- host: $([string](Get-V2OptionalProperty -InputObject $DbConnection -Name 'host' -DefaultValue 'localhost'))")
    $lines.Add("- port: $([int](Get-V2OptionalProperty -InputObject $DbConnection -Name 'port' -DefaultValue 0))")
    $lines.Add("- database: $([string](Get-V2OptionalProperty -InputObject $DbConnection -Name 'database' -DefaultValue ''))")
    $lines.Add("- user: $([string](Get-V2OptionalProperty -InputObject $DbConnection -Name 'user' -DefaultValue ''))")
    $lines.Add("- password: [stored in vault]")
    $lines.Add("")
    $lines.Add("## Neo4j")
    $lines.Add("- enabled: $([bool](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name 'enabled' -DefaultValue $false))")
    $lines.Add("- bolt_uri: $([string](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name 'uri' -DefaultValue ''))")
    $lines.Add("- browser_url: $neo4jBrowserUrl")
    $lines.Add("- database: $([string](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name 'database' -DefaultValue 'neo4j'))")
    $lines.Add("- user: $([string](Get-V2OptionalProperty -InputObject $Neo4jConnection -Name 'user' -DefaultValue 'neo4j'))")
    $lines.Add("- password: [stored in vault]")
    $lines.Add("")
    $lines.Add("## Qdrant")
    $lines.Add("- enabled: $([bool](Get-V2OptionalProperty -InputObject $QdrantConnection -Name 'enabled' -DefaultValue $false))")
    $lines.Add("- url: $([string](Get-V2OptionalProperty -InputObject $QdrantConnection -Name 'url' -DefaultValue ''))")
    $lines.Add("- collection: $([string](Get-V2OptionalProperty -InputObject $QdrantConnection -Name 'collection' -DefaultValue ''))")
    $lines.Add("")
    if ($null -ne $BootstrapVerification) {
        $lines.Add("## Bootstrap Verification")
        foreach ($backend in @("relational", "neo4j", "qdrant")) {
            $item = Get-V2OptionalProperty -InputObject $BootstrapVerification -Name $backend -DefaultValue $null
            if ($null -eq $item) {
                continue
            }

            $status = [string](Get-V2OptionalProperty -InputObject $item -Name "status" -DefaultValue "unknown")
            $records = [int](Get-V2OptionalProperty -InputObject $item -Name "records_seeded" -DefaultValue 0)
            $details = [string](Get-V2OptionalProperty -InputObject $item -Name "details" -DefaultValue "")
            $lines.Add("- ${backend}: status=$status records=$records details=$details")
        }
        $lines.Add("")
    }
    if (@($BootstrapNotes).Count -gt 0) {
        $lines.Add("## Bootstrap Notes")
        foreach ($note in @($BootstrapNotes)) {
            $lines.Add("- $note")
        }
        $lines.Add("")
    }
    if (@($BootstrapErrors).Count -gt 0) {
        $lines.Add("## Bootstrap Errors")
        foreach ($err in @($BootstrapErrors)) {
            $lines.Add("- $err")
        }
        $lines.Add("")
    }
    if (-not [string]::IsNullOrWhiteSpace($SecretsVaultRelativePath)) {
        $lines.Add("## Secret Vault")
        $lines.Add("- path: $SecretsVaultRelativePath")
        $lines.Add("- policy: credentials are persisted only in vault and masked in state/output")
        $lines.Add("")
    }
    $lines.Add("## How To Connect")
    $lines.Add("1. Open Neo4j Browser at the browser_url above.")
    $lines.Add("2. Use bolt_uri + user + password loaded from secret vault.")
    $lines.Add("3. If connection fails, check remapped ports in ai-orchestrator/project-pack/DOCKER_PORTS.json.")

    Write-V2File -Path $accessGuidePath -Content ($lines -join [Environment]::NewLine) -Force

    if (-not [string]::IsNullOrWhiteSpace($ScopedProjectRoot)) {
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "pack")
        $scopedAccessPath = Join-Path $ScopedProjectRoot "pack/access.md"
        Write-V2File -Path $scopedAccessPath -Content ($lines -join [Environment]::NewLine) -Force
    }

    return $accessGuidePath
}

function Invoke-V2ProjectPackBootstrap {
    param(
        [string]$ProjectPath,
        [string]$OrchestratorRoot,
        [string]$ProjectName,
        [string]$ScopedProjectRoot,
        [object]$Intake,
        [ValidateSet("dedicated-infra", "shared-infra")]
        [string]$InfraMode = "dedicated-infra",
        [switch]$EnableNeo4j,
        [switch]$EnableQdrant
    )

    $projectSlug = Get-V2ProjectSlug -Name $ProjectName
    $projectSchema = Get-V2ProjectSchemaName -ProjectSlug $projectSlug
    $dockerScript = Join-Path $PSScriptRoot "Invoke-DockerAutoBuilderV2.ps1"
    $composePath = ""
    $envPath = ""
    $dockerProjectPath = $ProjectPath
    $infraScope = if ($InfraMode -eq "shared-infra") { "shared" } else { "dedicated" }
    if ($InfraMode -eq "shared-infra") {
        $sharedInfraRoot = Join-Path $coordinationRoot "workspace/shared-infra"
        Initialize-V2Directory -Path $sharedInfraRoot
        $dockerProjectPath = $sharedInfraRoot
        $composePath = Join-Path $sharedInfraRoot "ai-orchestrator/docker/docker-compose.generated.yml"
        $envPath = Join-Path $sharedInfraRoot "ai-orchestrator/docker/.env.docker.generated"
    }
    else {
        $composePath = Join-Path $OrchestratorRoot "docker/docker-compose.generated.yml"
        $envPath = Join-Path $OrchestratorRoot "docker/.env.docker.generated"
    }

    $errors = New-Object System.Collections.Generic.List[string]
    $notes = New-Object System.Collections.Generic.List[string]
    $coordinationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $orchestratorCoreBootstrap = Invoke-V2OrchestratorCoreBootstrap -CoordinationRoot $coordinationRoot
    foreach ($coreNote in @((Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "notes" -DefaultValue @()))) {
        if (-not [string]::IsNullOrWhiteSpace([string]$coreNote)) {
            $notes.Add("orchestrator-core::$coreNote")
        }
    }
    foreach ($coreError in @((Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "errors" -DefaultValue @()))) {
        if (-not [string]::IsNullOrWhiteSpace([string]$coreError)) {
            $errors.Add("orchestrator-core::$coreError")
        }
    }

    $requestedDatabase = [string]$Database
    $detectedDbEngine = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $Intake.technical_fingerprint -Name "database" -DefaultValue ([PSCustomObject]@{ engine = "unknown" })) -Name "engine" -DefaultValue "unknown")
    $bootstrapDatabase = $requestedDatabase
    if ($requestedDatabase -eq "none") {
        $bootstrapDatabase = "postgres"
        $notes.Add("database-override: requested=none -> bootstrap=postgres (mandatory relational storage)")
    }
    elseif ($requestedDatabase -eq "auto") {
        if ($detectedDbEngine -in @("postgres", "mysql", "mongodb")) {
            $bootstrapDatabase = $detectedDbEngine
        }
        else {
            $bootstrapDatabase = "postgres"
            $notes.Add("database-override: requested=auto detected=$detectedDbEngine -> bootstrap=postgres (mandatory relational storage)")
        }
    }

    try {
        $dockerParams = @{
            ProjectPath = $dockerProjectPath
            Stack       = $Stack
            Database    = $bootstrapDatabase
            Force       = $true
        }
        if ($EnableNeo4j) { $dockerParams.IncludeNeo4j = $true }
        if ($EnableQdrant) { $dockerParams.IncludeQdrant = $true }
        if ($IncludeRedis) { $dockerParams.IncludeRedis = $true }
        if ($IncludeRabbitMq) { $dockerParams.IncludeRabbitMq = $true }
        if ($IncludeWorker) { $dockerParams.IncludeWorker = $true }
        & $dockerScript @dockerParams | Out-Null
        if ($InfraMode -eq "shared-infra") {
            $notes.Add("infra-mode-shared: docker assets and runtime use workspace/shared-infra")
        }
    }
    catch {
        $errors.Add("docker-builder-failed: $($_.Exception.Message)")
    }

    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        $errors.Add("compose-not-generated: $composePath")
    }

    $topology = Get-V2ComposeTopology -ComposePath $composePath
    $containerIsolation = [PSCustomObject]@{
        success    = $true
        status     = "skipped"
        notes      = @("isolation-check-skipped")
        errors     = @()
        containers = @()
    }
    if ($InfraMode -eq "dedicated-infra") {
        $containerIsolation = Test-V2DedicatedProjectContainerIsolation -ComposePath $composePath -ProjectSlug $projectSlug
        foreach ($isoNote in @((Get-V2OptionalProperty -InputObject $containerIsolation -Name "notes" -DefaultValue @()))) {
            if (-not [string]::IsNullOrWhiteSpace([string]$isoNote)) {
                $notes.Add("container-isolation::$isoNote")
            }
        }
        foreach ($isoError in @((Get-V2OptionalProperty -InputObject $containerIsolation -Name "errors" -DefaultValue @()))) {
            if (-not [string]::IsNullOrWhiteSpace([string]$isoError)) {
                $errors.Add("container-isolation::$isoError")
            }
        }
    }
    else {
        $notes.Add("container-isolation::shared-infra-mode-selected (project-level strict isolation disabled)")
    }

    $requiredServices = New-Object System.Collections.Generic.List[string]
    $requiredByContract = New-Object System.Collections.Generic.List[string]
    $requiredByContract.Add("db")
    if ($EnableNeo4j) { $requiredByContract.Add("neo4j") }
    if ($EnableQdrant) { $requiredByContract.Add("qdrant") }
    foreach ($required in @($requiredByContract.ToArray())) {
        if (@($topology.services) -contains $required) {
            $requiredServices.Add($required)
        }
        else {
            $errors.Add("missing-required-service: $required")
        }
    }

    $remaps = New-Object System.Collections.Generic.List[object]
    $usedPorts = Get-V2UsedHostPorts
    foreach ($mapping in @($topology.ports | Sort-Object line_index)) {
        $currentPort = [int]$mapping.host_port
        if ($usedPorts.Contains($currentPort)) {
            $nextPort = Find-V2AvailablePort -PreferredPort ($currentPort + 1) -UsedPorts $usedPorts
            if ($nextPort -gt 0) {
                [void]$usedPorts.Add($nextPort)
                $notes.Add("port-remap: service=$($mapping.service) $currentPort -> $nextPort")
                $remaps.Add([PSCustomObject]@{
                        service        = $mapping.service
                        host_port      = $mapping.host_port
                        new_host_port  = $nextPort
                        container_port = $mapping.container_port
                        line_index     = $mapping.line_index
                        indent         = $mapping.indent
                        quoted         = $mapping.quoted
                    })
            }
            else {
                $errors.Add("port-remap-failed: service=$($mapping.service) port=$currentPort")
                $notes.Add("port-remap-proposal: define a dedicated project range (example 25000-29000) and rerun bootstrap.")
            }
        }
        else {
            [void]$usedPorts.Add($currentPort)
        }
    }

    if ($remaps.Count -gt 0) {
        Set-V2ComposePortRemaps -ComposePath $composePath -Remaps @($remaps.ToArray())
        $topology = Get-V2ComposeTopology -ComposePath $composePath
    }

    $composeEngine = Get-V2DockerComposeEngine
    $dockerDaemonStatus = Get-V2DockerDaemonStatus
    $daemonWasUnavailable = -not [bool](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "available" -DefaultValue $false)
    $daemonReason = [string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "reason" -DefaultValue "docker-daemon-unreachable")
    $daemonDetail = [string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "detail" -DefaultValue "")
    if ($composeEngine -eq "unavailable") {
        $errors.Add("docker-compose-unavailable")
    }
    elseif ($daemonWasUnavailable) {
        # Non-blocking diagnostic by default; real block only if docker up fails.
        if (-not [string]::IsNullOrWhiteSpace($daemonDetail)) {
            $notes.Add("docker-daemon-detail: $daemonDetail")
        }
        $notes.Add("docker-daemon-action: Start Docker Desktop and ensure current user can access Docker daemon (docker-users group or elevated shell).")
    }

    $startedServices = New-Object System.Collections.Generic.List[string]
    if ($errors.Count -eq 0 -and $requiredServices.Count -gt 0) {
        try {
            $upResult = Invoke-V2ComposeUp -ComposeEngine $composeEngine -ComposePath $composePath -Services @($requiredServices.ToArray())
            $upExitCode = [int](Get-V2OptionalProperty -InputObject $upResult -Name "exit_code" -DefaultValue 1)
            $upText = [string](Get-V2OptionalProperty -InputObject $upResult -Name "text" -DefaultValue "")
            $upSucceeded = ($upExitCode -eq 0)

            if (-not $upSucceeded) {
                # Self-heal 1/2: auto-remap conflicting host ports reported by docker compose.
                for ($remapAttempt = 1; $remapAttempt -le 25 -and -not $upSucceeded; $remapAttempt++) {
                    $conflictPorts = @(Get-V2ComposeConflictHostPorts -ComposeOutputText $upText)
                    if ($conflictPorts.Count -eq 0) {
                        break
                    }

                    $autoRemaps = @(New-V2PortRemapsForConflicts -Topology $topology -ConflictPorts $conflictPorts -UsedPorts $usedPorts)
                    if ($autoRemaps.Count -eq 0) {
                        break
                    }

                    foreach ($autoRemap in $autoRemaps) {
                        $notes.Add("port-remap-auto: service=$($autoRemap.service) $($autoRemap.host_port) -> $($autoRemap.new_host_port)")
                        $remaps.Add($autoRemap)
                    }

                    Set-V2ComposePortRemaps -ComposePath $composePath -Remaps $autoRemaps
                    $topology = Get-V2ComposeTopology -ComposePath $composePath

                    $retryAfterRemap = Invoke-V2ComposeUp -ComposeEngine $composeEngine -ComposePath $composePath -Services @($requiredServices.ToArray())
                    $upExitCode = [int](Get-V2OptionalProperty -InputObject $retryAfterRemap -Name "exit_code" -DefaultValue 1)
                    $upText = [string](Get-V2OptionalProperty -InputObject $retryAfterRemap -Name "text" -DefaultValue "")
                    $upSucceeded = ($upExitCode -eq 0)
                }
            }

            if (-not $upSucceeded) {
                # Self-heal 2/2: permission fallback through Docker TCP socket if available.
                $fallbackSucceeded = $false
                if ($upText -match "(?i)access is denied|permission denied") {
                    if (Test-V2PortOpen -Port 2375 -TimeoutMs 700) {
                        $notes.Add("docker-host-fallback: retrying with tcp://localhost:2375")
                        $previousDockerHost = [string]$env:DOCKER_HOST
                        try {
                            $env:DOCKER_HOST = "tcp://localhost:2375"
                            $retryResult = Invoke-V2ComposeUp -ComposeEngine $composeEngine -ComposePath $composePath -Services @($requiredServices.ToArray())
                            $retryExitCode = [int](Get-V2OptionalProperty -InputObject $retryResult -Name "exit_code" -DefaultValue 1)
                            if ($retryExitCode -eq 0) {
                                $fallbackSucceeded = $true
                                $upSucceeded = $true
                                $notes.Add("docker-host-fallback: success")
                            }
                            else {
                                $retryText = [string](Get-V2OptionalProperty -InputObject $retryResult -Name "text" -DefaultValue "")
                                $retryExitCodeCurrent = $retryExitCode
                                $retrySucceeded = $false
                                $retryUpTextCurrent = $retryText

                                for ($fallbackRemapAttempt = 1; $fallbackRemapAttempt -le 25 -and -not $retrySucceeded; $fallbackRemapAttempt++) {
                                    $retryConflictPorts = @(Get-V2ComposeConflictHostPorts -ComposeOutputText $retryUpTextCurrent)
                                    if ($retryConflictPorts.Count -eq 0) {
                                        break
                                    }

                                    $retryConflictRemaps = @(New-V2PortRemapsForConflicts -Topology $topology -ConflictPorts $retryConflictPorts -UsedPorts $usedPorts)
                                    if ($retryConflictRemaps.Count -eq 0) {
                                        break
                                    }

                                    foreach ($retryRemap in $retryConflictRemaps) {
                                        $notes.Add("port-remap-auto: service=$($retryRemap.service) $($retryRemap.host_port) -> $($retryRemap.new_host_port)")
                                        $remaps.Add($retryRemap)
                                    }

                                    Set-V2ComposePortRemaps -ComposePath $composePath -Remaps $retryConflictRemaps
                                    $topology = Get-V2ComposeTopology -ComposePath $composePath

                                    $retryAfterFallbackRemap = Invoke-V2ComposeUp -ComposeEngine $composeEngine -ComposePath $composePath -Services @($requiredServices.ToArray())
                                    $retryAfterFallbackRemapCode = [int](Get-V2OptionalProperty -InputObject $retryAfterFallbackRemap -Name "exit_code" -DefaultValue 1)
                                    $retryAfterFallbackRemapText = [string](Get-V2OptionalProperty -InputObject $retryAfterFallbackRemap -Name "text" -DefaultValue "")
                                    if ($retryAfterFallbackRemapCode -eq 0) {
                                        $retrySucceeded = $true
                                        $fallbackSucceeded = $true
                                        $upSucceeded = $true
                                        $upExitCode = 0
                                        $upText = ""
                                        $notes.Add("docker-host-fallback: success-after-remap")
                                    }
                                    else {
                                        $retryExitCodeCurrent = $retryAfterFallbackRemapCode
                                        $retryUpTextCurrent = $retryAfterFallbackRemapText
                                    }
                                }

                                if (-not $retrySucceeded) {
                                    $upExitCode = $retryExitCodeCurrent
                                    $upText = $retryUpTextCurrent
                                    $errors.Add("docker-up-failed: exit-code=$retryExitCodeCurrent detail=$retryUpTextCurrent")
                                }
                            }
                        }
                        finally {
                            if ([string]::IsNullOrWhiteSpace($previousDockerHost)) {
                                Remove-Item -Path Env:DOCKER_HOST -ErrorAction SilentlyContinue
                            }
                            else {
                                $env:DOCKER_HOST = $previousDockerHost
                            }
                        }
                    }
                    else {
                        $notes.Add("docker-host-fallback: tcp://localhost:2375 unavailable")
                    }
                }

                if (-not $fallbackSucceeded -and -not ($errors | Where-Object { $_ -like "docker-up-failed:*" })) {
                    $errors.Add("docker-up-failed: exit-code=$upExitCode detail=$upText")
                }
            }

            if ($upSucceeded) {
                foreach ($service in @($requiredServices.ToArray())) {
                    $startedServices.Add([string]$service)
                }
            }
        }
        catch {
            $errors.Add("docker-up-failed: $($_.Exception.Message)")
        }
        if ($startedServices.Count -eq 0 -and $daemonWasUnavailable -and -not ($errors -contains $daemonReason)) {
            $errors.Add($daemonReason)
        }
    }

    $portsValidated = $false
    if ($errors.Count -eq 0) {
        $requiredPortMappings = @(
            @($topology.ports | Where-Object {
                    $serviceName = [string](Get-V2OptionalProperty -InputObject $_ -Name "service" -DefaultValue "")
                    @($requiredServices.ToArray()) -contains $serviceName
                } | ForEach-Object { [int](Get-V2OptionalProperty -InputObject $_ -Name "host_port" -DefaultValue 0) }) |
            Where-Object { $_ -gt 0 } |
            Select-Object -Unique
        )

        if ($requiredPortMappings.Count -eq 0) {
            $errors.Add("required-ports-missing")
        }
        else {
            $portsValidated = $true
            foreach ($hostPort in $requiredPortMappings) {
                if (-not (Test-V2PortOpen -Port $hostPort -TimeoutMs 2000)) {
                    $portsValidated = $false
                    $errors.Add("port-not-reachable: $hostPort")
                }
            }
        }
    }

    $envMap = Get-V2EnvMap -EnvPath $envPath
    $dbPorts = @($topology.ports | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "service" -DefaultValue "") -eq "db" })
    $neo4jPorts = @($topology.ports | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "service" -DefaultValue "") -eq "neo4j" })
    $qdrantPorts = @($topology.ports | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "service" -DefaultValue "") -eq "qdrant" })

    $dbEngineValue = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $Intake -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})) -Name "database" -DefaultValue ([PSCustomObject]@{ engine = "unknown" })).engine
    if ([string]::IsNullOrWhiteSpace($dbEngineValue) -or $dbEngineValue -eq "unknown") {
        $dbEngineValue = $bootstrapDatabase
    }
    $dbEngineValue = Get-V2CanonicalRelationalEngine -Engine $dbEngineValue
    $dbConnection = [PSCustomObject]@{
        enabled  = ($dbPorts.Count -gt 0)
        engine   = $dbEngineValue
        host     = "localhost"
        port     = if ($dbPorts.Count -gt 0) { [int]$dbPorts[0].host_port } else { 0 }
        database = if ($envMap.ContainsKey("DB_NAME")) { $envMap["DB_NAME"] } else { "" }
        user     = if ($envMap.ContainsKey("DB_USER")) { $envMap["DB_USER"] } else { "" }
        password = if ($envMap.ContainsKey("DB_PASSWORD")) { $envMap["DB_PASSWORD"] } else { "" }
        schema   = $projectSchema
        infra_mode = $InfraMode
    }

    $neo4jBoltPort = 0
    $neo4jHttpPort = 0
    foreach ($entry in $neo4jPorts) {
        if ([int]$entry.container_port -eq 7687) { $neo4jBoltPort = [int]$entry.host_port }
        if ([int]$entry.container_port -eq 7474) { $neo4jHttpPort = [int]$entry.host_port }
    }
    $neo4jConnection = [PSCustomObject]@{
        enabled   = ($neo4jPorts.Count -gt 0)
        host      = "localhost"
        bolt_port = $neo4jBoltPort
        http_port = $neo4jHttpPort
        uri       = if ($neo4jBoltPort -gt 0) { "bolt://localhost:$neo4jBoltPort" } else { "" }
        user      = if ($envMap.ContainsKey("NEO4J_USERNAME")) { $envMap["NEO4J_USERNAME"] } else { "neo4j" }
        password  = if ($envMap.ContainsKey("NEO4J_PASSWORD")) { $envMap["NEO4J_PASSWORD"] } else { "" }
        infra_mode = $InfraMode
    }
    if ($neo4jConnection.enabled -and [string]::IsNullOrWhiteSpace([string]$neo4jConnection.password)) {
        $notes.Add("neo4j-password-missing-from-env")
    }

    $qdrantPort = 0
    foreach ($entry in $qdrantPorts) {
        if ([int]$entry.container_port -eq 6333) { $qdrantPort = [int]$entry.host_port }
    }
    $qdrantConnection = [PSCustomObject]@{
        enabled           = ($qdrantPorts.Count -gt 0)
        host              = "localhost"
        port              = $qdrantPort
        url               = if ($qdrantPort -gt 0) { "http://localhost:$qdrantPort" } else { "" }
        collection_prefix = if ($envMap.ContainsKey("QDRANT_COLLECTION_PREFIX")) { $envMap["QDRANT_COLLECTION_PREFIX"] } else { $projectSlug }
        vector_size       = if ($envMap.ContainsKey("QDRANT_VECTOR_SIZE")) { [int]$envMap["QDRANT_VECTOR_SIZE"] } else { 768 }
        infra_mode        = $InfraMode
    }

    $neo4jProvision = [PSCustomObject]@{
        status            = "skipped"
        database          = "neo4j"
        project_namespace = $projectSlug
        details           = "neo4j-disabled"
    }
    $qdrantProvision = [PSCustomObject]@{
        status     = "skipped"
        collection = ""
        details    = "qdrant-disabled"
    }
    if ($errors.Count -eq 0) {
        $neo4jProvision = Ensure-V2Neo4jProjectNamespace -Connection $neo4jConnection -ProjectSlug $projectSlug
        $qdrantProvision = Ensure-V2QdrantProjectCollection -Connection $qdrantConnection -ProjectSlug $projectSlug

        if ([string]$neo4jProvision.status -like "error*") {
            $errors.Add("neo4j-provision-failed: $([string]$neo4jProvision.details)")
        }
        elseif (-not [string]::IsNullOrWhiteSpace([string]$neo4jProvision.details)) {
            $notes.Add("neo4j-provision: $([string]$neo4jProvision.details)")
        }

        if ([string]$qdrantProvision.status -like "error*") {
            $errors.Add("qdrant-provision-failed: $([string]$qdrantProvision.details)")
        }
        elseif (-not [string]::IsNullOrWhiteSpace([string]$qdrantProvision.details)) {
            $notes.Add("qdrant-provision: $([string]$qdrantProvision.details)")
        }
    }

    Set-V2DynamicProperty -InputObject $neo4jConnection -Name "database" -Value ([string](Get-V2OptionalProperty -InputObject $neo4jProvision -Name "database" -DefaultValue "neo4j"))
    Set-V2DynamicProperty -InputObject $neo4jConnection -Name "project_namespace" -Value $projectSlug
    Set-V2DynamicProperty -InputObject $qdrantConnection -Name "collection" -Value ([string](Get-V2OptionalProperty -InputObject $qdrantProvision -Name "collection" -DefaultValue ""))
    Set-V2DynamicProperty -InputObject $qdrantConnection -Name "collection_ready" -Value ([string](Get-V2OptionalProperty -InputObject $qdrantProvision -Name "status" -DefaultValue "unknown"))

    $bootstrapVerification = [PSCustomObject]@{
        status       = "skipped"
        verification = [PSCustomObject]@{
            relational = [PSCustomObject]@{
                status         = "skipped"
                backend        = "relational"
                records_seeded = 0
                details        = "bootstrap-preconditions-not-met"
            }
            relational_domain = [PSCustomObject]@{
                status         = "skipped"
                backend        = "relational-domain"
                records_seeded = 0
                details        = "bootstrap-preconditions-not-met"
            }
            neo4j      = [PSCustomObject]@{
                status         = "skipped"
                backend        = "neo4j"
                records_seeded = 0
                details        = "bootstrap-preconditions-not-met"
            }
            qdrant     = [PSCustomObject]@{
                status         = "skipped"
                backend        = "qdrant"
                records_seeded = 0
                details        = "bootstrap-preconditions-not-met"
            }
        }
        errors       = @()
        notes        = @()
    }
    if ($errors.Count -eq 0) {
        $bootstrapVerification = Invoke-V2BootstrapVerification `
            -ComposeEngine $composeEngine `
            -ComposePath $composePath `
            -DbConnection $dbConnection `
            -Neo4jConnection $neo4jConnection `
            -QdrantConnection $qdrantConnection `
            -ProjectPath $ProjectPath `
            -ProjectSlug $projectSlug `
            -ProjectName $ProjectName `
            -ProjectSchema $projectSchema `
            -InfraMode $InfraMode

        foreach ($verifyNote in @($bootstrapVerification.notes)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$verifyNote)) {
                $notes.Add([string]$verifyNote)
            }
        }
        foreach ($verifyError in @($bootstrapVerification.errors)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$verifyError)) {
                $errors.Add([string]$verifyError)
            }
        }
    }

    $secretsVault = Write-V2SecretsVault `
        -OrchestratorRoot $OrchestratorRoot `
        -ProjectSlug $projectSlug `
        -DbConnection $dbConnection `
        -Neo4jConnection $neo4jConnection

    $dbConnectionPublic = ConvertTo-V2PublicConnection -Connection $dbConnection -PasswordRef "vault://relational/password"
    $neo4jConnectionPublic = ConvertTo-V2PublicConnection -Connection $neo4jConnection -PasswordRef "vault://neo4j/password"
    $qdrantConnectionPublic = if ($null -eq $qdrantConnection) { [PSCustomObject]@{} } else { ($qdrantConnection | ConvertTo-Json -Depth 20 | ConvertFrom-Json) }

    $connectionPack = [PSCustomObject]@{
        generated_at   = Get-V2Timestamp
        project_name   = $ProjectName
        project_slug   = $projectSlug
        infra_mode     = $InfraMode
        infra_scope    = $infraScope
        secrets_vault  = [PSCustomObject]@{
            path = [string]$secretsVault.relative_path
        }
        docker_compose = [PSCustomObject]@{
            engine           = $composeEngine
            daemon_available = [bool](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "available" -DefaultValue $false)
            daemon_reason    = [string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "reason" -DefaultValue "unknown")
            daemon_detail    = [string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name "detail" -DefaultValue "")
            file             = "ai-orchestrator/docker/docker-compose.generated.yml"
            services_started = @($startedServices.ToArray())
            port_remaps      = @($remaps.ToArray())
            dedicated_container_isolation = [PSCustomObject]@{
                status     = [string](Get-V2OptionalProperty -InputObject $containerIsolation -Name "status" -DefaultValue "unknown")
                containers = @((Get-V2OptionalProperty -InputObject $containerIsolation -Name "containers" -DefaultValue @()))
            }
        }
        orchestrator_core = [PSCustomObject]@{
            status       = [string](Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "status" -DefaultValue "unknown")
            container    = [string](Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "container" -DefaultValue "orchestrator-core")
            network      = [string](Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "network" -DefaultValue "orchestrator-core-net")
            compose_path = [string](Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "compose_path" -DefaultValue "")
            root_path    = [string](Get-V2OptionalProperty -InputObject $orchestratorCoreBootstrap -Name "root_path" -DefaultValue "")
        }
        connections    = [PSCustomObject]@{
            transactional_db = $dbConnectionPublic
            neo4j            = $neo4jConnectionPublic
            qdrant           = $qdrantConnectionPublic
        }
        memory_resources = [PSCustomObject]@{
            neo4j  = $neo4jProvision
            qdrant = $qdrantProvision
        }
        bootstrap_verification = Get-V2OptionalProperty -InputObject $bootstrapVerification -Name "verification" -DefaultValue ([PSCustomObject]@{})
    }

    $packDirectory = Join-Path $OrchestratorRoot "project-pack"
    Initialize-V2Directory -Path $packDirectory
    $manifestPath = Join-Path $packDirectory "PACK_MANIFEST.json"
    $portsPath = Join-Path $packDirectory "DOCKER_PORTS.json"
    $connectionsPath = Join-Path $packDirectory "CONNECTIONS.json"
    $dbConnectionPath = Join-Path $OrchestratorRoot "database/connection-pack.json"
    Save-V2JsonContent -Path $manifestPath -Value ([PSCustomObject]@{
            pack_version = 1
            project_name = $ProjectName
            project_slug = $projectSlug
            infra_mode   = $InfraMode
            status       = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
            generated_at = Get-V2Timestamp
        })
    Save-V2JsonContent -Path $portsPath -Value ([PSCustomObject]@{
            generated_at = Get-V2Timestamp
            services     = @($topology.services)
            ports        = @($topology.ports)
            remaps       = @($remaps.ToArray())
        })
    Save-V2JsonContent -Path $connectionsPath -Value $connectionPack
    Save-V2JsonContent -Path $dbConnectionPath -Value $connectionPack

    if (-not [string]::IsNullOrWhiteSpace($ScopedProjectRoot)) {
        Initialize-V2Directory -Path $ScopedProjectRoot
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "pack")
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "docker")
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "graph")
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "vector")
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "state")
        Initialize-V2Directory -Path (Join-Path $ScopedProjectRoot "logs")

        $scopedManifestPath = Join-Path $ScopedProjectRoot "pack/PACK_MANIFEST.json"
        $scopedConnectionsPath = Join-Path $ScopedProjectRoot "pack/CONNECTIONS.json"
        Save-V2JsonContent -Path $scopedManifestPath -Value ([PSCustomObject]@{
                pack_version = 1
                project_name = $ProjectName
                project_slug = $projectSlug
                infra_mode   = $InfraMode
                status       = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                generated_at = Get-V2Timestamp
            })
        Save-V2JsonContent -Path $scopedConnectionsPath -Value $connectionPack

        $graphBootstrapPath = Join-Path $ScopedProjectRoot "graph/bootstrap-nodes.md"
        $graphBootstrap = @"
# Graph Bootstrap

## Node Types
- Project
- Service
- File
- Module
- Function
- Class
- Dependency
- Database
- Endpoint
"@
        Write-V2File -Path $graphBootstrapPath -Content $graphBootstrap -Force

        $vectorBootstrapPath = Join-Path $ScopedProjectRoot "vector/bootstrap-collections.md"
        $vectorBootstrap = @"
# Vector Bootstrap

## Embedding Collections
- source_code
- architecture
- documentation
- agent_outputs
- analysis_reports
"@
        Write-V2File -Path $vectorBootstrapPath -Content $vectorBootstrap -Force

        $scopedConfigYamlPath = Join-Path $ScopedProjectRoot "pack/config.yaml"
        $yamlLines = New-Object System.Collections.Generic.List[string]
        $yamlLines.Add("project_name: $ProjectName")
        $yamlLines.Add("project_slug: $projectSlug")
        $yamlLines.Add("infra_mode: $InfraMode")
        $yamlLines.Add("generated_at: $(Get-V2Timestamp)")
        $yamlLines.Add("status: $(if ($errors.Count -eq 0) { "ready" } else { "blocked" })")
        $yamlLines.Add("docker:")
        $yamlLines.Add("  engine: $composeEngine")
        $yamlLines.Add("  daemon_available: $(if ([bool](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name 'available' -DefaultValue $false)) { 'true' } else { 'false' })")
        $yamlLines.Add("  daemon_reason: $([string](Get-V2OptionalProperty -InputObject $dockerDaemonStatus -Name 'reason' -DefaultValue 'unknown'))")
        $yamlLines.Add("connections:")
        $yamlLines.Add("  relational:")
        $yamlLines.Add("    enabled: $(if ([bool]$dbConnection.enabled) { 'true' } else { 'false' })")
        $yamlLines.Add("    host: $($dbConnection.host)")
        $yamlLines.Add("    port: $($dbConnection.port)")
        $yamlLines.Add("    engine: $($dbConnection.engine)")
        $yamlLines.Add("  neo4j:")
        $yamlLines.Add("    enabled: $(if ([bool]$neo4jConnection.enabled) { 'true' } else { 'false' })")
        $yamlLines.Add("    uri: $($neo4jConnection.uri)")
        $yamlLines.Add("    database: $([string](Get-V2OptionalProperty -InputObject $neo4jConnection -Name 'database' -DefaultValue 'neo4j'))")
        $yamlLines.Add("  qdrant:")
        $yamlLines.Add("    enabled: $(if ([bool]$qdrantConnection.enabled) { 'true' } else { 'false' })")
        $yamlLines.Add("    url: $($qdrantConnection.url)")
        $yamlLines.Add("    collection: $([string](Get-V2OptionalProperty -InputObject $qdrantConnection -Name 'collection' -DefaultValue ''))")
        $yamlLines.Add("memory:")
        $yamlLines.Add("  graph: neo4j")
        $yamlLines.Add("  vector: qdrant")
        [System.IO.File]::WriteAllText($scopedConfigYamlPath, ($yamlLines -join [Environment]::NewLine))

        if (Test-Path -LiteralPath $composePath -PathType Leaf) {
            Copy-Item -LiteralPath $composePath -Destination (Join-Path $ScopedProjectRoot "docker/docker-compose.generated.yml") -Force
        }
        if (Test-Path -LiteralPath $envPath -PathType Leaf) {
            Copy-Item -LiteralPath $envPath -Destination (Join-Path $ScopedProjectRoot "docker/.env.docker.generated") -Force
        }

        $runtimeStatusPath = Join-Path $ScopedProjectRoot "state/runtime-services.json"
        Save-V2JsonContent -Path $runtimeStatusPath -Value ([PSCustomObject]@{
                generated_at = Get-V2Timestamp
                services     = [PSCustomObject]@{
                    agent_scheduler = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                    task_dag_engine = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                    observer        = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                    memory_sync     = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                    graph_sync      = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                    agent_runtime   = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
                }
            })
    }

    $dbConfigPath = Join-Path $OrchestratorRoot "database/config.md"
    $dbConfigLines = New-Object System.Collections.Generic.List[string]
    $dbConfigLines.Add("# Database Config")
    $dbConfigLines.Add("")
    $dbConfigLines.Add("- Generated At: $(Get-V2Timestamp)")
    $dbConfigLines.Add("- Project: $ProjectName ($projectSlug)")
    $dbConfigLines.Add("")
    $dbConfigLines.Add("## Connections")
    $dbConfigLines.Add("- Infra Mode: $InfraMode")
    $dbConfigLines.Add("- Transactional DB: enabled=$($dbConnection.enabled) host=$($dbConnection.host) port=$($dbConnection.port) engine=$($dbConnection.engine) schema=$([string](Get-V2OptionalProperty -InputObject $dbConnection -Name 'schema' -DefaultValue ''))")
    $dbConfigLines.Add("- Neo4j: enabled=$($neo4jConnection.enabled) uri=$($neo4jConnection.uri) database=$([string](Get-V2OptionalProperty -InputObject $neo4jConnection -Name 'database' -DefaultValue 'neo4j'))")
    $dbConfigLines.Add("- Qdrant: enabled=$($qdrantConnection.enabled) url=$($qdrantConnection.url) collection=$([string](Get-V2OptionalProperty -InputObject $qdrantConnection -Name 'collection' -DefaultValue ''))")
    if ($notes.Count -gt 0) {
        $dbConfigLines.Add("")
        $dbConfigLines.Add("## Bootstrap Notes")
        foreach ($note in @($notes.ToArray())) {
            $dbConfigLines.Add("- $note")
        }
    }
    if ($errors.Count -gt 0) {
        $dbConfigLines.Add("")
        $dbConfigLines.Add("## Bootstrap Errors")
        foreach ($err in @($errors.ToArray())) {
            $dbConfigLines.Add("- $err")
        }
    }
    Write-V2File -Path $dbConfigPath -Content ($dbConfigLines -join [Environment]::NewLine) -Force
    $accessGuidePath = Write-V2AccessGuide `
        -OrchestratorRoot $OrchestratorRoot `
        -ScopedProjectRoot $ScopedProjectRoot `
        -ProjectName $ProjectName `
        -ProjectSlug $projectSlug `
        -DbConnection $dbConnectionPublic `
        -Neo4jConnection $neo4jConnectionPublic `
        -QdrantConnection $qdrantConnectionPublic `
        -SecretsVaultRelativePath ([string]$secretsVault.relative_path) `
        -BootstrapVerification (Get-V2OptionalProperty -InputObject $connectionPack -Name "bootstrap_verification" -DefaultValue $null) `
        -BootstrapNotes @($notes.ToArray()) `
        -BootstrapErrors @($errors.ToArray())

    return [PSCustomObject]@{
        success             = ($errors.Count -eq 0)
        status              = if ($errors.Count -eq 0) { "ready" } else { "blocked" }
        infra_mode          = $InfraMode
        infra_scope         = $infraScope
        compose_path        = $composePath
        ports_validated     = [bool]$portsValidated
        project_name        = $ProjectName
        project_slug        = $projectSlug
        compose_engine      = $composeEngine
        services            = @($topology.services)
        port_mappings       = @($topology.ports)
        services_started    = @($startedServices.ToArray())
        port_remaps         = @($remaps.ToArray())
        orchestrator_core   = $orchestratorCoreBootstrap
        container_isolation = $containerIsolation
        errors              = @($errors.ToArray())
        notes               = @($notes.ToArray())
        access_guide_path   = $accessGuidePath
        secrets_vault_path  = [string]$secretsVault.relative_path
        connection_pack     = $connectionPack
        bootstrap_verification = Get-V2OptionalProperty -InputObject $connectionPack -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})
        scoped_project_root = $ScopedProjectRoot
    }
}

function Test-V2EnvironmentPrerequisites {
    param([string]$ProjectPath)

    $envCheckScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Check-Environment.ps1"
    if (-not (Test-Path -LiteralPath $envCheckScript -PathType Leaf)) {
        return [PSCustomObject]@{
            success = $true
            output  = @("env-check-script-missing: skipped")
            exit_code = 0
        }
    }

    $powershellCommand = Get-Command powershell -ErrorAction SilentlyContinue
    $powershellExe = if ($powershellCommand) { $powershellCommand.Source } else { "powershell" }
    $output = @(& $powershellExe -NoProfile -ExecutionPolicy Bypass -File $envCheckScript -ProjectPath $ProjectPath 2>&1)
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }

    return [PSCustomObject]@{
        success   = ($exitCode -eq 0)
        output    = @($output)
        exit_code = $exitCode
    }
}

function Invoke-V2DefaultScaffoldExecution {
    param(
        [string]$ProjectPath,
        [string]$OrchestratorRoot,
        [int]$MaxPasses = 4
    )

    $schedulerScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
    $agentLoopScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Run-AgentLoop.ps1"
    $taskDagPath = Join-Path $OrchestratorRoot "tasks/task-dag.json"

    if (-not (Test-Path -LiteralPath $schedulerScript -PathType Leaf) -or -not (Test-Path -LiteralPath $agentLoopScript -PathType Leaf)) {
        return [PSCustomObject]@{
            status         = "skipped"
            passes         = 0
            executed_tasks = 0
            details        = "missing-scheduler-or-agent-loop-script"
        }
    }
    if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
        return [PSCustomObject]@{
            status         = "skipped"
            passes         = 0
            executed_tasks = 0
            details        = "missing-task-dag"
        }
    }

    $executedTotal = 0
    $passes = 0

    for ($pass = 1; $pass -le $MaxPasses; $pass++) {
        $passes = $pass
        & $schedulerScript -ProjectPath $ProjectPath -MaxAssignmentsPerRun 8 | Out-Null

        $dagBefore = Get-V2JsonContent -Path $taskDagPath
        $tasksBefore = @(Get-V2OptionalProperty -InputObject $dagBefore -Name "tasks" -DefaultValue @())
        $doneBefore = @($tasksBefore | Where-Object {
            $s = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            $s -in @("done", "completed", "skipped")
        }).Count

        $inProgress = @($tasksBefore | Where-Object {
            [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "in-progress"
        })
        if ($inProgress.Count -eq 0) {
            break
        }

        $agents = @(
            $inProgress |
            ForEach-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "assigned_agent" -DefaultValue "") } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Sort-Object -Unique
        )
        if ($agents.Count -eq 0) {
            break
        }

        foreach ($agent in $agents) {
            & $agentLoopScript -ProjectPath $ProjectPath -AgentName $agent -RunOnce -MaxTasksPerCycle 8 | Out-Null
        }

        $dagAfter = Get-V2JsonContent -Path $taskDagPath
        $tasksAfter = @(Get-V2OptionalProperty -InputObject $dagAfter -Name "tasks" -DefaultValue @())
        $doneAfter = @($tasksAfter | Where-Object {
            $s = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            $s -in @("done", "completed", "skipped")
        }).Count

        $executedDelta = $doneAfter - $doneBefore
        if ($executedDelta -gt 0) {
            $executedTotal += $executedDelta
        }
        else {
            $stillInProgress = @($tasksAfter | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "") -eq "in-progress"
            }).Count
            if ($stillInProgress -eq 0) {
                break
            }
        }
    }

    return [PSCustomObject]@{
        status         = "completed"
        passes         = $passes
        executed_tasks = $executedTotal
        details        = "default-scaffold-pass-finished"
    }
}

function Invoke-V2Submission {
    param(
        [string]$TargetProjectPath,
        [string]$RequestedProjectName = "",
        [ValidateSet("dedicated-infra", "shared-infra")]
        [string]$InfraMode = "dedicated-infra",
        [switch]$PromptForMissingProjectName,
        [switch]$EnableNeo4j,
        [switch]$EnableQdrant
    )

    $resolvedProjectPath = Resolve-V2AbsolutePath -Path $TargetProjectPath
    if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
        throw "Project path does not exist: $TargetProjectPath"
    }
    Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-submit"

    $envCheck = Test-V2EnvironmentPrerequisites -ProjectPath $resolvedProjectPath
    if (-not $envCheck.success) {
        $tail = ((@($envCheck.output) | Out-String).Trim())
        if ($tail.Length -gt 1200) {
            $tail = $tail.Substring($tail.Length - 1200)
        }
        throw "env-check-failed-before-submit: $tail"
    }

    $initScript = Join-Path $PSScriptRoot "Initialize-AIOrchestratorLayer.ps1"
    $intakeScript = Join-Path $PSScriptRoot "Invoke-UniversalIntakeV2.ps1"
    # Do not reinitialize orchestration state during submit.
    # Submit may run repeatedly and must preserve state, tasks, locks, reports, and memory continuity.
    & $initScript -ProjectPath $resolvedProjectPath | Out-Null

    $orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
    $stateDirectory = Join-Path $orchestratorRoot "state"
    $statePath = Join-Path $stateDirectory "project-state.json"
    $openQuestionsPath = Join-Path $stateDirectory "open-questions.md"
    $intakeReportPath = Join-Path $stateDirectory "intake-report.md"
    $packManifestPath = Join-Path $orchestratorRoot "project-pack/PACK_MANIFEST.json"
    $packConnectionsPath = Join-Path $orchestratorRoot "project-pack/CONNECTIONS.json"
    $packPortsPath = Join-Path $orchestratorRoot "project-pack/DOCKER_PORTS.json"

    $existingState = Get-V2JsonContent -Path $statePath
    $projectName = Resolve-V2ProjectName -ProjectRoot $resolvedProjectPath -StatePath $statePath -ExplicitName $RequestedProjectName -PromptIfMissing:$PromptForMissingProjectName
    $projectSlug = Get-V2ProjectSlug -Name $projectName
    $scopedProjectRoot = Ensure-V2ScopedProjectPack -OrchestratorRoot $orchestratorRoot -ProjectName $projectName -ProjectSlug $projectSlug

    $intakeJson = & $intakeScript -ProjectPath $resolvedProjectPath -OutputPath $stateDirectory -RefactorPolicy $RefactorPolicy -EmitJson
    $intake = ($intakeJson | Out-String) | ConvertFrom-Json

    $bootstrap = Invoke-V2ProjectPackBootstrap `
        -ProjectPath $resolvedProjectPath `
        -OrchestratorRoot $orchestratorRoot `
        -ProjectName $projectName `
        -ScopedProjectRoot $scopedProjectRoot `
        -Intake $intake `
        -InfraMode $InfraMode `
        -EnableNeo4j:$EnableNeo4j `
        -EnableQdrant:$EnableQdrant
    $dockerStatus = if ($bootstrap.success) { "ready" } else { "blocked-startup-pack" }
    $memoryMode = Get-V2MemoryMode -IncludeNeo4j:$EnableNeo4j -IncludeQdrant:$EnableQdrant

    $combinedUnknowns = New-Object System.Collections.Generic.List[string]
    foreach ($unknown in @($intake.unknowns)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$unknown)) {
            $combinedUnknowns.Add([string]$unknown)
        }
    }
    foreach ($err in @($bootstrap.errors)) {
        $combinedUnknowns.Add("bootstrap::$err")
    }

    $openQuestions = New-Object System.Collections.Generic.List[string]
    foreach ($question in @($intake.open_questions)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$question)) {
            $openQuestions.Add([string]$question)
        }
    }
    foreach ($err in @($bootstrap.errors)) {
        $openQuestions.Add("Resolve startup bootstrap error: $err")
    }

    $effectiveStatus = [string](Get-V2OptionalProperty -InputObject $intake -Name "status" -DefaultValue "unknown")
    if (-not $bootstrap.success) {
        $effectiveStatus = "blocked-startup"
    }
    else {
        $blockingQuestionCount = @(
            @($openQuestions.ToArray()) | Where-Object {
                $q = [string]$_
                $q -match "Resolve startup bootstrap error" -or
                $q -match "(?i)refactor policy" -or
                $q -match "(?i)legacy"
            }
        ).Count
        if ($blockingQuestionCount -gt 0 -and ($effectiveStatus -notlike "blocked*")) {
            $effectiveStatus = "blocked-waiting-answers"
        }
    }

    $projectDnaPath = Update-V2ProjectDna `
        -ScopedProjectRoot $scopedProjectRoot `
        -ProjectName $projectName `
        -ProjectSlug $projectSlug `
        -Intake $intake `
        -Bootstrap $bootstrap `
        -EffectiveStatus $effectiveStatus `
        -Unknowns @($combinedUnknowns.ToArray()) `
        -OpenQuestions @($openQuestions.ToArray())

    Write-V2AnalysisArtifacts -OrchestratorRoot $orchestratorRoot -Intake $intake

    # Run Code Reader Agent to auto-fill architecture.md from real source code (non-blocking)
    $codeReaderScript = Join-Path $PSScriptRoot "Invoke-CodeReaderAgent.ps1"
    if (Test-Path -LiteralPath $codeReaderScript -PathType Leaf) {
        try {
            & $codeReaderScript -ProjectPath $resolvedProjectPath | Out-Null
            Write-Host "[Intake] Code Reader Agent completed — architecture.md populated from source scan."
        }
        catch {
            Write-Warning "[Intake] Code Reader Agent failed (non-fatal): $($_.Exception.Message)"
        }
    }

    $orchestrator360 = Invoke-V2360ArtifactsGeneration -ProjectPath $resolvedProjectPath
    if (-not [bool](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "success" -DefaultValue $false)) {
        $openQuestions.Add("Generate orchestrator 360 artifacts failed: $([string](Get-V2OptionalProperty -InputObject $orchestrator360 -Name 'details' -DefaultValue 'unknown-error'))")
    }
    Set-V2TaskSeeds -OrchestratorRoot $orchestratorRoot -Intake $intake -DockerRequested $true -RequestedStack $Stack
    Append-V2SubmissionLogs -OrchestratorRoot $orchestratorRoot -Intake $intake -DockerStatus $dockerStatus

    if ($openQuestions.Count -gt 0) {
        [System.IO.File]::WriteAllText($openQuestionsPath, (New-OpenQuestionsMarkdown -Questions @($openQuestions.ToArray())))
    }
    elseif (Test-Path -LiteralPath $openQuestionsPath) {
        Remove-Item -LiteralPath $openQuestionsPath -Force -ErrorAction SilentlyContinue
    }

    $startupRequirements = [PSCustomObject]@{
        project_name_defined = (-not [string]::IsNullOrWhiteSpace($projectName))
        ports_validated      = [bool](Get-V2OptionalProperty -InputObject $bootstrap -Name "ports_validated" -DefaultValue $false)
        databases_started    = [bool]$bootstrap.success
        connections_ready    = [bool]$bootstrap.success
        memory_layers_active = [bool]$bootstrap.success
        pack_integrity       = [bool]$bootstrap.success
        orchestrator_core_running = [bool](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $bootstrap -Name "orchestrator_core" -DefaultValue ([PSCustomObject]@{})) -Name "success" -DefaultValue $false)
        project_container_isolation = [bool](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $bootstrap -Name "container_isolation" -DefaultValue ([PSCustomObject]@{})) -Name "success" -DefaultValue ($InfraMode -ne "dedicated-infra"))
    }

    $creationDate = [string](Get-V2OptionalProperty -InputObject $existingState -Name "creation_date" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($creationDate)) {
        $creationDate = Get-V2Timestamp
    }
    $preservedHealthStatus = [string](Get-V2OptionalProperty -InputObject $existingState -Name "health_status" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($preservedHealthStatus)) {
        $preservedHealthStatus = "unknown"
    }
    $preservedObserverRun = [string](Get-V2OptionalProperty -InputObject $existingState -Name "last_observer_run" -DefaultValue "")
    $existingPhaseApprovals = Get-V2OptionalProperty -InputObject $existingState -Name "phase_approvals" -DefaultValue ([PSCustomObject]@{})
    $phaseApprovals = [PSCustomObject]@{
        context      = Get-V2OptionalProperty -InputObject $existingPhaseApprovals -Name "context" -DefaultValue ([PSCustomObject]@{ status = "approved"; updated_at = Get-V2Timestamp; updated_by = "system-auto" })
        architecture = Get-V2OptionalProperty -InputObject $existingPhaseApprovals -Name "architecture" -DefaultValue ([PSCustomObject]@{ status = "pending"; updated_at = Get-V2Timestamp; updated_by = "system-default" })
        execution    = Get-V2OptionalProperty -InputObject $existingPhaseApprovals -Name "execution" -DefaultValue ([PSCustomObject]@{ status = "pending"; updated_at = Get-V2Timestamp; updated_by = "system-default" })
        release      = Get-V2OptionalProperty -InputObject $existingPhaseApprovals -Name "release" -DefaultValue ([PSCustomObject]@{ status = "pending"; updated_at = Get-V2Timestamp; updated_by = "system-default" })
    }

    $connectionPack = Get-V2OptionalProperty -InputObject $bootstrap -Name "connection_pack" -DefaultValue ([PSCustomObject]@{})
    $connectionsMeta = Get-V2OptionalProperty -InputObject $connectionPack -Name "connections" -DefaultValue ([PSCustomObject]@{})
    $portsDocument = Get-V2JsonContent -Path $packPortsPath
    $resolvedPorts = @(Get-V2OptionalProperty -InputObject $portsDocument -Name "ports" -DefaultValue @())
    $unknownValues = @($combinedUnknowns.ToArray())
    $openQuestionValues = @($openQuestions.ToArray())
    $qdrantFallbackMaxRatioPercent = [double](Get-V2OptionalProperty -InputObject $existingState -Name "qdrant_fallback_max_ratio_percent" -DefaultValue 20)
    if ($qdrantFallbackMaxRatioPercent -lt 0) {
        $qdrantFallbackMaxRatioPercent = 0
    }
    $qdrantFallbackAlertCooldownSeconds = [int](Get-V2OptionalProperty -InputObject $existingState -Name "qdrant_fallback_alert_cooldown_seconds" -DefaultValue 1800)
    if ($qdrantFallbackAlertCooldownSeconds -lt 60) {
        $qdrantFallbackAlertCooldownSeconds = 60
    }
    $qdrantFallbackEnforceAfterCoreComplete = [bool](Get-V2OptionalProperty -InputObject $existingState -Name "qdrant_fallback_enforce_after_core_complete" -DefaultValue $true)
    $qdrantFallbackDeferredMaxCycles = [int](Get-V2OptionalProperty -InputObject $existingState -Name "qdrant_fallback_deferred_max_cycles" -DefaultValue 6)
    if ($qdrantFallbackDeferredMaxCycles -lt 1) {
        $qdrantFallbackDeferredMaxCycles = 1
    }

    $projectState = [PSCustomObject]@{
        creation_date         = $creationDate
        project_name          = $projectName
        project_slug          = $projectSlug
        project_type          = $intake.project_type
        confidence            = $intake.confidence
        refactor_policy       = $intake.refactor_policy
        status                = $effectiveStatus
        technical_fingerprint = $intake.technical_fingerprint
        verified_commands     = $intake.verified_commands
        unknowns              = if ($unknownValues.Count -gt 0) { [string[]]$unknownValues } else { @() }
        open_questions        = if ($openQuestionValues.Count -gt 0) { [string[]]$openQuestionValues } else { @() }
        analysis              = Get-V2OptionalProperty -InputObject $intake -Name "analysis" -DefaultValue ([PSCustomObject]@{})
        memory_mode           = $memoryMode
        infra_mode            = $InfraMode
        last_observer_run     = $preservedObserverRun
        health_status         = $preservedHealthStatus
        docker_status         = $dockerStatus
        orchestrator_core     = Get-V2OptionalProperty -InputObject $bootstrap -Name "orchestrator_core" -DefaultValue ([PSCustomObject]@{})
        include_neo4j         = [bool]$EnableNeo4j
        include_qdrant        = [bool]$EnableQdrant
        qdrant_fallback_max_ratio_percent = $qdrantFallbackMaxRatioPercent
        qdrant_fallback_alert_cooldown_seconds = $qdrantFallbackAlertCooldownSeconds
        qdrant_fallback_enforce_after_core_complete = $qdrantFallbackEnforceAfterCoreComplete
        qdrant_fallback_deferred_max_cycles = $qdrantFallbackDeferredMaxCycles
        project_pack_root     = ("ai-orchestrator/projects/{0}" -f $projectSlug)
        project_dna_path      = ("ai-orchestrator/projects/{0}/project_dna.json" -f $projectSlug)
        startup_pack_status   = $bootstrap.status
        startup_requirements  = $startupRequirements
        startup_errors        = @($bootstrap.errors)
        startup_notes         = @($bootstrap.notes)
        startup_paths         = [PSCustomObject]@{
            manifest            = "ai-orchestrator/project-pack/PACK_MANIFEST.json"
            docker_ports        = "ai-orchestrator/project-pack/DOCKER_PORTS.json"
            docker_compose_file = [string](Get-V2OptionalProperty -InputObject $bootstrap -Name "compose_path" -DefaultValue "ai-orchestrator/docker/docker-compose.generated.yml")
            connections         = "ai-orchestrator/project-pack/CONNECTIONS.json"
            database_connection = "ai-orchestrator/database/connection-pack.json"
            secrets_vault       = [string](Get-V2OptionalProperty -InputObject $bootstrap -Name "secrets_vault_path" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
            access_guide        = "ai-orchestrator/database/access.md"
            orchestrator_core_compose = [string](Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $bootstrap -Name "orchestrator_core" -DefaultValue ([PSCustomObject]@{})) -Name "compose_path" -DefaultValue "")
            scoped_pack         = ("ai-orchestrator/projects/{0}/pack" -f $projectSlug)
            scoped_config       = ("ai-orchestrator/projects/{0}/pack/config.yaml" -f $projectSlug)
            scoped_project_dna  = ("ai-orchestrator/projects/{0}/project_dna.json" -f $projectSlug)
        }
        ports                 = @(
            $resolvedPorts |
            ForEach-Object {
                [PSCustomObject]@{
                    service        = [string](Get-V2OptionalProperty -InputObject $_ -Name "service" -DefaultValue "")
                    host_port      = [int](Get-V2OptionalProperty -InputObject $_ -Name "host_port" -DefaultValue 0)
                    container_port = [int](Get-V2OptionalProperty -InputObject $_ -Name "container_port" -DefaultValue 0)
                }
            }
        )
        services              = [PSCustomObject]@{
            available = @($bootstrap.services)
            started   = @($bootstrap.services_started)
        }
        databases             = [PSCustomObject]@{
            relational = Get-V2OptionalProperty -InputObject $connectionsMeta -Name "transactional_db" -DefaultValue ([PSCustomObject]@{})
            neo4j      = Get-V2OptionalProperty -InputObject $connectionsMeta -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
            qdrant     = Get-V2OptionalProperty -InputObject $connectionsMeta -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
        }
        bootstrap_verification = Get-V2OptionalProperty -InputObject $connectionPack -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})
        agent_status          = if ($effectiveStatus -like "blocked*") { "blocked" } else { "ready" }
        memory_status         = if ($startupRequirements.memory_layers_active) { "active" } else { "blocked" }
        analysis_paths        = [PSCustomObject]@{
            architecture_report = "ai-orchestrator/analysis/architecture-report.md"
            dependency_graph    = "ai-orchestrator/analysis/dependency-graph.md"
            code_quality        = "ai-orchestrator/analysis/code-quality.md"
        }
        phase_approvals      = $phaseApprovals
        orchestrator_360      = [PSCustomObject]@{
            status  = [string](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "status" -DefaultValue "unknown")
            success = [bool](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "success" -DefaultValue $false)
            details = [string](Get-V2OptionalProperty -InputObject $orchestrator360 -Name "details" -DefaultValue "")
            paths   = Get-V2OptionalProperty -InputObject $orchestrator360 -Name "paths" -DefaultValue ([PSCustomObject]@{})
        }
        updated_at            = Get-V2Timestamp
    }

    Save-V2JsonContent -Path $statePath -Value $projectState

    $agentDispatchResult = [PSCustomObject]@{
        success   = $false
        phase     = ""
        selected  = 0
        executed  = 0
        failed    = 0
        report_json = ""
        details   = "not-run"
    }
    $agentValidationResult = [PSCustomObject]@{
        success    = $false
        verdict    = "NOT READY"
        phase      = ""
        selected   = 0
        pass       = 0
        fail       = 0
        report_json = ""
        details    = "not-run"
    }

    if ($effectiveStatus -notlike "blocked*") {
        $agentDispatchScript = Join-Path $PSScriptRoot "Invoke-AgentDispatcherV2.ps1"
        if (Test-Path -LiteralPath $agentDispatchScript -PathType Leaf) {
            try {
                $dispatchRaw = & $agentDispatchScript -ProjectPath $resolvedProjectPath -Phase auto -AutoRepairTasks -EmitJson 2>&1 | Out-String
                $dispatchParsed = $null
                try { $dispatchParsed = $dispatchRaw | ConvertFrom-Json } catch { $dispatchParsed = $null }
                if ($dispatchParsed) {
                    $agentDispatchResult = [PSCustomObject]@{
                        success     = [bool](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "success" -DefaultValue $false)
                        phase       = [string](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "phase" -DefaultValue "")
                        selected    = [int](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "selected" -DefaultValue 0)
                        executed    = [int](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "executed" -DefaultValue 0)
                        failed      = [int](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "failed" -DefaultValue 0)
                        report_json = [string](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "report_json" -DefaultValue "")
                        details     = if ([bool](Get-V2OptionalProperty -InputObject $dispatchParsed -Name "success" -DefaultValue $false)) { "ok" } else { "dispatch-not-success" }
                    }
                }
                else {
                    $agentDispatchResult = [PSCustomObject]@{
                        success   = $false
                        phase     = ""
                        selected  = 0
                        executed  = 0
                        failed    = 0
                        report_json = ""
                        details   = "invalid-json-output"
                    }
                }
            }
            catch {
                $agentDispatchResult = [PSCustomObject]@{
                    success   = $false
                    phase     = ""
                    selected  = 0
                    executed  = 0
                    failed    = 0
                    report_json = ""
                    details   = $_.Exception.Message
                }
            }
        }

        $agentValidationScript = Join-Path $PSScriptRoot "Validate-AgentArtifactsV2.ps1"
        if (Test-Path -LiteralPath $agentValidationScript -PathType Leaf) {
            try {
                $validationRaw = & $agentValidationScript -ProjectPath $resolvedProjectPath -Phase auto -AutoRepairTasks -EmitJson 2>&1 | Out-String
                $validationParsed = $null
                try { $validationParsed = $validationRaw | ConvertFrom-Json } catch { $validationParsed = $null }
                if ($validationParsed) {
                    $agentValidationResult = [PSCustomObject]@{
                        success     = [bool](Get-V2OptionalProperty -InputObject $validationParsed -Name "success" -DefaultValue $false)
                        verdict     = [string](Get-V2OptionalProperty -InputObject $validationParsed -Name "verdict" -DefaultValue "NOT READY")
                        phase       = [string](Get-V2OptionalProperty -InputObject $validationParsed -Name "phase" -DefaultValue "")
                        selected    = [int](Get-V2OptionalProperty -InputObject $validationParsed -Name "selected" -DefaultValue 0)
                        pass        = [int](Get-V2OptionalProperty -InputObject $validationParsed -Name "pass" -DefaultValue 0)
                        fail        = [int](Get-V2OptionalProperty -InputObject $validationParsed -Name "fail" -DefaultValue 0)
                        report_json = [string](Get-V2OptionalProperty -InputObject $validationParsed -Name "report_json" -DefaultValue "")
                        details     = if ([bool](Get-V2OptionalProperty -InputObject $validationParsed -Name "success" -DefaultValue $false)) { "ok" } else { "not-ready" }
                    }
                }
                else {
                    $agentValidationResult = [PSCustomObject]@{
                        success    = $false
                        verdict    = "NOT READY"
                        phase      = ""
                        selected   = 0
                        pass       = 0
                        fail       = 0
                        report_json = ""
                        details    = "invalid-json-output"
                    }
                }
            }
            catch {
                $agentValidationResult = [PSCustomObject]@{
                    success    = $false
                    verdict    = "NOT READY"
                    phase      = ""
                    selected   = 0
                    pass       = 0
                    fail       = 0
                    report_json = ""
                    details    = $_.Exception.Message
                }
            }
        }

        if (-not [bool]$agentDispatchResult.success -or -not [bool]$agentValidationResult.success) {
            $warningPath = Join-Path $orchestratorRoot "communication/alerts.md"
            Add-V2MarkdownLog -Path $warningPath -Header "# Alerts" -Lines @(
                "## $(Get-V2Timestamp)",
                "- source: submit",
                "- issue: agent-dispatch-or-validation",
                "- dispatch: success=$([bool]$agentDispatchResult.success) details=$([string]$agentDispatchResult.details)",
                "- validation: success=$([bool]$agentValidationResult.success) verdict=$([string]$agentValidationResult.verdict) details=$([string]$agentValidationResult.details)"
            )
        }
    }

    if ($effectiveStatus -like "blocked*") {
        $warningPath = Join-Path $orchestratorRoot "communication/alerts.md"
        Add-V2MarkdownLog -Path $warningPath -Header "# Alerts" -Lines @(
            "## $(Get-V2Timestamp)",
            "- source: submit",
            "- issue: startup-blocked",
            "- details: runtime services not started due to bootstrap validation failure"
        )
    }
    else {
        $schedulerScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
        try {
            & $schedulerScript -ProjectPath $resolvedProjectPath | Out-Null
            $autoScaffold = Invoke-V2DefaultScaffoldExecution -ProjectPath $resolvedProjectPath -OrchestratorRoot $orchestratorRoot -MaxPasses 4
            if ([string](Get-V2OptionalProperty -InputObject $autoScaffold -Name "status" -DefaultValue "") -eq "completed") {
                $historyPath = Join-Path $orchestratorRoot "tasks/execution-history.md"
                Add-V2MarkdownLog -Path $historyPath -Header "# Execution History" -Lines @(
                    "## $(Get-V2Timestamp)",
                    "- event: default-scaffold-pass",
                    "- passes: $([int](Get-V2OptionalProperty -InputObject $autoScaffold -Name 'passes' -DefaultValue 0))",
                    "- executed_tasks: $([int](Get-V2OptionalProperty -InputObject $autoScaffold -Name 'executed_tasks' -DefaultValue 0))",
                    "- details: $([string](Get-V2OptionalProperty -InputObject $autoScaffold -Name 'details' -DefaultValue ''))"
                )
            }
        }
        catch {
            $warningPath = Join-Path $orchestratorRoot "communication/alerts.md"
            Add-V2MarkdownLog -Path $warningPath -Header "# Alerts" -Lines @(
                "## $(Get-V2Timestamp)",
                "- source: submit",
                "- issue: scheduler-failed",
                "- details: $($_.Exception.Message)"
            )
        }
    }

    return [PSCustomObject]@{
        project_name        = $projectName
        project_slug        = $projectSlug
        infra_mode          = $InfraMode
        project_dna_path    = $projectDnaPath
        project_path        = $resolvedProjectPath
        orchestrator_root   = $orchestratorRoot
        state_path          = $statePath
        project_type        = $intake.project_type
        status              = $effectiveStatus
        docker_status       = $dockerStatus
        orchestrator_core_status = [string](Get-V2OptionalProperty -InputObject $projectState.orchestrator_core -Name "status" -DefaultValue "unknown")
        orchestrator_core_compose = [string](Get-V2OptionalProperty -InputObject $projectState.orchestrator_core -Name "compose_path" -DefaultValue "")
        startup_pack_status = $bootstrap.status
        pack_manifest_path  = $packManifestPath
        connections_path    = $packConnectionsPath
        ports_path          = $packPortsPath
        access_guide_path   = [string](Get-V2OptionalProperty -InputObject $bootstrap -Name "access_guide_path" -DefaultValue "")
        secrets_vault_path  = [string](Get-V2OptionalProperty -InputObject $bootstrap -Name "secrets_vault_path" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
        neo4j_uri           = [string](Get-V2OptionalProperty -InputObject $projectState.databases.neo4j -Name "uri" -DefaultValue "")
        neo4j_browser_url   = if ([int](Get-V2OptionalProperty -InputObject $projectState.databases.neo4j -Name "http_port" -DefaultValue 0) -gt 0) { "http://localhost:$([int](Get-V2OptionalProperty -InputObject $projectState.databases.neo4j -Name "http_port" -DefaultValue 0))/browser/" } else { "" }
        neo4j_user          = [string](Get-V2OptionalProperty -InputObject $projectState.databases.neo4j -Name "user" -DefaultValue "")
        neo4j_password_masked = [string](Get-V2OptionalProperty -InputObject $projectState.databases.neo4j -Name "password" -DefaultValue "")
        qdrant_url          = [string](Get-V2OptionalProperty -InputObject $projectState.databases.qdrant -Name "url" -DefaultValue "")
        qdrant_collection   = [string](Get-V2OptionalProperty -InputObject $projectState.databases.qdrant -Name "collection" -DefaultValue "")
        orchestrator_360_status = [string](Get-V2OptionalProperty -InputObject $projectState.orchestrator_360 -Name "status" -DefaultValue "")
        business_context_path = [string](Get-V2OptionalProperty -InputObject $projectState.orchestrator_360.paths -Name "business_context_json" -DefaultValue "")
        agent_dispatch_success = [bool]$agentDispatchResult.success
        agent_dispatch_report = [string]$agentDispatchResult.report_json
        agent_validation_verdict = [string]$agentValidationResult.verdict
        agent_validation_report = [string]$agentValidationResult.report_json
        bootstrap_verification = Get-V2OptionalProperty -InputObject $projectState -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})
    }
}

function Invoke-V2ProjectCleanup {
    param(
        [string]$ProjectPath,
        [switch]$Force
    )

    $resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
    if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
        throw "Project path does not exist: $ProjectPath"
    }
    Assert-V2ExecutionEnabled -ProjectRoot $resolvedProjectPath -ActionName "v2-clean-project"

    $orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
    $statePath = Join-Path $orchestratorRoot "state/project-state.json"
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw "project-state.json not found. Run v2-submit first."
    }

    $state = Get-V2JsonContent -Path $statePath
    if ($null -eq $state) {
        throw "Unable to read project state: $statePath"
    }

    $projectName = [string](Get-V2OptionalProperty -InputObject $state -Name "project_name" -DefaultValue (Split-Path -Leaf $resolvedProjectPath))
    $projectSlug = [string](Get-V2OptionalProperty -InputObject $state -Name "project_slug" -DefaultValue (Get-V2ProjectSlug -Name $projectName))
    $infraMode = [string](Get-V2OptionalProperty -InputObject $state -Name "infra_mode" -DefaultValue "dedicated-infra")
    $databaseMap = Get-V2OptionalProperty -InputObject $state -Name "databases" -DefaultValue ([PSCustomObject]@{})
    $relational = Get-V2OptionalProperty -InputObject $databaseMap -Name "relational" -DefaultValue ([PSCustomObject]@{})
    $neo4j = Get-V2OptionalProperty -InputObject $databaseMap -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
    $qdrant = Get-V2OptionalProperty -InputObject $databaseMap -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
    $startupPaths = Get-V2OptionalProperty -InputObject $state -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})

    $secretsVaultRelative = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "secrets_vault" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
    $secretsVaultPath = Join-Path $resolvedProjectPath $secretsVaultRelative
    $vault = Get-V2JsonContent -Path $secretsVaultPath
    if ($vault) {
        $vaultSecrets    = Get-V2OptionalProperty -InputObject $vault -Name "secrets" -DefaultValue ([PSCustomObject]@{})
        $vaultRelational = Get-V2OptionalProperty -InputObject $vaultSecrets -Name "relational" -DefaultValue ([PSCustomObject]@{})
        $vaultNeo4j      = Get-V2OptionalProperty -InputObject $vaultSecrets -Name "neo4j"      -DefaultValue ([PSCustomObject]@{})
        $vaultEncrypted  = [bool](Get-V2OptionalProperty -InputObject $vault -Name "encrypted" -DefaultValue $false)
        $rawVaultRelPass = [string](Get-V2OptionalProperty -InputObject $vaultRelational -Name "password" -DefaultValue "")
        $rawVaultNeoPass = [string](Get-V2OptionalProperty -InputObject $vaultNeo4j      -Name "password" -DefaultValue "")
        $clearRelPass = if ($vaultEncrypted -and -not [string]::IsNullOrEmpty($rawVaultRelPass)) { Unprotect-V2Secret -CipherBase64 $rawVaultRelPass } else { $rawVaultRelPass }
        $clearNeoPass = if ($vaultEncrypted -and -not [string]::IsNullOrEmpty($rawVaultNeoPass)) { Unprotect-V2Secret -CipherBase64 $rawVaultNeoPass } else { $rawVaultNeoPass }
        $dockerEnvPath = Join-Path $resolvedProjectPath "ai-orchestrator/docker/.env.docker.generated"
        $dockerEnvMap = Get-V2EnvMap -EnvPath $dockerEnvPath
        if ([string]::IsNullOrWhiteSpace($clearRelPass) -and $dockerEnvMap.ContainsKey("DB_PASSWORD")) {
            $clearRelPass = [string]$dockerEnvMap["DB_PASSWORD"]
        }
        if ([string]::IsNullOrWhiteSpace($clearNeoPass) -and $dockerEnvMap.ContainsKey("NEO4J_PASSWORD")) {
            $clearNeoPass = [string]$dockerEnvMap["NEO4J_PASSWORD"]
        }
        $relPass = [string](Get-V2OptionalProperty -InputObject $relational -Name "password" -DefaultValue "")
        if (([string]::IsNullOrWhiteSpace($relPass) -or $relPass -eq "[stored in vault]" -or $relPass -match "\*") -and -not [string]::IsNullOrWhiteSpace($clearRelPass)) {
            Set-V2DynamicProperty -InputObject $relational -Name "password" -Value $clearRelPass
        }
        $neoPass = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "password" -DefaultValue "")
        if (([string]::IsNullOrWhiteSpace($neoPass) -or $neoPass -eq "[stored in vault]" -or $neoPass -match "\*") -and -not [string]::IsNullOrWhiteSpace($clearNeoPass)) {
            Set-V2DynamicProperty -InputObject $neo4j -Name "password" -Value $clearNeoPass
        }
    }

    $coordinationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $composePathRaw = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "docker_compose_file" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($composePathRaw)) {
        if ($infraMode -eq "shared-infra") {
            $composePathRaw = Join-Path $coordinationRoot "workspace/shared-infra/ai-orchestrator/docker/docker-compose.generated.yml"
        }
        else {
            $composePathRaw = "ai-orchestrator/docker/docker-compose.generated.yml"
        }
    }
    $composePath = if ([System.IO.Path]::IsPathRooted($composePathRaw)) { $composePathRaw } else { Join-Path $resolvedProjectPath $composePathRaw }
    $composeEngine = Get-V2DockerComposeEngine

    $cleanupErrors = New-Object System.Collections.Generic.List[string]
    $cleanupNotes = New-Object System.Collections.Generic.List[string]

    if ([bool](Get-V2OptionalProperty -InputObject $qdrant -Name "enabled" -DefaultValue $false)) {
        try {
            $qdrantUrl = [string](Get-V2OptionalProperty -InputObject $qdrant -Name "url" -DefaultValue "")
            $qdrantCollection = [string](Get-V2OptionalProperty -InputObject $qdrant -Name "collection" -DefaultValue "")
            if ([string]::IsNullOrWhiteSpace($qdrantUrl) -or [string]::IsNullOrWhiteSpace($qdrantCollection)) {
                $cleanupErrors.Add("qdrant-cleanup-missing-url-or-collection")
            }
            else {
                $deleteUri = "{0}/collections/{1}" -f $qdrantUrl.TrimEnd("/"), $qdrantCollection
                try {
                    Invoke-RestMethod -Uri $deleteUri -Method Delete -TimeoutSec 12 | Out-Null
                    $cleanupNotes.Add("qdrant-collection-removed: $qdrantCollection")
                }
                catch {
                    $msg = [string]$_.Exception.Message
                    if ($msg -match "404|not found") {
                        $cleanupNotes.Add("qdrant-collection-not-found: $qdrantCollection")
                    }
                    else {
                        throw
                    }
                }
            }
        }
        catch {
            $cleanupErrors.Add("qdrant-cleanup-failed: $($_.Exception.Message)")
        }
    }

    if ([bool](Get-V2OptionalProperty -InputObject $neo4j -Name "enabled" -DefaultValue $false)) {
        try {
            $neoHost = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "host" -DefaultValue "localhost")
            $neoHttpPort = [int](Get-V2OptionalProperty -InputObject $neo4j -Name "http_port" -DefaultValue 7474)
            $neoUser = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "user" -DefaultValue "neo4j")
            $neoPassword = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "password" -DefaultValue "")
            $neoDatabase = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "database" -DefaultValue "neo4j")
            if ([string]::IsNullOrWhiteSpace($neoPassword)) {
                $cleanupErrors.Add("neo4j-cleanup-missing-password")
            }
            else {
                $baseUrl = "http://{0}:{1}" -f $neoHost, $neoHttpPort
                $headers = @{
                    Authorization = (New-V2BasicAuthHeader -Username $neoUser -Password $neoPassword)
                    "Content-Type" = "application/json"
                }
                Invoke-V2Neo4jHttpCommit -BaseUrl $baseUrl -Database $neoDatabase -Headers $headers -Statements @(
                    @{
                        statement = @"
MATCH (n:MemoryNode {project_slug: `$project_slug})
DETACH DELETE n
"@
                        parameters = @{ project_slug = $projectSlug }
                    }
                    @{
                        statement = @"
MATCH (p:Project {slug: `$project_slug})
DETACH DELETE p
"@
                        parameters = @{ project_slug = $projectSlug }
                    }
                ) | Out-Null
                $cleanupNotes.Add("neo4j-namespace-removed: $projectSlug")
            }
        }
        catch {
            $cleanupErrors.Add("neo4j-cleanup-failed: $($_.Exception.Message)")
        }
    }

    if ([bool](Get-V2OptionalProperty -InputObject $relational -Name "enabled" -DefaultValue $false)) {
        try {
            $dbEngine = Get-V2CanonicalRelationalEngine -Engine ([string](Get-V2OptionalProperty -InputObject $relational -Name "engine" -DefaultValue ""))
            if ($dbEngine -ne "postgres") {
                $cleanupNotes.Add("relational-cleanup-skipped-unsupported-engine:$dbEngine")
            }
            elseif ($composeEngine -eq "unavailable" -or -not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
                $cleanupErrors.Add("relational-cleanup-compose-not-available")
            }
            else {
                $dbName = [string](Get-V2OptionalProperty -InputObject $relational -Name "database" -DefaultValue "")
                $dbUser = [string](Get-V2OptionalProperty -InputObject $relational -Name "user" -DefaultValue "")
                $dbPassword = [string](Get-V2OptionalProperty -InputObject $relational -Name "password" -DefaultValue "")
                $schema = [string](Get-V2OptionalProperty -InputObject $relational -Name "schema" -DefaultValue (Get-V2ProjectSchemaName -ProjectSlug $projectSlug))
                $slugLiteral = ConvertTo-V2SqlLiteral -Value $projectSlug
                $schemaQuoted = '"' + ($schema -replace '"', '""') + '"'
                $sql = @"
DELETE FROM ai_orchestrator.project_registry WHERE project_slug = $slugLiteral;
DROP SCHEMA IF EXISTS $schemaQuoted CASCADE;
SELECT 1;
"@
                $command = @"
cat <<'SQL' | psql -h 127.0.0.1 -U "$dbUser" -d "$dbName" -v ON_ERROR_STOP=1 -tA
$sql
SQL
"@
                $exec = Invoke-V2ComposeExec `
                    -ComposeEngine $composeEngine `
                    -ComposePath $composePath `
                    -Service "db" `
                    -Arguments @("sh", "-lc", $command) `
                    -Environment @{ PGPASSWORD = $dbPassword }
                if (-not $exec.success) {
                    $cleanupErrors.Add("relational-cleanup-failed: $([string]$exec.text)")
                }
                else {
                    $cleanupNotes.Add("relational-schema-removed: $schema")
                }
            }
        }
        catch {
            $cleanupErrors.Add("relational-cleanup-exception: $($_.Exception.Message)")
        }
    }

    $cleanupStatus = if ($cleanupErrors.Count -eq 0) { "cleaned" } else { "cleanup-error" }
    Set-V2DynamicProperty -InputObject $state -Name "status" -Value $cleanupStatus
    Set-V2DynamicProperty -InputObject $state -Name "updated_at" -Value (Get-V2Timestamp)
    Set-V2DynamicProperty -InputObject $state -Name "cleanup_notes" -Value @($cleanupNotes.ToArray())
    Set-V2DynamicProperty -InputObject $state -Name "cleanup_errors" -Value @($cleanupErrors.ToArray())
    Save-V2JsonContent -Path $statePath -Value $state

    return [PSCustomObject]@{
        project_path = $resolvedProjectPath
        project_slug = $projectSlug
        infra_mode   = $infraMode
        status       = $cleanupStatus
        notes        = @($cleanupNotes.ToArray())
        errors       = @($cleanupErrors.ToArray())
    }
}

function Invoke-V2EnsureProjectIsolationFromState {
    param([string]$ProjectPath)

    $resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
    $orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
    $statePath = Join-Path $orchestratorRoot "state/project-state.json"
    $state = Get-V2JsonContent -Path $statePath
    if ($null -eq $state) {
        return [PSCustomObject]@{
            status = "skipped"
            notes  = @("state-unavailable")
            errors = @("project-state-unavailable")
        }
    }

    $projectSlug = [string](Get-V2OptionalProperty -InputObject $state -Name "project_slug" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($projectSlug)) {
        return [PSCustomObject]@{
            status = "error"
            notes  = @()
            errors = @("project-slug-missing-in-state")
        }
    }

    $databaseMap = Get-V2OptionalProperty -InputObject $state -Name "databases" -DefaultValue ([PSCustomObject]@{})
    $relational = Get-V2OptionalProperty -InputObject $databaseMap -Name "relational" -DefaultValue ([PSCustomObject]@{})
    $neo4j = Get-V2OptionalProperty -InputObject $databaseMap -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
    $qdrant = Get-V2OptionalProperty -InputObject $databaseMap -Name "qdrant" -DefaultValue ([PSCustomObject]@{})

    $startupPaths = Get-V2OptionalProperty -InputObject $state -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})
    $secretsVaultRelative = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "secrets_vault" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
    $secretsVaultPath = Join-Path $resolvedProjectPath $secretsVaultRelative
    $vault = Get-V2JsonContent -Path $secretsVaultPath
    if ($vault) {
        $vaultSecrets    = Get-V2OptionalProperty -InputObject $vault -Name "secrets" -DefaultValue ([PSCustomObject]@{})
        $vaultRelational = Get-V2OptionalProperty -InputObject $vaultSecrets -Name "relational" -DefaultValue ([PSCustomObject]@{})
        $vaultNeo4j      = Get-V2OptionalProperty -InputObject $vaultSecrets -Name "neo4j"      -DefaultValue ([PSCustomObject]@{})
        $vaultEncrypted  = [bool](Get-V2OptionalProperty -InputObject $vault -Name "encrypted" -DefaultValue $false)
        $rawVaultRelPass = [string](Get-V2OptionalProperty -InputObject $vaultRelational -Name "password" -DefaultValue "")
        $rawVaultNeoPass = [string](Get-V2OptionalProperty -InputObject $vaultNeo4j      -Name "password" -DefaultValue "")
        $clearRelPass = if ($vaultEncrypted -and -not [string]::IsNullOrEmpty($rawVaultRelPass)) { Unprotect-V2Secret -CipherBase64 $rawVaultRelPass } else { $rawVaultRelPass }
        $clearNeoPass = if ($vaultEncrypted -and -not [string]::IsNullOrEmpty($rawVaultNeoPass)) { Unprotect-V2Secret -CipherBase64 $rawVaultNeoPass } else { $rawVaultNeoPass }
        $dockerEnvPath = Join-Path $resolvedProjectPath "ai-orchestrator/docker/.env.docker.generated"
        $dockerEnvMap = Get-V2EnvMap -EnvPath $dockerEnvPath
        if ([string]::IsNullOrWhiteSpace($clearRelPass) -and $dockerEnvMap.ContainsKey("DB_PASSWORD")) {
            $clearRelPass = [string]$dockerEnvMap["DB_PASSWORD"]
        }
        if ([string]::IsNullOrWhiteSpace($clearNeoPass) -and $dockerEnvMap.ContainsKey("NEO4J_PASSWORD")) {
            $clearNeoPass = [string]$dockerEnvMap["NEO4J_PASSWORD"]
        }
        $relPass = [string](Get-V2OptionalProperty -InputObject $relational -Name "password" -DefaultValue "")
        if (([string]::IsNullOrWhiteSpace($relPass) -or $relPass -eq "[stored in vault]" -or $relPass -match "\*") -and -not [string]::IsNullOrWhiteSpace($clearRelPass)) {
            Set-V2DynamicProperty -InputObject $relational -Name "password" -Value $clearRelPass
        }
        $neoPass = [string](Get-V2OptionalProperty -InputObject $neo4j -Name "password" -DefaultValue "")
        if (([string]::IsNullOrWhiteSpace($neoPass) -or $neoPass -eq "[stored in vault]" -or $neoPass -match "\*") -and -not [string]::IsNullOrWhiteSpace($clearNeoPass)) {
            Set-V2DynamicProperty -InputObject $neo4j -Name "password" -Value $clearNeoPass
        }
    }

    $notes = New-Object System.Collections.Generic.List[string]
    $errors = New-Object System.Collections.Generic.List[string]
    $coordinationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $orchestratorCoreCheck = Invoke-V2OrchestratorCoreBootstrap -CoordinationRoot $coordinationRoot
    if (-not [bool](Get-V2OptionalProperty -InputObject $orchestratorCoreCheck -Name "success" -DefaultValue $false)) {
        $errors.Add("orchestrator-core-not-ready")
    }
    foreach ($coreNote in @((Get-V2OptionalProperty -InputObject $orchestratorCoreCheck -Name "notes" -DefaultValue @()))) {
        if (-not [string]::IsNullOrWhiteSpace([string]$coreNote)) {
            $notes.Add("orchestrator-core::$coreNote")
        }
    }
    foreach ($coreError in @((Get-V2OptionalProperty -InputObject $orchestratorCoreCheck -Name "errors" -DefaultValue @()))) {
        if (-not [string]::IsNullOrWhiteSpace([string]$coreError)) {
            $errors.Add("orchestrator-core::$coreError")
        }
    }
    $composePathRaw = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "docker_compose_file" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($composePathRaw)) {
        $infraMode = [string](Get-V2OptionalProperty -InputObject $state -Name "infra_mode" -DefaultValue "dedicated-infra")
        if ($infraMode -eq "shared-infra") {
            $composePathRaw = Join-Path $coordinationRoot "workspace/shared-infra/ai-orchestrator/docker/docker-compose.generated.yml"
        }
        else {
            $composePathRaw = "ai-orchestrator/docker/docker-compose.generated.yml"
        }
    }
    $composePath = if ([System.IO.Path]::IsPathRooted($composePathRaw)) { $composePathRaw } else { Join-Path $resolvedProjectPath $composePathRaw }
    $infraMode = [string](Get-V2OptionalProperty -InputObject $state -Name "infra_mode" -DefaultValue "dedicated-infra")
    if ($infraMode -eq "dedicated-infra") {
        $isolationCheck = Test-V2DedicatedProjectContainerIsolation -ComposePath $composePath -ProjectSlug $projectSlug
        foreach ($isolationNote in @((Get-V2OptionalProperty -InputObject $isolationCheck -Name "notes" -DefaultValue @()))) {
            if (-not [string]::IsNullOrWhiteSpace([string]$isolationNote)) {
                $notes.Add("container-isolation::$isolationNote")
            }
        }
        foreach ($isolationError in @((Get-V2OptionalProperty -InputObject $isolationCheck -Name "errors" -DefaultValue @()))) {
            if (-not [string]::IsNullOrWhiteSpace([string]$isolationError)) {
                $errors.Add("container-isolation::$isolationError")
            }
        }
    }
    else {
        $notes.Add("container-isolation::shared-infra-mode-selected (strict project container isolation skipped)")
    }

    $neo4jNamespace = Ensure-V2Neo4jProjectNamespace -Connection $neo4j -ProjectSlug $projectSlug
    if ([string](Get-V2OptionalProperty -InputObject $neo4jNamespace -Name "status" -DefaultValue "") -like "error*") {
        $errors.Add("neo4j-namespace-check-failed: $([string](Get-V2OptionalProperty -InputObject $neo4jNamespace -Name 'details' -DefaultValue ''))")
    }
    else {
        $notes.Add("neo4j-namespace-ok: $([string](Get-V2OptionalProperty -InputObject $neo4jNamespace -Name 'database' -DefaultValue 'neo4j'))")
    }

    $qdrantCollection = Ensure-V2QdrantProjectCollection -Connection $qdrant -ProjectSlug $projectSlug
    if ([string](Get-V2OptionalProperty -InputObject $qdrantCollection -Name "status" -DefaultValue "") -like "error*") {
        $errors.Add("qdrant-collection-check-failed: $([string](Get-V2OptionalProperty -InputObject $qdrantCollection -Name 'details' -DefaultValue ''))")
    }
    else {
        $notes.Add("qdrant-collection-ok: $([string](Get-V2OptionalProperty -InputObject $qdrantCollection -Name 'collection' -DefaultValue ''))")
    }

    $composeEngine = Get-V2DockerComposeEngine
    if ($composeEngine -ne "unavailable" -and (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        $schemaName = [string](Get-V2OptionalProperty -InputObject $relational -Name "schema" -DefaultValue (Get-V2ProjectSchemaName -ProjectSlug $projectSlug))
        $seed = Ensure-V2RelationalProjectSeed `
            -ComposeEngine $composeEngine `
            -ComposePath $composePath `
            -Connection $relational `
            -ProjectSlug $projectSlug `
            -ProjectName ([string](Get-V2OptionalProperty -InputObject $state -Name "project_name" -DefaultValue $projectSlug)) `
            -ProjectSchema $schemaName
        if ([string](Get-V2OptionalProperty -InputObject $seed -Name "status" -DefaultValue "") -like "error*") {
            $errors.Add("relational-schema-check-failed: $([string](Get-V2OptionalProperty -InputObject $seed -Name 'details' -DefaultValue ''))")
        }
        else {
            $notes.Add("relational-schema-ok: $schemaName")
        }
    }
    else {
        $notes.Add("relational-schema-check-skipped: compose-unavailable")
    }

    return [PSCustomObject]@{
        status = if ($errors.Count -eq 0) { "ready" } else { "error" }
        notes  = @($notes.ToArray())
        errors = @($errors.ToArray())
    }
}

function Start-V2WatchLoop {
    param(
        [string]$InboxRootPath,
        [ValidateSet("dedicated-infra", "shared-infra")]
        [string]$InfraMode = "dedicated-infra"
    )

    $resolvedInboxPath = Resolve-V2AbsolutePath -Path $InboxRootPath
    Initialize-V2Directory -Path $resolvedInboxPath
    $coordinationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    Assert-V2ExecutionEnabled -ProjectRoot $coordinationRoot -ActionName "v2-watch"
    Write-Output "Watching inbox (V2): $resolvedInboxPath"
    while ($true) {
        Assert-V2ExecutionEnabled -ProjectRoot $coordinationRoot -ActionName "v2-watch"
        $directories = @(Get-ChildItem -LiteralPath $resolvedInboxPath -Directory -Force | Sort-Object Name)
        foreach ($directory in $directories) {
            $statePath = Join-Path $directory.FullName "ai-orchestrator/state/project-state.json"
            if (Test-Path -LiteralPath $statePath) {
                continue
            }

            try {
                $result = Invoke-V2Submission `
                    -TargetProjectPath $directory.FullName `
                    -RequestedProjectName $directory.Name `
                    -InfraMode $InfraMode `
                    -EnableNeo4j:$effectiveIncludeNeo4j `
                    -EnableQdrant:$effectiveIncludeQdrant
                Write-Output "V2 submitted '$($result.project_path)' with status '$($result.status)'"
            }
            catch {
                Write-Warning "Failed to process '$($directory.FullName)': $($_.Exception.Message)"
            }
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }
}

function Invoke-V2TaskStateSync {
    param(
        [string]$ProjectRoot,
        [switch]$FailOnError
    )

    if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
        return $false
    }

    $taskSyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "Sync-TaskState.ps1"
    if (-not (Test-Path -LiteralPath $taskSyncScript -PathType Leaf)) {
        return $false
    }

    try {
        & $taskSyncScript -ProjectPath $ProjectRoot | Out-Null
        return $true
    }
    catch {
        $message = "task-sync-failed: $($_.Exception.Message)"
        if ($FailOnError) {
            throw $message
        }
        Write-Warning $message
        return $false
    }
}

function Get-V2CompletionStringList {
    param([object]$InputValue)

    if ($null -eq $InputValue) {
        return @()
    }
    if ($InputValue -is [string]) {
        $parts = @($InputValue -split ",\s*" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        return @($parts)
    }
    if ($InputValue -is [System.Collections.IEnumerable]) {
        $items = New-Object System.Collections.Generic.List[string]
        foreach ($item in $InputValue) {
            $text = [string]$item
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $items.Add($text)
            }
        }
        return @($items.ToArray())
    }
    $single = [string]$InputValue
    if ([string]::IsNullOrWhiteSpace($single)) {
        return @()
    }
    return @($single)
}

function Normalize-V2CompletionList {
    param(
        [object]$InputValue,
        [int]$MaxItems = 30,
        [int]$MaxItemLength = 300
    )

    $raw = @(Get-V2CompletionStringList -InputValue $InputValue)
    $items = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $raw) {
        if ($items.Count -ge $MaxItems) {
            break
        }
        $text = [string]$entry
        if ([string]::IsNullOrWhiteSpace($text)) {
            continue
        }
        $trimmed = $text.Trim()
        if ($trimmed.Length -gt $MaxItemLength) {
            $trimmed = $trimmed.Substring(0, $MaxItemLength) + "…"
        }
        $items.Add($trimmed)
    }
    return @($items.ToArray())
}

function New-V2CompletionPayload {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName,
        [string]$ArtifactsCsv,
        [string]$NotesText,
        [string]$PayloadPath
    )

    $artifactList = Get-V2CompletionStringList -InputValue $ArtifactsCsv
    $payload = $null

    if (-not [string]::IsNullOrWhiteSpace($PayloadPath)) {
        $resolvedPayloadPath = if ([System.IO.Path]::IsPathRooted($PayloadPath)) {
            $PayloadPath
        }
        else {
            Join-Path $ProjectRoot $PayloadPath
        }
        if (-not (Test-Path -LiteralPath $resolvedPayloadPath -PathType Leaf)) {
            throw "CompletionPayloadPath not found: $resolvedPayloadPath"
        }
        $payload = Get-V2JsonContent -Path $resolvedPayloadPath
    }
    elseif (-not [string]::IsNullOrWhiteSpace($NotesText)) {
        $notesTrimmed = $NotesText.Trim()
        if ($notesTrimmed.StartsWith("{") -or $notesTrimmed.StartsWith("[")) {
            try {
                $payload = $notesTrimmed | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                try {
                    $decoded = [regex]::Unescape($notesTrimmed)
                    $payload = $decoded | ConvertFrom-Json -ErrorAction Stop
                }
                catch {
                    # keep backward compatibility and fallback to summary text
                    $payload = $null
                }
            }
        }
    }

    if ($null -eq $payload) {
        $payload = [PSCustomObject]@{
            summary        = [string]$NotesText
            files_written  = @($artifactList)
            validation     = @()
            risks          = @()
            next_steps     = @()
            tests_passed   = $false
            source_files   = @($artifactList)
            source_modules = @()
            local_library_candidates = @()
            library_decision = [PSCustomObject]@{
                selected_option   = "not-applicable"
                justification     = "No local library reuse required for this task."
                selected_libraries = @()
                rejected_libraries = @()
            }
        }
    }

    $summary = [string](Get-V2OptionalProperty -InputObject $payload -Name "summary" -DefaultValue $NotesText)
    if (-not [string]::IsNullOrWhiteSpace($summary)) {
        $summary = $summary.Trim()
        if ($summary.Length -gt 2000) {
            $summary = $summary.Substring(0, 2000) + "…"
        }
    }
    $filesWritten = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "files_written" -DefaultValue @()) -MaxItems 100 -MaxItemLength 400)
    if (@($filesWritten).Count -eq 0) {
        $filesWritten = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "changes" -DefaultValue $artifactList) -MaxItems 100 -MaxItemLength 400)
    }
    if (@($filesWritten).Count -eq 0 -and @($artifactList).Count -gt 0) {
        $filesWritten = @(Normalize-V2CompletionList -InputValue $artifactList -MaxItems 100 -MaxItemLength 400)
    }
    if (@($filesWritten).Count -eq 0) {
        $filesWritten = @(
            "ai-orchestrator/tasks/execution-history.md",
            "ai-orchestrator/communication/messages.md"
        )
    }
    $validation = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "validation" -DefaultValue @()) -MaxItems 30 -MaxItemLength 250)
    $risks = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "risks" -DefaultValue @()) -MaxItems 30 -MaxItemLength 250)
    $nextSteps = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "next_steps" -DefaultValue @()) -MaxItems 30 -MaxItemLength 250)
    $sourceFiles = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "source_files" -DefaultValue $filesWritten) -MaxItems 100 -MaxItemLength 400)
    if (@($sourceFiles).Count -eq 0) {
        $sourceFiles = @($filesWritten)
    }
    if (@($sourceFiles).Count -eq 0) {
        $sourceFiles = @(
            "ai-orchestrator/tasks/execution-history.md",
            "ai-orchestrator/communication/messages.md"
        )
    }
    $sourceModules = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "source_modules" -DefaultValue @()) -MaxItems 60 -MaxItemLength 120)
    if (@($sourceModules).Count -eq 0) {
        $derivedModules = New-Object System.Collections.Generic.List[string]
        foreach ($sourceFile in @($sourceFiles)) {
            $normalized = ([string]$sourceFile -replace "\\", "/")
            $parts = @($normalized -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $module = if ($parts.Count -ge 2) { "$($parts[0])/$($parts[1])" } elseif ($parts.Count -eq 1) { $parts[0] } else { "" }
            if (-not [string]::IsNullOrWhiteSpace($module) -and -not $derivedModules.Contains($module)) {
                $derivedModules.Add($module)
            }
        }
        $sourceModules = @($derivedModules.ToArray())
    }
    $testsPassedRaw = Get-V2OptionalProperty -InputObject $payload -Name "tests_passed" -DefaultValue $null
    $testsPassed = $false
    if ($testsPassedRaw -is [bool]) {
        $testsPassed = [bool]$testsPassedRaw
    }
    elseif ($testsPassedRaw -isnot [bool] -and $null -ne $testsPassedRaw) {
        $testsPassed = [string]$testsPassedRaw -match "^(1|true|yes|passed)$"
    }
    elseif (@($validation | Where-Object { $_ -match "(?i)pass|ok|success" }).Count -gt 0) {
        $testsPassed = $true
    }
    $toolCalls = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "tool_calls" -DefaultValue @()) -MaxItems 80 -MaxItemLength 240)
    $localLibraryCandidates = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $payload -Name "local_library_candidates" -DefaultValue @()) -MaxItems 60 -MaxItemLength 200)

    $libraryDecisionRaw = Get-V2OptionalProperty -InputObject $payload -Name "library_decision" -DefaultValue $null
    $librarySelectedOption = "not-applicable"
    $libraryJustification = "No local library reuse required for this task."
    $librarySelectedLibraries = @()
    $libraryRejectedLibraries = @()
    if ($libraryDecisionRaw -is [string]) {
        $stringDecision = ([string]$libraryDecisionRaw).Trim()
        if (-not [string]::IsNullOrWhiteSpace($stringDecision)) {
            $librarySelectedOption = $stringDecision
            $libraryJustification = "Decision provided as scalar string in completion payload."
        }
    }
    elseif ($null -ne $libraryDecisionRaw) {
        $librarySelectedOption = [string](Get-V2OptionalProperty -InputObject $libraryDecisionRaw -Name "selected_option" -DefaultValue "not-applicable")
        $libraryJustification = [string](Get-V2OptionalProperty -InputObject $libraryDecisionRaw -Name "justification" -DefaultValue "No local library reuse required for this task.")
        $librarySelectedLibraries = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $libraryDecisionRaw -Name "selected_libraries" -DefaultValue @()) -MaxItems 30 -MaxItemLength 200)
        $libraryRejectedLibraries = @(Normalize-V2CompletionList -InputValue (Get-V2OptionalProperty -InputObject $libraryDecisionRaw -Name "rejected_libraries" -DefaultValue @()) -MaxItems 30 -MaxItemLength 200)
    }
    if ([string]::IsNullOrWhiteSpace($librarySelectedOption)) {
        $librarySelectedOption = "not-applicable"
    }
    if ([string]::IsNullOrWhiteSpace($libraryJustification)) {
        $libraryJustification = "No local library reuse required for this task."
    }

    $libraryDecision = [PSCustomObject]@{
        selected_option   = $librarySelectedOption
        justification     = $libraryJustification
        selected_libraries = @($librarySelectedLibraries)
        rejected_libraries = @($libraryRejectedLibraries)
    }

    return [PSCustomObject]@{
        schema_version = "v2"
        task_id        = $TaskId
        agent_name     = $AgentName
        timestamp      = Get-V2Timestamp
        recorded_at    = Get-V2Timestamp
        summary        = $summary
        files_written  = @($filesWritten)
        changes        = @($filesWritten)
        tests_passed   = [bool]$testsPassed
        validation     = @($validation)
        risks          = @($risks)
        next_steps     = @($nextSteps)
        source_files   = @($sourceFiles)
        source_modules = @($sourceModules)
        tool_calls     = @($toolCalls)
        local_library_candidates = @($localLibraryCandidates)
        library_decision = $libraryDecision
    }
}

function Save-V2CompletionPayload {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [object]$Payload
    )

    $completionsDir = Join-Path $ProjectRoot "ai-orchestrator/tasks/completions"
    Initialize-V2Directory -Path $completionsDir
    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $fileName = "{0}-{1}.json" -f $safeTaskId, (Get-Date -Format "yyyyMMddHHmmss")
    $targetPath = Join-Path $completionsDir $fileName
    Save-V2JsonContent -Path $targetPath -Value $Payload
    return (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $targetPath)
}

function Write-V2TaskTransactionEvent {
    param(
        [string]$ProjectRoot,
        [string]$Operation,
        [string]$TaskId,
        [string]$AgentName,
        [string]$StatusFrom,
        [string]$StatusTo,
        [bool]$Success,
        [string]$Reason = "",
        [string]$EvidencePath = "",
        [string]$TransactionId = ""
    )

    if ([string]::IsNullOrWhiteSpace($TransactionId)) {
        $TransactionId = [guid]::NewGuid().ToString()
    }
    $eventsPath = Join-Path $ProjectRoot "ai-orchestrator/state/task-events.jsonl"
    $eventsDir = Split-Path -Parent $eventsPath
    Initialize-V2Directory -Path $eventsDir

    $event = [PSCustomObject]@{
        timestamp      = Get-V2Timestamp
        transaction_id = $TransactionId
        operation      = $Operation
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

function Invoke-V2ValidateCompletionPayloadSchema {
    param(
        [string]$ProjectRoot,
        [string]$AgentName,
        [string]$CompletionPayloadPath
    )

    $validatorScript = Join-Path $PSScriptRoot "Invoke-OutputSchemaValidator.ps1"
    if (-not (Test-Path -LiteralPath $validatorScript -PathType Leaf)) {
        return [PSCustomObject]@{
            success = $false
            errors = @("validator-script-missing")
            warnings = @()
        }
    }

    try {
        $raw = & $validatorScript -ProjectPath $ProjectRoot -AgentName $AgentName -PayloadPath $CompletionPayloadPath -EmitJson 2>&1 | Out-String
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
        $err = $_.Exception.Message
        return [PSCustomObject]@{
            success = $false
            errors = @([string]$err)
            warnings = @()
        }
    }
}

function Ensure-V2TaskPreflightTemplate {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName
    )

    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $safeAgent = if ([string]::IsNullOrWhiteSpace($AgentName)) { "unassigned" } else { ($AgentName -replace "[^A-Za-z0-9_-]+", "_") }
    $preflightDir = Join-Path $ProjectRoot "ai-orchestrator/tasks/preflight"
    Initialize-V2Directory -Path $preflightDir
    $preflightPath = Join-Path $preflightDir ("{0}-{1}.json" -f $safeTaskId, $safeAgent)

    if (-not (Test-Path -LiteralPath $preflightPath -PathType Leaf)) {
        $template = [PSCustomObject]@{
            schema_version      = "v2-preflight"
            task_id             = $TaskId
            agent_name          = $AgentName
            created_at          = Get-V2Timestamp
            objective           = ""
            thought             = ""
            action_plan         = @()
            risks               = @()
            validation_plan     = @()
            dependencies_needed = @()
            requires_human_approval = $false
            library_first_policy = [PSCustomObject]@{
                enabled    = $true
                priority   = "P0"
                local_only = $true
                options    = @("use-existing-library", "hybrid", "custom-code-justified", "not-applicable")
            }
            local_library_candidates = @()
            build_vs_buy_recommendation = [PSCustomObject]@{
                recommended_option = ""
                confidence         = ""
                reason             = ""
            }
            library_decision_required = $true
        }
        Save-V2JsonContent -Path $preflightPath -Value $template
    }

    return (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $preflightPath)
}

$coordinationRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))

switch ($Mode) {
    "submit" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in submit mode."
        }
        $result = Invoke-V2Submission `
            -TargetProjectPath $ProjectPath `
            -RequestedProjectName $ProjectName `
            -InfraMode $InfraMode `
            -PromptForMissingProjectName `
            -EnableNeo4j:$effectiveIncludeNeo4j `
            -EnableQdrant:$effectiveIncludeQdrant
        Write-Output "V2 submit complete: $($result.project_path)"
        Write-Output "Project: $($result.project_name)"
        Write-Output "Project DNA: $($result.project_dna_path)"
        Write-Output "Infra Mode: $($result.infra_mode)"
        Write-Output "State: $($result.state_path)"
        Write-Output "Type: $($result.project_type)"
        Write-Output "Status: $($result.status)"
        Write-Output "Startup Pack: $($result.startup_pack_status)"
        Write-Output "Docker: $($result.docker_status)"
        Write-Output "Orchestrator Core: $($result.orchestrator_core_status)"
        if (-not [string]::IsNullOrWhiteSpace([string]$result.orchestrator_core_compose)) {
            Write-Output "Orchestrator Core Compose: $($result.orchestrator_core_compose)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.access_guide_path)) {
            Write-Output "Access Guide: $($result.access_guide_path)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.secrets_vault_path)) {
            Write-Output "Secrets Vault: $($result.secrets_vault_path)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.neo4j_uri)) {
            Write-Output "Neo4j Bolt: $($result.neo4j_uri)"
            Write-Output "Neo4j Browser: $($result.neo4j_browser_url)"
            Write-Output "Neo4j User: $($result.neo4j_user)"
            Write-Output "Neo4j Password: [stored in vault]"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.qdrant_url)) {
            Write-Output "Qdrant URL: $($result.qdrant_url)"
            Write-Output "Qdrant Collection: $($result.qdrant_collection)"
        }
        if ([string]$result.status -like "blocked-*") {
            throw "v2-submit-failed: project remained blocked after bootstrap ($([string]$result.status))."
        }
    }
    "new" {
        Assert-V2ExecutionEnabled -ProjectRoot $coordinationRoot -ActionName "v2-new"
        if ([string]::IsNullOrWhiteSpace($ProjectName)) {
            throw "ProjectName is required in new mode."
        }
        $managedRoot = Resolve-V2AbsolutePath -Path $ManagedProjectsRoot
        Initialize-V2Directory -Path $managedRoot
        $projectSlug = Get-V2ProjectSlug -Name $ProjectName
        $newProjectPath = Join-Path $managedRoot $projectSlug
        if ((Test-Path -LiteralPath $newProjectPath) -and -not $Force) {
            throw "Project path already exists: $newProjectPath. Use -Force to replace."
        }
        if (Test-Path -LiteralPath $newProjectPath) {
            if (-not $ForceConfirmed) {
                throw "Destructive action blocked: existing project path requires -ForceConfirmed together with -Force."
            }
            Remove-Item -LiteralPath $newProjectPath -Recurse -Force
        }
        Initialize-V2Directory -Path $newProjectPath

        $requestPath = Join-Path $newProjectPath "PROJECT_REQUEST.md"
        if (-not [string]::IsNullOrWhiteSpace($ProjectBriefPath)) {
            $briefPath = Resolve-V2AbsolutePath -Path $ProjectBriefPath
            if (-not (Test-Path -LiteralPath $briefPath -PathType Leaf)) {
                throw "ProjectBriefPath not found: $ProjectBriefPath"
            }
            Copy-Item -LiteralPath $briefPath -Destination $requestPath -Force
        }
        else {
            $template = @"
# Project Request

## Objective
[Describe project objective]

## Expected Architecture
[monolith|microservices|unknown]

## Main Language
[language/framework]

## Infrastructure Target
[local|cloud|hybrid]

## Database Preference
[postgres|mysql|mongodb|none|unknown]

## Deployment Target
[windows service|container|cloud runtime|unknown]
"@
            [System.IO.File]::WriteAllText($requestPath, $template)
        }

        $result = Invoke-V2Submission `
            -TargetProjectPath $newProjectPath `
            -RequestedProjectName $ProjectName `
            -InfraMode $InfraMode `
            -EnableNeo4j:$effectiveIncludeNeo4j `
            -EnableQdrant:$effectiveIncludeQdrant
        Write-Output "V2 new project initialized: $($result.project_path)"
        Write-Output "Project: $($result.project_name)"
        Write-Output "Project DNA: $($result.project_dna_path)"
        Write-Output "Infra Mode: $($result.infra_mode)"
        Write-Output "State: $($result.state_path)"
        Write-Output "Status: $($result.status)"
        Write-Output "Startup Pack: $($result.startup_pack_status)"
        Write-Output "Orchestrator Core: $($result.orchestrator_core_status)"
        if (-not [string]::IsNullOrWhiteSpace([string]$result.orchestrator_core_compose)) {
            Write-Output "Orchestrator Core Compose: $($result.orchestrator_core_compose)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.access_guide_path)) {
            Write-Output "Access Guide: $($result.access_guide_path)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.secrets_vault_path)) {
            Write-Output "Secrets Vault: $($result.secrets_vault_path)"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.neo4j_uri)) {
            Write-Output "Neo4j Bolt: $($result.neo4j_uri)"
            Write-Output "Neo4j Browser: $($result.neo4j_browser_url)"
            Write-Output "Neo4j User: $($result.neo4j_user)"
            Write-Output "Neo4j Password: [stored in vault]"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$result.qdrant_url)) {
            Write-Output "Qdrant URL: $($result.qdrant_url)"
            Write-Output "Qdrant Collection: $($result.qdrant_collection)"
        }
        if ([string]$result.status -like "blocked-*") {
            throw "v2-new-failed: project remained blocked after bootstrap ($([string]$result.status))."
        }
    }
    "watch" {
        Assert-V2ExecutionEnabled -ProjectRoot $coordinationRoot -ActionName "v2-watch"
        Start-V2WatchLoop -InboxRootPath $InboxPath -InfraMode $InfraMode
    }
    "observe" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in observe mode."
        }
        $resolvedObserveProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
        Assert-V2ExecutionEnabled -ProjectRoot $resolvedObserveProjectPath -ActionName "v2-observe"
        $startupCheck = Invoke-V2EnsureProjectIsolationFromState -ProjectPath $resolvedObserveProjectPath
        if (@($startupCheck.errors).Count -gt 0) {
            foreach ($startupErr in @($startupCheck.errors)) {
                Write-Warning "startup-isolation-check: $startupErr"
            }
        }

        $observerScript = Join-Path $PSScriptRoot "Invoke-ObserverV2.ps1"
        $observerArgs = @{
            ProjectPath   = $ProjectPath
            IncludeNeo4j  = $effectiveIncludeNeo4j
            IncludeQdrant = $effectiveIncludeQdrant
        }
        if ($RunOnce) { $observerArgs.RunOnce = $true }
        if ($SkipMemorySync) { $observerArgs.SkipMemorySync = $true }

        & $observerScript @observerArgs
    }
    "access" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in access mode."
        }

        $resolvedAccessProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
        if (-not $resolvedAccessProjectPath -or -not (Test-Path -LiteralPath $resolvedAccessProjectPath -PathType Container)) {
            throw "Project path does not exist: $ProjectPath"
        }

        $orchestratorRoot = Join-Path $resolvedAccessProjectPath "ai-orchestrator"
        $statePath = Join-Path $orchestratorRoot "state/project-state.json"
        if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
            throw "project-state.json not found. Run v2-submit first."
        }

        $state = Get-V2JsonContent -Path $statePath
        $startupPaths = Get-V2OptionalProperty -InputObject $state -Name "startup_paths" -DefaultValue ([PSCustomObject]@{})
        $accessGuideRelative = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "access_guide" -DefaultValue "ai-orchestrator/database/access.md")
        $accessGuidePath = Join-Path $resolvedAccessProjectPath $accessGuideRelative
        $secretsVaultRelative = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "secrets_vault" -DefaultValue "ai-orchestrator/database/.secrets/vault.json")
        $secretsVaultPath = Join-Path $resolvedAccessProjectPath $secretsVaultRelative
        $databaseMap = Get-V2OptionalProperty -InputObject $state -Name "databases" -DefaultValue ([PSCustomObject]@{})
        $neo4j = Get-V2OptionalProperty -InputObject $databaseMap -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
        $qdrant = Get-V2OptionalProperty -InputObject $databaseMap -Name "qdrant" -DefaultValue ([PSCustomObject]@{})
        $relational = Get-V2OptionalProperty -InputObject $databaseMap -Name "relational" -DefaultValue ([PSCustomObject]@{})
        $neo4jHttpPort = [int](Get-V2OptionalProperty -InputObject $neo4j -Name "http_port" -DefaultValue 0)
        $neo4jBrowserUrl = if ($neo4jHttpPort -gt 0) { "http://localhost:$neo4jHttpPort/browser/" } else { "" }

        Write-Output "V2 access snapshot: $resolvedAccessProjectPath"
        Write-Output "Project: $([string](Get-V2OptionalProperty -InputObject $state -Name 'project_name' -DefaultValue 'unknown'))"
        Write-Output "Infra Mode: $([string](Get-V2OptionalProperty -InputObject $state -Name 'infra_mode' -DefaultValue 'dedicated-infra'))"
        Write-Output "State: $statePath"
        Write-Output "Status: $([string](Get-V2OptionalProperty -InputObject $state -Name 'status' -DefaultValue 'unknown'))"
        $orchestratorCoreState = Get-V2OptionalProperty -InputObject $state -Name "orchestrator_core" -DefaultValue ([PSCustomObject]@{})
        Write-Output "Orchestrator Core Status: $([string](Get-V2OptionalProperty -InputObject $orchestratorCoreState -Name 'status' -DefaultValue 'unknown'))"
        $orchestratorCoreComposePath = [string](Get-V2OptionalProperty -InputObject $startupPaths -Name "orchestrator_core_compose" -DefaultValue "")
        if (-not [string]::IsNullOrWhiteSpace($orchestratorCoreComposePath)) {
            Write-Output "Orchestrator Core Compose: $orchestratorCoreComposePath"
        }
        if (Test-Path -LiteralPath $accessGuidePath -PathType Leaf) {
            Write-Output "Access Guide: $accessGuidePath"
        }
        else {
            Write-Output "Access Guide: missing ($accessGuidePath)"
        }
        if (Test-Path -LiteralPath $secretsVaultPath -PathType Leaf) {
            Write-Output "Secrets Vault: $secretsVaultPath"
        }
        else {
            Write-Output "Secrets Vault: missing ($secretsVaultPath)"
        }
        Write-Output "Relational Host: $([string](Get-V2OptionalProperty -InputObject $relational -Name 'host' -DefaultValue ''))"
        Write-Output "Relational Port: $([int](Get-V2OptionalProperty -InputObject $relational -Name 'port' -DefaultValue 0))"
        Write-Output "Relational DB: $([string](Get-V2OptionalProperty -InputObject $relational -Name 'database' -DefaultValue ''))"
        Write-Output "Relational Schema: $([string](Get-V2OptionalProperty -InputObject $relational -Name 'schema' -DefaultValue ''))"
        Write-Output "Relational User: $([string](Get-V2OptionalProperty -InputObject $relational -Name 'user' -DefaultValue ''))"
        Write-Output "Relational Password: [stored in vault]"
        Write-Output "Neo4j Bolt: $([string](Get-V2OptionalProperty -InputObject $neo4j -Name 'uri' -DefaultValue ''))"
        Write-Output "Neo4j Browser: $neo4jBrowserUrl"
        Write-Output "Neo4j Database: $([string](Get-V2OptionalProperty -InputObject $neo4j -Name 'database' -DefaultValue 'neo4j'))"
        Write-Output "Neo4j User: $([string](Get-V2OptionalProperty -InputObject $neo4j -Name 'user' -DefaultValue 'neo4j'))"
        Write-Output "Neo4j Password: [stored in vault]"
        Write-Output "Qdrant URL: $([string](Get-V2OptionalProperty -InputObject $qdrant -Name 'url' -DefaultValue ''))"
        Write-Output "Qdrant Collection: $([string](Get-V2OptionalProperty -InputObject $qdrant -Name 'collection' -DefaultValue ''))"
        $verification = Get-V2OptionalProperty -InputObject $state -Name "bootstrap_verification" -DefaultValue ([PSCustomObject]@{})
        foreach ($backend in @("relational", "relational_domain", "neo4j", "qdrant")) {
            $item = Get-V2OptionalProperty -InputObject $verification -Name $backend -DefaultValue $null
            if ($null -eq $item) {
                continue
            }
            $itemStatus = [string](Get-V2OptionalProperty -InputObject $item -Name "status" -DefaultValue "unknown")
            $itemCount = [int](Get-V2OptionalProperty -InputObject $item -Name "records_seeded" -DefaultValue 0)
            $itemDetails = [string](Get-V2OptionalProperty -InputObject $item -Name "details" -DefaultValue "")
            Write-Output ("Bootstrap Verify ({0}): status={1} records={2} details={3}" -f $backend, $itemStatus, $itemCount, $itemDetails)
        }
    }
    "schedule" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in schedule mode."
        }
        $resolvedScheduleProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
        Assert-V2ExecutionEnabled -ProjectRoot $resolvedScheduleProjectPath -ActionName "v2-schedule"
        $startupCheck = Invoke-V2EnsureProjectIsolationFromState -ProjectPath $resolvedScheduleProjectPath
        if (@($startupCheck.errors).Count -gt 0) {
            foreach ($startupErr in @($startupCheck.errors)) {
                Write-Warning "startup-isolation-check: $startupErr"
            }
        }

        $schedulerScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
        & $schedulerScript -ProjectPath $ProjectPath
    }
    "prompt" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in prompt mode."
        }
        $promptScript = Join-Path $PSScriptRoot "Invoke-AgentPromptOps.ps1"
        if (-not (Test-Path -LiteralPath $promptScript -PathType Leaf)) {
            throw "Prompt operations script not found: $promptScript"
        }
        $promptArgs = @{
            ProjectPath = $ProjectPath
            Action = $PromptAction
            AgentName = $AgentName
            TaskId = $TaskId
            Artifacts = $Artifacts
            Notes = $Notes
            CompletionPayloadPath = $CompletionPayloadPath
        }
        if ($EmitJson) {
            $promptArgs.EmitJson = $true
        }
        & $promptScript @promptArgs
    }
    "clean" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required in clean mode."
        }
        $result = Invoke-V2ProjectCleanup -ProjectPath $ProjectPath -Force:$Force
        Write-Output "V2 clean complete: $($result.project_path)"
        Write-Output "Project Slug: $($result.project_slug)"
        Write-Output "Infra Mode: $($result.infra_mode)"
        Write-Output "Cleanup Status: $($result.status)"
        foreach ($note in @($result.notes)) {
            Write-Output "Note: $note"
        }
        foreach ($err in @($result.errors)) {
            Write-Output "Error: $err"
        }
    }
    # ── claim: agent marks a task as in-progress and acquires it ──────────────────────────────
    "claim" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) { throw "ProjectPath is required in claim mode." }
        if ([string]::IsNullOrWhiteSpace($TaskId))      { throw "TaskId is required in claim mode." }
        if ([string]::IsNullOrWhiteSpace($AgentName))   { throw "AgentName is required in claim mode." }

        $claimProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
        $dagPath = Join-Path $claimProjectPath "ai-orchestrator/tasks/task-dag.json"
        if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) { throw "task-dag.json not found: $dagPath" }
        $dagMutexName = Get-V2DagMutexName -DagPath $dagPath
        $dagMutex = New-Object System.Threading.Mutex($false, $dagMutexName)
        $hasDagMutex = $false
        $dagBackupRaw = ""
        $dag = $null
        $found = $false
        $statusBefore = ""
        $preflightRelativePath = ""
        $claimTransactionId = [guid]::NewGuid().ToString()
        $claimedTask = $null
        try {
            $hasDagMutex = $dagMutex.WaitOne([TimeSpan]::FromSeconds(20))
            if (-not $hasDagMutex) {
                throw "dag-mutex-timeout: could not acquire task-dag lock for claim."
            }

            $dagBackupRaw = Get-Content -LiteralPath $dagPath -Raw -ErrorAction SilentlyContinue
            $dag = Get-V2JsonContent -Path $dagPath
            foreach ($task in @($dag.tasks)) {
                $id = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                if ($id -ne $TaskId) { continue }
                $currentStatus = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")
                $currentAssignee = [string](Get-V2OptionalProperty -InputObject $task -Name "assigned_agent" -DefaultValue "")
                $statusBefore = $currentStatus
                if ($currentStatus -eq "done" -or $currentStatus -eq "skipped") {
                    throw "Cannot claim task '$TaskId': already in status '$currentStatus'."
                }
                if (
                    $currentStatus -eq "in-progress" -and
                    -not [string]::IsNullOrWhiteSpace($currentAssignee) -and
                    $currentAssignee -ne $AgentName
                ) {
                    $idleMinutes = Get-V2TaskIdleMinutes -Task $task
                    $canTakeoverByIdle = ($ClaimTakeoverIdleMinutes -gt 0 -and $idleMinutes -ge [double]$ClaimTakeoverIdleMinutes)
                    if (-not $Force -and -not $canTakeoverByIdle) {
                        throw ("Cannot claim task '{0}': currently in-progress by '{1}' (idle={2}m, takeover_threshold={3}m). Use -Force to override." -f $TaskId, $currentAssignee, $idleMinutes, $ClaimTakeoverIdleMinutes)
                    }
                    $takeoverReason = if ($Force) { "manual-force-claim" } else { "claim-idle-timeout" }
                    Set-V2DynamicProperty -InputObject $task -Name "last_takeover_from_agent" -Value $currentAssignee
                    Set-V2DynamicProperty -InputObject $task -Name "last_takeover_reason" -Value $takeoverReason
                }
                Set-V2DynamicProperty -InputObject $task -Name "status"         -Value "in-progress"
                Set-V2DynamicProperty -InputObject $task -Name "assigned_agent" -Value $AgentName
                Set-V2DynamicProperty -InputObject $task -Name "assigned_at"    -Value (Get-V2Timestamp)
                Set-V2DynamicProperty -InputObject $task -Name "started_at"     -Value (Get-V2Timestamp)
                Set-V2DynamicProperty -InputObject $task -Name "updated_at"     -Value (Get-V2Timestamp)
                $preflightRelativePath = Ensure-V2TaskPreflightTemplate -ProjectRoot $claimProjectPath -TaskId $TaskId -AgentName $AgentName
                if (-not [string]::IsNullOrWhiteSpace($preflightRelativePath)) {
                    Set-V2DynamicProperty -InputObject $task -Name "preflight_path" -Value $preflightRelativePath
                }
                if (-not [string]::IsNullOrWhiteSpace($Notes)) {
                    Set-V2DynamicProperty -InputObject $task -Name "claim_notes" -Value $Notes
                }
                $found = $true
                $claimedTask = $task
                break
            }
            if (-not $found) { throw "Task '$TaskId' not found in task-dag.json." }

            Save-V2JsonContent -Path $dagPath -Value $dag

            # Regenerate task board markdown files so backlog.md / in-progress.md stay in sync
            Invoke-V2TaskStateSync -ProjectRoot $claimProjectPath -FailOnError
        }
        catch {
            if (-not [string]::IsNullOrWhiteSpace($dagBackupRaw)) {
                [System.IO.File]::WriteAllText($dagPath, $dagBackupRaw)
            }
            throw
        }
        finally {
            if ($hasDagMutex) {
                $dagMutex.ReleaseMutex()
            }
            $dagMutex.Dispose()
        }
        Write-V2TaskTransactionEvent `
            -ProjectRoot $claimProjectPath `
            -Operation "claim" `
            -TaskId $TaskId `
            -AgentName $AgentName `
            -StatusFrom $statusBefore `
            -StatusTo "in-progress" `
            -Success $true `
            -EvidencePath $preflightRelativePath `
            -TransactionId $claimTransactionId

        Write-Output "CLAIM OK: Task '$TaskId' claimed by '$AgentName'."
        if (-not [string]::IsNullOrWhiteSpace($preflightRelativePath)) {
            Write-Output "PRE-FLIGHT TEMPLATE: $preflightRelativePath"
        }
        Write-Output ($claimedTask | ConvertTo-Json -Depth 5)
    }

    # ── complete: agent marks a task as done and records artifacts ─────────────────────────────
    "complete" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) { throw "ProjectPath is required in complete mode." }
        if ([string]::IsNullOrWhiteSpace($TaskId))      { throw "TaskId is required in complete mode." }

        $completeProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
        $dagPath   = Join-Path $completeProjectPath "ai-orchestrator/tasks/task-dag.json"
        $locksPath = Join-Path $completeProjectPath "ai-orchestrator/locks/locks.json"
        $patternDir = Join-Path $completeProjectPath "ai-orchestrator/patterns"
        if (-not (Test-Path -LiteralPath $dagPath -PathType Leaf)) { throw "task-dag.json not found: $dagPath" }
        $dagMutexName = Get-V2DagMutexName -DagPath $dagPath
        $dagMutex = New-Object System.Threading.Mutex($false, $dagMutexName)
        $hasDagMutex = $false
        $dagBackupRaw = ""

        $completionPayload = New-V2CompletionPayload `
            -ProjectRoot $completeProjectPath `
            -TaskId $TaskId `
            -AgentName $AgentName `
            -ArtifactsCsv $Artifacts `
            -NotesText $Notes `
            -PayloadPath $CompletionPayloadPath

        $completionPayloadPath = Save-V2CompletionPayload `
            -ProjectRoot $completeProjectPath `
            -TaskId $TaskId `
            -Payload $completionPayload

        $payloadSummary = [string](Get-V2OptionalProperty -InputObject $completionPayload -Name "summary" -DefaultValue $Notes)
        $payloadChanges = Get-V2CompletionStringList -InputValue (Get-V2OptionalProperty -InputObject $completionPayload -Name "files_written" -DefaultValue @())
        if (@($payloadChanges).Count -eq 0) {
            $payloadChanges = Get-V2CompletionStringList -InputValue (Get-V2OptionalProperty -InputObject $completionPayload -Name "changes" -DefaultValue @())
        }
        $payloadSourceFiles = Get-V2CompletionStringList -InputValue (Get-V2OptionalProperty -InputObject $completionPayload -Name "source_files" -DefaultValue @())
        $payloadSourceModules = Get-V2CompletionStringList -InputValue (Get-V2OptionalProperty -InputObject $completionPayload -Name "source_modules" -DefaultValue @())

        $schemaValidation = Invoke-V2ValidateCompletionPayloadSchema `
            -ProjectRoot $completeProjectPath `
            -AgentName $AgentName `
            -CompletionPayloadPath $completionPayloadPath

        $schemaValid = [bool](Get-V2OptionalProperty -InputObject $schemaValidation -Name "success" -DefaultValue $false)
        $schemaErrors = @((Get-V2OptionalProperty -InputObject $schemaValidation -Name "errors" -DefaultValue @()))
        $schemaWarnings = @((Get-V2OptionalProperty -InputObject $schemaValidation -Name "warnings" -DefaultValue @()))

        $found = $false
        $completedTask = $null
        $statusBefore = ""
        $statusAfter = ""
        $completionAccepted = $false
        $completeTransactionId = [guid]::NewGuid().ToString()
        try {
            $hasDagMutex = $dagMutex.WaitOne([TimeSpan]::FromSeconds(20))
            if (-not $hasDagMutex) {
                throw "dag-mutex-timeout: could not acquire task-dag lock for complete."
            }

            $dagBackupRaw = Get-Content -LiteralPath $dagPath -Raw -ErrorAction SilentlyContinue
            $dag = Get-V2JsonContent -Path $dagPath
            foreach ($task in @($dag.tasks)) {
                $id = [string](Get-V2OptionalProperty -InputObject $task -Name "id" -DefaultValue "")
                if ($id -ne $TaskId) { continue }
                $statusBefore = [string](Get-V2OptionalProperty -InputObject $task -Name "status" -DefaultValue "")

                $timestamp = Get-V2Timestamp
                Set-V2DynamicProperty -InputObject $task -Name "updated_at" -Value $timestamp
                Set-V2DynamicProperty -InputObject $task -Name "completion_payload_schema" -Value "v2"
                Set-V2DynamicProperty -InputObject $task -Name "completion_payload_path" -Value $completionPayloadPath
                Set-V2DynamicProperty -InputObject $task -Name "completion_payload_summary" -Value $payloadSummary
                Set-V2DynamicProperty -InputObject $task -Name "completion_payload_changes" -Value @($payloadChanges | Select-Object -First 40)
                Set-V2DynamicProperty -InputObject $task -Name "completion_schema_validation" -Value ([PSCustomObject]@{
                        valid = $schemaValid
                        errors = @($schemaErrors | Select-Object -First 20)
                        warnings = @($schemaWarnings | Select-Object -First 20)
                        validated_at = $timestamp
                    })
                if ($task.PSObject.Properties.Name -contains "completion_payload") {
                    [void]$task.PSObject.Properties.Remove("completion_payload")
                }

                if (-not $schemaValid) {
                    $errorPreview = if (@($schemaErrors).Count -gt 0) { [string]$schemaErrors[0] } else { "output-schema-invalid" }
                    Set-V2DynamicProperty -InputObject $task -Name "status" -Value "needs-revision"
                    Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ("output-schema-invalid:" + $errorPreview)
                    Set-V2DynamicProperty -InputObject $task -Name "last_error" -Value ("output-schema-invalid:" + ($schemaErrors -join ","))
                    $statusAfter = "needs-revision"
                    $completionAccepted = $false
                }
                else {
                    Set-V2DynamicProperty -InputObject $task -Name "status" -Value "done"
                    Set-V2DynamicProperty -InputObject $task -Name "blocked_reason" -Value ""
                    Set-V2DynamicProperty -InputObject $task -Name "last_error" -Value ""
                    Set-V2DynamicProperty -InputObject $task -Name "completed_at" -Value $timestamp
                    if (@($payloadChanges).Count -gt 0) {
                        Set-V2DynamicProperty -InputObject $task -Name "artifacts" -Value @($payloadChanges)
                    }
                    if (-not [string]::IsNullOrWhiteSpace($payloadSummary)) {
                        Set-V2DynamicProperty -InputObject $task -Name "completion_note" -Value $payloadSummary
                    }
                    $statusAfter = "done"
                    $completionAccepted = $true
                }

                $found = $true
                $completedTask = $task
                break
            }
            if (-not $found) { throw "Task '$TaskId' not found in task-dag.json." }

            Save-V2JsonContent -Path $dagPath -Value $dag
            Invoke-V2TaskStateSync -ProjectRoot $completeProjectPath -FailOnError
        }
        catch {
            if (-not [string]::IsNullOrWhiteSpace($dagBackupRaw)) {
                [System.IO.File]::WriteAllText($dagPath, $dagBackupRaw)
            }
            throw
        }
        finally {
            if ($hasDagMutex) {
                $dagMutex.ReleaseMutex()
            }
            $dagMutex.Dispose()
        }

        # Ensure locks for this task are always released on completion/needs-revision.
        if (Test-Path -LiteralPath $locksPath -PathType Leaf) {
            try {
                [void](Remove-V2TaskLocks `
                    -LocksPath $locksPath `
                    -TaskId $TaskId `
                    -Reason ("status-" + $statusAfter))
            }
            catch {
                Write-Warning ("Could not release locks for task '{0}': {1}" -f $TaskId, $_.Exception.Message)
            }
        }

        $completionReason = if ($completionAccepted) { "completion-accepted" } else { "output-schema-invalid" }
        Write-V2TaskTransactionEvent `
            -ProjectRoot $completeProjectPath `
            -Operation "complete" `
            -TaskId $TaskId `
            -AgentName $AgentName `
            -StatusFrom $statusBefore `
            -StatusTo $statusAfter `
            -Success $completionAccepted `
            -Reason $completionReason `
            -EvidencePath $completionPayloadPath `
            -TransactionId $completeTransactionId

        if ($completionAccepted) {
            [void](Invoke-V2MemoryModuleIndex `
                -ProjectRoot $completeProjectPath `
                -TaskId $TaskId `
                -AgentName $AgentName `
                -SourceFiles @($payloadSourceFiles) `
                -SourceModules @($payloadSourceModules) `
                -Outcome "done")
        }
        else {
            [void](Invoke-V2MemoryModuleIndex `
                -ProjectRoot $completeProjectPath `
                -TaskId $TaskId `
                -AgentName $AgentName `
                -SourceFiles @($payloadSourceFiles) `
                -SourceModules @($payloadSourceModules) `
                -Outcome "needs-revision")
        }

        # ── Pattern library: save solution for REPAIR tasks so future projects benefit ─────
        $isRepair = ($TaskId -like "REPAIR-*") -and $completionAccepted
        if ($isRepair -and -not [string]::IsNullOrWhiteSpace($payloadSummary)) {
            try {
                Initialize-V2Directory -Path $patternDir
                $patternSlug    = ($TaskId -replace "[^a-z0-9]+", "-").ToLowerInvariant()
                $patternFile    = Join-Path $patternDir "$patternSlug.md"
                $taskTitle      = [string](Get-V2OptionalProperty -InputObject $completedTask -Name "title"          -DefaultValue $TaskId)
                $taskReason     = [string](Get-V2OptionalProperty -InputObject $completedTask -Name "reason"         -DefaultValue "")
                $taskAgent      = [string](Get-V2OptionalProperty -InputObject $completedTask -Name "assigned_agent" -DefaultValue "")
                $patternLines = [System.Collections.Generic.List[string]]::new()
                $patternLines.Add("# Pattern: $taskTitle")
                $patternLines.Add("")
                $patternLines.Add("**Source task:** $TaskId")
                $patternLines.Add("**Resolved by:** $taskAgent")
                $patternLines.Add("**Recorded at:** $(Get-V2Timestamp)")
                $patternLines.Add("")
                $patternLines.Add("## Problem")
                $patternLines.Add($taskReason)
                $patternLines.Add("")
                $patternLines.Add("## Solution")
                $patternLines.Add($payloadSummary)
                if (@($payloadChanges).Count -gt 0) {
                    $patternLines.Add("")
                    $patternLines.Add("## Artifacts")
                    foreach ($change in $payloadChanges) {
                        $patternLines.Add("- $change")
                    }
                }
                $patternContent = $patternLines -join [Environment]::NewLine
                [System.IO.File]::WriteAllText($patternFile, $patternContent, [System.Text.Encoding]::UTF8)
                Write-Output "PATTERN SAVED: $patternFile"
            }
            catch {
                Write-Warning "Could not save pattern for '$TaskId': $($_.Exception.Message)"
            }
        }

        if (-not $completionAccepted) {
            Write-Output "COMPLETE REJECTED: Task '$TaskId' moved to needs-revision."
            Write-Output "COMPLETION PAYLOAD: $completionPayloadPath"
            Write-Output ($completedTask | ConvertTo-Json -Depth 5)
            throw ("complete-rejected:output-schema-invalid:{0}" -f ($schemaErrors -join ","))
        }

        Write-Output "COMPLETE OK: Task '$TaskId' marked done."
        Write-Output "COMPLETION PAYLOAD: $completionPayloadPath"
        Write-Output ($completedTask | ConvertTo-Json -Depth 5)
    }

    default {
        throw "Unsupported mode: $Mode"
    }
}
