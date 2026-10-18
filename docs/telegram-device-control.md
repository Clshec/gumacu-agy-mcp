# Telegram response time and Windows shutdown

Telegram polling is long polling: the 20-second getUpdates timeout is a maximum
wait for new updates, not a compulsory delay for every button. Previously the
single update loop waited for acknowledgement HTTP requests and remote result
retrieval (up to 10 seconds). Windows checked for work every 2 seconds when idle.

The bot now reuses its HTTP client, runs callback acknowledgements and remote
last-reply reads outside the polling loop, and the device agent checks every
0.5 seconds while idle. Actual remote execution still takes time; these changes
do not promise instant results or a fixed latency. A diagnostic on 18 October
measured Telegram getMe at 0.068–0.152 seconds with new connections and
0.023–0.055 seconds with reused connections; Windows result retrieval took
2.059 seconds before the polling change.

## Close Antigravity on the selected Windows device

The persistent keyboard and Windows device panel contain an explicit shutdown
button. It opens a confirmation naming the device and warning about running
jobs and unsaved work. Confirmation expires after 120 seconds, is consumed
once, and remains bound to that device even if the active selection changes.
The cancel/back button invalidates the confirmation.

The agent shutdown action requires explicit broker allowed_actions opt-in:
add `shutdown` only for the intended Windows device. Defaults do not grant it.
The agent accepts only boolean `confirm` and `dry_run` fields. Normal execution
requires `confirm=true`; inventory-only testing uses `dry_run=true` and never
cancels work or stops applications.

Shutdown cancels the agent's running supervised jobs, stops matching processes
and descendants under the user's trusted Antigravity installation roots, and
checks for remaining processes. It preserves the agent, SSH connection,
Telegram service, credentials, session history and unrelated applications.
Unknown/custom application paths do not get killed; matching residual names
cause verification failure instead of false success. The app can be started
again later. This is not a command to shut down Windows itself.

Full closure supports Windows and Linux. Linux stops only the exact
antigravity-manager.service and trusted agy processes/descendants, preserving
their MCP/SSH/bot ancestors. PID start identities are rechecked before signals;
remaining processes and manager state are verified before reporting success.
A new Telegram task reopens the manager only if this shutdown marked it closed.
Do not test real shutdown while user tasks are running.

The persistent reply keyboard has been removed at the user's request. Use the
Telegram command menu: /devices, /device, /new, /use, /sessions, /status,
/progress, /last, /jobs, /recent, /shutdown and /help. Shutdown confirmation
remains contextual inside a message, never in a keyboard below the input.

The dedicated bot observes new durable local and remote job records every
second and sends a one-time receipt for work submitted outside Telegram. It
does not claim execution has started, replay historical jobs, start another
getUpdates poller, or restart a busy MCP process. Telegram-origin requests
retain their existing immediate acknowledgement. Successfully delivered
receipt IDs are persisted in bot state; failed sends can retry on the next pass.

Deploy the same reviewed agent/broker/bot code, enable the per-device allowlist,
restart only affected idle services/agent processes, and verify inventory-only
dispatch and UI confirmation. Do not test destructive shutdown on real user
work automatically: the final shutdown is the user's explicit confirmation in
Telegram. No additional getUpdates poller is needed.
