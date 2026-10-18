from pathlib import Path

import pytest

from agy_mcp.device_gateway import Broker, token_hash


@pytest.mark.parametrize("action,result", [
    ("sessions", {"success": True, "records": []}),
    ("doctor", {"success": True, "report": {"healthy": True}}),
    ("status", {"success": False, "error": "unknown job"}),
])
def test_recordless_completion_is_durable_and_retryable(tmp_path: Path, action, result):
    broker = Broker(tmp_path / "broker.sqlite", {"windows-main": {
        "token_sha256": token_hash("test-token"),
        "allowed_actions": ["sessions", "doctor", "status"],
    }})
    try:
        payload = {"job_id": "job_missing"} if action == "status" else {}
        ident = broker.submit("windows-main", action, payload)
        work = broker.poll("windows-main")
        assert work["id"] == ident
        broker.finish("windows-main", ident, work["claim"], result)
        broker.finish("windows-main", ident, work["claim"], result)
        saved = broker.get(ident)
        assert saved["state"] == "done"
        assert saved["result"] == result
    finally:
        broker.conn.close()
