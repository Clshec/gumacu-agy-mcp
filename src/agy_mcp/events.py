"""MCP Events subsystem implementing protocol version 2026-07-28.

Provides:
- Event definitions (job.completed, job.failed, job.cancelled)
- Standard Webhooks signing (HMAC-SHA256, webhook-id, webhook-timestamp, webhook-signature)
- Persistent subscription store (subscriptions.json)
- Asynchronous webhook dispatcher with retries, exponential backoff, and deduplication
- Integration handlers for events/list, events/subscribe, and events/unsubscribe
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Literal, Union

import httpx
import mcp.types as _mcp_types
from pydantic import BaseModel, ConfigDict, Field, RootModel

from agy_mcp.models import JobRecord
from agy_mcp.utils import ensure_directory, safe_write_text, utc_now_iso

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_2026_07_28 = "2026-07-28"

# ---------------------------------------------------------------------------
# Event Schemas & Definitions
# ---------------------------------------------------------------------------

JOB_COMPLETED_EVENT = "job.completed"
JOB_FAILED_EVENT = "job.failed"
JOB_CANCELLED_EVENT = "job.cancelled"

SUPPORTED_EVENT_NAMES = frozenset(
    {JOB_COMPLETED_EVENT, JOB_FAILED_EVENT, JOB_CANCELLED_EVENT}
)

INPUT_SCHEMA_JOB_FILTER: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "Optional specific job ID to monitor",
        },
        "session_id": {
            "type": "string",
            "description": "Optional Antigravity session ID to monitor across turns",
        },
    },
    "additionalProperties": False,
}

PAYLOAD_SCHEMA_COMPLETED: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "session_id": {"type": "string"},
        "status": {"type": "string", "enum": ["completed"]},
        "finished_at": {"type": "string"},
        "result_preview": {"type": "string"},
        "total_chars": {"type": "integer"},
        "sha256": {"type": "string"},
        "cwd": {"type": "string"},
    },
    "required": ["job_id", "status"],
}

PAYLOAD_SCHEMA_FAILED: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "session_id": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["failed", "upstream_error", "timeout", "no_final_response"],
        },
        "finished_at": {"type": "string"},
        "error": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
    },
    "required": ["job_id", "status", "error"],
}

PAYLOAD_SCHEMA_CANCELLED: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "session_id": {"type": "string"},
        "status": {"type": "string", "enum": ["cancelled"]},
        "finished_at": {"type": "string"},
    },
    "required": ["job_id", "status"],
}

EVENT_CATALOG: list[dict[str, Any]] = [
    {
        "name": JOB_COMPLETED_EVENT,
        "description": "Triggered when an Antigravity job finishes execution successfully.",
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_COMPLETED,
    },
    {
        "name": JOB_FAILED_EVENT,
        "description": "Triggered when an Antigravity job encounters an error or fails.",
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_FAILED,
    },
    {
        "name": JOB_CANCELLED_EVENT,
        "description": "Triggered when an Antigravity job is cancelled.",
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_CANCELLED,
    },
]


# ---------------------------------------------------------------------------
# Subscription Models & Storage
# ---------------------------------------------------------------------------


class EventDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: Literal["webhook"] = "webhook"
    url: str
    secret: str


class EventSubscription(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subscription_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: EventDelivery
    created_at: str = Field(default_factory=utc_now_iso)


class SubscriptionStore:
    """Thread-safe, file-backed subscription store."""

    def __init__(self, storage_path: Path) -> None:
        self.path = Path(storage_path)
        self._lock = threading.Lock()
        ensure_directory(self.path.parent, mode=0o700)
        self._subscriptions: dict[str, EventSubscription] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    sub = EventSubscription.model_validate(item)
                    self._subscriptions[sub.subscription_id] = sub
        except Exception as e:
            logger.warning("Failed to load subscriptions from %s: %s", self.path, e)

    def _save(self) -> None:
        data = [sub.model_dump(mode="json") for sub in self._subscriptions.values()]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        safe_write_text(self.path, payload)

    def add(
        self,
        name: str,
        delivery: EventDelivery,
        arguments: dict[str, Any] | None = None,
    ) -> EventSubscription:
        with self._lock:
            sub_id = f"sub_{int(time.time())}_{secrets.token_hex(6)}"
            sub = EventSubscription(
                subscription_id=sub_id,
                name=name,
                arguments=arguments or {},
                delivery=delivery,
            )
            self._subscriptions[sub_id] = sub
            self._save()
            return sub

    def remove(self, subscription_id: str) -> bool:
        with self._lock:
            if subscription_id in self._subscriptions:
                del self._subscriptions[subscription_id]
                self._save()
                return True
            return False

    def get(self, subscription_id: str) -> EventSubscription | None:
        with self._lock:
            return self._subscriptions.get(subscription_id)

    def list_all(self) -> list[EventSubscription]:
        with self._lock:
            return list(self._subscriptions.values())

    def matching(
        self,
        event_name: str,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> list[EventSubscription]:
        matches: list[EventSubscription] = []
        with self._lock:
            subs = list(self._subscriptions.values())
        for sub in subs:
            if sub.name != event_name:
                continue
            filter_job = sub.arguments.get("job_id")
            if filter_job and filter_job != job_id:
                continue
            filter_session = sub.arguments.get("session_id")
            if filter_session and filter_session != session_id:
                continue
            matches.append(sub)
        return matches


# ---------------------------------------------------------------------------
# Standard Webhooks Signing
# ---------------------------------------------------------------------------


class StandardWebhooksSigner:
    """Computes and verifies signatures per the Standard Webhooks specification."""

    @staticmethod
    def sign(
        webhook_id: str,
        timestamp: int,
        payload_bytes: bytes,
        secret: str,
    ) -> str:
        """Return the v1 signature formatted as 'v1,<base64>'."""
        if secret.startswith("whsec_"):
            raw_secret = secret[6:]
            try:
                key = base64.b64decode(raw_secret)
            except Exception:
                key = secret.encode("utf-8")
        else:
            key = secret.encode("utf-8")

        to_sign = f"{webhook_id}.{timestamp}.".encode("utf-8") + payload_bytes
        signature = base64.b64encode(
            hmac.new(key, to_sign, hashlib.sha256).digest()
        ).decode("utf-8")
        return f"v1,{signature}"

    @classmethod
    def verify(
        cls,
        webhook_id: str,
        timestamp: int,
        payload_bytes: bytes,
        secret: str,
        signature_header: str,
    ) -> bool:
        expected = cls.sign(webhook_id, timestamp, payload_bytes, secret)
        # signature_header may contain space-delimited multiple signatures
        for part in signature_header.split(" "):
            if hmac.compare_digest(part.strip(), expected):
                return True
        return False


# ---------------------------------------------------------------------------
# Webhook Dispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Asynchronous webhook dispatcher with retry, deduplication, and backoff."""

    def __init__(
        self,
        subscription_store: SubscriptionStore,
        *,
        max_retries: int = 3,
        timeout_seconds: float = 8.0,
        dedup_history_size: int = 500,
    ) -> None:
        self.store = subscription_store
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        self._sent_keys: set[str] = set()
        self._sent_keys_order: list[str] = []
        self._dedup_cap = dedup_history_size
        self._lock = threading.Lock()

    def _mark_sent(self, key: str) -> bool:
        with self._lock:
            if key in self._sent_keys:
                return False
            self._sent_keys.add(key)
            self._sent_keys_order.append(key)
            if len(self._sent_keys_order) > self._dedup_cap:
                oldest = self._sent_keys_order.pop(0)
                self._sent_keys.discard(oldest)
            return True

    async def deliver_subscription(
        self,
        subscription: EventSubscription,
        event_name: str,
        data: dict[str, Any],
    ) -> bool:
        webhook_id = f"msg_{secrets.token_hex(12)}"
        timestamp = int(time.time())
        envelope = {
            "event": event_name,
            "timestamp": utc_now_iso(),
            "subscription_id": subscription.subscription_id,
            "data": data,
        }
        body_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        signature = StandardWebhooksSigner.sign(
            webhook_id=webhook_id,
            timestamp=timestamp,
            payload_bytes=body_bytes,
            secret=subscription.delivery.secret,
        )

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "webhook-id": webhook_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": signature,
            "User-Agent": "Gumacu-Antigravity-MCP/0.1.9",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, self.max_retries + 1):
                try:
                    resp = await client.post(
                        subscription.delivery.url,
                        content=body_bytes,
                        headers=headers,
                    )
                    if 200 <= resp.status_code < 300:
                        logger.info(
                            "Webhook %s delivered for event %s to %s (HTTP %s)",
                            webhook_id,
                            event_name,
                            subscription.delivery.url,
                            resp.status_code,
                        )
                        return True
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        # Non-retryable client error
                        logger.warning(
                            "Webhook %s client rejected (HTTP %s): %s",
                            webhook_id,
                            resp.status_code,
                            resp.text[:200],
                        )
                        return False
                except (httpx.TransportError, httpx.TimeoutException) as err:
                    logger.debug(
                        "Webhook delivery attempt %s failed: %s", attempt, err
                    )

                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        logger.error(
            "Webhook %s delivery exhausted retries for subscription %s",
            webhook_id,
            subscription.subscription_id,
        )
        return False

    def dispatch_job_event(
        self,
        record: JobRecord,
        *,
        result_preview: str = "",
        total_chars: int = 0,
        sha256: str = "",
    ) -> None:
        """Trigger webhooks for a finalized job."""
        if record.status not in {
            "completed",
            "failed",
            "cancelled",
            "upstream_error",
            "timeout",
            "no_final_response",
        }:
            return

        if record.status == "completed":
            event_name = JOB_COMPLETED_EVENT
            data = {
                "job_id": record.job_id,
                "session_id": record.session_id or "",
                "status": "completed",
                "finished_at": record.finished_at or utc_now_iso(),
                "result_preview": result_preview[:1000] if result_preview else "",
                "total_chars": total_chars or (len(result_preview) if result_preview else 0),
                "sha256": sha256,
                "cwd": record.cwd,
            }
        elif record.status == "cancelled":
            event_name = JOB_CANCELLED_EVENT
            data = {
                "job_id": record.job_id,
                "session_id": record.session_id or "",
                "status": "cancelled",
                "finished_at": record.finished_at or utc_now_iso(),
            }
        else:
            event_name = JOB_FAILED_EVENT
            data = {
                "job_id": record.job_id,
                "session_id": record.session_id or "",
                "status": record.status,
                "finished_at": record.finished_at or utc_now_iso(),
                "error": record.error or "Unknown failure",
                "exit_code": record.exit_code,
            }

        matching = self.store.matching(
            event_name,
            job_id=record.job_id,
            session_id=record.session_id,
        )
        if not matching:
            return

        for sub in matching:
            dedup_key = f"{record.job_id}:{record.status}:{sub.subscription_id}"
            if not self._mark_sent(dedup_key):
                continue

            def _fire(s=sub, ev=event_name, d=data):
                try:
                    asyncio.run(self.deliver_subscription(s, ev, d))
                except Exception as ex:
                    logger.warning("Background webhook delivery error: %s", ex)

            t = threading.Thread(target=_fire, daemon=True)
            t.start()


# ---------------------------------------------------------------------------
# MCP Protocol Handlers (Protocol Version 2026-07-28)
# ---------------------------------------------------------------------------



class ListEventsRequest(BaseModel):
    method: Literal["events/list"] = "events/list"
    params: _mcp_types.RequestParams | None = None


class SubscribeEventParams(_mcp_types.RequestParams):
    model_config = ConfigDict(extra="ignore")
    name: str
    delivery: dict[str, Any]
    arguments: dict[str, Any] | None = None


class SubscribeEventRequest(BaseModel):
    method: Literal["events/subscribe"] = "events/subscribe"
    params: SubscribeEventParams


class UnsubscribeEventParams(_mcp_types.RequestParams):
    model_config = ConfigDict(extra="ignore")
    subscription_id: str | None = None
    subscriptionId: str | None = None

    @property
    def sub_id(self) -> str:
        return self.subscription_id or self.subscriptionId or ""


class UnsubscribeEventRequest(BaseModel):
    method: Literal["events/unsubscribe"] = "events/unsubscribe"
    params: UnsubscribeEventParams


def setup_events_protocol(mcp_instance: Any, sub_store: SubscriptionStore) -> None:
    """Wire MCP events protocol (2026-07-28) handlers and capabilities into FastMCP."""
    import mcp.server.session as mcp_session
    import mcp.types as mcp_types

    if PROTOCOL_VERSION_2026_07_28 not in mcp_session.SUPPORTED_PROTOCOL_VERSIONS:
        mcp_session.SUPPORTED_PROTOCOL_VERSIONS.append(PROTOCOL_VERSION_2026_07_28)

    server = mcp_instance._mcp_server
    orig_get_capabilities = server.get_capabilities

    def _get_capabilities_with_events(notification_options, experimental_capabilities):
        caps = orig_get_capabilities(notification_options, experimental_capabilities)
        caps.events = {"subscribe": True, "listChanged": True}
        return caps

    server.get_capabilities = _get_capabilities_with_events

    extended_union = Union[
        mcp_types.ClientRequestType,
        ListEventsRequest,
        SubscribeEventRequest,
        UnsubscribeEventRequest,
    ]

    class ExtendedClientRequest(RootModel[extended_union]):
        pass

    mcp_types.ClientRequest = ExtendedClientRequest
    mcp_session.types.ClientRequest = ExtendedClientRequest

    async def handle_list_events(req: ListEventsRequest) -> mcp_types.ServerResult:
        return mcp_types.ServerResult(mcp_types.EmptyResult(events=EVENT_CATALOG))

    async def handle_subscribe(req: SubscribeEventRequest) -> mcp_types.ServerResult:
        if req.params.name not in SUPPORTED_EVENT_NAMES:
            raise mcp_types.McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"Unsupported event name: {req.params.name}. Supported: {sorted(SUPPORTED_EVENT_NAMES)}",
                )
            )
        try:
            delivery = EventDelivery.model_validate(req.params.delivery)
        except Exception as exc:
            raise mcp_types.McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"Invalid event delivery configuration: {exc}",
                )
            ) from exc

        sub = sub_store.add(
            name=req.params.name,
            delivery=delivery,
            arguments=req.params.arguments,
        )
        return mcp_types.ServerResult(
            mcp_types.EmptyResult(
                subscription_id=sub.subscription_id,
                subscriptionId=sub.subscription_id,
            )
        )

    async def handle_unsubscribe(req: UnsubscribeEventRequest) -> mcp_types.ServerResult:
        sub_id = req.params.sub_id
        if not sub_id:
            raise mcp_types.McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message="Missing subscription_id or subscriptionId",
                )
            )
        removed = sub_store.remove(sub_id)
        return mcp_types.ServerResult(
            mcp_types.EmptyResult(
                success=removed,
                subscription_id=sub_id,
                subscriptionId=sub_id,
            )
        )

    server.request_handlers[ListEventsRequest] = handle_list_events
    server.request_handlers[SubscribeEventRequest] = handle_subscribe
    server.request_handlers[UnsubscribeEventRequest] = handle_unsubscribe
