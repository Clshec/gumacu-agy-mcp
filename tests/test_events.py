"""Tests for MCP Events subsystem (Protocol 2026-07-28)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from agy_mcp.events import (
    JOB_COMPLETED_EVENT,
    JOB_FAILED_EVENT,
    DiscoverRequest,
    EventDelivery,
    ListEventsRequest,
    StandardWebhooksSigner,
    SubscribeEventParams,
    SubscribeEventRequest,
    SubscriptionStore,
    UnsubscribeEventParams,
    UnsubscribeEventRequest,
    WebhookDispatcher,
    setup_events_protocol,
)
from agy_mcp.models import JobRecord

VALID_SECRET = "whsec_MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="


def test_subscription_store_crud_and_persistence(tmp_path: Path):
    store_file = tmp_path / "subscriptions.json"
    store = SubscriptionStore(store_file)

    delivery = EventDelivery(
        mode="webhook",
        url="https://webhook.example.com/events",
        secret=VALID_SECRET,
    )

    sub1 = store.add(JOB_COMPLETED_EVENT, delivery, {"job_id": "job_123"})
    assert sub1.subscription_id.startswith("sub_")
    assert sub1.id.startswith("sub_")
    assert sub1.name == JOB_COMPLETED_EVENT
    assert sub1.arguments == {"job_id": "job_123"}
    assert sub1.refresh_before is not None

    # Test persistence by reloading
    reloaded_store = SubscriptionStore(store_file)
    fetched = reloaded_store.get(sub1.subscription_id)
    assert fetched is not None
    assert fetched.id == sub1.id
    assert fetched.delivery.url == "https://webhook.example.com/events"

    # Matching logic
    assert len(store.matching(JOB_COMPLETED_EVENT, job_id="job_123")) == 1
    assert len(store.matching(JOB_COMPLETED_EVENT, job_id="job_other")) == 0
    assert len(store.matching(JOB_FAILED_EVENT, job_id="job_123")) == 0

    # Removal
    assert store.remove(sub1.subscription_id) is True
    assert store.remove("nonexistent") is False
    assert len(store.list_all()) == 0


def test_standard_webhooks_signer():
    secret = "whsec_MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"
    webhook_id = "msg_20261014abcdef"
    timestamp = 1791950000
    payload = b'{"eventId":"evt_1","name":"job.completed","data":{"job_id":"job_test"}}'

    sig = StandardWebhooksSigner.sign(webhook_id, timestamp, payload, secret)
    assert sig.startswith("v1,")

    # Verify matching
    assert StandardWebhooksSigner.verify(webhook_id, timestamp, payload, secret, sig)

    # Tampered payload
    assert not StandardWebhooksSigner.verify(
        webhook_id, timestamp, b'{"eventId":"evt_tampered"}', secret, sig
    )

    # Tampered timestamp
    assert not StandardWebhooksSigner.verify(
        webhook_id, timestamp + 1, payload, secret, sig
    )

    # Tampered secret
    assert not StandardWebhooksSigner.verify(
        webhook_id, timestamp, payload, "whsec_wrongsecret", sig
    )


@pytest.mark.asyncio
async def test_webhook_dispatcher_dedup_and_dispatch(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/v1/webhook_test",
        secret=VALID_SECRET,
    )
    sub = store.add(JOB_COMPLETED_EVENT, delivery)

    dispatcher = WebhookDispatcher(store)

    delivered_calls = []

    async def mock_deliver(s, ev, d, **kwargs):
        delivered_calls.append((s.subscription_id, ev, d["job_id"]))
        return True

    with patch.object(dispatcher, "deliver_subscription", side_effect=mock_deliver):
        record = JobRecord(
            job_id="job_test_dedup",
            status="completed",
            cwd="/test",
            request={},
            session_id="sess_123",
        )

        # First dispatch
        dispatcher.dispatch_job_event(record, result_preview="All tests pass")
        # Give worker thread a moment
        await asyncio.sleep(0.1)

        # Second dispatch of exact same job + status should be deduped
        dispatcher.dispatch_job_event(record, result_preview="All tests pass")
        await asyncio.sleep(0.1)

    assert len(delivered_calls) == 1
    assert delivered_calls[0] == (sub.subscription_id, JOB_COMPLETED_EVENT, "job_test_dedup")


@pytest.mark.asyncio
async def test_setup_events_protocol_handlers(tmp_path: Path):
    mcp = FastMCP("test-events")
    store = SubscriptionStore(tmp_path / "subscriptions.json")

    setup_events_protocol(mcp, store)

    # Verify capabilities
    from mcp.server.lowlevel.server import NotificationOptions

    caps = mcp._mcp_server.get_capabilities(NotificationOptions(), {})
    assert getattr(caps, "events", None) == {}

    # Verify server/discover handler
    discover_handler = mcp._mcp_server.request_handlers[DiscoverRequest]
    disc_res = await discover_handler(DiscoverRequest())
    assert disc_res.root.resultType == "complete"
    assert "2026-07-28" in disc_res.root.supportedVersions
    assert "events" in disc_res.root.capabilities

    # Verify events/list handler
    list_handler = mcp._mcp_server.request_handlers[ListEventsRequest]
    list_res = await list_handler(ListEventsRequest())
    assert hasattr(list_res.root, "events")
    assert len(list_res.root.events) == 3
    for ev in list_res.root.events:
        assert ev["delivery"] == ["webhook"]
        assert "inputSchema" in ev
        assert "payloadSchema" in ev

    # Verify events/subscribe handler with mocked callback verification
    sub_handler = mcp._mcp_server.request_handlers[SubscribeEventRequest]
    sub_req = SubscribeEventRequest(
        params=SubscribeEventParams(
            name=JOB_COMPLETED_EVENT,
            delivery={"mode": "webhook", "url": "https://example.com/hook", "secret": VALID_SECRET},
        )
    )

    with patch("agy_mcp.events.verify_callback_endpoint", new_callable=AsyncMock) as mock_verify:
        sub_res = await sub_handler(sub_req)
        assert mock_verify.called
        sub_id = getattr(sub_res.root, "id", None)
        assert sub_id is not None
        assert sub_id.startswith("sub_")
        assert sub_res.root.refreshBefore is not None
        assert sub_res.root.cursor is None
        assert sub_res.root.truncated is False

    # Verify events/unsubscribe handler
    unsub_handler = mcp._mcp_server.request_handlers[UnsubscribeEventRequest]
    unsub_req = UnsubscribeEventRequest(
        params=UnsubscribeEventParams(id=sub_id)
    )
    unsub_res = await unsub_handler(unsub_req)
    assert unsub_res.root.model_dump(exclude_none=True) == {}
    assert store.get(sub_id) is None


@pytest.mark.asyncio
async def test_webhook_dispatch_failure_reasons(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/v1/webhook_test",
        secret=VALID_SECRET,
    )
    _ = store.add(JOB_FAILED_EVENT, delivery)
    dispatcher = WebhookDispatcher(store)

    delivered = []

    async def mock_deliver(s, ev, d, **kwargs):
        delivered.append((s.subscription_id, ev, d))
        return True

    with patch.object(dispatcher, "deliver_subscription", side_effect=mock_deliver):
        # 1. Timeout job
        timeout_rec = JobRecord(
            job_id="job_to_1",
            status="timeout",
            cwd="/test",
            request={},
            session_id="sess_to",
            error="print timeout after 19m30s",
        )
        dispatcher.dispatch_job_event(timeout_rec)
        await asyncio.sleep(0.1)

        # 2. No final response job
        empty_rec = JobRecord(
            job_id="job_empty_1",
            status="no_final_response",
            cwd="/test",
            request={},
            session_id="sess_empty",
            error="process finished with exit code 0 but produced no final response",
        )
        dispatcher.dispatch_job_event(empty_rec)
        await asyncio.sleep(0.1)

        # 3. Quota exhausted: ChatGPT must get an explicit machine-readable reason.
        quota_rec = JobRecord(
            job_id="job_quota_1",
            status="quota_exhausted",
            cwd="/test",
            request={},
            session_id="sess_quota",
            error="Antigravity quota exhausted: usage limit reached",
        )
        dispatcher.dispatch_job_event(quota_rec)
        await asyncio.sleep(0.1)

    assert len(delivered) == 3
    assert delivered[0][1] == JOB_FAILED_EVENT
    assert delivered[0][2]["status"] == "timeout"
    assert delivered[0][2]["error"] == "print timeout after 19m30s"

    assert delivered[1][1] == JOB_FAILED_EVENT
    assert delivered[1][2]["status"] == "no_final_response"
    assert delivered[1][2]["error"] == "process finished with exit code 0 but produced no final response"

    assert delivered[2][1] == JOB_FAILED_EVENT
    assert delivered[2][2]["status"] == "quota_exhausted"
    assert delivered[2][2]["reason_code"] == "quota_exhausted"
    assert "quota exhausted" in delivered[2][2]["error"]
