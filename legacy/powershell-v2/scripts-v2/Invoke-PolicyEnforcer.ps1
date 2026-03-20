<#
.SYNOPSIS
    V2 Policy Enforcer - Ensures code compliance with Architecture Decision Records (ADRs) and Security Policies.
.DESCRIPTION
    Analyzes project signals against established governance rules (ADRs, Tenant Isolation).
    Automatically creates REPAIR tasks for high-severity violations.
.PARAMETER ProjectPath
    Path to the project root.
.PARAMETER EmitJson
    Output results as JSON.
.PARAMETER SkipRepair
    If set, findings will be reported but no REPAIR tasks will be created.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [switch]$EmitJson,
    [switch]$SkipRepair
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
$orchestratorRoot = Join-Path $resolvedProjectPath "ai-orchestrator"
$reportsDir = Join-Path $orchestratorRoot "reports"
$incidentsDir = Join-Path $reportsDir "incidents"
$dagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$backlogPath = Join-Path $orchestratorRoot "tasks/backlog.md"
$selfHealingPath = Join-Path $orchestratorRoot "state/self-healing-state.json"
$dedupPath = Join-Path $orchestratorRoot "state/observer-incident-dedup.json"

$findings = New-Object System.Collections.Generic.List[object]

# --- 0. Performance Monitoring Setup ---
$startTime = Get-Date

# ADR-0001 & ADR-0002 Enforcement
$policies = @{
    ADR0001 = @{
        MaxControllerLines = 400
        ExtremeControllerLines = 800
        ForbiddenPatterns = @("DB::table", "PDO", "die\(", "exit\(")
    }
    ADR0002 = @{
        MinDocLines = 10
        ForbiddenPlaceholders = @("TODO", "REVIEW_REQUIRED", "INSERT_CONTENT_HERE")
        CriticalArtifacts = @(
            "ai-orchestrator/context/business-context.json",
            "ai-orchestrator/documentation/architecture.md",
            "ai-orchestrator/documentation/interfaces/contracts.md"
        )
    }
}

# 1. ADR-0001: Controller Complexity & Patterns
$controllersPath = Join-Path $resolvedProjectPath "app/Http/Controllers"
if (Test-Path -LiteralPath $controllersPath) {
    $controllers = Get-ChildItem -LiteralPath $controllersPath -Filter "*.php" -Recurse
    foreach ($controller in $controllers) {
        $content = Get-Content -LiteralPath $controller.FullName
        $lineCount = $content.Count
        $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $controller.FullName

        if ($lineCount -gt $policies.ADR0001.ExtremeControllerLines) {
            $findings.Add([PSCustomObject]@{
                rule = "ADR-0001:ModularControllers"
                level = "HIGH"
                file = $relPath
                evidence = "Line count: $lineCount (Threshold: $($policies.ADR0001.ExtremeControllerLines))"
                recommendation = "CRITICAL: Refactor this massive controller immediately into smaller Services or Actions."
                severity = "HIGH"
            })
        }
        elseif ($lineCount -gt $policies.ADR0001.MaxControllerLines) {
            $findings.Add([PSCustomObject]@{
                rule = "ADR-0001:ThinControllers"
                level = "WARNING"
                file = $relPath
                evidence = "Line count: $lineCount"
                recommendation = "Refactor logic into Service classes or Actions."
                severity = "MEDIUM"
            })
        }

        $rawContent = $content -join "`n"
        foreach ($pattern in $policies.ADR0001.ForbiddenPatterns) {
            if ($rawContent -match $pattern) {
                $findings.Add([PSCustomObject]@{
                    rule = "ADR-0001:StandardizedPatterns"
                    level = "MEDIUM"
                    file = $relPath
                    evidence = "Found usage of '$pattern'"
                    recommendation = "Follow project standards. Use Eloquent/Query Builder and avoid abrupt exits."
                    severity = "MEDIUM"
                })
            }
        }
    }
}

# 2. ADR-0002: Artifact Quality
foreach ($artifactRelPath in $policies.ADR0002.CriticalArtifacts) {
    $fullPath = Join-Path $resolvedProjectPath $artifactRelPath
    if (-not (Test-Path -LiteralPath $fullPath)) {
        $findings.Add([PSCustomObject]@{
            rule = "ADR-0002:MissingCriticalArtifact"
            level = "HIGH"
            file = $artifactRelPath
            evidence = "File not found"
            recommendation = "Create this mandatory artifact to satisfy architectural requirements."
            severity = "HIGH"
        })
        continue
    }

    if ($artifactRelPath.EndsWith(".md")) {
        $content = Get-Content -LiteralPath $fullPath
        $nonEmptyLines = @($content | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
        
        if ($nonEmptyLines -lt $policies.ADR0002.MinDocLines) {
            $findings.Add([PSCustomObject]@{
                rule = "ADR-0002:InsufficientDocumentationContent"
                level = "MEDIUM"
                file = $artifactRelPath
                evidence = "Lines of content: $nonEmptyLines"
                recommendation = "Expand documentation to provide meaningful context (minimum $($policies.ADR0002.MinDocLines) lines)."
                severity = "MEDIUM"
            })
        }

        $rawContent = $content -join "`n"
        foreach ($placeholder in $policies.ADR0002.ForbiddenPlaceholders) {
            if ($rawContent -match [regex]::Escape($placeholder)) {
                $findings.Add([PSCustomObject]@{
                    rule = "ADR-0002:PlaceholderDetected"
                    level = "MEDIUM"
                    file = $artifactRelPath
                    evidence = "Found placeholder '$placeholder'"
                    recommendation = "Remove development placeholders and finalize documentation."
                    severity = "MEDIUM"
                })
            }
        }
    }
}

# 3. Structural & Domain Standards
$servicesPath = Join-Path $resolvedProjectPath "app/Services"
if (Test-Path -LiteralPath $servicesPath) {
    $serviceSuffixes = @("Service.php", "Provider.php", "Interface.php", "Manager.php", "Catalog.php", "Trait.php")
    $serviceFiles = Get-ChildItem -LiteralPath $servicesPath -Filter "*.php" -Recurse
    foreach ($file in $serviceFiles) {
        $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $file.FullName
        $isStandard = $false
        foreach ($suffix in $serviceSuffixes) {
            if ($file.Name.EndsWith($suffix)) { $isStandard = $true; break }
        }

        if (-not $isStandard) {
            $findings.Add([PSCustomObject]@{
                rule = "Domain:StructuralAnomaly"
                level = "MEDIUM"
                file = $relPath
                evidence = "Non-standard suffix in Services directory"
                recommendation = "Move this file to a more appropriate directory (e.g., DTOs, Results) or rename to follow Service patterns."
                severity = "MEDIUM"
            })
        }

        # Forbidden Comments Scan (Debug leftovers)
        $content = Get-Content -LiteralPath $file.FullName -Raw
        if ($content -match "dd\(" -or $content -match "dump\(" -or $content -match "var_dump\(") {
            $findings.Add([PSCustomObject]@{
                rule = "Domain:DebugLeftover"
                level = "MEDIUM"
                file = $relPath
                evidence = "Found debug call (dd/dump/var_dump)"
                recommendation = "Remove debug statements before finalizing code."
                severity = "MEDIUM"
            })
        }
    }
}

# 4. Controller Domain Standards
$controllersPath = Join-Path $resolvedProjectPath "app/Http/Controllers"
if (Test-Path -LiteralPath $controllersPath) {
    $controllers = Get-ChildItem -LiteralPath $controllersPath -Filter "*.php"
    foreach ($controller in $controllers) {
        if ($controller.Name -eq "Controller.php") { continue }
        $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $controller.FullName
        $findings.Add([PSCustomObject]@{
            rule = "Domain:FlatControllerAnomaly"
            level = "MEDIUM"
            file = $relPath
            evidence = "Controller lacks domain subdirectory grouping"
            recommendation = "Move controller into a domain subdirectory (e.g., app/Http/Controllers/DomainName/)."
            severity = "MEDIUM"
        })
    }
}

# 5. Model Integrity Standards
$modelsPath = Join-Path $resolvedProjectPath "app/Models"
if (Test-Path -LiteralPath $modelsPath) {
    $models = Get-ChildItem -LiteralPath $modelsPath -Filter "*.php"
    foreach ($model in $models) {
        $content = Get-Content -LiteralPath $model.FullName -Raw
        if ($content -notmatch "extends\s+Model" -and $content -notmatch "use\s+Illuminate\\Database\\Eloquent\\Model") {
             $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $model.FullName
             $findings.Add([PSCustomObject]@{
                rule = "Domain:ModelStructuralAnomaly"
                level = "MEDIUM"
                file = $relPath
                evidence = "File in Models/ does not appear to be an Eloquent Model"
                recommendation = "Ensure this file is a valid Eloquent Model or move it to a more appropriate directory."
                severity = "MEDIUM"
            })
        }
    }
}

# 6. Tenant Isolation Semantic Check
$isolationCheckPath = Join-Path $PSScriptRoot "Check-TenantIsolationPolicy.ps1"
if (Test-Path -LiteralPath $isolationCheckPath) {
    try {
        $isolationOutput = & $isolationCheckPath -ProjectPath $resolvedProjectPath -EmitJson
        $isolationReport = $isolationOutput | ConvertFrom-Json
        $isolationFindings = @(Get-V2OptionalProperty -InputObject $isolationReport -Name "findings" -DefaultValue @())
        
        foreach ($f in $isolationFindings) {
            $findings.Add([PSCustomObject]@{
                rule = "Security:TenantIsolation"
                level = $f.severity
                file = $f.file
                evidence = $f.evidence
                recommendation = $f.recommendation
                severity = $f.severity
                title = $f.title
            })
        }
    } catch {
        Write-Warning "Failed to run Tenant Isolation check: $($_.Exception.Message)"
    }
}

# --- Automated Repair Generation ---
if (-not $SkipRepair) {
    foreach ($f in $findings) {
        if ($f.severity -eq "HIGH") {
            $category = "policy-violation"
            $title = "Policy Violation: $($f.rule) in $($f.file)"
            $details = "Violation detected by Policy Enforcer.`n`nRule: $($f.rule)`nFile: $($f.file)`nEvidence: $($f.evidence)`nRecommendation: $($f.recommendation)"
            
            $incidentPath = New-IncidentReport `
                -ReportDirectory $incidentsDir `
                -Category $category `
                -Title $title `
                -Details $details `
                -DedupPath $dedupPath `
                -DedupCooldownSeconds 3600 `
                -DedupCategories @($category)

            if (-not [string]::IsNullOrWhiteSpace($incidentPath)) {
                Add-RepairTask `
                    -SelfHealingPath $selfHealingPath `
                    -BacklogPath $backlogPath `
                    -IncidentPath $incidentPath `
                    -Reason $f.recommendation `
                    -TaskDagJsonPath $dagPath `
                    -ExecutionMode "artifact-validation"
            }
        }
    }
}

# --- 4. Performance Finalization ---
$endTime = Get-Date
$executionTimeSeconds = [Math]::Round(($endTime - $startTime).TotalSeconds, 3)

# --- Reporting ---
$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    project_path = $resolvedProjectPath
    policy_compliance_score = [Math]::Max(0, (100 - ($findings.Count * 5)))
    performance_metadata = @{
        execution_time_seconds = $executionTimeSeconds
    }
    findings = @($findings.ToArray())
}

# Always persist report to disk so Invoke-ProjectAggregation.ps1 can aggregate it
if (-not (Test-Path -LiteralPath $reportsDir)) { New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null }
$reportJson = $report | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path $reportsDir "latest-policy-report.json"), $reportJson)

if ($EmitJson) {
    $reportJson
} else {
    Write-Host "--- Policy Enforcement Report ---" -ForegroundColor Cyan
    Write-Host "Overall Compliance Score: $($report.policy_compliance_score)%"
    Write-Host "Execution Time: $($report.performance_metadata.execution_time_seconds)s"
    Write-Host ""
    foreach ($f in $report.findings) {
        $color = switch ($f.severity) {
            "HIGH" { "Red" }
            "MEDIUM" { "Yellow" }
            default { "Gray" }
        }
        Write-Host "[$($f.severity)] $($f.rule)" -ForegroundColor $color
        Write-Host "  File: $($f.file)"
        Write-Host "  Rec: $($f.recommendation)"
        Write-Host ""
    }
}
