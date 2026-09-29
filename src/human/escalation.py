import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Set
import uuid

from playwright.async_api import Dialog, Frame, Page

from src.guardrails.redactor import PIIRedactor
from src.models.human import (
    InterventionRecord,
    InterventionRequest,
    OperatorAction,
    OperatorActionType,
)


TRACKER_JS = """
(() => {
    if (window.__humanTrackerInstalled) return;
    window.__humanTrackerInstalled = true;

    function getElementDescriptor(el) {
        if (!el || !el.tagName) return "unknown";
        let desc = el.tagName.toLowerCase();
        if (el.id) {
            desc += '#' + el.id;
        } else if (el.name) {
            desc += `[name='${el.name}']`;
        } else if (el.className && typeof el.className === 'string') {
            const classes = el.className.trim().split(/\\s+/).filter(Boolean);
            if (classes.length) desc += '.' + classes.slice(0, 2).join('.');
        }
        const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
        if (text && text.length > 0 && text.length <= 40) {
            desc += ` (text: "${text}")`;
        }
        return desc;
    }

    document.addEventListener('click', (e) => {
        try {
            const el = e.target;
            const interactive = el.closest('button, a, input, select, textarea, [role="button"]') || el;
            const desc = getElementDescriptor(interactive);
            if (window.__recordHumanAction) {
                window.__recordHumanAction({
                    action_type: 'click',
                    target: desc,
                    value: null,
                    details: {
                        tagName: interactive.tagName,
                        id: interactive.id || null,
                        name: interactive.name || null,
                        type: interactive.type || null,
                        clientX: e.clientX,
                        clientY: e.clientY
                    }
                });
            }
        } catch (err) {}
    }, true);

    const recordInput = (e) => {
        try {
            const el = e.target;
            if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) {
                const desc = getElementDescriptor(el);
                if (window.__recordHumanAction) {
                    window.__recordHumanAction({
                        action_type: 'input',
                        target: desc,
                        value: el.value,
                        details: {
                            tagName: el.tagName,
                            id: el.id || null,
                            name: el.name || null,
                            type: el.type || null
                        }
                    });
                }
            }
        } catch (err) {}
    };

    document.addEventListener('change', recordInput, true);
    document.addEventListener('blur', recordInput, true);
})();
"""


class EscalationManager:
    """
    Manages live session freezing, human operator handoff, and real-time
    operator action recording across the automation/operator seam.

    Preserves the live browser context without killing the session, attaches
    Playwright lifecycle hooks and DOM listeners, PII-redacts operator inputs,
    and returns a structured InterventionRecord with complete audit evidence.
    """

    def __init__(
        self,
        evidence_dir: str = "evidence",
        redactor: Optional[PIIRedactor] = None,
        operator_handler: Optional[
            Callable[
                [InterventionRequest, Page],
                Awaitable[Literal["RESUMED", "ABORTED", "OVERRIDDEN"]],
            ]
        ] = None,
        default_resolution: Literal["RESUMED", "ABORTED", "OVERRIDDEN"] = "ABORTED",
        interactive: Optional[bool] = None,
        auto_handle_dialogs: bool = True,
        default_dialog_action: Literal["accept", "dismiss"] = "accept",
        dialog_handler: Optional[Callable[[Dialog], Awaitable[None]]] = None,
    ):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or PIIRedactor()
        self.operator_handler = operator_handler
        self.default_resolution = default_resolution
        self.interactive = interactive if interactive is not None else sys.stdin.isatty()
        self.auto_handle_dialogs = auto_handle_dialogs
        self.default_dialog_action = default_dialog_action
        self.dialog_handler = dialog_handler

        self._active_actions: List[OperatorAction] = []
        self._action_lock = asyncio.Lock()
        self._binding_name = "__recordHumanAction"
        self._operator_prompt_active: bool = False

    async def _on_dom_action(self, source: Any, payload: Dict[str, Any]) -> None:
        """Callback invoked from browser JavaScript via exposed binding."""
        try:
            raw_type = payload.get("action_type", "custom")
            action_type = (
                OperatorActionType(raw_type)
                if raw_type in OperatorActionType._value2member_map_
                else OperatorActionType.CUSTOM
            )
            raw_val = payload.get("value")
            val = self.redactor.redact_text(str(raw_val)) if raw_val is not None else None
            target = payload.get("target")
            if target:
                target = self.redactor.redact_text(str(target))
            details = payload.get("details")
            if details and isinstance(details, dict):
                details = self.redactor.redact_dict(details)

            async with self._action_lock:
                # Deduplicate back-to-back identical input events for the same target
                if (
                    action_type == OperatorActionType.INPUT
                    and self._active_actions
                    and self._active_actions[-1].action_type == OperatorActionType.INPUT
                    and self._active_actions[-1].target == target
                ):
                    self._active_actions[-1].value = val
                    if details:
                        self._active_actions[-1].details = details
                    return

                self._active_actions.append(
                    OperatorAction(
                        action_type=action_type,
                        target=target,
                        value=val,
                        details=details,
                    )
                )
        except Exception:
            pass

    async def _handle_navigation(self, page: Page, frame: Frame) -> None:
        """Records page / main-frame navigation events initiated by the operator."""
        try:
            if frame == page.main_frame:
                url = self.redactor.redact_text(frame.url)
                async with self._action_lock:
                    self._active_actions.append(
                        OperatorAction(
                            action_type=OperatorActionType.NAVIGATION,
                            target=url,
                            details={"url": url, "name": frame.name},
                        )
                    )
        except Exception:
            pass

    async def _handle_dialog(self, dialog: Dialog) -> None:
        """
        Records browser dialog alerts/prompts and resolves them:
        - In interactive mode (or custom dialog_handler): prompts human operator or delegates to hook.
        - In unattended/automated mode: automatically resolves according to policy so the browser never deadlocks.
        """
        resolution_applied = "unresolved"
        try:
            msg = self.redactor.redact_text(dialog.message)
            default_val = (
                self.redactor.redact_text(dialog.default_value)
                if dialog.default_value
                else None
            )
            details: Dict[str, Any] = {}
            if default_val:
                details["default_value"] = default_val

            if self.dialog_handler is not None:
                await self.dialog_handler(dialog)
                resolution_applied = "custom_handler"
            elif dialog.type == "alert":
                await dialog.accept()
                resolution_applied = "accepted"
                print(f"ℹ️  [Browser Alert Auto-Accepted]: \"{msg}\"")
            elif self._operator_prompt_active:
                # Terminal stdin is currently held by the operator resolution CLI prompt.
                # Auto-resolve according to policy to avoid terminal contention & race deadlocks.
                if self.default_dialog_action == "accept":
                    await dialog.accept()
                    resolution_applied = "accepted"
                else:
                    await dialog.dismiss()
                    resolution_applied = "dismissed"
                print(
                    f"🔔 [Browser {dialog.type.capitalize()} Auto-Resolved ({self.default_dialog_action})]: \"{msg}\""
                )
            elif self.interactive:
                decision = await self._prompt_dialog_cli(dialog)
                if decision == "accept":
                    await dialog.accept()
                    resolution_applied = "accepted"
                else:
                    await dialog.dismiss()
                    resolution_applied = "dismissed"
            elif self.auto_handle_dialogs:
                if self.default_dialog_action == "accept":
                    await dialog.accept()
                    resolution_applied = "accepted"
                else:
                    await dialog.dismiss()
                    resolution_applied = "dismissed"

            details["resolution"] = resolution_applied

            async with self._action_lock:
                self._active_actions.append(
                    OperatorAction(
                        action_type=OperatorActionType.DIALOG,
                        target=dialog.type,
                        value=msg,
                        details=details,
                    )
                )
        except Exception:
            try:
                await dialog.dismiss()
            except Exception:
                pass

    async def _prompt_dialog_cli(self, dialog: Dialog) -> Literal["accept", "dismiss"]:
        """Prompts human operator in terminal to accept or dismiss intercepted native dialog."""
        prompt_banner = f"""
--------------------------------------------------------------------------------
🔔 NATIVE BROWSER DIALOG INTERCEPTED: [{dialog.type.upper()}]
Message: "{dialog.message}"
--------------------------------------------------------------------------------
Select action:
  [Y] Accept (OK)
  [N] Dismiss (Cancel)
"""
        print(prompt_banner)

        def _sync_dialog_input() -> Literal["accept", "dismiss"]:
            while True:
                try:
                    choice = input("Accept dialog? [Y/n] (default: Y): ").strip().upper()
                    if not choice or choice in ("Y", "YES", "A", "ACCEPT"):
                        return "accept"
                    elif choice in ("N", "NO", "D", "DISMISS"):
                        return "dismiss"
                    print("Invalid choice. Please enter 'Y' to accept or 'N' to dismiss.")
                except (EOFError, KeyboardInterrupt):
                    return "dismiss"

        return await asyncio.to_thread(_sync_dialog_input)

    async def record_custom_action(
        self,
        action_type: OperatorActionType,
        target: Optional[str] = None,
        value: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Allows programmatic recording of operator actions from external harnesses."""
        redacted_val = self.redactor.redact_text(value) if value is not None else None
        redacted_target = self.redactor.redact_text(target) if target is not None else None
        async with self._action_lock:
            self._active_actions.append(
                OperatorAction(
                    action_type=action_type,
                    target=redacted_target,
                    value=redacted_val,
                    details=details,
                )
            )

    async def request_intervention(
        self,
        page: Page,
        request: InterventionRequest,
    ) -> InterventionRecord:
        """
        Freezes the current execution, presents diagnostic context to the operator,
        attaches real-time action recording hooks to the live browser page,
        waits for operator resolution, and cleanly packages an InterventionRecord.
        """
        start_time = time.perf_counter()
        async with self._action_lock:
            self._active_actions.clear()

        # 1. Capture Before-Screenshot
        screenshot_before_name = f"escalation_before_{request.request_id}.png"
        screenshot_before_path = self.evidence_dir / screenshot_before_name
        try:
            await page.screenshot(path=str(screenshot_before_path), full_page=True)
            screenshot_before_str = str(screenshot_before_path)
        except Exception:
            screenshot_before_str = str(screenshot_before_path)

        request.screenshot_path = screenshot_before_str
        if not request.current_url:
            try:
                request.current_url = page.url
            except Exception:
                request.current_url = None

        # 2. Attach Listeners & Ingest DOM Actions
        frame_nav_handler = lambda frame: asyncio.create_task(
            self._handle_navigation(page, frame)
        )
        dialog_handler = lambda dialog: asyncio.create_task(
            self._handle_dialog(dialog)
        )

        page.on("framenavigated", frame_nav_handler)
        page.on("dialog", dialog_handler)

        try:
            await page.expose_binding(self._binding_name, self._on_dom_action)
        except Exception as e:
            if "already registered" not in str(e).lower():
                pass

        try:
            await page.add_init_script(TRACKER_JS)
        except Exception:
            pass

        try:
            await page.evaluate(TRACKER_JS)
        except Exception:
            pass

        # 3. Wait for Operator Resolution
        resolution: Literal["RESUMED", "ABORTED", "OVERRIDDEN"] = "RESUMED"
        try:
            if self.operator_handler is not None:
                resolution = await self.operator_handler(request, page)
            elif self.interactive:
                resolution = await self._prompt_operator_cli(request)
            else:
                resolution = self.default_resolution
        except Exception:
            resolution = "ABORTED"

        # Allow brief tick for in-flight events to flush
        await asyncio.sleep(0.05)

        # 4. Detach Lifecycle Listeners
        try:
            page.remove_listener("framenavigated", frame_nav_handler)
            page.remove_listener("dialog", dialog_handler)
        except Exception:
            pass

        # 5. Capture After-Screenshot
        screenshot_after_name = f"escalation_after_{request.request_id}.png"
        screenshot_after_path = self.evidence_dir / screenshot_after_name
        try:
            await page.screenshot(path=str(screenshot_after_path), full_page=True)
            screenshot_after_str = str(screenshot_after_path)
        except Exception:
            screenshot_after_str = str(screenshot_after_path)

        duration_seconds = round(time.perf_counter() - start_time, 2)

        # 6. Build and return immutable InterventionRecord
        async with self._action_lock:
            actions_snapshot = list(self._active_actions)

        record = InterventionRecord(
            request_id=request.request_id,
            trigger_step_id=request.step_id,
            reason=self.redactor.redact_text(request.reason),
            screenshot_before=screenshot_before_str,
            screenshot_after=screenshot_after_str,
            operator_actions=actions_snapshot,
            resolution=resolution,
            duration_seconds=duration_seconds,
        )
        return record

    async def _prompt_operator_cli(
        self, request: InterventionRequest
    ) -> Literal["RESUMED", "ABORTED", "OVERRIDDEN"]:
        """
        Renders an interactive CLI prompt and waits for operator decision
        in a background thread without blocking the async event loop.
        """
        prompt_banner = f"""
================================================================================
🚨  HUMAN OPERATOR INTERVENTION REQUIRED
================================================================================
Request ID:     {request.request_id}
Capability:     {request.capability_id}
Trigger Step:   {request.step_id if request.step_id is not None else 'N/A'}
Current URL:    {request.current_url or 'Unknown'}
Reason:         {request.reason}
Screenshot:     {request.screenshot_path or 'None'}
--------------------------------------------------------------------------------
The automation session is paused. The live browser window is awaiting operator
action. Please perform any required manual steps directly in the browser.

Available actions:
  [R] Resume   - Operator has resolved the barrier; continue/re-evaluate flow
  [O] Override - Mark current step complete and proceed to next step
  [A] Abort    - Terminate execution with hard failure
================================================================================
"""
        print(prompt_banner)

        def _sync_input() -> str:
            while True:
                try:
                    choice = (
                        input("Enter resolution choice [R/O/A] (default: R): ")
                        .strip()
                        .upper()
                    )
                    if not choice or choice == "R":
                        return "RESUMED"
                    elif choice in ("O", "M"):
                        return "OVERRIDDEN"
                    elif choice == "A":
                        return "ABORTED"
                    print("Invalid selection. Please enter 'R', 'O', or 'A'.")
                except (EOFError, KeyboardInterrupt):
                    return "ABORTED"

        self._operator_prompt_active = True
        try:
            return await asyncio.to_thread(_sync_input)
        finally:
            self._operator_prompt_active = False
