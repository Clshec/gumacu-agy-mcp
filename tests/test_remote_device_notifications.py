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

from pathlib import Path

import httpx
import pytest

from agy_mcp.device_gateway import Broker, token_hash
from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox


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

    # Metadata completion must not consume the one terminal notification.
    assert outbox.list_pending()["count"] == 1
    broker.check_running_jobs()
    result_work = broker.poll("windows-main")
    assert result_work["action"] == "result"
    broker.check_running_jobs()
    assert broker.poll("windows-main") is None  # no duplicate while leased
    broker.finish("windows-main", result_work["id"], result_work["claim"], {
        "success": True,
        "record": {"job_id": job_id, "status": "completed"},
        "result_text": "أهلاً وسهلاً! كيف يمكنني مساعدتك اليوم؟",
    })

    # Tracking ends only after the real answer is durably enqueued.
    with broker.lock:
        assert broker.conn.execute("SELECT * FROM running_jobs WHERE job_id=?", (job_id,)).fetchone() is None

    assert outbox.list_pending()["count"] == 2
    notes = outbox.list_pending()["notifications"]
    assert {n["status"] for n in notes} == {"started", "completed"}
    assert all(n["job_id"] == job_id for n in notes)
    assert next(n for n in notes if n["status"] == "completed")["summary"].startswith("أهلاً")


def test_remote_lifecycle_title_persistence_and_no_duplicates(test_env, monkeypatch: pytest.MonkeyPatch):
    broker, outbox, dispatcher = test_env

    delivered = []
    def fake_post(url, *, json, timeout):
        delivered.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    prompt = "Task 1: Run diagnostics\nSecond line with more details"
    ident = broker.submit("windows-main", "start", {"PROMPT": prompt, "SESSION_ID": "sess_abc123"})
    job_id = "job_r_" + ident[5:]

    # Check task label extracted and persisted in remote_lifecycle
    with broker.lock:
        row = broker.conn.execute("SELECT * FROM remote_lifecycle WHERE job_id=?", (job_id,)).fetchone()
        assert row is not None
        assert row["task_label"] == "Task 1: Run diagnostics"
        assert row["session_id"] == "sess_abc123"

    work = broker.poll("windows-main")
    # 1. Device reports running -> sends STARTED notification
    broker.finish("windows-main", ident, work["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "running",
    })
    assert len(delivered) == 1
    assert "started" in delivered[0]["text"]
    assert "Task 1: Run diagnostics" in delivered[0]["text"]
    assert "pending" not in delivered[0]["text"]
    assert delivered[0].get("reply_markup", {}).get("inline_keyboard") is not None

    # 2. Terminal update with metadata title via status check
    ident_stat = broker.submit("windows-main", "status", {"job_id": job_id})
    work_stat = broker.poll("windows-main")
    broker.finish("windows-main", ident_stat, work_stat["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "antigravity_title": "Real Antigravity Title",
        "result_text": "Diagnostics complete: OK",
    })
    assert len(delivered) == 2
    assert "completed" in delivered[1]["text"]
    assert "Real Antigravity Title" in delivered[1]["text"]
    assert "pending" not in delivered[1]["text"]

    # 3. Duplicate status/terminal must NOT send another terminal notification
    ident_stat2 = broker.submit("windows-main", "status", {"job_id": job_id})
    work_stat2 = broker.poll("windows-main")
    broker.finish("windows-main", ident_stat2, work_stat2["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "antigravity_title": "Real Antigravity Title",
        "result_text": "Diagnostics complete: OK",
    })
    assert len(delivered) == 2  # strictly 2, no duplicate!


def test_session_title_inheritance_across_turns(test_env):
    broker, outbox, dispatcher = test_env

    # First turn establishes title
    ident1 = broker.submit("windows-main", "start", {"PROMPT": "First Turn Prompt", "SESSION_ID": "sess_inherit"})
    job_id1 = "job_r_" + ident1[5:]
    work1 = broker.poll("windows-main")
    broker.finish("windows-main", ident1, work1["claim"], {
        "success": True,
        "job_id": job_id1,
        "status": "completed",
        "antigravity_title": "Inherited Session Title",
    })

    # Second turn inherits the title even with different prompt
    ident2 = broker.submit("windows-main", "start", {"PROMPT": "Follow-up question", "SESSION_ID": "sess_inherit"})
    job_id2 = "job_r_" + ident2[5:]

    with broker.lock:
        row = broker.conn.execute("SELECT * FROM remote_lifecycle WHERE job_id=?", (job_id2,)).fetchone()
        assert row is not None
        assert row["antigravity_title"] == "Inherited Session Title"
        assert row["title_source"] == "antigravity"


def test_load_remote_jobs_as_records_and_resolve(test_env):
    broker, outbox, dispatcher = test_env
    from agy_mcp.device_gateway import load_remote_jobs_as_records
    from agy_mcp.telegram_bot import resolve_job

    ident = broker.submit("windows-main", "start", {"PROMPT": "Quick test", "SESSION_ID": "sess_rec"})
    job_id = "job_r_" + ident[5:]
    work = broker.poll("windows-main")
    broker.finish("windows-main", ident, work["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "antigravity_title": "Record Test Title",
        "result_text": "Done OK",
    })

    records = load_remote_jobs_as_records(broker.conn_path if hasattr(broker, "conn_path") else broker.conn.execute("PRAGMA database_list").fetchone()["file"])
    assert len(records) >= 1
    rec = next(r for r in records if r.job_id == job_id)
    assert rec.antigravity_title == "Record Test Title"
    assert rec.extra["device_id"] == "windows-main"
    assert rec.extra["summary"] == "Done OK"

    # Verify resolve_job matches last 12 chars
    matched, _ = resolve_job(records, job_id[-12:])
    assert matched is not None
    assert matched.job_id == job_id


def test_remote_telegram_bot_callbacks(test_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    broker, outbox, dispatcher = test_env
    from agy_mcp.safety import SafetyPolicy
    from agy_mcp.session_store import SessionStore
    from agy_mcp.telegram_bot import TelegramBot

    db_file = broker.conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("AGY_DEVICE_GATEWAY_DB", db_file)

    ident = broker.submit("windows-main", "start", {"PROMPT": "Callback Test Task\nDetails", "SESSION_ID": "sess_cb_test"})
    job_id = "job_r_" + ident[5:]
    work = broker.poll("windows-main")
    broker.finish("windows-main", ident, work["claim"], {
        "success": True,
        "job_id": job_id,
        "status": "completed",
        "antigravity_title": "Antigravity Callback Title",
        "result_text": "Callback execution finished successfully",
    })

    store = SessionStore(tmp_path / "sessions")
    safety = SafetyPolicy()
    state_path = tmp_path / "telegram_state.json"
    bot = TelegramBot(token="dummy", chat_id="902051097", store=store, supervisor=None, safety=safety, state_path=state_path)
    monkeypatch.setattr(bot, "_run_background", lambda fn: fn(), raising=False)
    def read_result(device, action, payload):
        assert device == "windows-main" and action == "result"
        assert payload == {"job_id": job_id}
        return {"success": True, "result_text": "Callback execution finished successfully"}
    monkeypatch.setattr("agy_mcp.device_tools.submit", read_result)

    sent = []
    bot.send = lambda msg, buttons=None, menu=False: sent.append((msg, buttons))
    ref = job_id[-12:]

    # 1. Test status callback
    cb_status = {"id": "1", "message": {"chat": {"id": 902051097}}, "data": f"status:{ref}"}
    bot._handle_callback(cb_status)
    assert len(sent) == 1
    assert "windows-main" in sent[-1][0]
    assert "Antigravity Callback Title" in sent[-1][0]
    assert "completed" in sent[-1][0]
    assert sent[-1][1] is not None  # has buttons

    # 2. Test last reply callback
    cb_last = {"id": "2", "message": {"chat": {"id": 902051097}}, "data": f"last:{ref}"}
    bot._handle_callback(cb_last)
    assert len(sent) == 2
    assert "Callback execution finished successfully" in sent[-1][0]

    # 3. Test use callback
    cb_use = {"id": "3", "message": {"chat": {"id": 902051097}}, "data": f"use:{ref}"}
    bot._handle_callback(cb_use)
    assert len(sent) == 3
    assert "windows-main" in sent[-1][0]
    assert bot._state.get("active_device_id") == "windows-main"
    assert bot._state.get("active_session_id") == "sess_cb_test"


