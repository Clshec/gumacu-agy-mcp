# Branch consolidation — 2026-11-11

The repository is consolidated into `main`. All remote branch tips are retained
in its commit ancestry before the redundant branch refs are deleted.

Three remaining divergent branches required an explicit architecture decision:

| Branch | Resolution |
| --- | --- |
| `feature/btw-interactive-tmux-20261019` | History merged with the current tree retained. Its Antigravity TUI/model execution was removed by the Alf Control migration. PR #13 also records an unresolved progress-reporting limitation. The experimental feature is not reactivated. |
| `feature/chatgpt-workspace-provider` | History merged with the current tree retained. Its model-backed provider, Telegram integration and legacy gateway are superseded by direct Alf Control tools. |
| `fix/mcp-linux-standalone-after-windows-agent-removal-20261027` | History merged with the current launcher retained. The old fix made Telegram/gateway environment files optional; the current launcher eliminates those dependencies entirely. Its old agent acceptance commands and env-file tests are obsolete. |

These three use Git's `ours` merge strategy deliberately: their commits and
original source remain accessible through merge parents, while the supported
Alf Control implementation and verified Windows connection repair stay active.
This records supersession, not activation or live validation of old features.
All other branch tips were already ancestors of the supported branch.

Deleting remote branches does not remove these commits. Do not restore removed
agent executors simply to reproduce an older branch's file tree. See
[connection diagnostics](connection-diagnostics.md) for the supported acceptance
checks and recovery procedure.
