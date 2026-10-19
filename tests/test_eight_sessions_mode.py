"""Gumacu's eight-slot interactive orchestration contract."""

from __future__ import annotations

import pytest

from agy_mcp import server
from agy_mcp.orchestrator import StepDefinition
from agy_mcp.supervisor import Supervisor


def test_supervisor_default_is_eight_concurrent_slots():
    import inspect

    param = inspect.signature(Supervisor.__init__).parameters["max_concurrent_jobs"]
    assert param.default == 8


def test_mcp_request_has_no_deadline_even_when_caller_supplies_one(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "local")
    for requested in (0, 240, 900, 3600, 86400):
        req = server._build_request({
            "prompt": "Read-only check",
            "cwd": str(tmp_path),
            "timeout": requested,
            "backend": "agy",
        })
        assert req.timeout == 0


def test_new_pipeline_step_has_no_wallclock_deadline():
    step = StepDefinition(step_id="read", prompt="Inspect the service")
    assert step.timeout == 0


def test_invalid_deadlines_are_rejected_before_normalization(monkeypatch, tmp_path):
    monkeypatch.setenv("GUMACU_REMOTE_PROFILE", "local")
    for invalid in (-1, 86401):
        with pytest.raises(ValueError):
            server._build_request({
                "prompt": "read only", "cwd": str(tmp_path), "timeout": invalid,
            })
