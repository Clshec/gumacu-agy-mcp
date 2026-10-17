param(
    [Parameter(Mandatory=$true)][string]$RuntimeDirectory,
    [Parameter(Mandatory=$true)][string]$TunnelId,
    [string]$Alias = 'gumacu-netcup',
    [string]$StarterScript = ''
)

$ErrorActionPreference = 'Stop'
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$StarterScript = if ([string]::IsNullOrWhiteSpace($StarterScript)) {
    Join-Path $runtime 'start-private-tunnel.ps1'
} else {
    [IO.Path]::GetFullPath($StarterScript)
}
$client = Join-Path $runtime 'bin\tunnel-client.exe'
$logDirectory = Join-Path $runtime 'watchdog'
$logFile = Join-Path $logDirectory 'watchdog.log'

if ($TunnelId -notmatch '^tunnel_[a-zA-Z0-9_-]+$') { throw 'Invalid tunnel ID' }
if ($Alias -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid runtime alias' }
if (-not (Test-Path -LiteralPath $client)) { throw 'tunnel-client.exe was not found.' }
if (-not (Test-Path -LiteralPath $StarterScript)) { throw 'Tunnel starter script was not found.' }

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

try {
    $statusText = & $client runtimes status $Alias --json 2>$null
    $status = $statusText | ConvertFrom-Json
    if ($status.process_running -and $status.healthy -and $status.ready) { exit 0 }

    & $StarterScript -RuntimeDirectory $runtime -TunnelId $TunnelId | Out-Null
    $statusText = & $client runtimes status $Alias --json 2>$null
    $status = $statusText | ConvertFrom-Json
    if (-not ($status.process_running -and $status.healthy -and $status.ready)) {
        throw 'Tunnel runtime did not become healthy and ready.'
    }

    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) restarted $Alias successfully"
} catch {
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) restart failed: $($_.Exception.Message)"
    exit 1
}
