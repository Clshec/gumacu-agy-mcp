<#
.SYNOPSIS
    Idempotent helper to cleanly restart the OpenAI private developer MCP tunnel on Windows.
.DESCRIPTION
    Terminates ONLY the matching stale tunnel-client and child ssh processes for the gumacu-netcup
    tunnel, re-executes start-private-tunnel.ps1, and verifies local readiness at :49202.
    Safe and non-destructive: ignores unrelated SSH sessions and background device worker tunnels.
#>
param(
    [Parameter(Mandatory=$false)][string]$RuntimeDirectory = '',
    [Parameter(Mandatory=$false)][string]$TunnelId = 'tunnel_6ace3926c4e48191aeced4035a6997e5',
    [string]$Alias = 'gumacu-netcup',
    [int]$ReadinessTimeoutSec = 30
)

$ErrorActionPreference = 'Stop'

if ($TunnelId -notmatch '^tunnel_[a-zA-Z0-9_-]+$') {
    throw 'Invalid tunnel ID'
}
if ($Alias -notmatch '^[a-zA-Z0-9_-]+$') {
    throw 'Invalid runtime alias'
}

# Auto-detect RuntimeDirectory if omitted
if ([string]::IsNullOrWhiteSpace($RuntimeDirectory)) {
    $candidates = @(
        "C:\Users\TechnoHouse\Documents\Codex\2026-10-13\referenced-chatgpt-conversation-this-is-an\work\gumacu-tunnel-runtime",
        "$env:USERPROFILE\Documents\Codex\2026-10-13\referenced-chatgpt-conversation-this-is-an\work\gumacu-tunnel-runtime",
        "$PSScriptRoot\..\gumacu-tunnel-runtime"
    )
    foreach ($cand in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($cand) -and (Test-Path -LiteralPath (Join-Path $cand 'bin\tunnel-client.exe'))) {
            $RuntimeDirectory = $cand
            break
        }
    }
}

if ([string]::IsNullOrWhiteSpace($RuntimeDirectory) -or -not (Test-Path -LiteralPath $RuntimeDirectory)) {
    throw "RuntimeDirectory was not specified or not found: $RuntimeDirectory"
}

$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$client = Join-Path $runtime 'bin\tunnel-client.exe'
$starter = Join-Path $runtime 'start-private-tunnel.ps1'
if (-not (Test-Path -LiteralPath $starter)) {
    $starter = Join-Path $PSScriptRoot 'start-private-tunnel.ps1'
}

if (-not (Test-Path -LiteralPath $client)) { throw "tunnel-client.exe was not found in $runtime\bin" }
if (-not (Test-Path -LiteralPath $starter)) { throw "start-private-tunnel.ps1 was not found" }

Write-Host "[*] Inspecting running processes for $Alias..."

# 1. Identify and cleanly terminate ONLY matching gumacu-netcup tunnel and its child ssh.exe
# Strictly filter to avoid terminating unrelated SSH sessions, git, or device tunnels
$matchingProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq 'tunnel-client.exe' -and ($_.CommandLine -like "*$Alias*" -or $_.CommandLine -like "*$runtime*")) -or
    ($_.Name -eq 'ssh.exe' -and $_.CommandLine -like "*run-mcp.sh*" -and $_.CommandLine -like "*159.195.65.10*")
}

if ($matchingProcesses) {
    Write-Host "[*] Terminating matching stale tunnel process tree: $($matchingProcesses.ProcessId -join ', ')..."
    foreach ($p in $matchingProcesses) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Warning "Could not stop process $($p.ProcessId): $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 3
} else {
    Write-Host "[*] No active matching $Alias process was running."
}

# 2. Restart using starter script
Write-Host "[*] Starting fresh tunnel session for $Alias ($TunnelId)..."
try {
    & $starter -RuntimeDirectory $runtime -TunnelId $TunnelId
} catch {
    Write-Error "Failed to start private tunnel: $($_.Exception.Message)"
    throw
}

# 3. Health-check readiness endpoint
Write-Host "[*] Verifying readiness endpoint at http://127.0.0.1:49202/health/readiness..."
$deadline = (Get-Date).AddSeconds($ReadinessTimeoutSec)
$ready = $false

while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:49202/health/readiness" -TimeoutSec 2 -ErrorAction Stop
        if ($resp.status -eq 'healthy' -or $resp.ready -eq $true -or $resp -match 'ready') {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $ready) {
    # Check fallback status via CLI
    try {
        $statusJson = & $client runtimes status $Alias --json 2>$null | ConvertFrom-Json
        if ($statusJson.process_running -and $statusJson.ready) {
            $ready = $true
        }
    } catch {}
}

if ($ready) {
    Write-Host "[+] Tunnel $Alias refreshed successfully and reporting READY."
    exit 0
} else {
    Write-Error "[-] Tunnel readiness check timed out after $ReadinessTimeoutSec seconds."
    exit 1
}
