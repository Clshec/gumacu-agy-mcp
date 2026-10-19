# Gumacu Interactive Operations — ChatGPT is the execution manager

**Default workflow for user-driven Netcup operations in ordinary ChatGPT.**
Use the stable MCP `agy_capabilities` and `agy_dispatch` tools: no new
client-side tool registrations, no Work handoff, and no autonomous multi-hour
Antigravity prompt by default.

## Execution cycle

1. **ChatGPT creates a persistent operation** with
   `agy_dispatch(device_id="linux-netcup", action="operation_create",
   arguments={"title":"Odoo rollout"})`.
   Save the returned `operation_id`.
2. **ChatGPT chooses exactly one bounded step** and calls
   `agy_dispatch(... action="operation_step", arguments={
      "operation_id":"op_...", "request_id":"odoo-inspect-0001",
      "label":"Read-only preflight",
      "prompt":"Inspect the current Odoo revision and pod health. Report the
                concrete evidence; do not write or deploy.",
      "cd":"/approved/workspace", "mode":"ask", "allow_write":false
   })`.
   The MCP supervisor **detaches** this Antigravity task and returns a real
   `job_id`. The operation checkpoint persists without a ChatGPT connection.
3. **ChatGPT inspects real output** by calling
   `agy_dispatch(... action="operation_collect",
   arguments={"operation_id":"op_..."})` until terminal. While still
   running it returns `next_action="operation_collect"` and the `job_id`.
   Regular `agy_status` and `agy_read` remain available for progress.
   On completion, `result_text`, `job_status`, and
   `next_action="operation_review"` are returned.
4. **ChatGPT performs an independent verification** where relevant (for
   example `git rev-parse`, Fleet status, or Kubernetes deployment state)
   and explicitly records a review:
   `agy_dispatch(... action="operation_review", arguments={
     "operation_id":"op_...", "accepted":true,
     "evidence":"Deployment hash matched Git and 3 replicas became ready"})`.
   A failed job cannot be approved. When verification fails, submit
   `accepted:false` with the failure evidence, and the *next* step is a
   corrective action, not a silent retry.
5. **Only then** choose a fresh bounded next step and issue a new
   `operation_step` with a distinct `request_id`. Repeat until satisfied.
   Finally call `operation_finish` after review.
6. `operation_status` and `operation_list` recover the saved workflow
   and step/job IDs in a separate ChatGPT conversation.

The exact next Antigravity prompt is NEVER generated or run automatically
by this module. ChatGPT remains responsible for decomposing work, examining
results, making decisions and confirming completion.

## Concurrent execution, resumption and safety

- Up to **eight running micro-steps**, in *different operations*, can
  coexist. A ninth gets a retryable busy response; none of the eight jobs is
  killed or preempted. The existing Supervisor is another eight-slot gate,
  including jobs started from other tools. Work in different Git worktrees
  where appropriate; resource and filesystem conflicts are not solved merely
  by allowing eight sessions.
- Every micro-step launches through the existing `agy_start` authorization
  path, respecting remote profiles, approved workspaces, write permissions and
  worktree isolation. There is **no arbitrary unrestricted shell executor**
  in this change.
- In the new no-deadline policy the task timeout is always `0`; no
  `--print-timeout` or wall-clock task cancellation. Network checks can
  have timeouts, and explicit cancellation, agent/provider failures or node
  failures can still terminate work.
- SQLite checkpoints are stored inside `AGY_MCP_SESSION_ROOT`, using
  0700 parent/0600 DB permissions. Prompt text, complete outputs and
  verification evidence are **not duplicated** in the operation database;
  only hashes, job IDs and minimal labels are stored. Original job outputs
  remain in the existing supervised SessionStore.
- A client disconnect cannot delete an accepted background job while its
  owning Supervisor service stays alive. If that service restarts or a node
  crashes, a running job is **not guaranteed** to survive. On an ambiguous
  `launching` state, this engine refuses silent replay. Reconcile reality
  before attempting corrective operations; idempotency keys prevent
  accidental duplicate submissions with the same key.
- `operation_review` records a **ChatGPT/operator judgment** with a hashed
  receipt. This is NOT automatic proof of success; the caller must obtain
  and critically review independent evidence.

## New stable gateway actions

`operation_create(title)`,
`operation_step(operation_id,request_id,label,prompt,cd,mode,allow_write,worktree,operator_token)`,
`operation_collect(operation_id)`,
`operation_review(operation_id,accepted,evidence)`,
`operation_status(operation_id)`,
`operation_list(limit)`, and `operation_finish(operation_id)`.

They are exposed on `linux-netcup` through the **existing**
`agy_dispatch` tool schema. The tool schemas and legacy Antigravity
sessions are preserved. Multi-device operation execution on Windows requires
a separately authorized broker/agent implementation before being advertised.

The existing autonomous pipeline runner and long-job tools remain available
for intentionally delegated tasks; they are not the preferred default for
ordinary ChatGPT-led operations.
