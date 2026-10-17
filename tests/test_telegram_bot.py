from __future__ import annotations

import json
from pathlib import Path

from agy_mcp.antigravity_metadata import conversation_title
from agy_mcp.models import CanonicalEvent, JobRecord
from agy_mcp.telegram_bot import TelegramBot, parse_command, resolve_job, safe_progress


def record(job_id: str, title: str | None = None) -> JobRecord:
    return JobRecord(
        job_id=job_id,
        session_id="conv-1",
        antigravity_title=title,
        title_source="antigravity" if title else "pending",
    )


def test_reads_only_authoritative_antigravity_title(tmp_path: Path):
    path = tmp_path / "conversation_metadata.json"
    path.write_text(
        json.dumps(
            {
                "conversations": {
                    "conv-1": {
                        "summary": {
                            "Title": "  Repair   Telegram monitoring  ",
                            "Preview": "secret prompt that must not be returned",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert conversation_title("conv-1", path) == "Repair Telegram monitoring"
    assert conversation_title("missing", path) is None


def test_command_parser_supports_bot_mentions_and_plain_chat():
    assert parse_command("/status@GumacuBot task one") == ("status", "task one")
    assert parse_command("hello Antigravity") == (None, "hello Antigravity")


def test_resolve_job_by_title_short_id_and_reports_ambiguity():
    first = record("job_1234567890_aaaaaaaaaaaa", "Server audit")
    second = record("job_1234567890_bbbbbbbbbbbb", "Server audit")
    selected, matches = resolve_job([first], "aaaaaaaaaaaa")
    assert selected == first
    selected, matches = resolve_job([first, second], "Server audit")
    assert selected is None
    assert matches == [first, second]


def test_safe_progress_never_returns_raw_metadata_or_prompt():
    events = [
        CanonicalEvent(
            type="tool_use",
            subtype="server_metrics",
            metadata={"raw": "secret raw log", "prompt": "do not expose"},
        )
    ]
    progress, timestamp = safe_progress(events)
    assert progress == "يستخدم أداة: server_metrics"
    assert timestamp
    assert "secret" not in progress


def test_first_poll_discards_existing_updates_without_handling_them(tmp_path: Path, monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": [{"update_id": 99, "message": {"text": "old"}}]}

    monkeypatch.setattr("agy_mcp.telegram_bot.httpx.get", lambda *args, **kwargs: Response())
    bot = TelegramBot(
        token="token",
        chat_id="1",
        store=object(),
        supervisor=object(),
        safety=object(),
        state_path=tmp_path / "state.json",
    )
    bot._discard_backlog()
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["offset"] == 100

