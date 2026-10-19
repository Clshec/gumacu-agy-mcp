"""Native /btw TUI bridge: no false support for headless AGY jobs."""

from __future__ import annotations

import os
import sys

import pytest

from agy_mcp import interactive_btw as btw


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GUMACU_ENABLE_INTERACTIVE_BTW", raising=False)
    assert btw.interactive_open()["success"] is False
    assert btw.interactive_btw("job_a", "question")["success"] is False


def test_headless_job_id_is_rejected_without_injection(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_ENABLE_INTERACTIVE_BTW", "1")
    monkeypatch.setattr(btw, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(btw, "_tmux", lambda *args: pytest.fail("must not inject into headless jobs"))
    if os.name == "posix":
        r = btw.interactive_btw("job_1792424874_3782bb0863a5", "What now?")
        assert r["success"] is False
        assert "ValueError" in r["error"]


@pytest.mark.skipif(os.name != "posix", reason="interactive tmux runs only on POSIX")
def test_native_same_tui_one_in_flight_and_no_print(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_ENABLE_INTERACTIVE_BTW", "1")
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "local")
    monkeypatch.setenv("AGY_BIN", sys.executable)
    monkeypatch.setattr(btw, "_state_dir", lambda: tmp_path)
    commands = []

    def fake_tmux(*args):
        commands.append(args)
        return "side question card"
    
    monkeypatch.setattr(btw, "_tmux", fake_tmux)
    opened = btw.interactive_open(cwd=str(tmp_path))
    assert opened["success"]
    job_id = opened["job_id"]
    assert job_id.startswith("job_tui_")

    sent = btw.interactive_send(job_id, "Do an example task.")
    assert sent["success"]
    assert not btw.interactive_send(job_id, "Never run twice")["success"]

    side = btw.interactive_btw(job_id, "What is currently happening?")
    assert side["success"]
    assert side["status"] == "native_btw_submitted"
    assert side["answer_verified"] is False
    assert not btw.interactive_btw(job_id, "Another question")["success"]

    snapshot = btw.interactive_snapshot(job_id)
    assert snapshot["success"]
    assert snapshot["screen_snapshot"] == "side question card"
    assert snapshot["answer_verified"] is False
    assert snapshot["btw_pending"]

    snapshot = btw.interactive_snapshot(job_id, clear_btw_pending=True)
    assert snapshot["success"] and not snapshot["btw_pending"]
    assert btw.interactive_btw(job_id, "Next side question")["success"]

    new = [x for x in commands if x[0] == "new-session"]
    assert len(new) == 1
    assert new[0][-1] == sys.executable
    assert all("--print" not in " ".join(x) for x in commands)
    assert all("--print-timeout" not in " ".join(x) for x in commands)
    keys = [x for x in commands if x[0] == "send-keys" and "-l" in x]
    assert [x[-1] for x in keys] == [
        "Do an example task.",
        "/btw What is currently happening?",
        "/btw Next side question",
    ]
    assert len({x[x.index("-t") + 1] for x in keys}) == 1


@pytest.mark.skipif(os.name != "posix", reason="interactive tmux runs only on POSIX")
@pytest.mark.parametrize("bad", ["", "bad\nnew turn", "hello\rthere", "\x1b[2J"])
def test_control_characters_cannot_inject_more_tui_commands(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("GUMACU_ENABLE_INTERACTIVE_BTW", "1")
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "local")
    monkeypatch.setenv("AGY_BIN", sys.executable)
    monkeypatch.setattr(btw, "_state_dir", lambda: tmp_path)
    commands = []
    monkeypatch.setattr(btw, "_tmux", lambda *args: commands.append(args) or "")
    opened = btw.interactive_open(cwd=str(tmp_path))
    assert opened["success"]
    assert btw.interactive_send(opened["job_id"], "Start job")["success"]
    before = len(commands)
    rejected = btw.interactive_btw(opened["job_id"], bad)
    assert not rejected["success"]
    assert len(commands) == before


@pytest.mark.skipif(os.name != "posix", reason="interactive tmux runs only on POSIX")
def test_requires_operator_authorization_for_controlled_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_ENABLE_INTERACTIVE_BTW", "1")
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    monkeypatch.setenv("AGY_BIN", sys.executable)
    monkeypatch.setattr(btw, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(btw, "_tmux", lambda *args: pytest.fail("must not start TUI"))
    r = btw.interactive_open(cwd=str(tmp_path))
    assert not r["success"]
    assert r["error"] == "operator_authorization_required"
