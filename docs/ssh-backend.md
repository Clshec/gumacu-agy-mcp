# Private SSH backend and provider routing

Alf Control uses the upstream `slepp/ssh-mcp` executable without vendoring it.
The existing deployment's pinned binary is `/opt/gumacu-ssh-mcp/venv/bin/ssh-mcp`.
Each sidecar owns a persistent stdio child and SSH sessions. Set a private
0700 runtime directory, use a 0600 Unix socket, and run under the authorized
service user. An orphaned same-owner socket can be recovered; live listeners,
symlinks and unexpected filesystem objects are never replaced.

| Setting | Purpose | Existing-install fallback |
| --- | --- | --- |
| `ALF_CONTROL_SSH_SOCKET` | Default facade socket | `GUMACU_SSH_SOCKET`, `/run/gumacu-ssh-mcp/control.sock` |
| `ALF_CONTROL_SSH_TARGET` | Fixed sidecar target | `GUMACU_SSH_TARGET`, `gumacu-netcup-local` |
| `ALF_CONTROL_SSH_IDENTITY_FILE` | Operator-owned absolute key path | `GUMACU_SSH_IDENTITY_FILE` |
| `ALF_CONTROL_SSH_DENY_TOOLS` | Disabled upstream tools | `GUMACU_SSH_DENY_TOOLS`; forwarding tools disabled by default |
| `ALF_CONTROL_DEVICES_FILE` | Absolute device-registry JSON path | Default `linux-netcup` device |
| `SSH_MCP_STATE_DIR` | Upstream session state | Configured separately per sidecar |

`ALF_CONTROL_*` settings take precedence, including explicit empty values. They
are never read from tool arguments. Use `python -m alf_control.ssh_sidecar
--binary /absolute/path/to/ssh-mcp --socket /private/path/control.sock` to start
a sidecar. Pre-enroll SSH host keys: strict checking is always enabled. Caller
connection overrides are denied. Do not share writable config/socket directories
with untrusted users. The registry and service environment are operator-owned.

For another provider, run a second sidecar with a distinct fixed target, key,
socket and state directory. Add that socket to the registry using a unique device
ID and descriptive provider label. No provider is selected by model guesswork.
`windows-main` is reserved for the native Windows bridge.

Call `ssh_catalog`, then use `ssh_call` with `name` and optional `arguments` from
that catalog. Direct execution, files, persistent terminals and session controls
are supplied by upstream tools. Catalog reads have a ten-second transport timeout;
command calls have no extra facade deadline and are never retried automatically.
