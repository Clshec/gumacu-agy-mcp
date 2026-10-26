"""Exercise the installed bridge over MCP without touching existing user files."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(config: Path, output: Path):
    config, output = config.resolve(), output.resolve()
    bridge_path = Path(__file__).with_name("bridge.py")
    params = StdioServerParameters(command=sys.executable,
        args=[str(bridge_path), "--config", str(config)],
        env={"PYTHONUTF8": "1", "ANONYMIZED_TELEMETRY": "false"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert {"agy_capabilities", "agy_dispatch", "windows_PowerShell"} <= names
            caps = await session.call_tool("agy_capabilities", {})
            catalog = await session.call_tool("agy_dispatch", {"device_id": "windows-main", "action": "windows_catalog"})
            windows = catalog.structuredContent
            if not windows:
                windows = json.loads(catalog.content[0].text)
            powershell = next(t for t in windows["tools"] if t["name"] == "PowerShell")
            props = powershell["inputSchema"]["properties"]
            field = next(k for k in ("command", "script") if k in props)
            command = "[pscustomobject]@{Computer=$env:COMPUTERNAME;User=$env:USERNAME;PowerShell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress"
            direct = await session.call_tool("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "PowerShell", "arguments": {field: command}}})
            if direct.isError:
                print(json.dumps(direct.model_dump(exclude_none=True), ensure_ascii=False), flush=True)
            assert not direct.isError
            assert "Status Code: 0" in json.dumps(direct.model_dump())
            proof = output.parent / "temporary-windows-mcp-proof.txt"
            assert not proof.exists(), "Do not overwrite existing proof files"
            quoted = str(proof).replace("'", "''")
            file_command = f"$p='{quoted}'; try {{ [IO.File]::WriteAllText($p,'gumacu-windows-mcp-ok'); if([IO.File]::ReadAllText($p) -ne 'gumacu-windows-mcp-ok'){{throw 'Verification failed'}}; 'WINDOWS_MCP_FILE_VERIFIED' }} finally {{ if(Test-Path -LiteralPath $p){{Remove-Item -LiteralPath $p}} }}"
            files = await session.call_tool("agy_dispatch", {"device_id": "windows-main", "action": "windows_call", "arguments": {"name": "PowerShell", "arguments": {field: file_command}}})
            assert not files.isError
            assert "WINDOWS_MCP_FILE_VERIFIED" in json.dumps(files.model_dump())
            assert "Status Code: 0" in json.dumps(files.model_dump())
            assert not proof.exists()
            result = {"tool_count": len(tools), "windows_tools": [t["name"] for t in windows["tools"]],
                      "capabilities": caps.model_dump(exclude_none=True),
                      "read_only_command": direct.model_dump(exclude_none=True),
                      "temporary_file_write_read_delete": files.model_dump(exclude_none=True)}
            output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"success": True, "tool_count": len(tools), "windows_tool_count": len(windows["tools"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.config, args.output))
