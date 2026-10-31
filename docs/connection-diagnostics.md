# Connection diagnostics

Keep transport readiness, MCP initialization, catalog discovery, SSH authentication,
and command exit status separate. A healthy local runtime alone does not prove
that a ChatGPT request reached the server or that a command succeeded.

The 2026-10-31 read-only inspection found:

* The connected host integration could discover Linux and Windows actions and run
  read-only commands on both devices. A fresh direct SSH connection also worked.
* The running Linux checkout was `2b7d96b`, while GitHub main was `6c24c6d`.
  The installed launcher unconditionally sourced Telegram and device-gateway env
  files. Main's newer launcher had made those files optional. The new Alf Control
  launcher removes that dependency entirely.
* The Windows bridge already preserved PROGRAMDATA for OpenSSH and opened a fresh
  initialized Linux MCP transport per operation. These connection fixes are kept.
* The original conversation supplied no exact refusal error or timestamp. Its
  root cause cannot be proven from the available evidence. The current refusal
  was not reproduced, so a historic incident must not be marked fixed merely
  because today's checks pass.

If a future failure occurs, collect the exact tool/error and time. Inspect the
local runtime's MCP health (not just readiness), then test a newly initialized
connection, catalog and harmless command. Check the selected SSH host/key/known
hosts and the sidecar's private listener. Never disable host-key verification or
automatically replay an uncertain command to diagnose connectivity.

`alf_doctor` probes configured SSH catalog transports, not client visibility or
target command authentication. The server cannot independently certify that a
specific ChatGPT UI has refreshed its cached tool manifest. Run a command through
that host to establish end-to-end evidence.
