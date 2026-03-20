<#
.SYNOPSIS
    Generates mandatory pre-flight reasoning artifacts per task.
.DESCRIPTION
    Reads one task from task-dag.json and writes a deterministic preflight payload
    to ai-orchestrator/tasks/preflight. Used by Run-AgentLoop before task execution.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/.
.PARAMETER TaskId
    Task identifier to generate reasoning for.
.PARAMETER AgentName
    Agent assigned to execute this task.
.PARAMETER OutputPath
    Optional custom output path. If omitted, defaults to:
    ai-orchestrator/tasks/preflight/<task-id>-<agent>.json
.PARAMETER EmitJson
    Emits a JSON result payload.
#>
param(
    [string]$ProjectPath = ".",
    [string]$TaskId = "",
    [string]$AgentName = "Codex",
    [string]$OutputPath = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2TaskArray {
    param([object]$Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [string]) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
        return @($Value)
    }
    return @($Value)
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

function Get-V2DefaultPreflightPath {
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName
    )

    $safeTaskId = ($TaskId -replace "[^A-Za-z0-9_-]+", "_")
    $safeAgent = if ([string]::IsNullOrWhiteSpace($AgentName)) {
        "unassigned"
    }
    else {
        ($AgentName -replace "[^A-Za-z0-9_-]+", "_")
    }
    $dir = Join-Path $ProjectRoot "ai-orchestrator/tasks/preflight"
    Initialize-V2Directory -Path $dir
    return (Join-Path $dir ("{0}-{1}.json" -f $safeTaskId, $safeAgent))
}

function Get-V2TaskRiskHints {
    param(
        [string]$TaskId,
        [string]$ExecutionMode,
        [string[]]$FilesAffected
    )

    $hints = New-Object System.Collections.Generic.List[string]

    if ($ExecutionMode -in @("release", "deploy", "project-completion-gate")) {
        $hints.Add("Alteracao com impacto de release/deploy exige validacao forte.")
    }
    if ($TaskId -match "MIGRATION|DB|DATABASE") {
        $hints.Add("Mudanca de schema pode causar regressao de dados.")
    }

    $sensitivePatterns = @("auth", "login", "security", "payment", "finance", "medical", "record", "patient", "lgpd", "privacy")
    foreach ($path in @($FilesAffected)) {
        $normalized = [string]$path
        if ([string]::IsNullOrWhiteSpace($normalized)) { continue }
        foreach ($pattern in $sensitivePatterns) {
            if ($normalized.ToLowerInvariant().Contains($pattern)) {
                $hints.Add("Arquivo sensivel detectado: $normalized")
                break
            }
        }
    }

    if ($hints.Count -eq 0) {
        $hints.Add("Risco padrao: validar regressao funcional e tecnica.")
    }

    return @($hints.ToArray() | Select-Object -Unique)
}

function Get-V2LibraryFirstPolicy {
    param([string]$ProjectRoot)

    $defaultPolicy = [PSCustomObject]@{
        schema_version    = "v1"
        enabled           = $true
        priority          = "P0"
        local_only        = $true
        required_in_tasks = @("pending", "in-progress")
        decision_options  = @("use-existing-library", "hybrid", "custom-code-justified", "not-applicable")
    }

    $policyPath = Join-Path $ProjectRoot "ai-orchestrator/config/library-first-policy.json"
    if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
        return $defaultPolicy
    }

    $loaded = Get-V2JsonContent -Path $policyPath
    if (-not $loaded) {
        return $defaultPolicy
    }

    return [PSCustomObject]@{
        schema_version    = [string](Get-V2OptionalProperty -InputObject $loaded -Name "schema_version" -DefaultValue "v1")
        enabled           = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "enabled" -DefaultValue $true)
        priority          = [string](Get-V2OptionalProperty -InputObject $loaded -Name "priority" -DefaultValue "P0")
        local_only        = [bool](Get-V2OptionalProperty -InputObject $loaded -Name "local_only" -DefaultValue $true)
        required_in_tasks = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $loaded -Name "required_in_tasks" -DefaultValue @("pending", "in-progress")))
        decision_options  = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $loaded -Name "decision_options" -DefaultValue @("use-existing-library", "hybrid", "custom-code-justified", "not-applicable")))
    }
}

function Get-V2DependencyEntriesFromMap {
    param(
        [object]$Map,
        [string]$Ecosystem,
        [string]$Scope,
        [string]$SourceFile
    )

    $entries = New-Object System.Collections.Generic.List[object]
    if ($null -eq $Map) {
        return @()
    }

    $properties = @()
    try {
        $properties = @($Map.PSObject.Properties)
    }
    catch {
        $properties = @()
    }

    foreach ($prop in @($properties)) {
        $name = [string]$prop.Name
        $version = [string]$prop.Value
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        if ($Ecosystem -eq "composer") {
            if ($name -eq "php" -or $name.StartsWith("ext-")) { continue }
        }

        $entries.Add([PSCustomObject]@{
                package     = $name
                version     = if ([string]::IsNullOrWhiteSpace($version)) { "*" } else { $version }
                ecosystem   = $Ecosystem
                scope       = $Scope
                source_file = $SourceFile
            })
    }

    return @($entries.ToArray())
}

function Get-V2LocalLibraryCatalog {
    param([string]$ProjectRoot)

    $catalog = New-Object System.Collections.Generic.List[object]

    $composerPath = Join-Path $ProjectRoot "composer.json"
    if (Test-Path -LiteralPath $composerPath -PathType Leaf) {
        $composer = Get-V2JsonContent -Path $composerPath
        if ($composer) {
            $require = Get-V2OptionalProperty -InputObject $composer -Name "require" -DefaultValue $null
            foreach ($item in @(Get-V2DependencyEntriesFromMap -Map $require -Ecosystem "composer" -Scope "runtime" -SourceFile "composer.json")) {
                $catalog.Add($item)
            }
            $requireDev = Get-V2OptionalProperty -InputObject $composer -Name "require-dev" -DefaultValue $null
            foreach ($item in @(Get-V2DependencyEntriesFromMap -Map $requireDev -Ecosystem "composer" -Scope "dev" -SourceFile "composer.json")) {
                $catalog.Add($item)
            }
        }
    }

    $packagePath = Join-Path $ProjectRoot "package.json"
    if (Test-Path -LiteralPath $packagePath -PathType Leaf) {
        $packageJson = Get-V2JsonContent -Path $packagePath
        if ($packageJson) {
            $deps = Get-V2OptionalProperty -InputObject $packageJson -Name "dependencies" -DefaultValue $null
            foreach ($item in @(Get-V2DependencyEntriesFromMap -Map $deps -Ecosystem "npm" -Scope "runtime" -SourceFile "package.json")) {
                $catalog.Add($item)
            }
            $devDeps = Get-V2OptionalProperty -InputObject $packageJson -Name "devDependencies" -DefaultValue $null
            foreach ($item in @(Get-V2DependencyEntriesFromMap -Map $devDeps -Ecosystem "npm" -Scope "dev" -SourceFile "package.json")) {
                $catalog.Add($item)
            }
        }
    }

    return @($catalog.ToArray())
}

function Get-V2TaskTokens {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return @()
    }

    $tokens = New-Object System.Collections.Generic.List[string]
    $rawTokens = @($Text.ToLowerInvariant() -split "[^a-z0-9]+" | Where-Object { $_.Length -ge 3 })
    foreach ($token in $rawTokens) {
        if (-not $tokens.Contains($token)) {
            $tokens.Add($token)
        }
    }
    return @($tokens.ToArray())
}

function Get-V2TaskLibraryCandidates {
    param(
        [string]$TaskText,
        [string[]]$FilesAffected,
        [object[]]$Catalog,
        [int]$MaxCandidates = 12
    )

    $taskLower = [string]$TaskText
    $taskLower = $taskLower.ToLowerInvariant()
    $tokens = @(Get-V2TaskTokens -Text $taskLower)
    $scored = New-Object System.Collections.Generic.List[object]

    $topicMap = [ordered]@{
        "auth|login|permission|role|token|2fa|security|rbac" = @("sanctum", "passport", "jwt", "permission", "oauth", "auth");
        "report|relatorio|csv|export|pdf|receipt|invoice|finance|financial" = @("dompdf", "excel", "csv", "chart", "pdf", "report");
        "queue|job|worker|reminder|notific|schedule" = @("queue", "horizon", "redis", "amqp", "kafka");
        "integration|webhook|http|calendar|whatsapp|google|sync" = @("guzzle", "http", "sdk", "calendar", "whatsapp", "google");
        "document|upload|file|storage|media" = @("filesystem", "flysystem", "spatie", "storage", "media");
        "test|qa|feature|unit|coverage|mutation" = @("phpunit", "pest", "mockery", "faker", "testing");
    }

    $touchesTests = @($FilesAffected | Where-Object { ([string]$_).ToLowerInvariant().Contains("tests") }).Count -gt 0

    foreach ($lib in @($Catalog)) {
        $name = [string](Get-V2OptionalProperty -InputObject $lib -Name "package" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $nameLower = $name.ToLowerInvariant()
        $score = 0
        $rationale = New-Object System.Collections.Generic.List[string]

        foreach ($token in $tokens) {
            if ($nameLower.Contains($token)) {
                $score += 2
                if (-not $rationale.Contains("task-token-match:$token")) {
                    $rationale.Add("task-token-match:$token")
                }
            }
        }

        foreach ($topicPattern in @($topicMap.Keys)) {
            if ($taskLower -notmatch $topicPattern) { continue }
            foreach ($hint in @($topicMap[$topicPattern])) {
                if ($nameLower.Contains([string]$hint)) {
                    $score += 4
                    $tag = "topic-match:$hint"
                    if (-not $rationale.Contains($tag)) {
                        $rationale.Add($tag)
                    }
                }
            }
        }

        $scope = [string](Get-V2OptionalProperty -InputObject $lib -Name "scope" -DefaultValue "")
        if ($touchesTests -and $scope -eq "dev") {
            $score += 1
            $rationale.Add("dev-scope-useful-for-tests")
        }

        if ($score -le 0) { continue }

        $scored.Add([PSCustomObject]@{
                package     = $name
                version     = [string](Get-V2OptionalProperty -InputObject $lib -Name "version" -DefaultValue "*")
                ecosystem   = [string](Get-V2OptionalProperty -InputObject $lib -Name "ecosystem" -DefaultValue "")
                scope       = $scope
                source_file = [string](Get-V2OptionalProperty -InputObject $lib -Name "source_file" -DefaultValue "")
                score       = $score
                rationale   = @($rationale.ToArray() | Select-Object -Unique)
            })
    }

    $ordered = @($scored | Sort-Object -Property @{ Expression = "score"; Descending = $true }, @{ Expression = "package"; Descending = $false })
    if ($ordered.Count -eq 0) {
        $fallback = New-Object System.Collections.Generic.List[object]
        foreach ($lib in @($Catalog | Select-Object -First ([Math]::Max($MaxCandidates, 1)))) {
            $fallback.Add([PSCustomObject]@{
                    package     = [string](Get-V2OptionalProperty -InputObject $lib -Name "package" -DefaultValue "")
                    version     = [string](Get-V2OptionalProperty -InputObject $lib -Name "version" -DefaultValue "*")
                    ecosystem   = [string](Get-V2OptionalProperty -InputObject $lib -Name "ecosystem" -DefaultValue "")
                    scope       = [string](Get-V2OptionalProperty -InputObject $lib -Name "scope" -DefaultValue "")
                    source_file = [string](Get-V2OptionalProperty -InputObject $lib -Name "source_file" -DefaultValue "")
                    score       = 1
                    rationale   = @("foundation-candidate")
                })
        }
        return @($fallback.ToArray())
    }

    return @($ordered | Select-Object -First $MaxCandidates)
}

function Get-V2BuildVsBuyRecommendation {
    param([object[]]$Candidates)

    if (@($Candidates).Count -eq 0) {
        return [PSCustomObject]@{
            recommended_option = "custom-code-justified"
            confidence         = "medium"
            reason             = "No strong local library candidate detected."
        }
    }

    $top = $Candidates[0]
    $topScore = [int](Get-V2OptionalProperty -InputObject $top -Name "score" -DefaultValue 0)
    $topPackage = [string](Get-V2OptionalProperty -InputObject $top -Name "package" -DefaultValue "")

    if ($topScore -ge 8) {
        return [PSCustomObject]@{
            recommended_option = "use-existing-library"
            confidence         = "high"
            reason             = "Strong local candidate match: $topPackage"
        }
    }
    if ($topScore -ge 4) {
        return [PSCustomObject]@{
            recommended_option = "hybrid"
            confidence         = "medium"
            reason             = "Partial local fit found: $topPackage"
        }
    }

    return [PSCustomObject]@{
        recommended_option = "custom-code-justified"
        confidence         = "medium"
        reason             = "Local candidates are weak for this task."
    }
}

if ([string]::IsNullOrWhiteSpace($TaskId)) {
    throw "TaskId is required."
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$taskDagPath = Join-Path $projectRoot "ai-orchestrator/tasks/task-dag.json"
if (-not (Test-Path -LiteralPath $taskDagPath -PathType Leaf)) {
    throw "task-dag.json not found: $taskDagPath"
}

$dag = Get-V2JsonContent -Path $taskDagPath
$tasks = @(Get-V2OptionalProperty -InputObject $dag -Name "tasks" -DefaultValue @())
$task = @($tasks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $TaskId } | Select-Object -First 1)
if ($task.Count -eq 0) {
    throw "Task not found in DAG: $TaskId"
}
$task = $task[0]

$title = [string](Get-V2OptionalProperty -InputObject $task -Name "title" -DefaultValue "")
$description = [string](Get-V2OptionalProperty -InputObject $task -Name "description" -DefaultValue "")
$executionMode = [string](Get-V2OptionalProperty -InputObject $task -Name "execution_mode" -DefaultValue "")
$dependencies = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $task -Name "dependencies" -DefaultValue @()))
$acceptanceCriteria = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $task -Name "acceptance_criteria" -DefaultValue @()))
$filesAffected = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $task -Name "files_affected" -DefaultValue @()))
$priority = [string](Get-V2OptionalProperty -InputObject $task -Name "priority" -DefaultValue "")

$objective = if (-not [string]::IsNullOrWhiteSpace($description)) { $description } elseif (-not [string]::IsNullOrWhiteSpace($title)) { $title } else { "Execute task $TaskId safely." }
$policy = Get-V2LibraryFirstPolicy -ProjectRoot $projectRoot
$taskContextText = ("{0} {1}" -f $title, $description).Trim()
$catalog = @(Get-V2LocalLibraryCatalog -ProjectRoot $projectRoot)
$libraryCandidates = @(Get-V2TaskLibraryCandidates -TaskText $taskContextText -FilesAffected $filesAffected -Catalog $catalog -MaxCandidates 12)
$buildVsBuy = Get-V2BuildVsBuyRecommendation -Candidates $libraryCandidates

$thought = "Executar $TaskId com mudancas minimas, validacao obrigatoria e decisao build-vs-buy usando bibliotecas locais antes de codigo novo."

$actionPlan = @(
    [PSCustomObject]@{ step = 1; action = "Analisar contexto da tarefa e arquivos afetados"; tool = "read_file" },
    [PSCustomObject]@{ step = 2; action = "Aplicar mudancas minimamente necessarias"; tool = "write_file" },
    [PSCustomObject]@{ step = 3; action = "Executar validacao tecnica e funcional"; tool = "run_validation" }
)

$validationPlan = New-Object System.Collections.Generic.List[string]
if ($acceptanceCriteria.Count -gt 0) {
    foreach ($criterion in $acceptanceCriteria) {
        $entry = [string]$criterion
        if (-not [string]::IsNullOrWhiteSpace($entry)) {
            $validationPlan.Add(("Aceite: {0}" -f $entry))
        }
    }
}
$validationPlan.Add("Confirmar ausencia de erro de execucao no runtime.")
$validationPlan.Add("Confirmar consistencia de estado em task-dag/backlog.")
$validationPlan.Add("Registrar decisao de reutilizacao de biblioteca local (build-vs-buy) no payload de conclusao.")
$validationPlan = @($validationPlan.ToArray() | Select-Object -Unique)

$risks = @(Get-V2TaskRiskHints -TaskId $TaskId -ExecutionMode $executionMode -FilesAffected $filesAffected)
$regressionRisk = if ($risks.Count -gt 0) { $risks[0] } else { "Risco moderado de regressao." }
$requiresHumanApproval = $false
if ($priority -match "P0|critical|urgent" -or $executionMode -in @("release", "deploy", "project-completion-gate")) {
    $requiresHumanApproval = $true
}

$targetOutputPath = if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    Get-V2DefaultPreflightPath -ProjectRoot $projectRoot -TaskId $TaskId -AgentName $AgentName
}
else {
    if ([System.IO.Path]::IsPathRooted($OutputPath)) {
        $OutputPath
    }
    else {
        Join-Path $projectRoot $OutputPath
    }
}

$parent = Split-Path -Parent $targetOutputPath
Initialize-V2Directory -Path $parent

$payload = [PSCustomObject]@{
    schema_version          = "v2-preflight"
    task_id                 = $TaskId
    agent_name              = $AgentName
    generated_at            = Get-V2Timestamp
    objective               = $objective
    thought                 = $thought
    action_plan             = @($actionPlan)
    risks                   = @($risks)
    regression_risk         = $regressionRisk
    validation_plan         = @($validationPlan)
    dependencies_needed     = @($dependencies)
    files_to_change         = @($filesAffected)
    source_files            = @($filesAffected)
    requires_human_approval = $requiresHumanApproval
    library_first_policy    = [PSCustomObject]@{
        enabled      = [bool](Get-V2OptionalProperty -InputObject $policy -Name "enabled" -DefaultValue $true)
        priority     = [string](Get-V2OptionalProperty -InputObject $policy -Name "priority" -DefaultValue "P0")
        local_only   = [bool](Get-V2OptionalProperty -InputObject $policy -Name "local_only" -DefaultValue $true)
        options      = @(Get-V2TaskArray -Value (Get-V2OptionalProperty -InputObject $policy -Name "decision_options" -DefaultValue @("use-existing-library", "hybrid", "custom-code-justified", "not-applicable")))
    }
    local_library_candidates = @($libraryCandidates)
    build_vs_buy_recommendation = $buildVsBuy
    library_decision_required = [bool](Get-V2OptionalProperty -InputObject $policy -Name "enabled" -DefaultValue $true)
}

Save-V2JsonContent -Path $targetOutputPath -Value $payload
$relativePath = ConvertTo-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $targetOutputPath

$result = [PSCustomObject]@{
    success        = $true
    task_id        = $TaskId
    agent_name     = $AgentName
    preflight_path = $relativePath
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
else {
    Write-Output ("Pre-flight generated: {0}" -f $relativePath)
}
