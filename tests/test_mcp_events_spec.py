"""Comprehensive tests for OpenAI MCP Events specification (protocol 2026-07-28)."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from agy_mcp.events import (
    CALLBACK_ENDPOINT_ERROR,
    JOB_CANCELLED_EVENT,
    JOB_COMPLETED_EVENT,
    JOB_FAILED_EVENT,
    DiscoverRequest,
    EventDelivery,
    EventOutbox,
    ListEventsRequest,
    SubscribeEventParams,
    SubscribeEventRequest,
    SubscriptionStore,
    UnsubscribeEventParams,
    UnsubscribeEventRequest,
    VerificationCache,
    WebhookDispatcher,
    compute_subscription_id,
    setup_events_protocol,
    validate_callback_url,
    validate_secret,
    verify_callback_endpoint,
)
from agy_mcp.models import JobRecord

VALID_SECRET = "whsec_MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="


# ---------------------------------------------------------------------------
# 1. Discover and Catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_discover_advertises_protocol_and_events_capability(tmp_path: Path):
    mcp = FastMCP("test-discover")
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    setup_events_protocol(mcp, store)

    handler = mcp._mcp_server.request_handlers[DiscoverRequest]
    res = await handler(DiscoverRequest())

    data = res.root.model_dump(by_alias=True, exclude_none=True)
    assert data["resultType"] == "complete"
    assert data["supportedVersions"] == ["2026-07-28"]
    assert "tools" in data["capabilities"]
    assert "events" in data["capabilities"]


@pytest.mark.asyncio
async def test_events_list_catalog_conforms_to_spec(tmp_path: Path):
    mcp = FastMCP("test-list")
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    setup_events_protocol(mcp, store)

    handler = mcp._mcp_server.request_handlers[ListEventsRequest]
    res = await handler(ListEventsRequest())

    data = res.root.model_dump(by_alias=True, exclude_none=True)
    events = data["events"]
    assert len(events) == 3

    catalog_map = {e["name"]: e for e in events}
    assert set(catalog_map.keys()) == {JOB_COMPLETED_EVENT, JOB_FAILED_EVENT, JOB_CANCELLED_EVENT}

    for item in catalog_map.values():
        assert item["delivery"] == ["webhook"]
        assert isinstance(item["description"], str)
        assert item["inputSchema"]["type"] == "object"
        assert item["payloadSchema"]["type"] == "object"
        assert "job_id" in item["inputSchema"]["properties"]
        assert "status" in item["payloadSchema"]["properties"]


# ---------------------------------------------------------------------------
# 2. Secret Validation & SSRF Protection
# ---------------------------------------------------------------------------


def test_secret_validation_strict():
    # Valid: 32 bytes base64
    valid = "whsec_" + base64.b64encode(b"A" * 32).decode()
    validate_secret(valid)

    # Valid: 24 bytes base64 (minimum)
    valid_24 = "whsec_" + base64.b64encode(b"B" * 24).decode()
    validate_secret(valid_24)

    # Valid: 64 bytes base64 (maximum)
    valid_64 = "whsec_" + base64.b64encode(b"C" * 64).decode()
    validate_secret(valid_64)

    # Invalid: missing prefix
    with pytest.raises(ValueError, match="must begin with 'whsec_'"):
        validate_secret(base64.b64encode(b"A" * 32).decode())

    # Invalid: too short (< 24 bytes)
    short = "whsec_" + base64.b64encode(b"A" * 16).decode()
    with pytest.raises(ValueError, match="not within the required 24-64 bytes"):
        validate_secret(short)

    # Invalid: too long (> 64 bytes)
    long_sec = "whsec_" + base64.b64encode(b"A" * 65).decode()
    with pytest.raises(ValueError, match="not within the required 24-64 bytes"):
        validate_secret(long_sec)

    # Invalid: corrupt base64
    with pytest.raises(ValueError, match="base64 payload is invalid"):
        validate_secret("whsec_!!!not-valid-base64???")


def test_ssrf_url_validation():
    # Reject non-https
    with pytest.raises(ValueError, match="must use the https scheme"):
        validate_callback_url("http://webhook.openai.com/events")

    # Reject loopback IP directly
    with pytest.raises(ValueError, match="SSRF blocked"):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            validate_callback_url("https://localhost/events")

    # Reject private RFC1918
    with pytest.raises(ValueError, match="SSRF blocked"):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.100", 443))]):
            validate_callback_url("https://internal.lan/events")

    with pytest.raises(ValueError, match="SSRF blocked"):
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]):
            validate_callback_url("https://intranet.corp/events")

    # Accept public IP
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
        validate_callback_url("https://example.com/events")


# ---------------------------------------------------------------------------
# 3. Callback Verification Challenge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_challenge_verification_success():
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/callback_test",
        secret=VALID_SECRET,
    )
    v_cache = VerificationCache()

    captured_request = {}

    def mock_post(url, content, headers):
        captured_request["url"] = url
        captured_request["content"] = content
        captured_request["headers"] = headers
        body = json.loads(content.decode("utf-8"))
        challenge = body["challenge"]
        return httpx.Response(
            status_code=200,
            json={"challenge": challenge},
            request=httpx.Request("POST", url),
        )

    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            await verify_callback_endpoint(
                subscription_id="sub_123",
                delivery=delivery,
                verification_cache=v_cache,
            )

    # Verify captured request headers and format
    assert captured_request["headers"]["webhook-id"].startswith("msg_verification_")
    assert captured_request["headers"]["webhook-signature"].startswith("v1,")
    assert captured_request["headers"]["X-MCP-Subscription-Id"] == "sub_123"

    # Verify cached
    assert v_cache.is_verified("default", delivery.url)


@pytest.mark.asyncio
async def test_callback_challenge_mismatch_raises_error():
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/callback_test",
        secret=VALID_SECRET,
    )

    def mock_post(url, content, headers):
        return httpx.Response(
            status_code=200,
            json={"challenge": "wrong_challenge"},
            request=httpx.Request("POST", url),
        )

    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
        with patch("httpx.AsyncClient.post", side_effect=mock_post):
            with pytest.raises(McpError) as exc_info:
                await verify_callback_endpoint(
                    subscription_id="sub_123",
                    delivery=delivery,
                )
            assert exc_info.value.error.code == CALLBACK_ENDPOINT_ERROR
            assert exc_info.value.error.data["reason"] == "challenge_failed"


@pytest.mark.asyncio
async def test_callback_challenge_timeout_raises_error():
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/callback_test",
        secret=VALID_SECRET,
    )

    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
            with pytest.raises(McpError) as exc_info:
                await verify_callback_endpoint(
                    subscription_id="sub_123",
                    delivery=delivery,
                )
            assert exc_info.value.error.code == CALLBACK_ENDPOINT_ERROR
            assert exc_info.value.error.data["reason"] == "timeout"


# ---------------------------------------------------------------------------
# 4. Subscribe and Unsubscribe Contracts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_subscribe_idempotent_and_deterministic(tmp_path: Path):
    mcp = FastMCP("test-sub")
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    setup_events_protocol(mcp, store)

    sub_handler = mcp._mcp_server.request_handlers[SubscribeEventRequest]
    req = SubscribeEventRequest(
        params=SubscribeEventParams(
            name=JOB_COMPLETED_EVENT,
            arguments={"job_id": "job_spec_1"},
            delivery={"mode": "webhook", "url": "https://api.openai.com/hook", "secret": VALID_SECRET},
            ttlMs=3600000,
        )
    )

    with patch("agy_mcp.events.verify_callback_endpoint", new_callable=AsyncMock):
        res1 = await sub_handler(req)
        res2 = await sub_handler(req)

        # Same subscription ID derived deterministically
        assert res1.root.id == res2.root.id
        assert res1.root.id.startswith("sub_")
        assert res1.root.cursor is None
        assert res1.root.truncated is False
        assert res1.root.refreshBefore is not None

        # Verify deterministic calculation
        expected_id = compute_subscription_id(
            principal="default",
            url="https://api.openai.com/hook",
            name=JOB_COMPLETED_EVENT,
            arguments={"job_id": "job_spec_1"},
        )
        assert res1.root.id == expected_id


@pytest.mark.asyncio
async def test_events_unsubscribe_by_filter_and_id(tmp_path: Path):
    mcp = FastMCP("test-unsub")
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    setup_events_protocol(mcp, store)

    sub_handler = mcp._mcp_server.request_handlers[SubscribeEventRequest]
    unsub_handler = mcp._mcp_server.request_handlers[UnsubscribeEventRequest]

    with patch("agy_mcp.events.verify_callback_endpoint", new_callable=AsyncMock):
        sub_res = await sub_handler(
            SubscribeEventRequest(
                params=SubscribeEventParams(
                    name=JOB_COMPLETED_EVENT,
                    arguments={"job_id": "job_unsub"},
                    delivery={"mode": "webhook", "url": "https://api.openai.com/hook_unsub", "secret": VALID_SECRET},
                )
            )
        )
        sub_id = sub_res.root.id
        assert store.get(sub_id) is not None

        # Unsubscribe by filter object (as sent by ChatGPT)
        unsub_res = await unsub_handler(
            UnsubscribeEventRequest(
                params=UnsubscribeEventParams(
                    name=JOB_COMPLETED_EVENT,
                    arguments={"job_id": "job_unsub"},
                    delivery={"mode": "webhook", "url": "https://api.openai.com/hook_unsub"},
                )
            )
        )
        assert unsub_res.root.model_dump(exclude_none=True) == {}
        assert store.get(sub_id) is None


# ---------------------------------------------------------------------------
# 5. Webhook Delivery & Outbox Replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_delivery_envelope_and_standard_headers(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/webhook",
        secret=VALID_SECRET,
    )
    sub = store.add_or_update(JOB_COMPLETED_EVENT, delivery)
    dispatcher = WebhookDispatcher(store)

    delivered_payload = {}
    delivered_headers = {}

    def mock_post(url, content, headers):
        delivered_payload.update(json.loads(content.decode("utf-8")))
        delivered_headers.update(headers)
        return httpx.Response(status_code=200, request=httpx.Request("POST", url))

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        success = await dispatcher.deliver_subscription(
            sub,
            JOB_COMPLETED_EVENT,
            {"job_id": "job_99", "status": "completed"},
        )
        assert success is True

    # Verify envelope fields
    assert "eventId" in delivered_payload
    assert delivered_payload["name"] == JOB_COMPLETED_EVENT
    assert "timestamp" in delivered_payload
    assert delivered_payload["data"]["job_id"] == "job_99"
    assert delivered_payload["cursor"] is None

    # Verify standard webhook headers
    assert delivered_headers["webhook-id"] == delivered_payload["eventId"]
    assert "webhook-timestamp" in delivered_headers
    assert delivered_headers["webhook-signature"].startswith("v1,")
    assert delivered_headers["X-MCP-Subscription-Id"] == sub.id


@pytest.mark.asyncio
async def test_fast_finishing_job_replay(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    outbox = EventOutbox()
    dispatcher = WebhookDispatcher(store, outbox=outbox)

    # 1. Job completes BEFORE subscription exists
    finished_record = JobRecord(
        job_id="job_fast_1",
        status="completed",
        cwd="/workspace",
        request={},
        session_id="sess_fast",
    )
    dispatcher.dispatch_job_event(finished_record, result_preview="Finished in 50ms")

    # Outbox recorded the event
    matches = outbox.find_matching_events(JOB_COMPLETED_EVENT, job_id="job_fast_1")
    assert len(matches) == 1
    assert matches[0]["data"]["job_id"] == "job_fast_1"

    # 2. Subscription arrives AFTER job finished
    replayed = []

    async def mock_deliver(s, ev, d, **kwargs):
        replayed.append((s.id, ev, d["job_id"]))
        return True

    with patch.object(dispatcher, "deliver_subscription", side_effect=mock_deliver):
        delivery = EventDelivery(
            mode="webhook",
            url="https://api.openai.com/fast_hook",
            secret=VALID_SECRET,
        )
        sub = store.add_or_update(
            JOB_COMPLETED_EVENT,
            delivery,
            arguments={"job_id": "job_fast_1"},
        )

        # Trigger replay
        dispatcher.replay_for_subscription(sub)
        await asyncio.sleep(0.1)

    assert len(replayed) == 1
    assert replayed[0] == (sub.id, JOB_COMPLETED_EVENT, "job_fast_1")
