import asyncio
from datetime import datetime
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, ElementHandle, FrameLocator, Locator, Page, async_playwright

from src.models.artifact import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    LocatorStrategy,
    Step,
    SuccessCondition,
)
from src.models.result import ExecutionResult, ExecutionStatus, StepLog


class LocatorResolutionError(Exception):
    """Raised when all primary and fallback locators fail to resolve an element."""
    def __init__(self, message: str, tried_locators: List[str]):
        super().__init__(message)
        self.tried_locators = tried_locators


class ReplayExecutor:
    """
    Deterministic Playwright execution engine for Capability Artifacts.
    Executes pre-compiled workflows with multi-tier locator resolution,
    parameter injection, concurrent checkpoint state racing, irreversible
    action gating, and automated rich failure diagnostics. Zero LLM inference.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        headless: bool = True,
        slow_mo: int = 0,
        evidence_dir: str = "evidence",
        allow_irreversible: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.slow_mo = slow_mo
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.allow_irreversible = allow_irreversible

    async def execute(
        self,
        artifact: CapabilityArtifact,
        inputs: Optional[Dict[str, Any]] = None,
        page: Optional[Page] = None,
    ) -> ExecutionResult:
        """
        Executes a capability artifact against a Playwright page.
        If an existing page is not provided, a new browser session is launched.
        """
        params = inputs or {}
        if "base_url" not in params:
            params["base_url"] = self.base_url

        start_time = time.perf_counter()
        step_logs: List[StepLog] = []
        extracted_data: Dict[str, Any] = {}

        owns_browser = page is None
        playwright_instance = None
        browser = None
        context = None

        try:
            if owns_browser:
                playwright_instance = await async_playwright().start()
                browser = await playwright_instance.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                )
                context = await browser.new_context()
                page = await context.new_page()

            # Track whether pre-extraction state has been observed
            has_observed_pre_extract = False

            # Execute steps
            for step in artifact.steps:
                # Pre-Extraction Multi-Condition State Observer:
                # Race success condition against declared business outcomes before attempting extraction
                if step.action == ActionType.EXTRACT and not has_observed_pre_extract:
                    has_observed_pre_extract = True
                    obs_status, outcome_match = await self._observe_terminal_state(
                        page=page,
                        checkpoint=artifact.checkpoint,
                        params=params,
                    )
                    if obs_status == "BUSINESS_OUTCOME" and outcome_match:
                        total_duration = (time.perf_counter() - start_time) * 1000.0
                        return ExecutionResult(
                            status=ExecutionStatus.BUSINESS_OUTCOME,
                            capability_id=artifact.id,
                            version=artifact.version,
                            outcome_code=outcome_match.code,
                            outcome_message=outcome_match.description or f"Matched business outcome: {outcome_match.code}",
                            execution_time_ms=round(total_duration, 2),
                            steps_executed=len(step_logs),
                            step_logs=step_logs,
                            data=None,
                        )
                    elif obs_status != "SUCCESS":
                        evidence_img, evidence_html = await self._capture_failure_artifacts(
                            page, artifact.id, f"{step.step_id}_pre_extract"
                        )
                        total_duration = (time.perf_counter() - start_time) * 1000.0
                        return ExecutionResult(
                            status=ExecutionStatus.HARD_FAILURE,
                            capability_id=artifact.id,
                            version=artifact.version,
                            outcome_code="CHECKPOINT_FAILED",
                            outcome_message=(
                                f"Success condition '{artifact.checkpoint.success_condition.type}' "
                                f"targeting '{artifact.checkpoint.success_condition.target}' was not satisfied before extraction."
                            ),
                            execution_time_ms=round(total_duration, 2),
                            steps_executed=len(step_logs),
                            step_logs=step_logs,
                            evidence_path=evidence_img,
                            debug_context={
                                "html_snapshot": evidence_html,
                                "url": page.url,
                                "target": artifact.checkpoint.success_condition.target,
                            },
                        )

                # 1. Check Irreversible Action Policy Gate
                if step.is_irreversible and not self.allow_irreversible:
                    evidence_img, evidence_html = await self._capture_failure_artifacts(
                        page, artifact.id, step.step_id
                    )
                    step_logs.append(
                        StepLog(
                            step_id=step.step_id,
                            action=step.action.value,
                            status="FAILED",
                            error_message="Action blocked by irreversible action policy gate",
                        )
                    )
                    total_duration = (time.perf_counter() - start_time) * 1000.0
                    return ExecutionResult(
                        status=ExecutionStatus.HARD_FAILURE,
                        capability_id=artifact.id,
                        version=artifact.version,
                        outcome_code="IRREVERSIBLE_ACTION_BLOCKED",
                        outcome_message=(
                            f"Step {step.step_id} ({step.action.value}) is marked irreversible and "
                            "requires explicit authorization (allow_irreversible=True)."
                        ),
                        execution_time_ms=round(total_duration, 2),
                        steps_executed=len(step_logs),
                        step_logs=step_logs,
                        evidence_path=evidence_img,
                        debug_context={
                            "html_snapshot": evidence_html,
                            "step_id": step.step_id,
                            "action": step.action.value,
                            "url": page.url,
                        },
                    )

                step_start = time.perf_counter()
                try:
                    step_log = await self._dispatch_step(
                        page=page,
                        step=step,
                        artifact=artifact,
                        params=params,
                        extracted_data=extracted_data,
                    )
                    step_log.duration_ms = round((time.perf_counter() - step_start) * 1000.0, 2)
                    step_logs.append(step_log)
                except Exception as step_err:
                    step_duration = round((time.perf_counter() - step_start) * 1000.0, 2)
                    step_logs.append(
                        StepLog(
                            step_id=step.step_id,
                            action=step.action.value,
                            status="FAILED",
                            duration_ms=step_duration,
                            error_message=str(step_err),
                        )
                    )
                    evidence_img, evidence_html = await self._capture_failure_artifacts(
                        page, artifact.id, step.step_id
                    )
                    total_duration = (time.perf_counter() - start_time) * 1000.0
                    return ExecutionResult(
                        status=ExecutionStatus.HARD_FAILURE,
                        capability_id=artifact.id,
                        version=artifact.version,
                        outcome_code="STEP_EXECUTION_FAILED",
                        outcome_message=f"Step {step.step_id} failed: {step_err}",
                        execution_time_ms=round(total_duration, 2),
                        steps_executed=len(step_logs),
                        step_logs=step_logs,
                        evidence_path=evidence_img,
                        debug_context={
                            "html_snapshot": evidence_html,
                            "step_id": step.step_id,
                            "action": step.action.value,
                            "error": str(step_err),
                            "url": page.url,
                        },
                    )

            if has_observed_pre_extract:
                total_duration = (time.perf_counter() - start_time) * 1000.0
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    capability_id=artifact.id,
                    version=artifact.version,
                    data=extracted_data if extracted_data else None,
                    outcome_code="SUCCESS",
                    outcome_message="Execution completed and success checkpoint verified",
                    execution_time_ms=round(total_duration, 2),
                    steps_executed=len(step_logs),
                    step_logs=step_logs,
                )

            # Multi-Condition Observation Loop: Race success against declared business outcomes
            observation_status, outcome_match = await self._observe_terminal_state(
                page=page,
                checkpoint=artifact.checkpoint,
                params=params,
            )

            total_duration = (time.perf_counter() - start_time) * 1000.0

            if observation_status == "BUSINESS_OUTCOME" and outcome_match:
                return ExecutionResult(
                    status=ExecutionStatus.BUSINESS_OUTCOME,
                    capability_id=artifact.id,
                    version=artifact.version,
                    outcome_code=outcome_match.code,
                    outcome_message=outcome_match.description or f"Matched business outcome: {outcome_match.code}",
                    execution_time_ms=round(total_duration, 2),
                    steps_executed=len(step_logs),
                    step_logs=step_logs,
                    data=None,
                )

            if observation_status == "SUCCESS":
                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    capability_id=artifact.id,
                    version=artifact.version,
                    data=extracted_data if extracted_data else None,
                    outcome_code="SUCCESS",
                    outcome_message="Execution completed and success checkpoint verified",
                    execution_time_ms=round(total_duration, 2),
                    steps_executed=len(step_logs),
                    step_logs=step_logs,
                )

            # Observation timed out or unfulfilled
            evidence_img, evidence_html = await self._capture_failure_artifacts(
                page, artifact.id, "checkpoint"
            )
            return ExecutionResult(
                status=ExecutionStatus.HARD_FAILURE,
                capability_id=artifact.id,
                version=artifact.version,
                outcome_code="CHECKPOINT_FAILED",
                outcome_message=(
                    f"Success condition '{artifact.checkpoint.success_condition.type}' "
                    f"targeting '{artifact.checkpoint.success_condition.target}' was not satisfied."
                ),
                execution_time_ms=round(total_duration, 2),
                steps_executed=len(step_logs),
                step_logs=step_logs,
                evidence_path=evidence_img,
                debug_context={
                    "html_snapshot": evidence_html,
                    "url": page.url,
                    "target": artifact.checkpoint.success_condition.target,
                },
            )

        finally:
            if owns_browser:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
                if playwright_instance:
                    await playwright_instance.stop()

    async def _dispatch_step(
        self,
        page: Page,
        step: Step,
        artifact: CapabilityArtifact,
        params: Dict[str, Any],
        extracted_data: Dict[str, Any],
    ) -> StepLog:
        """
        Dispatches a single executable action with appropriate value templating
        and locator resolution.
        """
        rendered_value = artifact.render_parameter(step.value, params) if step.value else None

        if step.action == ActionType.NAVIGATE:
            target_url = rendered_value or artifact.get_effective_url(self.base_url)
            # Resolve relative URLs
            if target_url.startswith("/"):
                target_url = f"{self.base_url}{target_url}"
            await page.goto(target_url, timeout=step.timeout_ms)
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved="navigation_url",
            )

        if step.action == ActionType.CLICK_COORDINATE:
            if not step.coordinate:
                raise ValueError(f"Step {step.step_id} requires coordinate for CLICK_COORDINATE")
            await page.mouse.click(step.coordinate.x, step.coordinate.y)
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=f"coord:({step.coordinate.x},{step.coordinate.y})",
            )

        if step.action == ActionType.WAIT:
            wait_time = int(rendered_value) if rendered_value and rendered_value.isdigit() else 1000
            await page.wait_for_timeout(wait_time)
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=f"wait_ms:{wait_time}",
            )

        # Actions requiring target element resolution: CLICK, FILL, EXTRACT, ASSERT
        if not step.target:
            raise ValueError(f"Step {step.step_id} ({step.action.value}) requires a target locator strategy")

        locator, tier_name = await self._resolve_locator(
            page=page,
            strategy=step.target,
            timeout_ms=step.timeout_ms,
        )

        if step.action == ActionType.CLICK:
            await locator.click(timeout=step.timeout_ms)
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=tier_name,
            )

        if step.action == ActionType.FILL:
            fill_val = rendered_value or ""
            await locator.fill(fill_val, timeout=step.timeout_ms)
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=tier_name,
            )

        if step.action == ActionType.EXTRACT:
            text = (await locator.inner_text(timeout=step.timeout_ms)).strip()
            field_name = step.field_name or f"field_{step.step_id}"
            extracted_data[field_name] = text
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=tier_name,
                extracted_data={field_name: text},
            )

        if step.action == ActionType.ASSERT:
            await locator.wait_for(state="visible", timeout=step.timeout_ms)
            if rendered_value:
                content = await locator.inner_text(timeout=step.timeout_ms)
                if rendered_value not in content:
                    raise AssertionError(
                        f"Step {step.step_id} assertion failed: expected '{rendered_value}' in '{content}'"
                    )
            return StepLog(
                step_id=step.step_id,
                action=step.action.value,
                status="OK",
                target_resolved=tier_name,
            )

        raise ValueError(f"Unsupported action type: {step.action}")

    async def _resolve_locator(
        self,
        page: Page,
        strategy: LocatorStrategy,
        timeout_ms: int = 5000,
    ) -> Tuple[Locator, str]:
        """
        Attempts to resolve the target element across the multi-tier hierarchy:
        1. primary locator
        2. priority-ordered fallbacks
        With optional legacy <frame>/<iframe> scoping.
        """
        search_root = self._get_search_root(page, strategy.frame_selector)
        all_candidates = [("primary", strategy.primary)] + [
            (f"fallback_{i}", fb) for i, fb in enumerate(strategy.fallbacks)
        ]

        deadline = time.perf_counter() + (timeout_ms / 1000.0)
        n_candidates = len(all_candidates)
        tried_locators: List[str] = []

        for i, (tier_name, loc_str) in enumerate(all_candidates):
            time_left_ms = int((deadline - time.perf_counter()) * 1000.0)
            if time_left_ms <= 0:
                break

            remaining_candidates = n_candidates - i
            allocated_tier_timeout = max(int(time_left_ms / remaining_candidates), 150)

            tried_locators.append(f"{tier_name}:{loc_str}")
            locator = self._build_playwright_locator(search_root, loc_str)
            try:
                # Wait for element presence/visibility
                await locator.first.wait_for(state="visible", timeout=allocated_tier_timeout)
                return locator.first, f"{tier_name}:{loc_str}"
            except Exception:
                # Quick attached fallback only if sufficient remaining budget exists
                remaining_ms = int((deadline - time.perf_counter()) * 1000.0)
                if remaining_ms > 100:
                    try:
                        await locator.first.wait_for(state="attached", timeout=min(remaining_ms, 200))
                        return locator.first, f"{tier_name}:{loc_str}"
                    except Exception:
                        continue
                continue

        raise LocatorResolutionError(
            f"Failed to resolve element using any locator strategy within {timeout_ms}ms.",
            tried_locators=tried_locators,
        )

    def _get_search_root(self, page: Page, frame_selector: Optional[str]):
        """Returns FrameLocator if frame_selector provided, else Page."""
        if frame_selector:
            return page.frame_locator(frame_selector)
        return page

    def _build_playwright_locator(self, root, locator_str: str) -> Locator:
        """
        Parses specialized locator prefixes or delegates directly to Playwright locator engine:
        - role:<role>[name='<name>']
        - label:<text>
        - text:<text>
        - css:<selector>
        - xpath:<selector>
        """
        loc_str = locator_str.strip()

        # Role pattern: role:button[name='Search'] or role:textbox[name='Member ID']
        role_match = re.match(r"^role:([a-zA-Z]+)(?:\[name=['\"](.*?)['\"]\])?$", loc_str)
        if role_match:
            role = role_match.group(1)
            name = role_match.group(2)
            if name:
                return root.get_by_role(role, name=name)
            return root.get_by_role(role)

        if loc_str.startswith("label:"):
            return root.get_by_label(loc_str[6:].strip())

        if loc_str.startswith("text:"):
            return root.get_by_text(loc_str[5:].strip())

        if loc_str.startswith("css:"):
            return root.locator(loc_str[4:].strip())

        if loc_str.startswith("xpath:"):
            return root.locator(f"xpath={loc_str[6:].strip()}")

        # Standard CSS / XPath / text locator supported directly by Playwright
        return root.locator(loc_str)

    async def _observe_terminal_state(
        self,
        page: Page,
        checkpoint,
        params: Dict[str, Any],
        poll_interval_ms: int = 150,
    ) -> Tuple[str, Optional[BusinessOutcomeMatch]]:
        """
        Multi-Condition State Observation Loop:
        Concurrently races success condition against declared business outcome patterns
        without waiting for false locator timeouts.
        """
        success_cond = checkpoint.success_condition
        business_outcomes: List[BusinessOutcomeMatch] = checkpoint.business_outcomes
        timeout_ms = success_cond.timeout_ms
        deadline = time.time() + (timeout_ms / 1000.0)

        while time.time() < deadline:
            # 1. Race Business Outcomes First (immediate short-circuit)
            for outcome in business_outcomes:
                matched = await self._check_business_match(page, outcome)
                if matched:
                    return "BUSINESS_OUTCOME", outcome

            # 2. Check Success Condition
            success_matched = await self._check_success_match(page, success_cond, params)
            if success_matched:
                return "SUCCESS", None

            await asyncio.sleep(poll_interval_ms / 1000.0)

        # Final check at deadline
        for outcome in business_outcomes:
            if await self._check_business_match(page, outcome):
                return "BUSINESS_OUTCOME", outcome

        if await self._check_success_match(page, success_cond, params):
            return "SUCCESS", None

        return "TIMEOUT", None

    async def _check_business_match(self, page: Page, outcome: BusinessOutcomeMatch) -> bool:
        """Evaluates whether a business outcome condition is met on the live page."""
        try:
            if outcome.match_type == "url_contains":
                return outcome.pattern in page.url if outcome.pattern else outcome.target in page.url

            loc = self._build_playwright_locator(page, outcome.target)
            if outcome.match_type == "element_visible":
                return await loc.first.is_visible()

            if outcome.match_type == "element_text_contains":
                if await loc.first.is_visible():
                    text = await loc.first.inner_text()
                    if outcome.pattern:
                        return bool(re.search(outcome.pattern, text, re.IGNORECASE))
                    return True
        except Exception:
            return False
        return False

    async def _check_success_match(
        self, page: Page, success_cond: SuccessCondition, params: Dict[str, Any]
    ) -> bool:
        """Evaluates whether the terminal success condition is met on the live page."""
        try:
            if success_cond.type == "url_contains":
                target_url = success_cond.target
                effective_base = str(params.get("base_url") or self.base_url).rstrip("/")
                if "{{base_url}}" in target_url:
                    target_url = target_url.replace("{{base_url}}", effective_base)
                for k, v in params.items():
                    target_url = target_url.replace(f"{{{{{k}}}}}", str(v))
                return target_url in page.url

            loc = self._build_playwright_locator(page, success_cond.target)
            if success_cond.type == "element_visible":
                return await loc.first.is_visible()

            if success_cond.type == "element_text_contains":
                if await loc.first.is_visible():
                    text = await loc.first.inner_text()
                    pattern = success_cond.pattern or ""
                    # Template replacement in pattern if present
                    for k, v in params.items():
                        pattern = pattern.replace(f"{{{{{k}}}}}", str(v))
                    return pattern in text
        except Exception:
            return False
        return False

    async def _capture_failure_artifacts(
        self, page: Optional[Page], capability_id: str, step_ref: Any
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Captures full-page screenshot and full HTML DOM snapshot to evidence/
        on unhandled step failure or blocked action.
        """
        if not page:
            return None, None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        img_name = f"failure_{capability_id}_step{step_ref}_{timestamp}.png"
        html_name = f"failure_{capability_id}_step{step_ref}_{timestamp}.html"

        img_path = self.evidence_dir / img_name
        html_path = self.evidence_dir / html_name

        try:
            await page.screenshot(path=str(img_path), full_page=True)
            screenshot_recorded = str(img_path)
        except Exception:
            screenshot_recorded = None

        try:
            content = await page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(content)
            html_recorded = str(html_path)
        except Exception:
            html_recorded = None

        return screenshot_recorded, html_recorded
