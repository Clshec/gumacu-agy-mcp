# Gumacu multi-device Antigravity (Linux + Windows)

This feature adds an OPTIONAL private broker and native Windows/Linux worker to the existing Antigravity MCP. Existing Linux agy_* tools remain unchanged and are the default. Remote tools use agy_device_*.

Architecture: ChatGPT -> existing authenticated MCP -> private Netcup loopback broker with SQLite -> outbound SSH tunnel from Windows -> Windows worker -> native Windows agy CLI. Each node runs its own local CLI, with its own login and private workspace allowlist. No public HTTP/Windows inbound listener is required.

Security: broker only binds 127.0.0.1; SSH provides authenticated/encrypted forwarding. Set AGY_DEVICE_ADMIN_TOKEN (random >=32 characters) as a PRIVATE environment secret on Linux, shared only by the gateway and existing MCP process. Never put it on Windows. Give each node a DIFFERENT random AGY_DEVICE_TOKEN (>=32 characters), and store only its SHA256 digest in an operator-owned devices.json. Broker config shape:

    {"devices":[{"id":"windows-main","os":"Windows",
      "token_sha256":"<sha256>",
      "allowed_actions":["start","status","read","result","cancel","sessions","doctor"]}]}

To hash a node secret without echoing it:
    python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass('Device token: ').encode()).hexdigest())"

LINUX GATEWAY: Install pinned GitHub version into a dedicated venv, create a private persistent directory with strict permissions and use a non-root service account. Launch:
    python -m agy_mcp.device_gateway --db /var/lib/gumacu-agy-devices/broker.sqlite --devices /etc/gumacu-agy-devices/devices.json --port 8766
Provide AGY_DEVICE_ADMIN_TOKEN in gateway secret-store environment. On the EXISTING MCP service set AGY_DEVICE_GATEWAY_URL=http://127.0.0.1:8766 and provide the SAME admin token securely. Restart the existing service only after backup, preserving the SSH/tunnel connector and previous environment for rollback.

WINDOWS WORKER:
1. Install and authenticate native Google Antigravity CLI as the signed-in Windows user; verify agy --version and a harmless local prompt.
2. Clone the GitHub project and run uv sync (Python >=3.11). Do not use an SSH-executed Linux agy as a substitute for the native Windows CLI.
3. Store the separately generated Windows node token securely, inject it as AGY_DEVICE_TOKEN into worker environment; also set AGY_NODE_PROFILE=controlled (read-only by default) and GUMACU_ALLOWED_WORKSPACES to an absolute approved Windows path (multiple roots delimited by semicolons, e.g. C:\Work\Gumacu;D:\Projects).
4. Establish Windows->Linux OUTBOUND private SSH forwarding:
    ssh -N -L 127.0.0.1:8766:127.0.0.1:8766 linux-ssh-user@YOUR-SERVER
   Verify SSH host keys and use a restricted SSH identity. NEVER expose port 8766 publicly.
5. As the same Windows user who authenticated agy, run:
    uv run python -m agy_mcp.device_agent --device-id windows-main --gateway http://127.0.0.1:8766 --state "$env:LOCALAPPDATA\Gumacu\device-agent.sqlite"
   Ctrl+C stops worker. The SSH tunnel is managed separately. Only schedule unattended execution under that Windows user after securely providing its secret (never plaintext in a scheduled task command line). Do not use LocalSystem (cannot access user CLI login).

TO USE FROM CHATGPT:
- Existing Linux: agy_start, agy_status, agy_read and agy_result remain unchanged.
- agy_devices lists enrolled remote hosts and heartbeat online state.
- agy_device_doctor(device_id="windows-main") checks native agy readiness.
- agy_device_start(device_id="windows-main",PROMPT="Inspect project",cwd="C:\\Work\\Gumacu")
- agy_device_status/read/result/cancel all require device_id and job_id.
- agy_device_continue requires device_id, SESSION_ID and PROMPT. Known sessions are pinned to their first device; cross-device continuation is rejected.
- If a remote response is delayed, agy_device_call(request_id) retrieves it. DO NOT reissue a write job just because the response was delayed.

FEATURES/LIMITATIONS:
Device agent only runs explicitly enumerated agy-mcp tools; no arbitrary shell execution is forwarded. Workspace and execution permission checks use the existing remote_policy controlled/controlled-write profiles. Default controlled forbids writes; controlled-write grants headless writing only when explicitly enabled on that node. Request IDs and per-node job IDs are distinct, and session affinity is stored in SQLite. Leased requests are NOT silently replayed after agent crashes to avoid duplicated effects; investigate and resolve stuck leased jobs manually. Completion responses are cached locally by the agent. Broker state DB contains user prompts/results and must have strictly restricted file permissions and backups. Agent results capped at 1 MB. This is NOT GUI Computer Use, nor guaranteed real-time notifications for remote nodes.

QA/ROLLBACK: run pytest -q tests/test_device_gateway.py on Linux and Windows, compare legacy full test suite, then test agy_devices heartbeat and a harmless read-only remote run before enabling writes. Back up production environment and broker DB; stop broker/agent and remove new AGY_DEVICE_GATEWAY_* configuration to roll back without changing existing Linux tool implementations or session store.
# Direct Windows tools (optional)

For ChatGPT to operate Windows itself without delegating to Antigravity, use
the [Windows-MCP tunnel integration](../integrations/windows-mcp/README.md).
It preserves Linux tools and adds native Windows tools behind the same private
tunnel, including the stable `agy_capabilities` / `agy_dispatch` facade.
The legacy Windows Antigravity agent is a separate execution mode and need not
run alongside this integration. The optional integration is installed from the
repository checkout and has its own Python 3.14 environment.
