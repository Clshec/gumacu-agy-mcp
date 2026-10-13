"""Read-only MCP check through the reviewed SSH stdio configuration."""
import argparse
import asyncio
import json
import tomllib
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(config: Path, job_id: str) -> None:
    target = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["gumacu_netcup"]
    parameters = StdioServerParameters(
        command=target["command"], args=target["args"], env=target.get("env"),
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert "agy_result_page" in {t.name for t in tools.tools}
            if len(tools.tools) == 4:
                blocked = await client.call_tool("agy_start", {"PROMPT": "must not run"})
                if not blocked.isError:
                    raise RuntimeError("read-only endpoint unexpectedly accepted agy_start")
            result = (await client.call_tool("agy_result_page", {
                "job_id": job_id, "offset": 0, "limit": 16000,
            })).structuredContent
            if not result or not result.get("success"):
                raise RuntimeError(f"MCP result retrieval failed: {result}")
            print(json.dumps({"ssh_mcp_verified": True, "tools": len(tools.tools),
                              "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.config, args.job_id))
