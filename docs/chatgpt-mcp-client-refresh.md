# ChatGPT MCP Tool Refresh & Tunnel Re-Registration Runbook

**Status:** authoritative operational procedure for Gumacu AGY MCP and Netcup/Windows multi-device deployment.  
**Historical Proven Baseline:** October 14, 2026 & October 17, 2026.

---

## 1. Architectural Reality: Where Tool Schemas Are Cached

The Gumacu MCP integration with ChatGPT spans three tiers:
1. **Netcup Host (`159.195.65.10`)**: Runs `/opt/gumacu-agy-mcp/run-mcp.sh` (Python FastMCP server) publishing 26 operator/device tools over JSON-RPC stdio.
2. **Local Windows Tunnel Runtime**: Runs `tunnel-client.exe` (OpenAI Developer Tunnel client) under `gumacu-tunnel-runtime`. It connects outbound to OpenAI's control plane (`tunnel_6ace3926c4e48191aeced4035a6997e5`) and runs `ssh.exe` to Netcup.
3. **OpenAI / ChatGPT Cloud**: Maintains the developer tunnel session and binds tools to chat conversations.

### Why Server/SSH Restarts Alone Do Not Refresh ChatGPT
When the server process on Netcup is restarted or SSH is terminated:
* `tunnel-client.exe` on Windows detects child process termination and restarts `ssh.exe`.
* However, `tunnel-client.exe` **maintains its existing upstream tunnel connection to OpenAI**.
* The initial tool registration handshake (`tools/list`) with OpenAI's tunnel infrastructure happens **only when `tunnel-client.exe` establishes its tunnel session**.
* Therefore, child SSH respawns keep the old in-memory schema cached on the OpenAI tunnel.

---

## 2. Proven Windows Tunnel Refresh Procedure (From 14 Oct 2026)

To force an upstream tool re-registration without modifying files or touching ChatGPT UI:

### Step 1: Terminate Stale Windows Tunnel Process Tree
In local PowerShell on Windows:
```powershell
# Identify and terminate matching stale tunnel processes
Get-Process -Name 'tunnel-client', 'ssh' -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "*gumacu-tunnel-runtime*" -or $_.CommandLine -like "*gumacu-netcup*"
} | Stop-Process -Force
```

### Step 2: Restart the Tunnel via Starter Script
```powershell
# Launch fresh private tunnel
& "C:\Users\TechnoHouse\Documents\Codex\2026-10-13\referenced-chatgpt-conversation-this-is-an\work\gumacu-tunnel-runtime\start-private-tunnel.ps1" `
    -RuntimeDirectory "C:\Users\TechnoHouse\Documents\Codex\2026-10-13\referenced-chatgpt-conversation-this-is-an\work\gumacu-tunnel-runtime" `
    -TunnelId "tunnel_6ace3926c4e48191aeced4035a6997e5"
```
*(Alternatively, if the Scheduled Task `'Gumacu Secure MCP Tunnel Watchdog'` is running, terminating `tunnel-client.exe` triggers automatic restart within 1–2 minutes).*

### Step 3: Verify Local Tunnel Readiness
```powershell
# Check local tunnel readiness endpoint
Invoke-RestMethod -Uri "http://127.0.0.1:49202/health/readiness"
# Expected: status 200, ready: true
```

---

## 3. Server-Side Enhancements (Netcup)

1. **Tools Capability `listChanged: true`**:
   The Netcup FastMCP server now explicitly advertises:
   ```json
   "tools": { "listChanged": true }
   ```
   in `ServerCapabilities` during `initialize` and `server/discover`, signaling to MCP clients that tool definitions can be updated dynamically.
2. **Production Endpoint Verification**:
   Verify live tool listing on Netcup at any time:
   ```bash
   /opt/gumacu-agy-mcp/venv/bin/python -c '
   import subprocess, json
   p = subprocess.Popen(["/opt/gumacu-agy-mcp/run-mcp.sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
   p.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}})+"\n")
   p.stdin.flush(); p.stdout.readline()
   p.stdin.write(json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})+"\n")
   p.stdin.flush(); resp = json.loads(p.stdout.readline()); p.terminate()
   print("Active Tools:", len(resp["result"]["tools"]))
   '
   ```

---

## 4. Diagnosis Matrix

| Observation | Likely next action |
|---|---|
| Repo contains tool, runtime does not | Deploy correct SHA, verify process/runtime paths |
| Runtime doctor lists tool, production `tools/list` does not | Fix MCP export/transport/proxy/deployment; inspect logs and endpoint authorization |
| Production `tools/list` contains tool, ChatGPT lacks it | Terminate matching `tunnel-client.exe` on Windows; restart via `start-private-tunnel.ps1`; open a new chat |
| ChatGPT tool visible but calls fail | Check MCP request transport, schema/auth, gateway, Windows heartbeat, timeout handling |
| Windows Telegram enqueues but not delivered | Inspect terminal events, server-side outbox/dispatcher/retry; do not copy bot secret to Windows |

### Conditional Fallback (When Tunnel is Verified Current)
If the Windows tunnel has been restarted, reports healthy at `http://127.0.0.1:49202/health/readiness`, and new chats still fail to surface new actions, check workspace-managed custom MCP settings:
**Workspace settings → Apps → Action control → Refresh**, then approve new actions.

---

## 5. Required Release Acceptance Gates

1. `SOURCE_OK` — exact source SHA on `main` and passing tests.
2. `RUNTIME_OK` — deployed MCP process SHA/version and clean service health.
3. `TUNNEL_OK` — current `gumacu-netcup` process profile, readiness (`:49202`), new MCP handshake.
4. `PUBLIC_TOOLS_OK` — count and names from the ChatGPT-facing endpoint `tools/list` (26 tools).
5. `CHATGPT_TOOLS_OK` — actual invocation of a changed/new tool from ChatGPT in a new thread.
6. `WINDOWS_E2E_OK` — `agy_devices`, `agy_device_start/status/read/result/continue` round-trip.
7. `TELEGRAM_OK` — verifiable terminal notification delivered through server outbox.
