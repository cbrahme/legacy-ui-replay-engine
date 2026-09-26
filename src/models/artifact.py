from enum import Enum
import re
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    CLICK_COORDINATE = "click_coordinate"
    FILL = "fill"
    EXTRACT = "extract"
    ASSERT = "assert"
    WAIT = "wait"


class Coordinate(BaseModel):
    x: int
    y: int


class LocatorStrategy(BaseModel):
    """
    Multi-tier resilient locator definition with optional frame scoping.
    Priority:
    1. primary: Accessible role/name or stable identifier
    2. fallbacks: Priority-ordered text anchors, scoped CSS, and XPath
    3. frame_selector: Optional legacy <frame> or <iframe> boundary target
    """
    model_config = ConfigDict(extra="forbid")

    frame_selector: Optional[str] = Field(
        default=None,
        description="Optional selector for legacy <frame> or <iframe> boundary (e.g. frame[name='content'])"
    )
    primary: str = Field(
        ...,
        description="Primary locator (e.g. role:textbox[name='Member Search ID'])"
    )
    fallbacks: List[str] = Field(
        default_factory=list,
        description="Priority-ordered fallback locators (text, CSS, XPath)"
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Engineering rationale for locator design and fallback order"
    )


class Step(BaseModel):
    """
    Single discrete executable action within a capability flow.
    """
    model_config = ConfigDict(extra="forbid")

    step_id: int = Field(..., ge=1, description="1-indexed ordered step identifier")
    action: ActionType = Field(..., description="Action to perform")
    target: Optional[LocatorStrategy] = Field(
        default=None,
        description="Target element locator strategy for click, fill, extract, assert"
    )
    value: Optional[str] = Field(
        default=None,
        description="Input text, URL, or assertion value (supports {{variable}} templates)"
    )
    coordinate: Optional[Coordinate] = Field(
        default=None,
        description="Optional x, y coordinate for click_coordinate on canvas/non-DOM surfaces"
    )
    field_name: Optional[str] = Field(
        default=None,
        description="Target field name when action is 'extract'"
    )
    is_irreversible: bool = Field(
        default=False,
        description="Flags high-risk/financial mutating actions requiring policy approval"
    )
    timeout_ms: int = Field(
        default=10000,
        ge=500,
        description="Timeout bound in milliseconds for this specific step"
    )

    @field_validator("target")
    @classmethod
    def validate_target_for_actions(cls, v: Optional[LocatorStrategy], info) -> Optional[LocatorStrategy]:
        action = info.data.get("action")
        if action in (ActionType.CLICK, ActionType.FILL, ActionType.EXTRACT) and v is None:
            # Note: click_coordinate doesn't require target, but click/fill/extract does
            if action != ActionType.CLICK_COORDINATE:
                raise ValueError(f"Action '{action}' requires a 'target' LocatorStrategy")
        return v

    @field_validator("field_name")
    @classmethod
    def validate_field_name_for_extract(cls, v: Optional[str], info) -> Optional[str]:
        action = info.data.get("action")
        if action == ActionType.EXTRACT and not v:
            raise ValueError("Action 'extract' requires 'field_name' to be specified")
        return v


class SuccessCondition(BaseModel):
    """
    Condition confirming that the target task succeeded.
    """
    model_config = ConfigDict(extra="forbid")

    type: Literal["element_visible", "element_text_contains", "url_contains"] = Field(
        default="element_visible",
        description="Assertion condition type"
    )
    target: str = Field(..., description="Locator or URL pattern to verify")
    pattern: Optional[str] = Field(
        default=None,
        description="Expected text or regex pattern if type is element_text_contains"
    )
    timeout_ms: int = Field(
        default=5000,
        ge=500,
        description="Maximum wait time for condition resolution"
    )


class BusinessOutcomeMatch(BaseModel):
    """
    Specification for recognizing a legitimate, expected business condition
    (e.g., Member record not found in system).
    """
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Unique outcome code (e.g. MEMBER_NOT_FOUND)")
    match_type: Literal["element_text_contains", "element_visible", "url_contains"] = Field(
        default="element_text_contains",
        description="Matching mechanism"
    )
    target: str = Field(..., description="Locator or URL to inspect")
    pattern: Optional[str] = Field(
        default=None,
        description="Expected substring or regex pattern"
    )
    description: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of this business state"
    )


class Checkpoint(BaseModel):
    """
    Evaluates execution completion and discriminates between success,
    expected business outcomes, and security holds.
    """
    model_config = ConfigDict(extra="forbid")

    success_condition: SuccessCondition = Field(
        ...,
        description="Assertion confirming arrival at desired success state"
    )
    business_outcomes: List[BusinessOutcomeMatch] = Field(
        default_factory=list,
        description="Declared business outcome patterns to race against success"
    )


class CapabilityArtifact(BaseModel):
    """
    Versioned, typed, declarative specification of a reusable browser capability.
    Compiled once during discovery; deterministically replayed with zero LLM.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique capability identifier (e.g. member_balance_lookup)")
    name: str = Field(..., description="Human-readable title of the capability")
    version: str = Field(default="1.0.0", description="SemVer capability version")
    description: str = Field(..., description="Functional purpose and business intent")
    target_path: Optional[str] = Field(
        default=None,
        description="Canonical relative URL route (e.g. /members)"
    )
    target_url: Optional[str] = Field(
        default=None,
        description="Full URL or parameterized URL template (e.g. {{base_url}}/members)"
    )
    input_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON schema or typed specification of required input parameters"
    )
    output_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="JSON schema of extracted data output fields"
    )
    steps: List[Step] = Field(
        ...,
        min_length=1,
        description="Ordered sequence of executable interaction steps"
    )
    checkpoint: Checkpoint = Field(
        ...,
        description="Terminal checkpoint for success verification and business outcome racing"
    )

    def render_parameter(self, template: str, params: Dict[str, Any]) -> str:
        """
        Replaces {{key}} placeholders with values from params dictionary.
        """
        if not template or not isinstance(template, str):
            return template

        def replacer(match):
            key = match.group(1).strip()
            if key in params:
                return str(params[key])
            # Check default from input_schema if defined
            if key in self.input_schema and isinstance(self.input_schema[key], dict):
                default_val = self.input_schema[key].get("default")
                if default_val is not None:
                    return str(default_val)
            return match.group(0)  # Keep unchanged if not provided

        return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", replacer, template)

    def get_effective_url(self, base_url: str = "http://127.0.0.1:8000") -> str:
        """
        Computes the target start URL using base_url and target_path/target_url.
        """
        clean_base = base_url.rstrip("/")
        if self.target_path:
            clean_path = self.target_path if self.target_path.startswith("/") else f"/{self.target_path}"
            return f"{clean_base}{clean_path}"
        if self.target_url:
            return self.render_parameter(self.target_url, {"base_url": clean_base})
        return clean_base
