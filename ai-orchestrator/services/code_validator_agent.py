from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("orch.code_validator")

_TRUSTED_REVIEW_SOURCES = {"human_review", "peer_review", "ci_test_suite"}
_SAFE_VALIDATION_PREFIXES = (
    "pytest",
    "python -m pytest",
    f"{Path(sys.executable).name} -m pytest",
    "php artisan test",
    "npm test",
    "pnpm test",
    "yarn test",
)


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    detail: str = ""
    command: str | None = None


@dataclass
class ValidationDecision:
    verified: bool
    verification_source: str
    validation_passed: bool
    reason: str
    checks: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodeValidatorAgent:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or Path(__file__).resolve().parents[1]).resolve()

    async def validate_for_memory(self, state: dict[str, Any]) -> ValidationDecision:
        state = self._enrich_validation_state(dict(state))
        source = str(state.get("verification_source") or "").strip().lower()
        trusted_review = bool(state.get("human_verified") or state.get("verified_by_agent"))

        if source in {"human_review", "peer_review"} and trusted_review:
            check = ValidationCheck(
                name="trusted_review",
                passed=True,
                detail=f"verified by {source}",
            )
            return ValidationDecision(
                verified=True,
                verification_source=source,
                validation_passed=True,
                reason="trusted_review",
                checks=[asdict(check)],
            )

        ci_validation = state.get("ci_validation") or {}
        if source == "ci_test_suite" and bool(ci_validation.get("passed")):
            check = ValidationCheck(
                name="ci_suite",
                passed=True,
                detail=str(ci_validation.get("provider") or "ci_test_suite"),
                command=str(ci_validation.get("command") or ""),
            )
            return ValidationDecision(
                verified=True,
                verification_source="ci_test_suite",
                validation_passed=True,
                reason="ci_suite_passed",
                checks=[asdict(check)],
            )

        if source != "code_validator_agent":
            return ValidationDecision(
                verified=False,
                verification_source=source or "unverified",
                validation_passed=False,
                reason="missing_trusted_verifier",
                checks=[],
            )

        explicit_report = state.get("validator_report") or {}
        if explicit_report.get("passed") is True and explicit_report.get("checks"):
            return ValidationDecision(
                verified=True,
                verification_source="code_validator_agent",
                validation_passed=True,
                reason="validator_report",
                checks=list(explicit_report.get("checks") or []),
            )

        checks = await self._run_local_checks(state)
        validation_passed = all(check.passed for check in checks) if checks else False
        has_execution_proof = any(
            check.passed and check.name in {"test_command", "ci_command"} for check in checks
        )
        return ValidationDecision(
            verified=validation_passed and has_execution_proof,
            verification_source="code_validator_agent",
            validation_passed=validation_passed,
            reason="local_validation" if checks else "no_validation_artifacts",
            checks=[asdict(check) for check in checks],
        )

    def _enrich_validation_state(self, state: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(state)
        ci_validation = self._collect_ci_validation(enriched)
        if ci_validation:
            enriched["ci_validation"] = ci_validation
            source = str(enriched.get("verification_source") or "").strip().lower()
            if source in {"", "unverified"}:
                enriched["verification_source"] = "ci_test_suite"
        return enriched

    def _collect_ci_validation(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("ci_validation") or {}
        if existing:
            merged = dict(existing)
            merged.setdefault("provider", self._detect_ci_provider())
            if not merged.get("command"):
                commands = merged.get("commands") or []
                if isinstance(commands, list) and commands:
                    merged["command"] = str(commands[0])
            if "passed" in merged:
                return merged

        report_paths = self._collect_report_paths(state)
        for report_path in report_paths:
            parsed = self._parse_ci_report(report_path)
            if parsed:
                parsed.setdefault("provider", self._detect_ci_provider())
                command = str((state.get("ci_validation") or {}).get("command") or "").strip()
                if command:
                    parsed.setdefault("command", command)
                return parsed
        return {}

    def _collect_report_paths(self, state: dict[str, Any]) -> list[Path]:
        raw_paths: list[str] = []
        validation_artifacts = state.get("validation_artifacts") or {}
        ci_validation = state.get("ci_validation") or {}
        for bucket in (
            validation_artifacts.get("report_paths"),
            ci_validation.get("report_paths"),
        ):
            if isinstance(bucket, list):
                raw_paths.extend(str(item).strip() for item in bucket if str(item).strip())
        for single in (
            validation_artifacts.get("report_path"),
            ci_validation.get("report_path"),
        ):
            if single:
                raw_paths.append(str(single).strip())

        defaults = (
            "junit.xml",
            "pytest.xml",
            "reports/junit.xml",
            "reports/pytest.xml",
            "test-results/junit.xml",
            "test-results/pytest.xml",
        )
        raw_paths.extend(defaults)

        paths: list[Path] = []
        seen: set[str] = set()
        for rel_path in raw_paths:
            candidate = (self.workspace / rel_path).resolve()
            try:
                candidate.relative_to(self.workspace)
            except ValueError:
                continue
            if not candidate.exists():
                continue
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            paths.append(candidate)
        return paths

    def _parse_ci_report(self, report_path: Path) -> dict[str, Any]:
        if report_path.suffix.lower() == ".json":
            try:
                import json

                payload = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and "passed" in payload:
                    payload.setdefault("report_path", str(report_path))
                    return payload
            except Exception:
                return {}
        try:
            root = ET.fromstring(report_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

        tests = int(root.attrib.get("tests", "0") or 0)
        failures = int(root.attrib.get("failures", "0") or 0)
        errors = int(root.attrib.get("errors", "0") or 0)
        if root.tag == "testsuites":
            tests = failures = errors = 0
            for suite in root.findall("testsuite"):
                tests += int(suite.attrib.get("tests", "0") or 0)
                failures += int(suite.attrib.get("failures", "0") or 0)
                errors += int(suite.attrib.get("errors", "0") or 0)

        if tests <= 0:
            return {}

        return {
            "passed": failures == 0 and errors == 0,
            "provider": self._detect_ci_provider(),
            "suite": root.attrib.get("name", report_path.stem),
            "tests": tests,
            "failures": failures,
            "errors": errors,
            "report_path": str(report_path),
        }

    def _detect_ci_provider(self) -> str:
        if os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("GITHUB_ACTIONS") == "1":
            return "github-actions"
        if os.getenv("GITLAB_CI") == "true" or os.getenv("GITLAB_CI") == "1":
            return "gitlab-ci"
        if os.getenv("BUILDKITE") == "true" or os.getenv("BUILDKITE") == "1":
            return "buildkite"
        if os.getenv("CI") == "true" or os.getenv("CI") == "1":
            return "generic-ci"
        return "local-ci"

    async def _run_local_checks(self, state: dict[str, Any]) -> list[ValidationCheck]:
        files = self._collect_files(state)
        commands = self._collect_validation_commands(state)
        checks: list[ValidationCheck] = []

        python_files = [path for path in files if path.suffix == ".py"]
        for file_path in python_files:
            checks.append(await self._run_py_compile(file_path))

        for command in commands:
            checks.append(await self._run_safe_command(command))

        return checks

    def _collect_files(self, state: dict[str, Any]) -> list[Path]:
        task = state.get("task") or {}
        raw_files: list[str] = []
        for bucket in (
            state.get("files_modified"),
            state.get("files"),
            task.get("files_modified"),
            task.get("files"),
            task.get("files_affected"),
        ):
            if isinstance(bucket, list):
                raw_files.extend(str(item).strip() for item in bucket if str(item).strip())
        primary_file = str(task.get("primary_file") or "").strip()
        if primary_file:
            raw_files.append(primary_file)

        files: list[Path] = []
        seen: set[str] = set()
        for rel_path in raw_files:
            candidate = (self.workspace / rel_path).resolve()
            try:
                candidate.relative_to(self.workspace)
            except ValueError:
                continue
            if not candidate.exists():
                continue
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            files.append(candidate)
        return files

    def _collect_validation_commands(self, state: dict[str, Any]) -> list[str]:
        commands: list[str] = []
        for bucket in (
            state.get("validation_commands"),
            (state.get("validation_artifacts") or {}).get("commands"),
            (state.get("ci_validation") or {}).get("commands"),
        ):
            if isinstance(bucket, list):
                commands.extend(str(item).strip() for item in bucket if str(item).strip())
        command = str((state.get("ci_validation") or {}).get("command") or "").strip()
        if command:
            commands.append(command)
        deduped: list[str] = []
        seen: set[str] = set()
        for command in commands:
            if command in seen:
                continue
            seen.add(command)
            deduped.append(command)
        return deduped

    async def _run_py_compile(self, file_path: Path) -> ValidationCheck:
        def _run() -> ValidationCheck:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(file_path)],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                detail = (result.stderr or result.stdout or "").strip()
                return ValidationCheck(
                    name="py_compile",
                    passed=result.returncode == 0,
                    detail=detail[:500],
                    command=f"{sys.executable} -m py_compile {file_path}",
                )
            except Exception as exc:
                return ValidationCheck(
                    name="py_compile",
                    passed=False,
                    detail=str(exc),
                    command=f"{sys.executable} -m py_compile {file_path}",
                )

        return await asyncio.to_thread(_run)

    async def _run_safe_command(self, command: str) -> ValidationCheck:
        normalized = command.strip()
        allowed = any(normalized.lower().startswith(prefix.lower()) for prefix in _SAFE_VALIDATION_PREFIXES)
        if not allowed:
            return ValidationCheck(
                name="test_command",
                passed=False,
                detail="command_not_allowed",
                command=normalized,
            )

        def _run() -> ValidationCheck:
            try:
                result = subprocess.run(
                    normalized,
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    shell=True,
                    check=False,
                )
                detail = (result.stderr or result.stdout or "").strip()
                return ValidationCheck(
                    name="test_command",
                    passed=result.returncode == 0,
                    detail=detail[:1000],
                    command=normalized,
                )
            except Exception as exc:
                return ValidationCheck(
                    name="test_command",
                    passed=False,
                    detail=str(exc),
                    command=normalized,
                )

        return await asyncio.to_thread(_run)


_INSTANCE: CodeValidatorAgent | None = None


def get_code_validator_agent() -> CodeValidatorAgent:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CodeValidatorAgent()
    return _INSTANCE
