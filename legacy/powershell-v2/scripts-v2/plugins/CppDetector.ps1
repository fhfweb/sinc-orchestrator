$plugin = [PSCustomObject]@{
    Name = "CppDetector"
    Detect = {
        param($Context)

        $signals = New-Object System.Collections.Generic.List[string]
        if ($Context.RelativePaths -match "\.(cpp|cc|cxx|hpp|h)$") {
            $signals.Add("C/C++ source files")
        }
        if ($Context.RelativePaths -match "(^|/)CMakeLists\.txt$") {
            $signals.Add("CMakeLists.txt")
        }
        if ($Context.RelativePaths -match "(^|/)meson\.build$") {
            $signals.Add("meson.build")
        }

        return [PSCustomObject]@{
            Detected = ($signals.Count -gt 0)
            Signals  = @($signals)
            Language = "cpp"
        }
    }
    BuildHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)CMakeLists\.txt$") {
            return "cmake -S . -B build && cmake --build build # REVIEW_REQUIRED"
        }
        return "unknown"
    }
    RunHints = {
        param($Context)
        return "unknown"
    }
    TestHints = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)CTestTestfile\.cmake$|(^|/)tests?/") {
            return "ctest --test-dir build # REVIEW_REQUIRED"
        }
        return "unknown"
    }
    DockerHints = {
        param($Context)
        return [PSCustomObject]@{
            base_image = "ubuntu:24.04"
            port       = 0
        }
    }
    Confidence = {
        param($Context)
        if ($Context.RelativePaths -match "(^|/)CMakeLists\.txt$") { return "high" }
        return "medium"
    }
}

$plugin
