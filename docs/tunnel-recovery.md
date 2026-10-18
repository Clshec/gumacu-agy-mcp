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
