# Implementation Plan: Computer-Use Automation System

**Project:** Computer-Use Automation System for Legacy Banking Applications  
**Company:** interface.ai Take-Home Assignment  
**Specification References:** `docs/Assignment.pdf` & `docs/Engineering_Specification.docx`  
**Target Delivery:** High-judgment, production-ready vertical slice meeting all core requirements and deliverable specifications.

---

## 1. Executive Summary & Core Philosophy

### 1.1 The Problem
Banks and credit unions rely heavily on legacy back-office systems (core banking terminals, member servicing portals, underwriting consoles) that lack modern APIs. Automating these systems requires driving the UI just as human operators do. However, running an LLM in the loop for every production transaction is prohibitively slow, expensive, non-deterministic, and prone to hallucinations.

### 1.2 The Solution: "Record-Once, Replay-Many"
This system implements a two-phase architecture:
1. **Discovery Phase (LLM-in-the-loop with Live Interception):** An LLM agent explores a live application UI given a natural language goal, reasons through layout and intent, and executes tool actions. The browser host harness intercepts each live interaction, inspects the active DOM/accessibility tree, and programmatically derives a verified multi-tier locator hierarchy (Role -> Label/Text -> CSS -> XPath).
2. **Compilation Phase:** The discovery trajectory is compiled into a typed, versioned, parameterized **Capability Artifact** (JSON schema), parameterizing input literals (e.g. `12345` -> `{{member_id}}`) and mapping checkpoint conditions.
3. **Deterministic Replay Phase (Zero LLM):** Production invocations execute the saved capability artifact directly via Playwright with zero LLM inference. Replay utilizes resilient accessibility locators, parameter templating, a **Multi-Condition Observation Loop** that concurrently races success checkpoints against business outcome patterns before/during extraction, and a rigorous error taxonomy.
4. **Human Escalation Seam & Operator Action Recording:** When unexpected barriers, unrecoverable states, or irreversible/risky actions occur, the engine pauses the live execution session, transfers control to a human operator, attaches Playwright event listeners and DOM observers to capture manual operator actions (clicks, typed inputs, dialogs, page navigations), records these interventions in the execution audit log, and resumes automation seamlessly on the same session.

---

## 2. System Architecture & Component Design

```
+----------------------------------------------------------------------------------------------------+
|                                         User / Agent CLI                                           |
|                           (Typer CLI: discover, replay, test-harness)                             |
+------------------------------------+---------------------------------------------------------------+
                                     |
           +-------------------------+-------------------------+
           |                                                   |
           v                                                   v
+-----------------------+                           +-----------------------+
|  Discovery Agent Loop |                           | Deterministic Replay  |
|      (LLM-Driven)     |                           |    Engine (No LLM)    |
| - Playwright Browser  |                           | - Parameter Injection |
| - State Observer (A11y|                           | - Multi-tier Locators |
|   Tree & DOM Snapshot)|                           | - Checkpoint Assert   |
| - Tool-Calling Loop   |                           | - Extraction Rules    |
+-----------+-----------+                           +-----------+-----------+
            |                                                   ^
            | Compiles to                                       | Reads Artifact
            v                                                   |
    +---------------+   capabilities/                   +-------+-------+
    | Capability    |==================================>| Execution     |
    | Artifact JSON |                                   | Result Schema |
    +---------------+                                   +-------+-------+
            ^                                                   |
            |                                                   v
+-----------+-----------+                           +-----------------------+
| Safety & Guardrails   |                           | Human Escalation Seam |
| - Domain Allowlist    |                           | - State Freeze/Pause  |
| - Action Allowlist    |                           | - Operator Hand-off   |
| - Irreversible Gates  |                           | - Resume Execution    |
| - PII Redactor        |                           +-----------------------+
+-----------------------+
                                     |
                                     v
+----------------------------------------------------------------------------------------------------+
|                   Target Banking Application (Local FastAPI + Jinja2 Mock)                         |
|   - Legacy Table / Frame UI (No Test IDs)       - Member 12345: Active ($4,250.75)                 |
|   - Transient Loading States & Interstitials    - Member 99999: Business Outcome ("Not Found")    |
|   - Irreversible Action / Lock Modals           - Member 67890: Locked (Escalation Trigger)        |
+----------------------------------------------------------------------------------------------------+
```

### 2.1 Component Responsibilities

1. **`src/target_app/` (Local Core Banking Portal Mock):**
   - Implemented in FastAPI + Jinja2 with intentionally realistic legacy characteristics (nested tables, non-semantic HTML, inline styling, absence of `data-testid`).
   - Serves distinct member account scenarios:
     - `12345`: Active member with savings/checking balances and transaction tables.
     - `99999`: "Member record not found in system" — exercises business outcome handling.
     - `67890`: "Account under fraud hold / Manager intervention required" — exercises human escalation.
     - Intermittent delay & interstitial confirmation modal for balance transfers / sub-account openings.

2. **`src/models/` (Pydantic Contracts):**
   - `CapabilityArtifact`: Defines metadata, versioning, typed input parameters, output extraction schemas, ordered steps, robust locator strategies, and checkpoints.
   - `Step`: Single executable action (`click`, `fill`, `navigate`, `extract`, `assert`, `wait`).
   - `LocatorStrategy`: Priority-ordered locator chain with optional `frame_selector` for scoping actions within legacy `<frame>` and `<iframe>` hierarchies (accessibility role/name -> text anchor -> structural CSS/XPath).
   - `ExecutionResult`: Discriminated union capturing `SUCCESS`, `BUSINESS_OUTCOME`, `RECOVERABLE_ERROR`, and `HARD_FAILURE`.
   - `InterventionRequest`: Context packet for human operator handoff (goal, current step, failure reason, screenshot path).
   - `OperatorAction`: Fine-grained record of operator activity during handoff (action type, target selector/label, sanitized value, timestamp).
   - `InterventionRecord`: Complete audit log of the escalation event, before/after visual state, and ordered sequence of human actions.

3. **`src/engine/` (Deterministic Replay Executor):**
   - Pure deterministic Playwright driver without LLM dependencies.
   - Evaluates variable substitutions (e.g. `{{member_id}}`).
   - Executes multi-tier locator resolution with frame scoping (`page.frame_locator(...)`), automatic retry, and timeout bounds.
   - **Pre-Extraction Multi-Condition State Observer:** Resolves post-action latency and branching hazards. Rather than blindly executing extractions sequentially after form submission, the engine executes a concurrent state race between the success checkpoint (enabling extraction), business outcome alerts (short-circuiting immediately with `BUSINESS_OUTCOME`), and security lockout banners (triggering escalation), preventing false locator timeouts on warning/error screens.
   - Evaluates success checkpoints and extracts output fields upon verified success states.
   - Categorizes outcomes into the required error taxonomy (`SUCCESS`, `BUSINESS_OUTCOME`, `RECOVERABLE_ERROR`, `HARD_FAILURE`).
   - **Automated Rich Failure Signal Generation:** On any unhandled `HARD_FAILURE` (locator exhaustion, assertion error, unexpected crash), immediately captures full-page screenshot (`page.screenshot(full_page=True)`) and dumps complete DOM snapshot (`page.content()`) into `evidence/`, attaching artifact paths and locator diagnostics to `ExecutionResult`.

4. **`src/agent/` (LLM Discovery Loop & Programmatic Compiler Seam):**
   - Connects LLM (Claude 3.5 Sonnet / OpenAI GPT-4o) to a live Playwright page.
   - Supplies the LLM with clean, intent-focused tools (`navigate`, `click`, `click_coordinate`, `fill`, `extract`, `finish_goal`, `request_escalation`).
   - **Live DOM Inspector & Locator Interceptor (`src/agent/inspector.py`):** Intercepts tool calls at execution time on the live page. Rather than relying on the LLM to invent or hallucinate multi-tier fallback selectors, the harness inspects the live `ElementHandle` and programmatically derives and validates a resilient 4-tier locator hierarchy (accessible role/name -> text anchor -> unique CSS -> canonical XPath).
   - **Artifact Compiler (`src/agent/compiler.py`):** Synthesizes the execution trajectory, parameterizes dynamic inputs (e.g., replacing `"12345"` with `{{member_id}}`), binds checkpoints, and emits a validated `CapabilityArtifact`.

5. **`src/guardrails/` (Safety & Policy Engine):**
   - Enforces strict URL host/route allowlists.
   - Enforces action allowlist (disallowing arbitrary script execution or out-of-scope actions).
   - **Irreversible Action Policy & Execution Gating:** Defines explicit runtime policies for steps marked `is_irreversible: true`:
     - *Unattended / Headless:* Default fail-safe policy blocks execution, captures pre-execution state, and returns `HARD_FAILURE` (`outcome_code: IRREVERSIBLE_ACTION_BLOCKED`) unless explicit `--allow-irreversible` authorization is supplied.
     - *Interactive / Headed:* Freezes automation prior to the mutating step and delegates to `EscalationManager` for explicit operator clearance (`[C] Confirm & Execute` / `[A] Abort`).
     - *Audit Trail:* Emits immutable non-repudiation audit log events for any executed irreversible mutation.
   - Redacts PII (SSNs, full card numbers, bank account numbers, credentials) before writing logs or artifacts.

6. **`src/human/` (Human Escalation & Live Session Handoff):**
   - Implements the live browser control-transfer mechanism across the automation/operator seam.
   - Freezes automation, captures diagnostic context (before-screenshot, step error, state), and raises an intervention alert.
   - **Active Session Operator Action Recording:** During the handoff window, attaches Playwright lifecycle listeners (`page.on('dialog')`, `page.on('framenavigated')`) and injects a lightweight DOM observer (`page.expose_binding('__recordHumanAction', ...)` capturing user clicks and inputs).
   - Sanitizes/PII-redacts all operator inputs and records the chronological action trace in real time.
   - Detects operator completion via interactive CLI prompt (`[R] Resume`, `[A] Abort`, `[M] Mark Step Complete`), detaches listeners, captures an after-screenshot, verifies session state, and seamlessly resumes automation.

7. **`src/cli.py` (Typer CLI):**
   - `discover`: Run LLM discovery on a goal and generate a capability JSON.
   - `replay`: Deterministically replay a saved capability with input parameters.
   - `test-harness`: Run end-to-end test suite against the target mock app covering happy path, business outcome, and error escalation.
   - `serve-target`: Launch the local mock core banking portal.

---

## 3. Data Schema & Contracts

### 3.1 `CapabilityArtifact` Schema (`src/models/artifact.py`)
```json
{
  "id": "member_balance_lookup",
  "name": "Member Savings Balance Lookup",
  "version": "1.0.0",
  "description": "Searches for a credit union member by ID and extracts their current savings balance and status.",
  "target_path": "/members",
  "input_schema": {
    "member_id": {
      "type": "string",
      "description": "Unique 5-digit member account identifier",
      "example": "12345"
    },
    "base_url": {
      "type": "string",
      "description": "Base host origin (configurable via --base-url or TARGET_APP_URL env; defaults to http://127.0.0.1:8000)",
      "default": "http://127.0.0.1:8000"
    }
  },
  "output_schema": {
    "member_name": {"type": "string"},
    "savings_balance": {"type": "string"},
    "account_status": {"type": "string"}
  },
  "steps": [
    {
      "step_id": 1,
      "action": "navigate",
      "target": null,
      "value": "{{base_url}}/members",
      "is_irreversible": false,
      "timeout_ms": 10000
    },
    {
      "step_id": 2,
      "action": "fill",
      "target": {
        "frame_selector": null,
        "primary": "role:textbox[name='Member Search ID']",
        "fallbacks": [
          "text:Member ID >> input",
          "css:input[name='member_search']",
          "xpath://input[@placeholder='Enter Member ID']"
        ],
        "rationale": "Prioritize accessible role and label; fallback to legacy input attributes"
      },
      "value": "{{member_id}}",
      "is_irreversible": false
    },
    {
      "step_id": 3,
      "action": "click",
      "target": {
        "frame_selector": null,
        "primary": "role:button[name='Search Records']",
        "fallbacks": [
          "text:Search",
          "css:button.btn-search",
          "xpath://input[@type='submit']"
        ],
        "rationale": "Search button in legacy form submission table"
      },
      "value": null,
      "is_irreversible": false
    },
    {
      "step_id": 4,
      "action": "extract",
      "target": {
        "frame_selector": null,
        "primary": "css:#savings-balance-val",
        "fallbacks": [
          "xpath://td[contains(text(), 'Savings')]/following-sibling::td[1]",
          "text:Savings >> .. >> td.balance"
        ],
        "rationale": "Extract balance from savings account row in core ledger table"
      },
      "field_name": "savings_balance",
      "is_irreversible": false
    }
  ],
  "checkpoint": {
    "success_condition": {
      "type": "element_visible",
      "target": "text:Account Summary",
      "timeout_ms": 5000
    },
    "business_outcomes": [
      {
        "code": "MEMBER_NOT_FOUND",
        "match_type": "element_text_contains",
        "target": "css:.alert-warning",
        "pattern": "Member record not found",
        "description": "Member ID does not exist in institution registry"
      },
      {
        "code": "ACCOUNT_LOCKED",
        "match_type": "element_text_contains",
        "target": "css:.alert-danger",
        "pattern": "Account is locked",
        "description": "Security lock requires operator intervention"
      }
    ]
  }
}
```

### 3.1.1 Execution Lifecycle & Pre-Extraction State Race (Race Condition Protection)
To prevent the timing hazard where an extraction step (e.g., `step_id: 4`) prematurely times out when the application branches into a business outcome or error page, execution follows an explicit phased lifecycle:
1. **Action Phase (Steps 1–3):** Sequential execution of navigation, form filling, and submission clicks.
2. **Multi-Condition Observation Race:** Immediately following form submission or prior to executing any `extract` action, the engine does not blindly query extraction locators. Instead, it concurrently evaluates:
   - `checkpoint.business_outcomes`: If an outcome matches (e.g., `.alert-warning` containing `"Member record not found"`), execution **short-circuits immediately** with `BUSINESS_OUTCOME`, bypassing all subsequent extraction steps with zero locator timeout delay.
   - **Escalation Triggers:** If a security alert matches (e.g., `.alert-danger` containing `"Account is locked"`), control is routed to `EscalationManager`.
   - `checkpoint.success_condition`: If verified (e.g., `text:Account Summary`), the engine proceeds to the extraction phase.
   - **Timeout Boundary:** If neither state resolves within `timeout_ms`, the engine transitions to `HARD_FAILURE` and captures rich failure artifacts.
3. **Extraction Phase (Step 4+):** Data fields (`savings_balance`) are extracted strictly from the verified success state.

### 3.2 `ExecutionResult` Taxonomy (`src/models/result.py`)
Replay execution must distinguish between:
1. `SUCCESS`: Reached success checkpoint, all output fields extracted.
2. `BUSINESS_OUTCOME`: Expected, legitimate business condition (e.g. member not found). This is **not** an engine failure; it is valid business data for calling agents.
3. `RECOVERABLE_ERROR`: Handled interstitial, dismissal of routine dialog, or transient retry.
4. `HARD_FAILURE`: Locator failure, unrecoverable page error, or invariant breach. Detailed diagnostic context (step ID, expected vs observed state, screenshot path) is returned.

```python
class ExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    RECOVERABLE_ERROR = "RECOVERABLE_ERROR"
    HARD_FAILURE = "HARD_FAILURE"

class OperatorActionType(str, Enum):
    CLICK = "click"
    INPUT = "input"
    NAVIGATION = "navigation"
    DIALOG = "dialog"
    CUSTOM = "custom"

class OperatorAction(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action_type: OperatorActionType
    target: Optional[str] = None          # e.g., "button#supervisor-override-btn", "text:Approve"
    value: Optional[str] = None           # PII-redacted text entered or selected
    details: Optional[Dict[str, Any]] = None

class InterventionRecord(BaseModel):
    request_id: str
    trigger_step_id: Optional[int] = None
    reason: str
    screenshot_before: str
    screenshot_after: Optional[str] = None
    operator_actions: List[OperatorAction] = Field(default_factory=list)
    resolution: Literal["RESUMED", "ABORTED", "OVERRIDDEN"] = "RESUMED"
    duration_seconds: float = 0.0

class ExecutionResult(BaseModel):
    status: ExecutionStatus
    capability_id: str
    version: str
    data: Optional[Dict[str, Any]] = None
    outcome_code: Optional[str] = None
    outcome_message: Optional[str] = None
    execution_time_ms: float
    steps_executed: int
    interventions: List[InterventionRecord] = Field(default_factory=list)
    evidence_path: Optional[str] = None
    debug_context: Optional[Dict[str, Any]] = None
```

---

## 4. Target Application Architecture (`src/target_app/`)

To guarantee reproducible, safe, and zero-external-dependency execution, we build a local mock banking portal:
- **Framework:** FastAPI + Jinja2 + HTML/CSS.
- **Port:** `8000` (configurable via environment variable).
- **Surface Characteristics:**
  - Designed as an authentic core banking back-office UI.
  - Non-semantic markup: Deep `<table>` structures, legacy form tags, absence of modern test IDs.
  - Realistic latency simulator: Configurable delay to test auto-waiting and transient recovery.
  - Dedicated test accounts:
    - **Active Member (`12345`):** Returns complete account portfolio (Savings `$4,250.75`, Checking `$1,120.50`, active status).
    - **Non-Existent Member (`99999`):** Renders legacy warning banner: *"Warning: Member record not found in system"*.
    - **Locked Account (`67890`):** Renders security alert banner: *"Security Lockout: Account requires manual supervisor clearance"*.
    - **Money Transfer / Sub-Account Creation Flow:** Includes an irreversible action confirmation dialog.

---

## 5. Phased Implementation Roadmap

### Phase 1: Environment & Project Scaffolding
- Initialize project using `uv`.
- Configure `pyproject.toml` with pinned dependencies:
  - `playwright>=1.40.0`
  - `pydantic>=2.5.0`
  - `typer>=0.9.0`
  - `fastapi>=0.104.0`
  - `uvicorn>=0.24.0`
  - `jinja2>=3.1.2`
  - `anthropic>=0.18.0` / `openai>=1.12.0`
  - `python-dotenv>=1.0.0`
  - `pytest>=7.4.0`
  - `rich>=13.7.0`
- Install Playwright Chromium headless/headed binaries.
- Set up directories: `capabilities/`, `evidence/`, `src/`, `tests/`.
- Create `.env.example` and `.gitignore`.

### Phase 2: Mock Banking Target Application (`src/target_app/`)
- Build FastAPI server in `src/target_app/app.py`.
- Create Jinja2 HTML templates simulating legacy banking UI (`src/target_app/templates/`):
  - `login.html`: Staff authentication.
  - `dashboard.html`: Main navigation grid.
  - `member_lookup.html`: Form search with tables and simulated delay.
  - `member_detail.html`: Account balances and ledger.
  - `transfer.html`: Multi-field form with confirmation modal.
- Unit test mock routes to ensure reliable responses for IDs `12345`, `99999`, and `67890`.

### Phase 3: Core Pydantic Contracts & Schema Definition (`src/models/`)
- Define `CapabilityArtifact`, `Step`, `LocatorStrategy` (supporting `frame_selector: Optional[str] = None` for legacy `<frame>`/`<iframe>` boundaries), `Checkpoint`, `BusinessOutcomeMatch`.
- Define `ExecutionResult`, `ExecutionStatus`, `StepLog`.
- Define `InterventionRequest`, `InterventionRecord`, and `OperatorAction` (with action types `click`, `input`, `navigation`, `dialog`).
- Implement serialization/deserialization methods with strict validation.

### Phase 4: Deterministic Replay Engine (`src/engine/`)
- Implement `ReplayExecutor` in `src/engine/executor.py`:
  - Browser context initialization (Playwright async/sync API).
  - Parameter injection engine: replaces `{{key}}` from input dict and dynamically resolves `{{base_url}}` from CLI `--base-url`, tenant overlays, or `TARGET_APP_URL` environment variable (default: `http://127.0.0.1:8000`).
  - Canonical path resolver: automatically prepends base host and tenant route prefix when navigation steps specify relative paths (e.g. `/members`).
  - Multi-tier locator resolution with frame scoping:
    - Search root resolution (`get_search_context()`: returns `page.frame_locator(target.frame_selector)` if framed, else `page`).
    1. Role & accessible name (`root.get_by_role(...)`)
    2. Accessible text / label (`root.get_by_label(...)`, `root.get_by_text(...)`)
    3. CSS selector (`root.locator(...)`)
    4. XPath selector (`root.locator(...)`)
  - Auto-wait, retry loop, and custom timeouts.
  - Action dispatchers: `click`, `fill`, `navigate`, `extract`, `assert`, `wait`.
  - **Irreversible Step Execution Gating:**
    - Evaluates `step.is_irreversible` before dispatching mutating actions.
    - In unattended/headless mode without explicit `--allow-irreversible` authorization: halts safely before execution, captures pre-execution state, and returns `HARD_FAILURE` (`outcome_code: IRREVERSIBLE_ACTION_BLOCKED`).
    - In interactive/headed mode: halts and routes to `EscalationManager` requesting human authorization (`[C] Confirm & commit` / `[A] Abort`) before proceeding.
  - **Multi-Condition State Observation Loop (Race Condition Protection):**
    - Immediately following form submission or before executing any `extract` step, executes a concurrent observation race evaluating `checkpoint.success_condition`, all declared `checkpoint.business_outcomes`, and security escalation alerts.
    - If a business outcome matches (e.g. `MEMBER_NOT_FOUND`): immediately short-circuits execution, sets status to `BUSINESS_OUTCOME`, records outcome code/message, and cleanly skips extraction steps without locator timeout delays.
    - If success condition is confirmed: proceeds to execute `extract` steps on verified DOM elements.
    - If an escalation alert matches: transitions to `EscalationManager` without killing the live session.
    - If timeout elapses: transitions to `HARD_FAILURE` and activates rich failure capture.
  - Output data extraction and structured result compilation upon verified success states.
  - **Automated Rich Failure Capture Seam:** On encountering an unrecoverable locator or assertion error (`HARD_FAILURE`), immediately captures a full-page screenshot (`evidence/failure_*.png`) and dumps the complete DOM HTML snapshot (`evidence/failure_*.html`), injecting the file paths and failure context into `ExecutionResult.evidence_path` and `ExecutionResult.debug_context`.
- Create unit & integration tests against mock app for:
  - Happy path replay (`12345`).
  - Business outcome classification (`99999`).
  - Expected error reporting and rich failure artifact capture.

### Phase 5: Safety Guardrails & PII Redaction (`src/guardrails/`)
- Implement `GuardrailPolicy` in `src/guardrails/policy.py`:
  - **Domain / URL Allowlist:** Blocks requests outside configured domains (`127.0.0.1`, `localhost`, or specified customer domains).
  - **Action Allowlist:** Restricts executable action types.
  - **Irreversible Action Gating Policy:** Defines policy branches for `is_irreversible: true` steps:
    - `BLOCK_UNATTENDED`: Default fail-safe policy preventing unattended headless jobs from committing financial mutations without explicit clearance.
    - `ROUTE_TO_HUMAN`: Escalation prompt presenting transaction summary for operator confirmation.
    - `AUDIT_LOG`: Logs immutable non-repudiation audit record on approved execution.
- Implement `PIIRedactor` in `src/guardrails/redactor.py`:
  - Regex scrubbers for SSNs (`\d{3}-\d{2}-\d{4}`), Credit Card PANs, Account Numbers, and Bearer Tokens.
  - Applied automatically to all log sinks, step inputs, and serialized traces.

### Phase 6: Human-in-the-Loop Escalation, Live Handoff & Action Recording (`src/human/`)
- Implement `EscalationManager` in `src/human/escalation.py`:
  - **Detection & Trigger:** Triggers when a locator fails after all fallbacks, an unexpected barrier/lockout appears, or an irreversible action requires authorization.
  - **Session Freezing:** Pauses automation without closing the Playwright browser/page session, keeping cookies, form state, and DOM intact.
  - **Diagnostic Context:** Captures initial failure screenshot to `evidence/escalation_before_*.png` and initializes `InterventionRecord`.
  - **Active Session Operator Action Recording ("Record What the Human Did"):**
    - Attaches Playwright lifecycle event handlers:
      - `page.on("framenavigated")` &rarr; records page URL changes initiated by the human operator.
      - `page.on("dialog")` &rarr; records alerts/confirms triggered and accepted/dismissed by the operator.
    - Injects a lightweight DOM observer via `page.expose_binding("__recordHumanAction", ...)`:
      - Listens for `click` events (capturing element tag, accessible role/name, text, and selector).
      - Listens for `change` / `input` events (capturing input element identifier and sanitized, PII-scrubbed value).
    - Appends each captured event chronologically into `InterventionRecord.operator_actions`.
  - **Operator Control CLI:** Presents operator with clear failure diagnostics and interactive options:
    `[R] Resume automation` / `[A] Abort execution` / `[M] Mark step complete and proceed`.
  - **Control Re-acquisition & Resume:**
    - Safely detaches Playwright and DOM event listeners.
    - Captures post-intervention screenshot (`evidence/escalation_after_*.png`).
    - Appends completed `InterventionRecord` to `ExecutionResult.interventions`.
    - Re-evaluates page state / checkpoint before cleanly resuming automated execution.

### Phase 7: LLM Discovery Agent Loop & Compiler Seam (`src/agent/`)
- Implement `DOMInspector` in `src/agent/inspector.py`:
  - Injected JavaScript / Playwright introspection utility for live `ElementHandle` resolution.
  - Detects if an element resides inside a `<frame>` or `<iframe>` and resolves its enclosing `frame_selector`.
  - Programmatically derives a verified, 4-tier locator hierarchy directly from the active DOM:
    1. **Role & Accessible Name:** `role:<tag_or_aria>[name='...']` via Playwright accessibility API.
    2. **Accessible Text / Label Anchor:** Associated `<label>`, placeholder, or visible text anchor.
    3. **Structural CSS:** Unique ID (`#id`), unique attribute (`input[name='...']`), or scoped CSS path.
    4. **Stable XPath:** Relative text-contained or structural XPath (`//button[contains(text(), '...')]`).
  - Verifies in real-time that each fallback selector uniquely resolves to the target node on the active page/frame.
- Implement `DiscoveryAgent` in `src/agent/discovery.py`:
  - Visual & accessibility state extractor (dumps compact accessibility tree + interactive element map).
  - System prompt incorporating banking automation context and tool definitions.
  - Clean tool calling interface (LLM specifies high-level target intent without hallucinating CSS/XPath):
    - `navigate(url)`
    - `click(target_description)`
    - `click_coordinate(x, y)` (Fallback for canvas, embedded applets, or surfaces lacking a clean DOM; uses `document.elementFromPoint(x, y)` to resolve DOM node if present)
    - `fill(target_description, value)`
    - `extract(field_name, target_description)`
    - `finish_task(summary, output_fields)`
    - `request_human_help(reason)`
  - Tool execution harness intercepts each action on the live page, uses `DOMInspector` to bind verified `LocatorStrategy` to the step, and executes the action.
  - Run loop with termination bounds (max steps = 15, timeout = 120s).
- Implement `ArtifactCompiler` in `src/agent/compiler.py`:
  - Synthesizes recorded steps into a normalized `CapabilityArtifact`.
  - Canonicalizes navigation URLs (stripping concrete host origins like `http://127.0.0.1:8000` into `{{base_url}}/members` or relative paths `/members` to ensure multi-tenant portability).
  - Extracts parameters (replaces literal inputs like `"12345"` with `{{member_id}}`).
  - Formulates success checkpoint and business outcome assertions.
  - Validates output artifact strictly against `CapabilityArtifact` Pydantic model.

### Phase 8: Typer CLI & Developer Workflow (`src/cli.py`)
- CLI command structure:
  - `python -m src.cli serve-target [--port 8000]`
  - `python -m src.cli discover --goal "Find savings balance for member 12345" --url "http://127.0.0.1:8000/members" --output capabilities/member_lookup.json`
  - `python -m src.cli replay --artifact capabilities/member_lookup.json --params '{"member_id":"12345"}' [--base-url http://127.0.0.1:8000] [--allow-irreversible] [--headed]`
  - `python -m src.cli test-harness` (Executes the complete test matrix).

### Phase 9: End-to-End Evidence Generation (`evidence/`)
- Execute discovery run to generate `capabilities/member_lookup.json`.
- Execute and record evidence files:
  - `evidence/discovery_run.log`: Full discovery transcript demonstrating LLM reasoning, intent-based tool execution, live DOM inspector deriving validated locator hierarchies (Role -> Text -> CSS -> XPath), and the compiled capability artifact.
  - `evidence/replay_happy_path.log`: Replay with `member_id: 12345` confirming success and extracted balance `$4,250.75`.
  - `evidence/replay_business_outcome.log`: Replay with `member_id: 99999` confirming clean detection of `MEMBER_NOT_FOUND` business outcome via the Multi-Condition Observation Loop (bypassing balance extraction cleanly without timing out on `#savings-balance-val`).
  - `evidence/replay_escalation.log`, `evidence/escalation_before.png`, & `evidence/escalation_after.png`: Replay hitting account `67890` or simulated failure, demonstrating live session human takeover, real-time capture and structured logging of operator actions (clicks, inputs, navigations), and successful resumption.
  - `evidence/replay_hard_failure.log`, `evidence/failure_member_lookup.png`, & `evidence/failure_member_lookup.html`: Replay intentionally exercising an invalid locator or broken invariant without escalation, demonstrating clean `HARD_FAILURE` error classification and automatic dumping of rich full-page screenshot and complete DOM snapshot artifacts.
  - `evidence/replay_irreversible_blocked.log`: Replay exercising a funds transfer confirmation in unattended mode without `--allow-irreversible`, demonstrating fail-safe halting and audit trail generation without committing financial mutation.

### Phase 10: Comprehensive Documentation (`README.md` & `REPORT.md`)
- **`README.md`:** Complete installation instructions (with `uv`), environment configuration, command-line demo walk-throughs, and mock server operations.
- **`REPORT.md`:** Thorough technical write-up strictly organized under the 7 mandated headings:
  1. *Architecture*
  2. *Artifact schema*
  3. *Determinism & error handling*
  4. *Heterogeneity & multi-tenant*
  5. *Escalation & handoff*
  6. *Safety*
  7. *Cuts*

---

## 6. Heterogeneity & Multi-Tenant Architectural Design

To satisfy Section 3.7 and Section 6.2 of the Assignment Brief, the design explicitly decouples surface perception from flow orchestration:

### 6.1 Surface Abstraction Layer (`SurfaceAdapter` Protocol)
The execution engine communicates with target applications via an abstract interface:
```python
class SurfaceAdapter(Protocol):
    async def navigate(self, url: str) -> None: ...
    async def find_element(self, strategy: LocatorStrategy) -> Any: ...
    async def click(self, target: Any) -> None: ...
    async def type_text(self, target: Any, text: str) -> None: ...
    async def get_text(self, target: Any) -> str: ...
    async def get_state_snapshot(self) -> Dict[str, Any]: ...
```
- **Web Adapter (`PlaywrightAdapter`):** Implements modern DOM, accessibility tree, and frame traversal.
- **Legacy Web Adapter (`LegacyWebAdapter`):** Handles legacy framesets, nested `<iframe>` hierarchies, and window popups by binding `LocatorStrategy.frame_selector` to Playwright's `frame_locator` API, cleanly scoping locator evaluation within cross-frame DOM trees.
- **Desktop Adapter (`DesktopOSAdapter` - architectural design):** Maps the same `Step` definitions to OS Accessibility APIs (Windows UI Automation via `pywinauto` or macOS Accessibility via `AXUIElement`) or coordinate-anchored OCR.

### 6.2 Multi-Tenant Capability Inheritance & Overrides
In banking multi-tenancy, hundreds of institutions run the same core vendor system (e.g., FIS, Fiserv, Jack Henry) with custom branding, slightly altered forms, or differing URL paths.
- **Base Artifact:** Defines the canonical workflow with parameterized host and routes (e.g., `"value": "{{base_url}}/members"`).
- **Tenant Overlay Layer:** A sparse JSON overlay mapping tenant-specific base hosts, route prefixes, and locator adjustments:
  ```json
  {
    "tenant_id": "cu_metro_ny",
    "base_artifact": "fis_member_lookup@1.0.0",
    "base_url": "https://ny.cu-core.internal:8443",
    "route_prefix": "/portal/ny",
    "locator_overrides": {
      "step_2": {
        "primary": "role:textbox[name='Member Account #']"
      }
    }
  }
  ```
- **Dynamic URL Composition:** The replay engine evaluates canonical navigation steps by prepending the tenant's `base_url` and `route_prefix` (e.g., resolving to `https://ny.cu-core.internal:8443/portal/ny/members`), completely eliminating hardcoded host or port assumptions.
- **Version & Drift Detection:** During replay, if primary locators consistently fail over $N$ runs and trigger fallbacks or human escalation, the system flags the artifact for drift review and recommends an automated re-discovery diff.

---

## 7. Deliverables Checklist & Acceptance Criteria

| Deliverable | Requirement Specification | Verification Criteria |
| :--- | :--- | :--- |
| **Source Code** | Clean, typed Python 3.11+ codebase in Git repository | `pytest` passes; strict typing with Pydantic; zero lingering secrets. |
| **`README.md`** | Setup instructions + exact demo commands | A fresh clone can run `uv sync`, launch target app, and execute discovery + replay. |
| **`REPORT.md`** | 1–3 page write-up with 7 exact mandatory headings | Fully articulated engineering decisions, trade-offs, and cut lines. |
| **Capability Artifact** | Typed, versioned JSON in `capabilities/` | Validated by `CapabilityArtifact` schema; clean parameters & fallbacks. |
| **Evidence Files** | Real discovery & replay logs in `evidence/` | Logs demonstrate actual discovery, happy replay, business outcome, escalation, and rich failure signals (screenshot + DOM HTML dump) on `HARD_FAILURE`. |
| **Error Taxonomy** | Explicit separation of outcomes | Clean differentiation between `SUCCESS`, `BUSINESS_OUTCOME`, `RECOVERABLE_ERROR`, and `HARD_FAILURE`. |
| **Human Escalation & Action Recording** | Live session pause/resume seam & operator action tracking | Automation pauses without killing session, allows operator intervention, captures user actions (clicks/inputs/navigation) via Playwright/DOM listeners into audit logs, and resumes safely. |
| **Safety Guardrails** | Allowlist enforcement + PII redaction | Target domain restriction; no sensitive data persisted in logs or artifacts. |

---

## 8. Next Steps & Approval Gate

Upon review and approval of this plan:
1. Initialize the project scaffolding using `uv`.
2. Construct the local mock banking portal (`src/target_app/`).
3. Implement the Pydantic contracts and replay engine (`src/models/`, `src/engine/`).
4. Implement guardrails and human escalation (`src/guardrails/`, `src/human/`).
5. Wire up the LLM discovery agent (`src/agent/`).
6. Run the end-to-end demonstrations to produce all required evidence in `evidence/`.
7. Author `README.md` and `REPORT.md`.
