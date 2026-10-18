"""Unit tests for deploy/refresh-private-tunnel.ps1."""

from pathlib import Path


def test_refresh_private_tunnel_script_exists_and_structure():
    script_path = Path(__file__).resolve().parents[1] / "deploy" / "refresh-private-tunnel.ps1"
    assert script_path.is_file(), "deploy/refresh-private-tunnel.ps1 must exist"
    content = script_path.read_text(encoding="utf-8")

    # Parameters
    assert "param(" in content
    assert "$RuntimeDirectory" in content
    assert "$TunnelId" in content
    assert "$Alias = 'gumacu-netcup'" in content
    assert "$ReadinessTimeoutSec" in content

    # Validation regexes
    assert r"^tunnel_[a-zA-Z0-9_-]+$" in content
    assert r"^[a-zA-Z0-9_-]+$" in content

    # Process filtering safety
    assert "Win32_Process" in content
    assert "tunnel-client.exe" in content
    assert "run-mcp.sh" in content
    assert "Stop-Process" in content

    # Health check
    assert "49202/health/readiness" in content
    assert "Invoke-RestMethod" in content


def test_refresh_private_tunnel_matches_starter_contract():
    repo_root = Path(__file__).resolve().parents[1]
    refresh_path = repo_root / "deploy" / "refresh-private-tunnel.ps1"
    starter_path = repo_root / "deploy" / "start-private-tunnel.ps1"

    assert starter_path.is_file()
    refresh_code = refresh_path.read_text(encoding="utf-8")

    # Default tunnel ID should match
    assert "tunnel_6ace3926c4e48191aeced4035a6997e5" in refresh_code
    # Target invocations
    assert "start-private-tunnel.ps1" in refresh_code
