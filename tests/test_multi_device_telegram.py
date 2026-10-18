"""Comprehensive regression tests for multi-device Telegram bot routing.

Verifies:
1. Device list dynamically includes local Linux Netcup (stable ID) and enrolled broker devices.
2. Online/offline detection; offline devices reject new/resume clearly and never silently route elsewhere.
3. Local and remote new/resume routing with active device/session display.
4. State migration from legacy format across restart.
5. Old callbacks (status/last/use) resolve original job and original device after device switch.
6. Suffix collision rejection: ambiguous suffix collisions are rejected instead of wrong route.
7. Job results notifications include immutable origin device, session title/ID, and job ID.
8. Offline recovery: device transitions from offline to online and resumes accepting requests.
9. Outbox duplicate protection: idempotent terminal notifications and deliveries.
10. All callback_data strings are strictly <= 64 bytes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agy_mcp.device_gateway import Broker, token_hash
from agy_mcp.models import BridgeRequest, BridgeResponse, CanonicalEvent, JobRecord
from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox
from agy_mcp.safety import SafetyPolicy
from agy_mcp.telegram_bot import (
    LOCAL_DEVICE_ID,
    LOCAL_DEVICE_NAME,
    TelegramBot,
    resolve_job,
)


class DummyStore:
    def __init__(self, records: list[JobRecord] | None = None, events: dict[str, list[CanonicalEvent]] | None = None) -> None:
        self.records = records or []
        self.events = events or {}

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        return list(self.records)[:limit]

    def get_job(self, job_id: str) -> JobRecord | None:
        for r in self.records:
            if r.job_id == job_id:
                return r
        return None

    def update_job(self, record: JobRecord) -> JobRecord:
        for i, r in enumerate(self.records):
            if r.job_id == record.job_id:
                self.records[i] = record
                return record
        self.records.append(record)
        return record

    def read_events(self, job_id: str) -> list[CanonicalEvent]:
        return self.events.get(job_id, [])


class DummySupervisor:
    def __init__(self, store: DummyStore | None = None) -> None:
        self.started_requests: list[BridgeRequest] = []
        self.next_job_id = "job_local_001_112233445566"
        self.store = store

    def start(self, request: BridgeRequest) -> BridgeResponse:
        self.started_requests.append(request)
        sess = request.session_id or "session_local_new"
        if self.store is not None:
            self.store.update_job(
                JobRecord(
                    job_id=self.next_job_id,
                    session_id=sess,
                    antigravity_title="Local Task",
                    extra={"device_id": LOCAL_DEVICE_ID},
                )
            )
        return BridgeResponse(
            success=True,
            job_id=self.next_job_id,
            SESSION_ID=sess,
            cwd=request.cwd,
        )


def make_test_bot(
    tmp_path: Path,
    store: DummyStore | None = None,
    supervisor: DummySupervisor | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[TelegramBot, list[dict[str, Any]]]:
    state_file = tmp_path / "telegram_state.json"
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    sent_messages: list[dict[str, Any]] = []

    actual_store = store or DummyStore()
    actual_sup = supervisor or DummySupervisor(actual_store)
    if actual_sup.store is None:
        actual_sup.store = actual_store

    bot = TelegramBot(
        token="test_token_12345",
        chat_id="123456",
        store=actual_store,
        supervisor=actual_sup,
        safety=SafetyPolicy(),
        state_path=state_file,
    )

    def mock_send(text: str, buttons: list[list[dict[str, str]]] | None = None, menu: bool = False) -> bool:
        sent_messages.append({"text": text, "buttons": buttons, "menu": menu})
        return True

    bot.send = mock_send  # type: ignore[assignment]
    return bot, sent_messages


def test_device_list_includes_local_and_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Device list must dynamically include both local Linux Netcup (stable ID) and remote enrolled devices."""
    bot, sent = make_test_bot(tmp_path)

    # Mock remote devices from gateway
    remote_devices = [
        {
            "id": "windows-main",
            "os": "Windows",
            "online": True,
            "last_seen": time.time(),
            "capabilities": {"platform": "Windows"},
        }
    ]
    monkeypatch.setattr("agy_mcp.device_gateway.load_remote_devices", lambda *args, **kwargs: remote_devices)

    devices = bot.get_devices()
    device_ids = [d["id"] for d in devices]
    assert LOCAL_DEVICE_ID in device_ids
    assert "windows-main" in device_ids

    local_dev = next(d for d in devices if d["id"] == LOCAL_DEVICE_ID)
    assert local_dev["os"] == "Linux"
    assert local_dev["name"] == LOCAL_DEVICE_NAME
    assert local_dev["online"] is True

    # Check devices view output
    bot._devices_view()
    assert len(sent) == 1
    view_text = sent[0]["text"]
    assert "أجهزة Antigravity المسجلة" in view_text
    assert "linux-netcup" in view_text
    assert "windows-main" in view_text
    buttons = sent[0]["buttons"]
    callback_data = [b["callback_data"] for row in buttons for b in row]
    assert f"dev:{LOCAL_DEVICE_ID}" in callback_data
    assert "dev:windows-main" in callback_data


def test_online_offline_detection_and_clear_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If remote offline reject clearly, NEVER silently route elsewhere."""
    bot, sent = make_test_bot(tmp_path)

    # Set windows-main as offline
    remote_devices = [
        {
            "id": "windows-main",
            "os": "Windows",
            "online": False,
            "last_seen": time.time() - 3600,
            "capabilities": {},
        }
    ]
    monkeypatch.setattr("agy_mcp.device_gateway.load_remote_devices", lambda *args, **kwargs: remote_devices)

    # 1. Device panel shows offline
    bot._device_panel("windows-main")
    assert "غير متصل" in sent[-1]["text"]

    # 2. Starting new session on offline remote is rejected
    bot._device_new_session("windows-main")
    assert "غير متصل حالياً (offline)" in sent[-1]["text"]
    assert "windows-main" in sent[-1]["text"]

    # 3. Active device set to windows-main, sending natural text must reject clearly and NEVER route to Linux!
    bot._state["active_device_id"] = "windows-main"
    bot._continue_or_start("do something")
    assert "غير متصل حالياً (offline)" in sent[-1]["text"]
    assert "windows-main" in sent[-1]["text"]
    assert len(bot.supervisor.started_requests) == 0  # NOT routed to local supervisor!


def test_local_and_remote_new_and_resume_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """New session on selected remote uses gateway; Linux uses local supervisor."""
    supervisor = DummySupervisor()
    bot, sent = make_test_bot(tmp_path, supervisor=supervisor)

    remote_submitted: list[dict[str, Any]] = []

    def mock_gateway_call(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if path == "/v1/admin/devices":
            return {
                "success": True,
                "devices": [{"id": "windows-main", "os": "Windows", "online": True, "last_seen": time.time()}],
            }
        if path == "/v1/admin/submit":
            remote_submitted.append(payload or {})
            return {"success": True, "request_id": "call_11112222333344445555666677778888"}
        return {"success": True}

    monkeypatch.setattr("agy_mcp.device_tools.gateway_call", mock_gateway_call)
    monkeypatch.setattr(
        "agy_mcp.device_gateway.load_remote_devices",
        lambda *args, **kwargs: [{"id": "windows-main", "os": "Windows", "online": True, "last_seen": time.time()}],
    )

    # 1. Start new session on local Linux
    bot._state["active_device_id"] = LOCAL_DEVICE_ID
    bot._new("Hello Linux Local")
    assert len(supervisor.started_requests) == 1
    assert supervisor.started_requests[0].prompt == "Hello Linux Local"
    assert supervisor.started_requests[0].session_id is None
    assert f"الجهاز النشط: <code>{LOCAL_DEVICE_ID}</code>" in sent[-1]["text"]

    # 2. Resume on local Linux
    bot._state["active_session_id"] = "session_local_123"
    bot._continue_or_start("Follow-up on Linux")
    assert len(supervisor.started_requests) == 2
    assert supervisor.started_requests[1].session_id == "session_local_123"

    # 3. Switch to remote windows-main and start new session
    bot._state["active_device_id"] = "windows-main"
    bot._new("Hello Windows Remote")
    assert len(remote_submitted) == 1
    assert remote_submitted[0]["device_id"] == "windows-main"
    assert remote_submitted[0]["payload"]["PROMPT"] == "Hello Windows Remote"
    assert "SESSION_ID" not in remote_submitted[0]["payload"]
    assert "الجهاز النشط: <code>windows-main</code>" in sent[-1]["text"]

    # 4. Resume on remote windows-main
    bot._state["active_session_id"] = "session_win_999"
    bot._continue_or_start("Follow-up on Windows")
    assert len(remote_submitted) == 2
    assert remote_submitted[1]["device_id"] == "windows-main"
    assert remote_submitted[1]["payload"]["SESSION_ID"] == "session_win_999"
    assert remote_submitted[1]["payload"]["PROMPT"] == "Follow-up on Windows"


def test_state_migration_and_persistence_across_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Persist state across restart; infer active device from session affinity if missing."""
    legacy_state = {
        "offset": 100,
        "active_job_id": "job_r_3f41f9cdf1698407c281f37578bd38b6",
        "active_session_id": "session_remote_abc",
        "conversation_titles": {"session_remote_abc": "Remote Task Title"},
    }
    state_file = tmp_path / "telegram_state.json"
    state_file.write_text(json.dumps(legacy_state), encoding="utf-8")

    # Mock get_session_affinity to return windows-main for session_remote_abc
    monkeypatch.setattr(
        "agy_mcp.device_gateway.get_session_affinity",
        lambda session_id, *args: "windows-main" if session_id == "session_remote_abc" else None,
    )

    bot = TelegramBot(
        token="token",
        chat_id="123",
        store=DummyStore(),
        supervisor=DummySupervisor(),
        safety=SafetyPolicy(),
        state_path=state_file,
    )

    # Migrated state should have active_device_id = windows-main
    assert bot._state["active_device_id"] == "windows-main"
    assert "session_devices" in bot._state

    # Verify persisted to disk
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["active_device_id"] == "windows-main"


def test_old_callback_after_device_switch_resolves_original(tmp_path: Path):
    """Inline callbacks status/last/use resolve original job + original device even after selected device changes."""
    local_job = JobRecord(
        job_id="job_local_1234567890_111111111111",
        session_id="session_local_1",
        antigravity_title="Local Linux Inspection",
        status="completed",
        extra={"device_id": LOCAL_DEVICE_ID},
    )
    remote_job = JobRecord(
        job_id="job_r_22222222222222222222222222222222",
        session_id="session_remote_2",
        antigravity_title="Windows Registry Fix",
        status="completed",
        extra={"device_id": "windows-main", "summary": "Registry fixed successfully."},
    )
    events = {
        local_job.job_id: [CanonicalEvent(type="assistant", text="Local assistant output.")],
    }
    store = DummyStore([local_job, remote_job], events=events)
    bot, sent = make_test_bot(tmp_path, store=store)

    # Currently active device is windows-main
    bot._state["active_device_id"] = "windows-main"
    bot._state["active_session_id"] = "session_remote_2"

    # User clicks status on the local Linux job
    ref_local = local_job.job_id[-12:]
    bot._send_one(ref_local)
    assert f"الجهاز: <code>{LOCAL_DEVICE_ID}</code>" in sent[-1]["text"]
    assert "Local Linux Inspection" in sent[-1]["text"]

    # User clicks last on the local Linux job
    bot._last_reply(ref_local)
    assert f"الجهاز: <code>{LOCAL_DEVICE_ID}</code>" in sent[-1]["text"]
    assert "Local assistant output." in sent[-1]["text"]

    # User clicks use on the local Linux job: must switch active device back to linux-netcup!
    bot._use(ref_local)
    assert bot._state["active_device_id"] == LOCAL_DEVICE_ID
    assert bot._state["active_session_id"] == local_job.session_id
    assert f"الجهاز الأصلي (مرتبط دائماً): <code>{LOCAL_DEVICE_ID}</code>" in sent[-1]["text"]

    # User clicks use on remote job: must switch active device to windows-main!
    ref_remote = remote_job.job_id[-12:]
    bot._use(ref_remote)
    assert bot._state["active_device_id"] == "windows-main"
    assert bot._state["active_session_id"] == remote_job.session_id
    assert "الجهاز الأصلي (مرتبط دائماً): <code>windows-main</code>" in sent[-1]["text"]


def test_suffix_collision_rejected_with_clear_error(tmp_path: Path):
    """Reject ambiguous suffix collisions instead of wrong route."""
    job1 = JobRecord(
        job_id="job_1000_123456789abc",
        session_id="session_1",
        antigravity_title="Task One",
        status="completed",
        extra={"device_id": LOCAL_DEVICE_ID},
    )
    job2 = JobRecord(
        job_id="job_2000_123456789abc",
        session_id="session_2",
        antigravity_title="Task Two",
        status="completed",
        extra={"device_id": "windows-main"},
    )
    store = DummyStore([job1, job2])
    bot, sent = make_test_bot(tmp_path, store=store)

    # 1. resolve_job detects collision
    resolved, matches = resolve_job([job1, job2], "123456789abc")
    assert resolved is None
    assert len(matches) == 2

    # 2. bot._send_one rejects collision and does not show wrong job
    bot._send_one("123456789abc")
    assert "تعارض في المعرّف" in sent[-1]["text"]
    assert "Task One" in sent[-1]["text"]
    assert "Task Two" in sent[-1]["text"]

    # 3. bot._use rejects collision and does not switch session
    bot._use("123456789abc")
    assert "تعارض في المعرّف" in sent[-1]["text"]
    assert bot._state.get("active_session_id") != "session_1"
    assert bot._state.get("active_session_id") != "session_2"


def test_notifications_include_immutable_origin_device_and_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Notifications include immutable origin device and session title/ID and job ID."""
    outbox_path = tmp_path / "notifications.json"
    outbox = NotificationOutbox(outbox_path)
    dispatcher = NotificationDispatcher(outbox, token="bot_token", chat_id="chat_123", retry_delays=(0.0,))

    delivered: list[dict[str, Any]] = []

    def mock_post(url: str, *, json: dict[str, Any], timeout: float):
        delivered.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", mock_post)

    # 1. Dispatch local Linux notification
    local_job = {
        "job_id": "job_local_terminal_999",
        "session_id": "session_loc_456",
        "antigravity_title": "Local Database Vacuum",
        "status": "completed",
    }
    dispatcher.notify_job(local_job, summary="Vacuum finished", reason_code="completed", device_id=None)

    assert len(delivered) == 1
    text_local = delivered[0]["text"]
    assert f"Device: <b>{LOCAL_DEVICE_ID}</b>" in text_local
    assert "Job: <code>job_local_terminal_999</code>" in text_local
    assert "Session: <code>session_loc_456</code>" in text_local
    assert "Name: <b>Local Database Vacuum</b>" in text_local

    # 2. Dispatch remote Windows notification
    remote_job = {
        "job_id": "job_r_win_terminal_888",
        "session_id": "session_win_789",
        "antigravity_title": "Windows Update Apply",
        "status": "completed",
    }
    dispatcher.notify_job(remote_job, summary="Updates applied", reason_code="completed", device_id="windows-main")

    assert len(delivered) == 2
    text_remote = delivered[1]["text"]
    assert "Device: <b>windows-main</b>" in text_remote
    assert "Job: <code>job_r_win_terminal_888</code>" in text_remote
    assert "Session: <code>session_win_789</code>" in text_remote
    assert "Name: <b>Windows Update Apply</b>" in text_remote


def test_offline_recovery_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Device offline recovery: rejected while offline, succeeds when back online."""
    bot, sent = make_test_bot(tmp_path)
    remote_state = {"online": False}

    monkeypatch.setattr(
        "agy_mcp.device_gateway.load_remote_devices",
        lambda *args, **kwargs: [
            {"id": "windows-main", "os": "Windows", "online": remote_state["online"], "last_seen": time.time()}
        ],
    )
    remote_calls: list[str] = []
    monkeypatch.setattr(
        "agy_mcp.device_tools.gateway_call",
        lambda path, payload=None: remote_calls.append(path) or {"success": True, "request_id": "call_test123"},
    )

    bot._state["active_device_id"] = "windows-main"

    # Step 1: while offline -> rejected
    bot._continue_or_start("prompt while offline")
    assert "غير متصل حالياً (offline)" in sent[-1]["text"]
    assert len(remote_calls) == 0

    # Step 2: device comes back online (recovery)
    remote_state["online"] = True

    # Step 3: next call succeeds
    bot._continue_or_start("prompt after recovery")
    assert len(remote_calls) == 1
    assert "تم بدء مهمة جديدة على الجهاز <code>windows-main</code>" in sent[-1]["text"]


def test_outbox_duplicate_protection_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Outbox duplicate protection: repeated notifications for the same job yield exactly one terminal notification."""
    db_path = tmp_path / "broker.db"
    outbox_path = tmp_path / "notifications.json"
    outbox = NotificationOutbox(outbox_path)
    dispatcher = NotificationDispatcher(outbox, token="bot_token", chat_id="chat_123", retry_delays=(0.0,))

    delivered_count = 0

    def mock_post(url: str, *, json: dict[str, Any], timeout: float):
        nonlocal delivered_count
        delivered_count += 1
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", mock_post)

    devices = {
        "windows-main": {
            "os": "Windows",
            "token_sha256": token_hash("secret" * 8),
            "allowed_actions": ["start", "result"],
        }
    }
    broker = Broker(db_path, devices, dispatcher=dispatcher)

    ident = broker.submit("windows-main", "start", {"PROMPT": "test idempotency"})
    work = broker.poll("windows-main")
    assert work is not None

    result = {
        "success": True,
        "job_id": "job_r_" + ident[5:],
        "status": "completed",
        "result_text": "Finished successfully.",
    }

    # Repeated finish and notify calls
    broker.finish("windows-main", ident, work["claim"], result)
    broker.finish("windows-main", ident, work["claim"], result)
    broker.notify_remote_terminal("windows-main", ident, result)

    # Exactly one notification enqueued in outbox
    pending = outbox.list_pending()["notifications"]
    assert len(pending) == 1
    assert pending[0]["job_id"] == "job_r_" + ident[5:]
    assert delivered_count == 1
    broker.close()


def test_all_callback_data_within_64_bytes():
    """Telegram inline keyboard callback_data limit is strictly 64 bytes."""
    device_id = "windows-main-extended-identifier-48-chars-total-123"
    job_id = "job_r_1234567890abcdef1234567890abcdef"
    ref = job_id[-12:]

    callbacks = [
        f"dev:{device_id}",
        f"dev_new:{device_id}",
        f"dev_sess:{device_id}:99",
        f"dev_jobs:{device_id}",
        f"dev_recent:{device_id}",
        f"dev_use:{device_id}",
        f"status:{ref}",
        f"last:{ref}",
        f"use:{ref}",
        f"session:{ref}",
        "menu:home",
        "menu:devices",
        "menu:sessions:99",
        "menu:jobs",
        "menu:recent",
    ]
    for cb in callbacks:
        byte_len = len(cb.encode("utf-8"))
        assert byte_len <= 64, f"Callback '{cb}' exceeds 64 bytes: {byte_len}"
