# Architecture

ChatGPT → authenticated private tunnel → Alf Control → operator-owned SSH socket
→ persistent ssh-mcp → authorized server.

On Windows the optional bridge combines the Linux MCP transport with native
Windows-MCP. It maintains a Windows connection and creates a freshly initialized
Linux transport per operation. It forwards image/error results intact.

The direct facade is provider-independent. Registry entries choose an existing
private socket; request arguments cannot choose sockets, identity files, SSH port,
host-key policy, or arbitrary SSH targets. Each sidecar fixes its own target and
connection policy. Persistent upstream SSH sessions outlive frontend reconnects.

There is no model backend or agent job worker in this distribution. The generic
checkpoint library is retained for execution adapters, without exposing its
removed prompt-based backend actions as direct-control tools.

Normal MCP initialization is forwarded unchanged. A resumed cached tunnel session
is initialized internally before its first request is forwarded exactly once.
Bootstrap responses are hidden. In-flight requests block source-refresh recycling;
Linux also checks backend child processes. Transport failures are never replayed.
