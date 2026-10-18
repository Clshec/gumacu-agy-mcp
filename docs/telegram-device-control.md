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

Full application closure currently supports Windows only. Linux device panels
do not expose a shutdown button. Selecting Linux from the persistent keyboard
shows that limitation instead of closing a different device.

Deploy the same reviewed agent/broker/bot code, enable the per-device allowlist,
restart only affected idle services/agent processes, and verify inventory-only
dispatch and UI confirmation. Do not test destructive shutdown on real user
work automatically: the final shutdown is the user's explicit confirmation in
Telegram. No additional getUpdates poller is needed.
