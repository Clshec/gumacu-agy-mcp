"""MCP Events subsystem implementing protocol version 2026-07-28.

Provides:
- Official OpenAI MCP Events specification compliance (2026-07-28)
- server/discover capability advertisement
- Event definitions (job.completed, job.failed, job.cancelled) with webhook delivery
- Standard Webhooks signing (HMAC-SHA256, webhook-id, webhook-timestamp, webhook-signature)
- SSRF validation with DNS resolution and private/loopback/link-local blocking
- Strict secret validation (whsec_<base64>, 24-64 bytes)
- Callback challenge verification with single-use challenge token and constant-time match
- Deterministic subscription IDs and persistent subscription store (subscriptions.json)
- Outbox replay for fast-finishing jobs
- Asynchronous webhook dispatcher with retries, exponential backoff, and deduplication
- Integration handlers for server/discover, events/list, events/subscribe, and events/unsubscribe
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Literal, Union

import httpx
import mcp.types as _mcp_types
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, ConfigDict, Field, RootModel

from agy_mcp import __version__
from agy_mcp.models import JobRecord
from agy_mcp.utils import ensure_directory, safe_write_text, utc_now_iso

logger = logging.getLogger(__name__)

PROTOCOL_VERSION_2026_07_28 = "2026-07-28"
CALLBACK_ENDPOINT_ERROR = -32015
MAX_WEBHOOK_PAYLOAD_BYTES = 256 * 1024  # 256 KiB
DEFAULT_SUBSCRIPTION_TTL_SECONDS = 86400  # 24 hours
MIN_SUBSCRIPTION_TTL_SECONDS = 60  # 1 minute

# ---------------------------------------------------------------------------
# Event Schemas & Definitions (Official 2026-07-28 Spec)
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
            "enum": [
                "failed",
                "upstream_error",
                "quota_exhausted",
                "timeout",
                "no_final_response",
            ],
        },
        "finished_at": {"type": "string"},
        "error": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]},
        "reason_code": {"type": "string"},
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
        "description": "Triggered when an Antigravity background job finishes execution successfully.",
        "delivery": ["webhook"],
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_COMPLETED,
    },
    {
        "name": JOB_FAILED_EVENT,
        "description": "Triggered when an Antigravity background job encounters an error or fails.",
        "delivery": ["webhook"],
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_FAILED,
    },
    {
        "name": JOB_CANCELLED_EVENT,
        "description": "Triggered when an Antigravity background job is cancelled.",
        "delivery": ["webhook"],
        "inputSchema": INPUT_SCHEMA_JOB_FILTER,
        "payloadSchema": PAYLOAD_SCHEMA_CANCELLED,
    },
]


# ---------------------------------------------------------------------------
# SSRF Protection & Secret Validation
# ---------------------------------------------------------------------------


def validate_callback_url(url_str: str, *, allow_local: bool = False) -> None:
    """Validate callback URL against SSRF and protocol requirements."""
    allow_env = os.environ.get("AGY_MCP_ALLOW_LOCAL_URLS", "").lower() in {"1", "true", "yes"}
    allow = allow_local or allow_env

    try:
        parsed = urllib.parse.urlsplit(url_str)
    except Exception as exc:
        raise ValueError(f"Malformed callback URL: {exc}") from exc

    if not allow:
        if parsed.scheme != "https":
            raise ValueError("Callback URL must use the https scheme")
    elif parsed.scheme not in {"http", "https"}:
        raise ValueError("Callback URL must use http or https scheme")

    if not parsed.hostname:
        raise ValueError("Callback URL must contain a valid hostname")

    if allow:
        return

    # Resolve hostname to IPv4/IPv6 and verify that none are non-public
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise ValueError(f"DNS resolution failed for callback hostname {parsed.hostname}: {err}") from err

    if not addr_info:
        raise ValueError(f"No IP addresses resolved for {parsed.hostname}")

    for _, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as err:
            raise ValueError(f"Invalid resolved IP address: {ip_str}") from err

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"SSRF blocked: {parsed.hostname} resolved to non-public IP address ({ip})")


def validate_secret(secret: str) -> None:
    """Validate that the secret matches whsec_<base64> decoding to 24-64 bytes."""
    if not secret or not secret.startswith("whsec_"):
        raise ValueError("Webhook secret must begin with 'whsec_'")
    raw = secret[6:]
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"Webhook secret base64 payload is invalid: {exc}") from exc
    if not (24 <= len(decoded) <= 64):
        raise ValueError(
            f"Decoded secret length {len(decoded)} is not within the required 24-64 bytes"
        )


def compute_subscription_id(
    principal: str,
    url: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Derive deterministic subscription ID from principal, URL, name, and canonical JSON arguments."""
    canonical_args = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"))
    seed = f"{principal}|{url}|{name}|{canonical_args}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:24]
    return f"sub_{digest}"


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
        for part in signature_header.split(" "):
            if hmac.compare_digest(part.strip(), expected):
                return True
        return False


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

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: EventDelivery
    principal: str = "default"
    created_at: str = Field(default_factory=utc_now_iso)
    refresh_before: str | None = None
    expires_at: float | None = None
    cursor: str | None = None

    @property
    def subscription_id(self) -> str:
        """Backward compatibility accessor."""
        return self.id


class VerificationCache:
    """Thread-safe bounded in-memory cache for successfully verified endpoints."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._cache: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def is_verified(self, principal: str, url: str) -> bool:
        with self._lock:
            expiry = self._cache.get((principal, url))
            if expiry is not None and time.time() < expiry:
                return True
            if expiry is not None:
                self._cache.pop((principal, url), None)
            return False

    def mark_verified(self, principal: str, url: str) -> None:
        with self._lock:
            self._cache[(principal, url)] = time.time() + self._ttl


class EventOutbox:
    """Thread-safe ring buffer of recent events for replay and fast-job handling."""

    def __init__(self, capacity: int = 100) -> None:
        self._capacity = capacity
        self._events: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def record_event(self, event_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            if event_id in self._events:
                return
            self._events[event_id] = payload
            self._order.append(event_id)
            if len(self._order) > self._capacity:
                evict_id = self._order.pop(0)
                self._events.pop(evict_id, None)

    def find_matching_events(
        self,
        event_name: str,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        with self._lock:
            for ev_id in self._order:
                ev = self._events.get(ev_id)
                if not ev:
                    continue
                if ev.get("name") != event_name:
                    continue
                data = ev.get("data", {})
                if job_id and data.get("job_id") != job_id:
                    continue
                if session_id and data.get("session_id") != session_id:
                    continue
                matches.append(ev)
        return matches


class SubscriptionStore:
    """Thread-safe, file-backed subscription store with expiration and idempotency."""

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
                    sub_id = item.get("id") or item.get("subscription_id")
                    if not sub_id:
                        continue
                    item["id"] = sub_id
                    sub = EventSubscription.model_validate(item)
                    self._subscriptions[sub.id] = sub
        except Exception as e:
            logger.warning("Failed to load subscriptions from %s: %s", self.path, e)

    def _save(self) -> None:
        data = [sub.model_dump(mode="json") for sub in self._subscriptions.values()]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        safe_write_text(self.path, payload)

    def add_or_update(
        self,
        name: str,
        delivery: EventDelivery,
        arguments: dict[str, Any] | None = None,
        *,
        principal: str = "default",
        ttl_ms: int | None = None,
    ) -> EventSubscription:
        """Create or update subscription idempotently with deterministic ID."""
        with self._lock:
            sub_id = compute_subscription_id(
                principal=principal,
                url=delivery.url,
                name=name,
                arguments=arguments,
            )

            now = time.time()
            if ttl_ms is None:
                ttl_s = DEFAULT_SUBSCRIPTION_TTL_SECONDS
                expires_at = now + ttl_s
                dt = datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc)
                refresh_before = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                ttl_s = max(MIN_SUBSCRIPTION_TTL_SECONDS, ttl_ms // 1000)
                expires_at = now + ttl_s
                dt = datetime.datetime.fromtimestamp(expires_at, tz=datetime.timezone.utc)
                refresh_before = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            sub = EventSubscription(
                id=sub_id,
                name=name,
                arguments=arguments or {},
                delivery=delivery,
                principal=principal,
                refresh_before=refresh_before,
                expires_at=expires_at,
            )
            self._subscriptions[sub_id] = sub
            self._save()
            return sub

    def add(
        self,
        name: str,
        delivery: EventDelivery,
        arguments: dict[str, Any] | None = None,
    ) -> EventSubscription:
        """Legacy helper for backward compatibility."""
        return self.add_or_update(name=name, delivery=delivery, arguments=arguments)

    def remove(self, subscription_id: str) -> bool:
        with self._lock:
            if subscription_id in self._subscriptions:
                del self._subscriptions[subscription_id]
                self._save()
                return True
            return False

    def remove_by_filter(
        self,
        name: str,
        url: str,
        arguments: dict[str, Any] | None = None,
        *,
        principal: str = "default",
    ) -> bool:
        sub_id = compute_subscription_id(
            principal=principal,
            url=url,
            name=name,
            arguments=arguments,
        )
        return self.remove(sub_id)

    def get(self, subscription_id: str) -> EventSubscription | None:
        with self._lock:
            return self._subscriptions.get(subscription_id)

    def list_all(self) -> list[EventSubscription]:
        with self._lock:
            now = time.time()
            return [
                sub
                for sub in self._subscriptions.values()
                if sub.expires_at is None or sub.expires_at > now
            ]

    def matching(
        self,
        event_name: str,
        *,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> list[EventSubscription]:
        now = time.time()
        matches: list[EventSubscription] = []
        with self._lock:
            subs = list(self._subscriptions.values())
        for sub in subs:
            if sub.expires_at is not None and sub.expires_at <= now:
                continue
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
# Callback Verification
# ---------------------------------------------------------------------------


async def verify_callback_endpoint(
    subscription_id: str,
    delivery: EventDelivery,
    *,
    principal: str = "default",
    verification_cache: VerificationCache | None = None,
    timeout_seconds: float = 10.0,
    allow_local: bool = False,
) -> None:
    """Execute callback verification challenge per MCP 2.0 specification.

    Raises McpError with code -32015 on failure.
    """
    if verification_cache and verification_cache.is_verified(principal, delivery.url):
        return

    # SSRF verification
    try:
        validate_callback_url(delivery.url, allow_local=allow_local)
    except ValueError as err:
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message=f"SSRF validation blocked callback URL: {err}",
                data={"reason": "ssrf_blocked"},
            )
        ) from err

    challenge_token = secrets.token_urlsafe(32)
    challenge_body = {
        "type": "verification",
        "challenge": challenge_token,
    }
    body_bytes = json.dumps(challenge_body, separators=(",", ":")).encode("utf-8")
    webhook_id = f"msg_verification_{secrets.token_hex(12)}"
    timestamp = int(time.time())
    signature = StandardWebhooksSigner.sign(
        webhook_id=webhook_id,
        timestamp=timestamp,
        payload_bytes=body_bytes,
        secret=delivery.secret,
    )

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature,
        "X-MCP-Subscription-Id": subscription_id,
        "User-Agent": f"Gumacu-Antigravity-MCP/{__version__}",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            resp = await client.post(delivery.url, content=body_bytes, headers=headers)
    except httpx.TimeoutException as exc:
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message=f"Callback verification challenge timed out after {timeout_seconds}s",
                data={"reason": "timeout"},
            )
        ) from exc
    except Exception as exc:
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message=f"Callback verification connection error: {exc}",
                data={"reason": "connection_error"},
            )
        ) from exc

    if not (200 <= resp.status_code < 300):
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message=f"Callback endpoint returned HTTP {resp.status_code}",
                data={"reason": "callback_error", "status": resp.status_code},
            )
        )

    try:
        data = resp.json()
        returned_challenge = str(data.get("challenge", ""))
    except Exception as exc:
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message="Callback verification response was not valid JSON",
                data={"reason": "challenge_failed"},
            )
        ) from exc

    if not hmac.compare_digest(returned_challenge, challenge_token):
        raise McpError(
            error=_mcp_types.ErrorData(
                code=CALLBACK_ENDPOINT_ERROR,
                message="Callback verification challenge mismatch",
                data={"reason": "challenge_failed"},
            )
        )

    if verification_cache:
        verification_cache.mark_verified(principal, delivery.url)


# ---------------------------------------------------------------------------
# Webhook Dispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Asynchronous webhook dispatcher with retry, deduplication, backoff, and outbox."""

    def __init__(
        self,
        subscription_store: SubscriptionStore,
        *,
        max_retries: int = 3,
        timeout_seconds: float = 10.0,
        dedup_history_size: int = 500,
        verification_cache: VerificationCache | None = None,
        outbox: EventOutbox | None = None,
    ) -> None:
        self.store = subscription_store
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        self._sent_keys: set[str] = set()
        self._sent_keys_order: list[str] = []
        self._dedup_cap = dedup_history_size
        self._lock = threading.Lock()
        self.verification_cache = verification_cache or VerificationCache()
        self.outbox = outbox or EventOutbox()

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
        *,
        event_id: str | None = None,
        cursor: str | None = None,
    ) -> bool:
        """Deliver a signed webhook per official OpenAI MCP Events specification."""
        evt_id = event_id or f"evt_{secrets.token_hex(12)}"
        envelope = {
            "eventId": evt_id,
            "name": event_name,
            "timestamp": utc_now_iso(),
            "data": data,
            "cursor": cursor,
        }
        body_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")

        # Payload size cap: 256 KiB
        if len(body_bytes) > MAX_WEBHOOK_PAYLOAD_BYTES:
            logger.warning(
                "Event %s payload exceeds 256 KiB (%s bytes); trimming preview",
                evt_id,
                len(body_bytes),
            )
            trimmed_data = dict(data)
            if "result_preview" in trimmed_data:
                trimmed_data["result_preview"] = (
                    trimmed_data["result_preview"][:500] + "... [truncated for size]"
                )
            envelope["data"] = trimmed_data
            body_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            if len(body_bytes) > MAX_WEBHOOK_PAYLOAD_BYTES:
                logger.error("Event %s payload exceeds 256 KiB after trim; aborting", evt_id)
                return False

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for attempt in range(1, self.max_retries + 1):
                timestamp = int(time.time())
                signature = StandardWebhooksSigner.sign(
                    webhook_id=evt_id,
                    timestamp=timestamp,
                    payload_bytes=body_bytes,
                    secret=subscription.delivery.secret,
                )

                headers = {
                    "Content-Type": "application/json; charset=utf-8",
                    "webhook-id": evt_id,
                    "webhook-timestamp": str(timestamp),
                    "webhook-signature": signature,
                    "X-MCP-Subscription-Id": subscription.id,
                    "User-Agent": f"Gumacu-Antigravity-MCP/{__version__}",
                }

                try:
                    resp = await client.post(
                        subscription.delivery.url,
                        content=body_bytes,
                        headers=headers,
                    )
                    if 200 <= resp.status_code < 300:
                        logger.info(
                            "Webhook %s delivered for event %s to %s (HTTP %s)",
                            evt_id,
                            event_name,
                            subscription.delivery.url,
                            resp.status_code,
                        )
                        return True

                    # Handle HTTP 410 Gone: subscription cancelled by remote
                    if resp.status_code == 410:
                        logger.warning(
                            "Webhook endpoint returned 410 Gone; removing subscription %s",
                            subscription.id,
                        )
                        self.store.remove(subscription.id)
                        return False

                    # Handle HTTP 413 Payload Too Large: do not retry
                    if resp.status_code == 413:
                        logger.error(
                            "Webhook endpoint returned 413 Payload Too Large for %s",
                            evt_id,
                        )
                        return False

                    # Client error 4xx: do not retry (except rate limit 429)
                    if 400 <= resp.status_code < 500 and resp.status_code != 429:
                        logger.warning(
                            "Webhook %s client rejected (HTTP %s): %s",
                            evt_id,
                            resp.status_code,
                            resp.text[:200],
                        )
                        return False

                except (httpx.TransportError, httpx.TimeoutException) as err:
                    logger.debug("Webhook delivery attempt %s failed: %s", attempt, err)

                if attempt < self.max_retries:
                    await asyncio.sleep(2 ** (attempt - 1))

        logger.error(
            "Webhook %s delivery exhausted retries for subscription %s",
            evt_id,
            subscription.id,
        )
        return False

    def replay_for_subscription(self, subscription: EventSubscription) -> None:
        """Replay recent matching events for a newly registered subscription."""
        filter_job = subscription.arguments.get("job_id")
        filter_session = subscription.arguments.get("session_id")
        matches = self.outbox.find_matching_events(
            subscription.name,
            job_id=filter_job,
            session_id=filter_session,
        )
        for ev in matches:
            dedup_key = f"{ev['eventId']}:{subscription.id}"
            if not self._mark_sent(dedup_key):
                continue

            def _fire(s=subscription, e=ev):
                try:
                    asyncio.run(
                        self.deliver_subscription(
                            s,
                            e["name"],
                            e["data"],
                            event_id=e["eventId"],
                            cursor=e.get("cursor"),
                        )
                    )
                except Exception as ex:
                    logger.warning("Replay delivery error: %s", ex)

            t = threading.Thread(target=_fire, daemon=True)
            t.start()

    def dispatch_job_event(
        self,
        record: JobRecord,
        *,
        result_preview: str = "",
        total_chars: int = 0,
        sha256: str = "",
    ) -> None:
        """Trigger webhooks for a finalized job and record into outbox."""
        if record.status not in {
            "completed",
            "failed",
            "cancelled",
            "upstream_error",
            "quota_exhausted",
            "timeout",
            "no_final_response",
        }:
            return

        finished_at = record.finished_at or utc_now_iso()
        if record.status == "completed":
            event_name = JOB_COMPLETED_EVENT
            data = {
                "job_id": record.job_id,
                "session_id": record.session_id or "",
                "status": "completed",
                "finished_at": finished_at,
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
                "finished_at": finished_at,
            }
        else:
            event_name = JOB_FAILED_EVENT
            data = {
                "job_id": record.job_id,
                "session_id": record.session_id or "",
                "status": record.status,
                "finished_at": finished_at,
                "error": record.error or "Unknown failure",
                "exit_code": record.exit_code,
                "reason_code": (
                    "quota_exhausted" if record.status == "quota_exhausted" else record.status
                ),
            }

        event_id = f"evt_{secrets.token_hex(12)}"
        outbox_payload = {
            "eventId": event_id,
            "name": event_name,
            "timestamp": finished_at,
            "data": data,
            "cursor": None,
        }
        self.outbox.record_event(event_id, outbox_payload)

        matching = self.store.matching(
            event_name,
            job_id=record.job_id,
            session_id=record.session_id,
        )
        if not matching:
            return

        for sub in matching:
            dedup_key = f"{record.job_id}:{record.status}:{sub.id}"
            if not self._mark_sent(dedup_key):
                continue

            def _fire(s=sub, ev=event_name, d=data, eid=event_id):
                try:
                    asyncio.run(
                        self.deliver_subscription(
                            s,
                            ev,
                            d,
                            event_id=eid,
                        )
                    )
                except Exception as ex:
                    logger.warning("Background webhook delivery error: %s", ex)

            t = threading.Thread(target=_fire, daemon=True)
            t.start()


# ---------------------------------------------------------------------------
# MCP Protocol Handlers (Protocol Version 2026-07-28)
# ---------------------------------------------------------------------------


class DiscoverRequest(BaseModel):
    method: Literal["server/discover"] = "server/discover"
    params: _mcp_types.RequestParams | None = None


class ListEventsParams(_mcp_types.RequestParams):
    model_config = ConfigDict(extra="ignore")
    cursor: str | None = None


class ListEventsRequest(BaseModel):
    method: Literal["events/list"] = "events/list"
    params: ListEventsParams | _mcp_types.RequestParams | None = None


class SubscribeEventParams(_mcp_types.RequestParams):
    model_config = ConfigDict(extra="ignore")
    name: str
    delivery: dict[str, Any]
    arguments: dict[str, Any] | None = None
    cursor: str | None = None
    ttlMs: int | None = None


class SubscribeEventRequest(BaseModel):
    method: Literal["events/subscribe"] = "events/subscribe"
    params: SubscribeEventParams


class UnsubscribeDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str = "webhook"
    url: str


class UnsubscribeEventParams(_mcp_types.RequestParams):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    subscription_id: str | None = None
    subscriptionId: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None

    @property
    def target_id(self) -> str | None:
        return self.id or self.subscription_id or self.subscriptionId


class UnsubscribeEventRequest(BaseModel):
    method: Literal["events/unsubscribe"] = "events/unsubscribe"
    params: UnsubscribeEventParams | _mcp_types.RequestParams | None = None


def setup_events_protocol(
    mcp_instance: Any,
    sub_store: SubscriptionStore,
    *,
    dispatcher: WebhookDispatcher | None = None,
    allow_local_verification: bool = False,
) -> None:
    """Wire MCP events protocol (2026-07-28) handlers and capabilities into FastMCP."""
    import mcp.server.session as mcp_session
    import mcp.types as mcp_types

    if PROTOCOL_VERSION_2026_07_28 not in mcp_session.SUPPORTED_PROTOCOL_VERSIONS:
        mcp_session.SUPPORTED_PROTOCOL_VERSIONS.append(PROTOCOL_VERSION_2026_07_28)

    server = mcp_instance._mcp_server
    orig_get_capabilities = server.get_capabilities

    def _get_capabilities_with_events(notification_options, experimental_capabilities):
        caps = orig_get_capabilities(notification_options, experimental_capabilities)
        caps.events = {}
        if caps.tools is not None:
            caps.tools = mcp_types.ToolsCapability(listChanged=True)
        return caps

    server.get_capabilities = _get_capabilities_with_events

    extended_union = Union[
        mcp_types.ClientRequestType,
        DiscoverRequest,
        ListEventsRequest,
        SubscribeEventRequest,
        UnsubscribeEventRequest,
    ]

    class ExtendedClientRequest(RootModel[extended_union]):
        pass

    mcp_types.ClientRequest = ExtendedClientRequest
    mcp_session.types.ClientRequest = ExtendedClientRequest

    async def handle_discover(req: DiscoverRequest) -> mcp_types.ServerResult:
        """Handle server/discover per MCP 2.0 specification."""
        return mcp_types.ServerResult(
            mcp_types.EmptyResult(
                resultType="complete",
                supportedVersions=[PROTOCOL_VERSION_2026_07_28],
                capabilities={
                    "tools": {"listChanged": True},
                    "events": {},
                },
            )
        )

    async def handle_list_events(req: ListEventsRequest) -> mcp_types.ServerResult:
        """Handle events/list per MCP 2.0 specification."""
        return mcp_types.ServerResult(
            mcp_types.EmptyResult(
                events=EVENT_CATALOG,
                nextCursor=None,
            )
        )

    async def handle_subscribe(req: SubscribeEventRequest) -> mcp_types.ServerResult:
        """Handle events/subscribe per MCP 2.0 specification with callback verification."""
        if req.params.name not in SUPPORTED_EVENT_NAMES:
            raise McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"Unsupported event name: {req.params.name}. Supported: {sorted(SUPPORTED_EVENT_NAMES)}",
                )
            )

        try:
            delivery = EventDelivery.model_validate(req.params.delivery)
        except Exception as exc:
            raise McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"Invalid event delivery configuration: {exc}",
                )
            ) from exc

        # Validate secret
        try:
            validate_secret(delivery.secret)
        except ValueError as exc:
            raise McpError(
                error=mcp_types.ErrorData(
                    code=mcp_types.INVALID_PARAMS,
                    message=f"Invalid secret: {exc}",
                )
            ) from exc

        sub_id = compute_subscription_id(
            principal="default",
            url=delivery.url,
            name=req.params.name,
            arguments=req.params.arguments,
        )

        # Verify callback challenge before saving
        v_cache = dispatcher.verification_cache if dispatcher else None
        await verify_callback_endpoint(
            subscription_id=sub_id,
            delivery=delivery,
            principal="default",
            verification_cache=v_cache,
            allow_local=allow_local_verification,
        )

        sub = sub_store.add_or_update(
            name=req.params.name,
            delivery=delivery,
            arguments=req.params.arguments,
            principal="default",
            ttl_ms=req.params.ttlMs,
        )

        # Replay any recent fast-finishing job events
        if dispatcher:
            dispatcher.replay_for_subscription(sub)

        return mcp_types.ServerResult(
            mcp_types.EmptyResult(
                id=sub.id,
                refreshBefore=sub.refresh_before,
                cursor=None,
                truncated=False,
                # Compatibility fields for legacy consumers
                subscription_id=sub.id,
                subscriptionId=sub.id,
            )
        )

    async def handle_unsubscribe(req: UnsubscribeEventRequest) -> mcp_types.ServerResult:
        """Handle events/unsubscribe per MCP 2.0 specification."""
        params = req.params if isinstance(req.params, UnsubscribeEventParams) else None
        target_id = params.target_id if params else None

        if target_id:
            sub_store.remove(target_id)
        elif params and params.name and params.delivery and params.delivery.get("url"):
            sub_store.remove_by_filter(
                name=params.name,
                url=params.delivery["url"],
                arguments=params.arguments,
                principal="default",
            )
        else:
            # Idempotent: even if missing, acknowledge empty result
            pass

        return mcp_types.ServerResult(mcp_types.EmptyResult())

    server.request_handlers[DiscoverRequest] = handle_discover
    server.request_handlers[ListEventsRequest] = handle_list_events
    server.request_handlers[SubscribeEventRequest] = handle_subscribe
    server.request_handlers[UnsubscribeEventRequest] = handle_unsubscribe
