"""Unit and regression tests for the OpenAI Workspace Agents provider."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from agy_mcp import capability_gateway as gw
from agy_mcp import chatgpt_workspace as cw
from agy_mcp.telegram_bot import TelegramBot


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure clean test environment by stripping any live workspace env vars."""
    monkeypatch.delenv("CHATGPT_WORKSPACE_PAT", raising=False)
    monkeypatch.delenv("CHATGPT_WORKSPACE_TOKEN", raising=False)
    monkeypatch.delenv("CHATGPT_WORKSPACE_AGENTS", raising=False)
    monkeypatch.delenv("CHATGPT_WORKSPACE_AGENT_ID", raising=False)


def test_config_unconfigured_by_default():
    cfg = cw.load_config()
    assert not cfg.is_configured
    assert cfg.get_token() == ""
    assert cfg.agents == {}
    assert "***" not in repr(cfg)
    assert "<empty>" in repr(cfg)


def test_config_with_valid_json_agents(monkeypatch):
    agents_map = {
        "default": "agtch_1111222233334444",
        "analyst": "agtch_5555666677778888",
    }
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test_token_secret")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENTS", json.dumps(agents_map))

    cfg = cw.load_config()
    assert cfg.is_configured
    assert cfg.get_token() == "pat_test_token_secret"
    assert cfg.agents == agents_map
    assert "pat_test_token_secret" not in repr(cfg)
    assert "***" in repr(cfg)


def test_config_with_single_agent_fallback(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_TOKEN", "pat_fallback_token")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_singleagent001")

    cfg = cw.load_config()
    assert cfg.is_configured
    assert cfg.agents == {"default": "agtch_singleagent001"}


def test_config_with_comma_separated_agents(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test")
    monkeypatch.setenv(
        "CHATGPT_WORKSPACE_AGENTS",
        "main:agtch_111122223333,research:agtch_444455556666",
    )

    cfg = cw.load_config()
    assert cfg.is_configured
    assert cfg.agents == {
        "main": "agtch_111122223333",
        "research": "agtch_444455556666",
    }


def test_config_rejects_unallowed_base_url():
    with pytest.raises(ValueError, match="strictly target https://api.chatgpt.com"):
        cw.ChatGPTWorkspaceConfig(
            token="pat_x",
            agents={"default": "agtch_111122223333"},
            base_url="https://evil.attacker.com",
        )


def test_resolve_agent_id_cases():
    cfg = cw.ChatGPTWorkspaceConfig(
        token="pat_x",
        agents={
            "default": "agtch_default12345",
            "coder": "agtch_coder12345678",
        },
    )

    # None / empty resolves default
    assert cw.resolve_agent_id(cfg, None) == ("default", "agtch_default12345")
    assert cw.resolve_agent_id(cfg, "") == ("default", "agtch_default12345")

    # By alias
    assert cw.resolve_agent_id(cfg, "coder") == ("coder", "agtch_coder12345678")

    # By exact approved ID
    assert cw.resolve_agent_id(cfg, "agtch_coder12345678") == ("coder", "agtch_coder12345678")

    # Unapproved ID rejected
    with pytest.raises(ValueError, match="unapproved agent target"):
        cw.resolve_agent_id(cfg, "agtch_unapproved9999")

    # Unknown alias rejected
    with pytest.raises(ValueError, match="unapproved agent target"):
        cw.resolve_agent_id(cfg, "unknown_alias")


def test_capabilities_and_descriptor(monkeypatch):
    # Unconfigured
    desc_unconf = cw.descriptor()
    assert desc_unconf["id"] == "chatgpt-workspace"
    assert desc_unconf["kind"] == "provider"
    assert not desc_unconf["online"]
    assert not desc_unconf["configured"]

    caps_unconf = cw.get_capabilities()
    assert not caps_unconf["configured"]
    assert not caps_unconf["online"]
    assert caps_unconf["limits"]["content_retrieval_supported"] is False

    # Configured
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_test12345678")

    desc_conf = cw.descriptor()
    assert desc_conf["online"] is True
    assert desc_conf["configured"] is True

    caps_conf = cw.get_capabilities()
    assert caps_conf["online"] is True
    assert caps_conf["configured"] is True
    assert "trigger" in caps_conf["actions"]
    assert "status" in caps_conf["actions"]


def test_list_agents(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_test12345678")

    agents_res = cw.list_agents()
    assert agents_res["success"] is True
    assert len(agents_res["agents"]) == 1
    assert agents_res["agents"][0]["alias"] == "default"
    assert "agtch_" in agents_res["agents"][0]["agent_id"]


def test_trigger_run_unconfigured():
    res = cw.trigger_run("hello")
    assert not res["success"]
    assert "not configured" in res["error"]


def test_trigger_run_validation_errors(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_test12345678")

    # Empty input
    assert not cw.trigger_run("")["success"]
    assert not cw.trigger_run("   ")["success"]

    # Input too long
    too_long = "x" * (cw.MAX_INPUT_CHARS + 1)
    res_long = cw.trigger_run(too_long)
    assert not res_long["success"]
    assert "exceeds maximum" in res_long["error"]

    # Invalid conversation_key
    res_ck = cw.trigger_run("prompt", conversation_key="bad key with spaces @@@")
    assert not res_ck["success"]
    assert "conversation_key" in res_ck["error"]


def test_trigger_run_mocked_success():
    cfg = cw.ChatGPTWorkspaceConfig(
        token="pat_secret_abc",
        agents={"default": "agtch_agent123456"},
    )

    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.chatgpt.com/v1/workspace_agents/agtch_agent123456/trigger"
        assert request.headers.get("Authorization") == "Bearer pat_secret_abc"
        assert request.headers.get("OpenAI-Beta") == "workspace_agent_runs=v1"
        assert request.headers.get("Idempotency-Key") == "test-key-123"

        body = json.loads(request.content.decode("utf-8"))
        assert body["input"] == "Analyze log report"
        assert body["conversation_key"] == "sess_test_1"

        return httpx.Response(
            status_code=202,
            json={
                "agent_trigger_run_id": "apirun_run987654321",
                "conversation_url": "https://chatgpt.com/c/conv_12345",
                "status": "queued",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        res = cw.trigger_run(
            "Analyze log report",
            conversation_key="sess_test_1",
            idempotency_key="test-key-123",
            config=cfg,
            client=client,
        )

    assert res["success"] is True
    assert res["agent_trigger_run_id"] == "apirun_run987654321"
    assert res["conversation_url"] == "https://chatgpt.com/c/conv_12345"
    assert res["status"] == "queued"
    assert res["output_available"] is False
    assert res["output_retrieval"] == "unavailable"
    assert len(captured_requests) == 1


def test_trigger_run_mocked_http_error():
    cfg = cw.ChatGPTWorkspaceConfig(
        token="pat_secret_abc",
        agents={"default": "agtch_agent123456"},
    )

    def mock_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            json={"error": {"message": "Workspace agent not found or unauthorized"}},
        )

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        res = cw.trigger_run("Test prompt", config=cfg, client=client)

    assert res["success"] is False
    assert res["status_code"] == 403
    assert "Workspace agent not found" in res["error"]
    assert "pat_secret_abc" not in res["error"]


def test_get_run_status_mocked_success():
    cfg = cw.ChatGPTWorkspaceConfig(
        token="pat_secret_abc",
        agents={"default": "agtch_agent123456"},
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://api.chatgpt.com/v1/workspace_agents/agtch_agent123456/runs/apirun_abc12345678"
        assert request.headers.get("Authorization") == "Bearer pat_secret_abc"
        assert request.headers.get("OpenAI-Beta") == "workspace_agent_runs=v1"

        return httpx.Response(
            status_code=200,
            json={
                "id": "apirun_abc12345678",
                "status": "completed",
                "conversation_url": "https://chatgpt.com/c/conv_12345",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    with httpx.Client(transport=transport) as client:
        res = cw.get_run_status("apirun_abc12345678", config=cfg, client=client)

    assert res["success"] is True
    assert res["agent_trigger_run_id"] == "apirun_abc12345678"
    assert res["status"] == "completed"
    assert res["output_available"] is False
    assert res["output_retrieval"] == "unavailable"


def test_get_run_status_malformed_id(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_test")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_test12345678")

    res = cw.get_run_status("not-a-valid-run-id")
    assert not res["success"]
    assert "apirun_" in res["error"]


def test_dispatch_action_routing():
    cfg = cw.ChatGPTWorkspaceConfig(
        token="pat_secret",
        agents={"default": "agtch_agent123456"},
    )

    # Capabilities
    caps = cw.dispatch_action("capabilities", {}, config=cfg)
    assert caps["success"] is True
    assert caps["provider"] == "chatgpt-workspace"

    # Agents
    agents = cw.dispatch_action("agents", {}, config=cfg)
    assert agents["success"] is True
    assert len(agents["agents"]) == 1

    # Unsupported action
    bad = cw.dispatch_action("rm_rf", {}, config=cfg)
    assert not bad["success"]
    assert "unsupported action" in bad["error"]


def test_capability_gateway_integration(monkeypatch):
    monkeypatch.setenv("CHATGPT_WORKSPACE_PAT", "pat_secret")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ID", "agtch_agent123456")

    catalog = gw.catalog()
    assert "providers" in catalog
    provider_ids = [p["id"] for p in catalog["providers"]]
    assert "chatgpt-workspace" in provider_ids
    assert catalog["providers"][0]["online"] is True

    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name):
            def register(fn):
                self.tools[name] = fn
                return fn
            return register

    mcp = FakeMcp()
    gw.register_capability_tools(mcp, {})
    dispatch = mcp.tools["agy_dispatch"]

    # Dispatch capabilities to chatgpt-workspace provider
    caps_res = asyncio.run(dispatch("chatgpt-workspace", "capabilities", {}))
    assert caps_res["success"] is True
    assert caps_res["provider"] == "chatgpt-workspace"

    # Dispatch agents to chatgpt-workspace provider
    agents_res = asyncio.run(dispatch("chatgpt-workspace", "agents", {}))
    assert agents_res["success"] is True
    assert len(agents_res["agents"]) == 1


def test_telegram_chatgpt_command(tmp_path):
    store = MagicMock()
    supervisor = MagicMock()
    safety = MagicMock()

    bot = TelegramBot(
        token="test_token",
        chat_id="12345678",
        store=store,
        supervisor=supervisor,
        safety=safety,
        state_path=tmp_path / "state.json",
    )

    sent = []
    bot.send = lambda text, **kwargs: sent.append(text)

    # Test /chatgpt status
    bot._chatgpt_command("status")
    assert len(sent) == 1
    assert "ChatGPT Workspace Agents" in sent[0]
    assert "لا توفر OpenAI أي واجهة برمجية رسمية" in sent[0]

    # Test /chatgpt agents (unconfigured)
    bot._chatgpt_command("agents")
    assert len(sent) == 2
    assert "غير مهيأة" in sent[1]
