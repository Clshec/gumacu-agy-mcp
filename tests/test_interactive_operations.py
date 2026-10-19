"""Core acceptance tests for ChatGPT-led, checkpointed micro-operations."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from agy_mcp.interactive_operations import InteractiveOperations


def _reply(**kwargs):
    return SimpleNamespace(**kwargs)


def test_chatgpt_leads_each_step_and_review_is_a_hard_gate(tmp_path):
    manager = InteractiveOperations(tmp_path)
    opened = manager.create("Audit Odoo safely")
    op = opened["operation_id"]
    calls = []

    def launch():
        calls.append("launched")
        return _reply(success=True, job_id="job_one", error=None)

    first = manager.begin(op, "idempotent001", "Check status", "Inspect Odoo status", launch)
    assert first["success"] and first["job_id"] == "job_one"
    assert calls == ["launched"]
    assert manager.begin(op, "nextstep001", "Modify", "change things", launch)["success"] is False
    assert manager.collect(
        op, lambda job: _reply(status="running"),
        lambda job: _reply(success=True, result_text=""),
    )["step_status"] == "running"
    result = manager.collect(
        op, lambda job: _reply(status="completed"),
        lambda job: _reply(success=True, result_text="Observed healthy pods"),
    )
    assert result["result_text"] == "Observed healthy pods"
    assert result["next_action"] == "operation_review"
    assert not manager.begin(op, "nextstep001", "Modify", "change things", launch)["success"]

    accepted = manager.review(op, True, "Independent kubectl inspection confirmed desired replicas")
    assert accepted["success"] and accepted["accepted"]
    second = manager.begin(op, "nextstep001", "Next check", "Review Git commit", launch)
    assert second["success"] and second["step_number"] == 2

    # Preserve only hashed evidence and prompt digests, not secret-bearing text.
    with sqlite3.connect(manager.path) as db:
        stored = "\n".join(str(v) for row in db.execute("SELECT * FROM steps") for v in row)
    assert "kubectl inspection" not in stored
    assert "Inspect Odoo" not in stored
    assert "Review Git commit" not in stored


def test_eight_jobs_can_run_independently_and_ninth_does_not_interrupt(tmp_path):
    manager = InteractiveOperations(tmp_path)
    ids = [manager.create("Task " + str(i))["operation_id"] for i in range(9)]
    calls = []
    for n in range(8):
        started = manager.begin(
            ids[n], f"request{n:03d}", "One step", "Inspect service",
            lambda n=n: (calls.append(n) or _reply(
                success=True, job_id=f"job_{n}", error=None
            )),
        )
        assert started["success"]
    assert len(calls) == 8
    rejected = manager.begin(
        ids[8], "request008", "Next one", "Inspect another",
        lambda: _reply(success=True, job_id="job_nine", error=None),
    )
    assert not rejected["success"] and rejected["error"] == "eight_step_slots_busy"
    assert len(calls) == 8
    # Completion frees ONE slot, but requires an explicit review by ChatGPT.
    manager.collect(
        ids[0], lambda _: _reply(status="completed"),
        lambda _: _reply(success=True, result_text="Verified output"),
    )
    assert manager.review(ids[0], True, "Independent verification")["success"]
    ninth = manager.begin(
        ids[8], "request008", "Next one", "Inspect another",
        lambda: _reply(success=True, job_id="job_nine", error=None),
    )
    assert ninth["success"] and ninth["job_id"] == "job_nine"
    assert all(manager.state(op)["operation"]["status"] == "running" for op in ids[1:8])


def test_state_survives_client_restart_and_duplicate_step_is_not_reexecuted(tmp_path):
    before = InteractiveOperations(tmp_path)
    op = before.create("Restore cluster carefully")["operation_id"]
    observed = []
    task = lambda: (observed.append(1) or _reply(success=True, job_id="job_original", error=None))
    before.begin(op, "unique_key_2026", "Examine", "Check nodes", task)
    after = InteractiveOperations(tmp_path)
    retry = after.begin(op, "unique_key_2026", "Examine", "Check nodes", task)
    assert retry["reused"] and retry["job_id"] == "job_original"
    assert len(observed) == 1
    conflict = after.begin(op, "unique_key_2026", "Examine", "Modify database", task)
    assert not conflict["success"] and conflict["error"] == "idempotency_key_conflict"
    assert after.state(op)["steps"][0]["job_id"] == "job_original"


def test_failed_job_cannot_be_marked_verified_and_needs_correction(tmp_path):
    manager = InteractiveOperations(tmp_path)
    op = manager.create("Fix failing tests")["operation_id"]
    manager.begin(
        op, "step_for_failure", "Run tests", "Test package",
        lambda: _reply(success=True, job_id="job_bad", error=None),
    )
    result = manager.collect(
        op, lambda job: _reply(status="failed"),
        lambda job: _reply(success=True, result_text="failed tests"),
    )
    assert result["job_status"] == "failed"
    assert not manager.review(op, True, "all good")["success"]
    review = manager.review(op, False, "Fix failing tests and re-run")
    assert review["success"] and review["next_action"] == "corrective_operation_step"
    assert not manager.finish(op)["success"]
    assert manager.begin(
        op, "retry_new_key", "Repair", "Correct test files",
        lambda: _reply(success=True, job_id="job_corrected", error=None),
    )["success"]


def test_no_agent_autonomy_is_started_and_final_completion_requires_review(tmp_path):
    manager = InteractiveOperations(tmp_path)
    op = manager.create("Small deployment")["operation_id"]
    assert not manager.finish(op)["success"]
    manager.begin(
        op, "step_to_finish", "Inspect", "Read config",
        lambda: _reply(success=True, job_id="job_safe", error=None),
    )
    assert not manager.finish(op)["success"]
    manager.collect(
        op, lambda _: _reply(status="completed"),
        lambda _: _reply(success=True, result_text="Healthy"),
    )
    assert not manager.finish(op)["success"]
    manager.review(op, True, "Readiness independently checked")
    assert manager.finish(op)["success"]
    assert manager.state(op)["operation"]["status"] == "completed"
    assert not manager.begin(
        op, "another_new_key", "Again", "Modify anything",
        lambda: _reply(success=True, job_id="should-not-launch", error=None),
    )["success"]


def test_failed_spawn_is_recorded_without_holding_a_slot(tmp_path):
    manager = InteractiveOperations(tmp_path)
    op = manager.create("No available capacity")["operation_id"]
    failed = manager.begin(
        op, "step_rejected", "Inspect", "Check details",
        lambda: _reply(success=False, job_id=None, error="supervisor busy: eight"),
    )
    assert not failed["success"]
    assert manager.state(op)["operation"]["status"] == "ready"
    assert manager.begin(
        op, "step_new_try", "Inspect", "Check details",
        lambda: _reply(success=True, job_id="job_success", error=None),
    )["success"]


def test_all_step_operations_use_existing_supervisor_contract(tmp_path, monkeypatch):
    from agy_mcp import server
    from agy_mcp.models import BridgeResponse
    from agy_mcp.session_store import SessionStore

    store = SessionStore(tmp_path)
    monkeypatch.setattr(
        server, "_ensure_state", lambda: (None, None, store, None),
    )
    seen = []
    monkeypatch.setattr(
        server, "agy_start_tool", lambda **kwargs: (
            seen.append(kwargs) or BridgeResponse(success=True, status="running", job_id="job_new")
        ),
    )
    operation_id = server.operation_create("Guide operations")["operation_id"]
    started = server.operation_step(
        operation_id, "test_run_once", "Observe", "Inspect Odoo",
        mode="ask", allow_write=False,
    )
    assert started["success"]
    assert seen[0]["timeout"] == 0
    assert seen[0]["backend"] == "agy"
    assert seen[0]["allow_write"] is False
