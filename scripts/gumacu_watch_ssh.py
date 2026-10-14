"""SSH Runner for agy-watch from Windows terminal.

Streams real-time Antigravity job events and thoughts directly from the Netcup server.

Usage:
    python scripts/gumacu_watch_ssh.py --latest
    python scripts/gumacu_watch_ssh.py --latest --wait-new
    python scripts/gumacu_watch_ssh.py --job <job_id>
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_SSH_KEY = Path.home() / ".ssh" / "hhh.pem"
DEFAULT_HOST = "159.195.65.10"
DEFAULT_USER = "root"
REMOTE_RUNTIME = "/tmp/gumacu-agy-review.WyP04n3d"
REMOTE_BIN = f"{REMOTE_RUNTIME}/venv/bin/agy-watch"
REMOTE_SESSIONS = f"{REMOTE_RUNTIME}/live-sessions"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream Antigravity job progress from Netcup over SSH."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--latest",
        action="store_true",
        help="Monitor the most recent job on the remote server.",
    )
    group.add_argument(
        "--job",
        type=str,
        help="Specific job ID to monitor.",
    )
    parser.add_argument(
        "--wait-new",
        action="store_true",
        help="Wait for a new job to start before watching.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional timeout in seconds to wait for completion.",
    )
    parser.add_argument(
        "--ssh-key",
        type=Path,
        default=DEFAULT_SSH_KEY,
        help=f"Path to SSH private key (default: {DEFAULT_SSH_KEY}).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Remote SSH host (default: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=DEFAULT_USER,
        help=f"Remote SSH user (default: {DEFAULT_USER}).",
    )

    args = parser.parse_args(argv)

    if not args.ssh_key.is_file():
        print(f"Error: SSH key not found at {args.ssh_key}", file=sys.stderr)
        return 1

    remote_args = [
        f"AGY_MCP_SESSION_ROOT={REMOTE_SESSIONS}",
        REMOTE_BIN,
        f"--poll-interval {args.poll_interval}",
    ]
    if args.latest:
        remote_args.append("--latest")
    if args.job:
        remote_args.append(f"--job {args.job}")
    if args.wait_new:
        remote_args.append("--wait-new")
    if args.timeout:
        remote_args.append(f"--timeout {args.timeout}")

    remote_cmd = " ".join(remote_args)
    ssh_cmd = [
        "ssh",
        "-t",
        "-i",
        str(args.ssh_key),
        f"{args.user}@{args.host}",
        remote_cmd,
    ]

    try:
        proc = subprocess.run(ssh_cmd)
        return proc.returncode
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
