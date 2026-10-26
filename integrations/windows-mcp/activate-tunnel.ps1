param(
    [Parameter(Mandatory=$true)][string]$RuntimeDirectory,
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$Alias = 'gumacu-netcup',
    [string]$WatchdogTask = 'Gumacu Secure MCP Tunnel Watchdog'
)
$ErrorActionPreference = 'Stop'
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$install = [IO.Path]::GetFullPath($InstallDirectory).Replace('\','/')
if ($install -match '["\s]') { throw 'Use an installation path without spaces' }
$profilePath = Join-Path $runtime ('profiles\' + $Alias + '.yaml')
$client = Join-Path $runtime 'bin\tunnel-client.exe'
$starter = Join-Path $runtime 'start-private-tunnel.ps1'
foreach ($file in @($profilePath, $client, $starter, "$install/bridge.py", "$install/backends.json", "$install/.venv/Scripts/python.exe")) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Required file missing: $file" }
}
$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
$binding = @($profile.mcp.commands | Where-Object { $_.channel -eq 'main' })
if ($binding.Count -ne 1) { throw 'Expected one main stdio binding' }
$tunnelId = $profile.control_plane.tunnel_id
$backup = Join-Path $runtime ('backups\windows-mcp-' + (Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath $profilePath -Destination (Join-Path $backup 'profile.yaml')
$task = Get-ScheduledTask -TaskName $WatchdogTask -ErrorAction SilentlyContinue
$resumeWatchdog = $task -and $task.Settings.Enabled
if ($resumeWatchdog) { Disable-ScheduledTask -InputObject $task | Out-Null }
try {
    & $client runtimes stop $Alias --json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the old managed runtime' }
    $binding[0].command = "$install/.venv/Scripts/python.exe $install/bridge.py --config $install/backends.json"
    $profile | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $profilePath -Encoding UTF8
    & $starter -RuntimeDirectory $runtime -TunnelId $tunnelId | Out-Null
    if (-not $?) { throw 'Managed runtime startup failed' }
    $status = (& $client runtimes status $Alias --json | ConvertFrom-Json)
    if (-not ($status.process_running -and $status.healthy -and $status.ready)) { throw 'Managed runtime is not healthy and ready' }
    [pscustomobject]@{Ready=$true;Alias=$Alias;BackupDirectory=$backup;RuntimePID=$status.process.pid} | ConvertTo-Json
} catch {
    $reason = $_.Exception.Message
    & $client runtimes stop $Alias --json | Out-Null
    Copy-Item -LiteralPath (Join-Path $backup 'profile.yaml') -Destination $profilePath -Force
    & $starter -RuntimeDirectory $runtime -TunnelId $tunnelId | Out-Null
    throw "Activation failed; the previous profile was restored. $reason"
} finally {
    if ($resumeWatchdog) { Enable-ScheduledTask -TaskName $WatchdogTask | Out-Null }
}
