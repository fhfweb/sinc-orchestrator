param(
    [string]$ProjectPath = "g:\Fernando\project0\workspace\projects\SINC"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$gitHooksDir = Join-Path $ProjectPath ".git/hooks"
if (-not (Test-Path -LiteralPath $gitHooksDir)) {
    throw "Git hooks directory not found at $gitHooksDir. Is this a git repository?"
}

$hookPath = Join-Path $gitHooksDir "pre-commit"
$enforcerPath = Join-Path $PSScriptRoot "Invoke-PolicyEnforcer.ps1"

# Create the bash-compatible hook content
$hookContent = @"
#!/bin/sh
# AI Orchestrator Governance Hook
# Installed by Install-GovernanceHook.ps1

echo "--- Running AI Orchestrator Governance Check ---"

# Invoke the Policy Enforcer via PowerShell
# We use -EmitJson to get the compliance score
# ProjectPath is "." because we are running from the root of the project
RESULT=`$(powershell.exe -ExecutionPolicy Bypass -File "$enforcerPath" -ProjectPath . -EmitJson -SkipRepair)

# Extract compliance score using a simple grep/sed or if PowerShell output is JSON
SCORE=`$(echo "`$RESULT" | grep -oP '"policy_compliance_score":\s*\K[0-9]+')

echo "Compliance Score: `$SCORE%"

if [ "`$SCORE" -lt 90 ]; then
    echo "ERROR: Commit blocked due to low compliance score (< 90%)."
    echo "Please run 'powershell -File $enforcerPath -ProjectPath .' to see detailed violations."
    exit 1
fi

echo "Governance check passed!"
exit 0
"@

[System.IO.File]::WriteAllText($hookPath, $hookContent)

Write-Host "Governance pre-commit hook installed at $hookPath" -ForegroundColor Green
