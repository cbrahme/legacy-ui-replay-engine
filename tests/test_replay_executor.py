import json
import os
from pathlib import Path
import pytest
import pytest_asyncio
import uvicorn
from playwright.async_api import async_playwright

from src.engine.executor import ReplayExecutor
from src.models.artifact import (
    ActionType,
    CapabilityArtifact,
    Coordinate,
    LocatorStrategy,
    Step,
    SuccessCondition,
)
from src.models.result import ExecutionStatus
from src.target_app.app import app


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def target_server_url():
    """Runs target mock server in a background thread/process or returns test host."""
    import socket
    import threading

    # Find free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to become responsive
    import httpx
    import time
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            r = httpx.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    return base_url


@pytest.mark.asyncio
async def test_replay_happy_path(target_server_url, tmp_path):
    """Verifies end-to-end replay for an active member (12345)."""
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    artifact = CapabilityArtifact.model_validate(artifact_data)
    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact, inputs={"member_id": "12345"})

    assert result.status == ExecutionStatus.SUCCESS
    assert result.outcome_code == "SUCCESS"
    assert result.steps_executed == 4
    assert result.data is not None
    assert "$4,250.75" in result.data["savings_balance"]
    assert len(result.step_logs) == 4
    assert all(log.status == "OK" for log in result.step_logs)


@pytest.mark.asyncio
async def test_replay_business_outcome_member_not_found(target_server_url, tmp_path):
    """Verifies that business outcomes short-circuit without locator failure errors."""
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    artifact = CapabilityArtifact.model_validate(artifact_data)
    # Give step 4 a fast timeout if it were to run, but our observation loop races it
    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact, inputs={"member_id": "99999"})

    assert result.status == ExecutionStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert "not exist" in result.outcome_message or "not found" in result.outcome_message


@pytest.mark.asyncio
async def test_replay_business_outcome_fraud_hold(target_server_url, tmp_path):
    """Verifies business outcome detection for fraud hold account 67890."""
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    artifact = CapabilityArtifact.model_validate(artifact_data)
    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact, inputs={"member_id": "67890"})

    assert result.status == ExecutionStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "FRAUD_HOLD"


@pytest.mark.asyncio
async def test_replay_irreversible_action_gating(target_server_url, tmp_path):
    """Verifies that an irreversible action is halted safely when allow_irreversible is False."""
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    # Mark the click action as irreversible
    artifact_data["steps"][2]["is_irreversible"] = True
    artifact = CapabilityArtifact.model_validate(artifact_data)

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        allow_irreversible=False,
    )

    result = await executor.execute(artifact, inputs={"member_id": "12345"})

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "IRREVERSIBLE_ACTION_BLOCKED"
    assert result.evidence_path is not None
    assert Path(result.evidence_path).exists()
    assert result.debug_context is not None
    assert Path(result.debug_context["html_snapshot"]).exists()


@pytest.mark.asyncio
async def test_replay_locator_failure_rich_diagnostics(target_server_url, tmp_path):
    """Verifies that unresolvable locators cleanly trigger rich failure artifact capture."""
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    # Set non-existent target
    artifact_data["steps"][1]["target"] = {
        "primary": "role:button[name='NonExistentButtonXYZ']",
        "fallbacks": ["#definitely-not-there", "//div[@id='phantom']"],
    }
    artifact_data["steps"][1]["timeout_ms"] = 1500
    artifact = CapabilityArtifact.model_validate(artifact_data)

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact, inputs={"member_id": "12345"})

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "STEP_EXECUTION_FAILED"
    assert result.evidence_path is not None
    assert Path(result.evidence_path).exists()
    assert result.debug_context["html_snapshot"] is not None
    assert Path(result.debug_context["html_snapshot"]).exists()


@pytest.mark.asyncio
async def test_replay_wait_and_coordinate_actions(target_server_url, tmp_path):
    """Verifies wait and coordinate actions dispatch successfully."""
    artifact = CapabilityArtifact(
        id="coord_wait_test",
        name="Coord and Wait Test",
        description="Tests wait and click coordinate actions",
        target_path="/",
        steps=[
            Step(step_id=1, action=ActionType.NAVIGATE, value="{{base_url}}/"),
            Step(step_id=2, action=ActionType.WAIT, value="200"),
            Step(step_id=3, action=ActionType.CLICK_COORDINATE, coordinate=Coordinate(x=50, y=50)),
        ],
        checkpoint={
            "success_condition": {
                "type": "element_visible",
                "target": "body",
            }
        },
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact)
    assert result.status == ExecutionStatus.SUCCESS
    assert result.steps_executed == 3


@pytest.mark.asyncio
async def test_replay_base_url_override_url_checkpoint(target_server_url, tmp_path):
    """Verifies that base_url passed in inputs overrides default and resolves in url_contains checkpoint."""
    artifact = CapabilityArtifact(
        id="url_checkpoint_test",
        name="URL Checkpoint Test",
        description="Tests base_url parameter expansion in url_contains checkpoint",
        target_path="/members",
        steps=[
            Step(step_id=1, action=ActionType.NAVIGATE, value="{{base_url}}/members"),
        ],
        checkpoint={
            "success_condition": {
                "type": "url_contains",
                "target": "{{base_url}}/members",
                "timeout_ms": 2000,
            }
        },
    )

    # Instantiate executor with a dummy default base_url
    executor = ReplayExecutor(
        base_url="http://127.0.0.1:9999",
        headless=True,
        evidence_dir=str(tmp_path),
    )

    # Pass actual live target_server_url as override
    result = await executor.execute(artifact, inputs={"base_url": target_server_url})
    assert result.status == ExecutionStatus.SUCCESS
    assert result.outcome_code == "SUCCESS"



@pytest.mark.asyncio
async def test_replay_timeout_on_success_condition(target_server_url, tmp_path):
    """Verifies that a timeout occurs when the success condition is not met."""
    artifact = CapabilityArtifact(
        id="timeout_test",
        name="Timeout Test",
        description="Tests timeout on success condition",
        target_path="/",
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                value="{{base_url}}/"
            ),
        ],
        checkpoint={
            "success_condition": {
                "type": "element_visible",
                "target": "text:NonExistentElement",
                "timeout_ms": 1000,
            }
        },
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact)

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "CHECKPOINT_FAILED"
    assert "not satisfied" in result.outcome_message.lower()
    assert result.debug_context is not None
    assert "html_snapshot" in result.debug_context


@pytest.mark.asyncio
async def test_replay_invalid_success_condition_target(target_server_url, tmp_path):
    """Verifies that an invalid success condition target raises an appropriate failure."""
    artifact = CapabilityArtifact(
        id="invalid_success_condition_test",
        name="Invalid Success Condition Test",
        description="Tests failure when success condition target is invalid",
        target_path="/",
        steps=[
            Step(step_id=1, action=ActionType.NAVIGATE, value="{{base_url}}/"),
        ],
        checkpoint={
            "success_condition": {
                "type": "element_visible",
                "target": "invalid_locator:malformed_target",
                "timeout_ms": 1000,
            }
        },
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
    )

    result = await executor.execute(artifact)

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "CHECKPOINT_FAILED"
    assert "not satisfied" in result.outcome_message.lower()
