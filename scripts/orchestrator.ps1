<#
.SYNOPSIS
    V1 orchestrator - interactive project intake and managed workspace launcher.
.DESCRIPTION
    Original V1 entry point for submitting, watching, and initializing projects.
    Runs Invoke-ProjectIntake.ps1, generates Docker scaffolds, world models,
    and writes PROJECT_STATE.json + CLAUDE_SUBMISSION.md for each project.

    For the autonomous V2 loop (observe + schedule continuously), use the root
    orchestrator.ps1 with -Action v2-loop instead.
.PARAMETER Mode
    submit  - classify an existing project and write state files
    watch   - poll the inbox directory and auto-submit new arrivals
    new     - scaffold a new greenfield project workspace
.PARAMETER ProjectPath
    Path to the project to submit (required for Mode=submit).
.PARAMETER ProjectName
    Name for the new project (required for Mode=new).
.EXAMPLE
    .\scripts\orchestrator.ps1 -Mode submit -ProjectPath C:\projects\myapp
    .\scripts\orchestrator.ps1 -Mode submit -ProjectPath C:\projects\myapp -GenerateDocker
    .\scripts\orchestrator.ps1 -Mode new    -ProjectName "my-new-app"
    .\scripts\orchestrator.ps1 -Mode watch
#>
param(
    [ValidateSet("submit", "watch", "new")]
    [string]$Mode = "submit",
    [string]$ProjectPath,
    [string]$ProjectName,
    [string]$ProjectBriefPath,
    [string]$InboxPath = ".\workspace\incoming",
    [string]$ManagedProjectsRoot = ".\workspace\projects",
    [string]$StateRoot = ".\workspace\state",
    [string]$RegistryPath = ".\workspace\PROJECT_REGISTRY.json",
    [int]$PollIntervalSeconds = 10,
    [switch]$CopyIntoManagedRoot,
    [switch]$GenerateDocker,
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "auto",
    [ValidateSet("auto", "postgres", "mysql", "mongodb", "none")]
    [string]$Database = "auto",
    [ValidateSet("unknown", "stabilize-only", "targeted-refactor", "full-modernization")]
    [string]$LegacyPolicy = "unknown",
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ProjectSlug {
    param([string]$Name)

    $slug = $Name.ToLowerInvariant() -replace "[^a-z0-9]+", "-"
    $slug = $slug.Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) {
        return "project"
    }

    return $slug
}

function Resolve-AbsolutePath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    if (Test-Path -LiteralPath $Path) {
        return (Resolve-Path -LiteralPath $Path).Path
    }

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }

    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Path))
}

function Ensure-Directory {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Assert-SafeToDestroy {
    <#
    .SYNOPSIS
        Guards Remove-Item -Recurse -Force by verifying no active agent locks or
        in-progress tasks exist inside the target project directory.
        Throws a descriptive error if the directory is "hot".
    #>
    param([string]$ProjectDirectory)

    if (-not (Test-Path -LiteralPath $ProjectDirectory -PathType Container)) {
        return  # Nothing to check - directory doesn't exist yet
    }

    $reasons = New-Object System.Collections.Generic.List[string]

    # --- Check ai-orchestrator/locks.json for active locks ---
    $locksPath = Join-Path $ProjectDirectory "ai-orchestrator/locks.json"
    if (Test-Path -LiteralPath $locksPath -PathType Leaf) {
        try {
            $raw     = Get-Content -LiteralPath $locksPath -Raw -ErrorAction Stop
            $locksDoc = $raw | ConvertFrom-Json -ErrorAction Stop
            $locks = if ($locksDoc.PSObject.Properties.Name -contains "locks") { @($locksDoc.locks) } else { @() }
            $activeLocks = @($locks | Where-Object {
                $status = if ($_.PSObject.Properties.Name -contains "status") { [string]$_.status } else { "" }
                $status -in @("active", "held", "open")
            })
            if ($activeLocks.Count -gt 0) {
                $agentNames = ($activeLocks | ForEach-Object {
                    if ($_.PSObject.Properties.Name -contains "agent") { [string]$_.agent } else { "unknown" }
                }) -join ", "
                $reasons.Add("Active agent lock(s) found in locks.json: [$agentNames]")
            }
        }
        catch {
            # Non-fatal: unreadable locks.json is not a blocker
        }
    }

    # --- Check ai-orchestrator/tasks/task-dag.json for in-progress tasks ---
    $dagPath = Join-Path $ProjectDirectory "ai-orchestrator/tasks/task-dag.json"
    if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
        try {
            $raw    = Get-Content -LiteralPath $dagPath -Raw -ErrorAction Stop
            $dagDoc = $raw | ConvertFrom-Json -ErrorAction Stop
            $tasks  = if ($dagDoc.PSObject.Properties.Name -contains "tasks") { @($dagDoc.tasks) } else { @() }
            $hotTasks = @($tasks | Where-Object {
                $status = if ($_.PSObject.Properties.Name -contains "status") { [string]$_.status } else { "" }
                $status -eq "in-progress"
            })
            if ($hotTasks.Count -gt 0) {
                $taskIds = ($hotTasks | ForEach-Object {
                    if ($_.PSObject.Properties.Name -contains "id") { [string]$_.id } else { "?" }
                }) -join ", "
                $reasons.Add("In-progress tasks in task-dag.json: [$taskIds]")
            }
        }
        catch {
            # Non-fatal
        }
    }

    if ($reasons.Count -gt 0) {
        $detail = $reasons -join "; "
        throw "SAFETY ABORT - cannot destroy '$ProjectDirectory' while it is active. $detail. Stop all agents and re-run with -Force after confirming the project is idle."
    }
}

function Get-JsonContent {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Save-JsonContent {
    param(
        [string]$Path,
        [object]$Value
    )

    $directory = Split-Path -Parent $Path
    if ($directory) {
        Ensure-Directory -Path $directory
    }

    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12))
}

function Get-StateStatus {
    param(
        [string]$ModeName,
        [string]$LegacyPolicyName,
        [bool]$HasProjectBrief = $false
    )

    switch ($ModeName) {
        "greenfield" {
            if ($HasProjectBrief) {
                return "ready-for-brief-review"
            }

            return "awaiting-brief"
        }
        "existing" { return "ready-for-deep-analysis" }
        "legacy" {
            if ($LegacyPolicyName -eq "unknown") {
                return "awaiting-refactor-policy"
            }

            return "ready-for-deep-analysis"
        }
        default { return "unknown" }
    }
}

function New-NextActionsMarkdown {
    param(
        [object]$Intake,
        [string]$LegacyPolicyName,
        [bool]$DockerRequested,
        [string]$DockerStatus,
        [string]$WorldModelStatus = "not-generated",
        [int]$WorldModelEntityCount = 0
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Next Actions")
    $lines.Add("")
    $lines.Add("- Mode: $($Intake.Classification.Mode)")
    $lines.Add("- Confidence: $($Intake.Classification.Confidence)")
    $lines.Add("- Docker Status: $DockerStatus")
    $lines.Add("- World Model Status: $WorldModelStatus")
    $lines.Add("")

    switch ($Intake.Classification.Mode) {
        "greenfield" {
            $lines.Add("## Required")
            $lines.Add("1. Answer the product/runtime questions from the intake report.")
            $lines.Add("2. Confirm the target language/framework and database.")
            $lines.Add("3. Approve Docker from day one or skip it explicitly.")
        }
        "existing" {
            $lines.Add("## Required")
            $lines.Add("1. Verify the real install/start/test commands.")
            $lines.Add("2. Build module context files and architecture map.")
            $lines.Add("3. Break the project into tasks after the deep audit.")
        }
        "legacy" {
            $lines.Add("## Required")
            if ($LegacyPolicyName -eq "unknown") {
                $lines.Add("1. Choose the refactor policy: stabilize-only, targeted-refactor, or full-modernization.")
                $lines.Add("2. Do not modernize architecture before that choice is explicit.")
            }
            else {
                $lines.Add("1. Respect the selected legacy policy: $LegacyPolicyName.")
            }
            $lines.Add("3. Verify boot/test commands and isolate the highest-risk modules first.")
        }
    }

    if (@($Intake.Questions).Count -gt 0) {
        $lines.Add("")
        $lines.Add("## Open Questions")
        foreach ($question in $Intake.Questions) {
            $lines.Add("- $question")
        }
    }

    if ($DockerRequested -and $DockerStatus -like "generated*") {
        $lines.Add("")
        $lines.Add("## Docker Review")
        $lines.Add("- Review the generated compose and Dockerfile before replacing anything in the target project.")
    }

    if ($WorldModelEntityCount -gt 0) {
        $lines.Add("")
        $lines.Add("## World Model Review")
        $lines.Add("- Review the generated world model draft before using it as architectural truth.")
        $lines.Add("- Confirm detected entities and relationships against the real business domain.")
    }

    return $lines -join [Environment]::NewLine
}

function New-ClaudeSubmissionMarkdown {
    param(
        [string]$SourcePath,
        [string]$WorkingPath,
        [string]$StatePath,
        [object]$Intake,
        [string]$LegacyPolicyName,
        [string]$WorldModelPath = "",
        [string]$WorldModelStatus = "not-generated"
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Claude Code Submission Package")
    $lines.Add("")
    $lines.Add("Use this package when handing the project to Claude Code.")
    $lines.Add("")
    $lines.Add("## Project")
    $lines.Add("- Source Path: $SourcePath")
    $lines.Add("- Working Path: $WorkingPath")
    $lines.Add("- State Path: $StatePath")
    $lines.Add("- Mode: $($Intake.Classification.Mode)")
    $lines.Add("- Confidence: $($Intake.Classification.Confidence)")
    $lines.Add("- Legacy Policy: $LegacyPolicyName")
    $lines.Add("")
    $lines.Add("## Load First")
    $lines.Add("- docs/prompts/claude_code_universal_orchestrator.md")
    $lines.Add("- $(Join-Path $StatePath 'INTAKE_REPORT.md')")
    $lines.Add("- docs/agents/PROJECT_INTAKE.md")
    $lines.Add("- docs/agents/WORK_PROTOCOL.md")
    $lines.Add("- docs/agents/PROJECT_BRAIN.md")
    if (-not [string]::IsNullOrWhiteSpace($WorldModelPath) -and $WorldModelStatus -like "generated*") {
        $lines.Add("- $WorldModelPath")
    }
    $lines.Add("")
    $lines.Add("## Initial Objective")
    switch ($Intake.Classification.Mode) {
        "greenfield" {
            $lines.Add("Ask only the minimum design questions required to scaffold the project safely.")
        }
        "existing" {
            $lines.Add("Run a deep analysis and produce verified runtime, architecture, data, and delivery facts.")
        }
        "legacy" {
            if ($LegacyPolicyName -eq "unknown") {
                $lines.Add("Audit only. Do not refactor architecture until the legacy policy is chosen.")
            }
            else {
                $lines.Add("Audit and continue within the allowed legacy policy: $LegacyPolicyName.")
            }
        }
    }
    $lines.Add("")
    $lines.Add("## Generated Analysis Artifacts")
    $lines.Add("- World Model: $WorldModelStatus")
    $lines.Add("")
    $lines.Add("## Open Questions")
    if (@($Intake.Questions).Count -eq 0) {
        $lines.Add("- none")
    }
    else {
        foreach ($question in $Intake.Questions) {
            $lines.Add("- $question")
        }
    }

    return $lines -join [Environment]::NewLine
}

function Update-ProjectRegistry {
    param(
        [string]$Path,
        [object]$ProjectRecord
    )

    $existing = Get-JsonContent -Path $Path
    $items = @()
    if ($existing) {
        if ($existing.PSObject.Properties.Name -contains "Projects") {
            $items = @($existing.Projects)
        }
        else {
            $items = @($existing)
        }
    }

    $filtered = @($items | Where-Object { $_.Slug -ne $ProjectRecord.Slug })
    $updated = [PSCustomObject]@{
        Projects = @($filtered + $ProjectRecord)
    }
    Save-JsonContent -Path $Path -Value $updated
}

function Initialize-NewProjectWorkspace {
    param(
        [string]$Name,
        [string]$ManagedRootPath,
        [string]$BriefSourcePath,
        [switch]$ShouldForce
    )

    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "ProjectName is required when Mode=new."
    }

    Ensure-Directory -Path $ManagedRootPath

    $projectSlug = Get-ProjectSlug -Name $Name
    $projectPath = Join-Path $ManagedRootPath $projectSlug
    if ((Test-Path -LiteralPath $projectPath) -and -not $ShouldForce) {
        throw "Project path already exists: $projectPath. Use -Force to replace it."
    }

    if (Test-Path -LiteralPath $projectPath) {
        Assert-SafeToDestroy -ProjectDirectory $projectPath
        Remove-Item -LiteralPath $projectPath -Recurse -Force
    }

    Ensure-Directory -Path $projectPath

    $requestPath = Join-Path $projectPath "PROJECT_REQUEST.md"
    if (-not [string]::IsNullOrWhiteSpace($BriefSourcePath)) {
        $resolvedBriefPath = Resolve-AbsolutePath -Path $BriefSourcePath
        if (-not (Test-Path -LiteralPath $resolvedBriefPath -PathType Leaf)) {
            throw "ProjectBriefPath does not exist: $BriefSourcePath"
        }

        Copy-Item -LiteralPath $resolvedBriefPath -Destination $requestPath -Force
    }
    else {
        $template = @"
# Project Request

## Name
$Name

## Product Goal
[Describe what should be built.]

## Delivery Surface
[web | api | mobile | desktop | cli | mixed]

## Preferred Stack
[language/framework or "unknown"]

## Preferred Database
[postgres | mysql | mongodb | none | unknown]

## Docker Required
[yes | no | unknown]

## Deployment Target
[cloud, on-prem, desktop, unknown]

## Constraints
- [constraint]

## Notes
- [note]
"@
        [System.IO.File]::WriteAllText($requestPath, $template)
    }

    return [PSCustomObject]@{
        ProjectPath = $projectPath
        HasBrief    = $true
    }
}

function Invoke-ManagedProjectSubmission {
    param(
        [string]$SubmissionPath,
        [string]$ManagedRootPath,
        [string]$StateRootPath,
        [string]$ProjectRegistryPath,
        [switch]$ShouldCopyIntoManagedRoot,
        [switch]$ShouldGenerateDocker,
        [string]$StackName,
        [string]$DatabaseName,
        [string]$LegacyPolicyName,
        [bool]$HasProjectBrief = $false,
        [switch]$ShouldIncludeNeo4j,
        [switch]$ShouldIncludeQdrant,
        [switch]$ShouldForce
    )

    $resolvedSourcePath = Resolve-AbsolutePath -Path $SubmissionPath
    if (-not $resolvedSourcePath -or -not (Test-Path -LiteralPath $resolvedSourcePath -PathType Container)) {
        throw "Project path does not exist or is not a directory: $SubmissionPath"
    }

    Ensure-Directory -Path $ManagedRootPath
    Ensure-Directory -Path $StateRootPath

    $projectName = Split-Path -Leaf $resolvedSourcePath
    $projectSlug = Get-ProjectSlug -Name $projectName
    $managedProjectPath = Join-Path $ManagedRootPath $projectSlug
    $workingPath = $resolvedSourcePath

    if ($ShouldCopyIntoManagedRoot) {
        if ((Test-Path -LiteralPath $managedProjectPath) -and -not $ShouldForce) {
            throw "Managed project path already exists: $managedProjectPath. Use -Force to replace it."
        }

        if (Test-Path -LiteralPath $managedProjectPath) {
            Assert-SafeToDestroy -ProjectDirectory $managedProjectPath
            Remove-Item -LiteralPath $managedProjectPath -Recurse -Force
        }

        Copy-Item -LiteralPath $resolvedSourcePath -Destination $managedProjectPath -Recurse -Force
        $workingPath = $managedProjectPath
    }

    $statePath = Join-Path $StateRootPath $projectSlug
    Ensure-Directory -Path $statePath

    $intakeScriptPath = Join-Path $PSScriptRoot "Invoke-ProjectIntake.ps1"
    $dockerScriptPath = Join-Path $PSScriptRoot "New-DockerFactory.ps1"
    $worldModelScriptPath = Join-Path $PSScriptRoot "extract_world_model.py"
    $intakeOutputPath = Join-Path $statePath "INTAKE_REPORT.md"
    $worldModelOutputPath = Join-Path $statePath "WORLD_MODEL_AUTO.md"
    $worldModelJsonPath = Join-Path $statePath "WORLD_MODEL_AUTO.json"

    $intakeJsonText = & $intakeScriptPath -ProjectPath $workingPath -OutputPath $intakeOutputPath -EmitJson
    $intake = ($intakeJsonText | Out-String) | ConvertFrom-Json

    $worldModelStatus = "not-generated"
    $worldModelEntityCount = 0
    $worldModelRelationshipCount = 0
    try {
        $worldModelJsonText = python $worldModelScriptPath `
            --project-path $workingPath `
            --project-slug $projectSlug `
            --output-path $worldModelOutputPath `
            --json-output-path $worldModelJsonPath
        $worldModel = ($worldModelJsonText | Out-String) | ConvertFrom-Json
        $worldModelEntityCount = [int]$worldModel.summary.entity_count
        $worldModelRelationshipCount = [int]$worldModel.summary.relationship_count
        $worldModelStatus = "generated"
    }
    catch {
        $worldModelStatus = "skipped: $($_.Exception.Message)"
    }

    $dockerStatus = "not-requested"
    if ($ShouldGenerateDocker) {
        $dockerOutputDirectory = Join-Path $statePath "docker"
        try {
            $dockerParams = @{
                ProjectPath      = $workingPath
                OutputDirectory  = $dockerOutputDirectory
                Stack            = $StackName
                Database         = $DatabaseName
                Force            = $true
            }

            if ($ShouldIncludeNeo4j) {
                $dockerParams.IncludeNeo4j = $true
            }
            if ($ShouldIncludeQdrant) {
                $dockerParams.IncludeQdrant = $true
            }

            $dockerOutput = & $dockerScriptPath @dockerParams | Out-String
            $dockerStatus = ($dockerOutput.Trim())
        }
        catch {
            $dockerStatus = "skipped: $($_.Exception.Message)"
        }
    }

    $stateStatus = Get-StateStatus -ModeName $intake.Classification.Mode -LegacyPolicyName $LegacyPolicyName -HasProjectBrief $HasProjectBrief

    $projectState = [PSCustomObject]@{
        Slug               = $projectSlug
        Name               = $projectName
        SourcePath         = $resolvedSourcePath
        WorkingPath        = $workingPath
        StatePath          = $statePath
        Mode               = $intake.Classification.Mode
        Confidence         = $intake.Classification.Confidence
        LegacyPolicy       = $LegacyPolicyName
        Status             = $stateStatus
        Stack              = $intake.PrimaryStack.Name
        Frameworks         = @($intake.PrimaryStack.Frameworks)
        Database           = $intake.Database.Engine
        DockerStatus       = $dockerStatus
        IncludeNeo4j       = [bool]$ShouldIncludeNeo4j
        IncludeQdrant      = [bool]$ShouldIncludeQdrant
        CopiedToManagedRoot = [bool]$ShouldCopyIntoManagedRoot
        WorldModelStatus   = $worldModelStatus
        WorldModelPath     = $worldModelOutputPath
        WorldModelEntityCount = $worldModelEntityCount
        WorldModelRelationshipCount = $worldModelRelationshipCount
        UpdatedAt          = (Get-Date).ToString("s")
        OpenQuestions      = @($intake.Questions)
    }

    Save-JsonContent -Path (Join-Path $statePath "PROJECT_STATE.json") -Value $projectState
    [System.IO.File]::WriteAllText(
        (Join-Path $statePath "NEXT_ACTION.md"),
        (New-NextActionsMarkdown -Intake $intake -LegacyPolicyName $LegacyPolicyName -DockerRequested ([bool]$ShouldGenerateDocker) -DockerStatus $dockerStatus -WorldModelStatus $worldModelStatus -WorldModelEntityCount $worldModelEntityCount)
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $statePath "CLAUDE_SUBMISSION.md"),
        (New-ClaudeSubmissionMarkdown -SourcePath $resolvedSourcePath -WorkingPath $workingPath -StatePath $statePath -Intake $intake -LegacyPolicyName $LegacyPolicyName -WorldModelPath $worldModelOutputPath -WorldModelStatus $worldModelStatus)
    )

    Update-ProjectRegistry -Path $ProjectRegistryPath -ProjectRecord $projectState

    return $projectState
}

function Start-InboxWatchLoop {
    param(
        [string]$InboxRootPath,
        [string]$ManagedRootPath,
        [string]$StateRootPath,
        [string]$ProjectRegistryPath,
        [int]$IntervalSeconds,
        [switch]$ShouldCopyIntoManagedRoot,
        [switch]$ShouldGenerateDocker,
        [string]$StackName,
        [string]$DatabaseName,
        [string]$LegacyPolicyName,
        [switch]$ShouldIncludeNeo4j,
        [switch]$ShouldIncludeQdrant,
        [switch]$ShouldForce
    )

    Ensure-Directory -Path $InboxRootPath
    Ensure-Directory -Path $ManagedRootPath
    Ensure-Directory -Path $StateRootPath

    Write-Output "Watching inbox: $InboxRootPath"
    while ($true) {
        $directories = Get-ChildItem -LiteralPath $InboxRootPath -Directory -Force | Sort-Object Name
        foreach ($directory in $directories) {
            $slug = Get-ProjectSlug -Name $directory.Name
            $statePath = Join-Path $StateRootPath $slug
            $projectStatePath = Join-Path $statePath "PROJECT_STATE.json"
            if (Test-Path -LiteralPath $projectStatePath) {
                continue
            }

            try {
                $state = Invoke-ManagedProjectSubmission `
                    -SubmissionPath $directory.FullName `
                    -ManagedRootPath $ManagedRootPath `
                    -StateRootPath $StateRootPath `
                    -ProjectRegistryPath $ProjectRegistryPath `
                    -ShouldCopyIntoManagedRoot:$ShouldCopyIntoManagedRoot `
                    -ShouldGenerateDocker:$ShouldGenerateDocker `
                    -StackName $StackName `
                    -DatabaseName $DatabaseName `
                    -LegacyPolicyName $LegacyPolicyName `
                    -ShouldIncludeNeo4j:$ShouldIncludeNeo4j `
                    -ShouldIncludeQdrant:$ShouldIncludeQdrant `
                    -ShouldForce:$ShouldForce

                Write-Output "Submitted project '$($state.Slug)' with status '$($state.Status)'."
            }
            catch {
                Write-Warning "Failed to submit '$($directory.FullName)': $($_.Exception.Message)"
            }
        }

        Start-Sleep -Seconds $IntervalSeconds
    }
}

$resolvedManagedProjectsRoot = Resolve-AbsolutePath -Path $ManagedProjectsRoot
$resolvedStateRoot = Resolve-AbsolutePath -Path $StateRoot
$resolvedRegistryPath = Resolve-AbsolutePath -Path $RegistryPath
$resolvedInboxPath = Resolve-AbsolutePath -Path $InboxPath

switch ($Mode) {
    "submit" {
        if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
            throw "ProjectPath is required when Mode=submit."
        }

        $state = Invoke-ManagedProjectSubmission `
            -SubmissionPath $ProjectPath `
            -ManagedRootPath $resolvedManagedProjectsRoot `
            -StateRootPath $resolvedStateRoot `
            -ProjectRegistryPath $resolvedRegistryPath `
            -ShouldCopyIntoManagedRoot:$CopyIntoManagedRoot `
            -ShouldGenerateDocker:$GenerateDocker `
            -StackName $Stack `
            -DatabaseName $Database `
            -LegacyPolicyName $LegacyPolicy `
            -HasProjectBrief:$false `
            -ShouldIncludeNeo4j:$IncludeNeo4j `
            -ShouldIncludeQdrant:$IncludeQdrant `
            -ShouldForce:$Force

        Write-Output "Project submitted: $($state.Slug)"
        Write-Output "State path: $($state.StatePath)"
        Write-Output "Status: $($state.Status)"
    }
    "new" {
        $initialized = Initialize-NewProjectWorkspace `
            -Name $ProjectName `
            -ManagedRootPath $resolvedManagedProjectsRoot `
            -BriefSourcePath $ProjectBriefPath `
            -ShouldForce:$Force

        $state = Invoke-ManagedProjectSubmission `
            -SubmissionPath $initialized.ProjectPath `
            -ManagedRootPath $resolvedManagedProjectsRoot `
            -StateRootPath $resolvedStateRoot `
            -ProjectRegistryPath $resolvedRegistryPath `
            -ShouldCopyIntoManagedRoot:$false `
            -ShouldGenerateDocker:$GenerateDocker `
            -StackName $Stack `
            -DatabaseName $Database `
            -LegacyPolicyName "n/a" `
            -HasProjectBrief:$initialized.HasBrief `
            -ShouldIncludeNeo4j:$IncludeNeo4j `
            -ShouldIncludeQdrant:$IncludeQdrant `
            -ShouldForce:$Force

        Write-Output "New project initialized: $($state.Slug)"
        Write-Output "Working path: $($state.WorkingPath)"
        Write-Output "State path: $($state.StatePath)"
        Write-Output "Status: $($state.Status)"
    }
    "watch" {
        Start-InboxWatchLoop `
            -InboxRootPath $resolvedInboxPath `
            -ManagedRootPath $resolvedManagedProjectsRoot `
            -StateRootPath $resolvedStateRoot `
            -ProjectRegistryPath $resolvedRegistryPath `
            -IntervalSeconds $PollIntervalSeconds `
            -ShouldCopyIntoManagedRoot:$CopyIntoManagedRoot `
            -ShouldGenerateDocker:$GenerateDocker `
            -StackName $Stack `
            -DatabaseName $Database `
            -LegacyPolicyName $LegacyPolicy `
            -ShouldIncludeNeo4j:$IncludeNeo4j `
            -ShouldIncludeQdrant:$IncludeQdrant `
            -ShouldForce:$Force
    }
}
