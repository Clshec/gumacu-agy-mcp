"""Tests for response reliability, timeout detection, transcript recovery, and agy_result_page.

Covers:
1. CLI returns valid response with Exit Code 0 -> completed
2. CLI returns Exit Code 0 with empty response -> no_final_response
3. CLI terminates due to inner print timeout -> timeout
4. CLI times out but partial response is recovered from transcript -> timeout + partial text
5. CLI returns upstream error -> upstream_error
6. CLI terminates with non-zero exit code -> failed
7. CLI writes response at the very last moment before exit
8. CLI produces a long response that requires pagination
9. Re-reading the result after process completion is idempotent and durable
10. Consistency between agy_status, agy_read, and agy_result_page
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agy_mcp import server
from agy_mcp.adapters.agy import recover_assistant_text_from_transcript
from agy_mcp.adapters.base import AdapterRunResult, BaseAdapter
from agy_mcp.config import Config
from agy_mcp.models import BridgeRequest, CanonicalEvent, Capability
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import Supervisor


class _FakeAdapter(BaseAdapter):
    backend = "agy"

    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        had_timeout: bool = False,
        timeout_text: str | None = None,
        had_upstream_error: bool = False,
        upstream_error_text: str | None = None,
        events: list[CanonicalEvent] | None = None,
    ) -> None:
        super().__init__()
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._had_timeout = had_timeout
        self._timeout_text = timeout_text
        self._had_upstream_error = had_upstream_error
        self._upstream_error_text = upstream_error_text
        self._events = events or []

    def _probe(self) -> Capability:
        return Capability(
            bin_path="/fake/agy",
            backend="agy",
            version="1.3.5",
            supports_print=True,
            authenticated=True,
        )

    def build_command(self, request: BridgeRequest, *, log_path: Path | None) -> list[str]:
        return ["/fake/agy", f"--print={request.prompt}"]

    def run(self, request, **kwargs):
        event_sink = kwargs.get("event_sink")
        session_id = request.session_id or "sess_test_123"
        emitted_events = list(self._events)

        if self._stdout.strip():
            emitted_events.append(
                CanonicalEvent(
                    type="assistant",
                    subtype="text",
                    session_id=session_id,
                    text=self._stdout,
                )
            )

        if self._had_timeout:
            emitted_events.append(
                CanonicalEvent(
                    type="result",
                    subtype="timeout",
                    session_id=session_id,
                    text=self._timeout_text or "print timeout",
                )
            )
        elif self._had_upstream_error:
            emitted_events.append(
                CanonicalEvent(
                    type="result",
                    subtype="upstream_error",
                    session_id=session_id,
                    text=self._upstream_error_text or "upstream error",
                )
            )
        elif self._exit_code == 0:
            if self._stdout.strip():
                emitted_events.append(
                    CanonicalEvent(
                        type="result",
                        subtype="success",
                        session_id=session_id,
                    )
                )
            else:
                emitted_events.append(
                    CanonicalEvent(
                        type="result",
                        subtype="no_final_response",
                        session_id=session_id,
                        text="process finished with exit code 0 but produced no response",
                    )
                )
        else:
            emitted_events.append(
                CanonicalEvent(
                    type="result",
                    subtype="error",
                    session_id=session_id,
                    text=f"exit code {self._exit_code}",
                )
            )

        if event_sink:
            for ev in emitted_events:
                event_sink.emit(ev)

        return AdapterRunResult(
            events=emitted_events,
            session_id=session_id,
            exit_code=self._exit_code,
            duration_ms=100,
            stdout_tail=self._stdout,
            stderr_tail=self._stderr,
            log_path=None,
            artifacts=[],
            had_upstream_error=self._had_upstream_error,
            upstream_error_text=self._upstream_error_text,
            had_timeout=self._had_timeout,
            timeout_text=self._timeout_text,
        )


@pytest.fixture
def mock_supervisor_env(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    config = Config()
    safety = SafetyPolicy()

    def _factory(adapter_instance):
        return Supervisor(
            store=store,
            config=config,
            safety=safety,
            adapter_factory=lambda req, cfg, sft: (adapter_instance, []),
        )

    return SimpleNamespace(store=store, config=config, safety=safety, make_supervisor=_factory)


def test_1_cli_returns_valid_response_completed(mock_supervisor_env, monkeypatch):
    """1. CLI returns valid response with Exit Code 0 -> completed."""
    adapter = _FakeAdapter(exit_code=0, stdout="Detailed solution report.")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    resp = supervisor.start(
        BridgeRequest(prompt="do task", cwd=".", mode="execute", allow_write=True, worktree=False, timeout=60),
        job_id="job_success_test",
    )
    assert resp.status == "running"

    # Wait for thread to finish
    for _ in range(50):
        rec = supervisor.status("job_success_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_success_test")
    assert rec is not None
    assert rec.status == "completed"
    assert rec.exit_code == 0
    assert rec.error is None

    # Verify agy_result_page
    page = server.agy_result_page_tool("job_success_test")
    assert page["success"] is True
    assert page["status"] == "completed"
    assert page["text"] == "Detailed solution report."
    assert page["total_chars"] == len("Detailed solution report.")


def test_2_cli_returns_exit_0_empty_stdout_no_final_response(mock_supervisor_env, monkeypatch):
    """2. CLI returns Exit Code 0 without any response -> no_final_response."""
    adapter = _FakeAdapter(exit_code=0, stdout="")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="do task", cwd=".", mode="execute", allow_write=True, worktree=False, timeout=60),
        job_id="job_empty_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_empty_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_empty_test")
    assert rec is not None
    assert rec.status == "no_final_response"
    assert rec.exit_code == 0
    assert "no final response" in (rec.error or "")

    # agy_result_page MUST NOT report success=True with 0 chars
    page = server.agy_result_page_tool("job_empty_test")
    assert page["success"] is False
    assert page["status"] == "no_final_response"
    assert page["total_chars"] == 0
    assert page["text"] == ""
    assert "no final response" in page["error"]


def test_3_cli_terminates_due_to_inner_timeout_detected(mock_supervisor_env, monkeypatch):
    """3. CLI terminates due to internal print timeout -> timeout."""
    adapter = _FakeAdapter(
        exit_code=0,
        stdout="",
        stderr="[agy] print timeout after 19m30s with turn in progress; returning partial output",
        had_timeout=True,
        timeout_text="[agy] print timeout after 19m30s with turn in progress",
    )
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="long task", cwd=".", mode="execute", allow_write=True, worktree=False, timeout=1200),
        job_id="job_timeout_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_timeout_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_timeout_test")
    assert rec is not None
    assert rec.status == "timeout"
    assert "print timeout" in (rec.error or "")

    # agy_result_page returns success=False and explicit timeout status
    page = server.agy_result_page_tool("job_timeout_test")
    assert page["success"] is False
    assert page["status"] == "timeout"
    assert "print timeout" in page["error"]
    assert page["total_chars"] == 0


def test_4_cli_timeout_with_partial_response_recovered_from_transcript(tmp_path, monkeypatch):
    """4. CLI times out but partial response is recovered from session transcript."""
    session_id = "04c70691-8a9c-40ff-a82e-c56a9af2ab7a"
    fake_brain = tmp_path / "brain"
    session_logs = fake_brain / session_id / ".system_generated" / "logs"
    session_logs.mkdir(parents=True)

    transcript_file = session_logs / "transcript.jsonl"
    report_content = "### تقرير مهمة 5B: تشغيل ما تبقى من الـ CI المستهدف\n\nتم تنفيذ المهام بنجاح."
    lines = [
        json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
        json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": report_content, "status": "DONE"}),
        json.dumps({"step_index": 2, "type": "GENERIC", "source": "MODEL", "status": "DONE"}),
    ]
    transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("ANTIGRAVITY_BRAIN_DIR", str(fake_brain))

    recovered = recover_assistant_text_from_transcript(session_id)
    assert recovered == report_content

    # Now verify supervisor and agy_result_page when partial text is present during timeout
    store = SessionStore(tmp_path / "sessions")
    config = Config()
    safety = SafetyPolicy()

    # Create adapter that simulates recovered text
    adapter = _FakeAdapter(
        exit_code=0,
        stdout=report_content,
        had_timeout=True,
        timeout_text="print timeout after 19m30s",
    )
    supervisor = Supervisor(
        store=store,
        config=config,
        safety=safety,
        adapter_factory=lambda req, cfg, sft: (adapter, []),
    )
    monkeypatch.setattr(server, "_ensure_state", lambda: (config, safety, store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="long task", session_id=session_id, cwd=".", mode="execute", allow_write=True, worktree=False, timeout=1200),
        job_id="job_recovered_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_recovered_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_recovered_test")
    assert rec.status == "timeout"

    page = server.agy_result_page_tool("job_recovered_test")
    # Must report success=False, partial=True, and contain the recovered report
    assert page["success"] is False
    assert page["status"] == "timeout"
    assert page.get("partial") is True
    assert page["text"] == report_content
    assert page["total_chars"] == len(report_content)


def test_5_cli_returns_upstream_error(mock_supervisor_env, monkeypatch):
    """5. CLI returns upstream error -> upstream_error."""
    adapter = _FakeAdapter(
        exit_code=0,
        had_upstream_error=True,
        upstream_error_text="FAILED_PRECONDITION: account is blocked",
    )
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="task", cwd=".", mode="ask"),
        job_id="job_upstream_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_upstream_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_upstream_test")
    assert rec.status == "upstream_error"
    assert "FAILED_PRECONDITION" in (rec.error or "")

    page = server.agy_result_page_tool("job_upstream_test")
    assert page["success"] is False
    assert page["status"] == "upstream_error"


def test_6_cli_terminates_with_nonzero_exit(mock_supervisor_env, monkeypatch):
    """6. CLI terminates with non-zero exit code -> failed."""
    adapter = _FakeAdapter(exit_code=137, stderr="Killed by OOM")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="task", cwd=".", mode="ask"),
        job_id="job_fail_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_fail_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    rec = supervisor.status("job_fail_test")
    assert rec.status == "failed"
    assert rec.exit_code == 137

    page = server.agy_result_page_tool("job_fail_test")
    assert page["success"] is False
    assert page["status"] == "failed"


def test_7_cli_writes_response_last_moment(mock_supervisor_env, monkeypatch):
    """7. CLI writes response at the very last moment before exit."""
    adapter = _FakeAdapter(exit_code=0, stdout="Final burst reply")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="burst", cwd=".", mode="ask"),
        job_id="job_burst_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_burst_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    page = server.agy_result_page_tool("job_burst_test")
    assert page["success"] is True
    assert page["text"] == "Final burst reply"


def test_8_pagination_across_pages(mock_supervisor_env, monkeypatch):
    """8. CLI produces a long response that requires pagination."""
    big_text = "Paragraph number " * 2000  # ~34,000 chars
    adapter = _FakeAdapter(exit_code=0, stdout=big_text)
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="big", cwd=".", mode="ask"),
        job_id="job_big_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_big_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    offset = 0
    chunks = []
    digest = None
    while True:
        page = server.agy_result_page_tool("job_big_test", offset=offset, limit=10000, expected_sha256=digest)
        assert page["success"] is True
        chunks.append(page["text"])
        offset = page["next_offset"]
        digest = page["sha256"]
        if not page["has_more"]:
            break

    assert "".join(chunks) == big_text
    assert offset == len(big_text)


def test_9_reread_result_after_completion(mock_supervisor_env, monkeypatch):
    """9. Re-reading the result after process completion is idempotent and durable."""
    adapter = _FakeAdapter(exit_code=0, stdout="Idempotent output")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="idempotent", cwd=".", mode="ask"),
        job_id="job_idempotent_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_idempotent_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    # First read
    page1 = server.agy_result_page_tool("job_idempotent_test")
    # Second read
    page2 = server.agy_result_page_tool("job_idempotent_test")

    assert page1 == page2
    assert page1["success"] is True
    assert page1["text"] == "Idempotent output"


def test_10_consistency_across_status_read_result_page(mock_supervisor_env, monkeypatch):
    """10. Consistency between agy_status, agy_read, and agy_result_page."""
    adapter = _FakeAdapter(exit_code=0, stdout="Consistency test payload")
    supervisor = mock_supervisor_env.make_supervisor(adapter)
    monkeypatch.setattr(server, "_ensure_state", lambda: (mock_supervisor_env.config, mock_supervisor_env.safety, mock_supervisor_env.store, supervisor))

    supervisor.start(
        BridgeRequest(prompt="consistent", cwd=".", mode="ask"),
        job_id="job_consistent_test",
    )

    for _ in range(50):
        rec = supervisor.status("job_consistent_test")
        if rec and rec.status != "running":
            break
        time.sleep(0.05)

    status_resp = server.agy_status_tool("job_consistent_test")
    assert status_resp.success is True
    assert status_resp.record.status == "completed"

    read_resp = server.agy_read_tool("job_consistent_test")
    assert read_resp.success is True
    assert read_resp.count > 0

    page_resp = server.agy_result_page_tool("job_consistent_test")
    assert page_resp["success"] is True
    assert page_resp["status"] == "completed"
    assert page_resp["text"] == "Consistency test payload"
