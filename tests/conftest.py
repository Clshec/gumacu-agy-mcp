import asyncio
import os

import pytest

asyncio.set_event_loop(None)

@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("ALF_CONTROL_", "GUMACU_", "AGY_MCP_")):
            monkeypatch.delenv(key, raising=False)
