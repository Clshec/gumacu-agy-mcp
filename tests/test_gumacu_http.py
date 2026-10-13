"""Exercise a real loopback HTTP MCP server without Google credentials."""
import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agy_mcp.models import CanonicalEvent
from agy_mcp.session_store import SessionStore


@pytest.mark.asyncio
async def test_http_roundtrip_full_text_and_cursor(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create_job(job_id="job_http", session_id="session-http")
    text = "الرد الأصلي 🌍\n" * 5000
    store.append_event(record.job_id, CanonicalEvent(type="system", subtype="init"))
    store.append_event(record.job_id, CanonicalEvent(type="assistant", text=text))
    store.finalize_job(record.job_id, status="completed", exit_code=0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, AGY_MCP_SESSION_ROOT=str(store.root))
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agy_mcp.cli", "--transport", "streamable-http",
             "--port", str(port)], env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert proc.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                pytest.fail("HTTP server did not become ready")
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "agy_result_page" in {t.name for t in tools.tools}
                    chunks, offset, digest = [], 0, None
                    while True:
                        result = await session.call_tool("agy_result_page", {
                            "job_id": record.job_id, "offset": offset, "limit": 7777,
                            "expected_sha256": digest,
                        })
                        assert not result.isError
                        page = result.structuredContent
                        assert page["success"]
                        assert page["session_id"] == "session-http"
                        chunks.append(page["text"])
                        offset, digest = page["next_offset"], page["sha256"]
                        if not page["has_more"]:
                            break
                    assert "".join(chunks) == text
                    result = await session.call_tool("agy_read", {
                        "job_id": record.job_id, "limit": 1,
                    })
                    assert result.structuredContent["next_since"] == 1
                    assert result.structuredContent["has_more"]
        finally:
            proc.terminate()
            proc.wait(timeout=10)
