param(
    [Parameter(Mandatory=$true)][string]$RuntimeDirectory,
    [Parameter(Mandatory=$true)][string]$TunnelId,
    [string]$Alias = 'gumacu-netcup',
    [string]$TaskName = 'Gumacu Secure MCP Tunnel Watchdog',
    [int]$IntervalMinutes = 2
)

$ErrorActionPreference = 'Stop'
if ($IntervalMinutes -lt 1) { throw 'IntervalMinutes must be at least 1.' }

$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$source = Join-Path $PSScriptRoot 'ensure-private-tunnel.ps1'
$watchdog = Join-Path $runtime 'ensure-private-tunnel.ps1'
if (-not (Test-Path -LiteralPath $source)) { throw 'Watchdog source script was not found.' }
Copy-Item -LiteralPath $source -Destination $watchdog -Force

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = Get-Acl -LiteralPath $watchdog
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $watchdog -AclObject $acl

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden' +
    ' -File "' + $watchdog + '"' +
    ' -RuntimeDirectory "' + $runtime + '"' +
    ' -TunnelId "' + $TunnelId + '"' +
    ' -Alias "' + $Alias + '"'
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
$periodic = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $periodic) `
    -Settings $settings -Principal $principal `
    -Description 'Restarts a private Secure MCP Tunnel after logon or an SSH/server interruption.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName
