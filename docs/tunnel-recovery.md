# Private tunnel restart recovery

The Windows tunnel may resume a cached ChatGPT MCP session after its SSH stdio
child has restarted. The new Python MCP session has never received `initialize`;
tool calls then return `Invalid request parameters`, with the server warning
`Received request before initialization was complete`. Tunnel liveness and
readiness alone do not detect this protocol failure.

Install `deploy/run-mcp.sh` as `/opt/gumacu-agy-mcp/run-mcp.sh` together with the
package. It invokes `agy_mcp.stdio_reconnect`. A normal client's initialize is
passed through. If the first resumed request is not initialize, the adapter
initializes the new child, validates the response, sends initialized, then
forwards the original request exactly once. It never retries tool operations.
Initialization failure closes the child and fails closed.

Keep the Windows scheduled watchdog installed using
`deploy/install-private-tunnel-watchdog.ps1`. Run it as the credential-owning
interactive user, never SYSTEM. It recovers a stopped runtime; the adapter
repairs initialization after recovery. Neither component launches Telegram.
`gumacu-agy-telegram.service` remains the single Telegram poller.

Acceptance: test both initialized and resumed first requests; restart the
managed tunnel, then call `agy_doctor`, `agy_devices`, and a harmless
`agy_start(timeout=0)` through the registered connector. Verify Windows online
and the unchanged Telegram service PID. An initial in-flight request during
restart can time out and is deliberately not replayed.

## Linux-only MCP after Windows Antigravity agent removal (October 2026)

**Separate three independent parts:**
1. `Windows Antigravity device agent` is optional. Unenrolling it must not prevent
   Linux-local `agy_doctor`, `agy_sessions`, or `agy` from working.
2. `Device Gateway` is optional for Linux-local MCP methods. If
   `/etc/gumacu-agy-devices/gateway.env` was removed with the Windows agent,
   the original `deploy/run-mcp.sh` exited at startup due to `set -eu`
   and an unconditional shell dot-source. The repaired launcher sources
   optional credentials **only when files exist**, but refuses unreadable
   present files. Remote-device tools fail closed if no gateway is configured.
3. The **private ChatGPT MCP transport** may still be hosted by a managed
   tunnel-client on Windows. Deleting that tunnel runtime, SSH credentials or
   its watchdog removes ChatGPT's connection even when Linux MCP works.
   Rebuild/restore that transport separately or migrate to a supported Linux
   connector tunnel; do not confuse it with the optional Antigravity device agent.

### Non-destructive verification on Netcup (after deployment)

Record original revision and launcher path, back up the existing launch
script, do not print loaded env contents, and use the production Python venv.

```sh
test -x /opt/gumacu-agy-mcp/venv/bin/python
test -r /opt/gumacu-agy-mcp/run-mcp.sh
sh -n /opt/gumacu-agy-mcp/run-mcp.sh
/opt/gumacu-agy-mcp/venv/bin/python -m agy_mcp.doctor
```

A missing `gateway.env` should NOT stop the launcher. A present-but-unreadable
file should stop it. Keep the main MCP session-store path unchanged.
Do not install Windows tooling, rotate credentials, or restart the Telegram
poller to recover the Linux MCP tools.

Check the active managed tunnel runtime's reported `process_running`,
`healthy` and `ready`. For an existing Windows-managed private tunnel,
use its credential-owning user's **managed** runtime stop/start (not a broad
process kill), retaining the same tunnel ID, SSH identity and profile.
Inspect the actual profile command: it must call
`/opt/gumacu-agy-mcp/run-mcp.sh` which starts
`python -m agy_mcp.stdio_reconnect`, not the stale direct CLI process.
A fresh MCP client may require refreshing the netcup connection's tools list.

### MCP-native acceptance checklist

Run `agy_doctor`, `agy_sessions` and `agy_capabilities` from ChatGPT.
With the optional Device Gateway removed, `agy_capabilities` should still
report a Linux-local device and `gateway_available: false`, while remote
device calls report a structured denial. A remote-device outage MUST NOT
break Linux-local tools or the MCP transport. If **all** MCP methods fail with
the same transport-level `INVALID_ARGUMENT` or an empty text error, inspect
the tunnel's stderr and server launch failure BEFORE debugging Antigravity CLI
OAuth. If the child logs `Received request before initialization was complete`,
confirm the `stdio_reconnect` wrapper and restart ONLY the managed tunnel
transport, retaining existing session storage.

Git deployment alone is insufficient. The fixed launcher must be installed
on Netcup in the active `/opt/gumacu-agy-mcp/run-mcp.sh` and the managed
tunnel child relaunched before calling the incident resolved.
