# Optional SSH backend for the existing Gumacu MCP

**Goal:** ChatGPT ordinary chat -> existing agy_dispatch -> independent
pinned upstream slepp/ssh-mcp -> OpenSSH -> Netcup (or another explicitly
configured host). Antigravity tools and queues are untouched.

## Why a separate sidecar?

The upstream slepp/ssh-mcp server stores interactive sessions inside
its MCP process. ChatGPT's SSH stdio bridge reloads when Gumacu source
changes. Running ssh-mcp in that process would discard SSH sessions
whenever Gumacu code is updated.

The Gumacu ssh_sidecar keeps **one upstream stdio process alive** behind a
root-owned Unix socket at /run/gumacu-ssh-mcp/control.sock (mode 0600).
The ChatGPT-visible agy_dispatch schema stays stable as upstream tools change.
No second ChatGPT connection is required, and Antigravity is never removed.

The upstream command channel is serialized, not an eight-worker job service:
interactive sessions can execute independently between calls, but a blocking
ssh_exec holds the channel until it responds. SSH sessions still do not survive
restart of the SSH sidecar itself. The upstream does not provide this durability.

## Locked upstream dependency (MIT)

Upstream: https://github.com/slepp/ssh-mcp

Pinned source commit: 37381ddc68a941b62ed8557dcbb5644dc3b6c5fd

A dedicated fork can replace this source URI without modifying Gumacu's
MCP protocol adapter. Do not blindly track upstream main in production.

## Safe installation (opt-in)

1. Confirm a dedicated SSH account with required rights for the host.
   The systemd unit uses a single fixed root@127.0.0.1 endpoint with a previously
   verified existing operator SSH identity. The key file is not copied or read
   into the repository. Leave strict host key checking on.
   Check existing known_hosts and authorized_keys before adding any key.
2. Install upstream in an isolated virtual environment, never into Gumacu's:

   python3 -m venv /opt/gumacu-ssh-mcp/venv

   /opt/gumacu-ssh-mcp/venv/bin/pip install 'git+https://github.com/slepp/ssh-mcp.git@37381ddc68a941b62ed8557dcbb5644dc3b6c5fd'

   If pip is absent use uv venv/uv pip install in the same isolated venv.
3. Verify that the existing root loopback SSH key works before enabling
   the sidecar. The service controls its target/identity with
   GUMACU_SSH_TARGET and GUMACU_SSH_IDENTITY_FILE, not with chat-supplied
   SSH flags or changes to the host's SSH daemon.
   The already-tested installation can authenticate as root using the
   existing operator identity with strict host-key checking.
   Do not create new root SSH access solely for this feature.
4. Copy deploy/gumacu-ssh-mcp.service into /etc/systemd/system/, run
   systemctl daemon-reload and systemctl enable --now gumacu-ssh-mcp.service.
   Never restart the existing Gumacu MCP, Telegram, or Gateway services.
5. From ChatGPT: agy_capabilities, then
   agy_dispatch(device_id="linux-netcup", action="ssh_catalog", arguments={}).
   The catalog returns the upstream schemas dynamically. For example:
   agy_dispatch(device_id="linux-netcup",action="ssh_call",
   arguments={"name":"ssh_exec","arguments":
   {"target":"root@127.0.0.1","command":"id"}}).

Target is pinned; user-supplied port, identity_file, known_hosts_file,
strict_host_key_checking and extra_ssh_args are rejected. Port-forward
tools are disabled by default but can be enabled by operator configuration
GUMACU_SSH_DENY_TOOLS. This backend is ADMINISTRATOR-ONLY.

Existing agy_start and operation_step still use Antigravity. SSH results are
returned directly to ChatGPT with no second model.

## Health, sessions and rollback

- When the sidecar is unavailable, SSH calls fail closed, all other tools run.
- Keep the socket private (mode 0600, directory 0700), never expose via TCP.
- Check systemctl is-active gumacu-ssh-mcp.service, stat the socket,
  and run a read-only ssh_exec such as id as a smoke test.
- Stop only gumacu-ssh-mcp.service to roll back without affecting Antigravity.
- For long jobs use ssh_ensure_session / ssh_write_session / ssh_read_session.
- ssh_edit is not atomic and can overwrite another writer's changes.
- SSH tool permissions equal the underlying service account's SSH rights.
  Strong privileges should be reserved for trusted admin use; use approvals
  for destructive operations and audit changes through your standard workflow.
