"""Optional persistent ssh-mcp backend behind Gumacu's stable agy_dispatch.

This module does not vendor or alter the upstream slepp/ssh-mcp code. Its
own long-lived process owns a pinned upstream stdio child. ChatGPT-facing
MCP instances can come and go without dropping its SSH sessions.

Admin power is scoped by a private Unix socket and a fixed SSH target.
Only enable this service in a trusted operator-only installation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import socketserver
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

_MAX_INPUT = 256_000
_MAX_OUTPUT = 2_000_000
_ACTION_NAME = re.compile(r"^ssh_[a-z][a-z0-9_]{0,63}$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BLOCKED_CONNECTION_ARGS = frozenset({
    "extra_ssh_args", "identity_file", "known_hosts_file",
    "strict_host_key_checking", "port",
})
DEFAULT_SOCKET = "/run/gumacu-ssh-mcp/control.sock"
DEFAULT_TARGET = "gumacu-netcup-local"
_DEFAULT_DISABLED = "ssh_forward,ssh_list_forwards,ssh_stop_forward"


def _socket_path() -> Path:
    return Path(os.getenv("GUMACU_SSH_SOCKET", DEFAULT_SOCKET))


def _target() -> str:
    value = os.getenv("GUMACU_SSH_TARGET", DEFAULT_TARGET)
    if not _TARGET_RE.fullmatch(value):
        raise ValueError("invalid configured SSH target")
    return value


def _disabled_tools() -> set[str]:
    return {x.strip() for x in os.getenv(
        "GUMACU_SSH_DENY_TOOLS", _DEFAULT_DISABLED
    ).split(",") if x.strip()}


def _secure_arguments(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if not _ACTION_NAME.fullmatch(tool):
        raise ValueError("invalid SSH tool name")
    if tool in _disabled_tools():
        raise ValueError("SSH tool not enabled in operator profile")
    if not isinstance(args, dict) or not all(isinstance(k, str) for k in args):
        raise ValueError("SSH arguments must be an object")
    if set(args) & _BLOCKED_CONNECTION_ARGS:
        raise ValueError("connection parameters are fixed by operator configuration")
    fixed_target = _target()
    if "target" in args and args["target"] != fixed_target:
        raise ValueError("only the configured SSH target is allowed")
    return dict(args)


def _call_socket(message: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(raw) > _MAX_INPUT:
        return {"success": False, "error": "SSH request too large"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(_socket_path()))
            # No wall clock timeout for a long-running authorized SSH request.
            sock.sendall(raw)
            with sock.makefile("rb") as inp:
                line = inp.readline(_MAX_OUTPUT + 1)
            if len(line) > _MAX_OUTPUT:
                return {"success": False, "error": "SSH response exceeds maximum size"}
            return json.loads(line)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"success": False, "error": "SSH sidecar unavailable or invalid response"}


def ssh_catalog() -> dict[str, Any]:
    """Discover the upstream SSH MCP tools without expanding ChatGPT tool schemas."""
    return _call_socket({"kind": "list"})


def ssh_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a named upstream SSH tool; never interpret text as Python/shell locally."""
    try:
        secured = _secure_arguments(name, arguments or {})
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return _call_socket({"kind": "call", "name": name, "arguments": secured})


class StdioBackend:
    """One durable upstream MCP process per independent sidecar service."""

    def __init__(self, executable: str):
        binary = Path(executable)
        if not binary.is_absolute() or not binary.is_file():
            raise ValueError("GUMACU_SSH_MCP_BIN must be an existing absolute file")
        self.executable = str(binary)
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.seq = 0
        self.tools: list[dict[str, Any]] = []
        self.allowed: set[str] = set()

    def _raw(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.seq += 1
        wire = {"jsonrpc": "2.0", "method": method, "params": params, "id": self.seq}
        self.proc.stdin.write(json.dumps(wire).encode() + b"\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline(_MAX_OUTPUT + 1)
        if not line or len(line) > _MAX_OUTPUT:
            raise RuntimeError("upstream MCP stdout ended or exceeded response limit")
        result = json.loads(line)
        if not isinstance(result, dict) or result.get("id") != self.seq:
            raise RuntimeError("upstream MCP response identity mismatch")
        return result

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        # Do not replay an interrupted write automatically after a crash.
        self.proc = subprocess.Popen(
            [self.executable], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
        init = self._raw("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "gumacu-ssh-sidecar", "version": "1"},
        })
        if "result" not in init:
            raise RuntimeError("upstream MCP initialization failed")
        listed = self._raw("tools/list", {})
        data = listed.get("result", {})
        items = data.get("tools", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise RuntimeError("invalid upstream SSH MCP tool catalog")
        self.tools = [t for t in items if isinstance(t, dict) and
                      isinstance(t.get("name"), str) and
                      _ACTION_NAME.fullmatch(t["name"])]
        self.allowed = {t["name"] for t in self.tools}

    def dispatch(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            try:
                self._start()
                if data.get("kind") == "list":
                    disabled = _disabled_tools()
                    return {"success": True, "target": _target(),
                            "tools": [t for t in self.tools
                                      if t["name"] not in disabled],
                            "sidecar": "slepp-ssh-mcp"}
                if data.get("kind") != "call":
                    return {"success": False, "error": "unsupported request"}
                name = data.get("name")
                args = _secure_arguments(name, data.get("arguments", {}))
                if name not in self.allowed:
                    return {"success": False, "error": "unknown upstream SSH tool"}
                reply = self._raw("tools/call", {"name": name, "arguments": args})
                if "error" in reply:
                    return {"success": False, "error": "upstream MCP request error"}
                result = reply.get("result", {})
                if not isinstance(result, dict):
                    return {"success": False, "error": "invalid upstream MCP response"}
                # Upstream repeats structuredContent in its text content;
                # return only the structured result to avoid context inflation.
                structured = result.get("structuredContent")
                if isinstance(structured, dict):
                    return {"success": not bool(result.get("isError")), "result": structured,
                            "upstream_error": bool(result.get("isError"))}
                return {"success": not bool(result.get("isError")), "result": result}
            except (ValueError, OSError, RuntimeError, json.JSONDecodeError):
                # Never blindly replay an SSH command if the upstream transport
                # failed after its stdin write. Return an uncertain result.
                if self.proc and self.proc.poll() is not None:
                    self.proc = None
                return {"success": False, "error": "SSH backend error; command outcome may be unknown, do not auto-retry"}

    def close(self) -> None:
        with self.lock:
            if self.proc:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait()
                # Pipes must be explicitly closed even after wait(): failing
                # to close them emits ResourceWarnings under strict pytest.
                if self.proc.stdin:
                    self.proc.stdin.close()
                if self.proc.stdout:
                    self.proc.stdout.close()
                self.proc = None


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: str, backend: StdioBackend):
        self.backend = backend
        super().__init__(address, _Handler)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(_MAX_INPUT + 1)
            if not line or len(line) > _MAX_INPUT:
                result: dict[str, Any] = {"success": False, "error": "bad request size"}
            else:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid request")
                result = self.server.backend.dispatch(message)  # type: ignore[attr-defined]
            wire = json.dumps(result, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if len(wire) > _MAX_OUTPUT:
                wire = b'{"success":false,"error":"response too large"}\n'
            self.wfile.write(wire)
        except (ValueError, OSError, json.JSONDecodeError):
            pass


def serve(executable: str, socket_path: Path) -> None:
    # Refuse to unlink an existing socket that may belong to an active service.
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_path.parent.is_symlink():
        raise ValueError("socket parent cannot be symlink")
    if socket_path.exists() or socket_path.is_symlink():
        raise RuntimeError("SSH socket already exists; refusing to replace it")
    os.umask(0o077)
    backend = StdioBackend(executable)
    server = _Server(str(socket_path), backend)
    socket_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        backend.close()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    args = parser.parse_args()
    serve(args.binary, Path(args.socket))


if __name__ == "__main__":
    main()
