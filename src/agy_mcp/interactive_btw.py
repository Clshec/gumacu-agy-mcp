"""Opt-in native Antigravity TUI bridge for /btw side questions (Linux only).

Headless agy --print jobs are deliberately unsupported. The only process
receiving a side question is the SAME persistent interactive TUI created by
interactive_open. No second model turn, clone, or session resume is started.
Terminal content is a *screen snapshot*, not an authenticated answer payload.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows agent imports this module but cannot run POSIX TUI.
    fcntl = None  # type: ignore[assignment]
import json
import os
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agy_mcp.remote_policy import (
    get_approved_workspaces,
    is_operator_authorized,
    remote_profile,
    validate_workspace,
)
from agy_mcp.safety import SafetyPolicy

_JOB_ID = re.compile(r"^job_tui_[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAX_QUESTION = 2048
_MAX_PROMPT = 10000


def _enabled() -> bool:
    return os.environ.get("GUMACU_ENABLE_INTERACTIVE_BTW") == "1" and os.name == "posix"


def _state_dir() -> Path:
    configured = os.environ.get("GUMACU_BTW_STATE_DIR", "")
    path = Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "gumacu-agy-mcp" / "interactive"
    )
    if path.is_symlink():
        raise ValueError("session state directory must not be a symbolic link")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path.resolve()


def _validate_text(value: str, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a nonempty string of at most {limit} characters")
    if _CONTROL.search(value) or "\r" in value or "\n" in value:
        raise ValueError(f"{name} must be a single line without control characters")
    return value


def _tmux(*args: str) -> str:
    binary = shutil.which("tmux")
    if not binary:
        raise RuntimeError("tmux executable not installed; interactive /btw unavailable")
    # Only these short tmux CONTROL commands have a network/process safety
    # deadline. The TUI task itself is not subject to any deadline.
    result = subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=8, check=False,
    )
    if result.returncode:
        raise RuntimeError("tmux session unavailable or command failed")
    return result.stdout


def _session_name(job_id: str) -> str:
    return "gmbtw_" + job_id.removeprefix("job_tui_")


def _load(job_id: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("invalid TUI job identifier")
    path = _state_dir() / (job_id + ".json")
    if not path.is_file() or path.is_symlink():
        raise ValueError("unknown interactive TUI job")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("job_id") != job_id or obj.get("tmux") != _session_name(job_id):
        raise ValueError("invalid interactive TUI state")
    return obj


def _save(obj: dict[str, Any]) -> None:
    path = _state_dir() / (obj["job_id"] + ".json")
    temp = _state_dir() / (obj["job_id"] + "." + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(obj, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def _locked(job_id: str) -> Iterator[dict[str, Any]]:
    _load(job_id)  # Validate before deriving any filesystem path.
    lockpath = _state_dir() / (job_id + ".lock")
    descriptor = os.open(lockpath, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        obj = _load(job_id)
        yield obj


def _alive(obj: dict[str, Any]) -> bool:
    try:
        _tmux("has-session", "-t", obj["tmux"])
        return True
    except RuntimeError:
        return False


def _screen(obj: dict[str, Any]) -> str:
    # capture-pane returns terminal text only; it is not the native side
    # response API. Never treat its content as a verified model answer.
    value = _tmux("capture-pane", "-p", "-J", "-S", "-200", "-t", obj["tmux"])
    return SafetyPolicy().redact(value[-14000:])


def _reply(error: Exception) -> dict[str, Any]:
    return {"success": False, "error": type(error).__name__ + ": " + str(error)}


def interactive_open(cwd: str = ".", operator_token: str | None = None) -> dict[str, Any]:
    """Launch a NEW durable native TUI; existing --print jobs remain untouched."""
    if not _enabled():
        return {"success": False, "error": "interactive_btw_disabled_or_unsupported_platform"}
    try:
        profile = remote_profile()
        if profile == "read-only":
            return {"success": False, "error": "read_only_profile_cannot_launch_tui"}
        if not is_operator_authorized(profile, {"operator_token": operator_token}):
            return {"success": False, "error": "operator_authorization_required"}
        if profile == "local":
            root = Path(cwd).expanduser().resolve(strict=True)
        else:
            root = validate_workspace(cwd, get_approved_workspaces())
        if not root.is_dir():
            raise ValueError("cwd is not an existing directory")
        binary = os.environ.get("AGY_BIN") or shutil.which("agy")
        if not binary or not Path(binary).is_absolute() or not Path(binary).is_file():
            raise RuntimeError("trusted absolute agy binary unavailable")
        job_id = "job_tui_" + secrets.token_hex(16)
        name = _session_name(job_id)
        # tmux runs the CLI itself in an attached PTY. There is no --print,
        # --print-timeout, wrapper timer, shell command, or secret in argv.
        _tmux("new-session", "-d", "-s", name, "-c", str(root), binary)
        obj = {"job_id": job_id, "tmux": name, "cwd": str(root),
               "initial_sent": False, "btw_pending": False}
        _save(obj)
        return {"success": True, "job_id": job_id, "status": "tui_started",
                "native_btw_supported": True,
                "note": "Inspect interactive_snapshot for readiness, then interactive_send the primary task. No headless jobs are affected."}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _reply(exc)


def interactive_send(job_id: str, prompt: str) -> dict[str, Any]:
    """Send ONE primary task to a TUI after checking its prompt is ready."""
    if not _enabled():
        return {"success": False, "error": "interactive_btw_disabled_or_unsupported_platform"}
    try:
        text = _validate_text(prompt, _MAX_PROMPT, "prompt")
        with _locked(job_id) as obj:
            if not _alive(obj):
                return {"success": False, "error": "tui_not_running"}
            if obj["initial_sent"]:
                return {"success": False, "error": "primary_task_already_sent"}
            _tmux("send-keys", "-t", obj["tmux"], "-l", "--", text)
            _tmux("send-keys", "-t", obj["tmux"], "Enter")
            obj["initial_sent"] = True
            _save(obj)
        return {"success": True, "job_id": job_id, "status": "primary_prompt_sent",
                "note": "Persistent TUI continues independently; no task deadline is set."}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _reply(exc)


def interactive_btw(job_id: str, question: str) -> dict[str, Any]:
    """Send genuine /btw to the existing native interactive TUI.

    This does not work for existing job_... --print jobs, which are rejected.
    """
    if not _enabled():
        return {"success": False, "error": "interactive_btw_disabled_or_unsupported_platform"}
    try:
        text = _validate_text(question, _MAX_QUESTION, "question")
        with _locked(job_id) as obj:
            if not obj["initial_sent"]:
                return {"success": False, "error": "no_primary_conversation"}
            if obj["btw_pending"]:
                return {"success": False, "error": "btw_already_in_flight"}
            if not _alive(obj):
                return {"success": False, "error": "tui_not_running"}
            _tmux("send-keys", "-t", obj["tmux"], "-l", "--", "/btw " + text)
            _tmux("send-keys", "-t", obj["tmux"], "Enter")
            obj["btw_pending"] = True
            _save(obj)
        return {"success": True, "job_id": job_id, "status": "native_btw_submitted",
                "answer_verified": False,
                "note": "Read interactive_snapshot for the live card; no separate CLI job was created and main conversation was not resumed."}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _reply(exc)


def interactive_snapshot(job_id: str, clear_btw_pending: bool = False) -> dict[str, Any]:
    """Return a screen snapshot, never mislabel it as a verified /btw answer."""
    if not _enabled():
        return {"success": False, "error": "interactive_btw_disabled_or_unsupported_platform"}
    try:
        with _locked(job_id) as obj:
            if not _alive(obj):
                return {"success": False, "error": "tui_not_running"}
            screen = _screen(obj)
            if clear_btw_pending:
                # Explicit caller acknowledgement; do not infer completion
                # from TUI text because it has no structured result protocol.
                obj["btw_pending"] = False
                _save(obj)
            return {"success": True, "job_id": job_id, "status": "tui_live",
                    "screen_snapshot": screen, "answer_verified": False,
                    "btw_pending": obj["btw_pending"],
                    "note": "Terminal screenshot text is not a structured native answer. One side query at a time."}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return _reply(exc)
