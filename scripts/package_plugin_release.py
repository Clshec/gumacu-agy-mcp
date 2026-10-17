#!/usr/bin/env python3
"""Automation script to verify Netcup MCP health and package ChatGPT Plugin releases.

Usage:
    python scripts/package_plugin_release.py [--plugin-version 1.0.1] [--verify-remote]
"""

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PLUGIN_DIR = Path.home() / ".codex" / "plugins" / "cache" / "created-by-me-remote" / "dev-6acf79599a7c8191a7eb5b52b4d30c51"
DIST_DIR = REPO_ROOT / "dist"

PLUGIN_ID = "plugin_asdk_app_6acf79599a7c8191a7eb5b52b4d30c51"
APP_ID = "asdk_app_6acf79599a7c8191a7eb5b52b4d30c51"
INTERNAL_NAME = "dev-6acf79599a7c8191a7eb5b52b4d30c51"


def get_local_mcp_version() -> str:
    pyproject = REPO_ROOT / "pyproject.toml"
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if line.startswith("version ="):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "unknown"


def verify_remote_mcp(expected_version: str) -> bool:
    print(f"[*] Verifying Netcup remote MCP version (expecting {expected_version})...")
    ssh_script = """
import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    server_params = StdioServerParameters(
        command='/opt/gumacu-agy-mcp/venv/bin/python',
        args=['-m', 'agy_mcp.cli'],
        env={
            'GUMACU_REMOTE_PROFILE': 'operator',
            'GUMACU_ALLOWED_WORKSPACES': '/root/gumacu-infrastructure',
            'AGY_MCP_SESSION_ROOT': '/var/lib/gumacu-agy-mcp/sessions',
        }
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            doctor_res = await session.call_tool('agy_doctor', {})
            import json
            data = json.loads(doctor_res.content[0].text)
            print("INIT_VER:", init_res.serverInfo.version)
            print("DOCTOR_VER:", data.get("version"))
            print("HEALTHY:", data.get("report", {}).get("healthy"))

asyncio.run(main())
"""
    ssh_cmd = [
        "C:/Windows/System32/OpenSSH/ssh.exe",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "159.195.65.10",
        "/opt/gumacu-agy-mcp/venv/bin/python",
        "-",
    ]
    try:
        proc = subprocess.run(
            ssh_cmd,
            input=ssh_script,
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        out = proc.stdout
        print(f"    Remote response:\n    {out.strip()}")
        if f"DOCTOR_VER: {expected_version}" in out and "HEALTHY: True" in out:
            print("[+] Netcup MCP verification PASSED.")
            return True
        else:
            print("[-] Version mismatch or unhealthy report on Netcup.")
            return False
    except Exception as exc:
        print(f"[-] Failed to probe remote MCP: {exc}")
        return False


def build_plugin_files(version: str, target_dir: Path):
    target_dir.mkdir(parents=True, exist_ok=True)
    codex_plugin_dir = target_dir / ".codex-plugin"
    codex_plugin_dir.mkdir(parents=True, exist_ok=True)

    app_json = {
        "apps": {
            INTERNAL_NAME: {
                "id": APP_ID
            }
        }
    }
    (target_dir / ".app.json").write_text(json.dumps(app_json, indent=2) + "\n", encoding="utf-8")

    legacy_plugin_json = {
        "apps": "./.app.json",
        "author": {
            "name": "App developer"
        },
        "description": "netcup",
        "interface": {
            "capabilities": [],
            "category": "Other",
            "defaultPrompt": None,
            "developerName": "App developer",
            "displayName": "netcup",
            "longDescription": "netcup",
            "shortDescription": "netcup"
        },
        "name": INTERNAL_NAME,
        "version": version
    }
    (codex_plugin_dir / "plugin.json").write_text(json.dumps(legacy_plugin_json, indent=2) + "\n", encoding="utf-8")

    root_plugin_json = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": INTERNAL_NAME,
        "version": version,
        "description": "netcup",
        "author": {
            "name": "App developer"
        },
        "extensions": {
            "com.openai": {
                "apps": "./.app.json",
                "interface": {
                    "displayName": "netcup",
                    "shortDescription": "netcup",
                    "longDescription": "netcup",
                    "developerName": "App developer",
                    "category": "Other",
                    "capabilities": [],
                    "defaultPrompt": None
                }
            }
        }
    }
    (target_dir / "plugin.json").write_text(json.dumps(root_plugin_json, indent=2) + "\n", encoding="utf-8")
    print(f"[+] Prepared plugin manifests in {target_dir}")


def make_zip(source_dir: Path, output_zip: Path):
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(source_dir):
            for file in sorted(files):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, source_dir).replace("\\", "/")
                zf.write(full_path, rel_path)
    print(f"[+] Created release ZIP: {output_zip} ({output_zip.stat().st_size} bytes)")


def main():
    parser = argparse.ArgumentParser(description="Package ChatGPT Plugin release.")
    parser.add_argument("--plugin-version", default="1.0.1", help="Target ChatGPT Plugin version (e.g. 1.0.1)")
    parser.add_argument("--verify-remote", action="store_true", help="Probe Netcup server before building")
    args = parser.parse_args()

    mcp_ver = get_local_mcp_version()
    print(f"[*] Current MCP Server version: {mcp_ver}")
    print(f"[*] Target Plugin version: {args.plugin_version}")

    if args.verify_remote:
        ok = verify_remote_mcp(mcp_ver)
        if not ok:
            print("[!] Halting packaging because remote MCP verification failed.")
            sys.exit(1)

    # 1. Update repo root files
    build_plugin_files(args.plugin_version, REPO_ROOT)

    # 2. Update local Codex cache
    version_cache_dir = CACHE_PLUGIN_DIR / args.plugin_version
    build_plugin_files(args.plugin_version, version_cache_dir)

    # 3. Create release zip
    zip_path = DIST_DIR / f"netcup-plugin-{args.plugin_version}.zip"
    make_zip(version_cache_dir, zip_path)

    print("\n[SUCCESS] Plugin release artifacts ready:")
    print(f"  Archive: {zip_path}")
    print(f"  Mapping: Plugin {args.plugin_version} -> MCP Server {mcp_ver}")


if __name__ == "__main__":
    main()
