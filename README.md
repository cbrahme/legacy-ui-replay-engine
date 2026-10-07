# Computer-Use Automation System for Legacy Banking

A "record-once, replay-many" automation engine that lets AI agents safely operate legacy banking applications that have no APIs.

- **Discovery (LLM-in-the-loop):** An AI agent explores a live web UI to achieve a goal and compiles the interaction into a reusable, parameter-driven capability artifact.
- **Deterministic Replay (Zero LLM):** Production runs execute the saved capability directly with Playwright without model inference - fast, cheap, predictable, and resilient.
- **Safety & Human Escalation:** Enforces domain allowlists, redacts PII automatically, and transfers live session control to a human supervisor on security holds or unexpected blockers.

---

## Quick Start

### 1. Prerequisites & Setup
Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Clone repository
git clone https://github.com/cbrahme/legacy-ui-replay-engine.git
cd legacy-ui-replay-engine

# Install dependencies
uv sync

# Install Playwright browser
uv run playwright install chromium
```

### 2. Configuration (`.env`)
Copy the environment template:
```bash
cp .env.example .env
```

* **Running with live LLM (Discovery):** Set your OpenAI API key in `.env`:
  ```bash
  OPENAI_API_KEY=sk-...
  ```
* **Running without live LLM services:** You can replay all saved capabilities, run the test harness, and evaluate human escalation **without** an API key.

---

## Demo Walkthrough

### Step 1: Start the Local Banking Portal
In your first terminal, launch the local mock core banking portal:
```bash
uv run python -m src.cli serve-target --port 8000
```
Visit [http://127.0.0.1:8000](http://127.0.0.1:8000) to see the mock banking interface.

### Step 2: Discover a Capability (LLM)
In a second terminal, run the discovery agent to explore the lookup flow:
```bash
uv run python -m src.cli discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --url "http://127.0.0.1:8000/members" \
  --output capabilities/member_balance_lookup.json
```
The agent inspects elements on the live page, derives resilient 4-tier locators (Role &rarr; Text &rarr; CSS &rarr; XPath), parameterizes input literals (`{{member_id}}`), and saves the compiled artifact.

### Step 3: Deterministic Replay (Zero LLM)
Replay the saved capability artifact directly without model calls:

```bash
# Happy path: Active member 12345
uv run python -m src.cli replay \
  -a capabilities/member_balance_lookup.json \
  -p '{"member_id": "12345"}'

# Business outcome: Non-existent member 99999
uv run python -m src.cli replay \
  -a capabilities/member_balance_lookup.json \
  -p '{"member_id": "99999"}'
```

### Step 4: Run the Test Harness
Execute all 96 automated tests with coverage:
```bash
uv run python -m src.cli test-harness
```

### Step 5: Reproduce Evidence Artifacts
Regenerate the complete suite of logs, screenshots, and DOM snapshots in `evidence/`:
```bash
# 1. Regenerate live LLM discovery run log (requires OPENAI_API_KEY)
uv run python scripts/generate_discovery_evidence.py

# 2. Regenerate all replay logs, human escalation traces, and rich failure signals
uv run python scripts/generate_replay_evidence.py
```

---

## CLI Commands

| Command | Description |
| :--- | :--- |
| `serve-target` | Starts the mock core banking web app on port 8000 |
| `discover` | Uses an LLM agent to explore the UI and compile a capability artifact |
| `replay` | Replays a saved capability deterministically with input parameters |
| `test-harness` | Runs the full `pytest` test suite with coverage report |

---

## Repository Structure

```text
├── capabilities/    # Reusable capability artifacts (parameterized JSON)
├── evidence/        # Execution logs, failure screenshots, and DOM snapshots
├── scripts/         # Evidence reproduction scripts (discovery & replay)
├── src/
│   ├── agent/       # LLM discovery loop, DOM inspector, and artifact compiler
│   ├── engine/      # Deterministic replay executor & state observation race
│   ├── guardrails/  # URL/action allowlists, irreversible gates, and PII redactor
│   ├── human/       # Live session handoff, event listeners & operator recorder
│   ├── models/      # Pydantic contracts (CapabilityArtifact, ExecutionResult)
│   ├── target_app/  # Local mock core banking portal (FastAPI + Jinja2)
│   └── cli.py       # Developer CLI entry point
├── tests/           # 96 unit and integration tests
├── REPORT.md        # Technical architecture report and design decisions
└── README.md        # Setup guide and CLI walkthrough
```
