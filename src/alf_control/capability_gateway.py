"""Stable, provider-independent facade over private SSH MCP services."""
from __future__ import annotations

import json
from functools import partial
from typing import Any

import anyio

from alf_control.config import devices, profile
from alf_control.ssh_sidecar import _call_socket

ACTIONS = ("ssh_catalog", "ssh_call")
MAX_ARGUMENT_BYTES = 256_000

def catalog() -> dict[str, Any]:
    result = []
    for device in devices():
        probe = _call_socket({"kind": "list"}, socket_path=device.socket)
        result.append({"id": device.id, "os": device.os, "provider": device.provider,
                       "online": bool(probe.get("success")), "local": True,
                       "actions": [] if profile() == "read-only" else list(ACTIONS)})
    return {"success": True, "schema_version": 1, "devices": result,
            "gateway_available": any(d["online"] for d in result),
            "notes": "Discover the owning device and its tool schema before dispatch."}

def dispatch(device_id: str, action: str, arguments: dict[str, Any] | None = None) -> dict:
    if profile() == "read-only":
        return {"success": False, "error": "dispatch disabled in read-only profile"}
    if not isinstance(device_id, str) or not isinstance(action, str):
        return {"success": False, "error": "device_id and action must be strings"}
    args = {} if arguments is None else arguments
    if not isinstance(args, dict) or not all(isinstance(k, str) for k in args):
        return {"success": False, "error": "arguments must be an object"}
    try:
        if len(json.dumps(args, allow_nan=False).encode()) > MAX_ARGUMENT_BYTES:
            raise ValueError
    except (ValueError, TypeError):
        return {"success": False, "error": "invalid or oversized arguments"}
    device = next((d for d in devices() if d.id == device_id), None)
    if device is None:
        return {"success": False, "error": "device not registered"}
    if action == "ssh_catalog":
        if args:
            return {"success": False, "error": "catalog accepts no arguments"}
        return _call_socket({"kind": "list"}, socket_path=device.socket)
    if action == "ssh_call":
        if set(args) - {"name", "arguments"} or not isinstance(args.get("name"), str):
            return {"success": False, "error": "provide name and optional arguments"}
        native = args.get("arguments", {})
        if not isinstance(native, dict):
            return {"success": False, "error": "tool arguments must be an object"}
        return _call_socket({"kind": "call", "name": args["name"], "arguments": native},
                            socket_path=device.socket)
    return {"success": False, "error": "action not authorized on this device"}

def register_capability_tools(mcp: Any) -> None:
    async def alf_capabilities() -> dict[str, Any]:
        """Discover connected devices and authorized actions live."""
        try:
            return await anyio.to_thread.run_sync(catalog)
        except (OSError, TypeError, ValueError):
            return {"success": False, "error": "invalid or unreadable operator configuration"}

    async def alf_dispatch(device_id: str, action: str,
                           arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an advertised action on its owning device. Discover alf_capabilities first.

        For ssh_call, arguments contains name and arguments from ssh_catalog.
        Never automatically replay a failed operation: its outcome may be unknown.
        """
        try:
            return await anyio.to_thread.run_sync(partial(dispatch, device_id, action, arguments))
        except (OSError, TypeError, ValueError):
            return {"success": False, "error": "invalid or unreadable operator configuration"}

    mcp.tool(name="alf_capabilities")(alf_capabilities)
    mcp.tool(name="alf_dispatch")(alf_dispatch)
    # Wire aliases only: no legacy model execution is registered or imported.
    mcp.tool(name="agy_capabilities")(alf_capabilities)
    mcp.tool(name="agy_dispatch")(alf_dispatch)
