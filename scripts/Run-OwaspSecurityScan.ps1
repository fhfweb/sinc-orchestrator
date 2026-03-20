<#
.SYNOPSIS
    Runs an automated OWASP-oriented static security scan.
.DESCRIPTION
    Performs lightweight repository checks aligned with OWASP Top 10 guidance in docs/skills/security.md.
    Produces:
      - ai-orchestrator/analysis/security-report.md
      - ai-orchestrator/state/security-scan.json
      - optional alert entry for critical findings
.PARAMETER ProjectPath
    Project root path.
.PARAMETER EmitJson
    Emits summary JSON to stdout.
#>
param(
    [string]$ProjectPath = ".",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path (Join-Path $PSScriptRoot "v2") "Common.ps1")

function Add-Finding {
    param(
        [System.Collections.Generic.List[object]]$Collection,
        [string]$Category,
        [string]$Title,
        [string]$Severity,
        [string]$Evidence,
        [string]$Recommendation
    )

    $Collection.Add([PSCustomObject]@{
        category = $Category
        title = $Title
        severity = $Severity
        evidence = $Evidence
        recommendation = $Recommendation
    })
}

function Get-RepoFiles {
    param([string]$Root)

    $exclude = @(".git", "node_modules", "vendor", "dist", "build", "coverage", "__pycache__", ".pytest_cache", "pytest-cache-files-", ".venv", "workspace/tmp", "docs", "archive", "memory_graph", "ai-orchestrator")
    $allowedExtensions = @(".py", ".ps1", ".psm1", ".js", ".ts", ".tsx", ".jsx", ".php", ".go", ".java", ".cs", ".rb", ".rs", ".env", ".yml", ".yaml", ".json", ".ini", ".toml", ".xml", ".config", ".sql")
    return @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
            $full = $_.FullName.ToLowerInvariant().Replace("\", "/")
            if ($_.Name -eq "Run-OwaspSecurityScan.ps1") { return $false }
            if ($allowedExtensions -notcontains $_.Extension.ToLowerInvariant()) { return $false }
            foreach ($x in $exclude) {
                $segment = "/" + $x.ToLowerInvariant().Replace("\", "/").Trim("/") + "/"
                if ($full.Contains($segment)) { return $false }
            }
            return $true
        }
    )
}

function Find-PatternHits {
    param(
        [object[]]$Files,
        [string]$Pattern,
        [switch]$CaseSensitive
    )

    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($file in @($Files)) {
        try {
            $result = Select-String -Path $file.FullName -Pattern $Pattern -SimpleMatch:$false -CaseSensitive:$CaseSensitive -ErrorAction SilentlyContinue
            foreach ($hit in @($result | Select-Object -First 5)) {
                $lineText = [string]$hit.Line
                $lineTrim = $lineText.Trim()
                $ext = [string]$file.Extension
                $isComment = $false
                if ($lineTrim.Length -gt 0) {
                    switch ($ext.ToLowerInvariant()) {
                        ".py" { $isComment = $lineTrim.StartsWith("#") }
                        ".ps1" { $isComment = $lineTrim.StartsWith("#") }
                        ".psm1" { $isComment = $lineTrim.StartsWith("#") }
                        ".ini" { $isComment = $lineTrim.StartsWith("#") -or $lineTrim.StartsWith(";") }
                        ".toml" { $isComment = $lineTrim.StartsWith("#") }
                        ".yaml" { $isComment = $lineTrim.StartsWith("#") }
                        ".yml" { $isComment = $lineTrim.StartsWith("#") }
                        ".env" { $isComment = $lineTrim.StartsWith("#") }
                        ".sql" { $isComment = $lineTrim.StartsWith("--") -or $lineTrim.StartsWith("/*") }
                        default {
                            $isComment = $lineTrim.StartsWith("//") -or $lineTrim.StartsWith("/*") -or $lineTrim.StartsWith("*")
                        }
                    }
                }
                if ($isComment) { continue }
                $hits.Add(("{0}:{1}" -f $hit.Path, $hit.LineNumber))
            }
        }
        catch {
        }
    }
    return @($hits.ToArray())
}

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$analysisDir = Join-Path $resolvedProjectPath "ai-orchestrator/analysis"
$stateDir = Join-Path $resolvedProjectPath "ai-orchestrator/state"
$alertsPath = Join-Path $resolvedProjectPath "ai-orchestrator/communication/alerts.md"
Ensure-V2Directory -Path $analysisDir
Ensure-V2Directory -Path $stateDir

$files = @(Get-RepoFiles -Root $resolvedProjectPath)
$codeExtensions = @(".py", ".ps1", ".psm1", ".js", ".ts", ".tsx", ".jsx", ".php", ".go", ".java", ".cs", ".rb", ".rs")
$codeFiles = @($files | Where-Object { $codeExtensions -contains $_.Extension.ToLowerInvariant() })
$findings = New-Object System.Collections.Generic.List[object]

# A02 / A05: hardcoded secrets
$secretHits = @(Find-PatternHits -Files $files -Pattern "(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{4,}['\"]")
if (@($secretHits).Count -gt 0) {
    Add-Finding -Collection $findings -Category "A02/A05" -Title "Possible hardcoded secrets" -Severity "HIGH" -Evidence ($secretHits -join ", ") -Recommendation "Move secrets to environment variables and rotate exposed values."
}

# A05: debug enabled
$debugHits = @(Find-PatternHits -Files $files -Pattern "(?i)\b(debug|app_debug)\b\s*[:=]\s*(true|1)\b")
if (@($debugHits).Count -gt 0) {
    Add-Finding -Collection $findings -Category "A05" -Title "Debug mode enabled in code/config" -Severity "MEDIUM" -Evidence ($debugHits -join ", ") -Recommendation "Disable debug in production profiles."
}

# A03: SQL injection patterns
$sqlConcatHits = @(Find-PatternHits -Files $codeFiles -Pattern "(?i)(select|insert|update|delete)\s+.*(\+|\$\{|\{0\})")
if (@($sqlConcatHits).Count -gt 0) {
    Add-Finding -Collection $findings -Category "A03" -Title "Potential SQL concatenation" -Severity "HIGH" -Evidence ($sqlConcatHits -join ", ") -Recommendation "Use parameterized queries or ORM query builders."
}

# A03: command injection primitives (function/process call patterns only)
$execHits = @(Find-PatternHits -Files $codeFiles -Pattern "(?i)(\b(exec|shell_exec|system|passthru|proc_open|popen)\s*\(|\bProcess\.Start\s*\(|\bRuntime\.getRuntime\(\)\.exec\s*\()")
if (@($execHits).Count -gt 0) {
    Add-Finding -Collection $findings -Category "A03" -Title "Dangerous command execution primitives present" -Severity "HIGH" -Evidence ($execHits -join ", ") -Recommendation "Restrict command execution and validate all inputs."
}

# A01/A07: auth hints
$authMentions = @(Find-PatternHits -Files $codeFiles -Pattern "(?i)\b(auth|authorize|authorization|jwt|login|oauth|bearer)\b")
$csrfMentions = @(Find-PatternHits -Files $files -Pattern "(?i)\b(csrf|sameSite|httponly)\b")
if (@($authMentions).Count -gt 0 -and @($csrfMentions).Count -eq 0) {
    $authEvidence = (@($authMentions | Select-Object -First 3)) -join ", "
    Add-Finding -Collection $findings -Category "A01/A07" -Title "Auth/session signals found but CSRF/session hardening not detected" -Severity "MEDIUM" -Evidence $authEvidence -Recommendation "Enforce CSRF/session cookie hardening and explicit authorization checks."
}

# A04: rate limiting presence
$rateLimitMentions = @(Find-PatternHits -Files $files -Pattern "(?i)\b(rate.?limit|throttle|limiter)\b")
if (@($rateLimitMentions).Count -eq 0) {
    Add-Finding -Collection $findings -Category "A04" -Title "No rate-limiting evidence found" -Severity "MEDIUM" -Evidence "No matcher hits for rate-limit/throttle/limiter." -Recommendation "Add rate limiting to auth and public endpoints."
}

# A06/A08: dependency lock files
$manifestToLock = @(
    @{ manifest = "package.json"; lock = "package-lock.json|pnpm-lock.yaml|yarn.lock" },
    @{ manifest = "requirements.txt"; lock = "poetry.lock|Pipfile.lock" },
    @{ manifest = "composer.json"; lock = "composer.lock" },
    @{ manifest = "Cargo.toml"; lock = "Cargo.lock" },
    @{ manifest = "go.mod"; lock = "go.sum" }
)
foreach ($pair in $manifestToLock) {
    $manifestExists = Test-Path -LiteralPath (Join-Path $resolvedProjectPath $pair.manifest)
    if (-not $manifestExists) { continue }
    $lockExists = $false
    foreach ($candidate in @($pair.lock -split "\|")) {
        if (Test-Path -LiteralPath (Join-Path $resolvedProjectPath $candidate)) { $lockExists = $true; break }
    }
    if (-not $lockExists) {
        Add-Finding -Collection $findings -Category "A06/A08" -Title ("Dependency manifest without lock file: " + $pair.manifest) -Severity "MEDIUM" -Evidence $pair.manifest -Recommendation "Add and commit dependency lock files for supply-chain integrity."
    }
}

# A05: default credentials
$defaultCredHits = @(Find-PatternHits -Files $files -Pattern "(?i)(change[-_]?me|admin:admin|root:root|password123|default_password)")
if (@($defaultCredHits).Count -gt 0) {
    Add-Finding -Collection $findings -Category "A05" -Title "Default credentials patterns found" -Severity "HIGH" -Evidence ($defaultCredHits -join ", ") -Recommendation "Replace defaults and rotate credentials."
}

# A10: URL fetch patterns
$urlFetchHits = @(Find-PatternHits -Files $files -Pattern "(?i)\b(requests\.get|requests\.post|fetch\(|http\.get\(|axios\.get|axios\.post)\b")
if (@($urlFetchHits).Count -gt 0) {
    $urlEvidence = (@($urlFetchHits | Select-Object -First 5)) -join ", "
    Add-Finding -Collection $findings -Category "A10" -Title "Outbound URL fetch usage detected (review SSRF controls)" -Severity "LOW" -Evidence $urlEvidence -Recommendation "Validate URLs and block internal network ranges."
}

$criticalCount = @($findings | Where-Object { $_.severity -eq "CRITICAL" }).Count
$highCount = @($findings | Where-Object { $_.severity -eq "HIGH" }).Count
$mediumCount = @($findings | Where-Object { $_.severity -eq "MEDIUM" }).Count
$lowCount = @($findings | Where-Object { $_.severity -eq "LOW" }).Count
$overall = if ($criticalCount -gt 0) { "CRITICAL" } elseif ($highCount -gt 0) { "AT-RISK" } else { "NEEDS-REVIEW" }

$reportPath = Join-Path $analysisDir "security-report.md"
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Security Report (OWASP Automated)")
$lines.Add("")
$lines.Add("- Generated At: $(Get-V2Timestamp)")
$lines.Add("- Project: $resolvedProjectPath")
$lines.Add("- Findings: $($findings.Count)")
$lines.Add("- Overall: $overall")
$lines.Add("")
$lines.Add("| Severity | Count |")
$lines.Add("|----------|-------|")
$lines.Add("| CRITICAL | $criticalCount |")
$lines.Add("| HIGH | $highCount |")
$lines.Add("| MEDIUM | $mediumCount |")
$lines.Add("| LOW | $lowCount |")
$lines.Add("")
$lines.Add("## Findings")
if ($findings.Count -eq 0) {
    $lines.Add("- No findings detected by automated heuristics.")
}
else {
    foreach ($finding in $findings) {
        $lines.Add("")
        $lines.Add("### [$($finding.severity)] $($finding.category) - $($finding.title)")
        $lines.Add("- Evidence: $($finding.evidence)")
        $lines.Add("- Recommendation: $($finding.recommendation)")
    }
}
[System.IO.File]::WriteAllText($reportPath, ($lines -join [Environment]::NewLine))

$statePath = Join-Path $stateDir "security-scan.json"
$state = [ordered]@{
    generated_at = Get-V2Timestamp
    project_path = $resolvedProjectPath
    overall      = $overall
    counts       = [ordered]@{
        critical = [int]$criticalCount
        high     = [int]$highCount
        medium   = [int]$mediumCount
        low      = [int]$lowCount
        total    = [int]$findings.Count
    }
    report_path  = $reportPath
    findings     = @($findings.ToArray())
}
Save-V2JsonContent -Path $statePath -Value $state

if ($criticalCount -gt 0 -or $highCount -gt 0) {
    Append-V2MarkdownLog -Path $alertsPath -Header "# Alerts" -Lines @(
        "## $(Get-V2Timestamp)",
        "- type: security-scan",
        "- overall: $overall",
        "- critical: $criticalCount",
        "- high: $highCount",
        "- report: $reportPath"
    )
}

if ($EmitJson) {
    (Get-V2JsonContent -Path $statePath) | ConvertTo-Json -Depth 8
}
else {
    Write-Output "OWASP scan complete."
    Write-Output "Overall: $overall"
    Write-Output "Report: $reportPath"
}
