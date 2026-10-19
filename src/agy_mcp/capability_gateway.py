"""Stable MCP facade over live, allowlisted Gumacu device capabilities.

MCP clients cache tool schemas.  Keep agy_capabilities and agy_dispatch stable;
discover devices and their authorized actions from the private broker on EVERY
call instead of generating an MCP tool for every new device/capability.
"""

from __future__ import annotations

import json
import re
from functools import partial
from typing import Any, Callable

import anyio

from agy_mcp.device_tools import gateway_call, submit

LOCAL_DEVICE_ID = "linux-netcup"
ACTIONS = ("start", "status", "read", "result", "cancel", "sessions", "doctor",
           "interactive_open", "interactive_send", "btw", "interactive_snapshot")
_ACTION_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_ARGUMENT_BYTES = 256_000


def catalog() -> dict[str, Any]:
    """Read the live registry; never cache availability or remote actions."""
    devices = [{
        "id": LOCAL_DEVICE_ID,
        "os": "Linux",
        "online": True,
        "local": True,
        "actions": list(ACTIONS),
    }]
    reply = gateway_call("/v1/admin/devices")
    if reply.get("success") and isinstance(reply.get("devices"), list):
        for device in reply["devices"]:
            if not isinstance(device, dict):
                continue
            device_id = device.get("id")
            if not isinstance(device_id, str) or device_id == LOCAL_DEVICE_ID:
                continue
            actions = device.get("allowed_actions", [])
            if not isinstance(actions, list):
                actions = []
            devices.append({
                "id": device_id,
                "os": str(device.get("os", "unknown")),
                "online": bool(device.get("online", False)),
                "local": False,
                "actions": sorted({
                    a for a in actions
                    if isinstance(a, str) and _ACTION_NAME.fullmatch(a)
                }),
                "last_seen": device.get("last_seen"),
            })
    return {
        "success": True,
        "schema_version": 1,
        "devices": devices,
        "gateway_available": bool(reply.get("success")),
        "notes": "Only allowlisted actions are executable; no raw shell/SSH dispatch.",
    }


def _arguments(args: dict[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if not all(isinstance(k, str) for k in args):
        raise ValueError("argument names must be strings")
    try:
        size = len(json.dumps(args, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("arguments must be JSON-serializable") from exc
    if size > _MAX_ARGUMENT_BYTES:
        raise ValueError("arguments exceed maximum request size")
    return args


def invoke_remote(device_id: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Gateway and agent each enforce their own device and action allowlists."""
    reply = gateway_call("/v1/admin/devices")
    if not reply.get("success"):
        return {"success": False, "error": "private device gateway unavailable"}
    devices = reply.get("devices", [])
    device = next(
        (d for d in devices if isinstance(d, dict) and d.get("id") == device_id),
        None,
    ) if isinstance(devices, list) else None
    if device is None:
        return {"success": False, "error": "device not registered"}
    if not device.get("online"):
        return {"success": False, "error": "device offline"}
    if action not in device.get("allowed_actions", []):
        return {"success": False, "error": "action not authorized on this device"}
    return submit(device_id, action, args)


def register_capability_tools(mcp: Any, local: dict[str, Callable[..., Any]]) -> None:
    """Register TWO long-lived schemas independent of future remote devices."""

    @mcp.tool(name="agy_capabilities")
    async def agy_capabilities() -> dict[str, Any]:
        """Discover authorized actions and connected Linux/Windows devices live."""
        return await anyio.to_thread.run_sync(catalog)

    @mcp.tool(name="agy_dispatch")
    async def agy_dispatch(
        device_id: str,
        action: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke an advertised, permission-checked action on its owning device.

        Call agy_capabilities first to discover current devices and action names.
        This tool's JSON schema does not change when backend actions are added.
        """
        if not isinstance(device_id, str) or not isinstance(action, str):
            return {"success": False, "error": "device_id and action must be strings"}
        if not _ACTION_NAME.fullmatch(action):
            return {"success": False, "error": "invalid action name"}
        try:
            args = _arguments(arguments)
            if device_id == LOCAL_DEVICE_ID:
                handler = local.get(action)
                if handler is None:
                    return {"success": False, "error": "local action not permitted"}
                result = await anyio.to_thread.run_sync(partial(handler, **args))
                return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            return await anyio.to_thread.run_sync(partial(invoke_remote, device_id, action, args))
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": f"invalid action arguments: {type(exc).__name__}"}
