# Installation from a reviewed checkout

Use Python 3.11 or newer. Run `uv sync --locked`, then `uv run alf-control` for
the stdio MCP endpoint. `python -m alf_control` is equivalent. `alf-control-doctor`
checks configured private SSH transports and exits nonzero if any is unavailable.
It does not invoke or authenticate a model CLI.

The existing `--transport streamable-http --port 8765` arguments are also
supported. HTTP binds only to `127.0.0.1`; place it behind an authenticated
operator gateway/tunnel. `ALF_CONTROL_PROFILE=read-only` disables dispatch, with
`GUMACU_REMOTE_PROFILE` accepted as the legacy fallback. Existing local/operator
and controlled profile names remain valid; SSH target policy stays in the sidecar.

For a Linux host, install the reviewed wheel into an isolated environment. The
existing `/opt/gumacu-agy-mcp/venv` path remains a supported upgrade location.
`deploy/run-mcp.sh` starts `alf_control.stdio_reconnect` there; set
`ALF_CONTROL_PYTHON` to choose a different installed interpreter. Do not run this
launcher before installing the new package in that interpreter.

The launcher requires no Telegram environment or device-agent gateway. Configure
the SSH sidecar separately as described in [ssh-backend.md](ssh-backend.md).
`deploy/codex-alf-control.toml` is an SSH client example; configure its host alias
and verify host keys locally. Existing tunnel/App identities can stay unchanged.

The supplied readiness and sidecar unit filenames preserve existing service
identities. They are reviewed deployment templates, not automatically installed
by package installation. See [migration.md](migration.md) before applying them.
