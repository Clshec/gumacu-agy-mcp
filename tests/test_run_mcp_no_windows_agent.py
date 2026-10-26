"""The Linux ChatGPT MCP launcher must survive removal of the Windows agent."""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "deploy/run-mcp.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux POSIX launcher")


def _isolated_launcher(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    text = LAUNCHER.read_text(encoding="utf-8")
    telegram = tmp_path / "telegram.env"
    gateway = tmp_path / "gateway.env"
    text = text.replace("/etc/gumacu-agy-mcp/telegram.env", str(telegram))
    text = text.replace("/etc/gumacu-agy-devices/gateway.env", str(gateway))
    text = text.replace("/opt/gumacu-agy-mcp/venv/bin/python", shlex.quote(sys.executable))
    text = text.replace("/var/lib/gumacu-agy-mcp/sessions", str(tmp_path / "sessions"))
    text = text.replace("/root/gumacu-infrastructure", str(tmp_path))
    script = tmp_path / "run-mcp.sh"
    script.write_text(text, encoding="utf-8")
    subprocess.run(["sh", "-n", str(script)], check=True)
    return script, telegram, gateway


@pytest.mark.parametrize("optional_files_present", [False, True])
def test_mcp_starts_without_windows_gateway_env(tmp_path, optional_files_present):
    script, telegram, gateway = _isolated_launcher(tmp_path)
    if optional_files_present:
        telegram.write_text("AGY_TEST_OPTIONAL_TELEGRAM=set\n", encoding="utf-8")
        gateway.write_text("AGY_TEST_OPTIONAL_GATEWAY=set\n", encoding="utf-8")
    request = {"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}}
    env = dict(os.environ, AGY_MCP_DISABLE_TELEGRAM_BOT="1")
    result = subprocess.run(
        ["sh", str(script)],
        input=json.dumps(request) + "\n", text=True,
        capture_output=True, env=env, timeout=50, check=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert len(responses) == 1
    assert responses[0]["id"] == 42
    assert "result" in responses[0], result.stderr
    names = [tool["name"] for tool in responses[0]["result"]["tools"]]
    assert "agy_doctor" in names
    assert "agy_capabilities" in names
    # Newchild stdio recovery remains active after an uninitialized first call.
    assert "fresh MCP child initialized" in result.stderr


def test_bad_existing_optional_env_fails_closed(tmp_path):
    script, _, gateway = _isolated_launcher(tmp_path)
    gateway.mkdir()
    result = subprocess.run(
        ["sh", str(script)], input="", text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert "optional environment file exists but cannot be read" in result.stderr
    assert "AGY_DEVICE_ADMIN_TOKEN=" not in result.stderr
