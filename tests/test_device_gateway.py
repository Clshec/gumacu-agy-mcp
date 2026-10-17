"""Regression/security tests for multi-device routing (no real agy required)."""
from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agy_mcp.device_agent import AgentCache, dispatch
from agy_mcp.device_gateway import Broker, make_handler, token_hash


@pytest.fixture
def broker(tmp_path: Path) -> Broker:
    instance = Broker(tmp_path / "broker.db", {
        "windows-main": {"os": "Windows", "token_sha256": token_hash("w" * 40),
                         "allowed_actions": ["start", "status", "cancel"]},
        "netcup-main": {"os": "Linux", "token_sha256": token_hash("l" * 40),
                        "allowed_actions": ["start", "status"]},
    })
    yield instance
    instance.conn.close()


def test_devices_heartbeat_and_offline(broker: Broker) -> None:
    assert all(not n["online"] for n in broker.list_devices())
    broker.heartbeat("windows-main", {"platform": "Windows"})
    assert broker.list_devices()[1]["online"] is True
    assert broker.list_devices()[1]["capabilities"]["platform"] == "Windows"


def test_request_lifecycle_and_session_affinity(broker: Broker) -> None:
    ident = broker.submit("windows-main", "start", {"PROMPT": "hello"})
    assert broker.get(ident)["state"] == "queued"
    work = broker.poll("windows-main")
    assert work is not None and work["id"] == ident
    assert broker.poll("windows-main") is None
    with pytest.raises(ValueError, match="leased"):
        broker.finish("netcup-main", ident, work["claim"], {})
    with pytest.raises(ValueError, match="proof"):
        broker.finish("windows-main", ident, "wrong", {})
    broker.finish("windows-main", ident, work["claim"],
                  {"success": True, "SESSION_ID": "session-abc"})
    assert broker.get(ident)["result"]["SESSION_ID"] == "session-abc"
    assert broker.get(ident)["state"] == "done"
    broker.finish("windows-main", ident, work["claim"],
                  {"success": True, "SESSION_ID": "session-abc"})
    with pytest.raises(ValueError, match="completed result mismatch"):
        broker.finish("windows-main", ident, work["claim"], {"success": False})
    with pytest.raises(ValueError, match="another device"):
        broker.submit("netcup-main", "start",
                      {"PROMPT": "bad", "SESSION_ID": "session-abc"})
    assert broker.submit("windows-main", "start", {"PROMPT": "ok",
                                                  "SESSION_ID": "session-abc"})


def test_permissions_and_replay(broker: Broker) -> None:
    with pytest.raises(ValueError, match="not permitted"):
        broker.submit("netcup-main", "cancel", {"job_id": "job_x"})
    with pytest.raises(ValueError, match="unregistered"):
        broker.submit("unknown-host", "start", {})
    with pytest.raises(ValueError, match="session"):
        broker.submit("netcup-main", "start", {"SESSION_ID": "../../etc"})
    with pytest.raises(ValueError, match="request ID"):
        broker.get("../../password")
    ident = broker.submit("netcup-main", "start", {})
    work = broker.poll("netcup-main")
    assert work is not None
    assert work["id"] == ident
    assert broker.poll("netcup-main") is None  # no double dispatch on lost connection


def test_persistence(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    devices = {"windows-main": {"os": "Windows", "token_sha256": token_hash("w" * 40),
                                "allowed_actions": ["start"]}}
    first = Broker(db, devices)
    ident = first.submit("windows-main", "start", {"PROMPT": "hello"})
    work = first.poll("windows-main")
    assert work is not None
    first.finish("windows-main", ident, work["claim"], {"success": True})
    first.conn.close()
    second = Broker(db, devices)
    assert second.get(ident)["state"] == "done"
    assert second.poll("windows-main") is None
    second.conn.close()


def test_agent_completion_cache(tmp_path: Path) -> None:
    cache = AgentCache(tmp_path / "agent.sqlite")
    cache.save("call_" + "a" * 32, {"success": True, "job_id": "job_1"})
    cache.conn.close()
    recovered = AgentCache(tmp_path / "agent.sqlite")
    assert recovered.get("call_" + "a" * 32)["job_id"] == "job_1"
    recovered.queue_receipt("call_" + "a" * 32, "test-proof", {"success": True})
    recovered.conn.close()
    resumed = AgentCache(tmp_path / "agent.sqlite")
    assert resumed.pending_receipts()[0][1] == "test-proof"
    resumed.mark_sent("call_" + "a" * 32)
    assert resumed.pending_receipts() == []
    resumed.conn.close()
    with pytest.raises(ValueError, match="unsupported action"):
        dispatch("shell", {"command": "rm -rf /"}, "call_" + "a" * 32)


def test_http_auth_and_device_isolation(broker: Broker) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(broker, "secret" * 8))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def request(path: str, token: str, value: dict) -> dict:
        req = Request(base + path, json.dumps(value).encode(),
                      headers={"Authorization": "Bearer " + token})
        with urlopen(req, timeout=2) as res:
            return json.loads(res.read())

    try:
        with pytest.raises(HTTPError) as err:
            request("/v1/admin/submit", "w" * 40, {
                "device_id": "windows-main", "action": "start", "payload": {}})
        assert err.value.code == 401
        err.value.close()
        answer = request("/v1/admin/submit", "secret" * 8, {
            "device_id": "windows-main", "action": "start", "payload": {}})
        ident = answer["request_id"]
        with pytest.raises(HTTPError) as err:
            request("/v1/agent/poll", "l" * 40, {"device_id": "windows-main"})
        assert err.value.code == 401
        err.value.close()
        poll = request("/v1/agent/poll", "w" * 40, {"device_id": "windows-main"})
        assert poll["work"]["id"] == ident
        complete = request("/v1/agent/finish", "w" * 40, {
            "device_id": "windows-main", "id": ident,
            "claim": poll["work"]["claim"], "result": {"success": True}})
        assert complete["success"]
        assert request("/v1/admin/get", "secret" * 8,
                       {"request_id": ident})["state"] == "done"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
