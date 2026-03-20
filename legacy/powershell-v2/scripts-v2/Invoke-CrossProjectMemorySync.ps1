<#
.SYNOPSIS
    Builds a cross-project episode memory set and optionally syncs it to vector stores.
.DESCRIPTION
    Aggregates lessons/patterns from known projects and writes normalized memory nodes into
    memory_graph/cross-project/nodes. Optionally runs memory_sync.py using a dedicated
    project slug (default: cross-project-episodes) so Qdrant/Neo4j can index a global set.
.PARAMETER ProjectPath
    Current project root (used to resolve repository root and include current project).
.PARAMETER CollectionSlug
    Slug used when syncing cross-project nodes to vector stores.
.PARAMETER SyncToVectorStore
    If set, runs scripts/memory_sync.py after generating nodes.
.PARAMETER SkipQdrant
    Pass-through to memory_sync.py --skip-qdrant.
.PARAMETER SkipNeo4j
    Pass-through to memory_sync.py --skip-neo4j.
.PARAMETER EmitJson
    Emits machine-readable summary.
#>
param(
    [string]$ProjectPath = ".",
    [string]$CollectionSlug = "cross-project-episodes",
    [switch]$SyncToVectorStore,
    [switch]$SkipQdrant,
    [switch]$SkipNeo4j,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2ProjectRootsFromRegistry {
    param(
        [string]$RepoRoot,
        [string]$CurrentProjectRoot
    )

    $roots = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($CurrentProjectRoot) -and (Test-Path -LiteralPath $CurrentProjectRoot -PathType Container)) {
        $roots.Add((Resolve-Path -LiteralPath $CurrentProjectRoot).Path)
    }

    $registryPath = Join-Path $RepoRoot "workspace/PROJECT_REGISTRY.json"
    if (Test-Path -LiteralPath $registryPath -PathType Leaf) {
        try {
            $registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
            $projectsNode = if ($registry.PSObject.Properties.Name -contains "Projects") { $registry.Projects } else { $registry.projects }
            foreach ($item in @($projectsNode)) {
                $candidate = [string](Get-V2OptionalProperty -InputObject $item -Name "WorkingPath" -DefaultValue "")
                if ([string]::IsNullOrWhiteSpace($candidate)) {
                    $candidate = [string](Get-V2OptionalProperty -InputObject $item -Name "path" -DefaultValue "")
                }
                if ([string]::IsNullOrWhiteSpace($candidate)) {
                    $candidate = [string](Get-V2OptionalProperty -InputObject $item -Name "SourcePath" -DefaultValue "")
                }
                if ([string]::IsNullOrWhiteSpace($candidate)) {
                    continue
                }
                if (Test-Path -LiteralPath $candidate -PathType Container) {
                    $resolved = (Resolve-Path -LiteralPath $candidate).Path
                    if ($roots -notcontains $resolved) {
                        $roots.Add($resolved)
                    }
                }
            }
        }
        catch {
        }
    }

    if ($roots.Count -eq 0) {
        $roots.Add($RepoRoot)
    }
    return @($roots.ToArray())
}

function Get-V2SourceModule {
    param([string]$RelativePath)

    if ([string]::IsNullOrWhiteSpace($RelativePath)) {
        return "root"
    }
    $normalized = $RelativePath.Replace("\", "/").Trim("/")
    $parts = @($normalized -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts.Count -le 1) {
        return $parts[0]
    }
    if ($parts[-1].Contains(".")) {
        if ($parts.Count -eq 2) { return $parts[0] }
        return ($parts[0] + "/" + $parts[1])
    }
    return ($parts[0] + "/" + $parts[1])
}

function New-V2EpisodeNode {
    param(
        [string]$RepoRoot,
        [string]$NodesDir,
        [string]$ProjectRoot,
        [string]$SourcePath,
        [string]$SourceKind
    )

    $sourceContent = Get-Content -LiteralPath $SourcePath -Raw -ErrorAction Stop
    if ([string]::IsNullOrWhiteSpace($sourceContent)) {
        return $null
    }

    $sourceRel = Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $SourcePath
    $sourceRel = $sourceRel.Replace("\", "/")
    $sourceModule = Get-V2SourceModule -RelativePath $sourceRel
    $projectSlug = Split-Path -Leaf $ProjectRoot

    $hashInput = ("{0}|{1}|{2}" -f $projectSlug, $sourceRel, $sourceContent)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($hashInput))
    }
    finally {
        $sha.Dispose()
    }
    $hash = ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    $shortHash = $hash.Substring(0, 16)
    $nodeId = "cross-episode-$shortHash"
    $nodePath = Join-Path $NodesDir "$nodeId.md"

    $titleLine = @($sourceContent -split "(`r`n|`n|`r)" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) | Select-Object -First 1
    $titleText = [string]$titleLine
    if ($titleText.StartsWith("#")) {
        $titleText = ($titleText -replace "^#+\s*", "").Trim()
    }
    if ([string]::IsNullOrWhiteSpace($titleText)) {
        $titleText = Split-Path -LeafBase $SourcePath
    }

    $summary = $sourceContent.Trim()
    if ($summary.Length -gt 1800) {
        $summary = $summary.Substring(0, 1800) + "..."
    }

    $nodeContent = @"
---
id: $nodeId
type: pattern
project_slug: orchestrator-os
tags: [cross-project, memory, $SourceKind]
source_project: $projectSlug
source_kind: $SourceKind
source_files: [$sourceRel]
source_modules: [$sourceModule]
content_hash: $hash
---

# Cross-Project Episode: $titleText

## Summary
$summary

## Source
- project: $projectSlug
- path: $sourceRel
- imported_at: $(Get-V2Timestamp)
"@

    [System.IO.File]::WriteAllText($nodePath, $nodeContent, [System.Text.Encoding]::UTF8)
    return [PSCustomObject]@{
        node_id = $nodeId
        node_path = $nodePath
        source_project = $projectSlug
        source_path = $sourceRel
        source_kind = $SourceKind
        source_module = $sourceModule
    }
}

$currentProjectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $currentProjectRoot -or -not (Test-Path -LiteralPath $currentProjectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$allProjects = @(Get-V2ProjectRootsFromRegistry -RepoRoot $repoRoot -CurrentProjectRoot $currentProjectRoot)

$crossProjectRoot = Join-Path $repoRoot "memory_graph/cross-project"
$crossNodesDir = Join-Path $crossProjectRoot "nodes"
Initialize-V2Directory -Path $crossNodesDir

$imported = New-Object System.Collections.Generic.List[object]

foreach ($projectRoot in $allProjects) {
    $lessonDir = Join-Path $projectRoot "ai-orchestrator/knowledge_base/lessons_learned"
    $patternDir = Join-Path $projectRoot "ai-orchestrator/patterns"
    $globalPatternDir = Join-Path $repoRoot "memory_graph/patterns"

    $candidates = New-Object System.Collections.Generic.List[object]
    if (Test-Path -LiteralPath $lessonDir -PathType Container) {
        foreach ($f in @(Get-ChildItem -LiteralPath $lessonDir -Filter "*.md" -File -ErrorAction SilentlyContinue)) {
            $candidates.Add([PSCustomObject]@{ path = $f.FullName; kind = "lesson" })
        }
    }
    if (Test-Path -LiteralPath $patternDir -PathType Container) {
        foreach ($f in @(Get-ChildItem -LiteralPath $patternDir -Filter "*.md" -File -ErrorAction SilentlyContinue)) {
            $candidates.Add([PSCustomObject]@{ path = $f.FullName; kind = "pattern" })
        }
    }
    if ($projectRoot -eq $repoRoot -and (Test-Path -LiteralPath $globalPatternDir -PathType Container)) {
        foreach ($f in @(Get-ChildItem -LiteralPath $globalPatternDir -Filter "*.md" -File -ErrorAction SilentlyContinue)) {
            $candidates.Add([PSCustomObject]@{ path = $f.FullName; kind = "pattern" })
        }
    }

    foreach ($candidate in @($candidates.ToArray())) {
        try {
            $node = New-V2EpisodeNode -RepoRoot $repoRoot -NodesDir $crossNodesDir -ProjectRoot $projectRoot -SourcePath $candidate.path -SourceKind $candidate.kind
            if ($node) {
                $imported.Add($node)
            }
        }
        catch {
        }
    }
}

$syncAttempted = $false
$syncSucceeded = $false
$syncError = ""
if ($SyncToVectorStore) {
    $syncAttempted = $true
    try {
        $memorySyncScript = Join-Path $repoRoot "scripts/memory_sync.py"
        if (Test-Path -LiteralPath $memorySyncScript -PathType Leaf) {
            $args = @(
                $memorySyncScript,
                "--project-slug", $CollectionSlug,
                "--memory-dir", (Get-V2RelativeUnixPath -BasePath $repoRoot -TargetPath $crossNodesDir)
            )
            if ($SkipQdrant) { $args += "--skip-qdrant" }
            if ($SkipNeo4j) { $args += "--skip-neo4j" }
            & python @args | Out-Null
            $syncSucceeded = ($LASTEXITCODE -eq 0)
            if (-not $syncSucceeded) {
                $syncError = "memory_sync.py exited with code $LASTEXITCODE"
            }
        }
        else {
            $syncError = "memory_sync.py not found"
        }
    }
    catch {
        $syncError = $_.Exception.Message
        $syncSucceeded = $false
    }
}

$result = [PSCustomObject]@{
    success = $true
    project_path = $currentProjectRoot
    repository_root = $repoRoot
    collection_slug = $CollectionSlug
    projects_scanned = $allProjects.Count
    nodes_directory = $crossNodesDir
    imported_count = $imported.Count
    imported = @($imported.ToArray() | Select-Object -First 50)
    sync_attempted = $syncAttempted
    sync_succeeded = $syncSucceeded
    sync_error = $syncError
    generated_at = Get-V2Timestamp
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 8)
}
else {
    Write-Output ("Cross-project memory sync complete. Imported: {0} | Sync attempted: {1} | Sync ok: {2}" -f $imported.Count, $syncAttempted, $syncSucceeded)
}
