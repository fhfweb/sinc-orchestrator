<#
.SYNOPSIS
    Reads project source and generates a real architecture.md.
.DESCRIPTION
    Produces a content-rich architecture document from the actual codebase:
      - stack and framework hints
      - module map
      - detected entities/classes
      - detected API surface
      - pending tasks from task-dag
      - docker services from compose
    By default it preserves curated documentation and only overwrites stubs.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator and source code.
.PARAMETER Force
    Overwrite architecture.md even when it already has curated content.
#>
param(
    [string]$ProjectPath = ".",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$archPath = Join-Path $orchestratorRoot "documentation/architecture.md"
$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$composePath = Join-Path $orchestratorRoot "docker/docker-compose.generated.yml"

if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator not found. Run intake first: Invoke-UniversalOrchestratorV2.ps1 -Mode submit"
}

if (-not $Force -and (Test-Path -LiteralPath $archPath -PathType Leaf)) {
    $existing = Get-Content -LiteralPath $archPath -Raw -ErrorAction SilentlyContinue
    $lineCount = @(($existing -split "(`r`n|`n|`r)") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
    $looksStub = $existing -match "## Runtime Contracts" -and $existing -match "`task-dag.json` is canonical task state"
    $hasPlaceholder = $existing -match "This file should always reflect the current validated architecture"
    if ($lineCount -gt 30 -and -not $looksStub -and -not $hasPlaceholder) {
        Write-Host "architecture.md already has curated content ($lineCount lines). Skipping (use -Force to overwrite)."
        exit 0
    }
}

Initialize-V2Directory -Path (Split-Path -Parent $archPath)

$state = Get-V2JsonContent -Path $statePath
$stack = if ($state) {
    $stackValue = [string](Get-V2OptionalProperty -InputObject $state -Name "stack" -DefaultValue "unknown")
    if ([string]::IsNullOrWhiteSpace($stackValue) -or $stackValue -eq "unknown") {
        $tf = Get-V2OptionalProperty -InputObject $state -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
        $stackValue = [string](Get-V2OptionalProperty -InputObject $tf -Name "primary_language" -DefaultValue "unknown")
    }
    $stackValue
}
else {
    "unknown"
}
$dbType = if ($state) {
    $dbs = Get-V2OptionalProperty -InputObject $state -Name "databases" -DefaultValue ([PSCustomObject]@{})
    $rel = Get-V2OptionalProperty -InputObject $dbs -Name "relational" -DefaultValue ([PSCustomObject]@{})
    [string](Get-V2OptionalProperty -InputObject $rel -Name "engine" -DefaultValue "none")
}
else {
    "none"
}

$sourceExtensions = @{
    python = @("*.py")
    node = @("*.ts", "*.js", "*.mjs")
    php = @("*.php")
    go = @("*.go")
    dotnet = @("*.cs")
    java = @("*.java")
    ruby = @("*.rb")
    rust = @("*.rs")
}

$fallbackCodeExtensions = @("*.py", "*.ts", "*.js", "*.mjs", "*.php", "*.go", "*.cs", "*.java", "*.rb", "*.rs")
$exts = if ($sourceExtensions.ContainsKey($stack)) { $sourceExtensions[$stack] } else { $fallbackCodeExtensions }

$allSourceFiles = New-Object System.Collections.Generic.List[System.IO.FileInfo]
foreach ($ext in $exts) {
    foreach ($file in @(Get-ChildItem -LiteralPath $resolvedPath -Recurse -Filter $ext -File -ErrorAction SilentlyContinue)) {
        if ($file.FullName -match "\\(node_modules|vendor|\.git|__pycache__|dist|build|\.venv|venv)\\") { continue }
        $allSourceFiles.Add($file)
    }
}

$totalFiles = $allSourceFiles.Count

$moduleCounts = @{}
foreach ($file in @($allSourceFiles.ToArray())) {
    $relative = $file.FullName.Substring($resolvedPath.Length).TrimStart('\', '/')
    $top = ($relative -split '[/\\]')[0]
    if ([string]::IsNullOrWhiteSpace($top)) { continue }
    if ($top -match "^[^/\\]+\.[A-Za-z0-9]+$") { $top = "root" }
    if ($top -match "^(node_modules|vendor|\.git|__pycache__|dist|build|\.venv|venv)$") { continue }
    if (-not $moduleCounts.ContainsKey($top)) { $moduleCounts[$top] = 0 }
    $moduleCounts[$top] = [int]$moduleCounts[$top] + 1
}
$moduleDirs = @($moduleCounts.Keys | Sort-Object)

$routePatterns = New-Object System.Collections.Generic.HashSet[string]
$entityPatterns = New-Object System.Collections.Generic.HashSet[string]
$frameworkHints = New-Object System.Collections.Generic.HashSet[string]

$sampleFiles = @($allSourceFiles.ToArray() | Select-Object -First 120)
foreach ($file in $sampleFiles) {
    $content = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($content)) { continue }

    foreach ($m in [regex]::Matches($content, "@(app|router)\.(get|post|put|delete|patch)\s*\(\s*['`"]([^'`"]+)['`"]", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
        [void]$routePatterns.Add(("{0} {1}" -f $m.Groups[2].Value.ToUpperInvariant(), $m.Groups[3].Value))
    }
    foreach ($m in [regex]::Matches($content, "Route::(get|post|put|delete|patch)\s*\(\s*['`"]([^'`"]+)['`"]", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
        [void]$routePatterns.Add(("{0} {1}" -f $m.Groups[1].Value.ToUpperInvariant(), $m.Groups[2].Value))
    }

    foreach ($m in [regex]::Matches($content, "class\s+(\w+)\s*(extends\s+\w+)?\s*(\(|{|:)", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
        $cls = $m.Groups[1].Value
        if ($cls -match "^(Test|Abstract|Base|Interface|Exception|Error|Handler|Config|Settings|Enum|Mixin|Schema|Serializer|Form|View|Controller|Router|Factory|Command|Job|Event|Listener|Provider|Middleware|Guard|Pipe|Module|AppModule)$") {
            continue
        }
        [void]$entityPatterns.Add($cls)
    }

    if ($content -match "from fastapi|import FastAPI") { [void]$frameworkHints.Add("FastAPI") }
    if ($content -match "from flask|import Flask") { [void]$frameworkHints.Add("Flask") }
    if ($content -match "from django|import django") { [void]$frameworkHints.Add("Django") }
    if ($content -match "express\(\)|require\(.express.\)") { [void]$frameworkHints.Add("Express") }
    if ($content -match "@nestjs|NestFactory") { [void]$frameworkHints.Add("NestJS") }
    if ($content -match "Laravel\\|Illuminate\\|artisan") { [void]$frameworkHints.Add("Laravel") }
    if ($content -match "Gin\.|gin\.Default|gin\.New") { [void]$frameworkHints.Add("Gin") }
    if ($content -match "sqlalchemy|SQLAlchemy") { [void]$frameworkHints.Add("SQLAlchemy") }
    if ($content -match "prisma|@prisma") { [void]$frameworkHints.Add("Prisma") }
    if ($content -match "alembic|Alembic") { [void]$frameworkHints.Add("Alembic") }
    if ($content -match "celery|Celery") { [void]$frameworkHints.Add("Celery") }
}

$pendingTasks = @()
if (Test-Path -LiteralPath $dagPath -PathType Leaf) {
    try {
        $dag = Get-V2JsonContent -Path $dagPath
        $pendingTasks = @($dag.tasks | Where-Object {
            $s = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
            $normalized = $s.ToLowerInvariant()
            if ($normalized -eq "open") { $normalized = "pending" }
            $normalized -in @("pending", "in-progress", "blocked")
        } | ForEach-Object {
            $id = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
            $title = [string](Get-V2OptionalProperty -InputObject $_ -Name "title" -DefaultValue "")
            if ([string]::IsNullOrWhiteSpace($title)) {
                $title = [string](Get-V2OptionalProperty -InputObject $_ -Name "description" -DefaultValue $id)
            }
            $agent = [string](Get-V2OptionalProperty -InputObject $_ -Name "assigned_agent" -DefaultValue "unassigned")
            "- [$id] $title (agent: $agent)"
        })
    }
    catch {
        $pendingTasks = @()
    }
}

$dockerServices = @()
if (Test-Path -LiteralPath $composePath -PathType Leaf) {
    try {
        $composeLines = @(Get-Content -LiteralPath $composePath -ErrorAction SilentlyContinue)
        $inServices = $false
        foreach ($line in $composeLines) {
            if ($line -match "^\s*services:\s*$") {
                $inServices = $true
                continue
            }
            if (-not $inServices) { continue }
            if ($line -match "^[^\s]") { break }
            if ($line -match "^\s{2}([A-Za-z0-9_-]+):\s*$") {
                $dockerServices += $matches[1]
            }
        }
    }
    catch {
        $dockerServices = @()
    }
}

$ts = Get-V2Timestamp
$name = Split-Path -Leaf $resolvedPath

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Architecture - $name")
$lines.Add("")
$lines.Add(("_Generated by Invoke-CodeReaderAgent at {0}_" -f $ts))
$lines.Add("")
$lines.Add("## Tech Stack")
$lines.Add("")
$lines.Add("| Layer | Technology |")
$lines.Add("|-------|-----------|")
$lines.Add("| Runtime | $stack |")
$lines.Add("| Database | $dbType |")
foreach ($fw in ($frameworkHints | Sort-Object)) { $lines.Add("| Framework/Lib | $fw |") }
$lines.Add("| Source files scanned | $totalFiles |")
$lines.Add("")

$lines.Add("## Module Map")
$lines.Add("")
foreach ($dir in $moduleDirs) {
    $lines.Add(("- **{0}/** - {1} source file(s)" -f $dir, [int]$moduleCounts[$dir]))
}
$lines.Add("")

if ($entityPatterns.Count -gt 0) {
    $lines.Add("## Domain Entities (detected from class definitions)")
    $lines.Add("")
    foreach ($e in ($entityPatterns | Sort-Object)) {
        $lines.Add("- $e")
    }
    $lines.Add("")
}

if ($routePatterns.Count -gt 0) {
    $lines.Add("## API Surface (detected routes)")
    $lines.Add("")
    $lines.Add("| Method | Path |")
    $lines.Add("|--------|------|")
    foreach ($r in ($routePatterns | Sort-Object)) {
        $parts = $r -split " ", 2
        $method = if ($parts.Count -gt 1) { $parts[0] } else { "?" }
        $path = if ($parts.Count -gt 1) { $parts[1] } else { $r }
        $lines.Add("| $method | $path |")
    }
    $lines.Add("")
}

if (@($dockerServices).Count -gt 0) {
    $lines.Add("## Docker Services")
    $lines.Add("")
    foreach ($svc in @($dockerServices | Sort-Object -Unique)) { $lines.Add("- $svc") }
    $lines.Add("")
}

if (@($pendingTasks).Count -gt 0) {
    $lines.Add("## Pending Tasks")
    $lines.Add("")
    foreach ($t in $pendingTasks) { $lines.Add($t) }
    $lines.Add("")
}

$lines.Add("## Notes")
$lines.Add("- This file is generated from current source and should be reviewed by the Architecture Agent.")
$lines.Add("- Detection is regex-based and should be validated against real contracts.")
$lines.Add("- Re-run with -Force after large refactors.")

$content = $lines -join [Environment]::NewLine
[System.IO.File]::WriteAllText($archPath, $content, [System.Text.Encoding]::UTF8)

Write-Host ("architecture.md written: {0} ({1} lines, {2} source files scanned)" -f $archPath, $lines.Count, $totalFiles)
Write-Output ([PSCustomObject]@{
    path = $archPath
    lines = $lines.Count
    source_files = $totalFiles
    modules = @($moduleDirs)
    entities = @($entityPatterns | Sort-Object)
    routes = $routePatterns.Count
    frameworks = @($frameworkHints | Sort-Object)
    docker = @($dockerServices | Sort-Object -Unique)
} | ConvertTo-Json -Depth 5)
