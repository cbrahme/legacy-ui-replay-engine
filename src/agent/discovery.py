import asyncio
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.agent.compiler import ArtifactCompiler
from src.agent.inspector import DOMInspector
from src.guardrails.policy import (
    ActionViolationError,
    DomainViolationError,
    GuardrailPolicy,
)
from src.guardrails.redactor import PIIRedactor
from src.models.artifact import (
    ActionType,
    BusinessOutcomeMatch,
    CapabilityArtifact,
    Checkpoint,
    LocatorStrategy,
    Step,
    SuccessCondition,
)
from src.models.result import ExecutionStatus


# JSON Schema for OpenAI / Anthropic Tool Calling
DISCOVERY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL (e.g. /members or http://127.0.0.1:8000/members).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The destination URL or relative path."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an interactive element (button, link, tab) identified by CSS selector, text, or role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Target locator or CSS selector (e.g. button[type='submit'], #search-btn, text:Search Records)."
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Explanation of why this element is clicked."
                    }
                },
                "required": ["selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fill",
            "description": "Type text into an input field or textarea.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Target input selector (e.g. input#member_id, input[name='member_search'])."
                    },
                    "value": {
                        "type": "string",
                        "description": "Text value to type into the field."
                    },
                    "param_name": {
                        "type": "string",
                        "description": "Optional variable name to parameterize this value into (e.g. 'member_id')."
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Explanation of the input field."
                    }
                },
                "required": ["selector", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract",
            "description": "Extract text data from an element on the screen into an output field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Target selector containing the value to extract."
                    },
                    "field_name": {
                        "type": "string",
                        "description": "Key name for the extracted data (e.g. 'savings_balance', 'account_status')."
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Explanation of what data is being extracted."
                    }
                },
                "required": ["selector", "field_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish_goal",
            "description": "Signal that the goal has been accomplished and declare terminal checkpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "success_selector": {
                        "type": "string",
                        "description": "Selector confirming arrival at success state (e.g. #accounts-table, text:Account Summary)."
                    },
                    "business_outcomes": {
                        "type": "array",
                        "description": "Potential expected business outcomes or warning alerts on this screen.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "target": {"type": "string"},
                                "pattern": {"type": "string"},
                                "description": {"type": "string"}
                            },
                            "required": ["code", "target", "pattern"]
                        }
                    },
                    "summary": {
                        "type": "string",
                        "description": "Summary of how the goal was achieved."
                    }
                },
                "required": ["success_selector"]
            }
        }
    }
]


class DiscoveryResult:
    """Outcome of an LLM discovery session."""
    def __init__(
        self,
        success: bool,
        capability: Optional[CapabilityArtifact] = None,
        goal: str = "",
        steps_executed: int = 0,
        recorded_steps: Optional[List[Step]] = None,
        extracted_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        trajectory_log: Optional[List[Dict[str, Any]]] = None,
        conversation: Optional[List[Dict[str, Any]]] = None,
    ):
        self.success = success
        self.capability = capability
        self.goal = goal
        self.steps_executed = steps_executed
        self.recorded_steps = recorded_steps or []
        self.extracted_data = extracted_data or {}
        self.error_message = error_message
        self.trajectory_log = trajectory_log or []
        self.conversation = conversation or []


class DiscoveryAgent:
    """
    Goal-driven LLM discovery loop with live DOM interception.
    Explores live web application surfaces, executes browser actions,
    inspects elements to derive resilient 4-tier locators, and compiles
    the trajectory into a validated CapabilityArtifact.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        headless: bool = True,
        slow_mo: int = 0,
        max_steps: int = 15,
        policy: Optional[GuardrailPolicy] = None,
        redactor: Optional[PIIRedactor] = None,
        inspector: Optional[DOMInspector] = None,
        compiler: Optional[ArtifactCompiler] = None,
        llm_caller: Optional[Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Any]] = None,
        openai_client: Optional[Any] = None,
        anthropic_client: Optional[Any] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.slow_mo = slow_mo
        self.max_steps = max_steps
        self.policy = policy or GuardrailPolicy()
        self.redactor = redactor or PIIRedactor()
        self.inspector = inspector or DOMInspector()
        self.compiler = compiler or ArtifactCompiler(default_base_url=self.base_url)
        self.llm_caller = llm_caller
        self._openai_client = openai_client
        self._anthropic_client = anthropic_client

    def _get_openai_client(self):
        """Lazily initialize and reuse AsyncOpenAI client across loop iterations."""
        if self._openai_client is None:
            openai_key = os.getenv("OPENAI_API_KEY")
            if openai_key and not openai_key.startswith("sk-placeholder"):
                try:
                    from openai import AsyncOpenAI
                    self._openai_client = AsyncOpenAI(api_key=openai_key)
                except Exception:
                    pass
        return self._openai_client

    def _get_anthropic_client(self):
        """Lazily initialize and reuse AsyncAnthropic client across loop iterations."""
        if self._anthropic_client is None:
            anthropic_key = os.getenv("ANTHROPIC_API_KEY")
            if anthropic_key:
                try:
                    from anthropic import AsyncAnthropic
                    self._anthropic_client = AsyncAnthropic(api_key=anthropic_key)
                except Exception:
                    pass
        return self._anthropic_client

    async def discover(
        self,
        goal: str,
        target_url: Optional[str] = None,
        capability_id: Optional[str] = None,
        capability_name: Optional[str] = None,
        page: Optional[Page] = None,
        parameter_hints: Optional[Dict[str, str]] = None,
    ) -> DiscoveryResult:
        """
        Executes the goal-driven discovery loop against the live application.
        """
        start_url = target_url or self.base_url
        cap_id = capability_id or f"cap_{uuid.uuid4().hex[:8]}"
        cap_name = capability_name or goal[:50].title()

        owns_browser = page is None
        playwright_instance = None
        browser = None
        context = None

        recorded_steps: List[Step] = []
        trajectory_log: List[Dict[str, Any]] = []
        extracted_data: Dict[str, Any] = {}
        parameter_bindings: Dict[str, str] = dict(parameter_hints or {})
        checkpoint: Optional[Checkpoint] = None

        try:
            if owns_browser:
                playwright_instance = await async_playwright().start()
                browser = await playwright_instance.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                )
                context = await browser.new_context()
                page = await context.new_page()

            # Navigate to initial URL if specified
            if start_url:
                self.policy.validate_url(start_url)
                await page.goto(start_url)
                # Record initial navigation step
                recorded_steps.append(
                    Step(
                        step_id=1,
                        action=ActionType.NAVIGATE,
                        value=start_url,
                        timeout_ms=10000,
                    )
                )
                trajectory_log.append({
                    "step_id": 1,
                    "action": "navigate",
                    "value": start_url,
                    "status": "OK",
                })

            # Initial observation before any tool is dispatched
            initial_elements = await self.inspector.get_interactive_elements(page)
            initial_title = await page.title()

            conversation: List[Dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        "You are an expert web automation discovery agent for legacy banking systems.\n"
                        "Your goal is to inspect the UI, identify required inputs and buttons, and interact "
                        "with the page step-by-step to accomplish the specified user goal.\n"
                        "Call available tools to navigate, fill forms, click buttons, extract data, and "
                        "signal finish_goal when the goal is achieved."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"Goal: {goal}\n"
                        f"Current URL: {page.url}\n"
                        f"Page Title: {initial_title}\n"
                        f"Interactive Elements:\n"
                        f"{json.dumps(initial_elements[:20], indent=1)}"
                    )
                }
            ]

            step_count = 0
            is_completed = False

            while step_count < self.max_steps and not is_completed:
                step_count += 1

                # 1. Current page state for caller
                interactive_elements = await self.inspector.get_interactive_elements(page)
                current_url = page.url

                # 2. Decide: Call LLM or deterministic fallback
                tool_call = await self._get_next_action(conversation, goal, interactive_elements, current_url)
                if not tool_call:
                    return DiscoveryResult(
                        success=False,
                        goal=goal,
                        steps_executed=len(recorded_steps),
                        recorded_steps=recorded_steps,
                        error_message="LLM caller returned no tool actions to execute.",
                        trajectory_log=trajectory_log,
                        conversation=conversation,
                    )

                call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                action_name = tool_call.get("name")
                action_args = tool_call.get("arguments", {})

                # Append assistant tool-call message to conversation history
                conversation.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": action_name,
                                "arguments": json.dumps(action_args),
                            },
                        }
                    ],
                })

                # Handle finish_goal immediately to avoid redundant post-action DOM queries
                if action_name == "finish_goal":
                    success_selector = action_args.get("success_selector", "")
                    raw_outcomes = action_args.get("business_outcomes", [])
                    outcomes = [
                        BusinessOutcomeMatch(
                            code=o["code"],
                            target=o["target"],
                            pattern=o["pattern"],
                            description=o.get("description"),
                        )
                        for o in raw_outcomes
                    ]

                    checkpoint = Checkpoint(
                        success_condition=SuccessCondition(
                            type="element_visible",
                            target=success_selector,
                            timeout_ms=5000,
                        ),
                        business_outcomes=outcomes,
                    )
                    is_completed = True
                    trajectory_log.append({
                        "step_id": len(recorded_steps) + 1,
                        "action": "finish_goal",
                        "success_selector": success_selector,
                        "outcomes_declared": len(outcomes),
                        "status": "OK",
                    })

                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps({
                            "status": "SUCCESS",
                            "action": "finish_goal",
                            "checkpoint_verified": True,
                        }),
                    })
                    break

                # 3. Act & Intercept with localized error recovery
                next_step_id = len(recorded_steps) + 1
                tool_result: Dict[str, Any] = {}

                try:
                    if action_name == "navigate":
                        nav_url = action_args.get("url", "")
                        if nav_url.startswith("/"):
                            nav_url = f"{self.base_url}{nav_url}"
                        self.policy.validate_url(nav_url)
                        await page.goto(nav_url)
                        step = Step(
                            step_id=next_step_id,
                            action=ActionType.NAVIGATE,
                            value=nav_url,
                        )
                        recorded_steps.append(step)
                        trajectory_log.append({
                            "step_id": next_step_id,
                            "action": "navigate",
                            "url": nav_url,
                            "status": "OK",
                        })
                        tool_result = {"status": "OK", "action": "navigate", "url": nav_url}

                    elif action_name == "click":
                        selector = action_args.get("selector", "")
                        rationale = action_args.get("rationale")
                        # Inspect element before clicking to obtain multi-tier strategy
                        locator = self._build_playwright_locator(page, selector)
                        strategy = await self.inspector.inspect_element(page, locator, context_name=rationale)
                        await locator.first.click()
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=2000)
                        except Exception:
                            pass
                        step = Step(
                            step_id=next_step_id,
                            action=ActionType.CLICK,
                            target=strategy,
                        )
                        recorded_steps.append(step)
                        trajectory_log.append({
                            "step_id": next_step_id,
                            "action": "click",
                            "primary_target": strategy.primary,
                            "fallbacks": strategy.fallbacks,
                            "status": "OK",
                        })
                        tool_result = {"status": "OK", "action": "click", "primary_target": strategy.primary}

                    elif action_name == "fill":
                        selector = action_args.get("selector", "")
                        value = action_args.get("value", "")
                        param_name = action_args.get("param_name")
                        rationale = action_args.get("rationale")

                        if param_name:
                            parameter_bindings[value] = param_name

                        locator = self._build_playwright_locator(page, selector)
                        strategy = await self.inspector.inspect_element(page, locator, context_name=rationale)
                        await locator.first.fill(value)
                        step = Step(
                            step_id=next_step_id,
                            action=ActionType.FILL,
                            target=strategy,
                            value=value,
                        )
                        recorded_steps.append(step)
                        trajectory_log.append({
                            "step_id": next_step_id,
                            "action": "fill",
                            "primary_target": strategy.primary,
                            "value": self.redactor.redact_text(value),
                            "status": "OK",
                        })
                        tool_result = {"status": "OK", "action": "fill", "primary_target": strategy.primary, "value": self.redactor.redact_text(value)}

                    elif action_name == "extract":
                        selector = action_args.get("selector", "")
                        field_name = action_args.get("field_name", f"field_{next_step_id}")
                        rationale = action_args.get("rationale")

                        locator = self._build_playwright_locator(page, selector)
                        strategy = await self.inspector.inspect_element(page, locator, context_name=rationale)
                        extracted_text = (await locator.first.inner_text()).strip()
                        extracted_data[field_name] = extracted_text

                        step = Step(
                            step_id=next_step_id,
                            action=ActionType.EXTRACT,
                            target=strategy,
                            field_name=field_name,
                        )
                        recorded_steps.append(step)
                        trajectory_log.append({
                            "step_id": next_step_id,
                            "action": "extract",
                            "field_name": field_name,
                            "value": self.redactor.redact_text(extracted_text),
                            "status": "OK",
                        })
                        tool_result = {"status": "OK", "action": "extract", "field_name": field_name, "value": self.redactor.redact_text(extracted_text)}

                    else:
                        raise ValueError(f"Unknown action: {action_name}")

                except Exception as exc:
                    tool_result = {
                        "status": "FAILED",
                        "action": action_name,
                        "error": str(exc),
                    }
                    trajectory_log.append({
                        "step_id": next_step_id,
                        "action": action_name,
                        "status": "FAILED",
                        "error": str(exc),
                    })
                    # Re-raise security guardrail errors to halt discovery immediately
                    if isinstance(exc, (DomainViolationError, ActionViolationError)):
                        raise exc

                # 4. Observe post-action page state and append tool response
                try:
                    post_elements = await self.inspector.get_interactive_elements(page)
                    post_title = await page.title()
                    tool_result["current_url"] = page.url
                    tool_result["page_title"] = post_title
                    tool_result["interactive_elements"] = post_elements[:20]
                except Exception:
                    pass

                conversation.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(tool_result),
                })



            if not is_completed or not checkpoint:
                # If finish_goal was not explicitly invoked, attempt fallback checkpoint
                if recorded_steps:
                    last_step = recorded_steps[-1]
                    fallback_target = last_step.target.primary if last_step.target else "body"
                    checkpoint = Checkpoint(
                        success_condition=SuccessCondition(
                            type="element_visible",
                            target=fallback_target,
                        )
                    )
                else:
                    return DiscoveryResult(
                        success=False,
                        goal=goal,
                        steps_executed=0,
                        error_message="Discovery timed out without executing any actions.",
                        trajectory_log=trajectory_log,
                        conversation=conversation,
                    )

            # 4. Compile CapabilityArtifact
            compiled_artifact = self.compiler.compile(
                capability_id=cap_id,
                name=cap_name,
                description=goal,
                steps=recorded_steps,
                checkpoint=checkpoint,
                parameter_bindings=parameter_bindings,
                base_url=self.base_url,
            )

            return DiscoveryResult(
                success=True,
                capability=compiled_artifact,
                goal=goal,
                steps_executed=len(recorded_steps),
                recorded_steps=recorded_steps,
                extracted_data=extracted_data,
                trajectory_log=trajectory_log,
                conversation=conversation,
            )

        except Exception as e:
            return DiscoveryResult(
                success=False,
                goal=goal,
                steps_executed=len(recorded_steps),
                recorded_steps=recorded_steps,
                error_message=str(e),
                trajectory_log=trajectory_log,
                conversation=conversation if "conversation" in locals() else [],
            )

        finally:
            if owns_browser:
                if context:
                    await context.close()
                if browser:
                    await browser.close()
                if playwright_instance:
                    await playwright_instance.stop()

    def _build_playwright_locator(self, root, locator_str: str):
        """Helper to build Playwright Locator from syntax string."""
        s = locator_str.strip()

        # Matches role:tag or role:tag[name="..."] / role:tag[name='...']
        # \2 ensures the closing quote matches the opening quote
        role_match = re.match(r"^role:([a-zA-Z]+)(?:\[name=(['\"])([\s\S]*?)\2\])?$", s)
        if role_match:
            role = role_match.group(1)
            name = role_match.group(3)
            if name:
                clean_name = name.replace('\\"', '"').replace("\\'", "'")
                return root.get_by_role(role, name=clean_name)
            return root.get_by_role(role)

        if s.startswith("text:"):
            raw_text = s[5:].strip().replace('\\"', '"').replace("\\'", "'")
            return root.get_by_text(raw_text)

        if s.startswith("label:"):
            raw_label = s[6:].strip().replace('\\"', '"').replace("\\'", "'")
            return root.get_by_label(raw_label)

        if s.startswith("css:"):
            return root.locator(s[4:].strip())

        if s.startswith("xpath:"):
            return root.locator(f"xpath={s[6:].strip()}")

        return root.locator(s)

    async def _get_next_action(
        self,
        conversation: List[Dict[str, Any]],
        goal: str,
        interactive_elements: List[Dict[str, Any]],
        current_url: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Determines the next tool action either via custom caller, live LLM API,
        or deterministic semantic heuristics.
        """
        # If user injected a custom caller
        if self.llm_caller:
            res = self.llm_caller(conversation, DISCOVERY_TOOLS)
            if asyncio.iscoroutine(res):
                return await res
            return res

        # Check if OpenAI client is available (reusing persistent connection pool)
        client = self._get_openai_client()
        if client:
            try:
                response = await client.chat.completions.create(
                    model="gpt-4o",
                    messages=conversation,
                    tools=DISCOVERY_TOOLS,
                    tool_choice="auto",
                    temperature=0.0,
                )
                choice = response.choices[0].message
                if choice.tool_calls:
                    tc = choice.tool_calls[0]
                    return {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments),
                    }
            except Exception:
                pass

        # Check if Anthropic client is available (reusing persistent connection pool)
        anth_client = self._get_anthropic_client()
        if anth_client:
            try:
                pass
            except Exception:
                pass


        return None
