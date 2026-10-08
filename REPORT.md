# Engineering Report: Computer-Use Automation System

**Project:** Computer-Use Automation System for Legacy Banking Applications
**System Repository:** `legacy-ui-replay-engine`  
**Author:** Chaitrali Brahme  

---

## 1. Architecture: Key Decisions and Trade-offs

### 1.1 Core Architecture: "Record-Once, Replay-Many"
Financial institutions rely on legacy back-office software that lack modern APIs. Driving these applications using an LLM in the loop for every production transaction is slow, expensive, non-deterministic, and vulnerable to hallucinations.

This system decouples UI exploration from production execution via a two-phase architecture:
1. **Discovery Phase (LLM-Driven Exploration with Live Interception):** An autonomous discovery agent ([`DiscoveryAgent`](src/agent/discovery.py)) takes a natural language goal, inspects interactive UI elements, reasons through workflows, and issues tool actions. The browser harness intercepts each live interaction, inspects the active DOM tree via [`DOMInspector`](src/agent/inspector.py), and programmatically derives a resilient 4-tier locator hierarchy (Accessibility Role and Name -> Text Anchor -> Scoped CSS -> Canonical XPath).
2. **Compilation Phase:** The [`ArtifactCompiler`](src/agent/compiler.py) parameterizes dynamic literals (e.g., converting `"12345"` into `{{member_id}}` and `http://127.0.0.1:8000` into `{{base_url}}`), binds verified checkpoint conditions, and compiles the trajectory into a typed [`CapabilityArtifact`](src/models/artifact.py) JSON schema.
3. **Deterministic Replay Phase (Zero LLM Inference):** Production invocations execute through [`ReplayExecutor`](src/engine/executor.py) using pure Playwright automation with zero model inference. Replay utilizes accessibility locators, parameter templating, a concurrent multi-condition state race, and an explicit error taxonomy.
4. **Human Escalation Seam:** When the system encounters security barriers, unexpected account lockouts, or mutating steps, [`EscalationManager`](src/human/escalation.py) freezes the active browser session, presents diagnostic context, attaches real-time DOM observers and Playwright event listeners to record operator actions, and resumes automation on the same session.

```
+-----------------------------------------------------------------------------------+
|                                  Typer CLI / Agent                                |
|                     (discover, replay, serve-target, test-harness)                |
+-------------------------+---------------------------------+-----------------------+
                          |                                 |
                          v                                 v
        +-----------------------------------+    +----------------------------------+
        |  Discovery Agent (LLM in Loop)    |    | Replay Executor (Zero LLM)       |
        |  - Playwright browser context     |    | - Pure Playwright automation     |
        |  - Live DOM element inspection    |    | - Multi-tier locator resolution  |
        |  - GPT-4o observe-decide-act loop |    | - Pre-extraction outcome race    |
        +-----------------+-----------------+    +-----------------+----------------+
                          |                                 ^
             Compiles to  |                                 | Reads artifact
                          v                                 |
                 +-----------------+                        |
                 | Capability      |========================+
                 | Artifact (JSON) |
                 +-----------------+
```

### 1.2 Key Architectural Decisions and Trade-offs

* **Programmatic DOM Inspector vs. LLM-Generated Selectors:**
  * *Decision:* When the LLM decides to interact with an element (e.g. clicking "Search"), the browser harness intercepts the live `ElementHandle` and computes verified locators directly against the live DOM and accessibility tree.
  * *Trade-off:* Adds inspection logic during discovery, but completely eliminates fragile LLM-hallucinated CSS selectors and arbitrary XPath expressions.

* **Single-Process Async Runtime vs. Distributed Task Infrastructure:**
  * *Decision:* Built as a lightweight, typed Python 3.11 async engine driven by Typer CLI and Playwright, without distributed worker queues (Celery, Kafka, Redis).
  * *Trade-off:* Avoids premature infrastructure complexity, ensuring full end-to-end reproducibility, zero database dependencies, and sub-second test execution.

* **Flat JSON Artifact Catalog vs. Database Storage:**
  * *Decision:* Capabilities are stored as versioned JSON files in [`capabilities/`](capabilities/).
  * *Trade-off:* Enables code reviews in Git pull requests, strict schema enforcement via Pydantic v2, and effortless tenant deployment without relational database migrations.

---

## 2. Artifact Schema: Structure and Design Rationale

The capability artifact contract is defined in [`src/models/artifact.py`](src/models/artifact.py) as [`CapabilityArtifact`](src/models/artifact.py). It serves as a typed, versioned, and reviewable specification of a repeatable workflow.

```json
{
  "id": "member_balance_lookup",
  "name": "Member Savings Balance Lookup",
  "version": "1.0.0",
  "description": "Searches for a credit union member by ID and extracts their savings balance.",
  "target_path": "/members",
  "input_schema": {
    "member_id": {
      "type": "string",
      "description": "Unique 5-digit member account identifier",
      "example": "12345"
    },
    "base_url": {
      "type": "string",
      "description": "Base host origin",
      "default": "http://127.0.0.1:8000"
    }
  },
  "output_schema": {
    "savings_balance": { "type": "string" }
  },
  "steps": [
    {
      "step_id": 1,
      "action": "navigate",
      "value": "{{base_url}}/members",
      "timeout_ms": 10000
    },
    {
      "step_id": 2,
      "action": "fill",
      "target": {
        "primary": "role:textbox[name='Member Search ID']",
        "fallbacks": ["#member_id", "input[name='member_id']", "//*[@id='member_id']"],
        "rationale": "Accessible role and name with fallback to input ID"
      },
      "value": "{{member_id}}",
      "timeout_ms": 5000
    },
    {
      "step_id": 3,
      "action": "click",
      "target": {
        "primary": "role:button[name='Search Records']",
        "fallbacks": ["text:Search Records", "#search-button", "button.btn", "//*[@id='search-button']"],
        "rationale": "Search button in legacy form table"
      },
      "value": null,
      "timeout_ms": 5000
    },
    {
      "step_id": 4,
      "action": "extract",
      "target": {
        "primary": "role:table",
        "fallbacks": ["#accounts-table", "table.legacy-table", "//*[@id='accounts-table']"],
        "rationale": "Extract balance from core accounts ledger"
      },
      "field_name": "savings_balance",
      "timeout_ms": 5000
    }
  ],
  "checkpoint": {
    "success_condition": {
      "type": "element_visible",
      "target": "#accounts-table",
      "timeout_ms": 5000
    },
    "business_outcomes": [
      {
        "code": "MEMBER_NOT_FOUND",
        "match_type": "element_text_contains",
        "target": "#not-found-alert",
        "pattern": "not found in core system|not found in system",
        "description": "Member record does not exist in core banking system"
      },
      {
        "code": "FRAUD_HOLD",
        "match_type": "element_text_contains",
        "target": "#account-locked-alert",
        "pattern": "Security Lockout|locked due to suspected security flag",
        "description": "Account is locked under fraud investigation"
      }
    ]
  }
}
```

### Rationale Behind Schema Shape
1. **Decoupled from LLM Transcripts:** Prompts, system messages, and reasoning traces are discarded upon compilation. The resulting schema is an autonomous execution spec.
2. **Multi-Tier Locator Strategies:** The `target` object encapsulates a hierarchy:
   * *Tier 1 (Accessible Role/Name):* `role:textbox[name='Member Search ID']` is resilient to stylesheet and markup refactors.
   * *Tier 2 (Text/Label Anchors):* Anchors elements by visible human text.
   * *Tier 3 (Scoped CSS):* Element IDs and form-scoped classes.
   * *Tier 4 (Structural XPath):* Fallback tree navigation.
   * *Frame Scoping:* `frame_selector` attribute isolates resolution within nested `<iframe>` hierarchies.
3. **Parameter Injection:** The `{{base_url}}` parameter prevents hardcoding environments, enabling deployment across staging, testing, and production hosts.
4. **Explicit Terminal Checkpoints:** Separates action submission from state verification, guaranteeing that data extraction only occurs on confirmed screens.

---

## 3. Determinism & Error Handling: Runtime Reliability and Edge Cases

### 3.1 Eliminating Flakiness in Replay
Deterministic replay executes without model inference. Determinism is enforced through:
* **Playwright Auto-Waiting:** Built-in assertions for actionability (visible, attached, stable bounding box, enabled) before dispatching clicks or fills.
* **Deterministic Fallback Cascading:** If a primary locator fails to resolve within its timeout budget, the engine automatically attempts fallback locators in priority order before reporting an error.
* **Bounded Timeout Budgets:** Steps specify individual timeout limits (typically 5,000ms), preventing runaway execution threads.

### 3.2 Pre-Extraction Multi-Condition State Observer
A common failure in legacy web automation occurs when an application branches into an error or warning screen after form submission. If an automation engine blindly executes an extraction step (e.g. querying `#savings-balance-val`), it will wait for the locator to time out before reporting an error.

To solve this, the engine executes a concurrent state race prior to extraction:
1. Concurrently evaluates `checkpoint.success_condition` against all declared `checkpoint.business_outcomes`.
2. If a business outcome matches (e.g., `#not-found-alert` displaying "Member record not found"), execution short-circuits immediately in ~300ms, bypassing extraction steps.
3. If an escalation trigger matches (e.g., `#account-locked-alert`), the session routes to the human operator seam.
4. If the success condition resolves, execution proceeds to extraction.

### 3.3 Four-Tier Result Taxonomy
Execution outcomes are strictly categorized in [`src/models/result.py`](src/models/result.py):
1. **`SUCCESS`:** Target state reached, checkpoints passed, output fields extracted.
2. **`BUSINESS_OUTCOME`:** Expected domain outcomes (e.g. `MEMBER_NOT_FOUND`). This represents valid institutional data, not an automation error.
3. **`RECOVERABLE_ERROR`:** Transient latency, modal dismissals, or retryable network hiccups.
4. **`HARD_FAILURE`:** Exhausted locators, invariant breaches, or security halts.

### 3.4 Automated Rich Failure Signal Generation
When an unhandled `HARD_FAILURE` occurs, [`_capture_failure_artifacts()`](src/engine/executor.py) triggers automatically:
* Captures a full-page PNG screenshot to `evidence/failure_*.png`.
* Serializes and PII-sanitizes the full DOM HTML snapshot to `evidence/failure_*.html`.
* Attaches diagnostic artifact paths and locator resolution logs to [`ExecutionResult.debug_context`](src/models/result.py).

---

## 4. Heterogeneity & Multi-Tenant: Generalization and Scale

### 4.1 Surface Abstraction Layer
Legacy banking surfaces span modern single-page applications, server-rendered portals with framesets, and native Windows desktop apps. To decouple flow logic from surface rendering, the architecture defines a common `SurfaceAdapter` protocol:

```python
class SurfaceAdapter(Protocol):
    async def navigate(self, url: str) -> None: ...
    async def find_element(self, strategy: LocatorStrategy) -> Any: ...
    async def click(self, target: Any) -> None: ...
    async def type_text(self, target: Any, text: str) -> None: ...
    async def get_text(self, target: Any) -> str: ...
    async def get_state_snapshot(self) -> Dict[str, Any]: ...
```

* **Web Adapter (`PlaywrightAdapter`):** Implemented using Playwright's DOM and Accessibility Tree APIs.
* **Legacy Frameset Adapter (`LegacyWebAdapter`):** Extends the web adapter by scoping actions using `LocatorStrategy.frame_selector` via `page.frame_locator(...)`, isolating nested frames.
* **Desktop OS Adapter (`DesktopOSAdapter` - Architecture Design):** Maps `Step` actions to native OS accessibility APIs (UI Automation on Windows via `pywinauto`, macOS Accessibility via `AXUIElement`). Control targets resolve via `Name`, `AutomationId`, or OCR coordinate anchors, leaving flow orchestration unchanged.

### 4.2 Multi-Tenant Generalization and Tenant Overlays
In enterprise banking, hundreds of credit unions run identical vendor core platforms (e.g. FIS, Fiserv, Jack Henry) customized with institution-specific branding, routing prefixes, and field labels. Rather than re-recording capabilities for every tenant, the architecture uses a Base Artifact plus Tenant Overlay pattern:

1. **Canonical Base Artifact:** Defines the vendor core workflow with parameterized paths and generic selectors.
2. **Sparse Tenant Overlay:** A configuration file overriding institution-specific parameters:
   ```json
   {
     "tenant_id": "first_federal_cu",
     "base_artifact": "fis_member_lookup@1.0.0",
     "base_url": "https://core.firstfedcu.internal:8443",
     "route_prefix": "/servicing/v2",
     "locator_overrides": {
       "step_2": {
         "primary": "role:textbox[name='Member Account #']"
       }
     }
   }
   ```
3. **Drift Detection:** If fallback locators are triggered over N consecutive runs, the engine logs a telemetry alert flagging UI drift, recommending a diff review before failure occurs.

---

## 5. Escalation & Handoff: Detection, Control Transfer, and Resumption

### 5.1 Triggering Human Intervention
Human escalation is triggered when automation cannot safely proceed:
* Unresolved locator exhaustion after exhausting all fallbacks.
* Detection of flagged security states (e.g., `FRAUD_HOLD` alert).
* Execution of an irreversible step (`is_irreversible: true`) requiring human clearance.

### 5.2 Control Transfer Seam
Rather than terminating the process or spawning a new browser, [`EscalationManager`](src/human/escalation.py) preserves the live execution context:
1. **Automation Freeze:** Execution pauses on the active Playwright page without closing browser connections.
2. **Context Packet:** Creates an [`InterventionRequest`](src/models/human.py) with request ID, trigger step, reason, current URL, and an initial screenshot saved to `evidence/escalation_before_*.png`.
3. **Live Action Recording:** Injects a lightweight DOM observer script (`TRACKER_JS`) via `page.expose_binding('__recordHumanAction')` and attaches Playwright lifecycle listeners (`framenavigated`, `dialog`).
4. **Operator Interaction:** The supervisor interacts with the page (e.g., clicking "Authorize Supervisor Override" or entering authorization credentials). Clicks, inputs, dialogs, and navigations are captured in real time.
5. **PII Sanitization:** All operator inputs pass through [`PIIRedactor`](src/guardrails/redactor.py), redacting SSNs, account numbers, and tokens.
6. **Resumption:** The operator signals completion via CLI prompt (`[R] Resume`, `[A] Abort`, `[M] Mark Step Complete`). The engine detaches listeners, captures a post-intervention screenshot (`evidence/escalation_after_*.png`), verifies state clearance, and seamlessly resumes automated execution.

---

## 6. Safety: Guardrail Model, Action Gating, and Limitations

### 6.1 Safety Guardrails
The safety layer in [`src/guardrails/`](src/guardrails/) enforces institutional security policies across both discovery and replay:

1. **Strict URL / Domain Allowlist:**
   * [`GuardrailPolicy.validate_url()`](src/guardrails/policy.py) parses destination targets against approved origins (`127.0.0.1`, `localhost`, tenant domains).
   * Prevents open-redirect attacks and out-of-scope navigation.
2. **Action Allowlist:**
   * [`GuardrailPolicy.validate_action()`](src/guardrails/policy.py) restricts executable actions to verified primitives (`NAVIGATE`, `CLICK`, `FILL`, `EXTRACT`, `ASSERT`, `WAIT`).
   * Arbitrary script evaluation (`evaluate`, `eval`) is strictly prohibited during replay.
3. **Irreversible Action Policy Gate:**
   * Steps marked `is_irreversible: true` (e.g. fund disbursements, account closures) cannot execute unattended.
   * Under headless execution without the `--allow-irreversible` flag, the engine halts prior to dispatching the action, saves pre-execution diagnostic state, and returns `HARD_FAILURE` (`IRREVERSIBLE_ACTION_BLOCKED`).
   * In headed or interactive mode, execution routes to [`EscalationManager`](src/human/escalation.py) for supervisor approval.

### 6.2 Regulated Data Protection (PII Scrubbing)
Financial automation handles non-public personal information (NPI). The [`PIIRedactor`](src/guardrails/redactor.py) runs across all logging sinks, step execution traces, extracted dictionaries, and DOM dumps:
* Redacts SSNs (`\d{3}-\d{2}-\d{4}` -> `[REDACTED_SSN]`).
* Redacts Account Numbers (`CHK-...`, `SAV-...`, `MM-...` -> `[REDACTED_ACCOUNT]`).
* Redacts Credit Card PANs (`\b(?:\d{4}[ -]?){3}\d{4}\b` -> `[REDACTED_CARD]`).
* Redacts Bearer tokens and API secrets.

### 6.3 Security Boundaries and Limitations
* **Client-Side Redaction Scope:** Scrubbing applies to automation logs and serialized artifacts; it cannot prevent the target banking server itself from logging raw inputs.
* **Dynamic Pattern Drift:** Regex-based PII scrubbers require configuration updates when institutional core banking systems introduce novel account number formats.

---

## 7. Cuts: Scoping Decisions and Future Roadmap

### 7.1 What Was Deliberately Left Out (and Rationale)
* **Real-Time WebRTC Co-Browsing GUI Console:**
  * *Rationale:* I prioritized the core control-transfer seam: live session freezing, DOM action observation hooks, PII redaction, and CLI handoff.
* **Native Desktop OS Automation Engine:**
  * *Rationale:* Real enterprise back-office surfaces include Windows Thick Clients. While I designed the `SurfaceAdapter` architecture and `frame_selector` contract, building full Windows UI Automation drivers was cut in favor of deep web reliability.
* **Distributed Task Queue Infrastructure (Celery, RabbitMQ, Kafka):**
  * *Rationale:* Enterprise architectures require distributed execution pools, but introducing Redis/Celery plumbing locally adds operational friction without improving the core automation primitives.
* **Unbounded Open-Ended LLM Self-Healing on Replay:**
  * *Rationale:* Allowing an LLM to take over when replay fails introduces non-determinism, hallucinations, and unbudgeted latency into production. Replay strictly favors deterministic fallbacks and clean human escalation.

### 7.2 What to Build Next
1. **Agent-Facing Callable Capability Catalog:** Expose saved artifacts as callable tools (OpenAI Function Calling schema or MCP tool definitions) that master agents can discover and invoke on demand.
2. **Automated Bounded Drift Recovery:** If a primary locator fails repeatedly over multiple runs, trigger a bounded, single-step LLM discovery session to refresh the artifact locators via Git PR, without altering production flow.
3. **Visual Regression and OCR Anchoring:** Add perceptual layout diffing to detect visual displacement in legacy software lacking reliable accessibility labels.
