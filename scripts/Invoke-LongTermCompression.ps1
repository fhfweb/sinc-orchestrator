<#
.SYNOPSIS
    Applies long-term memory compression for large markdown logs.
.DESCRIPTION
    Implements P2 hardening for memory hygiene:
      - Compresses docs/agents/EVENT_LOG.md when line threshold is exceeded.
      - Compresses self_healing/resolution_log.md when entry threshold is exceeded.
      - Writes archive files under archive/.
      - Writes run summary to ai-orchestrator/reports/compression-report.md.
.PARAMETER ProjectPath
    Project root path.
.PARAMETER MaxEventLogLines
    Compress EVENT_LOG.md when line count exceeds this threshold.
.PARAMETER KeepRecentEventEntries
    Number of most recent event entries to keep after compression.
.PARAMETER MaxResolutionEntries
    Compress resolution_log.md when markdown entry count exceeds this threshold.
.PARAMETER KeepRecentResolutionEntries
    Number of most recent resolution entries to keep after compression.
.PARAMETER EmitJson
    Emits summary as JSON.
#>
param(
    [string]$ProjectPath = ".",
    [int]$MaxEventLogLines = 500,
    [int]$KeepRecentEventEntries = 80,
    [int]$MaxResolutionEntries = 30,
    [int]$KeepRecentResolutionEntries = 20,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path (Join-Path $PSScriptRoot "v2") "Common.ps1")

function Split-MarkdownEntries {
    param(
        [string[]]$Lines,
        [string]$EntryPattern = "^##\s+"
    )

    $entries = New-Object System.Collections.Generic.List[object]
    $current = New-Object System.Collections.Generic.List[string]
    $started = $false

    foreach ($line in $Lines) {
        if ($line -match $EntryPattern) {
            if ($started -and $current.Count -gt 0) {
                $entries.Add(@($current.ToArray()))
                $current = New-Object System.Collections.Generic.List[string]
            }
            $started = $true
        }
        if ($started) {
            $current.Add($line)
        }
    }

    if ($current.Count -gt 0) {
        $entries.Add(@($current.ToArray()))
    }

    return @($entries.ToArray())
}

function Write-ArchiveChunk {
    param(
        [string]$ArchivePath,
        [string]$Header,
        [object[]]$Entries
    )

    Ensure-V2Directory -Path (Split-Path -Parent $ArchivePath)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# $Header")
    $lines.Add("")
    $lines.Add("- archived_at: $(Get-V2Timestamp)")
    $lines.Add("- entry_count: $(@($Entries).Count)")
    $lines.Add("")
    foreach ($entry in @($Entries)) {
        foreach ($entryLine in @($entry)) {
            $lines.Add([string]$entryLine)
        }
        $lines.Add("")
    }
    [System.IO.File]::WriteAllText($ArchivePath, ($lines -join [Environment]::NewLine))
}

function Compress-EventLog {
    param(
        [string]$Path,
        [string]$ArchiveRoot,
        [int]$LineThreshold,
        [int]$KeepRecentEntries
    )

    $result = [PSCustomObject]@{
        compressed = $false
        source = $Path
        archive = ""
        archived_entries = 0
        kept_entries = 0
        reason = ""
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $result.reason = "missing-source"
        return $result
    }

    $allLines = Get-Content -LiteralPath $Path
    if ($allLines.Count -le $LineThreshold) {
        $result.reason = "below-threshold"
        return $result
    }

    $entries = @(Split-MarkdownEntries -Lines $allLines -EntryPattern "^##\s+")
    if ($entries.Count -le $KeepRecentEntries) {
        $result.reason = "insufficient-entries"
        return $result
    }

    $archiveEntries = @($entries | Select-Object -First ($entries.Count - $KeepRecentEntries))
    $keepEntries = @($entries | Select-Object -Last $KeepRecentEntries)
    $archiveFile = Join-Path $ArchiveRoot ("event_log_{0}.md" -f (Get-Date -Format "yyyy_MM_dd_HHmmss"))

    Write-ArchiveChunk -ArchivePath $archiveFile -Header "Event Log Archive" -Entries $archiveEntries

    $digest = New-Object System.Collections.Generic.List[string]
    $digest.Add("# Event Log")
    $digest.Add("")
    $digest.Add("> Compressed on $(Get-V2Timestamp) by scripts/Invoke-LongTermCompression.ps1")
    $digest.Add("> Archived entries: $($archiveEntries.Count)")
    $digest.Add("> Archive file: $archiveFile")
    $digest.Add("")
    $digest.Add("## Active Window")
    $digest.Add("")
    foreach ($entry in $keepEntries) {
        foreach ($line in @($entry)) {
            $digest.Add([string]$line)
        }
        $digest.Add("")
    }

    [System.IO.File]::WriteAllText($Path, ($digest -join [Environment]::NewLine))

    $result.compressed = $true
    $result.archive = $archiveFile
    $result.archived_entries = $archiveEntries.Count
    $result.kept_entries = $keepEntries.Count
    $result.reason = "compressed"
    return $result
}

function Compress-ResolutionLog {
    param(
        [string]$Path,
        [string]$ArchiveRoot,
        [int]$EntryThreshold,
        [int]$KeepRecentEntries
    )

    $result = [PSCustomObject]@{
        compressed = $false
        source = $Path
        archive = ""
        archived_entries = 0
        kept_entries = 0
        reason = ""
    }

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $result.reason = "missing-source"
        return $result
    }

    $allLines = Get-Content -LiteralPath $Path
    $entries = @(Split-MarkdownEntries -Lines $allLines -EntryPattern "^##\s+")
    if ($entries.Count -le $EntryThreshold) {
        $result.reason = "below-threshold"
        return $result
    }
    if ($entries.Count -le $KeepRecentEntries) {
        $result.reason = "insufficient-entries"
        return $result
    }

    $archiveEntries = @($entries | Select-Object -First ($entries.Count - $KeepRecentEntries))
    $keepEntries = @($entries | Select-Object -Last $KeepRecentEntries)
    $archiveFile = Join-Path $ArchiveRoot ("resolution_log_{0}.md" -f (Get-Date -Format "yyyy_MM_dd_HHmmss"))
    Write-ArchiveChunk -ArchivePath $archiveFile -Header "Resolution Log Archive" -Entries $archiveEntries

    $out = New-Object System.Collections.Generic.List[string]
    $out.Add("# Resolution Log")
    $out.Add("")
    $out.Add("> Compressed on $(Get-V2Timestamp) by scripts/Invoke-LongTermCompression.ps1")
    $out.Add("> Archived entries: $($archiveEntries.Count)")
    $out.Add("> Archive file: $archiveFile")
    $out.Add("")
    foreach ($entry in $keepEntries) {
        foreach ($line in @($entry)) {
            $out.Add([string]$line)
        }
        $out.Add("")
    }

    [System.IO.File]::WriteAllText($Path, ($out -join [Environment]::NewLine))
    $result.compressed = $true
    $result.archive = $archiveFile
    $result.archived_entries = $archiveEntries.Count
    $result.kept_entries = $keepEntries.Count
    $result.reason = "compressed"
    return $result
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$archiveRoot = Join-Path $repoRoot "archive"
Ensure-V2Directory -Path $archiveRoot

$eventLogPath = Join-Path $repoRoot "docs/agents/EVENT_LOG.md"
$resolutionLogPath = Join-Path $repoRoot "self_healing/resolution_log.md"

$eventResult = Compress-EventLog `
    -Path $eventLogPath `
    -ArchiveRoot $archiveRoot `
    -LineThreshold $MaxEventLogLines `
    -KeepRecentEntries $KeepRecentEventEntries

$resolutionResult = Compress-ResolutionLog `
    -Path $resolutionLogPath `
    -ArchiveRoot $archiveRoot `
    -EntryThreshold $MaxResolutionEntries `
    -KeepRecentEntries $KeepRecentResolutionEntries

$reportPath = Join-Path $resolvedProjectPath "ai-orchestrator/reports/compression-report.md"
Append-V2MarkdownLog -Path $reportPath -Header "# Compression Report" -Lines @(
    "## $(Get-V2Timestamp)",
    "- event_log: $($eventResult.reason)",
    "- event_log_archive: $($eventResult.archive)",
    "- resolution_log: $($resolutionResult.reason)",
    "- resolution_log_archive: $($resolutionResult.archive)"
)

$summary = [PSCustomObject]@{
    project_path = $resolvedProjectPath
    event_log = $eventResult
    resolution_log = $resolutionResult
    report_path = $reportPath
    completed_at = Get-V2Timestamp
}

if ($EmitJson) {
    $summary | ConvertTo-Json -Depth 8
}
else {
    Write-Output "Compression run complete."
    Write-Output "Event log: $($eventResult.reason)"
    Write-Output "Resolution log: $($resolutionResult.reason)"
}
