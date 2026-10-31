"""The pre-existing loopback HTTP transport remains usable after the rename."""
import asyncio
import os
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def test_loopback_http_exposes_direct_tools_in_legacy_read_only_profile(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, GUMACU_REMOTE_PROFILE="read-only")
    with (tmp_path / "http-server.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "agy_mcp.cli", "--transport", "streamable-http", "--port", str(port)],
            env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert child.poll() is None, (tmp_path / "http-server.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("loopback HTTP server did not start")
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "Alf Control"
                    names = {t.name for t in (await session.list_tools()).tools}
                    assert names == {"alf_capabilities", "agy_capabilities", "alf_doctor", "agy_doctor"}
                    denied = await session.call_tool("agy_dispatch", {
                        "device_id": "linux-netcup", "action": "ssh_call", "arguments": {"name": "ssh_exec"},
                    })
                    assert denied.isError
        finally:
            child.terminate()
            child.wait(timeout=10)
