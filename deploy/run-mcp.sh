#!/bin/sh
# Dedicated ChatGPT tunnel entry point. Telegram runs in its own service.
set -eu
set -a
. /etc/gumacu-agy-mcp/telegram.env
. /etc/gumacu-agy-devices/gateway.env
set +a
export AGY_MCP_DISABLE_TELEGRAM_BOT=1
export GUMACU_REMOTE_PROFILE=operator
export GUMACU_ALLOWED_WORKSPACES=/root/gumacu-infrastructure
export AGY_MCP_SESSION_ROOT=/var/lib/gumacu-agy-mcp/sessions
export AGY_DEVICE_GATEWAY_URL=http://127.0.0.1:8766
exec /opt/gumacu-agy-mcp/venv/bin/python -m agy_mcp.stdio_reconnect
