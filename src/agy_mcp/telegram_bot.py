"""Private Telegram control surface for supervised Antigravity conversations."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
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
LOCAL_DEVICE_ID = "linux-netcup"
LOCAL_DEVICE_NAME = "خادم لينكس (Netcup)"
DEFAULT_DEVICE_ID = LOCAL_DEVICE_ID

MENU_ACTIONS = {
    "🎛 لوحة التحكم": "panel",
    "💻 الأجهزة": "devices",
    "📋 حالة الجلسة": "active_status",
    "💬 آخر رد": "active_last",
    "🔄 المهام الجارية": "jobs",
    "🕘 المهام الأخيرة": "recent",
    "➕ جلسة جديدة": "new_help",
    "📚 اختيار جلسة": "sessions",
    "❓ المساعدة": "help",
    "⏹ إغلاق Antigravity": "shutdown",
}


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
    if len(exact_id) == 1:
        return exact_id[0], exact_id
    if len(exact_id) > 1:
        return None, exact_id

    # Suffix match (e.g. 12-char suffix from inline callbacks)
    suffix = [r for r in records if r.job_id.casefold().endswith(ref)]
    if len(suffix) == 1:
        return suffix[0], suffix
    if len(suffix) > 1:
        # Reject ambiguous suffix collisions instead of wrong route!
        return None, suffix

    # Prefix match
    prefix = [r for r in records if r.job_id.casefold().startswith(ref)]
    if len(prefix) == 1:
        return prefix[0], prefix
    if len(prefix) > 1:
        return None, prefix

    # Exact title match
    exact_name = [r for r in records if (r.antigravity_title or "").casefold() == ref]
    if len(exact_name) == 1:
        return exact_name[0], exact_name
    if len(exact_name) > 1:
        return None, exact_name

    # Partial title match
    partial_name = [r for r in records if ref in (r.antigravity_title or "").casefold()]
    if len(partial_name) == 1:
        return partial_name[0], partial_name
    if len(partial_name) > 1:
        return None, partial_name

    return None, []


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
        self._progress_thread: threading.Thread | None = None
        self._shutdown_confirmations: dict[str, tuple[str, float]] = {}
        self._http: Any = httpx  # Unstarted instances remain simple to test.
        self._receipt_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._receipt_thread: threading.Thread | None = None
        try:
            self.progress_interval = max(
                30.0,
                float(os.environ.get("AGY_MCP_TELEGRAM_PROGRESS_INTERVAL", "180")),
            )
        except ValueError:
            self.progress_interval = 180.0

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                value = {}
        except (OSError, ValueError, TypeError):
            value = {}

        # State migration: ensure active_device_id and session_devices are populated
        migrated = False
        if "session_devices" not in value or not isinstance(value["session_devices"], dict):
            value["session_devices"] = {}
            migrated = True

        if "active_device_id" not in value or not value["active_device_id"]:
            active_sess = value.get("active_session_id")
            active_job = value.get("active_job_id")
            inferred_dev = None
            if active_sess and isinstance(active_sess, str):
                inferred_dev = value["session_devices"].get(active_sess)
                if not inferred_dev:
                    try:
                        from agy_mcp.device_gateway import get_session_affinity
                        inferred_dev = get_session_affinity(active_sess)
                    except Exception:
                        pass
            if not inferred_dev and active_job and isinstance(active_job, str):
                if active_job.startswith("job_r_"):
                    inferred_dev = "windows-main"
            value["active_device_id"] = inferred_dev or LOCAL_DEVICE_ID
            migrated = True

        if migrated and self.state_path.parent.exists():
            try:
                ensure_directory(self.state_path.parent, mode=0o700)
                safe_write_text(self.state_path, json.dumps(value, ensure_ascii=False, indent=2))
            except Exception:
                pass
        return value

    def _save_state(self) -> None:
        ensure_directory(self.state_path.parent, mode=0o700)
        safe_write_text(self.state_path, json.dumps(self._state, ensure_ascii=False, indent=2))

    def get_devices(self) -> list[dict[str, Any]]:
        """List local Linux Netcup and all broker-enrolled remote devices."""
        devices: list[dict[str, Any]] = [
            {
                "id": LOCAL_DEVICE_ID,
                "name": LOCAL_DEVICE_NAME,
                "os": "Linux",
                "online": True,
                "last_seen": time.time(),
                "local": True,
                "capabilities": {"platform": "Linux", "role": "host"},
            }
        ]
        try:
            from agy_mcp.device_gateway import load_remote_devices
            remote = load_remote_devices()
            for r in remote:
                r_copy = dict(r)
                r_copy["local"] = False
                dev_id = r_copy.get("id", "remote")
                dev_os = r_copy.get("os", "unknown")
                if dev_id == "windows-main":
                    r_copy["name"] = f"جهاز ويندوز ({dev_id})"
                else:
                    r_copy["name"] = f"جهاز {dev_id} ({dev_os})"
                devices.append(r_copy)
        except Exception as exc:
            logger.error("Failed to load remote devices: %s", exc)
        return devices

    def _get_device_info(self, device_id: str) -> dict[str, Any] | None:
        for d in self.get_devices():
            if d["id"] == device_id:
                return d
        return None

    def _record_device(self, record: JobRecord) -> str:
        dev = (record.extra or {}).get("device_id") if isinstance(record.extra, dict) else None
        if dev:
            return str(dev)
        if record.job_id.startswith("job_r_"):
            return "windows-main"
        return LOCAL_DEVICE_ID

    def _is_remote_record(self, record: JobRecord) -> bool:
        dev = self._record_device(record)
        return bool(dev and dev != LOCAL_DEVICE_ID)

    def get_session_device(self, session_id: str | None) -> str:
        if not session_id:
            return self._state.get("active_device_id") or LOCAL_DEVICE_ID
        session_devices = self._state.get("session_devices", {})
        if isinstance(session_devices, dict) and session_devices.get(session_id):
            return str(session_devices[session_id])
        try:
            from agy_mcp.device_gateway import get_session_affinity
            aff = get_session_affinity(session_id)
            if aff:
                self._state.setdefault("session_devices", {})[session_id] = aff
                self._save_state()
                return aff
        except Exception:
            pass
        for r in self._records():
            if r.session_id == session_id:
                dev = self._record_device(r)
                self._state.setdefault("session_devices", {})[session_id] = dev
                self._save_state()
                return dev
        fallback_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID
        self._state.setdefault("session_devices", {})[session_id] = fallback_dev
        self._save_state()
        return fallback_dev

    def _session_device(self, record: JobRecord) -> str:
        if record.session_id:
            return self.get_session_device(record.session_id)
        return self._record_device(record)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._http = httpx.Client(timeout=10.0)
        self._thread = threading.Thread(target=self._poll_loop, name="agy-telegram-control", daemon=True)
        self._thread.start()
        self._configure_commands()
        self._progress_thread = threading.Thread(
            target=self._progress_loop,
            name="agy-telegram-progress",
            daemon=True,
        )
        self._progress_thread.start()
        self._receipt_thread = threading.Thread(target=self._receipt_loop, name="agy-telegram-receipts", daemon=True)
        self._receipt_thread.start()

    def _receipt_loop(self) -> None:
        # Observe the durable job stores, never start another getUpdates poller.
        while not self._stop.wait(1):
            try:
                self._send_receipts_once()
            except Exception as exc:
                logger.warning("Telegram receipt observation failed: %s", type(exc).__name__)

    def _send_receipts_once(self) -> None:
        records = self._records()
        for record in records:
            if not record.started_at or record.started_at < self._receipt_since:
                continue
            key = f"receipt:{record.job_id}"
            if self._state.get(key) or self.is_telegram_job(record):
                continue
            if record.job_id in self._state.get("telegram_remote_jobs", {}):
                continue
            device_id = self._record_device(record)
            if self.send(
                f"📥 تم استلام المهمة على <code>{html.escape(device_id)}</code>\n"
                f"المهمة: <b>{html.escape(job_title(record))}</b>\n"
                f"المعرّف: <code>{record.job_id[-12:]}</code>\n"
                "هذا تأكيد الاستلام؛ ستصلك حالة التنفيذ والنتيجة بشكل منفصل.",
                buttons=self._job_buttons(record),
            ):
                with self._lock:
                    self._state[key] = True
                    self._save_state()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _menu_keyboard() -> list[list[dict[str, str]]]:
        return [
            [{"text": "🎛 لوحة التحكم"}],
            [{"text": "💻 الأجهزة"}, {"text": "📋 حالة الجلسة"}],
            [{"text": "💬 آخر رد"}, {"text": "🔄 المهام الجارية"}],
            [{"text": "🕘 المهام الأخيرة"}, {"text": "➕ جلسة جديدة"}],
            [{"text": "📚 اختيار جلسة"}, {"text": "❓ المساعدة"}],
            [{"text": "⏹ إغلاق Antigravity"}],
        ]

    def _configure_commands(self) -> None:
        commands = [
            {"command": "menu", "description": "إظهار لوحة التحكم"},
            {"command": "devices", "description": "قائمة أجهزة Antigravity"},
            {"command": "device", "description": "اختيار جهاز: windows-main أو linux-netcup"},
            {"command": "last", "description": "آخر رد للجلسة المختارة أو رقم المهمة"},
            {"command": "shutdown", "description": "إغلاق Antigravity على الجهاز المختار بعد تأكيد"},
            {"command": "new", "description": "بدء محادثة جديدة"},
            {"command": "status", "description": "حالة الجلسة أو المهمة"},
            {"command": "progress", "description": "آخر تقدم"},
            {"command": "jobs", "description": "المهام الجارية"},
            {"command": "recent", "description": "المهام الأخيرة"},
            {"command": "use", "description": "اختيار جلسة"},
            {"command": "sessions", "description": "تصفح المحادثات"},
            {"command": "help", "description": "شرح الاستخدام"},
        ]
        try:
            self._http.post(
                f"{self._api}/setMyCommands",
                json={"commands": commands},
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram command menu setup failed: %s", type(exc).__name__)

    def send(
        self,
        text: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
        menu: bool = False,
    ) -> bool:
        try:
            chunks = self._telegram_chunks(text)
            delivered = True
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }
                if len(chunks) == 1:
                    payload["parse_mode"] = "HTML"
                if index == len(chunks) - 1:
                    if buttons:
                        payload["reply_markup"] = {"inline_keyboard": buttons}
                    elif menu:
                        payload["reply_markup"] = {"remove_keyboard": True}
                response = self._http.post(
                    f"{self._api}/sendMessage",
                    json=payload,
                    timeout=10.0,
                )
                delivered = delivered and response.is_success and bool(response.json().get("ok"))
            return delivered
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram send failed: %s", type(exc).__name__)
            return False

    @staticmethod
    def _telegram_chunks(text: str, limit: int = 3800) -> list[str]:
        if len(text) <= limit:
            return [text]
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        chunks: list[str] = []
        remaining = plain
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = max(remaining.rfind("\n", 0, limit), remaining.rfind(" ", 0, limit))
            if cut < limit // 2:
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        total = len(chunks)
        return [f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]

    def on_job_event(self, record: JobRecord, event: CanonicalEvent) -> None:
        record = self._inherit_conversation_title(record)
        subtype = event.subtype or event.type
        if subtype not in MEANINGFUL and event.type not in {"tool_use", "error"}:
            return
        progress, _ = safe_progress(self.store.read_events(record.job_id))
        now = time.time()
        key = f"progress:{record.job_id}"
        previous = self._state.get(key, {})
        fingerprint = f"{subtype}:{progress}"
        if previous.get("fingerprint") == fingerprint or now - float(previous.get("sent_at", 0)) < 60:
            return
        self._state[key] = {"fingerprint": fingerprint, "sent_at": now}
        self._save_state()
        self.send(self._format_record(record, progress=progress, heading="🔄 تحديث مهمة Antigravity"), buttons=self._job_buttons(record))

    def _inherit_conversation_title(self, record: JobRecord) -> JobRecord:
        """Pin the first session title, including ChatGPT-initiated follow-ups."""

        if self._is_remote_record(record):
            return record

        if not record.session_id:
            return refresh_title(self.store, record)

        titles = self._state.setdefault("conversation_titles", {})
        if not isinstance(titles, dict):
            titles = {}
            self._state["conversation_titles"] = titles

        title = titles.get(record.session_id)
        if not isinstance(title, str) or not title.strip():
            # If the state file lacks this title (e.g. a restart), use the
            # oldest available turn, never the latest follow-up prompt.
            # SessionStore lists by mutable directory mtime; sort by the
            # immutable start time to pick the first turn even if an older
            # job was updated after a newer one.
            previous_jobs = list(reversed(self.store.list_jobs(limit=100)))
            previous_jobs.sort(key=lambda previous: previous.started_at)
            title = next(
                (
                    previous.antigravity_title
                    for previous in previous_jobs
                    if previous.session_id == record.session_id
                    and previous.antigravity_title
                ),
                None,
            )
            if not title:
                record = refresh_title(self.store, record)
                title = record.antigravity_title
            if isinstance(title, str) and title.strip():
                title = title.strip()[:120]
                titles[record.session_id] = title
                self._save_state()

        if isinstance(title, str) and title.strip() and record.antigravity_title != title:
            record.antigravity_title = title
            record.title_source = "antigravity_task"
            self.store.update_job(record)
        return record

    def _remember_conversation_title(self, record: JobRecord) -> None:
        """Later turns must not replace the original session title."""

        if not record.session_id or not record.antigravity_title:
            return
        titles = self._state.setdefault("conversation_titles", {})
        if isinstance(titles, dict) and not titles.get(record.session_id):
            titles[record.session_id] = record.antigravity_title
            self._save_state()

    def is_telegram_job(self, record: JobRecord) -> bool:
        jobs = self._state.get("telegram_jobs", {})
        return bool(
            record.extra.get("telegram_origin")
            or (isinstance(jobs, dict) and jobs.get(record.job_id))
        )

    def _progress_loop(self) -> None:
        while not self._stop.wait(min(30.0, self.progress_interval)):
            try:
                self._send_periodic_updates_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Telegram periodic progress failed: %s", type(exc).__name__)

    def _send_periodic_updates_once(self, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        for record in self.store.list_jobs(limit=100):
            if record.status != "running" or not self.is_telegram_job(record):
                continue
            record = self._inherit_conversation_title(record)
            self._remember_conversation_title(record)
            key = f"progress:{record.job_id}"
            previous = self._state.get(key, {})
            if current - float(previous.get("sent_at", 0)) < self.progress_interval:
                continue
            progress, _ = safe_progress(self.store.read_events(record.job_id))
            self._state[key] = {
                "fingerprint": f"periodic:{progress}",
                "sent_at": current,
            }
            self._save_state()
            self.send(
                self._format_record(
                    record,
                    progress=progress,
                    heading="⏳ ما زالت جلسة Antigravity تعمل",
                ),
                buttons=self._job_buttons(record),
            )

    def terminal_message(self, record: JobRecord, summary: str) -> str:
        record = self._inherit_conversation_title(record)
        self._remember_conversation_title(record)
        if self._is_remote_record(record):
            progress = record.extra.get("summary") or record.status
        else:
            progress, _ = safe_progress(self.store.read_events(record.job_id))
        heading = "✅ اكتملت مهمة Antigravity" if record.status == "completed" else "⛔ انتهت الحصة" if record.status == "quota_exhausted" else "⚠️ انتهت مهمة Antigravity"
        return self._format_record(record, progress=progress, heading=heading, summary=summary)

    def conversation_reply(self, record: JobRecord, reply: str) -> str:
        record = self._inherit_conversation_title(record)
        self._remember_conversation_title(record)
        safe = self.safety.redact(reply)
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
        device_id = self._record_device(record)
        lines = [
            f"<b>{heading}</b>",
            f"الجهاز: <code>{html.escape(str(device_id))}</code>",
            f"الاسم: <b>{html.escape(job_title(record))}</b>",
            f"الجلسة: <code>{html.escape(str(record.session_id or 'none'))}</code>",
            f"الحالة: <code>{html.escape(record.status)}</code>",
            f"آخر تقدم: {html.escape(progress)}",
            f"آخر تحديث: <code>{html.escape(record.last_event_at or record.updated_at or '')}</code>",
            f"المعرّف: <code>{html.escape(record.job_id)}</code>",
        ]
        res_summary = summary or (record.extra.get("summary") if (record.extra and record.status in TERMINAL) else None)
        if res_summary:
            lines.append(f"النتيجة: {html.escape(self.safety.redact(res_summary))}")
        return "\n".join(lines)

    def _poll_loop(self) -> None:
        if "offset" not in self._state:
            self._discard_backlog()
        while not self._stop.is_set():
            try:
                offset = int(self._state.get("offset", 0))
                response = self._http.get(f"{self._api}/getUpdates", params={"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=self.timeout)
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
            response = self._http.get(
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
        menu_action = MENU_ACTIONS.get(text.strip())
        if menu_action:
            self._handle_menu_action(menu_action)
            return
        command, arg = parse_command(text)
        if command is None:
            self._continue_or_start(arg)
            return
        handlers = {
            "start": self._help,
            "help": self._help,
            "menu": self._control_panel,
            "devices": self._devices_view,
            "device": lambda: self._device_select(arg.strip()) if arg.strip() else self._devices_view(),
            "last": lambda: self._run_background(lambda: self._last_reply(arg.strip() or self._last_reference())),
            "shutdown": lambda: self._shutdown_prompt(str(self._state.get("active_device_id") or LOCAL_DEVICE_ID)),
            "jobs": self._jobs,
            "status": lambda: self._status(arg),
            "progress": lambda: self._progress(arg),
            "recent": self._recent,
            "new": lambda: self._new(arg),
            "use": lambda: self._use(arg),
            "sessions": self._sessions,
            "chatgpt": lambda: self._chatgpt_command(arg),
        }
        handler = handlers.get(command)
        if handler is None:
            self.send("أمر غير معروف. استخدم /help")
            return
        handler()

    def _last_reference(self) -> str:
        reference = str(self._state.get("active_job_id") or "")
        if reference:
            return reference
        device = str(self._state.get("active_device_id") or LOCAL_DEVICE_ID)
        session = self._state.get("active_session_id")
        return next((r.job_id for r in self._records()
                     if self._record_device(r) == device and (not session or r.session_id == session)), "")

    def _handle_menu_action(self, action: str) -> None:
        if action == "panel":
            self._control_panel()
        elif action == "devices":
            self._devices_view()
        elif action == "active_status":
            self._send_one(str(self._state.get("active_job_id") or ""))
        elif action == "active_last":
            reference = str(self._state.get("active_job_id") or "")
            self._run_background(lambda: self._last_reply(reference))
        elif action == "jobs":
            self._jobs()
        elif action == "recent":
            self._recent()
        elif action == "new_help":
            active_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID
            self.send(f"الجهاز النشط: <code>{html.escape(active_dev)}</code>\nلبدء جلسة جديدة اكتب:\n<code>/new رسالتك</code>", menu=True)
        elif action == "sessions":
            self._sessions()
        elif action == "shutdown":
            self._shutdown_prompt(str(self._state.get("active_device_id") or LOCAL_DEVICE_ID))
        else:
            self._help()

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        message = callback.get("message") or {}
        source_chat = str((message.get("chat") or {}).get("id", ""))
        if source_chat != self.chat_id:
            return
        callback_id = callback.get("id")
        if isinstance(callback_id, str):
            try:
                # Acknowledgement must not delay routing or remote result reads.
                self._run_background(lambda: self._http.post(
                    f"{self._api}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=5.0))
            except Exception:
                pass
        data = callback.get("data")
        if not isinstance(data, str):
            return
        action, _, reference = data.partition(":")
        if action == "status":
            self._send_one(reference)
        elif action == "last":
            self._run_background(lambda: self._last_reply(reference))
        elif action == "stop_ask":
            self._shutdown_prompt(reference)
        elif action == "stop_ok":
            self._shutdown_confirm(reference)
        elif action == "stop_no":
            with self._lock:
                pending = self._shutdown_confirmations.pop(reference, None)
            if pending:
                self._device_panel(pending[0])
        elif action == "use":
            self._use(reference)
        elif action == "menu":
            self._handle_panel_callback(reference)
        elif action == "session":
            self._session_panel(reference)
        elif action == "dev":
            self._device_panel(reference)
        elif action == "dev_new":
            self._device_new_session(reference)
        elif action == "dev_sess":
            dev_id, _, page_text = reference.partition(":")
            try:
                page = max(0, int(page_text or "0"))
            except ValueError:
                page = 0
            self._device_sessions(dev_id, page)
        elif action == "dev_jobs":
            self._device_jobs(reference)
        elif action == "dev_recent":
            self._device_recent(reference)
        elif action == "dev_use":
            self._device_select(reference)

    @staticmethod
    def _run_background(fn: Any) -> None:
        def work() -> None:
            try:
                fn()
            except Exception as exc:
                logger.warning("Telegram background action failed: %s", type(exc).__name__)
        threading.Thread(target=work, name="agy-telegram-action", daemon=True).start()

    def _shutdown_prompt(self, device_id: str) -> None:
        dev = self._get_device_info(device_id)
        if not dev or dev.get("os") not in {"Windows", "Linux"} or not dev.get("online"):
            self.send("الإغلاق الكامل متاح لجهاز Windows أو Linux المتصل فقط.")
            return
        token = secrets.token_hex(12)
        with self._lock:
            now = time.monotonic()
            self._shutdown_confirmations = {k: v for k, v in self._shutdown_confirmations.items() if v[1] > now}
            self._shutdown_confirmations[token] = (device_id, now + 120)
        self.send(
            f"⚠️ إغلاق Antigravity بالكامل على <code>{html.escape(device_id)}</code>؟\n"
            "سيوقف جميع مهامه الجارية ويغلق التطبيق وعملياته الخلفية على هذا الجهاز فقط. "
            "ستبقى المحادثات محفوظة ووكيل الاتصال يعمل. قد تفقد تعديلات غير محفوظة في التطبيق.",
            buttons=[[{"text": "⏹ تأكيد الإغلاق الكامل", "callback_data": f"stop_ok:{token}"}],
                     [{"text": "رجوع دون إغلاق", "callback_data": f"stop_no:{token}"}]],
        )

    def _shutdown_confirm(self, token: str) -> None:
        with self._lock:
            pending = self._shutdown_confirmations.pop(token, None)
        if pending is None or pending[1] <= time.monotonic():
            self.send("انتهت صلاحية التأكيد أو استُخدم بالفعل؛ افتح زر الإغلاق مرة أخرى.")
            return
        device_id = pending[0]  # Never use the possibly changed active device.
        self.send(f"⏳ جارٍ إغلاق Antigravity على <code>{html.escape(device_id)}</code>…")
        self._run_background(lambda: self._shutdown_device(device_id))

    def _shutdown_device(self, device_id: str) -> None:
        from agy_mcp.device_tools import await_call, submit
        if device_id == LOCAL_DEVICE_ID:
            from agy_mcp.runtime_control import shutdown_antigravity
            result = shutdown_antigravity(confirm=True)
        else:
            result = submit(device_id, "shutdown", {"confirm": True})
        if result.get("pending"):
            result = await_call(result["request_id"], seconds=30)
        if result.get("success") and not result.get("pending") and not result.get("remaining"):
            self.send(f"✅ أُغلق Antigravity بالكامل على <code>{html.escape(device_id)}</code>. وكيل الاتصال ما زال يعمل.")
        else:
            self.send(f"⚠️ لم أتمكن من تأكيد الإغلاق الكامل على <code>{html.escape(device_id)}</code>. راجع حالة الجهاز؛ لن أعرض نجاحاً غير مؤكد.")

    def _handle_panel_callback(self, reference: str) -> None:
        section, _, page_text = reference.partition(":")
        if section == "home":
            self._control_panel()
        elif section == "devices":
            self._devices_view()
        elif section == "sessions":
            try:
                page = max(0, int(page_text or "0"))
            except ValueError:
                page = 0
            self._session_browser(page)
        elif section == "jobs":
            self._jobs()
        elif section == "recent":
            self._recent()

    def _control_panel(self) -> None:
        active_job = str(self._state.get("active_job_id") or "")
        active_sess = str(self._state.get("active_session_id") or "")
        active_dev = str(self._state.get("active_device_id") or LOCAL_DEVICE_ID)
        dev_info = self._get_device_info(active_dev)
        status_icon = "🟢" if (dev_info and dev_info.get("online")) else "🔴"

        buttons: list[list[dict[str, str]]] = []
        if active_job:
            buttons.append([
                {"text": "📋 حالة الحالية", "callback_data": f"status:{active_job[-12:]}"},
                {"text": "💬 آخر رد", "callback_data": f"last:{active_job[-12:]}"},
            ])
        buttons.extend([
            [{"text": "💻 إدارة الأجهزة", "callback_data": "menu:devices"}],
            [{"text": "📚 كل المحادثات", "callback_data": "menu:sessions:0"}],
            [
                {"text": "🔄 المهام الجارية", "callback_data": "menu:jobs"},
                {"text": "🕘 المهام الأخيرة", "callback_data": "menu:recent"},
            ],
        ])
        sess_line = f"<code>{html.escape(active_sess)}</code>" if active_sess else "<i>لا توجد جلسة نشطة</i>"
        dev_line = f"<code>{html.escape(active_dev)}</code> [{status_icon}]"
        self.send(
            f"<b>🎛 لوحة تحكم Antigravity</b>\n\n"
            f"الجهاز النشط: {dev_line}\n"
            f"الجلسة النشطة: {sess_line}\n\n"
            "اختر القسم الذي تريد فتحه، أو اكتب رسالة مباشرة للجلسة الحالية.",
            buttons=buttons,
        )

    def _devices_view(self) -> None:
        devices = self.get_devices()
        active_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID
        lines = [
            "<b>💻 أجهزة Antigravity المسجلة</b>\n",
            f"الجهاز النشط حالياً: <code>{html.escape(active_dev)}</code>\n",
        ]
        buttons: list[list[dict[str, str]]] = []
        for d in devices:
            d_id = d["id"]
            d_name = d.get("name", d_id)
            d_os = d.get("os", "unknown")
            online = d.get("online", False)
            icon = "🟢" if online else "🔴"
            status_text = "متصل" if online else "غير متصل"
            last_seen = d.get("last_seen")
            if d.get("local"):
                seen_str = "نشط الآن"
            elif last_seen:
                ago = max(0, int(time.time() - last_seen))
                seen_str = f"منذ {ago} ثانية" if ago < 120 else f"منذ {ago // 60} دقيقة"
            else:
                seen_str = "لم يُشاهد بعد"
            is_active = " (النشط حالياً)" if d_id == active_dev else ""
            lines.append(
                f"• {icon} <b>{html.escape(d_name)}</b>{is_active}\n"
                f"  المعرّف: <code>{html.escape(d_id)}</code> | النظام: <code>{html.escape(d_os)}</code>\n"
                f"  الحالة: {status_text} | آخر ظهور: {seen_str}\n"
            )
            buttons.append([
                {"text": f"{icon} {d_name[:24]}", "callback_data": f"dev:{d_id}"}
            ])
        buttons.append([{"text": "🏠 القائمة الرئيسية", "callback_data": "menu:home"}])
        self.send("\n".join(lines), buttons=buttons)

    def _device_panel(self, device_id: str) -> None:
        dev = self._get_device_info(device_id)
        if dev is None:
            self.send(f"لم أجد جهازاً بالمعرّف <code>{html.escape(device_id)}</code>.")
            return
        active_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID
        is_active = (device_id == active_dev)
        online = dev.get("online", False)
        status_icon = "🟢" if online else "🔴"
        status_text = "متصل" if online else "غير متصل"
        last_seen = dev.get("last_seen")
        if dev.get("local"):
            seen_str = "نشط الآن"
        elif last_seen:
            ago = max(0, int(time.time() - last_seen))
            seen_str = f"منذ {ago} ثانية" if ago < 120 else f"منذ {ago // 60} دقيقة"
        else:
            seen_str = "لم يُشاهد بعد"

        sess_id = self._state.get("active_session_id") if is_active else None
        active_title = self._state.get("active_conversation_title") if is_active else None
        sess_text = f"<code>{html.escape(str(sess_id))}</code>" if sess_id else "<i>لا توجد جلسة محددة</i>"
        if active_title and sess_id:
            sess_text += f" ({html.escape(active_title)})"

        text = (
            f"<b>📱 لوحة تحكم الجهاز</b>\n\n"
            f"الاسم: <b>{html.escape(dev.get('name', device_id))}</b>\n"
            f"المعرّف: <code>{html.escape(device_id)}</code>\n"
            f"النظام: <code>{html.escape(dev.get('os', 'unknown'))}</code>\n"
            f"الحالة: {status_icon} <b>{status_text}</b>\n"
            f"آخر ظهور: {seen_str}\n"
            f"الجهاز النشط: {'نعم ✅' if is_active else 'لا ⚪'}\n"
            f"الجلسة النشطة: {sess_text}\n"
        )
        buttons: list[list[dict[str, str]]] = [
            [{"text": "➕ جلسة جديدة على هذا الجهاز", "callback_data": f"dev_new:{device_id}"}],
            [{"text": "📚 محادثات هذا الجهاز", "callback_data": f"dev_sess:{device_id}:0"}],
            [
                {"text": "🔄 المهام الجارية", "callback_data": f"dev_jobs:{device_id}"},
                {"text": "🕘 المهام الأخيرة", "callback_data": f"dev_recent:{device_id}"},
            ],
        ]
        if not is_active:
            buttons.append([{"text": "✅ تعيين كجهاز نشط", "callback_data": f"dev_use:{device_id}"}])
        if dev.get("os") in {"Windows", "Linux"}:
            buttons.append([{"text": "⏹ إغلاق Antigravity بالكامل", "callback_data": f"stop_ask:{device_id}"}])
        buttons.append([
            {"text": "⬅️ قائمة الأجهزة", "callback_data": "menu:devices"},
            {"text": "🏠 الرئيسية", "callback_data": "menu:home"},
        ])
        self.send(text, buttons=buttons)

    def _device_new_session(self, device_id: str) -> None:
        dev = self._get_device_info(device_id)
        if not dev:
            self.send(f"لم أجد جهازاً بالمعرّف <code>{html.escape(device_id)}</code>.")
            return
        if not dev.get("online"):
            self.send(
                f"⚠️ الجهاز <code>{html.escape(device_id)}</code> غير متصل حالياً (offline).\n"
                "لا يمكن بدء جلسة جديدة على هذا الجهاز."
            )
            return
        self._state["active_device_id"] = device_id
        self._state.pop("active_session_id", None)
        self._state.pop("active_job_id", None)
        self._state.pop("active_conversation_title", None)
        self._save_state()
        self.send(
            f"✅ تم اختيار الجهاز: <code>{html.escape(device_id)}</code> لبدء جلسة جديدة.\n"
            "أرسل رسالتك كنص عادي أو استخدم:\n"
            "<code>/new نص الرسالة</code>",
            menu=True,
        )

    def _device_sessions(self, device_id: str, page: int = 0) -> None:
        sessions = [s for s in self._unique_sessions() if self._session_device(s) == device_id]
        if not sessions:
            buttons = [
                [{"text": "➕ جلسة جديدة على هذا الجهاز", "callback_data": f"dev_new:{device_id}"}],
                [{"text": "⬅️ لوحة الجهاز", "callback_data": f"dev:{device_id}"}],
            ]
            self.send(
                f"لا توجد محادثات سابقة مسجلة على الجهاز <code>{html.escape(device_id)}</code>.",
                buttons=buttons,
            )
            return
        page_size = 6
        last_page = max(0, (len(sessions) - 1) // page_size)
        page = min(page, last_page)
        visible = sessions[page * page_size : (page + 1) * page_size]
        buttons = [
            [{"text": f"💬 {job_title(record)[:36]}", "callback_data": f"session:{record.job_id[-12:]}"}]
            for record in visible
        ]
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append({"text": "⬅️ السابق", "callback_data": f"dev_sess:{device_id}:{page - 1}"})
        if page < last_page:
            navigation.append({"text": "التالي ➡️", "callback_data": f"dev_sess:{device_id}:{page + 1}"})
        if navigation:
            buttons.append(navigation)
        buttons.append([
            {"text": "⬅️ لوحة الجهاز", "callback_data": f"dev:{device_id}"},
            {"text": "🏠 الرئيسية", "callback_data": "menu:home"},
        ])
        self.send(
            f"<b>محادثات الجهاز <code>{html.escape(device_id)}</code></b> — صفحة {page + 1}/{last_page + 1}\n"
            "اختر محادثة لفتح أدواتها:",
            buttons=buttons,
        )

    def _device_jobs(self, device_id: str) -> None:
        active = [r for r in self._records() if self._record_device(r) == device_id and r.status == "running"]
        buttons: list[list[dict[str, str]]] = [
            [{"text": f"🔄 {job_title(r)[:30]}", "callback_data": f"status:{r.job_id[-12:]}"}]
            for r in active[:10]
        ]
        buttons.append([{"text": "⬅️ لوحة الجهاز", "callback_data": f"dev:{device_id}"}])
        if not active:
            self.send(f"لا توجد مهام جارية على الجهاز <code>{html.escape(device_id)}</code> الآن.", buttons=buttons)
            return
        self.send(
            f"<b>المهام الجارية على <code>{html.escape(device_id)}</code></b>\n"
            + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code>" for r in active[:20]),
            buttons=buttons,
        )

    def _device_recent(self, device_id: str) -> None:
        recent = [r for r in self._records() if self._record_device(r) == device_id and r.status in TERMINAL][:10]
        buttons: list[list[dict[str, str]]] = [
            [{"text": f"📋 {job_title(r)[:30]}", "callback_data": f"status:{r.job_id[-12:]}"}]
            for r in recent[:10]
        ]
        buttons.append([{"text": "⬅️ لوحة الجهاز", "callback_data": f"dev:{device_id}"}])
        if not recent:
            self.send(f"لا توجد مهام منتهية مسجلة على الجهاز <code>{html.escape(device_id)}</code>.", buttons=buttons)
            return
        self.send(
            f"<b>أحدث المهام على <code>{html.escape(device_id)}</code></b>\n"
            + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.status}</code> — <code>{r.job_id[-12:]}</code>" for r in recent),
            buttons=buttons,
        )

    def _device_select(self, device_id: str) -> None:
        dev = self._get_device_info(device_id)
        if not dev:
            self.send(f"لم أجد جهازاً بالمعرّف <code>{html.escape(device_id)}</code>.")
            return
        self._state["active_device_id"] = device_id
        # Session cannot resume on other device: clear active session upon device switch
        self._state.pop("active_session_id", None)
        self._state.pop("active_job_id", None)
        self._state.pop("active_conversation_title", None)
        self._save_state()
        self.send(f"✅ تم تعيين الجهاز <code>{html.escape(device_id)}</code> كجهاز نشط.")

    def _unique_sessions(self) -> list[JobRecord]:
        seen: set[str] = set()
        sessions: list[JobRecord] = []
        for record in self._records():
            if record.session_id and record.session_id not in seen:
                seen.add(record.session_id)
                sessions.append(record)
        return sessions

    def _session_browser(self, page: int = 0) -> None:
        sessions = self._unique_sessions()
        if not sessions:
            self.send("لا توجد جلسات Antigravity مسجلة بعد.", menu=True)
            return
        page_size = 6
        last_page = max(0, (len(sessions) - 1) // page_size)
        page = min(page, last_page)
        visible = sessions[page * page_size : (page + 1) * page_size]
        buttons = [
            [{"text": f"💬 {job_title(record)[:36]}", "callback_data": f"session:{record.job_id[-12:]}"}]
            for record in visible
        ]
        navigation: list[dict[str, str]] = []
        if page > 0:
            navigation.append({"text": "⬅️ السابق", "callback_data": f"menu:sessions:{page - 1}"})
        if page < last_page:
            navigation.append({"text": "التالي ➡️", "callback_data": f"menu:sessions:{page + 1}"})
        if navigation:
            buttons.append(navigation)
        buttons.append([{"text": "🏠 القائمة الرئيسية", "callback_data": "menu:home"}])
        self.send(
            f"<b>محادثات Antigravity</b> — صفحة {page + 1}/{last_page + 1}\n"
            "اختر محادثة لفتح أدواتها:",
            buttons=buttons,
        )

    def _session_panel(self, reference: str) -> None:
        record, matches = resolve_job(self._records(), reference)
        if record is None:
            if matches:
                self.send(
                    "⚠️ تعارض في المعرّف؛ يطابق أكثر من مهمة:\n"
                    + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in matches[:10])
                )
            else:
                self.send("لم أجد هذه المحادثة.")
            return
        origin_dev = self._session_device(record)
        if self._is_remote_record(record):
            progress = record.extra.get("summary") or f"حالة الجهاز: {record.status}"
        else:
            progress, _ = safe_progress(self.store.read_events(record.job_id))
        buttons = [
            [
                {"text": "📋 تحديث الحالة", "callback_data": f"status:{record.job_id[-12:]}"},
                {"text": "💬 آخر رد", "callback_data": f"last:{record.job_id[-12:]}"},
            ],
            [{"text": "➡️ اختيارها للمتابعة", "callback_data": f"use:{record.job_id[-12:]}"}],
            [
                {"text": f"⬅️ محادثات {origin_dev[:16]}", "callback_data": f"dev_sess:{origin_dev}:0"},
                {"text": "📚 كل المحادثات", "callback_data": "menu:sessions:0"},
            ],
        ]
        self.send(
            self._format_record(record, progress=progress, heading="💬 أدوات المحادثة"),
            buttons=buttons,
        )

    def _last_reply(self, reference: str) -> None:
        record, matches = resolve_job(self._records(), reference)
        if record is None:
            if matches:
                self.send(
                    "⚠️ تعارض في المعرّف؛ يطابق أكثر من مهمة:\n"
                    + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in matches[:10])
                )
            else:
                self.send("لم أجد المهمة المرتبطة بهذه الرسالة.")
            return
        if self._is_remote_record(record):
            # Lifecycle summaries describe progress/completion, not the answer.
            # Fetch the result from the immutable owner, even after device switch.
            from agy_mcp.device_tools import submit

            result = submit(self._record_device(record), "result", {"job_id": record.job_id})
            if result.get("pending"):
                self.send("جارٍ جلب الرد من الجهاز؛ اضغط «آخر رد» مرة أخرى بعد قليل.", buttons=self._job_buttons(record))
                return
            if not result.get("success"):
                self.send("تعذر جلب الرد من الجهاز حالياً. حاول مرة أخرى بعد التأكد من اتصاله.", buttons=self._job_buttons(record))
                return
            text = result.get("result_text")
            if not isinstance(text, str):
                text = None
        else:
            events = self.store.read_events(record.job_id)
            text = next((event.text for event in reversed(events) if event.type == "assistant" and event.text), None)
        if not text:
            self.send("لم يصل رد نصي من Antigravity لهذه المهمة بعد.", buttons=self._job_buttons(record))
            return
        safe = self.safety.redact(text)
        origin_dev = self._record_device(record)
        self.send(
            f"<b>آخر رد من Antigravity</b>\n"
            f"الجهاز: <code>{html.escape(origin_dev)}</code>\n"
            f"الاسم: <b>{html.escape(job_title(record))}</b>\n\n{html.escape(safe)}",
            buttons=self._job_buttons(record),
        )

    def _records(self) -> list[JobRecord]:
        records = self.store.list_jobs(limit=100)
        refreshed = []
        for record in records:
            record = self._inherit_conversation_title(record)
            self._remember_conversation_title(record)
            refreshed.append(record)
        try:
            from agy_mcp.device_gateway import load_remote_jobs_as_records
            remote = load_remote_jobs_as_records()
            for r in remote:
                self._remember_conversation_title(r)
            refreshed.extend(remote)
        except Exception:
            pass
        refreshed.sort(key=lambda r: r.updated_at or r.started_at or "", reverse=True)
        return refreshed

    def _help(self) -> None:
        self.send("<b>أوامر Gumacu Antigravity</b>\n/devices قائمة الأجهزة\n/device windows-main أو linux-netcup — اختيار الجهاز\n/last آخر رد للجلسة المختارة\n/shutdown إغلاق الجهاز المختار بعد تأكيد\n/jobs المهام الجارية\n/status ملخص الجارية\n/status الاسم أو الرقم\n/progress الاسم أو الرقم\n/recent أحدث النتائج\n/new نص الرسالة — جلسة جديدة\n/use الاسم أو الرقم — اختيار جلسة\n/sessions تصفح المحادثات\n/chatgpt وكلاء مساحة عمل ChatGPT\nأي نص عادي — إرساله للجلسة المختارة", menu=True)

    def _sessions(self) -> None:
        self._session_browser(0)

    def _jobs(self) -> None:
        active = [r for r in self._records() if r.status == "running"]
        if not active:
            self.send("لا توجد مهام Antigravity جارية الآن.")
            return
        self.send("<b>المهام الجارية</b>\n" + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in active[:20]))

    def _status(self, reference: str) -> None:
        records = self._records()
        if not reference:
            active = [r for r in records if r.status == "running"]
            if not active:
                self.send("لا توجد مهام جارية الآن.")
                return
            for record in active[:10]:
                if self._is_remote_record(record):
                    progress = record.extra.get("summary") or f"حالة الجهاز: {record.status}"
                else:
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
                self.send(
                    "⚠️ تعارض في المعرّف؛ يطابق أكثر من مهمة:\n"
                    + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in matches[:10])
                )
            else:
                self.send("لم أجد مهمة بهذا الاسم أو الرقم.")
            return
        if self._is_remote_record(record):
            progress = record.extra.get("summary") or f"حالة الجهاز: {record.status}"
        else:
            progress, _ = safe_progress(self.store.read_events(record.job_id))
        self.send(self._format_record(record, progress=progress, heading="📋 حالة المهمة"), buttons=self._job_buttons(record))

    def _recent(self) -> None:
        recent = [r for r in self._records() if r.status in TERMINAL][:10]
        if not recent:
            self.send("لا توجد مهام منتهية مسجلة.")
            return
        self.send("<b>أحدث المهام</b>\n" + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.status}</code> — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in recent))

    def _new(self, prompt: str) -> None:
        if not prompt:
            self.send("استخدم: /new ثم اكتب الرسالة التي تريد إرسالها إلى Antigravity")
            return
        active_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID
        self._state["active_device_id"] = active_dev
        self._state.pop("active_session_id", None)
        self._state.pop("active_job_id", None)
        self._state.pop("active_conversation_title", None)
        self._save_state()

        if active_dev == LOCAL_DEVICE_ID:
            self._start(prompt, session_id=None)
        else:
            dev_info = self._get_device_info(active_dev)
            if not dev_info or not dev_info.get("online"):
                self.send(
                    f"⚠️ الجهاز <code>{html.escape(active_dev)}</code> غير متصل حالياً (offline).\n"
                    "لا يمكن بدء جلسة جديدة على هذا الجهاز."
                )
                return
            self._start_remote(active_dev, prompt, session_id=None)

    def _use(self, reference: str) -> None:
        record, matches = resolve_job(self._records(), reference)
        if record is None or not record.session_id:
            if matches:
                self.send(
                    "⚠️ تعارض في المعرّف؛ يطابق أكثر من مهمة:\n"
                    + "\n".join(f"• {html.escape(job_title(r))} — <code>{r.job_id[-12:]}</code> ({self._record_device(r)})" for r in matches[:10])
                )
            else:
                self.send("لم أجد جلسة قابلة للمتابعة بهذا الاسم أو الرقم.")
            return

        dev = self._session_device(record)
        # Permanent session binding: session cannot resume on other device
        self._state["active_device_id"] = dev
        self._state["active_session_id"] = record.session_id
        self._state["active_job_id"] = record.job_id
        self._state["active_conversation_title"] = job_title(record)
        self._state.setdefault("session_devices", {})[record.session_id] = dev
        self._remember_conversation_title(record)
        self._save_state()

        self.send(
            f"تم اختيار جلسة: <b>{html.escape(job_title(record))}</b>\n"
            f"الجهاز الأصلي (مرتبط دائماً): <code>{html.escape(dev)}</code>\n"
            f"معرّف الجلسة: <code>{html.escape(record.session_id)}</code>\n"
            "أرسل رسالتك كنص عادي للمتابعة."
        )

    def _continue_or_start(self, prompt: str) -> None:
        session_id = self._state.get("active_session_id")
        if session_id and isinstance(session_id, str):
            target_dev = self.get_session_device(session_id)
            self._state["active_device_id"] = target_dev
            self._save_state()
        else:
            target_dev = self._state.get("active_device_id") or LOCAL_DEVICE_ID

        if target_dev != LOCAL_DEVICE_ID:
            dev_info = self._get_device_info(target_dev)
            if not dev_info or not dev_info.get("online"):
                self.send(
                    f"⚠️ الجهاز <code>{html.escape(target_dev)}</code> غير متصل حالياً (offline).\n"
                    "لا يمكن إرسال الرسالة إلى هذه الجلسة/الجهاز."
                )
                return
            self._start_remote(target_dev, prompt, session_id if isinstance(session_id, str) else None)
        else:
            self._start(prompt, session_id if isinstance(session_id, str) else None)

    def _start_remote(self, device_id: str, prompt: str, session_id: str | None) -> None:
        from agy_mcp.device_tools import gateway_call
        payload: dict[str, Any] = {
            "PROMPT": prompt,
            "cwd": ".",
            "mode": "long",
            "allow_write": False,
            "timeout": 0,
        }
        if session_id:
            payload["SESSION_ID"] = session_id
        try:
            res = gateway_call("/v1/admin/submit", {
                "device_id": device_id,
                "action": "start",
                "payload": payload,
            })
        except Exception as exc:  # noqa: BLE001
            self.send(f"تعذر بدء الرسالة على الجهاز {device_id}: {html.escape(self.safety.redact(str(exc))[:300])}")
            return
        if not res.get("success"):
            self.send(f"تعذر إرسال الطلب إلى {device_id}: {html.escape(str(res.get('error') or 'خطأ غير معروف'))}")
            return
        req_id = res.get("request_id")
        job_id = "job_r_" + req_id[5:] if req_id and req_id.startswith("call_") else str(req_id)
        self._state["active_job_id"] = job_id
        self._state.setdefault("telegram_remote_jobs", {})[job_id] = True
        if session_id:
            self._state["active_session_id"] = session_id
            self._state.setdefault("session_devices", {})[session_id] = device_id
        self._state["active_device_id"] = device_id
        self._save_state()
        action = "متابعة الجلسة" if session_id else "بدء مهمة جديدة"
        title = self._state.get("active_conversation_title")
        title_line = f"\nالمحادثة: <b>{html.escape(str(title))}</b>" if title else ""
        sess_disp = str(session_id) if session_id else "جلسة جديدة"
        self.send(
            f"⏳ تم {action} على الجهاز <code>{html.escape(device_id)}</code>.\n"
            f"الجهاز النشط: <code>{html.escape(device_id)}</code>\n"
            f"الجلسة النشطة: <code>{html.escape(sess_disp)}</code>"
            f"{title_line}\n"
            f"المعرّف: <code>{html.escape(str(job_id)[-12:])}</code>\n"
            "سيرسل إشعار عند بدء التنفيذ ثم النتيجة النهائية."
        )

    def _start(self, prompt: str, session_id: str | None) -> None:
        cwd = os.environ.get("AGY_MCP_TELEGRAM_CWD")
        if not cwd:
            allowed = os.environ.get("GUMACU_ALLOWED_WORKSPACES", "")
            cwd = allowed.split(os.pathsep)[0] if allowed else "."
        try:
            from agy_mcp.runtime_control import reopen_linux_if_closed
            reopen_linux_if_closed()
            request = BridgeRequest(prompt=prompt, cwd=cwd, session_id=session_id, mode="long", timeout=0, detach=True, allow_write=False, worktree=False, backend="agy", output_protocol="codex")
            response = self.supervisor.start(request)
        except Exception as exc:  # noqa: BLE001
            self.send(f"تعذر بدء الرسالة: {html.escape(self.safety.redact(str(exc))[:300])}")
            return
        if not response.success or not response.job_id:
            self.send(f"تعذر بدء الرسالة: {html.escape(response.error or 'خطأ غير معروف')}")
            return
        self._state["active_job_id"] = response.job_id
        telegram_jobs = self._state.setdefault("telegram_jobs", {})
        if isinstance(telegram_jobs, dict):
            telegram_jobs[response.job_id] = True
        if session_id:
            self._state["active_session_id"] = session_id
            self._state.setdefault("session_devices", {})[session_id] = LOCAL_DEVICE_ID
        self._state["active_device_id"] = LOCAL_DEVICE_ID
        self._save_state()
        record = self.store.get_job(response.job_id)
        if record is not None:
            record.extra["telegram_origin"] = True
            record.extra["device_id"] = LOCAL_DEVICE_ID
            if record.session_id:
                self._state.setdefault("session_devices", {})[record.session_id] = LOCAL_DEVICE_ID
            inherited_title = self._state.get("active_conversation_title") if session_id else None
            record.antigravity_title = (
                str(inherited_title).strip()[:120]
                if isinstance(inherited_title, str) and inherited_title.strip()
                else next((" ".join(line.split()) for line in prompt.splitlines() if line.strip()), "")[:120]
            ) or None
            record.title_source = "antigravity_task" if record.antigravity_title else "pending"
            self.store.update_job(record)
        action = "متابعة الجلسة" if session_id else "بدء جلسة جديدة"
        self._state[f"progress:{response.job_id}"] = {
            "fingerprint": "started",
            "sent_at": time.time(),
        }
        if not session_id and record is not None and record.antigravity_title:
            self._state["active_conversation_title"] = record.antigravity_title
        self._save_state()
        sess_disp = str(session_id or (record.session_id if record else "")) or "جلسة جديدة"
        title_line = f"\nالمحادثة: <b>{html.escape(job_title(record))}</b>" if record is not None else ""
        self.send(
            f"⏳ تم {action} على الجهاز <code>{LOCAL_DEVICE_ID}</code>.\n"
            f"الجهاز النشط: <code>{LOCAL_DEVICE_ID}</code>\n"
            f"الجلسة النشطة: <code>{html.escape(sess_disp)}</code>"
            f"{title_line}\n"
            f"المعرّف: <code>{response.job_id[-12:]}</code>\n"
            "سأرسل تحديثًا تلقائيًا كل بضع دقائق، ثم النتيجة النهائية."
        )

    def _chatgpt_command(self, arg: str) -> None:
        """Handle opt-in /chatgpt command for OpenAI Workspace Agents."""
        from agy_mcp import chatgpt_workspace as cw

        parts = arg.strip().split(maxsplit=2)
        subcommand = parts[0].lower() if parts else "status"

        if subcommand in {"status", "info"}:
            cap = cw.get_capabilities()
            configured = cap.get("configured", False)
            status_icon = "🟢 متصل ومُهيأ" if configured else "⚪ غير مفعّل (Opt-in)"
            agents_info = cw.list_agents()
            agents_list = agents_info.get("agents", [])
            lines = [
                "<b>🤖 مكاملة ChatGPT Workspace Agents الرسمية</b>\n",
                f"الحالة: {status_icon}",
                f"المزود: <code>{cw.PROVIDER_ID}</code>",
                "",
                "ℹ️ <b>تنبيه هام حول محادثات ChatGPT الشخصية:</b>",
                "لا توفر OpenAI أي واجهة برمجية رسمية (Official API) لقراءة أو استئناف المحادثات الشخصية السابقة في حساب ChatGPT.",
                "هذا المكوّن يستخدم حصرياً الواجهة الرسمية لـ <b>OpenAI Workspace Agents API</b> لإطلاق وكلاء مساحة العمل ومتابعة حالتهم.",
                "⚠️ لا يمكن استرجاع نصوص الإجابات عبر الـ API؛ يتم عرض المحادثة والنتائج عبر رابط <code>conversation_url</code> في ChatGPT مباشرة.",
            ]
            if configured and agents_list:
                lines.append("\n<b>الوكلاء المسجلون (Agents):</b>")
                for a in agents_list:
                    lines.append(f"• <code>{html.escape(a['alias'])}</code> ({html.escape(a['agent_id'])})")
            lines.extend([
                "",
                "<b>الاستخدام:</b>",
                "• <code>/chatgpt status</code> لعرض الحالة",
                "• <code>/chatgpt agents</code> لعرض الوكلاء المتاحين",
                "• <code>/chatgpt trigger &lt;alias&gt; &lt;الطلب&gt;</code> لإطلاق وكيل",
                "• <code>/chatgpt run &lt;run_id&gt; [alias]</code> للاستعلام عن حالة مهمة",
            ])
            self.send("\n".join(lines))
        elif subcommand == "agents":
            agents_info = cw.list_agents()
            if not agents_info.get("configured"):
                self.send("⚠️ مكاملة ChatGPT Workspace غير مهيأة. يلزم ضبط <code>CHATGPT_WORKSPACE_PAT</code> و <code>CHATGPT_WORKSPACE_AGENTS</code>.")
                return
            agents_list = agents_info.get("agents", [])
            if not agents_list:
                self.send("لا يوجد وكلاء مسجلون حالياً.")
                return
            lines = ["<b>🤖 وكلاء ChatGPT Workspace المسجلون:</b>\n"]
            for a in agents_list:
                lines.append(f"• <code>{html.escape(a['alias'])}</code> -> <code>{html.escape(a['agent_id'])}</code>")
            self.send("\n".join(lines))
        elif subcommand == "trigger":
            if len(parts) < 3:
                self.send("الاستخدام: <code>/chatgpt trigger &lt;اسم الوكيل&gt; &lt;الطلب&gt;</code>")
                return
            alias = parts[1]
            prompt_text = parts[2]
            res = cw.trigger_run(prompt_text, agent=alias)
            if not res.get("success"):
                self.send(f"❌ فشل إطلاق الوكيل:\n<code>{html.escape(str(res.get('error')))}</code>")
                return
            run_id = res.get("agent_trigger_run_id", "unknown")
            url = res.get("conversation_url", "")
            lines = [
                "✅ <b>تم إطلاق ChatGPT Workspace Agent بنجاح</b>\n",
                f"الوكيل: <code>{html.escape(alias)}</code>",
                f"معرّف المهمة: <code>{html.escape(run_id)}</code>",
                f"الحالة: <b>{html.escape(str(res.get('status', 'queued')))}</b>",
            ]
            if url:
                lines.append(f"🔗 رابط المحادثة في ChatGPT:\n{html.escape(url)}")
            lines.append("\n⚠️ نصوص الإجابات تعرض عبر واجهة ChatGPT ولا يتم سحبها عبر الـ API.")
            self.send("\n".join(lines))
        elif subcommand == "run":
            if len(parts) < 2:
                self.send("الاستخدام: <code>/chatgpt run &lt;run_id&gt; [alias]</code>")
                return
            run_id = parts[1]
            alias = parts[2] if len(parts) > 2 else None
            res = cw.get_run_status(run_id, agent=alias)
            if not res.get("success"):
                self.send(f"❌ فشل الاستعلام:\n<code>{html.escape(str(res.get('error')))}</code>")
                return
            lines = [
                "<b>📊 حالة مهمة ChatGPT Workspace Agent:</b>\n",
                f"معرّف المهمة: <code>{html.escape(run_id)}</code>",
                f"الحالة: <b>{html.escape(str(res.get('status', 'unknown')))}</b>",
            ]
            url = res.get("conversation_url")
            if url:
                lines.append(f"🔗 رابط المحادثة: {html.escape(url)}")
            self.send("\n".join(lines))
        else:
            self.send("أمر غير معروف لـ /chatgpt. استخدم <code>/chatgpt status</code>")


def main() -> None:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for env_path in ("/etc/gumacu-agy-mcp/telegram.env", "/etc/gumacu-agy-devices/gateway.env"):
        p = Path(env_path)
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    k, _, v = line.strip().partition("=")
                    if k and not os.environ.get(k):
                        os.environ[k] = v.strip("'\"")
            except OSError:
                pass

    from agy_mcp.config import get_config
    from agy_mcp.events import SubscriptionStore, WebhookDispatcher
    from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox
    from agy_mcp.orchestrator import Orchestrator
    from agy_mcp.safety import SafetyPolicy
    from agy_mcp.session_store import SessionStore
    from agy_mcp.supervisor import Supervisor

    config = get_config()
    safety = SafetyPolicy.from_config(config)
    store = SessionStore(Path(config.session_store_root()).expanduser())
    sub_path = store.root / "subscriptions.json"
    subscription_store = SubscriptionStore(sub_path)
    webhook_dispatcher = WebhookDispatcher(subscription_store)
    outbox = NotificationOutbox(store.root / "notifications.json")
    dispatcher = NotificationDispatcher(outbox)
    supervisor = Supervisor(
        store=store,
        config=config,
        safety=safety,
        events_dispatcher=webhook_dispatcher,
        notification_dispatcher=dispatcher,
    )
    orchestrator = Orchestrator(store=store, supervisor=supervisor)
    supervisor.orchestrator = orchestrator

    token = os.environ.get("AGY_MCP_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("AGY_MCP_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.error("AGY_MCP_TELEGRAM_BOT_TOKEN and AGY_MCP_TELEGRAM_CHAT_ID must be set")
        sys.exit(1)

    state_path = store.root / "telegram_state.json"
    bot = TelegramBot(
        token=token,
        chat_id=chat_id,
        store=store,
        supervisor=supervisor,
        safety=safety,
        state_path=state_path,
    )
    logger.info("Starting dedicated Telegram bot service for chat %s", chat_id)
    bot.start()
    try:
        while not bot._stop.wait(1.0):
            pass
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping Telegram bot service...")
        bot.stop()


__all__ = [
    "DEFAULT_DEVICE_ID",
    "LOCAL_DEVICE_ID",
    "LOCAL_DEVICE_NAME",
    "TelegramBot",
    "job_title",
    "main",
    "parse_command",
    "refresh_title",
    "resolve_job",
    "safe_progress",
]

if __name__ == "__main__":
    main()
