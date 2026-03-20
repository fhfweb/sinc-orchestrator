<#
.SYNOPSIS
    V1 Docker Factory — generates isolated Docker scaffolding for any project.
.DESCRIPTION
    Reads project classification signals and generates Docker Compose, Dockerfile,
    .dockerignore, and .env.docker files under infra/<project-slug>/docker/.
    Uses slug-based naming for complete isolation (container names, networks, volumes,
    database names are all prefixed with the project slug).
    NEVER overwrites existing Docker files — uses .generated suffix first.
    For V2 projects use scripts/v2/Invoke-DockerAutoBuilderV2.ps1 instead.
.PARAMETER ProjectPath
    Path to the project root. Defaults to current directory.
.PARAMETER IncludeNeo4j
    If set, adds a Neo4j service to the compose file.
.PARAMETER IncludeQdrant
    If set, adds a Qdrant service to the compose file.
.PARAMETER IncludeRedis
    If set, adds a Redis service to the compose file.
.EXAMPLE
    .\scripts\New-DockerFactory.ps1 -ProjectPath C:\projects\myapp
    .\scripts\New-DockerFactory.ps1 -ProjectPath . -IncludeNeo4j -IncludeQdrant
#>param(
    [string]$ProjectPath = ".",
    [string]$OutputDirectory,
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

function Get-ProjectSlug {
    param([string]$Name)

    $slug = $Name.ToLowerInvariant() -replace "[^a-z0-9]+", "-"
    $slug = $slug.Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) {
        return "project"
    }

    return $slug
}

function Get-RelativeUnixPath {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $baseFullPath = [System.IO.Path]::GetFullPath($BasePath)
    $targetFullPath = [System.IO.Path]::GetFullPath($TargetPath)

    if (-not $baseFullPath.EndsWith("\")) {
        $baseFullPath = "$baseFullPath\"
    }

    if (Test-Path -LiteralPath $TargetPath -PathType Container) {
        if (-not $targetFullPath.EndsWith("\")) {
            $targetFullPath = "$targetFullPath\"
        }
    }

    $baseUri = [System.Uri]::new($baseFullPath)
    $targetUri = [System.Uri]::new($targetFullPath)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString())
}

function Get-JsonFile {
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

function Get-TextFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    return (Get-Content -LiteralPath $Path -Raw)
}

function Test-ObjectProperty {
    param(
        [object]$InputObject,
        [string]$Name
    )

    return ($null -ne $InputObject -and $InputObject.PSObject.Properties.Name -contains $Name)
}

function Get-ObjectPropertyValue {
    param(
        [object]$InputObject,
        [string]$Name
    )

    if (Test-ObjectProperty -InputObject $InputObject -Name $Name) {
        return $InputObject.$Name
    }

    return $null
}

function Add-StringListValues {
    param(
        [System.Collections.Generic.List[string]]$List,
        [object[]]$Values
    )

    foreach ($value in @($Values)) {
        $List.Add([string]$value)
    }
}

function New-RandomSecret {
    param([int]$ByteCount = 24)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }

    return (($bytes | ForEach-Object { "{0:x2}" -f $_ }) -join "")
}

function Test-TruthyValue {
    param([object]$Value)

    if ($null -eq $Value) {
        return $false
    }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $false
    }
    $normalized = $text.Trim().ToLowerInvariant()
    return ($normalized -in @("1", "true", "yes", "on", "enabled"))
}

function Get-EnvIntValue {
    param(
        [hashtable]$Map,
        [string]$Key,
        [int]$DefaultValue,
        [int]$Minimum = 0
    )

    if ($Map -and $Map.ContainsKey($Key)) {
        try {
            $parsed = [int]$Map[$Key]
            if ($parsed -ge $Minimum) {
                return $parsed
            }
        }
        catch {
        }
    }

    if ($DefaultValue -lt $Minimum) {
        return $Minimum
    }
    return $DefaultValue
}

function Read-GeneratedEnvMap {
    param([string]$Path)

    $map = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $map
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line.TrimStart().StartsWith("#")) { continue }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2) { continue }
        $key = $parts[0].Trim()
        if ([string]::IsNullOrWhiteSpace($key)) { continue }
        $map[$key] = $parts[1]
    }
    return $map
}

function New-DbConfig {
    param(
        [string]$Engine,
        [string]$Slug
    )

    switch ($Engine) {
        "postgres" {
            $dbPassword = New-RandomSecret
            return [PSCustomObject]@{
                Engine     = "postgres"
                Port       = 5432
                User       = $Slug
                Password   = $dbPassword
                RootPassword = ""
                Database   = $Slug
                Healthcheck = "pg_isready -U $Slug -d $Slug"
            }
        }
        "mysql" {
            $dbPassword = New-RandomSecret
            $rootPassword = New-RandomSecret
            return [PSCustomObject]@{
                Engine     = "mysql"
                Port       = 3306
                User       = $Slug
                Password   = $dbPassword
                RootPassword = $rootPassword
                Database   = $Slug
                Healthcheck = "mysqladmin ping -h 127.0.0.1 -u$Slug -p$dbPassword"
            }
        }
        "mongodb" {
            $dbPassword = New-RandomSecret
            return [PSCustomObject]@{
                Engine     = "mongodb"
                Port       = 27017
                User       = $Slug
                Password   = $dbPassword
                RootPassword = ""
                Database   = $Slug
                Healthcheck = "echo 'db.runCommand({ ping: 1 })' | mongosh localhost/$Slug --quiet"
            }
        }
        default {
            return $null
        }
    }
}

function Get-DefaultCommand {
    param([string]$StackName)

    switch ($StackName) {
        "node"   { return "node index.js" }
        "python" { return "uvicorn app.main:app --host 0.0.0.0 --port 8000" }
        "php"    { return "php artisan serve --host=0.0.0.0 --port=8000" }
        "go"     { return "go run ." }
        "dotnet" { return "dotnet run --urls http://0.0.0.0:8080" }
        "java"   { return "./mvnw spring-boot:run" }
        "ruby"   { return "bundle exec rails server -b 0.0.0.0 -p 3000" }
        "rust"   { return "cargo run" }
        "static" { return "npm run build" }
        default  { return 'echo "Set APP_COMMAND manually"' }
    }
}

function Sanitize-GeneratedCommand {
    param([string]$CommandText)

    if ([string]::IsNullOrWhiteSpace($CommandText)) {
        return ""
    }

    $sanitized = [string]$CommandText
    $sanitized = $sanitized -replace "\s+#\s*REVIEW_REQUIRED.*$", ""
    $sanitized = $sanitized.Trim()
    return $sanitized
}

function Get-StackPort {
    param([string]$StackName)

    switch ($StackName) {
        "node"   { return 3000 }
        "python" { return 8000 }
        "php"    { return 8000 }
        "go"     { return 8080 }
        "dotnet" { return 8080 }
        "java"   { return 8080 }
        "ruby"   { return 3000 }
        "rust"   { return 8080 }
        "static" { return 8080 }
        default  { return 8080 }
    }
}

function Get-DockerfileContent {
    param(
        [string]$StackName,
        [string]$CommandText
    )

    switch ($StackName) {
        "node" {
            return @"
FROM node:22-bookworm-slim
WORKDIR /workspace
COPY package*.json ./
COPY pnpm-lock.yaml* yarn.lock* bun.lockb* ./
RUN corepack enable || true
RUN if [ -f package-lock.json ]; then npm ci; elif [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; elif [ -f yarn.lock ]; then yarn install --frozen-lockfile; else npm install; fi
COPY . .
EXPOSE 3000
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "python" {
            return @"
FROM python:3.12-slim
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements*.txt* pyproject.toml poetry.lock* ./
RUN pip install --upgrade pip
RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; elif [ -f pyproject.toml ]; then pip install .; else echo "REVIEW_REQUIRED: verify Python dependency installation"; fi
COPY . .
EXPOSE 8000
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "php" {
            return @"
FROM php:8.3-cli
RUN apt-get update && apt-get install -y git unzip libpq-dev libzip-dev libonig-dev libicu-dev && pecl install redis && docker-php-ext-enable redis && docker-php-ext-install pdo pdo_mysql pdo_pgsql mbstring zip bcmath pcntl intl
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /workspace
ENV COMPOSER_ALLOW_SUPERUSER=1
COPY composer.json composer.lock* ./
RUN if [ -f composer.json ]; then composer install --no-interaction --prefer-dist --no-scripts; else echo "REVIEW_REQUIRED: composer.json not found"; fi
COPY . .
EXPOSE 8000
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "go" {
            return @"
FROM golang:1.24-alpine
WORKDIR /workspace
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
EXPOSE 8080
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "dotnet" {
            return @"
FROM mcr.microsoft.com/dotnet/sdk:8.0
WORKDIR /workspace
COPY . .
EXPOSE 8080
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "java" {
            return @"
FROM eclipse-temurin:21-jdk
WORKDIR /workspace
COPY . .
EXPOSE 8080
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "ruby" {
            return @"
FROM ruby:3.3-slim
WORKDIR /workspace
COPY Gemfile* ./
RUN if [ -f Gemfile ]; then bundle install; else echo "REVIEW_REQUIRED: Gemfile not found"; fi
COPY . .
EXPOSE 3000
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "rust" {
            return @"
FROM rust:1.83-slim
WORKDIR /workspace
COPY Cargo.toml Cargo.lock* ./
RUN cargo fetch
COPY . .
EXPOSE 8080
CMD ["sh", "-lc", "$CommandText"]
"@
        }
        "static" {
            return @"
FROM node:22-bookworm-slim AS build
WORKDIR /workspace
COPY package*.json ./
COPY pnpm-lock.yaml* yarn.lock* bun.lockb* ./
RUN corepack enable || true
RUN if [ -f package-lock.json ]; then npm ci; elif [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; elif [ -f yarn.lock ]; then yarn install --frozen-lockfile; else npm install; fi
COPY . .
RUN sh -lc "$CommandText"

FROM nginx:1.27-alpine
COPY --from=build /workspace/dist /usr/share/nginx/html
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
"@
        }
        default {
            throw "Unsupported stack: $StackName"
        }
    }
}

function Get-DbServiceContent {
    param([object]$DbConfig)

    if (-not $DbConfig) {
        return ""
    }

    switch ($DbConfig.Engine) {
        "postgres" {
            return @"
  db:
    image: postgres:17-alpine
    container_name: ${projectSlug}-db
    environment:
      POSTGRES_DB: $($DbConfig.Database)
      POSTGRES_USER: $($DbConfig.User)
      POSTGRES_PASSWORD: $($DbConfig.Password)
    ports:
      - "$($DbConfig.Port):5432"
    volumes:
      - ${projectSlug}-postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "$($DbConfig.Healthcheck)"]
      interval: 10s
      timeout: 5s
      retries: 10
"@
        }
        "mysql" {
            return @"
  db:
    image: mysql:8.4
    container_name: ${projectSlug}-db
    environment:
      MYSQL_DATABASE: $($DbConfig.Database)
      MYSQL_USER: $($DbConfig.User)
      MYSQL_PASSWORD: $($DbConfig.Password)
      MYSQL_ROOT_PASSWORD: $($DbConfig.RootPassword)
    ports:
      - "$($DbConfig.Port):3306"
    volumes:
      - ${projectSlug}-mysql-data:/var/lib/mysql
    healthcheck:
      test: ["CMD-SHELL", "$($DbConfig.Healthcheck)"]
      interval: 10s
      timeout: 5s
      retries: 10
"@
        }
        "mongodb" {
            return @"
  db:
    image: mongo:8
    container_name: ${projectSlug}-db
    environment:
      MONGO_INITDB_ROOT_USERNAME: $($DbConfig.User)
      MONGO_INITDB_ROOT_PASSWORD: $($DbConfig.Password)
      MONGO_INITDB_DATABASE: $($DbConfig.Database)
    ports:
      - "$($DbConfig.Port):27017"
    volumes:
      - ${projectSlug}-mongo-data:/data/db
    healthcheck:
      test: ["CMD-SHELL", "$($DbConfig.Healthcheck)"]
      interval: 15s
      timeout: 10s
      retries: 10
"@
        }
        default {
            return ""
        }
    }
}

function Get-DependencyLines {
    param(
        [bool]$NeedsApp = $false,
        [bool]$NeedsDatabase,
        [bool]$NeedsRedis,
        [bool]$NeedsRabbitMq,
        [bool]$NeedsNeo4j,
        [bool]$NeedsQdrant,
        [bool]$NeedsOllama = $false,
        [string]$Indent = "    "
    )

    $dependencies = New-Object System.Collections.Generic.List[object]
    if ($NeedsApp) {
        $dependencies.Add([PSCustomObject]@{ Name = "app"; Condition = "service_started" })
    }
    if ($NeedsDatabase) {
        $dependencies.Add([PSCustomObject]@{ Name = "db"; Condition = "service_healthy" })
    }
    if ($NeedsRedis) {
        $dependencies.Add([PSCustomObject]@{ Name = "redis"; Condition = "service_started" })
    }
    if ($NeedsRabbitMq) {
        $dependencies.Add([PSCustomObject]@{ Name = "rabbitmq"; Condition = "service_started" })
    }
    if ($NeedsNeo4j) {
        $dependencies.Add([PSCustomObject]@{ Name = "neo4j"; Condition = "service_started" })
    }
    if ($NeedsQdrant) {
        $dependencies.Add([PSCustomObject]@{ Name = "qdrant"; Condition = "service_started" })
    }
    if ($NeedsOllama) {
        $dependencies.Add([PSCustomObject]@{ Name = "ollama"; Condition = "service_started" })
    }

    if ($dependencies.Count -eq 0) {
        return @()
    }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("${Indent}depends_on:")
    foreach ($dependency in $dependencies) {
        $lines.Add("${Indent}  $($dependency.Name):")
        $lines.Add("${Indent}    condition: $($dependency.Condition)")
    }

    return @($lines)
}

function Get-RedisServiceContent {
    return @"
  redis:
    image: redis:7-alpine
    container_name: ${projectSlug}-redis
    ports:
      - "6379:6379"
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - ${projectSlug}-redis-data:/data
"@
}

function Get-RabbitMqServiceContent {
    param([string]$Password)

    return @"
  rabbitmq:
    image: rabbitmq:4-management
    container_name: ${projectSlug}-rabbitmq
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: $projectSlug
      RABBITMQ_DEFAULT_PASS: $Password
    volumes:
      - ${projectSlug}-rabbitmq-data:/var/lib/rabbitmq
"@
}

function Get-OllamaServiceContent {
    param(
        [string]$Image = "ollama/ollama:latest",
        [int]$HostPort = 11435,
        [bool]$EnableGpu = $true,
        [string]$NvidiaVisibleDevices = "all",
        [string]$CudaVisibleDevices = "0",
        [int64]$GpuOverheadBytes = 3221225472,
        [int]$NumParallel = 2,
        [int]$MaxLoadedModels = 2,
        [string]$KeepAlive = "30m"
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("  ollama:")
    $lines.Add("    image: $Image")
    $lines.Add("    container_name: ${projectSlug}-ollama")
    $lines.Add("    ports:")
    $lines.Add(("      - `"{0}:11434`"" -f $HostPort))
    $lines.Add("    environment:")
    $lines.Add("      OLLAMA_HOST: 0.0.0.0")
    $lines.Add("      OLLAMA_KEEP_ALIVE: `"$KeepAlive`"")
    $lines.Add("      OLLAMA_NUM_PARALLEL: `"$NumParallel`"")
    $lines.Add("      OLLAMA_MAX_LOADED_MODELS: `"$MaxLoadedModels`"")
    $lines.Add("      OLLAMA_GPU_OVERHEAD: `"$GpuOverheadBytes`"")
    if (-not [string]::IsNullOrWhiteSpace($CudaVisibleDevices)) {
        $lines.Add("      CUDA_VISIBLE_DEVICES: `"$CudaVisibleDevices`"")
    }
    if (-not [string]::IsNullOrWhiteSpace($NvidiaVisibleDevices)) {
        $lines.Add("      NVIDIA_VISIBLE_DEVICES: `"$NvidiaVisibleDevices`"")
    }
    $lines.Add("    volumes:")
    $lines.Add("      - ${projectSlug}-ollama-data:/root/.ollama")
    if ($EnableGpu) {
        $lines.Add("    gpus: all")
        $lines.Add("    deploy:")
        $lines.Add("      resources:")
        $lines.Add("        reservations:")
        $lines.Add("          devices:")
        $lines.Add("            - driver: nvidia")
        $lines.Add("              count: all")
        $lines.Add("              capabilities: [gpu]")
    }

    return ($lines -join [Environment]::NewLine)
}

function Get-WorkerServiceContent {
    param(
        [string]$RelativeContext,
        [string]$DockerfileRelativeToProject,
        [string]$CommandText,
        [string[]]$DependencyLines
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("  worker:")
    $lines.Add("    container_name: ${projectSlug}-worker")
    $lines.Add("    build:")
    $lines.Add("      context: $RelativeContext")
    $lines.Add("      dockerfile: $DockerfileRelativeToProject")
    $lines.Add("    working_dir: /workspace")
    $lines.Add("    volumes:")
    $lines.Add("      - ${relativeContext}:/workspace")
    $lines.Add("    env_file:")
    $lines.Add("      - .env.docker.generated")
    $lines.Add("    command: >-")
    $lines.Add("      sh -lc `"$CommandText`"")
    if (@($DependencyLines).Count -gt 0) {
        foreach ($line in $DependencyLines) {
            $lines.Add($line)
        }
    }

    return ($lines -join [Environment]::NewLine)
}

function Get-CommanderDashboardServiceContent {
    param(
        [string]$RelativeContext,
        [string]$OrchestratorRootRelativeToCompose,
        [int]$HostPort,
        [string[]]$DependencyLines
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("  # commander-dashboard: removed from generated stacks.")
    $lines.Add("  # Use the canonical provider dashboard instead:")
    $lines.Add(("  #   http://host.docker.internal:{0}/dashboard" -f $HostPort))
    $lines.Add("  # Legacy Flask dashboard generation via scripts/v2/Start-StreamingServer.py")
    $lines.Add("  # has been discontinued.")

    return ($lines -join [Environment]::NewLine)
}

function Get-AuxiliaryServicePlan {
    param(
        [string]$ProjectRoot,
        [string]$StackName,
        [switch]$ForceRedis,
        [switch]$ForceRabbitMq,
        [switch]$ForceWorker
    )

    $packageJson = Get-JsonFile -Path (Join-Path $ProjectRoot "package.json")
    $composerJson = Get-JsonFile -Path (Join-Path $ProjectRoot "composer.json")
    $pyprojectText = Get-TextFile -Path (Join-Path $ProjectRoot "pyproject.toml")
    $requirementsText = Get-TextFile -Path (Join-Path $ProjectRoot "requirements.txt")
    $gemfileText = Get-TextFile -Path (Join-Path $ProjectRoot "Gemfile")
    $envText = @(
        (Get-TextFile -Path (Join-Path $ProjectRoot ".env"))
        (Get-TextFile -Path (Join-Path $ProjectRoot ".env.example"))
        (Get-TextFile -Path (Join-Path $ProjectRoot ".env.local"))
    ) -join [Environment]::NewLine

    $packageDependencies = New-Object System.Collections.Generic.List[string]
    $packageScripts = @{}
    if ($packageJson) {
        $dependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "dependencies"
        $devDependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "devDependencies"
        $scripts = Get-ObjectPropertyValue -InputObject $packageJson -Name "scripts"
        if ($dependencies) {
            Add-StringListValues -List $packageDependencies -Values @($dependencies.PSObject.Properties.Name)
        }
        if ($devDependencies) {
            Add-StringListValues -List $packageDependencies -Values @($devDependencies.PSObject.Properties.Name)
        }
        if ($scripts) {
            foreach ($property in $scripts.PSObject.Properties) {
                $packageScripts[$property.Name] = [string]$property.Value
            }
        }
    }

    $composerDependencies = New-Object System.Collections.Generic.List[string]
    if ($composerJson) {
        $requireMap = Get-ObjectPropertyValue -InputObject $composerJson -Name "require"
        $requireDevMap = Get-ObjectPropertyValue -InputObject $composerJson -Name "require-dev"
        if ($requireMap) {
            Add-StringListValues -List $composerDependencies -Values @($requireMap.PSObject.Properties.Name)
        }
        if ($requireDevMap) {
            Add-StringListValues -List $composerDependencies -Values @($requireDevMap.PSObject.Properties.Name)
        }
    }

    $hasRedisSignals = $ForceRedis -or
        ($envText -match "(?im)^(REDIS_URL|REDIS_HOST|CACHE_DRIVER)\s*=") -or
        ($envText -match "(?im)^QUEUE_CONNECTION\s*=\s*redis") -or
        ($packageDependencies -contains "redis") -or
        ($packageDependencies -contains "ioredis") -or
        ($packageDependencies -contains "bull") -or
        ($packageDependencies -contains "bullmq") -or
        ($composerDependencies -contains "laravel/horizon") -or
        ($composerDependencies -contains "predis/predis") -or
        ($requirementsText -match "(?im)^celery") -or
        ($requirementsText -match "(?im)^redis") -or
        ($pyprojectText -match "celery") -or
        ($pyprojectText -match "redis") -or
        ($gemfileText -match "sidekiq") -or
        ($gemfileText -match "redis") -or
        (Test-Path -LiteralPath (Join-Path $ProjectRoot "config\horizon.php"))

    $hasRabbitSignals = $ForceRabbitMq -or
        ($envText -match "(?im)^(AMQP_URL|RABBITMQ_URL)\s*=") -or
        ($envText -match "(?im)^QUEUE_CONNECTION\s*=\s*rabbitmq") -or
        ($packageDependencies -contains "amqplib") -or
        ($packageDependencies -contains "amqp-connection-manager") -or
        ($requirementsText -match "(?im)^pika") -or
        ($requirementsText -match "(?im)^kombu") -or
        ($pyprojectText -match "pika") -or
        ($pyprojectText -match "kombu")

    $workerCommand = ""
    $workerConfidence = "unknown"
    $workerReason = ""

    switch ($StackName) {
        "node" {
            if ($packageScripts.ContainsKey("worker")) {
                $workerCommand = "npm run worker"
                $workerConfidence = "verified"
                $workerReason = "package.json scripts.worker"
            }
            elseif ($packageScripts.ContainsKey("queue")) {
                $workerCommand = "npm run queue"
                $workerConfidence = "verified"
                $workerReason = "package.json scripts.queue"
            }
            elseif ($ForceWorker -and (($packageDependencies -contains "bull") -or ($packageDependencies -contains "bullmq"))) {
                $workerCommand = "npm run worker"
                $workerConfidence = "inferred"
                $workerReason = "Bull or BullMQ dependency without explicit worker script"
            }
        }
        "python" {
            if (($requirementsText -match "(?im)^celery") -or ($pyprojectText -match "celery")) {
                if ($hasRedisSignals -or $hasRabbitSignals -or $ForceWorker) {
                    $workerCommand = "celery -A app worker --loglevel=info"
                    $workerConfidence = "inferred"
                    $workerReason = "Celery detected"
                }
            }
        }
        "php" {
            $artisanPath = Join-Path $ProjectRoot "artisan"
            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "config\horizon.php")) {
                $workerCommand = "php artisan horizon"
                $workerConfidence = "inferred"
                $workerReason = "config/horizon.php detected"
                $hasRedisSignals = $true
            }
            elseif ((Test-Path -LiteralPath (Join-Path $ProjectRoot "config\queue.php")) -and (Test-Path -LiteralPath $artisanPath)) {
                $workerCommand = "php artisan queue:work"
                $workerConfidence = "inferred"
                $workerReason = "config/queue.php detected"
            }
        }
        "ruby" {
            if ($gemfileText -match "sidekiq") {
                $workerCommand = "bundle exec sidekiq"
                $workerConfidence = "inferred"
                $workerReason = "Sidekiq dependency detected"
                $hasRedisSignals = $true
            }
        }
    }

    if ($ForceWorker -and [string]::IsNullOrWhiteSpace($workerCommand)) {
        switch ($StackName) {
            "php" { $workerCommand = "php artisan queue:work" }
            "python" { $workerCommand = "celery -A app worker --loglevel=info" }
            "node" { $workerCommand = "npm run worker" }
            "ruby" { $workerCommand = "bundle exec sidekiq" }
        }
        if (-not [string]::IsNullOrWhiteSpace($workerCommand)) {
            $workerConfidence = "inferred"
            $workerReason = "Worker requested explicitly"
        }
    }

    $workerCommand = Sanitize-GeneratedCommand -CommandText $workerCommand

    return [PSCustomObject]@{
        IncludeRedis       = [bool]$hasRedisSignals
        IncludeRabbitMq    = [bool]$hasRabbitSignals
        IncludeWorker      = -not [string]::IsNullOrWhiteSpace($workerCommand)
        WorkerCommand      = $workerCommand
        WorkerConfidence   = $workerConfidence
        WorkerReason       = $workerReason
    }
}

$resolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$projectName = Split-Path -Leaf $resolvedProjectPath
$projectSlug = Get-ProjectSlug -Name $projectName

$intakeScript = Join-Path $PSScriptRoot "Invoke-ProjectIntake.ps1"
$intakeOutputPath = Join-Path $resolvedProjectPath "docs\agents\INTAKE_REPORT.md"
$intakeJsonText = & $intakeScript -ProjectPath $resolvedProjectPath -OutputPath $intakeOutputPath -EmitJson
$report = ($intakeJsonText | Out-String) | ConvertFrom-Json

$resolvedOutputDirectory = if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    Join-Path $resolvedProjectPath "infra\$projectSlug\docker"
}
elseif ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
}
else {
    Join-Path $resolvedProjectPath $OutputDirectory
}

if (-not (Test-Path -LiteralPath $resolvedOutputDirectory)) {
    New-Item -ItemType Directory -Path $resolvedOutputDirectory -Force | Out-Null
}

$stackName = if ($Stack -eq "auto") { $report.PrimaryStack.Name } else { $Stack }
if ($stackName -eq "unknown" -or [string]::IsNullOrWhiteSpace($stackName)) {
    throw "Unable to determine stack automatically. Re-run with -Stack explicitly."
}

$databaseName = if ($Database -eq "auto") { $report.Database.Engine } else { $Database }
if ($databaseName -eq "unknown") {
    $databaseName = "none"
}
$databaseNormalized = ([string]$databaseName).Trim().ToLowerInvariant()
switch ($databaseNormalized) {
    "pgsql" { $databaseName = "postgres" }
    "postgresql" { $databaseName = "postgres" }
}

$dbConfig = if ($databaseName -eq "none") { $null } else { New-DbConfig -Engine $databaseName -Slug $projectSlug }
$appCommand = $report.Commands.Start.Value
$commandConfidence = $report.Commands.Start.Confidence
if ($stackName -eq "static" -and $report.Commands.Build.Value -ne "unknown") {
    $appCommand = $report.Commands.Build.Value
    $commandConfidence = $report.Commands.Build.Confidence
}

if ($appCommand -eq "unknown") {
    $appCommand = Get-DefaultCommand -StackName $stackName
    $commandConfidence = "inferred"
}
$appCommand = Sanitize-GeneratedCommand -CommandText $appCommand
if ([string]::IsNullOrWhiteSpace($appCommand)) {
    $appCommand = Get-DefaultCommand -StackName $stackName
    $commandConfidence = "inferred"
}

$dockerfilePath = Join-Path $resolvedOutputDirectory "Dockerfile.generated"
$composePath = Join-Path $resolvedOutputDirectory "docker-compose.generated.yml"
$dockerignorePath = Join-Path $resolvedOutputDirectory ".dockerignore.generated"
$envPath = Join-Path $resolvedOutputDirectory ".env.docker.generated"
$readmePath = Join-Path $resolvedOutputDirectory "README.generated.md"
$existingEnv = Read-GeneratedEnvMap -Path $envPath

$includeOllamaService = $false
if ($IncludeOllama) {
    $includeOllamaService = $true
}
elseif ($IncludeQdrant) {
    # Qdrant memory sync depends on embeddings; ship Ollama by default.
    $includeOllamaService = $true
}
elseif ($existingEnv.ContainsKey("ORCHESTRATOR_LLM_ENABLED") -and (Test-TruthyValue -Value $existingEnv["ORCHESTRATOR_LLM_ENABLED"])) {
    $includeOllamaService = $true
}
elseif ($existingEnv.ContainsKey("OLLAMA_EMBED_URL") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["OLLAMA_EMBED_URL"])) {
    $includeOllamaService = $true
}

foreach ($path in @($dockerfilePath, $composePath, $dockerignorePath, $envPath, $readmePath)) {
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        throw "Refusing to overwrite existing generated artifact: $path. Use -Force to replace it."
    }
}

$relativeContext = Get-RelativeUnixPath -BasePath $resolvedOutputDirectory -TargetPath $resolvedProjectPath
$dockerfileRelativeToProject = Get-RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $dockerfilePath
$appPort = Get-StackPort -StackName $stackName
if ($existingEnv.ContainsKey("APP_PORT")) {
    try {
        $existingPort = [int]$existingEnv["APP_PORT"]
        if ($existingPort -ge 1 -and $existingPort -le 65535) {
            $appPort = $existingPort
        }
    }
    catch {
    }
}

# Normalize PHP artisan serve command for container networking and selected app port.
if ($stackName -eq "php" -and $appCommand -match "^\s*php\s+artisan\s+serve(\s|$)") {
    if ($appCommand -notmatch "--host(?:=|\s)") {
        $appCommand = "$appCommand --host=0.0.0.0"
    }

    if ($appCommand -match "--port(?:=|\s+)\d{2,5}") {
        $appCommand = [regex]::Replace(
            $appCommand,
            "--port(?:=|\s+)\d{2,5}",
            ("--port={0}" -f $appPort),
            1
        )
    }
    elseif ($appCommand -notmatch "--port(?:=|\s)") {
        $appCommand = "$appCommand --port=$appPort"
    }
}

$dockerfileContent = Get-DockerfileContent -StackName $stackName -CommandText $appCommand
$auxiliaryServices = Get-AuxiliaryServicePlan `
    -ProjectRoot $resolvedProjectPath `
    -StackName $stackName `
    -ForceRedis:$IncludeRedis `
    -ForceRabbitMq:$IncludeRabbitMq `
    -ForceWorker:$IncludeWorker

if ($dbConfig) {
    if ($existingEnv.ContainsKey("DB_USER") -and -not [string]::IsNullOrWhiteSpace($existingEnv["DB_USER"])) {
        $dbConfig.User = [string]$existingEnv["DB_USER"]
    }
    if ($existingEnv.ContainsKey("DB_PASSWORD") -and -not [string]::IsNullOrWhiteSpace($existingEnv["DB_PASSWORD"])) {
        $dbConfig.Password = [string]$existingEnv["DB_PASSWORD"]
    }
    if ($existingEnv.ContainsKey("DB_NAME") -and -not [string]::IsNullOrWhiteSpace($existingEnv["DB_NAME"])) {
        $dbConfig.Database = [string]$existingEnv["DB_NAME"]
    }
    if ($dbConfig.Engine -eq "mysql" -and $existingEnv.ContainsKey("DB_ROOT_PASSWORD") -and -not [string]::IsNullOrWhiteSpace($existingEnv["DB_ROOT_PASSWORD"])) {
        $dbConfig.RootPassword = [string]$existingEnv["DB_ROOT_PASSWORD"]
    }
}

$appDependsOnLines = Get-DependencyLines `
    -NeedsDatabase ([bool]$dbConfig) `
    -NeedsRedis $auxiliaryServices.IncludeRedis `
    -NeedsRabbitMq $auxiliaryServices.IncludeRabbitMq `
    -NeedsNeo4j ([bool]$IncludeNeo4j) `
    -NeedsQdrant ([bool]$IncludeQdrant) `
    -NeedsOllama $includeOllamaService

$workerDependsOnLines = Get-DependencyLines `
    -NeedsDatabase ([bool]$dbConfig) `
    -NeedsRedis $auxiliaryServices.IncludeRedis `
    -NeedsRabbitMq $auxiliaryServices.IncludeRabbitMq `
    -NeedsNeo4j ([bool]$IncludeNeo4j) `
    -NeedsQdrant ([bool]$IncludeQdrant) `
    -NeedsOllama $includeOllamaService

$includeCommanderDashboard = if ($existingEnv.ContainsKey("ORCHESTRATOR_DASHBOARD_ENABLED")) {
    Test-TruthyValue -Value $existingEnv["ORCHESTRATOR_DASHBOARD_ENABLED"]
}
else {
    $true
}
$commanderDashboardPort = Get-EnvIntValue -Map $existingEnv -Key "ORCHESTRATOR_STREAMING_SERVER_PORT" -DefaultValue 8765 -Minimum 1024
$orchestratorRootRelativeToCompose = "../../../../../"
$dashboardDependsOnLines = Get-DependencyLines `
    -NeedsApp $true `
    -NeedsDatabase ([bool]$dbConfig) `
    -NeedsRedis $auxiliaryServices.IncludeRedis `
    -NeedsRabbitMq $auxiliaryServices.IncludeRabbitMq `
    -NeedsNeo4j ([bool]$IncludeNeo4j) `
    -NeedsQdrant ([bool]$IncludeQdrant) `
    -NeedsOllama $includeOllamaService

$neo4jPassword = if ($IncludeNeo4j) {
    if ($existingEnv.ContainsKey("NEO4J_PASSWORD") -and -not [string]::IsNullOrWhiteSpace($existingEnv["NEO4J_PASSWORD"])) {
        [string]$existingEnv["NEO4J_PASSWORD"]
    }
    else {
        New-RandomSecret
    }
}
else { "" }
$rabbitMqPassword = if ($auxiliaryServices.IncludeRabbitMq) {
    if ($existingEnv.ContainsKey("RABBITMQ_URL") -and [string]$existingEnv["RABBITMQ_URL"] -match "^[^:]+://[^:]+:([^@]+)@") {
        [string]$matches[1]
    }
    else {
        New-RandomSecret
    }
}
else { "" }

$ollamaImage = if ($existingEnv.ContainsKey("OLLAMA_IMAGE") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["OLLAMA_IMAGE"])) {
    [string]$existingEnv["OLLAMA_IMAGE"]
}
else {
    "ollama/ollama:latest"
}
$ollamaGpuEnabled = if ($existingEnv.ContainsKey("ORCHESTRATOR_OLLAMA_GPU_ENABLED")) {
    Test-TruthyValue -Value $existingEnv["ORCHESTRATOR_OLLAMA_GPU_ENABLED"]
}
else {
    $true
}
$ollamaVramReserveMb = Get-EnvIntValue -Map $existingEnv -Key "ORCHESTRATOR_GPU_VRAM_RESERVE_MB" -DefaultValue 3072 -Minimum 512
$ollamaGpuOverheadBytes = [int64]$ollamaVramReserveMb * 1MB
$ollamaKeepAlive = if ($existingEnv.ContainsKey("OLLAMA_KEEP_ALIVE") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["OLLAMA_KEEP_ALIVE"])) {
    [string]$existingEnv["OLLAMA_KEEP_ALIVE"]
}
else {
    "30m"
}
$ollamaEmbedModel = if ($existingEnv.ContainsKey("OLLAMA_EMBED_MODEL") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["OLLAMA_EMBED_MODEL"])) {
    [string]$existingEnv["OLLAMA_EMBED_MODEL"]
}
else {
    "mxbai-embed-large:latest"
}
$ollamaHostPort = Get-EnvIntValue -Map $existingEnv -Key "OLLAMA_HOST_PORT" -DefaultValue 11435 -Minimum 1024
$ollamaNumParallel = Get-EnvIntValue -Map $existingEnv -Key "OLLAMA_NUM_PARALLEL" -DefaultValue 2 -Minimum 1
$ollamaMaxLoadedModels = Get-EnvIntValue -Map $existingEnv -Key "OLLAMA_MAX_LOADED_MODELS" -DefaultValue 2 -Minimum 1
$ollamaCudaDevices = if ($existingEnv.ContainsKey("CUDA_VISIBLE_DEVICES") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["CUDA_VISIBLE_DEVICES"])) {
    [string]$existingEnv["CUDA_VISIBLE_DEVICES"]
}
else {
    "0"
}
$ollamaNvidiaDevices = if ($existingEnv.ContainsKey("NVIDIA_VISIBLE_DEVICES") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["NVIDIA_VISIBLE_DEVICES"])) {
    [string]$existingEnv["NVIDIA_VISIBLE_DEVICES"]
}
else {
    "all"
}

$neo4jService = if ($IncludeNeo4j) {
@"
  neo4j:
    image: neo4j:5
    container_name: ${projectSlug}-neo4j
    ports:
      - "7474:7474"
      - "7687:7687"
    environment:
      NEO4J_AUTH: neo4j/$neo4jPassword
    volumes:
      - ${projectSlug}-neo4j-data:/data
"@
} else { "" }

$qdrantService = if ($IncludeQdrant) {
@"
  qdrant:
    image: qdrant/qdrant:v1.13.4
    container_name: ${projectSlug}-qdrant
    ports:
      - "6333:6333"
    volumes:
      - ${projectSlug}-qdrant-data:/qdrant/storage
"@
} else { "" }

$ollamaService = if ($includeOllamaService) {
    Get-OllamaServiceContent `
        -Image $ollamaImage `
        -HostPort $ollamaHostPort `
        -EnableGpu $ollamaGpuEnabled `
        -NvidiaVisibleDevices $ollamaNvidiaDevices `
        -CudaVisibleDevices $ollamaCudaDevices `
        -GpuOverheadBytes $ollamaGpuOverheadBytes `
        -NumParallel $ollamaNumParallel `
        -MaxLoadedModels $ollamaMaxLoadedModels `
        -KeepAlive $ollamaKeepAlive
}
else { "" }

$redisService = if ($auxiliaryServices.IncludeRedis) {
    Get-RedisServiceContent
}
else { "" }

$rabbitMqService = if ($auxiliaryServices.IncludeRabbitMq) {
    Get-RabbitMqServiceContent -Password $rabbitMqPassword
}
else { "" }

$workerService = if ($auxiliaryServices.IncludeWorker) {
    Get-WorkerServiceContent `
        -RelativeContext $relativeContext `
        -DockerfileRelativeToProject $dockerfileRelativeToProject `
        -CommandText $auxiliaryServices.WorkerCommand `
        -DependencyLines $workerDependsOnLines
}
else { "" }

$commanderDashboardService = if ($includeCommanderDashboard) {
    Get-CommanderDashboardServiceContent `
        -RelativeContext $relativeContext `
        -OrchestratorRootRelativeToCompose $orchestratorRootRelativeToCompose `
        -HostPort $commanderDashboardPort `
        -DependencyLines $dashboardDependsOnLines
}
else { "" }

$volumeLines = New-Object System.Collections.Generic.List[string]
if ($dbConfig) {
    switch ($dbConfig.Engine) {
        "postgres" { $volumeLines.Add("  ${projectSlug}-postgres-data:") }
        "mysql" { $volumeLines.Add("  ${projectSlug}-mysql-data:") }
        "mongodb" { $volumeLines.Add("  ${projectSlug}-mongo-data:") }
    }
}
if ($auxiliaryServices.IncludeRedis) {
    $volumeLines.Add("  ${projectSlug}-redis-data:")
}
if ($auxiliaryServices.IncludeRabbitMq) {
    $volumeLines.Add("  ${projectSlug}-rabbitmq-data:")
}
if ($IncludeNeo4j) {
    $volumeLines.Add("  ${projectSlug}-neo4j-data:")
}
if ($IncludeQdrant) {
    $volumeLines.Add("  ${projectSlug}-qdrant-data:")
}
if ($includeOllamaService) {
    $volumeLines.Add("  ${projectSlug}-ollama-data:")
}

$composeLines = New-Object System.Collections.Generic.List[string]
$composeLines.Add("name: $projectSlug")
$composeLines.Add("")
$composeLines.Add("services:")
$composeLines.Add("  app:")
$composeLines.Add("    container_name: ${projectSlug}-app")
$composeLines.Add("    build:")
$composeLines.Add("      context: $relativeContext")
$composeLines.Add("      dockerfile: $dockerfileRelativeToProject")
$composeLines.Add("    working_dir: /workspace")
$composeLines.Add("    volumes:")
$composeLines.Add("      - ${relativeContext}:/workspace")
$composeLines.Add("    ports:")
$composeLines.Add("      - `"$appPort`:$appPort`"")
$composeLines.Add("    env_file:")
$composeLines.Add("      - .env.docker.generated")
$composeLines.Add("    command: >-")
$composeLines.Add("      sh -lc `"$appCommand`"")
if (@($appDependsOnLines).Count -gt 0) {
    $appDependsOnLines | ForEach-Object { $composeLines.Add($_) }
}
if ($dbConfig) {
    $composeLines.Add("")
    (Get-DbServiceContent -DbConfig $dbConfig).Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($IncludeNeo4j) {
    $composeLines.Add("")
    $neo4jService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($IncludeQdrant) {
    $composeLines.Add("")
    $qdrantService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($includeOllamaService) {
    $composeLines.Add("")
    $ollamaService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($auxiliaryServices.IncludeRedis) {
    $composeLines.Add("")
    $redisService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($auxiliaryServices.IncludeRabbitMq) {
    $composeLines.Add("")
    $rabbitMqService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($auxiliaryServices.IncludeWorker) {
    $composeLines.Add("")
    $workerService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($includeCommanderDashboard) {
    $composeLines.Add("")
    $commanderDashboardService.Split("`n") | ForEach-Object {
        $composeLines.Add($_.TrimEnd("`r"))
    }
}
if ($volumeLines.Count -gt 0) {
    $composeLines.Add("")
    $composeLines.Add("volumes:")
    $volumeLines | ForEach-Object { $composeLines.Add($_) }
}
$composeLines.Add("")
$composeLines.Add("networks:")
$composeLines.Add("  default:")
$composeLines.Add("    name: ${projectSlug}-net")

$dockerignoreContent = @"
.git
.github
node_modules
vendor
dist
build
coverage
.next
out
bin
obj
target
.venv
venv
infra
*.log
"@

$envLines = New-Object System.Collections.Generic.List[string]
$envLines.Add("PROJECT_SLUG=$projectSlug")
$envLines.Add("APP_PORT=$appPort")
$envLines.Add("APP_COMMAND=$appCommand")
$envLines.Add("APP_COMMAND_CONFIDENCE=$commandConfidence")
$envLines.Add("STACK=$stackName")
$envLines.Add("DATABASE_ENGINE=$databaseName")
if ($auxiliaryServices.IncludeWorker) {
    $envLines.Add("WORKER_COMMAND=$($auxiliaryServices.WorkerCommand)")
    $envLines.Add("WORKER_COMMAND_CONFIDENCE=$($auxiliaryServices.WorkerConfidence)")
}
if ($dbConfig) {
    $envLines.Add("DB_HOST=db")
    $envLines.Add("DB_PORT=$($dbConfig.Port)")
    $envLines.Add("DB_NAME=$($dbConfig.Database)")
    $envLines.Add("DB_USER=$($dbConfig.User)")
    $envLines.Add("DB_PASSWORD=$($dbConfig.Password)")
}
if ($auxiliaryServices.IncludeRedis) {
    $envLines.Add("REDIS_HOST=redis")
    $envLines.Add("REDIS_PORT=6379")
    $envLines.Add("REDIS_URL=redis://redis:6379")
}
if ($auxiliaryServices.IncludeRabbitMq) {
    $envLines.Add("RABBITMQ_HOST=rabbitmq")
    $envLines.Add("RABBITMQ_PORT=5672")
    $envLines.Add("RABBITMQ_URL=amqp://${projectSlug}:$rabbitMqPassword@rabbitmq:5672")
}
if ($IncludeNeo4j) {
    # Neo4j Community Edition supports only the default "neo4j" database.
    # Use project namespace for isolation instead of per-project database names.
    $neo4jDatabase = "neo4j"
    $envLines.Add("NEO4J_URI=bolt://neo4j:7687")
    $envLines.Add("NEO4J_USERNAME=neo4j")
    $envLines.Add("NEO4J_PASSWORD=$neo4jPassword")
    $envLines.Add("NEO4J_DATABASE=$neo4jDatabase")
    $envLines.Add("NEO4J_PROJECT_NAMESPACE=$projectSlug")
}
if ($IncludeQdrant) {
    $qdrantCollection = "$projectSlug-memory"
    $qdrantVectorSize = if ($existingEnv.ContainsKey("QDRANT_VECTOR_SIZE") -and -not [string]::IsNullOrWhiteSpace($existingEnv["QDRANT_VECTOR_SIZE"])) {
        [string]$existingEnv["QDRANT_VECTOR_SIZE"]
    }
    else {
        "768"
    }
    $envLines.Add("QDRANT_URL=http://qdrant:6333")
    $envLines.Add("QDRANT_COLLECTION_PREFIX=$projectSlug")
    $envLines.Add("QDRANT_COLLECTION=$qdrantCollection")
    $envLines.Add("QDRANT_VECTOR_SIZE=$qdrantVectorSize")
}
if ($includeOllamaService) {
    $llmModel = if ($existingEnv.ContainsKey("ORCHESTRATOR_LLM_MODEL") -and -not [string]::IsNullOrWhiteSpace([string]$existingEnv["ORCHESTRATOR_LLM_MODEL"])) {
        [string]$existingEnv["ORCHESTRATOR_LLM_MODEL"]
    }
    else {
        "ollama/llama3:8b"
    }
    $envLines.Add("ORCHESTRATOR_OLLAMA_SERVICE_ENABLED=1")
    $envLines.Add("ORCHESTRATOR_OLLAMA_GPU_ENABLED=" + $(if ($ollamaGpuEnabled) { "1" } else { "0" }))
    $envLines.Add("ORCHESTRATOR_GPU_VRAM_RESERVE_MB=$ollamaVramReserveMb")
    $envLines.Add("OLLAMA_GPU_OVERHEAD=$ollamaGpuOverheadBytes")
    $envLines.Add("OLLAMA_KEEP_ALIVE=$ollamaKeepAlive")
    $envLines.Add("OLLAMA_EMBED_MODEL=$ollamaEmbedModel")
    $envLines.Add("OLLAMA_NUM_PARALLEL=$ollamaNumParallel")
    $envLines.Add("OLLAMA_MAX_LOADED_MODELS=$ollamaMaxLoadedModels")
    $envLines.Add("OLLAMA_IMAGE=$ollamaImage")
    $envLines.Add("OLLAMA_HOST_PORT=$ollamaHostPort")
    $envLines.Add("OLLAMA_API_BASE=http://127.0.0.1:$ollamaHostPort")
    $envLines.Add("OLLAMA_EMBED_URL=http://127.0.0.1:$ollamaHostPort/v1/embeddings")
    $envLines.Add("OLLAMA_DOCKER_API_BASE=http://ollama:11434")
    $envLines.Add("ORCHESTRATOR_LLM_ENABLED=1")
    $envLines.Add("ORCHESTRATOR_LLM_MODEL=$llmModel")
    $envLines.Add("ORCHESTRATOR_LLM_MODEL_FAST=$llmModel")
    $envLines.Add("ORCHESTRATOR_LLM_API_BASE=http://127.0.0.1:$ollamaHostPort")
    $envLines.Add("ORCHESTRATOR_LLM_API_KEY=ollama")
}
if ($includeCommanderDashboard) {
    $envLines.Add("ORCHESTRATOR_DASHBOARD_ENABLED=1")
    $envLines.Add("ORCHESTRATOR_STREAMING_SERVER_PORT=$commanderDashboardPort")
}
else {
    $envLines.Add("ORCHESTRATOR_DASHBOARD_ENABLED=0")
}

$readmeLines = New-Object System.Collections.Generic.List[string]
$readmeLines.Add("# Generated Docker Factory Assets")
$readmeLines.Add("")
$readmeLines.Add("- Generated At: $(Get-Date -Format s)")
$readmeLines.Add("- Project Path: $resolvedProjectPath")
$readmeLines.Add("- Intake Mode: $($report.Classification.Mode)")
$readmeLines.Add("- Primary Stack: $stackName")
$readmeLines.Add("- Database: $databaseName")
$readmeLines.Add("- App Command: $appCommand")
$readmeLines.Add("- Command Confidence: $commandConfidence")
if ($auxiliaryServices.IncludeWorker) {
    $readmeLines.Add("- Worker Command: $($auxiliaryServices.WorkerCommand)")
    $readmeLines.Add("- Worker Confidence: $($auxiliaryServices.WorkerConfidence)")
}
$readmeLines.Add("")
$readmeLines.Add("## Review Notes")
if ($commandConfidence -ne "verified") {
    $readmeLines.Add("- Review the generated app command before using it in production.")
}
if ($report.Database.Confidence -ne "verified" -and $databaseName -ne "none") {
    $readmeLines.Add("- Database engine was not fully verified from repository evidence.")
}
if ($report.Classification.Mode -eq "legacy") {
    $readmeLines.Add("- Repository is classified as legacy. Generated assets are scaffolding, not approval to replace an existing setup.")
    $readmeLines.Add("- Refactor Gate: $($report.RefactorGate)")
}
if ($auxiliaryServices.IncludeRedis) {
    $readmeLines.Add("- Redis was included because queue/cache signals were detected.")
}
if ($auxiliaryServices.IncludeRabbitMq) {
    $readmeLines.Add("- RabbitMQ was included because AMQP signals were detected.")
}
if ($auxiliaryServices.IncludeWorker) {
    $readmeLines.Add("- Worker service was generated from detected background processing signals: $($auxiliaryServices.WorkerReason)")
}
if ($IncludeNeo4j) {
    $readmeLines.Add("- Neo4j is included for graph memory. Use a project namespace or dedicated database.")
}
if ($IncludeQdrant) {
    $readmeLines.Add("- Qdrant is included for vector retrieval. Use collection names prefixed with the project slug.")
}
$readmeLines.Add("- Ollama service (local LLM/embeddings) included: $includeOllamaService")
if ($includeOllamaService) {
    $readmeLines.Add("- Ollama GPU mode: $ollamaGpuEnabled | VRAM reserve MB: $ollamaVramReserveMb")
}
$readmeLines.Add("- Commander dashboard service included: $includeCommanderDashboard")
if ($includeCommanderDashboard) {
    $readmeLines.Add("- Commander dashboard port: $commanderDashboardPort")
}
$readmeLines.Add("")
$readmeLines.Add("## Files")
$readmeLines.Add("- Dockerfile.generated")
$readmeLines.Add("- docker-compose.generated.yml")
$readmeLines.Add("- .dockerignore.generated")
$readmeLines.Add("- .env.docker.generated")
$readmeLines.Add("- Auxiliary services: redis=$($auxiliaryServices.IncludeRedis), rabbitmq=$($auxiliaryServices.IncludeRabbitMq), worker=$($auxiliaryServices.IncludeWorker), ollama=$includeOllamaService")
$readmeLines.Add("")
$readmeLines.Add("## Next Steps")
$readmeLines.Add("1. Review the generated command, worker command, and database values.")
$readmeLines.Add("2. Compare with any existing Docker assets before replacing anything.")
$readmeLines.Add('3. Start with: `docker compose -f docker-compose.generated.yml up --build`')

[System.IO.File]::WriteAllText($dockerfilePath, $dockerfileContent)
[System.IO.File]::WriteAllText($composePath, ($composeLines -join [Environment]::NewLine))
[System.IO.File]::WriteAllText($dockerignorePath, $dockerignoreContent)
[System.IO.File]::WriteAllText($envPath, ($envLines -join [Environment]::NewLine))
[System.IO.File]::WriteAllText($readmePath, ($readmeLines -join [Environment]::NewLine))

Write-Output "Generated Docker assets in $resolvedOutputDirectory"

