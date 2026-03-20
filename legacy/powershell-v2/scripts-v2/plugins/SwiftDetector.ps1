$plugin = [PSCustomObject]@{
    Name = "SwiftDetector"
    Detect = {
        param($Context)

        $signals = New-Object System.Collections.Generic.List[string]
        if ($Context.RelativePaths -match "\.swift$") {
            $signals.Add("*.swift source files")
        }
        if ($Context.RelativePaths -match "(^|/)Package\.swift$") {
            $signals.Add("Package.swift")
        }
        if ($Context.RelativePaths -match "\.xcodeproj/|\.xcworkspace/") {
            $signals.Add("Xcode project")
        }

        return [PSCustomObject]@{
            Detected = ($signals.Count -gt 0)
            Signals  = @($signals)
            Language = "swift"
        }
    }
    BuildHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)Package\.swift$") {
            return "swift build # REVIEW_REQUIRED on Windows"
        }
        return "unknown"
    }
    RunHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)Package\.swift$") {
            return "swift run # REVIEW_REQUIRED on Windows"
        }
        return "unknown"
    }
    TestHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)Package\.swift$") {
            return "swift test # REVIEW_REQUIRED on Windows"
        }
        return "unknown"
    }
    DockerHints = {
        param($Context)
        return [PSCustomObject]@{
            base_image = "swift:6.0"
            port       = 8080
        }
    }
    Confidence = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)Package\.swift$") { return "high" }
        return "medium"
    }
}

$plugin
