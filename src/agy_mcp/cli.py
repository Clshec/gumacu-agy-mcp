"""Entry-point shim that starts the FastMCP stdio server."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Start the agy-mcp MCP stdio server.

    Returns the process exit code; 0 on clean shutdown.
    """

    # Import is deferred so that `python -m agy_mcp.cli --help` style invocations
    # in pre-flight environments do not pay the FastMCP import cost.
    parser = argparse.ArgumentParser(description="Gumacu Antigravity MCP bridge")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    from agy_mcp.server import run

    try:
        run(transport=args.transport, port=args.port)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — top-level guard, surfaces to stderr
        print(f"agy-mcp server failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
