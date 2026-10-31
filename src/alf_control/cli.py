"""Alf Control MCP entry point; existing transport arguments remain supported."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alf Control MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    from alf_control.server import run

    try:
        run(args.transport, args.port)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError):
        print("Alf Control startup failed; check operator configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
