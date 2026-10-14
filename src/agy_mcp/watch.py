"""Terminal watcher for Antigravity jobs (Mode 2: Manual Terminal Monitoring).

Displays real-time Antigravity thought, text, tool calls, and completion status.
When the job finishes, prints a clear completion banner and the exact instruction
to give to ChatGPT with the returned SESSION_ID and job_id.

Usage:
    agy-watch --latest
    agy-watch --job job_1791947576_439fabc9a2af
    python -m agy_mcp.watch --latest
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from agy_mcp.config import get_config
from agy_mcp.models import CanonicalEvent, JobRecord
from agy_mcp.session_store import SessionStore


# ANSI Colors with fallback
class Colors:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    @property
    def cyan(self) -> str:
        return "\033[36m" if self.enabled else ""

    @property
    def green(self) -> str:
        return "\033[32m" if self.enabled else ""

    @property
    def yellow(self) -> str:
        return "\033[33m" if self.enabled else ""

    @property
    def red(self) -> str:
        return "\033[31m" if self.enabled else ""

    @property
    def bold(self) -> str:
        return "\033[1m" if self.enabled else ""

    @property
    def dim(self) -> str:
        return "\033[2m" if self.enabled else ""

    @property
    def reset(self) -> str:
        return "\033[0m" if self.enabled else ""


def find_latest_job(store: SessionStore) -> JobRecord | None:
    """Find the most recently created or updated job in the store."""
    recent = store.list_jobs(limit=1)
    if recent:
        return recent[0]

    # Fallback to inspecting job directories directly
    candidates = []
    if store.root.is_dir():
        for entry in store.root.iterdir():
            if entry.is_dir() and entry.name.startswith("job_"):
                meta_file = entry / "meta.json"
                if meta_file.is_file():
                    try:
                        candidates.append((meta_file.stat().st_mtime, entry.name))
                    except OSError:
                        pass
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return store.get_job(candidates[0][1])


def format_event(event: CanonicalEvent, c: Colors) -> str:
    """Format a single CanonicalEvent for terminal output."""
    ts_str = event.ts[11:19] if len(event.ts) >= 19 else ""
    prefix = f"{c.dim}[{ts_str}]{c.reset} " if ts_str else ""

    if event.type == "assistant":
        text = event.text or ""
        return f"{prefix}{c.cyan}{text}{c.reset}"
    elif event.type == "tool_use":
        name = event.subtype or "tool"
        meta = str(event.metadata) if event.metadata else ""
        return f"{prefix}{c.yellow}⚙ Tool call: {c.bold}{name}{c.reset} {meta}"
    elif event.type == "tool_result":
        return f"{prefix}{c.dim}↳ Tool output: {event.text or ''}{c.reset}"
    elif event.type == "error":
        return f"{prefix}{c.red}✖ Error ({event.subtype or 'error'}): {event.text or ''}{c.reset}"
    elif event.type == "result":
        return f"{prefix}{c.green}✔ Completed turn: {event.subtype or ''}{c.reset}"
    elif event.type == "system":
        return f"{prefix}{c.dim}ℹ System: {event.text or ''}{c.reset}"
    return f"{prefix}{event.type}: {event.text or ''}"


def watch_job(
    store: SessionStore,
    job_id: str,
    *,
    poll_interval: float = 1.0,
    colors: Colors | None = None,
    timeout: float | None = None,
) -> int:
    """Watch and stream events for a specific job until completion."""
    c = colors or Colors(enabled=sys.stdout.isatty())
    record = store.get_job(job_id)
    if not record:
        print(f"{c.red}Error: Job {job_id} not found in {store.root}{c.reset}", file=sys.stderr)
        return 1

    prompt = ""
    if isinstance(record.request, dict):
        prompt = record.request.get("prompt") or record.request.get("PROMPT") or ""

    print(f"\n{c.bold}{'=' * 78}{c.reset}")
    print(f"{c.bold}🔍 Gumacu Antigravity Job Watcher{c.reset}")
    print(f"{'-' * 78}")
    print(f"{c.bold}Job ID:{c.reset}     {c.cyan}{record.job_id}{c.reset}")
    print(f"{c.bold}Session ID:{c.reset} {record.session_id or '(pending)'}")
    print(f"{c.bold}Workspace:{c.reset}  {record.cwd or '(default)'}")
    print(f"{c.bold}Backend:{c.reset}    {record.backend or 'agy'}")
    if prompt:
        first_line = prompt.strip().splitlines()[0]
        preview = (first_line[:70] + "...") if len(first_line) > 70 else first_line
        print(f"{c.bold}Prompt:{c.reset}     {preview}")
    print(f"{'=' * 78}\n")

    cursor = 0
    start_time = time.time()
    last_status = record.status

    # Initial events backlog
    events = store.read_events(job_id, since=cursor)
    for ev in events:
        line = format_event(ev, c)
        if line:
            print(line, flush=True)
    cursor += len(events)

    while True:
        if timeout and (time.time() - start_time) > timeout:
            print(f"\n{c.yellow}Watcher timed out after {timeout}s.{c.reset}", file=sys.stderr)
            return 2

        fresh_record = store.get_job(job_id)
        if fresh_record:
            record = fresh_record

        # Check for new events
        new_events = store.read_events(job_id, since=cursor)
        if new_events:
            for ev in new_events:
                line = format_event(ev, c)
                if line:
                    print(line, flush=True)
            cursor += len(new_events)

        if record.status in {"completed", "failed", "cancelled", "upstream_error"}:
            break

        if record.status != last_status:
            print(f"{c.dim}Status update: {record.status}{c.reset}", flush=True)
            last_status = record.status

        time.sleep(poll_interval)

    # Read any trailing events written right before finalization
    trailing = store.read_events(job_id, since=cursor)
    for ev in trailing:
        line = format_event(ev, c)
        if line:
            print(line, flush=True)

    # Fetch result summary
    final_text = ""
    paths = store.paths(job_id) if hasattr(store, "paths") else None
    if paths and paths.stdout.is_file():
        try:
            final_text = paths.stdout.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    duration = int(time.time() - start_time)
    status_color = c.green if record.status == "completed" else c.red
    status_icon = "✔" if record.status == "completed" else "✖"

    print(f"\n{c.bold}{'=' * 78}{c.reset}")
    print(f"{status_color}{c.bold}{status_icon} JOB FINISHED: {record.status.upper()} ({duration}s){c.reset}")
    print(f"{'-' * 78}")
    print(f"{c.bold}Job ID:{c.reset}     {record.job_id}")
    print(f"{c.bold}Session ID:{c.reset} {c.cyan}{record.session_id or 'None'}{c.reset}")
    if record.error:
        print(f"{c.red}{c.bold}Error:{c.reset}      {record.error}")

    if final_text:
        preview_lines = final_text.splitlines()[:10]
        preview_sample = "\n".join(preview_lines)
        if len(preview_lines) < len(final_text.splitlines()):
            preview_sample += f"\n{c.dim}... [+{len(final_text.splitlines()) - len(preview_lines)} more lines]{c.reset}"
        print(f"\n{c.bold}📄 Result Preview:{c.reset}")
        print(f"{'-' * 78}")
        print(preview_sample)

    print(f"\n{c.bold}{'-' * 78}{c.reset}")
    print(f"{c.yellow}{c.bold}📢 NEXT STEP FOR CHATGPT (Manual Mode):{c.reset}")
    print("Copy and send the following message into your ChatGPT chat:")
    print()
    sess_arg = f", SESSION_ID='{record.session_id}'" if record.session_id else ""
    print(
        f'{c.bold}Antigravity انتهى من المهمة بنجاح. '
        f'استرجع النتيجة الكاملة عبر أداة: '
        f'agy_result_page(job_id="{record.job_id}", offset=0, limit=16000) '
        f'ثم تابع الأوامر التالية في نفس الجلسة ({sess_arg.strip(", ")}).{c.reset}'
    )
    print(f"{'=' * 78}\n")

    return 0 if record.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor real-time Antigravity jobs and events.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--latest",
        action="store_true",
        help="Monitor the most recent job in the session store.",
    )
    group.add_argument(
        "--job",
        type=str,
        help="Specific job ID to monitor.",
    )
    parser.add_argument(
        "--session-root",
        type=Path,
        default=None,
        help="Custom path to the session store root.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color codes in output.",
    )
    parser.add_argument(
        "--wait-new",
        action="store_true",
        help="When used with --latest, wait for a new job to start before watching.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Optional timeout in seconds to wait for completion.",
    )

    args = parser.parse_args(argv)

    if args.session_root is not None:
        root = args.session_root
    elif os.environ.get("AGY_MCP_SESSION_ROOT"):
        root = Path(os.environ["AGY_MCP_SESSION_ROOT"]).expanduser()
    else:
        root = Path(get_config().session_store_root()).expanduser()

    store = SessionStore(root)
    colors = Colors(enabled=not args.no_color and sys.stdout.isatty())

    if args.latest:
        if args.wait_new:
            initial = find_latest_job(store)
            initial_id = initial.job_id if initial else None
            print(f"Waiting for a new job to start in {store.root}...")
            job = None
            start_wait = time.time()
            while True:
                if args.timeout and (time.time() - start_wait) > args.timeout:
                    print("Timed out waiting for new job.", file=sys.stderr)
                    return 2
                time.sleep(args.poll_interval)
                curr = find_latest_job(store)
                if curr and curr.job_id != initial_id:
                    job = curr
                    break
        else:
            job = find_latest_job(store)
            if not job:
                print(f"No jobs found in {store.root}. Waiting for a job to start...")
                start_wait = time.time()
                while not job:
                    if args.timeout and (time.time() - start_wait) > args.timeout:
                        print("Timed out waiting for job.", file=sys.stderr)
                        return 2
                    time.sleep(args.poll_interval)
                    job = find_latest_job(store)
        job_id = job.job_id
    else:
        job_id = args.job

    return watch_job(
        store,
        job_id,
        poll_interval=args.poll_interval,
        colors=colors,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
