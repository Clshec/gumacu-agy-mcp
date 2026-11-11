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

from alf_control.reload_guard import RequestTracker, watch_revision


def send(pipe, message):
    pipe.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    pipe.flush()


def main():
    tracker = RequestTracker()
    stop = threading.Event()
    child = subprocess.Popen(
        [sys.executable, "-m", "alf_control.cli"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
    )
    try:
        first = sys.stdin.buffer.readline()
        if not first:
            return 0
        request = json.loads(first)
        if request.get("method") != "initialize":
            bootstrap_id = "alf-control-reconnect-" + os.urandom(12).hex()
            send(child.stdin, {
                "jsonrpc": "2.0", "id": bootstrap_id, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "alf-control-tunnel-reconnect", "version": "1"}},
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
            print("alf-control reconnect: fresh MCP child initialized before resumed request", file=sys.stderr)
        tracker.request(first)
        child.stdin.write(first)
        child.stdin.flush()
        threading.Thread(
            target=watch_revision, args=(child, tracker, stop), daemon=True,
        ).start()

        def upstream():
            try:
                for line in sys.stdin.buffer:
                    tracker.request(line)
                    child.stdin.write(line)
                    child.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                child.stdin.close()

        threading.Thread(target=upstream, daemon=True).start()
        for line in child.stdout:
            tracker.response(line)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        return child.wait()
    finally:
        stop.set()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
