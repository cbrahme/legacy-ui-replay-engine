from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class OperatorActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    NAVIGATION = "navigation"
    DIALOG = "dialog"
    CUSTOM = "custom"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OperatorAction(BaseModel):
    """
    Granular record of an individual manual action taken by a human operator
    during an active escalation handoff session.
    """
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(
        default_factory=_utc_now,
        description="Timestamp when operator performed the action"
    )
    action_type: OperatorActionType = Field(
        ...,
        description="Category of manual action (click, input, navigation, dialog)"
    )
    target: Optional[str] = Field(
        default=None,
        description="Descriptor or selector of target element interacted with"
    )
    value: Optional[str] = Field(
        default=None,
        description="Sanitized (PII-redacted) text typed or selected"
    )
    details: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional action metadata (e.g. key codes, coordinates, dialog message)"
    )


class InterventionRecord(BaseModel):
    """
    Complete audit packet capturing the entire human escalation event,
    preserving context and evidence across the automation/operator seam.
    """
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(..., description="Unique intervention identifier")
    trigger_step_id: Optional[int] = Field(
        default=None,
        description="Step ID that triggered escalation"
    )
    reason: str = Field(..., description="Root cause or justification for escalation")
    screenshot_before: str = Field(
        ...,
        description="File path to the UI screenshot captured at the moment of escalation"
    )
    screenshot_after: Optional[str] = Field(
        default=None,
        description="File path to the UI screenshot captured when operator returned control"
    )
    operator_actions: List[OperatorAction] = Field(
        default_factory=list,
        description="Chronological log of operator actions captured while automation was paused"
    )
    resolution: Literal["RESUMED", "ABORTED", "OVERRIDDEN"] = Field(
        default="RESUMED",
        description="Terminal resolution of the intervention handoff"
    )
    duration_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Elapsed time in seconds that the operator retained control"
    )


class InterventionRequest(BaseModel):
    """
    Diagnostic context payload presented to the human operator upon handoff.
    """
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(..., description="Unique intervention identifier")
    capability_id: str = Field(..., description="ID of capability being executed")
    step_id: Optional[int] = Field(default=None, description="Step currently being attempted")
    reason: str = Field(..., description="Reason for requesting human intervention")
    current_url: Optional[str] = Field(default=None, description="Active page URL at handoff")
    screenshot_path: Optional[str] = Field(
        default=None,
        description="Diagnostic screenshot location"
    )
    created_at: datetime = Field(
        default_factory=_utc_now,
        description="Timestamp when intervention was triggered"
    )
