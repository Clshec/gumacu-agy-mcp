from __future__ import annotations

from pathlib import Path

import httpx

from agy_mcp.models import JobRecord
from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox


def _record(status: str = "completed") -> JobRecord:
    return JobRecord(
        job_id="job_1234567890_abcdef123456",
        status=status,
        cwd="/safe/workspace",
        started_at="2026-10-17T08:00:00Z",
        finished_at="2026-10-17T08:01:00Z",
        error="quota exhausted" if status == "quota_exhausted" else None,
    )


def test_outbox_persists_deduplicates_and_acknowledges(tmp_path: Path):
    path = tmp_path / "notifications.json"
    outbox = NotificationOutbox(path)
    first = outbox.record(_record(), summary="done")
    second = outbox.record(_record(), summary="duplicate")

    assert first["id"] == second["id"]
    assert outbox.list_pending()["count"] == 1

    reloaded = NotificationOutbox(path)
    assert reloaded.list_pending()["notifications"][0]["summary"] == "done"
    acknowledged = reloaded.acknowledge(first["id"])
    assert acknowledged is not None
    assert acknowledged["acknowledged_at"]
    assert reloaded.acknowledge(first["id"])["acknowledged_at"] == acknowledged["acknowledged_at"]
    assert NotificationOutbox(path).list_pending()["count"] == 0


def test_telegram_disabled_still_records_quota_notification(tmp_path: Path):
    outbox = NotificationOutbox(tmp_path / "notifications.json")
    dispatcher = NotificationDispatcher(outbox, token="", chat_id="")

    item = dispatcher.notify_job(
        _record("quota_exhausted"),
        summary="Antigravity quota is exhausted",
        reason_code="quota_exhausted",
    )

    assert item is not None
    stored = outbox.list_pending()["notifications"][0]
    assert stored["reason_code"] == "quota_exhausted"
    assert stored["telegram"]["configured"] is False
    assert stored["telegram"]["attempts"] == 0


def test_telegram_success_is_persisted_without_exposing_token(tmp_path: Path, monkeypatch):
    outbox = NotificationOutbox(tmp_path / "notifications.json")
    dispatcher = NotificationDispatcher(
        outbox, token="secret-token", chat_id="123", retry_delays=(0.0,),
    )
    item = outbox.record(_record(), summary="<done & safe>")
    seen: dict = {}

    def fake_post(url, *, json, timeout):
        seen.update(url=url, json=json, timeout=timeout)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    dispatcher._deliver_telegram(item)

    stored = NotificationOutbox(outbox.path).list_pending()["notifications"][0]
    assert stored["telegram"]["delivered"] is True
    assert stored["telegram"]["attempts"] == 1
    assert "secret-token" not in outbox.path.read_text(encoding="utf-8")
    assert "&lt;done &amp; safe&gt;" in seen["json"]["text"]


def test_telegram_retries_and_records_safe_failure(tmp_path: Path, monkeypatch):
    outbox = NotificationOutbox(tmp_path / "notifications.json")
    dispatcher = NotificationDispatcher(
        outbox, token="secret-token", chat_id="123", retry_delays=(0.0, 0.0, 0.0),
    )
    item = outbox.record(_record("failed"), summary="failed")

    def fail_post(*args, **kwargs):
        raise httpx.TimeoutException("request timed out with secret-token")

    monkeypatch.setattr(httpx, "post", fail_post)
    dispatcher._deliver_telegram(item)

    stored = NotificationOutbox(outbox.path).list_pending()["notifications"][0]
    assert stored["telegram"]["delivered"] is False
    assert stored["telegram"]["attempts"] == 3
    assert stored["telegram"]["last_error"] == "TimeoutException"
    assert "secret-token" not in outbox.path.read_text(encoding="utf-8")


def test_telegram_replay_survives_chat_acknowledgement(tmp_path: Path, monkeypatch):
    """Acknowledging in ChatGPT must not lose an undelivered Telegram alert."""
    outbox = NotificationOutbox(tmp_path / "notifications.json")
    item = outbox.record(_record(), summary="important result")
    outbox.acknowledge(item["id"])

    assert outbox.list_pending()["count"] == 0
    assert [n["id"] for n in outbox.list_telegram_undelivered()["notifications"]] == [
        item["id"]
    ]

    scheduled: list[str] = []

    class CapturingThread:
        def __init__(self, *, target, args, name, daemon):
            scheduled.append(args[0]["id"])

        def start(self):
            pass

    monkeypatch.setattr("agy_mcp.notifications.threading.Thread", CapturingThread)
    dispatcher = NotificationDispatcher(
        outbox, token="test-token", chat_id="123", retry_delays=(0.0,)
    )
    dispatcher.replay_pending_telegram()
    assert scheduled == [item["id"]]

    outbox.update_telegram(
        item["id"], configured=True, delivered=True, attempts=1, error=None
    )
    assert outbox.list_telegram_undelivered()["count"] == 0


def test_telegram_replay_schedules_all_pages_even_when_delivery_is_fast(
    tmp_path: Path, monkeypatch
):
    outbox = NotificationOutbox(tmp_path / "notifications.json")
    for i in range(105):
        record = _record()
        record.job_id = f"job_1234567890_{i:012x}"
        outbox.record(record, summary=f"result {i}")
    sent: list[str] = []

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon):
            self.item = args[0]

        def start(self):
            sent.append(self.item["id"])
            outbox.update_telegram(
                self.item["id"], configured=True, delivered=True,
                attempts=1, error=None,
            )

    monkeypatch.setattr("agy_mcp.notifications.threading.Thread", ImmediateThread)
    dispatcher = NotificationDispatcher(
        outbox, token="test-token", chat_id="123", retry_delays=(0.0,)
    )
    dispatcher.replay_pending_telegram()
    assert len(sent) == 105
    assert len(set(sent)) == 105
    assert outbox.list_telegram_undelivered()["count"] == 0
