# Security Report (OWASP Automated)

- Generated At: 2026-03-12T06:13:45
- Project: G:\Fernando\project0
- Findings: 4
- Overall: AT-RISK

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 2 |
| MEDIUM | 1 |
| LOW | 1 |

## Findings

### [MEDIUM] A05 - Debug mode enabled in code/config
- Evidence: G:\Fernando\project0\workspace\projects\sistema-gestao-psicologos-autonomos\.env:4
- Recommendation: Disable debug in production profiles.

### [HIGH] A03 - Potential SQL concatenation
- Evidence: G:\Fernando\project0\scripts\v2\Invoke-SchedulerV2.ps1:1379
- Recommendation: Use parameterized queries or ORM query builders.

### [HIGH] A03 - Dangerous command execution primitives present
- Evidence: G:\Fernando\project0\scripts\v2\Invoke-EphemeralToolRunner.ps1:184, G:\Fernando\project0\scripts\v2\Invoke-RunPowerShellCommandTool.ps1:199
- Recommendation: Restrict command execution and validate all inputs.

### [LOW] A10 - Outbound URL fetch usage detected (review SSRF controls)
- Evidence: G:\Fernando\project0\scripts\memory_sync.py:942, G:\Fernando\project0\scripts\memory_sync.py:1065, G:\Fernando\project0\scripts\memory_sync.py:1106, G:\Fernando\project0\scripts\query_lessons.py:288
- Recommendation: Validate URLs and block internal network ranges.