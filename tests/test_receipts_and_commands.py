from types import SimpleNamespace

from test_multi_device_telegram import make_test_bot

from agy_mcp import runtime_control
from agy_mcp.models import JobRecord


def test_external_receipt_once_without_claiming_execution(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    bot._receipt_since = "2026-10-18T00:00:00Z"
    record = JobRecord(job_id="job_new", status="running", started_at="2026-10-18T20:00:00Z")
    monkeypatch.setattr(bot, "_records", lambda: [record])
    bot._send_receipts_once()
    bot._send_receipts_once()
    assert len(sent) == 1
    assert "تم استلام المهمة" in sent[0]["text"]
    assert "تأكيد الاستلام" in sent[0]["text"]


def test_receipt_does_not_replay_history_or_duplicate_telegram_start(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    bot._receipt_since = "2026-10-18T19:00:00Z"
    records = [JobRecord(job_id="job_old", started_at="2026-10-18T18:00:00Z"),
               JobRecord(job_id="job_own", started_at="2026-10-18T20:00:00Z", extra={"telegram_origin": True})]
    monkeypatch.setattr(bot, "_records", lambda: records)
    bot._send_receipts_once()
    assert not sent


def test_linux_confirmation_does_not_execute_shutdown(tmp_path, monkeypatch):
    bot, sent = make_test_bot(tmp_path)
    monkeypatch.setattr(bot, "_get_device_info", lambda dev: {"os": "Linux", "online": True})
    bot._shutdown_prompt("linux-netcup")
    assert "linux-netcup" in sent[0]["text"]
    assert sent[0]["buttons"][0][0]["callback_data"].startswith("stop_ok:")


def test_linux_dry_run_never_stops_manager_or_kills_processes(monkeypatch):
    monkeypatch.setattr(runtime_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime_control, "_linux_targets", lambda: {99: ("/usr/local/bin/agy", "123")})
    monkeypatch.setattr(runtime_control, "_linux_identity", lambda pid: ("/usr/local/bin/agy", "123"))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime_control.subprocess, "run", run)
    result = runtime_control.shutdown_antigravity(dry_run=True)
    assert result["success"] and result["manager_active"]
    assert all("stop" not in args for args in calls)


def test_command_menu_contains_every_new_command(tmp_path, monkeypatch):
    bot, _ = make_test_bot(tmp_path)
    captured = []
    monkeypatch.setattr(bot._http, "post", lambda url, **kw: captured.append(kw["json"]))
    bot._configure_commands()
    commands = {c["command"] for c in captured[0]["commands"]}
    assert {"device", "last", "shutdown", "new", "use", "sessions", "status", "jobs"} <= commands
