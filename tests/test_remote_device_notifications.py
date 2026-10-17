"""Focused regression tests for remote Windows device Telegram notifications.

Verifies:
1. Terminal states (completed, failed, cancelled, timeout, upstream_error, quota_exhausted) enqueue durable notifications.
2. Notifications include device_id, request_id, and job_id.
3. Secrets are redacted by SafetyPolicy before enqueue and delivery.
4. Telegram API outage stores delivered=False; replay_pending_telegram() retries when online.
5. Duplicate delivery is completely idempotent.
6. Gateway running jobs watcher polls and completes running jobs.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agy_mcp.device_gateway import Broker, token_hash
from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox, TERMINAL_STATUSES


@pytest.fixture
def test_env(tmp_path: Path):
    db_path = tmp_path / "broker.db"
    outbox_path = tmp_path / "notifications.json"
    outbox = NotificationOutbox(outbox_path)
    dispatcher = NotificationDispatcher(
        outbox, token="test-bot-token-secret-12345", chat_id="999888", retry_delays=(0.0,)
    )
    devices = {
        "windows-main": {
            "os": "Windows",
            "token_sha256": token_hash("w" * 40),
            "allowed_actions": ["start", "status", "result", "cancel"],
        }
    }
    broker = Broker(db_path, devices, dispatcher=dispatcher)
    yield broker, outbox, dispatcher
    broker.close()


def test_remote_terminal_notification_enqueue(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env
    delivered_payloads: list[dict] = []

    def fake_post(url, *, json, timeout):
        delivered_payloads.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    # 1. Submit a start request
    ident = broker.submit("windows-main", "start", {"PROMPT": "echo hello"})
    work = broker.poll("windows-main")
    assert work is not None and work["id"] == ident

    # 2. Agent finishes with completed result
    result = {
        "success": True,
        "job_id": "job_r_" + ident[5:],
        "SESSION_ID": "session-123",
        "record": {
            "job_id": "job_r_" + ident[5:],
            "session_id": "session-123",
            "status": "completed",
            "antigravity_title": "Hello Task",
        },
        "result_text": "Task output successfully completed.",
    }
    broker.finish("windows-main", ident, work["claim"], result)

    # 3. Verify notification is recorded in outbox
    pending = outbox.list_pending()["notifications"]
    assert len(pending) == 1
    item = pending[0]
    assert item["job_id"] == "job_r_" + ident[5:]
    assert item["device_id"] == "windows-main"
    assert item["request_id"] == ident
    assert item["status"] == "completed"
    assert item["session_id"] == "session-123"

    # 4. Verify Telegram delivery content
    assert len(delivered_payloads) == 1
    text = delivered_payloads[0]["text"]
    assert "Device: <b>windows-main</b>" in text
    assert f"Request: <code>{ident}</code>" in text
    assert f"Job: <code>{item['job_id']}</code>" in text
    assert "Task output successfully completed." in text


def test_all_terminal_statuses_enqueued(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env

    def fake_post(url, *, json, timeout):
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    for status in ["completed", "failed", "cancelled", "timeout", "upstream_error", "quota_exhausted"]:
        ident = broker.submit("windows-main", "start", {"PROMPT": f"test {status}"})
        work = broker.poll("windows-main")
        result = {
            "success": status == "completed",
            "job_id": "job_r_" + ident[5:],
            "status": status,
            "error": f"Error for {status}" if status != "completed" else None,
            "result_text": f"Output for {status}" if status == "completed" else None,
        }
        broker.finish("windows-main", ident, work["claim"], result)

    notes = outbox.list_pending()["notifications"]
    statuses = [n["status"] for n in notes]
    for expected in ["completed", "failed", "cancelled", "timeout", "upstream_error", "quota_exhausted"]:
        assert expected in statuses


def test_remote_notification_duplicate_idempotency(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env
    delivery_count = 0

    def fake_post(url, *, json, timeout):
        nonlocal delivery_count
        delivery_count += 1
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    ident = broker.submit("windows-main", "result", {"job_id": "job_r_duplicate_test"})
    work = broker.poll("windows-main")

    result = {
        "success": True,
        "job_id": "job_r_duplicate_test",
        "record": {
            "job_id": "job_r_duplicate_test",
            "status": "completed",
        },
        "result_text": "Result text",
    }

    # First finish call
    broker.finish("windows-main", ident, work["claim"], result)
    # Repeated network retry
    broker.finish("windows-main", ident, work["claim"], result)
    # Repeated notify_remote_terminal call directly
    broker.notify_remote_terminal("windows-main", ident, result)

    # Exactly ONE notification in outbox and ONE delivery
    assert outbox.list_pending()["count"] == 1
    assert delivery_count == 1


def test_remote_notification_secret_redaction(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env
    delivered_payloads: list[dict] = []

    def fake_post(url, *, json, timeout):
        delivered_payloads.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    secret_key = "sk-proj-" + "x" * 48
    ident = broker.submit("windows-main", "result", {"job_id": "job_r_secret_redaction"})
    work = broker.poll("windows-main")

    result = {
        "success": True,
        "job_id": "job_r_secret_redaction",
        "record": {"job_id": "job_r_secret_redaction", "status": "completed"},
        "result_text": f"Finished operation with API_KEY={secret_key}",
    }
    broker.finish("windows-main", ident, work["claim"], result)

    item = outbox.list_pending()["notifications"][0]
    assert secret_key not in item["summary"]
    assert "***" in item["summary"]

    assert len(delivered_payloads) == 1
    assert secret_key not in delivered_payloads[0]["text"]
    assert "***" in delivered_payloads[0]["text"]


def test_remote_notification_offline_and_retry_outbox(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env
    should_fail = True
    delivered: list[dict] = []

    def flaky_post(url, *, json, timeout):
        if should_fail:
            raise httpx.ConnectError("Network unreachable", request=httpx.Request("POST", url))
        delivered.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", flaky_post)

    ident = broker.submit("windows-main", "start", {"PROMPT": "offline test"})
    work = broker.poll("windows-main")

    result = {
        "success": True,
        "job_id": "job_r_" + ident[5:],
        "record": {"job_id": "job_r_" + ident[5:], "status": "failed", "error": "Disk full"},
    }
    broker.finish("windows-main", ident, work["claim"], result)

    # Delivery failed, notification is durable in outbox
    stored = outbox.snapshot_telegram_undelivered()
    assert len(stored) == 1
    assert stored[0]["telegram"]["delivered"] is False
    assert stored[0]["telegram"]["attempts"] == 1

    # Network recovers
    should_fail = False
    dispatcher.replay_pending_telegram()

    # Undelivered queue is now empty
    undelivered = outbox.snapshot_telegram_undelivered()
    assert len(undelivered) == 0
    assert len(delivered) == 1
    assert "Device: <b>windows-main</b>" in delivered[0]["text"]


def test_gateway_running_jobs_watcher(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env

    def fake_post(url, *, json, timeout):
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    # 1. Device is present
    broker.heartbeat("windows-main", {"platform": "Windows"})

    # 2. Agent finishes start call with status "running"
    ident = broker.submit("windows-main", "start", {"PROMPT": "long task"})
    work = broker.poll("windows-main")
    job_id = "job_r_" + ident[5:]
    broker.finish("windows-main", ident, work["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "running",
    })

    # Running jobs table has our job
    with broker.lock:
        row = broker.conn.execute("SELECT * FROM running_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row is not None
        assert row["job_id"] == job_id

    # 3. Watcher check polls status
    broker.check_running_jobs()
    status_work = broker.poll("windows-main")
    assert status_work is not None
    assert status_work["action"] == "status"
    assert status_work["payload"]["job_id"] == job_id

    # 4. Device finishes status call with completed
    broker.finish("windows-main", status_work["id"], status_work["claim"], {
        "success": True,
        "record": {
            "job_id": job_id,
            "status": "completed",
            "task_preview": "long task",
        },
    })

    # Job is removed from running_jobs and notification enqueued
    with broker.lock:
        assert broker.conn.execute("SELECT * FROM running_jobs WHERE job_id=?", (job_id,)).fetchone() is None

    assert outbox.list_pending()["count"] == 1
    assert outbox.list_pending()["notifications"][0]["job_id"] == job_id
