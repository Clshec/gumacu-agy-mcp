"""Remote last-reply callbacks must fetch answers, never lifecycle summaries."""
from pathlib import Path

import pytest
from test_multi_device_telegram import DummyStore, make_test_bot

from agy_mcp.models import JobRecord


def remote_bot(tmp_path: Path, monkeypatch, reply):
    record = JobRecord(
        job_id="job_r_80ae3a83e5766062ed497aeafade71d7",
        status="completed",
        extra={"device_id": "windows-main", "summary": "Remote task on windows-main reached completed."},
    )
    bot, sent = make_test_bot(tmp_path, store=DummyStore([record]))
    monkeypatch.setattr(bot, "_records", lambda: [record])
    bot._state["active_device_id"] = "another-device"
    calls = []

    def submit(device, action, payload):
        calls.append((device, action, payload))
        return reply

    monkeypatch.setattr("agy_mcp.device_tools.submit", submit)
    return bot, sent, calls, record


def test_last_reply_fetches_full_answer_from_original_owner(tmp_path, monkeypatch):
    answer = "الحمد لله، كله تمام! <hello>\n" * 300
    bot, sent, calls, record = remote_bot(tmp_path, monkeypatch, {"success": True, "result_text": answer})
    bot._last_reply(record.job_id[-12:])
    assert calls == [("windows-main", "result", {"job_id": record.job_id})]
    assert "Remote task on" not in sent[-1]["text"]
    assert "&lt;hello&gt;" in sent[-1]["text"]
    assert sent[-1]["text"].count("الحمد لله") == 300
    assert "windows-main" in sent[-1]["text"]


@pytest.mark.parametrize("reply, expected", [
    ({"success": True, "pending": True}, "جارٍ جلب الرد"),
    ({"success": False, "error": "offline"}, "تعذر جلب الرد"),
    ({"success": True, "result_text": ""}, "لم يصل رد نصي"),
    ({"success": True, "result_text": {"invalid": True}}, "لم يصل رد نصي"),
])
def test_last_reply_never_substitutes_summary(tmp_path, monkeypatch, reply, expected):
    bot, sent, calls, record = remote_bot(tmp_path, monkeypatch, reply)
    bot._last_reply(record.job_id[-12:])
    assert expected in sent[-1]["text"]
    assert "Remote task on" not in sent[-1]["text"]
    assert len(calls) == 1
