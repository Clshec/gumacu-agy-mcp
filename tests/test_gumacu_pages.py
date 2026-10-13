"""Lossless Unicode pagination and transport boundaries without Google access."""
from types import SimpleNamespace

import pytest

from agy_mcp import server
from agy_mcp.cli import main
from agy_mcp.utils import anonymise_paths


def test_lossless_response_pages(monkeypatch):
    text = "مرحبا 🌍\n" * 20000
    monkeypatch.setattr(server, "agy_result_tool", lambda **kw: SimpleNamespace(
        success=True, result_text=text, job_id="job_test",
        record=SimpleNamespace(session_id="same-session"),
    ))
    offset, chunks, digest = 0, [], None
    while True:
        page = server.agy_result_page_tool("job_test", offset, 137, digest)
        assert page["success"]
        assert page["session_id"] == "same-session"
        chunks.append(page["text"])
        offset, digest = page["next_offset"], page["sha256"]
        if not page["has_more"]:
            break
    assert "".join(chunks) == text
    assert offset == len(text)
    assert not server.agy_result_page_tool("job_test", 0, 100, "changed")["success"]
    assert not server.agy_result_page_tool("job_test", len(text) + 1)["success"]


@pytest.mark.parametrize("offset,limit", [(-1, 100), (0, 0), (0, 64001)])
def test_page_bounds(offset, limit):
    assert not server.agy_result_page_tool("job_test", offset, limit)["success"]


def test_transport_is_loopback(monkeypatch):
    monkeypatch.setattr(server, "_ensure_state", lambda: None)
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append(kw))
    server.run("streamable-http", 9876)
    assert server.mcp.settings.host == "127.0.0.1"
    assert server.mcp.settings.port == 9876
    assert calls == [{"transport": "streamable-http"}]
    with pytest.raises(ValueError):
        server.run("sse")


def test_cli_selects_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "run", lambda **kw: calls.append(kw))
    assert main(["--transport", "streamable-http", "--port", "9876"]) == 0
    assert calls == [{"transport": "streamable-http", "port": 9876}]


def test_root_path_boundary():
    assert anonymise_paths("/root/private") == "~/private"
    assert anonymise_paths("/rooted/private") == "/rooted/private"
