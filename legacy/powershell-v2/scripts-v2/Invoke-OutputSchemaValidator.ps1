<#
.SYNOPSIS
    Validates agent completion payload against role output schema.
.DESCRIPTION
    Loads docs/agents/agent-output-schema.json and validates payload JSON.
    Intended to be called by complete mode before a task can be marked done.
.PARAMETER ProjectPath
    Project root containing ai-orchestrator.
.PARAMETER AgentName
    Agent identity to resolve role schema.
.PARAMETER PayloadPath
    JSON payload path (absolute or project-relative).
.PARAMETER EmitJson
    Emits machine-readable JSON output.
#>
param(
    [string]$ProjectPath = ".",
    [string]$AgentName = "",
    [string]$PayloadPath = "",
    [switch]$EmitJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

function Get-V2SchemaRoleConfig {
    param(
        [object]$Schema,
        [string]$RoleName
    )

    $roles = Get-V2OptionalProperty -InputObject $Schema -Name "roles" -DefaultValue ([PSCustomObject]@{})
    $defaultRoleName = [string](Get-V2OptionalProperty -InputObject $Schema -Name "default_role" -DefaultValue "default")
    $normalizedRole = [string]$RoleName
    if ([string]::IsNullOrWhiteSpace($normalizedRole)) {
        $normalizedRole = $defaultRoleName
    }
    $normalizedRole = $normalizedRole.Trim().ToLowerInvariant()

    $roleConfig = Get-V2OptionalProperty -InputObject $roles -Name $normalizedRole -DefaultValue $null
    $defaultConfig = Get-V2OptionalProperty -InputObject $roles -Name $defaultRoleName -DefaultValue ([PSCustomObject]@{})
    if ($null -eq $roleConfig) {
        $roleConfig = $defaultConfig
    }

    $extendsRole = [string](Get-V2OptionalProperty -InputObject $roleConfig -Name "extends" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($extendsRole)) {
        $parent = Get-V2OptionalProperty -InputObject $roles -Name $extendsRole.ToLowerInvariant() -DefaultValue $null
        if ($parent) {
            $merged = [ordered]@{}
            foreach ($name in @($parent.PSObject.Properties.Name)) { $merged[$name] = $parent.$name }
            foreach ($name in @($roleConfig.PSObject.Properties.Name)) { $merged[$name] = $roleConfig.$name }
            $roleConfig = [PSCustomObject]$merged
        }
    }

    return [PSCustomObject]@{
        role_name   = $normalizedRole
        role_config = $roleConfig
    }
}

function Get-V2NormalizedStringArray {
    param(
        [object]$InputValue
    )
    if ($null -eq $InputValue) { return @() }
    if ($InputValue -is [string]) {
        if ([string]::IsNullOrWhiteSpace($InputValue)) { return @() }
        return @($InputValue)
    }
    return @($InputValue | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Normalize-V2LibraryDecisionPayload {
    param(
        [object]$PayloadObject,
        [System.Collections.Generic.List[string]]$Warnings
    )

    if ($null -eq $PayloadObject) { return }

    if (-not ($PayloadObject.PSObject.Properties.Name -contains "local_library_candidates")) {
        Set-V2DynamicProperty -InputObject $PayloadObject -Name "local_library_candidates" -Value @()
        $Warnings.Add("local_library_candidates_defaulted_empty")
    }
    else {
        $normalizedCandidates = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $PayloadObject -Name "local_library_candidates" -DefaultValue @()))
        Set-V2DynamicProperty -InputObject $PayloadObject -Name "local_library_candidates" -Value @($normalizedCandidates)
    }

    $decisionDefault = [PSCustomObject]@{
        selected_option    = "not-applicable"
        justification      = "No local library reuse required for this task."
        selected_libraries = @()
        rejected_libraries = @()
    }

    $hasDecisionField = $PayloadObject.PSObject.Properties.Name -contains "library_decision"
    if (-not $hasDecisionField) {
        Set-V2DynamicProperty -InputObject $PayloadObject -Name "library_decision" -Value $decisionDefault
        $Warnings.Add("library_decision_defaulted_not_applicable")
        return
    }

    $decisionRaw = Get-V2OptionalProperty -InputObject $PayloadObject -Name "library_decision" -DefaultValue $null
    if ($null -eq $decisionRaw) {
        Set-V2DynamicProperty -InputObject $PayloadObject -Name "library_decision" -Value $decisionDefault
        $Warnings.Add("library_decision_null_defaulted_not_applicable")
        return
    }

    if ($decisionRaw -is [string]) {
        $selectedOption = [string]$decisionRaw
        if ([string]::IsNullOrWhiteSpace($selectedOption)) {
            $selectedOption = "not-applicable"
        }

        $coercedDecision = [PSCustomObject]@{
            selected_option    = $selectedOption
            justification      = "Library decision coerced from scalar value."
            selected_libraries = @()
            rejected_libraries = @()
        }

        Set-V2DynamicProperty -InputObject $PayloadObject -Name "library_decision" -Value $coercedDecision
        $Warnings.Add("library_decision_scalar_coerced_object")
        return
    }

    $selectedOptionRaw = [string](Get-V2OptionalProperty -InputObject $decisionRaw -Name "selected_option" -DefaultValue "not-applicable")
    $selectedOption = if ([string]::IsNullOrWhiteSpace($selectedOptionRaw)) { "not-applicable" } else { $selectedOptionRaw.Trim() }
    $justificationRaw = [string](Get-V2OptionalProperty -InputObject $decisionRaw -Name "justification" -DefaultValue "")
    $justification = if ([string]::IsNullOrWhiteSpace($justificationRaw)) { "No local library reuse required for this task." } else { $justificationRaw.Trim() }
    $selectedLibraries = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $decisionRaw -Name "selected_libraries" -DefaultValue @()))
    $rejectedLibraries = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $decisionRaw -Name "rejected_libraries" -DefaultValue @()))

    $normalizedDecision = [PSCustomObject]@{
        selected_option    = $selectedOption
        justification      = $justification
        selected_libraries = @($selectedLibraries)
        rejected_libraries = @($rejectedLibraries)
    }
    Set-V2DynamicProperty -InputObject $PayloadObject -Name "library_decision" -Value $normalizedDecision
}

function Get-V2PayloadPathList {
    param(
        [object]$PayloadObject
    )

    $fields = @("files_written", "source_files", "changes", "artifacts")
    $all = New-Object System.Collections.Generic.List[string]
    foreach ($field in $fields) {
        if (-not ($PayloadObject.PSObject.Properties.Name -contains $field)) { continue }
        $values = Get-V2OptionalProperty -InputObject $PayloadObject -Name $field -DefaultValue @()
        foreach ($value in @(Get-V2NormalizedStringArray -InputValue $values)) {
            $trimmed = [string]$value
            if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
            if (-not $all.Contains($trimmed)) {
                $all.Add($trimmed)
            }
        }
    }
    return @($all.ToArray())
}

function Invoke-V2LaravelFactoryGuard {
    param(
        [string]$ProjectRoot,
        [object]$PayloadObject,
        [System.Collections.Generic.List[string]]$Errors,
        [System.Collections.Generic.List[string]]$Warnings
    )

    $isPhpProject = (Test-Path -LiteralPath (Join-Path $ProjectRoot "composer.json") -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $ProjectRoot "artisan") -PathType Leaf)
    if (-not $isPhpProject) { return }

    $payloadPaths = @(Get-V2PayloadPathList -PayloadObject $PayloadObject)
    if ($payloadPaths.Count -eq 0) { return }

    $factoryRegex = [regex]'(?:\\?[\w\\]+\\)?([A-Z][A-Za-z0-9_]*)::factory\s*\('
    $modelToFiles = @{}
    foreach ($relativePathRaw in $payloadPaths) {
        $relativePath = ([string]$relativePathRaw).Replace("\", "/").Trim()
        if ([string]::IsNullOrWhiteSpace($relativePath)) { continue }
        $isTestFile = $relativePath -match '(^|/)tests?/' -or $relativePath -match '^tests?/'
        if (-not $isTestFile) { continue }
        if ($relativePath -notmatch '\.php$') { continue }

        $absolutePath = if ([System.IO.Path]::IsPathRooted($relativePathRaw)) { [string]$relativePathRaw } else { Join-Path $ProjectRoot $relativePathRaw }
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
            $Warnings.Add("factory_guard_test_file_missing:$relativePath")
            continue
        }

        $content = ""
        try {
            $content = Get-Content -LiteralPath $absolutePath -Raw -ErrorAction Stop
        }
        catch {
            $Warnings.Add("factory_guard_read_failed:$relativePath")
            continue
        }

        $matches = @($factoryRegex.Matches($content))
        if ($matches.Count -eq 0) { continue }
        foreach ($match in $matches) {
            if (-not $match.Success) { continue }
            $modelName = [string]$match.Groups[1].Value
            if ([string]::IsNullOrWhiteSpace($modelName)) { continue }
            if (-not $modelToFiles.ContainsKey($modelName)) {
                $modelToFiles[$modelName] = New-Object System.Collections.Generic.List[string]
            }
            if (-not $modelToFiles[$modelName].Contains($relativePath)) {
                $modelToFiles[$modelName].Add($relativePath)
            }
        }
    }

    foreach ($modelName in @($modelToFiles.Keys)) {
        $expectedFactory = Join-Path $ProjectRoot ("database/factories/{0}Factory.php" -f $modelName)
        if (Test-Path -LiteralPath $expectedFactory -PathType Leaf) {
            try {
                $factoryContent = Get-Content -LiteralPath $expectedFactory -Raw -ErrorAction Stop
                $namespaceMatch = [regex]::Match($factoryContent, '(?im)^\s*namespace\s+([^;]+);')
                if ($namespaceMatch.Success) {
                    $namespaceName = [string]$namespaceMatch.Groups[1].Value
                    $normalizedNamespace = ($namespaceName -replace '/', '\').Trim()
                    if ($normalizedNamespace -ne "Database\Factories") {
                        $Warnings.Add("factory_guard_nonstandard_factory_namespace:$modelName:file=database/factories/${modelName}Factory.php:namespace=$normalizedNamespace")
                    }
                }
                else {
                    $Warnings.Add("factory_guard_missing_factory_namespace:$modelName:file=database/factories/${modelName}Factory.php")
                }

                $classMatch = [regex]::Match($factoryContent, '(?im)^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b')
                $expectedClass = "${modelName}Factory"
                if ($classMatch.Success) {
                    $className = [string]$classMatch.Groups[1].Value
                    if ($className -ne $expectedClass) {
                        $Warnings.Add("factory_guard_nonstandard_factory_class:$modelName:file=database/factories/${modelName}Factory.php:class=$className:expected=$expectedClass")
                    }
                }
                else {
                    $Warnings.Add("factory_guard_missing_factory_class:$modelName:file=database/factories/${modelName}Factory.php")
                }
            }
            catch {
                $Warnings.Add("factory_guard_read_failed:database/factories/${modelName}Factory.php")
            }
            continue
        }

        $factoryDir = Join-Path $ProjectRoot "database/factories"
        $fallbackMatches = @()
        if (Test-Path -LiteralPath $factoryDir -PathType Container) {
            $fallbackMatches = @(Get-ChildItem -LiteralPath $factoryDir -Filter ("*{0}Factory.php" -f $modelName) -File -Recurse -ErrorAction SilentlyContinue)
        }
        if ($fallbackMatches.Count -gt 0) {
            $expectedRelative = ("database/factories/{0}Factory.php" -f $modelName)
            $foundRelative = @(
                $fallbackMatches |
                    ForEach-Object { Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $_.FullName } |
                    Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            )
            $Warnings.Add("factory_guard_nonstandard_factory_location:$modelName:expected=$expectedRelative:found=$(@($foundRelative) -join '|')")

            foreach ($matchFile in @($fallbackMatches)) {
                try {
                    $matchContent = Get-Content -LiteralPath $matchFile.FullName -Raw -ErrorAction Stop
                    $relativeFile = Get-V2RelativeUnixPath -BasePath $ProjectRoot -TargetPath $matchFile.FullName

                    $namespaceMatch = [regex]::Match($matchContent, '(?im)^\s*namespace\s+([^;]+);')
                    if ($namespaceMatch.Success) {
                        $namespaceName = [string]$namespaceMatch.Groups[1].Value
                        $normalizedNamespace = ($namespaceName -replace '/', '\').Trim()
                        if ($normalizedNamespace -ne "Database\Factories") {
                            $Warnings.Add("factory_guard_nonstandard_factory_namespace:$modelName:file=$relativeFile:namespace=$normalizedNamespace")
                        }
                    }

                    $classMatch = [regex]::Match($matchContent, '(?im)^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b')
                    $expectedClass = "${modelName}Factory"
                    if ($classMatch.Success) {
                        $className = [string]$classMatch.Groups[1].Value
                        if ($className -ne $expectedClass) {
                            $Warnings.Add("factory_guard_nonstandard_factory_class:$modelName:file=$relativeFile:class=$className:expected=$expectedClass")
                        }
                    }
                }
                catch {
                    continue
                }
            }
            continue
        }

        $referencedIn = @($modelToFiles[$modelName].ToArray()) -join "|"
        $Errors.Add("factory_guard_missing_factory:$modelName:referenced_in=$referencedIn")
    }
}

function Invoke-V2LibraryDecisionGuard {
    param(
        [object]$PayloadObject,
        [System.Collections.Generic.List[string]]$Errors,
        [System.Collections.Generic.List[string]]$Warnings
    )

    $decisionRequiredRaw = Get-V2OptionalProperty -InputObject $PayloadObject -Name "library_decision_required" -DefaultValue $true
    $decisionRequired = $true
    if ($decisionRequiredRaw -is [bool]) {
        $decisionRequired = [bool]$decisionRequiredRaw
    }
    elseif ($decisionRequiredRaw -is [int]) {
        $decisionRequired = ([int]$decisionRequiredRaw -ne 0)
    }
    else {
        $normalizedDecisionRequired = [string]$decisionRequiredRaw
        if ($normalizedDecisionRequired -match '^(false|0|no|off)$') {
            $decisionRequired = $false
        }
        elseif ($normalizedDecisionRequired -match '^(true|1|yes|on)$') {
            $decisionRequired = $true
        }
    }

    if (-not ($PayloadObject.PSObject.Properties.Name -contains "library_decision")) {
        if ($decisionRequired) {
            $Errors.Add("library_decision_missing")
        }
        else {
            $Warnings.Add("library_decision_skipped_not_required")
        }
        return
    }

    $decision = Get-V2OptionalProperty -InputObject $PayloadObject -Name "library_decision" -DefaultValue $null
    if ($null -eq $decision) {
        if ($decisionRequired) {
            $Errors.Add("library_decision_null")
        }
        else {
            $Warnings.Add("library_decision_null_not_required")
        }
        return
    }
    if ($decision -is [string]) {
        $scalarDecision = [string]$decision
        if ($scalarDecision -eq "not-applicable") {
            $Warnings.Add("library_decision_scalar_coerced_not_applicable")
            return
        }
        if ($decisionRequired) {
            $Warnings.Add("library_decision_scalar_detected")
            $Errors.Add("library_decision_invalid_type")
        }
        else {
            $Warnings.Add("library_decision_scalar_skipped_not_required")
        }
        return
    }

    $allowed = @("use-existing-library", "hybrid", "custom-code-justified", "not-applicable")
    $selectedOption = [string](Get-V2OptionalProperty -InputObject $decision -Name "selected_option" -DefaultValue "")
    $justification = [string](Get-V2OptionalProperty -InputObject $decision -Name "justification" -DefaultValue "")
    $selectedLibraries = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $decision -Name "selected_libraries" -DefaultValue @()))
    $rejectedLibraries = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $decision -Name "rejected_libraries" -DefaultValue @()))

    if ([string]::IsNullOrWhiteSpace($selectedOption)) {
        $Errors.Add("library_decision_missing_selected_option")
    }
    elseif ($allowed -notcontains $selectedOption) {
        $Errors.Add("library_decision_invalid_selected_option:$selectedOption")
    }

    if ([string]::IsNullOrWhiteSpace($justification)) {
        $Errors.Add("library_decision_missing_justification")
    }

    if ($selectedOption -eq "not-applicable") {
        return
    }

    if ($selectedOption -in @("use-existing-library", "hybrid") -and $selectedLibraries.Count -eq 0) {
        $Errors.Add("library_decision_selected_libraries_required_for:$selectedOption")
    }

    if ($selectedOption -eq "custom-code-justified" -and $rejectedLibraries.Count -eq 0) {
        $Warnings.Add("library_decision_rejected_libraries_recommended")
    }

    $hasCandidatesField = $PayloadObject.PSObject.Properties.Name -contains "local_library_candidates"
    if (-not $hasCandidatesField) {
        if ($decisionRequired) {
            $Warnings.Add("local_library_candidates_missing")
        }
    }
    else {
        $candidates = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $PayloadObject -Name "local_library_candidates" -DefaultValue @()))
        if ($candidates.Count -eq 0 -and $selectedOption -ne "not-applicable") {
            $Warnings.Add("local_library_candidates_empty")
        }
    }
}

if ([string]::IsNullOrWhiteSpace($PayloadPath)) {
    throw "PayloadPath is required."
}

$projectRoot = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $projectRoot -or -not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$payloadAbsolutePath = if ([System.IO.Path]::IsPathRooted($PayloadPath)) { $PayloadPath } else { Join-Path $projectRoot $PayloadPath }
if (-not (Test-Path -LiteralPath $payloadAbsolutePath -PathType Leaf)) {
    throw "Payload file not found: $payloadAbsolutePath"
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$schemaPath = Join-Path $repoRoot "docs/agents/agent-output-schema.json"
if (-not (Test-Path -LiteralPath $schemaPath -PathType Leaf)) {
    throw "Schema file not found: $schemaPath"
}

$schema = Get-V2JsonContent -Path $schemaPath
if (-not $schema) {
    throw "Schema file is invalid JSON: $schemaPath"
}

$payload = Get-V2JsonContent -Path $payloadAbsolutePath
if (-not $payload) {
    throw "Payload is invalid JSON: $payloadAbsolutePath"
}

$roleResolved = Get-V2SchemaRoleConfig -Schema $schema -RoleName $AgentName
$roleName = [string](Get-V2OptionalProperty -InputObject $roleResolved -Name "role_name" -DefaultValue "default")
$roleConfig = Get-V2OptionalProperty -InputObject $roleResolved -Name "role_config" -DefaultValue ([PSCustomObject]@{})

$requiredFields = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $roleConfig -Name "required_fields" -DefaultValue @()))
$nonEmptyFields = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $roleConfig -Name "non_empty_fields" -DefaultValue @()))
$arrayFields = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $roleConfig -Name "array_fields" -DefaultValue @()))
$arrayNonEmpty = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $roleConfig -Name "array_non_empty_fields" -DefaultValue @()))
$booleanFields = @(Get-V2NormalizedStringArray -InputValue (Get-V2OptionalProperty -InputObject $roleConfig -Name "boolean_fields" -DefaultValue @()))
$maxItems = Get-V2OptionalProperty -InputObject $roleConfig -Name "max_items" -DefaultValue ([PSCustomObject]@{})

$errors = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

Normalize-V2LibraryDecisionPayload `
    -PayloadObject $payload `
    -Warnings $warnings

foreach ($field in $requiredFields) {
    if (-not ($payload.PSObject.Properties.Name -contains $field)) {
        $errors.Add("missing_required_field:$field")
    }
}

foreach ($field in $nonEmptyFields) {
    if (-not ($payload.PSObject.Properties.Name -contains $field)) { continue }
    $value = [string](Get-V2OptionalProperty -InputObject $payload -Name $field -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($value)) {
        $errors.Add("empty_field:$field")
    }
}

foreach ($field in $arrayFields) {
    if (-not ($payload.PSObject.Properties.Name -contains $field)) { continue }
    $value = Get-V2OptionalProperty -InputObject $payload -Name $field -DefaultValue @()
    if ($value -is [string]) {
        $textValue = [string]$value
        if ([string]::IsNullOrWhiteSpace($textValue)) {
            $value = @()
        }
        else {
            # Runtime payloads may serialize single-value arrays as scalar strings.
            # Coerce and keep a warning instead of hard-failing.
            $value = @($textValue)
            $warnings.Add("array_coerced_from_scalar:$field")
        }
    }
    $arr = @($value)
    if ($arrayNonEmpty -contains $field -and $arr.Count -eq 0) {
        $errors.Add("array_empty:$field")
    }

    $maxForField = [int](Get-V2OptionalProperty -InputObject $maxItems -Name $field -DefaultValue 0)
    if ($maxForField -gt 0 -and $arr.Count -gt $maxForField) {
        $warnings.Add("array_truncated_recommended:$field:max=$maxForField:actual=$($arr.Count)")
    }
}

foreach ($field in $booleanFields) {
    if (-not ($payload.PSObject.Properties.Name -contains $field)) { continue }
    $value = Get-V2OptionalProperty -InputObject $payload -Name $field -DefaultValue $null
    if ($null -eq $value) {
        $errors.Add("boolean_required_non_null:$field")
        continue
    }
    if ($value -isnot [bool]) {
        $errors.Add("boolean_expected:$field")
    }
}

Invoke-V2LaravelFactoryGuard `
    -ProjectRoot $projectRoot `
    -PayloadObject $payload `
    -Errors $errors `
    -Warnings $warnings

Invoke-V2LibraryDecisionGuard `
    -PayloadObject $payload `
    -Errors $errors `
    -Warnings $warnings

$result = [PSCustomObject]@{
    success   = $errors.Count -eq 0
    role      = $roleName
    schema    = [string](Get-V2OptionalProperty -InputObject $schema -Name "schema_version" -DefaultValue "v1")
    payload   = Get-V2RelativeUnixPath -BasePath $projectRoot -TargetPath $payloadAbsolutePath
    errors    = @($errors.ToArray())
    warnings  = @($warnings.ToArray())
}

if ($EmitJson) {
    Write-Output ($result | ConvertTo-Json -Depth 10)
}
else {
    Write-Output ("Output schema validation: success={0} role={1} errors={2}" -f $result.success, $result.role, $errors.Count)
}

if ($errors.Count -gt 0 -and -not $EmitJson) {
    throw ("output-schema-invalid:{0}" -f ($errors -join ","))
}
