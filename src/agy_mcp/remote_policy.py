"""Opt-in remote tool/request restrictions, not an operating-system sandbox."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"agy_status", "agy_read", "agy_result_page", "agy_sessions"})
CONTROLLED_TOOLS = READ_TOOLS | {"agy_start", "agy_cancel"}


def remote_profile() -> str:
    profile = os.environ.get("GUMACU_REMOTE_PROFILE", "local")
    if profile not in {"local", "read-only", "controlled"}:
        raise ValueError("invalid GUMACU_REMOTE_PROFILE")
    return profile


def restrict_request(payload: dict[str, Any]) -> dict[str, Any]:
    profile = remote_profile()
    if profile == "local":
        return payload
    if profile == "read-only":
        raise ValueError("remote profile does not permit starting jobs")
    root_value = os.environ.get("GUMACU_ALLOWED_WORKSPACE", "")
    if not root_value or not Path(root_value).is_absolute():
        raise ValueError("controlled profile requires an absolute approved workspace")
    root = Path(root_value).resolve(strict=True)
    requested = payload.get("cwd", ".")
    cwd = root if requested == "." else Path(requested).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to(root):
        raise ValueError("workspace is outside the approved root")
    if payload.get("allow_write") or payload.get("mode", "ask") not in {"ask", "plan"}:
        raise ValueError("remote execution/write modes are not enabled")
    if payload.get("extra_env"):
        raise ValueError("remote environment overrides are not allowed")
    if payload.get("backend", "auto") not in {"auto", "agy"}:
        raise ValueError("this remote endpoint permits only the agy backend")
    if payload.get("worktree"):
        raise ValueError("remote worktree creation is not enabled")
    if not 1 <= payload.get("timeout", 900) <= 1200:
        raise ValueError("remote timeout must be 1..1200 seconds")
    return {**payload, "cwd": str(cwd), "sandbox": True, "backend": "agy",
            "allow_write": False, "worktree": False, "debug": False}


def restrict_tools(mcp: Any) -> None:
    profile = remote_profile()
    if profile == "local":
        return
    allowed = READ_TOOLS if profile == "read-only" else CONTROLLED_TOOLS
    for name in list(mcp._tool_manager._tools):
        if name not in allowed:
            mcp.remove_tool(name)
