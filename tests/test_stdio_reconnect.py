"""A cached client must work against a new tunnel child without manual refresh."""
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux SSH entry point")


@pytest.mark.parametrize("normal", [False, True])
def test_fresh_child_handles_normal_and_resumed_client(normal, tmp_path):
    env = dict(os.environ, AGY_MCP_SESSION_ROOT=str(tmp_path), AGY_MCP_DISABLE_TELEGRAM_BOT="1")
    child = subprocess.Popen(
        [sys.executable, "-m", "agy_mcp.stdio_reconnect"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    requests = []
    if normal:
        requests.append({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "regression", "version": "1"},
        }})
    requests.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    output, errors = child.communicate("".join(json.dumps(r) + "\n" for r in requests), timeout=30)
    assert child.returncode == 0, errors
    responses = [json.loads(line) for line in output.splitlines()]
    assert len(responses) == len(requests)  # bootstrap response never leaks upstream
    assert [r["id"] for r in responses] == [r["id"] for r in requests]
    assert "agy_doctor" in [t["name"] for t in responses[-1]["result"]["tools"]]
    assert ("fresh MCP child initialized" in errors) is not normal
