import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from src.models.artifact import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    Checkpoint,
    LocatorStrategy,
    Step,
    SuccessCondition,
)


class ArtifactCompiler:
    """
    Compiles an interactive discovery trajectory into a versioned, typed,
    parameter-driven CapabilityArtifact schema.
    
    Transforms literal interaction values (e.g. '12345' or 'http://127.0.0.1:8000/members')
    into template variables ({{member_id}}, {{base_url}}/members) and generates
    strict input and output schemas.
    """

    def __init__(self, default_base_url: str = "http://127.0.0.1:8000"):
        self.default_base_url = default_base_url.rstrip("/")

    def compile(
        self,
        capability_id: str,
        name: str,
        description: str,
        steps: List[Step],
        checkpoint: Checkpoint,
        version: str = "1.0.0",
        parameter_bindings: Optional[Dict[str, str]] = None,
        base_url: Optional[str] = None,
        output_schema_descriptions: Optional[Dict[str, str]] = None,
    ) -> CapabilityArtifact:
        """
        Compiles recorded steps and checkpoint into a validated CapabilityArtifact.
        
        Args:
            capability_id: Unique identifier (e.g. member_balance_lookup)
            name: Human-readable name
            description: Purpose and intent of the capability
            steps: Ordered interaction steps captured during discovery
            checkpoint: Terminal success condition and business outcome race patterns
            version: SemVer version string
            parameter_bindings: Literal values to replace with parameter names (e.g. {"12345": "member_id"})
            base_url: Base application URL to parameterize into {{base_url}}
            output_schema_descriptions: Optional field descriptions for outputs
        """
        active_base_url = (base_url or self.default_base_url).rstrip("/")
        bindings = parameter_bindings or {}

        # 1. Parameterize and normalize steps
        compiled_steps: List[Step] = []
        input_params: Dict[str, Dict[str, Any]] = {}
        output_schema: Dict[str, Dict[str, Any]] = {}
        target_path: Optional[str] = None

        # Always register base_url in input_schema
        input_params["base_url"] = {
            "type": "string",
            "description": "Base host origin (e.g. http://127.0.0.1:8000)",
            "default": active_base_url,
        }

        # Track parameters discovered from bindings
        for literal_val, param_name in bindings.items():
            if param_name != "base_url":
                input_params[param_name] = {
                    "type": "string",
                    "description": f"Input parameter for {param_name.replace('_', ' ').title()}",
                    "example": str(literal_val),
                }

        for idx, orig_step in enumerate(steps, start=1):
            step_val = orig_step.value

            # Parameterize navigation URLs
            if orig_step.action == ActionType.NAVIGATE and step_val:
                parsed = urlparse(step_val)
                if parsed.scheme and parsed.netloc:
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    path = parsed.path or "/"
                    if parsed.query:
                        path = f"{path}?{parsed.query}"
                    if not target_path:
                        target_path = path
                    step_val = f"{{{{base_url}}}}{path}"
                elif step_val.startswith("/"):
                    if not target_path:
                        target_path = step_val
                    step_val = f"{{{{base_url}}}}{step_val}"

            # Parameterize input literals in fill or assert values
            elif step_val:
                for literal_val, param_name in bindings.items():
                    if str(literal_val) in str(step_val):
                        step_val = step_val.replace(str(literal_val), f"{{{{{param_name}}}}}")

            # Collect output fields
            if orig_step.action == ActionType.EXTRACT and orig_step.field_name:
                field = orig_step.field_name
                desc = (output_schema_descriptions or {}).get(field, f"Extracted value for {field}")
                output_schema[field] = {
                    "type": "string",
                    "description": desc,
                }

            compiled_step = Step(
                step_id=idx,
                action=orig_step.action,
                target=orig_step.target,
                value=step_val,
                coordinate=orig_step.coordinate,
                field_name=orig_step.field_name,
                is_irreversible=orig_step.is_irreversible,
                timeout_ms=orig_step.timeout_ms,
            )
            compiled_steps.append(compiled_step)

        artifact = CapabilityArtifact(
            id=capability_id,
            name=name,
            version=version,
            description=description,
            target_path=target_path,
            input_schema=input_params,
            output_schema=output_schema,
            steps=compiled_steps,
            checkpoint=checkpoint,
        )

        return artifact

    def save_artifact(
        self,
        artifact: CapabilityArtifact,
        output_dir: Union[str, Path] = "capabilities",
        filename: Optional[str] = None,
    ) -> Path:
        """
        Serializes and saves a CapabilityArtifact to JSON disk storage.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        target_file = out_path / (filename or f"{artifact.id}.json")

        json_data = artifact.model_dump(mode="json", exclude_none=True)
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)

        return target_file
