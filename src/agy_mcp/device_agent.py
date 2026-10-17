"""Native Windows/Linux Antigravity agent. Connects OUTWARD through an SSH tunnel.

The worker executes only an enumerated collection of existing agy-mcp tools.
The existing remote_policy governs filesystem roots, writes, modes and timeouts.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agy_mcp.device_gateway import CALL_ID, DEVICE_ID, MAX_BODY

ACTIONS = frozenset({"start", "status", "read", "result", "cancel", "sessions", "doctor"})


def post(url: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        raise ValueError("agent requires localhost SSH-forwarded endpoint")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_BODY:
        raise ValueError("payload too large")
    request = Request(
        url.rstrip("/") + path, data=data,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        result = json.loads(response.read(MAX_BODY + 1))
        if not isinstance(result, dict) or not result.get("success"):
            raise ValueError("broker returned unsuccessful response")
        return result


def dispatch(action: str, payload: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Never accept a shell command or arbitrary Python function name."""
    if action not in ACTIONS or not CALL_ID.fullmatch(call_id):
        raise ValueError("unsupported action or command identity")
    if not isinstance(payload, dict):
        raise ValueError("invalid payload")
    from agy_mcp import server

    if action == "start":
        allowed = {"PROMPT", "SESSION_ID", "cwd", "mode", "allow_write",
                   "timeout", "sandbox", "worktree"}
        if not set(payload) <= allowed or not isinstance(payload.get("PROMPT"), str):
            raise ValueError("invalid start parameters")
        args = {key: val for key, val in payload.items() if key in allowed}
        args["job_id"] = "job_remote_" + call_id[5:]
        return server.agy_start_tool(**args).model_dump(mode="json")
    if action in {"status", "read", "result", "cancel"}:
        if action == "status":
            fn = server.agy_status_tool
            allowed = {"job_id"}
        elif action == "read":
            fn = server.agy_read_tool
            allowed = {"job_id", "since", "limit"}
        elif action == "result":
            fn = server.agy_result_tool
            allowed = {"job_id", "since", "include_events"}
        else:
            fn = server.agy_cancel_tool
            allowed = {"job_id"}
        if not set(payload) <= allowed or not isinstance(payload.get("job_id"), str):
            raise ValueError("invalid job lookup parameters")
        return fn(**payload).model_dump(mode="json")
    if payload:
        raise ValueError("unexpected arguments")
    if action == "sessions":
        return server.agy_sessions_tool(limit=50).model_dump(mode="json")
    import asyncio

    return asyncio.run(server.agy_doctor_tool()).model_dump(mode="json")


class AgentCache:
    """Save command completions before acknowledging to survive network loss."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("CREATE TABLE IF NOT EXISTS completed (id TEXT PRIMARY KEY, result TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY,"
                          " claim TEXT NOT NULL, result TEXT NOT NULL)")
        self.conn.commit()

    def get(self, ident: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT result FROM completed WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, ident: str, result: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO completed(id,result) VALUES(?,?)",
                              (ident, json.dumps(result, ensure_ascii=False)))

    def queue_receipt(self, ident: str, claim: str, result: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO receipts(id,claim,result) VALUES(?,?,?)",
                              (ident, claim, json.dumps(result, ensure_ascii=False)))

    def pending_receipts(self) -> list[tuple[str, str, dict[str, Any]]]:
        rows = self.conn.execute("SELECT id,claim,result FROM receipts ORDER BY id").fetchall()
        return [(rid, claim, json.loads(value)) for rid, claim, value in rows]

    def mark_sent(self, ident: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM receipts WHERE id=?", (ident,))


def run(device: str, token: str, gateway: str, cache_path: Path) -> None:
    if not DEVICE_ID.fullmatch(device) or len(token) < 32:
        raise ValueError("invalid device identity or token")
    if "GUMACU_ALLOWED_WORKSPACES" not in os.environ:
        raise ValueError("set GUMACU_ALLOWED_WORKSPACES to approved local directories")
    # Fail-closed: the default cannot inherit a broad 'local' or 'operator' profile.
    profile = os.getenv("AGY_NODE_PROFILE", "controlled")
    if profile not in {"controlled", "controlled-write"}:
        raise ValueError("AGY_NODE_PROFILE must be controlled or controlled-write")
    os.environ["GUMACU_REMOTE_PROFILE"] = profile
    os.environ.pop("GUMACU_OPERATOR_TOKEN", None)

    cache = AgentCache(cache_path)
    delay = 1.0
    while True:
        try:
            post(gateway, token, "/v1/agent/heartbeat",
                 {"device_id": device, "capabilities": {"platform": platform.system(),
                  "agent": "agy-mcp", "profile": profile}})
            # Retry cached completions after loss of SSH/network. The broker's
            # idempotent finish endpoint allows acknowledgment retransmission.
            for ident, proof, saved in cache.pending_receipts():
                post(gateway, token, "/v1/agent/finish",
                     {"device_id": device, "id": ident, "claim": proof, "result": saved})
                cache.mark_sent(ident)
            answer = post(gateway, token, "/v1/agent/poll", {"device_id": device})
            work = answer.get("work")
            if work:
                result = cache.get(work["id"])
                if result is None:
                    try:
                        result = dispatch(work["action"], work["payload"], work["id"])
                    except Exception as exc:
                        # Never include exception content that could contain credentials.
                        result = {"success": False, "error": type(exc).__name__}
                    cache.save(work["id"], result)
                cache.queue_receipt(work["id"], work["claim"], result)
                post(gateway, token, "/v1/agent/finish",
                     {"device_id": device, "id": work["id"],
                      "claim": work["claim"], "result": result})
                cache.mark_sent(work["id"])
            delay = 1.0
            time.sleep(1 if work else 2)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
            time.sleep(delay)
            delay = min(delay * 2, 30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gumacu Windows/Linux outbound agy worker")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--gateway", default="http://127.0.0.1:8766")
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("AGY_DEVICE_TOKEN", "")
    run(args.device_id, token, args.gateway, args.state)


if __name__ == "__main__":
    main()
