# Dynamic Capability Gateway (stable MCP contracts)

ChatGPT and other MCP clients may cache the MCP tool list for a session.  A
change from 26 to 28 MCP tools, or changing an existing tool's JSON schema,
**cannot be guaranteed to appear instantaneously in an already-open chat**.

This implementation introduces **two stable public MCP schemas**:

- `agy_capabilities()`: query the private broker *at call time* for enrolled
  device IDs, online status, and per-device allowed actions.
- `agy_dispatch(device_id, action, arguments={})`: invoke one currently
  advertised action. Use `device_id="linux-netcup"` for the local Linux
  adapter or a registered remote device ID such as `windows-main`.

Example flow:

1. Call `agy_capabilities()`.
2. Choose `device_id` and one of that device's `actions`.
3. Call `agy_dispatch(device_id, action, arguments)`.
4. For jobs, keep the returned job ID and retrieve the result by the same
   owner device, not by the current UI selection.

For example, after discovery:

```json
{
  "device_id": "windows-main",
  "action": "start",
  "arguments": {"PROMPT": "Read-only status check", "timeout": 0}
}
```

No shell commands, arbitrary Python imports, script evaluation or filesystem
write privileges are provided by this facade.  The private broker and
per-device agent retain the authoritative allowlists and workspace/write
policies.  A remote action must be listed in the broker's trusted
`allowed_actions` for that device *and* implemented in its agent.  New
backend actions may be added by updating broker and agent code/configuration
without adding another MCP tool or changing these two schemas.  The gateway
does not execute unknown actions or make offline devices look successful.

## Installation and rollout

1. Run the repo CI, including Python cross-platform tests.
2. Back up production source, session state, and private broker configuration.
3. Deploy the same commit to the Linux MCP venv and gateway service.
   Restart the **gateway** before reloading the MCP process: it now publishes
   `allowed_actions` to authenticated admin clients.
4. The MCP process must be restarted/reconnected to load newly added Python
   code and register the *initial* two stable tools. Keep the independent
   Telegram poller alive, with exactly one active `getUpdates` loop.
5. **One initial ChatGPT connector refresh/new connection may still be
   required** to register `agy_capabilities` and `agy_dispatch`.  Thereafter,
   backend capability/registered-device additions do not require modifying the
   MCP schema. The connector must still be healthy and able to call tools.
6. Validate both commands live, confirm Windows is online, verify
   `timeout=0` is accepted, and check the Telegram bot continues to operate.
   Do not infer a successful server deployment from a GitHub merge alone.

Do not overwrite or remove the existing 26 tools during the initial rollout.
No job should be cancelled or killed merely because ChatGPT disconnects.

## Known limits

- Backend functionality still requires its own deploy (and possibly an agent
  restart); this only removes *MCP tool schema churn*, not deployment time.
- An outage/broken MCP transport still blocks all tool calls: dynamic discovery
  cannot repair that transport.
- Cached ChatGPT UI tool definitions require an initial update; tools cannot
  proactively force the client to refresh its schema from server-side code.
- Authenticated gateway device listing exposes only device capabilities and
  authorized action names, never device credentials.
