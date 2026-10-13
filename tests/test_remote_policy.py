
import pytest
from mcp.server.fastmcp import FastMCP

from agy_mcp.remote_policy import READ_TOOLS, restrict_request, restrict_tools


@pytest.fixture
def approved(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))
    return tmp_path


def test_defaults_force_safe_arguments(approved):
    out = restrict_request({"cwd": ".", "prompt": "hello"})
    assert out["cwd"] == str(approved.resolve())
    assert out["sandbox"] and not out["allow_write"] and out["backend"] == "agy"


@pytest.mark.parametrize("override", [
    {"allow_write": True}, {"mode": "execute"}, {"backend": "gemini"},
    {"extra_env": {"HOME": "/root"}}, {"worktree": True}, {"timeout": 1201},
])
def test_remote_override_rejection(approved, override):
    with pytest.raises(ValueError):
        restrict_request({"cwd": ".", **override})


def test_outside_workspace_rejected(approved):
    with pytest.raises(ValueError):
        restrict_request({"cwd": str(approved.parent)})


def test_symlink_escape_rejected(approved, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = approved / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    with pytest.raises(ValueError):
        restrict_request({"cwd": str(link)})


def test_read_only_cannot_start(monkeypatch):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "read-only")
    with pytest.raises(ValueError):
        restrict_request({"prompt": "hello"})
    mcp = FastMCP("test")
    for name in [*READ_TOOLS, "agy_start", "agy_install_skill", "agy_purge"]:
        mcp.add_tool(lambda: "ok", name=name)
    restrict_tools(mcp)
    assert set(mcp._tool_manager._tools) == READ_TOOLS


def test_invalid_profile_fails_closed(monkeypatch):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "typo")
    with pytest.raises(ValueError):
        restrict_request({})


def test_missing_workspace_rejected(monkeypatch):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    monkeypatch.delenv("GUMACU_ALLOWED_WORKSPACE", raising=False)
    with pytest.raises(ValueError):
        restrict_request({})
