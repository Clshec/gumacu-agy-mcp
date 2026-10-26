# Native Windows tools in the existing Gumacu tunnel

This optional Windows host integration combines the existing Linux Gumacu MCP
backend with [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP).
ChatGPT calls native tools directly. Antigravity is not involved in Windows work.
Windows-MCP is an unmodified MIT-licensed dependency, not vendored source.
It is pinned to upstream commit `cef52ec47429f22ac3232c112d8f002ac7cb50b1`
rather than the older PyPI 0.8.8 artifact. That revision supports the relaxed
desktop policy: background PowerShell/file work can run while the user works,
while direct mouse and keyboard actions retain ownership protection.

## Connection and compatibility

The existing Secure MCP Tunnel remains the only remote entry point. Its `main`
stdio binding starts `bridge.py`, which maintains one initialized MCP connection
to each backend. Linux tool names and Linux dispatch requests remain unchanged.
Native Windows tools are also published as `windows_<original-name>`.

Clients with a cached Gumacu tool manifest can use the existing stable facade:

1. Call `agy_capabilities` and select `windows-main` (`backend: Windows-MCP`).
2. Call `agy_dispatch` with `device_id: windows-main`,
   `action: windows_catalog`, and `arguments: {}` to obtain live tool schemas.
3. Call `agy_dispatch` with `action: windows_call` and
   `arguments: {"name": "PowerShell", "arguments": {"command": "Get-Date"}}`.
   Always follow the installed tool's catalog schema rather than this illustrative
   command field if a package update changes it.

Native image, text, structured content and error results are relayed as MCP
content blocks. No model call or extra model API credential is introduced.
If a cached ChatGPT session resumes after the tunnel process is replaced, the
bridge initializes its fresh frontend before forwarding that first request.
Internal bootstrap responses are hidden; the user's tool call is never replayed.
Normal clients retain their original MCP initialization exchange.

## Installation

Use Python 3.14 on the Windows host. Create an isolated environment and install
`requirements.txt`. Keep the bridge and backend config under a persistent local
application directory, not the temporary clipboard or download directory.

Backend config is a JSON object with `upstream` and `windows` entries following
the MCP SDK's `StdioServerParameters` (`command`, `args`, and optional `env`).
Keep the current reviewed SSH command as `upstream`, explicitly passing the
host's non-secret `PROGRAMDATA` value in its `env`. The SDK's safe environment
omits this variable, and Windows OpenSSH exits 255 without it.
The Windows entry invokes
the installed `windows-mcp` executable with `serve`, and sets
`ANONYMIZED_TELEMETRY=false`. Do not put tokens or private keys in this file.

Back up the existing tunnel profile, replace only its `main` command with the
isolated Python executable and `bridge.py --config <backend-config.json>`, then
replace the existing managed tunnel process once. Never run two stdio runtimes
against the same tunnel ID. Existing DPAPI credentials and the existing tunnel
watchdog remain responsible for starting the runtime without a visible console.

The discontinued `Gumacu-Antigravity-Agent` scheduled task remains disabled.
Do not install Windows-MCP's separate login task: the tunnel owns its subprocess.
No inbound firewall rule, Windows SSH server, or public listener is required.

## Permissions and verification

Tools run as the signed-in Windows user, not as an elevated service. Desktop
actions require the user's unlocked interactive session. Preserve Windows-MCP's
desktop ownership protections and coordinate tasks that use the real mouse,
keyboard or clipboard. This bridge grants the already-authorized ChatGPT tunnel
access to Windows tools; it does not sandbox arbitrary PowerShell commands.

Run `python -m pytest tests/test_windows_bridge.py` from the repository root.
Before replacing the live tunnel, probe
MCP initialize and tools/list, a read-only PowerShell command, and a temporary
file write/read/delete in the task workspace. After replacement, verify the same
operations through the actual ChatGPT connector and verify Linux capabilities.

For rollback, stop the managed runtime, restore its backed-up profile and start
it with the existing credential-owning launcher. Never re-enable the old agent
unless explicitly requested.
