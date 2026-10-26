"""Combine existing Gumacu tools and native Windows-MCP in one private tunnel.

Windows-MCP remains an unmodified, separately licensed dependency. This module
only forwards MCP requests; it contains no shell executor or model client.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from pathlib import Path

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

WINDOWS_ID = "windows-main"
PREFIX = "windows_"
ACTIONS = ["windows_catalog", "windows_call"]
MAX_ARGUMENT_BYTES = 256_000


class ResumeSafeInput(ObjectReceiveStream):
    """Initialize a fresh server when a tunnel resumes a cached client session.

    The original tool request is forwarded exactly once. A normal client's
    initialize request passes unchanged. Unlike select-based pipe wrappers,
    this uses the SDK's async streams and works on Windows too.
    """

    def __init__(self, stream):
        self.stream = stream
        self.state = "first"
        self.pending = None
        self.bootstrap_id = "gumacu-bootstrap-" + uuid.uuid4().hex
        self.initialized = anyio.Event()
        self.bootstrap_error = False

    async def receive(self):
        if self.state == "first":
            self.pending = await self.stream.receive()
            if isinstance(self.pending, Exception):
                return self.pending
            if getattr(self.pending.message.root, "method", None) == "initialize":
                self.state = "normal"
                return self.pending
            self.state = "await_initialize"
            return SessionMessage(types.JSONRPCMessage(types.JSONRPCRequest(
                jsonrpc="2.0", id=self.bootstrap_id, method="initialize",
                params={"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "gumacu-resume", "version": "1"}},
            )))
        if self.state == "await_initialize":
            with anyio.fail_after(30):
                await self.initialized.wait()
            if self.bootstrap_error:
                raise RuntimeError("MCP session bootstrap failed")
            self.state = "pending"
            return SessionMessage(types.JSONRPCMessage(types.JSONRPCNotification(
                jsonrpc="2.0", method="notifications/initialized",
            )))
        if self.state == "pending":
            self.state = "normal"
            return self.pending
        return await self.stream.receive()

    async def aclose(self):
        await self.stream.aclose()


class ResumeSafeOutput(ObjectSendStream):
    def __init__(self, stream, input_stream: ResumeSafeInput):
        self.stream = stream
        self.input_stream = input_stream

    async def send(self, message):
        root = message.message.root
        if getattr(root, "id", None) == self.input_stream.bootstrap_id:
            self.input_stream.bootstrap_error = isinstance(root, types.JSONRPCError)
            self.input_stream.initialized.set()
            return
        await self.stream.send(message)

    async def aclose(self):
        await self.stream.aclose()


def json_result(data: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
        isError=False,
    )


def unpack(result: types.CallToolResult) -> dict:
    if result.isError:
        raise ValueError("upstream capability request failed")
    if isinstance(result.structuredContent, dict):
        return deepcopy(result.structuredContent)
    for block in result.content:
        if isinstance(block, types.TextContent):
            value = json.loads(block.text)
            if isinstance(value, dict):
                return value
    raise ValueError("upstream did not return an object")


def with_windows(data: dict, online: bool) -> dict:
    result = deepcopy(data)
    result["devices"] = [d for d in result.get("devices", []) if d.get("id") != WINDOWS_ID]
    result["devices"].append({
        "id": WINDOWS_ID, "os": "Windows", "online": online, "local": True,
        "actions": ACTIONS, "backend": "Windows-MCP", "mode": "direct-tools",
    })
    result["notes"] = "Discover per-device actions; windows-main executes native Windows-MCP tools directly."
    return result


class Bridge:
    def __init__(self, upstream: ClientSession, windows: ClientSession):
        self.upstream = upstream
        self.windows = windows

    async def windows_tools(self) -> list[types.Tool]:
        tools = []
        cursor = None
        while True:
            page = await self.windows.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                return tools

    async def call(self, name: str, arguments: dict) -> types.CallToolResult:
        if name == "agy_capabilities":
            data = unpack(await self.upstream.call_tool(name, arguments))
            try:
                await self.windows_tools()
                online = True
            except Exception:
                online = False
            return json_result(with_windows(data, online))
        if name == "agy_dispatch" and arguments.get("device_id") == WINDOWS_ID:
            action = arguments.get("action")
            args = arguments.get("arguments")
            args = {} if args is None else args
            if not isinstance(args, dict):
                return json_result({"success": False, "error": "arguments must be an object"})
            try:
                if len(json.dumps(args, allow_nan=False).encode("utf-8")) > MAX_ARGUMENT_BYTES:
                    raise ValueError("arguments too large")
            except (ValueError, TypeError):
                return json_result({"success": False, "error": "invalid or oversized arguments"})
            if action == "windows_catalog":
                if args:
                    return json_result({"success": False, "error": "catalog accepts no arguments"})
                tools = await self.windows_tools()
                return json_result({"success": True, "device_id": WINDOWS_ID,
                                    "backend": "Windows-MCP", "tools": [t.model_dump(exclude_none=True) for t in tools]})
            if action == "windows_call":
                if set(args) - {"name", "arguments"} or not isinstance(args.get("name"), str):
                    return json_result({"success": False, "error": "provide name and optional arguments"})
                native_args = args.get("arguments")
                return await self.call_windows(args["name"], {} if native_args is None else native_args)
            return json_result({"success": False, "error": "action not available; call agy_capabilities"})
        if name.startswith(PREFIX):
            return await self.call_windows(name[len(PREFIX):], arguments)
        return await self.upstream.call_tool(name, arguments)

    async def call_windows(self, name: str, arguments: dict) -> types.CallToolResult:
        allowed = {t.name for t in await self.windows_tools()}
        if name not in allowed or not isinstance(arguments, dict):
            return json_result({"success": False, "error": "unknown Windows tool or invalid arguments"})
        return await self.windows.call_tool(name, arguments)


def make_server(config: dict) -> tuple[Server, InitializationOptions]:
    initialization_options: InitializationOptions

    @asynccontextmanager
    async def lifespan(server):
        async with AsyncExitStack() as stack:
            sessions = []
            for entry in (config["upstream"], config["windows"]):
                params = StdioServerParameters(**entry)
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                initialized = await session.initialize()
                if not sessions:
                    initialization_options.instructions = (initialized.instructions or "") + (
                        "\n\nNative Windows: windows-main uses direct Windows-MCP tools, "
                        "not Antigravity jobs. Discover windows_catalog and windows_call "
                        "through agy_capabilities/agy_dispatch. Prefer PowerShell/FileSystem "
                        "for background work. Desktop input respects user ownership."
                    )
                sessions.append(session)
            yield Bridge(*sessions)

    server = Server("gumacu-windows-bridge", version="1.0.0", lifespan=lifespan)

    def bridge() -> Bridge:
        return server.request_context.lifespan_context

    @server.list_tools()
    async def list_tools():
        tools = []
        cursor = None
        while True:
            page = await bridge().upstream.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        try:
            for tool in await bridge().windows_tools():
                tools.append(tool.model_copy(update={"name": PREFIX + tool.name}))
        except Exception:
            # Keep the server catalog usable if Windows tool discovery fails.
            pass
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        return await bridge().call(name, arguments)

    @server.list_resources()
    async def list_resources():
        return (await bridge().upstream.list_resources()).resources

    async def read_resource(request):
        response = await bridge().upstream.read_resource(request.params.uri)
        return types.ServerResult(response)

    server.request_handlers[types.ReadResourceRequest] = read_resource

    @server.list_prompts()
    async def list_prompts():
        return (await bridge().upstream.list_prompts()).prompts

    @server.get_prompt()
    async def get_prompt(name, arguments):
        return await bridge().upstream.get_prompt(name, arguments)

    initialization_options = server.create_initialization_options()
    return server, initialization_options


async def main(config_path: Path):
    server, options = make_server(json.loads(config_path.read_text(encoding="utf-8-sig")))
    async with stdio_server() as (read, write):
        resume_input = ResumeSafeInput(read)
        await server.run(resume_input, ResumeSafeOutput(write, resume_input), options)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    asyncio.run(main(parser.parse_args().config))
