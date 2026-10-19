"""Tests for Local Codex Step Chaining & Orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agy_mcp.models import BridgeResponse, JobRecord
from agy_mcp.orchestrator import Orchestrator
from agy_mcp.session_store import SessionStore


def _mock_supervisor() -> MagicMock:
    """Use the real public surface so renamed methods fail tests immediately."""

    return MagicMock(spec_set=["start", "status", "cancel", "store"])


def _started(job_id: str, session_id: str = "") -> BridgeResponse:
    return BridgeResponse(
        success=True,
        job_id=job_id,
        status="running",
        SESSION_ID=session_id,
        cwd="/work",
    )


def test_orchestrator_create_and_persist_pipeline(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    state_file = tmp_path / "orchestrator_state.json"

    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=state_file,
        auto_start_worker=False,
    )

    pipe = orchestrator.create_pipeline(
        name="Build and Test Pipeline",
        steps=[
            {"step_id": "step_code", "prompt": "Generate auth module", "mode": "smart"},
            {"step_id": "step_test", "prompt": "Run pytest tests/", "mode": "smart"},
        ],
        session_id="sess_root_123",
        auto_start=False,
    )

    assert pipe.pipeline_id.startswith("pipe_")
    assert pipe.name == "Build and Test Pipeline"
    assert len(pipe.steps) == 2
    assert pipe.steps[0].step_id == "step_code"
    assert pipe.steps[1].step_id == "step_test"
    assert pipe.status == "pending"

    # Verify disk persistence and reload
    reloaded = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=state_file,
        auto_start_worker=False,
    )
    loaded_pipe = reloaded.get_pipeline(pipe.pipeline_id)
    assert loaded_pipe is not None
    assert loaded_pipe.pipeline_id == pipe.pipeline_id
    assert len(loaded_pipe.steps) == 2
    assert loaded_pipe.session_id == "sess_root_123"


def test_orchestrator_step_chaining_success(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    mock_supervisor.store = store
    state_file = tmp_path / "orchestrator_state.json"

    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=state_file,
        auto_start_worker=False,
    )

    pipe = orchestrator.create_pipeline(
        name="Sequential Pipeline",
        steps=[
            {"step_id": "s1", "prompt": "Do task 1"},
            {"step_id": "s2", "prompt": "Do task 2"},
        ],
        auto_start=True,
    )

    # Tick 1: Starts Step 1
    mock_supervisor.start.return_value = _started("job_step_1", "sess_chain")

    orchestrator._tick()

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.status == "running"
    assert pipe.current_step_index == 0
    assert pipe.steps[0].status == "running"
    assert pipe.steps[0].job_id == "job_step_1"

    # Tick 2: Step 1 still running
    mock_supervisor.status.return_value = JobRecord(
        job_id="job_step_1",
        status="running",
        cwd="/work",
        request={"prompt": "Do task 1"},
    )
    orchestrator._tick()
    assert orchestrator.get_pipeline(pipe.pipeline_id).current_step_index == 0

    # Tick 3: Step 1 completed -> advances to Step 2
    mock_supervisor.status.return_value = JobRecord(
        job_id="job_step_1",
        status="completed",
        cwd="/work",
        request={"prompt": "Do task 1"},
        finished_at="2026-10-15T12:00:00Z",
    )
    mock_supervisor.start.return_value = _started("job_step_2", "sess_chain")

    orchestrator._tick()

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.steps[0].status == "completed"
    assert pipe.current_step_index == 1
    assert pipe.steps[1].status == "pending"
    assert pipe.steps[1].job_id is None

    # Tick 4: Start step 2 after persisting the completed first step.
    orchestrator._tick()
    assert pipe.steps[1].status == "running"
    assert pipe.steps[1].job_id == "job_step_2"

    # Tick 5: Step 2 completed -> Pipeline fully completed
    mock_supervisor.status.return_value = JobRecord(
        job_id="job_step_2",
        status="completed",
        cwd="/work",
        request={"prompt": "Do task 2"},
        finished_at="2026-10-15T12:01:00Z",
    )

    orchestrator._tick()

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.steps[1].status == "completed"
    assert pipe.current_step_index == 2
    assert pipe.status == "completed"
    assert pipe.finished_at is not None


def test_orchestrator_step_failure_stops_pipeline(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    mock_supervisor.store = store
    state_file = tmp_path / "orchestrator_state.json"

    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=state_file,
        auto_start_worker=False,
    )

    pipe = orchestrator.create_pipeline(
        name="Failing Pipeline",
        steps=[
            {"step_id": "s1", "prompt": "Failing task"},
            {"step_id": "s2", "prompt": "Should not run"},
        ],
        auto_start=True,
    )

    # Launch step 1
    mock_supervisor.start.return_value = _started("job_fail_1")
    orchestrator._tick()

    # Step 1 reports failed
    mock_supervisor.status.return_value = JobRecord(
        job_id="job_fail_1",
        status="failed",
        cwd="/work",
        request={},
        error="SyntaxError: invalid syntax",
    )
    orchestrator._tick()

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.status == "failed"
    assert pipe.steps[0].status == "failed"
    assert "SyntaxError" in pipe.steps[0].error
    # Step 2 remained pending and never ran
    assert pipe.steps[1].status == "pending"
    assert pipe.steps[1].job_id is None


def test_orchestrator_uses_supervisor_contract_and_preserves_step_options(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=tmp_path / "orchestrator_state.json",
        auto_start_worker=False,
    )
    pipe = orchestrator.create_pipeline(
        name="Configured Pipeline",
        steps=[{
            "prompt": "Run configured task",
            "backend": "agy",
            "sandbox": True,
            "allow_write": True,
            "worktree": False,
            "output_protocol": "raw",
        }],
        auto_start=True,
    )
    mock_supervisor.start.return_value = _started("job_configured")

    orchestrator._tick()

    request = mock_supervisor.start.call_args.args[0]
    assert request.backend == "agy"
    assert request.sandbox is True
    assert request.allow_write is True
    assert request.worktree is False
    assert request.output_protocol == "raw"
    assert orchestrator.get_pipeline(pipe.pipeline_id).steps[0].job_id == "job_configured"


def test_orchestrator_records_supervisor_start_rejection(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=tmp_path / "orchestrator_state.json",
        auto_start_worker=False,
    )
    pipe = orchestrator.create_pipeline(
        name="Rejected Pipeline",
        steps=[{"prompt": "Rejected task"}],
        auto_start=True,
    )
    mock_supervisor.start.return_value = BridgeResponse(
        success=False,
        status="unknown",
        error="policy rejected",
    )

    orchestrator._tick()

    current = orchestrator.get_pipeline(pipe.pipeline_id)
    assert current.status == "failed"
    assert current.steps[0].status == "failed"
    assert current.steps[0].error == "policy rejected"


def test_orchestrator_cancellation(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = _mock_supervisor()
    state_file = tmp_path / "orchestrator_state.json"

    orchestrator = Orchestrator(
        store=store,
        supervisor=mock_supervisor,
        state_path=state_file,
        auto_start_worker=False,
    )

    pipe = orchestrator.create_pipeline(
        name="Cancel Pipeline",
        steps=[{"step_id": "s1", "prompt": "Long task"}],
        auto_start=True,
    )
    mock_supervisor.start.return_value = _started("job_to_cancel")
    orchestrator._tick()

    assert orchestrator.cancel_pipeline(pipe.pipeline_id) is True
    mock_supervisor.cancel.assert_called_once_with("job_to_cancel")

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.status == "cancelled"
    assert pipe.steps[0].status == "cancelled"


def test_server_pipeline_tools(tmp_path: Path):
    from agy_mcp.server import (
        _reset_state_for_tests,
        agy_pipeline_cancel,
        agy_pipeline_start,
        agy_pipeline_status,
    )

    _reset_state_for_tests()

    # Start pipeline
    res_start = agy_pipeline_start(
        name="Tool Test Pipeline",
        steps=[{"step_id": "t1", "prompt": "Echo 1"}],
    )
    assert res_start["success"] is True
    pipe_id = res_start["pipeline_id"]
    assert pipe_id.startswith("pipe_")

    # Check status
    res_status = agy_pipeline_status(pipeline_id=pipe_id)
    assert res_status["success"] is True
    assert res_status["pipeline"]["pipeline_id"] == pipe_id
    assert res_status["pipeline"]["name"] == "Tool Test Pipeline"

    # Cancel
    res_cancel = agy_pipeline_cancel(pipeline_id=pipe_id)
    assert res_cancel["success"] is True
    assert res_cancel["status"] == "cancelled"

    _reset_state_for_tests()


def test_eight_concurrent_pipelines_and_ninth_waits_in_durable_queue(tmp_path: Path):
    """A busy supervisor never stops the first eight jobs or fails the ninth."""
    store = SessionStore(tmp_path / "sessions")
    supervisor = _mock_supervisor()
    state = tmp_path / "queue.json"
    scheduler = Orchestrator(
        store=store, supervisor=supervisor, state_path=state,
        auto_start_worker=False,
    )
    pipes = [
        scheduler.create_pipeline(
            name=f"Independent job {i}", steps=[{"prompt": f"job {i}"}],
            auto_start=True,
        )
        for i in range(9)
    ]
    supervisor.start.side_effect = [
        *[_started(f"job_active_{i}") for i in range(8)],
        BridgeResponse(success=False, error="supervisor busy: 8 concurrent jobs already running"),
    ]
    scheduler._tick()
    for pipe in pipes[:8]:
        assert pipe.status == "running"
        assert pipe.steps[0].status == "running"
    assert pipes[8].status == "running"
    assert pipes[8].steps[0].status == "pending"
    assert pipes[8].steps[0].job_id is None

    # The queue survives a new controller instance / ChatGPT conversation.
    reloaded = Orchestrator(
        store=store, supervisor=supervisor, state_path=state,
        auto_start_worker=False,
    )
    ninth = reloaded.get_pipeline(pipes[8].pipeline_id)
    assert ninth is not None and ninth.steps[0].status == "pending"
    supervisor.status.return_value = JobRecord(
        job_id="job_unchanged", status="running", cwd="/work", request={},
    )
    supervisor.start.side_effect = None
    supervisor.start.return_value = _started("job_queued_9")
    reloaded._tick()
    assert ninth.steps[0].status == "running"
    assert ninth.steps[0].job_id == "job_queued_9"
    assert all(p.steps[0].status == "running" for p in reloaded.list_pipelines(limit=20))


def test_pipeline_timeout_is_always_unlimited_for_new_and_old_step_records(tmp_path: Path):
    from agy_mcp.orchestrator import StepDefinition

    store = SessionStore(tmp_path / "sessions")
    supervisor = _mock_supervisor()
    scheduler = Orchestrator(
        store=store, supervisor=supervisor,
        state_path=tmp_path / "state.json", auto_start_worker=False,
    )
    assert StepDefinition(step_id="one", prompt="work").timeout == 0
    pipeline = scheduler.create_pipeline(
        name="No task deadlines",
        steps=[{"prompt": "first", "timeout": 240},
               StepDefinition(step_id="second", prompt="second", timeout=900)],
        auto_start=True,
    )
    assert [step.timeout for step in pipeline.steps] == [0, 0]
    # A legacy pipeline loaded from disk may still carry timeout=900, but it
    # MUST NOT reach agy --print-timeout.
    pipeline.steps[0].timeout = 900
    supervisor.start.return_value = _started("job_no_timeout")
    scheduler._tick()
    assert supervisor.start.call_args.args[0].timeout == 0

    supervisor.status.return_value = JobRecord(
        job_id="job_no_timeout", status="completed", cwd="/work", request={},
    )
    scheduler._tick()  # complete first, keep next queued
    supervisor.start.return_value = _started("job_no_timeout_second")
    scheduler._tick()
    assert supervisor.start.call_args.args[0].timeout == 0
