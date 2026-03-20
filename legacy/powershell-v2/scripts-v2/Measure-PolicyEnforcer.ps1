param(
    [string]$ProjectPath = "g:\Fernando\project0\workspace\projects\SINC",
    [int]$Iterations = 5
)

$enforcerPath = Join-Path $PSScriptRoot "Invoke-PolicyEnforcer.ps1"
$totalTime = 0

Write-Host "Benchmarking Policy Enforcer ($Iterations iterations)..." -ForegroundColor Cyan

for ($i = 1; $i -le $Iterations; $i++) {
    Write-Host "Iteration $i... " -NoNewline
    $start = Get-Date
    try {
        # Run with SkipRepair to only measure analysis time
        & $enforcerPath -ProjectPath $ProjectPath -SkipRepair > $null
    } catch {
        Write-Host "Failed: $($_.Exception.Message)" -ForegroundColor Red
        continue
    }
    $end = Get-Date
    $duration = ($end - $start).TotalSeconds
    $totalTime += $duration
    Write-Host ("{0:N2}s" -f $duration)
}

$average = $totalTime / $Iterations
Write-Host ("`nAverage Execution Time: {0:N2}s" -f $average) -ForegroundColor Green
