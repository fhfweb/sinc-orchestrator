<#
.SYNOPSIS
    Semantic Security Engine - Checks for tenant isolation compliance in Laravel Projects.
.DESCRIPTION
    Scans Controllers and Models for missing multi-tenancy guards.
.PARAMETER ProjectPath
    Path to the project root.
.PARAMETER EmitJson
    Output findings as JSON.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
$findings = New-Object System.Collections.Generic.List[object]

$reqMiddleware = "EnsurePatientIsAuthenticated"
$reqTrait = "BelongsToTenant"
$sensitiveMethods = @("dashboard", "show", "edit", "update", "destroy", "verifyOtp")

# 1. Scan Controllers
$controllersPath = Join-Path $resolvedProjectPath "app/Http/Controllers"
if (Test-Path -LiteralPath $controllersPath) {
    $controllers = Get-ChildItem -LiteralPath $controllersPath -Filter "*.php" -Recurse
    foreach ($controller in $controllers) {
        $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $controller.FullName
        $content = Get-Content -LiteralPath $controller.FullName -Raw
        
        # Exclude Admin and Representative controllers from unscoped lookup check
        if ($relPath -match "app/Http/Controllers/Admin/" -or $relPath -match "app/Http/Controllers/Representative/") {
            continue
        }
        
        foreach ($method in $sensitiveMethods) {
            if ($content -match ('function\s+' + $method + '\s*\(')) {
                if ($content -match '[A-Z][A-Za-z0-9]*::find(?:OrFail)?\(' -and -not ($content -match "->where\(['\`"]tenant_id['\`"]\)")) {
                   $findings.Add([PSCustomObject]@{
                       file = $relPath
                       category = "isolation-leak"
                       severity = "HIGH"
                       title = ("Potentially unscoped lookup in " + $method)
                       evidence = "Usage of findOrFail/find without tenant_id filter detected."
                       recommendation = "Wrap lookup in ->where('tenant_id', ...) or use a tenant-aware repository."
                   })
                }
            }
        }
    }
}

# 2. Scan Models
$modelsPath = Join-Path $resolvedProjectPath "app/Models"
if (Test-Path -LiteralPath $modelsPath) {
    $models = Get-ChildItem -LiteralPath $modelsPath -Filter "*.php" -Recurse
    foreach ($model in $models) {
        $content = Get-Content -LiteralPath $model.FullName -Raw
        $relPath = Get-V2RelativeUnixPath -BasePath $resolvedProjectPath -TargetPath $model.FullName
        
        $globalModels = @(
            "User.php", "Tenant.php", "Plan.php", "Product.php", "Module.php", 
            "Representative.php", "RepresentativePayout.php", "SaaSModule.php", 
            "SaaSProduct.php", "Coupon.php", "SystemSetting.php", "ActivityLog.php"
        )
        
        if (-not ($content -match ("use\s+.*" + $reqTrait))) {
             if ($model.Name -notin $globalModels) {
                $findings.Add([PSCustomObject]@{
                    file = $relPath
                    category = "policy-violation"
                    severity = "MEDIUM"
                    title = "Model missing tenant global scope"
                    evidence = ("Trait " + $reqTrait + " not found.")
                    recommendation = "Add the BelongsToTenant trait to ensure automatic data isolation."
                })
             }
        }
    }
}

$report = [PSCustomObject]@{
    generated_at = Get-V2Timestamp
    project_path = $resolvedProjectPath
    finding_count = $findings.Count
    findings = @($findings.ToArray())
}

if ($EmitJson) {
    $report | ConvertTo-Json -Depth 10
} else {
    Write-Host "--- Tenant Isolation Policy Report ---" -ForegroundColor Cyan
    Write-Host ("Generated: " + $report.generated_at)
    Write-Host ("Findings: " + $report.finding_count)
    Write-Host ""
    foreach ($f in $report.findings) {
        $color = if ($f.severity -eq "HIGH") { "Red" } else { "Yellow" }
        Write-Host ("[" + $f.severity + "] " + $f.title) -ForegroundColor $color
        Write-Host ("  File: " + $f.file)
        Write-Host ("  Evidence: " + $f.evidence)
        Write-Host ("  Rec: " + $f.recommendation)
        Write-Host ""
    }
}
