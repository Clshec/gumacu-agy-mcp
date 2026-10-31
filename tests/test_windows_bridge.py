import asyncio
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types
from mcp.shared.message import SessionMessage

_source = Path(__file__).parents[1] / "integrations/windows-mcp/bridge.py"
_spec = importlib.util.spec_from_file_location("gumacu_windows_bridge", _source)
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)
Bridge = _bridge.Bridge
json_result = _bridge.json_result
with_windows = _bridge.with_windows


def test_failed_linux_operation_is_not_replayed_and_next_operation_reconnects():
    async def check():
        failed = session()
        failed.call_tool.side_effect = ConnectionError("frontend retired")
        fresh = session()
        connections = [failed, fresh]
        closed = []

        @asynccontextmanager
        async def connect():
            current = connections.pop(0)
            try:
                yield current, None
            finally:
                closed.append(current)

        upstream = _bridge.PerRequestUpstream({"command": "unused-test-command"})
        upstream.connect = connect
        with pytest.raises(ConnectionError):
            await upstream.call_tool("agy_dispatch", {"action": "operation_step"})
        failed.call_tool.assert_awaited_once()
        assert len(connections) == 1
        await upstream.call_tool("agy_doctor", {})
        fresh.call_tool.assert_awaited_once_with("agy_doctor", {})
        assert closed == [failed, fresh]
    run(check())


def test_resumed_session_bootstraps_without_replaying_or_leaking_internal_id():
    async def check():
        original = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="user-request", method="tools/call", params={"name": "agy_capabilities"},
        )))
        source = SimpleNamespace(receive=AsyncMock(side_effect=[original, original]), aclose=AsyncMock())
        sink = SimpleNamespace(send=AsyncMock(), aclose=AsyncMock())
        reader = _bridge.ResumeSafeInput(source)
        writer = _bridge.ResumeSafeOutput(sink, reader)
        bootstrap = await reader.receive()
        assert bootstrap.message.root.method == "initialize"
        await writer.send(SessionMessage(types.JSONRPCMessage(types.JSONRPCResponse(
            jsonrpc="2.0", id=reader.bootstrap_id, result={},
        ))))
        sink.send.assert_not_awaited()
        assert (await reader.receive()).message.root.method == "notifications/initialized"
        assert await reader.receive() is original
        assert source.receive.await_count == 1
        await writer.send(original)
        sink.send.assert_awaited_once_with(original)
    run(check())


def test_normal_client_initialization_passes_unchanged():
    async def check():
        original = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="client-init", method="initialize", params={},
        )))
        source = SimpleNamespace(receive=AsyncMock(return_value=original), aclose=AsyncMock())
        reader = _bridge.ResumeSafeInput(source)
        assert await reader.receive() is original
        assert reader.state == "normal"
    run(check())


def session(names=("PowerShell", "Screenshot")):
    return SimpleNamespace(
        list_tools=AsyncMock(return_value=types.ListToolsResult(tools=[
            types.Tool(name=n, inputSchema={"type": "object"}) for n in names
        ])),
        call_tool=AsyncMock(return_value=json_result({"ok": True})),
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_other_devices_forward_unchanged():
    upstream, windows = session(), session()
    args = {"device_id": "linux-netcup", "action": "ssh_call", "arguments": {"name": "ssh_exec"}}
    run(Bridge(upstream, windows).call("agy_dispatch", args))
    upstream.call_tool.assert_awaited_once_with("agy_dispatch", args)
    windows.call_tool.assert_not_awaited()


@pytest.mark.parametrize("facade", ["alf_dispatch", "agy_dispatch"])
def test_windows_catalog_is_live_and_lists_schemas(facade):
    upstream, windows = session(), session()
    result = run(Bridge(upstream, windows).call(facade, {"device_id": "windows-main", "action": "windows_catalog"}))
    assert [t["name"] for t in result.structuredContent["tools"]] == ["PowerShell", "Screenshot"]
    upstream.call_tool.assert_not_awaited()


def test_windows_call_preserves_image_and_error_result():
    upstream, windows = session(), session()
    native = types.CallToolResult(content=[types.ImageContent(type="image", data="eA==", mimeType="image/png")], isError=False)
    windows.call_tool.return_value = native
    result = run(Bridge(upstream, windows).call("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "Screenshot"}}))
    assert result is native
    windows.call_tool.assert_awaited_once_with("Screenshot", {})


def test_unknown_tool_and_old_agent_action_do_not_execute():
    upstream, windows = session(), session()
    bridge = Bridge(upstream, windows)
    for arguments in ({"device_id": "windows-main", "action": "start"},
                      {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "not_allowed"}}):
        assert run(bridge.call("agy_dispatch", arguments)).structuredContent["success"] is False
    upstream.call_tool.assert_not_awaited()
    windows.call_tool.assert_not_awaited()


def test_capabilities_replace_stale_agent_without_mutating_upstream():
    data = {"success": True, "devices": [{"id": "linux-netcup", "actions": ["ssh_call"]}, {"id": "windows-main", "online": False, "actions": ["start"]}]}
    result = with_windows(data, True)
    assert result["devices"][0] == data["devices"][0]
    assert result["devices"][1]["actions"] == ["windows_catalog", "windows_call"]
    assert data["devices"][1]["online"] is False


def test_old_upstream_cannot_reintroduce_removed_execution():
    upstream, windows = session(), session()
    bridge = Bridge(upstream, windows)
    for name, args in [("agy_start", {"PROMPT": "work"}),
                       ("agy_dispatch", {"device_id": "linux-netcup", "action": "start"})]:
        result = run(bridge.call(name, args))
        assert result.structuredContent["success"] is False
    upstream.call_tool.assert_not_awaited()
    data = {"devices": [{"id": "linux-netcup", "online": True,
                          "actions": ["start", "ssh_call", "operation_step"]}]}
    assert with_windows(data, True)["devices"][0]["actions"] == ["ssh_call"]


def test_direct_tool_and_argument_limit():
    upstream, windows = session(), session()
    bridge = Bridge(upstream, windows)
    run(bridge.call("windows_PowerShell", {"command": "Get-Date"}))
    windows.call_tool.assert_awaited_once_with("PowerShell", {"command": "Get-Date"})
    result = run(bridge.call("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "PowerShell", "arguments": {"command": "x" * 256_001}}}))
    assert result.structuredContent["success"] is False
    assert windows.call_tool.await_count == 1


def test_windows_tool_pagination():
    upstream, windows = session(), session()
    windows.list_tools.side_effect = [types.ListToolsResult(tools=[types.Tool(name="A", inputSchema={})], nextCursor="next"), types.ListToolsResult(tools=[types.Tool(name="B", inputSchema={})])]
    assert [t.name for t in run(Bridge(upstream, windows).windows_tools())] == ["A", "B"]
    assert windows.list_tools.call_args_list[1].kwargs == {"cursor": "next"}
