"""Explicit device-local Antigravity shutdown; never stop the broker or agent."""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

WINDOWS_CONTROL = r'''
$ErrorActionPreference = 'Stop'
$roots = @(
    (Join-Path $env:LOCALAPPDATA 'agy'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Antigravity'),
    (Join-Path $env:USERPROFILE '.gemini\antigravity-cli\bin')
)
function Targets {
    @(Get-CimInstance Win32_Process | Where-Object {
        $exe = $_.ExecutablePath
        if (-not $exe -or $_.ProcessId -eq $PID) { return $false }
        foreach ($root in $roots) {
            if ($exe.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
        return $false
    })
}
$targets = @(Targets)
if ($env:AGY_SHUTDOWN_DRY_RUN -ne '1') {
    foreach ($target in $targets) {
        $current = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$target.ProcessId)
        if ($current -and $current.CreationDate -eq $target.CreationDate -and $current.ExecutablePath -eq $target.ExecutablePath) {
            & taskkill.exe /PID ([string]$target.ProcessId) /T /F 2>$null | Out-Null
        }
    }
}
$remaining = @(Targets)
# Unknown/custom install paths must not produce a false "fully closed" result.
$unknown = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(Antigravity|agy|language_server_windows.*)\.exe$' -and
    $_.ProcessId -notin @($remaining | ForEach-Object {$_.ProcessId})
})
if ($env:AGY_SHUTDOWN_DRY_RUN -ne '1') { $remaining += $unknown }
@{success=($env:AGY_SHUTDOWN_DRY_RUN -eq '1' -or $remaining.Count -eq 0);
  dry_run=($env:AGY_SHUTDOWN_DRY_RUN -eq '1');
  targeted=@($targets | ForEach-Object {@{pid=$_.ProcessId;name=$_.Name}});
  remaining=@($remaining | ForEach-Object {@{pid=$_.ProcessId;name=$_.Name}})} | ConvertTo-Json -Depth 5 -Compress
'''


def shutdown_antigravity(*, confirm: bool = False, dry_run: bool = False) -> dict:
    if not dry_run and confirm is not True:
        return {"success": False, "error": "explicit confirmation required"}
    if platform.system() != "Windows":
        return {"success": False, "error": "full application shutdown is currently supported on Windows only"}
    cancelled = []
    if not dry_run:
        from agy_mcp.server import _ensure_state
        _, _, store, supervisor = _ensure_state()
        for record in store.list_jobs(limit=0):
            if record.status == "running" and supervisor.cancel(record.job_id):
                cancelled.append(record.job_id)
    env = {**os.environ, "AGY_SHUTDOWN_DRY_RUN": "1" if dry_run else "0"}
    # Fixed script and trusted install roots only; no caller command or path.
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", WINDOWS_CONTROL],
                            env=env, capture_output=True, text=True, timeout=25)
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {"success": False, "error": "could not verify Antigravity process shutdown", "cancelled_jobs": cancelled}
    response["cancelled_jobs"] = cancelled
    return response
