<#
.SYNOPSIS
    Hydrates code graph and vector memory for a project (Neo4j + Qdrant).
.DESCRIPTION
    Runs AST extraction for the project codebase, writes relationship edges, and
    executes memory_sync.py for full vector/graph synchronization.
    Intended as a dense, one-shot hydration step for V5 bootstrap.
#>
param(
    [string]$ProjectPath = ".",
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "auto",
    [string]$PythonExecutable = "python",
    [switch]$SkipNeo4j,
    [switch]$SkipQdrant
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Read-V2EnvMap {
    param([string]$Path)

    $map = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $map
    }
    foreach ($line in Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.TrimStart().StartsWith("#")) { continue }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) { continue }
        $key = [string]$parts[0]
        if ([string]::IsNullOrWhiteSpace($key)) { continue }
        $map[$key.Trim()] = [string]$parts[1]
    }
    return $map
}

function Resolve-V2HydrationStack {
    param(
        [string]$InputStack,
        [object]$ProjectState
    )

    if ($InputStack -and $InputStack -ne "auto") {
        return $InputStack
    }

    $stateStack = [string](Get-V2OptionalProperty -InputObject $ProjectState -Name "stack" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($stateStack) -and $stateStack -ne "unknown" -and $stateStack -ne "auto") {
        return $stateStack
    }

    $fingerprint = Get-V2OptionalProperty -InputObject $ProjectState -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
    $primaryLanguage = [string](Get-V2OptionalProperty -InputObject $fingerprint -Name "primary_language" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($primaryLanguage) -and $primaryLanguage -ne "unknown" -and $primaryLanguage -ne "auto") {
        return $primaryLanguage
    }

    return "php"
}

function Convert-V2DockerAliasToLocal {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $aliasMap = @{
        "neo4j"  = "127.0.0.1"
        "qdrant" = "127.0.0.1"
        "ollama" = "127.0.0.1"
        "db"     = "127.0.0.1"
    }

    $text = $Value.Trim()
    if ($text -match "^(?<scheme>[a-zA-Z][a-zA-Z0-9+.-]*):\/\/(?<host>[^\/:]+)(?<rest>.*)$") {
        $scheme = [string]$Matches["scheme"]
        $parsedHost = [string]$Matches["host"]
        $rest = [string]$Matches["rest"]
        $hostKey = $parsedHost.ToLowerInvariant()
        if ($aliasMap.ContainsKey($hostKey)) {
            return ("{0}://{1}{2}" -f $scheme, $aliasMap[$hostKey], $rest)
        }
        return $text
    }

    $key = $text.ToLowerInvariant()
    if ($aliasMap.ContainsKey($key)) {
        return [string]$aliasMap[$key]
    }

    return $text
}

function Write-V2GraphRelationshipsFile {
    param(
        [string]$GraphJsonPath,
        [string]$RelationshipsPath
    )

    $graphJson = Get-V2JsonContent -Path $GraphJsonPath
    if (-not $graphJson) {
        throw "graph-json-missing-or-invalid"
    }

    $graph = Get-V2OptionalProperty -InputObject $graphJson -Name "graph" -DefaultValue ([PSCustomObject]@{})
    $edges = @(Get-V2OptionalProperty -InputObject $graph -Name "edges" -DefaultValue @())
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Relationships")
    $lines.Add("")

    $seen = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($edge in $edges) {
        $fromId = [string](Get-V2OptionalProperty -InputObject $edge -Name "from" -DefaultValue "")
        $toId = [string](Get-V2OptionalProperty -InputObject $edge -Name "to" -DefaultValue "")
        $kindRaw = [string](Get-V2OptionalProperty -InputObject $edge -Name "kind" -DefaultValue "related_to")
        if ([string]::IsNullOrWhiteSpace($fromId) -or [string]::IsNullOrWhiteSpace($toId)) {
            continue
        }
        $kind = ($kindRaw -replace "[^A-Za-z0-9_]+", "_").ToUpperInvariant()
        if ([string]::IsNullOrWhiteSpace($kind)) {
            $kind = "RELATED_TO"
        }
        $entry = "[{0}] --[{1}]--> [{2}]" -f $fromId.Trim(), $kind, $toId.Trim()
        if ($seen.Add($entry)) {
            $lines.Add($entry)
        }
    }

    $parent = Split-Path -Parent $RelationshipsPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Initialize-V2Directory -Path $parent
    }
    [System.IO.File]::WriteAllText($RelationshipsPath, ($lines -join [Environment]::NewLine))
    return $seen.Count
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$worldModelJsonPath = Join-Path $orchestratorRoot "state/world-model-auto.json"
$envPath = Join-Path $orchestratorRoot "docker/.env.docker.generated"
$projectState = Get-V2JsonContent -Path $statePath
$envMap = Read-V2EnvMap -Path $envPath

$resolvedStack = Resolve-V2HydrationStack -InputStack $Stack -ProjectState $projectState
$projectSlug = [string](Get-V2OptionalProperty -InputObject $projectState -Name "project_slug" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($projectSlug)) {
    $projectSlug = Get-V2ProjectSlug -Name (Split-Path -Leaf $resolvedProjectPath)
}

$dbConnections = Get-V2OptionalProperty -InputObject $projectState -Name "databases" -DefaultValue ([PSCustomObject]@{})
$neo4jConn = Get-V2OptionalProperty -InputObject $dbConnections -Name "neo4j" -DefaultValue ([PSCustomObject]@{})
$qdrantConn = Get-V2OptionalProperty -InputObject $dbConnections -Name "qdrant" -DefaultValue ([PSCustomObject]@{})

$neo4jUri = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "uri" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($neo4jUri)) {
    $neo4jUri = [string]$(if ($envMap.ContainsKey("NEO4J_URI")) { $envMap["NEO4J_URI"] } else { "bolt://localhost:7687" })
}
$neo4jUri = Convert-V2DockerAliasToLocal -Value $neo4jUri
$neo4jUser = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "user" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($neo4jUser)) {
    $neo4jUser = [string]$(if ($envMap.ContainsKey("NEO4J_USERNAME")) { $envMap["NEO4J_USERNAME"] } else { "neo4j" })
}
$neo4jPassword = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "password" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($neo4jPassword) -and $envMap.ContainsKey("NEO4J_PASSWORD")) {
    $neo4jPassword = [string]$envMap["NEO4J_PASSWORD"]
}
$neo4jDatabase = [string](Get-V2OptionalProperty -InputObject $neo4jConn -Name "database" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($neo4jDatabase)) {
    $neo4jDatabase = [string]$(if ($envMap.ContainsKey("NEO4J_DATABASE")) { $envMap["NEO4J_DATABASE"] } else { "neo4j" })
}

$qdrantHost = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "host" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($qdrantHost)) {
    $qdrantHost = [string]$(if ($envMap.ContainsKey("QDRANT_HOST")) { $envMap["QDRANT_HOST"] } else { "localhost" })
}
$qdrantHost = Convert-V2DockerAliasToLocal -Value $qdrantHost
$qdrantPort = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "port" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($qdrantPort)) {
    $qdrantPort = [string]$(if ($envMap.ContainsKey("QDRANT_PORT")) { $envMap["QDRANT_PORT"] } else { "6333" })
}
$qdrantPrefix = [string](Get-V2OptionalProperty -InputObject $qdrantConn -Name "collection_prefix" -DefaultValue "")
if ([string]::IsNullOrWhiteSpace($qdrantPrefix) -and $envMap.ContainsKey("QDRANT_COLLECTION_PREFIX")) {
    $qdrantPrefix = [string]$envMap["QDRANT_COLLECTION_PREFIX"]
}

$ollamaEmbedUrl = [string]$(if ($envMap.ContainsKey("OLLAMA_EMBED_URL")) { $envMap["OLLAMA_EMBED_URL"] } else { "http://127.0.0.1:11435/v1/embeddings" })
$ollamaEmbedUrl = Convert-V2DockerAliasToLocal -Value $ollamaEmbedUrl
$ollamaEmbedModel = [string]$(if ($envMap.ContainsKey("OLLAMA_EMBED_MODEL")) { $envMap["OLLAMA_EMBED_MODEL"] } else { "all-minilm:latest" })
$ollamaKeepAlive = [string]$(if ($envMap.ContainsKey("OLLAMA_KEEP_ALIVE")) { $envMap["OLLAMA_KEEP_ALIVE"] } else { "10m" })

$reportsDir = Join-Path $orchestratorRoot "reports"
$memoryNodesDir = Join-Path $resolvedProjectPath "memory_graph/nodes"
$relationshipsPath = Join-Path $resolvedProjectPath "memory_graph/edges/relationships.md"
Initialize-V2Directory -Path $reportsDir
Initialize-V2Directory -Path $memoryNodesDir
Initialize-V2Directory -Path (Split-Path -Parent $relationshipsPath)

$stamp = Get-Date -Format "yyyyMMddHHmmss"
$graphOutputPath = Join-Path $reportsDir ("ast-graph-{0}.json" -f $stamp)
$latestGraphOutputPath = Join-Path $reportsDir "ast-graph-latest.json"

$astScript = Join-Path (Split-Path -Parent $PSScriptRoot) "ast_analyzer.py"
$memorySyncScript = Join-Path (Split-Path -Parent $PSScriptRoot) "memory_sync.py"
if (-not (Test-Path -LiteralPath $astScript -PathType Leaf)) {
    throw "ast-analyzer-script-not-found: $astScript"
}
if (-not (Test-Path -LiteralPath $memorySyncScript -PathType Leaf)) {
    throw "memory-sync-script-not-found: $memorySyncScript"
}

$astArgs = @(
    $astScript,
    "--project-path", $resolvedProjectPath,
    "--project-slug", $projectSlug,
    "--stack", $resolvedStack,
    "--save-memory-nodes",
    "--output", $graphOutputPath
)
$canSyncNeo4jAst = (-not $SkipNeo4j) -and -not [string]::IsNullOrWhiteSpace($neo4jPassword)
if ($canSyncNeo4jAst) {
    $astArgs += @(
        "--neo4j",
        "--neo4j-uri", $neo4jUri,
        "--neo4j-user", $neo4jUser,
        "--neo4j-password", $neo4jPassword,
        "--neo4j-db", $neo4jDatabase
    )
}

$astOutput = @(& $PythonExecutable @astArgs 2>&1)
if ($LASTEXITCODE -ne 0) {
    $tail = ($astOutput | Select-Object -Last 30) -join [Environment]::NewLine
    throw "ast-analyzer-failed: exit=$LASTEXITCODE output=$tail"
}

Copy-Item -LiteralPath $graphOutputPath -Destination $latestGraphOutputPath -Force
$relationshipsCount = Write-V2GraphRelationshipsFile -GraphJsonPath $graphOutputPath -RelationshipsPath $relationshipsPath

$guardProfile = Set-V2OllamaGpuGuardEnv
$memoryArgs = @(
    $memorySyncScript,
    "--project-slug", $projectSlug,
    "--project-root", $resolvedProjectPath,
    "--memory-dir", $memoryNodesDir,
    "--relationships-path", $relationshipsPath,
    "--ast-graph-path", $graphOutputPath,
    "--ollama-url", $ollamaEmbedUrl,
    "--ollama-model", $ollamaEmbedModel,
    "--ollama-keep-alive", $ollamaKeepAlive
)
if (Test-Path -LiteralPath (Join-Path $orchestratorRoot "tasks/task-dag.json") -PathType Leaf) {
    $memoryArgs += @("--task-dag-path", (Join-Path $orchestratorRoot "tasks/task-dag.json"))
}
if (Test-Path -LiteralPath (Join-Path $orchestratorRoot "tasks/completions") -PathType Container) {
    $memoryArgs += @("--task-completions-dir", (Join-Path $orchestratorRoot "tasks/completions"))
}
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    $memoryArgs += @("--dependency-graph-path", $statePath)
}
if (Test-Path -LiteralPath $worldModelJsonPath -PathType Leaf) {
    $memoryArgs += @("--world-model-json-path", $worldModelJsonPath)
}
if (-not [string]::IsNullOrWhiteSpace($neo4jUri)) { $memoryArgs += @("--neo4j-uri", $neo4jUri) }
if (-not [string]::IsNullOrWhiteSpace($neo4jUser)) { $memoryArgs += @("--neo4j-user", $neo4jUser) }
if (-not [string]::IsNullOrWhiteSpace($neo4jPassword)) { $memoryArgs += @("--neo4j-password", $neo4jPassword) }
if (-not [string]::IsNullOrWhiteSpace($neo4jDatabase)) { $memoryArgs += @("--neo4j-database", $neo4jDatabase) }
if (-not [string]::IsNullOrWhiteSpace($qdrantHost)) { $memoryArgs += @("--qdrant-host", $qdrantHost) }
if (-not [string]::IsNullOrWhiteSpace($qdrantPort)) { $memoryArgs += @("--qdrant-port", $qdrantPort) }
if (-not [string]::IsNullOrWhiteSpace($qdrantPrefix)) { $memoryArgs += @("--collection-prefix", $qdrantPrefix) }
if ($SkipNeo4j) { $memoryArgs += "--skip-neo4j" }
if ($SkipQdrant) { $memoryArgs += "--skip-qdrant" }

$memoryOutput = @(& $PythonExecutable @memoryArgs 2>&1)
if ($LASTEXITCODE -ne 0) {
    $tail = ($memoryOutput | Select-Object -Last 30) -join [Environment]::NewLine
    throw "memory-sync-failed: exit=$LASTEXITCODE output=$tail"
}

$memoryRaw = ($memoryOutput -join [Environment]::NewLine).Trim()
$memoryResult = $null
try {
    $memoryResult = $memoryRaw | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw "memory-sync-non-json-output: $memoryRaw"
}

$astGraph = Get-V2JsonContent -Path $graphOutputPath
$summary = [PSCustomObject]@{
    status = "ok"
    generated_at = Get-V2Timestamp
    project_path = $resolvedProjectPath
    project_slug = $projectSlug
    stack = $resolvedStack
    ast = [PSCustomObject]@{
        output_path = $graphOutputPath
        latest_output_path = $latestGraphOutputPath
        files_analyzed = [int](Get-V2OptionalProperty -InputObject $astGraph -Name "files_analyzed" -DefaultValue 0)
        nodes = [int](Get-V2OptionalProperty -InputObject $astGraph -Name "nodes" -DefaultValue 0)
        edges = [int](Get-V2OptionalProperty -InputObject $astGraph -Name "edges" -DefaultValue 0)
        relationships_written = $relationshipsCount
        neo4j_synced = $canSyncNeo4jAst
    }
    memory_sync = $memoryResult
    gpu_guard = $guardProfile
}

$latestHydrationPath = Join-Path $reportsDir "graph-hydration-latest.json"
Save-V2JsonContent -Path $latestHydrationPath -Value $summary
$summary | ConvertTo-Json -Depth 10
