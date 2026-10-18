"""Opt-in remote tool/request restrictions, not an operating-system sandbox."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({
    "agy_status", "agy_read", "agy_result_page", "agy_sessions",
    "agy_pipeline_status", "agy_notifications", "agy_notification_ack",
    "agy_active_jobs",
})
DEVICE_TOOLS = frozenset({
    "agy_devices", "agy_device_call", "agy_device_start",
    "agy_device_continue", "agy_device_status", "agy_device_read",
    "agy_device_result", "agy_device_cancel", "agy_device_sessions",
    "agy_device_doctor",
})
CONTROLLED_TOOLS = READ_TOOLS | {"agy_start", "agy_cancel", "agy_pipeline_start", "agy_pipeline_cancel"} | DEVICE_TOOLS
OPERATOR_TOOLS = CONTROLLED_TOOLS | {"agy", "agy_continue", "agy_result", "agy_doctor"}
VALID_PROFILES = frozenset({"local", "read-only", "controlled", "controlled-write", "operator"})


def remote_profile() -> str:
    profile = os.environ.get("GUMACU_REMOTE_PROFILE", "local")
    if profile not in VALID_PROFILES:
        raise ValueError("invalid GUMACU_REMOTE_PROFILE")
    return profile


def get_approved_workspaces() -> list[Path]:
    raw_list: list[str] = []
    if "GUMACU_ALLOWED_WORKSPACES" in os.environ:
        raw = os.environ["GUMACU_ALLOWED_WORKSPACES"]
        delimiters = r"[,;]+" if os.name == "nt" else r"[:,;]+"
        parts = re.split(delimiters, raw)
        raw_list.extend(p.strip() for p in parts if p.strip())
    elif "GUMACU_ALLOWED_WORKSPACE" in os.environ:
        val = os.environ["GUMACU_ALLOWED_WORKSPACE"].strip()
        if val:
            raw_list.append(val)

    if not raw_list:
        raise ValueError("controlled profile requires an absolute approved workspace")

    roots: list[Path] = []
    for item in raw_list:
        p = Path(item)
        if not p.is_absolute():
            raise ValueError(f"approved workspace must be absolute: {item}")
        try:
            resolved = p.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"approved workspace path error {item}: {exc}")
        if not resolved.is_dir():
            raise ValueError(f"approved workspace is not a directory: {item}")
        roots.append(resolved)

    if not roots:
        raise ValueError("controlled profile requires an absolute approved workspace")
    return roots


def validate_workspace(requested: str, roots: list[Path]) -> Path:
    if not requested or requested == ".":
        return roots[0]
    try:
        target = Path(requested).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"workspace path error: {exc}")
    if not target.is_dir() or not any(target == r or target.is_relative_to(r) for r in roots):
        raise ValueError("workspace is outside the approved root")
    return target


def is_operator_authorized(profile: str, payload: dict[str, Any]) -> bool:
    if profile in {"local", "operator", "controlled-write"}:
        return True
    server_token = os.environ.get("GUMACU_OPERATOR_TOKEN", "").strip()
    client_token = payload.get("operator_token")
    if client_token is not None:
        if server_token and secrets.compare_digest(str(client_token), server_token):
            return True
        raise ValueError("invalid operator token")
    return False


def restrict_request(payload: dict[str, Any]) -> dict[str, Any]:
    clean_payload = {k: v for k, v in payload.items() if k not in {"operator_token", "cd"}}
    profile = remote_profile()
    if profile == "local":
        return clean_payload
    if profile == "read-only":
        raise ValueError("remote profile does not permit starting jobs")

    roots = get_approved_workspaces()
    requested_cwd = payload.get("cwd") or payload.get("cd") or "."
    cwd = validate_workspace(str(requested_cwd), roots)

    operator_auth = is_operator_authorized(profile, payload)

    mode = payload.get("mode", "ask")
    allow_write = bool(payload.get("allow_write", False))

    # ``long`` is an execution-lifecycle hint, not a request for write
    # privileges. The agy adapter treats it like ask mode while the MCP
    # supervisor provides detachment and monitoring.
    if allow_write or mode not in {"ask", "plan", "long"}:
        if not operator_auth:
            raise ValueError("remote execution/write modes are not enabled")
        if mode not in {"ask", "plan", "long", "execute"}:
            raise ValueError(f"unsupported mode: {mode}")

    if payload.get("extra_env"):
        raise ValueError("remote environment overrides are not allowed")
    if payload.get("backend", "auto") not in {"auto", "agy"}:
        raise ValueError("this remote endpoint permits only the agy backend")
    if payload.get("worktree") and not operator_auth:
        raise ValueError("remote worktree creation is not enabled")
    detach = bool(payload.get("detach", False))
    # Enforce >=14400 sec default for DETACHED/LONG tasks; short tasks stay at 900s; upper limit 86400s
    default_timeout = 14400 if (mode == "long" or detach) else 900
    timeout_raw = payload.get("timeout")
    timeout_val = default_timeout if timeout_raw is None else int(timeout_raw)
    if not 1 <= timeout_val <= 86400:
        raise ValueError("remote timeout must be 1..86400 seconds")

    sandbox = not (operator_auth and allow_write)
    worktree = bool(payload.get("worktree", False)) if operator_auth else False
    clean_payload = {k: v for k, v in payload.items() if k not in {"operator_token", "cd"}}

    return {
        **clean_payload,
        "cwd": str(cwd),
        "sandbox": sandbox,
        "backend": "agy",
        "allow_write": allow_write if operator_auth else False,
        "mode": mode,
        "timeout": timeout_val,
        "worktree": worktree,
        "debug": False,
    }


def restrict_tools(mcp: Any) -> None:
    profile = remote_profile()
    if profile == "local":
        return
    if profile == "read-only":
        allowed = READ_TOOLS
    elif profile == "controlled":
        allowed = CONTROLLED_TOOLS
    elif profile in {"operator", "controlled-write"}:
        allowed = OPERATOR_TOOLS
    else:
        allowed = READ_TOOLS

    for name in list(mcp._tool_manager._tools):
        if name not in allowed:
            mcp.remove_tool(name)
