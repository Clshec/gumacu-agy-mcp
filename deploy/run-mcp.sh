#!/bin/sh
# Dedicated ChatGPT tunnel entry point. Telegram runs in its own service.
# Linux MCP must work without the optional Windows/device gateway component.
set -eu

# Optional credentials should not be a process-startup dependency. The broker's
# tools independently deny access when its admin token or gateway is absent.
# If a present file is unreadable, fail closed instead of silently dropping it.
set -a
for env_file in /etc/gumacu-agy-mcp/telegram.env /etc/gumacu-agy-devices/gateway.env; do
    if [ -e "$env_file" ] || [ -L "$env_file" ]; then
        if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
            echo "gumacu MCP: optional environment file exists but cannot be read" >&2
            exit 1
        fi
        . "$env_file"
    fi
done
set +a

export AGY_MCP_DISABLE_TELEGRAM_BOT=1
export GUMACU_REMOTE_PROFILE=operator
export GUMACU_ALLOWED_WORKSPACES=/root/gumacu-infrastructure
export AGY_MCP_SESSION_ROOT=/var/lib/gumacu-agy-mcp/sessions
export AGY_DEVICE_GATEWAY_URL=http://127.0.0.1:8766
exec /opt/gumacu-agy-mcp/venv/bin/python -m agy_mcp.stdio_reconnect
