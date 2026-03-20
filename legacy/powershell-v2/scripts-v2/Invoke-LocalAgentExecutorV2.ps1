<#
.SYNOPSIS
    Hybrid local agent executor — runs tasks via local CLI agents (codex exec, Python worker).
.DESCRIPTION
    Integrates into the autonomous loop's AgentRuntime step.
    For each in-progress task whose agent maps to a local backend, attempts to
    execute the task directly without waiting for external dispatch consumption.

    Backend priority:
      1. codex     — codex exec --full-auto (requires codex CLI installed)
      2. python    — services/agent_worker.py --run-once (requires ANTHROPIC_API_KEY or Ollama)
      3. skip      — falls back to existing dispatch file bridge

    Agent → backend mapping:
      Codex, Antigravity  → codex
      Claude, AI *        → python (anthropic/ollama)
      (unrecognized)      → skip (use existing bridge)
.PARAMETER ProjectPath
    SINC project root containing ai-orchestrator/.
.PARAMETER MaxTasks
    Maximum tasks to execute per call. Default: 3.
.PARAMETER TimeoutSeconds
    Per-task execution timeout. Default: 300.
.PARAMETER DryRun
    Log what would execute but don't run anything.
#>
param(
    [string]$ProjectPath    = ".",
    [int]$MaxTasks          = 3,
    [int]$TimeoutSeconds    = 300,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedPath -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$dispatchesDir    = Join-Path $orchestratorRoot "state/external-agent-bridge/dispatches"
$completionsDir   = Join-Path $orchestratorRoot "tasks/completions"
$servicesDir      = Join-Path $orchestratorRoot "services"
$workerScript     = Join-Path $servicesDir "agent_worker.py"

# ── Backend detection ─────────────────────────────────────────────────────────
function Find-LocalTool {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$codexBin  = Find-LocalTool "codex"
$pythonBin = Find-LocalTool "python"
if (-not $pythonBin) { $pythonBin = Find-LocalTool "python3" }

$hasCodex     = -not [string]::IsNullOrEmpty($codexBin)
$hasPython    = -not [string]::IsNullOrEmpty($pythonBin) -and (Test-Path -LiteralPath $workerScript -PathType Leaf)
$hasAnthropicKey = -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY)

Write-Host ("[LocalAgentExec] backends: codex={0} python={1} anthropic_key={2}" -f `
    $hasCodex, $hasPython, $hasAnthropicKey)

# ── Agent → backend routing table (21-agent ecosystem) ────────────────────────
$agentBackendMap = @{
    # Legacy / alias names
    "codex"                    = "codex"
    "antigravity"              = "codex"
    "claude"                   = "python"
    "claude code"              = "python"
    "agent-worker"             = "python"

    # ESTRATEGIA
    "business analyst"         = "python"
    "ai architect"             = "python"
    "ai product manager"       = "python"

    # CONSTRUCAO
    "ai engineer"              = "python"
    "ai engineer frontend"     = "python"
    "ai devops engineer"       = "python"
    "database agent"           = "python"
    "integration agent"        = "python"

    # QUALIDADE
    "code review agent"        = "python"
    "ai security engineer"     = "python"
    "performance agent"        = "python"
    "qa agent"                 = "python"

    # OPERACOES
    "devops agent"             = "python"
    "user simulation agent"    = "python"
    "observability agent"      = "python"
    "incident response agent"  = "python"

    # INTELIGENCIA
    "memory agent"             = "python"
    "learning agent"           = "python"
    "estimation agent"         = "python"

    # COORDENACAO
    "ai cto"                   = "python"
    "documentation agent"      = "python"
}

function Resolve-AgentBackend {
    param([string]$AgentName)
    $normalized = $AgentName.Trim().ToLowerInvariant()
    $preferred  = $agentBackendMap[$normalized]
    if ([string]::IsNullOrEmpty($preferred)) { return "skip" }

    switch ($preferred) {
        "codex"  { if ($hasCodex)  { return "codex" }; return "python" }
        "python" { if ($hasPython) { return "python" }; return "skip" }
        default  { return "skip" }
    }
}

# ── Execute via codex exec ─────────────────────────────────────────────────────
function Invoke-CodexTask {
    param(
        [string]$TaskId,
        [hashtable]$Dispatch,
        [string]$WorkspacePath
    )

    $taskDesc  = if ($Dispatch["description"]) { $Dispatch["description"] } else { $Dispatch["title"] }
    $filesStr  = ($Dispatch["files_affected"] | ForEach-Object { "- $_" }) -join "`n"
    $deps      = ($Dispatch["dependencies"] -join ", ") ; if (-not $deps) { $deps = "none" }

    $prompt = @"
Task: $TaskId
Title: $($Dispatch["title"])
Priority: $($Dispatch["priority"])

$taskDesc

Files to modify:
$filesStr

Completed dependencies: $deps

Project: Laravel 11 PHP multi-tenant SaaS (SINC).
Conventions: use App\Traits\BelongsToTenant on all models, no raw SQL, service layer in app/Services/.

Please complete this task.
"@

    Write-Host ("[LocalAgentExec] codex exec --full-auto for $TaskId")
    if ($DryRun) { Write-Host "[DRY-RUN] Would run codex"; return @{ status = "partial"; summary = "dry-run"; files_modified = @() } }

    $lastMsgFile = [System.IO.Path]::GetTempFileName()
    try {
        $codexArgs = @(
            "exec",
            "--full-auto",
            "--cd", $WorkspacePath,
            "--output-last-message", $lastMsgFile
        )
        if ($env:CODEX_MODEL) { $codexArgs += @("--model", $env:CODEX_MODEL) }
        $codexArgs += $prompt

        $proc = Start-Process -FilePath $codexBin -ArgumentList $codexArgs `
            -WorkingDirectory $WorkspacePath -NoNewWindow -PassThru -Wait

        $summary = ""
        if (Test-Path -LiteralPath $lastMsgFile) {
            $summary = (Get-Content -LiteralPath $lastMsgFile -Raw -ErrorAction SilentlyContinue).Trim()
        }
        if (-not $summary) { $summary = "codex exec completed (exit $($proc.ExitCode))" }

        $status = if ($proc.ExitCode -eq 0) { "done" } else { "partial" }
        return @{ status = $status; summary = $summary; files_modified = @() }
    }
    catch {
        return @{ status = "failed"; summary = "codex error: $($_.Exception.Message)"; files_modified = @() }
    }
    finally {
        Remove-Item $lastMsgFile -ErrorAction SilentlyContinue
    }
}

# ── Execute via Python agent worker (one-shot mode) ────────────────────────────
function Invoke-PythonAgentTask {
    param(
        [string]$TaskId,
        [string]$DispatchPath,
        [string]$WorkspacePath
    )

    Write-Host ("[LocalAgentExec] python agent_worker for $TaskId")
    if ($DryRun) { Write-Host "[DRY-RUN] Would run python worker"; return @{ status = "partial"; summary = "dry-run"; files_modified = @() } }

    # Pass dispatch file path via env to a one-shot execution
    $envVars = @{
        "AGENT_WORKSPACE"     = $WorkspacePath
        "AGENT_DISPATCH_FILE" = $DispatchPath
        "ORCH_DB_HOST"        = if ($env:ORCH_DB_HOST)     { $env:ORCH_DB_HOST }     else { "localhost" }
        "ORCH_DB_PORT"        = if ($env:ORCH_DB_PORT)     { $env:ORCH_DB_PORT }     else { "5434" }
        "ORCH_DB_NAME"        = if ($env:ORCH_DB_NAME)     { $env:ORCH_DB_NAME }     else { "orchestrator_tasks" }
        "ORCH_DB_USER"        = if ($env:ORCH_DB_USER)     { $env:ORCH_DB_USER }     else { "orchestrator" }
        "ORCH_DB_PASSWORD"    = if ($env:ORCH_DB_PASSWORD) { $env:ORCH_DB_PASSWORD } else { "" }
    }
    if ($env:ANTHROPIC_API_KEY) { $envVars["ANTHROPIC_API_KEY"] = $env:ANTHROPIC_API_KEY }

    # Build one-shot runner script that executes a single dispatch then exits
    $oneShotScript = @"
import sys, os, json
sys.path.insert(0, r'$($servicesDir.Replace("\","\\"))')
os.environ.update($(($envVars.GetEnumerator() | ForEach-Object { "`"$($_.Key)`": `"$($_.Value)`"" }) -join ","))
from agent_worker import *
from local_agent_runner import HybridAgentRunner
runner = HybridAgentRunner()
dispatch_file = os.environ.get('AGENT_DISPATCH_FILE','')
if not dispatch_file or not os.path.exists(dispatch_file): sys.exit(1)
dispatch = json.loads(open(dispatch_file, encoding='utf-8').read())
task_id = dispatch.get('task_id', os.path.splitext(os.path.basename(dispatch_file))[0])
preflight_ctx = ''
pf = dispatch.get('preflight_path','')
if pf:
    pf_full = os.path.join(str(WORKSPACE), pf.lstrip('/'))
    if os.path.exists(pf_full): preflight_ctx = open(pf_full, encoding='utf-8', errors='replace').read()
result = runner.run(task_id, dispatch, preflight_ctx)
write_completion(task_id, dispatch, result)
update_task_in_db(task_id, 'done' if result.status in ('done','partial') else 'failed', dispatch.get('assigned_agent','agent-worker'))
print(json.dumps({'status': result.status, 'summary': result.summary[:200]}))
"@

    $tmpScript = [System.IO.Path]::GetTempFileName() + ".py"
    try {
        [System.IO.File]::WriteAllText($tmpScript, $oneShotScript, [System.Text.Encoding]::UTF8)

        $proc = Start-Process -FilePath $pythonBin -ArgumentList @($tmpScript) `
            -WorkingDirectory $WorkspacePath -NoNewWindow -PassThru -Wait

        $status = if ($proc.ExitCode -eq 0) { "done" } else { "partial" }
        return @{ status = $status; summary = "python agent completed (exit $($proc.ExitCode))"; files_modified = @() }
    }
    catch {
        return @{ status = "failed"; summary = "python agent error: $($_.Exception.Message)"; files_modified = @() }
    }
    finally {
        Remove-Item $tmpScript -ErrorAction SilentlyContinue
    }
}

# ── Main: scan dispatches and execute locally ─────────────────────────────────
if (-not (Test-Path -LiteralPath $dispatchesDir -PathType Container)) {
    Write-Host "[LocalAgentExec] No dispatches directory found. Nothing to do."
    exit 0
}

$dispatches = @(Get-ChildItem -LiteralPath $dispatchesDir -Filter "*.json" -File -ErrorAction SilentlyContinue)
if ($dispatches.Count -eq 0) {
    Write-Host "[LocalAgentExec] No pending dispatches."
    exit 0
}

Write-Host ("[LocalAgentExec] Found {0} dispatch(es)" -f $dispatches.Count)

$executed = 0
foreach ($dispFile in ($dispatches | Select-Object -First $MaxTasks)) {
    $taskId = $dispFile.BaseName

    # Skip if already completed
    $completionPath = Join-Path $completionsDir "$taskId.json"
    if (Test-Path -LiteralPath $completionPath -PathType Leaf) {
        Write-Host "  [$taskId] already complete — removing dispatch"
        Remove-Item $dispFile.FullName -ErrorAction SilentlyContinue
        continue
    }

    $dispatch = $null
    try {
        $dispatch = Get-V2JsonContent -Path $dispFile.FullName
    }
    catch {
        Write-Warning "  [$taskId] failed to read dispatch: $($_.Exception.Message)"
        continue
    }

    $agentName = [string](Get-V2OptionalProperty -InputObject $dispatch -Name "assigned_agent" -DefaultValue "")
    $backend   = Resolve-AgentBackend -AgentName $agentName

    Write-Host ("  [$taskId] agent={0} backend={1}" -f $agentName, $backend)

    if ($backend -eq "skip") {
        Write-Host "  [$taskId] no local backend available — leaving for external agent"
        continue
    }

    $result = $null
    switch ($backend) {
        "codex"  { $result = Invoke-CodexTask  -TaskId $taskId -Dispatch @{
            title           = [string](Get-V2OptionalProperty -InputObject $dispatch -Name "title"           -DefaultValue $taskId)
            description     = [string](Get-V2OptionalProperty -InputObject $dispatch -Name "description"     -DefaultValue "")
            priority        = [string](Get-V2OptionalProperty -InputObject $dispatch -Name "priority"        -DefaultValue "P2")
            files_affected  = @(Get-V2OptionalProperty -InputObject $dispatch -Name "files_affected"  -DefaultValue @())
            dependencies    = @(Get-V2OptionalProperty -InputObject $dispatch -Name "dependencies"    -DefaultValue @())
        } -WorkspacePath $resolvedPath }

        "python" { $result = Invoke-PythonAgentTask -TaskId $taskId -DispatchPath $dispFile.FullName `
            -WorkspacePath $resolvedPath }
    }

    if ($result) {
        $executed++
        $statusStr = $result["status"]
        $summaryStr = [string]$result["summary"]
        Write-Host ("  [$taskId] result={0}: {1}" -f $statusStr, $summaryStr.Substring(0, [Math]::Min(100, $summaryStr.Length)))

        # Remove dispatch after successful local execution
        if ($statusStr -in @("done", "partial")) {
            Remove-Item $dispFile.FullName -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ("[LocalAgentExec] executed={0} remaining={1}" -f $executed, ($dispatches.Count - $executed))
