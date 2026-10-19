"""Safely drain stale MCP stdio children after a source-code upgrade.

The SSH tunnel starts a persistent stdio_reconnect parent and FastMCP child.
A source checkout update does not update already-imported Python modules.
This monitor closes *only* an idle, old-version MCP child so the existing
tunnel client may reconnect with the new source. It never cancels worker jobs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


class RequestTracker:
    """Track outstanding JSON-RPC requests; no automatic request replay."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: set[str] = set()
        self.last_activity = time.monotonic()

    def request(self, raw: bytes) -> None:
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(item, dict) and "method" in item and "id" in item:
            key = json.dumps(item["id"], sort_keys=True)
            with self.lock:
                self.pending.add(key)
                self.last_activity = time.monotonic()

    def response(self, raw: bytes) -> None:
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(item, dict) and "id" in item and (
            "result" in item or "error" in item
        ):
            key = json.dumps(item["id"], sort_keys=True)
            with self.lock:
                self.pending.discard(key)
                self.last_activity = time.monotonic()


def source_revision() -> str | None:
    """Track Git commit, or an explicit local-only test revision marker."""
    override = os.environ.get("AGY_MCP_RELOAD_REVISION_FILE")
    if override:
        try:
            value = Path(override).read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None
    checkout = Path(__file__).resolve().parents[2]
    if not (checkout / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"],
            check=False, capture_output=True, text=True,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _child_has_descendants(pid: int) -> bool:
    """Fail closed on platforms where we cannot inspect process descendants."""
    children = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return bool(children.read_text(encoding="ascii").strip())
    except OSError:
        return True


def _child_has_running_jobs(pid: int) -> bool:
    """Only jobs owned by this exact FastMCP process can block its recycle.

    If state cannot be safely checked, keep the old MCP instance alive.
    """
    try:
        from agy_mcp.config import get_config

        root = Path(get_config().session_store_root())
        if not root.is_dir():
            return True
        for meta in root.glob("job_*/meta.json"):
            try:
                data: Any = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return True
            if not isinstance(data, dict) or data.get("status") != "running":
                continue
            owner = data.get("extra", {}).get("supervisor", {})
            if not isinstance(owner, dict):
                continue
            if owner.get("pid") == pid:
                return True
        return False
    except Exception:
        return True


def can_recycle(child: subprocess.Popen[bytes], tracker: RequestTracker) -> bool:
    """Call under tracker.lock to avoid racing a new incoming tool request."""
    if child.poll() is not None:
        return False
    if tracker.pending or time.monotonic() - tracker.last_activity < 5.0:
        return False
    if sys.platform != "linux":
        return False
    return not _child_has_descendants(child.pid) and not _child_has_running_jobs(child.pid)


def watch_revision(
    child: subprocess.Popen[bytes],
    tracker: RequestTracker,
    stop: threading.Event,
) -> None:
    """When upgraded and idle, terminate only this child, never sshd/tunnel.

    Its stdio_reconnect parent exits normally when the child closes stdout.
    The SSH tunnel then reconnects on its next request. No inflight request
    is killed, no job replayed, and no Telegram/gateway process is affected.
    """
    if os.environ.get("AGY_MCP_AUTO_REFRESH", "1") == "0":
        return
    initial = source_revision()
    if not initial:
        return
    try:
        interval = max(0.2, min(float(
            os.environ.get("AGY_MCP_RELOAD_CHECK_SECONDS", "15")
        ), 300.0))
    except ValueError:
        interval = 15.0
    while not stop.wait(interval):
        fresh = source_revision()
        if not fresh or fresh == initial:
            continue
        with tracker.lock:
            if not can_recycle(child, tracker):
                continue
            try:
                child.terminate()
                print(
                    "gumacu MCP code revision changed; draining idle stdio child "
                    "(no in-flight requests or running jobs)",
                    file=sys.stderr, flush=True,
                )
            except OSError:
                pass
        return
