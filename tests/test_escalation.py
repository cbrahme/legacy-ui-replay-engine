import asyncio
import json
from pathlib import Path
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from unittest import mock
from src.engine.executor import ReplayExecutor
from src.guardrails.policy import GuardrailPolicy, IrreversiblePolicy
from src.guardrails.redactor import PIIRedactor
from src.human.escalation import EscalationManager
from src.models.artifact import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    Checkpoint,
    LocatorStrategy,
    Step,
    SuccessCondition,
)
from src.models.human import (
    InterventionRecord,
    InterventionRequest,
    OperatorAction,
    OperatorActionType,
)
from src.models.result import ExecutionStatus


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_escalation_manager_direct_intervention(tmp_path):
    """Verifies EscalationManager lifecycle, screenshot generation, and action recording."""
    evidence_dir = tmp_path / "evidence"
    manager = EscalationManager(evidence_dir=str(evidence_dir), interactive=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <html>
                <body>
                    <h2>Escalation Test Page</h2>
                    <input id="test-input" name="secret_code" type="text" value="" />
                    <button id="test-btn" class="btn primary">Confirm Override</button>
                </body>
            </html>
            """
        )

        async def mock_operator_handler(req: InterventionRequest, pg):
            # Human types into input and clicks button
            await pg.fill("#test-input", "SSN: 123-45-6789 confidential")
            # Trigger blur/change event
            await pg.evaluate("document.getElementById('test-input').dispatchEvent(new Event('change'))")
            await pg.click("#test-btn")
            # Wait for event loop to flush exposed binding
            await asyncio.sleep(0.1)
            return "RESUMED"

        manager.operator_handler = mock_operator_handler

        req = InterventionRequest(
            request_id="test_req_001",
            capability_id="test_cap",
            step_id=2,
            reason="Security Lockout Alert",
            current_url=page.url,
        )

        record = await manager.request_intervention(page, req)

        assert record.request_id == "test_req_001"
        assert record.trigger_step_id == 2
        assert record.resolution == "RESUMED"
        assert record.duration_seconds >= 0.0
        assert Path(record.screenshot_before).exists()
        assert Path(record.screenshot_after).exists()

        # Check recorded operator actions
        action_types = [a.action_type for a in record.operator_actions]
        assert OperatorActionType.CLICK in action_types
        # Find click action and verify element descriptor
        click_action = next(a for a in record.operator_actions if a.action_type == OperatorActionType.CLICK)
        assert "test-btn" in (click_action.target or "")

        # Verify PII redaction on input action
        input_actions = [a for a in record.operator_actions if a.action_type == OperatorActionType.INPUT]
        if input_actions:
            for act in input_actions:
                if act.value:
                    assert "123-45-6789" not in act.value
                    assert "[REDACTED_SSN]" in act.value

        await browser.close()


@pytest.mark.asyncio
async def test_escalation_manager_abort_resolution(tmp_path):
    """Verifies that an ABORTED decision by the operator produces a proper record."""
    evidence_dir = tmp_path / "evidence"
    manager = EscalationManager(
        evidence_dir=str(evidence_dir),
        operator_handler=lambda req, pg: asyncio.sleep(0.01, result="ABORTED"),
        interactive=False,
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("about:blank")

        req = InterventionRequest(
            request_id="test_abort_002",
            capability_id="member_lookup",
            step_id=1,
            reason="Unrecoverable page error",
        )

        record = await manager.request_intervention(page, req)
        assert record.resolution == "ABORTED"
        assert Path(record.screenshot_before).exists()
        assert Path(record.screenshot_after).exists()
        await browser.close()


@pytest.mark.asyncio
async def test_escalation_manager_cli_prompt(tmp_path, monkeypatch):
    """Verifies CLI interactive prompt parsing for Resume, Override, and Abort."""
    evidence_dir = tmp_path / "evidence"
    manager = EscalationManager(evidence_dir=str(evidence_dir), interactive=True)

    req = InterventionRequest(
        request_id="cli_test_001",
        capability_id="member_lookup",
        step_id=1,
        reason="Manual check required",
    )

    # Test "R" -> RESUMED
    monkeypatch.setattr("builtins.input", lambda prompt: "R")
    res = await manager._prompt_operator_cli(req)
    assert res == "RESUMED"

    # Test "O" -> OVERRIDDEN
    monkeypatch.setattr("builtins.input", lambda prompt: "o")
    res = await manager._prompt_operator_cli(req)
    assert res == "OVERRIDDEN"

    # Test "A" -> ABORTED
    monkeypatch.setattr("builtins.input", lambda prompt: "A")
    res = await manager._prompt_operator_cli(req)
    assert res == "ABORTED"

    # Test empty input defaults to "RESUMED"
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    res = await manager._prompt_operator_cli(req)
    assert res == "RESUMED"


@pytest.mark.asyncio
async def test_escalation_on_locked_account_e2e(target_server_url, tmp_path):
    """
    End-to-End integration test:
    Executing member_balance_lookup on locked account 67890 triggers human escalation.
    The operator clicks 'Authorize Supervisor Override' in the live session,
    which unlocks the account and redirects to the unlocked summary page.
    The engine re-observes state, verifies success, extracts data, and returns SUCCESS.
    """
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    artifact = CapabilityArtifact.model_validate(artifact_data)

    async def supervisor_operator_handler(req: InterventionRequest, page):
        # Human operator reviews lockout banner and authorizes override
        override_btn = page.locator("#supervisor-override-btn")
        assert await override_btn.is_visible()
        # Click override button
        await override_btn.click()
        # Wait for page to navigate to unlocked profile
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(0.1)
        return "RESUMED"

    escalation_mgr = EscalationManager(
        evidence_dir=str(tmp_path),
        operator_handler=supervisor_operator_handler,
        interactive=False,
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        escalation_manager=escalation_mgr,
        escalation_codes={"FRAUD_HOLD", "ACCOUNT_LOCKED"},
    )

    result = await executor.execute(artifact, inputs={"member_id": "67890"})

    assert result.status == ExecutionStatus.SUCCESS
    assert result.outcome_code == "SUCCESS"
    assert len(result.interventions) == 1
    intervention = result.interventions[0]
    assert intervention.resolution == "RESUMED"
    assert "fraud" in intervention.reason.lower() or "locked" in intervention.reason.lower()
    assert Path(intervention.screenshot_before).exists()
    assert Path(intervention.screenshot_after).exists()

    # Verify operator actions captured the click on supervisor-override-btn
    action_targets = [a.target for a in intervention.operator_actions if a.target]
    assert any("supervisor-override-btn" in t for t in action_targets)


@pytest.mark.asyncio
async def test_escalation_locked_account_aborted_by_operator(target_server_url, tmp_path):
    """
    Verifies that if an operator aborts the intervention for member 67890,
    the replay engine cleanly halts with HARD_FAILURE and outcome_code OPERATOR_ABORTED.
    """
    with open("capabilities/member_balance_lookup.json", "r") as f:
        artifact_data = json.load(f)

    artifact = CapabilityArtifact.model_validate(artifact_data)

    async def abort_operator_handler(req: InterventionRequest, page):
        await asyncio.sleep(0.05)
        return "ABORTED"

    escalation_mgr = EscalationManager(
        evidence_dir=str(tmp_path),
        operator_handler=abort_operator_handler,
        interactive=False,
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        escalation_manager=escalation_mgr,
        escalation_codes={"FRAUD_HOLD", "ACCOUNT_LOCKED"},
    )

    result = await executor.execute(artifact, inputs={"member_id": "67890"})

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "OPERATOR_ABORTED"
    assert len(result.interventions) == 1
    assert result.interventions[0].resolution == "ABORTED"


@pytest.mark.asyncio
async def test_escalation_irreversible_step_routed_to_human(target_server_url, tmp_path):
    """
    Verifies that an irreversible action step triggers human escalation when
    irreversible_policy is ROUTE_TO_HUMAN, and resumes execution upon operator approval.
    """
    artifact = CapabilityArtifact(
        id="irreversible_transfer_flow",
        name="Irreversible Transfer Flow",
        version="1.0.0",
        description="Tests human authorization seam for irreversible mutating actions",
        target_path="/transfers",
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                value="{{base_url}}/transfers",
            ),
            Step(
                step_id=2,
                action=ActionType.FILL,
                target=LocatorStrategy(primary="input[name='to_account']"),
                value="EXT-9871",
            ),
            Step(
                step_id=3,
                action=ActionType.FILL,
                target=LocatorStrategy(primary="input[name='amount']"),
                value="150.00",
            ),
            Step(
                step_id=4,
                action=ActionType.CLICK,
                target=LocatorStrategy(primary="#review-transfer-btn"),
                is_irreversible=True,  # Gated step requiring human clearance
            ),
        ],
        checkpoint=Checkpoint(
            success_condition=SuccessCondition(
                type="element_visible",
                target="#confirm-transfer-btn",
            )
        ),
    )

    policy = GuardrailPolicy(
        irreversible_policy=IrreversiblePolicy.ROUTE_TO_HUMAN,
    )

    operator_approved = False

    async def approve_operator_handler(req: InterventionRequest, page):
        nonlocal operator_approved
        operator_approved = True
        assert req.step_id == 4
        await asyncio.sleep(0.05)
        return "RESUMED"

    escalation_mgr = EscalationManager(
        evidence_dir=str(tmp_path),
        operator_handler=approve_operator_handler,
        interactive=False,
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        policy=policy,
        escalation_manager=escalation_mgr,
    )

    result = await executor.execute(artifact)

    assert operator_approved is True
    assert result.status == ExecutionStatus.SUCCESS
    assert len(result.interventions) == 1
    assert result.interventions[0].resolution == "RESUMED"
    assert result.interventions[0].trigger_step_id == 4


@pytest.mark.asyncio
async def test_escalation_on_step_failure_with_operator_override(target_server_url, tmp_path):
    """
    Verifies that when a step fails due to a bad locator, the escalation manager pauses,
    allows an operator to override the step, and execution proceeds cleanly.
    """
    artifact = CapabilityArtifact(
        id="broken_locator_flow",
        name="Broken Locator Flow",
        version="1.0.0",
        description="Tests operator override on step failure",
        target_path="/members",
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                value="{{base_url}}/members",
            ),
            Step(
                step_id=2,
                action=ActionType.CLICK,
                target=LocatorStrategy(
                    primary="#non-existent-button-xyz",
                    fallbacks=["button.does-not-exist"],
                ),
                timeout_ms=500,
            ),
        ],
        checkpoint=Checkpoint(
            success_condition=SuccessCondition(
                type="element_visible",
                target="h2",
            )
        ),
    )

    async def override_handler(req: InterventionRequest, page):
        assert req.step_id == 2
        return "OVERRIDDEN"

    escalation_mgr = EscalationManager(
        evidence_dir=str(tmp_path),
        operator_handler=override_handler,
        interactive=False,
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        escalation_manager=escalation_mgr,
        escalate_on_failure=True,
    )

    result = await executor.execute(artifact)

    assert result.status == ExecutionStatus.SUCCESS
    assert len(result.interventions) == 1
    assert result.interventions[0].resolution == "OVERRIDDEN"
    assert result.interventions[0].trigger_step_id == 2


@pytest.mark.asyncio
async def test_escalation_on_step_failure_with_retry_resume(target_server_url, tmp_path):
    """
    Verifies that when a step fails, the operator can fix the page state
    and return RESUMED, causing the engine to retry the failed step successfully.
    """
    artifact = CapabilityArtifact(
        id="retry_resume_flow",
        name="Retry Resume Flow",
        version="1.0.0",
        description="Tests operator manual page preparation followed by retry",
        target_path="/",
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                value="{{base_url}}/",
            ),
            Step(
                step_id=2,
                action=ActionType.FILL,
                target=LocatorStrategy(
                    primary="input[name='member_id']",
                ),
                value="12345",
                timeout_ms=500,
            ),
        ],
        checkpoint=Checkpoint(
            success_condition=SuccessCondition(
                type="url_contains",
                target="/members",
            )
        ),
    )

    async def navigate_and_resume_handler(req: InterventionRequest, page):
        # Step 2 failed because we were on /dashboard (root redirected), where input[name='member_id'] is absent.
        # Human operator navigates to /members
        await page.goto(f"{target_server_url}/members")
        await page.wait_for_load_state("networkidle")
        return "RESUMED"

    escalation_mgr = EscalationManager(
        evidence_dir=str(tmp_path),
        operator_handler=navigate_and_resume_handler,
        interactive=False,
    )

    executor = ReplayExecutor(
        base_url=target_server_url,
        headless=True,
        evidence_dir=str(tmp_path),
        escalation_manager=escalation_mgr,
        escalate_on_failure=True,
    )

    result = await executor.execute(artifact)

    assert result.status == ExecutionStatus.SUCCESS
    assert len(result.interventions) == 1
    assert result.interventions[0].resolution == "RESUMED"


@pytest.mark.asyncio
async def test_escalation_record_custom_action(tmp_path):
    """Verifies that custom actions can be recorded and PII is scrubbed."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=False)
    await manager.record_custom_action(
        action_type=OperatorActionType.CUSTOM,
        target="button#override-123-45-6789",
        value="Secret SSN: 999-88-7777",
        details={"note": "Manual supervisor bypass"},
    )
    actions = manager._active_actions
    assert len(actions) == 1
    assert actions[0].action_type == OperatorActionType.CUSTOM
    assert "123-45-6789" not in (actions[0].target or "")
    assert "[REDACTED_SSN]" in (actions[0].target or "")
    assert "999-88-7777" not in (actions[0].value or "")
    assert "[REDACTED_SSN]" in (actions[0].value or "")


@pytest.mark.asyncio
async def test_escalation_dialog_recording(tmp_path):
    """Verifies that browser dialog popups are auto-resolved in unattended mode with message PII redacted."""
    manager = EscalationManager(
        evidence_dir=str(tmp_path),
        interactive=False,
        auto_handle_dialogs=True,
        default_dialog_action="accept",
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handler_with_alert(req: InterventionRequest, pg):
            # Trigger browser alert without any manual page.once('dialog') workaround
            await pg.evaluate("alert('Verification required for SSN: 111-22-3333');")
            await asyncio.sleep(0.05)
            return "RESUMED"

        manager.operator_handler = handler_with_alert

        req = InterventionRequest(
            request_id="dlg_001",
            capability_id="dlg_cap",
            reason="Dialog trigger test",
        )
        record = await manager.request_intervention(page, req)
        assert record.resolution == "RESUMED"

        dialog_actions = [a for a in record.operator_actions if a.action_type == OperatorActionType.DIALOG]
        assert len(dialog_actions) == 1
        assert "111-22-3333" not in (dialog_actions[0].value or "")
        assert "[REDACTED_SSN]" in (dialog_actions[0].value or "")
        assert dialog_actions[0].details is not None
        assert dialog_actions[0].details.get("resolution") == "accepted"

        await browser.close()


@pytest.mark.asyncio
async def test_escalation_dialog_interactive_prompt(tmp_path, monkeypatch):
    """Verifies that interactive mode prompts operator to accept or dismiss dialogs."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=True)

    # Mock CLI input to accept
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    mock_dialog = mock.Mock()
    mock_dialog.type = "confirm"
    mock_dialog.message = "Authorize transfer of $500?"
    mock_dialog.default_value = None
    mock_dialog.accept = mock.AsyncMock()
    mock_dialog.dismiss = mock.AsyncMock()

    await manager._handle_dialog(mock_dialog)
    mock_dialog.accept.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "accepted"

    # Mock CLI input to dismiss
    monkeypatch.setattr("builtins.input", lambda prompt: "N")
    mock_dialog_dismiss = mock.Mock()
    mock_dialog_dismiss.type = "confirm"
    mock_dialog_dismiss.message = "Cancel transfer?"
    mock_dialog_dismiss.default_value = None
    mock_dialog_dismiss.accept = mock.AsyncMock()
    mock_dialog_dismiss.dismiss = mock.AsyncMock()

    await manager._handle_dialog(mock_dialog_dismiss)
    mock_dialog_dismiss.dismiss.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "dismissed"


@pytest.mark.asyncio
async def test_escalation_dialog_custom_handler(tmp_path):
    """Verifies that a custom dialog_handler hook is executed when provided."""
    custom_handled = False

    async def custom_dialog_hook(dialog):
        nonlocal custom_handled
        custom_handled = True
        await dialog.accept()

    manager = EscalationManager(
        evidence_dir=str(tmp_path),
        interactive=False,
        dialog_handler=custom_dialog_hook,
    )
    mock_dialog = mock.Mock()
    mock_dialog.type = "alert"
    mock_dialog.message = "Notice"
    mock_dialog.default_value = None
    mock_dialog.accept = mock.AsyncMock()

    await manager._handle_dialog(mock_dialog)
    assert custom_handled is True
    mock_dialog.accept.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "custom_handler"



@pytest.mark.asyncio
async def test_escalation_prompt_edge_cases(tmp_path, monkeypatch):
    """Verifies EOFError and invalid choice handling in CLI prompt."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=True)
    req = InterventionRequest(
        request_id="prompt_edge_001",
        capability_id="test",
        reason="Prompt edge test",
    )

    # Test EOFError -> ABORTED
    def raise_eof(p):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)
    assert await manager._prompt_operator_cli(req) == "ABORTED"

    # Test invalid option followed by valid option
    inputs = iter(["X", "Z", "M"])
    monkeypatch.setattr("builtins.input", lambda p: next(inputs))
    assert await manager._prompt_operator_cli(req) == "OVERRIDDEN"


@pytest.mark.asyncio
async def test_escalation_default_resolution_unattended(tmp_path):
    """Verifies default resolution is ABORTED when unattended, unless explicitly configured."""
    manager_default = EscalationManager(evidence_dir=str(tmp_path), interactive=False)
    assert manager_default.default_resolution == "ABORTED"

    manager = EscalationManager(
        evidence_dir=str(tmp_path),
        interactive=False,
        default_resolution="OVERRIDDEN",
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        req = InterventionRequest(
            request_id="default_res_001",
            capability_id="test",
            reason="Unattended default resolution",
        )
        record = await manager.request_intervention(page, req)
        assert record.resolution == "OVERRIDDEN"
        await browser.close()


@pytest.mark.asyncio
async def test_handle_dialog_empty_message(tmp_path):
    """Verifies that _handle_dialog handles dialogs with empty messages gracefully."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=False)
    mock_dialog = mock.Mock()
    mock_dialog.message = ""
    mock_dialog.default_value = "Default Value"
    try:
        await manager._handle_dialog(mock_dialog)
    except Exception as e:
        pytest.fail(f"_handle_dialog raised an unexpected exception: {e}")


@pytest.mark.asyncio
async def test_request_intervention_screenshot_exception(tmp_path):
    """Verifies that request_intervention handles screenshot exceptions gracefully."""
    evidence_dir = tmp_path / "evidence"
    manager = EscalationManager(evidence_dir=str(evidence_dir), interactive=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Mock the screenshot method to raise an exception
        async def mock_screenshot(*args, **kwargs):
            raise Exception("Screenshot failed")

        page.screenshot = mock_screenshot

        req = InterventionRequest(
            request_id="test_screenshot_exception",
            capability_id="test_cap",
            reason="Testing screenshot failure handling",
        )

        record = await manager.request_intervention(page, req)
        assert record.screenshot_before.endswith(".png")
        assert record.screenshot_after.endswith(".png")
        await browser.close()


@pytest.mark.asyncio
async def test_escalation_alert_auto_accepted_without_prompt(tmp_path, monkeypatch):
    """Verifies that alert dialogs are auto-accepted immediately without prompting stdin."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=True)

    # If input() were called, fail the test
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("input() should not be called for alerts"))

    mock_dialog = mock.Mock()
    mock_dialog.type = "alert"
    mock_dialog.message = "Information: Profile updated"
    mock_dialog.default_value = None
    mock_dialog.accept = mock.AsyncMock()
    mock_dialog.dismiss = mock.AsyncMock()

    await manager._handle_dialog(mock_dialog)
    mock_dialog.accept.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "accepted"


@pytest.mark.asyncio
async def test_escalation_dialog_during_active_operator_prompt(tmp_path, monkeypatch):
    """Verifies that dialogs occurring while operator prompt holds stdin are auto-resolved without contention."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=True, default_dialog_action="accept")
    manager._operator_prompt_active = True

    # If input() were called, fail the test
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: pytest.fail("input() should not be called while operator prompt is active"),
    )

    mock_confirm = mock.Mock()
    mock_confirm.type = "confirm"
    mock_confirm.message = "Proceed with transfer?"
    mock_confirm.default_value = None
    mock_confirm.accept = mock.AsyncMock()
    mock_confirm.dismiss = mock.AsyncMock()

    await manager._handle_dialog(mock_confirm)
    mock_confirm.accept.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "accepted"

    # Now test with default_dialog_action="dismiss"
    manager.default_dialog_action = "dismiss"
    mock_confirm_dismiss = mock.Mock()
    mock_confirm_dismiss.type = "confirm"
    mock_confirm_dismiss.message = "Proceed with transfer?"
    mock_confirm_dismiss.default_value = None
    mock_confirm_dismiss.accept = mock.AsyncMock()
    mock_confirm_dismiss.dismiss = mock.AsyncMock()

    await manager._handle_dialog(mock_confirm_dismiss)
    mock_confirm_dismiss.dismiss.assert_awaited_once()
    assert manager._active_actions[-1].details.get("resolution") == "dismissed"


@pytest.mark.asyncio
async def test_prompt_operator_cli_tracks_active_state(tmp_path, monkeypatch):
    """Verifies that _prompt_operator_cli sets and resets _operator_prompt_active flag."""
    manager = EscalationManager(evidence_dir=str(tmp_path), interactive=True)
    req = InterventionRequest(
        request_id="track_state_001",
        capability_id="test",
        reason="Tracking state test",
    )

    state_observed_during_input = None

    def mock_input(prompt):
        nonlocal state_observed_during_input
        state_observed_during_input = manager._operator_prompt_active
        return "R"

    monkeypatch.setattr("builtins.input", mock_input)

    assert manager._operator_prompt_active is False
    res = await manager._prompt_operator_cli(req)
    assert res == "RESUMED"
    assert state_observed_during_input is True
    assert manager._operator_prompt_active is False


