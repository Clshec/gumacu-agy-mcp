"""Opt-in MCP tools for Windows/Linux device routing via a private broker.

Legacy agy_* tools deliberately remain local and unchanged.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pathlib import Path

from agy_mcp.device_gateway import MAX_BODY


def gateway_call(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = os.environ.get("AGY_DEVICE_GATEWAY_URL", "")
    if not url:
        url = "http://127.0.0.1:8766"
    token = os.environ.get("AGY_DEVICE_ADMIN_TOKEN", "")
    if len(token) < 32:
        env_file = Path("/etc/gumacu-agy-devices/gateway.env")
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("AGY_DEVICE_ADMIN_TOKEN=") and "=" in line:
                        token = line.split("=", 1)[1].strip().strip("'\"")
                        break
            except OSError:
                pass
    if not url or not (url.startswith("http://127.0.0.1:")
                       or url.startswith("http://localhost:")):
        return {"success": False, "error": "private device gateway is not configured"}
    if len(token) < 32:
        return {"success": False, "error": "gateway admin credential not configured"}
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        if data is not None and len(data) > MAX_BODY:
            return {"success": False, "error": "request too large"}
        request = Request(f"{url.rstrip('/')}/{path.lstrip('/')}", data=data,
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"},
                          method="POST" if payload is not None else "GET")
        with urlopen(request, timeout=12) as response:
            return json.loads(response.read(MAX_BODY + 1))
    except (OSError, ValueError, HTTPError, URLError, TimeoutError):
        return {"success": False, "error": "device gateway is unavailable or denied access"}


def _notify_if_terminal(device: str, ident: str, result: dict[str, Any]) -> None:
    # Deprecated: Remote device lifecycle notifications are handled centrally
    # and durably by Broker in device_gateway to avoid duplicate delivery.
    pass


def await_call(ident: str, seconds: float = 10.0) -> dict[str, Any]:
    until = time.monotonic() + seconds
    while True:
        status = gateway_call("/v1/admin/get", {"request_id": ident})
        if not status.get("success"):
            return status
        if status.get("state") == "done":
            result = status.get("result") or {}
            out = {"device_id": status.get("device_id"), "request_id": ident, **result}
            return out
        if time.monotonic() >= until:
            return {"success": True, "pending": True, "request_id": ident,
                    "device_id": status.get("device_id"), "state": status.get("state"),
                    "note": "Call agy_device_call to retrieve the delayed response."}
        time.sleep(0.4)


def submit(device_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    reply = gateway_call("/v1/admin/submit",
                         {"device_id": device_id, "action": action, "payload": payload})
    if not reply.get("success"):
        return reply
    return await_call(reply["request_id"])


def register_device_tools(mcp: Any) -> None:
    """Attach device-specific tools without changing the legacy Linux API."""

    @mcp.tool(name="agy_devices")
    def agy_devices() -> dict[str, Any]:
        """List enrolled Windows/Linux Antigravity agents and heartbeat health."""
        return gateway_call("/v1/admin/devices")

    @mcp.tool(name="agy_device_call")
    def agy_device_call(request_id: str) -> dict[str, Any]:
        """Retrieve an outstanding remote operation by its exact request ID."""
        return await_call(request_id, seconds=1)

    @mcp.tool(name="agy_device_start")
    def agy_device_start(
        device_id: str, PROMPT: str, cwd: str = ".",
        SESSION_ID: str | None = None, mode: str = "ask",
        allow_write: bool = False, timeout: int = 14400,
    ) -> dict[str, Any]:
        """Start Antigravity on an explicitly selected device; original agy_start stays Linux-local."""
        if mode not in {"ask", "plan", "long", "execute"} or not 1 <= timeout <= 86400:
            return {"success": False, "error": "invalid mode or timeout"}
        return submit(device_id, "start",
                      {"PROMPT": PROMPT, "cwd": cwd, "SESSION_ID": SESSION_ID,
                       "mode": mode, "allow_write": allow_write, "timeout": timeout})

    @mcp.tool(name="agy_device_continue")
    def agy_device_continue(
        device_id: str, SESSION_ID: str, PROMPT: str,
        cwd: str = ".", mode: str = "ask", allow_write: bool = False,
        timeout: int = 14400,
    ) -> dict[str, Any]:
        """Resume exactly the given device's Antigravity session as a detached job."""
        if not SESSION_ID:
            return {"success": False, "error": "SESSION_ID required"}
        if mode not in {"ask", "plan", "long", "execute"} or not 1 <= timeout <= 86400:
            return {"success": False, "error": "invalid mode or timeout"}
        return submit(device_id, "start",
                      {"PROMPT": PROMPT, "SESSION_ID": SESSION_ID,
                       "cwd": cwd, "mode": mode, "allow_write": allow_write, "timeout": timeout})

    @mcp.tool(name="agy_device_status")
    def agy_device_status(device_id: str, job_id: str) -> dict[str, Any]:
        """Get status of a job from the named device, not a different host."""
        return submit(device_id, "status", {"job_id": job_id})

    @mcp.tool(name="agy_device_read")
    def agy_device_read(device_id: str, job_id: str, since: int = 0,
                        limit: int = 100) -> dict[str, Any]:
        """Read incremental Antigravity events for a device-specific job."""
        return submit(device_id, "read",
                      {"job_id": job_id, "since": since, "limit": limit})

    @mcp.tool(name="agy_device_result")
    def agy_device_result(device_id: str, job_id: str) -> dict[str, Any]:
        """Get a completed job's stored result on the specified device."""
        return submit(device_id, "result", {"job_id": job_id})

    @mcp.tool(name="agy_device_cancel")
    def agy_device_cancel(device_id: str, job_id: str) -> dict[str, Any]:
        """Cancel an Antigravity job on its owning device."""
        return submit(device_id, "cancel", {"job_id": job_id})

    @mcp.tool(name="agy_device_sessions")
    def agy_device_sessions(device_id: str) -> dict[str, Any]:
        """List recent local Antigravity sessions on the selected device."""
        return submit(device_id, "sessions", {})

    @mcp.tool(name="agy_device_doctor")
    def agy_device_doctor(device_id: str) -> dict[str, Any]:
        """Diagnose native Antigravity CLI authentication/capabilities on a device."""
        return submit(device_id, "doctor", {})
