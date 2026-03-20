<#
.SYNOPSIS
    Mutation testing: detects coverage gaps by injecting code mutations and checking test reactions.
.DESCRIPTION
    Implements a lightweight mutation testing loop:
      1. Copies project source to a temp directory (isolated clone)
      2. Applies simple operator mutations to Python/JS/TS/PHP source files:
         - Arithmetic: +  -, *  /
         - Comparison: ==  !=, >  <, >=  <=
         - Boolean: True  False, and  or (Python); true  false, &&  || (JS)
         - Return: return True  return False
      3. Runs the project's test suite against the mutated clone
      4. If tests PASS on mutated code  dead zone found (test doesn't catch the bug)
      5. Creates COBERTURA-FALHA-* tasks in task-dag.json for each dead zone
      6. Writes ai-orchestrator/reports/mutation-<timestamp>.json with results
    Runs during idle loop cycles (no pending tasks). Safe to run  never modifies source.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator/ and source code.
.PARAMETER MaxMutations
    Maximum mutations to attempt per run. Default: 20 (keeps runtime short).
.PARAMETER TimeoutSeconds
    Timeout per mutation test run. Default: 120.
.PARAMETER Stack
    Override language stack detection (python/node/php/go).
.EXAMPLE
    .\scripts\v2\Invoke-MutationTestingV2.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Invoke-MutationTestingV2.ps1 -ProjectPath C:\projects\myapp -MaxMutations 10 -Stack python
#>
param(
    [string]$ProjectPath      = ".",
    [int]$MaxMutations        = 20,
    [int]$TimeoutSeconds      = 120,
    [string]$Stack            = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedPath     = Resolve-V2AbsolutePath -Path $ProjectPath
if ([string]::IsNullOrWhiteSpace($resolvedPath) -or -not (Test-Path -LiteralPath $resolvedPath -PathType Container)) {
    throw "Project path does not exist or could not be resolved: $ProjectPath"
}

$orchestratorRoot = Join-Path $resolvedPath "ai-orchestrator"
$statePath        = Join-Path $orchestratorRoot "state/project-state.json"
$dagPath          = Join-Path $orchestratorRoot "tasks/task-dag.json"
$reportsDir       = Join-Path $orchestratorRoot "reports"
$ts               = Get-Date -Format "yyyyMMddHHmmss"
$reportPath       = Join-Path $reportsDir "mutation-$ts.json"

Initialize-V2Directory -Path $reportsDir

#  Read stack and test command from project state 
$state = Get-V2JsonContent -Path $statePath
$detectedStack = $Stack
if ([string]::IsNullOrWhiteSpace($detectedStack) -and $state) {
    $detectedStack = [string](Get-V2OptionalProperty -InputObject $state -Name "stack" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($detectedStack) -or $detectedStack -eq "unknown") {
        $tf = Get-V2OptionalProperty -InputObject $state -Name "technical_fingerprint" -DefaultValue ([PSCustomObject]@{})
        $detectedStack = [string](Get-V2OptionalProperty -InputObject $tf -Name "primary_language" -DefaultValue "python")
    }
}
if ([string]::IsNullOrWhiteSpace($detectedStack)) { $detectedStack = "python" }

$testCommand = ""
if ($state) {
    $verifiedCmds = Get-V2OptionalProperty -InputObject $state -Name "verified_commands" -DefaultValue ([PSCustomObject]@{})
    $testCommand = Get-V2VerifiedCommand -VerifiedCommands $verifiedCmds -CommandName "test"
}
if ([string]::IsNullOrWhiteSpace($testCommand)) {
    $testCommand = switch ($detectedStack) {
        "python" { "pytest --tb=no -q" }
        "node"   { "npm test" }
        "php"    { "php artisan test" }
        "go"     { "go test ./..." }
        default  { "" }
    }
}

if ([string]::IsNullOrWhiteSpace($testCommand)) {
    Write-Host "[MutationTest] No test command found. Skipping."
    exit 0
}

#  Mutation operators per stack 
$mutationRules = switch ($detectedStack) {
    "python" {
        @(
            @{ Pattern = " \+ ";  Replacement = " - ";  Description = "arithmetic: +  -"   },
            @{ Pattern = " \* ";  Replacement = " / ";  Description = "arithmetic: *  /"   },
            @{ Pattern = " == ";  Replacement = " != "; Description = "compare: ==  !="    },
            @{ Pattern = " > ";   Replacement = " < ";  Description = "compare: >  <"      },
            @{ Pattern = " >= ";  Replacement = " <= "; Description = "compare: >=  <="    },
            @{ Pattern = "\bTrue\b"; Replacement = "False"; Description = "bool: True  False" },
            @{ Pattern = "\band\b";  Replacement = "or";    Description = "bool: and  or"     },
            @{ Pattern = "return True";  Replacement = "return False"; Description = "return: True  False" }
        )
    }
    "node" {
        @(
            @{ Pattern = " \+ ";   Replacement = " - ";   Description = "arithmetic: +  -"     },
            @{ Pattern = " \* ";   Replacement = " / ";   Description = "arithmetic: *  /"     },
            @{ Pattern = " === ";  Replacement = " !== "; Description = "compare: ===  !=="    },
            @{ Pattern = " == ";   Replacement = " != ";  Description = "compare: ==  !="      },
            @{ Pattern = " > ";    Replacement = " < ";   Description = "compare: >  <"        },
            @{ Pattern = "\btrue\b";   Replacement = "false"; Description = "bool: true  false"    },
            @{ Pattern = " && ";   Replacement = " || ";  Description = "bool: &&  ||"         }
        )
    }
    "php" {
        @(
            @{ Pattern = " \+ ";   Replacement = " - ";   Description = "arithmetic: +  -"  },
            @{ Pattern = " == ";   Replacement = " != ";  Description = "compare: ==  !="   },
            @{ Pattern = " === ";  Replacement = " !== "; Description = "compare: ===  !==" },
            @{ Pattern = " > ";    Replacement = " < ";   Description = "compare: >  <"     },
            @{ Pattern = "\btrue\b";   Replacement = "false"; Description = "bool: true  false" },
            @{ Pattern = " && ";   Replacement = " || ";  Description = "bool: &&  ||"      }
        )
    }
    default {
        @(
            @{ Pattern = " \+ ";  Replacement = " - "; Description = "arithmetic: +  -" },
            @{ Pattern = " == ";  Replacement = " != "; Description = "compare: ==  !=" },
            @{ Pattern = " > ";   Replacement = " < "; Description = "compare: >  <"   }
        )
    }
}

#  Collect source files 
$extMap = @{
    python = @("*.py"); node = @("*.ts", "*.js"); php = @("*.php")
    go = @("*.go"); dotnet = @("*.cs"); java = @("*.java"); ruby = @("*.rb"); rust = @("*.rs")
}
$exts = if ($extMap.ContainsKey($detectedStack)) { $extMap[$detectedStack] } else { @("*.py") }
$excludeDirs = "\\(node_modules|vendor|\.git|__pycache__|dist|build|\.venv|venv|tests|test|spec|storage|bootstrap\\cache|ai-orchestrator)\\"

$sourceFiles = New-Object System.Collections.Generic.List[System.IO.FileInfo]
foreach ($ext in $exts) {
    foreach ($file in @(Get-ChildItem -LiteralPath $resolvedPath -Recurse -Filter $ext -File -ErrorAction SilentlyContinue)) {
        if ($file.FullName -match $excludeDirs) { continue }
        $relativePath = $file.FullName.Substring($resolvedPath.Length).TrimStart('\', '/')
        $normalizedRelativePath = ($relativePath -replace "\\", "/").ToLowerInvariant()

        # PHP guardrails: exclude rendered/template view artifacts from mutation candidates.
        if ($detectedStack -eq "php") {
            if ($normalizedRelativePath -like "resources/views/*") { continue }
            if ($normalizedRelativePath -like "storage/framework/views/*") { continue }
            if ($normalizedRelativePath -like "*.blade.php") { continue }
            if ($normalizedRelativePath -like "public/index.php") { continue }
            if ($normalizedRelativePath -like "config/*") { continue }
        }

        $sourceFiles.Add($file)
    }
}

if ($sourceFiles.Count -eq 0) {
    Write-Host "[MutationTest] No source files found for stack '$detectedStack'. Skipping."
    exit 0
}

Write-Host ("[MutationTest] Stack: {0} | Source files: {1} | Max mutations: {2}" -f $detectedStack, $sourceFiles.Count, $MaxMutations)
Write-Host "[MutationTest] Test command: $testCommand"

#  Create isolated temp clone 
$tempBase = Join-Path $env:TEMP "mutation_test_$ts"
Write-Host "[MutationTest] Cloning to: $tempBase"

try {
    Copy-Item -LiteralPath $resolvedPath -Destination $tempBase -Recurse -Force -ErrorAction Stop
}
catch {
    Write-Warning "[MutationTest] Could not clone project: $($_.Exception.Message)"
    exit 0
}

#  Helper: run test suite in temp clone 
function Invoke-MutationTestRun {
    param([string]$ClonePath, [string]$Command, [int]$Timeout)
    try {
        $proc = Start-Process -FilePath "cmd.exe" `
            -ArgumentList ("/c `"cd /d `"$ClonePath`" && $Command`"") `
            -WorkingDirectory $ClonePath `
            -RedirectStandardOutput "$env:TEMP\mut_stdout_$ts.txt" `
            -RedirectStandardError  "$env:TEMP\mut_stderr_$ts.txt" `
            -NoNewWindow -PassThru -Wait -ErrorAction Stop

        if (-not $proc.WaitForExit($Timeout * 1000)) {
            $proc.Kill()
            return @{ exit_code = -99; timed_out = $true }
        }
        return @{ exit_code = $proc.ExitCode; timed_out = $false }
    }
    catch {
        return @{ exit_code = -1; timed_out = $false; error = $_.Exception.Message }
    }
    finally {
        Remove-Item "$env:TEMP\mut_stdout_$ts.txt" -ErrorAction SilentlyContinue
        Remove-Item "$env:TEMP\mut_stderr_$ts.txt" -ErrorAction SilentlyContinue
    }
}

#  Verify baseline (tests must pass on unmodified clone) 
Write-Host "[MutationTest] Running baseline tests..."
$baseline = Invoke-MutationTestRun -ClonePath $tempBase -Command $testCommand -Timeout $TimeoutSeconds
if ($baseline.exit_code -ne 0) {
    Write-Host "[MutationTest] Baseline tests FAILED (exit $($baseline.exit_code)). Mutation testing skipped  fix tests first."
    Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
}
Write-Host "[MutationTest] Baseline: PASSED. Starting mutations..."

#  Apply mutations and test 
$survivedMutations = New-Object System.Collections.Generic.List[object]
$killedCount       = 0
$skippedCount      = 0
$mutationCount     = 0

$shuffledFiles = @($sourceFiles.ToArray() | Get-Random -Count ([Math]::Min($sourceFiles.Count, 50)))

:mutationLoop foreach ($file in $shuffledFiles) {
    if ($mutationCount -ge $MaxMutations) { break }

    $relativePath = $file.FullName.Substring($resolvedPath.Length).TrimStart('\', '/')
    $cloneFilePath = Join-Path $tempBase $relativePath

    if (-not (Test-Path -LiteralPath $cloneFilePath -PathType Leaf)) { continue }

    $originalContent = Get-Content -LiteralPath $cloneFilePath -Raw -ErrorAction SilentlyContinue
    if ([string]::IsNullOrWhiteSpace($originalContent)) { continue }

    foreach ($rule in $mutationRules) {
        if ($mutationCount -ge $MaxMutations) { break mutationLoop }

        # Find a line that matches the pattern
        $lines = $originalContent -split "(`r`n|`n|`r)"
        $matchedLine = -1
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match $rule.Pattern) {
                # Skip comments
                $trimmed = $lines[$i].TrimStart()
                if ($trimmed.StartsWith("#") -or $trimmed.StartsWith("//") -or $trimmed.StartsWith("*")) { continue }
                $matchedLine = $i
                break
            }
        }
        if ($matchedLine -lt 0) { continue }

        $mutationCount++
        $mutatedLines = [string[]]$lines.Clone()
        $mutatedLines[$matchedLine] = [regex]::Replace($mutatedLines[$matchedLine], $rule.Pattern, $rule.Replacement, 1)
        $mutatedContent = $mutatedLines -join [Environment]::NewLine

        # Write mutated file to clone
        [System.IO.File]::WriteAllText($cloneFilePath, $mutatedContent, [System.Text.Encoding]::UTF8)

        # Run tests
        $result = Invoke-MutationTestRun -ClonePath $tempBase -Command $testCommand -Timeout $TimeoutSeconds

        if ($result.timed_out) {
            $skippedCount++
        }
        elseif ($result.exit_code -eq 0) {
            # Tests PASSED on mutated code  dead zone
            $originalLineText = [string]$lines[$matchedLine]
            $mutatedLineText = [string]$mutatedLines[$matchedLine]
            if ($originalLineText.Length -gt 280) { $originalLineText = $originalLineText.Substring(0, 280) + "…" }
            if ($mutatedLineText.Length -gt 280) { $mutatedLineText = $mutatedLineText.Substring(0, 280) + "…" }
            $survivedMutations.Add([PSCustomObject]@{
                file        = $relativePath
                line        = $matchedLine + 1
                mutation    = $rule.Description
                original_line = $originalLineText.Trim()
                mutated_line  = $mutatedLineText.Trim()
            })
            Write-Host ("[MutationTest] SURVIVED: {0}:{1}  {2}" -f $relativePath, ($matchedLine + 1), $rule.Description)
        }
        else {
            $killedCount++
        }

        # Restore original file before next mutation
        [System.IO.File]::WriteAllText($cloneFilePath, $originalContent, [System.Text.Encoding]::UTF8)
    }
}

#  Cleanup 
Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue

$mutationScore = if ($mutationCount -gt 0) {
    [Math]::Round(($killedCount / [Math]::Max($mutationCount, 1)) * 100, 1)
} else { 100.0 }

Write-Host ("[MutationTest] Done. Mutations: {0} | Killed: {1} | Survived: {2} | Score: {3}%" -f $mutationCount, $killedCount, $survivedMutations.Count, $mutationScore)

#  Write report 
$report = [PSCustomObject]@{
    generated_at      = Get-V2Timestamp
    project           = Split-Path -Leaf $resolvedPath
    stack             = $detectedStack
    test_command      = $testCommand
    mutations_run     = $mutationCount
    mutations_killed  = $killedCount
    mutations_survived = $survivedMutations.Count
    mutations_skipped = $skippedCount
    mutation_score_pct = $mutationScore
    survived_details  = @($survivedMutations.ToArray())
}
Save-V2JsonContent -Path $reportPath -Value $report

#  Create COBERTURA-FALHA tasks for survived mutations 
if ($survivedMutations.Count -gt 0 -and (Test-Path -LiteralPath $dagPath -PathType Leaf)) {
    try {
        $dag = Get-V2JsonContent -Path $dagPath
        $createdCount = 0
        $reopenedCount = 0

        foreach ($mut in @($survivedMutations.ToArray())) {
            $fingerprintSource = ("{0}|{1}|{2}" -f [string]$mut.file, [string]$mut.line, [string]$mut.mutation).ToLowerInvariant()
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($fingerprintSource))
            }
            finally {
                $sha.Dispose()
            }
            $fingerprint = [System.BitConverter]::ToString($hash).Replace("-", "").Substring(0, 12).ToLowerInvariant()
            $mutId = "COBERTURA-FALHA-$fingerprint"

            # Deduplicate by semantic fingerprint (file+line+mutation), not by timestamp.
            $existingTask = @($dag.tasks | Where-Object {
                    $existingFingerprint = [string](Get-V2OptionalProperty -InputObject $_ -Name "coverage_fingerprint" -DefaultValue "")
                    $existingId = [string](Get-V2OptionalProperty -InputObject $_ -Name "id" -DefaultValue "")
                    $existingReason = [string](Get-V2OptionalProperty -InputObject $_ -Name "reason" -DefaultValue "")
                    ($existingFingerprint -eq $fingerprint) -or ($existingId -eq $mutId) -or ($existingReason -eq "mutation-survived:$fingerprint")
                } | Select-Object -First 1)

            if ($existingTask) {
                $existingStatus = [string](Get-V2OptionalProperty -InputObject $existingTask -Name "status" -DefaultValue "")
                if ($existingStatus -in @("done", "completed", "skipped")) {
                    Set-V2DynamicProperty -InputObject $existingTask -Name "status" -Value "pending"
                    Set-V2DynamicProperty -InputObject $existingTask -Name "updated_at" -Value (Get-V2Timestamp)
                    Set-V2DynamicProperty -InputObject $existingTask -Name "completion_note" -Value "reopened: mutation survived again"
                    $reopenedCount++
                }
                continue
            }

            if ($createdCount -ge 25) {
                # Guardrail: cap new coverage tasks per run to keep DAG bounded.
                break
            }

            $dag.tasks += [PSCustomObject]@{
                id             = $mutId
                title          = "Add test coverage: $($mut.mutation) in $($mut.file):$($mut.line)"
                description    = "Mutation '$($mut.mutation)' survived at line $($mut.line) of $($mut.file). Tests did not catch this logic error. Add a test that covers this branch."
                reason         = "mutation-survived:$fingerprint"
                priority       = "P2"
                dependencies   = @()
                preferred_agent = "AI QA"
                assigned_agent  = ""
                status         = "pending"
                execution_mode = "external-agent"
                files_affected = @($mut.file)
                source_report  = $reportPath
                coverage_fingerprint = $fingerprint
                original_line  = $mut.original_line
                mutated_line   = $mut.mutated_line
                created_at     = Get-V2Timestamp
                updated_at     = Get-V2Timestamp
            }
            $createdCount++
        }
        Save-V2JsonContent -Path $dagPath -Value $dag
        Write-Host ("[MutationTest] Coverage tasks created={0} reopened={1}" -f $createdCount, $reopenedCount)
    }
    catch {
        Write-Warning "[MutationTest] Could not update task-dag: $($_.Exception.Message)"
    }
}

Write-Output ($report | ConvertTo-Json -Depth 5)
