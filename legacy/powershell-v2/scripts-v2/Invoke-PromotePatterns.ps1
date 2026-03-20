<#
.SYNOPSIS
    Promotes resolved REPAIR patterns from a project's local pattern library
    to the global cross-project pattern library at memory_graph/patterns/.
.DESCRIPTION
    Scans ai-orchestrator/patterns/*.md in the project directory.
    For each pattern not yet in the global library, copies it there and
    adds a MemoryNode entry so it is indexed in Neo4j/Qdrant on next sync.
    Safe to run repeatedly: skips patterns already promoted (by file name).
    Supports confidence gating so only validated patterns are auto-promoted.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/patterns/.
.PARAMETER GlobalPatternsPath
    Path to the global pattern library. Defaults to memory_graph/patterns/
    relative to the orchestrator root.
.PARAMETER MinConfidenceScore
    Minimum confidence required for auto-promotion. Default: 0.80.
.EXAMPLE
    .\scripts\v2\Invoke-PromotePatterns.ps1 -ProjectPath C:\projects\myapp
#>
param(
    [string]$ProjectPath = ".",
    [string]$GlobalPatternsPath = "",
    [double]$MinConfidenceScore = 0.80
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2PatternConfidenceScore {
    param(
        [string]$PatternContent,
        [string]$PatternName
    )

    if ([string]::IsNullOrWhiteSpace($PatternContent)) {
        return 0.0
    }

    $frontMatterMatch = [regex]::Match($PatternContent, "(?im)^\s*confidence_score\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$")
    if ($frontMatterMatch.Success) {
        return [Math]::Round([double]$frontMatterMatch.Groups[1].Value, 3)
    }

    $inlineScoreMatch = [regex]::Match($PatternContent, "(?im)^\s*\*{0,2}confidence\s*score\*{0,2}\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$")
    if ($inlineScoreMatch.Success) {
        return [Math]::Round([double]$inlineScoreMatch.Groups[1].Value, 3)
    }

    $confidenceLabelMatch = [regex]::Match($PatternContent, "(?im)^\s*confidence\s*:\s*(verified|high|inferred|medium|missing|low)\s*$")
    if ($confidenceLabelMatch.Success) {
        switch ($confidenceLabelMatch.Groups[1].Value.ToLowerInvariant()) {
            "verified" { return 0.95 }
            "high" { return 0.90 }
            "inferred" { return 0.80 }
            "medium" { return 0.75 }
            "missing" { return 0.45 }
            "low" { return 0.40 }
        }
    }

    if ($PatternContent -match "(?im)^\s*\*\*Source task:\*\*\s*REPAIR-") {
        return 0.85
    }
    if ($PatternName -match "^repair-") {
        return 0.82
    }

    return 0.75
}

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

if ([string]::IsNullOrWhiteSpace($GlobalPatternsPath)) {
    $orchestratorRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $GlobalPatternsPath = Join-Path $orchestratorRoot "memory_graph/patterns"
}

$globalPatternsPath = Resolve-V2AbsolutePath -Path $GlobalPatternsPath
if (-not (Test-Path -LiteralPath $globalPatternsPath -PathType Container)) {
    New-Item -ItemType Directory -Path $globalPatternsPath -Force | Out-Null
}

$localPatternsPath = Join-Path $resolvedPath "ai-orchestrator/patterns"
if (-not (Test-Path -LiteralPath $localPatternsPath -PathType Container)) {
    Write-Host "No local patterns directory found at $localPatternsPath. Nothing to promote."
    exit 0
}

$localPatterns = @(Get-ChildItem -LiteralPath $localPatternsPath -Filter "*.md" -File)
if ($localPatterns.Count -eq 0) {
    Write-Host "No local patterns found. Nothing to promote."
    exit 0
}

$promoted = New-Object System.Collections.Generic.List[string]
$skippedExisting = New-Object System.Collections.Generic.List[string]
$skippedLowConfidence = New-Object System.Collections.Generic.List[object]

foreach ($pattern in $localPatterns) {
    $globalTarget = Join-Path $globalPatternsPath $pattern.Name
    if (Test-Path -LiteralPath $globalTarget -PathType Leaf) {
        $skippedExisting.Add($pattern.Name)
        continue
    }

    try {
        $localContent = Get-Content -LiteralPath $pattern.FullName -Raw
        $confidenceScore = Get-V2PatternConfidenceScore -PatternContent $localContent -PatternName $pattern.BaseName
        if ($confidenceScore -lt $MinConfidenceScore) {
            $skippedLowConfidence.Add([PSCustomObject]@{
                pattern = $pattern.Name
                confidence_score = [Math]::Round($confidenceScore, 3)
            })
            continue
        }

        $projectSlug = Split-Path -Leaf $resolvedPath
$promotionNote = @"

---
_Promoted from project: $projectSlug on $(Get-V2Timestamp)_
_Confidence score: $($confidenceScore)_
"@
        [System.IO.File]::WriteAllText($globalTarget, ($localContent + $promotionNote), [System.Text.Encoding]::UTF8)

        $nodesDir = Join-Path (Split-Path -Parent $globalPatternsPath) "nodes"
        if (Test-Path -LiteralPath $nodesDir -PathType Container) {
            $nodeSlug = "pattern-" + ($pattern.BaseName -replace "[^a-z0-9]+", "-").ToLowerInvariant()
            $nodePath = Join-Path $nodesDir "$nodeSlug.md"
            if (-not (Test-Path -LiteralPath $nodePath -PathType Leaf)) {
                $firstLine = ($localContent -split "`n")[0] -replace "^#\s*", ""
                $nodeContent = @"
---
id: $nodeSlug
type: pattern
project_slug: orchestrator-os
tags: [pattern, repair, cross-project]
source_project: $projectSlug
confidence_score: $confidenceScore
---

# Pattern: $firstLine

Promoted from project `$projectSlug`.
See full pattern: memory_graph/patterns/$($pattern.Name)
"@
                [System.IO.File]::WriteAllText($nodePath, $nodeContent, [System.Text.Encoding]::UTF8)
            }
        }

        $promoted.Add($pattern.Name)
        Write-Host ("Promoted: {0} -> {1} (confidence={2})" -f $pattern.Name, $globalTarget, $confidenceScore)
    }
    catch {
        Write-Warning "Failed to promote $($pattern.Name): $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host ("Promotion complete. Promoted: {0} | Already existed: {1} | Low confidence skipped: {2}" -f $promoted.Count, $skippedExisting.Count, $skippedLowConfidence.Count)

Write-Output ([PSCustomObject]@{
    project                = $resolvedPath
    promoted               = @($promoted.ToArray())
    skipped_existing       = @($skippedExisting.ToArray())
    skipped_low_confidence = @($skippedLowConfidence.ToArray())
    min_confidence_score   = [Math]::Round($MinConfidenceScore, 3)
    global_path            = $globalPatternsPath
} | ConvertTo-Json -Depth 6)
