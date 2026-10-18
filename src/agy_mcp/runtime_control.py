"""Explicit device-local Antigravity shutdown; never stop the broker or agent."""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import time
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


def _linux_identity(pid: int) -> tuple[str, str] | None:
    try:
        entry = Path("/proc") / str(pid)
        return (str((entry / "exe").resolve(strict=True)),
                (entry / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _linux_targets() -> dict[int, tuple[str, str]]:
    targets: dict[int, tuple[str, str]] = {}
    processes = {}
    protected = {os.getpid(), os.getppid()}
    roots = (Path.home() / ".gemini/antigravity-cli/bin",)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in protected:
            continue
        try:
            identity = _linux_identity(int(entry.name))
            if identity is None:
                continue
            executable = Path(identity[0])
            ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            processes[int(entry.name)] = (ppid, identity)
            if executable == Path("/usr/local/bin/agy") or any(executable.is_relative_to(root) for root in roots):
                targets[int(entry.name)] = identity
        except (OSError, ValueError, IndexError):
            continue
    # Descendants belong to these jobs, but ancestors (SSH/MCP/bot) never do.
    while True:
        children = {pid: identity for pid, (ppid, identity) in processes.items()
                    if ppid in targets and pid not in targets and pid not in protected}
        if not children:
            break
        targets.update(children)
    return targets


def _shutdown_linux(dry_run: bool) -> dict:
    targets = _linux_targets()
    service = ["/usr/bin/systemctl", "is-active", "antigravity-manager.service"]
    manager_active = subprocess.run(service, capture_output=True, timeout=10).returncode == 0
    if not dry_run:
        # Explicit stop suppresses Restart=always; it does not disable autostart.
        stopped = subprocess.run(["/usr/bin/systemctl", "stop", "antigravity-manager.service"],
                                 capture_output=True, timeout=30)
        if stopped.returncode != 0:
            return {"success": False, "error": "could not stop the Antigravity manager"}
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid, identity in targets.items():
                if _linux_identity(pid) == identity:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            if sig == signal.SIGTERM:
                time.sleep(1)
        manager_active = subprocess.run(service, capture_output=True, timeout=10).returncode == 0
    remaining = _linux_targets()
    remaining.update({pid: identity for pid, identity in targets.items() if _linux_identity(pid) == identity})
    if not dry_run and not remaining and not manager_active:
        root = os.environ.get("AGY_MCP_SESSION_ROOT")
        if root:
            (Path(root) / ".antigravity-closed").touch()
    return {"success": dry_run or (not remaining and not manager_active), "dry_run": dry_run,
            "targeted": [{"pid": pid, "name": Path(value[0]).name} for pid, value in targets.items()],
            "remaining": [{"pid": pid, "name": Path(value[0]).name} for pid, value in remaining.items()],
            "manager_active": manager_active}


def reopen_linux_if_closed() -> None:
    root = os.environ.get("AGY_MCP_SESSION_ROOT")
    marker = Path(root) / ".antigravity-closed" if root else None
    if platform.system() == "Linux" and marker and marker.exists():
        result = subprocess.run(["/usr/bin/systemctl", "start", "antigravity-manager.service"],
                                capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError("could not reopen the Antigravity manager")
        marker.unlink(missing_ok=True)


def shutdown_antigravity(*, confirm: bool = False, dry_run: bool = False) -> dict:
    if not dry_run and confirm is not True:
        return {"success": False, "error": "explicit confirmation required"}
    if platform.system() == "Linux":
        return _shutdown_linux(dry_run)
    if platform.system() != "Windows":
        return {"success": False, "error": "unsupported operating system"}
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
