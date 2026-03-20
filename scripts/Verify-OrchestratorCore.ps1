<#
.SYNOPSIS
    Verifies orchestrator core integrity for a managed project.
.DESCRIPTION
    Runs non-mutating checks over ai-orchestrator state, DAG, locks and memory backends.
    Produces READY / NOT READY verdict with evidence.
.PARAMETER ProjectPath
    Target project root that contains ai-orchestrator/.
.PARAMETER EmitJson
    Emits machine-readable JSON summary.
.PARAMETER FailOnWarning
    Treat WARN as FAIL in final verdict.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [switch]$EmitJson,
    [switch]$FailOnWarning
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Add-Check {
    param(
        [System.Collections.Generic.List[object]]$Checks,
        [string]$Name,
        [ValidateSet("PASS", "WARN", "FAIL")]
        [string]$Status,
        [string]$Evidence = ""
    )
    $Checks.Add([PSCustomObject]@{
        name     = $Name
        status   = $Status
        evidence = $Evidence
    })
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-PropNames {
    param([object]$Object)
    if ($null -eq $Object) { return @() }
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Get-V2VerifyNeo4jPasswordFromDockerEnv {
    param([string]$OrchestratorPath)

    if ([string]::IsNullOrWhiteSpace($OrchestratorPath)) {
        return ""
    }

    $envPath = Join-Path $OrchestratorPath "docker/.env.docker.generated"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        return ""
    }

    try {
        $line = Get-Content -LiteralPath $envPath -ErrorAction Stop |
            Where-Object { $_ -match "^\s*NEO4J_PASSWORD=" } |
            Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace([string]$line)) {
            return ""
        }

        return [string]$line.Substring($line.IndexOf("=") + 1).Trim()
    }
    catch {
        return ""
    }
}

function Unprotect-V2VerifySecret {
    param(
        [string]$Value,
        [bool]$VaultEncrypted = $false,
        [string]$VaultEncryption = ""
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    if (-not $VaultEncrypted) {
        return $Value
    }
    if (-not ([string]$VaultEncryption).ToLowerInvariant().StartsWith("dpapi")) {
        return $Value
    }

    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
        $cipherBytes = [Convert]::FromBase64String($Value)
        $plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $cipherBytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [System.Text.Encoding]::UTF8.GetString($plainBytes)
    }
    catch {
        return $Value
    }
}

$resolvedProjectPath = if (Test-Path -LiteralPath $ProjectPath -PathType Container) {
    (Resolve-Path -LiteralPath $ProjectPath).Path
}
else {
    throw "Project path does not exist: $ProjectPath"
}

$orch = Join-Path $resolvedProjectPath "ai-orchestrator"
$checks = New-Object System.Collections.Generic.List[object]

if (-not (Test-Path -LiteralPath $orch -PathType Container)) {
    Add-Check -Checks $checks -Name "orchestrator-layer" -Status "FAIL" -Evidence "Missing: $orch"
}
else {
    Add-Check -Checks $checks -Name "orchestrator-layer" -Status "PASS" -Evidence $orch
}

$projectStatePath = Join-Path $orch "state/project-state.json"
$healthPath = Join-Path $orch "state/health-report.json"
$loopPath = Join-Path $orch "state/loop-state.json"
$dagPath = Join-Path $orch "tasks/task-dag.json"
$locksPath = Join-Path $orch "locks/locks.json"
$connPath = Join-Path $orch "database/connection-pack.json"
$runtimeEventsPath = Join-Path $orch "projects"

$projectState = Read-JsonFile -Path $projectStatePath
if ($null -eq $projectState) {
    Add-Check -Checks $checks -Name "project-state" -Status "FAIL" -Evidence "Unreadable or missing: $projectStatePath"
}
else {
    $required = @("status", "health_status", "last_observer_run", "unknowns", "open_questions")
    $missing = @($required | Where-Object { $_ -notin (Get-PropNames -Object $projectState) })
    if ($missing.Count -gt 0) {
        Add-Check -Checks $checks -Name "project-state-shape" -Status "WARN" -Evidence ("Missing keys: " + ($missing -join ", "))
    }
    else {
        Add-Check -Checks $checks -Name "project-state-shape" -Status "PASS" -Evidence $projectStatePath
    }
}

$health = Read-JsonFile -Path $healthPath
if ($null -eq $health) {
    Add-Check -Checks $checks -Name "health-report" -Status "FAIL" -Evidence "Unreadable or missing: $healthPath"
}
else {
    $healthKeys = Get-PropNames -Object $health
    $requiredHealth = @("generated_at", "fingerprint", "health_status", "check_results", "incidents", "unknowns")
    $missingHealth = @($requiredHealth | Where-Object { $_ -notin $healthKeys })
    if ($missingHealth.Count -gt 0) {
        Add-Check -Checks $checks -Name "health-report-shape" -Status "FAIL" -Evidence ("Missing keys: " + ($missingHealth -join ", "))
    }
    elseif ("loop_running" -in $healthKeys) {
        Add-Check -Checks $checks -Name "health-report-integrity" -Status "FAIL" -Evidence "health-report.json contaminated by loop heartbeat schema."
    }
    else {
        Add-Check -Checks $checks -Name "health-report-integrity" -Status "PASS" -Evidence "Observer schema intact."
    }
}

$loopState = Read-JsonFile -Path $loopPath
if ($null -eq $loopState) {
    Add-Check -Checks $checks -Name "loop-state" -Status "WARN" -Evidence "Missing: $loopPath"
}
else {
    $loopKeys = Get-PropNames -Object $loopState
    if ("loop_running" -in $loopKeys -and "cycle" -in $loopKeys) {
        Add-Check -Checks $checks -Name "loop-state-shape" -Status "PASS" -Evidence $loopPath
    }
    else {
        Add-Check -Checks $checks -Name "loop-state-shape" -Status "WARN" -Evidence "Unexpected keys in loop-state.json"
    }
}

$dag = Read-JsonFile -Path $dagPath
if ($null -eq $dag) {
    Add-Check -Checks $checks -Name "task-dag" -Status "FAIL" -Evidence "Unreadable or missing: $dagPath"
}
else {
    $tasks = @($dag.tasks)
    if ($tasks.Count -eq 0) {
        Add-Check -Checks $checks -Name "task-dag-populated" -Status "FAIL" -Evidence "No tasks in DAG."
    }
    else {
        Add-Check -Checks $checks -Name "task-dag-populated" -Status "PASS" -Evidence ("tasks=" + $tasks.Count)
    }
}

$locks = Read-JsonFile -Path $locksPath
if ($null -eq $locks) {
    Add-Check -Checks $checks -Name "locks" -Status "WARN" -Evidence "Unreadable or missing: $locksPath"
}
else {
    $lockEntries = @()
    if ($locks -is [System.Array]) {
        $lockEntries = @($locks)
    }
    elseif ($locks.PSObject.Properties.Name -contains "locks") {
        $lockEntries = @($locks.locks)
    }
    else {
        $lockEntries = @($locks)
    }
    $activeLocks = @($lockEntries | Where-Object { [string]$_.status -eq "active" })
    Add-Check -Checks $checks -Name "locks-active-count" -Status "PASS" -Evidence ("active=" + $activeLocks.Count)
}

$runtimeEventCount = 0
$projectSlug = ""
if ($projectState -and ($projectState.PSObject.Properties.Name -contains "project_slug")) {
    $projectSlug = [string]$projectState.project_slug
}
if (-not [string]::IsNullOrWhiteSpace($projectSlug)) {
    $runtimePath = Join-Path $orch ("projects/{0}/memory/runtime-events" -f $projectSlug)
    if (Test-Path -LiteralPath $runtimePath -PathType Container) {
        $runtimeEventCount = (Get-ChildItem -LiteralPath $runtimePath -File -Filter "observer-*.md" -ErrorAction SilentlyContinue | Measure-Object).Count
    }
}
if ($runtimeEventCount -gt 0) {
    Add-Check -Checks $checks -Name "runtime-events" -Status "PASS" -Evidence ("files=" + $runtimeEventCount)
}
else {
    Add-Check -Checks $checks -Name "runtime-events" -Status "WARN" -Evidence "No observer runtime-event files found."
}

$conn = Read-JsonFile -Path $connPath
$vaultPath = Join-Path $orch "database/.secrets/vault.json"
$vault = if (Test-Path -LiteralPath $vaultPath -PathType Leaf) { Read-JsonFile -Path $vaultPath } else { $null }
$vaultEncrypted = $false
$vaultEncryptionMode = ""
if ($vault -and ($vault.PSObject.Properties.Name -contains "encrypted")) {
    try { $vaultEncrypted = [bool]$vault.encrypted } catch { $vaultEncrypted = $false }
}
if ($vault -and ($vault.PSObject.Properties.Name -contains "encryption")) {
    $vaultEncryptionMode = [string]$vault.encryption
}
if ($null -eq $conn) {
    Add-Check -Checks $checks -Name "connection-pack" -Status "WARN" -Evidence "Missing or unreadable: $connPath"
}
else {
    Add-Check -Checks $checks -Name "connection-pack" -Status "PASS" -Evidence $connPath

    $relational = $conn.connections.transactional_db
    if ($relational -and [bool]$relational.enabled) {
        $schemaName = [string]$relational.schema
        if ([string]::IsNullOrWhiteSpace($schemaName)) {
            Add-Check -Checks $checks -Name "relational-schema-isolation" -Status "FAIL" -Evidence "transactional_db.schema missing in connection pack"
        }
        else {
            Add-Check -Checks $checks -Name "relational-schema-isolation" -Status "PASS" -Evidence ("schema=" + $schemaName)
        }
    }

    try {
        $qdrant = $conn.connections.qdrant
        if ($qdrant -and [bool]$qdrant.enabled) {
            $qUrl = ("http://{0}:{1}/collections/{2}/points/scroll" -f $qdrant.host, $qdrant.port, $qdrant.collection)
            $qBody = '{"limit":1000,"with_payload":false,"with_vector":false}'
            $qRes = Invoke-RestMethod -Method POST -Uri $qUrl -ContentType "application/json" -Body $qBody -TimeoutSec 10
            $qPoints = @($qRes.result.points).Count
            $qStatus = if ($qPoints -gt 0) { "PASS" } else { "WARN" }
            Add-Check -Checks $checks -Name "qdrant-ingestion" -Status $qStatus -Evidence ("points=" + $qPoints)

            if (-not [string]::IsNullOrWhiteSpace($projectSlug)) {
                $qBodyWithPayload = '{"limit":1000,"with_payload":true,"with_vector":false}'
                $qResPayload = Invoke-RestMethod -Method POST -Uri $qUrl -ContentType "application/json" -Body $qBodyWithPayload -TimeoutSec 10
                $pointsWithPayload = @($qResPayload.result.points)
                $missingSlug = 0
                $foreignSlug = 0
                foreach ($point in $pointsWithPayload) {
                    $payload = $point.payload
                    $payloadSlug = [string]($payload.project_slug)
                    if ([string]::IsNullOrWhiteSpace($payloadSlug)) {
                        $missingSlug++
                    }
                    elseif ($payloadSlug -ne $projectSlug) {
                        $foreignSlug++
                    }
                }

                if ($missingSlug -gt 0 -or $foreignSlug -gt 0) {
                    Add-Check -Checks $checks -Name "qdrant-cross-project-contamination" -Status "FAIL" -Evidence ("missing_slug={0} foreign_slug={1}" -f $missingSlug, $foreignSlug)
                }
                else {
                    Add-Check -Checks $checks -Name "qdrant-cross-project-contamination" -Status "PASS" -Evidence ("validated_points=" + $pointsWithPayload.Count)
                }
            }
        }
    }
    catch {
        Add-Check -Checks $checks -Name "qdrant-ingestion" -Status "WARN" -Evidence $_.Exception.Message
    }

    try {
        $neo = $conn.connections.neo4j
        if ($neo -and [bool]$neo.enabled -and -not [string]::IsNullOrWhiteSpace([string]$projectSlug)) {
            $neoPassword = [string]$neo.password
            $dockerEnvNeo4jPassword = Get-V2VerifyNeo4jPasswordFromDockerEnv -OrchestratorPath $orch
            if ([string]::IsNullOrWhiteSpace($neoPassword) -or $neoPassword -match "\*" -or $neoPassword -eq "[stored in vault]") {
                if ($vault -and ($vault.PSObject.Properties.Name -contains "secrets")) {
                    $vaultSecrets = $vault.secrets
                    if ($vaultSecrets -and ($vaultSecrets.PSObject.Properties.Name -contains "neo4j")) {
                        $vaultNeo4j = $vaultSecrets.neo4j
                        if ($vaultNeo4j -and ($vaultNeo4j.PSObject.Properties.Name -contains "password")) {
                            $neoPassword = Unprotect-V2VerifySecret `
                                -Value ([string]$vaultNeo4j.password) `
                                -VaultEncrypted $vaultEncrypted `
                                -VaultEncryption $vaultEncryptionMode
                        }
                    }
                }
            }
            $looksLikeDpapiCipherText = $neoPassword -match "^[A-Za-z0-9+/=]+$" -and $neoPassword.StartsWith("AQAAANCM") -and $neoPassword.Length -ge 128
            if (($looksLikeDpapiCipherText -or [string]::IsNullOrWhiteSpace($neoPassword)) -and -not [string]::IsNullOrWhiteSpace($dockerEnvNeo4jPassword)) {
                $neoPassword = $dockerEnvNeo4jPassword
            }
            $b = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes(("{0}:{1}" -f $neo.user, $neoPassword)))
            $headers = @{
                Authorization = "Basic $b"
                Accept = "application/json"
                "Content-Type" = "application/json"
            }
            $db = [string]$neo.database
            if ([string]::IsNullOrWhiteSpace($db)) { $db = "neo4j" }
            $neo4jHttpPort = [int]$neo.http_port
            if ($neo4jHttpPort -le 0) { $neo4jHttpPort = 7474 }
            $body = '{ "statements": [ { "statement": "MATCH (n:MemoryNode {project_slug: $project_slug}) RETURN count(n) AS c", "parameters": { "project_slug": "' + $projectSlug + '" } } ] }'
            try {
                $nRes = Invoke-RestMethod -Method POST -Uri ("http://localhost:{0}/db/{1}/tx/commit" -f $neo4jHttpPort, $db) -Headers $headers -Body $body -TimeoutSec 10
            }
            catch {
                $statusCode = $null
                try { $statusCode = $_.Exception.Response.StatusCode.value__ } catch { $statusCode = $null }
                if ($statusCode -eq 401 -and -not [string]::IsNullOrWhiteSpace($dockerEnvNeo4jPassword) -and $dockerEnvNeo4jPassword -ne $neoPassword) {
                    $retryAuth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes(("{0}:{1}" -f $neo.user, $dockerEnvNeo4jPassword)))
                    $retryHeaders = @{
                        Authorization = "Basic $retryAuth"
                        Accept = "application/json"
                        "Content-Type" = "application/json"
                    }
                    $nRes = Invoke-RestMethod -Method POST -Uri ("http://localhost:{0}/db/{1}/tx/commit" -f $neo4jHttpPort, $db) -Headers $retryHeaders -Body $body -TimeoutSec 10
                    $headers = $retryHeaders
                    $neoPassword = $dockerEnvNeo4jPassword
                }
                else {
                    throw
                }
            }
            $nCount = [int]$nRes.results[0].data[0].row[0]
            $nStatus = if ($nCount -gt 0) { "PASS" } else { "WARN" }
            Add-Check -Checks $checks -Name "neo4j-ingestion" -Status $nStatus -Evidence ("nodes=" + $nCount)

            $contaminationBody = @'
{
  "statements": [
    {
      "statement": "MATCH (p:Project {slug: $project_slug})-[:HAS_NODE]->(n:MemoryNode) RETURN count(n) AS scoped_nodes, sum(CASE WHEN coalesce(n.project_slug,'') <> $project_slug THEN 1 ELSE 0 END) AS foreign_nodes",
      "parameters": {
        "project_slug": "__PROJECT_SLUG__"
      }
    }
  ]
}
'@.Replace("__PROJECT_SLUG__", $projectSlug)
            $contaminationRes = Invoke-RestMethod -Method POST -Uri ("http://localhost:{0}/db/{1}/tx/commit" -f $neo4jHttpPort, $db) -Headers $headers -Body $contaminationBody -TimeoutSec 10
            $contaminationRow = @($contaminationRes.results[0].data[0].row)
            $scopedNodes = if ($contaminationRow.Count -ge 1) { [int]$contaminationRow[0] } else { 0 }
            $foreignNodes = if ($contaminationRow.Count -ge 2) { [int]$contaminationRow[1] } else { 0 }
            if ($foreignNodes -gt 0) {
                Add-Check -Checks $checks -Name "neo4j-cross-project-contamination" -Status "FAIL" -Evidence ("scoped_nodes={0} foreign_nodes={1}" -f $scopedNodes, $foreignNodes)
            }
            else {
                Add-Check -Checks $checks -Name "neo4j-cross-project-contamination" -Status "PASS" -Evidence ("scoped_nodes=" + $scopedNodes)
            }
        }
    }
    catch {
        Add-Check -Checks $checks -Name "neo4j-ingestion" -Status "WARN" -Evidence $_.Exception.Message
    }
}

$failCount = @($checks | Where-Object { $_.status -eq "FAIL" }).Count
$warnCount = @($checks | Where-Object { $_.status -eq "WARN" }).Count
$passCount = @($checks | Where-Object { $_.status -eq "PASS" }).Count
$notReady = $failCount -gt 0 -or ($FailOnWarning -and $warnCount -gt 0)
$verdict = if ($notReady) { "NOT READY" } else { "READY" }

$result = [PSCustomObject]@{
    generated_at = (Get-Date).ToString("o")
    project_path = $resolvedProjectPath
    verdict = $verdict
    counts = [PSCustomObject]@{
        pass = $passCount
        warn = $warnCount
        fail = $failCount
    }
    checks = @($checks.ToArray())
}

if ($EmitJson) {
    $result | ConvertTo-Json -Depth 8
}
else {
    Write-Host ""
    Write-Host "=== Orchestrator Core Verification ==="
    Write-Host ("Project: {0}" -f $resolvedProjectPath)
    foreach ($check in @($checks.ToArray())) {
        $icon = switch ($check.status) {
            "PASS" { "[OK]" }
            "WARN" { "[WARN]" }
            default { "[FAIL]" }
        }
        Write-Host ("{0} {1} - {2}" -f $icon, $check.name, $check.evidence)
    }
    Write-Host ("Verdict: {0} (pass={1}, warn={2}, fail={3})" -f $verdict, $passCount, $warnCount, $failCount)
}

if ($notReady) { exit 1 }
exit 0
