
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
    {"extra_env": {"HOME": "/root"}}, {"worktree": True}, {"timeout": 86401},
    {"timeout": 0}, {"timeout": -1},
])
def test_remote_override_rejection(approved, override):
    with pytest.raises(ValueError):
        restrict_request({"cwd": ".", **override})


def test_remote_policy_timeout_gates_and_defaults(approved):
    # 21600 call now accepted (the exact issue reported)
    out = restrict_request({"cwd": ".", "prompt": "long job", "timeout": 21600})
    assert out["timeout"] == 21600

    # 86400 upper limit accepted
    assert restrict_request({"cwd": ".", "prompt": "max job", "timeout": 86400})["timeout"] == 86400

    # Short explicit health check preserved
    assert restrict_request({"cwd": ".", "prompt": "health check", "timeout": 60})["timeout"] == 60

    # DETACHED and LONG tasks default to >=14400s
    assert restrict_request({"cwd": ".", "prompt": "long mode", "mode": "long"})["timeout"] == 14400
    assert restrict_request({"cwd": ".", "prompt": "detached", "detach": True})["timeout"] == 14400

    # Existing short tasks stay short (900s)
    assert restrict_request({"cwd": ".", "prompt": "short task"})["timeout"] == 900


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
    monkeypatch.delenv("GUMACU_ALLOWED_WORKSPACES", raising=False)
    with pytest.raises(ValueError):
        restrict_request({})


def test_multiple_approved_workspaces(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    root1 = tmp_path / "infra"
    root2 = tmp_path / "workspace"
    root1.mkdir()
    root2.mkdir()
    sub1 = root1 / "roles"
    sub1.mkdir()

    delimiter = ";" if pytest.importorskip("os").name == "nt" else ":"
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACES", f"{root1}{delimiter}{root2}")

    out1 = restrict_request({"cwd": str(root1), "prompt": "infra test"})
    assert out1["cwd"] == str(root1.resolve())

    out2 = restrict_request({"cwd": str(root2), "prompt": "workspace test"})
    assert out2["cwd"] == str(root2.resolve())

    out3 = restrict_request({"cwd": str(sub1), "prompt": "roles test"})
    assert out3["cwd"] == str(sub1.resolve())

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="workspace is outside the approved root"):
        restrict_request({"cwd": str(outside), "prompt": "forbidden"})


def test_operator_profile_allows_execute_and_write(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "operator")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))

    out = restrict_request({
        "cwd": str(tmp_path),
        "mode": "execute",
        "allow_write": True,
        "prompt": "run command",
    })
    assert out["mode"] == "execute"
    assert out["allow_write"] is True
    assert out["sandbox"] is False
    assert out["cwd"] == str(tmp_path.resolve())


def test_remote_profile_allows_long_as_monitoring_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))

    out = restrict_request({
        "cwd": str(tmp_path),
        "mode": "long",
        "allow_write": False,
        "prompt": "monitor this task",
    })

    assert out["mode"] == "long"
    assert out["allow_write"] is False
    assert out["sandbox"] is True


def test_operator_still_enforces_workspace_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "operator")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))
    outside = tmp_path.parent / (tmp_path.name + "-unapproved")
    outside.mkdir()

    with pytest.raises(ValueError, match="workspace is outside the approved root"):
        restrict_request({
            "cwd": str(outside),
            "mode": "execute",
            "allow_write": True,
            "prompt": "outside",
        })


def test_operator_token_authorization(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "controlled")
    monkeypatch.setenv("GUMACU_ALLOWED_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GUMACU_OPERATOR_TOKEN", "valid-secret-token")

    # Without token: execute/write must fail
    with pytest.raises(ValueError, match="remote execution/write modes are not enabled"):
        restrict_request({
            "cwd": str(tmp_path),
            "mode": "execute",
            "allow_write": True,
            "prompt": "no token",
        })

    # With invalid token: must fail
    with pytest.raises(ValueError, match="invalid operator token"):
        restrict_request({
            "cwd": str(tmp_path),
            "mode": "execute",
            "allow_write": True,
            "operator_token": "wrong-token",
            "prompt": "wrong token",
        })

    # With valid token: must succeed
    out = restrict_request({
        "cwd": str(tmp_path),
        "mode": "execute",
        "allow_write": True,
        "operator_token": "valid-secret-token",
        "prompt": "authorized task",
    })
    assert out["mode"] == "execute"
    assert out["allow_write"] is True
    assert out["sandbox"] is False


def test_operator_tools_retained(monkeypatch):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "operator")
    mcp = FastMCP("test")
    test_tools = ["agy_status", "agy_read", "agy_result_page", "agy_sessions",
                  "agy_start", "agy_cancel", "agy", "agy_continue", "agy_result", "agy_doctor",
                  "agy_install_skill", "agy_purge"]
    for name in test_tools:
        mcp.add_tool(lambda: "ok", name=name)
    restrict_tools(mcp)
    expected = {"agy_status", "agy_read", "agy_result_page", "agy_sessions",
                "agy_start", "agy_cancel", "agy", "agy_continue", "agy_result", "agy_doctor"}
    assert set(mcp._tool_manager._tools) == expected

