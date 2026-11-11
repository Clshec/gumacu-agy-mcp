"""Routing isolation and the actual MCP wire contract for the renamed server."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from alf_control import capability_gateway as gateway
from alf_control.config import devices


def registry(tmp_path, monkeypatch, entries):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps({"devices": entries}), encoding="utf-8")
    monkeypatch.setenv("ALF_CONTROL_DEVICES_FILE", str(path))


def test_two_provider_routes_cannot_cross_or_override_socket(tmp_path, monkeypatch):
    one, two = str(tmp_path / "one.sock"), str(tmp_path / "two.sock")
    registry(tmp_path, monkeypatch, [
        {"id": "netcup-a", "provider": "netcup", "socket": one},
        {"id": "contabo-b", "provider": "contabo", "socket": two},
    ])
    calls = []

    def transport(message, *, socket_path):
        calls.append((socket_path, message))
        return {"success": True, "result": {"ok": True}}

    monkeypatch.setattr(gateway, "_call_socket", transport)
    assert [d["provider"] for d in gateway.catalog()["devices"]] == ["netcup", "contabo"]
    calls.clear()
    assert gateway.dispatch("contabo-b", "ssh_call", {
        "name": "ssh_exec", "arguments": {"command": "hostname"},
    })["success"]
    assert calls == [(two, {"kind": "call", "name": "ssh_exec", "arguments": {"command": "hostname"}})]
    calls.clear()
    for device, action, args in [
        ("missing", "ssh_catalog", {}),
        ("netcup-a", "start", {"PROMPT": "do work"}),
        ("netcup-a", "ssh_catalog", {"socket": two}),
        ("netcup-a", "ssh_call", {"name": "ssh_exec", "socket": two}),
        ("netcup-a", "ssh_call", {"name": "ssh_exec", "arguments": []}),
        ("netcup-a", "ssh_call", {"name": "ssh_exec", "arguments": {"x": "z" * 256_001}}),
        ("netcup-a", "ssh_call", {"name": "ssh_exec", "arguments": {"x": float("nan")}}),
    ]:
        assert not gateway.dispatch(device, action, args)["success"]
    assert calls == []


@pytest.mark.parametrize("entries", [
    [{"id": "same", "socket": "/run/a"}, {"id": "same", "socket": "/run/b"}],
    [{"id": "windows-main", "socket": "/run/a"}],
    [{"id": "bad/id", "socket": "/run/a"}],
    [{"id": "valid", "socket": "relative.sock"}],
    [{"id": "valid", "socket": "/run/a", "backend": "agent"}],
    [],
])
def test_invalid_operator_registry_fails_closed(entries, tmp_path, monkeypatch):
    registry(tmp_path, monkeypatch, entries)
    with pytest.raises((ValueError, TypeError)):
        devices()


def test_ssh_settings_keep_existing_install_and_prefer_new_name(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_SSH_SOCKET", str(tmp_path / "old.sock"))
    assert devices()[0].socket == str(tmp_path / "old.sock")
    monkeypatch.setenv("ALF_CONTROL_SSH_SOCKET", str(tmp_path / "new.sock"))
    assert devices()[0].socket == str(tmp_path / "new.sock")


def test_legacy_read_only_profile_cannot_dispatch(monkeypatch):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "read-only")
    called = []
    monkeypatch.setattr(gateway, "_call_socket", lambda *a, **k: called.append(1))
    assert not gateway.dispatch("linux-netcup", "ssh_call", {"name": "ssh_exec"})["success"]
    assert called == []


@pytest.mark.parametrize("module", ["alf_control", "agy_mcp", "agy_mcp.cli"])
async def test_real_stdio_identifies_alf_and_rejects_removed_execution(module, tmp_path):
    # Deliberately unusable agent settings must not affect direct initialization.
    params = StdioServerParameters(command=sys.executable, args=["-m", module], env={
        "ALF_CONTROL_SSH_SOCKET": str(tmp_path / "absent.sock"),
        "AGY_BIN": "/does/not/exist", "GEMINI_BIN": "/does/not/exist",
        "AGY_MCP_CONFIG": "/does/not/exist", "PYTHONUTF8": "1",
        **({"PROGRAMDATA": os.environ["PROGRAMDATA"]} if "PROGRAMDATA" in os.environ else {}),
    })
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Alf Control"
            assert initialized.serverInfo.version == "0.2.0"
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            assert names == {"alf_capabilities", "alf_dispatch", "alf_doctor",
                             "agy_capabilities", "agy_dispatch", "agy_doctor"}
            schemas = {t.name: t.inputSchema for t in listed.tools}
            assert schemas["alf_dispatch"] == schemas["agy_dispatch"]
            for canonical, legacy in [("alf_capabilities", "agy_capabilities"), ("alf_doctor", "agy_doctor")]:
                new = await session.call_tool(canonical, {})
                old = await session.call_tool(legacy, {})
                assert new.structuredContent == old.structuredContent
            for removed in ["agy", "agy_start", "agy_device_start", "agy_pipeline_start"]:
                denied = await session.call_tool(removed, {"PROMPT": "do work"})
                assert denied.isError
            result = await session.call_tool("alf_dispatch", {
                "device_id": "linux-netcup", "action": "start", "arguments": {"PROMPT": "do work"},
            })
            assert not result.structuredContent["success"]


def test_no_agent_execution_or_dependencies_are_packaged():
    source = Path(__file__).parents[1] / "src"
    forbidden = {"adapters", "device_agent.py", "device_gateway.py", "bridge.py",
                 "supervisor.py", "runtime_control.py", "telegram_bot.py", "_skill_bodies"}
    assert not any(p.name in forbidden for p in source.rglob("*"))
