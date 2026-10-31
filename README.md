# Alf Control

**Alf Control** (`alf-control`, Python package `alf_control`) gives ChatGPT direct
MCP access to authorized servers and devices across hosting providers.
Linux control uses a private persistent [ssh-mcp](https://github.com/slepp/ssh-mcp)
service. The optional Windows bridge forwards native Windows-MCP tools.
ChatGPT chooses each action and receives its result directly.

## Tools

| Tool | Purpose | Compatibility name |
| --- | --- | --- |
| `alf_capabilities` | Discover configured devices, availability and actions | `agy_capabilities` |
| `alf_dispatch` | Call an advertised action on a selected device | `agy_dispatch` |
| `alf_doctor` | Probe direct transport readiness | `agy_doctor` |

The three compatibility names execute the same direct functions. Agent prompts,
Antigravity/Gemini execution, autonomous pipelines, model sessions, agent process
control, collaboration skills, and their Telegram notifications were removed.
SSH interactive sessions and native Windows tools remain available through their
upstream catalogs. The backend-independent checkpoint library remains in
`alf_control.interactive_operations`; its old model-backed MCP actions were removed.

## Install and connect

This branch is source-only until reviewed. Build/install from this checkout:

```sh
uv sync --locked
uv run alf-control
```

`alf-control` speaks MCP over stdio; it is not a public unauthenticated HTTP server.
Use an existing authenticated private tunnel/SSH connection for a remote host.
See [installation](docs/installation.md), [SSH configuration](docs/ssh-backend.md),
[Windows integration](integrations/windows-mcp/README.md), and
[migration](docs/migration.md) before replacing an existing installation.

Typical call sequence:

```json
{"name":"alf_capabilities","arguments":{}}
{"name":"alf_dispatch","arguments":{"device_id":"linux-netcup","action":"ssh_catalog"}}
{"name":"alf_dispatch","arguments":{"device_id":"linux-netcup","action":"ssh_call","arguments":{"name":"ssh_exec","arguments":{"target":"root@127.0.0.1","command":"hostname"}}}}
```

Use the target and schemas returned by the configured device's catalog. A tool
transport success does not imply the remote command exited successfully: inspect
`upstream_error`, `result.ok`, `result.exit_code`, and `result.timed_out` when present.
An interrupted command may already have run; never blindly retry it.

## Multiple providers

No provider API or provider-specific executor is required. Configure one private
SSH sidecar per authorized target, each with its own key, known hosts, state and
socket, then set `ALF_CONTROL_DEVICES_FILE` to an operator-owned JSON registry.
The [example registry](deploy/devices.example.json) includes two providers; replace
the example sockets with real configured services. New devices do not change MCP
schemas. The existing `linux-netcup` and `windows-main` identities are preserved.

## Development

```sh
uv sync --locked
uv run ruff check src tests scripts integrations/windows-mcp
uv run pytest -q
uv build
uv run python scripts/check_release_artifacts.py
```

POSIX sidecar and resumed-SSH tests are skipped on Windows and run on Linux/macOS.
The Windows bridge has separate transport and routing regression tests. Read
[connection diagnostics](docs/connection-diagnostics.md) for what each check proves.

Repository URLs retain the existing GitHub repository identity; no repository,
App ID, tunnel ID, secret, installed profile, or production deployment is renamed
by editing this branch. Original MIT attribution is preserved in LICENSE.
