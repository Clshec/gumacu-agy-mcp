# Gumacu Interactive Operations — eight concurrent sessions

This is the default **workflow recommendation** for managing Netcup from an
ordinary ChatGPT conversation: ChatGPT chooses a bounded step, runs it through
the MCP service, inspects real results, verifies the observed state, and then
chooses the next step. Long autonomous tasks are explicitly delegated to
`agy_start` or a persistent pipeline.

## Concurrency and scheduling

- **Eight simultaneously running supervised jobs** per long-lived
  `Supervisor` instance are supported by the existing semaphore.
- **Independent jobs remain independent:** finishing or failing one job does
  not cancel another.
- For **pipelines**, an additional step encountering
  `supervisor busy: 8 concurrent...` is **left pending in persisted state**;
  the orchestrator retries it on the next scheduler tick. Pending steps are
  not failed or cancelled merely because capacity is exhausted.
- **Direct `agy_start` calls** above capacity currently receive an explicit
  `server busy` response and must be retried by the caller. They are not
  automatically enqueued. Do not report such a call as accepted or running.
- The eight-slot guard is currently per supervisor process. A single host-wide
  cross-process lease/queue is a separate hardening task; do not run multiple
  independent MCP worker processes expecting an aggregate limit of eight.
- This is an **admission cap, not a promise of available CPU or RAM**. On a
  pressured 8-vCPU / 16-GB node, configure appropriate application-level
  resource constraints before making eight heavy tasks fully active.

## No job deadlines

- MCP `agy`, `agy_continue`, and `agy_start` sanitize even an accidentally
  supplied `timeout: 240` or `timeout: 900` to `timeout: 0` before creating
  the backend request.
- All new pipeline steps default to zero. Previously saved positive timeout
  values are **not enforced** when a pipeline step starts.
- **No `--print-timeout` argument** is passed to Antigravity on these paths,
  and the subprocess wrapper does not apply a wall-clock execution deadline.
- Short bounded timeouts on HTTP communications, TUI probes, worker thread
  shutdown, and test assertions are **not task deadlines**. They remain
  necessary to avoid hanging control-plane requests.
- Manual cancellation, backend failures, provider quotas, service crashes,
  operating-system OOM, and infrastructure outages can still end a job.
  We cannot guarantee that any given task will complete successfully.

## Persistence and resumes

- Pipeline definitions, completed steps, and waiting steps are persisted in
  the supervisor session directory. After ChatGPT exits, a still-running MCP
  **service process** continues executing accepted jobs.
- The ordinary ChatGPT conversation itself is not running in the background;
  autonomous multi-step execution requires the Orchestrator service.
- **Important:** the existing supervisor uses daemon threads inside its
  service process. If that process stops or the host reboots, currently running
  jobs are not guaranteed to survive. Persisting the pipeline state does not
  magically preserve its subprocess. Do not auto-replay destructive commands.
  A future durable worker service with explicit recovery/idempotency is
  required to survive MCP process termination safely.
- Before performing any mutating operation: establish its owner workspace,
  show the command/change plan, enforce write permissions, use a Git worktree
  when applicable, and verify actual Git/Fleet/Kubernetes state afterward.

## Interactive tools

Use `agy` for quick, bounded question/answer execution and `agy_start`
for longer supervised operations; read `agy_status` and `agy_result`.
For multi-step autonomous runs, use the persisted pipeline tools. Distinguish
the Antigravity `SESSION_ID` from the MCP job identifier `job_id`.

This mode is a **policy and bounded scheduler**, not a claim of a host-global
command executor or a fully durable cross-process queue. Such new functionality
must be added with separate authorization checks, verifiable state transitions,
and tests; never expose unrestricted shell commands through the MCP interface.
