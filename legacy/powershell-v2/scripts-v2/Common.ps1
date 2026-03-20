# Common.ps1 — Shared utility functions for all V2 orchestration scripts.
# Dot-source this file at the top of any V2 script: . (Join-Path $PSScriptRoot 'Common.ps1')
#
# Provides: Get-V2ProjectSlug, Get-V2JsonFile, Get-V2TextFile, Get-V2RepoRoot,
#           Get-RelativeProjectFiles, Get-FirstMatches, Assert-CoordinationExecutionAllowed

Set-StrictMode -Version Latest

function Get-V2CoordinationState {
    param([string]$ProjectRoot)

    if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
        return $null
    }

    $candidateRoots = New-Object System.Collections.Generic.List[string]
    $candidateRoots.Add($ProjectRoot)
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    if (-not [string]::IsNullOrWhiteSpace($repoRoot)) {
        $candidateRoots.Add($repoRoot)
    }

    foreach ($root in @($candidateRoots.ToArray() | Select-Object -Unique)) {
        $candidates = @(
            (Join-Path $root "ai-orchestrator/state/coordination-mode.json"),
            (Join-Path $root ".ai-orchestrator/state/coordination-mode.json")
        )
        foreach ($path in $candidates) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                continue
            }
            try {
                $state = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
                if ($state) {
                    return $state
                }
            }
            catch {
            }
        }
    }

    return $null
}

function Assert-V2ExecutionEnabled {
    param(
        [string]$ProjectRoot,
        [string]$ActionName = "v2-action"
    )

    $coordination = Get-V2CoordinationState -ProjectRoot $ProjectRoot
    if (-not $coordination) {
        return
    }

    $mode = [string](Get-V2OptionalProperty -InputObject $coordination -Name "mode" -DefaultValue "execution-enabled")
    if ($mode -ne "planning-only") {
        return
    }

    $releaseCommand = [string](Get-V2OptionalProperty -InputObject $coordination -Name "release_trigger_command" -DefaultValue "liberado para implementar")
    throw "Action '$ActionName' blocked by coordination mode planning-only. Release first using confirm phrase '$releaseCommand'."
}

function Get-V2ProjectSlug {
    param([string]$Name)

    $slug = $Name.ToLowerInvariant() -replace "[^a-z0-9]+", "-"
    $slug = $slug.Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) {
        return "project"
    }

    return $slug
}

function Get-V2RepoRoot {
    # The repo root is usually the parent of 'ai-orchestrator' or where 'docs' exists.
    # From scripts/v2, it's ..
    # From ai-orchestrator/scripts/v2, it's ../../..
    $p = [System.IO.Path]::GetFullPath($PSScriptRoot)
    while (-not [string]::IsNullOrWhiteSpace($p)) {
        if (Test-Path -LiteralPath (Join-Path $p ".git") -PathType Container) { return $p }
        if (Test-Path -LiteralPath (Join-Path $p "ai-orchestrator") -PathType Container) { return $p }
        if (Test-Path -LiteralPath (Join-Path $p "docs/agents/agents-360.registry.json") -PathType Leaf) { return $p }
        $parent = Split-Path -Parent $p
        if ($parent -eq $p) { break }
        $p = $parent
    }
    # Fallback to current script logic if discovery fails
    return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
}

function Resolve-V2AbsolutePath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    # Normalize separators for consistent checking
    $normalizedPath = $Path -replace "\\", "/"

    # Handle stale Unix-style roots (e.g., from Docker) on Windows
    # If the path starts with /workspace (common in our orchestration setup),
    # we remap it to the actual project root.
    # Skip remapping if /workspace actually exists (running inside a Docker container).
    if ($normalizedPath -like "/workspace*" -and -not (Test-Path -LiteralPath "/workspace" -PathType Container)) {
        $repoRoot = Get-V2RepoRoot
        
        # remove "/workspace" prefix
        $relPath = $normalizedPath.Substring(10) 
        if ($relPath.StartsWith("/")) { $relPath = $relPath.Substring(1) }
        
        # The path might be /workspace/ai-orchestrator/... but ai-orchestrator is IN the repoRoot
        if ($relPath.StartsWith("ai-orchestrator/")) {
            $relPath = $relPath.Substring(16)
        }
        elseif ($relPath -eq "ai-orchestrator") {
            $relPath = ""
        }
        
        if ([string]::IsNullOrWhiteSpace($relPath)) {
            return $repoRoot
        }
        return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $relPath.Replace("/", "\")))
    }

    if (Test-Path -LiteralPath $Path) {
        return (Resolve-Path -LiteralPath $Path).Path
    }

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }

    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Path))
}

function Initialize-V2Directory {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-V2FileMutexName {
    param(
        [string]$Path,
        [string]$Prefix = "V2File"
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path).ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($fullPath)
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $hash = $sha1.ComputeHash($bytes)
        $token = ([System.BitConverter]::ToString($hash)).Replace("-", "")
        return ("Global\{0}_{1}" -f $Prefix, $token)
    }
    finally {
        $sha1.Dispose()
    }
}

function Invoke-V2WithFileMutex {
    param(
        [string]$Path,
        [scriptblock]$ScriptBlock,
        [int]$TimeoutSeconds = 20,
        [string]$Prefix = "V2File"
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Invoke-V2WithFileMutex requires a file path."
    }
    if (-not $ScriptBlock) {
        throw "Invoke-V2WithFileMutex requires a script block."
    }

    $safeTimeout = [Math]::Max(1, $TimeoutSeconds)
    $mutexName = Get-V2FileMutexName -Path $Path -Prefix $Prefix
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    $hasMutex = $false
    try {
        $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds($safeTimeout))
        if (-not $hasMutex) {
            throw "file-mutex-timeout:$mutexName"
        }
        return (& $ScriptBlock)
    }
    finally {
        if ($hasMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Get-V2DagMutexName {
    param([string]$DagPath)

    return (Get-V2FileMutexName -Path $DagPath -Prefix "V2TaskDag")
}

function Invoke-V2WithDagMutex {
    param(
        [string]$DagPath,
        [scriptblock]$ScriptBlock,
        [int]$TimeoutSeconds = 30
    )

    return (Invoke-V2WithFileMutex -Path $DagPath -Prefix "V2TaskDag" -TimeoutSeconds $TimeoutSeconds -ScriptBlock $ScriptBlock)
}

function Test-V2TaskDagPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    return ([System.IO.Path]::GetFileName($Path).ToLowerInvariant() -eq "task-dag.json")
}

function Test-V2WhiteboardPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    return ([System.IO.Path]::GetFileName($Path).ToLowerInvariant() -eq "whiteboard.json")
}

function Invoke-V2TaskDb {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Mode,
        [string]$Query = "open-execution",
        [string]$TasksJsonPath = "",
        [string]$DagPath = "",
        [int]$Limit = 20
    )

    $repoRoot = Get-V2RepoRoot
    $pyScript = Join-Path $repoRoot "scripts/v2/task_state_db.py"
    if (-not (Test-Path -LiteralPath $pyScript)) {
        return $null
    }

    $projectRoot = $null
    if ($Mode -eq "query" -or $Mode -eq "status" -or $Mode -eq "sync" -or $Mode -eq "flush-dag") {
        # We need project root to find .env or state
        # In this context, we usually have a global/ambient ProjectPath or we can infer it.
        # But for reliability, we'll try to use Get-Location if not provided.
        $projectRoot = (Get-Location).Path
    }

    $args = @("--project-path", $repoRoot, "--mode", $Mode, "--emit-json")
    if ($Mode -eq "query") {
        $args += @("--query", $Query, "--limit", [string]$Limit)
    }
    if (-not [string]::IsNullOrWhiteSpace($TasksJsonPath)) {
        $args += @("--tasks-json-path", $TasksJsonPath)
    }
    if (-not [string]::IsNullOrWhiteSpace($DagPath)) {
        $args += @("--dag-path", $DagPath)
    }

    try {
        Write-Host "[DEBUG] Invoke-V2TaskDb: python $pyScript $($args -join ' ')"
        $output = & python $pyScript @args | Out-String
        if ([string]::IsNullOrWhiteSpace($output)) { 
            Write-Host "[DEBUG] Invoke-V2TaskDb: Empty output"
            return $null 
        }
        $json = $output | ConvertFrom-Json
        return $json
    }
    catch {
        Write-Host "[DEBUG] Invoke-V2TaskDb: Error - $($_.Exception.Message)"
        return $null
    }
}

function Get-V2JsonContent {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    # Intercept whiteboard.json lookups
    if (Test-V2WhiteboardPath -Path $Path) {
        $dbResult = Invoke-V2TaskDb -Mode "whiteboard-status"
        if ($dbResult -and $dbResult.entries) {
            return $dbResult
        }
    }

    # Intercept task-dag.json lookups to ensure we get canonical state if DB is primary
    if (Test-V2TaskDagPath -Path $Path) {
        $dbStatus = Invoke-V2TaskDb -Mode "status"
        if ($dbStatus -and $dbStatus.ok -and $dbStatus.backend_mode -eq "db-primary-v1") {
            # In db-primary mode, the JSON might be stale if it wasn't flushed.
            # However, most scripts expect the FULL document structure (metadata + tasks).
            # If we query all tasks, we lose top-level metadata.
            # So we usually prefer to rely on the JSON being kept in sync by Save-V2JsonContent.
            # But let's check if the DB has a newer "scheduler_last_write_at" than the file.
            $fileTime = (Get-Item -LiteralPath $Path).LastWriteTime.ToString("s")
            $dbTime = [string]$dbStatus.scheduler_last_write_at
            
            # If DB is significantly newer, or if we just want to be safe, we could force a flush.
            # But flush is expensive. For now, we trust the projection.
        }
    }

    $reader = {
        try {
            $utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
            $raw = [System.IO.File]::ReadAllText($Path, $utf8Strict)
        }
        catch {
            try {
                $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
            }
            catch {
                return $null
            }
        }

        if ([string]::IsNullOrWhiteSpace($raw)) {
            return $null
        }

        try {
            return ($raw | ConvertFrom-Json)
        }
        catch {
            return $null
        }
    }

    if (Test-V2TaskDagPath -Path $Path) {
        return (Invoke-V2WithDagMutex -DagPath $Path -ScriptBlock $reader)
    }
    return (& $reader)
}

function Save-V2JsonContent {
    param(
        [string]$Path,
        [object]$Value
    )

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Initialize-V2Directory -Path $parent
    }

    # Intercept whiteboard.json writes
    if (Test-V2WhiteboardPath -Path $Path) {
        $tempJson = [System.IO.Path]::GetTempFileName()
        try {
            # Convert whole whiteboard object to temporary JSON file
            $Value | ConvertTo-Json -Depth 16 | Out-File -FilePath $tempJson -Encoding utf8
            $dbResult = Invoke-V2TaskDb -Mode "write-whiteboard" -TasksJsonPath $tempJson
            
            if ($dbResult -and $dbResult.ok) {
                # Update the mirror file from the DB
                $flushResult = Invoke-V2TaskDb -Mode "flush-whiteboard" -DagPath $Path
                if ($flushResult -and $flushResult.ok) {
                    return
                }
            }
        }
        finally {
            if (Test-Path -LiteralPath $tempJson) { Remove-Item -LiteralPath $tempJson -Force }
        }
    }

    # Intercept task-dag.json writes to ensure the DB (Postgres/SQLite) is updated first
    if (Test-V2TaskDagPath -Path $Path -and $Value.tasks) {
        Write-Host "[DEBUG] Intercepting write to task-dag.json. Tasks count: $($Value.tasks.Count)"
        $tempJson = [System.IO.Path]::GetTempFileName()
        try {
            # Convert tasks array to temporary JSON file for task_state_db.py
            @($Value.tasks) | ConvertTo-Json -Depth 16 | Out-File -FilePath $tempJson -Encoding utf8
            $dbResult = Invoke-V2TaskDb -Mode "write-tasks" -TasksJsonPath $tempJson
            
            if ($dbResult -and $dbResult.ok) {
                Write-Host "[DEBUG] DB write-tasks success. Flushing DAG..."
                $flushResult = Invoke-V2TaskDb -Mode "flush-dag" -DagPath $Path
                if ($flushResult -and $flushResult.ok) {
                    Write-Host "[DEBUG] DAG flush success."
                    return
                } else {
                    Write-Host "[DEBUG] DAG flush failed: $($flushResult | ConvertTo-Json)"
                }
            } else {
                Write-Host "[DEBUG] DB write-tasks failed: $($dbResult | ConvertTo-Json)"
            }
        }
        finally {
            if (Test-Path -LiteralPath $tempJson) { Remove-Item -LiteralPath $tempJson -Force }
        }
        # If DB write or flush failed, we fall back to direct file write to avoid data loss
    }

    $writer = {
        $json = $Value | ConvertTo-Json -Depth 16
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $targetPath = [System.IO.Path]::GetFullPath($Path)
        $targetParent = Split-Path -Parent $targetPath
        if (-not [string]::IsNullOrWhiteSpace($targetParent)) {
            Initialize-V2Directory -Path $targetParent
        }

        $tempPath = Join-Path $targetParent ("{0}.{1}.tmp" -f [System.IO.Path]::GetFileName($targetPath), [System.Guid]::NewGuid().ToString("N"))
        $backupPath = "$targetPath.bak"

        try {
            [System.IO.File]::WriteAllText($tempPath, $json, $utf8NoBom)
            if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
                try {
                    [System.IO.File]::Replace($tempPath, $targetPath, $backupPath, $true)
                    if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
                        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
                    }
                }
                catch {
                    Move-Item -LiteralPath $tempPath -Destination $targetPath -Force
                }
            }
            else {
                Move-Item -LiteralPath $tempPath -Destination $targetPath -Force
            }
        }
        finally {
            if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
                Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if (Test-V2TaskDagPath -Path $Path) {
        [void](Invoke-V2WithDagMutex -DagPath $Path -ScriptBlock $writer)
        return
    }

    [void](Invoke-V2WithFileMutex -Path $Path -ScriptBlock $writer -Prefix "V2FileWrite")
}

function Get-V2RelativeUnixPath {
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

function Write-V2File {
    param(
        [string]$Path,
        [string]$Content,
        [switch]$Force
    )

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Initialize-V2Directory -Path $parent
    }

    if ((Test-Path -LiteralPath $Path) -and -not $Force) {
        return
    }

    [System.IO.File]::WriteAllText($Path, $Content)
}

function Get-V2OptionalProperty {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$DefaultValue = $null
    )

    if ($null -eq $InputObject) {
        return $DefaultValue
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) {
            return $InputObject[$Name]
        }
        return $DefaultValue
    }

    try {
        $property = $InputObject.PSObject.Properties[$Name]
        if ($null -ne $property) {
            return $property.Value
        }
    }
    catch {
    }

    return $DefaultValue
}

function Set-V2DynamicProperty {
    param(
        [object]$InputObject,
        [string]$Name,
        [object]$Value
    )

    if ($null -eq $InputObject -or [string]::IsNullOrWhiteSpace($Name)) {
        return
    }

    if ($InputObject -is [System.Collections.IDictionary]) {
        $InputObject[$Name] = $Value
        return
    }

    $propertyNames = @()
    try {
        $propertyNames = @($InputObject.PSObject.Properties | ForEach-Object { $_.Name })
    }
    catch {
        $propertyNames = @()
    }

    if ($Name -in $propertyNames) {
        $InputObject.$Name = $Value
    }
    else {
        Add-Member -InputObject $InputObject -MemberType NoteProperty -Name $Name -Value $Value -Force
    }
}

function Get-V2SourceModuleFromPath {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ""
    }

    $normalized = $PathValue -replace "\\", "/"
    $parts = @($normalized -split "/" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts.Count -eq 0) {
        return ""
    }
    $pathSegments = @($parts)
    $lastSegment = [string]$pathSegments[$pathSegments.Count - 1]
    if ($lastSegment.Contains(".")) {
        if ($pathSegments.Count -eq 1) {
            return ""
        }
        $pathSegments = @($pathSegments[0..($pathSegments.Count - 2)])
    }
    if ($pathSegments.Count -eq 1) {
        return $pathSegments[0]
    }
    return ($pathSegments[0] + "/" + $pathSegments[1])
}

function Invoke-V2MemoryModuleIndex {
    <#
    .SYNOPSIS
        Stores per-file and per-module execution episodes for reuse.
    .DESCRIPTION
        Updates ai-orchestrator/memory/module-index.json with counters and latest task context.
    #>
    param(
        [string]$ProjectRoot,
        [string]$TaskId,
        [string]$AgentName,
        [string[]]$SourceFiles = @(),
        [string[]]$SourceModules = @(),
        [string]$Outcome = "unknown"
    )

    if ([string]::IsNullOrWhiteSpace($ProjectRoot) -or -not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
        return $null
    }

    $memoryDir = Join-Path $ProjectRoot "ai-orchestrator/memory"
    Initialize-V2Directory -Path $memoryDir
    $indexPath = Join-Path $memoryDir "module-index.json"
    $index = Get-V2JsonContent -Path $indexPath
    if (-not $index) {
        $index = [PSCustomObject]@{
            generated_at = Get-V2Timestamp
            files = [PSCustomObject]@{}
            modules = [PSCustomObject]@{}
        }
    }

    $fileMap = Get-V2OptionalProperty -InputObject $index -Name "files" -DefaultValue ([PSCustomObject]@{})
    $moduleMap = Get-V2OptionalProperty -InputObject $index -Name "modules" -DefaultValue ([PSCustomObject]@{})

    $normalizedFiles = @($SourceFiles | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
    $normalizedModules = New-Object System.Collections.Generic.List[string]
    foreach ($mod in @($SourceModules)) {
        $text = [string]$mod
        if (-not [string]::IsNullOrWhiteSpace($text) -and -not $normalizedModules.Contains($text)) {
            $normalizedModules.Add($text)
        }
    }
    foreach ($filePath in $normalizedFiles) {
        $derived = Get-V2SourceModuleFromPath -PathValue $filePath
        if (-not [string]::IsNullOrWhiteSpace($derived) -and -not $normalizedModules.Contains($derived)) {
            $normalizedModules.Add($derived)
        }
    }

    foreach ($filePath in $normalizedFiles) {
        $entry = Get-V2OptionalProperty -InputObject $fileMap -Name $filePath -DefaultValue $null
        if (-not $entry) {
            $entry = [PSCustomObject]@{
                count = 0
                last_task_id = ""
                last_agent = ""
                last_outcome = ""
                updated_at = ""
            }
        }
        Set-V2DynamicProperty -InputObject $entry -Name "count" -Value ([int](Get-V2OptionalProperty -InputObject $entry -Name "count" -DefaultValue 0) + 1)
        Set-V2DynamicProperty -InputObject $entry -Name "last_task_id" -Value $TaskId
        Set-V2DynamicProperty -InputObject $entry -Name "last_agent" -Value $AgentName
        Set-V2DynamicProperty -InputObject $entry -Name "last_outcome" -Value $Outcome
        Set-V2DynamicProperty -InputObject $entry -Name "updated_at" -Value (Get-V2Timestamp)
        Set-V2DynamicProperty -InputObject $fileMap -Name $filePath -Value $entry
    }

    foreach ($moduleName in @($normalizedModules.ToArray())) {
        $entry = Get-V2OptionalProperty -InputObject $moduleMap -Name $moduleName -DefaultValue $null
        if (-not $entry) {
            $entry = [PSCustomObject]@{
                count = 0
                last_task_id = ""
                last_agent = ""
                last_outcome = ""
                updated_at = ""
            }
        }
        Set-V2DynamicProperty -InputObject $entry -Name "count" -Value ([int](Get-V2OptionalProperty -InputObject $entry -Name "count" -DefaultValue 0) + 1)
        Set-V2DynamicProperty -InputObject $entry -Name "last_task_id" -Value $TaskId
        Set-V2DynamicProperty -InputObject $entry -Name "last_agent" -Value $AgentName
        Set-V2DynamicProperty -InputObject $entry -Name "last_outcome" -Value $Outcome
        Set-V2DynamicProperty -InputObject $entry -Name "updated_at" -Value (Get-V2Timestamp)
        Set-V2DynamicProperty -InputObject $moduleMap -Name $moduleName -Value $entry
    }

    Set-V2DynamicProperty -InputObject $index -Name "generated_at" -Value (Get-V2Timestamp)
    Set-V2DynamicProperty -InputObject $index -Name "files" -Value $fileMap
    Set-V2DynamicProperty -InputObject $index -Name "modules" -Value $moduleMap
    Save-V2JsonContent -Path $indexPath -Value $index

    return $indexPath
}

function New-V2PhaseApprovalEntry {
    param(
        [string]$Status = "pending",
        [string]$UpdatedBy = "system-default"
    )

    return [PSCustomObject]@{
        status     = $Status
        updated_at = Get-V2Timestamp
        updated_by = $UpdatedBy
    }
}

function Initialize-V2PhaseApprovals {
    <#
    .SYNOPSIS
        Ensures project-state has phase_approvals with context/architecture/execution/release.
    .DESCRIPTION
        Backfills legacy project-state.json documents that predate phase approval gates.
        Returns $true when state was mutated.
    #>
    param(
        [object]$ProjectState,
        [string]$UpdatedBy = "system-auto"
    )

    if ($null -eq $ProjectState) {
        return $false
    }

    $changed = $false
    $phaseApprovals = Get-V2OptionalProperty -InputObject $ProjectState -Name "phase_approvals" -DefaultValue $null
    if ($null -eq $phaseApprovals) {
        $phaseApprovals = [PSCustomObject]@{}
        $changed = $true
    }

    $defaults = [ordered]@{
        context      = "approved"
        architecture = "pending"
        execution    = "pending"
        release      = "pending"
    }

    foreach ($phase in @($defaults.Keys)) {
        $defaultStatus = [string]$defaults[$phase]
        $entry = Get-V2OptionalProperty -InputObject $phaseApprovals -Name $phase -DefaultValue $null
        if ($null -eq $entry) {
            Set-V2DynamicProperty -InputObject $phaseApprovals -Name $phase -Value (New-V2PhaseApprovalEntry -Status $defaultStatus -UpdatedBy $UpdatedBy)
            $changed = $true
            continue
        }

        $status = [string](Get-V2OptionalProperty -InputObject $entry -Name "status" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($status)) {
            Set-V2DynamicProperty -InputObject $entry -Name "status" -Value $defaultStatus
            $changed = $true
        }
        else {
            $normalizedStatus = $status.ToLowerInvariant()
            if ($normalizedStatus -ne $status) {
                Set-V2DynamicProperty -InputObject $entry -Name "status" -Value $normalizedStatus
                $changed = $true
            }
        }

        $updatedAt = [string](Get-V2OptionalProperty -InputObject $entry -Name "updated_at" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($updatedAt)) {
            Set-V2DynamicProperty -InputObject $entry -Name "updated_at" -Value (Get-V2Timestamp)
            $changed = $true
        }

        $entryUpdatedBy = [string](Get-V2OptionalProperty -InputObject $entry -Name "updated_by" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($entryUpdatedBy)) {
            Set-V2DynamicProperty -InputObject $entry -Name "updated_by" -Value $UpdatedBy
            $changed = $true
        }
    }

    Set-V2DynamicProperty -InputObject $ProjectState -Name "phase_approvals" -Value $phaseApprovals
    return $changed
}

function Add-V2MarkdownLog {
    param(
        [string]$Path,
        [string]$Header,
        [string[]]$Lines
    )

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Initialize-V2Directory -Path $parent
    }

    if (-not (Test-Path -LiteralPath $Path)) {
        [System.IO.File]::WriteAllText($Path, "$Header`r`n")
    }

    $entry = New-Object System.Collections.Generic.List[string]
    $entry.Add("")
    foreach ($line in @($Lines)) {
        $entry.Add($line)
    }
    Add-Content -LiteralPath $Path -Value ($entry -join [Environment]::NewLine)
}

function Get-V2MemoryMode {
    param(
        [bool]$IncludeNeo4j,
        [bool]$IncludeQdrant
    )

    if ($IncludeNeo4j -and $IncludeQdrant) {
        return "hybrid"
    }
    if ($IncludeNeo4j) {
        return "markdown+neo4j"
    }
    if ($IncludeQdrant) {
        return "markdown+qdrant"
    }

    return "markdown-only"
}

function Get-V2Timestamp {
    return (Get-Date).ToString("s")
}

function Get-V2Sha1Hex {
    param([string]$Text)

    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Text)
        $hash = $sha1.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash).Replace("-", "").ToLowerInvariant())
    }
    finally {
        $sha1.Dispose()
    }
}

function Get-V2ObserverIncidentDedupState {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [PSCustomObject]@{ entries = @() }
    }
    $doc = Get-V2JsonContent -Path $Path
    if (-not $doc) {
        return [PSCustomObject]@{ entries = @() }
    }
    $entries = @(Get-V2OptionalProperty -InputObject $doc -Name "entries" -DefaultValue @())
    return [PSCustomObject]@{ entries = @($entries) }
}

function Save-V2ObserverIncidentDedupState {
    param(
        [string]$Path,
        [object[]]$Entries
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    Save-V2JsonContent -Path $Path -Value ([PSCustomObject]@{
            updated_at = Get-V2Timestamp
            entries    = @($Entries)
        })
}

function New-IncidentReport {
    param(
        [string]$ReportDirectory,
        [string]$Category,
        [string]$Title,
        [string]$Details,
        [string]$CommandText = "",
        [string]$OutputText = "",
        [string]$DedupPath = "",
        [int]$DedupCooldownSeconds = 0,
        [string[]]$DedupCategories = @()
    )

    Initialize-V2Directory -Path $ReportDirectory
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $safeCategory = ($Category.ToLowerInvariant() -replace "[^a-z0-9]+", "_").Trim("_")

    $dedupEnabled = (
        -not [string]::IsNullOrWhiteSpace($DedupPath) -and
        $DedupCooldownSeconds -gt 0 -and
        $DedupCategories.Count -gt 0 -and
        ($DedupCategories -contains $safeCategory)
    )

    if ($dedupEnabled) {
        $normalizedDetails = ([string]$Details).ToLowerInvariant().Trim()
        $normalizedDetails = ($normalizedDetails -replace "\s+", " ")
        $fingerprint = Get-V2Sha1Hex -Text ("{0}|{1}|{2}" -f $safeCategory, ([string]$Title).Trim().ToLowerInvariant(), $normalizedDetails)
        $registry = Get-V2ObserverIncidentDedupState -Path $DedupPath
        $entries = @($registry.entries)
        $existing = @($entries | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "fingerprint" -DefaultValue "") -eq $fingerprint
            } | Select-Object -First 1)
        if ($existing.Count -eq 1 -and $existing[0]) {
            $existingAtRaw = [string](Get-V2OptionalProperty -InputObject $existing[0] -Name "last_created_at" -DefaultValue "")
            $existingPath = [string](Get-V2OptionalProperty -InputObject $existing[0] -Name "incident_path" -DefaultValue "")
            if (-not [string]::IsNullOrWhiteSpace($existingAtRaw)) {
                try {
                    $ageSeconds = ([DateTime]::UtcNow - [DateTimeOffset]::Parse($existingAtRaw).UtcDateTime).TotalSeconds
                    if ($ageSeconds -lt [double]$DedupCooldownSeconds -and -not [string]::IsNullOrWhiteSpace($existingPath)) {
                        return $existingPath
                    }
                }
                catch { }
            }
        }
    }

    $path = Join-Path $ReportDirectory "INCIDENT_${timestamp}_${safeCategory}.md"

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# Incident")
    $lines.Add("")
    $lines.Add("- Category: $Category")
    $lines.Add("- Time: $(Get-V2Timestamp)")
    $lines.Add("")
    $lines.Add("## Title")
    $lines.Add($Title)
    $lines.Add("")
    $lines.Add("## Details")
    $lines.Add($Details)
    if (-not [string]::IsNullOrWhiteSpace($CommandText)) {
        $lines.Add("")
        $lines.Add("## Command")
        $lines.Add('```text')
        $lines.Add($CommandText)
        $lines.Add('```')
    }
    if (-not [string]::IsNullOrWhiteSpace($OutputText)) {
        $lines.Add("")
        $lines.Add("## Output Tail")
        $lines.Add('```text')
        $lines.Add(($OutputText -split "(`r`n|`n|`r)" | Select-Object -Last 80) -join [Environment]::NewLine)
        $lines.Add('```')
    }

    [System.IO.File]::WriteAllText($path, ($lines -join [Environment]::NewLine))

    # Record to DB (Postgres/SQLite)
    try {
        $incidentPayload = [PSCustomObject]@{
            category      = $Category
            title         = $Title
            details       = $Details
            command_text  = $CommandText
            output_text   = $OutputText
            incident_path = $path
        }
        $tempIncidentJson = [System.IO.Path]::GetTempFileName()
        try {
            $incidentPayload | ConvertTo-Json -Depth 16 | Out-File -FilePath $tempIncidentJson -Encoding utf8
            Invoke-V2TaskDb -Mode "record-incident" -TasksJsonPath $tempIncidentJson | Out-Null
        }
        finally {
            if (Test-Path -LiteralPath $tempIncidentJson) { Remove-Item -LiteralPath $tempIncidentJson -Force }
        }
    }
    catch {
        Write-Warning "Failed to record incident to database: $($_.Exception.Message)"
    }

    if ($dedupEnabled) {
        $normalizedDetails = ([string]$Details).ToLowerInvariant().Trim()
        $normalizedDetails = ($normalizedDetails -replace "\s+", " ")
        $fingerprint = Get-V2Sha1Hex -Text ("{0}|{1}|{2}" -f $safeCategory, ([string]$Title).Trim().ToLowerInvariant(), $normalizedDetails)
        $registry = Get-V2ObserverIncidentDedupState -Path $DedupPath
        $entries = @($registry.entries | Where-Object {
                [string](Get-V2OptionalProperty -InputObject $_ -Name "fingerprint" -DefaultValue "") -ne $fingerprint
            })
        $entries += [PSCustomObject]@{
            fingerprint   = $fingerprint
            category      = $safeCategory
            title         = $Title
            last_created_at = (Get-V2Timestamp)
            incident_path = $path
        }
        if ($entries.Count -gt 300) {
            $entries = @($entries | Select-Object -Last 300)
        }
        Save-V2ObserverIncidentDedupState -Path $DedupPath -Entries $entries
    }

    return $path
}

function Add-RepairTask {
    param(
        [string]$SelfHealingPath,
        [string]$BacklogPath,
        [string]$IncidentPath,
        [string]$Reason,
        [string[]]$LessonHints = @(),
        [string]$TaskDagJsonPath = "",
        [string]$ExecutionMode = "artifact-validation",
        [int]$CooldownSeconds = 1800
    )

    $reasonText = [string]$Reason
    if ([string]::IsNullOrWhiteSpace($reasonText)) {
        $reasonText = "generated-repair"
    }
    $reasonText = $reasonText.Trim()
    if ($reasonText.Length -gt 500) {
        $reasonText = $reasonText.Substring(0, 500) + "…"
    }
    $reasonFingerprint = (($reasonText.ToLowerInvariant() -replace "\s+", " ").Trim())
    if ($reasonFingerprint.Length -gt 240) {
        $reasonFingerprint = $reasonFingerprint.Substring(0, 240)
    }

    if (-not [string]::IsNullOrWhiteSpace($TaskDagJsonPath) -and (Test-Path -LiteralPath $TaskDagJsonPath)) {
        $existingTaskId = Invoke-V2WithDagMutex -DagPath $TaskDagJsonPath -ScriptBlock {
            $existingDoc = Get-V2JsonContent -Path $TaskDagJsonPath
            if (-not $existingDoc -or -not ($existingDoc.PSObject.Properties.Name -contains "tasks")) {
                return ""
            }

            $openStatuses = Get-V2OpenTaskStatuses
            $existingOpen = @($existingDoc.tasks | Where-Object {
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    $taskReasonFingerprint = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason_fingerprint" -DefaultValue "")
                    if ([string]::IsNullOrWhiteSpace($taskReasonFingerprint)) {
                        $taskReasonRaw = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                        $taskReasonFingerprint = (($taskReasonRaw.ToLowerInvariant() -replace "\s+", " ").Trim())
                    }
                    ($taskStatus -in $openStatuses) -and ($taskReasonFingerprint -eq $reasonFingerprint)
                } | Select-Object -First 1)
            if ($existingOpen) {
                return [string](Get-V2OptionalProperty -InputObject $existingOpen -Name "id" -DefaultValue "")
            }

            if ($CooldownSeconds -lt 60) { $CooldownSeconds = 60 }
            $existingRecent = @($existingDoc.tasks | Where-Object {
                    $taskReasonFingerprint = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason_fingerprint" -DefaultValue "")
                    if ([string]::IsNullOrWhiteSpace($taskReasonFingerprint)) {
                        $taskReasonRaw = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                        $taskReasonFingerprint = (($taskReasonRaw.ToLowerInvariant() -replace "\s+", " ").Trim())
                    }
                    if ($taskReasonFingerprint -ne $reasonFingerprint) { return $false }
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    if ($taskStatus -in @("done", "completed", "skipped")) { return $false }
                    $updatedAtRaw = [string](Get-V2OptionalProperty -InputObject $_ -Name "updated_at" -DefaultValue "")
                    if ([string]::IsNullOrWhiteSpace($updatedAtRaw)) {
                        $updatedAtRaw = [string](Get-V2OptionalProperty -InputObject $_ -Name "created_at" -DefaultValue "")
                    }
                    if ([string]::IsNullOrWhiteSpace($updatedAtRaw)) { return $false }
                    try {
                        $updatedAt = [DateTimeOffset]::Parse($updatedAtRaw).UtcDateTime
                    }
                    catch {
                        return $false
                    }
                    $ageSeconds = ((Get-Date).ToUniversalTime() - $updatedAt).TotalSeconds
                    return ($ageSeconds -ge 0 -and $ageSeconds -lt $CooldownSeconds)
                } | Select-Object -First 1)
            if ($existingRecent) {
                return [string](Get-V2OptionalProperty -InputObject $existingRecent -Name "id" -DefaultValue "")
            }
            return ""
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$existingTaskId)) {
            return [string]$existingTaskId
        }
    }

    $taskId = "REPAIR-{0}-{1}" -f (Get-Date -Format "yyyyMMddHHmmss"), ([System.Guid]::NewGuid().ToString("N").Substring(0, 6))
    if (Test-Path -LiteralPath $SelfHealingPath) {
        $selfHealingContent = Get-Content -LiteralPath $SelfHealingPath -Raw
        $normalizedContent = $selfHealingContent -replace "(?m)^- none\s*$", ""
        if ($normalizedContent -ne $selfHealingContent) {
            [System.IO.File]::WriteAllText($SelfHealingPath, $normalizedContent)
        }
    }
    $entry = @"

## $taskId
- Time: $(Get-V2Timestamp)
- Reason: $reasonText
- Incident: $IncidentPath
- Status: open
"@
    if (@($LessonHints).Count -gt 0) {
        $entry += "`r`n- Similar Lessons:"
        foreach ($hint in @($LessonHints | Select-Object -First 5)) {
            $entry += "`r`n  - $hint"
        }
        $entry += "`r`n"
    }
    Add-Content -LiteralPath $SelfHealingPath -Value $entry

$backlogEntry = @"

- id: $taskId
  description: Repair task generated by orchestrator
  reason: $reasonText
  priority: P0
  dependencies: []
  assigned_agent: AI Engineer
  execution_mode: $ExecutionMode
  status: pending
  source_incident: $IncidentPath
"@
    if (@($LessonHints).Count -gt 0) {
        $backlogEntry += "`r`n  similar_lessons: [`"$(@($LessonHints | Select-Object -First 5) -join '", "')`"]`r`n"
    }
    if (Test-Path -LiteralPath $BacklogPath) {
        $backlogContent = Get-Content -LiteralPath $BacklogPath -Raw
        $normalizedBacklog = $backlogContent -replace "(?m)^- none\s*$", ""
        if ($normalizedBacklog -ne $backlogContent) {
            [System.IO.File]::WriteAllText($BacklogPath, $normalizedBacklog)
        }
    }
    Add-Content -LiteralPath $BacklogPath -Value $backlogEntry

    if (-not [string]::IsNullOrWhiteSpace($TaskDagJsonPath) -and (Test-Path -LiteralPath $TaskDagJsonPath)) {
        $createdTask = [bool](Invoke-V2WithDagMutex -DagPath $TaskDagJsonPath -ScriptBlock {
            $taskDocument = Get-V2JsonContent -Path $TaskDagJsonPath
            if (-not $taskDocument -or -not ($taskDocument.PSObject.Properties.Name -contains "tasks")) {
                return $false
            }

            $alreadyOpenByReason = @($taskDocument.tasks | Where-Object {
                    $taskStatus = [string](Get-V2OptionalProperty -InputObject $_ -Name "status" -DefaultValue "")
                    if (-not (Test-V2TaskStatusOpen -Status $taskStatus)) { return $false }
                    $taskReasonFingerprint = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason_fingerprint" -DefaultValue "")
                    if ([string]::IsNullOrWhiteSpace($taskReasonFingerprint)) {
                        $taskReasonRaw = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                        $taskReasonFingerprint = (($taskReasonRaw.ToLowerInvariant() -replace "\s+", " ").Trim())
                    }
                    return ($taskReasonFingerprint -eq $reasonFingerprint)
                }).Count -gt 0
            if ($alreadyOpenByReason) {
                return $false
            }

            $exists = @($taskDocument.tasks | Where-Object { [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "") -eq $taskId }).Count -gt 0
            if ($exists) {
                return $false
            }

            $taskDocument.tasks += [PSCustomObject]@{
                id              = $taskId
                description     = "Repair task generated by orchestrator"
                reason          = $reasonText
                reason_fingerprint = $reasonFingerprint
                priority        = "P0"
                dependencies    = @()
                preferred_agent = "AI Engineer"
                assigned_agent  = "AI Engineer"
                execution_mode  = $ExecutionMode
                status          = "pending"
                files_affected  = @("ai-orchestrator/analysis/self-healing.md")
                source_incident = $IncidentPath
                similar_lessons = @($LessonHints | Select-Object -First 5)
                created_at      = Get-V2Timestamp
                updated_at      = Get-V2Timestamp
            }
            if ($taskDocument.PSObject.Properties.Name -contains "updated_at") {
                $taskDocument.updated_at = Get-V2Timestamp
            }
            else {
                Add-Member -InputObject $taskDocument -MemberType NoteProperty -Name "updated_at" -Value (Get-V2Timestamp) -Force
            }
            Save-V2JsonContent -Path $TaskDagJsonPath -Value $taskDocument
            return $true
        })

        if ($createdTask) {
            $schedulerSyncScript = Join-Path $PSScriptRoot "Invoke-SchedulerV2.ps1"
            if (Test-Path -LiteralPath $schedulerSyncScript -PathType Leaf) {
                $projectRootForSync = Split-Path -Parent (Split-Path -Parent $TaskDagJsonPath)
                try {
                    & $schedulerSyncScript -ProjectPath $projectRootForSync -MaxAssignmentsPerRun 0 -EmitJson | Out-Null
                }
                catch { }
            }
        }
    }
    return $taskId
}

function Get-V2Timestamp {
    return (Get-Date).ToString("s")
}


function Get-V2GpuVramGuardProfile {
    param(
        [int]$DefaultReserveMb = 3072,
        [int]$MinimumReserveMb = 512
    )

    $reserveMb = $DefaultReserveMb
    try {
        if (-not [string]::IsNullOrWhiteSpace([string]$env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB)) {
            $reserveMb = [int]$env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB
        }
    }
    catch {
        $reserveMb = $DefaultReserveMb
    }
    if ($reserveMb -lt $MinimumReserveMb) {
        $reserveMb = $MinimumReserveMb
    }

    $numParallel = 2
    try {
        if (-not [string]::IsNullOrWhiteSpace([string]$env:OLLAMA_NUM_PARALLEL)) {
            $numParallel = [int]$env:OLLAMA_NUM_PARALLEL
        }
    }
    catch {
        $numParallel = 2
    }
    if ($numParallel -lt 1) {
        $numParallel = 1
    }

    $maxLoadedModels = 2
    try {
        if (-not [string]::IsNullOrWhiteSpace([string]$env:OLLAMA_MAX_LOADED_MODELS)) {
            $maxLoadedModels = [int]$env:OLLAMA_MAX_LOADED_MODELS
        }
    }
    catch {
        $maxLoadedModels = 2
    }
    if ($maxLoadedModels -lt 1) {
        $maxLoadedModels = 1
    }

    $gpuOverheadBytes = [int64]$reserveMb * 1MB
    return [PSCustomObject]@{
        reserve_mb         = [int]$reserveMb
        gpu_overhead_bytes = [int64]$gpuOverheadBytes
        num_parallel       = [int]$numParallel
        max_loaded_models  = [int]$maxLoadedModels
    }
}

function Set-V2OllamaGpuGuardEnv {
    param(
        [object]$V2Profile = $null
    )

    $effective = if ($V2Profile) { $V2Profile } else { Get-V2GpuVramGuardProfile }
    $reserveMb = [int](Get-V2OptionalProperty -InputObject $effective -Name "reserve_mb" -DefaultValue 3072)
    if ($reserveMb -lt 512) {
        $reserveMb = 512
    }
    $gpuOverheadBytes = [int64](Get-V2OptionalProperty -InputObject $effective -Name "gpu_overhead_bytes" -DefaultValue ([int64]$reserveMb * 1MB))
    $numParallel = [int](Get-V2OptionalProperty -InputObject $effective -Name "num_parallel" -DefaultValue 2)
    if ($numParallel -lt 1) {
        $numParallel = 1
    }
    $maxLoadedModels = [int](Get-V2OptionalProperty -InputObject $effective -Name "max_loaded_models" -DefaultValue 2)
    if ($maxLoadedModels -lt 1) {
        $maxLoadedModels = 1
    }

    $env:ORCHESTRATOR_GPU_VRAM_RESERVE_MB = [string]$reserveMb
    $env:OLLAMA_GPU_OVERHEAD = [string]$gpuOverheadBytes
    $env:OLLAMA_NUM_PARALLEL = [string]$numParallel
    $env:OLLAMA_MAX_LOADED_MODELS = [string]$maxLoadedModels

    return [PSCustomObject]@{
        reserve_mb         = $reserveMb
        gpu_overhead_bytes = $gpuOverheadBytes
        num_parallel       = $numParallel
        max_loaded_models  = $maxLoadedModels
    }
}

function Get-V2VerifiedCommand {
    <#
    .SYNOPSIS
        Safely extracts a command string from a verified_commands entry.
        Handles both plain strings and {value, confidence} objects produced by V2 intake.
    #>
    param(
        [object]$VerifiedCommands,
        [string]$CommandName,
        [string]$DefaultValue = ""
    )
    $raw = Get-V2OptionalProperty -InputObject $VerifiedCommands -Name $CommandName -DefaultValue $DefaultValue
    if ($raw -is [string]) { return $raw.Trim() }
    if ($raw -and $raw.PSObject.Properties.Name -contains "value") { return ([string]$raw.value).Trim() }
    return ([string]$raw).Trim()
}

function Get-V2FileFingerprint {
    param(
        [string]$RootPath,
        [string[]]$ExcludeRegexes = @()
    )

    $items = New-Object System.Collections.Generic.List[string]
    $files = @(Get-ChildItem -LiteralPath $RootPath -Recurse -File -Force -ErrorAction SilentlyContinue)
    foreach ($file in $files) {
        try {
            $relativePath = Get-V2RelativeUnixPath -BasePath $RootPath -TargetPath $file.FullName
            $fileLength = $file.Length
            $fileTicks = $file.LastWriteTimeUtc.Ticks
        }
        catch {
            continue
        }
        $skip = $false
        foreach ($pattern in $ExcludeRegexes) {
            if ($relativePath -match $pattern) {
                $skip = $true
                break
            }
        }

        if ($skip) {
            continue
        }

        $items.Add("$relativePath|$fileLength|$fileTicks")
    }

    $source = ($items | Sort-Object) -join "`n"
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($source)
        $hash = $sha256.ComputeHash($bytes)
    }
    finally {
        $sha256.Dispose()
    }

    return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

function Get-V2NormalizedPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }

    $normalized = $Path.Replace("\", "/").Trim()
    if ($normalized.StartsWith("./")) {
        $normalized = $normalized.Substring(2)
    }
    if ($normalized.StartsWith("/")) {
        $normalized = $normalized.Substring(1)
    }

    return $normalized.ToLowerInvariant()
}

function Get-V2ActiveLocks {
    param([string]$LocksPath)

    $content = Get-V2JsonContent -Path $LocksPath
    if (-not $content -or -not ($content.PSObject.Properties.Name -contains "locks")) {
        return @()
    }

    $nowUtc = (Get-Date).ToUniversalTime()
    $activeLocks = New-Object System.Collections.Generic.List[object]
    foreach ($lock in @($content.locks)) {
        if (-not $lock) { continue }

        $status = if ($lock.PSObject.Properties.Name -contains "status") { [string]$lock.status } else { "active" }
        if ([string]::IsNullOrWhiteSpace($status)) { $status = "active" }
        if ($status.ToLowerInvariant() -notin @("active", "locked", "in-progress")) {
            continue
        }

        $isExpired = $false
        $hasTtl = $lock.PSObject.Properties.Name -contains "ttl_seconds"
        $hasLockedAt = $lock.PSObject.Properties.Name -contains "locked_at"
        if ($hasTtl -and $hasLockedAt) {
            $ttl = 0
            try { $ttl = [int]$lock.ttl_seconds } catch { $ttl = 0 }
            $lockedAtRaw = [string]$lock.locked_at
            if ($ttl -gt 0 -and -not [string]::IsNullOrWhiteSpace($lockedAtRaw)) {
                try {
                    $lockedAtUtc = ([DateTime]::Parse($lockedAtRaw)).ToUniversalTime()
                    if ($lockedAtUtc.AddSeconds($ttl) -lt $nowUtc) {
                        $isExpired = $true
                    }
                }
                catch {
                    $isExpired = $false
                }
            }
        }

        if (-not $isExpired) {
            $activeLocks.Add($lock)
        }
    }

    return @($activeLocks.ToArray())
}

function Test-V2LockConflict {
    param(
        [string[]]$FilesAffected,
        [string]$LocksPath
    )

    $result = [PSCustomObject]@{
        has_conflict      = $false
        conflicting_locks = @()
    }

    if (@($FilesAffected).Count -eq 0) {
        return $result
    }

    $activeLocks = @(Get-V2ActiveLocks -LocksPath $LocksPath)
    if ($activeLocks.Count -eq 0) {
        return $result
    }

    $conflicts = New-Object System.Collections.Generic.List[object]
    foreach ($taskFile in @($FilesAffected)) {
        $taskPath = Get-V2NormalizedPath -Path $taskFile
        if ([string]::IsNullOrWhiteSpace($taskPath)) { continue }

        foreach ($lock in $activeLocks) {
            $lockPathRaw = if ($lock.PSObject.Properties.Name -contains "file_path") { [string]$lock.file_path } else { "" }
            $lockPath = Get-V2NormalizedPath -Path $lockPathRaw
            if ([string]::IsNullOrWhiteSpace($lockPath)) { continue }

            $isOverlap = $false
            if ($taskPath -eq $lockPath) {
                $isOverlap = $true
            }
            elseif ($taskPath.StartsWith("$lockPath/")) {
                $isOverlap = $true
            }
            elseif ($lockPath.StartsWith("$taskPath/")) {
                $isOverlap = $true
            }

            if ($isOverlap) {
                $conflicts.Add($lock)
            }
        }
    }

    if ($conflicts.Count -gt 0) {
        $result = [PSCustomObject]@{
            has_conflict      = $true
            conflicting_locks = @($conflicts.ToArray())
        }
    }

    return $result
}

function Get-V2LockHistoryPath {
    param([string]$LocksPath)

    $lockDirectory = Split-Path -Parent $LocksPath
    if ([string]::IsNullOrWhiteSpace($lockDirectory)) {
        return "lock-history.md"
    }

    return (Join-Path $lockDirectory "lock-history.md")
}

function Write-V2LockHistoryEvent {
    param(
        [string]$LocksPath,
        [string]$Action,
        [string]$TaskId,
        [string]$Agent,
        [string]$FilePath,
        [string]$Status = "ok",
        [string]$Notes = ""
    )

    $historyPath = Get-V2LockHistoryPath -LocksPath $LocksPath
    $lines = @(
        "## $(Get-V2Timestamp)",
        "- action: $Action",
        "- task_id: $TaskId",
        "- agent: $Agent",
        "- file_path: $FilePath",
        "- status: $Status"
    )
    if (-not [string]::IsNullOrWhiteSpace($Notes)) {
        $lines += "- notes: $Notes"
    }

    Add-V2MarkdownLog -Path $historyPath -Header "# Lock History" -Lines $lines
}

function Get-V2LockMutexName {
    param([string]$LocksPath)

    return (Get-V2FileMutexName -Path $LocksPath -Prefix "V2Locks")
}

function Initialize-V2LocksDocument {
    param([string]$LocksPath)

    $content = Get-V2JsonContent -Path $LocksPath
    if (-not $content -or -not ($content.PSObject.Properties.Name -contains "locks")) {
        $content = [PSCustomObject]@{
            locks = @()
        }
        Save-V2JsonContent -Path $LocksPath -Value $content
    }

    return $content
}

function Get-V2ActiveStatusSet {
    return @("active", "locked", "in-progress")
}

function Get-V2OpenTaskStatuses {
    $default = @(
        "pending",
        "in-progress",
        "blocked",
        "blocked-runtime",
        "blocked-lock-conflict",
        "blocked-phase-approval",
        "blocked-no-agent",
        "blocked-waiting-answers",
        "blocked-startup",
        "needs-revision"
    )

    $raw = [string][System.Environment]::GetEnvironmentVariable("ORCHESTRATOR_OPEN_TASK_STATUSES")
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $default
    }

    $custom = @(
        @($raw -split "," | ForEach-Object { [string]$_ } | ForEach-Object { $_.Trim().ToLowerInvariant() }) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
    )
    if ($custom.Count -eq 0) {
        return $default
    }
    return $custom
}

function Test-V2TaskStatusOpen {
    param([string]$Status)

    $normalized = [string]$Status
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        $normalized = "pending"
    }
    $normalized = $normalized.Trim().ToLowerInvariant()
    if ($normalized -eq "open") {
        $normalized = "pending"
    }
    return ($normalized -in (Get-V2OpenTaskStatuses))
}

function Get-V2ExecutionTaskPrefixes {
    $default = @("FEAT-", "DEV-", "TASK-", "REPAIR-", "COBERTURA-", "RECHECK-", "REFACTOR-")
    $raw = [string][System.Environment]::GetEnvironmentVariable("ORCHESTRATOR_EXECUTION_TASK_PREFIXES")
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $default
    }

    $custom = @(
        @($raw -split "," | ForEach-Object { [string]$_ } | ForEach-Object { $_.Trim().ToUpperInvariant() }) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
    )
    if ($custom.Count -eq 0) {
        return $default
    }
    return $custom
}

function Test-V2ExecutionTaskId {
    param([string]$TaskId)

    $id = [string]$TaskId
    if ([string]::IsNullOrWhiteSpace($id)) {
        return $false
    }
    $normalized = $id.Trim().ToUpperInvariant()
    foreach ($prefix in @(Get-V2ExecutionTaskPrefixes)) {
        if ($normalized.StartsWith([string]$prefix)) {
            return $true
        }
    }
    return $false
}

function Test-V2LockExpired {
    param([object]$Lock)

    if (-not $Lock) { return $false }
    if (-not ($Lock.PSObject.Properties.Name -contains "ttl_seconds")) { return $false }
    if (-not ($Lock.PSObject.Properties.Name -contains "locked_at")) { return $false }

    $ttlSeconds = 0
    try { $ttlSeconds = [int]$Lock.ttl_seconds } catch { $ttlSeconds = 0 }
    if ($ttlSeconds -le 0) { return $false }

    $lockedAtRaw = [string]$Lock.locked_at
    if ([string]::IsNullOrWhiteSpace($lockedAtRaw)) { return $false }

    try {
        $lockedAtUtc = ([DateTime]::Parse($lockedAtRaw)).ToUniversalTime()
        return ($lockedAtUtc.AddSeconds($ttlSeconds) -lt (Get-Date).ToUniversalTime())
    }
    catch {
        return $false
    }
}

function Remove-V2ExpiredLocks {
    param([string]$LocksPath)

    $content = Initialize-V2LocksDocument -LocksPath $LocksPath
    $changed = $false
    $expiredCount = 0
    $activeStatuses = Get-V2ActiveStatusSet
    foreach ($lock in @($content.locks)) {
        if (-not $lock) { continue }
        $status = if ($lock.PSObject.Properties.Name -contains "status") { [string]$lock.status } else { "active" }
        if ($status.ToLowerInvariant() -notin $activeStatuses) { continue }

        if (Test-V2LockExpired -Lock $lock) {
            Set-V2DynamicProperty -InputObject $lock -Name "status" -Value "expired"
            Set-V2DynamicProperty -InputObject $lock -Name "expired_at" -Value (Get-V2Timestamp)
            $changed = $true
            $expiredCount += 1
            Write-V2LockHistoryEvent `
                -LocksPath $LocksPath `
                -Action "expire" `
                -TaskId ([string]$(if ($lock.PSObject.Properties.Name -contains "task_id") { $lock.task_id } else { "unknown" })) `
                -Agent ([string]$(if ($lock.PSObject.Properties.Name -contains "agent") { $lock.agent } else { "unknown" })) `
                -FilePath ([string]$(if ($lock.PSObject.Properties.Name -contains "file_path") { $lock.file_path } else { "unknown" })) `
                -Status "expired"
        }
    }

    if ($changed) {
        Save-V2JsonContent -Path $LocksPath -Value $content
    }

    return $expiredCount
}

function New-V2TaskLocks {
    param(
        [string]$LocksPath,
        [string]$TaskId,
        [string]$Agent,
        [string[]]$FilesAffected,
        [int]$TtlSeconds = 7200
    )

    $result = [PSCustomObject]@{
        success   = $false
        acquired  = @()
        renewed   = @()
        conflicts = @()
        reason    = ""
    }

    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        $result.reason = "task-id-required"
        return $result
    }
    if ([string]::IsNullOrWhiteSpace($Agent)) {
        $result.reason = "agent-required"
        return $result
    }

    $normalizedFiles = @(
        @($FilesAffected | ForEach-Object { Get-V2NormalizedPath -Path ([string]$_) }) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        Select-Object -Unique
    )

    if ($normalizedFiles.Count -eq 0) {
        $result.success = $true
        return $result
    }

    $mutexName = Get-V2LockMutexName -LocksPath $LocksPath
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    $hasMutex = $false
    try {
        $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(20))
        if (-not $hasMutex) {
            $result.reason = "lock-mutex-timeout"
            return $result
        }

        [void](Remove-V2ExpiredLocks -LocksPath $LocksPath)
        $content = Initialize-V2LocksDocument -LocksPath $LocksPath
        $activeStatuses = Get-V2ActiveStatusSet

        $conflicts = New-Object System.Collections.Generic.List[object]
        foreach ($targetPath in $normalizedFiles) {
            foreach ($lock in @($content.locks)) {
                if (-not $lock) { continue }

                $status = if ($lock.PSObject.Properties.Name -contains "status") { [string]$lock.status } else { "active" }
                if ($status.ToLowerInvariant() -notin $activeStatuses) { continue }

                $lockPath = Get-V2NormalizedPath -Path ([string]$(if ($lock.PSObject.Properties.Name -contains "file_path") { $lock.file_path } else { "" }))
                if ([string]::IsNullOrWhiteSpace($lockPath)) { continue }

                $lockTaskId = [string]$(if ($lock.PSObject.Properties.Name -contains "task_id") { $lock.task_id } else { "" })
                $lockAgent = [string]$(if ($lock.PSObject.Properties.Name -contains "agent") { $lock.agent } else { "" })
                if ($lockTaskId -eq $TaskId -and $lockAgent -eq $Agent) {
                    continue
                }

                $isOverlap = $false
                if ($targetPath -eq $lockPath) { $isOverlap = $true }
                elseif ($targetPath.StartsWith("$lockPath/")) { $isOverlap = $true }
                elseif ($lockPath.StartsWith("$targetPath/")) { $isOverlap = $true }

                if ($isOverlap) {
                    $conflicts.Add($lock)
                }
            }
        }

        if ($conflicts.Count -gt 0) {
            $result.conflicts = @($conflicts.ToArray())
            $result.reason = "lock-conflict"
            return $result
        }

        $acquired = New-Object System.Collections.Generic.List[string]
        $renewed = New-Object System.Collections.Generic.List[string]
        $now = Get-V2Timestamp
        foreach ($targetPath in $normalizedFiles) {
            $existing = $null
            foreach ($lock in @($content.locks)) {
                if (-not $lock) { continue }
                $status = if ($lock.PSObject.Properties.Name -contains "status") { [string]$lock.status } else { "active" }
                if ($status.ToLowerInvariant() -notin $activeStatuses) { continue }

                $lockPath = Get-V2NormalizedPath -Path ([string]$(if ($lock.PSObject.Properties.Name -contains "file_path") { $lock.file_path } else { "" }))
                $lockTaskId = [string]$(if ($lock.PSObject.Properties.Name -contains "task_id") { $lock.task_id } else { "" })
                $lockAgent = [string]$(if ($lock.PSObject.Properties.Name -contains "agent") { $lock.agent } else { "" })
                if ($lockPath -eq $targetPath -and $lockTaskId -eq $TaskId -and $lockAgent -eq $Agent) {
                    $existing = $lock
                    break
                }
            }

            if ($existing) {
                Set-V2DynamicProperty -InputObject $existing -Name "locked_at" -Value $now
                Set-V2DynamicProperty -InputObject $existing -Name "ttl_seconds" -Value $TtlSeconds
                Set-V2DynamicProperty -InputObject $existing -Name "status" -Value "active"
                Set-V2DynamicProperty -InputObject $existing -Name "renewed_at" -Value $now
                $renewed.Add($targetPath)
                Write-V2LockHistoryEvent `
                    -LocksPath $LocksPath `
                    -Action "renew" `
                    -TaskId $TaskId `
                    -Agent $Agent `
                    -FilePath $targetPath
            }
            else {
                $content.locks += [PSCustomObject]@{
                    file_path   = $targetPath
                    task_id     = $TaskId
                    agent       = $Agent
                    locked_at   = $now
                    ttl_seconds = $TtlSeconds
                    status      = "active"
                }
                $acquired.Add($targetPath)
                Write-V2LockHistoryEvent `
                    -LocksPath $LocksPath `
                    -Action "acquire" `
                    -TaskId $TaskId `
                    -Agent $Agent `
                    -FilePath $targetPath
            }
        }

        Save-V2JsonContent -Path $LocksPath -Value $content

        $result.success = $true
        $result.acquired = @($acquired.ToArray())
        $result.renewed = @($renewed.ToArray())
        return $result
    }
    finally {
        if ($hasMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

function Remove-V2TaskLocks {
    param(
        [string]$LocksPath,
        [string]$TaskId,
        [string]$Agent = "",
        [string[]]$FilesAffected = @(),
        [string]$Reason = "completed"
    )

    $result = [PSCustomObject]@{
        released_count = 0
        released_files = @()
    }

    if ([string]::IsNullOrWhiteSpace($TaskId)) {
        return $result
    }

    $mutexName = Get-V2LockMutexName -LocksPath $LocksPath
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    $hasMutex = $false
    try {
        $hasMutex = $mutex.WaitOne([TimeSpan]::FromSeconds(20))
        if (-not $hasMutex) {
            return $result
        }

        [void](Remove-V2ExpiredLocks -LocksPath $LocksPath)
        $content = Initialize-V2LocksDocument -LocksPath $LocksPath
        $activeStatuses = Get-V2ActiveStatusSet
        $fileFilters = @(
            @($FilesAffected | ForEach-Object { Get-V2NormalizedPath -Path ([string]$_) }) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            Select-Object -Unique
        )

        $released = New-Object System.Collections.Generic.List[string]
        foreach ($lock in @($content.locks)) {
            if (-not $lock) { continue }
            $status = if ($lock.PSObject.Properties.Name -contains "status") { [string]$lock.status } else { "active" }
            if ($status.ToLowerInvariant() -notin $activeStatuses) { continue }

            $lockTaskId = [string]$(if ($lock.PSObject.Properties.Name -contains "task_id") { $lock.task_id } else { "" })
            if ($lockTaskId -ne $TaskId) { continue }

            $lockAgent = [string]$(if ($lock.PSObject.Properties.Name -contains "agent") { $lock.agent } else { "" })
            if (-not [string]::IsNullOrWhiteSpace($Agent) -and $lockAgent -ne $Agent) { continue }

            $lockPath = Get-V2NormalizedPath -Path ([string]$(if ($lock.PSObject.Properties.Name -contains "file_path") { $lock.file_path } else { "" }))
            if ($fileFilters.Count -gt 0 -and ($fileFilters -notcontains $lockPath)) { continue }

            Set-V2DynamicProperty -InputObject $lock -Name "status" -Value "released"
            Set-V2DynamicProperty -InputObject $lock -Name "released_at" -Value (Get-V2Timestamp)
            Set-V2DynamicProperty -InputObject $lock -Name "release_reason" -Value $Reason
            $released.Add($lockPath)
            Write-V2LockHistoryEvent `
                -LocksPath $LocksPath `
                -Action "release" `
                -TaskId $TaskId `
                -Agent $lockAgent `
                -FilePath $lockPath `
                -Status $Reason
        }

        if ($released.Count -gt 0) {
            Save-V2JsonContent -Path $LocksPath -Value $content
        }

        $result.released_count = $released.Count
        $result.released_files = @($released.ToArray())
        return $result
    }
    finally {
        if ($hasMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}
