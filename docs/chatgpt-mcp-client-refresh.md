# Verified ChatGPT MCP tool refresh procedure

Verified on 18 October 2026: 26 tools became 28 in the SAME existing conversation.

## What made the tools visible

1. Deploy the tested commit to the actual Linux MCP Python environment. A GitHub merge alone does not deploy or reload code.
2. Restart the device gateway before reloading MCP, so its authenticated registry publishes current allowed_actions.
3. Reload only the managed gumacu-netcup tunnel/MCP runtime, retaining the tracked stdio_reconnect launcher.
4. In ChatGPT use **Settings → Plugins → netcup → Manage app → Refresh tools**.
5. Inspect the plugin app action list, then the assistant's callable inventory. Allow the update to propagate and recheck. Invoke the new tools through native MCP.

The pre-existing **Allow all** permission was unchanged. Development mode, private tunnel identity and credentials were unchanged. No new chat, reinstall, key replacement or Telegram restart was needed in this rollout. A new conversation is only a fallback if the existing conversation does not receive the refreshed inventory.

This client refresh procedure is consistent with [OpenAI's connection/testing guide](https://developers.openai.com/plugins/deploy/connect-chatgpt). UI labels may change; the verified path above records the observed interface.

## Three different acceptance layers

| Layer | Required proof |
|---|---|
| Server | Actual tools/list contains expected tool names and schemas |
| ChatGPT registration | Plugin detail's app action list contains new tools |
| Assistant | Callable inventory includes new tools AND native connector calls succeed |

Server counts, UI visibility, package version and tunnel ready=true are individually insufficient to prove assistant access.

The verified native calls were:

```text
agy_capabilities({})
agy_dispatch({"device_id":"linux-netcup","action":"doctor","arguments":{}})
```

Discovery reported linux-netcup and windows-main online; dispatch returned success=true and report.healthy=true. Both names appeared in the assistant's actual callable inventory, with 28 Netcup tools total. Production commit: fe0734d8247256ff12f73b6a8b8155b9ac6c525a.

## Safe deployment and tunnel reload

Record source commit, active jobs and service PIDs. Back up source, sessions, broker configuration and launcher privately. Preserve job owner processes; choose a safe reload if MCP owns active workers. Never print credentials.

Deploy the tested commit to /opt/gumacu-agy-mcp/source and its existing venv. Install deploy/run-mcp.sh as the production launcher. It must run python -m agy_mcp.stdio_reconnect. Restart gumacu-agy-gateway.service for broker changes. Keep the independent gumacu-agy-telegram.service running, with exactly one Telegram poller.

In Windows PowerShell, supply the existing runtime directory and use the managed runtime API rather than broad process kills:

```powershell
$client = Join-Path $runtimeDirectory 'bin\tunnel-client.exe'
& $client runtimes stop gumacu-netcup --json | Out-Null
Start-ScheduledTask -TaskName 'Gumacu Secure MCP Tunnel Watchdog'

# Inspect after recovery. The local health port is dynamically assigned.
$status = & $client runtimes status gumacu-netcup --json | ConvertFrom-Json
$status | Select-Object process_running, healthy, ready, mcp_health_url
Invoke-RestMethod -Uri $status.mcp_health_url
```

Run as the credential-owning user; recovery after a Windows reboot requires login. Do not run its DPAPI credential as SYSTEM. See [tunnel recovery](tunnel-recovery.md).

The old runbook's fixed port, broad process termination, mandatory new-chat step and claim that tools/list happens only at tunnel startup are superseded. They are not reliable assumptions for future releases.

## Future updates

### Windows agents must receive policy changes too

On 18 October a Telegram-started Windows session failed with
`remote timeout must be 1..1200 seconds` even though Linux had already accepted
`timeout=0` (unlimited). The installed Windows agent source was still commit
`ddbba53`; Linux was `fe0734d`. Both could report package version `0.1.18`, so
the version string alone did not reveal the drift.

For policy/protocol changes, verify the actual Windows scheduled task action,
its interpreter and imported source checkout. Packaged Codex may use the
per-app LocalCache installation rather than the similarly named ordinary
LocalAppData folder. Back up the real source and settings. Check active Windows
jobs before stopping its agent, retain its state database, credential and SSH
forward, update to the reviewed server-compatible commit, reinstall in that
agent's existing venv, and restart ONLY its scheduled agent/worker processes.
Do not cancel active jobs or restart the ChatGPT tunnel/Telegram poller for a
Windows-only source update.

Run `tests/test_remote_policy.py` and `tests/test_timeout_regression.py` using
the agent interpreter. Then execute a harmless native
`agy_device_start(device_id="windows-main", timeout=0)` and verify its stored
timeout, terminal result, and broker/Telegram delivery as applicable. Record
the source SHA on BOTH devices as a release gate. Do not reduce the timeout to
1200 as a workaround; that hides an outdated agent and restores an unwanted cap.

| Change | Procedure |
|---|---|
| New public MCP tool or changed schema/description | Deploy/reload MCP, refresh ChatGPT registration, verify native invocation |
| New device/action behind stable agy_capabilities and agy_dispatch | Deploy broker/agent implementation and allowlist; call discovery again |
| Internal implementation with unchanged definitions | Reload affected component and verify behavior |
| Broken transport or missing initialization | Repair connectivity/initialization; schema refresh alone cannot repair it |

Keep the two facade schemas stable. New backend actions still require implementation, authorization and deployment. They do not automatically become new top-level MCP tools. See [dynamic gateway](dynamic-capability-gateway.md).

A tools.listChanged capability or notification is not proof that this client refreshed. Likewise /health/mcp may lack protocol observations while native calls work. Do not infer success or failure from that signal alone. The release gate's successful GitHub Action is a reminder, not proof that someone completed its manual acceptance checklist.

## التفسير العملي بالعربي

الإجراء الذي استخدمناه هو **Refresh tools** في إعدادات إضافة netcup، بعد نشر النسخة وإعادة اتصال MCP. صلاحية **Allow all** كانت موجودة أصلًا ولم نغيّرها. ظهرت الأداتان داخل نفس المحادثة ثم استُدعيتا عبر MCP فعلًا.

في التحديثات القادمة: إضافة أداة MCP جديدة تتطلب تحديث التسجيل والتحقق. أما إضافة قدرة خلف الأداتين الثابتتين فتتطلب نشرها ثم استدعاء agy_capabilities، دون تغيير تعريف أدوات MCP. لا توجد مهلة انتشار ثابتة مضمونة؛ نتحقق من الظهور والاستدعاء بدل الاكتفاء بالانتظار.
