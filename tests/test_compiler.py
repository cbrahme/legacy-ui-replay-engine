import json
from pathlib import Path
import pytest

from src.agent.compiler import ArtifactCompiler
from src.models.artifact import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    Checkpoint,
    LocatorStrategy,
    Step,
    SuccessCondition,
)


def test_compiler_parameterization_and_schema(tmp_path: Path):
    compiler = ArtifactCompiler(default_base_url="http://127.0.0.1:8000")

    raw_steps = [
        Step(
            step_id=1,
            action=ActionType.NAVIGATE,
            value="http://127.0.0.1:8000/members",
        ),
        Step(
            step_id=2,
            action=ActionType.FILL,
            target=LocatorStrategy(
                primary="role:textbox[name='Member Search ID']",
                fallbacks=["#member_id"],
                rationale="Accessible input",
            ),
            value="12345",
        ),
        Step(
            step_id=3,
            action=ActionType.CLICK,
            target=LocatorStrategy(
                primary="role:button[name='Search Records']",
                fallbacks=["button.btn-search"],
                rationale="Search button",
            ),
        ),
        Step(
            step_id=4,
            action=ActionType.EXTRACT,
            target=LocatorStrategy(
                primary="#savings-balance-val",
                fallbacks=["td.balance"],
                rationale="Balance cell",
            ),
            field_name="savings_balance",
        ),
    ]

    checkpoint = Checkpoint(
        success_condition=SuccessCondition(
            type="element_visible",
            target="#accounts-table",
        ),
        business_outcomes=[
            BusinessOutcomeMatch(
                code="MEMBER_NOT_FOUND",
                target="#not-found-alert",
                pattern="not found in system",
            )
        ],
    )

    artifact = compiler.compile(
        capability_id="test_member_lookup",
        name="Test Member Lookup",
        description="Lookup member savings balance",
        steps=raw_steps,
        checkpoint=checkpoint,
        version="1.0.0",
        parameter_bindings={"12345": "member_id"},
        base_url="http://127.0.0.1:8000",
    )

    assert isinstance(artifact, CapabilityArtifact)
    assert artifact.id == "test_member_lookup"
    assert artifact.target_path == "/members"

    # Step 1 should have parameterized URL
    assert artifact.steps[0].value == "{{base_url}}/members"

    # Step 2 should have parameterized value
    assert artifact.steps[1].value == "{{member_id}}"

    # Input schema should contain base_url and member_id
    assert "base_url" in artifact.input_schema
    assert "member_id" in artifact.input_schema
    assert artifact.input_schema["member_id"]["example"] == "12345"

    # Output schema should contain savings_balance
    assert "savings_balance" in artifact.output_schema

    # Save to disk
    out_file = compiler.save_artifact(artifact, output_dir=tmp_path)
    assert out_file.exists()

    with open(out_file, "r") as f:
        loaded_data = json.load(f)

    # Validate loaded data against schema
    reloaded = CapabilityArtifact.model_validate(loaded_data)
    assert reloaded.id == artifact.id
    assert len(reloaded.steps) == 4


def test_compiler_relative_urls_and_multi_bindings():
    compiler = ArtifactCompiler()

    raw_steps = [
        Step(
            step_id=1,
            action=ActionType.NAVIGATE,
            value="/members?tab=active",
        ),
        Step(
            step_id=2,
            action=ActionType.FILL,
            target=LocatorStrategy(primary="#user", fallbacks=[]),
            value="user_chaitra",
        ),
        Step(
            step_id=3,
            action=ActionType.ASSERT,
            target=LocatorStrategy(primary="#greeting", fallbacks=[]),
            value="Welcome user_chaitra",
        ),
    ]

    checkpoint = Checkpoint(
        success_condition=SuccessCondition(
            type="element_visible",
            target="#dashboard",
        )
    )

    artifact = compiler.compile(
        capability_id="multi_bind_test",
        name="Multi Bind Test",
        description="Tests relative URL and multiple bindings",
        steps=raw_steps,
        checkpoint=checkpoint,
        parameter_bindings={"user_chaitra": "username"},
    )

    assert artifact.target_path == "/members?tab=active"
    assert artifact.steps[0].value == "{{base_url}}/members?tab=active"
    assert artifact.steps[1].value == "{{username}}"
    assert artifact.steps[2].value == "Welcome {{username}}"
    assert "username" in artifact.input_schema

