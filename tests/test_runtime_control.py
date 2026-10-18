import json
from types import SimpleNamespace

from test_multi_device_telegram import make_test_bot

from agy_mcp import device_agent, runtime_control


def test_shutdown_requires_explicit_confirmation(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("must not execute")
    monkeypatch.setattr(runtime_control.subprocess, "run", unexpected)
    assert not runtime_control.shutdown_antigravity()["success"]


def test_shutdown_dry_run_never_cancels_jobs(monkeypatch):
    monkeypatch.setattr(runtime_control.platform, "system", lambda: "Windows")
    def run(args, **kwargs):
        assert kwargs["env"]["AGY_SHUTDOWN_DRY_RUN"] == "1"
        assert args[-1] == runtime_control.WINDOWS_CONTROL
        return SimpleNamespace(stdout=json.dumps({"success": True, "dry_run": True, "remaining": []}))
    monkeypatch.setattr(runtime_control.subprocess, "run", run)
    assert runtime_control.shutdown_antigravity(dry_run=True)["cancelled_jobs"] == []


def test_failed_process_inventory_never_claims_closed(monkeypatch):
    monkeypatch.setattr(runtime_control.platform, "system", lambda: "Windows")
    monkeypatch.setattr(runtime_control.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="invalid"))
    assert not runtime_control.shutdown_antigravity(dry_run=True)["success"]


def test_device_shutdown_is_explicit_and_validated(monkeypatch):
    monkeypatch.setattr(runtime_control, "shutdown_antigravity", lambda **kw: kw)
    assert device_agent.dispatch("shutdown", {"dry_run": True}, "call_" + "a"*32) == {"dry_run": True}
    import pytest
    with pytest.raises(ValueError):
        device_agent.dispatch("shutdown", {"confirm": "true"}, "call_" + "a"*32)


def test_shutdown_confirmation_bound_to_original_device_and_single_use(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    monkeypatch.setattr(bot, "_get_device_info", lambda dev: {"os": "Windows", "online": True})
    monkeypatch.setattr(bot, "_run_background", lambda fn: fn())
    calls = []
    monkeypatch.setattr(bot, "_shutdown_device", lambda dev: calls.append(dev))
    bot._shutdown_prompt("windows-main")
    assert not calls
    token = sent[-1]["buttons"][0][0]["callback_data"].split(":")[1]
    bot._state["active_device_id"] = "another-device"
    bot._shutdown_confirm(token)
    bot._shutdown_confirm(token)
    assert calls == ["windows-main"]


def test_cancel_shutdown_confirmation_cannot_be_reused(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    monkeypatch.setattr(bot, "_get_device_info", lambda dev: {"os": "Windows", "online": True})
    monkeypatch.setattr(bot, "_device_panel", lambda dev: None)
    bot._shutdown_prompt("windows-main")
    token = sent[-1]["buttons"][0][0]["callback_data"].split(":")[1]
    bot._handle_callback({"message": {"chat": {"id": bot.chat_id}}, "data": f"stop_no:{token}"})
    assert token not in bot._shutdown_confirmations


def test_last_callback_does_not_block_update_polling(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    queued = []
    monkeypatch.setattr(bot, "_run_background", queued.append)
    monkeypatch.setattr(bot, "_last_reply", lambda ref: (_ for _ in ()).throw(AssertionError("blocking")))
    bot._handle_callback({"message": {"chat": {"id": bot.chat_id}}, "id": "cb1", "data": "last:abc"})
    assert len(queued) == 2  # acknowledgement and result retrieval are off the poll loop
