# Gumacu fork: remote bridge review

Upstream: Boulea7/agy-mcp, MIT, commit 8829f5e0e23356b7591921e0b5ae387ad4128223.
Keep LICENSE and upstream attribution. This is a community bridge, not a Google product.

## Client modes

The intended default is ordinary ChatGPT Chat through a personal custom MCP plugin.
The alternative is Work/Codex. These are client profiles, not different Google backends.
The platform selects the chat mode; this server cannot change ChatGPT's default UI.
Use the same tools on either client after verifying account-specific availability.

For long jobs use `agy_start`, then `agy_status`. After completion, call
`agy_result_page(job_id, offset=0, limit=16000)`. Concatenate `text` exactly,
then repeat with `next_offset` and `expected_sha256` until `has_more=false`.
Offsets count Unicode characters, not bytes. The hash covers the whole redacted response.
Carry the returned `session_id` into `agy_start(SESSION_ID=...)` for the next long task.
Intermediate messages remain accessible through `agy_read`; follow `next_since`.
Event cursors count canonical events, including when translation expands one event.
The old one-shot tools still have their upstream output limits. Use the background
workflow for lossless results. Final-response pages do not reproduce every intermediate
message; use the event log for that. Existing secret/path redaction is intentionally
preserved: text is not summarized or length-truncated, but is not byte-identical
when privacy redaction applies. Very large single events still require further work
if the receiving client's context limit is exceeded. The event log is loaded in memory.

`agymcp --transport streamable-http --port 8765` binds only to 127.0.0.1.
`stdio` remains the local transport default. No unauthenticated public deployment:
place HTTP behind an authenticated HTTPS gateway or an authorized Secure MCP Tunnel.
This PR does not implement an OAuth authorization server. Loopback alone is not
authentication against other users/processes on the same host.

## Verification matrix

Local Windows: Unicode reconstruction, cursor validation, SHA mismatch rejection,
transport configuration, typed models, and root path boundary tests.
Actual ChatGPT ordinary Chat / Plus: NOT YET VERIFIED.
Actual Work/Codex connection: NOT YET VERIFIED.
Read/write tool invocation, confirmations, pagination across context limits,
same-session continuation and server restart recovery need end-to-end testing.
MCP Events are not implemented; SDK dependency is MCP 1.x. Do not advertise events.
Ordinary Chat must use explicit status polling rather than assumed background wakeups.

Official references checked 2026-10-13:
- https://learn.chatgpt.com/docs/plugins
- https://developers.openai.com/plugins/deploy/connect-chatgpt
- https://developers.openai.com/plugins/build/mcp-events
- https://antigravity.google/docs/faq

Google states that wrapping agy in third-party agents/applications is unsupported
on consumer plans and third-party access can lead to suspension/termination.
Do not treat Plus access or MIT licensing as Google account authorization.
Google recommends Gemini Enterprise or Google AI Studio API keys for external agents;
neither is automatically a drop-in authorization for this agy wrapper. Verify the
selected Google access route before production use. No account rotation is added.

## Isolation and deployment review

One service per independent server, distinct service identity, HOME, Google identity,
AGY_MCP_SESSION_ROOT and plugin connection. Job/session IDs are scoped to that endpoint.
Never share root's credential directory by default. Do not move existing root state.
The bridge exposes execution and filesystem operations: authorize a specific workspace
and review upstream tool exposure before granting remote access. Network authentication
does not by itself provide per-tool or per-workspace authorization.

Local read-only SSH audit: live agy is 1.3.4; infrastructure defaults pin 1.2.2.
Manager health reports 4.7.1 and runtime digest matches repository's immutable pin.
Manager is active with 127.0.0.1:8045 published; state directory is root-owned mode 700.
No bridge service is active. No production changes or Google prompts were run.
Account metadata indicates three Google AI Pro account files, none flagged disabled
or proxy-disabled, all with token metadata present. Tokens were not printed and live
authentication/quota validity was not tested. These are consumer-plan labels.
The two commits reported from the cloud were not recovered locally; this branch
contains newly reviewed changes and must not claim their test counts.
Local validation: 71 targeted tests passed; a real HTTP MCP roundtrip and page tests
passed (8 tests). Lint and wheel/sdist artifact audit passed. Full Windows comparison:
upstream 699 passed / 43 failed / 28 skipped; fork snapshot 706 passed / 43 failed /
28 skipped. Failure node IDs are identical (zero newly failing tests). The HTTP test
was added afterward and passed separately, including translated event cursors.
Final targeted server/utils/page suite: 185 passed / 7 failed / 13 skipped; those
seven failures are also in the upstream baseline. Full Windows suite is not a clean
Linux verification: upstream uses POSIX shell fixtures and privileged symlink creation.
Linux CI or an isolated Linux runner is required before production readiness.

## Authorized live test update

The user reported special authorization for agy wrapping and explicitly selected
the existing Netcup agy identities. This is user-reported authorization, not an
independent Google verification, and does not change the general FAQ restrictions.
No Google credential files were copied, edited or switched.
Code/dependencies were staged in a root-private temporary directory, without OS
package installation, systemd changes or a persistent listener.
Linux full suite: **778 passed**, including the real HTTP test.
Opt-in `scripts/gumacu_live_smoke.py` exercised actual MCP stdio tools:
`agy_start` -> `agy_status` -> `agy_result_page`, then another `agy_start` with
the same session ID. First full reply: `GUMACU_SMOKE_OK\n`; second: `ORCHID\n`.
Both jobs completed and used the same resumable Google conversation ID.
This establishes bridge-to-CLI continuation, not ordinary ChatGPT UI behavior.
Manager remained active/healthy at 4.7.1; no agy test process or HTTP listener remained.

ChatGPT integration is pending OpenAI Platform sign-in and tunnel availability.
Secure MCP Tunnel needs a tunnel ID, runtime credential and correct workspace
association. Those permissions are separate from Plus subscription access.
No tunnel ID/key was found in the inspected local configuration locations.
Use https://platform.openai.com/settings/organization/tunnels after signing in.

## Private connection hardening

The user approved creation and configuration of the private tunnel after reviewing
root execution risk. The user also created a second tunnel named netcup; its displayed
organization/workspace scope matches the prepared tunnel. Use the user's netcup tunnel
for the runtime; leave the other definition inactive until cleanup is authorized.
No runtime secret or tunnel identifier is stored in this repository.

Initial runtime uses GUMACU_REMOTE_PROFILE=read-only and SSH stdio (no public MCP port).
Only agy_status, agy_read, agy_result_page and agy_sessions are exposed. An actual
agy_start call through SSH was rejected; stored text retrieval succeeded.
GUMACU_REMOTE_PROFILE=controlled additionally allows start/cancel with request-level
workspace checks, no caller env overrides, only agy backend, no write/execute/worktree
mode, capped timeout and forced CLI sandbox. These are application guards, not proof
of operating-system filesystem isolation or prompt-injection immunity.
Remote source after hardening: full Linux suite **790 passed**. Ruff passed.
Requests to the tunnel poll endpoint without authorization or with a fake key returned
HTTP 401. Local doctor output alone does not establish remote authentication.

The Windows tunnel client v0.0.16 was downloaded from openai/tunnel-client and its
ZIP SHA256 matched the GitHub release digest. Credentials are entered by the user,
DPAPI-encrypted and file-ACL restricted to that Windows identity. They are passed in
the child environment, never on command lines. Runtime key scope: Tunnels Read+Use,
not models/files/admin; test expiry is seven days. Actual key creation/save remains
pending until the user completes the secure UI flow. Keep all runtime files outside
the checkout and out of Git. Windows account compromise still defeats this boundary.

This initial transport runs on Windows and uses existing SSH access to Netcup. It
requires that PC to remain connected; it is not an always-on server deployment.
No startup task or permanent server service has been installed. The remote /tmp
runtime must be replaced by a reviewed durable installation before relying on reboot
persistence. deploy/codex-netcup-testing.toml is a tested example, not an installed
global Codex configuration. Ordinary ChatGPT frontend tests are still pending.

## Controlled profile activation and live diagnostics

User authorized activation of `GUMACU_REMOTE_PROFILE=controlled` with workspace
restricted to `/tmp/gumacu-agy-review.WyP04n3d/workspace`.

All 6 controlled tools are exposed and verified via SSH probe:
`agy_start`, `agy_status`, `agy_read`, `agy_result_page`, `agy_sessions`, `agy_cancel`.
Write and execute modes remain blocked (`allow_write=False`, `worktree=False`, `sandbox=True`).

Verification:
- Two-turn live agy session completed successfully (`GUMACU_SMOKE_OK` -> `ORCHID`).
- Read-only diagnostics collector deployed at `workspace/diagnostics/collect.sh`.
- Live diagnostic task executed: agy analyzed `SYSTEM_SNAPSHOT.md` and returned a 3-point
  analysis of CPU, memory (93% utilization), Rook-Ceph, Rancher, and Kubernetes workloads.
- Windows tunnel runner updated with clean unquoted arguments, preventing flag collision (`-m`).
- Active tunnel daemon polling OpenAI control plane continuously (`readiness=1`, status 204).
- Post-reboot recovery verified: runtime re-staged on Netcup tmpfs (`pip-26.2.1` bootstrap wheel + `agy_mcp` editable venv).
- Real diagnostic task re-verified post-reboot: `agy` successfully analyzed fresh reboot metrics, returned Arabic breakdown, and retained session `eb683331-b2a3-4396-94a1-7b3063b7398f`.
- Tunnel client daemon running actively (`🟢 tunnel-client started`, `readiness=1`) connecting `tunnel_6ace3926c4e48191aeced4035a6997e5` to OpenAI control plane.


