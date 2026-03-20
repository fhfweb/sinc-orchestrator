<#
.SYNOPSIS
    V1 Project Intake — classifies a project as greenfield, existing, or legacy.
.DESCRIPTION
    Scans a project directory for tech stack signals (package manifests, runtime markers,
    test suites, Docker assets, CI/CD configs, environment files) and produces:
    - INTAKE_REPORT.md: classification + confidence + evidence + unknowns
    - Recommended next agent action
    This is the V1 intake tool. For projects already in .ai-orchestrator mode, use
    scripts/v2/Invoke-UniversalIntakeV2.ps1 instead.
.PARAMETER ProjectPath
    Path to the project to analyze. Defaults to current directory.
.PARAMETER OutputPath
    Where to write INTAKE_REPORT.md. Defaults to docs/agents/INTAKE_REPORT.md.
.EXAMPLE
    .\scripts\Invoke-ProjectIntake.ps1 -ProjectPath C:\projects\myapp
    .\scripts\Invoke-ProjectIntake.ps1 -ProjectPath . -OutputPath docs/agents/INTAKE_REPORT.md
#>param(
    [string]$ProjectPath = ".",
    [string]$OutputPath = ".\docs\agents\INTAKE_REPORT.md",
    [switch]$EmitJson
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

function Add-Score {
    param(
        [hashtable]$Scores,
        [string]$Name,
        [int]$Points
    )

    if (-not $Scores.ContainsKey($Name)) {
        $Scores[$Name] = 0
    }

    $Scores[$Name] += $Points
}

function Get-FirstMatches {
    param(
        [string[]]$Paths,
        [string]$Pattern,
        [int]$Limit = 5
    )

    return @($Paths | Where-Object { $_ -match $Pattern } | Select-Object -First $Limit)
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
        return $null
    }

    return Get-Content -LiteralPath $Path -Raw
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

function New-CommandHint {
    param(
        [string]$Value,
        [string]$Confidence
    )

    return [PSCustomObject]@{
        Value      = $Value
        Confidence = $Confidence
    }
}

function Get-CommandHints {
    param(
        [string]$ProjectRoot,
        [string]$PrimaryStack,
        [string[]]$RelativePaths
    )

    $packageJsonPath = Join-Path $ProjectRoot "package.json"
    $packageJson = Get-JsonFile -Path $packageJsonPath
    $composerJson = Get-JsonFile -Path (Join-Path $ProjectRoot "composer.json")
    $pyprojectText = Get-TextFile -Path (Join-Path $ProjectRoot "pyproject.toml")

    $install = New-CommandHint -Value "unknown" -Confidence "unknown"
    $start = New-CommandHint -Value "unknown" -Confidence "unknown"
    $test = New-CommandHint -Value "unknown" -Confidence "unknown"
    $build = New-CommandHint -Value "unknown" -Confidence "unknown"

    switch ($PrimaryStack) {
        "node" {
            if ($packageJson) {
                $scripts = Get-ObjectPropertyValue -InputObject $packageJson -Name "scripts"
                if (Test-Path -LiteralPath (Join-Path $ProjectRoot "package-lock.json")) {
                    $install = New-CommandHint -Value "npm ci" -Confidence "verified"
                }
                elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "pnpm-lock.yaml")) {
                    $install = New-CommandHint -Value "pnpm install --frozen-lockfile" -Confidence "verified"
                }
                elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "yarn.lock")) {
                    $install = New-CommandHint -Value "yarn install --frozen-lockfile" -Confidence "verified"
                }
                else {
                    $install = New-CommandHint -Value "npm install" -Confidence "inferred"
                }

                if ((Get-ObjectPropertyValue -InputObject $scripts -Name "dev")) {
                    $start = New-CommandHint -Value "npm run dev" -Confidence "verified"
                }
                elseif ((Get-ObjectPropertyValue -InputObject $scripts -Name "start")) {
                    $start = New-CommandHint -Value "npm run start" -Confidence "verified"
                }

                if ((Get-ObjectPropertyValue -InputObject $scripts -Name "test")) {
                    $test = New-CommandHint -Value "npm test" -Confidence "verified"
                }

                if ((Get-ObjectPropertyValue -InputObject $scripts -Name "build")) {
                    $build = New-CommandHint -Value "npm run build" -Confidence "verified"
                }
            }
        }
        "python" {
            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "requirements.txt")) {
                $install = New-CommandHint -Value "pip install -r requirements.txt" -Confidence "verified"
            }
            elseif ($pyprojectText) {
                $install = New-CommandHint -Value "pip install ." -Confidence "inferred"
            }

            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "manage.py")) {
                $start = New-CommandHint -Value "python manage.py runserver" -Confidence "verified"
                $test = New-CommandHint -Value "python manage.py test" -Confidence "verified"
            }
            elseif ($pyprojectText -match "fastapi" -or $pyprojectText -match "uvicorn") {
                $start = New-CommandHint -Value "uvicorn main:app --host 0.0.0.0 --port 8000" -Confidence "inferred"
            }
            elseif ($pyprojectText -match "flask") {
                $start = New-CommandHint -Value "flask run --host=0.0.0.0 --port=8000" -Confidence "inferred"
            }

            if (Get-FirstMatches -Paths $RelativePaths -Pattern "(^|/)(pytest\.ini|conftest\.py)$") {
                $test = New-CommandHint -Value "pytest" -Confidence "verified"
            }
        }
        "php" {
            if ($composerJson) {
                $install = New-CommandHint -Value "composer install" -Confidence "verified"
            }

            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "artisan")) {
                $start = New-CommandHint -Value "php artisan serve --host=0.0.0.0 --port=8000" -Confidence "verified"
                $test = New-CommandHint -Value "php artisan test" -Confidence "verified"
            }
        }
        "go" {
            $install = New-CommandHint -Value "go mod download" -Confidence "verified"
            $start = New-CommandHint -Value "go run ." -Confidence "inferred"
            $test = New-CommandHint -Value "go test ./..." -Confidence "verified"
            $build = New-CommandHint -Value "go build ./..." -Confidence "verified"
        }
        "dotnet" {
            $install = New-CommandHint -Value "dotnet restore" -Confidence "verified"
            $start = New-CommandHint -Value "dotnet run" -Confidence "verified"
            $test = New-CommandHint -Value "dotnet test" -Confidence "verified"
            $build = New-CommandHint -Value "dotnet build" -Confidence "verified"
        }
        "java" {
            if (Test-Path -LiteralPath (Join-Path $ProjectRoot "mvnw")) {
                $install = New-CommandHint -Value ".\mvnw dependency:resolve" -Confidence "verified"
                $start = New-CommandHint -Value ".\mvnw spring-boot:run" -Confidence "inferred"
                $test = New-CommandHint -Value ".\mvnw test" -Confidence "verified"
                $build = New-CommandHint -Value ".\mvnw package" -Confidence "verified"
            }
            elseif (Test-Path -LiteralPath (Join-Path $ProjectRoot "gradlew.bat")) {
                $install = New-CommandHint -Value ".\gradlew.bat dependencies" -Confidence "verified"
                $start = New-CommandHint -Value ".\gradlew.bat bootRun" -Confidence "inferred"
                $test = New-CommandHint -Value ".\gradlew.bat test" -Confidence "verified"
                $build = New-CommandHint -Value ".\gradlew.bat build" -Confidence "verified"
            }
        }
        "ruby" {
            $install = New-CommandHint -Value "bundle install" -Confidence "verified"
            $start = New-CommandHint -Value "bundle exec rails server" -Confidence "inferred"
            $test = New-CommandHint -Value "bundle exec rspec" -Confidence "inferred"
        }
        "rust" {
            $install = New-CommandHint -Value "cargo fetch" -Confidence "verified"
            $start = New-CommandHint -Value "cargo run" -Confidence "verified"
            $test = New-CommandHint -Value "cargo test" -Confidence "verified"
            $build = New-CommandHint -Value "cargo build" -Confidence "verified"
        }
        "static" {
            if ($packageJson) {
                $scripts = Get-ObjectPropertyValue -InputObject $packageJson -Name "scripts"
                if ((Get-ObjectPropertyValue -InputObject $scripts -Name "build")) {
                    $build = New-CommandHint -Value "npm run build" -Confidence "verified"
                }

                if ((Get-ObjectPropertyValue -InputObject $scripts -Name "dev")) {
                    $start = New-CommandHint -Value "npm run dev" -Confidence "verified"
                }
            }
        }
    }

    return [PSCustomObject]@{
        Install = $install
        Start   = $start
        Test    = $test
        Build   = $build
    }
}

function Get-DatabaseDetection {
    param(
        [string]$ProjectRoot,
        [string]$PrimaryStack,
        [string[]]$RelativePaths
    )

    $envCandidates = @(
        (Join-Path $ProjectRoot ".env"),
        (Join-Path $ProjectRoot ".env.example"),
        (Join-Path $ProjectRoot ".env.local")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    foreach ($envPath in $envCandidates) {
        $content = Get-TextFile -Path $envPath
        if ($content -match "(?m)^\s*DB_CONNECTION\s*=\s*([a-zA-Z0-9_]+)") {
            return [PSCustomObject]@{
                Engine     = $matches[1].ToLowerInvariant()
                Confidence = "verified"
                Evidence    = (Get-RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envPath)
            }
        }

        if ($content -match "(?m)^\s*DATABASE_URL\s*=\s*.*(postgres|postgresql)") {
            return [PSCustomObject]@{
                Engine     = "postgres"
                Confidence = "verified"
                Evidence    = (Get-RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envPath)
            }
        }

        if ($content -match "(?m)^\s*DATABASE_URL\s*=\s*.*mysql") {
            return [PSCustomObject]@{
                Engine     = "mysql"
                Confidence = "verified"
                Evidence    = (Get-RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envPath)
            }
        }

        if ($content -match "(?m)^\s*DATABASE_URL\s*=\s*.*mongodb") {
            return [PSCustomObject]@{
                Engine     = "mongodb"
                Confidence = "verified"
                Evidence    = (Get-RelativeUnixPath -BasePath $ProjectRoot -TargetPath $envPath)
            }
        }
    }

    $packageJson = Get-JsonFile -Path (Join-Path $ProjectRoot "package.json")
    $composerJson = Get-JsonFile -Path (Join-Path $ProjectRoot "composer.json")
    $pyprojectText = Get-TextFile -Path (Join-Path $ProjectRoot "pyproject.toml")

    if ($packageJson) {
        $deps = @()
        $dependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "dependencies"
        $devDependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "devDependencies"
        if ($dependencies) {
            $deps += $dependencies.PSObject.Properties.Name
        }
        if ($devDependencies) {
            $deps += $devDependencies.PSObject.Properties.Name
        }

        if ($deps -contains "pg" -or $deps -contains "prisma") {
            return [PSCustomObject]@{
                Engine     = "postgres"
                Confidence = "inferred"
                Evidence    = "package.json dependencies"
            }
        }

        if ($deps -contains "mysql2" -or $deps -contains "sequelize-mysql") {
            return [PSCustomObject]@{
                Engine     = "mysql"
                Confidence = "inferred"
                Evidence    = "package.json dependencies"
            }
        }

        if ($deps -contains "mongoose" -or $deps -contains "mongodb") {
            return [PSCustomObject]@{
                Engine     = "mongodb"
                Confidence = "inferred"
                Evidence    = "package.json dependencies"
            }
        }
    }

    if ($composerJson) {
        $requires = @()
        $requireMap = Get-ObjectPropertyValue -InputObject $composerJson -Name "require"
        if ($requireMap) {
            $requires += $requireMap.PSObject.Properties.Name
        }

        if ($requires -contains "laravel/framework") {
            return [PSCustomObject]@{
                Engine     = "unknown"
                Confidence = "unknown"
                Evidence    = "Laravel detected but DB not proven"
            }
        }
    }

    if ($pyprojectText -match "psycopg|asyncpg") {
        return [PSCustomObject]@{
            Engine     = "postgres"
            Confidence = "inferred"
            Evidence    = "pyproject.toml"
        }
    }

    if ($pyprojectText -match "pymysql|mysqlclient") {
        return [PSCustomObject]@{
            Engine     = "mysql"
            Confidence = "inferred"
            Evidence    = "pyproject.toml"
        }
    }

    if ($pyprojectText -match "pymongo") {
        return [PSCustomObject]@{
            Engine     = "mongodb"
            Confidence = "inferred"
            Evidence    = "pyproject.toml"
        }
    }

    return [PSCustomObject]@{
        Engine     = "unknown"
        Confidence = "unknown"
        Evidence    = "No verified database signal"
    }
}

$resolvedProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$projectName = Split-Path -Leaf $resolvedProjectPath
$projectSlug = Get-ProjectSlug -Name $projectName

$excludePattern = "\\(node_modules|vendor|dist|build|coverage|\.git|bin|obj|target|\.venv|venv|\.next|out|\.pytest_cache|pytest-cache-files-[^\\]+)\\"
$allFiles = @(Get-ChildItem -Path $resolvedProjectPath -Recurse -Force -File -ErrorAction SilentlyContinue) | Where-Object {
    $_.FullName -notmatch $excludePattern -and
    $_.Name -notmatch "\.generated($|\.)" -and
    $_.Name -ne "INTAKE_REPORT.md"
}
$relativePaths = @($allFiles | ForEach-Object { Get-RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $_.FullName })

$sourceExtensions = @(".js", ".jsx", ".ts", ".tsx", ".php", ".py", ".go", ".cs", ".java", ".rb", ".rs", ".kt", ".swift")
$sourceFiles = @($allFiles | Where-Object { $sourceExtensions -contains $_.Extension.ToLowerInvariant() })
$sourceRelativePaths = @($sourceFiles | ForEach-Object { Get-RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $_.FullName })

$scores = @{}
$frameworks = New-Object System.Collections.Generic.List[string]

if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)package\.json$") {
    Add-Score -Scores $scores -Name "node" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pnpm-lock\.yaml|yarn\.lock|package-lock\.json)$") {
    Add-Score -Scores $scores -Name "node" -Points 2
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)tsconfig\.json$") {
    Add-Score -Scores $scores -Name "node" -Points 2
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)composer\.json$") {
    Add-Score -Scores $scores -Name "php" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pyproject\.toml|requirements\.txt|Pipfile)$") {
    Add-Score -Scores $scores -Name "python" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)go\.mod$") {
    Add-Score -Scores $scores -Name "go" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/).+\.csproj$") {
    Add-Score -Scores $scores -Name "dotnet" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pom\.xml|build\.gradle|build\.gradle\.kts)$") {
    Add-Score -Scores $scores -Name "java" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)Gemfile$") {
    Add-Score -Scores $scores -Name "ruby" -Points 10
}
if (Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)Cargo\.toml$") {
    Add-Score -Scores $scores -Name "rust" -Points 10
}

$packageJson = Get-JsonFile -Path (Join-Path $resolvedProjectPath "package.json")
if ($packageJson) {
    $deps = @()
    $dependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "dependencies"
    $devDependencies = Get-ObjectPropertyValue -InputObject $packageJson -Name "devDependencies"
    if ($dependencies) {
        $deps += $dependencies.PSObject.Properties.Name
    }
    if ($devDependencies) {
        $deps += $devDependencies.PSObject.Properties.Name
    }

    if ($deps -contains "next") {
        $frameworks.Add("Next.js")
        Add-Score -Scores $scores -Name "node" -Points 3
    }
    if ($deps -contains "@nestjs/core") {
        $frameworks.Add("NestJS")
        Add-Score -Scores $scores -Name "node" -Points 3
    }
    if ($deps -contains "express") {
        $frameworks.Add("Express")
    }
    if ($deps -contains "react") {
        $frameworks.Add("React")
        if ($scores.ContainsKey("node")) {
            Add-Score -Scores $scores -Name "static" -Points 6
        }
    }
    if ($deps -contains "vite") {
        $frameworks.Add("Vite")
        Add-Score -Scores $scores -Name "static" -Points 8
    }
    if ($deps -contains "vue") {
        $frameworks.Add("Vue")
        Add-Score -Scores $scores -Name "static" -Points 6
    }
}

$composerJson = Get-JsonFile -Path (Join-Path $resolvedProjectPath "composer.json")
if ($composerJson) {
    $composerRequireMap = Get-ObjectPropertyValue -InputObject $composerJson -Name "require"
    $composerRequires = if ($composerRequireMap) { @($composerRequireMap.PSObject.Properties.Name) } else { @() }
    if ($composerRequires -contains "laravel/framework") {
        $frameworks.Add("Laravel")
        Add-Score -Scores $scores -Name "php" -Points 3
    }
    if ($composerRequires | Where-Object { $_ -like "symfony/*" }) {
        $frameworks.Add("Symfony")
    }
}

$pyprojectText = Get-TextFile -Path (Join-Path $resolvedProjectPath "pyproject.toml")
if ($pyprojectText) {
    if ($pyprojectText -match "fastapi") {
        $frameworks.Add("FastAPI")
    }
    if ($pyprojectText -match "django") {
        $frameworks.Add("Django")
    }
    if ($pyprojectText -match "flask") {
        $frameworks.Add("Flask")
    }
}

$manifestEvidence = @(
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)package\.json$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)composer\.json$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pyproject\.toml|requirements\.txt|Pipfile)$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)go\.mod$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/).+\.csproj$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pom\.xml|build\.gradle|build\.gradle\.kts)$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)Gemfile$"
    Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)Cargo\.toml$"
) | Where-Object { $_ } | ForEach-Object { $_ } | Select-Object -Unique

$testEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(tests?/|spec/|__tests__/|.+\.(spec|test)\.)" -Limit 10)
$dockerEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(Dockerfile|docker-compose.*\.ya?ml|compose\.ya?ml)$" -Limit 10)
$ciEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(\.github/workflows/|azure-pipelines|gitlab-ci|Jenkinsfile)" -Limit 10)
$envEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)\.env(\..+)?$" -Limit 10)
$migrationEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(database/migrations/|migrations/|prisma/migrations/|db/migrate/|alembic/)" -Limit 10)
$sourceDirEvidence = @(Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(src/|app/|server/|backend/|frontend/|packages/)" -Limit 10)

$candidateStacks = @(
    $scores.GetEnumerator() |
        Sort-Object -Property Value -Descending |
        ForEach-Object {
            [PSCustomObject]@{
                Name  = $_.Key
                Score = $_.Value
            }
        }
)

$primaryStack = if (@($candidateStacks).Count -gt 0) { $candidateStacks[0].Name } else { "unknown" }

$monorepoMarkers = [bool](Get-FirstMatches -Paths $relativePaths -Pattern "(^|/)(pnpm-workspace\.yaml|turbo\.json|nx\.json|lerna\.json)$" -Limit 1)
$riskFactors = New-Object System.Collections.Generic.List[string]
$classificationRationale = New-Object System.Collections.Generic.List[string]

if (@($sourceFiles).Count -eq 0 -and @($manifestEvidence).Count -eq 0 -and @($sourceDirEvidence).Count -eq 0) {
    $mode = "greenfield"
    $classificationRationale.Add("No reliable application manifest or source tree was detected.")
}
else {
    if ($testEvidence.Count -eq 0) {
        $riskFactors.Add("No automated test evidence detected.")
    }
    if ($dockerEvidence.Count -eq 0) {
        $riskFactors.Add("No Docker or Compose assets detected.")
    }
    if ($ciEvidence.Count -eq 0) {
        $riskFactors.Add("No CI/CD pipeline evidence detected.")
    }
    if (@($sourceFiles).Count -gt 80 -and @($sourceDirEvidence).Count -gt 0) {
        $riskFactors.Add("Large source surface detected.")
    }
    if (@($sourceFiles).Count -gt 40 -and @($testEvidence).Count -eq 0) {
        $riskFactors.Add("Large codebase without tests.")
    }
    if (@($candidateStacks | Where-Object { $_.Score -ge 10 }).Count -gt 1 -and -not $monorepoMarkers) {
        $riskFactors.Add("Multiple strong stack signals without clear monorepo markers.")
    }

    if ($riskFactors.Count -ge 3) {
        $mode = "legacy"
        $classificationRationale.Add("The repository has code but also enough risk signals to require a legacy gate.")
    }
    else {
        $mode = "existing"
        $classificationRationale.Add("The repository has a coherent enough structure to continue with deep analysis.")
    }
}

$confidence = "medium"
if ($mode -eq "greenfield" -and @($manifestEvidence).Count -eq 0 -and @($sourceFiles).Count -eq 0) {
    $confidence = "high"
}
elseif ($mode -eq "existing" -and $primaryStack -ne "unknown" -and @($sourceFiles).Count -gt 0) {
    if (@($manifestEvidence).Count -gt 0 -and (@($testEvidence).Count -gt 0 -or @($dockerEvidence).Count -gt 0 -or @($ciEvidence).Count -gt 0)) {
        $confidence = "high"
    }
}
elseif ($mode -eq "legacy" -and $riskFactors.Count -ge 4) {
    $confidence = "high"
}

$commands = Get-CommandHints -ProjectRoot $resolvedProjectPath -PrimaryStack $primaryStack -RelativePaths $relativePaths
$database = Get-DatabaseDetection -ProjectRoot $resolvedProjectPath -PrimaryStack $primaryStack -RelativePaths $relativePaths

$questions = New-Object System.Collections.Generic.List[string]
if ($mode -eq "greenfield") {
    @(
        "What is the product or system being built?",
        "Which primary language/framework should be used?",
        "Is this web, API, desktop, mobile, CLI, or mixed?",
        "Which transactional database should be used?",
        "Must Docker exist from day one?",
        "What deployment target is expected?"
    ) | ForEach-Object { $questions.Add($_) }
}
else {
    if ($commands.Start.Value -eq "unknown") {
        $questions.Add("What is the verified application start command?")
    }
    if ($commands.Test.Value -eq "unknown") {
        $questions.Add("What is the verified test command?")
    }
    if ($database.Engine -eq "unknown") {
        $questions.Add("Which transactional database engine should this project use?")
    }
}

$refactorGate = if ($mode -eq "legacy") { "Choose: stabilize-only | targeted-refactor | full-modernization" } else { "n/a" }
$summary = "Mode=$mode; PrimaryStack=$primaryStack; Database=$($database.Engine); Confidence=$confidence"

$result = [PSCustomObject]@{
    GeneratedAt      = (Get-Date).ToString("s")
    ProjectPath      = $resolvedProjectPath
    ProjectSlug      = $projectSlug
    Classification   = [PSCustomObject]@{
        Mode       = $mode
        Confidence = $confidence
        Rationale  = @($classificationRationale)
    }
    PrimaryStack     = [PSCustomObject]@{
        Name       = $primaryStack
        Frameworks = @($frameworks | Select-Object -Unique)
    }
    CandidateStacks  = @($candidateStacks)
    Commands         = $commands
    Database         = $database
    Evidence         = [PSCustomObject]@{
        Manifests         = @($manifestEvidence)
        SourceDirectories = @($sourceDirEvidence)
        Tests             = @($testEvidence)
        Docker            = @($dockerEvidence)
        CI                = @($ciEvidence)
        Environment       = @($envEvidence)
        Migrations        = @($migrationEvidence)
    }
    Risks            = @($riskFactors)
    Questions        = @($questions)
    RefactorGate     = $refactorGate
    DockerPlan       = [PSCustomObject]@{
        OutputDirectory   = "infra/$projectSlug/docker"
        SuggestedStack    = $primaryStack
        SuggestedDatabase = $database.Engine
    }
    Summary          = $summary
}

$markdownLines = New-Object System.Collections.Generic.List[string]
$markdownLines.Add("# Intake Report")
$markdownLines.Add("")
$markdownLines.Add("- Generated At: $($result.GeneratedAt)")
$markdownLines.Add("- Project Path: $($result.ProjectPath)")
$markdownLines.Add("- Project Slug: $($result.ProjectSlug)")
$markdownLines.Add("- Mode: $($result.Classification.Mode)")
$markdownLines.Add("- Confidence: $($result.Classification.Confidence)")
$markdownLines.Add("- Summary: $($result.Summary)")
$markdownLines.Add("")
$markdownLines.Add("## Classification Rationale")
foreach ($item in $result.Classification.Rationale) {
    $markdownLines.Add("- $item")
}
$markdownLines.Add("")
$markdownLines.Add("## Primary Stack")
$markdownLines.Add("- Stack: $($result.PrimaryStack.Name)")
$markdownLines.Add("- Frameworks: $((@($result.PrimaryStack.Frameworks) -join ', '))")
$markdownLines.Add("")
$markdownLines.Add("## Candidate Stacks")
$markdownLines.Add("| Stack | Score |")
$markdownLines.Add("|------|-------|")
foreach ($stack in $result.CandidateStacks) {
    $markdownLines.Add("| $($stack.Name) | $($stack.Score) |")
}
$markdownLines.Add("")
$markdownLines.Add("## Commands")
$markdownLines.Add("| Activity | Value | Confidence |")
$markdownLines.Add("|---------|-------|------------|")
$markdownLines.Add("| Install | $($result.Commands.Install.Value) | $($result.Commands.Install.Confidence) |")
$markdownLines.Add("| Start | $($result.Commands.Start.Value) | $($result.Commands.Start.Confidence) |")
$markdownLines.Add("| Test | $($result.Commands.Test.Value) | $($result.Commands.Test.Confidence) |")
$markdownLines.Add("| Build | $($result.Commands.Build.Value) | $($result.Commands.Build.Confidence) |")
$markdownLines.Add("")
$markdownLines.Add("## Database")
$markdownLines.Add("- Engine: $($result.Database.Engine)")
$markdownLines.Add("- Confidence: $($result.Database.Confidence)")
$markdownLines.Add("- Evidence: $($result.Database.Evidence)")
$markdownLines.Add("")
$markdownLines.Add("## Evidence")
foreach ($groupName in @("Manifests", "SourceDirectories", "Tests", "Docker", "CI", "Environment", "Migrations")) {
    $markdownLines.Add("### $groupName")
    $groupValues = @($result.Evidence.$groupName)
    if ($groupValues.Count -eq 0) {
        $markdownLines.Add("- none detected")
    }
    else {
        foreach ($value in $groupValues) {
            $markdownLines.Add("- $value")
        }
    }
    $markdownLines.Add("")
}
$markdownLines.Add("## Risks")
if (@($result.Risks).Count -eq 0) {
    $markdownLines.Add("- no major structural risk detected from the initial scan")
}
else {
    foreach ($risk in $result.Risks) {
        $markdownLines.Add("- $risk")
    }
}
$markdownLines.Add("")
$markdownLines.Add("## Questions")
if (@($result.Questions).Count -eq 0) {
    $markdownLines.Add("- none")
}
else {
    foreach ($question in $result.Questions) {
        $markdownLines.Add("- $question")
    }
}
$markdownLines.Add("")
$markdownLines.Add("## Docker Plan")
$markdownLines.Add("- Output Directory: $($result.DockerPlan.OutputDirectory)")
$markdownLines.Add("- Suggested Stack: $($result.DockerPlan.SuggestedStack)")
$markdownLines.Add("- Suggested Database: $($result.DockerPlan.SuggestedDatabase)")
$markdownLines.Add("")
$markdownLines.Add("## Refactor Gate")
$markdownLines.Add("- $($result.RefactorGate)")

$resolvedOutputPath = if ([System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath
}
else {
    Join-Path $resolvedProjectPath $OutputPath
}

$outputDirectory = Split-Path -Parent $resolvedOutputPath
if ($outputDirectory -and -not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

[System.IO.File]::WriteAllText($resolvedOutputPath, ($markdownLines -join [Environment]::NewLine))

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Wrote intake report to $resolvedOutputPath"
}

