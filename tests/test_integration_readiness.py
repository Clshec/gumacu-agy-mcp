"""Regression tests for bridge-side integration readiness diagnostics."""

from __future__ import annotations

from agy_mcp import server


def test_readiness_reports_server_state_without_claiming_chat_wakeup(monkeypatch):
    class Subscriptions:
        def list_all(self):
            return [object(), object()]

    class Outbox:
        def list_pending(self, *, limit):
            assert limit == 1
            return {"count": 1}

        def list_telegram_undelivered(self, *, limit):
            assert limit == 1
            return {"count": 0}

    class Dispatcher:
        telegram_configured = True
        bot = object()

    monkeypatch.setattr(server, "_subscription_store", Subscriptions())
    monkeypatch.setattr(server, "_notification_outbox", Outbox())
    monkeypatch.setattr(server, "_notification_dispatcher", Dispatcher())

    snapshot = server._doctor_integration_snapshot()

    assert "agy_doctor" in snapshot["server_registered_tools"]
    assert snapshot["server_registered_tool_count"] >= 1
    assert snapshot["event_subscription_count"] == 2
    assert snapshot["telegram_configured"] is True
    assert snapshot["telegram_bot_initialized"] is True
    assert snapshot["pending_chat_notifications"] is True
    assert snapshot["pending_telegram_deliveries"] is False
    assert snapshot["client_visible_tools"] == "not_observable_from_mcp_server"
    assert snapshot["chat_auto_resume_verified"] is False


def test_readiness_handles_unavailable_outbox_and_subscriptions(monkeypatch):
    class Broken:
        def list_all(self):
            raise RuntimeError("unavailable")

        def list_pending(self, *, limit):
            raise RuntimeError("unavailable")

    monkeypatch.setattr(server, "_subscription_store", Broken())
    monkeypatch.setattr(server, "_notification_outbox", Broken())
    monkeypatch.setattr(server, "_notification_dispatcher", None)

    snapshot = server._doctor_integration_snapshot()

    assert snapshot["event_subscription_count"] is None
    assert snapshot["pending_chat_notifications"] is None
    assert snapshot["pending_telegram_deliveries"] is None
    assert snapshot["telegram_configured"] is False
    assert snapshot["chat_auto_resume_verified"] is False
