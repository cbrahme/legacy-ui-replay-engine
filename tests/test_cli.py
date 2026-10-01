"""
Unit and integration tests for Typer CLI in src/cli.py.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.agent.discovery import DiscoveryResult
from src.cli import app
from src.models.artifact import CapabilityArtifact, Step
from src.models.result import ExecutionResult, ExecutionStatus, StepLog

runner = CliRunner()


def test_cli_help():
    """Verify top-level CLI help lists all commands."""
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "serve-target" in res.output
    assert "replay" in res.output
    assert "discover" in res.output
    assert "test-harness" in res.output


def test_cli_replay_missing_artifact():
    """Verify replay command exits with code 1 if artifact does not exist."""
    res = runner.invoke(app, ["replay", "--artifact", "non_existent.json"])
    assert res.exit_code == 1
    assert "Artifact file not found" in res.output


def test_cli_replay_invalid_params(tmp_path: Path):
    """Verify replay command exits with code 1 if params string is not valid JSON."""
    dummy_art = tmp_path / "art.json"
    dummy_art.write_text("{}", encoding="utf-8")

    res = runner.invoke(app, ["replay", "--artifact", str(dummy_art), "--params", "not-a-json"])
    assert res.exit_code == 1


@patch("src.cli.ReplayExecutor.execute")
def test_cli_replay_success(mock_execute: MagicMock, tmp_path: Path):
    """Verify successful capability replay renders result tables and exits 0."""
    artifact_path = Path("capabilities/member_balance_lookup.json")
    if not artifact_path.exists():
        pytest.skip("capabilities/member_balance_lookup.json not found")

    mock_result = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        capability_id="member_balance_lookup",
        version="1.0.0",
        outcome_code="MEMBER_BALANCE_EXTRACTED",
        execution_time_ms=1150.5,
        steps_executed=4,
        data={"savings_balance": "$12,450.00"},
        step_logs=[
            StepLog(
                step_id=1,
                action="navigate",
                status="OK",
                target_resolved=None,
                duration_ms=400.0,
            ),
            StepLog(
                step_id=2,
                action="fill",
                status="OK",
                target_resolved="role:textbox[name='Member Search ID']",
                duration_ms=250.0,
            ),
            StepLog(
                step_id=3,
                action="click",
                status="OK",
                target_resolved="role:button[name='Search Records']",
                duration_ms=200.0,
            ),
            StepLog(
                step_id=4,
                action="extract",
                status="OK",
                target_resolved="#savings-balance-val",
                duration_ms=300.5,
            ),
        ],
    )
    mock_execute.return_value = mock_result

    res = runner.invoke(
        app,
        [
            "replay",
            "--artifact",
            str(artifact_path),
            "--params",
            '{"member_id": "12345"}',
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ],
    )
    assert res.exit_code == 0
    assert "SUCCESS" in res.output
    assert "savings_balance" in res.output
    assert "$12,450.00" in res.output
    assert mock_execute.call_args[1]["inputs"] == {"member_id": "12345"}


@patch("src.cli.ReplayExecutor.execute")
def test_cli_replay_hard_failure(mock_execute: MagicMock, tmp_path: Path):
    """Verify replay command exits with code 1 on HARD_FAILURE."""
    artifact_path = Path("capabilities/member_balance_lookup.json")
    if not artifact_path.exists():
        pytest.skip("capabilities/member_balance_lookup.json not found")

    mock_result = ExecutionResult(
        status=ExecutionStatus.HARD_FAILURE,
        capability_id="member_balance_lookup",
        version="1.0.0",
        outcome_code="LOCATOR_EXHAUSTED",
        execution_time_ms=5000.0,
        steps_executed=2,
    )
    mock_execute.return_value = mock_result

    res = runner.invoke(
        app,
        [
            "replay",
            "--artifact",
            str(artifact_path),
            "--evidence-dir",
            str(tmp_path / "evidence"),
        ],
    )
    assert res.exit_code == 1
    assert "HARD_FAILURE" in res.output


@patch("src.cli.DiscoveryAgent.discover")
def test_cli_discover_command(mock_discover: MagicMock, tmp_path: Path):
    """Verify discover command creates output artifact file upon successful discovery."""
    from src.models.artifact import Checkpoint, SuccessCondition

    mock_cap = CapabilityArtifact(
        id="test_member_search",
        name="Search for member 12345",
        description="Search for member 12345 in members portal",
        version="1.0.0",
        target_path="/members",
        steps=[
            Step(step_id=1, action="navigate", value="http://127.0.0.1:8000/members"),
            Step(step_id=2, action="fill", value="12345"),
        ],
        checkpoint=Checkpoint(
            success_condition=SuccessCondition(target="#accounts-table")
        ),
    )
    mock_discover.return_value = DiscoveryResult(
        success=True,
        capability=mock_cap,
        goal="Search for member 12345",
        steps_executed=2,
    )

    out_file = tmp_path / "out_capability.json"
    res = runner.invoke(
        app,
        [
            "discover",
            "--goal",
            "Search for member 12345",
            "--url",
            "http://127.0.0.1:8000/members",
            "--output",
            str(out_file),
            "--id",
            "test_member_search",
        ],
    )
    assert res.exit_code == 0
    assert "Discovery Succeeded!" in res.output
    assert out_file.exists()

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["id"] == "test_member_search"
    assert len(data["steps"]) == 2


def test_cli_test_harness_help():
    """Verify test-harness options."""
    res = runner.invoke(app, ["test-harness", "--help"])
    assert res.exit_code == 0
    assert "--coverage" in res.output


@patch("src.cli.uvicorn.run")
def test_cli_serve_target(mock_uvicorn: MagicMock):
    """Verify serve-target invokes uvicorn with configured host and port."""
    res = runner.invoke(app, ["serve-target", "--port", "8888"])
    assert res.exit_code == 0
    assert "Starting Core Banking Target Portal" in res.output
    mock_uvicorn.assert_called_once_with(
        "src.target_app.app:app", host="127.0.0.1", port=8888, reload=False
    )


def test_cli_replay_invalid_artifact_json(tmp_path: Path):
    """Verify replay command exits 1 when artifact JSON is corrupted."""
    bad_file = tmp_path / "corrupt.json"
    bad_file.write_text("{not-valid-json", encoding="utf-8")
    res = runner.invoke(app, ["replay", "--artifact", str(bad_file)])
    assert res.exit_code == 1
    assert "Failed to parse artifact JSON" in res.output


@patch("src.cli.DiscoveryAgent.discover")
def test_cli_discover_failure(mock_discover: MagicMock):
    """Verify discover command exits 1 when discovery fails."""
    mock_discover.return_value = DiscoveryResult(
        success=False,
        error_message="Page took too long to load",
    )
    res = runner.invoke(app, ["discover", "--goal", "Transfer funds", "--url", "http://127.0.0.1:8000/transfers"])
    assert res.exit_code == 1
    assert "Discovery failed" in res.output


@patch("subprocess.run")
def test_cli_test_harness_run(mock_subproc: MagicMock):
    """Verify test-harness invokes pytest subprocess."""
    mock_subproc.return_value = MagicMock(returncode=0)
    res = runner.invoke(app, ["test-harness", "--no-coverage"])
    assert res.exit_code == 0
    assert "Running automated test harness" in res.output
    mock_subproc.assert_called_once()

