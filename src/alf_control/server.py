"""Alf Control MCP server. ChatGPT makes decisions; tools execute directly."""
from __future__ import annotations

import anyio
from mcp.server.fastmcp import FastMCP

from alf_control import __version__
from alf_control.capability_gateway import catalog, register_capability_tools
from alf_control.config import profile

mcp = FastMCP("Alf Control", instructions=(
    "Discover alf_capabilities, then alf_dispatch to the owning device. "
    "Read ssh_catalog/windows_catalog before choosing native tool arguments. "
    "ChatGPT directs every operation. Failed operations must not be blindly replayed."
))
mcp._mcp_server.version = __version__
register_capability_tools(mcp)

async def alf_doctor(force_refresh: bool = False) -> dict:
    """Probe direct transport availability without requiring any model CLI or auth."""
    del force_refresh  # retained for the legacy agy_doctor wire schema
    try:
        report = await anyio.to_thread.run_sync(catalog)
    except (OSError, TypeError, ValueError):
        return {"success": False, "version": __version__, "error": "invalid operator configuration"}
    healthy = bool(report["devices"]) and all(d["online"] for d in report["devices"])
    return {"success": healthy, "version": __version__,
            "report": {"healthy": healthy, "devices": report["devices"],
                       "client_visible_tools": "not_observable_from_mcp_server"}}

mcp.tool(name="alf_doctor")(alf_doctor)
mcp.tool(name="agy_doctor")(alf_doctor)


def run(transport: str = "stdio", port: int = 8765) -> None:
    if transport not in {"stdio", "streamable-http"} or not 1 <= port <= 65535:
        raise ValueError("invalid MCP transport or port")
    if profile() == "read-only":
        for name in ("alf_dispatch", "agy_dispatch"):
            mcp.remove_tool(name)
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = port
    mcp.run(transport=transport)
