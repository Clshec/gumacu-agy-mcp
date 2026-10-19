"""Local Codex Step Chaining & Orchestrator for Antigravity tasks.

Provides:
- Independent local Orchestrator/Worker for step chaining and automatic resumption
- Disk-backed state persistence (orchestrator_state.json)
- Idempotent execution and automatic recovery across restarts
- Step execution tracking, error isolation, and fallback handling
- Direct event hook integration with Supervisor
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agy_mcp.models import BridgeRequest
from agy_mcp.session_store import SessionStore
from agy_mcp.utils import ensure_directory, safe_write_text, utc_now_iso

logger = logging.getLogger(__name__)

StepStatus = Literal["pending", "running", "completed", "failed", "cancelled", "skipped"]
PipelineStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class StepDefinition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    prompt: str
    mode: str = "ask"
    cwd: str = "."
    session_id: str | None = None
    timeout: int = 0
    backend: str = "auto"
    sandbox: bool = False
    allow_write: bool = False
    worktree: bool | None = None
    output_protocol: str = "codex"


class StepExecution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    prompt: str
    status: StepStatus = "pending"
    mode: str = "ask"
    cwd: str = "."
    session_id: str | None = None
    timeout: int = 0
    backend: str = "auto"
    sandbox: bool = False
    allow_write: bool = False
    worktree: bool | None = None
    output_protocol: str = "codex"
    job_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result_preview: str | None = None
    error: str | None = None
    exit_code: int | None = None


class PipelineExecution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pipeline_id: str
    name: str
    steps: list[StepExecution]
    status: PipelineStatus = "pending"
    current_step_index: int = 0
    session_id: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    error: str | None = None


class Orchestrator:
    """Thread-safe, persistent local step-chaining engine for Antigravity jobs."""

    def __init__(
        self,
        store: SessionStore,
        supervisor: Any,
        *,
        state_path: Path | None = None,
        poll_interval: float = 1.0,
        auto_start_worker: bool = True,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.state_path = state_path or (self.store.root / "orchestrator_state.json")
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._pipelines: dict[str, PipelineExecution] = {}

        ensure_directory(self.state_path.parent, mode=0o700)
        self._load_state()

        self._worker_thread: threading.Thread | None = None
        if auto_start_worker:
            self.start_worker()

    def start_worker(self) -> None:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._run_loop,
                name="AgyOrchestratorWorker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop_worker(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    def wake(self) -> None:
        """Trigger an immediate tick of the orchestrator run loop."""
        self._wake_event.set()

    def _load_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    pipe = PipelineExecution.model_validate(item)
                    self._pipelines[pipe.pipeline_id] = pipe
        except Exception as exc:
            logger.warning("Failed to load orchestrator state from %s: %s", self.state_path, exc)

    def _save_state(self) -> None:
        data = [pipe.model_dump(mode="json") for pipe in self._pipelines.values()]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        safe_write_text(self.state_path, payload)

    def create_pipeline(
        self,
        name: str,
        steps: list[dict[str, Any] | StepDefinition],
        *,
        session_id: str | None = None,
        auto_start: bool = True,
    ) -> PipelineExecution:
        if not steps:
            raise ValueError("Pipeline must have at least one step")

        step_execs: list[StepExecution] = []
        for i, s in enumerate(steps):
            if isinstance(s, dict):
                step_id = str(s.get("step_id") or f"step_{i+1}")
                prompt = str(s.get("prompt", ""))
                raw_mode = str(s.get("mode") or "ask").lower()
                mode = raw_mode if raw_mode in {"ask", "plan", "prototype", "review", "execute", "browser", "long"} else "ask"
                cwd = str(s.get("cwd") or ".")
                step_session = s.get("session_id")
                timeout = 0  # Gumacu operations never impose a task wall-clock deadline.
                backend = str(s.get("backend") or "auto")
                sandbox = bool(s.get("sandbox", False))
                allow_write = bool(s.get("allow_write", False))
                worktree = s.get("worktree")
                output_protocol = str(s.get("output_protocol") or "codex")
            else:
                step_id = s.step_id
                prompt = s.prompt
                raw_mode = str(s.mode or "ask").lower()
                mode = raw_mode if raw_mode in {"ask", "plan", "prototype", "review", "execute", "browser", "long"} else "ask"
                cwd = str(s.cwd or ".")
                step_session = s.session_id
                timeout = 0  # Even persisted legacy step timeouts are not enforced.
                backend = s.backend
                sandbox = s.sandbox
                allow_write = s.allow_write
                worktree = s.worktree
                output_protocol = s.output_protocol

            if not prompt:
                raise ValueError(f"Step '{step_id}' must provide a prompt")

            step_execs.append(
                StepExecution(
                    step_id=step_id,
                    prompt=prompt,
                    mode=mode,
                    cwd=cwd,
                    session_id=step_session,
                    timeout=timeout,
                    backend=backend,
                    sandbox=sandbox,
                    allow_write=allow_write,
                    worktree=worktree,
                    output_protocol=output_protocol,
                    status="pending",
                )
            )

        pipe_id = f"pipe_{int(time.time())}_{secrets.token_hex(4)}"
        pipeline = PipelineExecution(
            pipeline_id=pipe_id,
            name=name,
            steps=step_execs,
            status="running" if auto_start else "pending",
            session_id=session_id,
        )

        with self._lock:
            self._pipelines[pipe_id] = pipeline
            self._save_state()

        if auto_start:
            self.wake()

        return pipeline

    def get_pipeline(self, pipeline_id: str) -> PipelineExecution | None:
        with self._lock:
            return self._pipelines.get(pipeline_id)

    def list_pipelines(self, limit: int = 50) -> list[PipelineExecution]:
        with self._lock:
            all_pipes = sorted(
                self._pipelines.values(),
                key=lambda p: p.created_at,
                reverse=True,
            )
            return all_pipes[:limit]

    def cancel_pipeline(self, pipeline_id: str) -> bool:
        with self._lock:
            pipe = self._pipelines.get(pipeline_id)
            if not pipe:
                return False
            if pipe.status in {"completed", "failed", "cancelled"}:
                return True

            pipe.status = "cancelled"
            pipe.updated_at = utc_now_iso()
            pipe.finished_at = utc_now_iso()

            # Cancel currently active job if any
            if pipe.current_step_index < len(pipe.steps):
                curr = pipe.steps[pipe.current_step_index]
                if curr.status == "running" and curr.job_id:
                    try:
                        self.supervisor.cancel(curr.job_id)
                    except Exception as exc:
                        logger.warning("Error cancelling job %s: %s", curr.job_id, exc)
                    curr.status = "cancelled"
                    curr.finished_at = utc_now_iso()

            self._save_state()
            return True

    def _tick(self) -> None:
        """Single tick of the pipeline execution state machine."""
        with self._lock:
            running_pipelines = [
                p for p in self._pipelines.values() if p.status == "running"
            ]

        for pipe in running_pipelines:
            self._process_pipeline(pipe)

    def _process_pipeline(self, pipe: PipelineExecution) -> None:
        with self._lock:
            if pipe.status != "running":
                return

            if pipe.current_step_index >= len(pipe.steps):
                pipe.status = "completed"
                pipe.updated_at = utc_now_iso()
                pipe.finished_at = utc_now_iso()
                self._save_state()
                return

            step = pipe.steps[pipe.current_step_index]

            if step.status == "pending":
                # Start this step
                effective_session = step.session_id or pipe.session_id
                safe_mode = step.mode if step.mode in {"ask", "plan", "prototype", "review", "execute", "browser", "long"} else "ask"
                req = BridgeRequest(
                    prompt=step.prompt,
                    session_id=effective_session,
                    mode=safe_mode,
                    cwd=step.cwd or ".",
                    timeout=0,  # Unlimited; no implicit legacy 900s fallback.
                    backend=step.backend,
                    sandbox=step.sandbox,
                    allow_write=step.allow_write,
                    worktree=step.worktree,
                    output_protocol=step.output_protocol,
                )

                try:
                    response = self.supervisor.start(req)
                    if not response.success:
                        if (response.error or "").startswith("supervisor busy:"):
                            # The supervisor runs at most eight jobs concurrently.
                            # Keep this step pending in durable state and retry on
                            # the next tick; never cancel or fail a queued pipeline.
                            logger.info(
                                "Pipeline %s step %s waiting for a free execution slot",
                                pipe.pipeline_id, step.step_id,
                            )
                            return
                        raise RuntimeError(response.error or "Supervisor did not start the job")
                    if not response.job_id:
                        raise RuntimeError("Supervisor did not return a job ID")
                    step.job_id = response.job_id
                    step.status = "running"
                    step.started_at = utc_now_iso()
                    if not pipe.session_id and response.SESSION_ID:
                        pipe.session_id = response.SESSION_ID
                    if not step.session_id and response.SESSION_ID:
                        step.session_id = response.SESSION_ID
                    pipe.updated_at = utc_now_iso()
                    self._save_state()
                    logger.info(
                        "Pipeline %s started step %s with job %s",
                        pipe.pipeline_id,
                        step.step_id,
                        step.job_id,
                    )
                except Exception as exc:
                    logger.error(
                        "Pipeline %s step %s start failed: %s",
                        pipe.pipeline_id,
                        step.step_id,
                        exc,
                    )
                    step.status = "failed"
                    step.error = str(exc)
                    step.finished_at = utc_now_iso()
                    pipe.status = "failed"
                    pipe.error = f"Step {step.step_id} failed to launch: {exc}"
                    pipe.updated_at = utc_now_iso()
                    pipe.finished_at = utc_now_iso()
                    self._save_state()
                return

            if step.status == "running":
                if not step.job_id:
                    step.status = "failed"
                    step.error = "Missing job ID"
                    pipe.status = "failed"
                    self._save_state()
                    return

                try:
                    rec = self.supervisor.status(step.job_id)
                except Exception as exc:
                    logger.warning("Could not query status for %s: %s", step.job_id, exc)
                    return

                if rec.status == "running":
                    return

                if rec.status == "completed":
                    step.status = "completed"
                    step.finished_at = rec.finished_at or utc_now_iso()
                    step.exit_code = rec.exit_code

                    # Retrieve preview
                    preview = ""
                    try:
                        paths = self.store.paths(step.job_id)
                        if paths.stdout.is_file():
                            preview = paths.stdout.read_text(encoding="utf-8")[:1000]
                    except Exception:
                        pass
                    step.result_preview = preview

                    logger.info(
                        "Pipeline %s step %s completed; advancing",
                        pipe.pipeline_id,
                        step.step_id,
                    )

                    pipe.current_step_index += 1
                    pipe.updated_at = utc_now_iso()

                    if pipe.current_step_index >= len(pipe.steps):
                        pipe.status = "completed"
                        pipe.finished_at = utc_now_iso()
                        logger.info("Pipeline %s fully completed", pipe.pipeline_id)
                    else:
                        # A pending step is retried by the durable worker loop.
                        # Do not fail the whole pipeline just because all eight
                        # execution slots are currently busy.
                        logger.debug("Pipeline %s has another pending step", pipe.pipeline_id)

                    self._save_state()
                    self.wake()

                elif rec.status in {
                    "failed",
                    "cancelled",
                    "upstream_error",
                    "quota_exhausted",
                    "timeout",
                    "no_final_response",
                }:
                    step.status = "failed"
                    step.finished_at = rec.finished_at or utc_now_iso()
                    step.error = rec.error or f"Job status: {rec.status}"
                    step.exit_code = rec.exit_code

                    pipe.status = "failed"
                    pipe.error = f"Step '{step.step_id}' failed: {step.error}"
                    pipe.updated_at = utc_now_iso()
                    pipe.finished_at = utc_now_iso()
                    logger.warning(
                        "Pipeline %s failed at step %s (%s)",
                        pipe.pipeline_id,
                        step.step_id,
                        step.error,
                    )
                    self._save_state()

    def _run_loop(self) -> None:
        """Worker loop executing state ticks and waiting on events."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.error("Unexpected error in orchestrator worker tick: %s", exc)

            self._wake_event.wait(timeout=self.poll_interval)
            self._wake_event.clear()
