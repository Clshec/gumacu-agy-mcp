"""Private multi-device request broker; no public listener or shell execution.

Launch behind SSH port forwarding. The broker stores request/response envelopes in
SQLite; every machine runs its own Antigravity CLI via device_agent.py.
"""
from __future__ import annotations

import argparse
import datetime
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

from agy_mcp.models import JobRecord
from agy_mcp.notifications import (
    NotificationDispatcher,
    NotificationOutbox,
    TERMINAL_STATUSES,
)
from agy_mcp.safety import SafetyPolicy

DEVICE_ID = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
CALL_ID = re.compile(r"^call_[0-9a-f]{32}$")
ACTIONS = frozenset({"start", "status", "read", "result", "cancel", "sessions", "doctor"})
MAX_BODY = 1024 * 1024


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_task_label(prompt: str) -> str:
    """Extract a sanitized, meaningful label from prompt text."""
    if not prompt:
        return "Remote Antigravity Task"
    first = next((" ".join(line.split()) for line in prompt.splitlines() if line.strip()), "")
    return first[:120] if first else "Remote Antigravity Task"


def remote_job_buttons(job_id: str, is_terminal: bool = False) -> list[list[dict[str, str]]]:
    ref = job_id[-12:]
    row1 = [
        {"text": "🔄 تحديث الحالة", "callback_data": f"status:{ref}"},
        {"text": "💬 آخر رد", "callback_data": f"last:{ref}"},
    ]
    if is_terminal:
        return [row1, [{"text": "➡️ متابعة هذه الجلسة", "callback_data": f"use:{ref}"}]]
    return [row1]


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

    def __init__(
        self,
        db_path: Path,
        devices: dict[str, dict[str, Any]],
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
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
            self.conn.execute("""CREATE TABLE IF NOT EXISTS running_jobs (
                job_id TEXT PRIMARY KEY, device TEXT NOT NULL, request_id TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS remote_lifecycle (
                job_id TEXT PRIMARY KEY, device TEXT NOT NULL, request_id TEXT NOT NULL,
                session_id TEXT, task_label TEXT, antigravity_title TEXT,
                title_source TEXT, prompt TEXT, status TEXT NOT NULL,
                started_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
                notified_started INTEGER DEFAULT 0, notified_terminal INTEGER DEFAULT 0,
                last_progress_at REAL DEFAULT 0, last_progress_fingerprint TEXT,
                summary TEXT, result TEXT)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                task_label TEXT, antigravity_title TEXT, title_source TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)""")

        if dispatcher is not None:
            self.dispatcher = dispatcher
        else:
            outbox_path = Path(os.environ.get(
                "AGY_MCP_NOTIFICATIONS_PATH",
                "/var/lib/gumacu-agy-mcp/sessions/notifications.json",
            ))
            if not outbox_path.parent.exists():
                outbox_path = db_path.parent / "notifications.json"
            try:
                outbox = NotificationOutbox(outbox_path)
                self.dispatcher = NotificationDispatcher(outbox)
            except Exception:
                self.dispatcher = None

        self._stop_watcher = False
        self._watcher_thread: threading.Thread | None = None

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
            if action == "start":
                job_id = "job_r_" + ident[5:]
                prompt = str(payload.get("PROMPT") or payload.get("prompt") or "")
                task_label = extract_task_label(prompt)
                antigravity_title = None
                title_source = "prompt_label"
                if session:
                    meta = self.conn.execute(
                        "SELECT task_label, antigravity_title, title_source FROM session_metadata WHERE session_id=?",
                        (session,),
                    ).fetchone()
                    if meta is not None:
                        if meta["antigravity_title"]:
                            antigravity_title = meta["antigravity_title"]
                            title_source = meta["title_source"] or "antigravity"
                        if not prompt and meta["task_label"]:
                            task_label = meta["task_label"]
                self.conn.execute(
                    "INSERT OR REPLACE INTO remote_lifecycle ("
                    "job_id, device, request_id, session_id, task_label, antigravity_title, "
                    "title_source, prompt, status, started_at, updated_at, notified_started, notified_terminal"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
                    (job_id, device, ident, session, task_label, antigravity_title, title_source, prompt, "queued", now, now),
                )
                if session:
                    self.conn.execute(
                        "INSERT INTO session_metadata(session_id, device, task_label, antigravity_title, title_source, created, updated)"
                        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                        "updated=excluded.updated, "
                        "task_label=COALESCE(excluded.task_label, session_metadata.task_label), "
                        "antigravity_title=COALESCE(excluded.antigravity_title, session_metadata.antigravity_title), "
                        "title_source=COALESCE(excluded.title_source, session_metadata.title_source)",
                        (session, device, task_label, antigravity_title, title_source, now, now),
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
        should_notify_started = False
        should_notify_progress = False
        progress_summary = None
        action = ""
        job_id = None
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
            action = str(row["action"])

            rec = result.get("record") if isinstance(result.get("record"), dict) else None
            if result.get("job_id"):
                job_id = str(result["job_id"])
            elif rec and rec.get("job_id"):
                job_id = str(rec["job_id"])
            elif action == "start":
                job_id = "job_r_" + ident[5:]
            elif isinstance(row.get("payload"), str):
                try:
                    p = json.loads(row["payload"])
                    if p.get("job_id"):
                        job_id = str(p["job_id"])
                except Exception:
                    pass

            antigravity_title = None
            title_source = None
            if rec and rec.get("antigravity_title"):
                antigravity_title = str(rec["antigravity_title"])
                title_source = str(rec.get("title_source") or "antigravity")
            elif result.get("antigravity_title"):
                antigravity_title = str(result["antigravity_title"])
                title_source = str(result.get("title_source") or "antigravity")

            if job_id:
                lc = self.conn.execute("SELECT * FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
                now = time.time()
                if lc is not None:
                    new_session = session or lc["session_id"]
                    new_title = antigravity_title or lc["antigravity_title"]
                    new_source = title_source if antigravity_title else lc["title_source"]
                    self.conn.execute(
                        "UPDATE remote_lifecycle SET session_id=?, antigravity_title=?, title_source=?, updated_at=? WHERE job_id=?",
                        (new_session, new_title, new_source, now, job_id),
                    )
                    if new_session and (antigravity_title or not lc["session_id"]):
                        self.conn.execute(
                            "INSERT INTO session_metadata(session_id, device, task_label, antigravity_title, title_source, created, updated)"
                            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                            "updated=excluded.updated, "
                            "antigravity_title=COALESCE(excluded.antigravity_title, session_metadata.antigravity_title), "
                            "title_source=COALESCE(excluded.title_source, session_metadata.title_source)",
                            (new_session, device, lc["task_label"], new_title, new_source, now, now),
                        )

            if action == "start":
                status = result.get("status")
                if status == "running" and job_id:
                    now = time.time()
                    self.conn.execute(
                        "INSERT OR REPLACE INTO running_jobs(job_id,device,request_id,created,updated)"
                        " VALUES(?,?,?,?,?)",
                        (job_id, device, ident, now, now),
                    )
                    self.conn.execute(
                        "UPDATE remote_lifecycle SET status='running', updated_at=? WHERE job_id=?",
                        (now, job_id),
                    )
                    lc = self.conn.execute("SELECT notified_started FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
                    if lc and lc["notified_started"] == 0:
                        self.conn.execute("UPDATE remote_lifecycle SET notified_started=1 WHERE job_id=?", (job_id,))
                        should_notify_started = True

            elif action in {"status", "result"} and job_id:
                status_val = (rec.get("status") if rec else None) or result.get("status")
                if status_val == "running":
                    prog_obj = result.get("progress") if isinstance(result.get("progress"), dict) else (rec.get("progress") if rec else None)
                    prog_summary = (prog_obj.get("label") if isinstance(prog_obj, dict) else None) or (prog_obj.get("summary") if isinstance(prog_obj, dict) else None) or "Working on the task"
                    fp = f"{status_val}:{prog_summary}"
                    now = time.time()
                    lc = self.conn.execute("SELECT last_progress_at, last_progress_fingerprint FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
                    if lc:
                        last_at = lc["last_progress_at"] or 0
                        last_fp = lc["last_progress_fingerprint"]
                        if (now - last_at >= 60.0) and fp != last_fp:
                            self.conn.execute(
                                "UPDATE remote_lifecycle SET last_progress_at=?, last_progress_fingerprint=? WHERE job_id=?",
                                (now, fp, job_id),
                            )
                            should_notify_progress = True
                            progress_summary = prog_summary

        if should_notify_started and job_id:
            self.notify_remote_lifecycle(device, job_id, "started", result, ident=ident)
        elif should_notify_progress and job_id:
            self.notify_remote_lifecycle(device, job_id, "progress", result, ident=ident, summary=progress_summary)

        self.notify_remote_terminal(device, ident, result, action=action)

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

    def notify_remote_terminal(
        self,
        device: str,
        ident: str,
        result: dict[str, Any],
        action: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.dispatcher or not isinstance(result, dict):
            return None
        record = result.get("record") if isinstance(result.get("record"), dict) else None
        status = None
        if record and record.get("status") in TERMINAL_STATUSES:
            status = str(record["status"])
        elif result.get("status") in TERMINAL_STATUSES:
            status = str(result["status"])
        elif action == "start" and result.get("success") is False:
            status = "failed"

        if not status:
            return None

        job_id = None
        if record and record.get("job_id"):
            job_id = str(record["job_id"])
        elif result.get("job_id"):
            job_id = str(result["job_id"])
        else:
            job_id = "job_r_" + ident[5:]

        with self.lock, self.conn:
            self.conn.execute("DELETE FROM running_jobs WHERE job_id=?", (job_id,))
            lc = self.conn.execute("SELECT notified_terminal FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
            if lc and lc["notified_terminal"] == 1:
                return None  # Exactly ONE terminal notification per job!
            now = time.time()
            if lc is not None:
                self.conn.execute(
                    "UPDATE remote_lifecycle SET notified_terminal=1, status=?, finished_at=?, updated_at=? WHERE job_id=?",
                    (status, now, now, job_id),
                )
            else:
                self.conn.execute(
                    "INSERT INTO remote_lifecycle (job_id, device, request_id, status, started_at, updated_at, finished_at, notified_started, notified_terminal) "
                    "VALUES (?,?,?,?,?,?,?,1,1)",
                    (job_id, device, ident, status, now, now, now),
                )

        return self.notify_remote_lifecycle(device, job_id, status, result, ident=ident)

    def notify_remote_lifecycle(
        self,
        device: str,
        job_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        ident: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.dispatcher:
            return None
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
            session_id = row["session_id"] if row else None
            task_label = row["task_label"] if row else None
            antigravity_title = row["antigravity_title"] if row else None
            title_source = row["title_source"] if row else None
            req_id = ident or (row["request_id"] if row else ident)

        is_terminal = status in TERMINAL_STATUSES
        if summary is None:
            if is_terminal:
                rec = result.get("record") if isinstance(result, dict) and isinstance(result.get("record"), dict) else None
                if result and result.get("result_text"):
                    summary = str(result["result_text"])
                elif rec and rec.get("error"):
                    summary = str(rec["error"])
                elif result and result.get("error"):
                    summary = str(result["error"])
                elif rec and isinstance(rec.get("progress"), dict) and rec["progress"].get("summary"):
                    summary = str(rec["progress"]["summary"])
                else:
                    summary = f"Remote task on {device} reached {status}."
            elif status == "started":
                summary = f"Antigravity task started on {device}."
            elif status == "progress":
                summary = f"Antigravity is working on {device}."
            else:
                summary = f"Remote task update: {status}."

        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE remote_lifecycle SET summary=?, result=?, updated_at=? WHERE job_id=?",
                (summary, json.dumps(result, ensure_ascii=False) if result else None, time.time(), job_id),
            )

        safety = SafetyPolicy()
        redacted_summary = safety.redact(summary)[:1000] if summary else ""

        buttons = remote_job_buttons(job_id, is_terminal=is_terminal)

        payload = {
            "job_id": job_id,
            "session_id": session_id,
            "antigravity_title": antigravity_title,
            "title_source": title_source,
            "task_label": task_label,
            "status": status,
        }
        try:
            return self.dispatcher.notify_job(
                payload,
                summary=redacted_summary,
                reason_code=status,
                device_id=device,
                request_id=req_id,
                buttons=buttons,
                title_source=title_source,
                task_label=task_label,
            )
        except Exception:
            return None

    def check_running_jobs(self) -> None:
        with self.lock:
            rows = self.conn.execute(
                "SELECT job_id, device, request_id FROM running_jobs"
            ).fetchall()
        now = time.time()
        for row in rows:
            job_id, device = row["job_id"], row["device"]
            if device not in self.devices:
                continue
            with self.lock:
                p = self.conn.execute("SELECT seen FROM presence WHERE device=?", (device,)).fetchone()
                if not p or (now - p["seen"]) > 35:
                    continue
                existing = self.conn.execute(
                    "SELECT id FROM calls WHERE device=? AND action='status' AND payload LIKE ? AND state IN ('queued', 'leased')",
                    (device, f'%"{job_id}"%'),
                ).fetchone()
            if existing is None:
                try:
                    self.submit(device, "status", {"job_id": job_id})
                except Exception:
                    pass

    def start_watcher(self, interval: float = 3.0) -> None:
        if self._watcher_thread is not None and self._watcher_thread.is_alive():
            return

        def loop():
            while not self._stop_watcher:
                try:
                    self.check_running_jobs()
                except Exception:
                    pass
                time.sleep(interval)

        self._watcher_thread = threading.Thread(
            target=loop, daemon=True, name="broker-running-jobs-watcher"
        )
        self._watcher_thread.start()

    def close(self) -> None:
        self._stop_watcher = True
        with self.lock:
            self.conn.close()


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
            elif self.command == "POST" and path == "/v1/agent/notify":
                broker.notify_remote_terminal(device, data.get("id") or "", data)
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


def load_remote_jobs_as_records(db_path: Path | str | None = None) -> list[JobRecord]:
    path = Path(db_path or os.environ.get("AGY_DEVICE_GATEWAY_DB", "/var/lib/gumacu-agy-devices/broker.sqlite"))
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='remote_lifecycle';").fetchone()
                if not table_check:
                    return []
                rows = conn.execute(
                    "SELECT job_id, device, request_id, session_id, task_label, antigravity_title, "
                    "title_source, prompt, status, started_at, updated_at, finished_at, summary "
                    "FROM remote_lifecycle ORDER BY updated_at DESC LIMIT 50"
                ).fetchall()
        finally:
            conn.close()
        records: list[JobRecord] = []
        for row in rows:
            started_iso = datetime.datetime.fromtimestamp(row["started_at"], datetime.timezone.utc).isoformat()
            updated_iso = datetime.datetime.fromtimestamp(row["updated_at"], datetime.timezone.utc).isoformat()
            finished_iso = (
                datetime.datetime.fromtimestamp(row["finished_at"], datetime.timezone.utc).isoformat()
                if row["finished_at"] else None
            )
            title = row["antigravity_title"] or row["task_label"] or "Remote Antigravity Task"
            raw_source = row["title_source"]
            if raw_source in {"antigravity", "antigravity_task", "prompt_label", "pending"}:
                title_source = raw_source
            else:
                title_source = "antigravity" if row["antigravity_title"] else "prompt_label"
            rec = JobRecord(
                job_id=row["job_id"],
                session_id=row["session_id"],
                antigravity_title=title,
                title_source=title_source,
                status=row["status"],
                backend="agy",
                cwd=f"<{row['device']}>",
                started_at=started_iso,
                updated_at=updated_iso,
                finished_at=finished_iso,
                request={"prompt": row["prompt"] or "", "SESSION_ID": row["session_id"]},
                extra={
                    "device_id": row["device"],
                    "request_id": row["request_id"],
                    "task_label": row["task_label"],
                    "summary": row["summary"],
                    "antigravity_title": row["antigravity_title"],
                    "title_source": row["title_source"],
                },
            )
            records.append(rec)
        return records
    except Exception as exc:
        logger.warning("Failed to load remote jobs as records: %s", exc)
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Private Gumacu device broker")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--devices", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--outbox", type=Path, default=None)
    args = parser.parse_args()
    if args.outbox:
        os.environ["AGY_MCP_NOTIFICATIONS_PATH"] = str(args.outbox)
    admin_token = os.environ.get("AGY_DEVICE_ADMIN_TOKEN", "")
    if len(admin_token) < 32:
        raise SystemExit("AGY_DEVICE_ADMIN_TOKEN must be a 32+ character private secret")
    devices = load_devices(args.devices)
    broker = Broker(args.db, devices)
    broker.start_watcher()
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(broker, admin_token))
    server.serve_forever()


if __name__ == "__main__":
    main()
