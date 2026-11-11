# Connection diagnostics

Keep transport readiness, MCP initialization, catalog discovery, SSH authentication,
and command exit status separate. A healthy local runtime alone does not prove
that a ChatGPT request reached the server or that a command succeeded.

The 2026-10-31 read-only inspection found:

* The connected host integration could discover Linux and Windows actions and run
  read-only commands on both devices. A fresh direct SSH connection also worked.
* The running Linux checkout was `2b7d96b`, while GitHub main was `6c24c6d`.
  The installed launcher unconditionally sourced Telegram and device-gateway env
  files. Main's newer launcher had made those files optional. The new Alf Control
  launcher removes that dependency entirely.
* The Windows bridge already preserved PROGRAMDATA for OpenSSH and opened a fresh
  initialized Linux MCP transport per operation. These connection fixes are kept.
* The original conversation supplied no exact refusal error or timestamp. Its
  root cause cannot be proven from the available evidence. The current refusal
  was not reproduced, so a historic incident must not be marked fixed merely
  because today's checks pass.

If a future failure occurs, collect the exact tool/error and time. Inspect the
local runtime's MCP health (not just readiness), then test a newly initialized
connection, catalog and harmless command. Check the selected SSH host/key/known
hosts and the sidecar's private listener. Never disable host-key verification or
automatically replay an uncertain command to diagnose connectivity.

`alf_doctor` probes configured SSH catalog transports, not client visibility or
target command authentication. The server cannot independently certify that a
specific ChatGPT UI has refreshed its cached tool manifest. Run a command through
that host to establish end-to-end evidence.

## Discovery probe timeouts (2026-11-11)

### Cause and evidence

The client attempted `server/discover`, a discovery method from the newer MCP
protocol. The Windows bridge uses MCP 1.x, which expects a legacy `initialize`
handshake. Its resume wrapper previously injected an internal initialization
before forwarding the discovery probe to the old request parser. The parser
rejected the unsupported method; the client could not complete useful negotiation
and tool requests timed out.

The local log contained validation errors with
`input_value='server/discover'` and expected methods such as `tools/list` or
`tools/call`. ChatGPT reported a tunnel connection timeout. Meanwhile, local
runtime readiness and MCP child status both reported healthy/running. Direct SSH
and the Linux sidecar catalog worked, so restarting the Linux sidecar alone would
not address this failure.

The fixed bridge returns JSON-RPC error `-32601` (method not found) directly for
`server/discover`, keeping its request ID. It consumes the probe before the old
parser and awaits the client's legacy `initialize` handshake. A discovery probe
received after initialization also leaves the active session intact. This is a
legacy compatibility path, not implementation of the newer MCP protocol. Clients
that require only the newer protocol need a future SDK/server migration.

The official [MCP Python SDK negotiation guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md)
describes discovery probing and fallback to legacy initialization.

### Recognition and recovery

1. Record the failing tool, exact error and timestamp. Match the active tunnel
   profile and its configured bridge command to the running process.
2. Inspect the tunnel log around that timestamp for the `server/discover`
   validation signature. Locate the log through the active profile or
   `tunnel-client runtimes status <alias> --json`; avoid sharing profile secrets.
3. Check readiness and the MCP health endpoint, but also call `alf_capabilities`
   from ChatGPT. A running process or healthy local endpoint cannot prove this
   negotiation works.
4. Install the fixed bridge in a versioned release directory. Back up the active
   profile, update its MCP command to the new bridge, and retain its backend
   configuration and desktop ownership policy. Updating source files alone does
   not reload an already running process.
5. Stop the affected managed alias with `tunnel-client runtimes stop <alias> --json`
   and run the installation's official `ensure-private-tunnel.ps1` or wrapper
   with its existing runtime directory and tunnel ID. Use the PowerShell host
   that can load `Microsoft.PowerShell.Security` and decrypt the operator's saved
   key. If startup fails, inspect `watchdog/watchdog.log`, restore the backed-up
   profile and start the previous release. Do not print or replace the API key.
6. From ChatGPT, call `alf_capabilities`, then `windows_catalog` on `windows-main`.
   Use the returned PowerShell schema to execute a harmless command such as
   `Get-Date -Format o`. Require a successful result and command exit status,
   then call `alf_doctor`. Never replay an uncertain write or desktop action.

The incident was resolved only after a real ChatGPT Windows PowerShell call
succeeded and `alf_doctor` reported both devices online. The initial
`MCP server is not initialized` report was not independently proven to have the
same cause; use the discovery log signature to distinguish this incident.

### Regression protection and monitoring limits

`tests/test_windows_bridge.py` covers discovery before initialization, discovery
after initialization, unchanged legacy initialization, and resumed-session
bootstrap. It also covers Windows transport reuse/recovery and non-replay of
failed actions. Run `uv run pytest tests/test_windows_bridge.py -q` after changes
to the bridge, SDK pins or tunnel client, followed by a real ChatGPT connection
check after deployment.

The existing watchdog checks runtime and child health. It can miss a live process
with a protocol incompatibility, as this incident demonstrated. Regression tests
and an end-to-end post-deployment check protect this specific case; they cannot
guarantee compatibility with every future client. Repeated discovery errors need
investigation rather than a restart loop. No automatic ChatGPT-side monitoring
has been added by this fix.
