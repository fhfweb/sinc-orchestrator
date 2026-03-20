<#
.SYNOPSIS
    Generates a Memory X-Ray visualization for a project (Mermaid diagram + optional HTML).
.DESCRIPTION
    Reads .ai-orchestrator/state/, tasks/, analysis/, and knowledge_base/ to produce
    a visual map of the project memory state including:
    - Task DAG as a Mermaid flowchart
    - Lock state overlay
    - Module dependency graph
    - Knowledge base lesson count
    Output: .ai-orchestrator/reports/memory-xray.md (and memory-xray.html if -EmitHtml)
.PARAMETER ProjectPath
    Path to the project root containing .ai-orchestrator/. Defaults to current directory.
.PARAMETER EmitHtml
    If set, also generates an HTML file with embedded Mermaid rendering.
.PARAMETER OutputPath
    Override default output path.
.EXAMPLE
    .\scripts\Visualize-Memory.ps1 -ProjectPath C:\projects\myapp
    .\scripts\Visualize-Memory.ps1 -ProjectPath C:\projects\myapp -EmitHtml
    .\orchestrator.ps1 -Action v2-visualize -ProjectPath C:\projects\myapp -EmitHtml
#>param(
    [string]$ProjectPath = ".",
    [string]$OutputPath = "",
    [switch]$EmitHtml,
    [string]$HtmlOutputPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "v2/Common.ps1")

function Convert-ToMermaidId {
    param([string]$Raw)

    if ([string]::IsNullOrWhiteSpace($Raw)) {
        return "n_empty"
    }

    $clean = ($Raw -replace "[^A-Za-z0-9_]", "_")
    if ($clean -match "^[0-9]") {
        $clean = "n_$clean"
    }
    return $clean
}

function Add-MermaidNode {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [hashtable]$Seen,
        [string]$Id,
        [string]$Label
    )

    if ($Seen.ContainsKey($Id)) {
        return
    }

    $safeLabel = ($Label -replace '"', "'")
    $Lines.Add(('{0}["{1}"]' -f $Id, $safeLabel))
    $Seen[$Id] = $true
}

function Add-MermaidEdge {
    param(
        [System.Collections.Generic.List[string]]$Lines,
        [hashtable]$Seen,
        [string]$Source,
        [string]$Target,
        [string]$Label
    )

    $key = "$Source|$Target|$Label"
    if ($Seen.ContainsKey($key)) {
        return
    }

    $safeLabel = ($Label -replace '"', "'")
    $Lines.Add(('{0} -->|"{1}"| {2}' -f $Source, $safeLabel, $Target))
    $Seen[$key] = $true
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
if (-not (Test-Path -LiteralPath $orchestratorRoot -PathType Container)) {
    throw "ai-orchestrator directory not found: $orchestratorRoot"
}

$statePath = Join-Path $orchestratorRoot "state/project-state.json"
$worldModelPath = Join-Path $orchestratorRoot "state/world-model-auto.json"

$state = Get-V2JsonContent -Path $statePath
if (-not $state) {
    throw "project-state.json not found or invalid JSON: $statePath"
}

$dependencyGraph = Get-V2OptionalProperty -InputObject (Get-V2OptionalProperty -InputObject $state -Name "analysis" -DefaultValue $null) -Name "dependency_graph" -DefaultValue ([PSCustomObject]@{})
$modules = @(Get-V2OptionalProperty -InputObject $dependencyGraph -Name "modules" -DefaultValue @())
$moduleEdges = @(Get-V2OptionalProperty -InputObject $dependencyGraph -Name "edges" -DefaultValue @())

$worldModel = Get-V2JsonContent -Path $worldModelPath
$entities = @()
if ($worldModel -and ($worldModel.PSObject.Properties.Name -contains "entities")) {
    $entities = @($worldModel.entities)
}

$graphLines = New-Object System.Collections.Generic.List[string]
$graphLines.Add("graph LR")
$nodeSeen = @{}
$edgeSeen = @{}

foreach ($module in $modules) {
    $moduleName = [string]$module
    if ([string]::IsNullOrWhiteSpace($moduleName)) { continue }
    Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id (Convert-ToMermaidId -Raw ("mod_" + $moduleName)) -Label ("Module: " + $moduleName)
}

foreach ($edge in $moduleEdges) {
    if (-not $edge) { continue }
    $source = [string](Get-V2OptionalProperty -InputObject $edge -Name "source" -DefaultValue "")
    $target = [string](Get-V2OptionalProperty -InputObject $edge -Name "target" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($source) -or [string]::IsNullOrWhiteSpace($target)) { continue }

    $sourceId = Convert-ToMermaidId -Raw ("mod_" + $source)
    $targetId = Convert-ToMermaidId -Raw ("mod_" + $target)
    Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id $sourceId -Label ("Module: " + $source)
    Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id $targetId -Label ("Module: " + $target)
    Add-MermaidEdge -Lines $graphLines -Seen $edgeSeen -Source $sourceId -Target $targetId -Label "DEPENDS_ON"
}

foreach ($entity in $entities) {
    if (-not $entity) { continue }
    $entityKey = [string](Get-V2OptionalProperty -InputObject $entity -Name "key" -DefaultValue "")
    $entityName = [string](Get-V2OptionalProperty -InputObject $entity -Name "name" -DefaultValue $entityKey)
    if ([string]::IsNullOrWhiteSpace($entityKey)) { continue }

    $entityId = Convert-ToMermaidId -Raw ("ent_" + $entityKey)
    Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id $entityId -Label ("Entity: " + $entityName)

    foreach ($file in @(Get-V2OptionalProperty -InputObject $entity -Name "files" -DefaultValue @())) {
        $fileText = ([string]$file).Replace('\', '/')
        if ([string]::IsNullOrWhiteSpace($fileText)) { continue }
        $moduleName = if ($fileText.Contains("/")) { $fileText.Split("/")[0].ToLowerInvariant() } else { "root" }
        $moduleId = Convert-ToMermaidId -Raw ("mod_" + $moduleName)
        Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id $moduleId -Label ("Module: " + $moduleName)
        Add-MermaidEdge -Lines $graphLines -Seen $edgeSeen -Source $entityId -Target $moduleId -Label "IMPLEMENTED_IN"
    }

    foreach ($relationship in @(Get-V2OptionalProperty -InputObject $entity -Name "relationships" -DefaultValue @())) {
        if (-not $relationship) { continue }
        $targetKey = [string](Get-V2OptionalProperty -InputObject $relationship -Name "target" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($targetKey)) { continue }
        $type = [string](Get-V2OptionalProperty -InputObject $relationship -Name "type" -DefaultValue "RELATES_TO")
        $targetId = Convert-ToMermaidId -Raw ("ent_" + $targetKey)
        Add-MermaidNode -Lines $graphLines -Seen $nodeSeen -Id $targetId -Label ("Entity: " + $targetKey)
        Add-MermaidEdge -Lines $graphLines -Seen $edgeSeen -Source $entityId -Target $targetId -Label $type
    }
}

$finalOutputPath = ""
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $finalOutputPath = Join-Path $orchestratorRoot "reports/memory-xray.md"
}
elseif ([System.IO.Path]::IsPathRooted($OutputPath)) {
    $finalOutputPath = $OutputPath
}
else {
    $finalOutputPath = Join-Path $resolvedProjectPath $OutputPath
}

$reportLines = New-Object System.Collections.Generic.List[string]
$reportLines.Add("# Memory X-Ray")
$reportLines.Add("")
$reportLines.Add("- Generated At: $(Get-V2Timestamp)")
$reportLines.Add("- Project: $resolvedProjectPath")
$reportLines.Add("- Modules: $(@($modules).Count)")
$reportLines.Add("- Module Edges: $(@($moduleEdges).Count)")
$reportLines.Add("- Entities: $(@($entities).Count)")
$reportLines.Add("")
$reportLines.Add("## Mermaid")
$reportLines.Add("")
$reportLines.Add('```mermaid')
foreach ($line in $graphLines) {
    $reportLines.Add($line)
}
$reportLines.Add('```')

[System.IO.File]::WriteAllText($finalOutputPath, ($reportLines -join [Environment]::NewLine))

if ($EmitHtml) {
    $targetHtmlPath = ""
    if ([string]::IsNullOrWhiteSpace($HtmlOutputPath)) {
        $targetHtmlPath = Join-Path $orchestratorRoot "reports/memory-xray.html"
    }
    elseif ([System.IO.Path]::IsPathRooted($HtmlOutputPath)) {
        $targetHtmlPath = $HtmlOutputPath
    }
    else {
        $targetHtmlPath = Join-Path $resolvedProjectPath $HtmlOutputPath
    }

    $htmlLines = New-Object System.Collections.Generic.List[string]
    $htmlLines.Add("<html><body><pre>")
    foreach ($line in $reportLines) {
        $escaped = $line.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;")
        $htmlLines.Add($escaped)
    }
    $htmlLines.Add("</pre></body></html>")
    [System.IO.File]::WriteAllText($targetHtmlPath, ($htmlLines -join [Environment]::NewLine))
}

Write-Output ('Memory visualization generated: {0}' -f $finalOutputPath)
if ($EmitHtml) {
    Write-Output 'HTML visualization generated.'
}

