import asyncio
import importlib.util
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

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


def test_server_discovery_falls_back_before_legacy_initialize():
    async def check():
        discovery = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="modern-discovery", method="server/discover",
            params={"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                              "io.modelcontextprotocol/clientCapabilities": {}}},
        )))
        initialize = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="legacy-init", method="initialize", params={},
        )))
        source = SimpleNamespace(receive=AsyncMock(side_effect=[discovery, initialize]), aclose=AsyncMock())
        sink = SimpleNamespace(send=AsyncMock(), aclose=AsyncMock())
        reader = _bridge.ResumeSafeInput(source)
        _bridge.ResumeSafeOutput(sink, reader)

        assert await reader.receive() is initialize
        sink.send.assert_awaited_once()
        response = sink.send.await_args.args[0].message.root
        assert isinstance(response, types.JSONRPCError)
        assert response.id == "modern-discovery"
        assert response.error.code == -32601
        assert reader.state == "normal"

    run(check())


def test_server_discovery_after_initialize_does_not_reset_session():
    async def check():
        initialize = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="legacy-init", method="initialize", params={},
        )))
        discovery = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="modern-discovery", method="server/discover",
            params={"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                              "io.modelcontextprotocol/clientCapabilities": {}}},
        )))
        listing = SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
            jsonrpc="2.0", id="legacy-list", method="tools/list", params={},
        )))
        source = SimpleNamespace(receive=AsyncMock(side_effect=[initialize, discovery, listing]), aclose=AsyncMock())
        sink = SimpleNamespace(send=AsyncMock(), aclose=AsyncMock())
        reader = _bridge.ResumeSafeInput(source)
        _bridge.ResumeSafeOutput(sink, reader)

        assert await reader.receive() is initialize
        assert await reader.receive() is listing
        response = sink.send.await_args.args[0].message.root
        assert isinstance(response, types.JSONRPCError)
        assert response.id == "modern-discovery"
        assert response.error.code == -32601
        assert reader.state == "normal"

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
    run(Bridge(upstream, backend(windows)).call("agy_dispatch", args))
    upstream.call_tool.assert_awaited_once_with("agy_dispatch", args)
    windows.call_tool.assert_not_awaited()


@pytest.mark.parametrize("facade", ["alf_dispatch", "agy_dispatch"])
def test_windows_catalog_is_live_and_lists_schemas(facade):
    upstream, windows = session(), session()
    result = run(Bridge(upstream, backend(windows)).call(facade, {"device_id": "windows-main", "action": "windows_catalog"}))
    assert [t["name"] for t in result.structuredContent["tools"]] == ["PowerShell", "Screenshot"]
    upstream.call_tool.assert_not_awaited()


def test_windows_call_preserves_image_and_error_result():
    upstream, windows = session(), session()
    native = types.CallToolResult(content=[types.ImageContent(type="image", data="eA==", mimeType="image/png")], isError=False)
    windows.call_tool.return_value = native
    result = run(Bridge(upstream, backend(windows)).call("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "Screenshot"}}))
    assert result is native
    windows.call_tool.assert_awaited_once_with("Screenshot", {})


def test_unknown_tool_and_old_agent_action_do_not_execute():
    upstream, windows = session(), session()
    bridge = Bridge(upstream, backend(windows))
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
    bridge = Bridge(upstream, backend(windows))
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
    bridge = Bridge(upstream, backend(windows))
    run(bridge.call("windows_PowerShell", {"command": "Get-Date"}))
    windows.call_tool.assert_awaited_once_with("PowerShell", {"command": "Get-Date"})
    result = run(bridge.call("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "PowerShell", "arguments": {"command": "x" * 256_001}}}))
    assert result.structuredContent["success"] is False
    assert windows.call_tool.await_count == 1


def test_windows_tool_pagination():
    upstream, windows = session(), session()
    windows.list_tools.side_effect = [types.ListToolsResult(tools=[types.Tool(name="A", inputSchema={})], nextCursor="next"), types.ListToolsResult(tools=[types.Tool(name="B", inputSchema={})])]
    assert [t.name for t in run(Bridge(upstream, backend(windows)).windows_tools())] == ["A", "B"]
    assert windows.list_tools.call_args_list[1].kwargs == {"cursor": "next"}

def backend(client):
    async def run(operation):
        return await operation(client)

    return SimpleNamespace(run=run)


def recovering_windows(sessions):
    transport_type = getattr(_bridge, "RecoveringWindows", None)
    assert transport_type is not None, "Windows bridge needs a recoverable persistent transport"
    windows = transport_type({"command": "unused-test-command"})
    opened = []
    closed = []

    async def open_session():
        current = sessions.pop(0)
        stack = AsyncExitStack()
        stack.callback(closed.append, current)
        opened.append(current)
        return current, stack

    windows._open_session = open_session
    return windows, opened, closed


def test_windows_session_is_reused_and_safely_reconnected_before_dispatch():
    async def check():
        first = session()
        fresh = session()
        windows, opened, closed = recovering_windows([first, fresh])
        bridge = _bridge.Bridge(session(), windows)

        await bridge.call("windows_PowerShell", {"command": "Get-Date"})
        await bridge.call("windows_PowerShell", {"command": "Get-Date"})
        assert opened == [first]
        assert closed == []
        first.call_tool.assert_has_awaits([
            call("PowerShell", {"command": "Get-Date"}),
            call("PowerShell", {"command": "Get-Date"}),
        ])

        first.list_tools.side_effect = ConnectionError("Windows-MCP child exited")
        result = await bridge.call("windows_PowerShell", {"command": "Get-Date"})
        assert result.structuredContent["ok"] is True
        assert opened == [first, fresh]
        assert closed == [first]
        assert first.call_tool.await_count == 2
        fresh.list_tools.assert_awaited_once_with(cursor=None)
        fresh.call_tool.assert_awaited_once_with("PowerShell", {"command": "Get-Date"})

        await windows.close()
        assert closed == [first, fresh]

    run(check())


def test_windows_action_failure_is_not_replayed_and_next_call_reconnects():
    async def check():
        broken = session()
        broken.call_tool.side_effect = ConnectionError("Windows-MCP child exited")
        fresh = session()
        windows, opened, closed = recovering_windows([broken, fresh])
        bridge = _bridge.Bridge(session(), windows)

        with pytest.raises(ConnectionError, match="Windows-MCP child exited"):
            await bridge.call("windows_PowerShell", {"command": "Get-Date"})

        broken.call_tool.assert_awaited_once_with("PowerShell", {"command": "Get-Date"})
        assert closed == [broken]
        assert opened == [broken]

        result = await bridge.call("windows_PowerShell", {"command": "Get-Date"})
        assert result.structuredContent["ok"] is True
        fresh.call_tool.assert_awaited_once_with("PowerShell", {"command": "Get-Date"})
        assert opened == [broken, fresh]
        await windows.close()
        assert closed == [broken, fresh]

    run(check())


def test_windows_stdio_lifecycle_is_owned_and_closed_by_one_worker_task():
    async def check():
        windows = _bridge.RecoveringWindows({"command": "unused-test-command"})
        opened_tasks = []
        closed_tasks = []

        async def open_session():
            stack = AsyncExitStack()
            opened_tasks.append(asyncio.current_task())

            async def record_close():
                closed_tasks.append(asyncio.current_task())

            stack.push_async_callback(record_close)
            return session(), stack

        windows._open_session = open_session
        await windows.run(lambda current: current.list_tools(cursor=None))
        await windows.close()
        assert opened_tasks == closed_tasks

    run(check())
