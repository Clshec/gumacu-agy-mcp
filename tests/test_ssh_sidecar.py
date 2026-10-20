"""Integration tests for the independent ssh-mcp backend and safe host routing."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agy_mcp import ssh_sidecar as ssh


def test_restrict_host_connection_options_and_forward_by_default(monkeypatch):
    monkeypatch.setenv("GUMACU_SSH_TARGET", "gumacu-netcup-local")
    monkeypatch.delenv("GUMACU_SSH_DENY_TOOLS", raising=False)
    assert ssh._secure_arguments("ssh_exec", {
        "target": "gumacu-netcup-local", "command": "id"
    })["strict_host_key_checking"] == "yes"
    with pytest.raises(ValueError, match="only the configured SSH target"):
        ssh._secure_arguments("ssh_exec", {"target": "other", "command": "id"})
    with pytest.raises(ValueError, match="connection parameters"):
        ssh._secure_arguments("ssh_exec", {"target": "gumacu-netcup-local",
                                          "extra_ssh_args": ["-F", "/tmp/bad"]})
    with pytest.raises(ValueError, match="not enabled"):
        ssh._secure_arguments("ssh_forward", {"target": "gumacu-netcup-local"})


def test_missing_sidecar_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_SSH_SOCKET", str(tmp_path / "missing.sock"))
    assert ssh.ssh_catalog()["success"] is False
    assert ssh.ssh_call("ssh_exec", {"target": "other"})["success"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stdio backend")
def test_stable_catalog_and_integration_over_unix_socket(monkeypatch, tmp_path):
    """Fake upstream JSON-RPC stdio child verifies that no manual tool schemas
    or secondary LLM is needed for ChatGPT to call any vendor ssh_* tool.
    """
    script = tmp_path / "fake-ssh-mcp"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " x=json.loads(line); m=x['method']; p=x['params']\n"
        " if m=='initialize': v={'result':{'serverInfo':{'name':'ssh-mcp'}}}\n"
        " elif m=='tools/list': v={'result':{'tools':[{'name':'ssh_exec','inputSchema':{'type':'object'}},"
        "{'name':'ssh_forward','inputSchema':{'type':'object'}}]}}\n"
        " elif m=='tools/call': v={'result':{'structuredContent':{'ok':True,'seen':p['name'],"
        "'argv':p['arguments']['command']},'isError':False}}\n"
        " else: v={'error':{'code':-1}}\n"
        " v.update(jsonrpc='2.0',id=x['id']); print(json.dumps(v),flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    # macOS AF_UNIX socket paths have a short limit; pytest tmp_path can exceed it.
    short_dir = tempfile.TemporaryDirectory(prefix="gsm-")
    sock = Path(short_dir.name) / "ssh.sock"
    monkeypatch.setenv("GUMACU_SSH_SOCKET", str(sock))
    monkeypatch.setenv("GUMACU_SSH_TARGET", "gumacu-netcup-local")
    monkeypatch.delenv("GUMACU_SSH_DENY_TOOLS", raising=False)
    backend = ssh.StdioBackend(str(script))
    server = ssh._Server(str(sock), backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
        catalog = ssh.ssh_catalog()
        assert catalog["success"]
        assert catalog["target"] == "gumacu-netcup-local"
        assert [t["name"] for t in catalog["tools"]] == ["ssh_exec"]
        result = ssh.ssh_call("ssh_exec", {
            "target": "gumacu-netcup-local", "command": "id"
        })
        assert result["success"]
        assert result["result"]["argv"] == "id"
        denied = ssh.ssh_call("ssh_exec", {"target": "evil", "command": "id"})
        assert not denied["success"]
        assert "target" in denied["error"]
    finally:
        server.shutdown()
        server.server_close()
        backend.close()
        thread.join(timeout=3)
        short_dir.cleanup()


def test_sidecar_uses_separate_stable_tools_without_registering_new_top_level_tools():
    from agy_mcp.capability_gateway import ACTIONS
    assert "ssh_catalog" in ACTIONS
    assert "ssh_call" in ACTIONS
    from agy_mcp import server
    names = [x.name for x in asyncio.run(server.mcp.list_tools())]
    assert "agy_dispatch" in names
    assert "ssh_exec" not in names
