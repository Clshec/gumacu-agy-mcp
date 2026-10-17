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


def test_title_falls_back_to_actual_antigravity_task_and_can_refresh(tmp_path: Path, monkeypatch):
    class Store:
        def update_job(self, value):
            return value

    item = record("job_1234567890_cccccccccccc")
    item.request = {"prompt": "  Inspect server load and report clearly  \nDo not change it"}
    monkeypatch.setattr("agy_mcp.telegram_bot.conversation_title", lambda session_id: None)
    from agy_mcp.telegram_bot import refresh_title

    refreshed = refresh_title(Store(), item)
    assert refreshed.antigravity_title == "Inspect server load and report clearly"
    assert refreshed.title_source == "antigravity_task"


class FakeStore:
    def __init__(self, records, events=None):
        self.records = records
        self.events = events or {}

    def list_jobs(self, limit=100):
        return list(self.records)

    def update_job(self, value):
        return value

    def read_events(self, job_id):
        return self.events.get(job_id, [])


def make_bot(tmp_path: Path, store: FakeStore) -> TelegramBot:
    return TelegramBot(
        token="token",
        chat_id="1",
        store=store,
        supervisor=object(),
        safety=object(),
        state_path=tmp_path / "state.json",
    )


def test_followup_job_inherits_stable_conversation_title(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agy_mcp.telegram_bot.conversation_title", lambda session_id: None)
    first = record("job_1", "Server load investigation")
    followup = record("job_2")
    followup.request = {"prompt": "What are you doing now?"}
    store = FakeStore([followup, first])
    bot = make_bot(tmp_path, store)

    inherited = bot._inherit_conversation_title(followup)
    refreshed = __import__("agy_mcp.telegram_bot", fromlist=["refresh_title"]).refresh_title(
        store, inherited,
    )

    assert refreshed.antigravity_title == "Server load investigation"
    assert refreshed.antigravity_title != "What are you doing now?"


def test_periodic_update_is_sent_only_after_interval(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agy_mcp.telegram_bot.conversation_title", lambda session_id: None)
    item = record("job_3", "Server load investigation")
    item.status = "running"
    item.extra["telegram_origin"] = True
    store = FakeStore(
        [item],
        {item.job_id: [CanonicalEvent(type="system", subtype="turn_start")]},
    )
    bot = make_bot(tmp_path, store)
    bot.progress_interval = 180
    bot._state[f"progress:{item.job_id}"] = {"sent_at": 1000, "fingerprint": "started"}
    sent = []
    monkeypatch.setattr(bot, "send", lambda text, buttons=None: sent.append(text) or True)

    bot._send_periodic_updates_once(now=1179)
    assert sent == []
    bot._send_periodic_updates_once(now=1180)

    assert len(sent) == 1
    assert "ما زالت جلسة Antigravity تعمل" in sent[0]
    assert "Server load investigation" in sent[0]


def test_persistent_menu_is_attached_below_the_chat(tmp_path: Path, monkeypatch):
    store = FakeStore([])
    bot = make_bot(tmp_path, store)
    captured = {}

    class Response:
        is_success = True

        def json(self):
            return {"ok": True}

    def fake_post(url, json, timeout):
        captured.update(json)
        return Response()

    monkeypatch.setattr("agy_mcp.telegram_bot.httpx.post", fake_post)
    assert bot.send("menu", menu=True) is True

    keyboard = captured["reply_markup"]["keyboard"]
    labels = {button["text"] for row in keyboard for button in row}
    assert "📋 حالة الجلسة" in labels
    assert "💬 آخر رد" in labels
    assert "➕ جلسة جديدة" in labels
    assert captured["reply_markup"]["is_persistent"] is True


def test_long_reply_is_delivered_in_full_numbered_chunks(tmp_path: Path, monkeypatch):
    bot = make_bot(tmp_path, FakeStore([]))
    payloads = []

    class Response:
        is_success = True

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(
        "agy_mcp.telegram_bot.httpx.post",
        lambda url, json, timeout: payloads.append(json) or Response(),
    )
    original = "بداية " + ("محتوى طويل " * 900) + " نهاية مؤكدة"
    assert bot.send(original, buttons=[[{"text": "آخر رد", "callback_data": "last:1"}]])

    assert len(payloads) > 1
    reconstructed = "".join(payload["text"].split("\n", 1)[1] for payload in payloads)
    assert "بداية" in reconstructed
    assert "نهاية مؤكدة" in reconstructed
    assert "parse_mode" not in payloads[0]
    assert "reply_markup" not in payloads[0]
    assert payloads[-1]["reply_markup"]["inline_keyboard"]


def test_advanced_session_browser_has_navigation_and_session_panels(tmp_path: Path, monkeypatch):
    records = []
    for index in range(8):
        item = record(f"job_{index:02d}_abcdefghijkl")
        item.session_id = f"conversation-{index}"
        item.antigravity_title = f"Conversation {index}"
        records.append(item)
    bot = make_bot(tmp_path, FakeStore(records))
    sent = []
    monkeypatch.setattr(
        bot,
        "send",
        lambda text, buttons=None, menu=False: sent.append((text, buttons)) or True,
    )

    bot._session_browser(0)

    text, buttons = sent[-1]
    callback_values = [button["callback_data"] for row in buttons for button in row]
    assert "صفحة 1/2" in text
    assert any(value.startswith("session:") for value in callback_values)
    assert "menu:sessions:1" in callback_values
    assert "menu:home" in callback_values



def test_existing_session_title_wins_over_followup_prompt_title(tmp_path: Path, monkeypatch):
    """A ChatGPT follow-up may have its own prompt title but not rename the session."""
    monkeypatch.setattr(
        "agy_mcp.telegram_bot.conversation_title",
        lambda session_id: "An updated Antigravity-generated title",
    )
    original = record("job_first", "Gumacu MCP integration")
    second = record("job_second", "Run another pytest pass")
    store = FakeStore([second, original])
    bot = make_bot(tmp_path, store)

    first_title = bot._inherit_conversation_title(original)
    assert first_title.antigravity_title == "Gumacu MCP integration"

    resumed = bot._inherit_conversation_title(second)
    assert resumed.antigravity_title == "Gumacu MCP integration"
    assert bot._state["conversation_titles"]["conv-1"] == "Gumacu MCP integration"

    # A new TelegramBot reads the same durable title after process restart.
    restarted = make_bot(tmp_path, store)
    third = record("job_third", "Deploy the server")
    assert restarted._inherit_conversation_title(third).antigravity_title == (
        "Gumacu MCP integration"
    )


def test_new_session_keeps_its_own_independent_title(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agy_mcp.telegram_bot.conversation_title", lambda session_id: None)
    first = record("job_a", "Gumacu MCP integration")
    independent = record("job_b", "Separate Odoo project")
    independent.session_id = "conv-2"
    store = FakeStore([independent, first])
    bot = make_bot(tmp_path, store)

    assert bot._inherit_conversation_title(first).antigravity_title == (
        "Gumacu MCP integration"
    )
    assert bot._inherit_conversation_title(independent).antigravity_title == (
        "Separate Odoo project"
    )
    assert bot._state["conversation_titles"] == {
        "conv-1": "Gumacu MCP integration",
        "conv-2": "Separate Odoo project",
    }


def test_terminal_notification_preserves_original_title(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "agy_mcp.telegram_bot.conversation_title",
        lambda session_id: "A newly suggested task name",
    )
    original = record("job_original", "Gumacu MCP integration")
    original.status = "completed"
    followup = record("job_followup", "Another unrelated instruction")
    followup.status = "completed"
    store = FakeStore([followup, original])
    bot = make_bot(tmp_path, store)

    assert bot._inherit_conversation_title(original).antigravity_title == (
        "Gumacu MCP integration"
    )
    terminal = bot.terminal_message(followup, "")

    assert "Gumacu MCP integration" in terminal
    assert "Another unrelated instruction" not in terminal
    assert "A newly suggested task name" not in terminal
