import json
import pytest
from playwright.async_api import async_playwright

from src.agent.discovery import DiscoveryAgent, DiscoveryResult
from src.engine.executor import ReplayExecutor
from src.guardrails.policy import GuardrailPolicy
from src.models.artifact import ActionType, CapabilityArtifact
from src.models.result import ExecutionStatus


@pytest.mark.asyncio
async def test_discovery_agent_end_to_end(target_server_url: str):
    base_url = target_server_url

    call_count = 0

    def mock_llm_caller(conversation, tools):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "name": "navigate",
                "arguments": {"url": f"{base_url}/members"},
            }
        elif call_count == 2:
            return {
                "name": "fill",
                "arguments": {
                    "selector": "#member_id",
                    "value": "12345",
                    "param_name": "member_id",
                    "rationale": "Enter member identifier to search",
                },
            }
        elif call_count == 3:
            return {
                "name": "click",
                "arguments": {
                    "selector": "#search-button",
                    "rationale": "Submit member lookup search",
                },
            }
        elif call_count == 4:
            return {
                "name": "extract",
                "arguments": {
                    "selector": "#savings-balance-val",
                    "field_name": "savings_balance",
                    "rationale": "Read active member savings balance",
                },
            }
        elif call_count == 5:
            return {
                "name": "finish_goal",
                "arguments": {
                    "success_selector": "#accounts-table",
                    "business_outcomes": [
                        {
                            "code": "MEMBER_NOT_FOUND",
                            "target": "#not-found-alert",
                            "pattern": "not found in system",
                            "description": "Member record does not exist in institution registry",
                        }
                    ],
                    "summary": "Successfully navigated, searched for member 12345, and extracted savings balance.",
                },
            }
        return None

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=10,
        headless=True,
        llm_caller=mock_llm_caller,
    )

    result = await agent.discover(
        goal="look up member 12345 and read their current savings balance",
        capability_id="discovered_member_lookup",
        capability_name="Discovered Member Lookup",
    )

    assert result.success is True, f"Discovery failed with: {result.error_message}"
    assert result.steps_executed >= 4
    assert result.capability is not None

    artifact = result.capability
    assert isinstance(artifact, CapabilityArtifact)
    assert artifact.id == "discovered_member_lookup"
    assert "member_id" in artifact.input_schema
    assert "savings_balance" in artifact.output_schema

    # Verify extracted data during discovery
    assert result.extracted_data.get("savings_balance") == "$4,250.75"

    # CRUCIAL PROOF: Replay the discovered artifact through ReplayExecutor with ZERO LLM!
    executor = ReplayExecutor(base_url=base_url, headless=True)
    replay_result = await executor.execute(
        artifact=artifact,
        inputs={"member_id": "12345"},
    )

    assert replay_result.status == ExecutionStatus.SUCCESS
    assert replay_result.data is not None
    assert replay_result.data["savings_balance"] == "$4,250.75"


@pytest.mark.asyncio
async def test_discovery_agent_guardrail_violation(target_server_url: str):
    base_url = target_server_url

    def mock_llm_caller(conversation, tools):
        return {
            "name": "navigate",
            "arguments": {"url": "https://malicious-external-site.com"},
        }

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=5,
        headless=True,
        llm_caller=mock_llm_caller,
        policy=GuardrailPolicy(allowed_domains=["127.0.0.1", "localhost"]),
    )

    result = await agent.discover(
        goal="navigate to external site",
    )

    assert result.success is False
    assert "domain allowlist" in (result.error_message or "").lower()


@pytest.mark.asyncio
async def test_discovery_agent_max_steps_timeout(target_server_url: str):
    base_url = target_server_url

    # Caller that never finishes
    def endless_llm_caller(conversation, tools):
        return {
            "name": "navigate",
            "arguments": {"url": f"{base_url}/members"},
        }

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=3,
        headless=True,
        llm_caller=endless_llm_caller,
    )

    result = await agent.discover(
        goal="explore members",
    )

    # Reaches max_steps and wraps up fallback checkpoint
    assert result.steps_executed >= 3


@pytest.mark.asyncio
async def test_discovery_agent_relative_nav_and_none_action(target_server_url: str):
    base_url = target_server_url

    # First call: relative URL navigation; second call: None
    call_count = 0
    def mock_caller(conv, tools):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "name": "navigate",
                "arguments": {"url": "/members"},
            }
        return None

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=5,
        headless=True,
        llm_caller=mock_caller,
    )

    result = await agent.discover(goal="check relative nav")
    assert result.success is False
    assert "returned no tool actions" in (result.error_message or "")


def test_discovery_agent_locator_builder():
    agent = DiscoveryAgent()
    
    class MockRoot:
        def get_by_role(self, role, name=None):
            return f"role:{role}:{name}"
        def get_by_text(self, text):
            return f"text:{text}"
        def get_by_label(self, label):
            return f"label:{label}"
        def locator(self, sel):
            return f"loc:{sel}"

    root = MockRoot()
    assert agent._build_playwright_locator(root, "role:button[name='Submit']") == "role:button:Submit"
    assert agent._build_playwright_locator(root, "role:textbox") == "role:textbox:None"
    assert agent._build_playwright_locator(root, "text:Search") == "text:Search"
    assert agent._build_playwright_locator(root, "label:Member ID") == "label:Member ID"
    assert agent._build_playwright_locator(root, "css:#btn") == "loc:#btn"
    assert agent._build_playwright_locator(root, "xpath=//div") == "loc:xpath=//div"
    assert agent._build_playwright_locator(root, ".plain-css") == "loc:.plain-css"


@pytest.mark.asyncio
async def test_discovery_agent_openai_integration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-valid-test-key")

    class MockFunction:
        name = "navigate"
        arguments = '{"url": "/members"}'

    class MockToolCall:
        id = "call_mock123"
        function = MockFunction()

    class MockMessage:
        tool_calls = [MockToolCall()]

    class MockChoice:
        message = MockMessage()

    class MockResponse:
        choices = [MockChoice()]

    class MockCompletions:
        async def create(self, **kwargs):
            return MockResponse()

    class MockChat:
        completions = MockCompletions()

    class MockAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = MockChat()

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", MockAsyncOpenAI)

    agent = DiscoveryAgent()
    action = await agent._get_next_action(
        conversation=[{"role": "user", "content": "hi"}],
        goal="navigate",
        interactive_elements=[],
        current_url="http://127.0.0.1:8000",
    )

    assert action is not None
    assert action["id"] == "call_mock123"
    assert action["name"] == "navigate"
    assert action["arguments"]["url"] == "/members"

    # Also verify that client instance is cached and reused
    assert agent._openai_client is not None
    cached_client = agent._get_openai_client()
    assert cached_client is agent._openai_client


def test_discovery_agent_anthropic_client_caching(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    class MockAsyncAnthropic:
        def __init__(self, api_key):
            self.api_key = api_key

    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", MockAsyncAnthropic)

    agent = DiscoveryAgent()
    client1 = agent._get_anthropic_client()
    assert client1 is not None
    assert client1.api_key == "test-anthropic-key"
    client2 = agent._get_anthropic_client()
    assert client2 is client1



@pytest.mark.asyncio
async def test_discovery_conversation_message_history(target_server_url: str):
    base_url = target_server_url
    captured_conversations = []

    call_count = 0
    def mock_caller(conversation, tools):
        nonlocal call_count
        captured_conversations.append(list(conversation))
        call_count += 1
        if call_count == 1:
            return {
                "id": "call_nav_1",
                "name": "navigate",
                "arguments": {"url": f"{base_url}/members"},
            }
        elif call_count == 2:
            return {
                "id": "call_finish_2",
                "name": "finish_goal",
                "arguments": {
                    "success_selector": "h2",
                    "summary": "Completed nav",
                },
            }
        return None

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=5,
        headless=True,
        llm_caller=mock_caller,
    )

    result = await agent.discover(goal="navigate and verify history")
    assert result.success is True

    # Check the conversation passed to call 2 (after tool 1 executed)
    second_call_conv = captured_conversations[1]
    roles = [m["role"] for m in second_call_conv]

    # Structure MUST be: system -> user (initial) -> assistant (tool_call 1) -> tool (tool_response 1)
    assert roles == ["system", "user", "assistant", "tool"]

    # Verify assistant message has tool_calls with ID
    assert second_call_conv[2]["tool_calls"][0]["id"] == "call_nav_1"
    assert second_call_conv[2]["tool_calls"][0]["function"]["name"] == "navigate"

    # Verify tool message has matching tool_call_id and post-action interactive elements
    assert second_call_conv[3]["tool_call_id"] == "call_nav_1"
    tool_content = json.loads(second_call_conv[3]["content"])
    assert tool_content["status"] == "OK"
    assert "interactive_elements" in tool_content
    assert tool_content["current_url"] == f"{base_url}/members"

    # Verify NO duplicate user message is injected before assistant's next turn
    user_msgs = [m for m in second_call_conv if m["role"] == "user"]
    assert len(user_msgs) == 1


@pytest.mark.asyncio
async def test_discovery_action_error_recovery(target_server_url: str):
    """
    Verifies that when an action encounters an error (e.g. invalid selector),
    the agent traps the exception, reports status: FAILED in the tool response,
    and allows the agent to self-correct in subsequent turns without crashing.
    """
    base_url = target_server_url
    captured_conversations = []
    call_count = 0

    def mock_caller(conversation, tools):
        nonlocal call_count
        captured_conversations.append(list(conversation))
        call_count += 1
        if call_count == 1:
            return {
                "id": "call_bad_click",
                "name": "click",
                "arguments": {"selector": "#non-existent-button-xyz"},
            }
        elif call_count == 2:
            return {
                "id": "call_good_nav",
                "name": "navigate",
                "arguments": {"url": f"{base_url}/members"},
            }
        elif call_count == 3:
            return {
                "id": "call_finish",
                "name": "finish_goal",
                "arguments": {"success_selector": "h2"},
            }
        return None

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=5,
        headless=True,
        llm_caller=mock_caller,
    )

    result = await agent.discover(goal="test error recovery")
    assert result.success is True

    # Check the conversation passed to call 2 (after bad click failed)
    second_call_conv = captured_conversations[1]
    tool_resp = json.loads(second_call_conv[3]["content"])

    # Failed action must be cleanly reported in tool response
    assert tool_resp["status"] == "FAILED"
    assert tool_resp["action"] == "click"
    assert "error" in tool_resp

    # Trajectory log records failure without crashing
    failed_logs = [log for log in result.trajectory_log if log.get("status") == "FAILED"]
    assert len(failed_logs) == 1


@pytest.mark.asyncio
async def test_discovery_finish_goal_immediate_exit(target_server_url: str):
    """
    Verifies that finish_goal appends completion tool turn and breaks immediately,
    without executing redundant post-action DOM queries.
    """
    base_url = target_server_url
    call_count = 0

    def mock_caller(conversation, tools):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "id": "call_finish_now",
                "name": "finish_goal",
                "arguments": {"success_selector": "body"},
            }
        return None

    agent = DiscoveryAgent(
        base_url=base_url,
        max_steps=5,
        headless=True,
        llm_caller=mock_caller,
    )

    result = await agent.discover(goal="immediate finish")
    assert result.success is True
    assert result.steps_executed == 1  # only initial nav
    finish_logs = [log for log in result.trajectory_log if log.get("action") == "finish_goal"]
    assert len(finish_logs) == 1




