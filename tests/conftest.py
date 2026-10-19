"""Shared fixtures for agy-mcp test suite."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Prevent pytest-asyncio on Python 3.12 from implicitly creating an
# unmanaged default event loop (and unclosed socketpair).
asyncio.set_event_loop(None)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Start the test runner without an implicit thread-local event loop."""
    asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def isolate_bridge_host_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep Netcup's live bridge configuration out of unit tests.

    Tests that intentionally exercise a profile or feature may set its
    variables with monkeypatch after this autouse fixture has run.
    """
    for key in tuple(os.environ):
        if key.startswith(("GUMACU_", "AGY_MCP_", "AGY_DEVICE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGY_DEVICE_GATEWAY_DB", str(tmp_path / "isolated-broker.sqlite"))


@pytest.fixture
def tmp_session_root(tmp_path: Path) -> Path:
    """Isolated session-store root for tests that exercise the SessionStore."""

    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Clear AGY_MCP_* / agy / gemini env vars so config tests are deterministic."""

    cleared = []
    for key in list(os.environ.keys()):
        if key.startswith("AGY_MCP_") or key in {"AGY_BIN", "GEMINI_BIN", "XDG_CONFIG_HOME"}:
            cleared.append(key)
            monkeypatch.delenv(key, raising=False)
    yield {key: "" for key in cleared}
