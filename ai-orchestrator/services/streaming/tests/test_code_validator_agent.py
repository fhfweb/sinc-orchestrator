from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from services.code_validator_agent import CodeValidatorAgent


@pytest.mark.asyncio
async def test_code_validator_agent_accepts_trusted_ci_suite():
    agent = CodeValidatorAgent(workspace=Path("ai-orchestrator").resolve())

    decision = await agent.validate_for_memory(
        {
            "verification_source": "ci_test_suite",
            "ci_validation": {"passed": True, "provider": "github-actions", "command": "pytest -q"},
        }
    )

    assert decision.verified is True
    assert decision.verification_source == "ci_test_suite"
    assert decision.reason == "ci_suite_passed"


@pytest.mark.asyncio
async def test_code_validator_agent_requires_execution_proof_for_local_validation(monkeypatch):
    agent = CodeValidatorAgent(workspace=Path("ai-orchestrator").resolve())
    sample_file = Path("ai-orchestrator/services/code_validator_agent.py").resolve()

    async def _empty_checks(_state):
        return []

    monkeypatch.setattr(agent, "_run_local_checks", _empty_checks)

    decision = await agent.validate_for_memory(
        {
            "verification_source": "code_validator_agent",
            "files_modified": [str(sample_file.relative_to(agent.workspace))],
            "validation_passed": True,
        }
    )

    assert decision.verified is False
    assert decision.reason == "no_validation_artifacts"


@pytest.mark.asyncio
async def test_code_validator_agent_promotes_junit_report_from_ci(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = workspace / "reports" / "junit.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        textwrap.dedent(
            """\
            <testsuite name="pytest" tests="4" failures="0" errors="0">
              <testcase classname="suite" name="test_ok" />
            </testsuite>
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    agent = CodeValidatorAgent(workspace=workspace)
    decision = await agent.validate_for_memory(
        {
            "validation_artifacts": {
                "report_path": "reports/junit.xml",
            },
            "ci_validation": {
                "command": "pytest -q",
            },
        }
    )

    assert decision.verified is True
    assert decision.verification_source == "ci_test_suite"
    assert decision.reason == "ci_suite_passed"
