"""Private Telegram control surface for supervised Antigravity conversations."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from agy_mcp.antigravity_metadata import conversation_title
from agy_mcp.models import BridgeRequest, CanonicalEvent, JobRecord
from agy_mcp.utils import ensure_directory, safe_write_text

logger = logging.getLogger(__name__)
# httpx logs full request URLs at INFO; Telegram embeds the bot credential in
# the URL path, so production must never allow that logger to emit requests.
logging.getLogger("httpx").setLevel(logging.WARNING)
TERMINAL = {"completed", "failed", "cancelled", "upstream_error", "quota_exhausted", "timeout", "no_final_response"}
MEANINGFUL = {"conversation_started", "conversation_resumed", "print_starting", "turn_start", "stream_start", "rewind", "quota_exhausted", "timeout", "success", "cancelled"}


def parse_command(text: str) -> tuple[str | None, str]:
    value = text.strip()
    if not value.startswith("/"):
        return None, value
    head, _, rest = value.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def job_title(record: JobRecord) -> str:
    return record.antigravity_title or "جاري جلب اسم المهمة من Antigravity"


def refresh_title(store: Any, record: JobRecord) -> JobRecord:
    title = conversation_title(record.session_id)
    if title and (record.antigravity_title != title or record.title_source != "antigravity"):
        record.antigravity_title = title
        record.title_source = "antigravity"
        store.update_job(record)
    elif not record.antigravity_title:
        prompt = ""
        if isinstance(record.request, dict):
            prompt = str(record.request.get("prompt") or record.request.get("PROMPT") or "")
        first = next((" ".join(line.split()) for line in prompt.splitlines() if line.strip()), "")
        if first:
            record.antigravity_title = first[:120]
            record.title_source = "antigravity_task"
            store.update_job(record)
    return record


def resolve_job(records: list[JobRecord], reference: str) -> tuple[JobRecord | None, list[JobRecord]]:
    ref = " ".join(reference.split()).casefold()
    if not ref:
        return None, []
    exact_id = [r for r in records if r.job_id.casefold() == ref]
    if exact_id:
        return exact_id[0], exact_id
    prefix = [
        r for r in records
        if r.job_id.casefold().startswith(ref) or r.job_id.casefold().endswith(ref)
    ]
    if len(prefix) == 1:
        return prefix[0], prefix
    exact_name = [r for r in records if (r.antigravity_title or "").casefold() == ref]
    if len(exact_name) == 1:
        return exact_name[0], exact_name
    partial_name = [r for r in records if ref in (r.antigravity_title or "").casefold()]
    if len(partial_name) == 1:
        return partial_name[0], partial_name
    return None, exact_name or prefix or partial_name


def safe_progress(events: list[CanonicalEvent]) -> tuple[str, str | None]:
    for event in reversed(events):
        subtype = event.subtype or event.type
        if event.type == "tool_use":
            return f"يستخدم أداة: {subtype}", event.ts
        if event.type == "tool_result":
            return f"استلم نتيجة الأداة: {subtype}", event.ts
        if subtype in MEANINGFUL:
            labels = {
                "conversation_started": "بدأت جلسة Antigravity",
                "conversation_resumed": "تمت متابعة جلسة Antigravity",
                "print_starting": "تم إرسال الرسالة إلى Antigravity",
                "turn_start": "Antigravity يعمل على الرسالة",
                "stream_start": "بدأ وصول الرد",
                "rewind": "Antigravity يعيد محاولة خطوة سابقة",
                "quota_exhausted": "انتهت حصة Antigravity",
                "timeout": "انتهت مهلة التنفيذ",
                "success": "اكتملت المهمة",
                "cancelled": "أُلغيت المهمة",
            }
            return labels.get(subtype, subtype), event.ts
        if event.type == "error":
            return f"أبلغ Antigravity عن خطأ: {subtype}", event.ts
    return "لا يوجد تحديث تفصيلي بعد", None


class TelegramBot:
    """Single-chat, read-mostly bot plus safe conversation forwarding."""

    def __init__(self, *, token: str, chat_id: str, store: Any, supervisor: Any, safety: Any, state_path: Path, timeout: float = 25.0) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.store = store
        self.supervisor = supervisor
        self.safety = safety
        self.state_path = state_path
        self.timeout = timeout
        self._api = f"https://api.telegram.org/bot{token}"
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._thread: threading.Thread | None = None

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_state(self) -> None:
        ensure_directory(self.state_path.parent, mode=0o700)
        safe_write_text(self.state_path, json.dumps(self._state, ensure_ascii=False, indent=2))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._poll_loop, name="agy-telegram-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send(self, text: str, *, buttons: list[list[dict[str, str]]] | None = None) -> bool:
        try:
            payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            response = httpx.post(
                f"{self._api}/sendMessage",
                json=payload,
                timeout=10.0,
            )
            return response.is_success and bool(response.json().get("ok"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram send failed: %s", type(exc).__name__)
            return False

    def on_job_event(self, record: JobRecord, event: CanonicalEvent) -> None:
        record = refresh_title(self.store, record)
        subtype = event.subtype or event.type
        if subtype not in MEANINGFUL and event.type not in {"tool_use", "error"}:
            return
        progress, _ = safe_progress(self.store.read_events(record.job_id))
        now = time.monotonic()
        key = f"progress:{record.job_id}"
        previous = self._state.get(key, {})
        fingerprint = f"{subtype}:{progress}"
        if previous.get("fingerprint") == fingerprint or now - float(previous.get("sent_at", 0)) < 60:
            return
        self._state[key] = {"fingerprint": fingerprint, "sent_at": now}
        self._save_state()
        self.send(self._format_record(record, progress=progress, heading="🔄 تحديث مهمة Antigravity"), buttons=self._job_buttons(record))

    def terminal_message(self, record: JobRecord, summary: str) -> str:
        record = refresh_title(self.store, record)
        progress, _ = safe_progress(self.store.read_events(record.job_id))
        heading = "✅ اكتملت مهمة Antigravity" if record.status == "completed" else "⛔ انتهت الحصة" if record.status == "quota_exhausted" else "⚠️ انتهت مهمة Antigravity"
        return self._format_record(record, progress=progress, heading=heading, summary=summary)

    def conversation_reply(self, record: JobRecord, reply: str) -> str:
        record = refresh_title(self.store, record)
        safe = self.safety.redact(reply)[:3400]
        return (
            f"🤖 <b>{html.escape(job_title(record))}</b>\n\n"
            f"{html.escape(safe or 'انتهت الجلسة دون رد نصي.') }"
        )

    @staticmethod
    def _job_buttons(record: JobRecord) -> list[list[dict[str, str]]]:
        ref = record.job_id[-12:]
        return [
            [
                {"text": "🔄 تحديث الحالة", "callback_data": f"status:{ref}"},
                {"text": "💬 آخر رد", "callback_data": f"last:{ref}"},
            ],
            [{"text": "➡️ متابعة هذه الجلسة", "callback_data": f"use:{ref}"}],
        ]

    def _format_record(self, record: JobRecord, *, progress: str, heading: str, summary: str | None = None) -> str:
        lines = [
            f"<b>{heading}</b>",
            f"الاسم: <b>{html.escape(job_title(record))}</b>",
            f"الحالة: <code>{html.escape(record.status)}</code>",
            f"آخر تقدم: {html.escape(progress)}",
            f"آخر تحديث: <code>{html.escape(record.last_event_at or record.updated_at)}</code>",
            f"المعرّف: <code>{html.escape(record.job_id[-12:])}</code>",
        ]
        if summary:
            lines.append(f"النتيجة: {html.escape(self.safety.redact(summary)[:700])}")
        return "\n".join(lines)

    def _poll_loop(self) -> None:
        if "offset" not in self._state:
            self._discard_backlog()
        while not self._stop.is_set():
            try:
                offset = int(self._state.get("offset", 0))
                response = httpx.get(f"{self._api}/getUpdates", params={"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                for update in payload.get("result", []):
                    update_id = int(update.get("update_id", -1))
                    self._state["offset"] = max(int(self._state.get("offset", 0)), update_id + 1)
                    self._save_state()
                    self._handle_update(update)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram polling failed: %s", type(exc).__name__)
                self._stop.wait(5)

    def _discard_backlog(self) -> None:
        """Pin the initial offset after the newest update without executing history."""

        try:
            response = httpx.get(
                f"{self._api}/getUpdates",
                params={"offset": -1, "limit": 1, "timeout": 0},
                timeout=10.0,
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
            latest = max((int(item.get("update_id", -1)) for item in updates), default=-1)
            self._state["offset"] = latest + 1
            self._save_state()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram backlog initialization failed: %s", type(exc).__name__)

    def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        source_chat = str((message.get("chat") or {}).get("id", ""))
        if source_chat != self.chat_id:
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        command, arg = parse_command(text)
        if command is None:
            self._continue_or_start(arg)
            return
        handlers = {
            "start": self._help,
            "help": self._help,
            "jobs": self._jobs,
            "status": lambda: self._status(arg),
            "progress": lambda: self._progress(arg),
            "recent": self._recent,
            "new": lambda: self._new(arg),
            "use": lambda: self._use(arg),
        }
        handler = handlers.get(command)
        if handler is None:
            self.send("أمر غير معروف. استخدم /help")
            return
        handler()

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        source_chat = str((message.get("chat") or {}).get("id", ""))
        if source_chat != self.chat_id:
            return
        callback_id = callback.get("id")
        if isinstance(callback_id, str):
            try:
                httpx.post(f"{self._api}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=5.0)
            except Exception:
                pass
        data = callback.get("data")
        if not isinstance(data, str):
            return
        action, _, reference = data.partition(":")
        if action == "status":
            self._send_one(reference)
        elif action == "last":
            self._last_reply(reference)
        elif action == "use":
            self._use(reference)

    def _last_reply(self, reference: str) -> None:
        record, _ = resolve_job(self._records(), reference)
        if record is None:
            self.send("لم أجد المهمة المرتبطة بهذه الرسالة.")
            return
        events = self.store.read_events(record.job_id)
        text = next((event.text for event in reversed(events) if event.type == "assistant" and event.text), None)
        if not text:
            self.send("لم يصل رد نصي من Antigravity لهذه المهمة بعد.", buttons=self._job_buttons(record))
            return
        safe = self.safety.redact(text)[:3400]
        self.send(f"<b>آخر رد من Antigravity</b>\nالاسم: <b>{html.escape(job_title(record))}</b>\n\n{html.escape(safe)}", buttons=self._job_buttons(record))

    def _records(self) -> list[JobRecord]:
        records = self.store.list_jobs(limit=100)
        return [refresh_title(self.store, record) for record in records]

    def _help(self) -> None:
        self.send("<b>أوامر Gumacu Antigravity</b>\n/jobs المهام الجارية\n/status ملخص الجارية\n/status الاسم أو الرقم\n/progress الاسم أو الرقم\n/recent أحدث النتائج\n/new نص الرسالة — جلسة جديدة\n/use الاسم أو الرقم — اختيار جلسة\nأي نص عادي — إرساله للجلسة المختارة")

    def _jobs(self) -> None:
        active = [r for r in self._records() if r.status == "running"]
        if not active:
            self.send("لا توجد مهام Antigravity جارية الآن.")
            return
        self.send("<b>المهام الجارية</b>\n" + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code>" for r in active[:20]))

    def _status(self, reference: str) -> None:
        records = self._records()
        if not reference:
            active = [r for r in records if r.status == "running"]
            if not active:
                self.send("لا توجد مهام جارية الآن.")
                return
            for record in active[:10]:
                progress, _ = safe_progress(self.store.read_events(record.job_id))
                self.send(self._format_record(record, progress=progress, heading="📋 حالة المهمة"), buttons=self._job_buttons(record))
            return
        self._send_one(reference)

    def _progress(self, reference: str) -> None:
        if not reference:
            self.send("استخدم: /progress اسم المهمة أو رقمها")
            return
        self._send_one(reference)

    def _send_one(self, reference: str) -> None:
        record, matches = resolve_job(self._records(), reference)
        if record is None:
            if matches:
                self.send("الاسم يطابق أكثر من مهمة:\n" + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code>" for r in matches[:10]))
            else:
                self.send("لم أجد مهمة بهذا الاسم أو الرقم.")
            return
        progress, _ = safe_progress(self.store.read_events(record.job_id))
        self.send(self._format_record(record, progress=progress, heading="📋 حالة المهمة"), buttons=self._job_buttons(record))

    def _recent(self) -> None:
        recent = [r for r in self._records() if r.status in TERMINAL][:10]
        if not recent:
            self.send("لا توجد مهام منتهية مسجلة.")
            return
        self.send("<b>أحدث المهام</b>\n" + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.status}</code> — <code>{r.job_id[-12:]}</code>" for r in recent))

    def _new(self, prompt: str) -> None:
        if not prompt:
            self.send("استخدم: /new ثم اكتب الرسالة التي تريد إرسالها إلى Antigravity")
            return
        self._start(prompt, None)

    def _use(self, reference: str) -> None:
        record, matches = resolve_job(self._records(), reference)
        if record is None or not record.session_id:
            if matches:
                self.send("الاختيار غير محدد أو الجلسة لم تحصل على رقم Antigravity بعد. استخدم الرقم المختصر.")
            else:
                self.send("لم أجد جلسة قابلة للمتابعة بهذا الاسم أو الرقم.")
            return
        self._state["active_session_id"] = record.session_id
        self._state["active_job_id"] = record.job_id
        self._save_state()
        self.send(f"تم اختيار جلسة: <b>{html.escape(job_title(record))}</b>\nأرسل رسالتك كنص عادي.")

    def _continue_or_start(self, prompt: str) -> None:
        session_id = self._state.get("active_session_id")
        self._start(prompt, session_id if isinstance(session_id, str) else None)

    def _start(self, prompt: str, session_id: str | None) -> None:
        cwd = os.environ.get("AGY_MCP_TELEGRAM_CWD")
        if not cwd:
            allowed = os.environ.get("GUMACU_ALLOWED_WORKSPACES", "")
            cwd = allowed.split(os.pathsep)[0] if allowed else "."
        try:
            request = BridgeRequest(prompt=prompt, cwd=cwd, session_id=session_id, mode="long", timeout=1200, detach=True, allow_write=False, worktree=False, backend="agy", output_protocol="codex")
            response = self.supervisor.start(request)
        except Exception as exc:  # noqa: BLE001
            self.send(f"تعذر بدء الرسالة: {html.escape(self.safety.redact(str(exc))[:300])}")
            return
        if not response.success or not response.job_id:
            self.send(f"تعذر بدء الرسالة: {html.escape(response.error or 'خطأ غير معروف')}")
            return
        self._state["active_job_id"] = response.job_id
        if session_id:
            self._state["active_session_id"] = session_id
        self._save_state()
        record = self.store.get_job(response.job_id)
        if record is not None:
            record.extra["telegram_origin"] = True
            self.store.update_job(record)
        action = "متابعة الجلسة" if session_id else "بدء جلسة جديدة"
        self.send(f"✅ تم {action}.\nالمعرّف: <code>{response.job_id[-12:]}</code>\nسأرسل التقدم والنتيجة هنا.")


__all__ = ["TelegramBot", "job_title", "parse_command", "refresh_title", "resolve_job", "safe_progress"]
