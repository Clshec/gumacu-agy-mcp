"""Durable job notifications and optional Telegram delivery.

This module is deliberately independent from MCP Events.  Local ChatGPT and
scheduled tasks can consume the durable outbox through MCP tools, while a
Telegram bot can deliver terminal outcomes immediately.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from agy_mcp.models import JobRecord
from agy_mcp.telegram_bot import TelegramBot, refresh_title
from agy_mcp.utils import ensure_directory, safe_write_text, utc_now_iso

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN_ENV = "AGY_MCP_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "AGY_MCP_TELEGRAM_CHAT_ID"
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "upstream_error",
        "quota_exhausted",
        "timeout",
        "no_final_response",
    }
)
LIFECYCLE_STATUSES = TERMINAL_STATUSES | {"started", "progress"}



class NotificationOutbox:
    """Thread-safe JSON outbox with stable IDs and idempotent acknowledgement."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        ensure_directory(self.path.parent, mode=0o700)
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def notification_id(job_id: str, status: str) -> str:
        digest = hashlib.sha256(f"{job_id}\0{status}".encode()).hexdigest()[:24]
        return f"note_{digest}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        self._records[item["id"]] = item
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Failed to load notification outbox: %s", exc)

    def _save(self) -> None:
        ordered = sorted(self._records.values(), key=lambda item: item["created_at"])
        safe_write_text(self.path, json.dumps(ordered, ensure_ascii=False, indent=2))

    def record(
        self,
        record: JobRecord | dict[str, Any],
        *,
        summary: str,
        reason_code: str | None = None,
        device_id: str | None = None,
        request_id: str | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
        title_source: str | None = None,
        task_label: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(record, dict):
            job_id = str(record.get("job_id") or "")
            status = str(record.get("status") or "")
            session_id = record.get("session_id")
            antigravity_title = record.get("antigravity_title")
            finished_at = record.get("finished_at")
            title_source = title_source or record.get("title_source")
            task_label = task_label or record.get("task_label")
        else:
            job_id = record.job_id
            status = record.status
            session_id = record.session_id
            antigravity_title = record.antigravity_title
            finished_at = record.finished_at
            title_source = title_source or record.title_source

        if status not in LIFECYCLE_STATUSES:
            raise ValueError("notification requires a valid job status")
        note_id = self.notification_id(job_id, status)
        with self._lock:
            existing = self._records.get(note_id)
            if existing is not None:
                return dict(existing)
            now = utc_now_iso()
            item = {
                "id": note_id,
                "job_id": job_id,
                "session_id": session_id,
                "antigravity_title": antigravity_title,
                "title_source": title_source,
                "task_label": task_label,
                "status": status,
                "reason_code": reason_code or status,
                "summary": summary[:60000],
                "created_at": finished_at or now,
                "acknowledged_at": None,
                "device_id": device_id,
                "request_id": request_id,
                "buttons": buttons,
                "telegram": {
                    "configured": False,
                    "delivered": False,
                    "delivered_at": None,
                    "attempts": 0,
                    "last_error": None,
                },
            }
            self._records[note_id] = item
            self._save()
            return dict(item)

    def list_pending(self, *, cursor: int = 0, limit: int = 20) -> dict[str, Any]:
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("cursor must be non-negative and limit must be between 1 and 100")
        with self._lock:
            pending = sorted(
                (item for item in self._records.values() if not item.get("acknowledged_at")),
                key=lambda item: item["created_at"],
            )
            page = pending[cursor : cursor + limit]
            next_cursor = cursor + len(page)
            return {
                "notifications": [dict(item) for item in page],
                "count": len(page),
                "next_cursor": next_cursor if next_cursor < len(pending) else None,
                "has_more": next_cursor < len(pending),
            }

    def snapshot_telegram_undelivered(self) -> list[dict[str, Any]]:
        """Atomically snapshot Telegram deliveries, independent of chat ACKs."""
        with self._lock:
            return [
                dict(item)
                for item in sorted(self._records.values(), key=lambda item: item["created_at"])
                if not item.get("telegram", {}).get("delivered")
            ]

    def list_telegram_undelivered(self, *, cursor: int = 0, limit: int = 20) -> dict[str, Any]:
        """Page Telegram deliveries independently of ChatGPT acknowledgement."""
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("cursor must be non-negative and limit must be between 1 and 100")
        undelivered = self.snapshot_telegram_undelivered()
        page = undelivered[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return {
            "notifications": page,
            "count": len(page),
            "next_cursor": next_cursor if next_cursor < len(undelivered) else None,
            "has_more": next_cursor < len(undelivered),
        }

    def acknowledge(self, notification_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._records.get(notification_id)
            if item is None:
                return None
            if not item.get("acknowledged_at"):
                item["acknowledged_at"] = utc_now_iso()
                self._save()
            return dict(item)

    def update_telegram(
        self,
        notification_id: str,
        *,
        configured: bool,
        delivered: bool,
        attempts: int,
        error: str | None,
    ) -> None:
        with self._lock:
            item = self._records.get(notification_id)
            if item is None:
                return
            item["telegram"] = {
                "configured": configured,
                "delivered": delivered,
                "delivered_at": utc_now_iso() if delivered else None,
                "attempts": attempts,
                "last_error": error[:240] if error else None,
            }
            self._save()


class NotificationDispatcher:
    """Persist every terminal result and optionally deliver it to Telegram."""

    def __init__(
        self,
        outbox: NotificationOutbox,
        *,
        token: str | None = None,
        chat_id: str | None = None,
        timeout_seconds: float = 10.0,
        retry_delays: tuple[float, ...] = (0.0, 1.0, 3.0),
    ) -> None:
        self.outbox = outbox
        self._token = token if token is not None else os.environ.get(TELEGRAM_TOKEN_ENV, "")
        self._chat_id = chat_id if chat_id is not None else os.environ.get(TELEGRAM_CHAT_ID_ENV, "")
        self.timeout_seconds = timeout_seconds
        self.retry_delays = retry_delays
        self.bot: TelegramBot | None = None
        if self.telegram_configured:
            self.replay_pending_telegram()

    @property
    def telegram_configured(self) -> bool:
        return bool(self._token.strip() and self._chat_id.strip())

    def readiness(self) -> dict[str, Any]:
        return {
            "telegram_configured": self.telegram_configured,
            "notification_outbox": str(self.outbox.path.name),
        }

    def bind_runtime(self, *, store: Any, supervisor: Any, safety: Any) -> None:
        """Attach the live supervisor and start the private Telegram command loop."""

        if not self.telegram_configured or self.bot is not None:
            return
        if os.environ.get("AGY_MCP_DISABLE_TELEGRAM_BOT") in {"1", "true", "True"}:
            return
        state_path = self.outbox.path.with_name("telegram_state.json")
        self.bot = TelegramBot(
            token=self._token,
            chat_id=self._chat_id,
            store=store,
            supervisor=supervisor,
            safety=safety,
            state_path=state_path,
        )
        self.bot.start()

    def on_job_event(self, record: JobRecord, event: Any) -> None:
        if self.bot is not None:
            self.bot.on_job_event(record, event)

    def notify_job(
        self,
        record: JobRecord | dict[str, Any],
        *,
        summary: str,
        reason_code: str | None = None,
        device_id: str | None = None,
        request_id: str | None = None,
        buttons: list[list[dict[str, str]]] | None = None,
        title_source: str | None = None,
        task_label: str | None = None,
    ) -> dict[str, Any] | None:
        if isinstance(record, dict):
            status = record.get("status")
            job_id = record.get("job_id")
        else:
            status = record.status
            job_id = record.job_id
        if status not in LIFECYCLE_STATUSES:
            return None
        if self.bot is not None and isinstance(record, JobRecord):
            record = refresh_title(self.bot.store, record)
        item = self.outbox.record(
            record,
            summary=summary,
            reason_code=reason_code,
            device_id=device_id,
            request_id=request_id,
            buttons=buttons,
            title_source=title_source,
            task_label=task_label,
        )
        if item.get("telegram", {}).get("delivered"):
            return item
        if not self.telegram_configured:
            self.outbox.update_telegram(
                item["id"], configured=False, delivered=False, attempts=0, error=None,
            )
            return item
        thread = threading.Thread(
            target=self._deliver_telegram,
            args=(item,),
            name=f"agy-telegram-{str(job_id)[-12:]}",
            daemon=True,
        )
        thread.start()
        return item

    def replay_pending_telegram(self) -> None:
        """Retry undelivered durable notifications after a process restart."""

        # Snapshot atomically before starting workers: a fast delivery must
        # not shrink the filtered collection during cursor-based pagination.
        for item in self.outbox.snapshot_telegram_undelivered():
            thread = threading.Thread(
                target=self._deliver_telegram,
                args=(item,),
                name=f"agy-telegram-replay-{item['id'][-8:]}",
                daemon=True,
            )
            thread.start()

    def _message(self, item: dict[str, Any]) -> str:
        status = str(item["status"])
        icon = (
            "✅" if status == "completed"
            else "⛔" if status == "quota_exhausted"
            else "⏳" if status == "started"
            else "🔄" if status == "progress"
            else "⚠️"
        )
        lines = [f"{icon} <b>Antigravity: {html.escape(status)}</b>"]
        device_id = str(item.get("device_id") or "linux-netcup")
        lines.append(f"Device: <b>{html.escape(device_id)}</b>")
        title = item.get("antigravity_title")
        source = item.get("title_source")
        task_label = item.get("task_label")
        if title and source == "antigravity":
            lines.append(f"Name: <b>{html.escape(str(title))}</b>")
        elif title:
            lines.append(f"Name: <b>{html.escape(str(title))}</b>")
        elif task_label:
            lines.append(f"Name: <b>{html.escape(str(task_label))}</b>")
        else:
            lines.append(f"Name: <b>{html.escape(str(item.get('antigravity_title') or 'pending'))}</b>")
        lines.append(f"Job: <code>{html.escape(str(item['job_id']))}</code>")
        if item.get("session_id"):
            lines.append(f"Session: <code>{html.escape(str(item['session_id']))}</code>")
        if item.get("request_id"):
            lines.append(f"Request: <code>{html.escape(str(item['request_id']))}</code>")
        lines.append(f"Reason: <code>{html.escape(str(item['reason_code']))}</code>")
        if item.get("summary"):
            lines.append(html.escape(str(item["summary"])))
        return "\n".join(lines)

    def _deliver_telegram(self, item: dict[str, Any]) -> None:
        if self.bot is not None:
            record = self.bot.store.get_job(str(item["job_id"]))
            if record is not None:
                if self.bot.is_telegram_job(record):
                    message = self.bot.conversation_reply(
                        record, str(item.get("summary") or ""),
                    )
                else:
                    message = self.bot.terminal_message(
                        record, str(item.get("summary") or ""),
                    )
                delivered = False
                attempts = 0
                error: str | None = "TelegramSendFailed"
                buttons = self.bot._job_buttons(record)
                for delay in self.retry_delays:
                    if delay:
                        time.sleep(delay)
                    attempts += 1
                    try:
                        delivered = bool(self.bot.send(message, buttons=buttons))
                        error = None if delivered else "TelegramSendFailed"
                    except Exception as exc:  # noqa: BLE001
                        error = type(exc).__name__
                    if delivered:
                        break
                self.outbox.update_telegram(
                    item["id"], configured=True, delivered=delivered,
                    attempts=attempts, error=error,
                )
                return
            elif item.get("device_id"):
                message = self._message(item)
                buttons = item.get("buttons")
                delivered = False
                attempts = 0
                error: str | None = "TelegramSendFailed"
                for delay in self.retry_delays:
                    if delay:
                        time.sleep(delay)
                    attempts += 1
                    try:
                        delivered = bool(self.bot.send(message, buttons=buttons))
                        error = None if delivered else "TelegramSendFailed"
                    except Exception as exc:  # noqa: BLE001
                        error = type(exc).__name__
                    if delivered:
                        break
                self.outbox.update_telegram(
                    item["id"], configured=True, delivered=delivered,
                    attempts=attempts, error=error,
                )
                return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        error: str | None = None
        attempts = 0
        for delay in self.retry_delays:
            if delay:
                time.sleep(delay)
            attempts += 1
            try:
                payload: dict[str, Any] = {
                    "chat_id": self._chat_id,
                    "text": self._message(item),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                buttons = item.get("buttons")
                if buttons:
                    payload["reply_markup"] = {"inline_keyboard": buttons}
                response = httpx.post(
                    url,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                res_payload = response.json()
                if not res_payload.get("ok"):
                    raise RuntimeError("Telegram API rejected the notification")
            except Exception as exc:  # noqa: BLE001 - delivery must not affect job finalization
                error = type(exc).__name__
                continue
            self.outbox.update_telegram(
                item["id"], configured=True, delivered=True, attempts=attempts, error=None,
            )
            return
        logger.warning("Telegram delivery failed for notification %s", item["id"])
        self.outbox.update_telegram(
            item["id"], configured=True, delivered=False, attempts=attempts, error=error,
        )


__all__ = [
    "LIFECYCLE_STATUSES",
    "NotificationDispatcher",
    "NotificationOutbox",
    "TELEGRAM_CHAT_ID_ENV",
    "TELEGRAM_TOKEN_ENV",
    "TERMINAL_STATUSES",
]
