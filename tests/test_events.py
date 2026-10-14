"""Tests for MCP Events subsystem (Protocol 2026-07-28)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server.fastmcp import FastMCP

from agy_mcp.events import (
    JOB_CANCELLED_EVENT,
    JOB_COMPLETED_EVENT,
    JOB_FAILED_EVENT,
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


def test_subscription_store_crud_and_persistence(tmp_path: Path):
    store_file = tmp_path / "subscriptions.json"
    store = SubscriptionStore(store_file)

    delivery = EventDelivery(
        mode="webhook",
        url="https://webhook.example.com/events",
        secret="whsec_dGVzdHNlY3JldDEyMzQ1Njc4OTA=",
    )

    sub1 = store.add(JOB_COMPLETED_EVENT, delivery, {"job_id": "job_123"})
    assert sub1.subscription_id.startswith("sub_")
    assert sub1.name == JOB_COMPLETED_EVENT
    assert sub1.arguments == {"job_id": "job_123"}

    # Test persistence by reloading
    reloaded_store = SubscriptionStore(store_file)
    fetched = reloaded_store.get(sub1.subscription_id)
    assert fetched is not None
    assert fetched.subscription_id == sub1.subscription_id
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
    payload = b'{"event":"job.completed","data":{"job_id":"job_test"}}'

    sig = StandardWebhooksSigner.sign(webhook_id, timestamp, payload, secret)
    assert sig.startswith("v1,")

    # Verify matching
    assert StandardWebhooksSigner.verify(webhook_id, timestamp, payload, secret, sig)

    # Tampered payload
    assert not StandardWebhooksSigner.verify(
        webhook_id, timestamp, b'{"event":"job.tampered"}', secret, sig
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
        secret="whsec_dGVzdA==",
    )
    sub = store.add(JOB_COMPLETED_EVENT, delivery)

    dispatcher = WebhookDispatcher(store)

    delivered_calls = []

    async def mock_deliver(s, ev, d):
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
    assert getattr(caps, "events", None) == {"subscribe": True, "listChanged": True}

    # Verify events/list handler
    list_handler = mcp._mcp_server.request_handlers[ListEventsRequest]
    list_res = await list_handler(ListEventsRequest())
    assert hasattr(list_res.root, "events")
    assert len(list_res.root.events) == 3
    event_names = {e["name"] for e in list_res.root.events}
    assert event_names == {JOB_COMPLETED_EVENT, JOB_FAILED_EVENT, JOB_CANCELLED_EVENT}

    # Verify events/subscribe handler
    sub_handler = mcp._mcp_server.request_handlers[SubscribeEventRequest]
    sub_req = SubscribeEventRequest(
        params=SubscribeEventParams(
            name=JOB_COMPLETED_EVENT,
            delivery={"mode": "webhook", "url": "https://example.com/hook", "secret": "whsec_abc123"},
        )
    )
    sub_res = await sub_handler(sub_req)
    sub_id = getattr(sub_res.root, "subscription_id", None)
    assert sub_id is not None
    assert sub_id.startswith("sub_")

    # Verify events/unsubscribe handler
    unsub_handler = mcp._mcp_server.request_handlers[UnsubscribeEventRequest]
    unsub_req = UnsubscribeEventRequest(
        params=UnsubscribeEventParams(subscriptionId=sub_id)
    )
    unsub_res = await unsub_handler(unsub_req)
    assert getattr(unsub_res.root, "success", None) is True
    assert store.get(sub_id) is None


@pytest.mark.asyncio
async def test_webhook_dispatch_timeout_and_no_final_response(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "subscriptions.json")
    delivery = EventDelivery(
        mode="webhook",
        url="https://api.openai.com/v1/webhook_test",
        secret="whsec_dGVzdHNlY3JldDEyMzQ1Njc4OTA=",
    )
    _ = store.add(JOB_FAILED_EVENT, delivery)
    dispatcher = WebhookDispatcher(store)

    delivered = []

    async def mock_deliver(s, ev, d):
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

    assert len(delivered) == 2
    assert delivered[0][1] == JOB_FAILED_EVENT
    assert delivered[0][2]["status"] == "timeout"
    assert delivered[0][2]["error"] == "print timeout after 19m30s"

    assert delivered[1][1] == JOB_FAILED_EVENT
    assert delivered[1][2]["status"] == "no_final_response"
    assert delivered[1][2]["error"] == "process finished with exit code 0 but produced no final response"
