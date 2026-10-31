# Direct-control security

Alf Control is an operator integration. Its stdio connection must be protected by
the existing authenticated private tunnel/SSH transport. It does not expose a
public HTTP endpoint. Native tools can administer their enrolled host and return
command output; callers and the host's permission policy govern those actions.

The private SSH sidecar fixes its target and host-key policy, rejects caller key,
port and SSH-argument overrides, and disables forwarding tools by default. A
provider registry is trusted operator configuration, never caller input. Keep
registry files, sockets, SSH keys and known hosts private. A single service per
target keeps credentials and session state separate.

Windows-MCP remains a separately licensed pinned dependency. The bridge forwards
its tools/results and preserves its configured policy; it adds no substitute
shell executor. Its `relaxed` example grants broad native control and must only
be used in the intended authorized operator account. Telemetry is disabled in
the installation example. No credentials belong in committed backend JSON.

Transport failures can leave a command's outcome unknown. No layer automatically
replays a failed operation. Check the remote state before any manual retry.
