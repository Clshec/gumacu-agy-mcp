# Gumacu ChatGPT MCP — Tool Visibility and Tunnel Refresh Runbook

**Mandatory check after each MCP tool/schema update.** Record exact runtime and client evidence; do not mark COMPLETE on a GitHub push alone.

## Confirmed historical resolution: 14 October 2026

This installation previously refreshed the **actual ChatGPT-visible MCP version/tools with no manual action in ChatGPT** by restarting its private Windows tunnel runtime cleanly. The problem was stale child processes (a \`tunnel-client.exe\` with matching \`ssh.exe\` and Python process) from an earlier deployment still running. After terminating only those matching old processes and starting the correct \`gumacu-netcup\` tunnel profile, MCP version \`0.1.10\` became visible and \`agy_result_page\` was callable from ChatGPT.

**Relevant components/identifiers (do not expose private paths, tokens, or host addresses in logs):**
- Windows tunnel runtime: \`gumacu-tunnel-runtime\` (confirm *current* path before acting; historical paths may have moved).
- Launcher: \`deploy/start-private-tunnel.ps1\` with the existing RuntimeDirectory and TunnelId obtained from the local deployment config.
- Direct fallback launcher: \`tunnel-client.exe run --profile gumacu-netcup --profile-dir "<runtime>/profiles"\`.
- Local readiness probe used previously: \`http://127.0.0.1:49202/health/readiness\`. Check the current bound port from runtime configuration before probing.
- The \`windows-main\` device SSH reverse tunnel/Gateway and the **ChatGPT/OpenAI private connector tunnel** are conceptually separate: identify which tunnel terminates the ChatGPT MCP session before restarting anything.

### Primary server/tunnel-first recovery

1. Record all running MCP/gateway/tunnel/agent processes, parent PID, executable paths, command lines, listening ports, and service ownership. Confirm which exact binary and profile the ChatGPT connection uses. Back up persistent profile/config and preserve OAuth, sessions, Telegram secrets.
2. Verify **running version**, \`serverInfo.version\`, the authenticated MCP **\`tools/list\` via the *same public/client-facing tunnel***, and compare against \`agy_doctor.server_registered_tools\`. A direct local \`stdio\` MCP invocation with 26 tools is necessary evidence but **does not verify the tunnel**.
3. If a previous or duplicate \`gumacu-netcup\` tunnel-client is still alive, stop **only the matching old process tree** in a controlled fashion (and its dedicated child SSH/Python processes, not system-wide processes). Avoid disruption to unrelated SSH, Rancher, Windows Agent and other services.
4. Restart the canonical \`start-private-tunnel.ps1\` launcher/current profile and verify its process/autostart policy. Only if there is evidence that it is the wrong running binary, reinstall/upgrade the tunnel runtime.
5. Verify tunnel readiness, MCP handshake, actual runtime \`serverInfo.version\`, and complete \`tools/list\` through the external connector path. Check absence of orphan processes and that reconnect persists.
6. Run a harmless ChatGPT tool call using a **newly added tool**, if available. Do not claim that ChatGPT sees a new tool just because the server lists it.

**What NOT to do:** blindly kill every \`ssh.exe\` or \`python.exe\`, restart unrelated cluster services, replace secrets, or present a successful internal doctor as proof that the client refreshed.

### Diagnostic branches

| Evidence | Next action |
|---|---|
| Source updated but running server old | Deploy/restart the canonical MCP runner and re-check version |
| Local MCP \`tools/list\` new but tunnel route returns old version/tools | Fix stale \`gumacu-netcup\` tunnel runtime, wrong target, old child processes, cached routing/profile, then revalidate |
| Client-facing \`tools/list\` new but ChatGPT still old | First confirm the live connection was actually re-established with the new endpoint and check client action permissions/snapshot; avoid claiming tunnel restart always suffices |
| ChatGPT tool visible but calls fail | Check MCP request transport, schema/auth, gateway, Windows heartbeat, timeout handling |
| Windows Telegram enqueues but not delivered | Inspect terminal events, server-side outbox/dispatcher/retry; do not copy bot secret to Windows |

### Product-specific fallback (not the first step for our known tunnel issue)

OpenAI documentation for **workspace-managed custom MCP apps** says approved tool/action lists may be frozen, and eligible Business/Enterprise/Edu admins may use **Workspace settings → Apps → Action control → Refresh**, then approve new actions. For personal/developer or other connection types, control names and behavior may differ. This is a **conditional fallback** when client-facing MCP transport is already verified current but the approved client action snapshot remains stale. It is **not** proof our October 14 tunnel fix was irrelevant, or that every Gumacu update requires user interaction.

Sources:
- https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt
- https://help.openai.com/en/articles/20001497-troubleshooting-plugins-apps-in-chatgpt

## Required release evidence, per update

1. \`SOURCE_OK\` — exact source SHA on \`main\` and tests.
2. \`RUNTIME_OK\` — deployed MCP process SHA/version and correct service restart.
3. \`TUNNEL_OK\` — current \`gumacu-netcup\` process profile, readiness, new MCP handshake + version.
4. \`PUBLIC_TOOLS_OK\` — count and names from the **ChatGPT-facing** endpoint \`tools/list\`; compare with server's registry, not just local stdio.
5. \`CHATGPT_TOOLS_OK\` — actual invocation of a changed/new tool from ChatGPT, or mark **PENDING**, never infer.
6. \`WINDOWS_E2E_OK\` — \`agy_devices\`, \`agy_device_start/status/read/result/continue\` and a harmless Windows round-trip when applicable.
7. \`TELEGRAM_OK\` — verifiable terminal notification delivered through server outbox, without revealing keys.
8. Keep explicit PASS / FAIL / PENDING with evidence/UTC timestamp; don't falsely mark delivery complete.

**Known discrepancy (17 Oct 2026):** \`agy_doctor\` and direct production-process stdio \`tools/list\` listed **26 tools**, whereas the ChatGPT tool inventory in the conversation listed **10**. Need check the Windows private connector/tunnel route after the historical clean-restart technique. Stdio list does not rule out a stale tunnel upstream.

## Automated reminder

The workflow at \`.github/workflows/chatgpt-mcp-refresh-reminder.yml\` posts a GitHub Actions **job summary + warning annotation for every push to main**. This repository's GitHub Issues are disabled, so no workflow may assume it can create issues. The workflow reminds an operator; it does **not** restart production services or claim ChatGPT auto-discovered tools.

**Security:** never commit bot tokens, auth credentials, secret URLs, user-specific paths, or unredacted process command lines.
