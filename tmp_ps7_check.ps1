$files = Get-ChildItem '/orchestrator-root/scripts/v2' -Recurse -Filter '*.ps1'
$errors_found = @()
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    foreach ($e in $parseErrors) {
        $errors_found += [PSCustomObject]@{
            file = $file.Name
            line = $e.Extent.StartLineNumber
            msg = $e.Message
        }
    }
}
$errors_found | ConvertTo-Json -Depth 3
Write-Output "Total: $($errors_found.Count)"
