#!/bin/sh
# Direct MCP transport: no agent, Telegram or broker environment is required.
set -eu
# Preserve the existing install path for reviewed in-place upgrades.
exec "${ALF_CONTROL_PYTHON:-/opt/gumacu-agy-mcp/venv/bin/python}" -m alf_control.stdio_reconnect
