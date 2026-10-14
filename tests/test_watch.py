"""Tests for agy-watch CLI and event streaming."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from agy_mcp.models import CanonicalEvent, JobRecord
from agy_mcp.session_store import SessionStore
from agy_mcp.watch import Colors, find_latest_job, format_event, main, watch_job


def test_format_event():
    c = Colors(enabled=False)

    ev_assistant = CanonicalEvent(
        ts="2026-10-14T08:00:00Z",
        type="assistant",
        text="Checking server load average...",
    )
    formatted = format_event(ev_assistant, c)
    assert "[08:00:00]" in formatted
    assert "Checking server load average..." in formatted

    ev_tool = CanonicalEvent(
        ts="2026-10-14T08:00:01Z",
        type="tool_use",
        subtype="run_command",
        metadata={"cmd": "uptime"},
    )
    formatted_tool = format_event(ev_tool, c)
    assert "Tool call: run_command" in formatted_tool

    ev_result = CanonicalEvent(
        ts="2026-10-14T08:00:05Z",
        type="result",
        subtype="completed",
    )
    assert "Completed turn" in format_event(ev_result, c)


def test_find_latest_job(tmp_path: Path):
    store = SessionStore(tmp_path)
    assert find_latest_job(store) is None

    rec1 = store.create_job(
        cwd="/tmp",
        request={"prompt": "Task 1", "mode": "ask"},
        backend="agy",
    )
    latest = find_latest_job(store)
    assert latest is not None
    assert latest.job_id == rec1.job_id


def test_watch_job_completed(tmp_path: Path):
    store = SessionStore(tmp_path)
    rec = store.create_job(
        cwd="/tmp",
        request={"prompt": "Analyze CPU load", "mode": "ask"},
        backend="agy",
    )

    # Append an event
    store.append_event(
        rec.job_id,
        CanonicalEvent(ts="2026-10-14T08:00:00Z", type="assistant", text="Starting CPU check"),
    )

    # Finalize job
    store.finalize_job(rec.job_id, status="completed", exit_code=0)

    # Watch job
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        code = watch_job(store, rec.job_id, poll_interval=0.01)

    assert code == 0
    output = buf.getvalue()
    assert "Gumacu Antigravity Job Watcher" in output
    assert "Starting CPU check" in output
    assert "JOB FINISHED: COMPLETED" in output
    assert "NEXT STEP FOR CHATGPT" in output
    assert "agy_result_page" in output


def test_watch_cli_args(tmp_path: Path):
    store = SessionStore(tmp_path)
    rec = store.create_job(
        cwd="/tmp",
        request={"prompt": "Test CLI", "mode": "ask"},
        backend="agy",
    )
    store.finalize_job(rec.job_id, status="completed")

    buf = io.StringIO()
    with patch("sys.stdout", buf):
        code = main([
            "--latest",
            "--session-root",
            str(tmp_path),
            "--no-color",
            "--poll-interval",
            "0.01",
            "--timeout",
            "5",
        ])

    assert code == 0
    assert "JOB FINISHED: COMPLETED" in buf.getvalue()
