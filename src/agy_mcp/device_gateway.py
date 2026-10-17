"""Private multi-device request broker; no public listener or shell execution.

Launch behind SSH port forwarding. The broker stores request/response envelopes in
SQLite; every machine runs its own Antigravity CLI via device_agent.py.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEVICE_ID = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
CALL_ID = re.compile(r"^call_[0-9a-f]{32}$")
ACTIONS = frozenset({"start", "status", "read", "result", "cancel", "sessions", "doctor"})
MAX_BODY = 1024 * 1024


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_devices(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    for item in data["devices"]:
        name = item["id"]
        digest = item["token_sha256"]
        if not DEVICE_ID.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid device identity or token hash")
        if name in result:
            raise ValueError("duplicate device ID")
        actions = item.get("allowed_actions", sorted(ACTIONS))
        if not set(actions) <= ACTIONS:
            raise ValueError("unknown permitted action")
        result[name] = {"token_sha256": digest, "os": item.get("os", "unknown"),
                        "allowed_actions": actions}
    return result


class Broker:
    """SQLite-backed, process-local lock; leased work is never silently replayed."""

    def __init__(self, db_path: Path, devices: dict[str, dict[str, Any]]) -> None:
        self.devices = devices
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.conn:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, device TEXT NOT NULL, action TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, claim TEXT,
                result TEXT, created REAL NOT NULL, updated REAL NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS presence (
                device TEXT PRIMARY KEY, seen REAL NOT NULL, capabilities TEXT NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS affinity (
                session TEXT PRIMARY KEY, device TEXT NOT NULL)""")

    def submit(self, device: str, action: str, payload: dict[str, Any]) -> str:
        if device not in self.devices:
            raise ValueError("unregistered device")
        if action not in self.devices[device]["allowed_actions"]:
            raise ValueError("operation not permitted on this device")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        session = payload.get("SESSION_ID")
        if session is not None:
            if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", session):
                raise ValueError("invalid session identity")
        with self.lock, self.conn:
            if session:
                row = self.conn.execute("SELECT device FROM affinity WHERE session=?",
                                        (session,)).fetchone()
                if row is not None and row["device"] != device:
                    raise ValueError("session belongs to another device")
            ident = "call_" + secrets.token_hex(16)
            now = time.time()
            self.conn.execute(
                "INSERT INTO calls (id,device,action,payload,state,created,updated)"
                " VALUES (?,?,?,?,?,?,?)",
                (ident, device, action, json.dumps(payload), "queued", now, now),
            )
            return ident

    def poll(self, device: str) -> dict[str, Any] | None:
        if device not in self.devices:
            raise ValueError("unregistered device")
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM calls WHERE device=? AND state='queued'"
                " ORDER BY created,id LIMIT 1", (device,),
            ).fetchone()
            if row is None:
                return None
            claim = secrets.token_urlsafe(32)
            self.conn.execute("UPDATE calls SET state='leased',claim=?,updated=? WHERE id=?",
                              (claim, time.time(), row["id"]))
            return {"id": row["id"], "action": row["action"],
                    "payload": json.loads(row["payload"]), "claim": claim}

    def finish(self, device: str, ident: str, claim: str, result: dict[str, Any]) -> None:
        if not CALL_ID.fullmatch(ident) or not isinstance(result, dict):
            raise ValueError("invalid completion envelope")
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM calls WHERE id=?", (ident,)).fetchone()
            if row is None or row["device"] != device:
                raise ValueError("job not leased to this device")
            if not hmac.compare_digest(str(row["claim"]), claim):
                raise ValueError("invalid lease proof")
            value = json.dumps(result, ensure_ascii=False)
            if len(value.encode("utf-8")) > MAX_BODY:
                raise ValueError("result too large")
            if row["state"] == "done":
                if row["result"] == value:
                    return  # Network retry after a successful, durable acknowledgment.
                raise ValueError("completed result mismatch")
            if row["state"] != "leased":
                raise ValueError("job not leased to this device")
            session = result.get("SESSION_ID")
            if not session and isinstance(result.get("record"), dict):
                session = result["record"].get("session_id")
            if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,96}", session):
                original = self.conn.execute("SELECT device FROM affinity WHERE session=?",
                                             (session,)).fetchone()
                if original is not None and original["device"] != device:
                    raise ValueError("session binding conflict")
                self.conn.execute("INSERT OR IGNORE INTO affinity(session,device) VALUES(?,?)",
                                  (session, device))
            self.conn.execute("UPDATE calls SET state='done',result=?,updated=?"
                              " WHERE id=?", (value, time.time(), ident))

    def get(self, ident: str) -> dict[str, Any]:
        if not CALL_ID.fullmatch(ident):
            raise ValueError("invalid request ID")
        with self.lock:
            row = self.conn.execute("SELECT * FROM calls WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("request not found")
        return {"id": ident, "device_id": row["device"], "action": row["action"],
                "state": row["state"], "result": json.loads(row["result"])
                if row["result"] else None, "updated": row["updated"]}

    def heartbeat(self, device: str, capabilities: dict[str, Any]) -> None:
        if device not in self.devices:
            raise ValueError("unregistered device")
        if len(json.dumps(capabilities)) > 8192:
            raise ValueError("capabilities too long")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO presence(device,seen,capabilities) VALUES(?,?,?)"
                " ON CONFLICT(device) DO UPDATE SET seen=excluded.seen,"
                " capabilities=excluded.capabilities",
                (device, time.time(), json.dumps(capabilities)),
            )

    def list_devices(self) -> list[dict[str, Any]]:
        with self.lock:
            seen = {r["device"]: r for r in self.conn.execute("SELECT * FROM presence")}
        now = time.time()
        return [{"id": k, "os": v["os"], "online": k in seen and now - seen[k]["seen"] < 35,
                 "last_seen": seen[k]["seen"] if k in seen else None,
                 "capabilities": json.loads(seen[k]["capabilities"]) if k in seen else {}}
                for k, v in sorted(self.devices.items())]


def make_handler(broker: Broker, admin_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return  # do not log credentials, payloads, or session content

        def send(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_data(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY:
                raise ValueError("invalid body size")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("expected JSON object")
            return value

        def role(self, device: str | None = None) -> bool:
            raw = self.headers.get("Authorization", "")
            if not raw.startswith("Bearer "):
                return False
            candidate = raw[7:]
            if device is None:
                return hmac.compare_digest(candidate, admin_token)
            config = broker.devices.get(device)
            return bool(config and hmac.compare_digest(token_hash(candidate),
                                                        config["token_sha256"]))

        def handle_request(self) -> None:
            path = urlsplit(self.path).path
            is_admin = path.startswith("/v1/admin/")
            data = self.read_data() if self.command == "POST" else {}
            device = data.get("device_id") if not is_admin else None
            if not self.role(device):
                self.send(401, {"success": False, "error": "unauthorized"})
                return
            if self.command == "GET" and path == "/v1/admin/devices":
                self.send(200, {"success": True, "devices": broker.list_devices()})
            elif self.command == "POST" and path == "/v1/admin/submit":
                ident = broker.submit(data["device_id"], data["action"], data["payload"])
                self.send(200, {"success": True, "request_id": ident})
            elif self.command == "POST" and path == "/v1/admin/get":
                self.send(200, {"success": True, **broker.get(data["request_id"])})
            elif self.command == "POST" and path == "/v1/agent/heartbeat":
                broker.heartbeat(device, data.get("capabilities", {}))
                self.send(200, {"success": True})
            elif self.command == "POST" and path == "/v1/agent/poll":
                self.send(200, {"success": True, "work": broker.poll(device)})
            elif self.command == "POST" and path == "/v1/agent/finish":
                broker.finish(device, data["id"], data["claim"], data["result"])
                self.send(200, {"success": True})
            else:
                self.send(404, {"success": False, "error": "unknown route"})

        def do_GET(self) -> None:
            try:
                self.handle_request()
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.send(400, {"success": False, "error": "invalid request"})

        def do_POST(self) -> None:
            self.do_GET()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Gumacu device broker")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--devices", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    admin_token = os.environ.get("AGY_DEVICE_ADMIN_TOKEN", "")
    if len(admin_token) < 32:
        raise SystemExit("AGY_DEVICE_ADMIN_TOKEN must be a 32+ character private secret")
    devices = load_devices(args.devices)
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(Broker(args.db, devices), admin_token))
    server.serve_forever()


if __name__ == "__main__":
    main()
