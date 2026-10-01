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

### 📊 Project Progress Tracker

| Phase | Description | Status | Evidence / Validation |
| :--- | :--- | :---: | :--- |
| **Phase 1** | Environment & Project Scaffolding | `[x] COMPLETED` | `uv`, Python 3.11, Playwright, `.env`, `.gitignore` configured |
| **Phase 2** | Mock Banking Target Application | `[x] COMPLETED` | [`src/target_app/`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/target_app/) (Jinja2 templates, members `12345`, `99999`, `67890`) |
| **Phase 3** | Core Pydantic Contracts & Schema | `[x] COMPLETED` | [`src/models/`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/models/) (`CapabilityArtifact`, `ExecutionResult`, `InterventionRecord`) |
| **Phase 4** | Deterministic Replay Engine | `[x] COMPLETED` | [`src/engine/executor.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/engine/executor.py), 83% coverage, 24 unit/integration tests passing |
| **Phase 5** | Safety Guardrails & PII Redaction | `[x] COMPLETED` | [`src/guardrails/`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/guardrails/) (Domain allowlist, action gating, PII scrubber, 95% coverage, 14 tests) |
| **Phase 6** | Human Escalation & Action Recording | `[x] COMPLETED` | [`src/human/escalation.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/human/escalation.py), 83% coverage, 12 tests, live DOM & Playwright action recording |
| **Phase 7** | LLM Discovery Agent Loop & Compiler | `[x] COMPLETED` | [`src/agent/`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/agent/) (`DOMInspector`, `DiscoveryAgent`, `ArtifactCompiler`, 91% coverage, 15 tests) |
| **Phase 8** | Typer CLI & Developer Workflow | `[x] COMPLETED` | [`src/cli.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/cli.py) (`serve-target`, `discover`, `replay`, `test-harness`, 85% coverage, 11 tests) |
| **Phase 9** | End-to-End Evidence Generation | `[ ] PENDING` | Logs, screenshots, and DOM snapshots in `evidence/` |
| **Phase 10** | Comprehensive Documentation | `[ ] PENDING` | `README.md` & `REPORT.md` (7 mandated sections) |

---

### Phase 1: Environment & Project Scaffolding
- [x] Initialize project using `uv`.
- [x] Configure `pyproject.toml` with pinned dependencies (`playwright`, `pydantic`, `typer`, `fastapi`, `uvicorn`, `jinja2`, `openai`, `python-dotenv`, `pytest`, `pytest-cov`, `rich`).
- [x] Install Playwright Chromium headless/headed binaries.
- [x] Set up directories: `capabilities/`, `evidence/`, `src/`, `tests/`.
- [x] Create `.env.example` and `.gitignore`.

### Phase 2: Mock Banking Target Application (`src/target_app/`)
- [x] Build FastAPI server in `src/target_app/app.py`.
- [x] Create Jinja2 HTML templates simulating legacy banking UI (`src/target_app/templates/`):
  - [x] `dashboard.html`: Main navigation grid.
  - [x] `member_search.html`: Form search with tables and simulated delay.
  - [x] `member_detail.html`: Account balances and ledger.
  - [x] `transfers.html`: Multi-field form with confirmation modal.
  - [x] `admin.html`: System configuration and tenant overlays.
- [x] Unit test mock routes to ensure reliable responses for IDs `12345`, `99999`, and `67890`.

### Phase 3: Core Pydantic Contracts & Schema Definition (`src/models/`)
- [x] Define `CapabilityArtifact`, `Step`, `LocatorStrategy` (supporting `frame_selector: Optional[str] = None` for legacy `<frame>`/`<iframe>` boundaries), `Checkpoint`, `BusinessOutcomeMatch` in [`src/models/artifact.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/models/artifact.py).
- [x] Define `ExecutionResult`, `ExecutionStatus`, `StepLog` in [`src/models/result.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/models/result.py).
- [x] Define `InterventionRequest`, `InterventionRecord`, and `OperatorAction` in [`src/models/human.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/models/human.py).
- [x] Implement serialization/deserialization methods with strict validation.

### Phase 4: Deterministic Replay Engine (`src/engine/`)
- [x] Implement `ReplayExecutor` in [`src/engine/executor.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/engine/executor.py):
  - [x] Browser context initialization (Playwright async API).
  - [x] Parameter injection engine: replaces `{{key}}` and dynamically resolves `{{base_url}}` (from inputs, defaults, or environment).
  - [x] Canonical path resolver: automatically prepends base host when navigation steps specify relative paths (`/members`).
  - [x] Multi-tier locator resolution with frame scoping (`page.frame_locator`) across Role &rarr; Label &rarr; Text &rarr; CSS &rarr; XPath with strict timeout budgeting.
  - [x] Action dispatchers: `click`, `fill`, `navigate`, `extract`, `assert`, `wait`, `click_coordinate`.
  - [x] **Irreversible Step Execution Gating:** Safe halt and failure capture when `is_irreversible: true` without `allow_irreversible`.
  - [x] **Pre-Extraction Multi-Condition State Observation Loop:** Concurrently races success checkpoints against declared business outcomes (`MEMBER_NOT_FOUND`, `FRAUD_HOLD`) to short-circuit immediately without locator timeout delays.
  - [x] Structured output extraction and result compilation upon verified success states.
  - [x] **Automated Rich Failure Capture Seam:** Dumps full-page screenshots (`evidence/failure_*.png`) and DOM HTML snapshots (`evidence/failure_*.html`) on `HARD_FAILURE`.
- [x] Create comprehensive test suite in [`tests/test_replay_executor.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/tests/test_replay_executor.py) & isolation fixture in [`tests/conftest.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/tests/conftest.py).
- [x] Comprehensive test coverage with unit and integration test suite.

### Phase 5: Safety Guardrails & PII Redaction (`src/guardrails/`)
- [x] Implement `GuardrailPolicy` in [`src/guardrails/policy.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/guardrails/policy.py):
  - [x] **Domain / URL Allowlist:** Blocks requests outside configured domains (`127.0.0.1`, `localhost`, or specified customer domains/wildcards).
  - [x] **Action Allowlist:** Restricts executable action types (`NAVIGATE`, `CLICK`, `FILL`, `EXTRACT`, etc.).
  - [x] **Irreversible Action Gating Policy:** Defines policy branches for `is_irreversible: true` steps (`BLOCK_UNATTENDED`, `ROUTE_TO_HUMAN`, `AUDIT_LOG`).
- [x] Implement `PIIRedactor` in [`src/guardrails/redactor.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/guardrails/redactor.py):
  - [x] Regex scrubbers for SSNs (`\d{3}-\d{2}-\d{4}`), Credit Card PANs, Account Numbers (`CHK-...`, `SAV-...`), Secret Bearer/API Tokens, and Emails.
  - [x] Applied automatically to all log sinks, step inputs, extracted data dictionaries, and serialized traces.
- [x] Comprehensive unit and integration test suite in [`tests/test_guardrails.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/tests/test_guardrails.py).
- [x] Comprehensive test coverage with unit and integration test suite.

### Phase 6: Human-in-the-Loop Escalation, Live Handoff & Action Recording (`src/human/`)
- [x] Implement `EscalationManager` in `src/human/escalation.py`:
  - [x] **Detection & Trigger:** Triggers on locator exhaustion, unexpected barriers/lockout, or required authorization.
  - [x] **Session Freezing:** Pauses automation without closing the Playwright browser/page session.
  - [x] **Diagnostic Context:** Captures initial failure screenshot to `evidence/escalation_before_*.png`.
  - [x] **Active Session Operator Action Recording:** Attaches Playwright lifecycle event handlers (`framenavigated`, `dialog`) and injects DOM observer (`__recordHumanAction`) capturing operator clicks and inputs.
  - [x] **Operator Control CLI:** Interactive prompts (`[R] Resume`, `[A] Abort`, `[M] Mark step complete`).
  - [x] **Control Re-acquisition & Resume:** Detaches listeners, captures post-intervention screenshot (`evidence/escalation_after_*.png`), and cleanly resumes automation.
- [x] Integration tests demonstrating seamless handoff and operator action recording.

### Phase 7: LLM Discovery Agent Loop & Compiler Seam (`src/agent/`)
- [x] Implement `DOMInspector` in [`src/agent/inspector.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/agent/inspector.py):
  - [x] Injected Playwright introspection utility for live `ElementHandle` resolution.
  - [x] Programmatically derives and validates a verified 4-tier locator hierarchy (Role &rarr; Label/Text &rarr; Scoped CSS &rarr; Stable XPath) with frame boundary detection.
- [x] Implement `DiscoveryAgent` in [`src/agent/discovery.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/agent/discovery.py):
  - [x] Compact accessibility tree and interactive element map extraction.
  - [x] Tool calling interface (`navigate`, `click`, `fill`, `extract`, `finish_goal`).
  - [x] Live execution interception binding verified `LocatorStrategy` to recorded steps.
- [x] Implement `ArtifactCompiler` in [`src/agent/compiler.py`](file:///Users/chaitralibrahme/Desktop/Projects/Interface%20AI%20Project/src/agent/compiler.py):
  - [x] Normalizes trajectory into valid `CapabilityArtifact`.
  - [x] Parameterizes inputs (`12345` &rarr; `{{member_id}}`).
  - [x] Emits validated capability JSON schemas.

### Phase 8: Typer CLI & Developer Workflow (`src/cli.py`)
- [x] Implement Typer CLI in `src/cli.py`:
  - [x] `python -m src.cli serve-target [--port 8000]`
  - [x] `python -m src.cli discover --goal "..." --url "..." --output ...`
  - [x] `python -m src.cli replay --artifact ... --params ...`
  - [x] `python -m src.cli test-harness`

### Phase 9: End-to-End Evidence Generation (`evidence/`)
- [ ] Generate `evidence/discovery_run.log`.
- [ ] Generate `evidence/replay_happy_path.log`.
- [ ] Generate `evidence/replay_business_outcome.log`.
- [ ] Generate `evidence/replay_escalation.log`, `evidence/escalation_before.png`, `evidence/escalation_after.png`.
- [ ] Generate `evidence/replay_hard_failure.log`, `evidence/failure_*.png`, `evidence/failure_*.html`.
- [ ] Generate `evidence/replay_irreversible_blocked.log`.

### Phase 10: Comprehensive Documentation (`README.md` & `REPORT.md`)
- [ ] Author `README.md` with complete installation, architecture summary, and CLI usage.
- [ ] Author `REPORT.md` answering the 7 required engineering specification sections.

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
