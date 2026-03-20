$plugin = [PSCustomObject]@{
    Name = "KotlinDetector"
    Detect = {
        param($Context)

        $signals = New-Object System.Collections.Generic.List[string]
        if ($Context.RelativePaths -match "(^|/)build\.gradle\.kts$") {
            $signals.Add("build.gradle.kts")
        }
        if ($Context.RelativePaths -match "(^|/)settings\.gradle\.kts$") {
            $signals.Add("settings.gradle.kts")
        }
        if ($Context.RelativePaths -match "\.kt$") {
            $signals.Add("*.kt source files")
        }

        return [PSCustomObject]@{
            Detected = ($signals.Count -gt 0)
            Signals  = @($signals)
            Language = "kotlin"
        }
    }
    BuildHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)gradlew(\.bat)?$") {
            return "gradlew.bat build"
        }

        return "unknown"
    }
    RunHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)gradlew(\.bat)?$") {
            return "gradlew.bat bootRun # REVIEW_REQUIRED"
        }

        return "unknown"
    }
    TestHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)gradlew(\.bat)?$") {
            return "gradlew.bat test"
        }

        return "unknown"
    }
    DockerHints = {
        param($Context)
        return [PSCustomObject]@{
            base_image = "eclipse-temurin:21-jdk"
            port       = 8080
        }
    }
    Confidence = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)build\.gradle\.kts$") {
            return "high"
        }

        return "medium"
    }
}

$plugin
