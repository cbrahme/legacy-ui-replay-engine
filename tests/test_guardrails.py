from pathlib import Path
import pytest

from src.engine.executor import ReplayExecutor
from src.guardrails.policy import (
    ActionViolationError,
    DomainViolationError,
    GuardrailPolicy,
    IrreversiblePolicy,
)
from src.guardrails.redactor import PIIRedactor
from src.models.artifact import (
    ActionType,
    CapabilityArtifact,
    Step,
)
from src.models.result import ExecutionResult, ExecutionStatus, StepLog


# ---------------------------------------------------------------------------
# 1. GuardrailPolicy Unit Tests
# ---------------------------------------------------------------------------

def test_guardrail_policy_valid_urls():
    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost", "*.internalbank.com"])

    # Relative paths always permitted
    policy.validate_url("/members")
    policy.validate_url("/dashboard")

    # Local loopback domains
    policy.validate_url("http://127.0.0.1:8000/members")
    policy.validate_url("http://localhost:3000/api")

    # Wildcard domain
    policy.validate_url("https://core.internalbank.com/service")
    policy.validate_url("http://teller.internalbank.com:8080/query")


def test_guardrail_policy_schemeless_urls():
    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost", "*.internalbank.com"])

    # Permitted schemeless loopback targets
    policy.validate_url("127.0.0.1:8000/members")
    policy.validate_url("localhost:3000/api")
    policy.validate_url("localhost:8000")

    # Permitted schemeless wildcard target
    policy.validate_url("core.internalbank.com/service")

    # Disallowed schemeless targets
    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("evil.com/phish")
    assert exc_info.value.host == "evil.com"

    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("attacker.com:8080/admin")
    assert exc_info.value.host == "attacker.com"

    # Custom scheme using allowlisted single-label host name must NOT bypass scheme validation
    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("localhost:payload")
    assert exc_info.value.host == "localhost"

    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("custom-scheme:data")


def test_guardrail_policy_domain_wildcards_and_boundaries():
    policy = GuardrailPolicy(
        allowed_domains=["*.internalbank.com", ".partner.org", "localhost"]
    )

    # 1. Apex domain matching for *.domain
    policy.validate_url("https://internalbank.com/portal")
    policy.validate_url("http://internalbank.com:8000/dashboard")

    # 2. Subdomains for *.domain
    policy.validate_url("https://teller.internalbank.com/search")
    policy.validate_url("https://secure.core.internalbank.com/accounts")

    # 3. Trailing FQDN dot stripping
    policy.validate_url("http://localhost./dashboard")
    policy.validate_url("https://internalbank.com./portal")

    # 4. Alternative dot-prefix (.partner.org)
    policy.validate_url("https://partner.org/home")
    policy.validate_url("https://auth.partner.org/login")

    # 5. Prevent subdomain traversal and prefix collision bypasses
    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("https://evil-internalbank.com/phish")
    assert exc_info.value.host == "evil-internalbank.com"

    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("https://attackerinternalbank.com/exfiltrate")
    assert exc_info.value.host == "attackerinternalbank.com"

    with pytest.raises(DomainViolationError):
        policy.validate_url("https://notpartner.org/login")

    # 6. Universal wildcard
    all_allowed_policy = GuardrailPolicy(allowed_domains=["*"])
    all_allowed_policy.validate_url("https://arbitrary-bank-domain.com/feed")




def test_guardrail_policy_disallowed_domains():
    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost"])

    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("https://malicious-external-host.com/exfiltrate")
    assert "malicious-external-host.com" in str(exc_info.value)
    assert exc_info.value.host == "malicious-external-host.com"

    with pytest.raises(DomainViolationError):
        policy.validate_url("http://192.168.1.100:8080/admin")


def test_guardrail_policy_disallowed_schemes():
    policy = GuardrailPolicy(allowed_domains=["localhost"])

    # JavaScript URI injection
    with pytest.raises(DomainViolationError):
        policy.validate_url("javascript:alert('pwned')")

    # File URI access
    with pytest.raises(DomainViolationError):
        policy.validate_url("file:///etc/passwd")


def test_guardrail_policy_disallowed_action():
    # Policy with restricted action set (read-only: no clicks or fills)
    read_only_policy = GuardrailPolicy(
        allowed_actions={ActionType.NAVIGATE, ActionType.EXTRACT, ActionType.ASSERT, ActionType.WAIT}
    )

    # Allowed actions
    read_only_policy.validate_action(ActionType.NAVIGATE)
    read_only_policy.validate_action(ActionType.EXTRACT)

    # Prohibited action
    with pytest.raises(ActionViolationError) as exc_info:
        read_only_policy.validate_action(ActionType.CLICK)
    assert "CLICK" in str(exc_info.value) or "click" in str(exc_info.value)


def test_guardrail_policy_irreversible_branches():
    # 1. BLOCK_UNATTENDED (default)
    strict_policy = GuardrailPolicy(irreversible_policy=IrreversiblePolicy.BLOCK_UNATTENDED)
    can_proceed, reason = strict_policy.evaluate_irreversible_step(
        is_irreversible=True, allow_irreversible=False
    )
    assert not can_proceed
    assert "IRREVERSIBLE_ACTION_BLOCKED" in reason

    can_proceed, reason = strict_policy.evaluate_irreversible_step(
        is_irreversible=True, allow_irreversible=True
    )
    assert can_proceed
    assert reason is None

    # 2. ROUTE_TO_HUMAN
    human_policy = GuardrailPolicy(irreversible_policy=IrreversiblePolicy.ROUTE_TO_HUMAN)
    can_proceed, reason = human_policy.evaluate_irreversible_step(
        is_irreversible=True, allow_irreversible=False
    )
    assert not can_proceed
    assert "IRREVERSIBLE_ACTION_REQUIRES_HUMAN" in reason


# ---------------------------------------------------------------------------
# 2. PIIRedactor Unit Tests
# ---------------------------------------------------------------------------

def test_pii_redactor_text_patterns():
    redactor = PIIRedactor()

    # SSN
    text_ssn = "Customer SSN is 123-45-6789 on record."
    assert redactor.redact_text(text_ssn) == "Customer SSN is [REDACTED_SSN] on record."

    # Credit Card
    text_cc = "Payment card number: 4532 0150 1234 5678 expiring soon."
    assert redactor.redact_text(text_cc) == "Payment card number: [REDACTED_CARD] expiring soon."

    # Bank Account
    text_acc = "Transferred from CHK-4001 to SAV-8002."
    assert redactor.redact_text(text_acc) == "Transferred from [REDACTED_ACCOUNT] to [REDACTED_ACCOUNT]."

    # API Token
    text_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and sk-proj-1234567890abcdef1234567890"
    redacted = redactor.redact_text(text_token)
    assert "[REDACTED_TOKEN]" in redacted

    # Email
    text_email = "Contact compliance at officer.smith@firstfederal.org for clearance."
    assert redactor.redact_text(text_email) == "Contact compliance at [REDACTED_EMAIL] for clearance."


def test_pii_redactor_nested_structures():
    redactor = PIIRedactor()

    payload = {
        "member_name": "Eleanor Vance",
        "ssn": "987-65-4321",
        "accounts": [
            {"account_number": "CHK-4001", "type": "checking"},
            {"account_number": "SAV-8002", "type": "savings"},
        ],
        "contact": {
            "email": "eleanor@vance-holdings.com",
            "notes": "Verified SSN 987-65-4321 with branch manager",
        },
    }

    redacted = redactor.redact_data(payload)
    assert redacted["ssn"] == "[REDACTED_SSN]"
    assert redacted["accounts"][0]["account_number"] == "[REDACTED_ACCOUNT]"
    assert redacted["accounts"][1]["account_number"] == "[REDACTED_ACCOUNT]"
    assert redacted["contact"]["email"] == "[REDACTED_EMAIL]"
    assert "[REDACTED_SSN]" in redacted["contact"]["notes"]


# ---------------------------------------------------------------------------
# 3. Integration Tests with ReplayExecutor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_executor_blocks_unauthorized_domain(tmp_path):
    """Verifies that navigation to unauthorized domain is halted with DOMAIN_NOT_ALLOWED."""
    artifact = CapabilityArtifact(
        id="unauthorized_domain_test",
        name="Unauthorized Domain Test",
        description="Attempts to navigate outside permitted allowlist",
        target_path="/",
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                value="https://unauthorized-phishing-host.com/login",
            ),
        ],
        checkpoint={
            "success_condition": {
                "type": "element_visible",
                "target": "body",
            }
        },
    )

    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost"])
    executor = ReplayExecutor(
        base_url="http://127.0.0.1:8000",
        headless=True,
        evidence_dir=str(tmp_path),
        policy=policy,
    )

    result = await executor.execute(artifact)

    assert result.status == ExecutionStatus.HARD_FAILURE
    assert result.outcome_code == "DOMAIN_NOT_ALLOWED"
    assert "prohibited by domain allowlist" in result.outcome_message
    assert result.evidence_path is not None
    assert Path(result.evidence_path).exists()


@pytest.mark.asyncio
async def test_replay_executor_sanitizes_pii_in_results(tmp_path):
    """Verifies that extracted data and messages containing sensitive accounts/SSNs are scrubbed."""
    # Test result directly with redactor integration
    redactor = PIIRedactor()
    raw_result = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        capability_id="pii_sanitize_test",
        version="1.0.0",
        data={
            "ssn": "123-45-6789",
            "account": "CHK-4001",
            "balance": "$4,250.75",
        },
        outcome_code="SUCCESS",
        outcome_message="Successfully extracted balance for account SAV-8002 and SSN 123-45-6789",
        step_logs=[
            StepLog(
                step_id=1,
                action="extract",
                status="OK",
                extracted_data={"raw_id": "CHK-4001"},
            )
        ],
    )

    executor = ReplayExecutor(
        evidence_dir=str(tmp_path),
        redactor=redactor,
    )
    sanitized = executor._sanitize_result(raw_result)

    assert sanitized.data["ssn"] == "[REDACTED_SSN]"
    assert sanitized.data["account"] == "[REDACTED_ACCOUNT]"
    assert sanitized.data["balance"] == "$4,250.75"
    assert "[REDACTED_ACCOUNT]" in sanitized.outcome_message
    assert "[REDACTED_SSN]" in sanitized.outcome_message
    assert sanitized.step_logs[0].extracted_data["raw_id"] == "[REDACTED_ACCOUNT]"


def test_guardrail_policy_empty_or_none_url():
    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost"])
    
    # Empty URL
    policy.validate_url("")
    
    # None URL
    policy.validate_url(None)


def test_guardrail_policy_no_hostname():
    policy = GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost"])
    
    # Malformed absolute URL with no hostname
    with pytest.raises(DomainViolationError) as exc_info:
        policy.validate_url("http:///path/to/resource")
    assert exc_info.value.host is None
    assert "Permitted hosts" in str(exc_info.value)


def test_pii_redactor_redact_data_none():
    redactor = PIIRedactor()

    assert redactor.redact_data(None) is None


def test_pii_redactor_redact_text_non_string():
    redactor = PIIRedactor()

    non_string_inputs = [12345, None, 3.14, True, {"key": "value"}]
    for input_data in non_string_inputs:
        assert redactor.redact_text(input_data) == input_data


def test_pii_redactor_redact_data_with_tuples():
    redactor = PIIRedactor()

    data = ("SSN: 123-45-6789", "Email: user@example.com", "Account: CHK-4001")
    redacted = redactor.redact_data(data)

    assert redacted == ("SSN: [REDACTED_SSN]", "Email: [REDACTED_EMAIL]", "Account: [REDACTED_ACCOUNT]")


def test_sanitize_result_redacts_debug_context():
    executor = ReplayExecutor()
    raw_result = ExecutionResult(
        status=ExecutionStatus.HARD_FAILURE,
        capability_id="test_cap",
        version="1.0",
        outcome_code="CHECKPOINT_FAILED",
        outcome_message="Failed for 123-45-6789",
        debug_context={
            "url": "http://localhost:8000/members?ssn=987-65-4321",
            "token": "sk-proj-abc12345678901234567890",
            "details": ["user@example.com", "CHK-9999"],
        },
    )

    sanitized = executor._sanitize_result(raw_result)
    assert "[REDACTED_SSN]" in sanitized.outcome_message
    assert sanitized.debug_context["url"] == "http://localhost:8000/members?ssn=[REDACTED_SSN]"
    assert sanitized.debug_context["token"] == "[REDACTED_TOKEN]"
    assert sanitized.debug_context["details"] == ["[REDACTED_EMAIL]", "[REDACTED_ACCOUNT]"]


@pytest.mark.asyncio
async def test_capture_failure_artifacts_redacts_html(tmp_path):
    class DummyPage:
        async def screenshot(self, path, full_page=True):
            pass

        async def content(self):
            return "<html><body>SSN: 123-45-6789, email: bank@internal.com</body></html>"

    executor = ReplayExecutor(evidence_dir=str(tmp_path))
    _, html_path = await executor._capture_failure_artifacts(DummyPage(), "test_cap", 1)

    assert html_path is not None
    saved_html = Path(html_path).read_text(encoding="utf-8")
    assert "123-45-6789" not in saved_html
    assert "[REDACTED_SSN]" in saved_html
    assert "[REDACTED_EMAIL]" in saved_html


@pytest.mark.asyncio
async def test_dispatch_step_rejects_post_action_disallowed_navigation():
    class DummyPage:
        url = "https://unauthorized-redirect.com/login"

        async def goto(self, url, timeout=5000):
            pass

    executor = ReplayExecutor(
        policy=GuardrailPolicy(allowed_domains=["localhost", "127.0.0.1"])
    )
    step = Step(step_id=1, action=ActionType.NAVIGATE, value="http://localhost:8000/members")
    artifact = CapabilityArtifact(
        id="test_cap",
        name="Test",
        description="Test",
        target_path="/",
        steps=[step],
        checkpoint={
            "success_condition": {
                "type": "element_visible",
                "target": "body",
            }
        },
    )

    with pytest.raises(DomainViolationError) as exc_info:
        await executor._dispatch_step(DummyPage(), step, artifact, {}, {})

    assert "unauthorized-redirect.com" in str(exc_info.value)



def test_evaluate_irreversible_step_audit_log_policy():
    policy = GuardrailPolicy(irreversible_policy=IrreversiblePolicy.AUDIT_LOG)
    can_proceed, reason = policy.evaluate_irreversible_step(is_irreversible=True, allow_irreversible=False)
    assert can_proceed is True
    assert reason is None

