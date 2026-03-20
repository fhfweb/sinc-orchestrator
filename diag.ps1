$errors = @()
$tokens = @()
[System.Management.Automation.Language.Parser]::ParseFile('g:\Fernando\project0\scripts\v2\Invoke-SchedulerV2.ps1', [ref]$tokens, [ref]$errors)
foreach ($err in $errors) {
    "Error: $($err.Message) at line $($err.Extent.StartLineNumber), column $($err.Extent.StartColumnNumber)"
}
