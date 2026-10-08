# Engineering Report: Computer-Use Automation System

**Project:** Computer-Use Automation System for Legacy Banking Applications  
**System Repository:** `legacy-ui-replay-engine`  
**Author:** Chaitrali Brahme  

---

## 1. Architecture: Key Decisions and Trade-offs

### 1.1 Core Architecture: "Record-Once, Replay-Many"
Many banks and credit unions still run on legacy back-office software that lacks modern APIs. If we asked an LLM to look at the screen and decide what to click for every single production transaction, it would be far too slow, expensive, and unpredictable for real banking operations.

To solve this, I built a "record-once, replay-many" system that separates learning a workflow from running it in production:

1. **Discovery Phase (LLM in the loop):** When learning a new task, an autonomous agent ([`DiscoveryAgent`](src/agent/discovery.py)) explores the live webpage to figure out how to reach the goal. As it interacts with the page, my browser harness inspects the live elements using [`DOMInspector`](src/agent/inspector.py) and automatically builds a stable, 4-tier locator hierarchy (Accessibility Role and Name -> Text Anchor -> CSS ID/Class -> XPath).
2. **Compilation Phase:** The [`ArtifactCompiler`](src/agent/compiler.py) turns that discovery trajectory into a clean, reusable JSON artifact ([`CapabilityArtifact`](src/models/artifact.py)). It replaces hardcoded values like `"12345"` with parameters like `{{member_id}}` and records checkpoints to verify success.
3. **Deterministic Replay Phase (Zero LLM):** When running in production, [`ReplayExecutor`](src/engine/executor.py) executes the saved JSON artifact using Playwright with zero LLM calls. It is fast, consistent, and cheap.
4. **Human Escalation Seam:** If the automation hits an unexpected blocker (such as an account fraud lock or an irreversible action), [`EscalationManager`](src/human/escalation.py) pauses the live browser session, lets a human operator take over to resolve it, records what the human did, and resumes automation on that same session.

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

* **Automatic DOM Inspection vs. Asking the LLM for Selectors:**
  * *My Decision:* When the LLM decides to interact with an element (for example, clicking "Search Records"), my code intercepts the live element handle and computes verified locators directly against the live DOM and accessibility tree.
  * *Trade-off:* This adds a brief inspection step during discovery, but it completely eliminates the problem of LLMs hallucinating fragile CSS selectors or broken XPath expressions.

* **Single-Process Async Runtime vs. Distributed Task Infrastructure:**
  * *My Decision:* I built the system as a clean Python 3.11 async engine driven by Typer CLI and Playwright, without distributed worker queues like Celery or Kafka.
  * *Trade-off:* This avoids unnecessary infrastructure complexity, keeping the project easy to run, fully reproducible locally, and allowing the test suite to finish in seconds.

* **Flat JSON Artifact Files vs. A Database:**
  * *My Decision:* I chose to store capabilities as versioned JSON files in [`capabilities/`](capabilities/) rather than in a relational database.
  * *Trade-off:* This lets developers review, diff, and approve workflows directly in Git pull requests, with strict validation via Pydantic v2 and zero database migration overhead.

---

## 2. Artifact Schema: Structure and Design Rationale

I designed the capability artifact contract in [`src/models/artifact.py`](src/models/artifact.py) using Pydantic v2. It acts as a clear, typed specification for any automated task.

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

### Why I Shaped the Schema This Way
1. **Completely Separated from LLM Transcripts:** Once discovery is complete, I discard the conversational prompt history and model tokens. The artifact only keeps the clean steps, inputs, and outputs needed to execute the workflow.
2. **Multi-Tier Locator Fallbacks:** Web pages change slightly over time. For each target element, I store:
   * *Tier 1 (Accessible Role and Name):* e.g. `role:textbox[name='Member Search ID']`. This is the most resilient because accessibility names rarely change even when page styling changes.
   * *Tier 2 (Text Anchors):* Visible button or label text.
   * *Tier 3 (Scoped CSS):* Element IDs and form classes.
   * *Tier 4 (Structural XPath):* Fallback tree navigation.
   * *Frame Scoping:* A `frame_selector` field so the engine can locate elements inside legacy `<iframe>` or `<frame>` structures.
3. **Reusable Parameters:** Using `{{base_url}}` and `{{member_id}}` ensures that the same artifact can run across local, staging, and production environments without modifying the recorded steps.
4. **Explicit Checkpoints:** I separated form submission from data extraction so the engine only reads values after verifying that the expected page actually loaded.

---

## 3. Determinism & Error Handling: Runtime Reliability and Edge Cases

### 3.1 Eliminating Flakiness in Replay
In production replay, there are zero LLM calls. I ensure reliability through:
* **Playwright Auto-Waiting:** Playwright automatically waits for elements to be visible, attached to the DOM, and enabled before clicking or typing.
* **Prioritized Fallback Cascading:** If a primary locator fails to resolve within its timeout budget, the engine automatically tries the fallback locators in order.
* **Bounded Timeout Budgets:** Each step specifies a timeout (typically 5,000ms), so an issue fails cleanly instead of hanging the process indefinitely.

### 3.2 Pre-Extraction Multi-Condition State Race (Handling Branching and Errors)
A common problem in legacy banking portals is that submitting a form might take you to an error message or warning banner instead of the success screen (for example, searching for a member that does not exist).
If an automation engine blindly tries to extract the savings balance on an error screen, it would wait for 5 seconds until the locator times out, and then report a confusing failure.

To fix this, I made the engine run a concurrent state race right before extraction:
1. The engine checks for both the success checkpoint and known business outcomes at the same time.
2. If it detects a warning banner (like `#not-found-alert` saying "Member record not found in system"), execution stops immediately in about 300ms, returning `BUSINESS_OUTCOME`. It skips the extraction step entirely with zero delay.
3. If it detects a security alert (like `#account-locked-alert`), the session routes to the human operator seam.
4. Only when the success condition resolves does the engine proceed to extract the account balance.

### 3.3 Four-Tier Result Taxonomy
I categorized execution outcomes into four clear statuses in [`src/models/result.py`](src/models/result.py):
1. **`SUCCESS`:** The flow reached the target screen, verified checkpoints, and extracted all declared data fields.
2. **`BUSINESS_OUTCOME`:** An expected banking outcome occurred (such as "Member Not Found"). This is valid business information for the caller, not a crash or automation bug.
3. **`RECOVERABLE_ERROR`:** Temporary issues that were resolved, such as dismissing an unexpected modal or retrying a slow page.
4. **`HARD_FAILURE`:** Exhausted locators, invariant failures, or security policy violations.

### 3.4 Automatic Diagnostic Capture on Failure
Whenever an unhandled `HARD_FAILURE` occurs, my code automatically triggers [`_capture_failure_artifacts()`](src/engine/executor.py):
* Takes a full-page PNG screenshot and saves it to `evidence/failure_*.png`.
* Serializes and PII-sanitizes the full DOM HTML snapshot to `evidence/failure_*.html`.
* Attaches these file paths and diagnostic details to [`ExecutionResult.debug_context`](src/models/result.py) so developers can debug quickly.

---

## 4. Heterogeneity & Multi-Tenant: Generalization and Scale

### 4.1 Supporting Different Surfaces
Legacy banking software spans modern web portals, server-rendered pages with framesets, and desktop Windows apps. To decouple flow logic from how a specific screen renders, I designed a common `SurfaceAdapter` protocol:

```python
class SurfaceAdapter(Protocol):
    async def navigate(self, url: str) -> None: ...
    async def find_element(self, strategy: LocatorStrategy) -> Any: ...
    async def click(self, target: Any) -> None: ...
    async def type_text(self, target: Any, text: str) -> None: ...
    async def get_text(self, target: Any) -> str: ...
    async def get_state_snapshot(self) -> Dict[str, Any]: ...
```

* **Web Adapter (`PlaywrightAdapter`):** Drives modern web applications using Playwright's DOM and Accessibility Tree APIs.
* **Legacy Frameset Adapter (`LegacyWebAdapter`):** Handles older applications with nested `<iframe>` and `<frame>` structures by scoping actions using `LocatorStrategy.frame_selector` via `page.frame_locator(...)`.
* **Desktop OS Adapter (`DesktopOSAdapter` - Architecture Design):** Maps the exact same `Step` actions to native OS accessibility APIs (such as Windows UI Automation via `pywinauto` or macOS Accessibility via `AXUIElement`). Control targets resolve via `Name`, `AutomationId`, or OCR coordinates, while the overall workflow remains identical.

### 4.2 Reusing Artifacts Across Multiple Tenants (Banks)
In banking, hundreds of credit unions use the same core vendor software (like FIS, Fiserv, or Jack Henry), but each institution has its own branding, URL prefix, or slightly altered fields.
Re-recording the same workflow from scratch for every bank would be wasteful. Instead, I designed a Base Artifact plus Tenant Overlay approach:

1. **Canonical Base Artifact:** Defines the vendor core workflow with parameterized paths and standard locators.
2. **Sparse Tenant Overlay:** A configuration file overriding only what is unique to that institution:
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
3. **Drift Monitoring:** If a portal update causes the engine to fall back to secondary locators multiple times in a row, the system flags that locator for review before it breaks completely.

---

## 5. Escalation & Handoff: Detection, Control Transfer, and Resumption

### 5.1 When the System Hands Off to a Human
Automation pauses and asks for human intervention when:
* All primary and fallback locators fail on a step.
* A security flag or fraud lockout is detected (such as `FRAUD_HOLD`).
* A risky or irreversible step (like transferring funds) needs explicit human sign-off.

### 5.2 How the Live Handoff Works
Instead of closing the browser and making the user start over, [`EscalationManager`](src/human/escalation.py) keeps the live browser window open and pauses automation:
1. **Automation Freeze:** Execution pauses on the active Playwright page without closing browser connections.
2. **Context Packet:** It creates an [`InterventionRequest`](src/models/human.py) with the reason, current URL, and an initial screenshot saved to `evidence/escalation_before_*.png`.
3. **Live Action Recording:** I injected a lightweight DOM observer script (`TRACKER_JS`) into the page and attached Playwright event handlers (`dialog`, `framenavigated`).
4. **Human Takes Control:** When the human operator clicks on the page (for example, clicking "Authorize Supervisor Override") or types in credentials, the engine records those actions in real time.
5. **PII Masking:** Any text the operator types is automatically filtered through [`PIIRedactor`](src/guardrails/redactor.py) so no sensitive data is saved.
6. **Clean Resumption:** When the operator finishes and confirms in the CLI (`[R] Resume`), the engine takes an after-screenshot (`evidence/escalation_after_*.png`), verifies that the lockout was cleared, detaches the listeners, and continues the automated run on that same page.

---

## 6. Safety: Guardrail Model, Action Gating, and Limitations

### 6.1 Safety Guardrails
I built guardrails in [`src/guardrails/`](src/guardrails/) to keep the agent safe and compliant with banking regulations:

1. **Strict URL Allowlist:**
   * [`GuardrailPolicy.validate_url()`](src/guardrails/policy.py) checks every destination link against approved domains (`127.0.0.1`, `localhost`, or approved bank domains).
   * This blocks open-redirect attacks and prevents the browser from navigating to unauthorized external websites.
2. **Action Allowlist:**
   * [`GuardrailPolicy.validate_action()`](src/guardrails/policy.py) restricts executable actions to verified safe primitives (`NAVIGATE`, `CLICK`, `FILL`, `EXTRACT`, `ASSERT`, `WAIT`).
   * Arbitrary script evaluation (`evaluate`, `eval`) is strictly prohibited during replay.
3. **Irreversible Action Policy Gate:**
   * Steps that modify financial state (like transferring money or closing an account) are flagged with `is_irreversible: true`.
   * In unattended headless runs, the engine stops before taking the action and returns `HARD_FAILURE` (`IRREVERSIBLE_ACTION_BLOCKED`) unless the `--allow-irreversible` flag is explicitly passed.
   * In interactive runs, it routes to [`EscalationManager`](src/human/escalation.py) for supervisor approval.

### 6.2 Protecting Regulated Financial Data (PII Scrubbing)
Bank data contains personal customer information that must never be leaked into logs or GitHub. My [`PIIRedactor`](src/guardrails/redactor.py) automatically cleans all text logs, extracted data, operator inputs, and HTML dumps:
* Social Security Numbers (`\d{3}-\d{2}-\d{4}` -> `[REDACTED_SSN]`).
* Bank Account Numbers (`CHK-...`, `SAV-...`, `MM-...` -> `[REDACTED_ACCOUNT]`).
* Credit Card Numbers (`\b(?:\d{4}[ -]?){3}\d{4}\b` -> `[REDACTED_CARD]`).
* Bearer tokens, secrets, and API keys.

### 6.3 Security Boundaries and Limitations
* **Client-Side Scope:** My redactor cleans data before writing to local logs, artifacts, and traces. It cannot control what the target bank's own web server records in its server logs.
* **Custom Account Formats:** The regex patterns cover standard US banking identifiers, but new or non-standard account formats require updating the redaction pattern list.

---

## 7. Cuts: Scoping Decisions and Future Roadmap

### 7.1 What I Deliberately Left Out (and Why)
* **Real-Time WebRTC Video Streaming Console:**
  * *Rationale:* Building an enterprise multi-user video-streaming co-browsing console is out of scope per Section 3.6 of the brief. I prioritized making the actual handoff seam solid: pausing the live browser, injecting event listeners to record what the human clicks and types, and resuming cleanly.
* **Native Desktop OS Automation Engine:**
  * *Rationale:* Real enterprise back-office surfaces include Windows Thick Clients. While I designed the `SurfaceAdapter` architecture and `frame_selector` contract, building full Windows UI Automation drivers was cut in favor of deep web reliability.
* **Distributed Task Queue Infrastructure (Celery, RabbitMQ, Kafka):**
  * *Rationale:* In production at scale, you would run jobs through message queues. For this project, adding Docker containers and brokers would add setup friction without improving the core automation primitives.
* **Unbounded Open-Ended LLM Self-Healing on Replay:**
  * *Rationale:* Allowing an LLM to take over when replay fails introduces non-determinism, hallucinations, and unbudgeted latency into production. Replay strictly favors deterministic fallbacks and clean human escalation.

### 7.2 What I Would Build Next
1. **Agent-Facing Callable Capability Catalog:** Expose saved capability artifacts as callable functions (like OpenAI tool definitions or MCP tools) so higher-level AI agents can discover and trigger them on demand.
2. **Automated Bounded Drift Recovery:** If a step consistently relies on fallback locators over several runs, trigger a bounded, single-step LLM discovery run in the background to update the primary locator via a Git pull request.
3. **Perceptual Layout Diffing:** Add screenshot comparison for legacy applications where accessibility tags are completely missing and elements can only be recognized visually.
