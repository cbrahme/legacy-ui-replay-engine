from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from src.models.human import InterventionRecord


class ExecutionStatus(str, Enum):
    """
    Taxonomy of execution outcomes separating legitimate business outcomes
    from recoverable transients and hard system/locator failures.
    """
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    RECOVERABLE_ERROR = "RECOVERABLE_ERROR"
    HARD_FAILURE = "HARD_FAILURE"


class StepLog(BaseModel):
    """
    Structured execution trace for a single step.
    """
    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(..., description="Step identifier")
    action: str = Field(..., description="Action name executed")
    status: str = Field(..., description="Step outcome status (OK, SKIPPED, RETRIED, FAILED)")
    duration_ms: float = Field(default=0.0, description="Execution duration in milliseconds")
    target_resolved: Optional[str] = Field(
        default=None,
        description="The specific locator tier that matched (e.g. role, text, css)"
    )
    extracted_data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Key-value extracted data if action was 'extract'"
    )
    error_message: Optional[str] = Field(default=None, description="Diagnostic error if failed")


class ExecutionResult(BaseModel):
    """
    The canonical output payload produced by the Deterministic Replay Engine.
    Distinguishes clean business outcomes from hard automation failures.
    """
    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus = Field(
        ...,
        description="Taxonomy outcome code (SUCCESS, BUSINESS_OUTCOME, RECOVERABLE_ERROR, HARD_FAILURE)"
    )
    capability_id: str = Field(..., description="Identifier of the executed capability artifact")
    version: str = Field(..., description="Artifact version string")
    data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Extracted key-value business data on SUCCESS"
    )
    outcome_code: Optional[str] = Field(
        default=None,
        description="Business outcome code (e.g. MEMBER_NOT_FOUND) or failure code (e.g. LOCATOR_EXHAUSTED)"
    )
    outcome_message: Optional[str] = Field(
        default=None,
        description="Human-readable business message or root-cause failure rationale"
    )
    execution_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Total end-to-end replay duration in milliseconds"
    )
    steps_executed: int = Field(
        default=0,
        ge=0,
        description="Total number of steps dispatched before completion or short-circuit"
    )
    step_logs: List[StepLog] = Field(
        default_factory=list,
        description="Detailed chronological trace of each executed step"
    )
    interventions: List[InterventionRecord] = Field(
        default_factory=list,
        description="History of human escalation events and manual operator actions"
    )
    evidence_path: Optional[str] = Field(
        default=None,
        description="Primary evidence file (failure screenshot, trace, or before-handoff image)"
    )
    debug_context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Rich diagnostic metadata on failure (DOM snapshot path, failing locators, URL)"
    )

    @property
    def is_success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS

    @property
    def is_business_outcome(self) -> bool:
        return self.status == ExecutionStatus.BUSINESS_OUTCOME

    @property
    def is_hard_failure(self) -> bool:
        return self.status == ExecutionStatus.HARD_FAILURE
