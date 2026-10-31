# Alf Control migration and compatibility

This is a deliberate removal of the Antigravity execution path, not a remote
production upgrade. Review and test the branch before installing it.

## Preserved identities

The Python distribution is `alf-control`, the implementation is `alf_control`,
the stdio command is `alf-control`, and MCP server/display metadata says
**Alf Control**. The App-backed package's `dev-*` name and `.app.json` App ID
are immutable connection identities and remain unchanged. Renaming those would
create a different integration instead of updating the existing one.

Repository URLs remain `Clshec/gumacu-agy-mcp` because that is the actual GitHub
repository; changing source metadata does not rename a hosted repository.

The existing tunnel alias `gumacu-netcup`, service filenames, install directories,
private socket/state paths and `linux-netcup`/`windows-main` device IDs are retained
for in-place compatibility. They are deployment identities, not project branding.
New installs can choose their own host, socket and runtime configuration.

The following launch paths are thin shims into Alf Control:

* `agymcp` and `python -m agy_mcp[.cli]`
* `python -m agy_mcp.stdio_reconnect`
* `python -m agy_mcp.ssh_sidecar`
* `agy-doctor` and `python -m agy_mcp.doctor`

Only `agy_capabilities`, `agy_dispatch`, and `agy_doctor` remain as wire aliases.
Their argument shapes are preserved. Discovery now advertises direct actions.
Model-job, device-agent, pipeline, prompt, notification and skill-install tools
are removed, including their dispatch actions. Cached agent tools must fail as
unknown/unavailable; they are never redirected into shell execution.

The generic checkpoint class remains available as a library. Existing agent
job/session archives are not migrated, read, purged, or deleted. SSH session state
and Windows-MCP dependencies remain independent. Remove any obsolete notification
polling or agent scheduling from clients separately when deploying.

## Reviewed deployment sequence

1. Record current revision, interpreter/packages, service units and tunnel profile.
   Preserve encrypted keys, known hosts and persistent SSH session state.
2. Build and test the branch in an isolated environment. Validate initialization,
   catalogs, a read-only command, resumed transport, and legacy facade aliases.
3. Install the reviewed wheel in the selected environment, then update the Linux
   launcher and readiness unit. Apply the sidecar template only if deliberately
   upgrading that service; its existing socket protocol remains compatible.
4. Update the optional Windows bridge from this branch in a separate reviewed
   step. Retain the existing backend paths, PROGRAMDATA, Windows-MCP environment
   and authenticated tunnel binding. The activation script backs up the profile
   and restores it on startup failure.
5. Refresh the client tool listing/display metadata and verify direct Linux and
   Windows commands from the intended ChatGPT host. Display changes to an installed
   App require its release/configuration workflow; branch edits alone do not do it.
6. If verification fails, restore the previous package, launcher, unit and bridge
   profile. Do not replay commands whose outcome is unknown.

The branch does not stop, uninstall or modify any production service, scheduled
task, existing agent application, or user data. These source deletions only remove
that integration from the new distribution.
