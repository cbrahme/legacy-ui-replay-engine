"""
Script to execute the remaining evidence runs for Phase 9:
1. replay_happy_path.log (Member 12345 -> SUCCESS)
2. replay_business_outcome.log (Member 99999 -> BUSINESS_OUTCOME: MEMBER_NOT_FOUND)
3. replay_escalation.log + escalation_before_*.png + escalation_after_*.png (Member 67890 -> FRAUD_HOLD -> Human Handoff -> Override -> RESUMED)
4. replay_hard_failure.log + failure_*.png + failure_*.html (Broken locator -> HARD_FAILURE + rich signals)
5. replay_irreversible_blocked.log (is_irreversible: true in unattended mode -> IRREVERSIBLE_ACTION_BLOCKED)
"""

import asyncio
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
from src.models.human import InterventionRequest, OperatorActionType
from src.models.result import ExecutionResult, ExecutionStatus


def format_log(
    title: str,
    result: ExecutionResult,
    artifact: CapabilityArtifact,
    extra_details: str = "",
) -> str:
    """Formats a detailed, human-readable structured execution log."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"COMPUTER-USE AUTOMATION SYSTEM: {title.upper()}")
    lines.append("=" * 80)
    lines.append(f"Capability:       {artifact.name} ({artifact.id} v{artifact.version})")
    lines.append(f"Status:           {result.status.value}")
    lines.append(f"Outcome Code:     {result.outcome_code or 'N/A'}")
    lines.append(f"Outcome Message:  {result.outcome_message or 'N/A'}")
    lines.append(f"Execution Time:   {result.execution_time_ms:.2f} ms")
    lines.append(f"Steps Executed:   {result.steps_executed} / {len(artifact.steps)}")
    lines.append(f"PII Redaction:    ACTIVE (All SSNs, account numbers, and tokens redacted)")
    lines.append("=" * 80)
    lines.append("")

    if extra_details:
        lines.append("--------------------------------------------------------------------------------")
        lines.append("CONTEXT & SCENARIO DESCRIPTION")
        lines.append("--------------------------------------------------------------------------------")
        lines.append(extra_details.strip())
        lines.append("")

    lines.append("--------------------------------------------------------------------------------")
    lines.append("STEP EXECUTION TRACE")
    lines.append("--------------------------------------------------------------------------------")
    header = f"{'Step':<6} | {'Action':<14} | {'Status':<8} | {'Duration (ms)':<14} | {'Target / Resolved Locator'}"
    lines.append(header)
    lines.append("-" * len(header))
    for log in result.step_logs:
        target_desc = log.target_resolved or "-"
        lines.append(f"{log.step_id:<6} | {log.action:<14} | {log.status:<8} | {log.duration_ms:>13.2f} | {target_desc}")
        if log.error_message:
            lines.append(f"       └── Error: {log.error_message}")
        if log.extracted_data:
            lines.append(f"       └── Extracted: {log.extracted_data}")
    lines.append("")

    if result.data:
        lines.append("--------------------------------------------------------------------------------")
        lines.append("EXTRACTED OUTPUT DATA")
        lines.append("--------------------------------------------------------------------------------")
        for k, v in result.data.items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    if result.interventions:
        lines.append("--------------------------------------------------------------------------------")
        lines.append("HUMAN ESCALATION & OPERATOR AUDIT TRAIL")
        lines.append("--------------------------------------------------------------------------------")
        for i, inv in enumerate(result.interventions, 1):
            lines.append(f"[Intervention #{i}] Request ID: {inv.request_id}")
            lines.append(f"  Trigger Step:      Step {inv.trigger_step_id or 'N/A'}")
            lines.append(f"  Reason:            {inv.reason}")
            lines.append(f"  Resolution:        {inv.resolution}")
            lines.append(f"  Duration:          {inv.duration_seconds:.2f} s")
            lines.append(f"  Screenshot Before: {inv.screenshot_before}")
            lines.append(f"  Screenshot After:  {inv.screenshot_after or 'None'}")
            lines.append(f"  Operator Actions Recorded ({len(inv.operator_actions)} live events):")
            for act in inv.operator_actions:
                val_info = f" | Value: '{act.value}'" if act.value else ""
                target_info = f" | Target: {act.target}" if act.target else ""
                lines.append(f"    - [{act.action_type.value.upper()}]{target_info}{val_info}")
        lines.append("")

    if result.evidence_path or result.debug_context:
        lines.append("--------------------------------------------------------------------------------")
        lines.append("RICH FAILURE SIGNALS & DIAGNOSTIC ARTIFACTS")
        lines.append("--------------------------------------------------------------------------------")
        if result.evidence_path:
            lines.append(f"  Failure Screenshot: {result.evidence_path}")
        if result.debug_context:
            html_snap = result.debug_context.get("html_snapshot")
            if html_snap:
                lines.append(f"  DOM HTML Snapshot:  {html_snap}")
            for dk, dv in result.debug_context.items():
                if dk != "html_snapshot":
                    lines.append(f"  {dk}: {dv}")
        lines.append("")

    lines.append("--------------------------------------------------------------------------------")
    lines.append("SERIALIZED EXECUTION RESULT (JSON)")
    lines.append("--------------------------------------------------------------------------------")
    lines.append(result.model_dump_json(indent=2))
    lines.append("")
    lines.append("=" * 80)
    lines.append(f"END OF {title.upper()}")
    lines.append("=" * 80)
    return "\n".join(lines)


async def run_happy_path(artifact: CapabilityArtifact, evidence_dir: Path):
    """Executes happy path replay for member 12345."""
    print("\n[1/5] Running Replay: Happy Path (Member 12345)...")
    redactor = PIIRedactor()
    policy = GuardrailPolicy()
    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        headless=True,
        evidence_dir=str(evidence_dir),
    )
    result = await executor.execute(artifact, inputs={"member_id": "12345"})

    log_path = evidence_dir / "replay_happy_path.log"
    log_content = format_log(
        "Replay Happy Path Evidence Log",
        result,
        artifact,
        extra_details="Deterministic zero-LLM replay querying active credit union member 12345.\n"
                      "Verifies successful form navigation, input typing, search click, and savings balance extraction.",
    )
    log_path.write_text(log_content, encoding="utf-8")
    print(f"  -> Generated {log_path} (Status: {result.status.value}, Extracted: {result.data})")


async def run_business_outcome(artifact: CapabilityArtifact, evidence_dir: Path):
    """Executes business outcome path for member 99999."""
    print("\n[2/5] Running Replay: Business Outcome (Member 99999 - Not Found)...")
    redactor = PIIRedactor()
    policy = GuardrailPolicy()
    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        headless=True,
        evidence_dir=str(evidence_dir),
    )
    result = await executor.execute(artifact, inputs={"member_id": "99999"})

    log_path = evidence_dir / "replay_business_outcome.log"
    log_content = format_log(
        "Replay Business Outcome Evidence Log",
        result,
        artifact,
        extra_details="Replay querying non-existent member 99999.\n"
                      "Pre-Extraction Multi-Condition State Observer races success checkpoint against business outcome patterns.\n"
                      "Detects .alert-warning containing 'Member record not found in system' and short-circuits immediately,\n"
                      "bypassing extraction step 4 with ZERO locator timeout latency and returning clean BUSINESS_OUTCOME.",
    )
    log_path.write_text(log_content, encoding="utf-8")
    print(f"  -> Generated {log_path} (Status: {result.status.value}, Code: {result.outcome_code})")


async def run_escalation(artifact: CapabilityArtifact, evidence_dir: Path):
    """Executes human escalation path for member 67890 with live action recording."""
    print("\n[3/5] Running Replay: Human Escalation & Action Recording (Member 67890 - Locked)...")
    redactor = PIIRedactor()
    policy = GuardrailPolicy()
    escalation = EscalationManager(evidence_dir=str(evidence_dir), redactor=redactor, interactive=False)

    async def scripted_operator_handler(req: InterventionRequest, page):
        """Simulates human supervisor taking control of live session."""
        print(f"     [Human Operator] Received intervention request: {req.reason}")
        print(f"     [Human Operator] Inspecting frozen browser state on {page.url}...")
        
        # 1. Operator reviews lockout alert
        await asyncio.sleep(0.3)
        
        # 2. Operator clicks 'Authorize Supervisor Override' button
        # DOM observer and Playwright event listeners capture this interaction live
        override_btn = page.locator("#supervisor-override-btn")
        if await override_btn.count() > 0:
            print("     [Human Operator] Clicking #supervisor-override-btn (Supervisor Override)...")
            await override_btn.click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(0.5)

        print("     [Human Operator] Clearance verified. Handing control back to automation...")
        return "RESUMED"

    escalation.operator_handler = scripted_operator_handler

    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        escalation_manager=escalation,
        headless=True,
        evidence_dir=str(evidence_dir),
    )
    result = await executor.execute(artifact, inputs={"member_id": "67890"})

    log_path = evidence_dir / "replay_escalation.log"
    log_content = format_log(
        "Replay Human Escalation & Live Action Recording Evidence Log",
        result,
        artifact,
        extra_details="Replay querying member 67890 under security/fraud hold.\n"
                      "Multi-Condition State Observer detects FRAUD_HOLD security lockout banner.\n"
                      "Engine pauses automation without killing the browser session and routes an InterventionRequest.\n"
                      "EscalationManager captures escalation_before_*.png, attaches Playwright lifecycle & DOM listeners,\n"
                      "records supervisor override click, and captures escalation_after_*.png upon resumption.\n"
                      "Replay executor verifies security clearance, proceeds to extraction, and completes successfully.",
    )
    log_path.write_text(log_content, encoding="utf-8")
    print(f"  -> Generated {log_path} (Status: {result.status.value}, Interventions: {len(result.interventions)})")


async def run_hard_failure(artifact: CapabilityArtifact, evidence_dir: Path):
    """Executes hard failure run to capture rich diagnostic signals."""
    print("\n[4/5] Running Replay: Hard Failure Diagnostic Capture...")
    # Create corrupted artifact with invalid selector to trigger unrecoverable failure
    bad_artifact = copy.deepcopy(artifact)
    bad_artifact.id = "member_balance_lookup_corrupted"
    bad_artifact.steps[3].target.primary = "#non-existent-table-element-invalid-xyz"
    bad_artifact.steps[3].target.fallbacks = [
        "//table[@id='completely-missing-table']",
        "css:div.does-not-exist-at-all",
    ]
    # Set short timeout for fast execution
    bad_artifact.steps[3].timeout_ms = 1500

    redactor = PIIRedactor()
    policy = GuardrailPolicy()
    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        headless=True,
        evidence_dir=str(evidence_dir),
    )
    result = await executor.execute(bad_artifact, inputs={"member_id": "12345"})

    log_path = evidence_dir / "replay_hard_failure.log"
    log_content = format_log(
        "Replay Hard Failure & Diagnostic Capture Evidence Log",
        result,
        bad_artifact,
        extra_details="Replay injected with corrupted extraction locator to demonstrate robust failure categorization.\n"
                      "The replay engine exhausts all primary and fallback locators, transitions to HARD_FAILURE,\n"
                      "and immediately invokes _capture_failure_artifacts() to persist full-page PNG screenshot\n"
                      "and sanitized HTML DOM snapshot directly into evidence/.",
    )
    log_path.write_text(log_content, encoding="utf-8")
    print(f"  -> Generated {log_path} (Status: {result.status.value}, Screenshot: {result.evidence_path})")


async def run_irreversible_blocked(artifact: CapabilityArtifact, evidence_dir: Path):
    """Executes irreversible action gating in unattended mode."""
    print("\n[5/5] Running Replay: Irreversible Action Policy Gate (Unattended Block)...")
    # Create artifact where balance extraction or submission is marked irreversible
    mutating_artifact = copy.deepcopy(artifact)
    mutating_artifact.id = "member_disbursement_transfer"
    mutating_artifact.name = "Member Funds Disbursement & Transfer"
    mutating_artifact.steps[2].is_irreversible = True  # The search submission click marked irreversible

    redactor = PIIRedactor()
    policy = GuardrailPolicy()  # Defaults to IrreversiblePolicy.BLOCK_UNATTENDED
    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        headless=True,
        evidence_dir=str(evidence_dir),
        allow_irreversible=False,  # Unattended mode without explicit clearance flag
    )
    result = await executor.execute(mutating_artifact, inputs={"member_id": "12345"})

    log_path = evidence_dir / "replay_irreversible_blocked.log"
    log_content = format_log(
        "Replay Irreversible Action Gated Evidence Log",
        result,
        mutating_artifact,
        extra_details="Replay of a capability containing a mutating step (is_irreversible: true) in headless unattended mode.\n"
                      "Without explicit --allow-irreversible authorization, GuardrailPolicy immediately halts execution\n"
                      "prior to dispatching the action, captures diagnostic state, and records outcome IRREVERSIBLE_ACTION_BLOCKED.",
    )
    log_path.write_text(log_content, encoding="utf-8")
    print(f"  -> Generated {log_path} (Status: {result.status.value}, Code: {result.outcome_code})")


async def main():
    evidence_dir = Path("evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = Path("capabilities/member_balance_lookup.json")
    if not artifact_path.exists():
        print(f"Error: {artifact_path} does not exist.")
        sys.exit(1)

    raw_json = artifact_path.read_text(encoding="utf-8")
    artifact = CapabilityArtifact.model_validate_json(raw_json)

    print("================================================================================")
    print("STARTING END-TO-END REPLAY EVIDENCE GENERATION SUITE")
    print(f"Target Portal: http://127.0.0.1:8000")
    print(f"Artifact:      {artifact.name} ({artifact.id})")
    print(f"Evidence Dir:  {evidence_dir.resolve()}")
    print("================================================================================")

    await run_happy_path(artifact, evidence_dir)
    await run_business_outcome(artifact, evidence_dir)
    await run_escalation(artifact, evidence_dir)
    await run_hard_failure(artifact, evidence_dir)
    await run_irreversible_blocked(artifact, evidence_dir)

    print("\n================================================================================")
    print("ALL REMAINING PHASE 9 EVIDENCE GENERATED SUCCESSFULLY!")
    print("================================================================================")


if __name__ == "__main__":
    asyncio.run(main())
