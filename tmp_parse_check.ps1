$files = Get-ChildItem 'g:\Fernando\project0\scripts\v2' -Recurse -Filter '*.ps1'
$errors_found = @()
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    foreach ($e in $parseErrors) {
        $errors_found += [PSCustomObject]@{
            file = $file.Name
            line = $e.Extent.StartLineNumber
            message = $e.Message
        }
    }
}
$errors_found | Format-Table -AutoSize
Write-Output "Total errors: $($errors_found.Count)"
