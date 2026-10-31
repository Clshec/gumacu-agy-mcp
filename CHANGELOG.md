# Changelog

## 0.2.0 — Alf Control

* Rename the distribution, implementation, MCP server and display metadata.
* Remove Antigravity/Gemini execution, agents, model jobs/pipelines, agent-specific
  Telegram handling, collaboration skills, installer code and configuration.
* Preserve direct SSH and native Windows tools, persistent SSH session behavior,
  transport initialization recovery, and no-replay behavior.
* Keep the three generic facade aliases and minimal legacy launch shims.
* Support an operator-owned registry of separately secured SSH devices/providers.
* Remove obsolete environment-file dependencies from the MCP launcher and replace
  model-auth readiness with direct transport readiness.
* Retain original MIT attribution and existing App/deployment identities.

Earlier history is available in Git. This branch does not deploy itself.
