"""ChatGPT-led, one-step-at-a-time operations over the existing Supervisor.

Each decision is made by the calling ChatGPT conversation, never by a background
model loop. Execution jobs remain detached/monitorable and may outlive a client
connection. No prompt, credential, or full output is retained in this database.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

_ID = re.compile(r"^op_[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[a-zA-Z0-9_-]{8,100}$")
_MAX_RUNNING = 8
_TERMINAL = frozenset({
    "completed", "failed", "cancelled", "timeout", "upstream_error",
    "quota_exhausted", "no_final_response",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be 1..{limit} characters")
    if any(ord(c) == 0 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} contains control characters")
    if name in {"title", "label"} and any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a single line")
    return value.strip()


class InteractiveOperations:
    """SQLite-backed checkpoint gate with atomic admission across MCP processes."""

    def __init__(self, root: Path):
        directory = Path(root).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "interactive_operations.sqlite3"
        if self.path.is_symlink():
            raise ValueError("operation database cannot be a symlink")
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
                    updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps(
                    op_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    request_id TEXT NOT NULL, label TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL, job_id TEXT,
                    status TEXT NOT NULL, outcome TEXT, review TEXT,
                    started TEXT NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(op_id,seq), UNIQUE(op_id,request_id),
                    FOREIGN KEY(op_id) REFERENCES operations(id)
                );
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            try:
                yield db
                if db.in_transaction:
                    db.commit()
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise
        finally:
            db.close()

    @staticmethod
    def _op_id(operation_id: str) -> str:
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise ValueError("invalid operation_id")
        return operation_id

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create(self, title: str) -> dict[str, Any]:
        title = _text(title, "title", 160)
        operation_id = "op_" + secrets.token_hex(16)
        now = _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO operations(id,title,status,seq,created,updated) "
                "VALUES(?,?,'ready',0,?,?)",
                (operation_id, title, now, now),
            )
        return {
            "success": True, "operation_id": operation_id,
            "status": "ready", "next_action": "operation_step",
            "note": "ChatGPT chooses and reviews each step; no autonomous plan is running.",
        }

    def state(self, operation_id: str) -> dict[str, Any]:
        operation_id = self._op_id(operation_id)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                return {"success": False, "error": "operation_not_found"}
            recent = db.execute(
                "SELECT seq,request_id,label,job_id,status,outcome,review,started,updated "
                "FROM steps WHERE op_id=? ORDER BY seq DESC LIMIT 12",
                (operation_id,),
            ).fetchall()
        return {
            "success": True, "operation": self._row(row),
            "steps": [dict(x) for x in recent],
        }

    def list(self, limit: int = 20) -> dict[str, Any]:
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be 1..50")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM operations ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return {"success": True, "operations": [dict(x) for x in rows]}

    def begin(
        self, operation_id: str, request_id: str, label: str, prompt: str,
        launch: Callable[[], Any],
    ) -> dict[str, Any]:
        """Reserve a short task; launch once; require review before another step."""
        operation_id = self._op_id(operation_id)
        if not isinstance(request_id, str) or not _TOKEN.fullmatch(request_id):
            raise ValueError("request_id must be 8..100 safe characters")
        label = _text(label, "label", 120)
        prompt = _text(prompt, "prompt", 40000)
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if op is None:
                return {"success": False, "error": "operation_not_found"}
            previous = db.execute(
                "SELECT * FROM steps WHERE op_id=? AND request_id=?",
                (operation_id, request_id),
            ).fetchone()
            if previous is not None:
                if previous["prompt_hash"] != digest:
                    return {"success": False, "error": "idempotency_key_conflict"}
                admitted = previous["job_id"] is not None
                return {
                    "success": admitted, "reused": True,
                    "job_id": previous["job_id"],
                    "step_status": previous["status"], "operation_id": operation_id,
                    "next_action": (
                        "operation_collect" if admitted
                        else "retry_with_a_new_request_id"
                    ),
                }
            if op["status"] != "ready":
                return {
                    "success": False, "error": "review_or_previous_step_required",
                    "status": op["status"], "next_action": "operation_collect_or_review",
                }
            in_flight = db.execute(
                "SELECT COUNT(*) FROM steps WHERE status IN ('launching','running')"
            ).fetchone()[0]
            if in_flight >= _MAX_RUNNING:
                return {
                    "success": False, "error": "eight_step_slots_busy",
                    "retryable": True,
                }
            seq = op["seq"] + 1
            now = _now()
            db.execute(
                "INSERT INTO steps(op_id,seq,request_id,label,prompt_hash,job_id,"
                "status,outcome,review,started,updated) "
                "VALUES(?,?,?,?,?,NULL,'launching',NULL,NULL,?,?)",
                (operation_id, seq, request_id, label, digest, now, now),
            )
            db.execute(
                "UPDATE operations SET status='launching',seq=?,updated=? WHERE id=?",
                (seq, now, operation_id),
            )
        # Never keep a SQLite write lock while launching Antigravity.
        try:
            result = launch()
            success = bool(getattr(result, "success", False))
            job_id = getattr(result, "job_id", None) if success else None
            message = getattr(result, "error", None)
        except Exception as exc:
            success, job_id, message = False, None, type(exc).__name__
        state = "running" if success and job_id else "rejected"
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE steps SET job_id=?,status=?,outcome=?,updated=? "
                "WHERE op_id=? AND seq=? AND status='launching'",
                (job_id, state, None if state == "running" else
                 str(message or "launch_rejected")[:240], _now(), operation_id, seq),
            )
            db.execute(
                "UPDATE operations SET status=?,updated=? WHERE id=? AND status='launching'",
                (state if state == "running" else "ready", _now(), operation_id),
            )
        return {
            "success": state == "running", "operation_id": operation_id,
            "step_number": seq, "job_id": job_id, "step_status": state,
            "error": None if state == "running" else str(message or "launch_rejected")[:240],
            "next_action": "operation_collect" if state == "running" else "retry_or_revise",
        }

    def collect(
        self, operation_id: str,
        job_status: Callable[[str], Any],
        job_result: Callable[[str], Any],
    ) -> dict[str, Any]:
        operation_id = self._op_id(operation_id)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM steps WHERE op_id=? ORDER BY seq DESC LIMIT 1",
                (operation_id,),
            ).fetchone()
        if row is None:
            return {"success": False, "error": "no_step_submitted"}
        if row["status"] == "launching":
            return {"success": False, "error": "launch_in_progress_or_interrupted",
                    "requires_operator_reconciliation": True}
        if row["status"] in {"reviewed", "rejected"}:
            return {"success": True, "step_status": row["status"], "job_id": row["job_id"],
                    "next_action": "operation_step"}
        if not row["job_id"]:
            return {"success": False, "error": "step_has_no_job"}
        rec = job_status(row["job_id"])
        if rec is None:
            return {"success": False, "error": "supervisor_job_not_found",
                    "requires_operator_reconciliation": True}
        status = getattr(rec, "status", "unknown")
        if status == "running":
            return {"success": True, "step_status": "running", "job_id": row["job_id"],
                    "next_action": "operation_collect"}
        if status not in _TERMINAL:
            return {"success": False, "error": "unrecognized_job_state",
                    "job_status": status}
        result = job_result(row["job_id"])
        if not getattr(result, "success", False):
            return {"success": False, "error": "result_not_available",
                    "job_id": row["job_id"]}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE steps SET status='awaiting_review',outcome=?,updated=? "
                "WHERE op_id=? AND seq=? AND status='running'",
                (status, _now(), operation_id, row["seq"]),
            )
            db.execute(
                "UPDATE operations SET status='awaiting_review',updated=? "
                "WHERE id=? AND status='running'",
                (_now(), operation_id),
            )
        return {
            "success": True, "operation_id": operation_id,
            "job_id": row["job_id"], "job_status": status,
            "step_status": "awaiting_review",
            "result_text": getattr(result, "result_text", "")[:50000],
            "next_action": "operation_review",
            "note": "ChatGPT must check evidence and explicitly review before next step.",
        }

    def review(self, operation_id: str, accepted: bool, evidence: str) -> dict[str, Any]:
        operation_id = self._op_id(operation_id)
        if not isinstance(accepted, bool):
            raise ValueError("accepted must be boolean")
        evidence = _text(evidence, "evidence", 800)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if op is None:
                return {"success": False, "error": "operation_not_found"}
            if op["status"] != "awaiting_review":
                return {"success": False, "error": "no_result_awaiting_review"}
            latest = db.execute(
                "SELECT outcome FROM steps WHERE op_id=? AND seq=?",
                (operation_id, op["seq"]),
            ).fetchone()
            if accepted and (latest is None or latest["outcome"] != "completed"):
                return {"success": False, "error": "cannot_approve_failed_job"}
            # Never store raw evidence; the review receipt is non-sensitive.
            receipt = ("approved:" if accepted else "needs_correction:") + (
                hashlib.sha256(evidence.encode()).hexdigest()[:20]
            )
            now = _now()
            db.execute(
                "UPDATE steps SET status='reviewed',review=?,updated=? "
                "WHERE op_id=? AND seq=? AND status='awaiting_review'",
                (receipt, now, operation_id, op["seq"]),
            )
            db.execute(
                "UPDATE operations SET status='ready',updated=? WHERE id=?",
                (now, operation_id),
            )
        return {
            "success": True, "operation_id": operation_id,
            "accepted": accepted, "review_receipt": receipt,
            "next_action": "operation_step_or_finish" if accepted else "corrective_operation_step",
            "note": "Decision recorded by ChatGPT/operator, not independently certified.",
        }

    def finish(self, operation_id: str) -> dict[str, Any]:
        operation_id = self._op_id(operation_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            op = db.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if op is None:
                return {"success": False, "error": "operation_not_found"}
            if op["status"] != "ready" or op["seq"] < 1:
                return {"success": False, "error": "pending_step_or_review"}
            latest = db.execute(
                "SELECT review FROM steps WHERE op_id=? AND seq=?",
                (operation_id, op["seq"]),
            ).fetchone()
            if latest is None or not (latest["review"] or "").startswith("approved:"):
                return {"success": False, "error": "latest_step_not_approved"}
            db.execute(
                "UPDATE operations SET status='completed',updated=? WHERE id=?",
                (_now(), operation_id),
            )
        return {"success": True, "operation_id": operation_id, "status": "completed"}
