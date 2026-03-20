<#
.SYNOPSIS
    V2 Technical Intake - fingerprints and classifies a project as new, existing, or legacy.
.DESCRIPTION
    Performs deep technical analysis of a project directory using semantic (AST) and heuristic
    dependency detection. Produces intake-report.md, project-state.json, architecture-report.md,
    dependency-graph.md, and code-quality.md under .ai-orchestrator/.
    Classification: new (greenfield) | existing (active project) | legacy (high uncertainty/debt).
    Falls back from semantic to heuristic mode if Python/AST tools are unavailable.
.PARAMETER ProjectPath
    Path to the project root to analyze. Defaults to current directory.
.PARAMETER OutputPath
    Override output directory. Defaults to <ProjectPath>/.ai-orchestrator/.
.PARAMETER RefactorPolicy
    Legacy refactor policy: unknown | maintain | gradual-refactor | full-refactor
.EXAMPLE
    .\scripts\v2\Invoke-UniversalIntakeV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-UniversalIntakeV2.ps1 -ProjectPath C:\projects\myapp -RefactorPolicy gradual-refactor
#>param(
    [string]$ProjectPath = ".",
    [string]$OutputPath,
    [ValidateSet("unknown", "maintain", "gradual-refactor", "full-refactor")]
    [string]$RefactorPolicy = "unknown",
    [string]$AuditFindingPath,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2JsonFile {
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

function Get-V2TextFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }

    return Get-Content -LiteralPath $Path -Raw
}

function Test-V2ObjectProperty {
    param(
        [object]$InputObject,
        [string]$Name
    )

    return ($null -ne $InputObject -and $InputObject.PSObject.Properties.Name -contains $Name)
}

function Get-V2ObjectProperty {
    param(
        [object]$InputObject,
        [string]$Name
    )

    if (Test-V2ObjectProperty -InputObject $InputObject -Name $Name) {
        return $InputObject.$Name
    }

    return $null
}

function Get-FirstMatches {
    param(
        [string[]]$Paths,
        [string]$Pattern,
        [int]$Limit = 8
    )

    return @($Paths | Where-Object { $_ -match $Pattern } | Select-Object -First $Limit)
}

function Get-RelativeProjectFiles {
    param([string]$RootPath)

    $excludePatterns = @(
        '(^|/)\.git/',
        '(^|/)node_modules/',
        '(^|/)vendor/',
        '(^|/)dist/',
        '(^|/)build/',
        '(^|/)coverage/',
        '(^|/)\.next/',
        '(^|/)out/',
        '(^|/)target/',
        '(^|/)bin/',
        '(^|/)obj/',
        '(^|/)\.venv/',
        '(^|/)venv/',
        '(^|/)__pycache__/',
        '(^|/)pytest-cache-files-[^/]+$',
        '(^|/)\.pytest_cache/',
        '(^|/)workspace/',
        '(^|/)ai-orchestrator/',
        '(^|/)infra/.+/docker/'
    )

    $files = New-Object System.Collections.Generic.List[string]
    $enumeratedFiles = @(Get-ChildItem -LiteralPath $RootPath -Recurse -File -Force -ErrorAction SilentlyContinue)
    foreach ($file in $enumeratedFiles) {
        $relativePath = Get-V2RelativeUnixPath -BasePath $RootPath -TargetPath $file.FullName
        $skip = $false
        foreach ($pattern in $excludePatterns) {
            if ($relativePath -match $pattern) {
                $skip = $true
                break
            }
        }
        if (-not $skip) {
            $files.Add($relativePath)
        }
    }

    return @($files)
}

function Get-TopLevelModuleName {
    param([string]$RelativePath)

    $normalized = Get-V2NormalizedPath -Path $RelativePath
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return "root"
    }

    $parts = $normalized.Split("/")
    if ($parts.Count -gt 1) {
        return $parts[0]
    }

    return "root"
}

function Get-LanguageSignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths
    )

    $counts = [ordered]@{
        python     = 0
        node       = 0
        java       = 0
        go         = 0
        rust       = 0
        csharp     = 0
        php        = 0
        ruby       = 0
        cpp        = 0
        kotlin     = 0
        swift      = 0
        powershell = 0
    }

    foreach ($path in $RelativePaths) {
        switch -Regex ($path) {
            "\.py$" { $counts.python += 1; continue }
            "\.(ts|tsx|js|jsx|mjs|cjs)$" { $counts.node += 1; continue }
            "\.java$" { $counts.java += 1; continue }
            "\.go$" { $counts.go += 1; continue }
            "\.rs$" { $counts.rust += 1; continue }
            "\.cs$" { $counts.csharp += 1; continue }
            "\.php$" { $counts.php += 1; continue }
            "\.rb$" { $counts.ruby += 1; continue }
            "\.(cpp|cc|cxx|hpp|h)$" { $counts.cpp += 1; continue }
            "\.kt$" { $counts.kotlin += 1; continue }
            "\.swift$" { $counts.swift += 1; continue }
            "\.(ps1|psm1)$" { $counts.powershell += 1; continue }
        }
    }

    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)package\.json$") { $counts.node += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)(requirements\.txt|pyproject\.toml|Pipfile)$") { $counts.python += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)(pom\.xml|build\.gradle|build\.gradle\.kts)$") { $counts.java += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)go\.mod$") { $counts.go += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)Cargo\.toml$") { $counts.rust += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/).+\.csproj$") { $counts.csharp += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)composer\.json$") { $counts.php += 12 }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)Gemfile$") { $counts.ruby += 12 }

    $sorted = @($counts.GetEnumerator() | Sort-Object Value -Descending)
    $detected = @($sorted | Where-Object { $_.Value -gt 0 } | ForEach-Object {
        [PSCustomObject]@{
            language = $_.Key
            score    = $_.Value
        }
    })

    return [PSCustomObject]@{
        detected = $detected
        primary  = if ($detected.Count -gt 0) { $detected[0].language } else { "unknown" }
    }
}

function Get-FrameworkSignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths
    )

    $frameworks = New-Object System.Collections.Generic.List[string]
    $frontend = New-Object System.Collections.Generic.List[string]
    $infra = New-Object System.Collections.Generic.List[string]
    $testFrameworks = New-Object System.Collections.Generic.List[string]

    $packageJson = Get-V2JsonFile -Path (Join-Path $ProjectRoot "package.json")
    $composerJson = Get-V2JsonFile -Path (Join-Path $ProjectRoot "composer.json")
    $pyproject = Get-V2TextFile -Path (Join-Path $ProjectRoot "pyproject.toml")
    $requirements = Get-V2TextFile -Path (Join-Path $ProjectRoot "requirements.txt")
    $gemfile = Get-V2TextFile -Path (Join-Path $ProjectRoot "Gemfile")
    $cargo = Get-V2TextFile -Path (Join-Path $ProjectRoot "Cargo.toml")
    $goMod = Get-V2TextFile -Path (Join-Path $ProjectRoot "go.mod")

    $nodeDeps = New-Object System.Collections.Generic.List[string]
    if ($packageJson) {
        foreach ($sectionName in @("dependencies", "devDependencies")) {
            $section = Get-V2ObjectProperty -InputObject $packageJson -Name $sectionName
            if ($section) {
                foreach ($name in $section.PSObject.Properties.Name) {
                    $nodeDeps.Add($name)
                }
            }
        }
    }

    if ($nodeDeps -contains "next") { $frameworks.Add("Next.js"); $frontend.Add("React") }
    if ($nodeDeps -contains "react") { $frameworks.Add("React"); $frontend.Add("React") }
    if ($nodeDeps -contains "vue") { $frameworks.Add("Vue"); $frontend.Add("Vue") }
    if ($nodeDeps -contains "svelte") { $frameworks.Add("Svelte"); $frontend.Add("Svelte") }
    if ($nodeDeps -contains "angular") { $frameworks.Add("Angular"); $frontend.Add("Angular") }
    if ($nodeDeps -contains "@nestjs/core") { $frameworks.Add("NestJS") }
    if ($nodeDeps -contains "express") { $frameworks.Add("Express") }
    if ($nodeDeps -contains "fastify") { $frameworks.Add("Fastify") }
    if ($nodeDeps -contains "vitest") { $testFrameworks.Add("Vitest") }
    if ($nodeDeps -contains "jest") { $testFrameworks.Add("Jest") }
    if ($nodeDeps -contains "playwright") { $testFrameworks.Add("Playwright") }

    if ($pyproject -match "django" -or $requirements -match "(?im)^django") { $frameworks.Add("Django") }
    if ($pyproject -match "fastapi" -or $requirements -match "(?im)^fastapi") { $frameworks.Add("FastAPI") }
    if ($pyproject -match "flask" -or $requirements -match "(?im)^flask") { $frameworks.Add("Flask") }
    if ($pyproject -match "pytest" -or $requirements -match "(?im)^pytest") { $testFrameworks.Add("Pytest") }

    if ($composerJson) {
        $requireSection = Get-V2ObjectProperty -InputObject $composerJson -Name "require"
        if ($requireSection) {
            $deps = @($requireSection.PSObject.Properties.Name)
            if ($deps -contains "laravel/framework") { $frameworks.Add("Laravel") }
            if (@($deps | Where-Object { $_ -like "symfony/*" }).Count -gt 0) { $frameworks.Add("Symfony") }
            if ($deps -contains "phpunit/phpunit") { $testFrameworks.Add("PHPUnit") }
        }
    }

    if ($gemfile -match "rails") { $frameworks.Add("Ruby on Rails") }
    if ($gemfile -match "rspec") { $testFrameworks.Add("RSpec") }

    if ($goMod -match "gin-gonic/gin") { $frameworks.Add("Gin") }
    if ($goMod -match "gofiber/fiber") { $frameworks.Add("Fiber") }
    if ($goMod -match "testify") { $testFrameworks.Add("Testify") }

    if ($cargo -match "actix-web") { $frameworks.Add("Actix Web") }
    if ($cargo -match "axum") { $frameworks.Add("Axum") }
    if ($cargo -match "tokio") { $frameworks.Add("Tokio") }

    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)(Dockerfile|docker-compose.*\.ya?ml|compose\.ya?ml)$") { $infra.Add("Docker") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)(terraform\.tf|main\.tf)$") { $infra.Add("Terraform") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)helm/|(^|/)charts/") { $infra.Add("Helm") }

    return [PSCustomObject]@{
        frameworks      = @($frameworks | Select-Object -Unique)
        frontend        = @($frontend | Select-Object -Unique)
        infra_tools     = @($infra | Select-Object -Unique)
        test_frameworks = @($testFrameworks | Select-Object -Unique)
    }
}

function Get-CommandHints {
    param(
        [string]$ProjectRoot,
        [string]$PrimaryLanguage,
        [string[]]$RelativePaths
    )

    $packageJson = Get-V2JsonFile -Path (Join-Path $ProjectRoot "package.json")
    $scripts = if ($packageJson) { Get-V2ObjectProperty -InputObject $packageJson -Name "scripts" } else { $null }
    $composerJson = Get-V2JsonFile -Path (Join-Path $ProjectRoot "composer.json")
    $composerScripts = if ($composerJson) { Get-V2ObjectProperty -InputObject $composerJson -Name "scripts" } else { $null }

    $hints = [ordered]@{
        install = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
        build   = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
        run     = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
        test    = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
    }

    switch ($PrimaryLanguage) {
        "node" {
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)package-lock\.json$") { $hints.install = [PSCustomObject]@{ value = "npm ci"; confidence = "verified" } }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pnpm-lock\.yaml$") { $hints.install = [PSCustomObject]@{ value = "pnpm install --frozen-lockfile"; confidence = "verified" } }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)yarn\.lock$") { $hints.install = [PSCustomObject]@{ value = "yarn install --frozen-lockfile"; confidence = "verified" } }
            elseif ($packageJson) { $hints.install = [PSCustomObject]@{ value = "npm install"; confidence = "inferred" } }

            if ((Get-V2ObjectProperty -InputObject $scripts -Name "dev")) { $hints.run = [PSCustomObject]@{ value = "npm run dev"; confidence = "verified" } }
            elseif ((Get-V2ObjectProperty -InputObject $scripts -Name "start")) { $hints.run = [PSCustomObject]@{ value = "npm run start"; confidence = "verified" } }
            if ((Get-V2ObjectProperty -InputObject $scripts -Name "build")) { $hints.build = [PSCustomObject]@{ value = "npm run build"; confidence = "verified" } }
            if ((Get-V2ObjectProperty -InputObject $scripts -Name "test")) { $hints.test = [PSCustomObject]@{ value = "npm test"; confidence = "verified" } }
        }
        "python" {
            $requirementsPath = Join-Path $ProjectRoot "requirements.txt"
            $requirementsContent = Get-V2TextFile -Path $requirementsPath
            if ($null -eq $requirementsContent) { $requirementsContent = "" }
            $hasPytestDependency = $requirementsContent -match "(?im)^pytest([<>=!~].*)?$"
            $hasTestsDir = ($RelativePaths | Where-Object { $_ -match "(^|/)tests?/" } | Measure-Object).Count -gt 0
            $hasPythonSources = ($RelativePaths | Where-Object { $_ -match "\.py$" } | Measure-Object).Count -gt 0

            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)requirements\.txt$") { $hints.install = [PSCustomObject]@{ value = "pip install -r requirements.txt"; confidence = "verified" } }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pyproject\.toml$") { $hints.install = [PSCustomObject]@{ value = "pip install ."; confidence = "inferred" } }

            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)manage\.py$") {
                $hints.run = [PSCustomObject]@{ value = "python manage.py runserver"; confidence = "verified" }
                $hints.test = [PSCustomObject]@{ value = "python manage.py test"; confidence = "verified" }
            }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)main\.py$") {
                $hints.run = [PSCustomObject]@{ value = "python main.py"; confidence = "inferred" }
            }

            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pytest\.ini$|(^|/)conftest\.py$") {
                $hints.test = [PSCustomObject]@{ value = "pytest"; confidence = "verified" }
            }
            elseif ($hasTestsDir -and $hasPytestDependency) {
                $hints.test = [PSCustomObject]@{ value = "python -m pytest"; confidence = "inferred" }
            }

            if ($hasPythonSources) {
                if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)app/") {
                    $hints.build = [PSCustomObject]@{ value = "python -m compileall app"; confidence = "verified" }
                }
                else {
                    $hints.build = [PSCustomObject]@{ value = "python -m compileall ."; confidence = "verified" }
                }
            }
        }
        "php" {
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)composer\.json$") { $hints.install = [PSCustomObject]@{ value = "composer install"; confidence = "verified" } }
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)artisan$") {
                $hints.run = [PSCustomObject]@{ value = "php artisan serve --host=0.0.0.0 --port=8000"; confidence = "verified" }
                $hints.build = [PSCustomObject]@{ value = "php artisan test --testsuite=Unit"; confidence = "verified" }
                $hints.test = [PSCustomObject]@{ value = "php artisan test"; confidence = "verified" }
            }
            elseif ((Get-V2ObjectProperty -InputObject $composerScripts -Name "test")) {
                $hints.build = [PSCustomObject]@{ value = "composer test"; confidence = "verified" }
                $hints.test = [PSCustomObject]@{ value = "composer test"; confidence = "verified" }
            }
        }
        "go" {
            $hints.install = [PSCustomObject]@{ value = "go mod download"; confidence = "verified" }
            $hints.build = [PSCustomObject]@{ value = "go build ./..."; confidence = "verified" }
            $hints.run = [PSCustomObject]@{ value = "go run ."; confidence = "inferred" }
            $hints.test = [PSCustomObject]@{ value = "go test ./..."; confidence = "verified" }
        }
        "java" {
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)gradlew(\.bat)?$") {
                $hints.install = [PSCustomObject]@{ value = ".\gradlew.bat dependencies"; confidence = "verified" }
                $hints.build = [PSCustomObject]@{ value = ".\gradlew.bat build"; confidence = "verified" }
                $hints.run = [PSCustomObject]@{ value = ".\gradlew.bat bootRun"; confidence = "inferred" }
                $hints.test = [PSCustomObject]@{ value = ".\gradlew.bat test"; confidence = "verified" }
            }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)mvnw(\.cmd)?$") {
                $hints.install = [PSCustomObject]@{ value = ".\mvnw dependency:resolve"; confidence = "verified" }
                $hints.build = [PSCustomObject]@{ value = ".\mvnw package"; confidence = "verified" }
                $hints.run = [PSCustomObject]@{ value = ".\mvnw spring-boot:run"; confidence = "inferred" }
                $hints.test = [PSCustomObject]@{ value = ".\mvnw test"; confidence = "verified" }
            }
        }
        "csharp" {
            $hints.install = [PSCustomObject]@{ value = "dotnet restore"; confidence = "verified" }
            $hints.build = [PSCustomObject]@{ value = "dotnet build"; confidence = "verified" }
            $hints.run = [PSCustomObject]@{ value = "dotnet run"; confidence = "verified" }
            $hints.test = [PSCustomObject]@{ value = "dotnet test"; confidence = "verified" }
        }
        "ruby" {
            $hints.install = [PSCustomObject]@{ value = "bundle install"; confidence = "verified" }
            $hints.run = [PSCustomObject]@{ value = "bundle exec rails server"; confidence = "inferred" }
            $hints.test = [PSCustomObject]@{ value = "bundle exec rspec"; confidence = "inferred" }
        }
        "rust" {
            $hints.install = [PSCustomObject]@{ value = "cargo fetch"; confidence = "verified" }
            $hints.build = [PSCustomObject]@{ value = "cargo build"; confidence = "verified" }
            $hints.run = [PSCustomObject]@{ value = "cargo run"; confidence = "verified" }
            $hints.test = [PSCustomObject]@{ value = "cargo test"; confidence = "verified" }
        }
        "powershell" {
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)orchestrator\.ps1$") {
                $hints.run = [PSCustomObject]@{ value = "powershell -ExecutionPolicy Bypass -File .\orchestrator.ps1"; confidence = "inferred" }
            }
            elseif (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)scripts/.+\.ps1$") {
                $hints.run = [PSCustomObject]@{ value = "powershell -ExecutionPolicy Bypass -File .\scripts\orchestrator.ps1"; confidence = "inferred" }
            }
            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)tests?/.*\.tests\.ps1$") {
                $hints.test = [PSCustomObject]@{ value = "Invoke-Pester"; confidence = "inferred" }
            }
        }
    }

    return [PSCustomObject]$hints
}

function Get-DatabaseSignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths
    )

    $envFiles = @(
        (Join-Path $ProjectRoot ".env"),
        (Join-Path $ProjectRoot ".env.local"),
        (Join-Path $ProjectRoot ".env.example")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    foreach ($envFile in $envFiles) {
        $content = Get-V2TextFile -Path $envFile
        if ($null -eq $content) { $content = "" }
        if ($content -match "(?im)^DB_CONNECTION\s*=\s*([a-zA-Z0-9_]+)") {
            return [PSCustomObject]@{
                engine     = $matches[1].ToLowerInvariant()
                confidence = "verified"
                evidence   = Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envFile
            }
        }
        if ($content -match "(?im)^DATABASE_URL\s*=\s*.*(postgres|postgresql)") {
            return [PSCustomObject]@{ engine = "postgres"; confidence = "verified"; evidence = (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envFile) }
        }
        if ($content -match "(?im)^DATABASE_URL\s*=\s*.*mysql") {
            return [PSCustomObject]@{ engine = "mysql"; confidence = "verified"; evidence = (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envFile) }
        }
        if ($content -match "(?im)^DATABASE_URL\s*=\s*.*mongodb") {
            return [PSCustomObject]@{ engine = "mongodb"; confidence = "verified"; evidence = (Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envFile) }
        }
    }

    $orchestratorEnvPath = Join-Path $ProjectRoot "ai-orchestrator/docker/.env.docker.generated"
    if (Test-Path -LiteralPath $orchestratorEnvPath -PathType Leaf) {
        $orchestratorEnvContent = Get-V2TextFile -Path $orchestratorEnvPath
        if ($null -eq $orchestratorEnvContent) { $orchestratorEnvContent = "" }
        if ($orchestratorEnvContent -match "(?im)^DATABASE_ENGINE\s*=\s*([a-zA-Z0-9_]+)") {
            $engineValue = [string]$matches[1]
            switch ($engineValue.ToLowerInvariant()) {
                "postgres" { return [PSCustomObject]@{ engine = "postgres"; confidence = "inferred"; evidence = "ai-orchestrator/docker/.env.docker.generated DATABASE_ENGINE" } }
                "mysql" { return [PSCustomObject]@{ engine = "mysql"; confidence = "inferred"; evidence = "ai-orchestrator/docker/.env.docker.generated DATABASE_ENGINE" } }
                "mongodb" { return [PSCustomObject]@{ engine = "mongodb"; confidence = "inferred"; evidence = "ai-orchestrator/docker/.env.docker.generated DATABASE_ENGINE" } }
            }
        }
        if ($orchestratorEnvContent -match "(?im)^DB_PORT\s*=\s*5432") {
            return [PSCustomObject]@{ engine = "postgres"; confidence = "inferred"; evidence = "ai-orchestrator/docker/.env.docker.generated DB_PORT=5432" }
        }
        if ($orchestratorEnvContent -match "(?im)^DB_PORT\s*=\s*3306") {
            return [PSCustomObject]@{ engine = "mysql"; confidence = "inferred"; evidence = "ai-orchestrator/docker/.env.docker.generated DB_PORT=3306" }
        }
    }

    $packageJson = Get-V2JsonFile -Path (Join-Path $ProjectRoot "package.json")
    if ($packageJson) {
        $deps = New-Object System.Collections.Generic.List[string]
        foreach ($section in @("dependencies", "devDependencies")) {
            $part = Get-V2ObjectProperty -InputObject $packageJson -Name $section
            if ($part) {
                foreach ($name in $part.PSObject.Properties.Name) {
                    $deps.Add($name)
                }
            }
        }

        if ($deps -contains "pg" -or $deps -contains "prisma") { return [PSCustomObject]@{ engine = "postgres"; confidence = "inferred"; evidence = "package.json dependencies" } }
        if ($deps -contains "mysql2") { return [PSCustomObject]@{ engine = "mysql"; confidence = "inferred"; evidence = "package.json dependencies" } }
        if ($deps -contains "mongodb" -or $deps -contains "mongoose") { return [PSCustomObject]@{ engine = "mongodb"; confidence = "inferred"; evidence = "package.json dependencies" } }
        if ($deps -contains "ioredis" -or $deps -contains "redis") { return [PSCustomObject]@{ engine = "redis"; confidence = "inferred"; evidence = "package.json dependencies" } }
    }

    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)database/migrations/") { return [PSCustomObject]@{ engine = "sql"; confidence = "inferred"; evidence = "database/migrations" } }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)schema\.prisma$") { return [PSCustomObject]@{ engine = "postgres"; confidence = "inferred"; evidence = "schema.prisma" } }

    return [PSCustomObject]@{
        engine     = "unknown"
        confidence = "unknown"
        evidence   = "none"
    }
}

function Get-ArchitectureSignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths
    )

    $topDirectories = @(
        Get-ChildItem -LiteralPath $ProjectRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notin @(".git", "node_modules", "vendor", "ai-orchestrator", "workspace", "infra") }
    )

    $serviceDirs = New-Object System.Collections.Generic.List[string]
    foreach ($directory in $topDirectories) {
        $manifestFound = Get-ChildItem -LiteralPath $directory.FullName -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -in @("package.json", "go.mod", "Cargo.toml", "composer.json", "pyproject.toml", "requirements.txt", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile") } |
            Select-Object -First 1
        if ($manifestFound) {
            $serviceDirs.Add($directory.Name)
        }
    }

    $isMicroservices = $serviceDirs.Count -ge 2

    $apiPatterns = New-Object System.Collections.Generic.List[string]
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)routes/api\.(php|ts|js)$|(^|/)controllers?/") { $apiPatterns.Add("REST") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)schema\.graphql$|(^|/).*\.gql$") { $apiPatterns.Add("GraphQL") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/).*\.proto$") { $apiPatterns.Add("gRPC/Proto") }

    return [PSCustomObject]@{
        pattern           = if ($isMicroservices) { "microservices" } else { "monolith" }
        service_structure = @($serviceDirs)
        api_patterns      = @($apiPatterns | Select-Object -Unique)
    }
}

function Get-CIAndBuildSignals {
    param([string[]]$RelativePaths)

    $ci = New-Object System.Collections.Generic.List[string]
    $buildSystems = New-Object System.Collections.Generic.List[string]
    $packageManagers = New-Object System.Collections.Generic.List[string]

    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)\.github/workflows/.*\.ya?ml$") { $ci.Add("GitHub Actions") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)\.gitlab-ci\.yml$") { $ci.Add("GitLab CI") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)Jenkinsfile$") { $ci.Add("Jenkins") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)azure-pipelines\.yml$") { $ci.Add("Azure Pipelines") }

    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)package\.json$") { $buildSystems.Add("npm scripts"); $packageManagers.Add("npm") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pnpm-lock\.yaml$") { $packageManagers.Add("pnpm") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)yarn\.lock$") { $packageManagers.Add("yarn") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)composer\.json$") { $buildSystems.Add("Composer"); $packageManagers.Add("composer") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)go\.mod$") { $buildSystems.Add("Go toolchain"); $packageManagers.Add("go modules") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)Cargo\.toml$") { $buildSystems.Add("Cargo"); $packageManagers.Add("cargo") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pyproject\.toml$|(^|/)requirements\.txt$") { $buildSystems.Add("Python packaging"); $packageManagers.Add("pip") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)pom\.xml$") { $buildSystems.Add("Maven"); $packageManagers.Add("maven") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)build\.gradle(\.kts)?$") { $buildSystems.Add("Gradle"); $packageManagers.Add("gradle") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/).+\.csproj$") { $buildSystems.Add(".NET SDK"); $packageManagers.Add("nuget") }
    if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)Gemfile$") { $buildSystems.Add("Bundler"); $packageManagers.Add("bundler") }

    return [PSCustomObject]@{
        ci_cd            = @($ci | Select-Object -Unique)
        build_systems    = @($buildSystems | Select-Object -Unique)
        package_managers = @($packageManagers | Select-Object -Unique)
    }
}

function Get-ModuleDependencySignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths
    )

    $codePaths = @($RelativePaths | Where-Object { $_ -match "\.(py|ts|tsx|js|jsx|java|go|rs|cs|php|rb|cpp|cc|cxx|kt|swift)$" })
    if ($codePaths.Count -eq 0) {
        return [PSCustomObject]@{
            modules             = @()
            edges               = @()
            edge_count          = 0
            cycle_detected      = $false
            independent_modules = @()
        }
    }

    $moduleList = @($codePaths | ForEach-Object { Get-TopLevelModuleName -RelativePath $_ } | Select-Object -Unique | Sort-Object)
    $moduleSet = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($module in $moduleList) { [void]$moduleSet.Add($module) }

    function Resolve-ImportToModule {
        param(
            [string]$ImportPath,
            [string]$CurrentFile
        )

        if ([string]::IsNullOrWhiteSpace($ImportPath)) {
            return $null
        }

        $candidate = $ImportPath.Trim()
        if ($candidate.StartsWith(".")) {
            $currentDirectory = Split-Path -Parent $CurrentFile
            $absoluteBase = if ([string]::IsNullOrWhiteSpace($currentDirectory)) { $ProjectRoot } else { Join-Path $ProjectRoot $currentDirectory }
            $absoluteTarget = [System.IO.Path]::GetFullPath((Join-Path $absoluteBase $candidate))
            $rootPath = [System.IO.Path]::GetFullPath($ProjectRoot)
            if ($absoluteTarget.StartsWith($rootPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                $relativeTarget = Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $absoluteTarget
                return Get-TopLevelModuleName -RelativePath $relativeTarget
            }
            return $null
        }

        $normalized = $candidate.Replace("\", "/")
        $token = if ($normalized.StartsWith("@")) {
            $segments = $normalized.Split("/")
            if ($segments.Count -ge 2) { "$($segments[0])/$($segments[1])" } else { $segments[0] }
        }
        else {
            ($normalized -split "[/\.]")[0]
        }

        if ($moduleSet.Contains($token)) {
            return $token
        }

        $firstSegment = ($normalized.Split("/")[0])
        if ($moduleSet.Contains($firstSegment)) {
            return $firstSegment
        }

        return $null
    }

    $edgeKeys = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
    $edges = New-Object System.Collections.Generic.List[object]
    foreach ($relativePath in $codePaths) {
        $absolutePath = Join-Path $ProjectRoot $relativePath
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) { continue }

        $sourceModule = Get-TopLevelModuleName -RelativePath $relativePath
        $content = Get-V2TextFile -Path $absolutePath
        if ([string]::IsNullOrWhiteSpace($content)) { continue }

        $importCandidates = New-Object System.Collections.Generic.List[string]
        foreach ($match in [regex]::Matches($content, "(?m)^\s*from\s+([A-Za-z0-9_\.]+)\s+import\s+")) {
            $importCandidates.Add($match.Groups[1].Value)
        }
        foreach ($match in [regex]::Matches($content, "(?m)^\s*import\s+([A-Za-z0-9_\.]+)")) {
            $importCandidates.Add(($match.Groups[1].Value.Split(",")[0].Trim()))
        }
        foreach ($match in [regex]::Matches($content, "(?m)^\s*import\s+.+?\s+from\s+['""]([^'""]+)['""]")) {
            $importCandidates.Add($match.Groups[1].Value)
        }
        foreach ($match in [regex]::Matches($content, "require\(['""]([^'""]+)['""]\)")) {
            $importCandidates.Add($match.Groups[1].Value)
        }
        foreach ($match in [regex]::Matches($content, "(?m)^\s*use\s+([A-Za-z0-9_\\\\]+)\s*;")) {
            $importCandidates.Add($match.Groups[1].Value)
        }
        foreach ($match in [regex]::Matches($content, "(?m)^\s*\""([A-Za-z0-9_\\./-]+)\""")) {
            $importCandidates.Add($match.Groups[1].Value)
        }

        foreach ($candidate in @($importCandidates | Select-Object -Unique)) {
            $targetModule = Resolve-ImportToModule -ImportPath $candidate -CurrentFile $relativePath
            if ([string]::IsNullOrWhiteSpace($targetModule)) { continue }
            if ($targetModule -eq $sourceModule) { continue }

            $edgeKey = "$sourceModule->$targetModule"
            if ($edgeKeys.Add($edgeKey)) {
                $edges.Add([PSCustomObject]@{
                    source = $sourceModule
                    target = $targetModule
                })
            }
        }
    }

    $adjacency = @{}
    foreach ($module in $moduleList) {
        $adjacency[$module] = New-Object System.Collections.Generic.List[string]
    }
    foreach ($edge in $edges) {
        if (-not $adjacency.ContainsKey($edge.source)) {
            $adjacency[$edge.source] = New-Object System.Collections.Generic.List[string]
        }
        $adjacency[$edge.source].Add([string]$edge.target)
    }

    $visited = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
    $inStack = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
    $cycleDetected = $false

    function Visit-DependencyNode {
        param([string]$Node)

        if ($cycleDetected) { return }
        if ($inStack.Contains($Node)) {
            $script:cycleDetected = $true
            return
        }
        if ($visited.Contains($Node)) { return }

        [void]$visited.Add($Node)
        [void]$inStack.Add($Node)
        foreach ($neighbor in @($adjacency[$Node])) {
            Visit-DependencyNode -Node $neighbor
            if ($cycleDetected) { break }
        }
        [void]$inStack.Remove($Node)
    }

    foreach ($module in $moduleList) {
        Visit-DependencyNode -Node $module
        if ($cycleDetected) { break }
    }

    $independentModules = New-Object System.Collections.Generic.List[string]
    foreach ($module in $moduleList) {
        $outbound = @($adjacency[$module]).Count
        $inbound = @($edges | Where-Object { $_.target -eq $module }).Count
        if ($outbound -eq 0 -and $inbound -eq 0) {
            $independentModules.Add($module)
        }
    }

    return [PSCustomObject]@{
        modules             = @($moduleList)
        edges               = @($edges | Select-Object -First 500)
        edge_count          = $edges.Count
        cycle_detected      = $cycleDetected
        independent_modules = @($independentModules)
    }
}

function Get-SemanticDependencySignals {
    param([string]$ProjectRoot)

    $semanticScript = Join-Path $PSScriptRoot "semantic_dependency_scan.py"
    if (-not (Test-Path -LiteralPath $semanticScript -PathType Leaf)) {
        return [PSCustomObject]@{
            enabled = $false
            reason  = "semantic-script-not-found"
        }
    }

    try {
        $output = & python $semanticScript --project-root $ProjectRoot --max-files 6000 --max-errors 120 --max-entities 200
        if ([string]::IsNullOrWhiteSpace(($output | Out-String))) {
            return [PSCustomObject]@{
                enabled = $false
                reason  = "semantic-empty-output"
            }
        }

        $parsed = ($output | Out-String) | ConvertFrom-Json
        if (-not $parsed) {
            return [PSCustomObject]@{
                enabled = $false
                reason  = "semantic-json-parse-failed"
            }
        }

        return [PSCustomObject]@{
            enabled             = $true
            engine              = [string](Get-V2OptionalProperty -InputObject $parsed -Name "engine" -DefaultValue "semantic-v1")
            modules             = @(Get-V2OptionalProperty -InputObject $parsed -Name "modules" -DefaultValue @())
            edges               = @(Get-V2OptionalProperty -InputObject $parsed -Name "edges" -DefaultValue @())
            edge_count          = [int](Get-V2OptionalProperty -InputObject $parsed -Name "edge_count" -DefaultValue 0)
            cycle_detected      = [bool](Get-V2OptionalProperty -InputObject $parsed -Name "cycle_detected" -DefaultValue $false)
            independent_modules = @(Get-V2OptionalProperty -InputObject $parsed -Name "independent_modules" -DefaultValue @())
            summary             = Get-V2OptionalProperty -InputObject $parsed -Name "summary" -DefaultValue ([PSCustomObject]@{})
            entities            = Get-V2OptionalProperty -InputObject $parsed -Name "entities" -DefaultValue ([PSCustomObject]@{
                functions = @()
                classes   = @()
            })
            file_imports        = @(Get-V2OptionalProperty -InputObject $parsed -Name "file_imports" -DefaultValue @())
            errors              = @(Get-V2OptionalProperty -InputObject $parsed -Name "errors" -DefaultValue @())
        }
    }
    catch {
        return [PSCustomObject]@{
            enabled = $false
            reason  = "semantic-scan-failed: $($_.Exception.Message)"
        }
    }
}

function Get-CodeQualitySignals {
    param(
        [string]$ProjectRoot,
        [string[]]$RelativePaths,
        [object]$DependencySignals
    )

    $codePaths = @($RelativePaths | Where-Object { $_ -match "\.(py|ts|tsx|js|jsx|java|go|rs|cs|php|rb|cpp|cc|cxx|kt|swift)$" })
    if ($codePaths.Count -eq 0) {
        return [PSCustomObject]@{
            code_file_count              = 0
            complexity_proxy             = 0
            complexity_rating            = "low"
            large_files                  = @()
            large_file_count             = 0
            duplicate_groups             = @()
            duplicate_group_count        = 0
            dead_code_candidates         = @()
            dead_code_count              = 0
            circular_dependency_detected = [bool](Get-V2OptionalProperty -InputObject $DependencySignals -Name "cycle_detected" -DefaultValue $false)
            vulnerability_signals        = @()
            vulnerability_count          = 0
        }
    }

    $largeFiles = New-Object System.Collections.Generic.List[string]
    $duplicateGroups = New-Object System.Collections.Generic.List[string]
    $deadCodeCandidates = New-Object System.Collections.Generic.List[string]
    $vulnerabilitySignals = New-Object System.Collections.Generic.List[string]
    $hashToFiles = @{}
    $complexityProxy = 0
    $maxLargeFileLines = 500

    foreach ($relativePath in $codePaths) {
        $absolutePath = Join-Path $ProjectRoot $relativePath
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) { continue }

        $content = Get-V2TextFile -Path $absolutePath
        if ($null -eq $content) {
            $content = ""
        }
        $lineCount = if ([string]::IsNullOrWhiteSpace($content)) { 0 } else { (@($content -split "(`r`n|`n|`r)") | Where-Object { $_ -ne "" }).Count }
        if ($lineCount -ge $maxLargeFileLines) {
            $largeFiles.Add("$relativePath ($lineCount lines)")
        }

        $complexityProxy += [Math]::Min([int][Math]::Ceiling($lineCount / 40.0), 25)

        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($content)
            $hash = ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
            if (-not $hashToFiles.ContainsKey($hash)) {
                $hashToFiles[$hash] = New-Object System.Collections.Generic.List[string]
            }
            $hashToFiles[$hash].Add($relativePath)
        }
        finally {
            $sha.Dispose()
        }
    }

    foreach ($entry in $hashToFiles.GetEnumerator()) {
        if ($entry.Value.Count -gt 1) {
            $duplicateGroups.Add(($entry.Value -join " <-> "))
        }
    }

    foreach ($path in $RelativePaths) {
        if ($path -match "(?i)(^|/)(tmp|temp|backup|old|deprecated|archive)/" -or $path -match "(?i)\.(bak|old|tmp|orig)$") {
            $deadCodeCandidates.Add($path)
        }
    }

    if (@($RelativePaths | Where-Object { $_ -eq ".env" }).Count -gt 0) {
        $vulnerabilitySignals.Add("Potential secret exposure: .env file tracked in repository.")
    }

    $scanForSecrets = @($codePaths | Select-Object -First 200)
    foreach ($relativePath in $scanForSecrets) {
        $absolutePath = Join-Path $ProjectRoot $relativePath
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) { continue }
        $content = Get-V2TextFile -Path $absolutePath
        if ($null -eq $content) {
            $content = ""
        }
        if ($content -match "(?im)(api[_-]?key|secret|token|password)\s*[:=]\s*['""][^'""]{8,}['""]") {
            $vulnerabilitySignals.Add("Potential hardcoded credential pattern in $relativePath")
        }
        if ($content -match "BEGIN (RSA|EC|OPENSSH) PRIVATE KEY") {
            $vulnerabilitySignals.Add("Private key material pattern detected in $relativePath")
        }
    }

    $complexityRating = "low"
    if ($complexityProxy -ge 250) {
        $complexityRating = "high"
    }
    elseif ($complexityProxy -ge 90) {
        $complexityRating = "medium"
    }

    return [PSCustomObject]@{
        code_file_count              = $codePaths.Count
        complexity_proxy             = $complexityProxy
        complexity_rating            = $complexityRating
        large_files                  = @($largeFiles | Select-Object -First 100)
        large_file_count             = $largeFiles.Count
        duplicate_groups             = @($duplicateGroups | Select-Object -First 80)
        duplicate_group_count        = $duplicateGroups.Count
        dead_code_candidates         = @($deadCodeCandidates | Select-Object -First 120)
        dead_code_count              = $deadCodeCandidates.Count
        circular_dependency_detected = [bool](Get-V2OptionalProperty -InputObject $DependencySignals -Name "cycle_detected" -DefaultValue $false)
        vulnerability_signals        = @($vulnerabilitySignals | Select-Object -Unique | Select-Object -First 80)
        vulnerability_count          = @($vulnerabilitySignals | Select-Object -Unique).Count
    }
}

function Get-PluginDetections {
    param(
        [string]$PluginDirectory,
        [object]$Context
    )

    $detections = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $PluginDirectory -PathType Container)) {
        return @($detections.ToArray())
    }

    foreach ($pluginPath in Get-ChildItem -LiteralPath $PluginDirectory -Filter "*.ps1" -File -ErrorAction SilentlyContinue) {
        try {
            $plugin = & $pluginPath.FullName
            if (-not $plugin) { continue }
            $requiredKeys = @("Name", "Detect", "BuildHints", "RunHints", "TestHints", "DockerHints", "Confidence")
            $missing = @($requiredKeys | Where-Object { -not ($plugin.PSObject.Properties.Name -contains $_) })
            if ($missing.Count -gt 0) {
                continue
            }

            $detectResult = & $plugin.Detect $Context
            if ($detectResult -and $detectResult.Detected) {
                $detections.Add([PSCustomObject]@{
                    plugin      = $plugin.Name
                    language    = $detectResult.Language
                    confidence  = (& $plugin.Confidence $Context)
                    signals     = @($detectResult.Signals)
                    build_hint  = (& $plugin.BuildHints $Context)
                    run_hint    = (& $plugin.RunHints $Context)
                    test_hint   = (& $plugin.TestHints $Context)
                    docker_hint = (& $plugin.DockerHints $Context)
                })
            }
        }
        catch {
        }
    }

    return @($detections.ToArray())
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$relativePaths = Get-RelativeProjectFiles -RootPath $resolvedProjectPath
$sourceFiles = @($relativePaths | Where-Object { $_ -match "\.(py|ts|tsx|js|jsx|java|go|rs|cs|php|rb|cpp|cc|cxx|kt|swift|ps1|psm1)$" })

$languageSignals = Get-LanguageSignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths
$frameworkSignals = Get-FrameworkSignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths
$commands = Get-CommandHints -ProjectRoot $resolvedProjectPath -PrimaryLanguage $languageSignals.primary -RelativePaths $relativePaths
$database = Get-DatabaseSignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths
$architecture = Get-ArchitectureSignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths
$ciBuild = Get-CIAndBuildSignals -RelativePaths $relativePaths
$heuristicDependencySignals = Get-ModuleDependencySignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths
$semanticDependencySignals = Get-SemanticDependencySignals -ProjectRoot $resolvedProjectPath
$dependencySignals = $heuristicDependencySignals
$dependencyDetectionMode = "heuristic"
$dependencyDetectionReason = "semantic-unavailable"

if ($semanticDependencySignals.enabled) {
    $semanticEdgeCount = [int](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "edge_count" -DefaultValue 0)
    $heuristicEdgeCount = [int](Get-V2OptionalProperty -InputObject $heuristicDependencySignals -Name "edge_count" -DefaultValue 0)

    if ($semanticEdgeCount -gt 0 -or $heuristicEdgeCount -eq 0) {
        $dependencySignals = [PSCustomObject]@{
            modules             = @(Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "modules" -DefaultValue @())
            edges               = @((Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "edges" -DefaultValue @()) | Select-Object -First 500)
            edge_count          = $semanticEdgeCount
            cycle_detected      = [bool](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "cycle_detected" -DefaultValue $false)
            independent_modules = @(Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "independent_modules" -DefaultValue @())
        }
        $dependencyDetectionMode = "semantic"
        $dependencyDetectionReason = "semantic-selected"
    }
    else {
        $dependencyDetectionMode = "heuristic"
        $dependencyDetectionReason = "semantic-zero-edges"
    }
}
else {
    $dependencyDetectionMode = "heuristic"
    $dependencyDetectionReason = [string](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "reason" -DefaultValue "semantic-unavailable")
}

Set-V2DynamicProperty -InputObject $dependencySignals -Name "detection_mode" -Value $dependencyDetectionMode
Set-V2DynamicProperty -InputObject $dependencySignals -Name "detection_reason" -Value $dependencyDetectionReason
Set-V2DynamicProperty -InputObject $dependencySignals -Name "heuristic_edge_count" -Value ([int](Get-V2OptionalProperty -InputObject $heuristicDependencySignals -Name "edge_count" -DefaultValue 0))
Set-V2DynamicProperty -InputObject $dependencySignals -Name "semantic_edge_count" -Value ([int](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "edge_count" -DefaultValue 0))
Set-V2DynamicProperty -InputObject $dependencySignals -Name "semantic_available" -Value ([bool](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "enabled" -DefaultValue $false))
$qualitySignals = Get-CodeQualitySignals -ProjectRoot $resolvedProjectPath -RelativePaths $relativePaths -DependencySignals $dependencySignals

$pluginContext = [PSCustomObject]@{
    ProjectRoot   = $resolvedProjectPath
    RelativePaths = $relativePaths
}
$pluginDetections = Get-PluginDetections -PluginDirectory (Join-Path $PSScriptRoot "plugins") -Context $pluginContext

$documentationFiles = @($relativePaths | Where-Object { $_ -match "(^|/)(README|readme|CHANGELOG|docs/).*(\.md|\.rst)?$" } | Select-Object -First 15)
$dockerFiles = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(Dockerfile|docker-compose.*\.ya?ml|compose\.ya?ml)$" -Limit 12)
$testSignals = @(
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(tests?/|__tests__/|spec/|pytest\.ini|phpunit\.xml|vitest\.config|jest\.config|go\.test|Cargo\.toml)" -Limit 20
)

$riskFactors = New-Object System.Collections.Generic.List[string]
$riskScore = 0

if ($sourceFiles.Count -gt 50 -and $testSignals.Count -eq 0) {
    $riskFactors.Add("Large source footprint without explicit test signals.")
    $riskScore += 2
}
if ($ciBuild.ci_cd.Count -eq 0 -and $sourceFiles.Count -gt 0) {
    $riskFactors.Add("No CI/CD workflow detected.")
    $riskScore += 1
}
if ($dockerFiles.Count -eq 0 -and $sourceFiles.Count -gt 0) {
    $riskFactors.Add("No Docker assets detected.")
    $riskScore += 1
}
if (@($languageSignals.detected).Count -gt 2) {
    $riskFactors.Add("Polyglot repository with more than two active language groups.")
    $riskScore += 1
}
if ($commands.run.value -eq "unknown") {
    $riskFactors.Add("Run command could not be verified.")
    $riskScore += 1
}
if ($commands.test.value -eq "unknown" -and $sourceFiles.Count -gt 0) {
    $riskFactors.Add("Test command could not be verified.")
    $riskScore += 1
}
if ($database.engine -eq "unknown") {
    $riskFactors.Add("Transactional database engine is unknown.")
    $riskScore += 1
}
if ($qualitySignals.circular_dependency_detected) {
    $riskFactors.Add("Circular dependency signal detected between project modules.")
    $riskScore += 2
}
if ($qualitySignals.duplicate_group_count -gt 0) {
    $riskFactors.Add("Potential duplicated code blocks detected.")
    $riskScore += 1
}
if ($qualitySignals.vulnerability_count -gt 0) {
    $riskFactors.Add("Potential credential/security exposure signals detected.")
    $riskScore += 2
}
if ($qualitySignals.large_file_count -gt 10) {
    $riskFactors.Add("Large file concentration suggests high complexity hotspots.")
    $riskScore += 1
}
if (-not [bool](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "enabled" -DefaultValue $false) -and $sourceFiles.Count -gt 0) {
    $riskFactors.Add("Semantic dependency scan unavailable; fallback to heuristic graph.")
}

$manifestCount = @(
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)package\.json$" -Limit 30)).Count,
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)composer\.json$" -Limit 30)).Count,
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pyproject\.toml|requirements\.txt)$" -Limit 30)).Count,
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)go\.mod$" -Limit 30)).Count,
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)Cargo\.toml$" -Limit 30)).Count,
    (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)pom\.xml$|(^|/)build\.gradle(\.kts)?$" -Limit 30)).Count
) | Measure-Object -Sum | Select-Object -ExpandProperty Sum

$legacySignalScore = 0
if ($sourceFiles.Count -ge 200) {
    $legacySignalScore += 2
}
if ($riskScore -ge 5 -and $sourceFiles.Count -ge 40) {
    $legacySignalScore += 2
}
if ($architecture.pattern -eq "microservices" -and $ciBuild.ci_cd.Count -eq 0 -and $testSignals.Count -eq 0) {
    $legacySignalScore += 1
}
if (@(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(legacy|deprecated|archive)/" -Limit 20).Count -gt 0) {
    $legacySignalScore += 1
}

$projectType = "existing"
if ($sourceFiles.Count -eq 0 -and $manifestCount -eq 0) {
    $projectType = "new"
}
elseif ($sourceFiles.Count -gt 0) {
    if ($legacySignalScore -ge 2) {
        $projectType = "legacy"
    }
    else {
        $projectType = "existing"
    }
}
else {
    $projectType = "new"
}

$confidence = "medium"
if ($projectType -eq "new" -and $sourceFiles.Count -eq 0 -and $manifestCount -eq 0) {
    $confidence = "high"
}
elseif ($projectType -eq "existing" -and $languageSignals.primary -ne "unknown" -and $commands.run.value -ne "unknown") {
    $confidence = "high"
}
elseif ($projectType -eq "legacy" -and $legacySignalScore -ge 3) {
    $confidence = "high"
}
elseif ($commands.run.value -eq "unknown" -or $languageSignals.primary -eq "unknown") {
    $confidence = "low"
}

$newProjectRequestPath = Join-Path $resolvedProjectPath "PROJECT_REQUEST.md"
$newProjectRequestContent = Get-V2TextFile -Path $newProjectRequestPath
$newProjectBriefProvided = $false
if (-not [string]::IsNullOrWhiteSpace($newProjectRequestContent)) {
    $requestLines = @($newProjectRequestContent -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($requestLines.Count -ge 4 -or $newProjectRequestContent.Length -ge 200) {
        $newProjectBriefProvided = $true
    }
}

$unknowns = New-Object System.Collections.Generic.List[string]
if ($commands.run.value -eq "unknown" -and $projectType -ne "new") { $unknowns.Add("run-command") }
if ($commands.test.value -eq "unknown" -and $sourceFiles.Count -gt 0) { $unknowns.Add("test-command") }
if ($database.engine -eq "unknown" -and $projectType -ne "new") { $unknowns.Add("database-engine") }
if ($languageSignals.primary -eq "unknown" -and $sourceFiles.Count -gt 0) { $unknowns.Add("primary-language") }
if ($projectType -eq "legacy" -and $RefactorPolicy -eq "unknown") { $unknowns.Add("legacy-refactor-policy") }
if ($confidence -eq "low") { $unknowns.Add("classification-confidence") }

$questions = New-Object System.Collections.Generic.List[string]
if ($projectType -eq "new") {
    if (-not $newProjectBriefProvided) {
        foreach ($q in @(
            "What is the project objective?",
            "What architecture pattern is expected?",
            "What is the main language/framework?",
            "What target infrastructure is expected?",
            "Which database should be used?",
            "What deployment target should be used?"
        )) { $questions.Add($q) }
    }
}
else {
    if ($commands.run.value -eq "unknown") { $questions.Add("What is the verified run command?") }
    if ($commands.test.value -eq "unknown") { $questions.Add("What is the verified test command?") }
    if ($database.engine -eq "unknown") { $questions.Add("Which transactional database engine should this project use?") }
    if ($projectType -eq "legacy" -and $RefactorPolicy -eq "unknown") { $questions.Add("Choose legacy policy: maintain | gradual-refactor | full-refactor") }
}
if ($confidence -eq "low") {
    $questions.Add("Project classification is low confidence. Confirm if this repository should be treated as new, existing, or legacy.")
}

$status = "ready-for-analysis"
if ($unknowns.Count -gt 0) {
    $status = "blocked-waiting-answers"
}
elseif ($projectType -eq "new") {
    $status = "ready-for-scaffold"
}
elseif ($projectType -eq "legacy" -and $RefactorPolicy -eq "unknown") {
    $status = "blocked-waiting-answers"
}

$fingerprintHash = Get-V2FileFingerprint -RootPath $resolvedProjectPath -ExcludeRegexes @(
    '(^|/)\.git/',
    '(^|/)node_modules/',
    '(^|/)vendor/',
    '(^|/)\.pytest_cache/',
    '(^|/)__pycache__/',
    '\.pyc$',
    '\.pyo$',
    '(^|/)workspace/',
    '(^|/)docs/agents/',
    '(^|/)analysis/dependency_graph\.json$',
    '(^|/)ai-orchestrator/',
    '(^|/)infra/.+/docker/'
)

if (-not [string]::IsNullOrWhiteSpace($AuditFindingPath) -and (Test-Path -LiteralPath $AuditFindingPath -PathType Leaf)) {
    try {
        $auditData = Get-Content -LiteralPath $AuditFindingPath -Raw | ConvertFrom-Json
        if ($auditData -and ($auditData.PSObject.Properties.Name -contains "findings")) {
            foreach ($f in $auditData.findings) {
                $findingTitle = [string](Get-V2OptionalProperty -InputObject $f -Name "title" -DefaultValue "manual-finding")
                $riskFactors.Add("Manual Audit Finding: $findingTitle")
                $riskScore += 2
            }
        }
    }
    catch {}
}

$result = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    project_slug = Get-V2ProjectSlug -Name (Split-Path -Leaf $resolvedProjectPath)
    project_path = $resolvedProjectPath
    project_type = $projectType
    confidence   = $confidence
    status       = $status
    refactor_policy = $RefactorPolicy
    technical_fingerprint = [PSCustomObject]@{
        hash                  = $fingerprintHash
        source_file_count     = $sourceFiles.Count
        languages             = @($languageSignals.detected)
        primary_language      = $languageSignals.primary
        frameworks            = @($frameworkSignals.frameworks)
        frontend_frameworks   = @($frameworkSignals.frontend)
        infrastructure_tools  = @($frameworkSignals.infra_tools)
        package_managers      = @($ciBuild.package_managers)
        build_systems         = @($ciBuild.build_systems)
        ci_cd                 = @($ciBuild.ci_cd)
        test_frameworks       = @($frameworkSignals.test_frameworks)
        docker_assets         = @($dockerFiles)
        database              = $database
        architecture_pattern  = $architecture.pattern
        service_structure     = @($architecture.service_structure)
        monolith_vs_microservices = $architecture.pattern
        api_patterns          = @($architecture.api_patterns)
        dependency_graph      = $dependencySignals
        dependency_detection  = [PSCustomObject]@{
            mode                = $dependencyDetectionMode
            reason              = $dependencyDetectionReason
            heuristic_edge_count = [int](Get-V2OptionalProperty -InputObject $heuristicDependencySignals -Name "edge_count" -DefaultValue 0)
            semantic_edge_count = [int](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "edge_count" -DefaultValue 0)
            semantic_available  = [bool](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "enabled" -DefaultValue $false)
            semantic_engine     = [string](Get-V2OptionalProperty -InputObject $semanticDependencySignals -Name "engine" -DefaultValue "semantic-v1")
        }
        code_quality          = $qualitySignals
        legacy_signal_score   = $legacySignalScore
        documentation_signals = @($documentationFiles)
        plugin_detections     = @($pluginDetections)
    }
    verified_commands = [PSCustomObject]@{
        install = $commands.install
        build   = $commands.build
        run     = $commands.run
        test    = $commands.test
    }
    unknowns       = @($unknowns.ToArray())
    open_questions = @($questions.ToArray())
    risk_factors   = @($riskFactors.ToArray())
    analysis       = [PSCustomObject]@{
        dependency_graph = $dependencySignals
        semantic_graph   = $semanticDependencySignals
        code_quality     = $qualitySignals
    }
}

$reportLines = New-Object System.Collections.Generic.List[string]
$reportLines.Add("# Universal Intake V2")
$reportLines.Add("")
$reportLines.Add("- Generated At: $($result.generated_at)")
$reportLines.Add("- Project: $($result.project_slug)")
$reportLines.Add("- Type: $($result.project_type)")
$reportLines.Add("- Confidence: $($result.confidence)")
$reportLines.Add("- Status: $($result.status)")
$reportLines.Add("- Refactor Policy: $($result.refactor_policy)")
$reportLines.Add("- Fingerprint: $($result.technical_fingerprint.hash)")
$reportLines.Add("")
$reportLines.Add("## Commands")
$reportLines.Add("| Action | Value | Confidence |")
$reportLines.Add("|---|---|---|")
$reportLines.Add("| install | $($result.verified_commands.install.value) | $($result.verified_commands.install.confidence) |")
$reportLines.Add("| build | $($result.verified_commands.build.value) | $($result.verified_commands.build.confidence) |")
$reportLines.Add("| run | $($result.verified_commands.run.value) | $($result.verified_commands.run.confidence) |")
$reportLines.Add("| test | $($result.verified_commands.test.value) | $($result.verified_commands.test.confidence) |")
$reportLines.Add("")
$reportLines.Add("## Technical Fingerprint")
$reportLines.Add("- Primary Language: $($result.technical_fingerprint.primary_language)")
$reportLines.Add("- Languages: $((@($result.technical_fingerprint.languages | ForEach-Object { "$($_.language):$($_.score)" }) -join ', '))")
$reportLines.Add("- Frameworks: $((@($result.technical_fingerprint.frameworks) -join ', '))")
$reportLines.Add("- Frontend Frameworks: $((@($result.technical_fingerprint.frontend_frameworks) -join ', '))")
$reportLines.Add("- Architecture Pattern: $($result.technical_fingerprint.architecture_pattern)")
$reportLines.Add("- Legacy Signal Score: $($result.technical_fingerprint.legacy_signal_score)")
$reportLines.Add("- Service Structure: $((@($result.technical_fingerprint.service_structure) -join ', '))")
$reportLines.Add("- API Patterns: $((@($result.technical_fingerprint.api_patterns) -join ', '))")
$reportLines.Add("- Database: $($result.technical_fingerprint.database.engine) ($($result.technical_fingerprint.database.confidence))")
$reportLines.Add("- Build Systems: $((@($result.technical_fingerprint.build_systems) -join ', '))")
$reportLines.Add("- Package Managers: $((@($result.technical_fingerprint.package_managers) -join ', '))")
$reportLines.Add("- CI/CD: $((@($result.technical_fingerprint.ci_cd) -join ', '))")
$reportLines.Add("- Tests: $((@($result.technical_fingerprint.test_frameworks) -join ', '))")
$reportLines.Add("")
$reportLines.Add("## Dependency Graph")
$reportLines.Add("- Detection Mode: $($result.analysis.dependency_graph.detection_mode)")
$reportLines.Add("- Detection Reason: $($result.analysis.dependency_graph.detection_reason)")
$reportLines.Add("- Heuristic Edge Count: $($result.analysis.dependency_graph.heuristic_edge_count)")
$reportLines.Add("- Semantic Edge Count: $($result.analysis.dependency_graph.semantic_edge_count)")
$reportLines.Add("- Modules: $(@($result.analysis.dependency_graph.modules).Count)")
$reportLines.Add("- Edges: $($result.analysis.dependency_graph.edge_count)")
$reportLines.Add("- Circular Dependency Detected: $($result.analysis.dependency_graph.cycle_detected)")
if (@($result.analysis.dependency_graph.edges).Count -gt 0) {
    $reportLines.Add("- Sample Edges:")
    foreach ($edge in @($result.analysis.dependency_graph.edges | Select-Object -First 20)) {
        $reportLines.Add("  - $($edge.source) -> $($edge.target)")
    }
}
else {
    $reportLines.Add("- Sample Edges: none")
}
$reportLines.Add("- Semantic Enabled: $($result.analysis.semantic_graph.enabled)")
if ($result.analysis.semantic_graph.enabled) {
    $summary = Get-V2OptionalProperty -InputObject $result.analysis.semantic_graph -Name "summary" -DefaultValue ([PSCustomObject]@{})
    $reportLines.Add("- Semantic Engine: $($result.analysis.semantic_graph.engine)")
    $reportLines.Add("- Semantic Files Scanned: $(Get-V2OptionalProperty -InputObject $summary -Name 'files_scanned' -DefaultValue 0)")
    $reportLines.Add("- Semantic Parse Errors: $(Get-V2OptionalProperty -InputObject $summary -Name 'parse_errors' -DefaultValue 0)")
    $reportLines.Add("- Semantic Adapters Used: $((@(Get-V2OptionalProperty -InputObject $summary -Name 'adapters_used' -DefaultValue @()) -join ', '))")
}
else {
    $reportLines.Add("- Semantic Reason: $($result.analysis.semantic_graph.reason)")
}
$reportLines.Add("")
$reportLines.Add("## Code Quality")
$reportLines.Add("- Code Files: $($result.analysis.code_quality.code_file_count)")
$reportLines.Add("- Complexity Proxy: $($result.analysis.code_quality.complexity_proxy) ($($result.analysis.code_quality.complexity_rating))")
$reportLines.Add("- Large Files: $($result.analysis.code_quality.large_file_count)")
$reportLines.Add("- Duplicate Groups: $($result.analysis.code_quality.duplicate_group_count)")
$reportLines.Add("- Dead Code Candidates: $($result.analysis.code_quality.dead_code_count)")
$reportLines.Add("- Vulnerability Signals: $($result.analysis.code_quality.vulnerability_count)")
$reportLines.Add("")
$reportLines.Add("## Risks")
if ($result.risk_factors.Count -eq 0) {
    $reportLines.Add("- none identified from static scan")
}
else {
    foreach ($risk in $result.risk_factors) {
        $reportLines.Add("- $risk")
    }
}
$reportLines.Add("")
$reportLines.Add("## Unknowns")
if ($result.unknowns.Count -eq 0) {
    $reportLines.Add("- none")
}
else {
    foreach ($unknown in $result.unknowns) {
        $reportLines.Add("- $unknown")
    }
}
$reportLines.Add("")
$reportLines.Add("## Open Questions")
if ($result.open_questions.Count -eq 0) {
    $reportLines.Add("- none")
}
else {
    foreach ($question in $result.open_questions) {
        $reportLines.Add("- $question")
    }
}

$resolvedOutputPath = if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    Join-Path $resolvedProjectPath "ai-orchestrator/state/intake-report.md"
}
elseif ([System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath
}
else {
    Join-Path $resolvedProjectPath $OutputPath
}

Write-V2File -Path $resolvedOutputPath -Content ($reportLines -join [Environment]::NewLine) -Force

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 14
}
else {
    Write-Output "Wrote V2 intake report to $resolvedOutputPath"
}

