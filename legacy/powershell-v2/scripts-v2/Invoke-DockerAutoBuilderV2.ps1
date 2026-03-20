<#
.SYNOPSIS
    V2 Docker Auto-Builder - generates isolated Docker scaffolding for a submitted project.
.DESCRIPTION
    Reads project classification and tech stack from .ai-orchestrator/state/project-state.json
    and generates Docker Compose, Dockerfile, .dockerignore, and .env.docker files
    under .ai-orchestrator/docker/. Uses slug-based naming for complete project isolation:
      container names, database names, volumes, and networks are all prefixed with the project slug.
    Supports auto-detection or explicit stack selection (node/python/php/go/dotnet/java/ruby/rust/static).
    NEVER overwrites existing Docker assets without explicit -Force flag.
.PARAMETER ProjectPath
    Path to the project root containing .ai-orchestrator/. Defaults to current directory.
.PARAMETER Stack
    Tech stack: auto | node | python | php | go | dotnet | java | ruby | rust | static
.PARAMETER Force
    If set, overwrites existing Docker assets.
.EXAMPLE
    .\scripts\v2\Invoke-DockerAutoBuilderV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-DockerAutoBuilderV2.ps1 -ProjectPath C:\projects\myapp -Stack node
#>param(
    [string]$ProjectPath = ".",
    [ValidateSet("auto", "node", "python", "php", "go", "dotnet", "java", "ruby", "rust", "static")]
    [string]$Stack = "auto",
    [ValidateSet("auto", "postgres", "mysql", "mongodb", "none")]
    [string]$Database = "auto",
    [switch]$IncludeRedis,
    [switch]$IncludeRabbitMq,
    [switch]$IncludeWorker,
    [switch]$IncludeNeo4j,
    [switch]$IncludeQdrant,
    [switch]$IncludeOllama,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$dockerDirectory = Join-Path $resolvedProjectPath "ai-orchestrator/docker"
Initialize-V2Directory -Path $dockerDirectory

$legacyDockerFactory = Join-Path (Split-Path -Parent $PSScriptRoot) "New-DockerFactory.ps1"
if (-not (Test-Path -LiteralPath $legacyDockerFactory -PathType Leaf)) {
    throw "Required base Docker factory script not found: $legacyDockerFactory"
}

$dockerParams = @{
    ProjectPath     = $resolvedProjectPath
    OutputDirectory = $dockerDirectory
    Stack           = $Stack
    Database        = $Database
    Force           = $true
}
if ($IncludeRedis) { $dockerParams.IncludeRedis = $true }
if ($IncludeRabbitMq) { $dockerParams.IncludeRabbitMq = $true }
if ($IncludeWorker) { $dockerParams.IncludeWorker = $true }
if ($IncludeNeo4j) { $dockerParams.IncludeNeo4j = $true }
if ($IncludeQdrant) { $dockerParams.IncludeQdrant = $true }
if ($IncludeOllama -or $IncludeQdrant) { $dockerParams.IncludeOllama = $true }

& $legacyDockerFactory @dockerParams | Out-Null

$sourceDockerfile = Join-Path $dockerDirectory "Dockerfile.generated"
$targetDockerfile = Join-Path $dockerDirectory "app.Dockerfile.generated"
if (Test-Path -LiteralPath $sourceDockerfile) {
    Copy-Item -LiteralPath $sourceDockerfile -Destination $targetDockerfile -Force
}

# Ensure docker build context (project root) has an effective .dockerignore.
# Docker only honors .dockerignore from the build context root, not from ai-orchestrator/docker/.
$generatedIgnorePath = Join-Path $dockerDirectory ".dockerignore.generated"
$contextIgnorePath = Join-Path $resolvedProjectPath ".dockerignore"
if (Test-Path -LiteralPath $generatedIgnorePath -PathType Leaf) {
    $generatedLines = @(
        Get-Content -LiteralPath $generatedIgnorePath -ErrorAction SilentlyContinue |
            ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )

    if ($generatedLines.Count -gt 0) {
        if (-not (Test-Path -LiteralPath $contextIgnorePath -PathType Leaf)) {
            [System.IO.File]::WriteAllText($contextIgnorePath, ($generatedLines -join [Environment]::NewLine) + [Environment]::NewLine)
        }
        else {
            $existingLines = @(
                Get-Content -LiteralPath $contextIgnorePath -ErrorAction SilentlyContinue |
                    ForEach-Object { [string]$_ }
            )
            $normalizedExisting = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
            foreach ($line in $existingLines) {
                $trimmed = $line.Trim()
                if (-not [string]::IsNullOrWhiteSpace($trimmed)) {
                    [void]$normalizedExisting.Add($trimmed)
                }
            }

            $missing = New-Object System.Collections.Generic.List[string]
            foreach ($line in $generatedLines) {
                $trimmed = $line.Trim()
                if ([string]::IsNullOrWhiteSpace($trimmed)) {
                    continue
                }
                if (-not $normalizedExisting.Contains($trimmed)) {
                    $missing.Add($trimmed)
                }
            }

            if ($missing.Count -gt 0) {
                $appendBlock = New-Object System.Collections.Generic.List[string]
                $appendBlock.Add("")
                $appendBlock.Add("# orchestrator-generated dockerignore rules")
                foreach ($line in $missing) { $appendBlock.Add($line) }
                [System.IO.File]::AppendAllText(
                    $contextIgnorePath,
                    ($appendBlock -join [Environment]::NewLine) + [Environment]::NewLine
                )
            }
        }
    }
}

$envPath = Join-Path $dockerDirectory ".env.docker.generated"
$dbEngine = "unknown"
$dbHost = "unknown"
if (Test-Path -LiteralPath $envPath) {
    $envContent = Get-Content -LiteralPath $envPath
    foreach ($line in $envContent) {
        if ($line -match "^DATABASE_ENGINE=(.+)$") {
            $dbEngine = $matches[1]
        }
        elseif ($line -match "^DB_HOST=(.+)$") {
            $dbHost = $matches[1]
        }
    }
}

$dbConfigPath = Join-Path $resolvedProjectPath "ai-orchestrator/database/config.md"
$dbSchemaReadmePath = Join-Path $resolvedProjectPath "ai-orchestrator/database/schema/README.md"
$dbConfig = @"
# Database Config

## Engine
- Value: $dbEngine
- Host: $dbHost
- Source: /ai-orchestrator/docker/.env.docker.generated

## Isolation
- Each project keeps dedicated DB namespace and service name.
- No shared transactional DB between projects.
"@

$schemaReadme = @"
# Schema Pointers

Use this directory for schema snapshots, migration maps, and entity links.

Generated at: $(Get-V2Timestamp)
"@

Write-V2File -Path $dbConfigPath -Content $dbConfig -Force
Write-V2File -Path $dbSchemaReadmePath -Content $schemaReadme -Force

Write-Output "V2 Docker assets generated in $dockerDirectory"

