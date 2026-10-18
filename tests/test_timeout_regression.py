import pytest
from unittest.mock import MagicMock
from pathlib import Path

from agy_mcp.models import BridgeRequest
from agy_mcp import device_tools
from agy_mcp import device_agent
from agy_mcp import server


def test_bridge_request_timeout_defaults_and_gates():
    # Detached or long task defaults to 14400s
    assert BridgeRequest(prompt="test", mode="long").timeout == 14400
    assert BridgeRequest(prompt="test", detach=True).timeout == 14400
    # Short tasks stay at 900s
    assert BridgeRequest(prompt="test", mode="ask").timeout == 900
    # Explicit custom timeout accepted up to 86400
    assert BridgeRequest(prompt="test", timeout=21600).timeout == 21600
    assert BridgeRequest(prompt="test", timeout=86400).timeout == 86400
    assert BridgeRequest(prompt="test", timeout=60, detach=True).timeout == 60

    # Over 86400 or non-positive rejected
    with pytest.raises(ValueError, match="timeout exceeds 86400 seconds"):
        BridgeRequest(prompt="test", timeout=86401)
    with pytest.raises(ValueError, match="timeout must be positive seconds"):
        BridgeRequest(prompt="test", timeout=0)


def test_device_tools_timeout_gates(monkeypatch):
    captured_payload = None

    def fake_submit(device_id, action, payload):
        nonlocal captured_payload
        captured_payload = payload
        return {"success": True, "request_id": "call_123456"}

    monkeypatch.setattr(device_tools, "submit", fake_submit)

    # agy_device_start with 21600 accepted
    mcp_mock = MagicMock()
    # Register tools
    tools = {}
    def mock_tool(name):
        def decorator(fn):
            tools[name] = fn
            return fn
        return decorator
    mcp_mock.tool = mock_tool
    device_tools.register_device_tools(mcp_mock)

    start_fn = tools["agy_device_start"]
    cont_fn = tools["agy_device_continue"]

    # 21600 call accepted
    res = start_fn(device_id="windows-main", PROMPT="test prompt", timeout=21600)
    assert res["success"] is True
    assert captured_payload["timeout"] == 21600

    # Default timeout is 14400
    res = start_fn(device_id="windows-main", PROMPT="test prompt")
    assert res["success"] is True
    assert captured_payload["timeout"] == 14400

    # 86401 rejected
    res = start_fn(device_id="windows-main", PROMPT="test prompt", timeout=86401)
    assert res["success"] is False
    assert res["error"] == "invalid mode or timeout"

    # continue tool default timeout is 14400
    res = cont_fn(device_id="windows-main", SESSION_ID="sess_123", PROMPT="continue prompt")
    assert res["success"] is True
    assert captured_payload["timeout"] == 14400


def test_device_agent_start_timeout(monkeypatch):
    captured_args = None

    def fake_start(**kwargs):
        nonlocal captured_args
        captured_args = kwargs
        resp = MagicMock()
        resp.model_dump.return_value = {"success": True, "job_id": kwargs.get("job_id")}
        return resp

    monkeypatch.setattr(server, "agy_start_tool", fake_start)

    # Dispatch with 21600 timeout passes through
    res = device_agent.dispatch("start", {"PROMPT": "run", "timeout": 21600}, "call_1234567890abcdef1234567890abcdef")
    assert res["success"] is True
    assert captured_args["timeout"] == 21600

    # Dispatch without timeout defaults to 14400
    res = device_agent.dispatch("start", {"PROMPT": "run"}, "call_1234567890abcdef1234567890abcdef")
    assert res["success"] is True
    assert captured_args["timeout"] == 14400


def test_agy_start_tool_accepts_21600(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "operator")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))

    # Mock supervisor
    mock_supervisor = MagicMock()
    mock_resp = MagicMock()
    mock_resp.success = True
    mock_resp.job_id = "job_test_21600"
    mock_resp.error = None
    mock_supervisor.start.return_value = mock_resp

    mock_safety = MagicMock()
    mock_safety.redact.side_effect = lambda s: s

    monkeypatch.setattr(server, "_ensure_state", lambda: (MagicMock(), mock_safety, MagicMock(), mock_supervisor))

    # agy_start_tool with timeout=21600 MUST NOT fail with "remote timeout must be 1..1200 seconds"
    resp = server.agy_start_tool(PROMPT="test 21600", cd=str(tmp_path), timeout=21600)
    assert resp.success is True
    assert resp.job_id == "job_test_21600"

    call_req = mock_supervisor.start.call_args[0][0]
    assert isinstance(call_req, BridgeRequest)
    assert call_req.timeout == 21600
