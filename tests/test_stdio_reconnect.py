"""A cached client must work against a new tunnel child without manual refresh."""
import json
import os
import selectors
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux SSH entry point")


@pytest.mark.parametrize("normal", [False, True])
@pytest.mark.parametrize("entry", ["alf_control.stdio_reconnect", "agy_mcp.stdio_reconnect", "launcher"])
def test_fresh_child_handles_normal_and_resumed_client(normal, entry, tmp_path):
    env = dict(os.environ, ALF_CONTROL_AUTO_REFRESH="0")
    if entry == "launcher":
        from pathlib import Path
        command = ["sh", str(Path(__file__).parents[1] / "deploy/run-mcp.sh")]
        env["ALF_CONTROL_PYTHON"] = sys.executable
    else:
        command = [sys.executable, "-m", entry]
    child = subprocess.Popen(
        command,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    requests = []
    if normal:
        requests.append({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "regression", "version": "1"},
        }})
    requests.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    responses = []
    try:
        for request in requests:
            child.stdin.write(json.dumps(request) + "\n")
            child.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                assert selector.select(30), "MCP response timed out"
            responses.append(json.loads(child.stdout.readline()))
            if request["method"] == "initialize":
                child.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
                child.stdin.flush()
        # Keep the transport open until each response arrives, as a real client does.
        output, errors = child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()
    assert child.returncode == 0, errors
    assert not output  # bootstrap response never leaks upstream
    assert len(responses) == len(requests)  # bootstrap response never leaks upstream
    assert [r["id"] for r in responses] == [r["id"] for r in requests]
    assert "agy_doctor" in [t["name"] for t in responses[-1]["result"]["tools"]]
    assert ("fresh MCP child initialized" in errors) is not normal
