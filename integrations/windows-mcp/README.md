# Native Windows tools for Alf Control

This optional bridge combines an Alf Control Linux upstream and a native
Windows-MCP stdio backend in the existing authenticated private tunnel. The
separately licensed upstream dependency is pinned in `requirements.txt` and
`requirements.lock.txt`; no model CLI is required.

Call `alf_capabilities`, then `alf_dispatch` on `windows-main` with
`windows_catalog`. Invoke a discovered tool through `windows_call` using
`{"name":"PowerShell","arguments":{"command":"[Environment]::OSVersion.VersionString"}}`.
The legacy generic `agy_*` facade names work too. Native `windows_*` tool aliases
are also listed. Images and native error results pass through unchanged.

`prepare-install.ps1 -TunnelProfile <existing-profile> -InstallDirectory <path>`
creates a separate pinned environment, copies the bridge and derives `backends.json`
from the existing upstream command. It does not change the tunnel profile. The
backend configuration preserves PROGRAMDATA, required by Windows OpenSSH under
the MCP SDK's restricted inherited environment. Windows telemetry is disabled.

`activate-tunnel.ps1` is a separate deployment action. It backs up the profile,
temporarily suspends the existing watchdog, updates the binding, verifies runtime
readiness and restores the old profile on failure. Its defaults preserve the
existing runtime alias and watchdog identity. Do not run it merely to test source.

For isolated validation, provide a separate backend config to
`python probe.py --config <config> --output <report>`. The probe checks MCP
initialization, discovery, a read-only Windows command and a temporary file
write/read/delete under the report's directory. It never overwrites the proof
file if it already exists.

The bridge initializes a fresh Linux transport for each operation and keeps its
native Windows session. It does not retry failed commands. A resumed cached MCP
session initializes the new frontend before forwarding its original request once.
