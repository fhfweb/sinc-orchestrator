$commonPath = Join-Path $PSScriptRoot "scripts\v2\Common.ps1"
. $commonPath

$testPaths = @("/workspace", "/workspace/projects/SINC", "G:\Fernando\project0")
foreach ($p in $testPaths) {
    $resolved = Resolve-V2AbsolutePath -Path $p
    Write-Host "In: $p | Out: $resolved"
}
