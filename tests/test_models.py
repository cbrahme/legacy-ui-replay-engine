import json
import pytest
from pydantic import ValidationError

from src.models import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    Checkpoint,
    Coordinate,
    ExecutionResult,
    ExecutionStatus,
    InterventionRecord,
    InterventionRequest,
    LocatorStrategy,
    OperatorAction,
    OperatorActionType,
    Step,
    StepLog,
    SuccessCondition,
)


def sample_artifact_dict():
    return {
        "id": "member_balance_lookup",
        "name": "Member Savings Balance Lookup",
        "version": "1.0.0",
        "description": "Searches for a credit union member by ID and extracts savings balance.",
        "target_path": "/members",
        "input_schema": {
            "member_id": {
                "type": "string",
                "description": "5-digit member account identifier",
                "example": "12345",
            },
            "base_url": {
                "type": "string",
                "default": "http://127.0.0.1:8000",
            },
        },
        "output_schema": {
            "member_name": {"type": "string"},
            "savings_balance": {"type": "string"},
        },
        "steps": [
            {
                "step_id": 1,
                "action": "navigate",
                "target": None,
                "value": "{{base_url}}/members",
                "is_irreversible": False,
                "timeout_ms": 10000,
            },
            {
                "step_id": 2,
                "action": "fill",
                "target": {
                    "frame_selector": None,
                    "primary": "role:textbox[name='Member Search ID']",
                    "fallbacks": ["css:input#member_id", "xpath://input[@name='member_id']"],
                    "rationale": "Primary accessible textbox; fallback to ID and name",
                },
                "value": "{{member_id}}",
                "is_irreversible": False,
                "timeout_ms": 5000,
            },
            {
                "step_id": 3,
                "action": "click",
                "target": {
                    "frame_selector": "iframe#legacy-frame",
                    "primary": "role:button[name='Search Records']",
                    "fallbacks": ["css:button#search-button", "text:Search Records"],
                    "rationale": "Submit button inside legacy frame",
                },
                "value": None,
                "is_irreversible": False,
            },
            {
                "step_id": 4,
                "action": "extract",
                "target": {
                    "frame_selector": None,
                    "primary": "css:#savings-balance-val",
                    "fallbacks": ["xpath://td[contains(text(), 'Savings')]/following-sibling::td[1]"],
                },
                "field_name": "savings_balance",
                "is_irreversible": False,
            },
        ],
        "checkpoint": {
            "success_condition": {
                "type": "element_visible",
                "target": "text:Account Summary",
                "timeout_ms": 5000,
            },
            "business_outcomes": [
                {
                    "code": "MEMBER_NOT_FOUND",
                    "match_type": "element_text_contains",
                    "target": "css:.alert-warning",
                    "pattern": "Member record not found",
                    "description": "Account ID is not registered in core database",
                },
                {
                    "code": "ACCOUNT_LOCKED",
                    "match_type": "element_text_contains",
                    "target": "css:.alert-danger",
                    "pattern": "Account is locked",
                    "description": "Security hold requires manager override",
                },
            ],
        },
    }


def test_capability_artifact_deserialization():
    data = sample_artifact_dict()
    artifact = CapabilityArtifact.model_validate(data)

    assert artifact.id == "member_balance_lookup"
    assert artifact.version == "1.0.0"
    assert len(artifact.steps) == 4
    assert artifact.steps[0].action == ActionType.NAVIGATE
    assert artifact.steps[1].action == ActionType.FILL
    assert artifact.steps[2].target.frame_selector == "iframe#legacy-frame"
    assert artifact.steps[3].action == ActionType.EXTRACT
    assert artifact.steps[3].field_name == "savings_balance"
    assert len(artifact.checkpoint.business_outcomes) == 2


def test_parameter_templating():
    data = sample_artifact_dict()
    artifact = CapabilityArtifact.model_validate(data)

    # Test parameter replacement
    templated = artifact.render_parameter("{{base_url}}/members?id={{member_id}}", {
        "base_url": "https://bank.example.com:8443",
        "member_id": "12345",
    })
    assert templated == "https://bank.example.com:8443/members?id=12345"

    # Test default fallback for base_url
    default_templated = artifact.render_parameter("{{base_url}}/members", {})
    assert default_templated == "http://127.0.0.1:8000/members"

    # Test effective URL resolution
    eff_url = artifact.get_effective_url("http://localhost:9000")
    assert eff_url == "http://localhost:9000/members"


def test_step_validation_rules():
    # 1. Fill step missing target should fail
    with pytest.raises(ValidationError):
        Step(step_id=1, action=ActionType.FILL, target=None, value="12345")

    # 2. Extract step missing field_name should fail
    with pytest.raises(ValidationError):
        Step(
            step_id=2,
            action=ActionType.EXTRACT,
            target=LocatorStrategy(primary="css:#balance"),
            field_name=None,
        )

    # 3. Coordinate click without target is valid
    coord_step = Step(
        step_id=3,
        action=ActionType.CLICK_COORDINATE,
        coordinate=Coordinate(x=250, y=400),
    )
    assert coord_step.coordinate.x == 250
    assert coord_step.coordinate.y == 400


def test_human_escalation_models():
    op_action1 = OperatorAction(
        action_type=OperatorActionType.CLICK,
        target="button#supervisor-override-btn",
        value=None,
        details={"button_text": "Authorize Supervisor Override"},
    )
    assert op_action1.action_type == OperatorActionType.CLICK
    assert op_action1.timestamp is not None

    op_action2 = OperatorAction(
        action_type=OperatorActionType.INPUT,
        target="input#override_reason",
        value="[REDACTED_REASON]",
    )

    record = InterventionRecord(
        request_id="int_001",
        trigger_step_id=3,
        reason="Security lockout on member 67890",
        screenshot_before="evidence/escalation_before_67890.png",
        screenshot_after="evidence/escalation_after_67890.png",
        operator_actions=[op_action1, op_action2],
        resolution="RESUMED",
        duration_seconds=14.5,
    )

    assert len(record.operator_actions) == 2
    assert record.resolution == "RESUMED"
    assert record.duration_seconds == 14.5

    # Test serialization round-trip
    record_json = record.model_dump_json()
    reconstructed = InterventionRecord.model_validate_json(record_json)
    assert reconstructed.request_id == "int_001"
    assert len(reconstructed.operator_actions) == 2


def test_execution_result_taxonomy():
    # Success outcome
    success_res = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        capability_id="member_balance_lookup",
        version="1.0.0",
        data={"member_name": "Eleanor Vance", "savings_balance": "$4,250.75"},
        execution_time_ms=850.4,
        steps_executed=4,
        step_logs=[
            StepLog(step_id=1, action="navigate", status="OK", duration_ms=120.0),
            StepLog(step_id=2, action="fill", status="OK", duration_ms=80.0),
            StepLog(step_id=3, action="click", status="OK", duration_ms=300.0),
            StepLog(step_id=4, action="extract", status="OK", duration_ms=50.0, extracted_data={"savings_balance": "$4,250.75"}),
        ],
    )
    assert success_res.is_success
    assert not success_res.is_business_outcome
    assert not success_res.is_hard_failure
    assert success_res.data["savings_balance"] == "$4,250.75"

    # Business outcome (Not an engine failure!)
    biz_res = ExecutionResult(
        status=ExecutionStatus.BUSINESS_OUTCOME,
        capability_id="member_balance_lookup",
        version="1.0.0",
        outcome_code="MEMBER_NOT_FOUND",
        outcome_message="Member record not found in system (ID: 99999)",
        execution_time_ms=620.0,
        steps_executed=3,
    )
    assert not biz_res.is_success
    assert biz_res.is_business_outcome
    assert not biz_res.is_hard_failure
    assert biz_res.outcome_code == "MEMBER_NOT_FOUND"

    # Hard failure with rich signal paths
    fail_res = ExecutionResult(
        status=ExecutionStatus.HARD_FAILURE,
        capability_id="member_balance_lookup",
        version="1.0.0",
        outcome_code="LOCATOR_EXHAUSTION",
        outcome_message="Failed to resolve primary and fallbacks for step 3",
        execution_time_ms=10500.0,
        steps_executed=2,
        evidence_path="evidence/fail_member_balance_lookup_step3.png",
        debug_context={
            "dom_snapshot_file": "evidence/fail_member_balance_lookup_step3.html",
            "attempted_locators": ["role:button[name='Search Records']", "css:button.btn-search"],
            "url": "http://127.0.0.1:8000/members",
        },
    )
    assert fail_res.is_hard_failure
    assert fail_res.evidence_path == "evidence/fail_member_balance_lookup_step3.png"
    assert fail_res.debug_context["dom_snapshot_file"].endswith(".html")
