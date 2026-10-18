"""Regression tests for the two stable, dynamic MCP tools."""

from __future__ import annotations

import asyncio

from agy_mcp import capability_gateway as gw
from agy_mcp.device_gateway import Broker, load_devices


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name):
        def register(fn):
            self.tools[name] = fn
            return fn
        return register


def test_live_catalog_refreshes_without_changing_mcp_schema(monkeypatch):
    backend = {
        "devices": [
            {"id": "windows-main", "os": "Windows", "online": True,
             "allowed_actions": ["start", "status"], "last_seen": 10},
        ],
    }
    monkeypatch.setattr(gw, "gateway_call", lambda path: {"success": True, **backend})
    mcp = FakeMcp()
    gw.register_capability_tools(mcp, {"status": lambda job_id: {"job_id": job_id}})
    assert set(mcp.tools) == {"agy_capabilities", "agy_dispatch"}
    first = asyncio.run(mcp.tools["agy_capabilities"]())
    assert first["devices"][1]["actions"] == ["start", "status"]

    # Simulates a newly enrolled agent and a new broker-allowlisted action.
    backend["devices"].append({
        "id": "linux-edge", "online": True, "os": "Linux",
        "allowed_actions": ["status", "ext.camera.snapshot"],
    })
    second = asyncio.run(mcp.tools["agy_capabilities"]())
    assert second["devices"][2]["id"] == "linux-edge"
    assert second["devices"][2]["actions"] == ["ext.camera.snapshot", "status"]
    assert set(mcp.tools) == {"agy_capabilities", "agy_dispatch"}


def test_dispatch_routes_only_explicitly_authorized_live_device_actions(monkeypatch):
    remote = {
        "devices": [{
            "id": "windows-main", "online": True,
            "allowed_actions": ["start", "status"],
        }],
    }
    dispatched = []
    monkeypatch.setattr(gw, "gateway_call", lambda path: {"success": True, **remote})
    monkeypatch.setattr(
        gw, "submit",
        lambda d, action, payload: dispatched.append((d, action, payload))
        or {"success": True},
    )
    mcp = FakeMcp()
    gw.register_capability_tools(mcp, {})
    dispatch = mcp.tools["agy_dispatch"]
    req = {"PROMPT": "Say hello", "timeout": 0}
    assert asyncio.run(dispatch("windows-main", "start", req))["success"] is True
    assert dispatched == [("windows-main", "start", req)]
    assert not asyncio.run(dispatch("windows-main", "cancel", {"job_id": "job_a"}))["success"]
    assert not asyncio.run(dispatch("windows-main", "shell", {"command": "whoami"}))["success"]
    assert len(dispatched) == 1

    remote["devices"][0]["online"] = False
    assert not asyncio.run(dispatch("windows-main", "start", req))["success"]
    assert len(dispatched) == 1


def test_dispatch_local_is_allowlisted_and_validated():
    mcp = FakeMcp()
    called = []
    gw.register_capability_tools(
        mcp,
        {"start": lambda PROMPT, timeout=0: called.append((PROMPT, timeout))
         or {"success": True, "job_id": "job_test"}},
    )
    dispatch = mcp.tools["agy_dispatch"]
    result = asyncio.run(dispatch("linux-netcup", "start", {"PROMPT": "hello"}))
    assert result["success"] and called == [("hello", 0)]
    assert not asyncio.run(dispatch("linux-netcup", "not_a_tool", {}))["success"]
    assert not asyncio.run(dispatch("linux-netcup", "start", {"PROMPT": "hi", "bad": 1}))["success"]


def test_gateway_broker_reports_only_configured_actions(tmp_path):
    broker = Broker(
        tmp_path / "broker.sqlite",
        {"windows-main": {
            "os": "Windows", "token_sha256": "0" * 64,
            "allowed_actions": ["status"],
        }},
    )
    try:
        devices = broker.list_devices()
        assert devices[0]["allowed_actions"] == ["status"]
    finally:
        broker.close()


def test_remote_catalog_denies_unavailable_gateway(monkeypatch):
    monkeypatch.setattr(gw, "gateway_call", lambda path: {"success": False, "error": "offline"})
    data = gw.catalog()
    assert not data["gateway_available"]
    assert [d["id"] for d in data["devices"]] == ["linux-netcup"]
    assert not gw.invoke_remote("windows-main", "start", {"PROMPT": "hi"})["success"]
