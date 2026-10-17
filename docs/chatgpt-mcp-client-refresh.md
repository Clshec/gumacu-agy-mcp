# ChatGPT MCP Tool-Snapshot Refresh — Release Runbook

**Status:** required release gate for Gumacu AGY MCP and its Netcup/Windows multi-device deployment.  
**Last verified source:** OpenAI Help Center, October 17, 2026.

## Why this exists

Updating the repository, restarting the server, redeploying the SSH tunnel, or confirming `agy_doctor` is **not sufficient proof** that ChatGPT exposes newly added/modified MCP tools. Published custom MCP connections can use an **approved ("frozen") tool/action snapshot**. Server-side `tools/list` and ChatGPT's allowed actions are **distinct states**.

OpenAI official documentation:
- [Connecting custom MCP servers in ChatGPT](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
- [Troubleshooting plugins and apps](https://help.openai.com/en/articles/20001497-troubleshooting-plugins-apps-in-chatgpt)

OpenAI currently documents refreshing the connection's action list through **Workspace settings → Apps → ... → Action control → Refresh** for supported Business/Enterprise/Edu custom MCP connections. Review changed schemas; approve/enable new actions (new actions may default to disabled), and publish the update when required. UI labels and permissions vary; personal/developer connections may offer a different Manage/reconnect/recreate flow. Do **not** claim that everyone has the same menu or that a server restart refreshes ChatGPT's approved snapshot. There is no verified server-side action in this project that automatically refreshes ChatGPT's approved tool list.

## Required acceptance sequence after EVERY main release

1. **Repository:** Record released commit SHA(s), changed tool names/signatures, and changed deployment pins. Run unit/CI tests. Do not infer running code from a GitHub commit alone.
2. **Runtime:** Deploy the correct commit to Netcup. Verify the host MCP process, Gateway (`127.0.0.1:8766`), and SSH tunnel/Windows Agent where applicable. Preserve OAuth, Telegram secrets, and session stores.
3. **MCP protocol:** Query the **same authenticated production MCP endpoint ChatGPT connects to**, using an MCP client/inspector and `tools/list`. Record its tool-name inventory and compare expected tool names and schemas. `agy_doctor`'s `server_registered_tools` list only proves internal registration; it is not a substitute for the production endpoint `tools/list`.
4. **ChatGPT action snapshot:** Open the existing custom connection's management UI. For supported workspace-managed connections, use **Action control → Refresh**, review diffs, enable new actions and publish/apply the change. In other plan/UI variants, look for an equivalent refresh/manage/reconnect path. If unavailable, report that the user/admin must update/recreate the connection via the available UI — do not attempt to manipulate ChatGPT's backend or pretend a tunnel restart forces refresh.
5. **Client acceptance:** Start a **new ChatGPT chat/message** with the refreshed app/connection selected and confirm visibility and invocation of key actions (not just count). At minimum: `agy_devices`, `agy_device_start`, `agy_device_status`, `agy_device_read`, `agy_device_result`, `agy_device_continue`, and `agy_device_doctor`. Respect tool permissions and execute only a harmless Windows test, then verify read/result, and Telegram terminal notification. Existing chats can retain older tool snapshots.
6. **Sign off separately:** Record `REPO_OK`, `RUNTIME_OK`, `MCP_ENDPOINT_OK`, `CHATGPT_REFRESHED`, `WINDOWS_E2E_OK`, `TELEGRAM_OK` as **PASS / FAIL / PENDING**, with evidence and date. Do not declare overall completion until all user-required gates pass. If user/admin action is required, report exactly one scoped UI step rather than masking it as a server problem.

### Diagnosis matrix

| Observation | Likely next action |
|---|---|
| Repo contains tool, runtime does not | Deploy correct SHA, verify process/runtime paths |
| Runtime doctor lists tool, production `tools/list` does not | Fix MCP export/transport/proxy/deployment; inspect logs and endpoint authorization |
| Production `tools/list` contains tool, ChatGPT lacks it | Refresh/approve ChatGPT action snapshot via supported app/connection UI; new chat; client-side action |
| ChatGPT sees tool but call fails | Check tool permissions, schema drift, authentication, gateway, Windows heartbeat and logs |
| Telegram configured but no delivered notification | Test server-side outbox, dispatcher/retry and terminal event; never paste secrets |

### Historical issue (October 17, 2026)

Netcup MCP internally registered **26 tools** according to `agy_doctor`, while this ChatGPT conversation surfaced only **10 legacy tools**, missing `agy_devices` and other multi-device actions. This discrepancy requires verification through the production MCP endpoint, followed by the ChatGPT-side refresh. **Historical observations are not current deployment acceptance.**

## Ongoing reminder

`.github/workflows/chatgpt-mcp-refresh-reminder.yml` runs on pushes to `main` and manual dispatch. It creates/updates an **open GitHub issue** as a release gate with the commit SHA. Each release must be checked, and the issue closed **only after client acceptance**. A GitHub issue/workflow cannot remotely force ChatGPT to refresh its tool snapshot.

**Security:** Never store API tokens, connection secrets, bot credentials, OAuth materials, user-specific private endpoint URLs or unredacted logs in issues, workflow summaries or commits.
