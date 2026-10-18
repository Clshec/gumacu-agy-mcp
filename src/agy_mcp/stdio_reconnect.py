"""Repair a resumed tunnel's fresh stdio session without replaying tool calls.

Normal MCP clients keep their own initialization. If the tunnel resumes a
cached client session against a new child, initialize that child first.
No tool request is retried. stderr is inherited; stdout is JSON-RPC only.
"""
import json
import os
import selectors
import subprocess
import sys
import threading


def send(pipe, message):
    pipe.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    pipe.flush()


def main():
    child = subprocess.Popen(
        [sys.executable, "-m", "agy_mcp.cli"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
    )
    try:
        first = sys.stdin.buffer.readline()
        if not first:
            return 0
        request = json.loads(first)
        if request.get("method") != "initialize":
            bootstrap_id = "gumacu-reconnect-" + os.urandom(12).hex()
            send(child.stdin, {
                "jsonrpc": "2.0", "id": bootstrap_id, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "gumacu-tunnel-reconnect", "version": "1"}},
            })
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            if not selector.select(30):
                raise RuntimeError("MCP initialization timed out")
            response = json.loads(child.stdout.readline())
            selector.close()
            if response.get("id") != bootstrap_id or "result" not in response:
                raise RuntimeError("MCP initialization failed")
            send(child.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            print("gumacu reconnect: fresh MCP child initialized before resumed request", file=sys.stderr)
        child.stdin.write(first)
        child.stdin.flush()

        def upstream():
            try:
                for line in sys.stdin.buffer:
                    child.stdin.write(line)
                    child.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                child.stdin.close()

        threading.Thread(target=upstream, daemon=True).start()
        for line in child.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
