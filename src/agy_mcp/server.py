"""FastMCP server exposing the agy-mcp toolkit over stdio.

Tools (all return dicts with stable keys; never raise across the wire):

* ``agy`` — synchronous one-shot bridge call.
* ``agy_start`` — spawn a background job, return ``status="running"`` envelope.
* ``agy_continue`` — like ``agy``, but ``SESSION_ID`` is required.
* ``agy_status`` — poll a running job's :class:`JobRecord`.
* ``agy_read`` — read events from a job (raw or translated).
* ``agy_result`` — fetch captured output for a finished job.
* ``agy_cancel`` — signal a running job to stop.
* ``agy_sessions`` — list recent jobs.
* ``agy_doctor`` — environment + capability probe.
* ``agy_install_skill`` — write the scaffold skill into target dirs.

Threading model: the FastMCP runtime drives tools from an asyncio loop and
calls sync tool functions inline. ``agy`` and ``agy_continue`` would block
that loop while ``_bridge_run`` waits on a subprocess, so they are declared
``async def`` and dispatch the blocking work to a worker thread via
:func:`anyio.to_thread.run_sync` (Phase 5 R1 arch P1.1).
``agy_doctor`` likewise runs its blocking probes in a worker thread.

Every tool routes its output through :class:`SafetyPolicy` before
serialisation — adapter buffers, capability warnings, and error strings
have already been scrubbed by the lower layers, but the doctor / install
helpers also redact their own paths so a transcript capture never leaks
a ``/Users/<user>/`` path.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import re
import threading
import weakref
from functools import partial
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from agy_mcp import __version__
from agy_mcp.adapters.agy import AgyPrintBackend
from agy_mcp.adapters.gemini import GeminiCliBackend
from agy_mcp.adapters.protocol import ProtocolTranslator
from agy_mcp.bridge import _run as _bridge_run
from agy_mcp.capability_gateway import register_capability_tools
from agy_mcp.config import Config, get_config
from agy_mcp.device_tools import register_device_tools
from agy_mcp.doctor import run_doctor
from agy_mcp.events import (
    SubscriptionStore,
    WebhookDispatcher,
    setup_events_protocol,
)
from agy_mcp.install import SkillScope, SkillTarget, install_skills
from agy_mcp.interactive_btw import (
    interactive_btw,
    interactive_open,
    interactive_send,
    interactive_snapshot,
    route_legacy_command,
)
from agy_mcp.models import (
    ActiveJobsToolResponse,
    BackendName,
    BridgeRequest,
    BridgeResponse,
    CancelToolResponse,
    DoctorToolResponse,
    InstallSkillToolResponse,
    Mode,
    NotificationAckToolResponse,
    NotificationsToolResponse,
    OutputProtocol,
    PurgeToolResponse,
    ReadToolResponse,
    ResultToolResponse,
    SessionsToolResponse,
    StatusToolResponse,
)
from agy_mcp.notifications import NotificationDispatcher, NotificationOutbox
from agy_mcp.orchestrator import Orchestrator
from agy_mcp.remote_policy import restrict_request, restrict_tools
from agy_mcp.safety import SafetyPolicy
from agy_mcp.session_store import SessionStore
from agy_mcp.supervisor import Supervisor

# ---------------------------------------------------------------------------
# Module-level singletons. The FastMCP runtime imports this module exactly
# once per process; the singletons are lazily materialised on the first tool
# call so importing ``agy_mcp.server`` for tests stays cheap.
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_config: Config | None = None
_safety: SafetyPolicy | None = None
_store: SessionStore | None = None
_supervisor: Supervisor | None = None
_subscription_store: SubscriptionStore | None = None
_webhook_dispatcher: WebhookDispatcher | None = None
_notification_outbox: NotificationOutbox | None = None
_notification_dispatcher: NotificationDispatcher | None = None
_orchestrator: Orchestrator | None = None
_events_initialized: bool = False
# Cached adapters for the doctor probe so we don't pay 4 subprocess calls
# (one help + one version per backend) on every ``agy_doctor`` invocation.
# Phase 5 R1 arch P1.5.
_agy_adapter: AgyPrintBackend | None = None
_gemini_adapter: GeminiCliBackend | None = None

# Defence-in-depth cap so a malicious or buggy caller can't burn unbounded
# memory by passing a multi-megabyte string in place of a job_id slug. We
# also pin a charset so structured failures don't echo control bytes back
# to the caller (Phase 5 R2 security P2-2). The pattern aligns with the
# session_store's own regex (`^job_[A-Za-z0-9_-]{1,80}$`, max 84 chars)
# so the server gate never accepts more than the deeper layer will store
# (Phase 5 R3 security P2).
_MAX_JOB_ID_LEN = 84
_JOB_ID_PATTERN = re.compile(r"^job_[A-Za-z0-9_-]{1,80}$")
_JOB_ID_REFERENCE_PATTERN = _JOB_ID_PATTERN
_NOTIFICATION_ID_PATTERN = re.compile(r"^note_[a-f0-9]{24}$")
_RESULT_JOB_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "upstream_error",
        "quota_exhausted",
        "timeout",
        "no_final_response",
    }
)
_MAX_SESSION_ID_LEN = 96
# Conservative charset for SESSION_ID; mirrors models._SESSION_ID_RE so
# the server-side fast path and the pydantic validator stay in lockstep.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
# Concurrency limiter for the async bridge tools. anyio's default thread
# limiter is global to the process (40); ``_bridge_run`` itself spawns
# additional reader threads + a subprocess per call, so we add a finer cap
# to keep a flood of concurrent MCP calls from exhausting local resources
# (Phase 5 R2 security P1-3). ``anyio.CapacityLimiter`` is loop-affine —
# each instance is bound to the asyncio loop that created it — so we cache
# per running loop id rather than process-globally (Phase 5 R3 arch P1).
_BRIDGE_CONCURRENCY = 8
# Phase 5 R3 P3.14: keyed on the running loop object itself via
# WeakKeyDictionary so a GC'd loop drops its limiter automatically.
# (The previous ``dict[int, ...]`` keyed on ``id(loop)`` was correct
# under cache-replace-on-collision semantics, but kept stale limiters
# alive for the process lifetime when a loop was discarded.)
_bridge_limiters_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, anyio.CapacityLimiter
] = weakref.WeakKeyDictionary()
_bridge_limiter_lock = threading.Lock()

# Defence-in-depth cap on the install-skill argument surface
# (Phase 5 R2 security P1-1). 16 is well above the four documented
# targets (claude, codex, antigravity, all) — large enough for forward
# extensions, small enough to refuse pathological payloads.
_MAX_INSTALL_TARGETS = 16
_ALLOWED_TARGETS: frozenset[str] = frozenset({"claude", "codex", "antigravity", "all"})

# Conservative cap on the purge cutoff. Operators occasionally want to
# nuke everything older than a few hours; we still refuse zero/negative
# (handled by SessionStore.purge_older_than) and refuse anything past 10
# years to defend against an integer typo wiping the whole store via
# ``days=99999`` evaluating to a noop cutoff.
_PURGE_MAX_DAYS = 365 * 10


def _ensure_state() -> tuple[Config, SafetyPolicy, SessionStore, Supervisor]:
    global _config, _safety, _store, _supervisor, _subscription_store, _webhook_dispatcher, _notification_outbox, _notification_dispatcher, _events_initialized, _orchestrator
    with _state_lock:
        if _config is None:
            _config = get_config()
        if _safety is None:
            _safety = SafetyPolicy.from_config(_config)
        if _store is None:
            _store = SessionStore(Path(_config.session_store_root()).expanduser())
        if _subscription_store is None:
            sub_path = _store.root / "subscriptions.json"
            _subscription_store = SubscriptionStore(sub_path)
            _webhook_dispatcher = WebhookDispatcher(_subscription_store)
        if _notification_outbox is None:
            _notification_outbox = NotificationOutbox(_store.root / "notifications.json")
            _notification_dispatcher = NotificationDispatcher(_notification_outbox)
        if _supervisor is None:
            _supervisor = Supervisor(
                store=_store,
                config=_config,
                safety=_safety,
                events_dispatcher=_webhook_dispatcher,
                notification_dispatcher=_notification_dispatcher,
            )
            _notification_dispatcher.bind_runtime(
                store=_store,
                supervisor=_supervisor,
                safety=_safety,
            )
        if _orchestrator is None:
            _orchestrator = Orchestrator(store=_store, supervisor=_supervisor)
            _supervisor.orchestrator = _orchestrator
        if not _events_initialized:
            setup_events_protocol(mcp, _subscription_store, dispatcher=_webhook_dispatcher)
            _events_initialized = True
        return _config, _safety, _store, _supervisor


def _ensure_events_state() -> tuple[SubscriptionStore, WebhookDispatcher]:
    _ensure_state()
    assert _subscription_store is not None
    assert _webhook_dispatcher is not None
    return _subscription_store, _webhook_dispatcher


def _ensure_orchestrator_state() -> Orchestrator:
    _ensure_state()
    assert _orchestrator is not None
    return _orchestrator


def _ensure_adapters(*, force_refresh: bool = False) -> tuple[AgyPrintBackend, GeminiCliBackend]:
    """Lazily build doctor adapter singletons.

    Each adapter probes its CLI exactly once (caching the result), so the
    doctor probe can reuse them across calls instead of forking
    ``agy --help`` / ``agy --version`` / ``gemini --help`` /
    ``gemini --version`` every invocation. The MCP server is the only
    caller; tests bypass this by passing fresh adapters directly to
    ``run_doctor``.

    ``force_refresh=True`` drops the cached singletons so an operator who
    just upgraded an underlying binary can re-probe without restarting
    the MCP server. (Phase 5 R2 security P2-1.)
    """

    global _agy_adapter, _gemini_adapter
    _, safety, _store_, _supervisor_ = _ensure_state()
    with _state_lock:
        if force_refresh:
            _agy_adapter = None
            _gemini_adapter = None
        if _agy_adapter is None:
            _agy_adapter = AgyPrintBackend(safety=safety)
        if _gemini_adapter is None:
            _gemini_adapter = GeminiCliBackend(safety=safety)
        return _agy_adapter, _gemini_adapter


async def _get_bridge_limiter() -> anyio.CapacityLimiter:
    """Return (and lazily build) the per-loop bridge concurrency cap.

    ``anyio.CapacityLimiter`` binds to the asyncio loop active when the
    instance is constructed, so a process-global singleton breaks when
    a second loop is spun up (tests using ``asyncio.run`` per call,
    embedded sidecar loops, hot reloads). We cache one limiter per
    running loop via a ``weakref.WeakKeyDictionary`` so a discarded
    loop frees its limiter automatically — and a re-used ``id(loop)``
    cannot resurrect a stale limiter from a dead loop (Phase 5 R3
    arch P1 + R3 P3.14).
    """

    loop = asyncio.get_running_loop()
    with _bridge_limiter_lock:
        limiter = _bridge_limiters_by_loop.get(loop)
        if limiter is None:
            limiter = anyio.CapacityLimiter(_BRIDGE_CONCURRENCY)
            _bridge_limiters_by_loop[loop] = limiter
        return limiter


def _reset_state_for_tests() -> None:
    """Drop the cached singletons so tests can swap in fresh stores.

    Holds ``_state_lock`` and ``_bridge_limiter_lock`` while wiping.
    **Invariant for future maintainers:** callbacks invoked while
    those locks are held MUST NOT call back into ``_ensure_state``
    / ``_ensure_adapters`` / ``_get_bridge_limiter``, or the test
    teardown will deadlock. The current implementation only mutates
    module-level globals — keep it that way (Phase 5 R2 P3.7).
    """

    global _config, _safety, _store, _supervisor, _agy_adapter, _gemini_adapter, _subscription_store, _webhook_dispatcher, _notification_outbox, _notification_dispatcher, _events_initialized, _orchestrator
    with _state_lock:
        if _orchestrator is not None:
            _orchestrator.stop_worker()
            _orchestrator = None
        _config = None
        _safety = None
        _store = None
        _supervisor = None
        _agy_adapter = None
        _gemini_adapter = None
        _subscription_store = None
        _webhook_dispatcher = None
        _notification_outbox = None
        _notification_dispatcher = None
        _events_initialized = False
    with _bridge_limiter_lock:
        _bridge_limiters_by_loop.clear()


# ---------------------------------------------------------------------------
# FastMCP instance and tool registrations
# ---------------------------------------------------------------------------


try:
    mcp = FastMCP(
        name="agy-mcp",
        version=__version__,
        instructions=(
            "Google Antigravity (agy) CLI bridge with long-task supervisor. "
            "IMPORTANT: use ``agy_start`` for every task that may take more than a "
            "few seconds, or whenever the user may wait, monitor, ask for progress, "
            "or expect an automatic completion event. A synchronous ``agy`` call "
            "occupies its request and cannot be inspected reliably while running. "
            "Use ``agy_status`` to answer progress questions from its progress field, "
            "then ``agy_result`` after completion. Use ``agy`` only for quick one-shot "
            "prompts. Never promise automatic resume unless an event subscription "
            "was actually created; agy_start warnings and agy_status.progress report "
            "subscription availability. Local and scheduled chats should use "
            "agy_notifications plus agy_notification_ack; Telegram delivery is "
            "independent of chat wake-up. Use "
            "``agy_doctor`` to check the environment."
        ),
    )
except TypeError:
    mcp = FastMCP(
        name="agy-mcp",
        instructions=(
            "Google Antigravity (agy) CLI bridge with long-task supervisor. "
            "IMPORTANT: use ``agy_start`` for every task that may take more than a "
            "few seconds, or whenever the user may wait, monitor, ask for progress, "
            "or expect an automatic completion event. A synchronous ``agy`` call "
            "occupies its request and cannot be inspected reliably while running. "
            "Use ``agy_status`` to answer progress questions from its progress field, "
            "then ``agy_result`` after completion. Use ``agy`` only for quick one-shot "
            "prompts. Never promise automatic resume unless an event subscription "
            "was actually created; agy_start warnings and agy_status.progress report "
            "subscription availability. Local and scheduled chats should use "
            "agy_notifications plus agy_notification_ack; Telegram delivery is "
            "independent of chat wake-up. Use "
            "``agy_doctor`` to check the environment."
        ),
    )

if hasattr(mcp, "_mcp_server"):
    mcp._mcp_server.version = __version__


# ---------------------------------------------------------------------------
# Helpers used by the synchronous tools (``agy``, ``agy_continue``)
# ---------------------------------------------------------------------------


def _build_request(payload: dict[str, Any]) -> BridgeRequest:
    """Validate the incoming MCP arguments through the BridgeRequest schema.

    Pydantic raises ``ValidationError`` on bad input; the tool wrapper
    catches it and converts to a structured failure envelope.
    """

    return BridgeRequest(**restrict_request(payload))


def _structured_failure(safety: SafetyPolicy, exc: BaseException, *, cwd: str = "") -> BridgeResponse:
    """Top-level guard: any tool exception becomes a structured envelope.

    Returns a :class:`BridgeResponse` instance so the FastMCP runtime can
    emit ``structuredContent`` alongside the text fallback. Callers that
    historically expected a ``dict`` can still call ``.model_dump()`` —
    but the tool functions themselves now return the model directly.
    """

    return BridgeResponse(
        success=False,
        error=safety.redact(str(exc)),
        cwd=safety.redact(cwd),
    )


def _wrapper_failure(
    safety: SafetyPolicy,
    exc: BaseException,
    cls: type,
    **extra: Any,
):
    """Failure envelope for the metadata tools (status / read / cancel / ...).

    ``cls`` is one of the wrapper Tool response models; ``extra`` lets the
    caller pin echo fields like ``job_id`` so a client comparing input and
    output can correlate the failure with the call. Pydantic validates
    that ``extra`` fits the model's field set, so a typo blows up at
    development time, not at serialisation.
    """

    safe_extra = {
        key: safety.redact(value) if isinstance(value, str) else value
        for key, value in extra.items()
    }
    return cls(
        success=False,
        error=safety.redact(str(exc)),
        **safe_extra,
    )


def _response_to_dict(resp: BridgeResponse) -> dict[str, Any]:
    # Retained for tests / external callers that depended on the dict form.
    return resp.model_dump(mode="json")


def _validate_job_id(safety: SafetyPolicy, job_id: str) -> str | None:
    """Return a redacted error string if ``job_id`` is invalid, else None."""

    if not job_id:
        return safety.redact("job_id is required")
    if len(job_id) > _MAX_JOB_ID_LEN:
        return safety.redact(
            f"job_id exceeds {_MAX_JOB_ID_LEN} chars; refusing to look up",
        )
    if not _JOB_ID_PATTERN.match(job_id):
        # Don't echo the raw value back — it might contain control bytes.
        return safety.redact(
            "job_id must match ^job_[A-Za-z0-9_-]{1,80}$",
        )
    if safety.redact(job_id) != job_id:
        return safety.redact("job_id must not contain secret-shaped text")
    return None


def _validate_job_id_reference(safety: SafetyPolicy, reference: str) -> str | None:
    """Return a redacted error string if a job id or prefix is invalid."""

    if not reference:
        return safety.redact("job_id is required")
    if len(reference) > _MAX_JOB_ID_LEN:
        return safety.redact(
            f"job_id exceeds {_MAX_JOB_ID_LEN} chars; refusing to look up",
        )
    if not _JOB_ID_REFERENCE_PATTERN.fullmatch(reference):
        return safety.redact(
            "job_id must match ^job_[A-Za-z0-9_-]{1,80}$",
        )
    if safety.redact(reference) != reference:
        return safety.redact("job_id must not contain secret-shaped text")
    return None


def _resolve_job_id_reference(
    safety: SafetyPolicy,
    store: SessionStore,
    supervisor: Supervisor,
    reference: str,
) -> tuple[str | None, str | None]:
    """Resolve an exact job id or unique prefix; return ``(job_id, error)``."""

    err = _validate_job_id_reference(safety, reference)
    if err is not None:
        return None, err
    try:
        # Preserve a local exact identity even if its record was purged.
        if supervisor.has_active_job(reference):
            return reference, None
        record = store.resolve_job_reference(reference)
    except ValueError as exc:
        return None, safety.redact(str(exc))
    except Exception as exc:
        return None, safety.redact(f"job_id reference lookup failed: {exc}")
    if record is not None:
        return record.job_id, None
    # Preserve the old not-found path for callers that pass a well-formed
    # job_id which has not been created in this session store.
    return reference, None


def _validate_session_id(safety: SafetyPolicy, session_id: str) -> str | None:
    """Length-cap and charset-check SESSION_ID before it reaches the bridge.

    The bridge layer treats SESSION_ID as a worktree slug seed, a child-
    process env entry, and an ``--conversation=<id>`` flag value; a
    multi-megabyte value or one containing NUL/CR/LF would either crash
    ``os.execvpe`` (Linux glibc) or smuggle through on macOS. We mirror
    the BridgeRequest model validator here so the server returns a clean
    structured error before the pydantic round-trip raises. (Phase 5 R2
    arch P2 #3; Phase 8 review P1-1.)
    """

    if len(session_id) > _MAX_SESSION_ID_LEN:
        return safety.redact(
            f"SESSION_ID exceeds {_MAX_SESSION_ID_LEN} chars",
        )
    if not _SESSION_ID_RE.fullmatch(session_id):
        return safety.redact(
            "SESSION_ID must match ^[A-Za-z0-9._-]{1,96}$ "
            "(no whitespace, NUL, CR/LF, slashes, or shell metacharacters)",
        )
    return None


def _job_record_recency(record: Any) -> str:
    return record.finished_at or record.updated_at or record.started_at or ""


def _result_text_from_events(
    events: list[Any],
    *,
    status: str,
    fallback: str | None,
    safety: SafetyPolicy,
) -> str:
    """Return the best human-readable result text from stored events."""

    # For no_final_response, the CLI explicitly produced no response text
    if status == "no_final_response":
        return ""

    # For timeout, if we have assistant output (e.g. recovered partial text), prefer returning the text
    if status == "timeout":
        for event in reversed(events):
            text = getattr(event, "text", None)
            if getattr(event, "type", None) == "assistant" and text and text.strip():
                return safety.redact(text)
        return ""

    for event in reversed(events):
        text = getattr(event, "text", None)
        if (
            getattr(event, "type", None) == "result"
            and getattr(event, "subtype", None) not in {None, "success"}
            and text
        ):
            return safety.redact(text)

    if status != "completed":
        for event_type in ("result", "error"):
            for event in reversed(events):
                text = getattr(event, "text", None)
                if getattr(event, "type", None) == event_type and text:
                    return safety.redact(text)
        if fallback:
            return safety.redact(fallback)

    for event in reversed(events):
        text = getattr(event, "text", None)
        if getattr(event, "type", None) == "assistant" and text:
            return safety.redact(text)
    for event in reversed(events):
        text = getattr(event, "text", None)
        if getattr(event, "type", None) in {"result", "error"} and text:
            return safety.redact(text)
    if fallback:
        return safety.redact(fallback)
    return ""


# ---------------------------------------------------------------------------
# Tool: agy — synchronous one-shot
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy",
    description=(
        "Run agy --print synchronously and return the assistant text + "
        "metadata. Use only for quick one-shot prompts. If a task may take "
        "more than a few seconds, or the user may ask for progress, use "
        "agy_start instead; synchronous calls cannot be inspected reliably "
        "while their request is occupied. Compatible with the legacy `gemini` tool: same "
        "PROMPT / cd / sandbox / SESSION_ID / return_all_messages / model "
        "fields, with new mode / timeout / allow_write / worktree / backend "
        "/ output_protocol options."
    ),
)
async def agy_tool(
    PROMPT: str,
    cd: str = ".",
    SESSION_ID: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    return_all_messages: bool = False,
    mode: Mode = "ask",
    timeout: int = 0,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
    operator_token: str | None = None,
) -> BridgeResponse:
    # Phase 5 R4 P3.11: ``extra_env`` keeps ``| None = None`` on the
    # tool surface (MCP clients omit the key entirely on most callers)
    # but normalises to ``{}`` immediately so the downstream
    # ``BridgeRequest`` field (declared as ``dict[str, str]``) matches.
    # The JSON-schema view ends up as ``anyOf [{object}, {null}]``;
    # that is intentional and documented here so a future schema audit
    # doesn't try to "fix" it back to a bare ``object``.
    config, safety, _store_, _supervisor_ = _ensure_state()
    effective_cwd = cwd if cwd is not None else cd
    # Route opt-in native TUI commands without adding/changing MCP tool schemas.
    if isinstance(PROMPT, str) and PROMPT.partition(" ")[0] in {
        "/btw", "/tui-open", "/tui-send", "/tui-snapshot",
    }:
        if SESSION_ID is not None:
            return _structured_failure(
                safety, ValueError("TUI commands require job_tui_ ID, not SESSION_ID"),
                cwd=effective_cwd,
            )
        if dry_run:
            return _structured_failure(
                safety, ValueError("TUI commands do not support dry_run"),
                cwd=effective_cwd,
            )
        result = await anyio.to_thread.run_sync(
            partial(
                route_legacy_command, PROMPT, cwd=effective_cwd,
                operator_token=operator_token,
            )
        )
        if result is not None:
            return BridgeResponse(
                success=bool(result.get("success")),
                status="completed" if result.get("success") else "failed",
                agent_messages=json.dumps(result, ensure_ascii=False),
                error=result.get("error"),
                cwd=effective_cwd,
            )
    if SESSION_ID is not None:
        err = _validate_session_id(safety, SESSION_ID)
        if err is not None:
            return _structured_failure(safety, ValueError(err), cwd=effective_cwd)
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": effective_cwd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": return_all_messages,
                "mode": mode,
                "timeout": timeout,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "dry_run": dry_run,
                "extra_env": extra_env or {},
                "operator_token": operator_token,
            }
        )
    except Exception as exc:  # noqa: BLE001 - validation guard
        return _structured_failure(safety, exc, cwd=effective_cwd)
    # ``_bridge_run`` launches the agy subprocess and blocks until it
    # finishes — offload to a worker thread so the FastMCP asyncio loop
    # stays free to dispatch other tool calls. (Phase 5 R1 arch P1.1.)
    # The CapacityLimiter caps concurrent bridge calls per process so a
    # flood of MCP requests can't exhaust local resources. (Phase 5 R2
    # security P1-3.)
    limiter = await _get_bridge_limiter()
    response = await anyio.to_thread.run_sync(
        _bridge_run, request, config, safety, limiter=limiter,
    )
    return response


# ---------------------------------------------------------------------------
# Tool: agy_continue — same as agy but session_id is required
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_continue",
    description=(
        "Continue an existing agy session synchronously for a quick prompt. "
        "For monitored or potentially long continuation work, call agy_start "
        "with SESSION_ID instead."
    ),
)
async def agy_continue_tool(
    SESSION_ID: str,
    PROMPT: str,
    cd: str = ".",
    model: str | None = None,
    sandbox: bool = False,
    return_all_messages: bool = False,
    mode: Mode = "ask",
    timeout: int = 0,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
    operator_token: str | None = None,
) -> BridgeResponse:
    config, safety, _store_, _supervisor_ = _ensure_state()
    effective_cwd = cwd if cwd is not None else cd
    if not SESSION_ID:
        return _structured_failure(
            safety, ValueError("SESSION_ID is required for agy_continue"), cwd=effective_cwd,
        )
    err = _validate_session_id(safety, SESSION_ID)
    if err is not None:
        return _structured_failure(safety, ValueError(err), cwd=effective_cwd)
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": effective_cwd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": return_all_messages,
                "mode": mode,
                "timeout": timeout,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "dry_run": dry_run,
                "extra_env": extra_env or {},
                "operator_token": operator_token,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc, cwd=effective_cwd)
    limiter = await _get_bridge_limiter()
    response = await anyio.to_thread.run_sync(
        _bridge_run, request, config, safety, limiter=limiter,
    )
    return response


# ---------------------------------------------------------------------------
# Tool: agy_start — spawn a background job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_start",
    description=(
        "Start a monitorable Antigravity job in the background. REQUIRED for "
        "tasks that may take more than a few seconds, when the user may ask "
        "what is happening, or when an automatic completion/failure event is "
        "expected. Returns status='running' and a job_id; use agy_status for a "
        "safe live progress summary and agy_result after completion."
    ),
)
def agy_start_tool(
    PROMPT: str,
    cd: str = ".",
    SESSION_ID: str | None = None,
    model: str | None = None,
    sandbox: bool = False,
    mode: Mode = "ask",
    timeout: int = 0,
    allow_write: bool = False,
    worktree: bool | None = None,
    backend: BackendName = "auto",
    output_protocol: OutputProtocol = "claude",
    debug: bool = False,
    extra_env: dict[str, str] | None = None,
    job_id: str | None = None,
    cwd: str | None = None,
    operator_token: str | None = None,
) -> BridgeResponse:
    config, safety, _store_, supervisor = _ensure_state()
    effective_cwd = cwd if cwd is not None else cd
    if SESSION_ID is not None:
        err = _validate_session_id(safety, SESSION_ID)
        if err is not None:
            return _structured_failure(safety, ValueError(err), cwd=effective_cwd)
    try:
        request = _build_request(
            {
                "prompt": PROMPT,
                "cwd": effective_cwd,
                "session_id": SESSION_ID,
                "model": model,
                "sandbox": sandbox,
                "return_all_messages": False,
                "mode": mode,
                "timeout": timeout,
                "detach": True,
                "allow_write": allow_write,
                "worktree": worktree,
                "backend": backend,
                "output_protocol": output_protocol,
                "debug": debug,
                "extra_env": extra_env or {},
                "operator_token": operator_token,
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _structured_failure(safety, exc, cwd=effective_cwd)
    if job_id is not None:
        err = _validate_job_id(safety, job_id)
        if err is not None:
            return _structured_failure(safety, ValueError(err), cwd=effective_cwd)
    try:
        response = supervisor.start(request, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        return _structured_failure(safety, exc, cwd=effective_cwd)
    subscription_count = len(_subscription_store.list_all()) if _subscription_store else 0
    if subscription_count == 0:
        telegram_ready = bool(
            _notification_dispatcher and _notification_dispatcher.telegram_configured
        )
        response.warnings.append(
            "No active MCP event subscription exists. This job cannot automatically "
            "resume ChatGPT; use agy_status manually or subscribe from an eligible "
            "ChatGPT Work/Cloud chat before promising automatic notification. "
            + (
                "Telegram terminal notifications are configured."
                if telegram_ready
                else "Telegram terminal notifications are not configured."
            )
        )
    return response


# ---------------------------------------------------------------------------
# Tool: agy_status — poll a job's JobRecord
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_status",
    description=(
        "Return status plus a safe live progress summary for a job_id or "
        "unique job-id prefix. While a job is running, use progress.summary "
        "and progress.recent_activity to explain what Antigravity is doing."
    ),
)
def agy_status_tool(job_id: str) -> StatusToolResponse:
    config, safety, _store_, supervisor = _ensure_state()
    resolved_job_id, err = _resolve_job_id_reference(safety, _store_, supervisor, job_id)
    if err is not None:
        return _wrapper_failure(safety, ValueError(err), StatusToolResponse)
    try:
        record = supervisor.status(resolved_job_id or job_id)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, StatusToolResponse)
    if record is None:
        # Use the same envelope shape as other failures so consumers can
        # rely on ``success/error`` keys regardless of why the lookup
        # failed. (Phase 5 R1 arch P1.3)
        return _wrapper_failure(
            safety,
            ValueError(f"job_id {resolved_job_id or job_id!r} not found"),
            StatusToolResponse,
        )
    try:
        events = supervisor.read_events(record.job_id)
    except Exception:  # noqa: BLE001 - status must remain available if the log is damaged
        events = []
    return StatusToolResponse(
        success=True,
        record=record,
        progress=_build_progress_summary(
            record,
            events,
            safety,
            event_subscription_count=(
                len(_subscription_store.list_all()) if _subscription_store else 0
            ),
        ),
    )


_PROGRESS_LABELS = {
    "init": "Preparing the Antigravity runtime",
    "sidecar_ready": "Antigravity runtime is ready",
    "sidecar_http_ready": "Antigravity connection is ready",
    "conversation_started": "Started the Antigravity conversation",
    "conversation_resumed": "Resumed the Antigravity conversation",
    "print_starting": "Sending the task to Antigravity",
    "turn_start": "Antigravity is working on the requested task",
    "input_flush": "The task input was delivered",
    "stream_start": "Receiving Antigravity output",
    "turn_end": "Antigravity finished the current turn",
    "rewind": "Antigravity retried an earlier step",
    "quota_exhausted": "Antigravity quota is exhausted",
    "timeout": "Antigravity timed out",
    "success": "Antigravity completed the task",
    "cancelled": "The Antigravity task was cancelled",
}


def _build_progress_summary(
    record: Any,
    events: list[Any],
    safety: SafetyPolicy,
    *,
    event_subscription_count: int = 0,
) -> dict[str, Any]:
    """Build a bounded, redacted operational view without exposing raw logs."""
    prompt = ""
    if isinstance(record.request, dict):
        prompt = str(record.request.get("prompt") or record.request.get("PROMPT") or "")
    task_preview = safety.redact(" ".join(prompt.split()))[:240] or None

    activity: list[dict[str, Any]] = []
    for event in events:
        subtype = event.subtype or event.type
        label = _PROGRESS_LABELS.get(subtype)
        if event.type == "tool_use":
            label = f"Using tool: {subtype}"
        elif event.type == "tool_result":
            label = f"Received output from: {subtype}"
        elif event.type == "error" and not label:
            label = f"Antigravity reported an error: {subtype}"
        elif event.type == "assistant" and event.text:
            label = "Antigravity produced an output update"
        if not label:
            continue
        item: dict[str, Any] = {
            "at": event.ts,
            "type": event.type,
            "subtype": event.subtype,
            "description": label,
        }
        # Assistant text is already redacted by the adapter. Include only a
        # short output preview; never copy raw klog metadata or model thoughts.
        if event.type == "assistant" and event.text:
            item["output_preview"] = safety.redact(event.text)[:240]
        activity.append(item)

    recent = activity[-5:]
    if record.status == "running":
        latest = recent[-1]["description"] if recent else "Antigravity is running"
        summary = latest
        if task_preview:
            summary = f"{latest}. Current task: {task_preview}"
    elif record.status == "quota_exhausted":
        summary = "Antigravity stopped because its quota is exhausted"
    elif recent:
        summary = recent[-1]["description"]
    else:
        summary = f"Antigravity job status: {record.status}"

    return {
        "summary": summary,
        "phase": (recent[-1]["subtype"] or recent[-1]["type"]) if recent else record.status,
        "task_preview": task_preview,
        "last_event_at": record.last_event_at or (events[-1].ts if events else None),
        "event_count": len(events),
        "recent_activity": recent,
        "detail_level": "operational",
        "event_subscription_count": event_subscription_count,
        "automatic_resume_available": event_subscription_count > 0,
    }


# ---------------------------------------------------------------------------
# Tool: agy_read — read events from a job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_read",
    description=(
        "Read events from a job's event log. ``since`` is the 0-based offset; "
        "``translate`` may be 'raw', 'claude', or 'codex' to wire-format the "
        "events (default returns canonical events as dicts). ``job_id`` may be "
        "a full id or unique prefix."
    ),
)
def agy_read_tool(
    job_id: str,
    since: int = 0,
    translate: OutputProtocol | None = None,
    limit: int = 100,
) -> ReadToolResponse:
    config, safety, _store_, supervisor = _ensure_state()
    resolved_job_id, err = _resolve_job_id_reference(safety, _store_, supervisor, job_id)
    if err is not None:
        return _wrapper_failure(
            safety, ValueError(err), ReadToolResponse,
        )
    if since < 0 or not 1 <= limit <= 1000:
        return _wrapper_failure(
            safety,
            ValueError("since must be non-negative and limit must be between 1 and 1000"),
            ReadToolResponse,
            job_id=job_id,
            since=since,
        )
    try:
        record = supervisor.status(resolved_job_id or job_id)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, ReadToolResponse, job_id=resolved_job_id)
    if record is None:
        return _wrapper_failure(
            safety,
            ValueError(f"job_id {resolved_job_id or job_id!r} not found"),
            ReadToolResponse,
            job_id=resolved_job_id,
        )
    try:
        events = supervisor.read_events(resolved_job_id or job_id, since=since)
        total_remaining = len(events)
        events = events[:limit]
        if translate is None:
            payload: list[dict[str, Any]] = [
                e.model_dump(mode="json") for e in events
            ]
        else:
            payload = ProtocolTranslator(
                translate, safety=safety, include_raw=False,
            ).translate_many(events)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(
            safety, exc, ReadToolResponse, job_id=resolved_job_id, since=since,
        )
    return ReadToolResponse(
        success=True,
        job_id=resolved_job_id or job_id,
        since=since,
        translate=translate,
        events=payload,
        count=len(payload),
        next_since=since + len(events),
        has_more=total_remaining > len(events),
    )


# ---------------------------------------------------------------------------
# Tool: agy_result — return captured output for a finished job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_result",
    description=(
        "Return the captured output for a finished background job. Pass "
        "``job_id`` (full id or unique prefix) to select a job, or omit it "
        "to use the latest finished job from agy_sessions. Set "
        "``include_events=true`` to also return stored events; otherwise use "
        "agy_read for the full event log."
    ),
)
def agy_result_tool(
    job_id: str | None = None,
    include_events: bool = False,
    since: int = 0,
) -> ResultToolResponse:
    _config, safety, _store_, supervisor = _ensure_state()
    selected_job_id = job_id
    if selected_job_id is not None:
        selected_job_id, err = _resolve_job_id_reference(
            safety, _store_, supervisor, selected_job_id,
        )
        if err is not None:
            return _wrapper_failure(safety, ValueError(err), ResultToolResponse)
    if since < 0:
        return _wrapper_failure(
            safety,
            ValueError("since must be a non-negative integer"),
            ResultToolResponse,
            job_id=selected_job_id,
            since=since,
        )

    try:
        if selected_job_id is None:
            records = [
                current
                for record in supervisor.list_sessions(limit=None)
                if (current := supervisor.status(record.job_id)) is not None
                and current.status in _RESULT_JOB_STATUSES
            ]
            if not records:
                return _wrapper_failure(
                    safety,
                    ValueError("no finished jobs found; run agy_sessions to inspect recent jobs"),
                    ResultToolResponse,
                )
            record = max(records, key=_job_record_recency)
            selected_job_id = record.job_id
        else:
            record = supervisor.status(selected_job_id)
            if record is None:
                return _wrapper_failure(
                    safety,
                    ValueError(f"job_id {selected_job_id!r} not found"),
                    ResultToolResponse,
                    job_id=selected_job_id,
                )

        if record.status == "running":
            return _wrapper_failure(
                safety,
                ValueError(
                    f"job_id {selected_job_id!r} is still running; use agy_status or agy_read",
                ),
                ResultToolResponse,
                job_id=selected_job_id,
            )
        if record.status not in _RESULT_JOB_STATUSES:
            return _wrapper_failure(
                safety,
                ValueError(f"job_id {selected_job_id!r} has no finished result"),
                ResultToolResponse,
                job_id=selected_job_id,
            )

        events = supervisor.read_events(selected_job_id)
        payload = [
            event.model_dump(mode="json")
            for event in (events[since:] if include_events else [])
        ]
        result_text = _result_text_from_events(
            events,
            status=record.status,
            fallback=record.error,
            safety=safety,
        )
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(
            safety,
            exc,
            ResultToolResponse,
            job_id=selected_job_id,
        )

    return ResultToolResponse(
        success=True,
        job_id=selected_job_id,
        record=record,
        result_text=result_text,
        include_events=include_events,
        since=since,
        events=payload,
        count=len(payload),
    )


# ---------------------------------------------------------------------------
# Tool: agy_cancel — signal a running job
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_cancel",
    description=(
        "Signal a running job to stop by full job_id or unique prefix. Returns "
        "``success=True, signalled=True`` if the worker was alive, "
        "``signalled=False`` if it was unknown / already finished."
    ),
)
def agy_cancel_tool(job_id: str) -> CancelToolResponse:
    config, safety, _store_, supervisor = _ensure_state()
    resolved_job_id, err = _resolve_job_id_reference(safety, _store_, supervisor, job_id)
    if err is not None:
        return _wrapper_failure(
            safety, ValueError(err), CancelToolResponse,
        )
    try:
        signalled = supervisor.cancel(resolved_job_id or job_id)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(
            safety, exc, CancelToolResponse, job_id=resolved_job_id,
        )
    return CancelToolResponse(
        success=True,
        job_id=safety.redact(resolved_job_id or job_id),
        signalled=signalled,
    )


# ---------------------------------------------------------------------------
# Tool: agy_sessions — list recent jobs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_sessions",
    description=(
        "List recent jobs, newest first. ``limit`` defaults to 50; pass 0 "
        "for the full list."
    ),
)
def agy_sessions_tool(limit: int = 50) -> SessionsToolResponse:
    config, safety, _store_, supervisor = _ensure_state()
    if limit < 0:
        return _wrapper_failure(
            safety,
            ValueError("limit must be a non-negative integer"),
            SessionsToolResponse,
        )
    effective: int | None = limit if limit > 0 else None
    try:
        records = supervisor.list_sessions(limit=effective)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, SessionsToolResponse)
    return SessionsToolResponse(
        success=True,
        count=len(records),
        records=list(records),
        integration=_doctor_integration_snapshot(),
    )


# ---------------------------------------------------------------------------
# Tools: durable notifications for local/ordinary ChatGPT automations
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_notifications",
    description=(
        "List pending terminal Antigravity notifications from the durable outbox. "
        "Scheduled tasks should stay silent when count is zero, report each returned "
        "notification, then acknowledge it with agy_notification_ack."
    ),
)
def agy_notifications_tool(cursor: int = 0, limit: int = 20) -> NotificationsToolResponse:
    _config_, safety, _store_, _supervisor_ = _ensure_state()
    assert _notification_outbox is not None
    assert _notification_dispatcher is not None
    try:
        page = _notification_outbox.list_pending(cursor=cursor, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, NotificationsToolResponse)
    return NotificationsToolResponse(
        success=True,
        notifications=page["notifications"],
        count=page["count"],
        next_cursor=page["next_cursor"],
        has_more=page["has_more"],
        telegram_configured=_notification_dispatcher.telegram_configured,
    )


@mcp.tool(
    name="agy_notification_ack",
    description=(
        "Acknowledge a durable Antigravity notification after it has been shown to "
        "the user. Repeated acknowledgement is safe and idempotent."
    ),
)
def agy_notification_ack_tool(notification_id: str) -> NotificationAckToolResponse:
    _config_, safety, _store_, _supervisor_ = _ensure_state()
    assert _notification_outbox is not None
    if not _NOTIFICATION_ID_PATTERN.fullmatch(notification_id):
        return _wrapper_failure(
            safety, ValueError("invalid notification_id"), NotificationAckToolResponse,
        )
    try:
        item = _notification_outbox.acknowledge(notification_id)
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, NotificationAckToolResponse)
    if item is None:
        return _wrapper_failure(
            safety, ValueError("notification_id not found"), NotificationAckToolResponse,
            notification_id=notification_id,
        )
    return NotificationAckToolResponse(
        success=True,
        notification_id=notification_id,
        acknowledged=True,
    )


@mcp.tool(
    name="agy_active_jobs",
    description=(
        "List running Antigravity jobs with bounded, safe operational progress. "
        "Use this when the user asks what Antigravity is doing but has no job id."
    ),
)
def agy_active_jobs_tool(limit: int = 20) -> ActiveJobsToolResponse:
    _config_, safety, _store_, supervisor = _ensure_state()
    if not 1 <= limit <= 100:
        return _wrapper_failure(
            safety, ValueError("limit must be between 1 and 100"), ActiveJobsToolResponse,
        )
    try:
        active = [item for item in supervisor.list_sessions(limit=None) if item.status == "running"]
        active = active[:limit]
        subscription_count = len(_subscription_store.list_all()) if _subscription_store else 0
        jobs = []
        for record in active:
            try:
                events = supervisor.read_events(record.job_id)
            except Exception:  # noqa: BLE001
                events = []
            jobs.append(
                {
                    "record": record.model_dump(mode="json"),
                    "progress": _build_progress_summary(
                        record,
                        events,
                        safety,
                        event_subscription_count=subscription_count,
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, ActiveJobsToolResponse)
    return ActiveJobsToolResponse(success=True, jobs=jobs, count=len(jobs))


# ---------------------------------------------------------------------------
# Tool: agy_doctor — environment probe
# ---------------------------------------------------------------------------


def _doctor_integration_snapshot() -> dict[str, Any]:
    """Report bridge-side integration readiness without assuming host capabilities.

    A registered server tool or webhook subscription does not prove that an
    ordinary ChatGPT chat has a tool binding or can be automatically resumed.
    """
    manager = getattr(mcp, "_tool_manager", None)
    registered = getattr(manager, "_tools", {})
    tool_names = sorted(registered) if isinstance(registered, dict) else []

    subscription_count: int | None = None
    if _subscription_store is not None:
        try:
            subscription_count = len(_subscription_store.list_all())
        except Exception:  # noqa: BLE001 - diagnostics must not fail the doctor
            pass

    pending_chat: bool | None = None
    pending_telegram: bool | None = None
    if _notification_outbox is not None:
        try:
            pending_chat = bool(_notification_outbox.list_pending(limit=1)["count"])
            pending_telegram = bool(
                _notification_outbox.list_telegram_undelivered(limit=1)["count"]
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "server_registered_tools": tool_names,
        "server_registered_tool_count": len(tool_names),
        "event_subscription_count": subscription_count,
        "telegram_configured": bool(
            _notification_dispatcher and _notification_dispatcher.telegram_configured
        ),
        "telegram_bot_initialized": bool(
            _notification_dispatcher and _notification_dispatcher.bot is not None
        ),
        "pending_chat_notifications": pending_chat,
        "pending_telegram_deliveries": pending_telegram,
        "client_visible_tools": "not_observable_from_mcp_server",
        "chat_auto_resume_verified": False,
    }


@mcp.tool(
    name="agy_doctor",
    description=(
        "Run capability + auth + session-store probes. Returns a structured "
        "report (no secrets) suitable for surfacing to a user via MCP. "
        "Pass ``force_refresh=true`` to drop the cached binary probe (use "
        "after upgrading the underlying agy / gemini CLI without restarting "
        "the MCP server)."
    ),
)
async def agy_doctor_tool(force_refresh: bool = False) -> DoctorToolResponse:
    config, safety, store, _supervisor_ = _ensure_state()
    try:
        agy_adapter, gemini_adapter = _ensure_adapters(force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001 - never let init crash the tool
        return _wrapper_failure(safety, exc, DoctorToolResponse, version=__version__)
    try:
        report = await anyio.to_thread.run_sync(
            partial(
                run_doctor,
                config=config,
                safety=safety,
                agy_adapter=agy_adapter,
                gemini_adapter=gemini_adapter,
                session_store=store,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, DoctorToolResponse, version=__version__)
    return DoctorToolResponse(
        success=True,
        report={**report.to_dict(), "integration": _doctor_integration_snapshot()},
        version=__version__,
    )


# ---------------------------------------------------------------------------
# Tool: agy_install_skill — write scaffold skill into target dirs
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_install_skill",
    description=(
        "Install the agy-mcp collaboration skill bundle into one or more "
        "agent platforms. ``targets`` may include 'claude', 'codex', "
        "'antigravity', or 'all' (default expands to all three). User "
        "scope writes to ``~/.claude/skills/``, ``~/.agents/skills/``, "
        "and ``~/.agy/skills/`` respectively; project scope writes to "
        "``.claude/skills/``, ``.agents/skills/``, and "
        "``.antigravity/skills/`` under the supplied ``project_root``. "
        "Antigravity's wrapper-owned ``~/.agy/`` is used in user scope "
        "because the standing rule 'do not write under ~/.gemini/' rules "
        "out the Antigravity CLI's own state directory. ``force=False`` "
        "(the default) skips files whose on-disk body already matches; "
        "``force=True`` rewrites every file unconditionally."
    ),
)
def agy_install_skill_tool(
    targets: list[str] | None = None,
    scope: SkillScope = "user",
    project_root: str | None = None,
    force: bool = False,
) -> InstallSkillToolResponse:
    config, safety, _store_, _supervisor_ = _ensure_state()
    if scope not in ("user", "project"):
        return _wrapper_failure(
            safety,
            ValueError(f"scope must be 'user' or 'project', got {scope!r}"),
            InstallSkillToolResponse,
        )
    chosen_targets = targets if targets else ["all"]
    # Defence-in-depth at the tool boundary. The installer's own
    # ``_expand_targets`` does the same string / allow-list check
    # again, but the MCP entry point gets reachable from arbitrary
    # caller configs, so we re-validate here AND apply
    # ``_MAX_INSTALL_TARGETS`` (which ``_expand_targets`` does not
    # know about). Phase 7 R1 arch P2-2: keep both layers but flag
    # the duplication so future contributors don't relax one and
    # leave the other exposed.
    if not isinstance(chosen_targets, list):
        return _wrapper_failure(
            safety,
            ValueError("targets must be a list of strings"),
            InstallSkillToolResponse,
        )
    if len(chosen_targets) > _MAX_INSTALL_TARGETS:
        return _wrapper_failure(
            safety,
            ValueError(
                f"targets exceeds {_MAX_INSTALL_TARGETS} entries "
                f"({len(chosen_targets)} given)",
            ),
            InstallSkillToolResponse,
        )
    cleaned: list[SkillTarget] = []
    for t in chosen_targets:
        if not isinstance(t, str):
            return _wrapper_failure(
                safety,
                ValueError("targets entries must be strings"),
                InstallSkillToolResponse,
            )
        if t not in _ALLOWED_TARGETS:
            return _wrapper_failure(
                safety,
                ValueError(f"unknown skill target: {t!r}"),
                InstallSkillToolResponse,
            )
        cleaned.append(t)  # type: ignore[arg-type]
    try:
        result = install_skills(
            targets=cleaned,
            scope=scope,
            project_root=Path(project_root) if project_root else None,
            safety=safety,
            force=force,
        )
    except Exception as exc:  # noqa: BLE001
        return _wrapper_failure(safety, exc, InstallSkillToolResponse)
    payload = result.to_dict()
    return InstallSkillToolResponse(
        success=bool(payload.get("success", False)),
        error=payload.get("error"),
        warnings=list(payload.get("warnings", [])),
        installed=list(payload.get("installed", [])),
    )


# ---------------------------------------------------------------------------
# Tool: agy_purge — drop session-store rows older than ``days``
# ---------------------------------------------------------------------------


@mcp.tool(
    name="agy_purge",
    description=(
        "Delete session-store job directories whose mtime is older than "
        "``days``. Returns the removed job ids and a coarse count of "
        "remaining jobs. ``days`` must be a positive integer <= "
        f"{_PURGE_MAX_DAYS}; zero or negative values are rejected to "
        "avoid an accidental wipe via off-by-one config."
    ),
)
def agy_purge_tool(days: int = 30) -> PurgeToolResponse:
    config, safety, store, _supervisor_ = _ensure_state()
    if not isinstance(days, int) or isinstance(days, bool):
        return _wrapper_failure(
            safety,
            ValueError("days must be a positive integer"),
            PurgeToolResponse,
        )
    if days <= 0:
        return _wrapper_failure(
            safety,
            ValueError("days must be > 0 (refusing to wipe the entire store)"),
            PurgeToolResponse,
            days=days,
        )
    if days > _PURGE_MAX_DAYS:
        return _wrapper_failure(
            safety,
            ValueError(
                f"days exceeds the {_PURGE_MAX_DAYS}-day cap; pick a smaller cutoff"
            ),
            PurgeToolResponse,
            days=days,
        )
    try:
        removed = store.purge_older_than(days)
        remaining_records = store.list_jobs(limit=None)
    except Exception as exc:  # noqa: BLE001 - top-level guard
        return _wrapper_failure(
            safety, exc, PurgeToolResponse, days=days,
        )
    return PurgeToolResponse(
        success=True,
        days=days,
        removed=[safety.redact(job_id) for job_id in removed],
        removed_count=len(removed),
        remaining=len(remaining_records),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@mcp.tool(name="agy_result_page", description=(
    "Read a finished job's full final response without summarization or truncation. "
    "Repeat with next_offset until has_more is false. Secret redaction remains enabled. "
    "Use agy_read for all intermediate messages."
))
def agy_result_page_tool(
    job_id: str, offset: int = 0, limit: int = 16000,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 64000:
        return {"success": False, "error": "offset must be non-negative; limit must be 1..64000"}
    result = agy_result_tool(job_id=job_id)
    if not result.success:
        return {"success": False, "error": result.error}
    text = result.result_text
    record = result.record
    status = getattr(record, "status", "completed" if text else "unknown") if record else ("completed" if text else "unknown")

    if not text:
        err_msg = (getattr(record, "error", None) if record else None) or f"job ended with status '{status}' and produced no output"
        return {
            "success": False,
            "status": status,
            "error": err_msg,
            "job_id": result.job_id,
            "session_id": getattr(record, "session_id", None) if record else None,
            "text": "",
            "offset": 0,
            "next_offset": 0,
            "total_chars": 0,
            "has_more": False,
            "redaction_enabled": True,
        }

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        return {"success": False, "error": "result changed; restart pagination at offset 0"}
    if offset > len(text):
        return {"success": False, "error": "offset exceeds result length"}
    end = min(offset + limit, len(text))

    if status in (
        "timeout",
        "failed",
        "cancelled",
        "upstream_error",
        "quota_exhausted",
        "no_final_response",
    ):
        err_msg = (getattr(record, "error", None) if record else None) or f"job ended with status '{status}'"
        return {
            "success": False,
            "status": status,
            "error": err_msg,
            "partial": True,
            "job_id": result.job_id,
            "session_id": getattr(record, "session_id", None) if record else None,
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end,
            "total_chars": len(text),
            "has_more": end < len(text),
            "sha256": digest,
            "redaction_enabled": True,
        }

    return {
        "success": True,
        "status": "completed",
        "job_id": result.job_id,
        "session_id": getattr(record, "session_id", None) if record else None,
        "text": text[offset:end],
        "offset": offset,
        "next_offset": end,
        "total_chars": len(text),
        "has_more": end < len(text),
        "sha256": digest,
        "redaction_enabled": True,
    }


@mcp.tool()
def agy_pipeline_start(
    name: str,
    steps: list[dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Start an autonomous sequential pipeline of Antigravity tasks (Codex step chaining)."""
    orch = _ensure_orchestrator_state()
    try:
        pipe = orch.create_pipeline(name=name, steps=steps, session_id=session_id)
        return {
            "success": True,
            "pipeline_id": pipe.pipeline_id,
            "name": pipe.name,
            "status": pipe.status,
            "steps_count": len(pipe.steps),
            "created_at": pipe.created_at,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@mcp.tool()
def agy_pipeline_status(pipeline_id: str) -> dict[str, Any]:
    """Check the status of an autonomous pipeline and its constituent steps."""
    orch = _ensure_orchestrator_state()
    pipe = orch.get_pipeline(pipeline_id)
    if not pipe:
        return {"success": False, "error": f"Pipeline not found: {pipeline_id}"}
    return {
        "success": True,
        "pipeline": pipe.model_dump(mode="json"),
    }


@mcp.tool()
def agy_pipeline_cancel(pipeline_id: str) -> dict[str, Any]:
    """Cancel an active autonomous pipeline and any currently running job."""
    orch = _ensure_orchestrator_state()
    cancelled = orch.cancel_pipeline(pipeline_id)
    return {
        "success": cancelled,
        "pipeline_id": pipeline_id,
        "status": "cancelled" if cancelled else "not_found",
    }


# Optional Windows/Linux multi-device layer. Legacy agy_* tools remain local.
register_device_tools(mcp)

# Stable facade: these two MCP schemas stay constant when the broker's
# allowlisted devices/actions change.  Legacy tools remain available.
register_capability_tools(
    mcp,
    {
        "start": agy_start_tool,
        "status": agy_status_tool,
        "read": agy_read_tool,
        "result": agy_result_tool,
        "cancel": agy_cancel_tool,
        "sessions": agy_sessions_tool,
        "doctor": lambda: asyncio.run(agy_doctor_tool()),
        "interactive_open": interactive_open,
        "interactive_send": interactive_send,
        "btw": interactive_btw,
        "interactive_snapshot": interactive_snapshot,
    },
)


def run(transport: str = "stdio", port: int = 8765) -> None:
    """Start the FastMCP stdio server."""

    # Materialise singletons before stdio takes over so any configuration
    # error surfaces with a stack trace rather than a closed pipe.
    _ensure_state()
    restrict_tools(mcp)
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("unsupported transport")
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    # Remote access belongs behind an authenticated TLS gateway or secure tunnel.
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = port
    mcp.run(transport=transport)


__all__ = [
    "agy_active_jobs_tool",
    "agy_cancel_tool",
    "agy_continue_tool",
    "agy_doctor_tool",
    "agy_install_skill_tool",
    "agy_notification_ack_tool",
    "agy_notifications_tool",
    "agy_pipeline_cancel",
    "agy_pipeline_start",
    "agy_pipeline_status",
    "agy_purge_tool",
    "agy_read_tool",
    "agy_result_tool",
    "agy_sessions_tool",
    "agy_start_tool",
    "agy_status_tool",
    "agy_tool",
    "mcp",
    "run",
    "_ensure_events_state",
    "_ensure_orchestrator_state",
]
