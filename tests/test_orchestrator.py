"""Tests for Local Codex Step Chaining & Orchestrator."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agy_mcp.models import JobRecord
from agy_mcp.orchestrator import Orchestrator, PipelineExecution
from agy_mcp.session_store import SessionStore


def test_orchestrator_create_and_persist_pipeline(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = MagicMock()
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
    mock_supervisor = MagicMock()
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
    mock_supervisor.start_job.return_value = JobRecord(
        job_id="job_step_1",
        status="running",
        cwd="/work",
        request={"prompt": "Do task 1"},
        session_id="sess_chain",
    )

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
    mock_supervisor.start_job.return_value = JobRecord(
        job_id="job_step_2",
        status="running",
        cwd="/work",
        request={"prompt": "Do task 2"},
        session_id="sess_chain",
    )

    orchestrator._tick()

    pipe = orchestrator.get_pipeline(pipe.pipeline_id)
    assert pipe.steps[0].status == "completed"
    assert pipe.current_step_index == 1
    assert pipe.steps[1].status == "running"
    assert pipe.steps[1].job_id == "job_step_2"

    # Tick 4: Step 2 completed -> Pipeline fully completed
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
    mock_supervisor = MagicMock()
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
    mock_supervisor.start_job.return_value = JobRecord(
        job_id="job_fail_1",
        status="running",
        cwd="/work",
        request={},
    )
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


def test_orchestrator_cancellation(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    mock_supervisor = MagicMock()
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
    mock_supervisor.start_job.return_value = JobRecord(
        job_id="job_to_cancel",
        status="running",
        cwd="/work",
        request={},
    )
    orchestrator._tick()

    assert orchestrator.cancel_pipeline(pipe.pipeline_id) is True
    mock_supervisor.cancel_job.assert_called_once_with("job_to_cancel")

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
