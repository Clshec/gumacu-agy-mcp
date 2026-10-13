"""Opt-in, two-turn real agy test through MCP stdio. Never part of CI.

Uses existing agy login; does not enroll or rotate accounts. Run only with
authorization for the Google identity and a dedicated scratch workspace.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(workspace: Path, session_root: Path) -> None:
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agy_mcp.cli"],
        env=dict(os.environ, AGY_MCP_SESSION_ROOT=str(session_root)),
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()

            async def turn(prompt: str, conversation: str | None = None) -> dict:
                response = await client.call_tool("agy_start", {
                    "PROMPT": prompt, "cd": str(workspace), "SESSION_ID": conversation,
                    "backend": "agy", "mode": "ask", "sandbox": True,
                    "allow_write": False, "worktree": False, "timeout": 120,
                })
                data = response.structuredContent
                if not data or not data.get("success") or not data.get("job_id"):
                    raise RuntimeError(f"start failed: {data}")
                job = data["job_id"]
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    status = (await client.call_tool("agy_status", {"job_id": job})).structuredContent
                    if status and status.get("record", {}).get("status") != "running":
                        break
                    await asyncio.sleep(2)
                else:
                    await client.call_tool("agy_cancel", {"job_id": job})
                    raise RuntimeError("test job exceeded deadline; cancellation requested")
                pieces, offset, digest = [], 0, None
                while True:
                    page = (await client.call_tool("agy_result_page", {
                        "job_id": job, "offset": offset, "limit": 7,
                        "expected_sha256": digest,
                    })).structuredContent
                    if not page or not page.get("success"):
                        raise RuntimeError(f"page failed: {page}")
                    pieces.append(page["text"])
                    offset, digest = page["next_offset"], page["sha256"]
                    if not page["has_more"]:
                        break
                return {"job_id": job, "session_id": page["session_id"],
                        "text": "".join(pieces), "sha256": digest,
                        "status": status["record"]["status"]}

            first = await turn(
                "Do not use tools, commands, files, network, or plugins. "
                "Remember the test word ORCHID for this conversation. "
                "Reply only with GUMACU_SMOKE_OK."
            )
            print(json.dumps({"turn": 1, **first}, ensure_ascii=False), flush=True)
            if first["status"] != "completed" or "GUMACU_SMOKE_OK" not in first["text"]:
                raise RuntimeError("first test turn failed")
            if not first["session_id"]:
                raise RuntimeError("agy did not expose a resumable session ID")
            second = await turn(
                "Do not use tools, commands, files, network, or plugins. "
                "What test word did I ask you to remember in the previous turn? "
                "Reply with that word only.", first["session_id"],
            )
            print(json.dumps({"turn": 2, **second}, ensure_ascii=False), flush=True)
            if second["status"] != "completed" or "ORCHID" not in second["text"]:
                raise RuntimeError("session continuation failed")
            if second["session_id"] != first["session_id"]:
                raise RuntimeError("session identity changed")
            print("MCP_TWO_TURN_SMOKE_PASSED", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.workspace.resolve(), args.session_root.resolve()))


if __name__ == "__main__":
    main()
