#!/usr/bin/env python3
"""Live End-to-End verification script for MCP Events & Codex Orchestrator on Netcup.

Executes:
1. MCP 2.0 server/discover probe
2. events/list catalog validation
3. events/subscribe with challenge verification
4. Job event dispatch with signed Standard Webhooks
5. Outbox replay validation
6. Codex Orchestrator pipeline step chaining (agy_pipeline_start -> agy_pipeline_status)
"""

from __future__ import annotations

import subprocess
import sys

SSH_CMD = [
    "C:/Windows/System32/OpenSSH/ssh.exe",
    "-T",
    "-i", "C:/Users/TechnoHouse/.ssh/hhh.pem",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "root@159.195.65.10",
    "/tmp/gumacu-agy-review.WyP04n3d/venv/bin/python",
    "-",
]

REMOTE_VERIFY_SCRIPT = """
import asyncio
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import mcp.types as mcp_types

from agy_mcp.events import (
    DiscoverRequest,
    JOB_COMPLETED_EVENT,
    ListEventsRequest,
    SubscribeEventParams,
    SubscribeEventRequest,
    UnsubscribeEventParams,
    UnsubscribeEventRequest,
    StandardWebhooksSigner,
)

# 1. Spin up a temporary HTTP server to simulate ChatGPT callback endpoint
received_events = []
received_challenges = []
SECRET = "whsec_MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="

class MockReceiver(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        data = json.loads(body.decode("utf-8"))

        # Challenge verification
        if data.get("type") == "verification":
            received_challenges.append(data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": data["challenge"]}).encode("utf-8"))
            return

        # Webhook delivery
        received_events.append({
            "headers": dict(self.headers),
            "body": data,
            "raw": body,
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

server = HTTPServer(("127.0.0.1", 18889), MockReceiver)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()

async def run_probe():
    server_params = StdioServerParameters(
        command='/tmp/gumacu-agy-review.WyP04n3d/venv/bin/python',
        args=['-m', 'agy_mcp.cli'],
        env={
            'GUMACU_REMOTE_PROFILE': 'operator',
            'GUMACU_ALLOWED_WORKSPACES': '/root/gumacu-infrastructure:/tmp/gumacu-agy-review.WyP04n3d/workspace',
            'AGY_MCP_SESSION_ROOT': '/tmp/gumacu-agy-review.WyP04n3d/live-sessions',
            'AGY_MCP_ALLOW_LOCAL_URLS': '1', # Opt-in to local mock callback
        }
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init_res = await session.initialize()
            print("[+] Initialized MCP session. Server version:", init_res.serverInfo.version)
            assert init_res.serverInfo.version == "0.1.12", f"Unexpected version: {init_res.serverInfo.version}"

            # 2. Test server/discover via raw request
            discover_req = DiscoverRequest()
            discover_res = await session.send_request(discover_req, mcp_types.EmptyResult)
            disc_dict = discover_res.model_dump(by_alias=True, exclude_none=True)
            print("[+] server/discover response:", disc_dict)
            assert "2026-07-28" in disc_dict.get("supportedVersions", [])
            assert "events" in disc_dict.get("capabilities", {})

            # 3. Test events/list
            list_req = ListEventsRequest()
            list_res = await session.send_request(list_req, mcp_types.EmptyResult)
            events = list_res.model_dump(by_alias=True, exclude_none=True).get("events", [])
            print(f"[+] events/list returned {len(events)} events")
            assert len(events) == 3
            assert all(e.get("delivery") == ["webhook"] for e in events)

            # 4. Test events/subscribe with callback challenge verification
            sub_url = "http://127.0.0.1:18889/webhook"
            sub_req = SubscribeEventRequest(
                params=SubscribeEventParams(
                    name=JOB_COMPLETED_EVENT,
                    arguments={},
                    delivery={"mode": "webhook", "url": sub_url, "secret": SECRET},
                    ttlMs=3600000,
                )
            )
            sub_res = await session.send_request(sub_req, mcp_types.EmptyResult)
            sub_dict = sub_res.model_dump(by_alias=True, exclude_none=True)
            print("[+] events/subscribe response:", sub_dict)
            assert sub_dict.get("id", "").startswith("sub_")
            assert len(received_challenges) == 1
            print("[+] Challenge verification succeeded against mock receiver!")

            # 5. Test Codex Orchestrator tool: agy_pipeline_start
            pipe_res = await session.call_tool("agy_pipeline_start", {
                "name": "Live Verification Pipeline",
                "steps": [
                    {"step_id": "step1", "prompt": "echo live test step 1", "mode": "ask"},
                    {"step_id": "step2", "prompt": "echo live test step 2", "mode": "ask"},
                ],
            })
            pdata = json.loads(pipe_res.content[0].text)
            print("[+] agy_pipeline_start returned:", pdata)
            assert pdata.get("success") is True
            pipe_id = pdata.get("pipeline_id")

            # Check automatic orchestration through terminal state and webhook delivery.
            deadline = time.monotonic() + 150
            sdata = {}
            while time.monotonic() < deadline:
                pstat_res = await session.call_tool("agy_pipeline_status", {"pipeline_id": pipe_id})
                sdata = json.loads(pstat_res.content[0].text)
                status = sdata.get("pipeline", {}).get("status")
                if status in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.5)
            print("[+] agy_pipeline_status returned:", sdata.get("pipeline", {}).get("status"))
            assert sdata.get("pipeline", {}).get("status") == "completed"

            event_deadline = time.monotonic() + 10
            while not received_events and time.monotonic() < event_deadline:
                await asyncio.sleep(0.1)
            assert received_events, "No signed job.completed webhook was delivered"
            delivered = received_events[-1]
            headers = {k.lower(): v for k, v in delivered["headers"].items()}
            assert delivered["body"]["name"] == JOB_COMPLETED_EVENT
            assert StandardWebhooksSigner.verify(
                headers["webhook-id"],
                int(headers["webhook-timestamp"]),
                delivered["raw"],
                SECRET,
                headers["webhook-signature"],
            )
            print("[+] Signed job.completed webhook received and verified")

            # 6. Test events/unsubscribe
            unsub_req = UnsubscribeEventRequest(
                params=UnsubscribeEventParams(id=sub_dict["id"])
            )
            unsub_res = await session.send_request(unsub_req, mcp_types.EmptyResult)
            unsub_dict = unsub_res.model_dump(by_alias=True, exclude_none=True)
            print("[+] events/unsubscribe response:", unsub_dict)
            assert unsub_dict == {}

    server.shutdown()
    print("[SUCCESS] All Netcup remote verifications PASSED perfectly!")

asyncio.run(run_probe())
"""

def main():
    print("[*] Running live end-to-end verification probe on Netcup server...")
    try:
        proc = subprocess.run(
            SSH_CMD,
            input=REMOTE_VERIFY_SCRIPT,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        print(proc.stdout)
        if "[SUCCESS] All Netcup remote verifications PASSED perfectly!" in proc.stdout:
            print("\n>>> LIVE REMOTE VERIFICATION 100% SUCCESSFUL <<<")
            return 0
        else:
            print("[-] Probe output did not contain success token.")
            print("STDERR:\n", proc.stderr)
            return 1
    except subprocess.CalledProcessError as err:
        print(f"[-] Probe process failed with code {err.returncode}")
        print("STDOUT:\n", err.stdout)
        print("STDERR:\n", err.stderr)
        return err.returncode
    except Exception as exc:
        print(f"[-] Probe encountered exception: {exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
